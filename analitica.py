"""
analitica.py — Analítica de importaciones para Antillana Comercial.

Panel de gráficas de negocio (no operativo), estilo BI: filtros interactivos
(segmented_control para Año/Categoría, clic-para-filtrar en las gráficas de
País y Categoría), donas con total al centro, área bajo la línea de tendencia,
cada gráfica en su propia tarjeta. No es una fuente de datos aparte: se arma
sobre lo mismo que ya trae cargar_todo() (activos + histórico). Sin
escrituras: todo aquí es de solo lectura.

Pagos (aduanas, costos) se dejó AFUERA a propósito: todavía hay muy poca data
registrada en esa pestaña como para que un promedio signifique algo.

Nota sobre el clic-para-filtrar (on_select de st.plotly_chart, disponible
desde Streamlit 1.35+ y confirmado en la versión fijada, 1.61.0): clicar una
barra de país o una porción de categoría agrega ese valor al filtro efectivo
de TODO el panel, incluida la propia gráfica que se clicó -- es la forma más
simple y predecible de implementarlo sin duplicar el estado de cada gráfica
por separado. Para quitar la selección, se vuelve a clicar el mismo punto o
se usa el botón "Limpiar selección de gráficas".
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
from logica import ETAPAS_PUERTO, es_aereo
from ui_componentes import _enriquecer_cacheado, esc


# Paleta propia para esta pestaña -- más viva que la de tránsito (PALETA_PAISES
# se reserva para las tarjetas de embarque, que conviven con el resto de la
# app). Pensada para verse bien tanto en barras como en donas y heatmap.
PALETA_BI = ["#2563EB", "#F59E0B", "#10B981", "#8B5CF6", "#EF4444",
            "#0EA5E9", "#EC4899", "#14B8A6", "#F97316", "#6366F1"]

COLOR_AEREO = "#2563EB"
COLOR_MARITIMO = "#10B981"

# "Aéreos" sigue siendo una pestaña real del Sheet (y se puede filtrar por
# ella), pero NO es un tipo de producto -- es un modo de transporte, igual que
# Vía. Por eso se excluye de los gráficos que responden "qué se importa" y de
# "categoría con más días en puerto": mezclarla ahí haría ver como si "cargar
# por avión" fuera una categoría de mercancía, que no lo es. Esa dimensión ya
# tiene su propio gráfico (Aéreo vs Marítimo, y las tarjetas de "ahora mismo").
CATEGORIA_NO_PRODUCTO = "Aéreos"

_FUENTE = "Segoe UI, -apple-system, BlinkMacSystemFont, sans-serif"

_LAYOUT_BASE = dict(
    margin=dict(t=36, b=10, l=10, r=10),
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#374151", size=12, family=_FUENTE),
    title_font=dict(size=15, family=_FUENTE, color="#111827"),
    showlegend=False, dragmode=False, hoverlabel=dict(font_size=13, font_family=_FUENTE),
)


def _config_interactiva() -> dict:
    """A diferencia de las mini-gráficas de tránsito (donde staticPlot evita
    redibujar decenas de figuras por pantalla), aquí solo hay un puñado de
    gráficas grandes: sí vale la pena el hover y el zoom reales."""
    return {"displayModeBar": False, "responsive": True, "scrollZoom": False}


def _mes_de(valor) -> date | None:
    f = parsear_fecha(valor)
    return date(f.year, f.month, 1) if f else None


def _tarjeta_kpi_bi(icono: str, label: str, valor: str, color_a: str, color_b: str) -> str:
    """Tarjeta KPI propia de esta pestaña: degradado, ícono e ícono más grande
    que la tarjeta plana que usa el resto de la app -- aquí es el resumen
    ejecutivo, así que puede pesar más visualmente."""
    return (
        f'<div style="background:linear-gradient(135deg,{color_a} 0%,{color_b} 100%); '
        f'border-radius:16px; padding:16px 12px; min-height:108px; '
        f'box-shadow:0 4px 14px rgba(17,24,39,0.16); display:flex; flex-direction:column; '
        f'align-items:center; justify-content:center; text-align:center;">'
        f'<div style="font-size:1.5rem; line-height:1;">{icono}</div>'
        f'<div style="font-size:1.5rem; font-weight:800; color:#fff; margin-top:4px; '
        f'font-family:{_FUENTE};">{esc(str(valor))}</div>'
        f'<div style="font-size:0.68rem; font-weight:700; letter-spacing:0.04em; '
        f'text-transform:uppercase; color:rgba(255,255,255,0.92); margin-top:3px;">{esc(label)}</div>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# DATOS BASE
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
            "Pais": activos[COL_PAIS], "Descripcion": activos[COL_DESC],
            "Anio": [(f.year if f else None) for f in (parsear_fecha(v) for v in activos[COL_ETA])],
        }))
    if historico is not None and not historico.empty:
        fechas_ref = historico["Fecha_Recibido"] if "Fecha_Recibido" in historico.columns else [""] * len(historico)
        piezas.append(pd.DataFrame({
            "BL": historico[COL_BL], "Categoria": historico.get("Categoria_Origen", ""),
            "Pais": unificar_paises(historico[COL_PAIS]), "Descripcion": historico[COL_DESC],
            "Anio": [(f.year if f else None) for f in (parsear_fecha(v) for v in fechas_ref)],
        }))
    if not piezas:
        return pd.DataFrame(columns=columnas)
    return pd.concat(piezas, ignore_index=True)[columnas]


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _ciclo_historico(historico: pd.DataFrame) -> pd.DataFrame:
    """Un renglón por embarque cerrado con las TRES etapas del ciclo:
      dias_transito: Fecha_Salida -> Fecha_Llegada_Puerto (opcional: falta en
                     bastantes filas porque depende del booking del forwarder)
      dias          : Fecha_Llegada_Puerto -> Fecha_Declaracion
      dias_tramite  : Fecha_Declaracion -> Fecha_Almacen (siempre presente:
                     se graba solo al archivar)"""
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


def _aplicar_filtros(df: pd.DataFrame, anios_sel: list, paises_sel: list, cats_sel: list) -> pd.DataFrame:
    """Vacío en cualquier filtro significa 'todos'."""
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
    if dias_df.empty:
        return None, 0
    umbral = costos_puerto()["umbral"]
    cumplidos = int((dias_df["dias"] <= umbral).sum())
    return round(100 * cumplidos / len(dias_df), 1), len(dias_df)


def _puntos_clicados(evento, campo_preferido: str = "y") -> list:
    """Extrae las etiquetas de los puntos clicados en una gráfica con
    on_select='rerun'. Barras horizontales guardan la categoría en 'y'; donas
    la guardan en 'label'. Se revisan varias claves porque el nombre exacto
    depende del tipo de traza, y prefiero tolerar variaciones a que un cambio
    menor de Plotly rompa el clic en silencio."""
    if not evento:
        return []
    puntos = (evento.get("selection", {}) or {}).get("points", []) if isinstance(evento, dict) else []
    valores = []
    for p in puntos:
        v = p.get(campo_preferido) or p.get("label") or p.get("y") or p.get("x")
        if v:
            valores.append(str(v))
    return valores


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
    colores = [PALETA_BI[i % len(PALETA_BI)] for i in range(len(serie))]
    fig = go.Figure(data=[go.Bar(
        x=serie.values, y=serie.index, orientation="h", marker=dict(color=colores, line=dict(width=0)),
        text=serie.values, textposition="outside", textfont=dict(size=12, family=_FUENTE),
        hovertemplate="<b>%{y}</b><br>%{x} embarque(s)<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=max(240, 36 * len(serie)),
                      title=dict(text="🌍 Embarques por país de origen — clic para filtrar"),
                      xaxis=dict(showgrid=True, gridcolor="#F1F5F9", title=""),
                      yaxis=dict(showgrid=False, title=""))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_categorias(universo: pd.DataFrame) -> go.Figure | None:
    if universo.empty:
        return None
    base = universo[universo["Categoria"] != CATEGORIA_NO_PRODUCTO]
    serie = base["Categoria"].replace("", NO_ESPECIFICADO).value_counts()
    if serie.empty:
        return None
    total = int(serie.sum())
    fig = go.Figure(data=[go.Pie(
        labels=serie.index, values=serie.values, hole=0.62,
        marker=dict(colors=[PALETA_BI[i % len(PALETA_BI)] for i in range(len(serie))],
                   line=dict(color="#FFFFFF", width=2)),
        textinfo="percent", textfont=dict(size=12, family=_FUENTE, color="#fff"),
        hovertemplate="<b>%{label}</b><br>%{value} embarque(s) (%{percent})<extra></extra>",
    )])
    fig.add_annotation(text=f"<b>{total}</b><br>embarques", x=0.5, y=0.5, showarrow=False,
                       font=dict(size=15, family=_FUENTE, color="#111827"))
    fig.update_layout(**{**_LAYOUT_BASE, "showlegend": True}, height=380,
                      legend=dict(orientation="h", y=-0.15, font=dict(size=11, family=_FUENTE)),
                      title=dict(text="🏷️ Embarques por categoría — clic para filtrar"))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_top_productos(universo: pd.DataFrame, n: int = 10) -> go.Figure | None:
    if universo.empty:
        return None
    serie = universo["Descripcion"].astype(str).str.strip()
    serie = serie[serie != ""].str.upper().value_counts().head(n).sort_values()
    if serie.empty:
        return None
    colores = [PALETA_BI[i % len(PALETA_BI)] for i in range(len(serie))]
    fig = go.Figure(data=[go.Bar(
        x=serie.values, y=serie.index, orientation="h", marker=dict(color=colores, line=dict(width=0)),
        text=serie.values, textposition="outside", textfont=dict(size=12, family=_FUENTE),
        hovertemplate="<b>%{y}</b><br>%{x} embarque(s)<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=max(300, 36 * len(serie)),
                      title=dict(text="📦 Los 10 productos más importados"),
                      xaxis=dict(showgrid=True, gridcolor="#F1F5F9", title=""),
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
        line=dict(color=PALETA_BI[0], width=3, shape="spline", smoothing=0.3),
        marker=dict(size=8, color=PALETA_BI[0], line=dict(color="#fff", width=1)),
        fill="tozeroy", fillcolor="rgba(37,99,235,0.12)",
        text=conteo.values, textposition="top center", textfont=dict(size=11, family=_FUENTE),
        hovertemplate="<b>%{x}</b><br>%{y} embarque(s) recibido(s)<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=300,
                      title=dict(text="📈 Embarques recibidos por mes"),
                      xaxis=dict(showgrid=False, title="", tickangle=-45),
                      yaxis=dict(showgrid=True, gridcolor="#F1F5F9", title="", rangemode="tozero"))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_tiempo_puerto_categoria(dias_df: pd.DataFrame) -> go.Figure | None:
    if dias_df.empty:
        return None
    base = dias_df[dias_df["Categoria"] != CATEGORIA_NO_PRODUCTO]
    prom = base.groupby("Categoria")["dias"].mean().sort_values()
    if prom.empty:
        return None
    colores = [PALETA_BI[i % len(PALETA_BI)] for i in range(len(prom))]
    fig = go.Figure(data=[go.Bar(
        x=prom.values, y=prom.index, orientation="h", marker=dict(color=colores, line=dict(width=0)),
        text=[f"{v:.1f} d" for v in prom.values], textposition="outside", textfont=dict(size=12, family=_FUENTE),
        hovertemplate="<b>%{y}</b><br>%{x:.1f} días promedio<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=max(240, 36 * len(prom)),
                      title=dict(text="⏱️ Días promedio en puerto por categoría"),
                      xaxis=dict(showgrid=True, gridcolor="#F1F5F9", title=""),
                      yaxis=dict(showgrid=False, title=""))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_ciclo_etapas(dias_df: pd.DataFrame) -> go.Figure | None:
    if dias_df.empty:
        return None
    etapas, valores, notas, colores = [], [], [], []
    con_transito = dias_df["dias_transito"].dropna()
    if len(con_transito):
        etapas.append("Tránsito<br>(salida → llegada)")
        valores.append(con_transito.mean())
        notas.append(f"n={len(con_transito)}")
        colores.append(PALETA_BI[0])
    etapas.append("En puerto<br>(llegada → declaración)")
    valores.append(dias_df["dias"].mean())
    notas.append(f"n={len(dias_df)}")
    colores.append(PALETA_BI[1])
    con_tramite = dias_df["dias_tramite"].dropna()
    if len(con_tramite):
        etapas.append("Trámite final<br>(declaración → almacén)")
        valores.append(con_tramite.mean())
        notas.append(f"n={len(con_tramite)}")
        colores.append(PALETA_BI[2])
    if len(etapas) < 2:
        return None
    fig = go.Figure(data=[go.Bar(
        x=etapas, y=valores, marker=dict(color=colores, line=dict(width=0)),
        text=[f"{v:.1f} d ({n})" for v, n in zip(valores, notas)], textposition="outside",
        textfont=dict(size=12, family=_FUENTE),
        hovertemplate="%{x}: %{y:.1f} días promedio<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=300,
                      title=dict(text="🔄 Ciclo completo por etapa (días promedio)"),
                      xaxis=dict(showgrid=False, title=""),
                      yaxis=dict(showgrid=True, gridcolor="#F1F5F9", title="", rangemode="tozero"))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_heatmap_pais_categoria(universo: pd.DataFrame, top_paises: int = 8, top_cats: int = 8) -> go.Figure | None:
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
        text=cruce.values, texttemplate="%{text}", textfont=dict(size=12, family=_FUENTE),
        hovertemplate="<b>%{y}</b> desde <b>%{x}</b>: %{z} embarque(s)<extra></extra>",
        colorbar=dict(thickness=12, len=0.8),
    ))
    fig.update_layout(**{**_LAYOUT_BASE, "font": dict(color="#374151", size=11, family=_FUENTE)},
                      height=max(300, 40 * len(cats_top)),
                      title=dict(text="🗺️ Qué categoría viene de qué país"),
                      xaxis=dict(showgrid=False, title="", side="bottom", tickangle=-45),
                      yaxis=dict(showgrid=False, title="", autorange="reversed"))
    return fig


def _tarjeta_via(dias_df: pd.DataFrame) -> str:
    if dias_df.empty or dias_df["Via"].nunique() < 2:
        return ""
    prom = dias_df.groupby("Via")["dias"].mean()
    aereo, maritimo = prom.get(VIA_AEREA), prom.get("Marítimo")
    if aereo is None or maritimo is None:
        return ""
    piezas = ['<div class="paises"><div class="atttl">✈️🚢 Días en puerto: Aéreo vs Marítimo</div>']
    tope = max(aereo, maritimo) or 1
    for etiqueta, valor, color in ((VIA_AEREA, aereo, COLOR_AEREO), ("Marítimo", maritimo, COLOR_MARITIMO)):
        ancho = max(4.0, (valor / tope) * 100.0)
        piezas.append(
            f'<div class="pfila"><div class="pnom">{esc(etiqueta)}</div>'
            f'<div class="pbarra"><span style="width:{ancho:.1f}%;background:{color};border-radius:6px;"></span></div>'
            f'<div class="pval">{valor:.1f} d</div></div>'
        )
    piezas.append("</div>")
    return "".join(piezas)


# ---------------------------------------------------------------------------
# PANEL
# ---------------------------------------------------------------------------
@st.fragment
def panel_analitica(datos: dict):
    st.subheader("📊 Analítica de importaciones")
    st.caption("Se arma sola con lo que ya está en tránsito y en el histórico. Los filtros de arriba y el clic "
              "sobre las barras de País o las porciones de Categoría actualizan todo el panel.")

    activos_crudo, historico = datos.get("activos"), datos.get("historico")
    activos = _enriquecer_cacheado(activos_crudo)
    universo = _universo(activos_crudo, historico)
    dias_df = _ciclo_historico(historico)
    mensual = _mensual_historico(historico)

    if universo.empty and (historico is None or historico.empty):
        st.info("Todavía no hay suficientes embarques (activos o recibidos) para armar analítica.")
        return

    anios_disp = sorted({int(a) for a in universo["Anio"].dropna()}, reverse=True)
    paises_disp = sorted({p for p in universo["Pais"].replace("", NO_ESPECIFICADO).unique() if p})
    cats_disp = sorted({c for c in universo["Categoria"].replace("", NO_ESPECIFICADO).unique() if c})

    f1, f2, f3 = st.columns([1, 1.4, 1.6])
    with f1:
        anios_sel = st.segmented_control("Año", anios_disp, selection_mode="multi",
                                         default=[], key="an_f_anio") or []
    with f2:
        paises_sel_menu = st.multiselect("País de origen", paises_disp, default=[], key="an_f_pais",
                                         help="Vacío = todos los países")
    with f3:
        cats_sel_menu = st.multiselect("Categoría", cats_disp, default=[], key="an_f_cat",
                                       help="Vacío = todas. Incluye Aéreos, aunque los gráficos de "
                                            "'qué se importa' no la usen como categoría de producto.")

    if st.button("✕ Limpiar selección de gráficas", key="an_limpiar_click"):
        for k in ("an_paises", "an_categorias"):
            st.session_state.pop(k, None)
        st.rerun()

    # --- gráficas (se construyen con los filtros del menú; el clic sobre
    # ellas se lee más abajo y se suma al filtro efectivo del resto) ---
    universo_menu = _aplicar_filtros(universo, anios_sel, paises_sel_menu, cats_sel_menu)

    c1, c2 = st.columns(2)
    with c1:
        with st.container(border=True):
            fig_paises = _figura_paises(universo_menu)
            if fig_paises:
                evento_pais = st.plotly_chart(fig_paises, width="stretch", config=_config_interactiva(),
                                              on_select="rerun", selection_mode="points", key="an_paises")
            else:
                evento_pais = None
                st.caption("Sin datos de país todavía.")
    with c2:
        with st.container(border=True):
            fig_cats = _figura_categorias(universo_menu)
            if fig_cats:
                evento_cat = st.plotly_chart(fig_cats, width="stretch", config=_config_interactiva(),
                                             on_select="rerun", selection_mode="points", key="an_categorias")
            else:
                evento_cat = None
                st.caption("Sin datos de categoría todavía.")
    st.caption("'Aéreos' no aparece en la gráfica de categoría: es un modo de transporte, no un tipo de "
              "producto. Se refleja en 'Mercancía en Aeropuerto ahora' y en la comparación de Vía, más abajo.")

    # Clic en cualquiera de las dos gráficas se suma al filtro efectivo de
    # TODO el panel (incluidas ellas mismas): es la forma más predecible de
    # implementarlo sin mantener un estado de resaltado aparte por gráfica.
    paises_click = _puntos_clicados(evento_pais, "y")
    cats_click = _puntos_clicados(evento_cat, "label")
    anios_sel_ef = anios_sel
    paises_sel = sorted(set(paises_sel_menu) | set(paises_click))
    cats_sel = sorted(set(cats_sel_menu) | set(cats_click))
    if paises_click or cats_click:
        st.caption("🔎 Filtro activo por clic: " +
                  ", ".join([f"País = {', '.join(paises_click)}"] * bool(paises_click) +
                            [f"Categoría = {', '.join(cats_click)}"] * bool(cats_click)))

    universo_f = _aplicar_filtros(universo, anios_sel_ef, paises_sel, cats_sel)
    dias_f = _aplicar_filtros(dias_df, anios_sel_ef, paises_sel, cats_sel)
    mensual_f = _aplicar_filtros(mensual, anios_sel_ef, paises_sel, cats_sel)
    en_puerto, en_aeropuerto = _en_puerto_ahora(activos, paises_sel, cats_sel)

    pais_top = universo_f["Pais"].replace("", NO_ESPECIFICADO).mode()
    cat_top = universo_f[universo_f["Categoria"] != CATEGORIA_NO_PRODUCTO]["Categoria"] \
        .replace("", NO_ESPECIFICADO).mode()
    dias_prom = dias_f["dias"].mean() if not dias_f.empty else None
    cat_lenta, dias_lenta = _categoria_mas_lenta(dias_f)
    sla_pct, sla_n = _cumplimiento_sla(dias_f)

    st.write("")
    with st.container(key="bikpirow"):
        cols = st.columns(6)
        tarjetas = [
            ("📦", "Embarques recibidos", str(len(mensual_f)), "#059669", "#10B981"),
            ("🌍", "País principal", pais_top.iloc[0] if len(pais_top) else "—", "#0284C7", "#38BDF8"),
            ("🏷️", "Categoría principal", cat_top.iloc[0] if len(cat_top) else "—", "#1E3A5F", "#0C4A6E"),
            ("⏱️", "Días en puerto (prom.)", f"{dias_prom:.1f} d" if dias_prom is not None else "—",
             "#B45309", "#F59E0B"),
            ("🐢", "Categoría más lenta", f"{cat_lenta} · {dias_lenta:.1f} d" if cat_lenta else "—",
             "#B91C1C", "#EF4444"),
            ("✅", "Cumplimiento SLA en puerto", f"{sla_pct}% ({sla_n})" if sla_pct is not None else "—",
             "#0F766E", "#14B8A6"),
        ]
        for col, (icono, label, valor, ca, cb) in zip(cols, tarjetas):
            with col:
                st.markdown(_tarjeta_kpi_bi(icono, label, valor, ca, cb), unsafe_allow_html=True)

    st.write("")
    ca1, ca2 = st.columns(2)
    with ca1:
        st.markdown(_tarjeta_kpi_bi("⚓", "Mercancía en Puerto ahora", str(en_puerto),
                                    COLOR_MARITIMO, "#059669"), unsafe_allow_html=True)
    with ca2:
        st.markdown(_tarjeta_kpi_bi("✈️", "Mercancía en Aeropuerto ahora", str(en_aeropuerto),
                                    COLOR_AEREO, "#1D4ED8"), unsafe_allow_html=True)
    st.caption("Llegada confirmada, todavía sin declarar ante Aduanas — la carga sigue físicamente ahí.")

    st.write("")
    with st.container(border=True):
        fig = _figura_heatmap_pais_categoria(universo_f)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_heatmap")
        else:
            st.caption("Sin suficiente cruce de país y categoría todavía.")

    st.write("")
    with st.container(border=True):
        fig = _figura_top_productos(universo_f)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_productos")
        else:
            st.caption("Sin descripciones suficientes para el top de productos.")

    st.write("")
    c3, c4 = st.columns(2)
    with c3:
        with st.container(border=True):
            fig = _figura_tendencia_mensual(mensual_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_tendencia")
            else:
                st.caption("Todavía no hay suficiente histórico mes a mes con estos filtros.")
    with c4:
        with st.container(border=True):
            fig = _figura_tiempo_puerto_categoria(dias_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_tiempo")
            else:
                st.caption("Todavía no hay embarques archivados con llegada y declaración para medir tiempo en puerto.")

    st.write("")
    c5, c6 = st.columns(2)
    with c5:
        with st.container(border=True):
            fig = _figura_ciclo_etapas(dias_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_ciclo")
            else:
                st.caption("Todavía no hay suficientes embarques con fechas completas para partir el ciclo en etapas.")
    with c6:
        with st.container(border=True):
            html_via = _tarjeta_via(dias_f)
            if html_via:
                st.markdown(html_via, unsafe_allow_html=True)
            else:
                st.caption("Falta variedad de Vía (Aéreo/Marítimo) en el histórico filtrado para comparar.")

    st.caption(f"Última actualización de los datos: {datos['hora'].strftime('%H:%M:%S')}")
