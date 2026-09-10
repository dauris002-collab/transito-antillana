"""
test_baseline.py — Golden master de comportamiento para la revisión Antillana.

Ejecuta las funciones puras de logica.py y sheets_io.py sobre datos sintéticos
y vuelca los resultados a JSON. La Etapa 3 (optimización) debe producir un JSON
IDÉNTICO con el código optimizado: cualquier diferencia es un cambio de
comportamiento introducido por la optimización.

Uso:  python3 test_baseline.py <salida.json>
"""

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import sheets_io as sio
import logica as lg


def ser(v):
    """Serializador JSON para date/datetime/sets."""
    if isinstance(v, (date,)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): ser(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [ser(x) for x in v]
    if isinstance(v, float) and v != v:  # NaN
        return None
    return v


resultados = {}

# ---------------------------------------------------------------- parseo de fechas
CASOS_FECHA = [
    "2026-08-25", "25/08/2026", "25-08-26", "2026/08/25", "20260825",
    "6 de julio 2026", "julio 28 del 2026", "1ro de mayo/2026", "28-Jul-26",
    "02-05-2026",           # ambiguo: debe leerse día/mes
    "46181",                # serial Sheets/Excel
    "2026-08-25 00:00:00", "September 3 2026", "setiembre 3 2026",
    "", None, "no es fecha", "31/02/2026", "2026-13-01", 46181, 46181.0,
    "12/25/2026",           # mes primero inequívoco (25 > 12)
    "enero 5, 2027", "5 ene 27",
]
resultados["parsear_fecha"] = [ser(sio.parsear_fecha(c)) for c in CASOS_FECHA]
resultados["formato_eta"] = [sio.formato_eta(c) for c in CASOS_FECHA[:12]]

# ---------------------------------------------------------------- _norm / países / números
resultados["norm"] = [sio._norm(t) for t in [" ¿Llegó?  SI/NO ", "CLIENTE / STOCK", "Cliente_Stock", "TILDE áéíóú Ñ"]]
resultados["norm_encabezado"] = [sio._norm_encabezado(t) for t in ["CLIENTE / STOCK", "Cliente/Stock", "Cliente_Stock", "cliente stock"]]
serie_paises = pd.Series(["china", "CHINA ", "USA", "ee.uu", "Italia", "italia", "2026-08-04 19:58:00", "", "N/A", "Corea", "corea del sur"])
resultados["unificar_paises"] = list(sio.unificar_paises(serie_paises))
resultados["a_numero"] = [sio.a_numero(v) for v in ["US$ 145,300.50", "145300.50", "1.234.567,89", "RD$ 5,000", "", None, "abc", "0", "-50.25", "12,5"]]
resultados["es_llego"] = [(sio.es_llego_si(v), sio.es_llego_no(v)) for v in ["SI", "si", "Sí", "SÍ", " s ", "yes", "NO", "no", "N", "", None, "quizá"]]

# ---------------------------------------------------------------- estado_embarque
HOY = date(2026, 9, 10)
casos_estado = [
    ("2026-09-15", ""), ("2026-09-12", ""), ("2026-09-10", "SI"), ("2026-09-08", ""),
    ("2026-09-08", "SI"), ("2026-09-08", "NO"), ("", ""), ("texto sucio", ""),
    ("2026-10-01", "NO"), ("2026-09-11", ""),
]
resultados["estado_embarque"] = [lg.estado_embarque(eta, llego, HOY) for eta, llego in casos_estado]
resultados["texto_estado"] = [lg.texto_estado(e, d, via) for (e, d), (_, _), via in
                              zip([lg.estado_embarque(eta, l, HOY) for eta, l in casos_estado],
                                  casos_estado, ["", "Aéreo", "", "", "Aéreo", "", "", "", "", ""])]

# ---------------------------------------------------------------- enriquecer()
activos = pd.DataFrame([
    # BL, Desc, Cant, Pais, ETA, Llego, Declaracion, Via, Categoria, FilaSheet
    {sio.COL_BL: "BL001", sio.COL_DESC: "Montacargas", sio.COL_CANT: "2", sio.COL_PAIS: "China",
     sio.COL_ETA: "2026-09-05", sio.COL_LLEGO: "SI", sio.COL_FECHA_DECLARACION: "2026-09-08",
     sio.COL_VIA: "", "Categoria": "Montacargas", "FilaSheet": 2},
    {sio.COL_BL: "BL002", sio.COL_DESC: "Excavadora", sio.COL_CANT: "1", sio.COL_PAIS: "Corea",
     sio.COL_ETA: "2026-09-12", sio.COL_LLEGO: "", sio.COL_FECHA_DECLARACION: "",
     sio.COL_VIA: "", "Categoria": "Construcción y Minería", "FilaSheet": 3},
    {sio.COL_BL: "BL003", sio.COL_DESC: "Generador", sio.COL_CANT: "1", sio.COL_PAIS: "USA",
     sio.COL_ETA: "2026-08-20", sio.COL_LLEGO: "NO", sio.COL_FECHA_DECLARACION: "",
     sio.COL_VIA: "", "Categoria": "Generadores", "FilaSheet": 4},
    {sio.COL_BL: "BL001", sio.COL_DESC: "Montacargas parcial", sio.COL_CANT: "1", sio.COL_PAIS: "China",
     sio.COL_ETA: "2026-09-25", sio.COL_LLEGO: "", sio.COL_FECHA_DECLARACION: "",
     sio.COL_VIA: "Aéreo", "Categoria": "Montacargas", "FilaSheet": 5},
    {sio.COL_BL: "BL004", sio.COL_DESC: "Repuestos", sio.COL_CANT: "3 cajas", sio.COL_PAIS: "Italia",
     sio.COL_ETA: "2026-09-06", sio.COL_LLEGO: "SI", sio.COL_FECHA_DECLARACION: "",
     sio.COL_VIA: "Aéreo", "Categoria": "Aéreos", "FilaSheet": 6},
    {sio.COL_BL: "BL005", sio.COL_DESC: "Sin fecha", sio.COL_CANT: "1", sio.COL_PAIS: "",
     sio.COL_ETA: "ilegible", sio.COL_LLEGO: "", sio.COL_FECHA_DECLARACION: "",
     sio.COL_VIA: "", "Categoria": "General", "FilaSheet": 7},
])
enr = lg.enriquecer(activos)
cols_volcado = [sio.COL_BL, "EstadoTexto", "DiasRel", "MesETA", "Prioridad", "EtapaActual",
                "DiasTransito", "DiasEnPuerto", "DiasEnEtapa", "Alerta", "AlertaDias",
                "BLRepetido", "FlujoRaro", "OrdenSec"]
resultados["enriquecer"] = enr[cols_volcado].astype(object).where(pd.notna(enr[cols_volcado]), None).to_dict("records")
resultados["resumen_atraso_puerto"] = {k: v for k, v in lg.resumen_atraso_puerto(enr).items() if k != "detalle"}
resultados["resumen_atraso_puerto_detalle"] = lg.resumen_atraso_puerto(enr)["detalle"]
resultados["contar_mes_eta"] = lg.contar_activos_por_mes_eta(activos)

# ---------------------------------------------------------------- pagos
pagos = pd.DataFrame([
    {sio.COL_EMPRESA: "Antillana Comercial", sio.COL_BL: "BL001", sio.COL_DESC: "Montacargas",
     sio.COL_CANT: "2", sio.COL_PAGO_LLEGADA: "2026-09-05",
     "ADUANAS": "150000", "DPH": "1200", "FLETE": "3000",
     sio.COL_ESTADO_PAGO: "", sio.COL_FECHA_SIN_MORA: "2026-09-01",
     sio.COL_FECHA_PAGO_REAL: "2026-09-06", sio.COL_PAGOREAL_USD: "150", sio.COL_PAGOREAL_DOP: "0"},
    {sio.COL_EMPRESA: "Tecnicaribe", sio.COL_BL: "TC-100", sio.COL_DESC: "Repuestos",
     sio.COL_CANT: "1", sio.COL_PAGO_LLEGADA: "2026-08-15",
     "ADUANAS": "80000", "TRANSPORTE": "25000",
     sio.COL_ESTADO_PAGO: "Pendiente", sio.COL_FECHA_SIN_MORA: "2026-08-20",
     sio.COL_FECHA_PAGO_REAL: "", sio.COL_PAGOREAL_USD: "", sio.COL_PAGOREAL_DOP: ""},
    {sio.COL_EMPRESA: "", sio.COL_BL: "BL003", sio.COL_DESC: "Generador",
     sio.COL_CANT: "1", sio.COL_PAGO_LLEGADA: "",
     "GESTION ADUANAL": "45000",
     sio.COL_ESTADO_PAGO: "Pagado", sio.COL_FECHA_SIN_MORA: "",
     sio.COL_FECHA_PAGO_REAL: "", sio.COL_PAGOREAL_USD: "", sio.COL_PAGOREAL_DOP: ""},
    {sio.COL_EMPRESA: "TECNICARIBE", sio.COL_BL: "TC-101", sio.COL_DESC: "Otro",
     sio.COL_CANT: "1", sio.COL_PAGO_LLEGADA: "2026-09-01",
     "CARGOS LOCALES": "500",
     sio.COL_ESTADO_PAGO: "", sio.COL_FECHA_SIN_MORA: "",
     sio.COL_FECHA_PAGO_REAL: "2026-09-03", sio.COL_PAGOREAL_USD: "-20", sio.COL_PAGOREAL_DOP: ""},
])
historico = pd.DataFrame([
    {sio.COL_BL: "BL999", sio.COL_FECHA_LLEGADA_PUERTO: "2026-07-01"},
])
enr_p = lg.enriquecer_pagos(pagos, activos, historico)
cols_p = [sio.COL_BL, "EmpresaEfectiva", "EstadoEfectivo", "BLSinTransito", "TieneMontos",
          "TotalActual", "DiasSinPagar", "MontoExtra", "TotalPagado", "DiasMora"]
resultados["enriquecer_pagos"] = enr_p[cols_p].astype(object).where(pd.notna(enr_p[cols_p]), None).to_dict("records")
resultados["resumen_pagos"] = lg.resumen_pagos(enr_p)
# resumen sobre subconjunto filtrado (como hace la app con el filtro Empresa)
resultados["resumen_pagos_tecnicaribe"] = lg.resumen_pagos(enr_p[enr_p["EmpresaEfectiva"] == "Tecnicaribe"])
resultados["totales_conceptos"] = [lg.totales_conceptos(r) for _, r in pagos.iterrows()]

# ---------------------------------------------------------------- varios
resultados["analizar_eta"] = [lg.analizar_eta(v) for v in ["2026-08-25", "02-05-2026", "6 de julio 2026", "", "xyz", "12/25/2026"]]
resultados["es_aereo"] = [lg.es_aereo(v) for v in ["Aéreo", "aéreo", "AEREO", "", None, "Marítimo"]]
resultados["etiqueta_etapa"] = [lg.etiqueta_etapa(e, via) for e in sio.ETAPAS_PUERTO for via in ("", "Aéreo")]
resultados["ordenar_vista"] = {
    c: list(lg.ordenar_vista(enr, c)[sio.COL_BL])
    for c in ["Urgencia", "Más días detenido", "ETA más próximo", "ETA más lejano", "BL", "País", "Descripción"]
}

salida = json.dumps(resultados, default=ser, ensure_ascii=False, indent=1, sort_keys=True)
Path(sys.argv[1] if len(sys.argv) > 1 else "golden.json").write_text(salida, encoding="utf-8")
print(f"OK — {len(resultados)} grupos de resultados volcados")
