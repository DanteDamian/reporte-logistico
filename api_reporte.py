from pathlib import Path
from threading import Lock, Thread
import gc
import os
import time
import traceback
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from ReporteSemanalV13 import OUTPUT_DIR, consultar_eventos_abiertos, generar_reporte

app = FastAPI(
    title="Reporte Semanal de Eventos Logísticos",
    version="16.0-render512-jobs",
)

# Página de espera (docs/index.html) publicada en GitHub Pages.
# Se pueden agregar más orígenes con la variable de entorno CORS_ORIGINS (separados por coma).
ORIGENES = ["https://dantedamian.github.io"] + [
    o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

NOMBRE_DESCARGA = "Reporte_Semanal_Eventos_Logisticos.pdf"
REUTILIZAR_SEGUNDOS = 120   # un clic dentro de 2 min recibe el mismo PDF recién generado
LIMPIAR_SEGUNDOS = 3600     # trabajos y PDFs de más de 1 hora se eliminan
MAX_ESPERA_SEGUNDOS = 600   # espera máxima de /reporte-semanal

# Registro de trabajos en memoria. Render free = 1 instancia; un solo reporte a la vez
# para no pasar de 512 MB.
TRABAJOS: dict = {}
_candado = Lock()
_actual = None


def log(msg: str):
    print(f"[REPORTE] {msg}", flush=True)


def memoria_max_mb():
    try:
        import resource
        valor = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return valor / 1024.0  # Linux/Render: KB -> MB
    except Exception:
        return None


def _log_memoria(etapa: str):
    mem = memoria_max_mb()
    if mem is not None:
        log(f"Memoria máxima {etapa}: {mem:.1f} MB")


# ------------------------------------------------------------------
# Generación en segundo plano
# ------------------------------------------------------------------
def _ejecutar(job_id: str):
    trabajo = TRABAJOS[job_id]
    inicio = time.time()
    try:
        log(f"Trabajo {job_id[:8]} iniciado. PID={os.getpid()}")
        _log_memoria("al inicio")

        trabajo["mensaje"] = "Consultando eventos abiertos…"
        eventos = consultar_eventos_abiertos()
        log(f"Eventos consultados: {len(eventos)}")
        _log_memoria("después de consulta")

        trabajo["mensaje"] = f"Generando PDF con {len(eventos)} eventos (mapas y gráficas)…"
        log("Generando PDF con mapas/gráficas Pillow...")
        ruta_pdf = Path(generar_reporte(eventos))
        if not ruta_pdf.exists():
            raise RuntimeError(f"No se encontró el PDF generado: {ruta_pdf}")

        del eventos
        _log_memoria("al finalizar")
        log(
            f"PDF listo: {ruta_pdf.name} | "
            f"{ruta_pdf.stat().st_size / (1024*1024):.2f} MB | "
            f"{time.time()-inicio:.1f} s"
        )
        trabajo.update(estado="listo", ruta=str(ruta_pdf), mensaje="Reporte generado.", fin=time.time())
    except Exception as exc:
        log(f"ERROR: {exc}")
        log(traceback.format_exc())
        trabajo.update(estado="error", error=str(exc), mensaje="Error generando el reporte.", fin=time.time())
    finally:
        gc.collect()


def _limpiar():
    ahora = time.time()
    for jid in [j for j, t in TRABAJOS.items() if j != _actual and ahora - t["inicio"] > LIMPIAR_SEGUNDOS]:
        TRABAJOS.pop(jid, None)
    try:
        for pdf in Path(OUTPUT_DIR).glob("*.pdf"):
            if ahora - pdf.stat().st_mtime > LIMPIAR_SEGUNDOS:
                pdf.unlink(missing_ok=True)
    except Exception as exc:
        log(f"No se pudieron limpiar PDFs antiguos: {exc}")


def obtener_o_crear_trabajo():
    """Devuelve (job_id, reutilizado). Si ya hay un reporte en curso (o uno recién
    terminado), se entrega ese mismo en vez de rechazar la solicitud."""
    global _actual
    with _candado:
        _limpiar()
        t = TRABAJOS.get(_actual) if _actual else None
        if t and (
            t["estado"] == "procesando"
            or (t["estado"] == "listo" and time.time() - t["fin"] < REUTILIZAR_SEGUNDOS
                and Path(t["ruta"]).exists())
        ):
            return _actual, True

        job_id = uuid.uuid4().hex
        TRABAJOS[job_id] = {
            "estado": "procesando", "mensaje": "En cola…", "inicio": time.time(),
            "fin": None, "ruta": None, "error": None,
        }
        _actual = job_id
    Thread(target=_ejecutar, args=(job_id,), daemon=True).start()
    return job_id, False


def _respuesta_pdf(ruta: str):
    return FileResponse(
        path=ruta,
        media_type="application/pdf",
        filename=NOMBRE_DESCARGA,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


def _trabajo_o_404(job_id: str):
    t = TRABAJOS.get(job_id)
    if not t:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado (el servicio pudo reiniciarse).")
    return t


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------
@app.get("/")
def inicio():
    return {
        "servicio": "Reporte semanal de eventos logísticos",
        "estado": "disponible",
        "perfil": "Render 512 MB - Pillow",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/reportes")
def crear_reporte():
    job_id, reutilizado = obtener_o_crear_trabajo()
    t = TRABAJOS[job_id]
    return {"job_id": job_id, "estado": t["estado"], "reutilizado": reutilizado}


@app.get("/reportes/{job_id}")
def estado_reporte(job_id: str):
    t = _trabajo_o_404(job_id)
    return {
        "job_id": job_id,
        "estado": t["estado"],          # procesando | listo | error
        "mensaje": t["mensaje"],
        "error": t["error"],
        "segundos": round((t["fin"] or time.time()) - t["inicio"]),
    }


@app.get("/reportes/{job_id}/pdf")
def descargar_reporte(job_id: str):
    t = _trabajo_o_404(job_id)
    if t["estado"] != "listo":
        raise HTTPException(status_code=409, detail=f"El reporte está en estado '{t['estado']}'.")
    if not Path(t["ruta"]).exists():
        raise HTTPException(status_code=410, detail="El PDF ya no está disponible. Genere uno nuevo.")
    return _respuesta_pdf(t["ruta"])


@app.get("/reporte-semanal")
def reporte_semanal():
    """URL anterior (compatibilidad). Ya no responde 429: si hay un reporte en curso,
    espera ese mismo resultado y lo entrega."""
    job_id, reutilizado = obtener_o_crear_trabajo()
    if reutilizado:
        log(f"/reporte-semanal se une al trabajo en curso {job_id[:8]}")
    limite = time.time() + MAX_ESPERA_SEGUNDOS
    while time.time() < limite:
        t = TRABAJOS.get(job_id)
        if t is None:
            break
        if t["estado"] == "listo":
            return _respuesta_pdf(t["ruta"])
        if t["estado"] == "error":
            raise HTTPException(status_code=500, detail=t["error"])
        time.sleep(1)
    raise HTTPException(status_code=504, detail="La generación tardó demasiado. Intente nuevamente.")
