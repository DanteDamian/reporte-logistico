# -*- coding: utf-8 -*-
"""
PASO 37 - Reporte completo institucional.

Integra en un solo PDF:
1. Portada institucional.
2. Resumen ejecutivo con indicadores y gráficas.
3. Detalle de todos los eventos abiertos, agrupados por corredor y departamento.
4. Al inicio de cada corredor, mapa OpenStreetMap con ubicación exacta desde Shape.
5. Registro raster/vector corregido: OSM e incidentes comparten EPSG:3857.
6. Corredores oficiales desde CorredorWaze/FeatureServer/0.
7. Mapa general discreto en el resumen con agrupación visual de eventos.
8. Gráfica de barras con distribución por departamento.

Requisitos:
    pip install requests reportlab matplotlib pillow

Perfil V14 Render512:
- reduce resolución raster a la necesaria para media página;
- generaliza únicamente CorredorWaze en el servidor;
- libera tiles, mosaicos y geometrías inmediatamente;
- evita mantener simultáneamente geometría ArcGIS y paths duplicados;
- fuerza garbage collection entre mapas/gráficas.

Archivo requerido en la misma carpeta:
    logo_mintransporte.png
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from html import escape
from pathlib import Path
from io import BytesIO
import math
import tempfile
import time
import textwrap
import gc

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

# Perfil de renderizado de bajo consumo para Render Free (512 MB).
# Matplotlib documenta que simplificar paths y trocear líneas grandes reduce
# el trabajo del backend Agg al dibujar geometrías densas.
matplotlib.rcParams["path.simplify"] = True
matplotlib.rcParams["path.simplify_threshold"] = 0.55
matplotlib.rcParams["agg.path.chunksize"] = 20000
import requests
from PIL import Image as PILImage

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    Image,
    KeepTogether,
    LongTable,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


# ============================================================
# CONFIGURACIÓN
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
LOGO = BASE_DIR / "logo_mintransporte.png"
OUTPUT_DIR = BASE_DIR / "salida"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SERVICE_QUERY_URL = (
    "https://services3.arcgis.com/PpJ89hvCx3B7cBIO/"
    "arcgis/rest/services/Incidentes_Logistica_vista_Reporte/"
    "FeatureServer/0/query"
)

LAYER_METADATA_URL = SERVICE_QUERY_URL.rsplit("/query", 1)[0]

# Servicio oficial suministrado para dibujar los corredores logísticos.
CORRIDOR_LAYER_URL = (
    "https://services3.arcgis.com/PpJ89hvCx3B7cBIO/"
    "arcgis/rest/services/CorredorWaze/FeatureServer/0"
)
CORRIDOR_QUERY_URL = CORRIDOR_LAYER_URL + "/query"
CORRIDOR_NAME_FIELD = "Name"
EXPECTED_CORRIDOR_GEOMETRY_TYPE = "esriGeometryPolyline"

# Resumen cartográfico. La agrupación es solo visual y no mueve los Shape
# originales en los mapas detallados. 50 km funciona como escala nacional.
SUMMARY_CLUSTER_RADIUS_M = 50_000.0
# Perfil de imágenes reducido: el mapa del resumen se inserta en un recuadro
# pequeño del PDF, por lo que 720x520 px conserva nitidez suficiente sin
# mantener buffers raster innecesariamente grandes en memoria.
SUMMARY_MAP_FIGSIZE = (5.0, 3.6)
SUMMARY_MAP_TARGET_PIXELS = (720, 520)

# OpenStreetMap estándar. La política oficial exige usar este host,
# identificar la aplicación y mostrar atribución visible en cada mapa.
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_USER_AGENT = "MinTransporte-ReporteSemanalEventos/8.0 (Grupo de Logistica)"
OSM_CACHE_DIR = BASE_DIR / ".osm_tile_cache"
OSM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
OSM_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
OSM_MAX_TILES_PER_MAP = 16
OSM_TILE_SIZE = 256
# Solicita un nivel adicional de detalle cuando el número de tiles lo permite.
# Si supera OSM_MAX_TILES_PER_MAP, el algoritmo reduce el zoom automáticamente.
OSM_DETAIL_ZOOM_BONUS = 0

# OpenStreetMap usa Web Mercator. Se consulta el Feature Service en 3857 para
# dibujar exactamente la geometría Shape devuelta por el servicio, sin
# reconstruir coordenadas ni calcular centroides.
WEB_MERCATOR_WKID = 3857
WEB_MERCATOR_ALIASES = {3857, 102100, 102113}
WEB_MERCATOR_HALF_WORLD = 20037508.342789244
EXPECTED_GEOMETRY_TYPE = "esriGeometryPoint"
STRICT_GEOMETRY_VALIDATION = True

# Todos los mapas del PDF usan exactamente el mismo tamaño/aspecto.
# El mapa ocupa aproximadamente media página en el PDF. 900x560 px ya ofrece
# una densidad visual alta en ese tamaño, mientras reduce de forma importante
# los buffers RGBA de Matplotlib frente a 1400x850/200 dpi.
MAP_FIGSIZE = (7.2, 4.45)
MAP_TARGET_PIXELS = (900, 560)
CHART_DPI = 125
MAP_DPI = 125

# Generalización visual de CorredorWaze. outSR=3857 usa metros, por lo que el
# servidor puede reducir vértices antes de enviar la geometría. Los eventos NO
# se generalizan ni se mueven.
CORRIDOR_DETAIL_OFFSET_M = 25.0
CORRIDOR_SUMMARY_OFFSET_M = 250.0
MAP_PANEL_ASPECT = MAP_TARGET_PIXELS[0] / MAP_TARGET_PIXELS[1]

COLOMBIA_TZ = timezone(timedelta(hours=-5))

# Paleta tomada del logo suministrado
NARANJA = "#D7723F"
AMARILLO = "#FFC800"
AZUL = "#003189"
ROJO = "#D80025"
GRIS = "#555555"
GRIS_OSCURO = "#222222"
GRIS_CLARO = "#F3F3F3"
GRIS_LINEA = "#D9D9D9"

FIELDS = [
    "objectid",
    "corredor_logistico",
    "departamento",
    "municipio",
    "segmento_corredor",
    "pr_inicio",
    "pr_fin",
    "Fecha_inicio",
    "FechaSeguimiento",
    "tipo_evento",
    "tipo_cierre",
    "tipo_impacto",
    "Detalle",
    "Seguimiento",
    "SeguimientoPreventivo",
]

DEPARTAMENTOS_NORMALIZADOS = {
    "valle del cauca": "Valle del Cauca",
}

CORREDORES_ETIQUETA = {
    "Bogota_Barranquilla": "Bogotá - Barranquilla",
    "Bogota_Buenaventura_Ipiales": "Bogotá - Buenaventura - Ipiales",
    "Bogota_Cucuta": "Bogotá - Cúcuta",
    "Bogota_PuertoAsis": "Bogotá - Puerto Asís",
    "Bogota_Yopal": "Bogotá - Yopal",
    "Cali_Medellin_Cartagena": "Cali - Medellín - Cartagena",
    "Medellin_Bucaramanga": "Medellín - Bucaramanga",
}

# Relación explícita entre el campo corredor_logistico de incidentes y Name del
# servicio CorredorWaze. No se hace geocodificación ni inferencia geométrica.
# El servicio suministrado contiene Bogotá-Buenaventura, no una geometría
# Bogotá-Buenaventura-Ipiales; por ello ese caso queda marcado como PARCIAL.
CORREDOR_SERVICE_ALIAS = {
    "Bogota_Barranquilla": ("Bogotá-Barranquilla", False),
    "Bogota_Buenaventura_Ipiales": ("Bogotá-Buenaventura", True),
    "Bogota_Cucuta": ("Bogotá-Cúcuta", False),
    "Bogota_PuertoAsis": ("Bogotá-Puerto Asís", False),
    "Bogota_Yopal": ("Bogotá-Yopal", False),
    "Cali_Medellin_Cartagena": ("Cali-Medellín-Cartagena", False),
    "Medellin_Bucaramanga": ("Medellín-Bucaramanga", False),
}


# ============================================================
# UTILIDADES
# ============================================================

def limpiar(valor, vacio="Sin información registrada"):
    if valor is None:
        return vacio
    texto = str(valor).strip()
    return texto if texto else vacio


def tiene_informacion(valor):
    """True cuando el valor contiene información útil para mostrar en el reporte."""
    if valor is None:
        return False
    texto = str(valor).strip()
    if not texto:
        return False
    return texto.casefold() not in {
        "none", "null", "nan", "n/a", "na",
        "sin informacion", "sin información",
        "sin informacion registrada", "sin información registrada",
    }


def normalizar_departamento(valor):
    texto = limpiar(valor, "Sin departamento")
    return DEPARTAMENTOS_NORMALIZADOS.get(texto.casefold(), texto)


def etiqueta_corredor(valor):
    texto = limpiar(valor, "Sin corredor")
    return CORREDORES_ETIQUETA.get(texto, texto.replace("_", " "))


def etiqueta_categoria(valor):
    return limpiar(valor).replace("_", " ")


def fecha_colombia(epoch_ms, vacio="Sin fecha registrada"):
    if epoch_ms in (None, ""):
        return vacio
    dt_utc = datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc)
    return dt_utc.astimezone(COLOMBIA_TZ).strftime("%d/%m/%Y %H:%M")


def pr_texto(valor):
    if valor in (None, ""):
        return "N/D"
    try:
        metros = int(float(valor))
    except (TypeError, ValueError):
        return limpiar(valor, "N/D")
    km = metros // 1000
    resto = metros % 1000
    return f"PR {km}+{resto:03d}"


def texto_parrafo(valor, vacio="Sin información registrada"):
    texto = limpiar(valor, vacio)
    reemplazos = {
        "\u2022": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u00a0": " ",
    }
    for origen, destino in reemplazos.items():
        texto = texto.replace(origen, destino)
    return escape(texto).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")


# ============================================================
# CONSULTA
# ============================================================

def validar_fuente_espacial():
    """
    Verifica en tiempo de ejecución que la capa fuente siga siendo una capa
    puntual. Así el mapa nunca inventa un centroide ni interpreta otro tipo de
    Shape como si fuera un punto.
    """
    respuesta = requests.get(
        LAYER_METADATA_URL,
        params={"f": "json"},
        timeout=60,
        headers={"User-Agent": OSM_USER_AGENT},
    )
    respuesta.raise_for_status()
    data = respuesta.json()

    if "error" in data:
        raise RuntimeError(f"Error consultando metadatos ArcGIS REST: {data['error']}")

    geometry_type = data.get("geometryType")
    if geometry_type != EXPECTED_GEOMETRY_TYPE:
        raise RuntimeError(
            "La capa del reporte no es puntual. "
            f"ArcGIS informa geometryType={geometry_type!r}; se esperaba "
            f"{EXPECTED_GEOMETRY_TYPE!r}. Se detiene el proceso para no asumir "
            "una ubicación que no corresponda al Shape real."
        )

    return data


def _shape_point_3857(geometry):
    """Devuelve exclusivamente el x/y del Shape puntual retornado por ArcGIS."""
    if not isinstance(geometry, dict):
        return None
    if "x" not in geometry or "y" not in geometry:
        return None

    try:
        x = float(geometry["x"])
        y = float(geometry["y"])
    except (TypeError, ValueError):
        return None

    if not (math.isfinite(x) and math.isfinite(y)):
        return None

    limite = WEB_MERCATOR_HALF_WORLD * 1.001
    if not (-limite <= x <= limite and -limite <= y <= limite):
        return None

    return x, y


def consultar_eventos_abiertos():
    # Validación explícita de la geometría declarada por el Feature Service.
    validar_fuente_espacial()

    # Solo eventos abiertos iniciados dentro del año calendario actual en Colombia.
    # El servicio alojado almacena las fechas como UTC; por ello se convierten
    # los límites 01/01 00:00 de Colombia a UTC antes de construir el WHERE.
    anio_actual = datetime.now(COLOMBIA_TZ).year
    inicio_local = datetime(anio_actual, 1, 1, 0, 0, 0, tzinfo=COLOMBIA_TZ)
    fin_local = datetime(anio_actual + 1, 1, 1, 0, 0, 0, tzinfo=COLOMBIA_TZ)
    inicio_utc = inicio_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    fin_utc = fin_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    where_eventos = (
        "evento_cerrado = 'No' "
        f"AND Fecha_inicio >= TIMESTAMP '{inicio_utc}' "
        f"AND Fecha_inicio < TIMESTAMP '{fin_utc}'"
    )

    print(
        f"Filtro de eventos: abiertos y Fecha_inicio dentro de {anio_actual} "
        "(año calendario de Colombia)."
    )

    params = {
        "f": "json",
        "where": where_eventos,
        "outFields": ",".join(FIELDS),
        "returnGeometry": "true",
        # OpenStreetMap usa Web Mercator. La proyección la realiza ArcGIS Server.
        "outSR": WEB_MERCATOR_WKID,
        "returnZ": "false",
        "returnM": "false",
        "resultRecordCount": 1000,
        "orderByFields": "corredor_logistico,departamento,objectid",
    }

    respuesta = requests.get(
        SERVICE_QUERY_URL,
        params=params,
        timeout=60,
        headers={"User-Agent": OSM_USER_AGENT},
    )
    respuesta.raise_for_status()
    data = respuesta.json()

    if "error" in data:
        raise RuntimeError(f"Error ArcGIS REST: {data['error']}")

    # No se asume el sistema de referencia: se valida lo que realmente devolvió
    # el servicio después de solicitar outSR=3857.
    sr = data.get("spatialReference") or {}
    wkids = {sr.get("wkid"), sr.get("latestWkid")}
    wkids.discard(None)
    if not (wkids & WEB_MERCATOR_ALIASES):
        raise RuntimeError(
            "La consulta no devolvió geometría Web Mercator. "
            f"spatialReference recibido: {sr!r}. Se detiene para evitar "
            "posicionar eventos con un SR incorrecto."
        )

    eventos = []
    geometrias_invalidas = []

    for feature in data.get("features", []):
        atributos = dict(feature.get("attributes", {}))
        geometry = feature.get("geometry")
        atributos["_geometry"] = geometry

        if _shape_point_3857(geometry) is None:
            geometrias_invalidas.append(atributos.get("objectid"))

        eventos.append(atributos)

    if not eventos:
        raise RuntimeError("La consulta no devolvió eventos abiertos.")

    if geometrias_invalidas and STRICT_GEOMETRY_VALIDATION:
        ids = ", ".join(str(x) for x in geometrias_invalidas[:20])
        sufijo = "..." if len(geometrias_invalidas) > 20 else ""
        raise RuntimeError(
            "Hay eventos abiertos sin Shape puntual válido en la respuesta del "
            f"Feature Service. OBJECTID: {ids}{sufijo}. "
            "No se genera el reporte para evitar ubicar puntos de forma asumida."
        )

    print(
        f"Geometría validada: {len(eventos) - len(geometrias_invalidas)} de "
        f"{len(eventos)} eventos con Shape puntual Web Mercator."
    )
    return eventos


# ============================================================
# CORREDORES LOGÍSTICOS - FEATURE SERVICE CorredorWaze
# ============================================================

def _paths_corredor_3857(geometry):
    """Convierte los paths ArcGIS devueltos por CorredorWaze a pares X/Y."""
    if not isinstance(geometry, dict):
        return []
    paths = geometry.get("paths")
    if not isinstance(paths, list):
        return []

    salida = []
    for path in paths:
        if not isinstance(path, list) or len(path) < 2:
            continue
        puntos = []
        for vertice in path:
            if not isinstance(vertice, (list, tuple)) or len(vertice) < 2:
                continue
            try:
                x = float(vertice[0])
                y = float(vertice[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(x) and math.isfinite(y):
                puntos.append((x, y))
        if len(puntos) >= 2:
            salida.append(puntos)
    return salida


def _sql_literal_arcgis(texto):
    return "'" + str(texto).replace("'", "''") + "'"


def nombre_corredor_waze(corredor_raw):
    """
    Devuelve el valor exacto que debe filtrarse en CorredorWaze.Name.
    La relación es únicamente textual y explícita; no hay análisis espacial.
    """
    alias = CORREDOR_SERVICE_ALIAS.get(corredor_raw)
    if alias:
        return alias
    return limpiar(corredor_raw, ""), False


def consultar_corredor_waze_por_name(nombre, max_allowable_offset=CORRIDOR_DETAIL_OFFSET_M):
    """
    Consulta únicamente las entidades de CorredorWaze cuyo campo Name coincide
    exactamente con el corredor actual. Devuelve TODOS los segmentos recibidos
    para ese Name, porque un mismo corredor puede estar dividido en muchas
    polilíneas dentro del Feature Service.
    """
    if not nombre:
        return []

    where = f"{CORRIDOR_NAME_FIELD} = {_sql_literal_arcgis(nombre)}"
    params = {
        "f": "json",
        "where": where,
        "outFields": f"OBJECTID,{CORRIDOR_NAME_FIELD}",
        "returnGeometry": "true",
        # OSM y los eventos del reporte se dibujan en Web Mercator.
        # ArcGIS proyecta la geometría del corredor al mismo SR en la consulta.
        "outSR": WEB_MERCATOR_WKID,
        "returnZ": "false",
        "returnM": "false",
        "resultRecordCount": 2000,
        # Reduce vértices en el servidor antes de transferirlos. En EPSG:3857
        # el valor se interpreta en metros.
        "maxAllowableOffset": float(max_allowable_offset),
        "geometryPrecision": 1,
    }

    resp = requests.get(
        CORRIDOR_QUERY_URL,
        params=params,
        timeout=60,
        headers={"User-Agent": OSM_USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(
            f"Error consultando CorredorWaze para Name={nombre!r}: {data['error']}"
        )

    segmentos = []
    for feature in data.get("features", []):
        attrs = feature.get("attributes", {}) or {}
        geometry = feature.get("geometry")
        paths = _paths_corredor_3857(geometry)
        if not paths:
            continue
        # No conservamos geometry Y paths simultáneamente: ambos contienen la
        # misma geometría y duplicaban memoria. Para dibujar solo necesitamos
        # la versión normalizada en paths.
        segmentos.append({
            "OBJECTID": attrs.get("OBJECTID"),
            "Name": attrs.get(CORRIDOR_NAME_FIELD) or nombre,
            "paths": paths,
        })

    print(
        f"CorredorWaze: Name={nombre!r} -> {len(segmentos)} segmento(s) dibujados."
    )
    return segmentos


# ============================================================
# GRÁFICAS
# ============================================================

def estilo_base(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(GRIS_LINEA)
    ax.spines["bottom"].set_color(GRIS_LINEA)
    ax.tick_params(colors=GRIS, labelsize=8)
    ax.grid(axis="x", alpha=0.15)
    ax.set_axisbelow(True)


def grafica_corredores(eventos, ruta):
    """
    Gráfica horizontal apilada:
    cada barra representa un corredor y se subdivide por impacto logístico.
    """
    orden_impacto = ["Critico", "Importante", "Moderado", "Medio", "Minimo"]

    colores_impacto = {
        "Critico": ROJO,
        "Importante": NARANJA,
        "Moderado": AZUL,
        "Medio": AMARILLO,
        "Minimo": "#A7A9AC",
    }

    # Conteo por corredor e impacto
    matriz = defaultdict(Counter)

    for evento in eventos:
        corredor = etiqueta_corredor(evento.get("corredor_logistico"))
        impacto = etiqueta_categoria(evento.get("tipo_impacto"))
        matriz[corredor][impacto] += 1

    # Ordenar corredores por total de eventos, de mayor a menor
    corredores = sorted(
        matriz.keys(),
        key=lambda c: sum(matriz[c].values()),
        reverse=True,
    )

    # Para barh, invertimos para dejar el mayor arriba
    corredores_plot = corredores[::-1]

    fig, ax = plt.subplots(figsize=(7.4, 3.8))

    acumulado = [0] * len(corredores_plot)

    for impacto in orden_impacto:
        valores = [matriz[c].get(impacto, 0) for c in corredores_plot]

        ax.barh(
            corredores_plot,
            valores,
            left=acumulado,
            label=impacto,
            color=colores_impacto[impacto],
        )

        acumulado = [
            base + valor
            for base, valor in zip(acumulado, valores)
        ]

    # Mostrar el total al final de cada barra
    for i, corredor in enumerate(corredores_plot):
        total = sum(matriz[corredor].values())
        ax.text(
            total + 0.25,
            i,
            str(total),
            va="center",
            fontsize=8,
            fontweight="bold",
            color=GRIS,
        )

    ax.set_title(
        "Eventos abiertos por corredor e impacto logístico",
        fontsize=11,
        fontweight="bold",
        pad=8,
    )
    ax.set_xlabel("Cantidad de eventos", fontsize=8)

    estilo_base(ax)

    max_total = max(sum(matriz[c].values()) for c in corredores_plot)
    ax.set_xlim(0, max_total * 1.15)

    handles, labels = ax.get_legend_handles_labels()
    if handles:
        fig.legend(
            handles,
            labels,
            title="Impacto logístico",
            loc="lower center",
            bbox_to_anchor=(0.5, 0.015),
            ncol=min(5, len(handles)),
            fontsize=7,
            title_fontsize=7.5,
            frameon=False,
            columnspacing=1.1,
        )

    # Reserva un área exclusiva para la leyenda; evita que se superponga con
    # el rótulo "Cantidad de eventos".
    fig.tight_layout(rect=[0, 0.19, 1, 1])
    fig.savefig(
        ruta,
        dpi=CHART_DPI,
        facecolor="white",
    )
    plt.close(fig)
    gc.collect()


def grafica_tipo_evento(eventos, ruta):
    contador = Counter(etiqueta_categoria(e.get("tipo_evento")) for e in eventos)
    labels = list(contador.keys())
    valores = list(contador.values())
    paleta = [NARANJA, AZUL, AMARILLO, ROJO][:len(labels)]

    fig, ax = plt.subplots(figsize=(4.0, 3.2))
    wedges, _ = ax.pie(
        valores,
        startangle=90,
        colors=paleta,
        wedgeprops={"width": 0.42, "edgecolor": "white"},
    )

    ax.text(0, 0.08, str(sum(valores)), ha="center", va="center",
            fontsize=18, fontweight="bold", color=NARANJA)
    ax.text(0, -0.16, "eventos", ha="center", va="center",
            fontsize=8, color=GRIS)

    ax.set_title("Tipo de evento", fontsize=11, fontweight="bold")
    fig.legend(
        wedges,
        [f"{lab}: {val}" for lab, val in zip(labels, valores)],
        loc="lower center",
        bbox_to_anchor=(0.5, 0.02),
        fontsize=7.2,
        frameon=False,
        ncol=1,
    )

    fig.tight_layout(rect=[0, 0.18, 1, 1])
    fig.savefig(ruta, dpi=CHART_DPI, facecolor="white")
    plt.close(fig)
    gc.collect()


def grafica_tipo_cierre(eventos, ruta):
    contador = Counter(etiqueta_categoria(e.get("tipo_cierre")) for e in eventos)
    labels = list(contador.keys())
    valores = list(contador.values())
    paleta = [AZUL, NARANJA, ROJO][:len(labels)]

    fig, ax = plt.subplots(figsize=(4.0, 3.2))
    wedges, _ = ax.pie(
        valores,
        startangle=90,
        colors=paleta,
        wedgeprops={"width": 0.42, "edgecolor": "white"},
    )

    ax.text(0, 0.08, str(sum(valores)), ha="center", va="center",
            fontsize=18, fontweight="bold", color=AZUL)
    ax.text(0, -0.16, "eventos", ha="center", va="center",
            fontsize=8, color=GRIS)

    ax.set_title("Tipo de cierre", fontsize=11, fontweight="bold")
    fig.legend(
        wedges,
        [f"{lab}: {val}" for lab, val in zip(labels, valores)],
        loc="lower center",
        bbox_to_anchor=(0.5, 0.02),
        fontsize=7.2,
        frameon=False,
        ncol=1,
    )

    fig.tight_layout(rect=[0, 0.18, 1, 1])
    fig.savefig(ruta, dpi=CHART_DPI, facecolor="white")
    plt.close(fig)
    gc.collect()


def grafica_impacto(eventos, ruta):
    contador = Counter(etiqueta_categoria(e.get("tipo_impacto")) for e in eventos)

    orden = ["Critico", "Importante", "Moderado", "Medio", "Minimo"]
    datos = [(cat, contador.get(cat, 0)) for cat in orden if cat in contador]
    conocidos = {x[0] for x in datos}

    for cat, valor in contador.items():
        if cat not in conocidos:
            datos.append((cat, valor))

    nombres = [x[0] for x in datos][::-1]
    valores = [x[1] for x in datos][::-1]

    colores_impacto = {
        "Critico": ROJO,
        "Importante": NARANJA,
        "Moderado": AZUL,
        "Medio": AMARILLO,
        "Minimo": "#A7A9AC",
    }

    fig, ax = plt.subplots(figsize=(7.2, 3.25))
    barras = ax.barh(
        nombres,
        valores,
        color=[colores_impacto.get(n, NARANJA) for n in nombres],
    )

    ax.set_title("Impacto logístico", fontsize=11, fontweight="bold")
    ax.set_xlabel("Cantidad de eventos", fontsize=8)
    estilo_base(ax)

    for barra, valor in zip(barras, valores):
        ax.text(
            barra.get_width() + 0.18,
            barra.get_y() + barra.get_height() / 2,
            str(valor),
            va="center",
            fontsize=8,
            fontweight="bold",
            color=GRIS,
        )

    ax.set_xlim(0, max(valores) * 1.18)
    fig.tight_layout()
    fig.savefig(ruta, dpi=CHART_DPI, facecolor="white")
    plt.close(fig)
    gc.collect()


def grafica_departamentos_corredor(eventos_corredor, ruta, nombre_corredor):
    """
    Gráfica horizontal apilada por departamento e impacto logístico.

    Está diseñada para mostrarse junto al mapa dentro de una tarjeta del PDF:
    por eso evita repetir el nombre del corredor y prioriza barras, etiquetas
    y leyenda.
    """
    orden_impacto = ["Critico", "Importante", "Moderado", "Medio", "Minimo"]

    colores_impacto = {
        "Critico": ROJO,
        "Importante": NARANJA,
        "Moderado": AZUL,
        "Medio": AMARILLO,
        "Minimo": "#A7A9AC",
    }

    matriz = defaultdict(Counter)
    for evento in eventos_corredor:
        departamento = normalizar_departamento(evento.get("departamento"))
        impacto = etiqueta_categoria(evento.get("tipo_impacto"))
        matriz[departamento][impacto] += 1

    impactos_presentes = {
        impacto
        for conteos in matriz.values()
        for impacto in conteos.keys()
    }
    impactos_extra = sorted(
        impactos_presentes.difference(orden_impacto),
        key=str.casefold,
    )
    orden_final = orden_impacto + impactos_extra

    # Mayor número de eventos arriba.
    departamentos = sorted(
        matriz.keys(),
        key=lambda d: (sum(matriz[d].values()), d.casefold()),
    )

    altura = max(3.6, 0.46 * len(departamentos) + 2.15)
    fig, ax = plt.subplots(figsize=(7.6, altura))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#FBFCFE")

    acumulado = [0] * len(departamentos)
    for impacto in orden_final:
        valores = [matriz[d].get(impacto, 0) for d in departamentos]
        if not any(valores):
            continue
        ax.barh(
            departamentos,
            valores,
            left=acumulado,
            height=0.62,
            label=impacto,
            color=colores_impacto.get(impacto, "#7F7F7F"),
            edgecolor="white",
            linewidth=0.7,
        )
        acumulado = [base + valor for base, valor in zip(acumulado, valores)]

    max_total = max((sum(matriz[d].values()) for d in departamentos), default=1)
    for i, departamento in enumerate(departamentos):
        total = sum(matriz[departamento].values())
        ax.text(
            total + max(0.07, max_total * 0.025),
            i,
            str(total),
            va="center",
            fontsize=8.4,
            fontweight="bold",
            color=GRIS_OSCURO,
        )

    # El título principal lo aporta la tarjeta del PDF.
    ax.set_xlabel("Cantidad de eventos abiertos", fontsize=8.2, labelpad=6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color(GRIS_LINEA)
    ax.tick_params(axis="y", colors=GRIS_OSCURO, labelsize=8.2, length=0)
    ax.tick_params(axis="x", colors=GRIS, labelsize=7.6)
    ax.xaxis.grid(True, alpha=0.18, linewidth=0.7)
    ax.set_axisbelow(True)
    ax.set_xlim(0, max_total * 1.18 if max_total else 1)

    handles, labels = ax.get_legend_handles_labels()
    if handles:
        fig.legend(
            handles,
            labels,
            title="Impacto logístico",
            loc="lower center",
            bbox_to_anchor=(0.5, 0.012),
            ncol=min(5, len(handles)),
            fontsize=7.1,
            title_fontsize=7.5,
            frameon=False,
            handlelength=1.6,
            handletextpad=0.45,
            columnspacing=1.0,
        )

    fig.tight_layout(rect=[0.015, 0.18, 0.985, 0.98])
    fig.savefig(
        ruta,
        dpi=CHART_DPI,
        facecolor="white",
    )
    plt.close(fig)
    gc.collect()


# ============================================================
# MAPAS POR CORREDOR - OPENSTREETMAP + SHAPE DEL FEATURE SERVICE
# ============================================================

def _ajustar_extension_mapa_3857(
    puntos,
    aspecto_panel=MAP_PANEL_ASPECT,
    margen=1.12,
    span_minimo_m=35000.0,
):
    """
    Calcula una extensión a partir de coordenadas reales EPSG:3857.
    Solo añade contexto y ajusta la relación de aspecto; nunca modifica puntos.
    """
    xs = [p[0] for p in puntos]
    ys = [p[1] for p in puntos]

    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    span_x = max(xmax - xmin, span_minimo_m)
    span_y = max(ymax - ymin, span_minimo_m)
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0

    span_x *= margen
    span_y *= margen

    aspecto = span_x / span_y
    if aspecto < aspecto_panel:
        span_x = span_y * aspecto_panel
    elif aspecto > aspecto_panel:
        span_y = span_x / aspecto_panel

    xmin = cx - span_x / 2.0
    xmax = cx + span_x / 2.0
    ymin = cy - span_y / 2.0
    ymax = cy + span_y / 2.0

    limite = WEB_MERCATOR_HALF_WORLD * 0.999999
    return (
        max(-limite, xmin),
        max(-limite, ymin),
        min(limite, xmax),
        min(limite, ymax),
    )


def _tile_indices_para_extension(extent, zoom):
    xmin, ymin, xmax, ymax = extent
    n = 2 ** zoom
    mundo = 2.0 * WEB_MERCATOR_HALF_WORLD
    tam_tile_m = mundo / n

    def tx(x):
        return int(math.floor((x + WEB_MERCATOR_HALF_WORLD) / tam_tile_m))

    def ty(y):
        return int(math.floor((WEB_MERCATOR_HALF_WORLD - y) / tam_tile_m))

    x0 = max(0, min(n - 1, tx(xmin)))
    x1 = max(0, min(n - 1, tx(xmax)))
    y0 = max(0, min(n - 1, ty(ymax)))
    y1 = max(0, min(n - 1, ty(ymin)))
    return x0, x1, y0, y1


def _seleccionar_zoom_osm(extent):
    """
    Selecciona el mayor zoom útil que cabe dentro del límite de tiles.

    El cálculo parte de la resolución objetivo del panel y, cuando es posible,
    suma un nivel de detalle para que nombres de vías y trazados se lean mejor.
    No se descargan tiles fuera de la extensión visible.
    """
    xmin, ymin, xmax, ymax = extent
    span_x = max(1.0, xmax - xmin)
    span_y = max(1.0, ymax - ymin)
    px_w, px_h = MAP_TARGET_PIXELS

    metros_por_pixel_objetivo = max(span_x / px_w, span_y / px_h)
    mundo = 2.0 * WEB_MERCATOR_HALF_WORLD

    zoom_base = int(math.floor(
        math.log2(mundo / (OSM_TILE_SIZE * metros_por_pixel_objetivo))
    ))
    zoom = max(4, min(16, zoom_base + OSM_DETAIL_ZOOM_BONUS))

    while zoom > 4:
        x0, x1, y0, y1 = _tile_indices_para_extension(extent, zoom)
        cantidad = (x1 - x0 + 1) * (y1 - y0 + 1)
        if cantidad <= OSM_MAX_TILES_PER_MAP:
            break
        zoom -= 1

    return zoom


def _ruta_cache_tile(z, x, y):
    return OSM_CACHE_DIR / str(z) / str(x) / f"{y}.png"


def _leer_tile_cache(ruta):
    if not ruta.exists():
        return None
    try:
        with PILImage.open(ruta) as img:
            return img.convert("RGB")
    except Exception:
        return None


def _descargar_tile_osm(z, x, y, session):
    """
    Descarga un tile OSM respetando un caché local mínimo de 7 días.
    Si existe un tile antiguo y la red falla, se usa ese tile en vez de inventar
    un fondo cartográfico diferente.
    """
    ruta = _ruta_cache_tile(z, x, y)
    ruta.parent.mkdir(parents=True, exist_ok=True)

    if ruta.exists():
        edad = time.time() - ruta.stat().st_mtime
        if edad <= OSM_CACHE_TTL_SECONDS:
            tile = _leer_tile_cache(ruta)
            if tile is not None:
                return tile

    url = OSM_TILE_URL.format(z=z, x=x, y=y)
    headers = {"User-Agent": OSM_USER_AGENT}

    try:
        respuesta = session.get(url, headers=headers, timeout=30)
        respuesta.raise_for_status()
        ctype = respuesta.headers.get("Content-Type", "").lower()
        if "image" not in ctype:
            raise RuntimeError(f"OSM devolvió Content-Type={ctype!r}")

        with PILImage.open(BytesIO(respuesta.content)) as img:
            tile = img.convert("RGB")
            tile.save(ruta, format="PNG")
            return tile.copy()

    except Exception as exc:
        # Un tile cacheado sigue siendo preferible a reemplazar OSM por otro
        # mapa base silenciosamente.
        tile = _leer_tile_cache(ruta)
        if tile is not None:
            print(f"Advertencia OSM: usando tile cacheado {z}/{x}/{y}: {exc}")
            return tile
        raise RuntimeError(
            f"No fue posible obtener el tile OpenStreetMap {z}/{x}/{y}: {exc}"
        ) from exc


def descargar_mosaico_osm(extent):
    """Descarga y ensambla únicamente los tiles OSM necesarios para el mapa."""
    zoom = _seleccionar_zoom_osm(extent)
    x0, x1, y0, y1 = _tile_indices_para_extension(extent, zoom)

    cols = x1 - x0 + 1
    rows = y1 - y0 + 1
    if cols * rows > OSM_MAX_TILES_PER_MAP:
        raise RuntimeError(
            f"El mapa requeriría {cols * rows} tiles OSM; el límite configurado "
            f"es {OSM_MAX_TILES_PER_MAP}."
        )

    mosaico = PILImage.new("RGB", (cols * OSM_TILE_SIZE, rows * OSM_TILE_SIZE))

    with requests.Session() as session:
        for fila, y in enumerate(range(y0, y1 + 1)):
            for col, x in enumerate(range(x0, x1 + 1)):
                tile = _descargar_tile_osm(zoom, x, y, session)
                try:
                    mosaico.paste(tile, (col * OSM_TILE_SIZE, fila * OSM_TILE_SIZE))
                finally:
                    # Libera de inmediato el buffer del tile; el mosaico ya tiene
                    # una copia de sus píxeles.
                    try:
                        tile.close()
                    except Exception:
                        pass

    n = 2 ** zoom
    mundo = 2.0 * WEB_MERCATOR_HALF_WORLD
    tam_tile_m = mundo / n

    mosaico_xmin = -WEB_MERCATOR_HALF_WORLD + x0 * tam_tile_m
    mosaico_xmax = -WEB_MERCATOR_HALF_WORLD + (x1 + 1) * tam_tile_m
    mosaico_ymax = WEB_MERCATOR_HALF_WORLD - y0 * tam_tile_m
    mosaico_ymin = WEB_MERCATOR_HALF_WORLD - (y1 + 1) * tam_tile_m

    # IMPORTANTE: internamente mantenemos el orden GIS tradicional
    # (xmin, ymin, xmax, ymax). Matplotlib NO usa ese orden en imshow.
    return mosaico, (mosaico_xmin, mosaico_ymin, mosaico_xmax, mosaico_ymax), zoom


def _extent_imshow_desde_xyxy(extent_xyxy):
    """
    Convierte (xmin, ymin, xmax, ymax) al orden requerido por imshow:
    (left, right, bottom, top).

    Mantener esta conversión explícita evita deformar el mapa base y es
    esencial para que el raster OSM y los Shape EPSG:3857 compartan exactamente
    el mismo sistema de coordenadas en Matplotlib.
    """
    xmin, ymin, xmax, ymax = extent_xyxy
    if not (xmin < xmax and ymin < ymax):
        raise ValueError(f"Extensión OSM inválida: {extent_xyxy!r}")
    return (xmin, xmax, ymin, ymax)


def _webmercator_a_lonlat(x, y):
    """Conversión de control EPSG:3857 -> longitud/latitud WGS84."""
    lon = (x / WEB_MERCATOR_HALF_WORLD) * 180.0
    lat = math.degrees(
        math.atan(math.sinh((y / WEB_MERCATOR_HALF_WORLD) * math.pi))
    )
    return lon, lat


def _validar_registro_osm_shape(registros, basemap_extent, basemap):
    """
    Valida matemáticamente el registro entre Shape y mosaico OSM.

    - cada Shape debe quedar dentro del mosaico descargado;
    - cada punto debe corresponder a un píxel válido del raster;
    - su conversión 3857 -> lon/lat debe producir coordenadas terrestres válidas.

    Esta validación NO mueve ni ajusta los puntos. Si algo no es coherente,
    detiene la generación del mapa para evitar un reporte visualmente erróneo.
    """
    xmin, ymin, xmax, ymax = basemap_extent
    width, height = basemap.size
    errores = []

    for x, y, evento in registros:
        oid = evento.get("objectid")

        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            errores.append(f"OBJECTID {oid}: Shape fuera del mosaico OSM")
            continue

        # XYZ/OSM tiene origen de imagen en la esquina superior izquierda.
        px = (x - xmin) / (xmax - xmin) * width
        py = (ymax - y) / (ymax - ymin) * height
        if not (-1e-6 <= px <= width + 1e-6 and -1e-6 <= py <= height + 1e-6):
            errores.append(f"OBJECTID {oid}: píxel OSM fuera de rango")

        lon, lat = _webmercator_a_lonlat(x, y)
        if not (-180.0 <= lon <= 180.0 and -85.051129 <= lat <= 85.051129):
            errores.append(
                f"OBJECTID {oid}: coordenada Web Mercator inválida "
                f"(lon={lon:.6f}, lat={lat:.6f})"
            )

    if errores:
        muestra = "; ".join(errores[:10])
        sufijo = " ..." if len(errores) > 10 else ""
        raise RuntimeError(
            "Falló la validación de registro Shape/OSM. " + muestra + sufijo
        )

    lons_lats = [_webmercator_a_lonlat(x, y) for x, y, _ in registros]
    min_lon = min(p[0] for p in lons_lats)
    max_lon = max(p[0] for p in lons_lats)
    min_lat = min(p[1] for p in lons_lats)
    max_lat = max(p[1] for p in lons_lats)
    return {
        "min_lon": min_lon,
        "max_lon": max_lon,
        "min_lat": min_lat,
        "max_lat": max_lat,
    }


def _normalizar_features_corredor(features):
    """
    Normaliza la entrada para trabajar siempre con una lista de diccionarios.

    consultar_corredor_waze_por_name() devuelve una lista de segmentos, pero
    algunas rutinas pueden recibir accidentalmente un único diccionario. Esta
    función evita iterar las llaves de ese diccionario como si fueran features.
    """
    if not features:
        return []
    if isinstance(features, dict):
        return [features]
    if isinstance(features, (list, tuple)):
        return [feature for feature in features if isinstance(feature, dict)]
    return []


def _vertices_corredor(features):
    """Devuelve todos los vértices de todos los segmentos del corredor."""
    vertices = []
    for feature in _normalizar_features_corredor(features):
        for path in feature.get("paths", []):
            vertices.extend(path)
    return vertices


def _dibujar_corredor(ax, features, color="#F36C21", alpha=0.95, lw=2.25, zorder=3):
    """Dibuja todos los segmentos devueltos por el filtro CorredorWaze.Name."""
    for feature in _normalizar_features_corredor(features):
        for path in feature.get("paths", []):
            if len(path) < 2:
                continue
            xs = [p[0] for p in path]
            ys = [p[1] for p in path]
            ax.plot(xs, ys, color="white", lw=lw + 2.4, alpha=0.9, zorder=zorder)
            ax.plot(xs, ys, color=color, lw=lw, alpha=alpha, zorder=zorder + 0.1)


def _envolver_titulo(texto, ancho=58):
    """Envuelve títulos sin cortar palabras ni desbordar el ancho del mapa."""
    texto = " ".join(str(texto).split())
    if not texto:
        return []
    return textwrap.wrap(
        texto,
        width=ancho,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [texto]


def _titulo_mapa_corredor(nombre_corredor, corredor_name=None, cobertura_parcial=False, hay_corredor=True):
    """Construye un título multilínea sin cortar palabras."""
    lineas = ["Ubicación de eventos abiertos"]
    lineas.extend(_envolver_titulo(nombre_corredor, 58))

    if cobertura_parcial and corredor_name:
        linea = f"Corredor: {corredor_name} (cobertura parcial)"
        lineas.extend(_envolver_titulo(linea, 62))
    elif not hay_corredor:
        lineas.append("Corredor no encontrado")

    return "\n".join(lineas[:4])


def mapa_eventos_corredor(
    eventos_corredor,
    ruta,
    nombre_corredor,
    corredor_features=None,
    corredor_name=None,
    cobertura_parcial=False,
):
    """
    Mapa del corredor actual.

    El flujo es intencionalmente simple:
      1) usa el Shape puntual del evento ya consultado en EPSG:3857;
      2) recibe TODOS los segmentos de CorredorWaze filtrados por Name;
      3) dibuja ambos sobre OpenStreetMap en Web Mercator.

    No calcula distancias al corredor, no hace snapping y no selecciona un
    segmento por cercanía.
    """
    corredor_features = corredor_features or []
    registros = []
    invalidos = []

    for evento in eventos_corredor:
        punto = _shape_point_3857(evento.get("_geometry"))
        if punto is None:
            invalidos.append(evento.get("objectid"))
            continue
        registros.append((punto[0], punto[1], evento))

    if invalidos and STRICT_GEOMETRY_VALIDATION:
        raise RuntimeError(
            f"El corredor {nombre_corredor!r} tiene eventos sin Shape puntual válido: "
            f"{invalidos}."
        )
    if not registros:
        return False

    puntos_contexto = [(x, y) for x, y, _ in registros]
    puntos_contexto.extend(_vertices_corredor(corredor_features))

    extent = _ajustar_extension_mapa_3857(
        puntos_contexto,
        aspecto_panel=MAP_PANEL_ASPECT,
        margen=1.10,
        span_minimo_m=35000.0,
    )
    basemap, basemap_extent, zoom = descargar_mosaico_osm(extent)
    _validar_registro_osm_shape(registros, basemap_extent, basemap)
    imshow_extent = _extent_imshow_desde_xyxy(basemap_extent)

    colores_impacto = {
        "Critico": ROJO,
        "Importante": NARANJA,
        "Moderado": AZUL,
        "Medio": AMARILLO,
        "Minimo": "#A7A9AC",
    }

    fig = plt.figure(figsize=MAP_FIGSIZE)
    ax = fig.add_axes([0.025, 0.145, 0.95, 0.825])
    ax.imshow(
        basemap,
        extent=imshow_extent,
        origin="upper",
        interpolation="nearest",
        resample=False,
        zorder=0,
    )

    _dibujar_corredor(ax, corredor_features, color="#F36C21", lw=2.25, zorder=2.5)

    impactos = defaultdict(list)
    for x, y, evento in registros:
        impacto = etiqueta_categoria(evento.get("tipo_impacto"))
        impactos[impacto].append((x, y, evento))

    orden = ["Critico", "Importante", "Moderado", "Medio", "Minimo"]
    extras = sorted(set(impactos).difference(orden), key=str.casefold)
    orden_visible = [i for i in orden + extras if impactos.get(i)]

    for impacto in orden_visible:
        grupo = impactos[impacto]
        xs = [r[0] for r in grupo]
        ys = [r[1] for r in grupo]
        color_impacto = colores_impacto.get(impacto, "#7F7F7F")
        ax.scatter(xs, ys, s=84, facecolors="none", edgecolors="white", linewidths=3.8, zorder=5)
        ax.scatter(xs, ys, s=66, facecolors="none", edgecolors=color_impacto, linewidths=2.2, zorder=6)
        ax.scatter(xs, ys, s=12, c=color_impacto, edgecolors="none", zorder=7)

    offsets = [(5, 6), (5, -13), (-5, 6), (-5, -13), (8, 0), (-8, 0)]
    for indice, (x, y, evento) in enumerate(
        sorted(registros, key=lambda r: int(r[2].get("objectid") or 0))
    ):
        oid = str(evento.get("objectid") or "")
        if not oid:
            continue
        dx, dy = offsets[indice % len(offsets)]
        ax.annotate(
            oid, (x, y), xytext=(dx, dy), textcoords="offset points",
            ha="left" if dx >= 0 else "right",
            va="bottom" if dy >= 0 else "top",
            fontsize=6.7, fontweight="bold", color=GRIS_OSCURO,
            bbox={"boxstyle": "round,pad=0.16", "fc": "white", "ec": "#B8B8B8", "lw": 0.45, "alpha": 0.90},
            zorder=8,
        )

    xmin, ymin, xmax, ymax = extent
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")

    ax.annotate(
        "N", xy=(0.965, 0.94), xytext=(0.965, 0.82),
        xycoords="axes fraction", textcoords="axes fraction",
        ha="center", va="center", fontsize=9, fontweight="bold",
        arrowprops={"arrowstyle": "-|>", "lw": 1.25, "color": GRIS_OSCURO},
        bbox={"boxstyle": "round,pad=0.10", "fc": "white", "ec": "none", "alpha": 0.72},
        zorder=8,
    )

    handles = []
    if corredor_features:
        handles.append(Line2D([0], [0], color="#F36C21", lw=2.6, label="Corredor logístico"))
    handles.extend([
        Line2D(
            [0], [0], marker="o", linestyle="None", markersize=7.4,
            markerfacecolor="white", markeredgecolor=colores_impacto.get(impacto, "#7F7F7F"),
            markeredgewidth=1.8, label=f"{impacto} ({len(impactos[impacto])})",
        )
        for impacto in orden_visible
    ])
    if handles:
        fig.legend(
            handles=handles,
            title="Impacto logístico",
            loc="lower center", bbox_to_anchor=(0.5, 0.008),
            ncol=min(6, len(handles)), fontsize=7.0, title_fontsize=7.6,
            frameon=False, handletextpad=0.5, columnspacing=1.0,
        )

    if cobertura_parcial and corredor_name:
        ax.text(
            0.012, 0.018, f"Corredor disponible: {corredor_name}",
            transform=ax.transAxes, fontsize=6.1, color=GRIS_OSCURO,
            ha="left", va="bottom",
            bbox={"boxstyle": "round,pad=0.18", "fc": "white", "ec": "#D8DDE5", "lw": 0.45, "alpha": 0.88},
            zorder=8,
        )

    ax.text(
        0.988, 0.018, "© OpenStreetMap contributors",
        transform=ax.transAxes, fontsize=6.0, color=GRIS_OSCURO,
        ha="right", va="bottom",
        bbox={"boxstyle": "round,pad=0.16", "fc": "white", "ec": "none", "alpha": 0.82},
        zorder=8,
    )

    fig.savefig(ruta, dpi=MAP_DPI, facecolor="white")
    plt.close(fig)
    try:
        basemap.close()
    except Exception:
        pass
    del basemap
    gc.collect()

    print(
        f"Mapa {nombre_corredor}: {len(registros)} evento(s), "
        f"{len(corredor_features)} segmento(s) de CorredorWaze "
        f"filtrados por Name={corredor_name!r}."
    )
    return True


def _agrupar_eventos_proximidad(registros, radio_m=SUMMARY_CLUSTER_RADIUS_M):
    """
    Agrupación visual por proximidad en EPSG:3857 usando componentes conectados.
    No modifica los Shape detallados y no asigna nombres de lugar inferidos.
    """
    if not registros:
        return []

    n = len(registros)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    celda = radio_m
    bins = defaultdict(list)
    for i, (x, y, _evento) in enumerate(registros):
        gx = int(math.floor(x / celda))
        gy = int(math.floor(y / celda))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in bins.get((gx + dx, gy + dy), []):
                    xj, yj, _ = registros[j]
                    if (x - xj) ** 2 + (y - yj) ** 2 <= radio_m ** 2:
                        union(i, j)
        bins[(gx, gy)].append(i)

    grupos = defaultdict(list)
    for i, reg in enumerate(registros):
        grupos[find(i)].append(reg)

    salida = []
    for grupo in grupos.values():
        x = sum(r[0] for r in grupo) / len(grupo)
        y = sum(r[1] for r in grupo) / len(grupo)
        salida.append({"x": x, "y": y, "n": len(grupo), "registros": grupo})
    return sorted(salida, key=lambda g: (-g["n"], g["x"], g["y"]))


def mapa_resumen_general(eventos, corredores_activos, ruta):
    """
    Mapa compacto para el resumen ejecutivo.

    Ocupa casi todo el lienzo del recuadro, muestra únicamente los corredores
    asociados a eventos abiertos y agrupa visualmente incidentes próximos para
    evitar saturación a escala nacional.
    """
    registros = []
    for evento in eventos:
        punto = _shape_point_3857(evento.get("_geometry"))
        if punto is not None:
            registros.append((punto[0], punto[1], evento))
    if not registros:
        return False

    contexto = [(x, y) for x, y, _ in registros]
    # corredores_activos ya es una lista plana de todos los segmentos
    # consultados por Name. Se procesa de una sola vez.
    contexto.extend(_vertices_corredor(corredores_activos))

    aspecto = SUMMARY_MAP_TARGET_PIXELS[0] / SUMMARY_MAP_TARGET_PIXELS[1]
    extent = _ajustar_extension_mapa_3857(
        contexto,
        aspecto_panel=aspecto,
        margen=1.06,
        span_minimo_m=250_000.0,
    )
    basemap, basemap_extent, zoom = descargar_mosaico_osm(extent)
    _validar_registro_osm_shape(registros, basemap_extent, basemap)
    imshow_extent = _extent_imshow_desde_xyxy(basemap_extent)

    clusters = _agrupar_eventos_proximidad(registros, SUMMARY_CLUSTER_RADIUS_M)
    fig = plt.figure(figsize=SUMMARY_MAP_FIGSIZE)
    # Se minimizan márgenes internos: el mapa debe ser claramente legible aun
    # cuando se inserte como panel secundario del resumen ejecutivo.
    ax = fig.add_axes([0.015, 0.045, 0.97, 0.885])
    ax.imshow(
        basemap, extent=imshow_extent, origin="upper",
        interpolation="nearest", resample=False, zorder=0,
    )

    # Dibuja todos los segmentos de todos los corredores activos.
    _dibujar_corredor(
        ax, corredores_activos, color="#F36C21", alpha=0.82, lw=1.55, zorder=2
    )

    for cluster in clusters:
        n = cluster["n"]
        size = 30 if n == 1 else min(165, 48 + 20 * math.sqrt(n))
        ax.scatter(
            [cluster["x"]], [cluster["y"]], s=size,
            c=ROJO, edgecolors="white", linewidths=1.55, alpha=0.94, zorder=5,
        )
        if n > 1:
            ax.text(
                cluster["x"], cluster["y"], str(n),
                ha="center", va="center", fontsize=6.7, fontweight="bold",
                color="white", zorder=6,
            )

    xmin, ymin, xmax, ymax = extent
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")

    fig.text(
        0.5, 0.975, "Distribución espacial de eventos abiertos",
        ha="center", va="top", fontsize=9.2, fontweight="bold",
    )

    departamentos = Counter(normalizar_departamento(e.get("departamento")) for e in eventos)
    top_dep = departamentos.most_common(3)
    n_grupos = sum(1 for c in clusters if c["n"] > 1)
    max_grupo = max((c["n"] for c in clusters), default=1)

    linea1 = (
        f"{len(registros)} eventos  ·  {n_grupos} agrupaciones  ·  "
        f"máx. {max_grupo} eventos/grupo"
    )
    lineas_estad = [linea1]
    if top_dep:
        lineas_estad.append(
            "Mayor presencia: " + ", ".join(f"{d} ({n})" for d, n in top_dep)
        )

    ax.text(
        0.014, 0.986, "\n".join(lineas_estad), transform=ax.transAxes,
        ha="left", va="top", fontsize=5.75, color=GRIS_OSCURO,
        bbox={"boxstyle": "round,pad=0.22", "fc": "white", "ec": "#D0D0D0", "lw": 0.4, "alpha": 0.90},
        zorder=8,
    )

    handles = [
        Line2D([0], [0], color="#F36C21", lw=1.9, label="Corredor con eventos"),
        Line2D(
            [0], [0], marker="o", linestyle="None", markersize=5.8,
            markerfacecolor=ROJO, markeredgecolor="white",
            label=f"Evento / agrupación ≤ {SUMMARY_CLUSTER_RADIUS_M/1000:.0f} km",
        ),
    ]
    leg = ax.legend(
        handles=handles,
        loc="lower left",
        bbox_to_anchor=(0.008, 0.014),
        ncol=1,
        fontsize=5.35,
        frameon=True,
        fancybox=True,
        framealpha=0.86,
        borderpad=0.35,
        handletextpad=0.4,
        labelspacing=0.25,
    )
    leg.get_frame().set_edgecolor("#D0D0D0")
    leg.get_frame().set_linewidth(0.4)

    ax.text(
        0.986, 0.018, f"OSM z{zoom} · © OpenStreetMap contributors",
        transform=ax.transAxes, ha="right", va="bottom", fontsize=5.1, color=GRIS_OSCURO,
        bbox={"boxstyle": "round,pad=0.13", "fc": "white", "ec": "none", "alpha": 0.82},
        zorder=8,
    )

    fig.savefig(ruta, dpi=MAP_DPI, facecolor="white")
    plt.close(fig)
    try:
        basemap.close()
    except Exception:
        pass
    del basemap
    gc.collect()
    return True


# ============================================================
# DIBUJO DE PORTADA Y RESUMEN
# ============================================================

def draw_fitted_image(c, path, x, y, w, h):
    img = ImageReader(str(path))
    iw, ih = img.getSize()
    escala = min(w / iw, h / ih)
    dw = iw * escala
    dh = ih * escala

    c.drawImage(
        img,
        x + (w - dw) / 2,
        y + (h - dh) / 2,
        width=dw,
        height=dh,
        preserveAspectRatio=True,
        mask="auto",
    )


def tarjeta_kpi(c, x, y, w, h, titulo, valor, color):
    c.setFillColor(colors.HexColor(GRIS_CLARO))
    c.roundRect(x, y, w, h, 8, fill=1, stroke=0)

    c.setFillColor(colors.HexColor(color))
    c.rect(x, y, 5, h, fill=1, stroke=0)

    c.setFillColor(colors.HexColor(GRIS))
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(x + 12, y + h - 15, titulo.upper())

    c.setFillColor(colors.HexColor(color))
    c.setFont("Helvetica-Bold", 19)
    c.drawString(x + 12, y + 10, str(valor))


class PortadaFlowable(Flowable):
    def __init__(self, eventos, fecha_corte):
        super().__init__()
        self.width, self.height = A4
        self.eventos = eventos
        self.fecha_corte = fecha_corte

    def wrap(self, availWidth, availHeight):
        return self.width, self.height

    def draw(self):
        c = self.canv
        ancho, alto = A4

        c.setFillColor(colors.white)
        c.rect(0, 0, ancho, alto, fill=1, stroke=0)

        c.setFillColor(colors.HexColor(NARANJA))
        c.rect(0, alto - 1.15 * cm, ancho, 1.15 * cm, fill=1, stroke=0)

        logo = ImageReader(str(LOGO))
        iw, ih = logo.getSize()
        max_w = 8.2 * cm
        max_h = 5.2 * cm
        escala = min(max_w / iw, max_h / ih)
        dw = iw * escala
        dh = ih * escala

        c.drawImage(
            logo,
            (ancho - dw) / 2,
            alto - 7.1 * cm,
            width=dw,
            height=dh,
            preserveAspectRatio=True,
            mask="auto",
        )

        c.setFillColor(colors.HexColor(NARANJA))
        c.setFont("Helvetica-Bold", 24)
        c.drawCentredString(ancho / 2, alto - 9.05 * cm, "REPORTE SEMANAL")

        c.setFont("Helvetica-Bold", 19)
        c.drawCentredString(ancho / 2, alto - 9.95 * cm, "DE EVENTOS LOGÍSTICOS")

        c.setFillColor(colors.HexColor(GRIS))
        c.setFont("Helvetica-Bold", 13)
        c.drawCentredString(ancho / 2, alto - 11.15 * cm, "Grupo de Logística")

        linea_y = alto - 11.75 * cm
        total = 7.2 * cm
        inicio = (ancho - total) / 2

        c.setFillColor(colors.HexColor(AMARILLO))
        c.rect(inicio, linea_y, total * 0.5, 5, fill=1, stroke=0)
        c.setFillColor(colors.HexColor(AZUL))
        c.rect(inicio + total * 0.5, linea_y, total * 0.25, 5, fill=1, stroke=0)
        c.setFillColor(colors.HexColor(ROJO))
        c.rect(inicio + total * 0.75, linea_y, total * 0.25, 5, fill=1, stroke=0)

        tarjeta_w = 12.5 * cm
        tarjeta_h = 3.15 * cm
        tx = (ancho - tarjeta_w) / 2
        ty = alto - 16.35 * cm

        c.setFillColor(colors.HexColor(GRIS_CLARO))
        c.roundRect(tx, ty, tarjeta_w, tarjeta_h, 10, fill=1, stroke=0)

        c.setFillColor(colors.HexColor(NARANJA))
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(ancho / 2, ty + tarjeta_h - 0.72 * cm, "FECHA DE CORTE")

        c.setFillColor(colors.HexColor(GRIS_OSCURO))
        c.setFont("Helvetica-Bold", 16)
        c.drawCentredString(
            ancho / 2,
            ty + tarjeta_h - 1.45 * cm,
            self.fecha_corte.strftime("%d/%m/%Y"),
        )

        c.setFillColor(colors.HexColor(GRIS))
        c.setFont("Helvetica", 9.5)
        c.drawCentredString(ancho / 2, ty + 0.92 * cm, "Hora Colombia")

        c.setFillColor(colors.HexColor(NARANJA))
        c.setFont("Helvetica-Bold", 21)
        c.drawCentredString(ancho / 2, ty - 1.55 * cm, str(len(self.eventos)))

        c.setFillColor(colors.HexColor(GRIS))
        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(ancho / 2, ty - 2.05 * cm, "EVENTOS ABIERTOS")

        c.setFillColor(colors.HexColor(NARANJA))
        c.rect(0, 0, ancho, 1.05 * cm, fill=1, stroke=0)

        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawCentredString(ancho / 2, 0.40 * cm, "MINISTERIO DE TRANSPORTE")


class ResumenFlowable(Flowable):
    def __init__(self, eventos, fecha_corte, chart_paths, summary_map_path=None):
        super().__init__()
        self.width, self.height = A4
        self.eventos = eventos
        self.fecha_corte = fecha_corte
        self.chart_paths = chart_paths
        self.summary_map_path = summary_map_path

    def wrap(self, availWidth, availHeight):
        return self.width, self.height

    def draw(self):
        c = self.canv
        ancho, alto = A4

        c.setFillColor(colors.white)
        c.rect(0, 0, ancho, alto, fill=1, stroke=0)

        c.setFillColor(colors.HexColor(NARANJA))
        c.rect(0, alto - 34, ancho, 34, fill=1, stroke=0)

        logo = ImageReader(str(LOGO))
        iw, ih = logo.getSize()
        max_w, max_h = 92, 58
        esc = min(max_w / iw, max_h / ih)
        dw, dh = iw * esc, ih * esc

        c.drawImage(
            logo,
            32,
            alto - 104,
            width=dw,
            height=dh,
            preserveAspectRatio=True,
            mask="auto",
        )

        c.setFillColor(colors.HexColor(NARANJA))
        c.setFont("Helvetica-Bold", 20)
        c.drawString(145, alto - 68, "RESUMEN EJECUTIVO")

        c.setFillColor(colors.HexColor(GRIS))
        c.setFont("Helvetica", 9)
        c.drawString(145, alto - 85, "Reporte semanal de eventos logísticos")
        c.drawRightString(
            ancho - 34,
            alto - 85,
            f"Corte: {self.fecha_corte.strftime('%d/%m/%Y %H:%M')}",
        )

        x0 = 145
        y0 = alto - 96
        largo = 135
        c.setFillColor(colors.HexColor(AMARILLO))
        c.rect(x0, y0, largo * 0.5, 4, fill=1, stroke=0)
        c.setFillColor(colors.HexColor(AZUL))
        c.rect(x0 + largo * 0.5, y0, largo * 0.25, 4, fill=1, stroke=0)
        c.setFillColor(colors.HexColor(ROJO))
        c.rect(x0 + largo * 0.75, y0, largo * 0.25, 4, fill=1, stroke=0)

        total = len(self.eventos)
        cierres_totales = sum(
            1 for e in self.eventos if etiqueta_categoria(e.get("tipo_cierre")) == "Cierre total"
        )
        corredores = len({
            limpiar(e.get("corredor_logistico"), "Sin corredor")
            for e in self.eventos
        })
        criticos = sum(
            1 for e in self.eventos if etiqueta_categoria(e.get("tipo_impacto")) == "Critico"
        )

        margen = 34
        gap = 9
        kpi_y = alto - 166
        kpi_h = 48
        kpi_w = (ancho - 2 * margen - 3 * gap) / 4

        tarjeta_kpi(c, margen, kpi_y, kpi_w, kpi_h, "Eventos abiertos", total, NARANJA)
        tarjeta_kpi(c, margen + (kpi_w + gap), kpi_y, kpi_w, kpi_h, "Cierres totales", cierres_totales, ROJO)
        tarjeta_kpi(c, margen + 2 * (kpi_w + gap), kpi_y, kpi_w, kpi_h, "Corredores", corredores, AZUL)
        tarjeta_kpi(c, margen + 3 * (kpi_w + gap), kpi_y, kpi_w, kpi_h, "Impacto crítico", criticos, ROJO)

        # Composición compacta: el mapa acompaña el resumen sin competir con KPIs/gráficas.
        charts_top = kpi_y - 12
        chart_gap = 10
        content_w = ancho - 2 * margen

        # Fila 1: gráfico + mapa general. El mapa gana algo de espacio, pero
        # sigue siendo un elemento secundario respecto de los KPIs y gráficas.
        row1_h = 188
        row1_y = charts_top - row1_h
        map_w = content_w * 0.46
        corr_w = content_w - map_w - chart_gap

        c.setStrokeColor(colors.HexColor(GRIS_LINEA))
        c.roundRect(margen, row1_y, corr_w, row1_h, 8, fill=0, stroke=1)
        draw_fitted_image(
            c, self.chart_paths["corredores"],
            margen + 6, row1_y + 5,
            corr_w - 12, row1_h - 10,
        )

        map_x = margen + corr_w + chart_gap
        c.roundRect(map_x, row1_y, map_w, row1_h, 8, fill=0, stroke=1)
        if self.summary_map_path and Path(self.summary_map_path).exists():
            draw_fitted_image(
                c, self.summary_map_path,
                map_x + 5, row1_y + 5,
                map_w - 10, row1_h - 10,
            )
        else:
            c.setFillColor(colors.HexColor(GRIS))
            c.setFont("Helvetica", 7.5)
            c.drawCentredString(map_x + map_w / 2, row1_y + row1_h / 2, "Mapa no disponible")

        # Fila 2: tipos de evento y cierre.
        row2_h = 210
        row2_y = row1_y - chart_gap - row2_h
        half_w = (content_w - chart_gap) / 2
        c.roundRect(margen, row2_y, half_w, row2_h, 8, fill=0, stroke=1)
        draw_fitted_image(
            c, self.chart_paths["tipo_evento"],
            margen + 6, row2_y + 6,
            half_w - 12, row2_h - 12,
        )

        x_right = margen + half_w + chart_gap
        c.roundRect(x_right, row2_y, half_w, row2_h, 8, fill=0, stroke=1)
        draw_fitted_image(
            c, self.chart_paths["tipo_cierre"],
            x_right + 6, row2_y + 6,
            half_w - 12, row2_h - 12,
        )

        # Fila 3: impacto logístico. Se calcula la altura con el espacio
        # realmente disponible para eliminar el vacío entre la fila 2 y esta gráfica.
        impact_y = 38
        impact_top = row2_y - chart_gap
        impact_h = max(145, impact_top - impact_y)
        c.roundRect(margen, impact_y, content_w, impact_h, 8, fill=0, stroke=1)
        draw_fitted_image(
            c, self.chart_paths["impacto"],
            margen + 8, impact_y + 5,
            content_w - 16, impact_h - 10,
        )

        c.setFillColor(colors.HexColor(NARANJA))
        c.rect(0, 0, ancho, 24, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawCentredString(
            ancho / 2,
            8,
            "MINISTERIO DE TRANSPORTE - GRUPO DE LOGÍSTICA",
        )


# ============================================================
# ESTILOS DEL CUERPO
# ============================================================

def crear_estilos():
    base = getSampleStyleSheet()

    estilos = {}

    estilos["h1"] = ParagraphStyle(
        "Corredor",
        parent=base["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        textColor=colors.white,
        backColor=colors.HexColor(NARANJA),
        leftIndent=7,
        rightIndent=7,
        borderPadding=6,
        spaceBefore=8,
        spaceAfter=8,
    )

    estilos["h2"] = ParagraphStyle(
        "Departamento",
        parent=base["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.HexColor(AZUL),
        spaceBefore=7,
        spaceAfter=6,
    )

    estilos["evento"] = ParagraphStyle(
        "Evento",
        parent=base["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=colors.HexColor(GRIS_OSCURO),
        spaceBefore=5,
        spaceAfter=5,
    )

    estilos["campo"] = ParagraphStyle(
        "Campo",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=8.8,
        leading=11.3,
        alignment=TA_LEFT,
        spaceAfter=3,
    )

    estilos["tabla"] = ParagraphStyle(
        "Tabla",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=8.2,
        leading=10,
        alignment=TA_LEFT,
    )

    estilos["seccion"] = ParagraphStyle(
        "SeccionCorredor",
        parent=base["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.HexColor(AZUL),
        spaceBefore=10,
        spaceAfter=6,
    )

    # Tabla consolidada: no partir palabras internamente. Cuando una celda
    # necesita más de una línea, el salto se realiza únicamente en espacios.
    estilos["tabla_consolidada"] = ParagraphStyle(
        "TablaConsolidada",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=5.55,
        leading=7.0,
        alignment=TA_LEFT,
        splitLongWords=0,
        wordWrap="LTR",
    )

    estilos["tabla_consolidada_cab"] = ParagraphStyle(
        "TablaConsolidadaCab",
        parent=base["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=5.65,
        leading=6.9,
        textColor=colors.white,
        alignment=TA_CENTER,
        splitLongWords=0,
        wordWrap="LTR",
    )

    estilos["tarjeta_visual_titulo"] = ParagraphStyle(
        "TarjetaVisualTitulo",
        parent=base["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=8.8,
        leading=10.5,
        textColor=colors.HexColor(AZUL),
        alignment=TA_LEFT,
        leftIndent=0,
        rightIndent=0,
        spaceAfter=0,
    )

    estilos["item_evento"] = ParagraphStyle(
        "ItemEvento",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=8.6,
        leading=11.2,
        alignment=TA_LEFT,
        spaceAfter=7,
    )

    return estilos


def tabla_datos_evento(evento, estilos):
    segmento = limpiar(evento.get("segmento_corredor"), "Sin segmento registrado")
    municipio = limpiar(evento.get("municipio"), "Sin municipio")
    fecha_inicio = fecha_colombia(
        evento.get("Fecha_inicio"),
        "Sin fecha de inicio registrada",
    )
    fecha_seguimiento = fecha_colombia(
        evento.get("FechaSeguimiento"),
        "Sin fecha de seguimiento registrada",
    )

    filas = [
        [
            Paragraph("<b>Municipio</b>", estilos["tabla"]),
            Paragraph(escape(municipio), estilos["tabla"]),
            Paragraph("<b>Segmento</b>", estilos["tabla"]),
            Paragraph(escape(segmento), estilos["tabla"]),
        ],
        [
            Paragraph("<b>Fecha inicio</b>", estilos["tabla"]),
            Paragraph(escape(fecha_inicio), estilos["tabla"]),
            Paragraph("<b>Fecha seguimiento</b>", estilos["tabla"]),
            Paragraph(escape(fecha_seguimiento), estilos["tabla"]),
        ],
        [
            Paragraph("<b>Tipo de evento</b>", estilos["tabla"]),
            Paragraph(escape(etiqueta_categoria(evento.get("tipo_evento"))), estilos["tabla"]),
            Paragraph("<b>Tipo de cierre</b>", estilos["tabla"]),
            Paragraph(escape(etiqueta_categoria(evento.get("tipo_cierre"))), estilos["tabla"]),
        ],
        [
            Paragraph("<b>Impacto logístico</b>", estilos["tabla"]),
            Paragraph(escape(etiqueta_categoria(evento.get("tipo_impacto"))), estilos["tabla"]),
            Paragraph("<b>PR afectado</b>", estilos["tabla"]),
            Paragraph(
                escape(
                    f"{pr_texto(evento.get('pr_inicio'))} - "
                    f"{pr_texto(evento.get('pr_fin'))}"
                ),
                estilos["tabla"],
            ),
        ],
    ]

    tabla = Table(
        filas,
        colWidths=[2.5 * cm, 5.1 * cm, 2.8 * cm, 5.2 * cm],
    )

    tabla.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C7C7C7")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor(GRIS_CLARO)),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor(GRIS_CLARO)),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    return tabla


def tarjeta_visual(titulo, ruta_imagen, estilos, ancho=8.45 * cm, max_alto_imagen=5.85 * cm, acento=NARANJA):
    """Construye una tarjeta visual compacta para integrar mapa y gráfica."""
    if not ruta_imagen or not Path(ruta_imagen).exists():
        contenido = Paragraph("Visual no disponible", estilos["tabla"])
    else:
        img = Image(str(ruta_imagen))
        max_ancho_imagen = ancho - 0.34 * cm
        escala = min(
            max_ancho_imagen / img.imageWidth,
            max_alto_imagen / img.imageHeight,
        )
        img.drawWidth = img.imageWidth * escala
        img.drawHeight = img.imageHeight * escala
        img.hAlign = "CENTER"
        contenido = img

    tarjeta = Table(
        [
            [Paragraph(escape(titulo), estilos["tarjeta_visual_titulo"])],
            [contenido],
        ],
        colWidths=[ancho],
        hAlign="CENTER",
    )
    tarjeta.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4F7FB")),
                ("BACKGROUND", (0, 1), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.55, colors.HexColor("#D6DCE5")),
                ("LINEABOVE", (0, 0), (-1, 0), 2.2, colors.HexColor(acento)),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, 0), 5),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
                ("TOPPADDING", (0, 1), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
            ]
        )
    )
    return tarjeta


def fecha_tabla(epoch_ms):
    if epoch_ms in (None, ""):
        return "Sin registrar"
    return fecha_colombia(epoch_ms, "Sin registrar")


def ordenar_eventos_por_fecha_desc(eventos):
    """
    Ordena los eventos desde la Fecha_inicio más reciente hasta la más antigua.
    Los registros sin Fecha_inicio válida se envían al final.
    En empates de fecha se usa departamento, municipio y OBJECTID para mantener
    un orden estable y reproducible.
    """
    def clave(evento):
        valor = evento.get("Fecha_inicio")
        try:
            fecha_ms = int(float(valor)) if valor not in (None, "") else None
        except (TypeError, ValueError):
            fecha_ms = None

        return (
            0 if fecha_ms is not None else 1,
            -fecha_ms if fecha_ms is not None else 0,
            normalizar_departamento(evento.get("departamento")).casefold(),
            limpiar(evento.get("municipio"), "").casefold(),
            int(evento.get("objectid") or 0),
        )

    return sorted(eventos, key=clave)


def tabla_eventos_corredor(eventos_corredor, estilos):
    """
    Tabla consolidada de todos los eventos del corredor.
    Mantiene las variables de la ficha actual y añade Evento y Departamento
    para identificar cada registro en la tabla conjunta.
    """
    encabezados = [
        "Evento",
        "Departamento",
        "Municipio",
        "Segmento",
        "Fecha inicio",
        "Fecha seguimiento",
        "Tipo evento",
        "Tipo cierre",
        "Impacto",
        "PR afectado",
    ]

    filas = [
        [
            Paragraph(escape(cab), estilos["tabla_consolidada_cab"])
            for cab in encabezados
        ]
    ]

    eventos_ordenados = ordenar_eventos_por_fecha_desc(eventos_corredor)

    for evento in eventos_ordenados:
        pr = (
            f"{pr_texto(evento.get('pr_inicio'))} - "
            f"{pr_texto(evento.get('pr_fin'))}"
        )

        valores = [
            str(evento.get("objectid") or ""),
            normalizar_departamento(evento.get("departamento")),
            limpiar(evento.get("municipio"), "Sin municipio"),
            limpiar(evento.get("segmento_corredor"), "Sin segmento"),
            fecha_tabla(evento.get("Fecha_inicio")),
            fecha_tabla(evento.get("FechaSeguimiento")),
            etiqueta_categoria(evento.get("tipo_evento")),
            etiqueta_categoria(evento.get("tipo_cierre")),
            etiqueta_categoria(evento.get("tipo_impacto")),
            pr,
        ]

        filas.append(
            [
                Paragraph(escape(str(valor)), estilos["tabla_consolidada"])
                for valor in valores
            ]
        )

    # Distribución optimizada para el ancho útil A4 (18,1 cm).
    # Las columnas con palabras largas reciben mayor espacio para evitar
    # cortes internos como "Infraestructur / a".
    anchos = [
        0.90 * cm,   # Evento
        1.70 * cm,   # Departamento
        1.55 * cm,   # Municipio
        2.70 * cm,   # Segmento
        1.65 * cm,   # Fecha inicio
        1.75 * cm,   # Fecha seguimiento
        1.95 * cm,   # Tipo evento
        1.65 * cm,   # Tipo cierre
        1.25 * cm,   # Impacto
        2.50 * cm,   # PR afectado
    ]

    tabla = LongTable(
        filas,
        colWidths=anchos,
        repeatRows=1,
        splitByRow=1,
        hAlign="CENTER",
    )

    tabla.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(AZUL)),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.30, colors.HexColor("#C7C7C7")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
                    colors.white,
                    colors.HexColor("#F6F8FB"),
                ]),
                ("LEFTPADDING", (0, 0), (-1, -1), 3.0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3.0),
                ("TOPPADDING", (0, 0), (-1, 0), 5.0),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 5.0),
                ("TOPPADDING", (0, 1), (-1, -1), 4.0),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 4.0),
                ("LINEBELOW", (0, 0), (-1, 0), 0.7, colors.HexColor("#083A91")),
            ]
        )
    )

    return tabla


def bloque_listado_eventos(eventos_corredor, campo, estilos, omitir_vacios=False):
    elementos = []

    eventos_ordenados = ordenar_eventos_por_fecha_desc(eventos_corredor)

    for evento in eventos_ordenados:
        oid = escape(str(evento.get("objectid") or ""))
        municipio = escape(limpiar(evento.get("municipio"), "Sin municipio"))
        departamento = escape(
            normalizar_departamento(evento.get("departamento"))
        )

        valor_campo = evento.get(campo)
        if omitir_vacios and not tiene_informacion(valor_campo):
            continue

        contenido = texto_parrafo(valor_campo)

        elementos.append(
            Paragraph(
                f"<b>Evento {oid} - {municipio} ({departamento})</b><br/>"
                f"{contenido}",
                estilos["item_evento"],
            )
        )

    return elementos


# ============================================================
# PLANTILLAS DE PÁGINA
# ============================================================

def pagina_detalle(canvas, doc):
    canvas.saveState()
    ancho, alto = A4

    # Encabezado
    canvas.setFillColor(colors.HexColor(NARANJA))
    canvas.rect(0, alto - 20, ancho, 20, fill=1, stroke=0)

    canvas.setFillColor(colors.HexColor(GRIS))
    canvas.setFont("Helvetica-Bold", 7.7)
    canvas.drawString(1.45 * cm, alto - 37, "REPORTE SEMANAL DE EVENTOS LOGÍSTICOS")

    canvas.setFont("Helvetica", 7.7)
    canvas.drawRightString(ancho - 1.45 * cm, alto - 37, "Grupo de Logística")

    # Pie
    canvas.setStrokeColor(colors.HexColor(GRIS_LINEA))
    canvas.setLineWidth(0.4)
    canvas.line(1.45 * cm, 1.30 * cm, ancho - 1.45 * cm, 1.30 * cm)

    canvas.setFillColor(colors.HexColor(GRIS))
    canvas.setFont("Helvetica", 7.3)
    canvas.drawString(1.45 * cm, 0.88 * cm, "Ministerio de Transporte")
    canvas.drawRightString(ancho - 1.45 * cm, 0.88 * cm, f"Página {doc.page}")

    canvas.restoreState()


# ============================================================
# CONSTRUCCIÓN DEL PDF
# ============================================================

def generar_reporte(eventos):
    if not LOGO.exists():
        raise FileNotFoundError(f"No se encontró el logo: {LOGO}")

    fecha_corte = datetime.now(COLOMBIA_TZ)
    nombre = f"Reporte_Logistico_Institucional_{fecha_corte.strftime('%Y%m%d_%H%M')}.pdf"
    ruta_pdf = OUTPUT_DIR / nombre

    ancho, alto = A4

    doc = BaseDocTemplate(
        str(ruta_pdf),
        pagesize=A4,
        leftMargin=0,
        rightMargin=0,
        topMargin=0,
        bottomMargin=0,
        title="Reporte semanal de eventos logísticos",
        author="Ministerio de Transporte - Grupo de Logística",
    )

    frame_full = Frame(
        0, 0, ancho, alto,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="full",
    )

    frame_body = Frame(
        1.45 * cm,
        1.55 * cm,
        ancho - 2.90 * cm,
        alto - 3.25 * cm,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="body",
    )

    doc.addPageTemplates([
        PageTemplate(id="portada", frames=[frame_full]),
        PageTemplate(id="resumen", frames=[frame_full]),
        PageTemplate(id="detalle", frames=[frame_body], onPage=pagina_detalle),
    ])

    estilos = crear_estilos()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        chart_paths = {
            "corredores": tmp / "corredores.png",
            "tipo_evento": tmp / "tipo_evento.png",
            "tipo_cierre": tmp / "tipo_cierre.png",
            "impacto": tmp / "impacto.png",
        }
        summary_map_path = tmp / "mapa_resumen_general.jpg"

        grafica_corredores(eventos, chart_paths["corredores"])
        grafica_tipo_evento(eventos, chart_paths["tipo_evento"])
        grafica_tipo_cierre(eventos, chart_paths["tipo_cierre"])
        grafica_impacto(eventos, chart_paths["impacto"])

        # Agrupación principal del reporte por corredor. A partir de este punto,
        # cada corredor consulta SU propia geometría en CorredorWaze mediante
        # un filtro exacto sobre el campo Name.
        eventos_por_corredor = defaultdict(list)
        for evento in eventos:
            corredor_raw = limpiar(evento.get("corredor_logistico"), "Sin corredor")
            eventos_por_corredor[corredor_raw].append(evento)

        chart_departamentos = {}
        mapas_corredor = {}
        # No retenemos todas las geometrías detalladas hasta el final: cada
        # corredor se libera tras generar su mapa. El resumen se consulta luego
        # con una geometría más generalizada y liviana.
        nombres_corredor_resumen = set()
        for indice, (corredor_raw, lista_eventos) in enumerate(
            sorted(
                eventos_por_corredor.items(),
                key=lambda x: etiqueta_corredor(x[0]).casefold(),
            ),
            start=1,
        ):
            ruta_chart = tmp / f"departamentos_corredor_{indice:02d}.png"
            grafica_departamentos_corredor(
                lista_eventos,
                ruta_chart,
                etiqueta_corredor(corredor_raw),
            )
            chart_departamentos[corredor_raw] = ruta_chart

            # Filtro directo de CorredorWaze por el campo Name del corredor actual.
            corredor_name, cobertura_parcial = nombre_corredor_waze(corredor_raw)
            corredor_features = consultar_corredor_waze_por_name(
                corredor_name,
                max_allowable_offset=CORRIDOR_DETAIL_OFFSET_M,
            )
            if corredor_name:
                nombres_corredor_resumen.add(corredor_name)

            ruta_mapa = tmp / f"mapa_corredor_{indice:02d}.jpg"
            if mapa_eventos_corredor(
                lista_eventos,
                ruta_mapa,
                etiqueta_corredor(corredor_raw),
                corredor_features=corredor_features,
                corredor_name=corredor_name,
                cobertura_parcial=cobertura_parcial,
            ):
                mapas_corredor[corredor_raw] = ruta_mapa

            # La geometría detallada ya quedó rasterizada en el JPG del mapa.
            # No debe permanecer en RAM durante el resto del reporte.
            del corredor_features
            gc.collect()

        # Para el mapa nacional se vuelven a consultar únicamente los corredores
        # activos con una generalización mayor. Esto sacrifica vértices que no son
        # visibles a esa escala, no la ubicación de los eventos.
        corredores_resumen = []
        for nombre_resumen in sorted(nombres_corredor_resumen, key=str.casefold):
            corredores_resumen.extend(
                consultar_corredor_waze_por_name(
                    nombre_resumen,
                    max_allowable_offset=CORRIDOR_SUMMARY_OFFSET_M,
                )
            )

        if not mapa_resumen_general(eventos, corredores_resumen, summary_map_path):
            summary_map_path = None

        del corredores_resumen
        gc.collect()

        story = []

        # Página 1 - Portada
        story.append(PortadaFlowable(eventos, fecha_corte))
        story.append(NextPageTemplate("resumen"))
        story.append(PageBreak())

        # Página 2 - Resumen ejecutivo
        story.append(ResumenFlowable(eventos, fecha_corte, chart_paths, summary_map_path))
        story.append(NextPageTemplate("detalle"))
        story.append(PageBreak())

        # Página 3 en adelante - Detalle
        agrupados = defaultdict(lambda: defaultdict(list))

        for evento in eventos:
            corredor_raw = limpiar(evento.get("corredor_logistico"), "Sin corredor")
            departamento = normalizar_departamento(evento.get("departamento"))
            agrupados[corredor_raw][departamento].append(evento)

        corredores_ordenados = sorted(
            agrupados,
            key=lambda x: etiqueta_corredor(x).casefold(),
        )

        for idx_corredor, corredor_raw in enumerate(corredores_ordenados):
            departamentos = agrupados[corredor_raw]

            eventos_corredor = []
            for lista_eventos in departamentos.values():
                eventos_corredor.extend(lista_eventos)

            # Orden cronológico descendente dentro de cada corredor:
            # evento más reciente primero y el más antiguo al final.
            eventos_corredor = ordenar_eventos_por_fecha_desc(eventos_corredor)

            total_corredor = len(eventos_corredor)

            # 1. Encabezado del corredor
            story.append(
                Paragraph(
                    f"{escape(etiqueta_corredor(corredor_raw))} "
                    f"({total_corredor} evento(s) abierto(s))",
                    estilos["h1"],
                )
            )

            # Separación visual entre la franja naranja del corredor y las
            # tarjetas de mapa/gráfica. Evita que ambos bloques se perciban
            # pegados al título y mejora la jerarquía visual de la página.
            story.append(Spacer(1, 0.42 * cm))

            # 2. Mapa + distribución por departamento en una fila integrada.
            ruta_mapa = mapas_corredor.get(corredor_raw)
            ruta_grafica_dep = chart_departamentos[corredor_raw]

            tarjeta_mapa = tarjeta_visual(
                "Ubicación de eventos sobre el corredor",
                ruta_mapa,
                estilos,
                ancho=8.45 * cm,
                max_alto_imagen=5.90 * cm,
                acento=NARANJA,
            )
            tarjeta_dep = tarjeta_visual(
                "Distribución por departamento e impacto",
                ruta_grafica_dep,
                estilos,
                ancho=8.45 * cm,
                max_alto_imagen=5.90 * cm,
                acento=AZUL,
            )

            tabla_visuales = Table(
                [[tarjeta_mapa, "", tarjeta_dep]],
                colWidths=[8.45 * cm, 0.28 * cm, 8.45 * cm],
                hAlign="CENTER",
            )
            tabla_visuales.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ]
                )
            )
            story.append(tabla_visuales)
            story.append(Spacer(1, 0.34 * cm))

            # 3. Tabla consolidada
            story.append(
                Paragraph(
                    "Inventario consolidado de eventos del corredor",
                    estilos["seccion"],
                )
            )
            story.append(tabla_eventos_corredor(eventos_corredor, estilos))
            story.append(Spacer(1, 0.30 * cm))

            # 5. Descripciones
            story.append(
                Paragraph(
                    "Descripciones",
                    estilos["seccion"],
                )
            )
            story.extend(
                bloque_listado_eventos(
                    eventos_corredor,
                    "Detalle",
                    estilos,
                )
            )

            # 6. Seguimientos
            story.append(
                Paragraph(
                    "Seguimientos",
                    estilos["seccion"],
                )
            )
            story.extend(
                bloque_listado_eventos(
                    eventos_corredor,
                    "Seguimiento",
                    estilos,
                )
            )

            # 7. Vulnerabilidades preventivas
            # Solo se muestran eventos con contenido real en SeguimientoPreventivo.
            preventivos = bloque_listado_eventos(
                eventos_corredor,
                "SeguimientoPreventivo",
                estilos,
                omitir_vacios=True,
            )
            if preventivos:
                story.append(
                    Paragraph(
                        "Vulnerabilidades que requieren seguimiento preventivo",
                        estilos["seccion"],
                    )
                )
                story.extend(preventivos)

            if idx_corredor < len(corredores_ordenados) - 1:
                story.append(PageBreak())

        doc.build(story)

    return ruta_pdf


def main():
    print("Consultando eventos abiertos...")
    eventos = consultar_eventos_abiertos()
    print(f"Eventos abiertos consultados: {len(eventos)}")

    ruta = generar_reporte(eventos)

    print("Reporte completo generado correctamente.")
    print(f"PDF: {ruta}")


if __name__ == "__main__":
    main()
