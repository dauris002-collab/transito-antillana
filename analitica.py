"""
analitica.py — Analítica de importaciones para Antillana Comercial.

Panel de gráficas de negocio (no operativo), con acabado tipo BI: filtros
interactivos (segmented_control para Año, multiselects para País/Categoría,
clic-para-filtrar en las gráficas de País y Categoría), barra visible de
filtros activos con limpieza total de un toque, tarjetas KPI con tendencia,
donas con total al centro, área bajo la línea de tendencia, cada gráfica en
su propia tarjeta con título y la pregunta de negocio que responde. No es una
fuente de datos aparte: se arma sobre lo mismo que ya trae cargar_todo()
(activos + histórico). Sin escrituras: todo aquí es de solo lectura.

Pagos (aduanas, costos) se dejó AFUERA a propósito: todavía hay muy poca data
registrada en esa pestaña como para que un promedio signifique algo. (Sep
2026: se mantiene el criterio — se vuelve a evaluar cuando haya más data.)

Tiempos con MEDIANA en vez de promedio (mismo criterio que Herramientas: un
embarque trancado tres meses no debe mover el número de toda una categoría o
vía), y el ciclo completo llegada→almacén en vez de solo el tramo
llegada→declaración para las comparaciones de fondo (categoría más lenta,
Aéreo vs Marítimo). Las gráficas de "retrasos" basadas en el umbral de días
de sla_etapas() se quitaron: Logística no lo usa como criterio real, así que
mostrar un % de incumplimiento contra ese número sería más ruido que señal.

Fecha_Almacen (para dias_total y Mes) tiene respaldo en Fecha_Recibido cuando
falta: julio y agosto (29 de 47 filas del histórico real) se archivaron antes
de que esa columna se empezara a llenar, así que sin el respaldo el ciclo
completo y la comparación de vía solo reflejaban septiembre. Ver AlmacenAprox
en _ciclo_historico().

Clic-para-filtrar (on_select de st.plotly_chart, Streamlit 1.35+): clicar una
barra de país o una porción de categoría agrega ese valor al filtro efectivo
de TODO el panel — EXCEPTO de las dos gráficas clicables mismas, que se
dibujan solo con los filtros de menú para que siempre se vean todas las
opciones y se pueda seguir agregando valores con clic (si se auto-filtraran,
clicar "China" dejaría solo a China visible y no habría cómo sumar otro país
desde la gráfica). La selección se lee del estado de sesión ANTES de dibujar
nada, así que la barra de "filtros activos" la muestra igual que los filtros
de menú, y valores que ya no existen en la data (un país que se borró del
Sheet, p.ej.) se descartan en vez de filtrar en silencio. Para quitar la
selección: clic de nuevo sobre el mismo punto, o "Limpiar todos los filtros".

Rediseño visual (sep 2026): títulos y subtítulos FUERA de la figura Plotly
(tipografía HTML nítida, estilo tarjeta de Power BI/Tableau), un solo color de
acento por gráfica de una sola medida en vez de arcoíris por barra, esquinas
redondeadas en barras, rejillas apenas visibles solo en el eje de valores,
tooltip oscuro unificado, y KPIs con la tendencia en pastilla.
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
    parsear_fecha, unificar_paises,
)
from logica import ETAPAS_PUERTO, es_aereo
from ui_componentes import _enriquecer_cacheado, esc


# Paleta categórica para las gráficas donde el color DISTINGUE categorías
# (dona de categoría). Para gráficas de una sola medida se usa UN acento —
# el arcoíris por barra es lo que hacía verse amateur; en BI el color solo se
# gasta cuando codifica información.
PALETA_BI = ["#2563EB", "#F59E0B", "#10B981", "#8B5CF6", "#EF4444",
            "#0EA5E9", "#EC4899", "#14B8A6", "#F97316", "#6366F1"]

COLOR_AEREO = "#2563EB"
COLOR_MARITIMO = "#10B981"

# Acentos de una sola medida
_ACCENT_AZUL = "#2563EB"      # volumen / conteos
_ACCENT_AMBAR = "#F59E0B"     # tiempos / atención
_ACCENT_VERDE = "#10B981"     # productos
_AZUL_SUAVE = "rgba(37,99,235,0.10)"

# "Aéreos" sigue siendo una pestaña real del Sheet (y se puede filtrar por
# ella), pero NO es un tipo de producto -- es un modo de transporte, igual que
# Vía. Por eso se excluye de los gráficos que responden "qué se importa" y de
# "categoría con más días en puerto": mezclarla ahí haría ver como si "cargar
# por avión" fuera una categoría de mercancía, que no lo es. Esa dimensión ya
# tiene su propio gráfico (Aéreo vs Marítimo, y las tarjetas de "ahora mismo").
CATEGORIA_NO_PRODUCTO = "Aéreos"

_FUENTE = "Segoe UI, -apple-system, BlinkMacSystemFont, sans-serif"

_TXT_FUERTE = "#111827"
_TXT = "#374151"
_TXT_SUAVE = "#6B7280"
_REJILLA = "#EEF2F7"

_LAYOUT_BASE = dict(
    margin=dict(t=8, b=6, l=8, r=14),
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color=_TXT, size=12, family=_FUENTE),
    showlegend=False, dragmode=False,
    hoverlabel=dict(bgcolor=_TXT_FUERTE, bordercolor=_TXT_FUERTE,
                    font=dict(color="#F9FAFB", size=12.5, family=_FUENTE)),
)


def _eje_valores(**extra) -> dict:
    """Eje de la medida: rejilla apenas visible, sin línea de eje, desde cero.
    Los overrides llegan como kwargs y se funden sobre los defaults."""
    eje = dict(showgrid=True, gridcolor=_REJILLA, zeroline=False, title="",
               tickfont=dict(size=11, color=_TXT_SUAVE), rangemode="tozero")
    eje.update(extra)
    return eje


def _eje_categorias(**extra) -> dict:
    """Eje de las etiquetas: sin rejilla, texto un poco más fuerte.
    Los overrides llegan como kwargs y se funden sobre los defaults."""
    eje = dict(showgrid=False, zeroline=False, title="",
               tickfont=dict(size=12, color=_TXT))
    eje.update(extra)
    return eje


def _encabezado_grafica(icono: str, titulo: str, detalle: str = ""):
    """Título de tarjeta estilo BI: fuera de la figura Plotly, con la pregunta
    de negocio que responde como subtítulo. La tipografía HTML es más nítida
    que la del canvas de Plotly y deja el área del gráfico solo para datos."""
    sub = (f'<div style="font-size:0.76rem; color:{_TXT_SUAVE}; margin-top:1px;">{esc(detalle)}</div>'
           if detalle else "")
    st.markdown(
        f'<div style="margin:2px 0 6px;">'
        f'<span style="font-size:0.95rem;">{icono}</span> '
        f'<span style="font-size:0.9rem; font-weight:700; color:{_TXT_FUERTE};">{esc(titulo)}</span>'
        f"{sub}</div>",
        unsafe_allow_html=True,
    )


def _titulo_seccion(texto: str):
    """Separador de bloque en versalitas grises, como los encabezados de grupo
    de un dashboard BI."""
    st.markdown(
        f'<div style="font-size:0.7rem; font-weight:800; letter-spacing:0.09em; '
        f'text-transform:uppercase; color:{_TXT_SUAVE}; margin:14px 0 2px;">{esc(texto)}</div>',
        unsafe_allow_html=True,
    )


def _margen_izquierdo(etiquetas, base: int = 14) -> int:
    """Margen izquierdo según el largo de la etiqueta más larga. Con l=8 fijo,
    Plotly a veces recorta las etiquetas del eje Y (barras horizontales,
    heatmap); calculado a mano se ve igual en el navegador y en la exportación."""
    tope = max((len(str(e)) for e in etiquetas), default=0)
    return min(190, base + tope * 7)


def _config_interactiva() -> dict:
    """A diferencia de las mini-gráficas de tránsito (donde staticPlot evita
    redibujar decenas de figuras por pantalla), aquí solo hay un puñado de
    gráficas grandes: sí vale la pena el hover y el zoom reales."""
    return {"displayModeBar": False, "responsive": True, "scrollZoom": False}


def _mes_de(valor) -> date | None:
    f = parsear_fecha(valor)
    return date(f.year, f.month, 1) if f else None


def _tarjeta_kpi_bi(icono: str, label: str, valor: str, color_a: str, color_b: str, delta: str = "") -> str:
    """Tarjeta KPI propia de esta pestaña: degradado, ícono y valor grande --
    aquí es el resumen ejecutivo, así que puede pesar más visualmente. La
    tendencia ('delta') va en una pastilla semitransparente: el texto suelto
    sobre el degradado se perdía, y un color semántico (verde/rojo) encima de
    un fondo ya de color es más ruido que señal -- qué dirección es "mejor"
    se explica en el caption debajo de la fila, no en la tarjeta misma."""
    delta_html = (
        f'<div style="display:inline-block; background:rgba(255,255,255,0.22); '
        f'border-radius:999px; padding:2px 10px; font-size:0.62rem; font-weight:600; '
        f'color:#fff; margin-top:6px;">{esc(delta)}</div>'
    ) if delta else ""
    return (
        f'<div style="background:linear-gradient(135deg,{color_a} 0%,{color_b} 100%); '
        f'border-radius:14px; padding:15px 12px 13px; min-height:112px; '
        f'box-shadow:0 2px 10px rgba(17,24,39,0.10); display:flex; flex-direction:column; '
        f'align-items:center; justify-content:center; text-align:center;">'
        f'<div style="font-size:1.25rem; line-height:1; opacity:0.95;">{icono}</div>'
        f'<div style="font-size:1.45rem; font-weight:800; color:#fff; margin-top:4px; '
        f'font-family:{_FUENTE}; letter-spacing:-0.01em;">{esc(str(valor))}</div>'
        f'<div style="font-size:0.64rem; font-weight:700; letter-spacing:0.05em; '
        f'text-transform:uppercase; color:rgba(255,255,255,0.92); margin-top:3px;">{esc(label)}</div>'
        f'{delta_html}'
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
      dias_tramite  : Fecha_Declaracion -> Almacén (ver AlmacenAprox abajo)
      dias_total    : Fecha_Llegada_Puerto -> Almacén, el ciclo COMPLETO de
                     puerto/aeropuerto. Se calcula directo contra Almacén (no
                     como dias + dias_tramite) para no perderlo si algún
                     registro viejo tiene declaración sin fecha de trámite
                     coherente. Es la base de las comparaciones de fondo
                     (categoría más lenta, Aéreo vs Marítimo): compararlas
                     solo por el tramo de declaración deja fuera el trámite
                     final, que es el tramo con dato más completo de los tres.
      Mes           : mes de Almacén, para las tendencias mensuales por vía.
      AlmacenAprox  : True si Fecha_Almacen venía vacía y se usó Fecha_Recibido
                     como respaldo -- en el histórico real, julio y agosto
                     (29 de 47 filas cerradas) no tienen Fecha_Almacen porque
                     esa columna se empezó a llenar más adelante; sin este
                     respaldo, todo lo que mide 'días en puerto' (incluida la
                     comparación Aéreo vs Marítimo) ignoraba julio y agosto por
                     completo y solo reflejaba septiembre. Fecha_Recibido es
                     razonable como respaldo porque, por diseño de la app
                     (BASE_FECHA_RECIBIDO='almacen'), se graba igual a Almacén
                     al archivar -- pero en un puñado de filas muy viejas
                     (de antes de ese criterio) puede reflejar en cambio la
                     llegada; ahí el candado de orden (almacén >= declaración)
                     ya descarta el dato en vez de dar un número incoherente."""
    columnas = ["dias", "dias_transito", "dias_tramite", "dias_total", "Categoria", "Pais", "Anio", "Via", "Mes",
               "AlmacenAprox"]
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
        almacen_real = parsear_fecha(r.get(COL_FECHA_ALMACEN, ""))
        almacen_aprox_val = None if almacen_real else parsear_fecha(r.get("Fecha_Recibido", ""))
        almacen = almacen_real or almacen_aprox_val
        almacen_es_aprox = almacen_real is None and almacen_aprox_val is not None
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
            "AlmacenAprox": almacen_es_aprox if dias_total is not None else None,
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


def _ultimos_dos_meses(df: pd.DataFrame) -> tuple:
    """Los 2 meses más recientes con datos en df, o (None, None) si hay menos
    de 2. Sirve para las flechas de tendencia de los KPI: 'este mes' se
    compara siempre contra el mes inmediato anterior CON datos, sin importar
    el filtro de Año activo en pantalla -- el filtro de Año decide qué años
    entran al total que se muestra, la flecha siempre mira el pulso más
    reciente disponible dentro de eso."""
    if df.empty or "Mes" not in df.columns:
        return None, None
    meses = sorted(df["Mes"].dropna().unique())
    if len(meses) < 2:
        return None, None
    return meses[-1], meses[-2]


def _flecha(delta, decimales: int = 0, sufijo: str = "") -> str:
    """Texto de tendencia consistente para las tarjetas KPI. Sin flecha
    (string vacío) si no hay 2 meses para comparar -- nunca se inventa una
    tendencia con un solo dato."""
    if delta is None:
        return ""
    if abs(delta) < (10 ** (-decimales)) / 2:
        return "→ sin cambio vs mes ant."
    signo = "▲" if delta > 0 else "▼"
    return f"{signo} {abs(delta):.{decimales}f}{sufijo} vs mes ant."


def _tendencias_kpi(mensual_f: pd.DataFrame, dias_f: pd.DataFrame) -> dict:
    """Delta mes-actual-con-datos vs mes-anterior-con-datos para los KPI que
    sí tienen una dirección clara de mejor/peor. País principal, Categoría
    principal y Categoría más lenta se quedan sin flecha a propósito: son
    etiquetas, o pueden cambiar de cuál es la protagonista de un mes a otro
    (la categoría más lenta de agosto no tiene por qué ser la misma de
    septiembre) -- una flecha ahí compararía cosas distintas disfrazada de
    tendencia, que es peor que no mostrar nada."""
    resultado = {"conteo": None, "dias_puerto": None, "dias_aeropuerto": None}

    ult, pen = _ultimos_dos_meses(mensual_f)
    if ult is not None:
        resultado["conteo"] = (int((mensual_f["Mes"] == ult).sum())
                               - int((mensual_f["Mes"] == pen).sum()))

    ult, pen = _ultimos_dos_meses(dias_f)
    if ult is not None:
        base_ult, base_pen = dias_f[dias_f["Mes"] == ult], dias_f[dias_f["Mes"] == pen]
        for clave, via in (("dias_puerto", VIA_MARITIMA), ("dias_aeropuerto", VIA_AEREA)):
            med_ult, _ = _mediana_n(base_ult.loc[base_ult["Via"] == via, "dias_total"])
            med_pen, _ = _mediana_n(base_pen.loc[base_pen["Via"] == via, "dias_total"])
            if med_ult is not None and med_pen is not None:
                resultado[clave] = med_ult - med_pen

    return resultado


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
    """Cuánta mercancía llegó y sigue sin declarar, AHORA MISMO. El filtro de
    Año no aplica aquí a propósito: es una foto del presente, no un corte
    histórico -- filtrarla por 2025 la vaciaría sin que eso signifique nada."""
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


def _puntos_clicados(evento, campo_preferido: str = "y") -> list:
    """Extrae las etiquetas de los puntos clicados en una gráfica con
    on_select='rerun'. Barras horizontales guardan la categoría en 'y'; donas
    la guardan en 'label'. Se revisan varias claves porque el nombre exacto
    depende del tipo de traza, y prefiero tolerar variaciones a que un cambio
    menor de Plotly rompa el clic en silencio. Acepta tanto el evento que
    devuelve st.plotly_chart como el valor guardado en st.session_state (son
    el mismo objeto), con duck-typing en vez de isinstance: el tipo concreto
    lo define Streamlit y no quiero que un cambio de clase apague el filtro."""
    if evento is None or not hasattr(evento, "get"):
        return []
    puntos = (evento.get("selection", {}) or {}).get("points", [])
    if not isinstance(puntos, list):
        return []
    valores = []
    for p in puntos:
        if not hasattr(p, "get"):
            continue
        v = p.get(campo_preferido) or p.get("label") or p.get("y") or p.get("x")
        if v:
            valores.append(str(v))
    return valores


def _clics_vigentes(clave_estado: str, campo: str, opciones: list) -> list:
    """Selección por clic guardada en session_state, depurada contra la data
    actual: un valor que ya no existe (país borrado del Sheet, categoría
    renombrada) NO debe seguir filtrando en silencio -- esa era una de las
    formas en que el panel 'se quedaba pegado' en un filtro invisible. Si no
    queda nada válido, la clave se borra del estado para que la gráfica tampoco
    siga mostrando un resaltado fantasma."""
    crudos = _puntos_clicados(st.session_state.get(clave_estado), campo)
    if not crudos:
        return []
    validos = [v for v in crudos if v in opciones]
    if not validos:
        st.session_state.pop(clave_estado, None)
    return validos


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
    fig = go.Figure(data=[go.Bar(
        x=serie.values, y=serie.index, orientation="h",
        marker=dict(color=_ACCENT_AZUL, cornerradius=5, line=dict(width=0)),
        text=serie.values, textposition="outside", cliponaxis=False,
        textfont=dict(size=12, color=_TXT, family=_FUENTE),
        hovertemplate="<b>%{y}</b><br>%{x} embarque(s)<extra></extra>",
    )])
    layout = {**_LAYOUT_BASE, "margin": {**_LAYOUT_BASE["margin"],
              "l": _margen_izquierdo(serie.index)}}
    fig.update_layout(**layout, height=max(250, 42 * len(serie)),
                      xaxis=_eje_valores(),
                      yaxis=_eje_categorias())
    # Espacio a la derecha para que la etiqueta del valor no se corte
    fig.update_xaxes(range=[0, serie.max() * 1.15])
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
        labels=serie.index, values=serie.values, hole=0.62, sort=False,
        marker=dict(colors=[PALETA_BI[i % len(PALETA_BI)] for i in range(len(serie))],
                   line=dict(color="#FFFFFF", width=2)),
        textinfo="percent", textfont=dict(size=11.5, family=_FUENTE, color="#fff"),
        insidetextorientation="radial",
        hovertemplate="<b>%{label}</b><br>%{value} embarque(s) · %{percent}<extra></extra>",
    )])
    fig.add_annotation(text=f"<b>{total}</b><br><span style='font-size:11px;'>embarques</span>",
                       x=0.5, y=0.5, showarrow=False,
                       font=dict(size=17, family=_FUENTE, color=_TXT_FUERTE))
    fig.update_layout(**{**_LAYOUT_BASE, "showlegend": True}, height=360,
                      legend=dict(orientation="h", y=-0.12,
                                  font=dict(size=11, family=_FUENTE, color=_TXT)))
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
    fig = go.Figure(data=[go.Bar(
        x=serie.values, y=serie.index, orientation="h",
        marker=dict(color=_ACCENT_VERDE, cornerradius=5, line=dict(width=0)),
        text=serie.values, textposition="outside", cliponaxis=False,
        textfont=dict(size=12, color=_TXT, family=_FUENTE),
        hovertemplate="<b>%{y}</b><br>%{x} embarque(s)<extra></extra>",
    )])
    layout = {**_LAYOUT_BASE, "margin": {**_LAYOUT_BASE["margin"],
              "l": _margen_izquierdo(serie.index)}}
    fig.update_layout(**layout, height=max(300, 42 * len(serie)),
                      xaxis=_eje_valores(),
                      yaxis=_eje_categorias())
    fig.update_xaxes(range=[0, serie.max() * 1.15])
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
        line=dict(color=_ACCENT_AZUL, width=3, shape="spline", smoothing=0.4),
        marker=dict(size=7, color="#FFFFFF", line=dict(color=_ACCENT_AZUL, width=2.5)),
        fill="tozeroy", fillcolor=_AZUL_SUAVE,
        text=conteo.values, textposition="top center",
        textfont=dict(size=11, color=_TXT, family=_FUENTE),
        hovertemplate="<b>%{x}</b><br>%{y} embarque(s) recibido(s)<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=300,
                      xaxis=_eje_categorias(tickangle=-45,
                                            tickfont=dict(size=11, color=_TXT_SUAVE)),
                      yaxis=_eje_valores())
    fig.update_yaxes(range=[0, conteo.max() * 1.25 + 1])
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
    fig = go.Figure(data=[go.Bar(
        x=agg["mediana"].values, y=agg.index, orientation="h",
        marker=dict(color=_ACCENT_AMBAR, cornerradius=5, line=dict(width=0)),
        text=[f"{v:.0f} d" for v in agg["mediana"].values], textposition="outside", cliponaxis=False,
        textfont=dict(size=12, color=_TXT, family=_FUENTE),
        customdata=agg["n"].values,
        hovertemplate="<b>%{y}</b><br>%{x:.0f} días (mediana) · n=%{customdata}<extra></extra>",
    )])
    layout = {**_LAYOUT_BASE, "margin": {**_LAYOUT_BASE["margin"],
              "l": _margen_izquierdo(agg.index)}}
    fig.update_layout(**layout, height=max(250, 42 * len(agg)),
                      xaxis=_eje_valores(),
                      yaxis=_eje_categorias())
    fig.update_xaxes(range=[0, agg["mediana"].max() * 1.18])
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
        colores.append(_ACCENT_AZUL)
    med, n = _mediana_n(dias_df["dias"])
    if med is not None:
        etapas.append("En puerto<br>(llegada → declaración)")
        valores.append(med)
        notas.append(f"n={n}")
        colores.append(_ACCENT_AMBAR)
    med, n = _mediana_n(dias_df["dias_tramite"])
    if med is not None:
        etapas.append("Trámite final<br>(declaración → almacén)")
        valores.append(med)
        notas.append(f"n={n}")
        colores.append(_ACCENT_VERDE)
    if len(etapas) < 2:
        return None
    fig = go.Figure(data=[go.Bar(
        x=etapas, y=valores,
        marker=dict(color=colores, cornerradius=7, line=dict(width=0)),
        text=[f"{v:.0f} d · {n}" for v, n in zip(valores, notas)], textposition="outside", cliponaxis=False,
        textfont=dict(size=12.5, color=_TXT_FUERTE, family=_FUENTE),
        hovertemplate="%{x}<br>%{y:.0f} días (mediana)<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=300,
                      xaxis=_eje_categorias(),
                      yaxis=_eje_valores())
    fig.update_yaxes(range=[0, max(valores) * 1.22])
    return fig


_ESCALA_AZULES = [[0.0, "#F8FAFC"], [0.3, "#DBEAFE"], [0.65, "#93C5FD"], [1.0, "#2563EB"]]


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
        z=cruce.values, x=cruce.columns, y=cruce.index, colorscale=_ESCALA_AZULES,
        showscale=False, xgap=3, ygap=3,
        hovertemplate="<b>%{y}</b> desde <b>%{x}</b>: %{z} embarque(s)<extra></extra>",
    ))
    # Anotaciones por celda (en vez de texttemplate) para elegir el color del
    # número según qué tan oscura quedó la celda: blanco sobre azul fuerte,
    # tinta sobre azul claro. Con textfont único siempre había un extremo
    # ilegible.
    tope = max(1, int(cruce.values.max()))
    for i, cat in enumerate(cruce.index):
        for j, pais in enumerate(cruce.columns):
            v = int(cruce.iloc[i, j])
            if v == 0:
                continue
            fig.add_annotation(
                x=pais, y=cat, text=str(v), showarrow=False,
                font=dict(size=12, family=_FUENTE,
                          color="#FFFFFF" if v / tope > 0.55 else _TXT_FUERTE),
            )
    layout = {**_LAYOUT_BASE, "font": dict(color=_TXT, size=11.5, family=_FUENTE),
              "margin": {**_LAYOUT_BASE["margin"], "l": _margen_izquierdo(cats_top)}}
    fig.update_layout(**layout,
                      height=max(300, 44 * len(cats_top) + 60),
                      xaxis=dict(showgrid=False, title="", side="bottom", tickangle=-45,
                                 tickfont=dict(size=11, color=_TXT_SUAVE)),
                      yaxis=dict(showgrid=False, title="", autorange="reversed",
                                 tickfont=dict(size=11.5, color=_TXT)))
    return fig


def _tarjeta_ahora(en_puerto: int, en_aeropuerto: int) -> str:
    """Barra compacta con lo que llegó y sigue sin declarar, ahora mismo.
    Reusa las clases CSS de tránsito (.paises/.pfila/...) para que las dos
    cifras se lean juntas de un vistazo."""
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
    con mediana en vez de promedio."""
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
    de datos: hoy los embarques cerrados con ciclo completo se concentran en
    muy pocos meses, así que ninguna tendencia mensual es posible todavía --
    no es un bug, es que la fecha de almacén recién se empezó a capturar bien.
    Esto muestra la distribución real de cada embarque cerrado en vez de nada:
    además de la mediana (que ya da _tarjeta_via), se ve cuánto varía el ciclo
    dentro de cada vía -- un embarque atípico salta a la vista en vez de
    perderse en un promedio. Funciona con tan solo 1 embarque por vía; cuando
    haya 2+ meses reales, el panel vuelve a preferir la tendencia mensual
    sobre esto."""
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
        rgb = "37,99,235" if via == VIA_AEREA else "16,185,129"
        fig.add_trace(go.Box(
            y=grupo["dias_total"], name=f"{via} (n={len(grupo)})",
            marker=dict(color=color, size=6, opacity=0.75), line=dict(color=color, width=2),
            fillcolor=f"rgba({rgb},0.14)", boxpoints="all", pointpos=0, jitter=0.45,
            hovertemplate="%{y:.0f} días<extra></extra>",
        ))
    if not fig.data:
        return None
    fig.update_layout(**{**_LAYOUT_BASE, "showlegend": False}, height=320,
                      xaxis=_eje_categorias(),
                      yaxis=_eje_valores(title=dict(text="Días", font=dict(size=11, color=_TXT_SUAVE))))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_tendencia_via_mensual(dias_df: pd.DataFrame, meses: int = 18) -> go.Figure | None:
    """La pregunta real detrás de 'Aéreo vs Marítimo': no solo cuánto tarda
    cada uno AHORA, sino si esa brecha se está cerrando o abriendo mes a mes.
    Mediana mensual por vía, sobre el ciclo completo."""
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
            line=dict(color=color, width=3, shape="spline", smoothing=0.3),
            marker=dict(size=7, color="#FFFFFF", line=dict(color=color, width=2.5)),
            customdata=conteo.values,
            hovertemplate=f"<b>{via}</b> · %{{x}}<br>%{{y:.0f}} días (mediana) · n=%{{customdata}}<extra></extra>",
        ))
    if not fig.data:
        return None
    fig.update_layout(**{**_LAYOUT_BASE, "showlegend": True}, height=320,
                      legend=dict(orientation="h", y=-0.28,
                                  font=dict(size=11.5, family=_FUENTE, color=_TXT)),
                      xaxis=_eje_categorias(tickangle=-45,
                                            tickfont=dict(size=11, color=_TXT_SUAVE)),
                      yaxis=_eje_valores(title=dict(text="Días (mediana)",
                                                    font=dict(size=11, color=_TXT_SUAVE))))
    return fig


# ---------------------------------------------------------------------------
# PANEL
# ---------------------------------------------------------------------------
def _chips_filtros(anios_sel: list, paises_menu: list, cats_menu: list,
                   paises_click: list, cats_click: list) -> str:
    """HTML de la barra 'filtros activos': cada filtro que está recortando el
    panel, visible de un vistazo. Sin esto, una selección por clic (o un año
    elegido hace tres semanas) quedaba activa sin que se notara -- la causa
    principal de que el panel 'se quedara pegado' para quien lo miraba."""
    chips = []
    for etiqueta, valores in (("Año", anios_sel), ("País", paises_menu), ("Categoría", cats_menu),
                              ("Clic en país", paises_click), ("Clic en categoría", cats_click)):
        for v in valores:
            chips.append(
                f'<span style="display:inline-block; background:#EFF6FF; color:#1D4ED8; '
                f'border:1px solid #BFDBFE; font-size:0.72rem; font-weight:600; '
                f'padding:3px 11px; border-radius:999px; margin:2px 4px 2px 0;">'
                f'{esc(etiqueta)}: {esc(str(v))}</span>'
            )
    if not chips:
        return ""
    return ('<div style="margin:2px 0 4px;">'
            f'<span style="font-size:0.72rem; font-weight:800; letter-spacing:0.07em; '
            f'text-transform:uppercase; color:{_TXT_SUAVE}; margin-right:6px;">Filtros activos</span>'
            + "".join(chips) + "</div>")


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

    # La selección por clic se lee del estado de sesión ANTES de dibujar las
    # gráficas clicables: así la barra de filtros activos la muestra igual que
    # cualquier filtro de menú, y los valores que ya no existen en la data se
    # descartan aquí en vez de filtrar en silencio.
    paises_click = _clics_vigentes("an_paises", "y", paises_disp)
    cats_click = _clics_vigentes("an_categorias", "label", cats_disp)

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

    paises_sel = sorted(set(paises_sel_menu) | set(paises_click))
    cats_sel = sorted(set(cats_sel_menu) | set(cats_click))

    # Barra de filtros activos + limpieza TOTAL de un toque (menús y clics).
    # Antes solo había un botón para los clics y ningún resumen visible: si un
    # filtro quedaba puesto, la única pista era que los números "no cuadraban".
    hay_filtros = bool(anios_sel or paises_sel or cats_sel)
    if hay_filtros:
        col_chips, col_limpiar = st.columns([4.6, 1.4])
        with col_chips:
            st.markdown(_chips_filtros(anios_sel, paises_sel_menu, cats_sel_menu,
                                       paises_click, cats_click), unsafe_allow_html=True)
        with col_limpiar:
            if st.button("✕ Limpiar todos los filtros", key="an_limpiar_todo", width="stretch"):
                for k in ("an_f_anio", "an_f_pais", "an_f_cat", "an_paises", "an_categorias"):
                    st.session_state.pop(k, None)
                st.rerun()

    # --- gráficas clicables: se construyen SOLO con los filtros de menú para
    # que siempre muestren todas las opciones disponibles y se pueda seguir
    # sumando valores con clic (ver docstring del módulo). El clic ya se leyó
    # arriba y se aplica al resto del panel. ---
    universo_menu = _aplicar_filtros(universo, anios_sel, paises_sel_menu, cats_sel_menu)

    c1, c2 = st.columns(2)
    with c1:
        with st.container(border=True):
            _encabezado_grafica("🌍", "Embarques por país de origen",
                                "De dónde viene la carga · clic en una barra para filtrar el panel")
            fig_paises = _figura_paises(universo_menu)
            if fig_paises:
                st.plotly_chart(fig_paises, width="stretch", config=_config_interactiva(),
                                on_select="rerun", selection_mode="points", key="an_paises")
            else:
                st.caption("Sin datos de país todavía.")
    with c2:
        with st.container(border=True):
            _encabezado_grafica("🏷️", "Embarques por categoría",
                                "Qué tipo de carga se mueve más · clic en una porción para filtrar")
            fig_cats = _figura_categorias(universo_menu)
            if fig_cats:
                st.plotly_chart(fig_cats, width="stretch", config=_config_interactiva(),
                                on_select="rerun", selection_mode="points", key="an_categorias")
            else:
                st.caption("Sin datos de categoría todavía.")
    st.caption("'Aéreos' no aparece en la gráfica de categoría: es un modo de transporte, no un tipo de "
              "producto. Se refleja en 'Mercancía en Aeropuerto ahora' y en la comparación de Vía, más abajo.")

    universo_f = _aplicar_filtros(universo, anios_sel, paises_sel, cats_sel)
    dias_f = _aplicar_filtros(dias_df, anios_sel, paises_sel, cats_sel)
    mensual_f = _aplicar_filtros(mensual, anios_sel, paises_sel, cats_sel)
    en_puerto, en_aeropuerto = _en_puerto_ahora(activos, paises_sel, cats_sel)

    pais_top = universo_f["Pais"].replace("", NO_ESPECIFICADO).mode()
    cat_top = universo_f[universo_f["Categoria"] != CATEGORIA_NO_PRODUCTO]["Categoria"] \
        .replace("", NO_ESPECIFICADO).mode()
    tiene_dias_total = "dias_total" in dias_f.columns
    dias_puerto_mediana, dias_puerto_n = _mediana_n(
        dias_f.loc[dias_f["Via"] == VIA_MARITIMA, "dias_total"]) if tiene_dias_total else (None, 0)
    dias_aeropuerto_mediana, dias_aeropuerto_n = _mediana_n(
        dias_f.loc[dias_f["Via"] == VIA_AEREA, "dias_total"]) if tiene_dias_total else (None, 0)
    cat_lenta, dias_lenta = _categoria_mas_lenta(dias_f)
    tendencias = _tendencias_kpi(mensual_f, dias_f)

    _titulo_seccion("Resumen ejecutivo")
    with st.container(key="bikpirow"):
        cols = st.columns(6)
        tarjetas = [
            ("📦", "Embarques recibidos", str(len(mensual_f)), "#059669", "#10B981",
             _flecha(tendencias["conteo"])),
            ("🌍", "País principal", pais_top.iloc[0] if len(pais_top) else "—", "#0284C7", "#38BDF8", ""),
            ("🏷️", "Categoría principal", cat_top.iloc[0] if len(cat_top) else "—", "#1E3A5F", "#0C4A6E", ""),
            ("⚓", "Días en puerto (mediana)",
             f"{dias_puerto_mediana:.0f} d · n={dias_puerto_n}" if dias_puerto_mediana is not None else "—",
             "#059669", "#10B981", _flecha(tendencias["dias_puerto"], sufijo="d")),
            ("✈️", "Días en aeropuerto (mediana)",
             f"{dias_aeropuerto_mediana:.0f} d · n={dias_aeropuerto_n}" if dias_aeropuerto_mediana is not None else "—",
             "#1D4ED8", "#2563EB", _flecha(tendencias["dias_aeropuerto"], sufijo="d")),
            ("🐢", "Categoría más lenta",
             f"{cat_lenta} · {dias_lenta:.0f} d" if cat_lenta else "—",
             "#B91C1C", "#EF4444", ""),
        ]
        for col, (icono, label, valor, ca, cb, delta) in zip(cols, tarjetas):
            with col:
                st.markdown(_tarjeta_kpi_bi(icono, label, valor, ca, cb, delta), unsafe_allow_html=True)
    st.caption("Días en puerto/aeropuerto y categoría más lenta: mediana del ciclo completo (llegada → almacén). "
              "Flechas: mes más reciente con datos vs el inmediato anterior — en Embarques recibidos es "
              "solo volumen (ni mejor ni peor); en Días en puerto/aeropuerto ▼ es mejor.")

    _titulo_seccion("Operación en puerto y aeropuerto")
    with st.container(border=True):
        _encabezado_grafica("🕐", "Días en puerto por categoría",
                            "Mediana del ciclo completo (llegada → almacén) · qué tipo de carga se tranca más")
        fig = _figura_tiempo_puerto_categoria(dias_f)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_tiempo_visible")
            if "dias_total" in dias_f.columns and "AlmacenAprox" in dias_f.columns:
                con_ciclo = dias_f["dias_total"].notna()
                n_aprox = int(dias_f.loc[con_ciclo, "AlmacenAprox"].fillna(False).sum())
                n_total = int(con_ciclo.sum())
                if n_aprox:
                    st.caption(f"Incluye julio y agosto: {n_aprox} de {n_total} embarques con ciclo medido "
                              "usan la fecha de recepción como aproximación de la fecha de almacén, porque "
                              "esa columna todavía no se llenaba cuando se archivaron.")
        else:
            st.caption("Todavía no hay embarques archivados con llegada y declaración para medir tiempo en puerto.")

    st.write("")
    html_ahora = _tarjeta_ahora(en_puerto, en_aeropuerto)
    with st.container(border=True):
        if html_ahora:
            st.markdown(html_ahora, unsafe_allow_html=True)
            st.caption("Llegada confirmada, todavía sin declarar ante Aduanas — la carga sigue físicamente ahí. "
                      "Es una foto del presente: no cambia con el filtro de Año.")
        else:
            st.caption("Nada llegado a puerto o aeropuerto esperando declarar en este momento.")

    # --- Bloque operativo: vía y ciclo, lo que de verdad responde "cómo va la
    # operación" para la presidencia. Sube por encima de lo descriptivo
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
                _encabezado_grafica("📉", "Tendencia mensual por vía",
                                    "Si la brecha Aéreo vs Marítimo se cierra o se abre, mes a mes")
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_via_tendencia")
            else:
                fig = _figura_distribucion_via(dias_f)
                if fig:
                    _encabezado_grafica("📦", "Distribución del ciclo por embarque",
                                        "Cada punto es un embarque cerrado · un atípico se ve al instante")
                    st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_via_distribucion")
                else:
                    st.caption("Todavía no hay embarques cerrados de ambas vías para comparar.")

    st.write("")
    with st.container(border=True):
        _encabezado_grafica("🔄", "El viaje completo, por etapa",
                            "Mediana de días en cada tramo · dónde se va el tiempo entre salida y almacén")
        fig = _figura_ciclo_etapas(dias_f)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_ciclo")
        else:
            st.caption("Todavía no hay suficientes embarques con fechas completas para partir el ciclo en etapas.")

    # --- Bloque descriptivo: "qué se importa". Útil para el equipo, menos
    # accionable para la presidencia -- por eso además de ir debajo de todo lo
    # operativo, queda colapsado por defecto: el resumen ejecutivo termina
    # arriba, esto es "para el que quiera entrar al detalle".
    st.write("")
    with st.expander("📂 Detalle: qué se importa (país, categoría, productos)", expanded=False):
        with st.container(border=True):
            _encabezado_grafica("🗺️", "Qué categoría viene de qué país",
                                "Cruce de origen y tipo de carga · dónde se concentra la operación")
            fig = _figura_heatmap_pais_categoria(universo_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_heatmap")
            else:
                st.caption("Sin suficiente cruce de país y categoría todavía.")

        st.write("")
        with st.container(border=True):
            _encabezado_grafica("📦", "Los 10 productos más importados",
                                "Familias de producto por volumen de embarques")
            fig = _figura_top_productos(universo_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_productos")
            else:
                st.caption("Sin descripciones suficientes para el top de productos.")

        st.write("")
        with st.container(border=True):
            _encabezado_grafica("📈", "Embarques recibidos por mes",
                                "Ritmo de recepción de la operación, mes a mes")
            fig = _figura_tendencia_mensual(mensual_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_tendencia")
            else:
                st.caption("Todavía no hay suficiente histórico mes a mes con estos filtros.")

    st.caption(f"Última actualización de los datos: {datos['hora'].strftime('%H:%M:%S')}")
