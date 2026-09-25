# reporte-logistico
Creación de reportelogistico

API FastAPI (Render) que genera el reporte semanal de eventos logísticos en PDF.

## Flujo desde Experience Builder
El botón "Reporte" abre la página de espera **https://dantedamian.github.io/reporte-logistico/**
(`docs/index.html`, publicada con GitHub Pages). La página:
1. Consulta `/health` hasta que el servicio de Render despierta (sin mostrar la pantalla de Render).
2. Pide el reporte con `POST /reportes` y consulta su avance en `GET /reportes/{id}`.
3. Descarga el PDF desde `GET /reportes/{id}/pdf`.

## Endpoints
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Verificación de disponibilidad |
| POST | `/reportes` | Crea un reporte o reutiliza el que está en curso (o uno de hace < 2 min) |
| GET | `/reportes/{id}` | Estado: `procesando`, `listo` o `error` |
| GET | `/reportes/{id}/pdf` | Descarga el PDF |
| GET | `/reporte-semanal` | Ruta anterior; si ya hay un reporte en curso, espera ese mismo y lo entrega |

Variable de entorno opcional `CORS_ORIGINS`: orígenes adicionales permitidos, separados por coma.
