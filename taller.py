"""
taller.py - Sección Taller, paso 1: vínculo de SOLO LECTURA con el Sheet aparte
================================================================================

Muestra lo que hay en el Sheet de Repuesto y Taller (secret TALLER_SHEET_ID), leído con su
propia llamada y su propia caché (sheets_io.cargar_taller): un fallo de Taller no
toca Tránsito ni Pagos. Esta versión sirve para comprobar el vínculo y los datos;
las tarjetas, las fichas y los formularios llegan en pasos siguientes.

Capa: igual que pagos.py (importa de sheets_io hacia abajo). No importa pagos.py a
propósito: un cambio en Pagos no debe poder romper Taller.

Reglas (decididas el 12 oct 2026):
 - FECHA DE CORTE = fecha en que se pagó. Con fecha, el renglón está Pagado (misma
   regla que Pagos, decisión 9); sin fecha, está Pendiente.
 - Días sin pagar = (fecha de pago, o hoy si sigue pendiente) - fecha de la
   solicitud. Se calcula aquí: la columna DÍAS SIN PAGAR del Sheet no se lee.
 - Prioridad 1-4 (decisión 11); varias filas pueden compartirla; vacía va debajo.
   Orden: prioridad y, dentro de cada nivel, más días sin pagar primero.
 - USD y DOP son columnas separadas: nunca se suman entre sí.
"""

from __future__ import annotations

import re
from datetime import date
import unicodedata
from numbers import Real

import pandas as pd
import streamlit as st

from sheets_io import (
    COL_T_CLIENTE, COL_T_CORTE, COL_T_DOP, COL_T_EMPRESA, COL_T_PRIORIDAD,
    COL_T_PROVEEDOR, COL_T_SOLICITUD, COL_T_USD, EMPRESA_ANTILLANA,
    EMPRESA_MOTOR_IBERICO, EMPRESA_TECNICARIBE, EMPRESAS_PAGO, MESES_ES_CORTO,
    PRIORIDADES_PAGO, _norm_encabezado, a_numero, ahora_rd, cargar_taller, hoy_rd, parsear_fecha,
    snapshot_taller, taller_agregar, taller_editar, taller_limpiar, taller_marcar_pago,
)
from html import escape as _esc
from ui_encabezado import encabezado_html

# El Sheet puede traer "ANTILLANA" o "Antillana Comercial": ambas valen.
_EMPRESAS = {
    "antillana": EMPRESA_ANTILLANA, "antillana comercial": EMPRESA_ANTILLANA,
    "tecnicaribe": EMPRESA_TECNICARIBE, "motor iberico": EMPRESA_MOTOR_IBERICO,
}
_COLUMNAS = ["Fila", "Empresa", "Proveedor", "Prioridad", "Cliente", "Solicitud",
             "Pago", "Pagado", "Dias", "USD", "DOP", "Descripcion", "Departamento", "Local"]


_DIAS_SEMANA = r"(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|lun|mar|mi[eé]|jue|vie|s[aá]b|dom)"


def _fecha_celda(v):
    """Fecha de una celda de Repuesto y Taller, con más tolerancia que parsear_fecha
    (que sigue igual para Embarques y Pagos). Además de lo que ya entiende, acepta
    hora al final ('14/10/2026 10:30 PM'), día de la semana ('miércoles 14 de octubre
    de 2026'), separado por espacios ('14 10 2026'), pegado ('14102026', '14oct2026')
    y mes en texto sin año ('14-oct', se asume el año en curso). Si nada sirve,
    devuelve None y la fila se avisa en pantalla en vez de quedar mal en silencio."""
    f = parsear_fecha(v)
    if f is not None or not isinstance(v, str):
        return f
    t = v.strip().lower()
    t = re.sub(r"[\s,;]*\d{1,2}:\d{2}(:\d{2})?(\.\d+)?\s*(a\.?\s?m\.?|p\.?\s?m\.?)?\s*$", "", t)   # hora al final
    t = re.sub(r"^\s*" + _DIAS_SEMANA + r"\.?[\s,]*", "", t)                                   # día de la semana
    t = t.strip()
    f = parsear_fecha(t)
    if f is not None:
        return f
    m = re.fullmatch(r"(\d{1,2})[\s./-]+(\d{1,2})[\s./-]+(\d{2,4})", t)                          # 14 10 2026
    if not m:
        m = re.fullmatch(r"(\d{2})(\d{2})(\d{4})", t)                                             # 14102026
    if m:
        d, mo, a = int(m[1]), int(m[2]), int(m[3])
        a += 2000 if a < 100 else 0
        try:
            return date(a, mo, d)
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})\s*([a-záéíóú]{3,})\.?\s*(\d{2,4})", t)                          # 14oct2026
    if m:
        return parsear_fecha(f"{m[1]} {m[2]} {m[3]}")
    m = re.fullmatch(r"(\d{1,2})[\s./-]*([a-záéíóú]{3,})\.?", t)                                   # 14-oct
    if m:
        return parsear_fecha(f"{m[1]} {m[2]} {hoy_rd().year}")
    return None


def _plano(v):
    """pandas puede entregar escalares de numpy; se pasan a Python puro."""
    return v.item() if hasattr(v, "item") else v


def _texto(v) -> str:
    v = _plano(v)
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _clave_empresa(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return " ".join(t.lower().split())


def _monto(v):
    v = _plano(v)
    if isinstance(v, Real) and not isinstance(v, bool):
        return float(v)
    return a_numero(v)


def _prioridad(texto):
    try:
        n = float(texto)
        return int(n) if n == int(n) and int(n) in PRIORIDADES_PAGO else None
    except (ValueError, OverflowError):
        return None


def enriquecer_taller(df: pd.DataFrame, hoy):
    """Devuelve (DataFrame limpio, avisos sobre los datos del Sheet)."""
    filas, avisos = [], []
    # Columnas opcionales del formato de Repuesto: si el Sheet no las tiene, quedan vacías.
    def _busca(clave):
        return next((c for c in df.columns if _norm_encabezado(c) == clave), None)
    col_desc, col_dep, col_loc = _busca("descripcion"), _busca("departamento"), _busca("localfabrica")
    for _, r in df.iterrows():
        prov, cli = _texto(r.get(COL_T_PROVEEDOR)), _texto(r.get(COL_T_CLIENTE))
        usd, dop = _monto(r.get(COL_T_USD)), _monto(r.get(COL_T_DOP))
        if not prov and not cli and usd is None and dop is None:
            continue  # fila preparada y todavía en blanco
        n = int(r["FilaSheet"])
        sol_crudo, pago_crudo = _plano(r.get(COL_T_SOLICITUD)), _plano(r.get(COL_T_CORTE))
        sol, pago = _fecha_celda(sol_crudo), _fecha_celda(pago_crudo)
        emp_txt, prio_txt = _texto(r.get(COL_T_EMPRESA)), _texto(r.get(COL_T_PRIORIDAD))
        emp, prio = _EMPRESAS.get(_clave_empresa(emp_txt), ""), _prioridad(prio_txt)
        if emp_txt and not emp:
            avisos.append(f"Fila {n}: empresa '{emp_txt}' no reconocida.")
        if prio_txt and prio is None:
            avisos.append(f"Fila {n}: prioridad '{prio_txt}' no válida (usa 1 a 4).")
        if sol is None:
            avisos.append(f"Fila {n}: " + (f"fecha de solicitud '{_texto(sol_crudo)}' no válida; no se cuentan días."
                                           if _texto(sol_crudo) else "sin fecha de solicitud; no se cuentan días."))
        if pago is None and _texto(pago_crudo):
            avisos.append(f"Fila {n}: fecha de pago '{_texto(pago_crudo)}' no válida; se toma como pendiente.")
        elif pago and pago < sol:
            avisos.append(f"Fila {n}: la fecha de corte es anterior a la solicitud.")
        if usd is None and dop is None:
            avisos.append(f"Fila {n}: sin monto.")
        dias = None if sol is None else max(0, ((pago or hoy) - sol).days)
        filas.append({"Fila": n, "Empresa": emp or "Sin empresa",
                      "Proveedor": prov or "(sin proveedor)", "Prioridad": prio,
                      "Cliente": cli, "Solicitud": sol, "Pago": pago,
                      "Pagado": pago is not None, "Dias": dias, "USD": usd, "DOP": dop,
                      "Descripcion": _texto(r.get(col_desc)) if col_desc else "",
                      "Departamento": _texto(r.get(col_dep)) if col_dep else "",
                      "Local": _texto(r.get(col_loc)) if col_loc else ""})
    out = pd.DataFrame(filas, columns=_COLUMNAS)
    for c in ("Prioridad", "Dias", "USD", "DOP"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out, avisos


def ordenar_taller(df: pd.DataFrame) -> pd.DataFrame:
    """Prioridad primero (1 arriba, sin prioridad al final); dentro de cada nivel,
    más días sin pagar primero; sin contador de días después."""
    clave = pd.DataFrame({"p": df["Prioridad"].fillna(99), "s": df["Dias"].isna(),
                          "d": -df["Dias"].fillna(0), "f": df["Fila"]}, index=df.index)
    return df.loc[clave.sort_values(["p", "s", "d", "f"]).index]


def _fecha(d) -> str:
    return "—" if d is None or pd.isna(d) else d.strftime("%d/%m/%Y")


def _num(x, prefijo: str = "", decimales: int = 0) -> str:
    return "—" if pd.isna(x) else f"{prefijo}{x:,.{decimales}f}"


def _ini_monto(x) -> float:
    return 0.0 if x is None or pd.isna(x) else float(x)


def _num_o_none(x):
    return None if not x else float(x)


def _campos(prefijo: str, ini: dict) -> dict:
    """Campos comunes de agregar/editar (dentro de un st.form)."""
    emp = ini.get("Empresa")
    c1, c2 = st.columns(2)
    empresa = c1.selectbox("Empresa", EMPRESAS_PAGO, key=f"{prefijo}_emp",
                           index=EMPRESAS_PAGO.index(emp) if emp in EMPRESAS_PAGO else None)
    proveedor = c2.text_input("Proveedor", value=ini.get("Proveedor", ""), key=f"{prefijo}_prov", max_chars=120)
    c3, c4 = st.columns(2)
    opciones = [None] + list(PRIORIDADES_PAGO)
    p = ini.get("Prioridad")
    prioridad = c3.selectbox("Prioridad", opciones, key=f"{prefijo}_prio",
                             index=opciones.index(int(p)) if p is not None and not pd.isna(p) and int(p) in opciones else 0,
                             format_func=lambda x: "—" if x is None else str(x))
    cliente = c4.text_input("Cliente", value=ini.get("Cliente", ""), key=f"{prefijo}_cli", max_chars=120)
    c5, c6, c7 = st.columns(3)
    solicitud = c5.date_input("Fecha de la solicitud", value=ini.get("Solicitud") or hoy_rd(),
                              max_value=hoy_rd(), format="DD/MM/YYYY", key=f"{prefijo}_sol")
    usd = c6.number_input("USD pendiente (solo dólares)", min_value=0.0, step=0.01, format="%.2f",
                          value=_ini_monto(ini.get("USD")),
                          key=f"{prefijo}_usd")
    dop = c7.number_input("DOP pendiente (solo pesos)", min_value=0.0, step=0.01, format="%.2f",
                          value=_ini_monto(ini.get("DOP")),
                          key=f"{prefijo}_dop")
    return {"empresa": empresa, "proveedor": proveedor, "prioridad": prioridad, "cliente": cliente,
            "solicitud": solicitud, "usd": _num_o_none(usd), "dop": _num_o_none(dop)}


def _resultado(ok: bool, msg: str):
    """Tras escribir: sube la versión (reinicia formularios y selección) y recarga."""
    if ok:
        st.session_state["taller_v"] = st.session_state.get("taller_v", 0) + 1
        st.session_state.pop("taller_snap", None)
        st.session_state["taller_msg"] = msg
        st.rerun()
    else:
        st.error(msg)


def _gestion(datos_raw: pd.DataFrame, df: pd.DataFrame):
    """Agregar / editar / pagar / vaciar. Solo rol taller o admin (se vuelve a
    verificar dentro de cada función de escritura)."""
    v = st.session_state.get("taller_v", 0)
    if st.session_state.get("taller_msg"):
        st.success(st.session_state.pop("taller_msg"))
    st.markdown("#### Gestionar Repuesto y Taller")
    t_add, t_edit = st.tabs(["Agregar", "Editar / pagar / vaciar"])
    with t_add:
        with st.form(f"taller_form_add_{v}"):
            nuevo = _campos(f"tad_{v}", {})
            if st.form_submit_button("Agregar", type="primary"):
                _resultado(*taller_agregar(nuevo))
    with t_edit:
        if df.empty:
            st.info("No hay renglones para editar.")
            return
        ordenado = ordenar_taller(df)
        filas = list(ordenado["Fila"])
        etiquetas = {r.Fila: f"Fila {r.Fila} · {r.Proveedor} · {r.Empresa}" + (" · Pagado" if r.Pagado else "")
                     for r in ordenado.itertuples()}
        n = st.selectbox("Renglón", filas, index=None, format_func=etiquetas.get,
                         placeholder="Elige un renglón", key=f"taller_sel_{v}")
        if n is None:
            st.session_state.pop("taller_snap", None)
            return
        sel = st.session_state.get("taller_snap")
        if not sel or sel["fila"] != n:        # la huella se toma al SELECCIONAR, no al guardar
            crudo = datos_raw[datos_raw["FilaSheet"] == n]
            if crudo.empty:
                st.warning("Ese renglón ya no está. Pulsa Actualizar datos.")
                return
            fila_df = df[df["Fila"] == n].iloc[0]
            sel = {"fila": n, "snap": snapshot_taller(crudo.iloc[0]), "vals": fila_df.to_dict()}
            st.session_state["taller_snap"] = sel
        if st.button("Recargar este renglón", key=f"taller_rec_{v}"):
            st.session_state.pop("taller_snap", None)
            st.rerun()
        ini = sel["vals"]
        st.caption("Datos tal como estaban al seleccionar el renglón. Si alguien lo cambia en el Sheet, "
                   "guardar se rechaza para no pisar su cambio.")
        with st.form(f"taller_form_edit_{v}_{n}"):
            datos = _campos(f"ted_{v}_{n}", ini)
            if st.form_submit_button("Guardar cambios", type="primary"):
                datos["corte"] = ini.get("Pago")      # el pago se maneja abajo
                _resultado(*taller_editar(n, sel["snap"], datos))
        st.markdown("**Pago**")
        pc1, pc2, pc3 = st.columns([2, 1, 1])
        fpago = pc1.date_input("Fecha de pago", value=ini.get("Pago") or hoy_rd(), max_value=hoy_rd(),
                               format="DD/MM/YYYY", key=f"tpg_{v}_{n}")
        if pc2.button("Marcar pagado", key=f"tpm_{v}_{n}"):
            _resultado(*taller_marcar_pago(n, sel["snap"], fpago))
        if ini.get("Pagado") and pc3.button("Quitar pago", key=f"tpq_{v}_{n}"):
            _resultado(*taller_marcar_pago(n, sel["snap"], None))
        st.markdown("**Eliminar**")
        ok = st.checkbox("Confirmo vaciar este renglón (queda en la bitácora)", key=f"tel_ok_{v}_{n}")
        if st.button("Vaciar renglón", disabled=not ok, key=f"tel_{v}_{n}"):
            _resultado(*taller_limpiar(n, sel["snap"]))


# --- Vista (mismo estilo que Estatus de Pagos de embarques) -----------------
_CSS = (
    "<style>"
    ".tl-wrap { overflow-x:auto; margin:6px 0 16px; }"
    ".tl-tabla { width:100%; border-collapse:separate; border-spacing:0; background:#fff;"
    " border:1px solid #E5E7EB; border-radius:12px; overflow:hidden; box-shadow:0 1px 4px rgba(17,24,39,0.06); }"
    ".tl-tabla th { background:#111827; color:#fff; font-size:0.72rem; font-weight:700; text-transform:uppercase;"
    " letter-spacing:0.05em; padding:11px 14px; text-align:center; white-space:nowrap; }"
    ".tl-tabla td { padding:9px 14px; text-align:center; font-size:0.88rem; color:#111827;"
    " border-top:1px solid #F3F4F6; white-space:nowrap; }"
    ".tl-tabla tbody tr:nth-child(even) { background:#F9FAFB; }"
    ".tl-tabla td.d { font-weight:800; } .tl-tabla td.v { color:#9CA3AF; }"
    ".tl-tabla td.rojo { font-weight:800; color:#B91C1C; background:#FEE2E2; }"
    ".tl-card { border:1px solid #E5E7EB; border-radius:12px; padding:16px 20px; margin-bottom:14px;"
    " background:#fff; box-shadow:0 1px 4px rgba(17,24,39,0.06); }"
    ".tl-cabeza { display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }"
    ".tl-prov { font-weight:700; font-size:1.02rem; color:#111827; }"
    ".tl-emp { display:inline-block; background:#EEF2FF; color:#3730A3; font-size:0.72rem; font-weight:700;"
    " padding:1px 10px; border-radius:999px; margin-left:10px; }"
    ".tl-estado { display:inline-block; font-size:0.75rem; font-weight:700; color:#fff; padding:2px 12px;"
    " border-radius:999px; }"
    ".tl-meta { color:#6B7280; font-size:0.85rem; margin-top:2px; }"
    ".tl-chips { display:flex; flex-wrap:wrap; justify-content:center; gap:8px; margin:14px 0; }"
    ".tl-chip { padding:6px 15px; border-radius:999px; color:#fff; font-size:0.83rem; font-weight:700;"
    " overflow-wrap:anywhere; }"
    ".tl-totales { display:flex; flex-wrap:wrap; justify-content:center; align-items:baseline; gap:28px; }"
    ".tl-etq { font-size:0.68rem; text-transform:uppercase; letter-spacing:0.04em; color:#6B7280; display:block;"
    " text-align:center; }"
    ".tl-val { font-size:1.2rem; font-weight:800; color:#111827; display:block; text-align:center; }"
    ".tl-val.f { font-size:0.95rem; }"
    ".tl-pie { text-align:center; color:#9CA3AF; font-size:0.78rem; margin-top:10px; }"
    "@media (max-width:640px) { .tl-totales { gap:14px 18px; } }"
    ".tl-prio { display:inline-block; font-size:0.72rem; font-weight:800; letter-spacing:0.03em; padding:1px 10px;"
    " border-radius:999px; border:2px solid; background:#fff; white-space:nowrap; }"
    ".tl-prio b { font-size:0.95rem; }"
    ".tl-tarjetas { display:grid; grid-template-columns:repeat(4,1fr); gap:1rem; margin:0.4rem 0 0.8rem; }"
    ".tl-t { color:#fff; border-radius:14px; min-height:92px; display:flex; flex-direction:column;"
    " align-items:center; justify-content:center; padding:14px 10px; text-align:center;"
    " box-shadow:0 2px 8px rgba(17,24,39,0.12); }"
    ".tl-t .e { font-size:0.68rem; font-weight:700; letter-spacing:0.05em; text-transform:uppercase; opacity:0.92; }"
    ".tl-t .n { font-size:1.35rem; font-weight:800; margin-top:6px; }"
    "@media (max-width:900px) { .tl-tarjetas { grid-template-columns:repeat(2,1fr); } }"
    "</style>"
)
DIAS_ROJO = 30   # más de 30 días sin pagar: el contador sale en rojo
_COLOR_PRIO = {1: "#B91C1C", 2: "#B45309", 3: "#0C447C", 4: "#6B7280"}
_ESTATUS = ["Todos", "Pendiente", "Pagado"]


def _fd(d) -> str:
    return "—" if d is None or pd.isna(d) else f"{d.day:02d} {MESES_ES_CORTO[d.month]} {d.year}"


def _usd(x) -> str:
    return "—" if pd.isna(x) or not x else f"US$ {x:,.2f}"


def _dop(x) -> str:
    return "—" if pd.isna(x) or not x else f"RD$ {x:,.2f}"


def _pildora(n) -> str:
    if pd.isna(n):
        return '<td class="v">—</td>'
    n = int(n)
    c = _COLOR_PRIO.get(n, "#6B7280")
    return f'<td><span class="tl-prio" style="border-color:{c}; color:{c};">PRIORIDAD&nbsp;<b>{n}</b></span></td>'


def _celda(x, clase: str = "") -> str:
    vacio = x is None or x == "" or (isinstance(x, float) and pd.isna(x))
    return '<td class="v">—</td>' if vacio else f'<td class="{clase}">{_esc(str(x))}</td>'


def _tabla(df: pd.DataFrame, pagados: bool) -> str:
    if pagados:
        cab = ["Empresa", "Proveedor", "Prioridad", "Cliente", "Solicitud", "Fecha de pago",
               "Días de pago", "USD pagado", "DOP pagado"]
    else:
        cab = ["Empresa", "Proveedor", "Prioridad", "Cliente", "Solicitud", "Días sin pagar",
               "USD pendiente", "DOP pendiente"]
    filas = []
    for r in df.itertuples():
        dias = "" if pd.isna(r.Dias) else int(r.Dias)
        celdas = [_celda(r.Empresa), _celda(r.Proveedor), _pildora(r.Prioridad), _celda(r.Cliente),
                  _celda(_fd(r.Solicitud) if r.Solicitud is not None else "")]
        if pagados:
            celdas.append(_celda(_fd(r.Pago) if r.Pago is not None else ""))
        rojo = (not pagados) and dias != "" and dias > DIAS_ROJO
        celdas += [_celda(dias, "rojo" if rojo else "d"), _celda(_usd(r.USD) if _usd(r.USD) != "—" else ""),
                   _celda(_dop(r.DOP) if _dop(r.DOP) != "—" else "")]
        filas.append("<tr>" + "".join(celdas) + "</tr>")
    return ('<div class="tl-wrap"><table class="tl-tabla"><thead><tr>'
            + "".join(f"<th>{c}</th>" for c in cab) + "</tr></thead><tbody>" + "".join(filas)
            + "</tbody></table></div>")


def _pildora_span(n) -> str:
    if pd.isna(n):
        return ""
    n = int(n)
    c = _COLOR_PRIO.get(n, "#6B7280")
    return f'<span class="tl-prio" style="border-color:{c}; color:{c};">PRIORIDAD&nbsp;<b>{n}</b></span>'


def _tarjeta_pagado(r) -> str:
    """Un renglón PAGADO con todo lo que tiene, con la estructura de las tarjetas de
    Pagos de tránsito: cabecera, detalle, chip descripción: monto y fila de totales."""
    hay_usd, hay_dop = not pd.isna(r.USD) and bool(r.USD), not pd.isna(r.DOP) and bool(r.DOP)
    desc = _esc(str(r.Descripcion)).strip() if r.Descripcion else ""
    chips = []
    if hay_usd:
        chips.append(f'<span class="tl-chip" style="background:#2563EB;">{(desc or "PAGO").upper()}: {_usd(r.USD)}</span>')
    if hay_dop:
        chips.append(f'<span class="tl-chip" style="background:#0F9D7A;">{(desc or "PAGO").upper()}: {_dop(r.DOP)}</span>')
    totales = []
    if hay_usd:
        totales.append(f'<div><span class="tl-etq">Monto pagado US$</span><span class="tl-val">{_usd(r.USD)}</span></div>')
    if hay_dop:
        totales.append(f'<div><span class="tl-etq">Monto pagado RD$</span><span class="tl-val">{_dop(r.DOP)}</span></div>')
    if not totales:
        totales.append('<div><span class="tl-etq">Monto pagado</span><span class="tl-val">—</span></div>')
    totales.append(f'<div><span class="tl-etq">Fecha de solicitud</span><span class="tl-val f">{_fd(r.Solicitud)}</span></div>')
    totales.append(f'<div><span class="tl-etq">Fecha de pago</span><span class="tl-val f">{_fd(r.Pago)}</span></div>')
    dias = "—" if pd.isna(r.Dias) else str(int(r.Dias))
    totales.append(f'<div><span class="tl-etq">Días de pago</span><span class="tl-val">{dias}</span></div>')
    partes = [f"Cliente: {_esc(str(r.Cliente))}" if r.Cliente else "",
              f"Departamento: {_esc(str(r.Departamento))}" if r.Departamento else "",
              f"Local / Fábrica: {_esc(str(r.Local))}" if r.Local else ""]
    meta = " · ".join(x for x in partes if x)
    return ('<div class="tl-card">'
            f'<div class="tl-cabeza"><span class="tl-prov">{_esc(str(r.Proveedor))}'
            f'<span class="tl-emp">{_esc(str(r.Empresa))}</span> {_pildora_span(r.Prioridad)}</span>'
            '<span class="tl-estado" style="background:#2E7D32;">Pagado</span></div>'
            + (f'<div class="tl-meta">{meta}</div>' if meta else "")
            + f'<div class="tl-chips">{"".join(chips)}</div>'
            + f'<div class="tl-totales">{"".join(totales)}</div>'
            + f'<div class="tl-pie">Pagado el {_fd(r.Pago)}</div></div>')


def _lista_pagados(df: pd.DataFrame) -> str:
    return "".join(_tarjeta_pagado(r) for r in df.itertuples())


def _tarjetas(pagados: pd.DataFrame, pend: pd.DataFrame) -> str:
    con_ambas = pagados[pagados["Dias"].notna()]
    prom = f"{con_ambas['Dias'].mean():.0f} d" if len(con_ambas) else "—"
    total = (f"US$ {pend['USD'].sum():,.2f} · RD$ {pend['DOP'].sum():,.2f}")
    t = lambda color, etq, val: (f'<div class="tl-t" style="background:{color};"><div class="e">{etq}</div>'
                                 f'<div class="n">{val}</div></div>')
    return ('<div class="tl-tarjetas">' + t("#17A2B8", "Pagados", len(pagados))
            + t("#2E86DE", "Pendientes", len(pend)) + t("#B45309", "Tiempo promedio de pago", prom)
            + t("#0C447C", "Total por pagar", total) + "</div>"), len(con_ambas)


def _encabezado(hora) -> None:
    sello = f"Sheet de Repuesto y Taller leído a las {hora.strftime('%I:%M %p').lstrip('0').lower()} (hora RD)"
    st.markdown(encabezado_html("Estatus de Pagos", "taller", hoy_rd().year, sello, " · Repuesto y Taller"),
                unsafe_allow_html=True)


def _diagnostico(crudo: pd.DataFrame) -> None:
    """Qué leyó la app del Sheet: cuántas celdas con dato hay por columna reconocida y
    qué encabezados no reconoció. Solo lo ven admin y Repuesto y Taller; sirve para
    separar 'el Sheet no tiene el dato' de 'la app no lo está leyendo'."""
    reconocidas = [COL_T_EMPRESA, COL_T_PROVEEDOR, COL_T_PRIORIDAD, COL_T_CLIENTE,
                   COL_T_SOLICITUD, "DÍAS SIN PAGAR", COL_T_CORTE, COL_T_USD, COL_T_DOP]
    with st.expander("Diagnóstico de lectura"):
        filas = []
        for c in reconocidas:
            n = 0 if c not in crudo.columns else int(sum(1 for v in crudo[c] if _texto(v) != ""))
            filas.append({"Columna que usa la app": c, "Celdas con dato": n})
        st.dataframe(pd.DataFrame(filas), hide_index=True)
        otras = [c for c in crudo.columns if c not in reconocidas and c != "FilaSheet"]
        st.caption("Encabezados del Sheet que la app no usa (se ignoran): " + (", ".join(map(str, otras)) or "ninguno"))
        con_pago = crudo[[_texto(v) != "" for v in crudo[COL_T_CORTE]]] if COL_T_CORTE in crudo.columns else crudo.iloc[0:0]
        if con_pago.empty:
            st.caption("No hay ninguna celda con dato en la columna de fecha de pago (FECHA DE PAGO / FECHA DE CORTE).")
        else:
            st.caption("Valores crudos leídos en fecha de pago (fila del Sheet → valor):")
            for _, r in con_pago.head(10).iterrows():
                v = _plano(r[COL_T_CORTE])
                st.write(f"Fila {int(r['FilaSheet'])} → {v!r} → " + (str(_fecha_celda(v)) if _fecha_celda(v) else "no se entiende como fecha"))


def panel_taller(puede_escribir: bool, selector_origen):
    """Vista de Taller dentro de Estatus de Pagos. selector_origen() dibuja el filtro
    'Origen' (lo provee pagos.py) en la primera columna de la fila de filtros."""
    st.markdown(_CSS, unsafe_allow_html=True)
    with st.spinner("Leyendo Repuesto y Taller…"):
        datos = cargar_taller()
    _encabezado(datos["hora"] if datos["hora"] else ahora_rd())
    df, avisos = enriquecer_taller(datos["df"], hoy_rd()) if not datos["error"] else (pd.DataFrame(), [])
    c0, c1, c2, c3 = st.columns(4)
    with c0:
        selector_origen()
    if datos["error"]:
        st.error(datos["error"])
        return
    if datos["aviso"]:
        st.warning(datos["aviso"])
    if df.empty:
        st.info("El Sheet de Repuesto y Taller todavía no tiene filas con datos.")
        if puede_escribir:
            _gestion(datos["df"], df)
        return

    with c1:
        empresa = st.selectbox("Empresa", ["Todas"] + EMPRESAS_PAGO, key="taller_empresa")
    vista = df if empresa == "Todas" else df[df["Empresa"] == empresa]
    meses = sorted({(d.year, d.month) for d in vista["Solicitud"] if d is not None and not pd.isna(d)})
    opciones_mes = ["Todos"] + [f"{MESES_ES_CORTO[m]} {a}" for a, m in meses]
    if st.session_state.get("taller_mes") not in opciones_mes:   # antes de crear el widget
        st.session_state["taller_mes"] = "Todos"
    with c2:
        estatus = st.selectbox("Estatus", _ESTATUS, key="taller_estatus")
    with c3:
        mes = st.selectbox("Mes (Solicitud)", opciones_mes, key="taller_mes")
    if mes != "Todos":
        a_sel, m_sel = next((a, m) for a, m in meses if f"{MESES_ES_CORTO[m]} {a}" == mes)
        vista = vista[vista["Solicitud"].apply(
            lambda d: d is not None and not pd.isna(d) and d.year == a_sel and d.month == m_sel)]

    pagados, pend = vista[vista["Pagado"]], vista[~vista["Pagado"]]
    html_t, n_prom = _tarjetas(pagados, pend)
    st.markdown(html_t, unsafe_allow_html=True)
    if n_prom:
        st.caption(f"Tiempo promedio de pago: días entre la solicitud y el pago realizado, "
                   f"sobre {n_prom} renglón(es) pagados con ambas fechas.")
    if estatus in ("Todos", "Pendiente"):
        st.markdown("**Pendientes por antigüedad**")
        if pend.empty:
            st.caption("No hay pendientes con esta selección.")
        else:
            st.markdown(_tabla(ordenar_taller(pend), False), unsafe_allow_html=True)
    if estatus in ("Todos", "Pagado"):
        st.markdown("**Pagados**")
        if pagados.empty:
            st.caption("No hay pagos registrados con esta selección.")
        else:
            st.markdown(_lista_pagados(pagados.sort_values("Pago", ascending=False)), unsafe_allow_html=True)

    if avisos:
        with st.expander(f"Datos por revisar en el Sheet ({len(avisos)})"):
            for a in avisos[:50]:
                st.write(a)
            if len(avisos) > 50:
                st.write(f"… y {len(avisos) - 50} más.")
    if puede_escribir:
        _diagnostico(datos["df"])
        st.divider()
        _gestion(datos["df"], df)
