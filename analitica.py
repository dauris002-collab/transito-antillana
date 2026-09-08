"""
analitica.py — Analítica de importaciones para Antillana Comercial.

Panel de gráficas de negocio (no operativo): países de origen, qué se importa,
tendencia mensual, costo de aduanas y tiempo en puerto. No es una fuente de
datos aparte -- se arma sobre lo mismo que ya trae cargar_todo() (activos +
histórico + pagos), así que cambia solo con lo que el equipo ya está cargando
en tránsito y en Pagos. Sin escrituras: todo lo de aquí es de solo lectura.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from sheets_io import (
    CACHE_TTL, COL_BL, COL_DESC, COL_FECHA_DECLARACION,
    COL_FECHA_LLEGADA_PUERTO, COL_PAGO_LLEGADA, COL_PAIS, COL_VIA,
    MESES_ES_CORTO, NO_ESPECIFICADO, VIA_AEREA,
    a_numero, parsear_fecha, unificar_paises,
)
from logica import PALETA_PAISES, es_aereo
from ui_componentes import COLOR_ALERTA, COLOR_RECIBIDAS_MES, COLOR_TOTAL, esc, tarjeta_kpi


COLOR_ADUANAS = "#6D28D9"       # violeta, para diferenciarlo de los KPI ya usados en Pagos
COLOR_CATEGORIA = "#0C4A6E"     # azul oscuro, para el bloque "qué se importa"
COLOR_AEREO = "#2a78d6"
COLOR_MARITIMO = "#1baf7a"


_LAYOUT_BASE = dict(
    margin=dict(t=28, b=10, l=10, r=10),
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#374151", size=11), showlegend=False, dragmode=False,
)


def _config_estatica(key: str) -> dict:
    return {"displayModeBar": False, "staticPlot": True, "responsive": True}


# ---------------------------------------------------------------------------
# UNIVERSO DE DATOS
# ---------------------------------------------------------------------------
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _universo(activos: pd.DataFrame, historico: pd.DataFrame) -> pd.DataFrame:
    """Activos + histórico combinados en un solo DataFrame, con las mismas
    columnas (BL, Categoria, Pais, Descripcion) para país/categoría -- así
    'qué se importa' incluye tanto lo ya recibido como lo que viene en camino.
    El país se unifica aquí porque cargar_todo() solo lo unifica para activos;
    el histórico llega tal cual está escrito en el Sheet."""
    piezas = []
    if activos is not None and not activos.empty:
        piezas.append(pd.DataFrame({
            "BL": activos[COL_BL], "Categoria": activos.get("Categoria", ""),
            "Pais": unificar_paises(activos[COL_PAIS]), "Descripcion": activos[COL_DESC],
        }))
    if historico is not None and not historico.empty:
        piezas.append(pd.DataFrame({
            "BL": historico[COL_BL], "Categoria": historico.get("Categoria_Origen", ""),
            "Pais": unificar_paises(historico[COL_PAIS]), "Descripcion": historico[COL_DESC],
        }))
    if not piezas:
        return pd.DataFrame(columns=["BL", "Categoria", "Pais", "Descripcion"])
    return pd.concat(piezas, ignore_index=True)


# ---------------------------------------------------------------------------
# PAÍSES DE ORIGEN
# ---------------------------------------------------------------------------
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_paises(universo: pd.DataFrame) -> go.Figure | None:
    if universo.empty:
        return None
    serie = universo["Pais"].replace("", NO_ESPECIFICADO).value_counts().head(10).sort_values()
    if serie.empty:
        return None
    colores = [PALETA_PAISES[i % len(PALETA_PAISES)] for i in range(len(serie))]
    fig = go.Figure(data=[go.Bar(
        x=serie.values, y=serie.index, orientation="h", marker=dict(color=colores),
        text=serie.values, textposition="outside",
        hovertemplate="%{y}: %{x} embarque(s)<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=max(220, 34 * len(serie)),
                      title=dict(text="Embarques por país de origen", font=dict(size=13)),
                      xaxis=dict(showgrid=True, gridcolor="#F3F4F6", title=""),
                      yaxis=dict(showgrid=False, title=""))
    return fig


# ---------------------------------------------------------------------------
# QUÉ SE IMPORTA
# ---------------------------------------------------------------------------
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_categorias(universo: pd.DataFrame) -> go.Figure | None:
    """Por categoría, no por Descripción: la Descripción es texto libre
    ('Montacarga', 'MONTACARGAS', 'Repuesto para camiones'...) y agruparla tal
    cual produciría barras duplicadas para la misma cosa. La categoría es el
    dato limpio que ya existe para responder 'qué se está importando'."""
    if universo.empty:
        return None
    serie = universo["Categoria"].replace("", NO_ESPECIFICADO).value_counts()
    if serie.empty:
        return None
    fig = go.Figure(data=[go.Pie(
        labels=serie.index, values=serie.values, hole=0.55,
        marker=dict(colors=[PALETA_PAISES[i % len(PALETA_PAISES)] for i in range(len(serie))]),
        textinfo="label+percent", hovertemplate="%{label}: %{value} embarque(s)<extra></extra>",
    )])
    fig.update_layout(**{**_LAYOUT_BASE, "showlegend": False}, height=320,
                      title=dict(text="Embarques por categoría", font=dict(size=13)))
    return fig


def _top_descripciones(universo: pd.DataFrame, n: int = 8) -> list:
    """Complemento de texto libre: los productos más repetidos tal como se
    escribieron, para cuando la categoría es muy amplia y hace falta ver el
    detalle real (ej. dentro de 'Carga Suelta')."""
    if universo.empty:
        return []
    serie = universo["Descripcion"].astype(str).str.strip()
    serie = serie[serie != ""].str.upper().value_counts().head(n)
    return list(serie.items())


# ---------------------------------------------------------------------------
# TENDENCIA MENSUAL (solo histórico: son ciclos ya cerrados, no estimados)
# ---------------------------------------------------------------------------
def _mes_de(valor) -> date | None:
    f = parsear_fecha(valor)
    return date(f.year, f.month, 1) if f else None


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_tendencia_mensual(historico: pd.DataFrame, meses: int = 12) -> go.Figure | None:
    if historico is None or historico.empty or "Fecha_Recibido" not in historico.columns:
        return None
    meses_fila = [_mes_de(v) for v in historico["Fecha_Recibido"]]
    conteo = pd.Series([m for m in meses_fila if m]).value_counts().sort_index()
    if conteo.empty:
        return None
    conteo = conteo.tail(meses)
    etiquetas = [f"{MESES_ES_CORTO[m.month]} {m.year}" for m in conteo.index]
    fig = go.Figure(data=[go.Scatter(
        x=etiquetas, y=conteo.values, mode="lines+markers+text",
        line=dict(color=COLOR_RECIBIDAS_MES, width=3), marker=dict(size=7),
        text=conteo.values, textposition="top center",
        hovertemplate="%{x}: %{y} embarque(s) recibido(s)<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=260,
                      title=dict(text="Embarques recibidos por mes", font=dict(size=13)),
                      xaxis=dict(showgrid=False, title=""),
                      yaxis=dict(showgrid=True, gridcolor="#F3F4F6", title="", rangemode="tozero"))
    return fig


# ---------------------------------------------------------------------------
# PAGOS EN ADUANAS (de la pestaña Pagos, no del histórico de tránsito)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _serie_aduanas(pagos: pd.DataFrame) -> pd.Series:
    """Un valor de ADUANAS (DOP) por fila con dato real, indexado por mes de
    Llegada. Vacío no es cero -- un expediente sin ADUANAS todavía no infla ni
    desinfla el promedio."""
    if pagos is None or pagos.empty or "ADUANAS" not in pagos.columns:
        return pd.Series(dtype=float)
    filas = []
    for _, r in pagos.iterrows():
        monto = a_numero(r.get("ADUANAS", ""))
        if monto is None:
            continue
        mes = _mes_de(r.get(COL_PAGO_LLEGADA, ""))
        if mes:
            filas.append((mes, monto))
    if not filas:
        return pd.Series(dtype=float)
    df = pd.DataFrame(filas, columns=["mes", "monto"])
    return df.groupby("mes")["monto"].mean()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_aduanas_mensual(pagos: pd.DataFrame, meses: int = 12) -> go.Figure | None:
    serie = _serie_aduanas(pagos)
    if serie.empty:
        return None
    serie = serie.sort_index().tail(meses)
    etiquetas = [f"{MESES_ES_CORTO[m.month]} {m.year}" for m in serie.index]
    fig = go.Figure(data=[go.Bar(
        x=etiquetas, y=serie.values, marker=dict(color=COLOR_ADUANAS),
        text=[f"RD$ {v:,.0f}" for v in serie.values], textposition="outside",
        hovertemplate="%{x}: RD$ %{y:,.0f} promedio<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=260,
                      title=dict(text="Pago promedio de Aduanas por mes (RD$)", font=dict(size=13)),
                      xaxis=dict(showgrid=False, title=""),
                      yaxis=dict(showgrid=True, gridcolor="#F3F4F6", title="", rangemode="tozero"))
    return fig


# ---------------------------------------------------------------------------
# TIEMPO EN PUERTO (ciclos cerrados: llegada confirmada -> declaración)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _dias_en_puerto_historico(historico: pd.DataFrame) -> pd.DataFrame:
    """Un renglón por embarque cerrado con sus días en puerto, categoría y vía.
    Solo histórico: un embarque activo todavía no completó el ciclo, así que
    mezclarlo con los cerrados sesgaría el promedio hacia abajo."""
    columnas = ["dias", "Categoria", "Via"]
    if (historico is None or historico.empty or COL_FECHA_LLEGADA_PUERTO not in historico.columns
            or COL_FECHA_DECLARACION not in historico.columns):
        return pd.DataFrame(columns=columnas)
    filas = []
    for _, r in historico.iterrows():
        llegada = parsear_fecha(r.get(COL_FECHA_LLEGADA_PUERTO, ""))
        declaracion = parsear_fecha(r.get(COL_FECHA_DECLARACION, ""))
        if not llegada or not declaracion or declaracion < llegada:
            continue
        filas.append({
            "dias": (declaracion - llegada).days,
            "Categoria": r.get("Categoria_Origen", "") or NO_ESPECIFICADO,
            "Via": VIA_AEREA if es_aereo(r.get(COL_VIA, "")) else "Marítimo",
        })
    return pd.DataFrame(filas, columns=columnas)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_tiempo_puerto_categoria(dias_df: pd.DataFrame) -> go.Figure | None:
    if dias_df.empty:
        return None
    prom = dias_df.groupby("Categoria")["dias"].mean().sort_values()
    if prom.empty:
        return None
    colores = [PALETA_PAISES[i % len(PALETA_PAISES)] for i in range(len(prom))]
    fig = go.Figure(data=[go.Bar(
        x=prom.values, y=prom.index, orientation="h", marker=dict(color=colores),
        text=[f"{v:.1f} d" for v in prom.values], textposition="outside",
        hovertemplate="%{y}: %{x:.1f} días promedio<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=max(220, 34 * len(prom)),
                      title=dict(text="Días promedio en puerto por categoría", font=dict(size=13)),
                      xaxis=dict(showgrid=True, gridcolor="#F3F4F6", title=""),
                      yaxis=dict(showgrid=False, title=""))
    return fig


def _tarjeta_via(dias_df: pd.DataFrame) -> str:
    """Comparación Aéreo vs Marítimo -- posible ahora que la Vía es un dato
    por embarque y no depende de la categoría."""
    if dias_df.empty or dias_df["Via"].nunique() < 2:
        return ""
    prom = dias_df.groupby("Via")["dias"].mean()
    aereo = prom.get(VIA_AEREA)
    maritimo = prom.get("Marítimo")
    piezas = ['<div class="paises"><div class="atttl">Días en puerto: Aéreo vs Marítimo</div>']
    for etiqueta, valor, color in ((VIA_AEREA, aereo, COLOR_AEREO), ("Marítimo", maritimo, COLOR_MARITIMO)):
        if valor is None:
            continue
        tope = max(v for v in (aereo, maritimo) if v is not None) or 1
        ancho = max(4.0, (valor / tope) * 100.0)
        piezas.append(
            f'<div class="pfila"><div class="pnom">{esc(etiqueta)}</div>'
            f'<div class="pbarra"><span style="width:{ancho:.1f}%;background:{color}"></span></div>'
            f'<div class="pval">{valor:.1f} d</div></div>'
        )
    piezas.append("</div>")
    return "".join(piezas)


# ---------------------------------------------------------------------------
# PANEL
# ---------------------------------------------------------------------------
def panel_analitica(datos: dict):
    st.subheader("📊 Analítica de importaciones")
    st.caption("Se arma sola con lo que ya está en tránsito, en el histórico y en Pagos — "
              "no hay que capturar nada aparte.")

    activos, historico, pagos = datos.get("activos"), datos.get("historico"), datos.get("pagos")
    universo = _universo(activos, historico)
    dias_df = _dias_en_puerto_historico(historico)
    serie_aduanas = _serie_aduanas(pagos)

    if universo.empty and (historico is None or historico.empty):
        st.info("Todavía no hay suficientes embarques (activos o recibidos) para armar analítica.")
        return

    pais_top = universo["Pais"].replace("", NO_ESPECIFICADO).mode()
    cat_top = universo["Categoria"].replace("", NO_ESPECIFICADO).mode()
    dias_prom = dias_df["dias"].mean() if not dias_df.empty else None
    aduanas_prom = serie_aduanas.mean() if not serie_aduanas.empty else None
    n_historico = 0 if historico is None else len(historico)

    cols = st.columns(5)
    tarjetas = [
        ("Embarques recibidos", str(n_historico), COLOR_RECIBIDAS_MES),
        ("País principal", pais_top.iloc[0] if len(pais_top) else "—", COLOR_TOTAL),
        ("Categoría principal", cat_top.iloc[0] if len(cat_top) else "—", COLOR_CATEGORIA),
        ("Días en puerto (prom.)", f"{dias_prom:.1f} d" if dias_prom is not None else "—", COLOR_ALERTA),
        ("Aduanas (prom.)", f"RD$ {aduanas_prom:,.0f}" if aduanas_prom is not None else "—", COLOR_ADUANAS),
    ]
    for col, (label, valor, color) in zip(cols, tarjetas):
        with col:
            st.markdown(tarjeta_kpi(label, valor, color), unsafe_allow_html=True)

    st.write("")
    c1, c2 = st.columns(2)
    with c1:
        fig = _figura_paises(universo)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_estatica("paises"), key="an_paises")
        else:
            st.caption("Sin datos de país todavía.")
    with c2:
        fig = _figura_categorias(universo)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_estatica("cat"), key="an_categorias")
        else:
            st.caption("Sin datos de categoría todavía.")

    top_desc = _top_descripciones(universo)
    if top_desc:
        with st.expander("Ver detalle: productos más repetidos por descripción"):
            for desc, n in top_desc:
                st.markdown(f"**{n}** — {esc(desc)}", unsafe_allow_html=True)

    st.write("")
    c3, c4 = st.columns(2)
    with c3:
        fig = _figura_tendencia_mensual(historico)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_estatica("tendencia"), key="an_tendencia")
        else:
            st.caption("Todavía no hay suficiente histórico mes a mes.")
    with c4:
        fig = _figura_aduanas_mensual(pagos)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_estatica("aduanas"), key="an_aduanas")
        else:
            st.caption("Todavía no hay pagos de Aduanas registrados en Pagos.")

    st.write("")
    c5, c6 = st.columns(2)
    with c5:
        fig = _figura_tiempo_puerto_categoria(dias_df)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_estatica("tiempo"), key="an_tiempo")
        else:
            st.caption("Todavía no hay embarques archivados con llegada y declaración para medir tiempo en puerto.")
    with c6:
        html_via = _tarjeta_via(dias_df)
        if html_via:
            st.markdown(html_via, unsafe_allow_html=True)
        else:
            st.caption("Falta variedad de Vía (Aéreo/Marítimo) en el histórico para comparar.")

    st.caption(f"Última actualización de los datos: {datos['hora'].strftime('%H:%M:%S')}")
