from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ReporteSemanalV13_Orden_Fecha_Desc import (
    consultar_eventos_abiertos,
    generar_reporte,
)


app = FastAPI(
    title="Reporte Semanal de Eventos Logísticos",
    version="1.0.0"
)

# Evita que dos reportes se generen simultáneamente
reporte_lock = Lock()


@app.get("/")
def inicio():
    return {
        "servicio": "Reporte semanal de eventos logísticos",
        "estado": "disponible"
    }


@app.get("/health")
def health():
    return {
        "status": "ok"
    }


@app.get("/reporte-semanal")
def reporte_semanal():

    if not reporte_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Ya se está generando un reporte. Intente nuevamente en unos segundos."
        )

    try:
        print("================================================")
        print("Solicitud de generación de reporte recibida")
        print("================================================")

        # 1. Consultar los eventos abiertos
        eventos = consultar_eventos_abiertos()

        print(f"Eventos consultados: {len(eventos)}")

        # 2. Generar el PDF
        ruta_pdf = Path(generar_reporte(eventos))

        # 3. Verificar que efectivamente se haya creado
        if not ruta_pdf.exists():
            raise RuntimeError(
                f"No se encontró el PDF generado: {ruta_pdf}"
            )

        print(f"PDF generado correctamente: {ruta_pdf}")

        # 4. Enviar el PDF al navegador
        return FileResponse(
            path=str(ruta_pdf),
            media_type="application/pdf",
            filename="Reporte_Semanal_Eventos_Logisticos.pdf",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            }
        )

    except Exception as error:

        print("ERROR GENERANDO EL REPORTE:")
        print(str(error))

        raise HTTPException(
            status_code=500,
            detail=f"No fue posible generar el reporte: {error}"
        )

    finally:
        reporte_lock.release()
