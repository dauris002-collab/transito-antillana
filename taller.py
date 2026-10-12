"""
taller.py - Sección Taller, paso 1: vínculo de SOLO LECTURA con el Sheet aparte
================================================================================

Muestra lo que hay en el Sheet de Taller (secret TALLER_SHEET_ID), leído con su
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

import unicodedata
from numbers import Real

import pandas as pd
import streamlit as st

from sheets_io import (
    COL_T_CLIENTE, COL_T_CORTE, COL_T_DOP, COL_T_EMPRESA, COL_T_PRIORIDAD,
    COL_T_PROVEEDOR, COL_T_SOLICITUD, COL_T_USD, EMPRESA_ANTILLANA,
    EMPRESA_MOTOR_IBERICO, EMPRESA_TECNICARIBE, EMPRESAS_PAGO, PRIORIDADES_PAGO,
    a_numero, cargar_taller, hoy_rd, parsear_fecha, snapshot_taller, taller_agregar,
    taller_editar, taller_limpiar, taller_marcar_pago,
)

# El Sheet puede traer "ANTILLANA" o "Antillana Comercial": ambas valen.
_EMPRESAS = {
    "antillana": EMPRESA_ANTILLANA, "antillana comercial": EMPRESA_ANTILLANA,
    "tecnicaribe": EMPRESA_TECNICARIBE, "motor iberico": EMPRESA_MOTOR_IBERICO,
}
_COLUMNAS = ["Fila", "Empresa", "Proveedor", "Prioridad", "Cliente", "Solicitud",
             "Pago", "Pagado", "Dias", "USD", "DOP"]


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
    for _, r in df.iterrows():
        prov, cli = _texto(r.get(COL_T_PROVEEDOR)), _texto(r.get(COL_T_CLIENTE))
        usd, dop = _monto(r.get(COL_T_USD)), _monto(r.get(COL_T_DOP))
        if not prov and not cli and usd is None and dop is None:
            continue  # fila preparada y todavía en blanco
        n = int(r["FilaSheet"])
        sol = parsear_fecha(_plano(r.get(COL_T_SOLICITUD)))
        pago = parsear_fecha(_plano(r.get(COL_T_CORTE)))
        emp_txt, prio_txt = _texto(r.get(COL_T_EMPRESA)), _texto(r.get(COL_T_PRIORIDAD))
        emp, prio = _EMPRESAS.get(_clave_empresa(emp_txt), ""), _prioridad(prio_txt)
        if emp_txt and not emp:
            avisos.append(f"Fila {n}: empresa '{emp_txt}' no reconocida.")
        if prio_txt and prio is None:
            avisos.append(f"Fila {n}: prioridad '{prio_txt}' no válida (usa 1 a 4).")
        if sol is None:
            avisos.append(f"Fila {n}: sin fecha de solicitud; no se cuentan días.")
        elif pago and pago < sol:
            avisos.append(f"Fila {n}: la fecha de corte es anterior a la solicitud.")
        if usd is None and dop is None:
            avisos.append(f"Fila {n}: sin monto.")
        dias = None if sol is None else max(0, ((pago or hoy) - sol).days)
        filas.append({"Fila": n, "Empresa": emp or "Sin empresa",
                      "Proveedor": prov or "(sin proveedor)", "Prioridad": prio,
                      "Cliente": cli, "Solicitud": sol, "Pago": pago,
                      "Pagado": pago is not None, "Dias": dias, "USD": usd, "DOP": dop})
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
    st.markdown("#### Gestionar Taller")
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


def panel_taller(puede_escribir: bool = False):
    st.subheader("Taller")
    datos = cargar_taller()
    if datos["error"]:
        st.error(datos["error"])
        return
    df, avisos = enriquecer_taller(datos["df"], hoy_rd())
    if datos["aviso"]:
        st.warning(datos["aviso"])
    if df.empty:
        st.info("El Sheet de Taller todavía no tiene filas con datos.")
        if puede_escribir:
            _gestion(datos["df"], df)
        return

    c_emp, c_est = st.columns(2)
    with c_emp:
        empresa = st.selectbox("Empresa", ["Todas"] + sorted(df["Empresa"].unique()),
                               key="taller_empresa")
    with c_est:
        estado = st.radio("Estado", ["Pendientes", "Pagados", "Todos"], horizontal=True,
                          key="taller_estado")
    if empresa != "Todas":
        df = df[df["Empresa"] == empresa]

    pend = df[~df["Pagado"]]
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Pendientes", len(pend))
    k2.metric("USD pendiente", _num(pend["USD"].sum(), "US$ ", 2))
    k3.metric("DOP pendiente", _num(pend["DOP"].sum(), "RD$ ", 2))
    k4.metric("Más antiguo (días)", _num(pend["Dias"].max()))

    vista = df if estado == "Todos" else df[df["Pagado"] == (estado == "Pagados")]
    if vista.empty:
        st.info("No hay renglones con ese filtro.")
    else:
        v = ordenar_taller(vista)
        st.dataframe(pd.DataFrame({
            "Empresa": v["Empresa"], "Proveedor": v["Proveedor"],
            "Prioridad": v["Prioridad"].map(_num),
            "Cliente": v["Cliente"].replace("", "—"),
            "Fecha de la solicitud": v["Solicitud"].map(_fecha),
            "Días sin pagar": v["Dias"].map(_num),
            "Fecha de corte (pago)": v["Pago"].map(_fecha),
            "USD": v["USD"].map(lambda x: _num(x, "US$ ", 2)),
            "DOP": v["DOP"].map(lambda x: _num(x, "RD$ ", 2)),
        }), hide_index=True, width="stretch")

    if avisos:
        with st.expander(f"Datos por revisar en el Sheet ({len(avisos)})"):
            for a in avisos[:50]:
                st.write(a)
            if len(avisos) > 50:
                st.write(f"… y {len(avisos) - 50} más.")
    st.caption(f"Sheet de Taller leído a las {datos['hora'].strftime('%H:%M:%S')}."
               + ("" if puede_escribir else " Solo lectura."))
    if puede_escribir:
        st.divider()
        _gestion(datos["df"], df)
