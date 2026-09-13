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

Rediseño (sep 2026): los tiempos que resume esta pestaña ahora usan MEDIANA en
vez de promedio (mismo criterio que ya usaba Herramientas: un embarque
trancado tres meses no debe mover el número de toda una categoría o vía), y
el ciclo completo llegada→almacén en vez de solo el tramo llegada→declaración
para las comparaciones de fondo (categoría más lenta, Aéreo vs Marítimo). El
"Cumplimiento SLA" pasó a leer el mismo umbral de sla_etapas() que ya colorea
las tarjetas del dashboard en vivo -- antes comparaba contra
costos_puerto()['umbral'], un valor que quedó huérfano cuando se retiró el
filtro de "atrasados" del panel operativo y que ningún otro lugar de la app
usa; los dos números podían no coincidir sin que nada lo explicara.

Nota sobre el clic-para-filtrar (on_select de st.plotly_chart, disponible
desde Streamlit 1.35+ y confirmado en la versión fijada, 1.61.0): clicar una
barra de país o una porción de categoría agrega ese valor al filtro efectivo
de TODO el panel, incluida la propia gráfica que se clicó -- es la forma más
simple y predecible de implementarlo sin duplicar el estado de cada gráfica
por separado. Para quitar la selección, se vuelve a clicar el mismo punto o
se usa el botón "Limpiar selección de gráficas".
"""

from __future__ import annotations

import re
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from sheets_io import (
    CACHE_TTL, COL_BL, COL_DESC, COL_ETA, COL_FECHA_ALMACEN, COL_FECHA_DECLARACION,
    COL_FECHA_LLEGADA_PUERTO, COL_FECHA_SALIDA, COL_PAIS, COL_VIA,
    MESES_ES_CORTO, NO_ESPECIFICADO, VIA_AEREA, VIA_MARITIMA,
    parsear_fecha, sla_etapas, unificar_paises,
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
    """Un renglón por embarque cerrado con las etapas del ciclo:
      dias_transito: Fecha_Salida -> Fecha_Llegada_Puerto (opcional: falta en
                     bastantes filas porque depende del booking del forwarder)
      dias          : Fecha_Llegada_Puerto -> Fecha_Declaracion
      dias_tramite  : Fecha_Declaracion -> Fecha_Almacen (siempre presente:
                     se graba solo al archivar)
      dias_total    : Fecha_Llegada_Puerto -> Fecha_Almacen, el ciclo COMPLETO
                     de puerto/aeropuerto. Se calcula directo contra Almacén
                     (no como dias + dias_tramite) para no perderlo si algún
                     registro viejo tiene declaración sin fecha de trámite
                     coherente. Es la base de las comparaciones de fondo
                     (categoría más lenta, Aéreo vs Marítimo): compararlas
                     solo por el tramo de declaración deja fuera el trámite
                     final, que es el tramo con dato más completo de los tres.
      Mes           : mes de Fecha_Almacen (mismo criterio que _mensual_historico,
                     que usa Fecha_Recibido = Fecha_Almacen por BASE_FECHA_RECIBIDO),
                     para las tendencias mensuales por vía."""
    columnas = ["dias", "dias_transito", "dias_tramite", "dias_total", "Categoria", "Pais", "Anio", "Via", "Mes"]
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
        dias_total = (almacen - llegada).days if almacen and almacen >= llegada else None
        # Mismo fallback que enriquecer() en logica.py para los activos: Vía
        # vacía + categoría 'Aéreos' se infiere aérea, en vez de caer por
        # default a Marítimo. En el histórico real, 26 de 31 filas archivadas
        # bajo 'Aéreos' no traían Via_Transporte -- sin esto, la comparación
        # Aéreo vs Marítimo cuenta la mayoría de lo aéreo como marítimo.
        via_cruda = str(r.get(COL_VIA, "") or "").strip()
        categoria_origen = r.get("Categoria_Origen", "") or ""
        es_aerea = es_aereo(via_cruda) or (not via_cruda and categoria_origen == "Aéreos")
        filas.append({
            "dias": (declaracion - llegada).days,
            "dias_transito": dias_transito,
            "dias_tramite": dias_tramite,
            "dias_total": dias_total,
            "Categoria": r.get("Categoria_Origen", "") or NO_ESPECIFICADO,
            "Pais": pais or NO_ESPECIFICADO,
            "Anio": llegada.year,
            "Via": VIA_AEREA if es_aerea else VIA_MARITIMA,
            "Mes": date(almacen.year, almacen.month, 1) if almacen else None,
        })
    return pd.DataFrame(filas, columns=columnas)


def _mediana_n(serie: pd.Series) -> tuple:
    """Mediana + cantidad de datos detrás. Se usa en vez de .mean() en toda
    esta pestaña por el mismo motivo que Herramientas: un solo embarque
    atípico (trancado meses) no debe mover el número que ve la presidencia."""
    limpio = pd.to_numeric(serie, errors="coerce").dropna()
    if limpio.empty:
        return None, 0
    return float(limpio.median()), int(len(limpio))


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
    """Mediana del ciclo COMPLETO (llegada→almacén) por categoría, no del
    tramo de declaración: mide qué categoría de carga tarda más en salir de
    puerto de punta a punta, que es la pregunta de negocio real."""
    base = dias_df[dias_df["Categoria"] != CATEGORIA_NO_PRODUCTO] if not dias_df.empty else dias_df
    if base.empty or "dias_total" not in base.columns or not base["dias_total"].notna().any():
        return None, None
    medianas = base.groupby("Categoria")["dias_total"].median().dropna().sort_values(ascending=False)
    return (medianas.index[0], medianas.iloc[0]) if len(medianas) else (None, None)


def _cumplimiento_sla(dias_df: pd.DataFrame):
    """% de embarques cuyo tramo llegada→declaración quedó dentro del SLA
    operativo de 'Recepción y declaración' (sla_etapas(), el mismo valor que
    colorea en vivo las tarjetas del dashboard). Antes comparaba contra
    costos_puerto()['umbral'] (5 días por defecto, sin relación con ningún
    otro criterio visible en la app); ahora los dos números miden lo mismo.
    Devuelve (pct, n, limite) -- el límite se necesita para rotular la
    tarjeta con el número real contra el que se está comparando."""
    if dias_df.empty or "dias" not in dias_df.columns:
        return None, 0, None
    limite = sla_etapas().get("Recepción y declaración")
    if limite is None:
        return None, 0, None
    base = dias_df["dias"].dropna()
    if base.empty:
        return None, 0, limite
    cumplidos = int((base <= limite).sum())
    return round(100 * cumplidos / len(base), 1), len(base), limite


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


_TOKEN_CON_DIGITO = re.compile(r"\d")

# Palabras de relleno que pueden quedar colgando entre el nombre del producto
# y su capacidad ('GENERADOR DE 600KW', 'GENERADOR 400KW / 500KW'): sin esto,
# el recorte se detenía en 'DE' o en '/' antes de llegar al nombre real.
_CONECTORES_RELLENO = {"DE", "PARA"}

# Variantes conocidas que sobreviven al recorte de sufijos porque la
# diferencia está en la palabra base, no en lo que le sigue (aquí: singular
# vs. plural). Se revisa contra el resultado YA recortado completo, nunca
# palabra por palabra, para no tocar frases donde 'GENERADORES' es parte
# legítima de algo más largo ('REPUESTOS PARA GENERADORES ELECTRICOS').
# Es una tabla chica a mano, no un normalizador lingüístico: crece según lo
# que se vaya viendo en el Sheet real, no intenta adivinar plurales en general.
_ALIAS_FAMILIA = {
    "GENERADORES": "GENERADOR",
}


def _familia_producto(desc: str) -> str:
    """Recorta del final de la descripción los tokens que traen un dígito
    pegado -- capacidad ('350KW'), serie o modelo ('6013918') -- y los
    conectores de relleno que puedan quedar colgando después de recortar
    ('DE', 'PARA', '/', '-'), para que 'GENERADOR', 'GENERADOR 350KW' y
    'GENERADOR DE 600KW' cuenten como el mismo producto en este ranking. Se
    detiene en el primer token que no sea ninguna de las dos cosas, así que
    una marca en medio ('EXCAVADORA CAT 320') queda como 'EXCAVADORA CAT', no
    se pierde. Nunca recorta hasta dejar la descripción vacía.

    Límite conocido: solo agrupa cuando el número y la unidad van pegados en
    un mismo token ('350KW'). Si en el Sheet real aparece con espacio
    ('350 KW'), esta regla no lo detecta -- no hay forma de saberlo sin ver
    el texto real, así que no se intentó adivinar un patrón más agresivo."""
    tokens = desc.split()
    while len(tokens) > 1:
        ultimo = tokens[-1].strip("/-,")
        if _TOKEN_CON_DIGITO.search(tokens[-1]) or ultimo in _CONECTORES_RELLENO or ultimo == "":
            tokens.pop()
            continue
        break
    resultado = " ".join(tokens) if tokens else desc
    return _ALIAS_FAMILIA.get(resultado, resultado)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_top_productos(universo: pd.DataFrame, n: int = 10) -> go.Figure | None:
    if universo.empty:
        return None
    serie = universo["Descripcion"].astype(str).str.strip()
    serie = serie[serie != ""].str.upper().map(_familia_producto)
    serie = serie.value_counts().head(n).sort_values()
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
    if dias_df.empty or "dias_total" not in dias_df.columns:
        return None
    base = dias_df[dias_df["Categoria"] != CATEGORIA_NO_PRODUCTO]
    agg = base.groupby("Categoria")["dias_total"].agg(mediana="median", n="count").dropna(subset=["mediana"])
    agg = agg.sort_values("mediana")
    if agg.empty:
        return None
    colores = [PALETA_BI[i % len(PALETA_BI)] for i in range(len(agg))]
    fig = go.Figure(data=[go.Bar(
        x=agg["mediana"].values, y=agg.index, orientation="h", marker=dict(color=colores, line=dict(width=0)),
        text=[f"{v:.0f} d" for v in agg["mediana"].values], textposition="outside", textfont=dict(size=12, family=_FUENTE),
        customdata=agg["n"].values,
        hovertemplate="<b>%{y}</b><br>%{x:.0f} días (mediana) · n=%{customdata}<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=max(240, 36 * len(agg)),
                      title=dict(text="🕐 Mediana de días en puerto por categoría (ciclo completo)"),
                      xaxis=dict(showgrid=True, gridcolor="#F1F5F9", title=""),
                      yaxis=dict(showgrid=False, title=""))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_ciclo_etapas(dias_df: pd.DataFrame) -> go.Figure | None:
    if dias_df.empty:
        return None
    etapas, valores, notas, colores = [], [], [], []
    med, n = _mediana_n(dias_df["dias_transito"])
    if med is not None:
        etapas.append("Tránsito<br>(salida → llegada)")
        valores.append(med)
        notas.append(f"n={n}")
        colores.append(PALETA_BI[0])
    med, n = _mediana_n(dias_df["dias"])
    if med is not None:
        etapas.append("En puerto<br>(llegada → declaración)")
        valores.append(med)
        notas.append(f"n={n}")
        colores.append(PALETA_BI[1])
    med, n = _mediana_n(dias_df["dias_tramite"])
    if med is not None:
        etapas.append("Trámite final<br>(declaración → almacén)")
        valores.append(med)
        notas.append(f"n={n}")
        colores.append(PALETA_BI[2])
    if len(etapas) < 2:
        return None
    fig = go.Figure(data=[go.Bar(
        x=etapas, y=valores, marker=dict(color=colores, line=dict(width=0)),
        text=[f"{v:.0f} d ({n})" for v, n in zip(valores, notas)], textposition="outside",
        textfont=dict(size=12, family=_FUENTE),
        hovertemplate="%{x}: %{y:.0f} días (mediana)<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=300,
                      title=dict(text="🔄 Ciclo completo por etapa (mediana de días)"),
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


def _tarjeta_ahora(en_puerto: int, en_aeropuerto: int) -> str:
    """Reemplaza las dos tarjetas KPI grandes de 'Mercancía en Puerto/Aeropuerto
    ahora': ese componente (_tarjeta_kpi_bi) se diseñó para 6 tarjetas angostas
    en fila -- estirado a solo 2 columnas de medio ancho cada una, quedaba
    mucho color plano y casi nada de información. Mismo estilo de barra
    compacta que ya usa _tarjeta_via, para que las dos cifras se lean juntas
    de un vistazo en vez de como dos bloques sueltos."""
    if en_puerto == 0 and en_aeropuerto == 0:
        return ""
    tope = max(en_puerto, en_aeropuerto, 1)
    piezas = ['<div class="paises"><div class="atttl">⚓✈️ Llegó, sin declarar todavía, ahora mismo</div>']
    for etiqueta, valor, color in (("En puerto", en_puerto, COLOR_MARITIMO),
                                   ("En aeropuerto", en_aeropuerto, COLOR_AEREO)):
        ancho = max(4.0, (valor / tope) * 100.0)
        piezas.append(
            f'<div class="pfila"><div class="pnom">{esc(etiqueta)}</div>'
            f'<div class="pbarra"><span style="width:{ancho:.1f}%;background:{color};border-radius:6px;"></span></div>'
            f'<div class="pval">{valor}</div></div>'
        )
    piezas.append("</div>")
    return "".join(piezas)


def _tarjeta_via(dias_df: pd.DataFrame) -> str:
    """Comparación Aéreo vs Marítimo del ciclo COMPLETO (llegada → almacén),
    con mediana en vez de promedio. Antes comparaba solo el tramo
    llegada→declaración -- una sola de las tres piezas del viaje, dejando
    fuera el trámite final, que es la que tiene el dato más completo."""
    if dias_df.empty or "dias_total" not in dias_df.columns or dias_df["Via"].nunique() < 2:
        return ""
    medianas, ns = {}, {}
    for via, grupo in dias_df.groupby("Via"):
        med, n = _mediana_n(grupo["dias_total"])
        if med is not None:
            medianas[via], ns[via] = med, n
    aereo, maritimo = medianas.get(VIA_AEREA), medianas.get(VIA_MARITIMA)
    if aereo is None or maritimo is None:
        return ""
    piezas = ['<div class="paises"><div class="atttl">✈️🚢 Ciclo completo, llegada → almacén (mediana): '
              'Aéreo vs Marítimo</div>']
    tope = max(aereo, maritimo) or 1
    for etiqueta, valor, color in ((VIA_AEREA, aereo, COLOR_AEREO), (VIA_MARITIMA, maritimo, COLOR_MARITIMO)):
        ancho = max(4.0, (valor / tope) * 100.0)
        piezas.append(
            f'<div class="pfila"><div class="pnom">{esc(etiqueta)} (n={ns[etiqueta]})</div>'
            f'<div class="pbarra"><span style="width:{ancho:.1f}%;background:{color};border-radius:6px;"></span></div>'
            f'<div class="pval">{valor:.0f} d</div></div>'
        )
    piezas.append("</div>")
    return "".join(piezas)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_distribucion_via(dias_df: pd.DataFrame) -> go.Figure | None:
    """Respaldo de _figura_tendencia_via_mensual para cuando no hay 2+ meses
    de datos: hoy los 18 embarques cerrados con ciclo completo caen todos en
    el mismo mes (septiembre 2026), así que ninguna tendencia mensual es
    posible todavía -- no es un bug, es que la fecha de almacén recién se
    empezó a capturar bien. Esto muestra la distribución real de cada
    embarque cerrado en vez de nada: además de la mediana (que ya da
    _tarjeta_via), se ve cuánto varía el ciclo dentro de cada vía -- un
    embarque atípico salta a la vista en vez de perderse en un promedio.
    Funciona con tan solo 1 embarque por vía; cuando haya 2+ meses reales,
    el panel vuelve a preferir la tendencia mensual sobre esto."""
    if dias_df.empty or "dias_total" not in dias_df.columns:
        return None
    base = dias_df.dropna(subset=["dias_total"])
    if base.empty or base["Via"].nunique() < 2:
        return None
    fig = go.Figure()
    for via, color in ((VIA_AEREA, COLOR_AEREO), (VIA_MARITIMA, COLOR_MARITIMO)):
        grupo = base[base["Via"] == via]
        if grupo.empty:
            continue
        fig.add_trace(go.Box(
            y=grupo["dias_total"], name=f"{via} (n={len(grupo)})",
            marker=dict(color=color, size=6), line=dict(color=color),
            fillcolor="rgba(0,0,0,0)", boxpoints="all", pointpos=0, jitter=0.45,
            hovertemplate="%{y:.0f} días<extra></extra>",
        ))
    if not fig.data:
        return None
    fig.update_layout(**{**_LAYOUT_BASE, "showlegend": False}, height=320,
                      title=dict(text="📦 Distribución del ciclo completo por embarque — Aéreo vs Marítimo"),
                      xaxis=dict(showgrid=False, title=""),
                      yaxis=dict(showgrid=True, gridcolor="#F1F5F9", title="Días", rangemode="tozero"))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_tendencia_via_mensual(dias_df: pd.DataFrame, meses: int = 18) -> go.Figure | None:
    """Lo que hoy no existe en esta pestaña y es la pregunta real detrás de
    'Aéreo vs Marítimo': no solo cuánto tarda cada uno AHORA, sino si esa
    brecha se está cerrando o abriendo mes a mes. Mediana mensual por vía,
    sobre el ciclo completo."""
    if dias_df.empty or "Mes" not in dias_df.columns or "dias_total" not in dias_df.columns:
        return None
    base = dias_df.dropna(subset=["Mes", "dias_total"])
    if base.empty or base["Via"].nunique() < 2:
        return None
    meses_disp = sorted(base["Mes"].unique())[-meses:]
    if len(meses_disp) < 2:
        return None
    etiquetas = [f"{MESES_ES_CORTO[m.month]} {m.year}" for m in meses_disp]
    fig = go.Figure()
    for via, color in ((VIA_AEREA, COLOR_AEREO), (VIA_MARITIMA, COLOR_MARITIMO)):
        grupo = base[base["Via"] == via]
        if grupo.empty:
            continue
        por_mes = grupo.groupby("Mes")["dias_total"]
        serie = por_mes.median().reindex(meses_disp)
        conteo = por_mes.count().reindex(meses_disp).fillna(0).astype(int)
        fig.add_trace(go.Scatter(
            x=etiquetas, y=serie.values, mode="lines+markers", name=via, connectgaps=True,
            line=dict(color=color, width=3), marker=dict(size=7, color=color),
            customdata=conteo.values,
            hovertemplate=f"<b>{via}</b> · %{{x}}<br>%{{y:.0f}} días (mediana) · n=%{{customdata}}<extra></extra>",
        ))
    if not fig.data:
        return None
    fig.update_layout(**{**_LAYOUT_BASE, "showlegend": True}, height=320,
                      title=dict(text="📉 Tendencia mensual del ciclo completo — Aéreo vs Marítimo"),
                      legend=dict(orientation="h", y=-0.22, font=dict(size=11, family=_FUENTE)),
                      xaxis=dict(showgrid=False, title="", tickangle=-45),
                      yaxis=dict(showgrid=True, gridcolor="#F1F5F9", title="Días (mediana)", rangemode="tozero"))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_retrasos_mensual(dias_df: pd.DataFrame, meses: int = 18) -> go.Figure | None:
    """El pilar de 'retrasos' que hoy no tiene tendencia en esta pestaña: qué
    porcentaje de lo cerrado cada mes superó el SLA de declaración (el mismo
    de sla_etapas() que colorea el dashboard en vivo). Un % puntual ya existe
    en la tarjeta de arriba; esto muestra si va mejorando o empeorando."""
    if dias_df.empty or "Mes" not in dias_df.columns or "dias" not in dias_df.columns:
        return None
    limite = sla_etapas().get("Recepción y declaración")
    if limite is None:
        return None
    base = dias_df.dropna(subset=["Mes", "dias"])
    if base.empty:
        return None
    meses_disp = sorted(base["Mes"].unique())[-meses:]
    if len(meses_disp) < 2:
        return None
    etiquetas = [f"{MESES_ES_CORTO[m.month]} {m.year}" for m in meses_disp]
    total = base.groupby("Mes").size().reindex(meses_disp, fill_value=0)
    fuera = base[base["dias"] > limite].groupby("Mes").size().reindex(meses_disp, fill_value=0)
    pct = [(100.0 * f / t) if t > 0 else None for f, t in zip(fuera, total)]
    colores = ["#9CA3AF" if v is None else "#EF4444" if v > 30 else "#F59E0B" if v > 10 else "#10B981"
              for v in pct]
    fig = go.Figure(data=[go.Bar(
        x=etiquetas, y=pct, marker=dict(color=colores),
        text=[f"{v:.0f}" if v is not None else "" for v in pct], textposition="outside",
        textfont=dict(size=12, family=_FUENTE),
        customdata=total.values,
        hovertemplate="%{x}<br>%{y:.0f} de cada 100 fuera de SLA · n=%{customdata}<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=300,
                      title=dict(text=f"🚨 % fuera del SLA de declaración (>{limite}d) por mes"),
                      xaxis=dict(showgrid=False, title="", tickangle=-45),
                      yaxis=dict(showgrid=True, gridcolor="#F1F5F9", title="%", rangemode="tozero"))
    return fig


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
    dias_mediana, dias_n = _mediana_n(dias_f["dias_total"]) if "dias_total" in dias_f.columns else (None, 0)
    cat_lenta, dias_lenta = _categoria_mas_lenta(dias_f)
    sla_pct, sla_n, sla_limite = _cumplimiento_sla(dias_f)

    st.write("")
    with st.container(key="bikpirow"):
        cols = st.columns(6)
        tarjetas = [
            ("📦", "Embarques recibidos", str(len(mensual_f)), "#059669", "#10B981"),
            ("🌍", "País principal", pais_top.iloc[0] if len(pais_top) else "—", "#0284C7", "#38BDF8"),
            ("🏷️", "Categoría principal", cat_top.iloc[0] if len(cat_top) else "—", "#1E3A5F", "#0C4A6E"),
            ("⏱️", "Días en puerto (mediana)",
             f"{dias_mediana:.0f} d · n={dias_n}" if dias_mediana is not None else "—",
             "#B45309", "#F59E0B"),
            ("🐢", "Categoría más lenta",
             f"{cat_lenta} · {dias_lenta:.0f} d" if cat_lenta else "—",
             "#B91C1C", "#EF4444"),
            ("✅", f"SLA declaración (≤{sla_limite}d)" if sla_limite is not None else "SLA declaración",
             f"{sla_pct}% ({sla_n})" if sla_pct is not None else "—",
             "#0F766E", "#14B8A6"),
        ]
        for col, (icono, label, valor, ca, cb) in zip(cols, tarjetas):
            with col:
                st.markdown(_tarjeta_kpi_bi(icono, label, valor, ca, cb), unsafe_allow_html=True)
    st.caption("Días en puerto y categoría más lenta: mediana del ciclo completo (llegada → almacén). "
              "SLA declaración: % que cerró el tramo llegada → declaración dentro del mismo umbral que "
              "colorea las tarjetas del dashboard en vivo.")

    st.write("")
    html_ahora = _tarjeta_ahora(en_puerto, en_aeropuerto)
    with st.container(border=True):
        if html_ahora:
            st.markdown(html_ahora, unsafe_allow_html=True)
            st.caption("Llegada confirmada, todavía sin declarar ante Aduanas — la carga sigue físicamente ahí.")
        else:
            st.caption("Nada llegado a puerto o aeropuerto esperando declarar en este momento.")

    # --- Bloque operativo: vía y retrasos, lo que de verdad responde "cómo -
    # va la operación" para la presidencia. Sube por encima de lo descriptivo
    # (país/categoría/producto), que es más útil al equipo de Logística que a
    # quien dirige la empresa. ---
    st.write("")
    cv1, cv2 = st.columns(2)
    with cv1:
        with st.container(border=True):
            html_via = _tarjeta_via(dias_f)
            if html_via:
                st.markdown(html_via, unsafe_allow_html=True)
            else:
                st.caption("Falta variedad de Vía (Aéreo/Marítimo) en el histórico filtrado para comparar.")
    with cv2:
        with st.container(border=True):
            fig = _figura_tendencia_via_mensual(dias_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_via_tendencia")
            else:
                fig = _figura_distribucion_via(dias_f)
                if fig:
                    st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_via_distribucion")
                else:
                    st.caption("Todavía no hay embarques cerrados de ambas vías para comparar.")

    st.write("")
    cr1, cr2 = st.columns(2)
    with cr1:
        with st.container(border=True):
            fig = _figura_retrasos_mensual(dias_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_retrasos")
            else:
                st.caption("Todavía no hay al menos 2 meses de histórico para trazar retrasos.")
    with cr2:
        with st.container(border=True):
            fig = _figura_ciclo_etapas(dias_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_ciclo")
            else:
                st.caption("Todavía no hay suficientes embarques con fechas completas para partir el ciclo en etapas.")

    # --- Bloque descriptivo: "qué se importa". Útil para el equipo, menos
    # accionable para la presidencia -- por eso queda debajo del bloque
    # operativo de arriba. ---
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

    st.caption(f"Última actualización de los datos: {datos['hora'].strftime('%H:%M:%S')}")
