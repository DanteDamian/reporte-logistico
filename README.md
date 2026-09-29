# reporte-logistico
Creación de reportelogistico

API FastAPI (Render) que genera el reporte semanal de eventos logísticos en PDF.

## Ocupación portuaria

Al solicitar el PDF se consulta también la tabla pública
[`Vista_Ocupacion/FeatureServer/0`](https://services3.arcgis.com/PpJ89hvCx3B7cBIO/arcgis/rest/services/Vista_Ocupacion/FeatureServer/0).
Se incluyen los siete días calendario más recientes, incluido el día de la
solicitud, en hora Colombia. El PDF contiene dos páginas adicionales: terminales
por zona y operación, y patios de contenedores por zona. Las barras utilizan un
único día de corte (el último con registros del componente) y se agrupan por
zona. Las series temporales promedian todos los registros diarios por operación
en terminales y por zona portuaria en patios. Una zona que solo reportó un día
permanece visible como un punto con su nombre en la leyenda.
No se eliminan respuestas que compartan terminal y fecha, ya que pueden ser
registros válidos; los huecos no equivalen a cero.
La ejecución falla con un mensaje explícito si la consulta al servicio devuelve
una respuesta incompleta o un error, para evitar publicar cifras parciales.

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
