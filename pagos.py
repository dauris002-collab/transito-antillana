"""
pagos.py — Estatus de Pago de Antillana Comercial.

Módulo nuevo, separado de tránsito a propósito: reusa el mismo Google Sheet,
el mismo login y el mismo Streamlit Cloud, pero su hoja ('Pagos'), sus
columnas y sus escrituras viven aparte porque el dato es distinto (dinero, no
etapas de embarque) y así un error aquí no puede tocar tránsito.

Objetivo del módulo: visibilizar lo que cuesta no pagar a tiempo. No se
tabulan tarifas por día —varían por naviera, terminal, volumen y espacio—;
se compara lo que Logística fija como pago saludable ("SIN MORA") contra lo
que realmente se terminó pagando ("Pago Realizado"), y la diferencia es el
sobrecosto. Ambas ventanas se pueden corregir después si cambia el monto o la
fecha: no son de una sola vez y ya.

Usa Streamlit e importa de sheets_io.py, logica.py y ui_componentes.py.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from sheets_io import (
    CATEGORIAS, COL_BL, COL_CANT, COL_DESC, COL_ESTADO_PAGO, COL_ETA,
    COL_FECHA_LLEGADA_PUERTO, COL_FECHA_PAGO_REAL, COL_FECHA_SIN_MORA,
    CONCEPTOS_PAGO, ESTADO_PAGO_PAGADO, ESTADO_PAGO_PENDIENTE, MONEDA_CONCEPTO,
    a_numero, fecha_llegada_fila, formato_eta, guardar_pago, hoy_rd,
    invalidar_caches, marcar_estado_pago, parsear_fecha, registrar_log,
    registrar_pago_realizado, registrar_sin_mora,
)
from logica import enriquecer_pagos, resumen_pagos, totales_conceptos
from ui_componentes import COLOR_RECIBIDAS_MES, COLOR_TOTAL, CUSTOM_CSS, tarjeta_kpi


COLOR_SOBRECOSTO = "#991B1B"


COLOR_MORA_PROMEDIO = "#B45309"


def _fmt(monto, moneda: str) -> str:
    simbolo = "US$" if moneda == "USD" else "RD$"
    return f"{simbolo} {monto:,.2f}"


def _texto_dias_mora(dias) -> str:
    if dias is None:
        return "—"
    if dias > 0:
        return f"{dias} día(s) de mora"
    if dias < 0:
        return f"pagado {abs(dias)} día(s) antes"
    return "pagado justo a tiempo"


CATEGORIA_RECIBIDOS = "Recibidos (histórico)"


def _opciones_categoria(activos: pd.DataFrame, historico: pd.DataFrame) -> list:
    """Mismo orden de categorías que usa tránsito (Equipos, Generadores,
    Aéreos, Carga Suelta, Consolidados), y al final los ya archivados: para
    cuando el pago se registra después de que la carga ya se recibió."""
    disponibles = [c for c in CATEGORIAS
                  if activos is not None and not activos.empty and (activos["Categoria"] == c).any()]
    if historico is not None and not historico.empty:
        disponibles.append(CATEGORIA_RECIBIDOS)
    return disponibles


def _embarques_de_categoria(categoria: str, activos: pd.DataFrame, historico: pd.DataFrame) -> list:
    """BL, Descripción, Cantidad y Llegada de tránsito para elegir de una lista
    — nada de esto se vuelve a teclear. Ordenado por BL para que la lista sea
    estable entre refrescos."""
    filas = []
    if categoria == CATEGORIA_RECIBIDOS:
        for _, r in historico.iterrows():
            llegada = (parsear_fecha(r.get(COL_FECHA_LLEGADA_PUERTO, ""))
                      or parsear_fecha(r.get(COL_ETA, "")))
            filas.append({
                "bl": str(r.get(COL_BL, "")).strip(),
                "desc": str(r.get(COL_DESC, "")),
                "cant": str(r.get(COL_CANT, "")),
                "llegada_txt": formato_eta(llegada) if llegada else "—",
            })
    else:
        sub = activos[activos["Categoria"] == categoria]
        for _, r in sub.iterrows():
            llegada = fecha_llegada_fila(r)
            if llegada:
                llegada_txt = formato_eta(llegada)
            else:
                # Todavía sin confirmar: se muestra el ETA igual, marcado como
                # estimado, para no dejar la lista en blanco.
                eta = parsear_fecha(r.get(COL_ETA, ""))
                llegada_txt = f"{formato_eta(eta)} (ETA, sin confirmar)" if eta else "sin ETA"
            filas.append({
                "bl": str(r.get(COL_BL, "")).strip(),
                "desc": str(r.get(COL_DESC, "")),
                "cant": str(r.get(COL_CANT, "")),
                "llegada_txt": llegada_txt,
            })
    filas = [f for f in filas if f["bl"]]
    filas.sort(key=lambda f: f["bl"])
    return filas


# ---------------------------------------------------------------------------
# DASHBOARD (viewer + admin)
# ---------------------------------------------------------------------------
def _tarjetas_resumen(resumen: dict):
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(tarjeta_kpi("Expedientes cerrados", resumen["n_pagados"], COLOR_TOTAL),
                unsafe_allow_html=True)
    c2.markdown(tarjeta_kpi("Pagados a tiempo", resumen["n_a_tiempo"], COLOR_RECIBIDAS_MES),
                unsafe_allow_html=True)
    prom = resumen["dias_mora_promedio"]
    c3.markdown(tarjeta_kpi("Mora promedio", f"{prom:.0f} d" if prom is not None else "—",
                            COLOR_MORA_PROMEDIO), unsafe_allow_html=True)
    sobre = resumen["sobrecosto"]
    c4.markdown(tarjeta_kpi("Sobrecosto acumulado",
                            f"US$ {sobre['USD']:,.0f} · RD$ {sobre['DOP']:,.0f}",
                            COLOR_SOBRECOSTO), unsafe_allow_html=True)


def _tabla_pagos(df: pd.DataFrame) -> pd.DataFrame:
    filas = []
    for _, r in df.iterrows():
        sm = r.get("SinMoraTotales") or {}
        pr = r.get("PagoRealTotales") or {}
        extra = r.get("MontoExtra") or {}
        desc = r.get("Descripcion_T", "")
        if r.get("BLSinTransito"):
            desc = "⚠ BL sin datos de tránsito"
        filas.append({
            "BL": r.get(COL_BL, ""),
            "Descripción": desc,
            "Estado": str(r.get(COL_ESTADO_PAGO, "")).strip() or ESTADO_PAGO_PENDIENTE,
            "SIN MORA (fecha)": r.get(COL_FECHA_SIN_MORA, ""),
            "SIN MORA US$": sm.get("USD"),
            "SIN MORA RD$": sm.get("DOP"),
            "Pago real (fecha)": r.get(COL_FECHA_PAGO_REAL, ""),
            "Pago real US$": pr.get("USD"),
            "Pago real RD$": pr.get("DOP"),
            "Extra US$": extra.get("USD"),
            "Extra RD$": extra.get("DOP"),
            "Días de mora": r.get("DiasMora"),
        })
    return pd.DataFrame(filas)


def mostrar_dashboard_pagos(enriquecido: pd.DataFrame):
    resumen = resumen_pagos(enriquecido)
    _tarjetas_resumen(resumen)
    st.caption("El sobrecosto no se estima con una tarifa: es la diferencia entre lo que Logística "
               "fijó como pago saludable (SIN MORA) y lo que realmente se terminó pagando.")

    if enriquecido.empty:
        st.info("Todavía no hay expedientes con conceptos de pago registrados.")
        return

    sin_transito = int(enriquecido["BLSinTransito"].sum())
    if sin_transito:
        st.warning(f"{sin_transito} expediente(s) de Pagos no tienen un BL coincidente en tránsito "
                   "(activo ni histórico). Se muestran igual, pero verifica que el BL esté bien escrito.")

    st.dataframe(_tabla_pagos(enriquecido), width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# FORMULARIOS (solo admin)
# ---------------------------------------------------------------------------
def form_registrar_conceptos(enriquecido: pd.DataFrame, activos: pd.DataFrame, historico: pd.DataFrame):
    st.markdown("**Registrar / editar conceptos de un expediente**")

    categorias_disp = _opciones_categoria(activos, historico)
    if not categorias_disp:
        st.info("Todavía no hay embarques en tránsito ni en el histórico para elegir.")
        return

    categoria = st.selectbox("Categoría", categorias_disp, key="pago_categoria_sel")
    embarques = _embarques_de_categoria(categoria, activos, historico)
    if not embarques:
        st.info(f"No hay embarques con BL en '{categoria}'.")
        return

    etiquetas = [f"{e['bl']} · {e['desc'][:40] or 'sin descripción'} · {e['cant']} · Llegada: {e['llegada_txt']}"
                for e in embarques]
    idx = st.selectbox("Expediente", range(len(embarques)), format_func=lambda i: etiquetas[i],
                       key="pago_expediente_sel")
    elegido = embarques[idx]
    bl = elegido["bl"]

    fila_existente = None
    if not enriquecido.empty:
        coincidencias = enriquecido[enriquecido[COL_BL].astype(str).str.strip() == bl]
        if not coincidencias.empty:
            fila_existente = coincidencias.iloc[0]
    if fila_existente is None:
        st.caption("Expediente nuevo en Pagos — todavía sin conceptos registrados.")
    else:
        st.caption("Este expediente ya tiene conceptos registrados; los valores de abajo son los actuales.")

    valores_previos = {c: fila_existente.get(c, "") for c in CONCEPTOS_PAGO} if fila_existente is not None else {}
    seleccionados_previos = [c for c in CONCEPTOS_PAGO if str(valores_previos.get(c, "")).strip()]

    conceptos_aplican = st.multiselect(
        "Conceptos que aplican a este expediente (deja fuera los que no apliquen — vacío no es cero)",
        CONCEPTOS_PAGO, default=seleccionados_previos, key="pago_conceptos_sel",
    )

    montos = {}
    if conceptos_aplican:
        cols = st.columns(2)
        for i, concepto in enumerate(conceptos_aplican):
            moneda = MONEDA_CONCEPTO[concepto]
            valor_previo = a_numero(valores_previos.get(concepto, "")) or 0.0
            with cols[i % 2]:
                montos[concepto] = st.number_input(
                    f"{concepto} ({moneda})", min_value=0.0, value=float(valor_previo),
                    step=100.0, key=f"pago_monto_{concepto}",
                )

    if st.button("Guardar conceptos", type="primary", key="btn_guardar_conceptos"):
        if not bl:
            st.error("Falta el BL.")
            return
        # Los conceptos NO seleccionados se guardan vacíos a propósito: "no
        # aplica" no es lo mismo que "cero", y así solo se suma lo que de
        # verdad tiene el expediente.
        datos = {c: (montos[c] if c in conceptos_aplican else "") for c in CONCEPTOS_PAGO}
        ok, mensaje = guardar_pago(bl, datos)
        if ok:
            registrar_log("Conceptos de pago guardados", bl, "", ", ".join(conceptos_aplican) or "(ninguno)")
            invalidar_caches()
            st.success(f"Conceptos guardados para el BL {bl}.")
            st.rerun()
        else:
            st.error(mensaje)


def form_ventana_pago(enriquecido: pd.DataFrame, tipo: str):
    """tipo = 'sin_mora' o 'pago_real'. Comparte la misma mecánica: elegir el
    expediente, fijar una fecha, congelar el total de lo que esté lleno en los
    conceptos EN ESE MOMENTO. Corregible después con la casilla de abajo."""
    es_sin_mora = tipo == "sin_mora"
    st.markdown(f"**{'Registrar ventana SIN MORA' if es_sin_mora else 'Registrar Pago Realizado'}**")

    if enriquecido.empty:
        st.info("Todavía no hay expedientes con conceptos registrados.")
        return

    opciones = sorted(enriquecido[COL_BL].astype(str).str.strip().unique())
    bl = st.selectbox("Expediente (BL)", opciones, key=f"sel_bl_{tipo}")
    fila = enriquecido[enriquecido[COL_BL].astype(str).str.strip() == bl].iloc[0]

    totales = totales_conceptos(fila)
    if totales is None:
        st.warning("Este expediente no tiene ningún concepto con monto todavía.")
        return
    st.caption(f"Suma de los conceptos llenos ahora mismo: {_fmt(totales['USD'], 'USD')} · "
              f"{_fmt(totales['DOP'], 'DOP')}")

    col_fecha = COL_FECHA_SIN_MORA if es_sin_mora else COL_FECHA_PAGO_REAL
    ya_registrado = str(fila.get(col_fecha, "")).strip()
    corregir = False
    if ya_registrado:
        st.info(f"Ya registrado: {ya_registrado}.")
        corregir = st.checkbox("Corregir fecha y/o monto", key=f"corregir_{tipo}")
        if not corregir:
            return

    if not es_sin_mora and not str(fila.get(COL_FECHA_SIN_MORA, "")).strip():
        st.warning("Este expediente todavía no tiene la ventana SIN MORA registrada. "
                  "Es la referencia contra la que se mide el pago; regístrala primero.")
        return

    etiqueta_fecha = "Fecha límite saludable de pago" if es_sin_mora else "Fecha en que se pagó"
    fecha = st.date_input(etiqueta_fecha, value=hoy_rd(), format="DD/MM/YYYY", key=f"fecha_{tipo}")

    if st.button("Confirmar", type="primary", key=f"btn_{tipo}"):
        fn = registrar_sin_mora if es_sin_mora else registrar_pago_realizado
        ok, mensaje = fn(bl, fecha, totales, sobrescribir=corregir)
        if ok:
            titulo = "Ventana SIN MORA" if es_sin_mora else "Pago Realizado"
            registrar_log(f"{titulo} registrado", bl, "",
                         f"{fecha.isoformat()} · {_fmt(totales['USD'], 'USD')} · {_fmt(totales['DOP'], 'DOP')}")
            invalidar_caches()
            st.success("Guardado.")
            st.rerun()
        else:
            st.error(mensaje)


def form_estado_pago(enriquecido: pd.DataFrame):
    st.markdown("**Marcar estado (Pendiente / Pagado)**")
    if enriquecido.empty:
        st.info("No hay expedientes registrados.")
        return
    opciones = sorted(enriquecido[COL_BL].astype(str).str.strip().unique())
    bl = st.selectbox("Expediente (BL)", opciones, key="sel_bl_estado")
    fila = enriquecido[enriquecido[COL_BL].astype(str).str.strip() == bl].iloc[0]
    actual = str(fila.get(COL_ESTADO_PAGO, "")).strip() or ESTADO_PAGO_PENDIENTE
    estado = st.radio("Estado", [ESTADO_PAGO_PENDIENTE, ESTADO_PAGO_PAGADO],
                      index=0 if actual == ESTADO_PAGO_PENDIENTE else 1, key="radio_estado_pago")
    if st.button("Guardar estado", type="primary", key="btn_estado_pago"):
        ok, mensaje = marcar_estado_pago(bl, estado)
        if ok:
            registrar_log("Estado de pago actualizado", bl, "", estado)
            invalidar_caches()
            st.rerun()
        else:
            st.error(mensaje)


# ---------------------------------------------------------------------------
# PANEL PRINCIPAL — lo único que app.py necesita llamar
# ---------------------------------------------------------------------------
def panel_pagos(datos: dict, es_admin: bool):
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.subheader("Estatus de Pago")

    activos = datos.get("activos", pd.DataFrame())
    historico = datos.get("historico", pd.DataFrame())
    df_pagos = datos.get("pagos", pd.DataFrame())
    enriquecido = enriquecer_pagos(df_pagos, activos, historico)

    mostrar_dashboard_pagos(enriquecido)

    if not es_admin:
        return

    st.divider()
    with st.expander("Registrar / editar conceptos de un expediente"):
        form_registrar_conceptos(enriquecido, activos, historico)
    with st.expander("Registrar ventana SIN MORA"):
        form_ventana_pago(enriquecido, "sin_mora")
    with st.expander("Registrar Pago Realizado"):
        form_ventana_pago(enriquecido, "pago_real")
    with st.expander("Marcar estado (Pendiente/Pagado)"):
        form_estado_pago(enriquecido)
