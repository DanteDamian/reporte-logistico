"""Consulta y gráficos de ocupación portuaria para el reporte PDF.

Fuente: Vista_Ocupacion/FeatureServer/0 (tabla, sin geometría). Las fechas de
ArcGIS son UTC y los días del reporte se calculan en horario de Colombia.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from math import ceil, isfinite
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

SERVICE_URL = (
    "https://services3.arcgis.com/PpJ89hvCx3B7cBIO/arcgis/rest/services/"
    "Vista_Ocupacion/FeatureServer/0"
)
COLOMBIA = timezone(timedelta(hours=-5))
FIELDS = "ObjectId,FechaReporte,OcupacionPorcent,Puerto,Componente,Terminal,PatioContenedor,Opera,EditDate"
OPERATION_COLORS = {
    "Carbón": "#119AC4",
    "Carga general": "#1269B1",
    "Contenedores": "#A1C72B",
    "Granel (Alimenticio)": "#9B54A2",
    "Vehículos": "#F38925",
    "Granel líquido": "#D3A313",
}
OTHER_COLORS = ("#BE5963", "#448D69", "#6757C6", "#A86B2E", "#D7589E", "#4F7885")


def _json(url, params):
    response = requests.get(url, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(f"Vista_Ocupacion: {data['error']}")
    return data


def consultar_ocupacion(fecha_corte=None):
    """Devuelve registros de los siete días calendario, incluido hoy (Colombia).

    Los ID se piden primero y luego por lotes, sin depender del límite de 1000
    registros de una respuesta ArcGIS. Se descartan registros futuros.
    """
    corte = fecha_corte or datetime.now(COLOMBIA)
    corte = corte.astimezone(COLOMBIA)
    inicio = datetime.combine(corte.date() - timedelta(days=6), time.min, COLOMBIA)
    fin = datetime.combine(corte.date() + timedelta(days=1), time.min, COLOMBIA)
    utc = lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    where = (f"FechaReporte >= TIMESTAMP '{utc(inicio)}' "
             f"AND FechaReporte < TIMESTAMP '{utc(fin)}'")
    ids_data = _json(SERVICE_URL + "/query", {
        "f": "json", "where": where, "returnIdsOnly": "true",
        "returnGeometry": "false",
    })
    ids = sorted(ids_data.get("objectIds") or [])
    records = []
    for pos in range(0, len(ids), 200):
        data = _json(SERVICE_URL + "/query", {
            "f": "json", "objectIds": ",".join(map(str, ids[pos:pos + 200])),
            "outFields": FIELDS, "returnGeometry": "false",
        })
        records.extend(f["attributes"] for f in data.get("features", []))
    if len(records) != len(ids):
        raise RuntimeError(f"Vista_Ocupacion incompleta: {len(records)} de {len(ids)} registros")
    return [r for r in records if r.get("FechaReporte") is not None
            and r["FechaReporte"] <= corte.timestamp() * 1000], inicio.date(), corte.date()


def _label(value):
    return str(value).strip() if value is not None and str(value).strip() else None


def _valid_records(records, inicio, fin):
    """Filtra registros válidos sin eliminar respuestas que el tablero sí cuenta."""
    for row in records:
        comp = _label(row.get("Componente"))
        if comp not in ("Terminal", "Patio Contenedores"):
            continue
        zone = _label(row.get("Puerto"))
        operation = _label(row.get("Opera")) if comp == "Terminal" else "Patio Contenedores"
        try:
            val = float(row["OcupacionPorcent"])
            day = datetime.fromtimestamp(row["FechaReporte"] / 1000, timezone.utc).astimezone(COLOMBIA).date()
        except (ValueError, TypeError, OverflowError, KeyError):
            continue
        if not zone or not operation or not isfinite(val) or not inicio <= day <= fin:
            continue
        yield comp, zone, operation, day, val


def resumir(records, inicio, fin):
    """Promedio de todas las respuestas por componente, zona, operación y día."""
    groups = defaultdict(list)
    for comp, zone, operation, day, value in _valid_records(records, inicio, fin):
        groups[(comp, zone, operation, day)].append(value)
    return {key: sum(values) / len(values) for key, values in groups.items()}


def _operation_colors(operations):
    unknown = [op for op in sorted(operations) if op not in OPERATION_COLORS]
    return {**OPERATION_COLORS, **{op: OTHER_COLORS[i % len(OTHER_COLORS)]
                                    for i, op in enumerate(unknown)}}


def _font(size, bold=False):
    root = "/usr/share/fonts/truetype/dejavu/DejaVuSans"
    try:
        return ImageFont.truetype(root + ("-Bold" if bold else "") + ".ttf", size)
    except OSError:
        return ImageFont.load_default()


def _text(draw, xy, value, size=18, color="#293348", bold=False, anchor=None):
    draw.text(xy, str(value), font=_font(size, bold), fill=color, anchor=anchor)


def _fmt(value):
    return f"{value:.1f}".replace(".", ",") + " %"


def _base(title, subtitle, width, height):
    im = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(im)
    _text(d, (28, 22), title, 25, "#173477", True)
    _text(d, (28, 60), subtitle, 15, "#5A6778")
    return im, d


def grafica_zonas(groups, component, path):
    """Barras del mismo último día para todas las zonas, agrupadas por zona."""
    data = [(zone, op, day, value) for (comp, zone, op, day), value in groups.items() if comp == component]
    last_day = max((day for _, _, day, _ in data), default=None)
    items = sorted([(z, op, v) for z, op, day, v in data if day == last_day],
                   key=lambda x: (x[0].casefold(), x[1].casefold()))
    zones = sorted({z for z, _, _ in items})
    # La altura responde a las filas presentes; cada zona recibe su encabezado.
    top, row_height, group_gap = 110, 47, 13
    bottom = top + len(items)*row_height + len(zones)*(32+group_gap) + 12
    height = max(260, bottom+63)
    im, d = _base("Ocupación por zona portuaria" + (" y operación" if component == "Terminal" else ""),
                  f"Promedio de reportes · {last_day:%d/%m/%Y} (hora Colombia)" if last_day else
                  "Sin registros en el período", 1120, height)
    if not data:
        _text(d, (560, height/2), "Sin registros en los últimos siete días", 24, anchor="mm")
    else:
        colors = _operation_colors({op for _, op, _ in items})
        high = max(100, ceil(max(v for _, _, v in items) / 20) * 20)
        left, right = 280, 1000
        for tick in range(0, high + 1, 20):
            x = left + (right-left)*tick/high
            d.line((x, top, x, bottom), fill="#E6EBEF", width=2)
            _text(d, (x, bottom+8), str(tick), 13, anchor="mt")
        y = top
        for zone in zones:
            _text(d, (28, y), zone, 19, "#173477", True)
            y += 32
            for _, op, value in [item for item in items if item[0] == zone]:
                bar_y = y + row_height/2
                if component == "Terminal":
                    _text(d, (left-12, bar_y), op, 15, anchor="rm")
                else:
                    _text(d, (left-12, bar_y), "Patio de contenedores", 15, anchor="rm")
                w = (right-left)*max(0, value)/high
                color = colors[op] if component == "Terminal" else "#A1C72B"
                d.rectangle((left, bar_y-12, left+w, bar_y+12), fill=color)
                _text(d, (min(left+w+8, 1030), bar_y), _fmt(value), 14, anchor="lm")
                y += row_height
            y += group_gap
    im.save(path)
    im.close()


def grafica_tiempo(records, component, inicio, fin, path):
    """Terminales: por operación; patios: por zona, aun con un solo día."""
    samples = defaultdict(list)
    for comp, zone, op, day, value in _valid_records(records, inicio, fin):
        if comp == component:
            samples[(op if component == "Terminal" else zone, day)].append(value)
    series = defaultdict(dict)
    for (name, day), values in samples.items():
        series[name][day] = sum(values)/len(values)
    im, d = _base("Evolución diaria de terminales" if component == "Terminal" else
                  "Evolución diaria de patios de contenedores",
                  "Promedio de todos los reportes por día y operación" if component == "Terminal" else
                  "Promedio diario por zona portuaria; sin datos no se traza línea",
                  1120, 590)
    days = [inicio + timedelta(days=i) for i in range((fin-inicio).days+1)]
    left, right, top, bottom = 85, 1050, 125, 390
    maxval = max([v for history in series.values() for v in history.values()] or [100])
    ymax = max(100, ceil(maxval / 20)*20)
    for tick in range(0, ymax+1, 20):
        y = bottom-(bottom-top)*tick/ymax
        d.line((left, y, right, y), fill="#E6EBEF", width=2)
        _text(d, (left-13, y), str(tick), 14, anchor="rm")
    for i, day in enumerate(days):
        x = left + (right-left)*i/max(1, len(days)-1)
        _text(d, (x, bottom+14), day.strftime("%d/%m"), 15, anchor="mt")
    if not series:
        _text(d, (560, 270), "Sin registros en los últimos siete días", 24, anchor="mm")
    colors = _operation_colors(series)
    zone_colors = {"Cartagena": "#A1C72B", "Barranquilla": "#1269B1"}
    other_zones = [name for name in sorted(series) if name not in zone_colors]
    zone_colors.update({name: OTHER_COLORS[i % len(OTHER_COLORS)]
                        for i, name in enumerate(other_zones)})
    for index, (name, history) in enumerate(sorted(series.items())):
        if component == "Terminal":
            color = colors[name]
        else:
            color = zone_colors[name]
        previous = None
        for i, day in enumerate(days):
            if day not in history:
                previous = None
                continue
            x = left + (right-left)*i/max(1, len(days)-1)
            y = bottom-(bottom-top)*history[day]/ymax
            if previous: d.line((*previous, x, y), fill=color, width=4)
            d.ellipse((x-5,y-5,x+5,y+5), fill=color)
            previous = x, y
        col, row = index % 3, index // 3
        if row < 5:
            x, y = 33 + col*365, 450 + row*26
            if len(history) == 1:
                d.ellipse((x+7, y+3, x+19, y+15), fill=color)
            else:
                d.line((x, y+9, x+21, y+9), fill=color, width=4)
            legend_name = f"{name} (1 día)" if component != "Terminal" and len(history) == 1 else name
            _text(d, (x+28, y), legend_name[:31], 13)
    if len(series) > 15:
        _text(d, (900, 564), f"+{len(series)-15} series", 12)
    im.save(path)
    im.close()


def graficas_ocupacion(records, inicio, fin, carpeta):
    groups = resumir(records, inicio, fin)
    result = {}
    for component, key in (("Terminal", "terminal"), ("Patio Contenedores", "patio")):
        barras = Path(carpeta) / f"{key}_zonas.png"
        tiempo = Path(carpeta) / f"{key}_tiempo.png"
        grafica_zonas(groups, component, barras)
        grafica_tiempo(records, component, inicio, fin, tiempo)
        last_day = max((day for comp, z, op, day in groups if comp == component), default=None)
        latest_values = [value for comp, z, op, day, value in _valid_records(records, inicio, fin)
                         if comp == component and day == last_day]
        result[key] = (barras, tiempo, latest_values,
                       len({z for comp, z, op, day in groups if comp == component and day == last_day}),
                       len({z for comp, z, op, day in groups if comp == component}))
    return result
