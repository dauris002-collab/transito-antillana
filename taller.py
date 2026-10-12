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
    EMPRESA_MOTOR_IBERICO, EMPRESA_TECNICARIBE, PRIORIDADES_PAGO, a_numero,
    cargar_taller, hoy_rd, parsear_fecha,
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


def panel_taller():
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
    st.caption(f"Sheet de Taller leído a las {datos['hora'].strftime('%H:%M:%S')}. Solo lectura.")
