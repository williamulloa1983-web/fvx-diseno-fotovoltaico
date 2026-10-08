"""FVx: aplicación local para predimensionamiento de sistemas solares on-grid."""
from io import BytesIO
from math import ceil, floor
import json
import re
import csv
import os
import subprocess
import tempfile
from html import escape
import unicodedata
from urllib.parse import quote
from urllib.request import Request, urlopen

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from flask import Flask, Response, render_template, request, url_for
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, white
from reportlab.lib.utils import ImageReader

app = Flask(__name__)
DEFAULTS = {"consumo": 900.0, "hsp": 4.5, "cobertura": 100.0, "pr": .80,
            "modulo_wp": 550.0, "voc": 49.5, "coef_voc": .28, "temp_min": 10.0,
            "vdc_max": 1000.0, "dcac": 1.20}

def values(source):
    data = {}
    for field, default in DEFAULTS.items():
        try: data[field] = float(source.get(field, default))
        except (TypeError, ValueError): data[field] = default
    return data

def fmt(value, places=2):
    return f"{value:,.{places}f}".replace(",", "X").replace(".", ",").replace("X", ".")

def design(d):
    positive = ("consumo", "hsp", "cobertura", "pr", "modulo_wp", "voc", "coef_voc", "vdc_max", "dcac")
    if any(d[k] <= 0 for k in positive) or d["cobertura"] > 100 or d["pr"] > 1:
        raise ValueError("Revisa los valores ingresados: deben ser positivos; cobertura y PR no pueden superar 100% y 1.")
    objective = d["consumo"] * d["cobertura"] / 100
    daily = objective / 30
    needed_kwp = daily / (d["hsp"] * d["pr"])
    required_modules = ceil(needed_kwp * 1000 / d["modulo_wp"])
    # Voc aumenta con el frío: se valida el máximo número de módulos en serie.
    cold_voc = d["voc"] * (1 + d["coef_voc"] / 100 * (25 - d["temp_min"]))
    max_series = floor(d["vdc_max"] / cold_voc)
    if max_series < 1: raise ValueError("La tensión máxima del inversor no admite ni un módulo.")
    modules_series = min(max_series, required_modules)
    strings = ceil(required_modules / modules_series)
    modules = strings * modules_series
    installed_kwp = modules * d["modulo_wp"] / 1000
    inverter_kw = installed_kwp / d["dcac"]
    annual = installed_kwp * d["hsp"] * 365 * d["pr"]
    steps = [
        ("Energía objetivo", f"{fmt(d['consumo'], 0)} kWh/mes × {fmt(d['cobertura'], 0)}% = {fmt(objective, 1)} kWh/mes."),
        ("Demanda diaria", f"{fmt(objective, 1)} ÷ 30 = {fmt(daily)} kWh/día."),
        ("Potencia FV requerida", f"{fmt(daily)} ÷ ({fmt(d['hsp'], 1)} HSP × PR {fmt(d['pr'])}) = {fmt(needed_kwp)} kWp."),
        ("Validación de tensión", f"Voc a {fmt(d['temp_min'], 0)} °C: {fmt(cold_voc)} V/módulo; máximo: {max_series} módulos/string."),
        ("Arreglo seleccionado", f"{strings} strings × {modules_series} módulos = {modules} módulos de {fmt(d['modulo_wp'], 0)} Wp ({fmt(installed_kwp)} kWp)."),
        ("Inversor y producción", f"Inversor ≥ {fmt(inverter_kw)} kWac (DC/AC {fmt(d['dcac'])}); producción: {fmt(annual, 0)} kWh/año."),
    ]
    return {"pdc": fmt(installed_kwp), "modulos": modules, "pac": fmt(inverter_kw), "energia_anual": fmt(annual, 0),
            "strings": strings, "modulos_serie": modules_series, "voc_string": fmt(cold_voc * modules_series, 0),
            "cobertura": fmt(d["cobertura"], 0), "hsp": fmt(d["hsp"], 1), "pr": fmt(d["pr"]), "steps": steps,
            "warning": f"El máximo eléctrico es {max_series} módulos por string." if max_series < 8 else None}

BATTERY_CATALOG = {
    "Pylontech US3000C": (48.0, 74.0),
    "Dyness A48100": (48.0, 100.0),
    "Narada REXC": (48.0, 100.0),
    "Litio LiFePO4 referencial": (48.0, 100.0),
}

def _number(source, key, default, minimum=None, maximum=None):
    try: value = float(source.get(key, default))
    except (TypeError, ValueError): value = default
    if minimum is not None: value = max(minimum, value)
    if maximum is not None: value = min(maximum, value)
    return value

def battery_design(d, source):
    """Dimensiona el banco para topologías off-grid e híbrida.

    En híbrido la red cubre la energía no respaldada. El banco se calcula sobre
    la fracción de respaldo elegida, sin ser inferior a las cargas críticas.
    """
    mode = source.get("modoSistema", "on-grid").lower()
    offgrid = mode == "off-grid"
    hybrid = mode in ("híbrido", "hibrido", "hybrid")
    storage = offgrid or hybrid
    brand = source.get("marcaBateria", "Dyness A48100")
    default_v, default_ah = BATTERY_CATALOG.get(brand, BATTERY_CATALOG["Dyness A48100"])
    voltage = _number(source, "bateriaV", default_v, 12, 60)
    ah = _number(source, "bateriaAh", default_ah, 20, 500)
    autonomy = _number(source, "autonomiaDias", 1, .25, 5)
    dod = _number(source, "profundidadDescarga", 80, 50, 95) / 100
    efficiency = _number(source, "eficienciaBateria", 90, 70, 98) / 100
    support_fraction = 1.0 if offgrid else _number(source, "fraccionRespaldo", 50, 0, 100) / 100
    critical_fraction = _number(source, "cargasCriticas", 30, 0, 100) / 100
    daily = d["consumo"] * d["cobertura"] / 100 / 30
    per_battery = voltage * ah / 1000
    supported_energy = daily * autonomy * support_fraction
    critical_energy = daily * autonomy * critical_fraction
    required_energy = max(supported_energy, critical_energy) if storage else 0
    required = required_energy / (dod * efficiency) if storage else 0
    raw_count = max(1, ceil(required / per_battery)) if storage else 0
    series = max(1, ceil(48 / voltage))
    parallel = ceil(raw_count / series) if storage else 0
    count = series * parallel if storage else 0
    nominal = count * per_battery
    usable = nominal * dod * efficiency
    actual_autonomy = usable / daily if daily else 0
    return {"offgrid": offgrid, "hybrid": hybrid, "storage": storage,
            "mode": "off-grid" if offgrid else ("híbrido" if hybrid else "on-grid"), "brand": brand,
            "voltage": voltage, "ah": ah, "autonomy": autonomy, "dod": dod, "efficiency": efficiency,
            "daily": daily, "per_battery": per_battery, "required": required, "count": count,
            "series": series, "parallel": parallel, "nominal": nominal, "usable": usable,
            "actual_autonomy": actual_autonomy, "support_fraction": support_fraction,
            "critical_fraction": critical_fraction, "supported_energy": supported_energy,
            "critical_energy": critical_energy, "required_energy": required_energy}

def _build_excel_fallback(payload):
    """Libro compatible con servidores web sin el runtime local de Codex."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
    from openpyxl.utils import get_column_letter
    wb = Workbook(); summary = wb.active; summary.title = "Resumen"; calc = wb.create_sheet("Calculo"); bat = wb.create_sheet("Baterias")
    orange, navy, pale, thin = "F57C17", "134D80", "EAF1F5", Side(style="thin", color="B7C8D6")
    def setup(ws, title):
        ws.sheet_view.showGridLines = False; ws.merge_cells("A2:D2"); ws["A2"] = title; ws["A2"].font = Font(name="Arial", size=15, bold=True, color=navy)
        for col, width in enumerate((28, 32, 24, 34), 1): ws.column_dimensions[get_column_letter(col)].width = width
    def table(ws, start, headers, rows):
        for col, value in enumerate(headers, 1):
            cell = ws.cell(start, col, value); cell.fill = PatternFill("solid", fgColor=orange); cell.font = Font(bold=True, color="FFFFFF")
        for row_index, row in enumerate(rows, start + 1):
            for col, value in enumerate(row, 1):
                cell = ws.cell(row_index, col, value); cell.border = Border(left=thin, right=thin, top=thin, bottom=thin); cell.alignment = Alignment(vertical="center", wrap_text=True)
                if row_index % 2 == 0: cell.fill = PatternFill("solid", fgColor=pale)
    setup(summary, f"TWE Welt Energy | Hoja de cálculo {payload.get('mode', 'on-grid')}")
    table(summary, 5, ["Dato", "Valor", "Dato", "Valor"], [("Cliente", payload["client"], "Proyecto", f"Sistema {payload.get('mode', 'on-grid')}"), ("Ubicación", payload["address"], "Latitud", payload["lat"]), ("Longitud", payload["lng"], "Recurso solar", payload["nasa"]), ("Operador", payload["operator"], "Tarifa COP/kWh", payload["tariff"])])
    table(summary, 11, ["Resultado", "Valor", "Unidad"], [("Potencia FV instalada", payload["pdc"], "kWp"), ("Módulos", payload["modules"], "unidades"), ("Configuración", payload["strings"], "strings x módulos"), ("Inversor mínimo", payload["pac"], "kWac"), ("Producción anual", payload["annual"], "kWh/año")])
    setup(calc, "Cálculo técnico fotovoltaico")
    table(calc, 5, ["Variable", "Valor", "Unidad"], [("Consumo mensual", payload["consumo"], "kWh/mes"), ("Horas sol pico", payload["hsp"], "h/día"), ("Cobertura", payload["coverage"], "%"), ("Performance ratio", payload["pr"], "0-1"), ("Potencia módulo", payload["moduleWp"], "Wp"), ("Tensión máxima DC", payload["vdcMax"], "V")])
    table(calc, 14, ["Resultado", "Valor", "Unidad"], [("Demanda diaria", payload["consumo"] * payload["coverage"] / 30, "kWh/día"), ("Potencia FV", payload["pdc"], "kWp"), ("Módulos", payload["modules"], "unidades"), ("Producción anual", payload["annual"], "kWh/año")])
    setup(bat, "Banco de baterías y respaldo energético")
    table(bat, 5, ["Variable", "Valor", "Unidad"], [("Tipo de sistema", payload.get("mode", "on-grid"), ""), ("Marca / referencia", payload.get("batteryBrand", "No aplica"), ""), ("Autonomía solicitada", payload.get("autonomy", 0), "días"), ("Respaldo configurado", payload.get("supportFraction", 0) * 100, "%"), ("Cargas críticas", payload.get("criticalFraction", 0) * 100, "% de demanda"), ("Energía mínima respaldada", payload.get("batteryRequiredEnergy", 0), "kWh"), ("Tensión por batería", payload.get("batteryV", 0), "V"), ("Capacidad por batería", payload.get("batteryAh", 0), "Ah"), ("Banco seleccionado", payload.get("batteryCount", 0), "unidades"), ("Configuración", f"{payload.get('batterySeries', 0)}S x {payload.get('batteryParallel', 0)}P", ""), ("Capacidad nominal", payload.get("batteryNominal", 0), "kWh"), ("Capacidad útil", payload.get("batteryUsable", 0), "kWh"), ("Autonomía calculada", payload.get("batteryAutonomy", 0), "días")])
    output = BytesIO(); wb.save(output); return output.getvalue()

def report_image(r):
    fig = plt.figure(figsize=(14, 8), facecolor="#f5f8f4")
    fig.text(.05, .94, "FVx | Diseño fotovoltaico on-grid", fontsize=22, weight="bold", color="#16362d")
    fig.text(.05, .905, "Memoria de cálculo preliminar — validar con RETIE y operador de red", fontsize=10, color="#64776e")
    metrics = [("Potencia FV", f"{r['pdc']} kWp"), ("Módulos", str(r['modulos'])), ("Inversor mínimo", f"{r['pac']} kWac"), ("Producción anual", f"{r['energia_anual']} kWh"), ("Strings", f"{r['strings']} × {r['modulos_serie']}")]
    for i, (label, value) in enumerate(metrics):
        x = .05 + i * .185
        fig.patches.append(plt.Rectangle((x, .77), .165, .09, transform=fig.transFigure, fc="white", ec="#d9e4dc"))
        fig.text(x+.012, .815, value, fontsize=13, weight="bold", color="#08765d")
        fig.text(x+.012, .788, label, fontsize=8, color="#64776e")
    fig.text(.05, .70, "Proceso del diseño", fontsize=14, weight="bold", color="#16362d")
    y = .66
    for index, (title, text) in enumerate(r["steps"], 1):
        fig.text(.055, y, str(index), fontsize=9, weight="bold", bbox={"boxstyle":"circle", "fc":"#d7ed78", "ec":"none"})
        fig.text(.082, y, title, fontsize=10, weight="bold", color="#16362d")
        fig.text(.082, y-.025, text, fontsize=8.5, color="#64776e")
        y -= .075
    fig.text(.05, .18, "Esquema: Arreglo FV → Protección DC → Inversor on-grid → Protección AC → Medidor → Red", fontsize=12, weight="bold", color="#16362d")
    fig.text(.05, .05, "Documento de predimensionamiento. La ingeniería final debe verificar protecciones, calibres y normativa.", fontsize=8, color="#64776e")
    output = BytesIO(); fig.savefig(output, format="png", dpi=180, bbox_inches="tight", facecolor=fig.get_facecolor()); plt.close(fig); output.seek(0)
    return output

@app.route("/")
def home():
    data = values(request.args)
    try: result = design(data)
    except ValueError: data = DEFAULTS.copy(); result = design(data)
    return render_template("index.html", data=data, r=result, png_url=url_for("image", **data))

@app.route("/lamina.png")
def image():
    try: result = design(values(request.args))
    except ValueError: return Response("Parámetros inválidos", status=400)
    return Response(report_image(result).getvalue(), mimetype="image/png", headers={"Content-Disposition": "attachment; filename=diseno-fotovoltaico-on-grid.png"})

@app.route("/health")
def health(): return {"estado": "correcto", "python": "activo", "aplicacion": "FVx On-grid y Off-grid"}

@app.route("/api/geocode")
def geocode():
    """Convierte una dirección elegida por el cliente en coordenadas del proyecto."""
    query = request.args.get("q", "").strip()
    if not query:
        return {"error": "Ingresa una dirección."}, 400
    try:
        # Las fincas no siempre están indexadas por nombre. Se intenta la dirección
        # completa y después municipio/departamento, manteniendo el texto original.
        normalized = re.sub(r"\bsantasof[ií]a\b", "Santa Sofía", query, flags=re.IGNORECASE)
        words = normalized.replace(",", " ").split()
        candidates = [query, normalized, f"{query}, Colombia", f"{normalized}, Colombia"]
        if len(words) >= 3:
            candidates.extend([" ".join(words[-3:]) + ", Colombia", " ".join(words[-2:]) + ", Colombia"])
        location = None
        for candidate in dict.fromkeys(candidates):
            url = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=" + quote(candidate)
            req = Request(url, headers={"User-Agent": "FVx-on-grid-design/1.0 (local application)"})
            with urlopen(req, timeout=12) as response:
                result = json.load(response)
            if result:
                location = result[0]
                break
        if not location:
            return {"error": "No se encontraron coordenadas. Prueba incluyendo municipio y departamento."}, 404
        return {"lat": float(location["lat"]), "lng": float(location["lon"]), "label": location["display_name"]}
    except Exception:
        return {"error": "No fue posible obtener las coordenadas en este momento."}, 502

@app.route("/api/nasa-solar")
def nasa_solar():
    """Consulta irradiación global horizontal climatológica de NASA POWER para un punto."""
    try:
        lat, lng = float(request.args["lat"]), float(request.args["lng"])
        if not (-90 <= lat <= 90 and -180 <= lng <= 180): raise ValueError
    except (KeyError, ValueError):
        return {"error": "Coordenadas inválidas."}, 400
    try:
        url = ("https://power.larc.nasa.gov/api/temporal/climatology/point?"
               f"parameters=ALLSKY_SFC_SW_DWN&community=RE&longitude={lng}&latitude={lat}&format=JSON")
        with urlopen(Request(url, headers={"User-Agent": "FVx-on-grid-design/1.0"}), timeout=15) as response:
            payload = json.load(response)
        values = payload["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]
        annual = values["ANN"]
        return {"hsp": annual, "units": "kW-hr/m^2/day", "source": "NASA POWER", "period": "2001–2020 climatología", "coordinates": payload["geometry"]["coordinates"]}
    except Exception:
        return {"error": "NASA POWER no respondió. Conservamos la HSP ingresada manualmente."}, 502

@app.route("/hoja-calculo.xlsx")
@app.route("/hoja-calculo.csv")
def spreadsheet():
    """Entrega un libro Excel editable y presentado para el cliente."""
    try:
        d = values(request.args)
        r = design(d)
    except ValueError as exc:
        return Response(str(exc), status=400)
    def number(value):
        return float(str(value).replace(".", "").replace(",", "."))
    battery = battery_design(d, request.args)
    payload = {
        "client": request.args.get("cliente", "Por confirmar"),
        "address": request.args.get("address", "Pendiente de seleccionar"),
        "lat": request.args.get("lat", ""), "lng": request.args.get("lng", ""),
        "nasa": request.args.get("nasa", "NASA POWER - HSP del diseño"),
        "operator": request.args.get("operador", "Por confirmar"),
        "tariff": float(request.args.get("tarifa", 0) or 0),
        "tariffPeriod": request.args.get("periodoTarifa", "Por confirmar"),
        "selfConsumption": float(request.args.get("autoconsumo", 90) or 90) / 100,
        "consumo": d["consumo"], "hsp": d["hsp"], "coverage": d["cobertura"] / 100,
        "pr": d["pr"], "moduleWp": d["modulo_wp"], "voc": d["voc"], "coefVoc": d["coef_voc"],
        "tempMin": d["temp_min"], "vdcMax": d["vdc_max"], "dcac": d["dcac"],
        "pdc": number(r["pdc"]), "modules": r["modulos"], "strings": f"{r['strings']} x {r['modulos_serie']}",
        "pac": number(r["pac"]), "annual": number(r["energia_anual"]),
        "mode": battery["mode"], "batteryBrand": battery["brand"], "autonomy": battery["autonomy"],
        "batteryV": battery["voltage"], "batteryAh": battery["ah"], "dod": battery["dod"],
        "batteryEfficiency": battery["efficiency"], "batteryCount": battery["count"],
        "batterySeries": battery["series"], "batteryParallel": battery["parallel"],
        "batteryNominal": battery["nominal"], "batteryUsable": battery["usable"],
        "batteryAutonomy": battery["actual_autonomy"], "batteryRequired": battery["required"],
        "supportFraction": battery["support_fraction"], "criticalFraction": battery["critical_fraction"],
        "batteryRequiredEnergy": battery["required_energy"],
    }
    node = os.environ.get("FVX_NODE", r"C:\Users\Usuario\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe")
    if not os.path.exists(node): node = "node"
    try:
        with tempfile.TemporaryDirectory(prefix="fvx_excel_") as folder:
            input_path, output_path = os.path.join(folder, "datos.json"), os.path.join(folder, "hoja-calculo-fotovoltaico.xlsx")
            with open(input_path, "w", encoding="utf-8") as stream: json.dump(payload, stream)
            result = subprocess.run([node, os.path.join(app.root_path, "build_excel.mjs"), input_path, output_path], capture_output=True, text=True, timeout=45)
            if result.returncode or not os.path.exists(output_path):
                raise RuntimeError(result.stderr[-500:] or "No se pudo generar el libro Excel.")
            with open(output_path, "rb") as stream: content = stream.read()
    except Exception:
        # En hosting público no existe el runtime local usado durante desarrollo.
        try: content = _build_excel_fallback(payload)
        except Exception as exc: return Response(f"No fue posible generar el Excel: {exc}", status=500)
    filename = f"hoja-calculo-{_filename_client(request.args)}.xlsx"
    return Response(content, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.route("/hoja-calculo-preview")
def spreadsheet_preview():
    """Vista previa navegable del contenido que se exportará al libro Excel."""
    try:
        d, r, address, lat, lng, client = _project_data(request.args)
    except ValueError as exc:
        return Response(str(exc), status=400)
    battery = battery_design(d, request.args)
    rows = [
        ("Potencia FV instalada", f"{r['pdc']} kWp"), ("Módulos fotovoltaicos", str(r["modulos"])),
        ("Configuración", f"{r['strings']} x {r['modulos_serie']}"), ("Inversor AC mínimo", f"{r['pac']} kWac"),
        ("Producción anual estimada", f"{r['energia_anual']} kWh/año"),
    ]
    if battery["storage"]:
        rows.extend([("Banco de baterías", f"{battery['count']} × {battery['brand']}"),
                     ("Respaldo / cargas críticas", f"{fmt(battery['support_fraction']*100, 0)} % / {fmt(battery['critical_fraction']*100, 0)} %"),
                     ("Energía mínima respaldada", f"{fmt(battery['required_energy'])} kWh"),
                     ("Energía útil disponible", f"{fmt(battery['usable'])} kWh"),
                     ("Autonomía estimada", f"{fmt(battery['actual_autonomy'])} días")])
    inputs = [("Tipo de sistema", battery["mode"]), ("Consumo mensual", f"{fmt(d['consumo'])} kWh/mes"), ("HSP", f"{fmt(d['hsp'], 4)} h/día"),
              ("Cobertura", f"{fmt(d['cobertura'], 0)} %"), ("Performance ratio", fmt(d["pr"])),
              ("Potencia de módulo", f"{fmt(d['modulo_wp'], 0)} Wp"), ("Tensión máxima DC", f"{fmt(d['vdc_max'], 0)} V")]
    def body(items): return "".join(f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>" for label, value in items)
    coordinates = f"{lat:.6f}, {lng:.6f}" if lat is not None and lng is not None else "Pendientes de seleccionar"
    document = f"""<!doctype html><html lang='es'><meta charset='utf-8'><style>
    body{{font-family:Arial,sans-serif;color:#19202a;margin:0;background:#f3f5f7;padding:28px}}.page{{max-width:820px;margin:auto;background:#fff;padding:34px;box-shadow:0 4px 18px #0002}}h1{{color:#134d80;margin:0;font-size:24px}}.line{{height:3px;background:#f57c17;margin:13px 0 24px}}h2{{color:#f57c17;font-size:16px;margin:27px 0 10px}}.data{{display:grid;grid-template-columns:1fr 1fr;gap:0 20px}}.data p{{margin:5px 0}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border:1px solid #b7c8d6;text-align:left}}th{{background:#eaf1f5;color:#134d80;width:52%}}.note{{font-size:12px;color:#5e646b;margin-top:25px}}</style>
    <div class='page'><h1>TWE Welt Energy | Vista previa de Excel</h1><div class='line'></div>
    <div class='data'><p><b>Cliente:</b> {escape(client)}</p><p><b>Ubicación:</b> {escape(address)}</p><p><b>Coordenadas:</b> {coordinates}</p><p><b>Recurso:</b> {escape(request.args.get('nasa', 'HSP del diseño'))}</p></div>
    <h2>Resultados del diseño</h2><table>{body(rows)}</table><h2>Entradas editables incluidas</h2><table>{body(inputs)}</table>
    <p class='note'>Esta es la información que se exportará al libro Excel, que incluirá además una hoja de cálculo editable con las fórmulas.</p></div>"""
    return Response(document, mimetype="text/html; charset=utf-8")

ORANGE, BLUE, TEXT, MUTED, ROW = (HexColor("#F57C17"), HexColor("#134D80"),
                                   HexColor("#17191D"), HexColor("#5E646B"), HexColor("#EAF1F5"))

def _safe(text):
    return str(text).encode("latin-1", "replace").decode("latin-1")

def _filename_client(source):
    """Convierte el cliente en una parte de nombre de archivo compatible con Windows."""
    client = source.get("cliente", "").strip() or "cliente"
    normalized = unicodedata.normalize("NFKD", client).encode("ascii", "ignore").decode("ascii")
    clean = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_")
    return clean[:60] or "cliente"

def _project_data(source):
    d = values(source)
    r = design(d)
    address = source.get("address", "Ubicación pendiente de confirmar")
    client = source.get("cliente", "Por confirmar").strip() or "Por confirmar"
    try:
        lat, lng = float(source.get("lat", "")), float(source.get("lng", ""))
    except (TypeError, ValueError):
        lat, lng = None, None
    return d, r, address, lat, lng, client

def _satellite_image(lat, lng):
    """Obtiene una imagen satelital centrada exactamente en la coordenada elegida."""
    if lat is None or lng is None:
        return None
    delta = 0.006
    bbox = f"{lng-delta},{lat-delta},{lng+delta},{lat+delta}"
    url = ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export?"
           f"bbox={quote(bbox)}&bboxSR=4326&imageSR=4326&size=1100%2C700&format=png32&transparent=false&f=image")
    try:
        req = Request(url, headers={"User-Agent": "FVx-on-grid-design/1.0"})
        with urlopen(req, timeout=20) as response:
            return BytesIO(response.read())
    except Exception:
        return None

def _header(pdf, title, page):
    page_w, page_h = A4
    try:
        pdf.drawImage(app.static_folder + "/twe-logo.jpeg", 44, page_h - 62, width=70, height=30,
                      preserveAspectRatio=True, mask="auto")
    except Exception:
        pass
    pdf.setStrokeColor(ORANGE); pdf.setLineWidth(1.2); pdf.line(44, page_h - 68, page_w - 44, page_h - 68)
    pdf.setFillColor(ORANGE); pdf.setFont("Helvetica-Bold", 18); pdf.drawCentredString(page_w / 2, page_h - 116, _safe(title))
    pdf.setFillColor(MUTED); pdf.setFont("Helvetica", 8); pdf.drawString(44, 31, "TWE Welt Energy - Diseño fotovoltaico")
    pdf.drawRightString(page_w - 44, 31, f"Página {page}")

def _section(pdf, y, title):
    pdf.setFillColor(ORANGE); pdf.setFont("Helvetica-Bold", 13); pdf.drawString(52, y, _safe(title))
    return y - 16

def _table(pdf, x, y, width, headers, rows, col_widths, row_height=21):
    """Tabla legible con encabezado naranja y filas alternadas."""
    pdf.setFont("Helvetica-Bold", 8)
    pdf.setFillColor(ORANGE); pdf.rect(x, y - row_height, width, row_height, stroke=0, fill=1)
    cursor = x
    for title, col_w in zip(headers, col_widths):
        pdf.setFillColor(white); pdf.drawString(cursor + 6, y - 14, _safe(title)); cursor += col_w
    for i, row in enumerate(rows):
        base_y = y - row_height * (i + 2)
        pdf.setFillColor(ROW if i % 2 == 0 else white); pdf.rect(x, base_y, width, row_height, stroke=0, fill=1)
        pdf.setStrokeColor(HexColor("#B7C8D6")); pdf.rect(x, base_y, width, row_height, stroke=1, fill=0)
        cursor = x
        for value, col_w in zip(row, col_widths):
            pdf.line(cursor, base_y, cursor, base_y + row_height); cursor += col_w
        pdf.line(x + width, base_y, x + width, base_y + row_height)
        cursor = x; pdf.setFont("Helvetica", 8.2); pdf.setFillColor(TEXT)
        for value, col_w in zip(row, col_widths):
            pdf.drawString(cursor + 6, base_y + 7, _safe(value)[:70]); cursor += col_w
    return y - row_height * (len(rows) + 1)

def _calculation_rows(d, r):
    objective = d["consumo"] * d["cobertura"] / 100
    daily = objective / 30
    required = daily / (d["hsp"] * d["pr"])
    cold_voc = d["voc"] * (1 + d["coef_voc"] / 100 * (25 - d["temp_min"]))
    return [
        ("1", f"Energía objetivo: {fmt(d['consumo'], 0)} kWh/mes x {fmt(d['cobertura'], 0)} % = {fmt(objective, 1)} kWh/mes."),
        ("2", f"Demanda diaria: {fmt(objective, 1)} / 30 = {fmt(daily)} kWh/día."),
        ("3", f"Potencia requerida: {fmt(daily)} / ({fmt(d['hsp'], 1)} HSP x PR {fmt(d['pr'])}) = {fmt(required)} kWp."),
        ("4", f"Tensión: Voc a {fmt(d['temp_min'], 0)} °C = {fmt(cold_voc)} V/módulo; máximo {r['modulos_serie']} módulos/string."),
        ("5", f"Arreglo: {r['strings']} strings x {r['modulos_serie']} módulos = {r['modulos']} módulos ({r['pdc']} kWp)."),
        ("6", f"Inversor: mínimo {r['pac']} kWac; producción anual {r['energia_anual']} kWh/año."),
    ]

def _draw_location_and_diagram(pdf, r, address, lat, lng, page_title, calculation_rows=None, battery=None):
    """Segunda página común: evidencia geográfica real y diagrama técnico."""
    _header(pdf, page_title, 2)
    page_w, page_h = A4
    y = _section(pdf, page_h - 150, "Evidencia geográfica y plano de implantación")
    pdf.setStrokeColor(BLUE); pdf.rect(48, 390, 238, 255, stroke=1, fill=0)
    pdf.setFillColor(BLUE); pdf.setFont("Helvetica-Bold", 9); pdf.drawString(58, 625, "VISTA SATELITAL DEL SITIO")
    if lat is not None and lng is not None:
        image = _satellite_image(lat, lng)
        if image:
            pdf.drawImage(ImageReader(image), 58, 435, width=218, height=170, preserveAspectRatio=True, mask="auto")
            pdf.setFillColor(ORANGE); pdf.circle(167, 520, 6, stroke=1, fill=1)
        else:
            pdf.setFillColor(ROW); pdf.rect(58, 435, 218, 170, stroke=0, fill=1)
            pdf.setFillColor(MUTED); pdf.setFont("Helvetica", 8); pdf.drawCentredString(167, 520, "No fue posible descargar la imagen satelital.")
        pdf.setFillColor(TEXT); pdf.setFont("Helvetica", 7.4); pdf.drawString(58, 420, _safe(address)[:46])
        pdf.drawString(58, 408, f"Coordenadas: {lat:.6f}, {lng:.6f}")
        pdf.setFillColor(MUTED); pdf.setFont("Helvetica", 6.5); pdf.drawString(58, 397, "Imagen satelital de referencia © Esri World Imagery.")
    else:
        pdf.setFillColor(ROW); pdf.rect(58, 435, 218, 170, stroke=0, fill=1)
        pdf.setFillColor(MUTED); pdf.setFont("Helvetica", 8); pdf.drawCentredString(167, 520, "Selecciona una dirección para incorporar la vista satelital.")

    pdf.setStrokeColor(BLUE); pdf.rect(302, 390, 245, 255, stroke=1, fill=0)
    pdf.setFillColor(BLUE); pdf.setFont("Helvetica-Bold", 9); pdf.drawString(313, 625, "DIAGRAMA UNIFILAR Y EQUIPOS")
    nodes = [
        ("ARREGLO FV", f"{r['strings']} x {r['modulos_serie']} módulos - {r['pdc']} kWp"),
        ("PROTECCIÓN DC", "Fusible, seccionador y DPS DC"),
    ]
    if battery and battery["storage"]:
        nodes.extend([("INVERSOR HÍBRIDO / OFF-GRID", f"Potencia mínima {r['pac']} kWac"),
                      ("BANCO DE BATERÍAS", f"{battery['count']} x {battery['brand']} - {fmt(battery['usable'])} kWh útiles"),
                      ("TABLERO AC Y CARGAS", "Breaker, DPS AC y cargas prioritarias")])
    else:
        nodes.extend([("INVERSOR ON-GRID", f"Potencia mínima {r['pac']} kWac"),
                      ("TABLERO AC Y MEDIDOR", "Breaker, DPS AC, medidor bidireccional y red")])
    ny = 582
    for index, (name, detail) in enumerate(nodes):
        height = 28 if len(nodes) > 4 else 35
        pdf.setFillColor(HexColor("#FBFCFD")); pdf.roundRect(323, ny, 202, height, 4, stroke=1, fill=1)
        pdf.setFillColor(TEXT); pdf.setFont("Helvetica-Bold", 7.3); pdf.drawCentredString(424, ny + 22, _safe(name))
        pdf.setFont("Helvetica", 6.4); pdf.drawCentredString(424, ny + 7, _safe(detail))
        if index < len(nodes) - 1:
            pdf.setFillColor(ORANGE); pdf.setFont("Helvetica-Bold", 13); pdf.drawCentredString(424, ny - 13, "↓")
        ny -= 40 if len(nodes) > 4 else 52
    pdf.setFillColor(MUTED); pdf.setFont("Helvetica", 6.8); pdf.drawString(313, 401, "Protecciones y calibres sujetos a ingeniería final y RETIE.")
    y = _section(pdf, 355, "Notas y condiciones")
    pdf.setFillColor(TEXT); pdf.setFont("Helvetica", 9)
    lines = [
        "La posición geográfica y la vista satelital corresponden al punto seleccionado en la aplicación.",
        "La implantación final debe confirmar área disponible, sombras, estructura y distancias de seguridad.",
        "El diagrama es referencial; el diseño definitivo requiere verificación de protecciones y normativa aplicable.",
    ]
    if battery and battery["storage"]:
        lines.append(f"Banco LiFePO4 referencial: {battery['count']} unidades, {fmt(battery['usable'])} kWh útiles y {fmt(battery['actual_autonomy'])} días de autonomía.")
    for line in lines:
        pdf.drawString(52, y, _safe(line)); y -= 18
    if calculation_rows:
        y = _section(pdf, 270, "Proceso de cálculo")
        _table(pdf, 52, y, 495, ["Fase", "Resultado"], calculation_rows, [40, 455], row_height=17)

@app.route("/evidencia-satelital.png")
def satellite_preview():
    try:
        lat, lng = float(request.args["lat"]), float(request.args["lng"])
        if not (-90 <= lat <= 90 and -180 <= lng <= 180): raise ValueError
    except (KeyError, ValueError):
        return Response("Coordenadas inválidas", status=400)
    image = _satellite_image(lat, lng)
    if not image:
        return Response("Imagen satelital no disponible", status=502)
    return Response(image.getvalue(), mimetype="image/png")

@app.route("/memoria-calculo.pdf")
def calculation_memory_pdf():
    """Memoria técnica en el mismo orden del ejemplo aprobado por el usuario."""
    try:
        d, r, address, lat, lng, client = _project_data(request.args)
    except ValueError as exc:
        return Response(str(exc), status=400)
    battery = battery_design(d, request.args)
    system_name = ("Sistema fotovoltaico off-grid con almacenamiento" if battery["offgrid"]
                   else ("Sistema fotovoltaico híbrido con red y respaldo" if battery["hybrid"] else "Sistema fotovoltaico on-grid"))
    buffer = BytesIO(); pdf = canvas.Canvas(buffer, pagesize=A4); page_h = A4[1]
    _header(pdf, "Memoria de cálculo fotovoltaico", 1)
    pdf.setFillColor(TEXT); pdf.setFont("Helvetica-Bold", 9.5); pdf.drawString(52, page_h - 150, "Proyecto:")
    pdf.setFont("Helvetica", 9.5); pdf.drawString(101, page_h - 150, _safe(system_name))
    pdf.setFont("Helvetica-Bold", 9.5); pdf.drawString(52, page_h - 166, "Cliente:")
    pdf.setFont("Helvetica", 9.5); pdf.drawString(96, page_h - 166, _safe(client)[:80])
    pdf.setFont("Helvetica-Bold", 9.5); pdf.drawString(52, page_h - 182, "Ubicación:")
    pdf.setFont("Helvetica", 9.5); pdf.drawString(106, page_h - 182, _safe(address)[:82])
    if lat is not None and lng is not None:
        pdf.setFont("Helvetica", 8); pdf.drawString(106, page_h - 196, f"Latitud {lat:.6f}  |  Longitud {lng:.6f}")
    y = _section(pdf, page_h - 222, "Alcance")
    pdf.setFillColor(TEXT); pdf.setFont("Helvetica", 9); pdf.drawString(52, y, _safe(f"Resultado de predimensionamiento {battery['mode']}. Validar en visita técnica, RETIE y requisitos del operador de red."))
    y = _section(pdf, y - 34, "Datos del sitio y supuestos")
    y = _table(pdf, 70, y, 455, ["Variable", "Valor"], [
        ("Horas sol pico", f"{r['hsp']} h/día"), ("Performance ratio", r["pr"]),
        ("Cobertura de consumo", f"{r['cobertura']} %"), ("Potencia del módulo", f"{fmt(d['modulo_wp'], 0)} Wp"),
        ("Voc del módulo", f"{fmt(d['voc'])} V"), ("Temperatura mínima", f"{fmt(d['temp_min'], 0)} °C"),
        ("Tensión máxima DC", f"{fmt(d['vdc_max'], 0)} V"),
    ], [225, 230])
    y = _section(pdf, y - 22, "Dimensionamiento preliminar")
    y = _table(pdf, 70, y, 455, ["Resultado", "Valor"], [
        ("Demanda diaria objetivo", f"{fmt(d['consumo'] * d['cobertura'] / 100 / 30)} kWh/día"),
        ("Arreglo FV instalado", f"{r['pdc']} kWp"), ("Módulos instalados", f"{r['modulos']} ({r['strings']} x {r['modulos_serie']})"),
        ("Inversor mínimo", f"{r['pac']} kWac"), ("Producción anual", f"{r['energia_anual']} kWh/año"),
        ("Voc string en frío", f"{r['voc_string']} V"),
    ], [225, 230])
    if battery["storage"]:
        y = _section(pdf, y - 20, "Almacenamiento y respaldo nocturno")
        _table(pdf, 70, y, 455, ["Variable", "Resultado"], [
            ("Marca / referencia", battery["brand"]), ("Autonomía solicitada", f"{fmt(battery['autonomy'])} días"),
            ("Respaldo / cargas críticas", f"{fmt(battery['support_fraction']*100, 0)} % / {fmt(battery['critical_fraction']*100, 0)} %"),
            ("Energía mínima a respaldar", f"{fmt(battery['required_energy'])} kWh"),
            ("Profundidad de descarga / eficiencia", f"{fmt(battery['dod']*100, 0)} % / {fmt(battery['efficiency']*100, 0)} %"),
            ("Banco seleccionado", f"{battery['count']} unidades ({battery['series']}S x {battery['parallel']}P)"),
            ("Capacidad nominal / útil", f"{fmt(battery['nominal'])} / {fmt(battery['usable'])} kWh"),
        ], [225, 230], row_height=18)
    pdf.showPage(); _draw_location_and_diagram(pdf, r, address, lat, lng, "Memoria de cálculo fotovoltaico", _calculation_rows(d, r), battery)
    pdf.save(); buffer.seek(0)
    disposition = "inline" if request.args.get("preview") == "1" else "attachment"
    filename = f"memoria-calculo-{_filename_client(request.args)}.pdf"
    return Response(buffer.getvalue(), mimetype="application/pdf", headers={"Content-Disposition": f"{disposition}; filename={filename}"})

@app.route("/cotizacion.pdf")
def quotation_pdf():
    """Cotización formal en documento separado de la memoria de cálculo."""
    try:
        d, r, address, lat, lng, client = _project_data(request.args)
    except ValueError as exc:
        return Response(str(exc), status=400)
    battery = battery_design(d, request.args)
    try:
        tariff = float(request.args.get("tarifa", 0) or 0); cost_kwp = float(request.args.get("costoKwp", 4200000) or 4200000)
        battery_cost_kwh = float(request.args.get("costoBateriaKwh", 1800000) or 1800000)
    except ValueError:
        tariff, cost_kwp, battery_cost_kwh = 0, 4200000, 1800000
    operator = request.args.get("operador", "Operador por confirmar").strip() or "Operador por confirmar"
    tariff_period = request.args.get("periodoTarifa", "Periodo por confirmar").strip() or "Periodo por confirmar"
    try: self_consumption = max(0, min(100, float(request.args.get("autoconsumo", 90) or 90))) / 100
    except ValueError: self_consumption = .90
    installed = float(r["pdc"].replace(".", "").replace(",", ".")); pv_investment = installed * cost_kwp
    battery_cost = battery["nominal"] * battery_cost_kwh if battery["storage"] else 0
    investment = pv_investment + battery_cost
    module_cost = pv_investment * .34; inverter_cost = pv_investment * .18; structure_cost = pv_investment * .14; protections_cost = pv_investment * .11; installation_cost = pv_investment - module_cost - inverter_cost - structure_cost - protections_cost
    annual_baseline = d["consumo"] * 12
    annual_pv_usable = min(float(r["energia_anual"].replace(".", "").replace(",", ".")), annual_baseline) * self_consumption
    current_monthly_bill = d["consumo"] * tariff
    current_annual_bill = annual_baseline * tariff
    annual_savings = annual_pv_usable * tariff; payback = investment / annual_savings if annual_savings else 0
    buffer = BytesIO(); pdf = canvas.Canvas(buffer, pagesize=A4); page_h = A4[1]
    _header(pdf, "COTIZACIÓN FORMAL - SISTEMA FOTOVOLTAICO", 1)
    system_name = ("Sistema fotovoltaico off-grid con baterías" if battery["offgrid"]
                   else ("Sistema fotovoltaico híbrido con red y baterías" if battery["hybrid"] else "Sistema fotovoltaico on-grid"))
    meta = [("Proveedor:", "TWE Welt Energy"), ("Cliente:", client), ("Proyecto:", system_name), ("Predio:", address), ("Vigencia:", "30 días calendario")]
    y = page_h - 150
    for label, value in meta:
        pdf.setFillColor(TEXT); pdf.setFont("Helvetica-Bold", 9.5); pdf.drawString(52, y, _safe(label)); pdf.setFont("Helvetica", 9.5); pdf.drawString(104, y, _safe(value)[:78]); y -= 15
    y = _section(pdf, y - 18, "Alcance de suministro")
    pdf.setFillColor(TEXT); pdf.setFont("Helvetica", 9); pdf.drawString(52, y, "Suministro e instalación referencial del sistema fotovoltaico dimensionado. Valores sujetos a visita técnica.")
    y = _section(pdf, y - 34, "Equipos, materiales y servicios")
    rows = [
        (f"Módulo FV {fmt(d['modulo_wp'], 0)} Wp", str(r["modulos"]), "und", f"$ {module_cost/r['modulos']:,.0f}", f"$ {module_cost:,.0f}"),
        (("Inversor híbrido / off-grid" if battery["storage"] else "Inversor on-grid") + " y monitoreo", "1", "und", f"$ {inverter_cost:,.0f}", f"$ {inverter_cost:,.0f}"),
        ("Estructura de montaje, fijaciones y rieles", "1", "kit", f"$ {structure_cost:,.0f}", f"$ {structure_cost:,.0f}"),
        ("Protecciones DC/AC, cableado y puesta a tierra", "1", "kit", f"$ {protections_cost:,.0f}", f"$ {protections_cost:,.0f}"),
        ("Instalación, pruebas y puesta en marcha", "1", "serv", f"$ {installation_cost:,.0f}", f"$ {installation_cost:,.0f}"),
    ]
    if battery["storage"]:
        rows.insert(2, (f"Batería {battery['brand']} {fmt(battery['voltage'], 0)} V {fmt(battery['ah'], 0)} Ah", str(battery["count"]), "und", f"$ {battery_cost/battery['count']:,.0f}", f"$ {battery_cost:,.0f}"))
    y = _table(pdf, 48, y, 500, ["Descripción", "Cant.", "Und.", "Vr. unitario", "Vr. total"], rows, [220, 40, 42, 98, 100], row_height=24)
    y = _section(pdf, y - 28, "Resumen económico y soporte tarifario")
    economics_rows = [("TOTAL COTIZADO", f"$ {investment:,.0f} COP"), ("Operador de red", operator), ("Periodo de tarifa", tariff_period),
                      ("Tarifa aplicada", f"$ {tariff:,.0f} COP/kWh"), ("Consumo actual mensual", f"$ {current_monthly_bill:,.0f} COP/mes"),
                      ("Gasto anual actual estimado", f"$ {current_annual_bill:,.0f} COP/año"),
                      ("Ahorro anual FV estimado", f"$ {annual_savings:,.0f} COP/año"), ("Retorno simple estimado", f"{payback:.1f} años" if annual_savings else "Requiere tarifa validada")]
    y = _table(pdf, 48, y, 500, ["Concepto", "Valor"], economics_rows, [260, 240], row_height=20)
    pdf.setFillColor(MUTED); pdf.setFont("Helvetica", 7.2); pdf.drawString(52, y - 16, f"Ahorro calculado sobre {self_consumption*100:.0f}% de autoconsumo. La energía excedente no se valora como ahorro hasta confirmar su compensación.")
    pdf.drawString(52, y - 28, "Tarifa a validar contra la factura vigente del cliente y el tarifario oficial del operador de red.")
    pdf.showPage(); _draw_location_and_diagram(pdf, r, address, lat, lng, "Plano de implantación y diagrama unifilar", battery=battery)
    pdf.save(); buffer.seek(0)
    disposition = "inline" if request.args.get("preview") == "1" else "attachment"
    filename = f"cotizacion-formal-{_filename_client(request.args)}.pdf"
    return Response(buffer.getvalue(), mimetype="application/pdf", headers={"Content-Disposition": f"{disposition}; filename={filename}"})

if __name__ == "__main__":
    # Desarrollo local y compatibilidad con plataformas de despliegue.
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
