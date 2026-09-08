"""
analitica.py — Analítica de importaciones para Antillana Comercial.

Panel de gráficas de negocio (no operativo): países de origen, qué se importa,
top de productos, mercancía en puerto/aeropuerto ahora mismo, tendencia
mensual y tiempo en puerto -- con filtros de Año / País / Categoría. No es una
fuente de datos aparte: se arma sobre lo mismo que ya trae cargar_todo()
(activos + histórico), así que cambia solo con lo que el equipo ya está
cargando en tránsito. Sin escrituras: todo aquí es de solo lectura.

Pagos (aduanas, costos) se dejó AFUERA a propósito: todavía hay muy poca data
registrada en esa pestaña como para que un promedio signifique algo. Se agrega
cuando haya volumen real.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from sheets_io import (
    CACHE_TTL, COL_BL, COL_DESC, COL_ETA, COL_FECHA_ALMACEN, COL_FECHA_DECLARACION,
    COL_FECHA_LLEGADA_PUERTO, COL_FECHA_SALIDA, COL_PAIS, COL_VIA,
    MESES_ES_CORTO, NO_ESPECIFICADO, VIA_AEREA,
    costos_puerto, parsear_fecha, unificar_paises,
)
from logica import ETAPAS_PUERTO, PALETA_PAISES, enriquecer, es_aereo
from ui_componentes import COLOR_ALERTA, COLOR_RECIBIDAS_MES, COLOR_TOTAL, esc, tarjeta_kpi


COLOR_CATEGORIA = "#0C4A6E"     # azul oscuro, para el bloque "qué se importa"
COLOR_AEREO = "#2a78d6"
COLOR_MARITIMO = "#1baf7a"

# "Aéreos" sigue siendo una pestaña real del Sheet (y se puede filtrar por
# ella), pero NO es un tipo de producto -- es un modo de transporte, igual que
# Vía. Por eso se excluye de los gráficos que responden "qué se importa" y de
# "categoría con más días en puerto": mezclarla ahí haría ver como si "cargar
# por avión" fuera una categoría de mercancía, que no lo es. Esa dimensión ya
# tiene su propio gráfico (Aéreo vs Marítimo, y las tarjetas de "ahora mismo").
CATEGORIA_NO_PRODUCTO = "Aéreos"

_LAYOUT_BASE = dict(
    margin=dict(t=28, b=10, l=10, r=10),
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#374151", size=11), showlegend=False, dragmode=False,
)


def _config_estatica(key: str) -> dict:
    return {"displayModeBar": False, "staticPlot": True, "responsive": True}


def _mes_de(valor) -> date | None:
    f = parsear_fecha(valor)
    return date(f.year, f.month, 1) if f else None


# ---------------------------------------------------------------------------
# DATOS BASE (universo combinado, histórico con ciclo cerrado, activos vivos)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _universo(activos: pd.DataFrame, historico: pd.DataFrame) -> pd.DataFrame:
    """Activos + histórico combinados (BL, Categoria, Pais, Descripcion, Anio)
    para responder 'qué se importa' incluyendo tanto lo ya recibido como lo
    que viene en camino. El país se re-normaliza aquí porque cargar_todo()
    solo lo normaliza para activos; el histórico llega tal cual está en el
    Sheet."""
    columnas = ["BL", "Categoria", "Pais", "Descripcion", "Anio"]
    piezas = []
    if activos is not None and not activos.empty:
        piezas.append(pd.DataFrame({
            "BL": activos[COL_BL], "Categoria": activos.get("Categoria", ""),
            "Pais": unificar_paises(activos[COL_PAIS]), "Descripcion": activos[COL_DESC],
            "Anio": [(parsear_fecha(v).year if parsear_fecha(v) else None) for v in activos[COL_ETA]],
        }))
    if historico is not None and not historico.empty:
        fechas_ref = historico["Fecha_Recibido"] if "Fecha_Recibido" in historico.columns else [""] * len(historico)
        piezas.append(pd.DataFrame({
            "BL": historico[COL_BL], "Categoria": historico.get("Categoria_Origen", ""),
            "Pais": unificar_paises(historico[COL_PAIS]), "Descripcion": historico[COL_DESC],
            "Anio": [(parsear_fecha(v).year if parsear_fecha(v) else None) for v in fechas_ref],
        }))
    if not piezas:
        return pd.DataFrame(columns=columnas)
    return pd.concat(piezas, ignore_index=True)[columnas]


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _ciclo_historico(historico: pd.DataFrame) -> pd.DataFrame:
    """Un renglón por embarque cerrado con las TRES etapas del ciclo, no solo
    'días en puerto':
      dias_transito: Fecha_Salida -> Fecha_Llegada_Puerto (si hay Fecha_Salida;
                     ese campo es opcional -- lo trae quien conoce el booking
                     del forwarder/naviera, así que suele faltar en varias filas)
      dias          : Fecha_Llegada_Puerto -> Fecha_Declaracion (llegada
                     confirmada hasta que se declara ante Aduanas -- lo que ya
                     se media antes como 'tiempo en puerto')
      dias_tramite  : Fecha_Declaracion -> Fecha_Almacen (declarado hasta que
                     entra físicamente al almacén; Fecha_Almacen siempre se
                     graba al archivar, así que esta etapa no tiene huecos)
    Separarlas dice DÓNDE se atasca un embarque -- en el barco/avión, esperando
    que Aduanas lo revise, o en la logística interna de retirarlo -- en vez de
    un solo número que mezcla las tres cosas."""
    columnas = ["dias", "dias_transito", "dias_tramite", "Categoria", "Pais", "Anio", "Via"]
    if (historico is None or historico.empty or COL_FECHA_LLEGADA_PUERTO not in historico.columns
            or COL_FECHA_DECLARACION not in historico.columns):
        return pd.DataFrame(columns=columnas)
    paises = unificar_paises(historico[COL_PAIS]) if COL_PAIS in historico.columns else pd.Series([""] * len(historico))
    filas = []
    for (_, r), pais in zip(historico.iterrows(), paises):
        llegada = parsear_fecha(r.get(COL_FECHA_LLEGADA_PUERTO, ""))
        declaracion = parsear_fecha(r.get(COL_FECHA_DECLARACION, ""))
        if not llegada or not declaracion or declaracion < llegada:
            continue
        salida = parsear_fecha(r.get(COL_FECHA_SALIDA, ""))
        dias_transito = (llegada - salida).days if salida and salida <= llegada else None
        almacen = parsear_fecha(r.get(COL_FECHA_ALMACEN, ""))
        dias_tramite = (almacen - declaracion).days if almacen and almacen >= declaracion else None
        filas.append({
            "dias": (declaracion - llegada).days,
            "dias_transito": dias_transito,
            "dias_tramite": dias_tramite,
            "Categoria": r.get("Categoria_Origen", "") or NO_ESPECIFICADO,
            "Pais": pais or NO_ESPECIFICADO,
            "Anio": llegada.year,
            "Via": VIA_AEREA if es_aereo(r.get(COL_VIA, "")) else "Marítimo",
        })
    return pd.DataFrame(filas, columns=columnas)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _mensual_historico(historico: pd.DataFrame) -> pd.DataFrame:
    """Un renglón por embarque recibido, con su mes y las dimensiones para
    poder filtrar la tendencia por País/Categoría/Año igual que el resto."""
    columnas = ["Mes", "Anio", "Categoria", "Pais"]
    if historico is None or historico.empty or "Fecha_Recibido" not in historico.columns:
        return pd.DataFrame(columns=columnas)
    paises = unificar_paises(historico[COL_PAIS]) if COL_PAIS in historico.columns else pd.Series([""] * len(historico))
    filas = []
    for (_, r), pais in zip(historico.iterrows(), paises):
        mes = _mes_de(r.get("Fecha_Recibido", ""))
        if not mes:
            continue
        filas.append({"Mes": mes, "Anio": mes.year, "Categoria": r.get("Categoria_Origen", "") or NO_ESPECIFICADO,
                      "Pais": pais or NO_ESPECIFICADO})
    return pd.DataFrame(filas, columns=columnas)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _enriquecer_activos_cacheado(activos: pd.DataFrame) -> pd.DataFrame:
    if activos is None or activos.empty:
        return activos
    return enriquecer(activos)


def _aplicar_filtros(df: pd.DataFrame, anios_sel: list, paises_sel: list, cats_sel: list) -> pd.DataFrame:
    """Vacío en cualquier filtro significa 'todos' -- así arranca mostrando el
    panorama completo y no una pantalla vacía la primera vez que se abre."""
    if df.empty:
        return df
    m = pd.Series(True, index=df.index)
    if anios_sel and "Anio" in df.columns:
        m &= df["Anio"].isin(anios_sel)
    if paises_sel and "Pais" in df.columns:
        m &= df["Pais"].replace("", NO_ESPECIFICADO).isin(paises_sel)
    if cats_sel and "Categoria" in df.columns:
        m &= df["Categoria"].replace("", NO_ESPECIFICADO).isin(cats_sel)
    return df[m]


def _en_puerto_ahora(activos_enriq: pd.DataFrame, paises_sel: list, cats_sel: list) -> tuple:
    """Cuenta embarques ACTIVOS que ya confirmaron llegada y todavía no se
    declaran -- es decir, físicamente sentados en puerto o en aeropuerto hoy.
    Es la misma etapa que usa el tablero (ETAPAS_PUERTO[0]), no un cálculo
    aparte. Devuelve (en_puerto, en_aeropuerto)."""
    if activos_enriq is None or activos_enriq.empty or "EtapaActual" not in activos_enriq.columns:
        return 0, 0
    df = activos_enriq[activos_enriq["EtapaActual"] == ETAPAS_PUERTO[0]]
    if paises_sel and COL_PAIS in df.columns:
        df = df[df[COL_PAIS].isin(paises_sel)]
    if cats_sel and "Categoria" in df.columns:
        df = df[df["Categoria"].replace("", NO_ESPECIFICADO).isin(cats_sel)]
    if df.empty:
        return 0, 0
    aereo = df[COL_VIA].apply(es_aereo) if COL_VIA in df.columns else pd.Series([False] * len(df), index=df.index)
    return int((~aereo).sum()), int(aereo.sum())


def _categoria_mas_lenta(dias_df: pd.DataFrame):
    base = dias_df[dias_df["Categoria"] != CATEGORIA_NO_PRODUCTO] if not dias_df.empty else dias_df
    if base.empty:
        return None, None
    prom = base.groupby("Categoria")["dias"].mean().sort_values(ascending=False)
    return (prom.index[0], prom.iloc[0]) if len(prom) else (None, None)


def _cumplimiento_sla(dias_df: pd.DataFrame):
    """Un promedio esconde qué tan seguido se cumple: 8 días en 9 de 10
    embarques y un atraso de 80 días en el décimo también dan 'promedio 16
    días', pero son historias muy distintas. El % dentro del umbral configurado
    (el mismo que usa el tablero para marcar 'retrasado', costos_puerto())
    responde la pregunta operativa real: ¿qué tan seguido salimos a tiempo?"""
    if dias_df.empty:
        return None, 0
    umbral = costos_puerto()["umbral"]
    cumplidos = int((dias_df["dias"] <= umbral).sum())
    return round(100 * cumplidos / len(dias_df), 1), len(dias_df)


# ---------------------------------------------------------------------------
# GRÁFICAS
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


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_categorias(universo: pd.DataFrame) -> go.Figure | None:
    """Por categoría, no por Descripción: la Descripción es texto libre y
    agruparla tal cual duplicaría barras para la misma cosa. Excluye Aéreos
    (ver CATEGORIA_NO_PRODUCTO)."""
    if universo.empty:
        return None
    base = universo[universo["Categoria"] != CATEGORIA_NO_PRODUCTO]
    serie = base["Categoria"].replace("", NO_ESPECIFICADO).value_counts()
    if serie.empty:
        return None
    fig = go.Figure(data=[go.Pie(
        labels=serie.index, values=serie.values, hole=0.55,
        marker=dict(colors=[PALETA_PAISES[i % len(PALETA_PAISES)] for i in range(len(serie))]),
        textinfo="label+percent", hovertemplate="%{label}: %{value} embarque(s)<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=320,
                      title=dict(text="Embarques por categoría de producto", font=dict(size=13)))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_top_productos(universo: pd.DataFrame, n: int = 10) -> go.Figure | None:
    """Top de productos por Descripción, normalizada a mayúsculas para que
    'Montacarga' y 'MONTACARGA' cuenten como lo mismo. Sigue siendo texto
    libre -- variantes con detalle distinto ('Montacarga Toyota' vs
    'Montacarga') no se agrupan solas."""
    if universo.empty:
        return None
    serie = universo["Descripcion"].astype(str).str.strip()
    serie = serie[serie != ""].str.upper().value_counts().head(n).sort_values()
    if serie.empty:
        return None
    colores = [PALETA_PAISES[i % len(PALETA_PAISES)] for i in range(len(serie))]
    fig = go.Figure(data=[go.Bar(
        x=serie.values, y=serie.index, orientation="h", marker=dict(color=colores),
        text=serie.values, textposition="outside",
        hovertemplate="%{y}: %{x} embarque(s)<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=max(280, 34 * len(serie)),
                      title=dict(text="Los 10 productos más importados", font=dict(size=13)),
                      xaxis=dict(showgrid=True, gridcolor="#F3F4F6", title=""),
                      yaxis=dict(showgrid=False, title=""))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_tendencia_mensual(mensual: pd.DataFrame, meses: int = 24) -> go.Figure | None:
    if mensual.empty:
        return None
    conteo = mensual["Mes"].value_counts().sort_index().tail(meses)
    if conteo.empty:
        return None
    etiquetas = [f"{MESES_ES_CORTO[m.month]} {m.year}" for m in conteo.index]
    fig = go.Figure(data=[go.Scatter(
        x=etiquetas, y=conteo.values, mode="lines+markers+text",
        line=dict(color=COLOR_RECIBIDAS_MES, width=3), marker=dict(size=7),
        text=conteo.values, textposition="top center",
        hovertemplate="%{x}: %{y} embarque(s) recibido(s)<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=280,
                      title=dict(text="Embarques recibidos por mes", font=dict(size=13)),
                      xaxis=dict(showgrid=False, title=""),
                      yaxis=dict(showgrid=True, gridcolor="#F3F4F6", title="", rangemode="tozero"))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_tiempo_puerto_categoria(dias_df: pd.DataFrame) -> go.Figure | None:
    if dias_df.empty:
        return None
    base = dias_df[dias_df["Categoria"] != CATEGORIA_NO_PRODUCTO]
    prom = base.groupby("Categoria")["dias"].mean().sort_values()
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


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_ciclo_etapas(dias_df: pd.DataFrame) -> go.Figure | None:
    """El ciclo completo partido en sus 3 etapas reales, no un solo número que
    las mezcla. 'Tránsito' solo cuenta embarques que traen Fecha_Salida (es
    opcional, así que casi siempre son menos que el total) -- se etiqueta con
    cuántos son, para no dar un promedio como si fuera de todos."""
    if dias_df.empty:
        return None
    etapas, valores, notas, colores = [], [], [], []
    con_transito = dias_df["dias_transito"].dropna()
    if len(con_transito):
        etapas.append("Tránsito (salida → llegada)")
        valores.append(con_transito.mean())
        notas.append(f"n={len(con_transito)}")
        colores.append(COLOR_TOTAL)
    etapas.append("En puerto (llegada → declaración)")
    valores.append(dias_df["dias"].mean())
    notas.append(f"n={len(dias_df)}")
    colores.append(COLOR_ALERTA)
    con_tramite = dias_df["dias_tramite"].dropna()
    if len(con_tramite):
        etapas.append("Trámite final (declaración → almacén)")
        valores.append(con_tramite.mean())
        notas.append(f"n={len(con_tramite)}")
        colores.append(COLOR_CATEGORIA)
    if len(etapas) < 2:
        return None  # con solo una etapa no hay "ciclo" que mostrar
    fig = go.Figure(data=[go.Bar(
        x=etapas, y=valores, marker=dict(color=colores),
        text=[f"{v:.1f} d ({n})" for v, n in zip(valores, notas)], textposition="outside",
        hovertemplate="%{x}: %{y:.1f} días promedio<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=280,
                      title=dict(text="Ciclo completo por etapa (días promedio)", font=dict(size=13)),
                      xaxis=dict(showgrid=False, title=""),
                      yaxis=dict(showgrid=True, gridcolor="#F3F4F6", title="", rangemode="tozero"))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_heatmap_pais_categoria(universo: pd.DataFrame, top_paises: int = 8, top_cats: int = 8) -> go.Figure | None:
    """De dónde viene cada categoría, cruzado -- no dos gráficos separados de
    'por país' y 'por categoría' que no dicen si es el MISMO país el que
    concentra una categoría entera. Es la vista que responde 'si este
    proveedor/país falla, ¿qué categoría se me atasca?'."""
    base = universo[universo["Categoria"] != CATEGORIA_NO_PRODUCTO]
    if base.empty:
        return None
    paises_top = base["Pais"].replace("", NO_ESPECIFICADO).value_counts().head(top_paises).index.tolist()
    cats_top = base["Categoria"].replace("", NO_ESPECIFICADO).value_counts().head(top_cats).index.tolist()
    tabla = base[base["Pais"].replace("", NO_ESPECIFICADO).isin(paises_top)
                & base["Categoria"].replace("", NO_ESPECIFICADO).isin(cats_top)]
    if tabla.empty:
        return None
    cruce = pd.crosstab(tabla["Categoria"].replace("", NO_ESPECIFICADO),
                        tabla["Pais"].replace("", NO_ESPECIFICADO))
    cruce = cruce.reindex(index=cats_top, columns=paises_top, fill_value=0)
    fig = go.Figure(data=go.Heatmap(
        z=cruce.values, x=cruce.columns, y=cruce.index, colorscale="Blues",
        text=cruce.values, texttemplate="%{text}", hovertemplate="%{y} desde %{x}: %{z} embarque(s)<extra></extra>",
    ))
    fig.update_layout(**{**_LAYOUT_BASE, "font": dict(color="#374151", size=10)},
                      height=max(280, 34 * len(cats_top)),
                      title=dict(text="Qué categoría viene de qué país", font=dict(size=13)),
                      xaxis=dict(showgrid=False, title="", side="bottom"),
                      yaxis=dict(showgrid=False, title="", autorange="reversed"))
    return fig


def _tarjeta_via(dias_df: pd.DataFrame) -> str:
    """Comparación Aéreo vs Marítimo -- la Vía es un dato por embarque, así
    que esta es la comparación correcta de 'tiempo por modo de transporte',
    separada de 'tiempo por categoría de producto'."""
    if dias_df.empty or dias_df["Via"].nunique() < 2:
        return ""
    prom = dias_df.groupby("Via")["dias"].mean()
    aereo, maritimo = prom.get(VIA_AEREA), prom.get("Marítimo")
    if aereo is None or maritimo is None:
        return ""
    piezas = ['<div class="paises"><div class="atttl">Días en puerto: Aéreo vs Marítimo</div>']
    tope = max(aereo, maritimo) or 1
    for etiqueta, valor, color in ((VIA_AEREA, aereo, COLOR_AEREO), ("Marítimo", maritimo, COLOR_MARITIMO)):
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
    st.caption("Se arma sola con lo que ya está en tránsito y en el histórico — no hay que capturar nada aparte.")

    activos_crudo, historico = datos.get("activos"), datos.get("historico")
    activos = _enriquecer_activos_cacheado(activos_crudo)
    universo = _universo(activos_crudo, historico)
    dias_df = _ciclo_historico(historico)
    mensual = _mensual_historico(historico)

    if universo.empty and (historico is None or historico.empty):
        st.info("Todavía no hay suficientes embarques (activos o recibidos) para armar analítica.")
        return

    anios_disp = sorted({int(a) for a in universo["Anio"].dropna()}, reverse=True)
    paises_disp = sorted({p for p in universo["Pais"].replace("", NO_ESPECIFICADO).unique() if p})
    cats_disp = sorted({c for c in universo["Categoria"].replace("", NO_ESPECIFICADO).unique() if c})

    f1, f2, f3 = st.columns(3)
    anios_sel = f1.multiselect("Año", anios_disp, default=[], key="an_f_anio", help="Vacío = todos los años")
    paises_sel = f2.multiselect("País de origen", paises_disp, default=[], key="an_f_pais",
                                help="Vacío = todos los países")
    cats_sel = f3.multiselect("Categoría", cats_disp, default=[], key="an_f_cat",
                              help="Vacío = todas las categorías. Incluye Aéreos, aunque los gráficos de "
                                   "'qué se importa' no la usen como categoría de producto.")

    universo_f = _aplicar_filtros(universo, anios_sel, paises_sel, cats_sel)
    dias_f = _aplicar_filtros(dias_df, anios_sel, paises_sel, cats_sel)
    mensual_f = _aplicar_filtros(mensual, anios_sel, paises_sel, cats_sel)
    en_puerto, en_aeropuerto = _en_puerto_ahora(activos, paises_sel, cats_sel)

    pais_top = universo_f["Pais"].replace("", NO_ESPECIFICADO).mode()
    cat_top = universo_f[universo_f["Categoria"] != CATEGORIA_NO_PRODUCTO]["Categoria"] \
        .replace("", NO_ESPECIFICADO).mode()
    dias_prom = dias_f["dias"].mean() if not dias_f.empty else None
    cat_lenta, dias_lenta = _categoria_mas_lenta(dias_f)
    sla_pct, sla_n = _cumplimiento_sla(dias_f)

    st.write("")
    cols = st.columns(6)
    tarjetas = [
        ("Embarques recibidos", str(len(mensual_f)), COLOR_RECIBIDAS_MES),
        ("País principal", pais_top.iloc[0] if len(pais_top) else "—", COLOR_TOTAL),
        ("Categoría principal", cat_top.iloc[0] if len(cat_top) else "—", COLOR_CATEGORIA),
        ("Días en puerto (prom.)", f"{dias_prom:.1f} d" if dias_prom is not None else "—", COLOR_ALERTA),
        ("Categoría con más días en puerto",
         f"{cat_lenta} · {dias_lenta:.1f} d" if cat_lenta else "—", COLOR_ALERTA),
        ("Cumplimiento SLA en puerto",
         f"{sla_pct}% ({sla_n})" if sla_pct is not None else "—", COLOR_MARITIMO),
    ]
    for col, (label, valor, color) in zip(cols, tarjetas):
        with col:
            st.markdown(tarjeta_kpi(label, valor, color), unsafe_allow_html=True)

    st.write("")
    ca1, ca2 = st.columns(2)
    with ca1:
        st.markdown(tarjeta_kpi("Mercancía en Puerto ahora", str(en_puerto), COLOR_MARITIMO),
                    unsafe_allow_html=True)
    with ca2:
        st.markdown(tarjeta_kpi("Mercancía en Aeropuerto ahora", str(en_aeropuerto), COLOR_AEREO),
                    unsafe_allow_html=True)
    st.caption("Llegada confirmada, todavía sin declarar ante Aduanas — la carga sigue físicamente ahí.")

    st.write("")
    c1, c2 = st.columns(2)
    with c1:
        fig = _figura_paises(universo_f)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_estatica("paises"), key="an_paises")
        else:
            st.caption("Sin datos de país todavía.")
    with c2:
        fig = _figura_categorias(universo_f)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_estatica("cat"), key="an_categorias")
        else:
            st.caption("Sin datos de categoría todavía.")
    st.caption("'Aéreos' no aparece en este gráfico: es un modo de transporte, no un tipo de producto. "
              "Se refleja arriba, en 'Mercancía en Aeropuerto ahora', y en la comparación de Vía más abajo.")

    st.write("")
    fig = _figura_heatmap_pais_categoria(universo_f)
    if fig:
        st.plotly_chart(fig, width="stretch", config=_config_estatica("heatmap"), key="an_heatmap")
    else:
        st.caption("Sin suficiente cruce de país y categoría todavía.")

    st.write("")
    fig = _figura_top_productos(universo_f)
    if fig:
        st.plotly_chart(fig, width="stretch", config=_config_estatica("productos"), key="an_productos")
    else:
        st.caption("Sin descripciones suficientes para el top de productos.")

    st.write("")
    c3, c4 = st.columns(2)
    with c3:
        fig = _figura_tendencia_mensual(mensual_f)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_estatica("tendencia"), key="an_tendencia")
        else:
            st.caption("Todavía no hay suficiente histórico mes a mes con estos filtros.")
    with c4:
        fig = _figura_tiempo_puerto_categoria(dias_f)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_estatica("tiempo"), key="an_tiempo")
        else:
            st.caption("Todavía no hay embarques archivados con llegada y declaración para medir tiempo en puerto.")

    st.write("")
    c5, c6 = st.columns(2)
    with c5:
        fig = _figura_ciclo_etapas(dias_f)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_estatica("ciclo"), key="an_ciclo")
        else:
            st.caption("Todavía no hay suficientes embarques con fechas completas para partir el ciclo en etapas.")
    with c6:
        html_via = _tarjeta_via(dias_f)
        if html_via:
            st.markdown(html_via, unsafe_allow_html=True)
        else:
            st.caption("Falta variedad de Vía (Aéreo/Marítimo) en el histórico filtrado para comparar.")

    st.caption(f"Última actualización de los datos: {datos['hora'].strftime('%H:%M:%S')}")
