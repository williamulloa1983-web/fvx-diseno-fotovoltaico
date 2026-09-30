import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) throw new Error("Uso: node build_excel.mjs entrada.json salida.xlsx");
const data = JSON.parse(await fs.readFile(inputPath, "utf8"));
const wb = Workbook.create();
const summary = wb.worksheets.add("Resumen");
const calc = wb.worksheets.add("Calculo");
const batteries = wb.worksheets.add("Baterias");
const orange = "#F57C17", navy = "#134D80", pale = "#EAF1F5", line = "#B7C8D6";
const baseFont = { name: "Arial", size: 10, color: "#17191D" };

function heading(sheet, range, text) {
  sheet.mergeCells(range);
  const cell = sheet.getRange(range);
  cell.values = [[text]];
  cell.format = { fill: orange, font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" }, verticalAlignment: "center" };
  cell.format.rowHeight = 22;
}
function tableStyle(sheet, range) {
  const r = sheet.getRange(range);
  r.format.borders = { preset: "all", style: "thin", color: line };
  r.format.verticalAlignment = "center";
}
function setWidths(sheet) {
  ["A:A", "B:B", "C:C", "D:D", "E:E", "F:F"].forEach((column, index) => {
    sheet.getRange(column).format.columnWidth = [28, 28, 18, 34, 14, 14][index];
  });
}

summary.showGridLines = false;
summary.tabColor = navy;
setWidths(summary);
summary.mergeCells("A2:F2"); summary.getRange("A2").values = [[`TWE Welt Energy | Hoja de cálculo fotovoltaica ${data.mode || "on-grid"}`]];
summary.getRange("A2:F2").format = { font: { name: "Arial", size: 16, bold: true, color: navy }, verticalAlignment: "center" };
summary.getRange("A2:F2").format.rowHeight = 28;
summary.getRange("A3:F3").format.borders = { bottom: { style: "medium", color: orange } };
summary.getRange("A5:D8").values = [
  ["Cliente", data.client || "Por confirmar", "Proyecto", `Sistema fotovoltaico ${data.mode || "on-grid"}`],
  ["Ubicación", data.address || "Pendiente de seleccionar", "Latitud", data.lat ?? ""],
  ["Longitud", data.lng ?? "", "Recurso solar", data.nasa || "HSP ingresada manualmente"],
  ["Operador de red", data.operator || "Por confirmar", "Tarifa", data.tariff || 0],
];
summary.getRange("A5:D8").format = { font: baseFont, verticalAlignment: "center" };
summary.getRange("A5:A8").format.font = { name: "Arial", size: 10, bold: true, color: navy };
summary.getRange("C5:C8").format.font = { name: "Arial", size: 10, bold: true, color: navy };
summary.getRange("A5:D8").format.borders = { preset: "all", style: "thin", color: line };
summary.getRange("B5:B8").format.fill = "#FFF7ED";
summary.getRange("D5:D8").format.fill = "#FFF7ED";
heading(summary, "A9:D9", "Resultados del diseño");
summary.getRange("A10:C15").values = [
  ["Indicador", "Valor", "Unidad"],
  ["Potencia FV instalada", data.pdc, "kWp"],
  ["Módulos fotovoltaicos", data.modules, "unidades"],
  ["Configuración", data.strings, "strings x módulos"],
  ["Inversor AC mínimo", data.pac, "kWac"],
  ["Producción anual estimada", data.annual, "kWh/año"],
];
summary.getRange("A10:C10").format = { fill: navy, font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" }, verticalAlignment: "center" };
summary.getRange("A10:C15").format.font = baseFont; tableStyle(summary, "A10:C15");
summary.getRange("B11:B11").format.numberFormat = "#,##0.00";
summary.getRange("B12:B12").format.numberFormat = "#,##0";
summary.getRange("B14:B14").format.numberFormat = "#,##0.00";
summary.getRange("B15:B15").format.numberFormat = "#,##0";
summary.mergeCells("A17:F17"); summary.getRange("A17").values = [["Documento generado desde FVx. Edita los supuestos en la hoja Calculo para revisar el predimensionamiento."]];
summary.getRange("A17:F17").format = { font: { name: "Arial", size: 9, italic: true, color: "#5E646B" } };
summary.freezePanes.freezeRows(3);

calc.showGridLines = false;
calc.tabColor = orange;
setWidths(calc);
calc.mergeCells("A2:D2"); calc.getRange("A2").values = [["Cálculo técnico fotovoltaico on-grid"]];
calc.getRange("A2:D2").format = { font: { name: "Arial", size: 15, bold: true, color: navy }, verticalAlignment: "center" };
calc.getRange("A3:D3").format.borders = { bottom: { style: "medium", color: orange } };
heading(calc, "A5:C5", "Entradas editables");
calc.getRange("A6:C16").values = [
  ["Variable", "Valor", "Unidad"],
  ["Consumo mensual", data.consumo, "kWh/mes"],
  ["Horas sol pico", data.hsp, "h/día"],
  ["Cobertura", data.coverage, "%"],
  ["Performance ratio", data.pr, "0-1"],
  ["Potencia del módulo", data.moduleWp, "Wp"],
  ["Voc del módulo", data.voc, "V"],
  ["Coeficiente Voc", data.coefVoc, "%/°C"],
  ["Temperatura mínima", data.tempMin, "°C"],
  ["Tensión máxima DC", data.vdcMax, "V"],
  ["Relación DC/AC", data.dcac, "0-1"],
];
calc.getRange("A6:C6").format = { fill: navy, font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" } };
calc.getRange("A6:C16").format.font = baseFont; tableStyle(calc, "A6:C16");
calc.getRange("B7:B16").format.fill = "#FFF2CC";
calc.getRange("B7:B8").format.numberFormat = "#,##0.00";
calc.getRange("B9:B9").format.numberFormat = "0.0%";
calc.getRange("B10:B10").format.numberFormat = "0.00";
calc.getRange("B11:B16").format.numberFormat = "#,##0.00";
heading(calc, "A17:C17", "Resultados calculados");
calc.getRange("A18:C29").values = [
  ["Resultado", "Valor", "Unidad"],
  ["Energía objetivo mensual", null, "kWh/mes"], ["Demanda diaria", null, "kWh/día"],
  ["Potencia FV requerida", null, "kWp"], ["Módulos requeridos", null, "unidades"],
  ["Voc en frío", null, "V/módulo"], ["Máximo módulos por string", null, "módulos"],
  ["Módulos por string", null, "módulos"], ["Número de strings", null, "strings"],
  ["Módulos instalados", null, "unidades"], ["Potencia FV instalada", null, "kWp"],
  ["Inversor AC mínimo", null, "kWac"], ["Producción anual estimada", null, "kWh/año"],
];
calc.getRange("A18:C18").format = { fill: navy, font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" } };
calc.getRange("A18:C29").format.font = baseFont; tableStyle(calc, "A18:C29");
calc.getRange("B19:B29").formulas = [
  ["=B7*B9"], ["=B19/30"], ["=B20/(B8*B10)"], ["=ROUNDUP(B21*1000/B11,0)"],
  ["=B12*(1+(B13/100)*(25-B14))"], ["=ROUNDDOWN(B15/B23,0)"], ["=MIN(B24,B22)"],
  ["=ROUNDUP(B22/B25,0)"], ["=B26*B25"], ["=B27*B11/1000"], ["=B28/B16"], ["=B28*B8*365*B10"],
];
calc.getRange("B19:B29").format.fill = pale;
calc.getRange("B19:B21").format.numberFormat = "#,##0.00";
calc.getRange("B22:B27").format.numberFormat = "#,##0";
calc.getRange("B28:B29").format.numberFormat = "#,##0.00";
calc.getRange("A31:D31").values = [["Cliente", data.client || "Por confirmar", "Ubicación", data.address || "Pendiente de seleccionar"]];
calc.getRange("A31:D31").format = { font: { name: "Arial", size: 9, italic: true, color: "#5E646B" } };
calc.freezePanes.freezeRows(6);

batteries.showGridLines = false;
batteries.tabColor = "#16805E";
setWidths(batteries);
batteries.mergeCells("A2:D2"); batteries.getRange("A2").values = [["Banco de baterías y respaldo energético"]];
batteries.getRange("A2:D2").format = { font: { name: "Arial", size: 15, bold: true, color: navy }, verticalAlignment: "center" };
batteries.getRange("A3:D3").format.borders = { bottom: { style: "medium", color: orange } };
heading(batteries, "A5:C5", "Configuración de almacenamiento");
batteries.getRange("A6:C17").values = [
  ["Variable", "Valor", "Unidad"], ["Tipo de sistema", data.mode || "on-grid", ""],
  ["Marca / referencia", data.batteryBrand || "No aplica", ""], ["Autonomía solicitada", data.autonomy || 0, "días"],
  ["Tensión de batería", data.batteryV || 0, "V"], ["Capacidad de batería", data.batteryAh || 0, "Ah"],
  ["Profundidad de descarga", data.dod || 0, "%"], ["Eficiencia del banco", data.batteryEfficiency || 0, "%"],
  ["Capacidad requerida", data.batteryRequired || 0, "kWh"], ["Baterías seleccionadas", data.batteryCount || 0, "unidades"],
  ["Configuración", `${data.batterySeries || 0}S x ${data.batteryParallel || 0}P`, ""], ["Capacidad útil estimada", data.batteryUsable || 0, "kWh"],
];
batteries.getRange("A6:C6").format = { fill: navy, font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" } };
batteries.getRange("A6:C17").format.font = baseFont; tableStyle(batteries, "A6:C17");
batteries.getRange("B7:B17").format.fill = "#F1FBF7";
batteries.getRange("B9:B16").format.numberFormat = "#,##0.00";
batteries.getRange("A19:D20").values = [["Autonomía real calculada", data.batteryAutonomy || 0, "días", ""], ["Nota", "Valores referenciales. Confirmar BMS, compatibilidad del inversor, protecciones y ficha técnica.", "", ""]];
batteries.getRange("A19:D20").format = { font: baseFont, wrapText: true, verticalAlignment: "center" }; tableStyle(batteries, "A19:D20");
batteries.getRange("A19:A20").format.font = { name: "Arial", size: 10, bold: true, color: navy };
batteries.getRange("B19").format.numberFormat = "#,##0.00";
batteries.freezePanes.freezeRows(5);

wb.recalculate();
await fs.mkdir(new URL(".", `file://${outputPath.replace(/\\/g, "/")}`).pathname, { recursive: true }).catch(() => {});
const xlsx = await SpreadsheetFile.exportXlsx(wb);
await xlsx.save(outputPath);
