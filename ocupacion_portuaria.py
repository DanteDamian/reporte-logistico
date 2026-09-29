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
COLORS = ("#119AC4", "#A1C72B", "#A66AA4", "#F38925", "#3265AF", "#E3BB27", "#448D69", "#BE5963")


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


def resumir(records, inicio, fin):
    """Promedio simple de las unidades reportantes por zona, operación y día.

    Si una unidad envía más de una respuesta para la misma fecha, se toma la
    versión con EditDate/ObjectId mayor. Se preservan 0 y valores > 100.
    """
    latest = {}
    for row in records:
        comp = _label(row.get("Componente"))
        if comp not in ("Terminal", "Patio Contenedores"):
            continue
        zone = _label(row.get("Puerto"))
        unit = _label(row.get("Terminal" if comp == "Terminal" else "PatioContenedor"))
        operation = _label(row.get("Opera")) if comp == "Terminal" else "Patio Contenedores"
        try:
            val = float(row["OcupacionPorcent"])
            day = datetime.fromtimestamp(row["FechaReporte"] / 1000, timezone.utc).astimezone(COLOMBIA).date()
        except (ValueError, TypeError, OverflowError, KeyError):
            continue
        if not zone or not unit or not operation or not isfinite(val) or not inicio <= day <= fin:
            continue
        key = comp, zone, operation, unit, day
        priority = (row.get("EditDate") or 0, row.get("ObjectId") or 0)
        if key not in latest or priority > latest[key][0]:
            latest[key] = (priority, val)
    groups = defaultdict(list)
    for (comp, zone, operation, unit, day), (_, value) in latest.items():
        groups[(comp, zone, operation, day)].append(value)
    return {key: sum(values) / len(values) for key, values in groups.items()}


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
    """Barras de la fecha más reciente disponible, agrupadas por zona y operación."""
    data = [(zone, op, day, value) for (comp, zone, op, day), value in groups.items() if comp == component]
    im, d = _base("Ocupación por zona portuaria" + (" y operación" if component == "Terminal" else ""),
                  "Promedio de unidades reportantes · último día con registros por zona", 1120, 550)
    if not data:
        _text(d, (560, 265), "Sin registros en los últimos siete días", 24, anchor="mm")
    else:
        last_day = {zone: max(day for z, op, day, value in data if z == zone) for zone, _, _, _ in data}
        items = sorted([(z, op, v, day) for z, op, day, v in data if day == last_day[z]],
                       key=lambda x: (x[0].casefold(), x[1].casefold()))
        ops = sorted({op for _, op, _, _ in items})
        colors = ({op: COLORS[i % len(COLORS)] for i, op in enumerate(ops)}
                  if component == "Terminal" else
                  {op: "#A1C72B" for op in ops})
        high = max(100, ceil(max(v for _, _, v, _ in items) / 20) * 20)
        left, right, top, bottom = 325, 1000, 112, 490
        for tick in range(0, high + 1, 20):
            x = left + (right-left)*tick/high
            d.line((x, top, x, bottom), fill="#E6EBEF", width=2)
            _text(d, (x, bottom+8), str(tick), 13, anchor="mt")
        row = min(46, (bottom-top)/max(len(items), 1))
        for idx, (zone, op, value, day) in enumerate(items):
            y = top + (idx+.5)*row
            label = zone if component != "Terminal" else f"{zone} · {op}"
            if len(label) > 38: label = label[:35] + "…"
            _text(d, (left-12, y), label, 15, anchor="rm")
            w = (right-left)*max(0, value)/high
            d.rectangle((left, y-11, left+w, y+11), fill=colors[op])
            _text(d, (min(left+w+8, 1030), y), _fmt(value), 14, anchor="lm")
        _text(d, (28, 521), "Fecha por zona: " + " · ".join(
            f"{z} {day:%d/%m}" for z, day in sorted(last_day.items()))[:115], 13)
    im.save(path)
    im.close()


def grafica_tiempo(groups, component, inicio, fin, path):
    """Serie diaria por zona y operación. Ausencias se muestran como huecos."""
    series = defaultdict(dict)
    for (comp, zone, op, day), value in groups.items():
        if comp == component:
            series[(zone, op)][day] = value
    im, d = _base("Evolución diaria de terminales" if component == "Terminal" else
                  "Evolución diaria de patios de contenedores",
                  "Porcentaje promedio por zona" + (" y tipo de operación" if component == "Terminal" else ""),
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
    operations = sorted({op for zone, op in series})
    zones = sorted({zone for zone, op in series})
    for index, ((zone, op), history) in enumerate(sorted(series.items())):
        if component == "Terminal":
            color = COLORS[operations.index(op) % len(COLORS)]
        else:
            color = ("#A1C72B", "#119AC4", "#A66AA4", "#F38925")[zones.index(zone) % 4]
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
        name = f"{zone} · {op}" if component == "Terminal" else zone
        col, row = index % 3, index // 3
        if row < 5:
            x, y = 33 + col*365, 450 + row*26
            d.line((x, y+9, x+21, y+9), fill=color, width=4)
            _text(d, (x+28, y), name[:31], 13)
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
        grafica_tiempo(groups, component, inicio, fin, tiempo)
        result[key] = (barras, tiempo, [v for (comp, z, op, day), v in groups.items() if comp == component],
                       len({z for comp, z, op, day in groups if comp == component}))
    return result
