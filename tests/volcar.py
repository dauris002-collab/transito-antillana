"""Vuelca a JSON lo que devuelve el código de <repo> para un conjunto FIJO de entradas.
Se ejecuta dos veces (versión base y versión nueva) y test_diferencial.py compara.

    python tests/volcar.py <carpeta_del_repo> <salida.json>
"""
import json
import random
import re
import sys
import time
import types
from datetime import date, timedelta
from pathlib import Path

repo, salida = Path(sys.argv[1]).resolve(), sys.argv[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))   # fake_gspread, datos (siempre los de ESTA carpeta)
sys.path.insert(0, str(repo))                              # sheets_io, logica... de la versión a medir

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
import sheets_io as sio  # noqa: E402
import logica  # noqa: E402
import datos  # noqa: E402

sio.time = types.SimpleNamespace(sleep=lambda *_: None, time=time.time)
rnd = random.Random(20261116)
HOY = sio.hoy_rd()


def d(n):
    return HOY + timedelta(days=n)


def plano(v):
    if isinstance(v, pd.DataFrame):
        return [{k: plano(x) for k, x in fila.items()} for fila in v.to_dict("records")]
    if isinstance(v, dict):
        return {str(k): plano(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [plano(x) for x in v]
    if isinstance(v, float) and v != v:
        return None
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return str(v)


def seguro(fn, *a, **k):
    try:
        return plano(fn(*a, **k))
    except Exception as e:  # noqa: BLE001 - una excepción también es un resultado a comparar
        return f"EXCEPCIÓN {type(e).__name__}: {e}"


res = {}

# ---- 1. Funciones puras -------------------------------------------------------
meses = list(sio.MESES_ES.values()) + list(sio.MESES_ES_CORTO.values())
textos_fecha = ["", " ", "nan", "ayer", "31/02/2026", "2026-13-40", "46350", "46350.0", "20261116", "1ro de mayo/2026",
                "julio 28 del 2026", "28-Jul-26", "06/08/2026", "8/6/26", "2026/08/25", "25.08.2026", "2026-08-25 00:00:00",
                "miércoles 14 de octubre de 2026", "14 10 2026", "5", "0", "-3", "1e5", "12/13/2026", "13/12/2026"]
for _ in range(600):
    f = date(2000, 1, 1) + timedelta(days=rnd.randrange(29000))
    textos_fecha += [f.isoformat(), f.strftime("%d/%m/%Y"), f.strftime("%m/%d/%y"), f"{f.day} de {rnd.choice(meses)} {f.year}",
                     f"{f.day}-{rnd.choice(meses)[:3]}-{f.year % 100:02d}", str((f - sio.EPOCH_SHEETS).days)]
res["parsear_fecha"] = {t: seguro(sio.parsear_fecha, t) for t in textos_fecha}
res["analizar_eta"] = {t: seguro(logica.analizar_eta, t) for t in textos_fecha[:200]}

textos_num = ["", "-", "abc", "1.5", "1.234", "1,5", "1,50", "1,500", "1.500", "RD$ 1.500", "US$ 145,300.50", "(500)", "-500",
              "1 200,5", "1.200.000", "12.345.678", "1.200.000,50", "1,200,000.50", "1e3", "5%", "0", "0.0", "00012", "1..2", "1,2,3"]
for _ in range(500):
    x = rnd.randrange(10**10) / 100
    textos_num += [f"{x:,.2f}", f"{x:.2f}", f"{x:,.0f}", f"RD$ {x:,.2f}", f"{x:,.2f}".replace(",", " ")]
res["a_numero"] = {t: seguro(sio.a_numero, t) for t in textos_num}

res["es_aereo"] = {v: seguro(logica.es_aereo, v) for v in ["", "Aéreo", "Aereo", "AÉREO", " aéreo ", "Aérea", "Marítimo", "maritimo", "x", "None"]}
res["es_prioridad_valida"] = {repr(v): seguro(sio.es_prioridad_valida, v)
                              for v in [None, True, 0, 1, 4, 5, 2**53, 1.0, 1.5, "3", "x", -1, float("nan"), float("inf")]}
res["estado_embarque"] = {f"{eta}|{ll}": seguro(logica.estado_embarque, eta, ll, HOY)
                          for eta in [d(-30).isoformat(), d(-1).isoformat(), d(0).isoformat(), d(2).isoformat(), d(3).isoformat(),
                                      d(4).isoformat(), d(40).isoformat(), "", "malo"]
                          for ll in ["", "SI", "sí", "NO", "x"]}
res["validar_eta_confirmado"] = {f"{a}|{b}": seguro(sio.validar_eta_confirmado, a, b)
                                 for a in [d(-5).isoformat(), d(0).isoformat(), d(1).isoformat(), "", "malo"]
                                 for b in [None, d(-6).isoformat(), d(-5).isoformat(), d(0).isoformat()]}
res["orden_flujo"] = {f"{a}|{b}|{c}": seguro(sio._validar_orden_flujo, a and d(a), b and d(b), c and d(c))
                      for a in (None, -5, -1) for b in (None, -6, -3) for c in (None, -4, -2)}
res["conceptos_cotejados"] = {t: seguro(logica.conceptos_cotejados, {sio.COL_CONCEPTOS_PAGADOS: t})
                              for t in ["", "ADUANAS", "aduanas, flete", "Gestión aduanal; DPH", "ADUANA", "FLETE/HIT|FDA\nDPW", " , ,"]}
res["canonizar_empresa"] = {t: seguro(logica.canonizar_empresa, t) for t in ["", "nan", "antillana", "ANTILLANA COMERCIAL", "Tecnicaribe", "motor iberico", "otra"]}
res["unificar_paises"] = seguro(lambda: list(sio.unificar_paises(pd.Series(["USA", "usa", "China", "CHINA ", "", "n/a", "2026-08-04", "Peru", "Perú", "perú"]))))

# ---- 2. Enriquecimiento sobre el libro de prueba -----------------------------
libro = datos.libro_principal()
sio.get_spreadsheet = lambda: libro
st.cache_data.clear()
st.cache_resource.clear()
todo = sio.cargar_todo()
res["cargar_todo.error"] = todo["error"]
quitar = ["Buscar"]
res["enriquecer"] = seguro(lambda: logica.enriquecer(todo["activos"]).drop(columns=quitar, errors="ignore"))
enr_p = logica.enriquecer_pagos(todo["pagos"], todo["activos"], todo["historico"])
res["enriquecer_pagos"] = seguro(lambda: enr_p)
res["resumen_pagos"] = seguro(logica.resumen_pagos, enr_p[enr_p["TieneMontos"]])
res["llegadas_desfasadas"] = seguro(logica.llegadas_desfasadas, todo["pagos"], todo["activos"], todo["historico"])
res["pagos_sin_empresa"] = seguro(logica.pagos_sin_empresa, todo["pagos"], todo["activos"], todo["historico"])
res["resumen_atraso_puerto"] = seguro(lambda: logica.resumen_atraso_puerto(logica.enriquecer(todo["activos"])))
res["contar_por_mes"] = seguro(logica.contar_activos_por_mes_eta, todo["activos"])

# ---- 3. Escrituras: mismo guion, se compara cómo queda el Sheet --------------
SELLO = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def foto(libro_):
    """Lo que se VE en cada pestaña tras el guion (los sellos de hora se enmascaran)."""
    return {h.title: [["<sello>" if isinstance(c, str) and SELLO.match(c) else plano(c) for c in fila] for fila in h.matriz()]
            for h in libro_.hojas if h.title != "Log"}


S = "2026-11-01 08:00:00"
GUIONES = {
    "confirmar_llegada": lambda: sio.confirmar_llegada("MON-VENCIDO", "Montacargas", fila_sugerida=4, sello_esperado=S),
    "confirmar_llegada_eta_futuro": lambda: sio.confirmar_llegada("MON-TRANSITO", "Montacargas", fila_sugerida=2, sello_esperado=S),
    "no_llego": lambda: sio.marcar_no_llego("MON-VENCIDO", "Montacargas", fila_sugerida=4, sello_esperado=S),
    "no_llego_pantalla_vieja": lambda: sio.marcar_no_llego("MON-PUERTO", "Montacargas", fila_sugerida=6, sello_esperado="otro"),
    "declarar_hoy": lambda: sio.fijar_fecha_declaracion("MON-PUERTO", "Montacargas", fecha=HOY, fila_sugerida=6, sello_esperado=S),
    "declarar_antes_de_llegar": lambda: sio.fijar_fecha_declaracion("MON-PUERTO", "Montacargas", fecha=d(-9), fila_sugerida=6),
    "declarar_futuro": lambda: sio.fijar_fecha_declaracion("MON-PUERTO", "Montacargas", fecha=d(2), fila_sugerida=6),
    "corregir_declaracion": lambda: sio.fijar_fecha_declaracion("MON-DECL", "Montacargas", fecha=d(-6), fila_sugerida=7, sobrescribir=True),
    "quitar_declaracion": lambda: sio.avanzar_estado_puerto("MON-DECL", "Montacargas", sio.ETAPA_LLEGADA, fila_sugerida=7, sello_esperado=S),
    "editar_cantidad_y_eta": lambda: sio.actualizar_embarque("MON-TRANSITO", "Montacargas", {sio.COL_CANT: "9", sio.COL_ETA: d(25).isoformat(), sio.COL_MODELO: "ZZ"}, fila_sugerida=2, sello_esperado=S),
    "editar_bl_repetido_fila_2a": lambda: sio.actualizar_embarque("MON-DUP", "Montacargas", {sio.COL_DESC: "Parcial B"}, fila_sugerida=9, sello_esperado=S),
    "editar_bl_repetido_sin_fila": lambda: sio.actualizar_embarque("MON-DUP", "Montacargas", {sio.COL_DESC: "?"}, fila_sugerida=None, sello_esperado=S),
    "editar_sello_viejo": lambda: sio.actualizar_embarque("MON-TRANSITO", "Montacargas", {sio.COL_CANT: "9"}, fila_sugerida=2, sello_esperado="viejo"),
    "editar_columna_nueva": lambda: sio.actualizar_embarque("CYM-1", "Construcción y Minería", {sio.COL_MODELO: "ZX200-5G"}, fila_sugerida=2, sello_esperado=S),
    "alta": lambda: sio.append_row({sio.COL_BL: "NUEVO", sio.COL_DESC: "x", sio.COL_ETA: d(9).isoformat(), sio.COL_OC: "45"}, "General"),
    "alta_repetida": lambda: sio.append_row({sio.COL_BL: "grl-1", sio.COL_DESC: "x"}, "General"),
    "archivar": lambda: sio.marcar_como_recibido("MON-DECL", "Montacargas", fila_sugerida=7, fecha_almacen=d(-1)),
    "archivar_cif": lambda: sio.marcar_como_recibido("ELE-1", "Elevadores", fila_sugerida=2, venta_cif=True),
    "archivar_sin_declarar_con_fecha": lambda: sio.marcar_como_recibido("MON-PUERTO", "Montacargas", fila_sugerida=6, fecha_declaracion=d(-2), fecha_almacen=d(-1)),
    "archivar_sin_llegada": lambda: sio.marcar_como_recibido("MON-TRANSITO", "Montacargas", fila_sugerida=2),
    "archivar_almacen_futuro": lambda: sio.marcar_como_recibido("MON-DECL", "Montacargas", fila_sugerida=7, fecha_almacen=d(3)),
    "revertir": lambda: sio.quitar_de_recibido("HIS-1", fila_sugerida=2),
    "corregir_fechas_recibido": lambda: sio.corregir_fechas_recibido("HIS-1", fila_sugerida=2, almacen=d(-29), sello_esperado="2026-10-01 09:00:00"),
    "eliminar": lambda: sio.eliminar_embarque("GEN-1", "Generadores", fila_sugerida=2),
    "normalizar_etas": lambda: sio.normalizar_etas([("Montacargas", 2, d(20).isoformat(), "2099-01-01"), ("Montacargas", 3, "otro", "2099-01-02")]),
    "pago_nuevo": lambda: sio.guardar_pago("CYM-1", {"ADUANAS": 1500.0, "FLETE": ""}, empresa="Antillana Comercial", referencia={sio.COL_DESC: "Excavadora"}),
    "pago_editar": lambda: sio.guardar_pago("mon-decl", {c: (700.0 if c == "DPH" else "") for c in sio.CONCEPTOS_PAGO}, empresa="Tecnicaribe", sello_esperado=S),
    "pago_corregir_llegada": lambda: sio.guardar_pago("SIN-TRANSITO", {}, llegada=d(-3).isoformat(), sello_esperado=S),
    "pago_sin_mora": lambda: sio.registrar_sin_mora("MON-DECL", d(5)),
    "pago_sin_mora_repetido": lambda: sio.registrar_sin_mora("MON-PUERTO", d(5)),
    "pago_realizado": lambda: sio.registrar_pago_realizado("MON-DECL", HOY, {"USD": 10.0, "DOP": 0.0}),
    "pago_estado": lambda: sio.marcar_estado_pago("MON-PUERTO", "Pagado"),
    "pago_prioridad": lambda: sio.fijar_prioridad_pago("MON-PUERTO", 7),
    "pago_prioridad_quitar": lambda: sio.fijar_prioridad_pago("MON-PUERTO", None),
    "sincronizar_pagos": lambda: sio.sincronizar_pagos_con_transito(todo["activos"], todo["historico"]),
    "igualar_llegadas": lambda: sio.igualar_llegadas_pagos([("MON-PUERTO", d(-4)), ("NO-ESTA", d(-1))]),
}
res["escrituras"] = {}
for nombre, guion in GUIONES.items():
    libro = datos.libro_principal()
    sio.get_spreadsheet = lambda: libro
    st.cache_data.clear()
    st.cache_resource.clear()
    resultado = seguro(guion)
    res["escrituras"][nombre] = {"resultado": resultado, "sheet": foto(libro)}

Path(salida).write_text(json.dumps(res, ensure_ascii=False, indent=1, sort_keys=True, default=str), encoding="utf-8")
print(f"OK {salida}")
