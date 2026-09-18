from pathlib import Path
from threading import Lock
import gc
import os
import time
import traceback

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ReporteSemanalV13 import consultar_eventos_abiertos, generar_reporte

app = FastAPI(
    title="Reporte Semanal de Eventos Logísticos",
    version="15.0-render512-pil",
)

reporte_lock = Lock()


def log(msg: str):
    print(f"[REPORTE] {msg}", flush=True)


def memoria_max_mb():
    try:
        import resource
        valor = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return valor / 1024.0  # Linux/Render: KB -> MB
    except Exception:
        return None


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


@app.get("/reporte-semanal")
def reporte_semanal():
    if not reporte_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Ya se está generando un reporte. Intente nuevamente en unos segundos.",
        )

    inicio = time.time()
    try:
        log(f"Solicitud recibida. PID={os.getpid()}")
        mem = memoria_max_mb()
        if mem is not None:
            log(f"Memoria máxima al inicio: {mem:.1f} MB")

        eventos = consultar_eventos_abiertos()
        log(f"Eventos consultados: {len(eventos)}")
        mem = memoria_max_mb()
        if mem is not None:
            log(f"Memoria máxima después de consulta: {mem:.1f} MB")

        log("Generando PDF con mapas/gráficas Pillow...")
        ruta_pdf = Path(generar_reporte(eventos))
        if not ruta_pdf.exists():
            raise RuntimeError(f"No se encontró el PDF generado: {ruta_pdf}")

        mem = memoria_max_mb()
        if mem is not None:
            log(f"Memoria máxima al finalizar: {mem:.1f} MB")
        log(
            f"PDF listo: {ruta_pdf.name} | "
            f"{ruta_pdf.stat().st_size / (1024*1024):.2f} MB | "
            f"{time.time()-inicio:.1f} s"
        )

        del eventos
        gc.collect()

        return FileResponse(
            path=str(ruta_pdf),
            media_type="application/pdf",
            filename="Reporte_Semanal_Eventos_Logisticos.pdf",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    except HTTPException:
        raise
    except Exception as exc:
        log(f"ERROR: {exc}")
        log(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        gc.collect()
        reporte_lock.release()
