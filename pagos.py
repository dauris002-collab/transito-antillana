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
    CATEGORIAS, COL_BL, COL_CANT, COL_DESC, COL_EMPRESA, COL_ESTADO_PAGO,
    COL_ETA, COL_FECHA_LLEGADA_PUERTO, COL_FECHA_PAGO_REAL, COL_FECHA_SIN_MORA,
    COL_PAGO_LLEGADA, CONCEPTOS_PAGO, EMPRESA_ANTILLANA, EMPRESAS_PAGO,
    ESTADO_PAGO_PAGADO, ESTADO_PAGO_PENDIENTE, MONEDA_CONCEPTO, _norm,
    a_numero, aplicar_selectores_pagos, fecha_llegada_fila, formato_eta,
    guardar_pago, hoy_rd, invalidar_caches, marcar_estado_pago, parsear_fecha,
    registrar_log, registrar_pago_realizado, registrar_sin_mora,
    sincronizar_pagos_con_transito,
)
from logica import PALETA_PAISES, enriquecer_pagos, esc, resumen_pagos, totales_conceptos
from ui_componentes import COLOR_RECIBIDAS_MES, COLOR_TOTAL, CUSTOM_CSS, tarjeta_kpi


COLOR_SOBRECOSTO = "#991B1B"


COLOR_MORA_PROMEDIO = "#B45309"


# Un color fijo por concepto — reusa la misma paleta "amigable" que ya usan
# los gráficos de país en tránsito, así no se inventa una gama nueva.
COLOR_CONCEPTO = dict(zip(CONCEPTOS_PAGO, PALETA_PAISES))


PAGOS_CSS = """
<style>
.pago-tarjeta { border:1px solid #E5E7EB; border-radius:12px; padding:16px 20px; margin-bottom:14px;
                background:#fff; box-shadow:0 1px 4px rgba(17,24,39,0.06); }
.pago-cabeza { display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }
.pago-bl { font-weight:700; font-size:1.02rem; color:#111827; }
.pago-empresa { display:inline-block; background:#EEF2FF; color:#3730A3; font-size:0.72rem;
                font-weight:700; padding:2px 10px; border-radius:999px; margin-left:8px; }
.pago-estado { display:inline-block; font-size:0.75rem; font-weight:700; color:#fff;
               padding:3px 12px; border-radius:999px; white-space:nowrap; }
.pago-meta { color:#6B7280; font-size:0.85rem; margin-top:2px; }
.pago-conceptos { display:flex; flex-wrap:wrap; justify-content:center; gap:8px; margin:14px 0; }
.pago-chip { padding:6px 15px; border-radius:999px; color:#fff; font-size:0.83rem; font-weight:700;
             white-space:nowrap; }
.pago-totales { display:flex; flex-wrap:wrap; justify-content:center; align-items:baseline; gap:28px;
                margin-top:6px; }
.pago-total-etq { font-size:0.68rem; text-transform:uppercase; letter-spacing:0.04em; color:#6B7280;
                  display:block; text-align:center; }
.pago-total-val { font-size:1.2rem; font-weight:800; color:#111827; display:block; text-align:center; }
.pago-cerrado { text-align:center; color:#9CA3AF; font-size:0.78rem; margin-top:10px; }
</style>
"""


def _fmt(monto, moneda: str) -> str:
    simbolo = "US$" if moneda == "USD" else "RD$"
    return f"{simbolo} {monto:,.2f}"


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
    — nada de esto se vuelve a teclear. 'llegada_iso' viaja aparte (AAAA-MM-DD)
    para poder guardarla si hay que crear la fila de Pagos desde aquí, antes de
    que la sincronización automática la alcance. Ordenado por BL para que la
    lista sea estable entre refrescos."""
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
                "llegada_iso": llegada.isoformat() if llegada else "",
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
                llegada = parsear_fecha(r.get(COL_ETA, ""))
                llegada_txt = f"{formato_eta(llegada)} (ETA, sin confirmar)" if llegada else "sin ETA"
            filas.append({
                "bl": str(r.get(COL_BL, "")).strip(),
                "desc": str(r.get(COL_DESC, "")),
                "cant": str(r.get(COL_CANT, "")),
                "llegada_txt": llegada_txt,
                "llegada_iso": llegada.isoformat() if llegada else "",
            })
    filas = [f for f in filas if f["bl"]]
    filas.sort(key=lambda f: f["bl"])
    return filas


def _hay_bls_sin_sincronizar(activos: pd.DataFrame, historico: pd.DataFrame, pagos_actual: pd.DataFrame) -> bool:
    """Comparación en memoria contra lo que ya cargó cargar_todo() — cero
    llamadas extra a la API. Solo cuando esto da True vale la pena pagar el
    costo de sincronizar_pagos_con_transito() (que sí lee y escribe de verdad)."""
    existentes = ({_norm(b) for b in pagos_actual[COL_BL] if str(b).strip()}
                 if pagos_actual is not None and not pagos_actual.empty else set())
    for fuente in (activos, historico):
        if fuente is None or fuente.empty:
            continue
        for b in fuente[COL_BL]:
            b = str(b).strip()
            if b and _norm(b) not in existentes:
                return True
    return False


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


def _html_expediente(r) -> str:
    bl = esc(r.get(COL_BL, "")) or "(sin BL)"
    desc = esc(r.get(COL_DESC, ""))
    cant = esc(r.get(COL_CANT, ""))
    llegada = esc(r.get(COL_PAGO_LLEGADA, "")) or "—"
    empresa = esc(r.get("EmpresaEfectiva", "")) or EMPRESA_ANTILLANA
    estado = str(r.get(COL_ESTADO_PAGO, "")).strip() or ESTADO_PAGO_PENDIENTE
    color_estado = "#B45309" if estado == ESTADO_PAGO_PENDIENTE else "#2E7D32"

    # Un concepto vacío NO sale — solo los que de verdad tiene el expediente.
    chips = []
    for concepto in CONCEPTOS_PAGO:
        valor = a_numero(r.get(concepto, ""))
        if valor is None:
            continue
        color = COLOR_CONCEPTO[concepto]
        chips.append(
            f'<span class="pago-chip" style="background:{color};">'
            f'{esc(concepto)}: {_fmt(valor, MONEDA_CONCEPTO[concepto])}</span>'
        )

    total = r.get("TotalActual") or {}
    dias_sin_pagar = r.get("DiasSinPagar")
    dias_sin_pagar_txt = "—" if dias_sin_pagar is None or pd.isna(dias_sin_pagar) else str(int(dias_sin_pagar))
    fecha_saludable = esc(r.get(COL_FECHA_SIN_MORA, "")) or "sin fijar"

    pie_cerrado = ""
    fecha_pago = str(r.get(COL_FECHA_PAGO_REAL, "")).strip()
    if fecha_pago:
        extra = r.get("MontoExtra") or {}
        partes = [_fmt(v, m) for m, v in extra.items() if v is not None and abs(v) > 0.005]
        extra_txt = " · Extra sobre lo saludable: " + " y ".join(partes) if partes else ""
        pie_cerrado = f'<div class="pago-cerrado">Pagado el {esc(fecha_pago)}{extra_txt}</div>'

    return (
        '<div class="pago-tarjeta">'
        f'<div class="pago-cabeza"><span class="pago-bl">{bl}<span class="pago-empresa">{empresa}</span></span>'
        f'<span class="pago-estado" style="background:{color_estado};">{esc(estado)}</span></div>'
        f'<div class="pago-meta">{desc} · {cant} · Llegada: {llegada}</div>'
        f'<div class="pago-conceptos">{"".join(chips)}</div>'
        '<div class="pago-totales">'
        f'<div><span class="pago-total-etq">Total a pagar US$</span>'
        f'<span class="pago-total-val">{_fmt(total.get("USD") or 0.0, "USD")}</span></div>'
        f'<div><span class="pago-total-etq">Total a pagar RD$</span>'
        f'<span class="pago-total-val">{_fmt(total.get("DOP") or 0.0, "DOP")}</span></div>'
        f'<div><span class="pago-total-etq">Fecha saludable</span>'
        f'<span class="pago-total-val" style="font-size:0.95rem;">{fecha_saludable}</span></div>'
        f'<div><span class="pago-total-etq">Días sin pagar</span>'
        f'<span class="pago-total-val">{dias_sin_pagar_txt}</span></div>'
        '</div>'
        f'{pie_cerrado}'
        '</div>'
    )


def mostrar_dashboard_pagos(enriquecido: pd.DataFrame):
    empresa_sel = st.selectbox("Empresa", ["Todas"] + EMPRESAS_PAGO, key="pago_filtro_empresa")
    vista = enriquecido if empresa_sel == "Todas" or enriquecido.empty \
        else enriquecido[enriquecido["EmpresaEfectiva"] == empresa_sel]

    resumen = resumen_pagos(vista)
    _tarjetas_resumen(resumen)
    st.caption("\"Total a pagar\" es lo que hay cargado en los conceptos AHORA MISMO. Lo que aparece "
              "\"Pagado el...\" al pie de la tarjeta usa la ventana SIN MORA que se haya congelado "
              "desde la app para calcular el extra sobre lo saludable.")

    if vista.empty:
        st.info("No hay expedientes de Pagos para esta selección.")
        return

    con_montos = vista[vista["TieneMontos"]]
    sin_montos = len(vista) - len(con_montos)

    sin_transito = int(vista["BLSinTransito"].sum())
    if sin_transito:
        st.warning(f"{sin_transito} expediente(s) de Pagos ya no tienen un BL coincidente en tránsito "
                   "(activo ni histórico) — puede que se hayan eliminado o cambiado de BL ahí.")

    if con_montos.empty:
        st.info(f"Hay {len(vista)} expediente(s) en esta selección, pero ninguno tiene montos "
                "cargados todavía. Se muestran aquí solo cuando tengan al menos un concepto lleno.")
        return

    if sin_montos:
        st.caption(f"Mostrando {len(con_montos)} expediente(s) con montos cargados · "
                  f"{sin_montos} más ya están en la hoja esperando que se les llenen los conceptos.")

    st.markdown(PAGOS_CSS, unsafe_allow_html=True)
    st.markdown("".join(_html_expediente(r) for _, r in con_montos.iterrows()), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# FORMULARIOS (solo admin)
# ---------------------------------------------------------------------------
def form_registrar_conceptos(enriquecido: pd.DataFrame, activos: pd.DataFrame, historico: pd.DataFrame):
    st.markdown("**Registrar / editar conceptos de un expediente**")
    st.caption("Esta lista sale de tránsito, que solo trackea Antillana Comercial. Para Tecnicaribe o "
              "Motor Ibérico, agrega la fila directo en la pestaña Pagos del Sheet (BL, Empresa, "
              "Descripción, Cantidad y Llegada a mano) y luego edítala aquí si quieres usar el resto "
              "de estos formularios sobre ella.")

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
        referencia = {COL_DESC: elegido["desc"], COL_CANT: elegido["cant"],
                     COL_PAGO_LLEGADA: elegido["llegada_iso"], COL_EMPRESA: EMPRESA_ANTILLANA}
        ok, mensaje = guardar_pago(bl, datos, referencia=referencia)
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

    # Sincronización automática: solo admin (los viewers nunca deben disparar
    # escrituras), y solo cuando la comparación en memoria encuentra un BL de
    # tránsito que Pagos todavía no tiene — así la mayoría de las veces esto no
    # cuesta ninguna llamada extra a la API.
    if es_admin and _hay_bls_sin_sincronizar(activos, historico, df_pagos):
        ok, mensaje = sincronizar_pagos_con_transito(activos, historico)
        if ok:
            invalidar_caches()
            st.rerun()
        else:
            st.warning(f"No se pudo sincronizar Pagos con tránsito automáticamente: {mensaje}")

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
    with st.expander("Activar selectores en Sheets (fechas y Empresa)"):
        st.caption("Solo hace falta correrlo una vez. Agrega el ícono de calendario nativo de Google "
                  "Sheets en las fechas, y una lista desplegable en Empresa, para elegir con clic en "
                  "vez de teclear.")
        if st.button("Activar selectores", key="btn_selector_fecha"):
            ok, mensaje = aplicar_selectores_pagos()
            (st.success if ok else st.error)(mensaje)
