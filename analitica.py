"""
analitica.py — Analítica de importaciones para Antillana Comercial.

Panel de gráficas de negocio (no operativo), con acabado tipo BI: filtros
interactivos (segmented_control para Año, multiselects para País/Categoría,
clic-para-filtrar en las gráficas de País y Categoría), barra visible de
filtros activos con limpieza total de un toque, tarjetas KPI con tendencia,
cada gráfica en
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

Rediseño formal (v5.13, nov 2026): el panel se ordena por COMPORTAMIENTO de las
importaciones -- volumen mes a mes (línea de llegadas con lo que viene en camino,
barras por vía y de recepción en almacén), origen y mezcla (barras; la dona de
categoría pasó a barras) y tiempos del ciclo (línea mensual por vía y barras). Un
solo color por medida; Marítimo azul y Aéreo naranja en todas las gráficas; KPI en
tarjetas blancas sin íconos ni degradados.

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
    COL_LLEGO, MESES_ES_CORTO, NO_ESPECIFICADO, VIA_AEREA, VIA_MARITIMA,
    es_llego_si, hoy_rd, parsear_fecha, unificar_paises,
)
from logica import ETAPAS_PUERTO, es_aereo
from ui_componentes import _enriquecer_cacheado, esc


# Estilo formal (v5.13): un solo color por medida, gris para lo secundario y
# color de serie solo cuando DISTINGUE algo (Marítimo vs Aéreo). Marítimo y
# Aéreo conservan su color en todas las gráficas (el color sigue a la entidad).
# El par azul/naranja pasa la validación de daltonismo (ΔE 24.7 protan).
COLOR_MARITIMO = "#2A78D6"
COLOR_AEREO = "#EB6834"
_ACCENT = "#2A78D6"           # medida única (conteos y días)
_ACCENT_CLARO = "#9EC5F4"     # misma medida con un solo dato (no es promedio)
_GRIS_REF = "#898781"         # líneas de referencia (promedio)
# Se conservan los nombres anteriores para no tocar el resto del módulo.
_ACCENT_AZUL = _ACCENT_AMBAR = _ACCENT_VERDE = _ACCENT
PALETA_BI = [COLOR_MARITIMO, COLOR_AEREO]

# "Aéreos" sigue siendo una pestaña real del Sheet (y se puede filtrar por
# ella), pero NO es un tipo de producto -- es un modo de transporte, igual que
# Vía. Por eso se excluye de los gráficos que responden "qué se importa" y de
# "categoría con más días en puerto": mezclarla ahí haría ver como si "cargar
# por avión" fuera una categoría de mercancía, que no lo es. Esa dimensión ya
# tiene su propio gráfico (Aéreo vs Marítimo, y las tarjetas de "ahora mismo").
CATEGORIA_NO_PRODUCTO = "Aéreos"

_FUENTE = "system-ui, -apple-system, Segoe UI, sans-serif"

_TXT_FUERTE = "#0B0B0B"
_TXT = "#52514E"
_TXT_SUAVE = "#898781"
_REJILLA = "#E1E0D9"
_BASE_EJE = "#C3C2B7"

_LAYOUT_BASE = dict(
    margin=dict(t=8, b=6, l=8, r=14), bargap=0.35,
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color=_TXT, size=12, family=_FUENTE),
    showlegend=False, dragmode=False,
    hoverlabel=dict(bgcolor=_TXT_FUERTE, bordercolor=_TXT_FUERTE,
                    font=dict(color="#F9FAFB", size=12.5, family=_FUENTE)),
)


def _eje_valores(**extra) -> dict:
    """Eje de la medida: rejilla apenas visible, sin línea de eje, desde cero.
    Los overrides llegan como kwargs y se funden sobre los defaults."""
    eje = dict(showgrid=True, gridcolor=_REJILLA, gridwidth=1, zeroline=False, title="",
               tickfont=dict(size=11, color=_TXT_SUAVE), rangemode="tozero")
    eje.update(extra)
    return eje


def _eje_categorias(**extra) -> dict:
    """Eje de las etiquetas: sin rejilla, texto un poco más fuerte.
    Los overrides llegan como kwargs y se funden sobre los defaults."""
    eje = dict(showgrid=False, zeroline=False, title="", showline=True, linecolor=_BASE_EJE,
               tickfont=dict(size=11.5, color=_TXT))
    eje.update(extra)
    return eje


def _encabezado_grafica(icono: str, titulo: str, detalle: str = ""):
    """Título de tarjeta estilo BI: fuera de la figura Plotly, con la pregunta
    de negocio que responde como subtítulo. La tipografía HTML es más nítida
    que la del canvas de Plotly y deja el área del gráfico solo para datos."""
    # Formal: sin ícono (el parámetro se conserva para no tocar las llamadas).
    sub = (f'<div style="font-size:0.76rem; color:{_TXT}; margin-top:2px;">{esc(detalle)}</div>'
           if detalle else "")
    st.markdown(
        f'<div style="margin:2px 0 8px;">'
        f'<span style="font-size:0.92rem; font-weight:650; color:{_TXT_FUERTE};">{esc(titulo)}</span>'
        f"{sub}</div>",
        unsafe_allow_html=True,
    )


def _titulo_seccion(texto: str):
    """Separador de bloque en versalitas grises, como los encabezados de grupo
    de un dashboard BI."""
    st.markdown(
        f'<div style="font-size:0.72rem; font-weight:700; letter-spacing:0.08em; '
        f'text-transform:uppercase; color:{_TXT}; margin:22px 0 6px; padding-bottom:6px; '
        f'border-bottom:1px solid {_REJILLA};">{esc(texto)}</div>',
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


_NAVY = "#0C447C"


def _sparkline(valores: list, ancho: int = 120, alto: int = 26) -> str:
    """Mini-línea SVG (sin ejes) para dar la tendencia de un KPI de un vistazo.
    El último punto se marca. Vacía si hay menos de 3 valores."""
    vals = [float(v) for v in valores if v is not None and v == v]
    if len(vals) < 3:
        return ""
    lo, hi = min(vals), max(vals)
    rango = (hi - lo) or 1.0
    paso = ancho / (len(vals) - 1)
    pts = [(i * paso, alto - 3 - (v - lo) / rango * (alto - 6)) for i, v in enumerate(vals)]
    linea = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    x, y = pts[-1]
    return (f'<svg viewBox="0 0 {ancho} {alto}" width="100%" height="{alto}" preserveAspectRatio="none" '
            f'style="display:block; margin-top:8px; overflow:visible;" aria-hidden="true">'
            f'<polyline points="{linea}" fill="none" stroke="{_ACCENT}" stroke-width="1.5" '
            f'vector-effect="non-scaling-stroke" stroke-linejoin="round"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{_ACCENT}"/></svg>')


def _barra_participacion(pct) -> str:
    """Barra fina de participación (0–100 %), para País y Categoría principal."""
    if pct is None:
        return ""
    return (f'<div style="height:4px; background:#EEF0F3; border-radius:2px; margin-top:10px; overflow:hidden;">'
            f'<div style="width:{max(2.0, min(100.0, pct)):.1f}%; height:100%; background:{_ACCENT};"></div></div>')


def _celda_kpi(etiqueta: str, valor_html: str, nota: str = "", extra: str = "") -> str:
    nota_html = (f'<div style="font-size:0.72rem; color:{_TXT}; margin-top:5px; line-height:1.35;">'
                 f'{nota}</div>') if nota else ""
    return (f'<div class="kpi-celda">'
            f'<div style="font-size:0.64rem; font-weight:700; letter-spacing:0.08em; text-transform:uppercase; '
            f'color:{_TXT};">{esc(etiqueta)}</div>'
            f'<div style="margin-top:8px; color:{_NAVY}; font-family:{_FUENTE}; line-height:1.1;">{valor_html}</div>'
            f'{nota_html}{extra}</div>')


def _cifra(valor, unidad: str = "", grande: bool = True) -> str:
    """Cifra en grande con la unidad aparte, más chica: '15 días' se lee como dato,
    '15 d' como abreviatura."""
    if valor in (None, "", "—"):
        return f'<span style="font-size:1.6rem; font-weight:700; color:{_TXT_SUAVE};">—</span>'
    tam = "1.9rem" if grande else "1.15rem"
    u = (f'<span style="font-size:0.78rem; font-weight:600; color:{_TXT}; margin-left:4px;">{esc(unidad)}</span>'
         if unidad else "")
    return f'<span style="font-size:{tam}; font-weight:700; letter-spacing:-0.02em;">{esc(str(valor))}</span>{u}'


def _bloque_kpis(celdas: list, corte: str, alcance: str) -> str:
    """Panel ejecutivo de indicadores: una sola pieza con encabezado azul marino,
    fecha de corte y alcance (filtros), y celdas separadas por filetes. Se acomoda
    solo: 6 columnas en escritorio, 2 en celular."""
    return (
        '<style>'
        '.kpi-bloque{border:1px solid #D9DCE1; border-radius:10px; overflow:hidden; background:#fff;'
        ' box-shadow:0 1px 2px rgba(16,24,40,0.04);}'
        f'.kpi-cab{{background:{_NAVY}; color:#fff; display:flex; justify-content:space-between; align-items:center;'
        ' flex-wrap:wrap; gap:4px 16px; padding:9px 18px;}'
        '.kpi-rejilla{display:grid; grid-template-columns:repeat(6,minmax(0,1fr));}'
        '.kpi-celda{padding:16px 18px 15px; border-left:1px solid #E6E8EC; min-width:0;}'
        '.kpi-celda:first-child{border-left:none;}'
        '@media (max-width:1100px){.kpi-rejilla{grid-template-columns:repeat(3,minmax(0,1fr));}'
        ' .kpi-celda:nth-child(3n+1){border-left:none;} .kpi-celda:nth-child(n+4){border-top:1px solid #E6E8EC;}}'
        '@media (max-width:640px){.kpi-rejilla{grid-template-columns:repeat(2,minmax(0,1fr));}'
        ' .kpi-celda{border-left:1px solid #E6E8EC !important; border-top:1px solid #E6E8EC;}'
        ' .kpi-celda:nth-child(odd){border-left:none !important;} .kpi-celda:nth-child(-n+2){border-top:none;}}'
        '</style>'
        '<div class="kpi-bloque">'
        '<div class="kpi-cab">'
        '<span style="font-size:0.7rem; font-weight:700; letter-spacing:0.12em; text-transform:uppercase;">'
        'Indicadores clave de importación</span>'
        f'<span style="font-size:0.72rem; color:rgba(255,255,255,0.82);">{esc(corte)} · {esc(alcance)}</span>'
        '</div>'
        f'<div class="kpi-rejilla">{"".join(celdas)}</div></div>'
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
        # Venta CIF: no hubo entrada a almacén propio; el respaldo con
        # Fecha_Recibido (= declaración) fabricaría un trámite de 0 días.
        es_cif = str(r.get("Tipo_Recepcion", "") or "").strip() in ("Venta CIF", "Enviado al Cliente")
        almacen_aprox_val = None if (almacen_real or es_cif) else parsear_fecha(r.get("Fecha_Recibido", ""))
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


def _promedio_n(serie: pd.Series) -> tuple:
    """(promedio, mediana, n) de una serie de días. El promedio es la cifra que
    pide Logística; la mediana va al lado para que un embarque trancado meses
    no pase desapercibido (DECISIONS #12)."""
    limpio = pd.to_numeric(serie, errors="coerce").dropna()
    if limpio.empty:
        return None, None, 0
    return float(limpio.mean()), float(limpio.median()), int(len(limpio))


def _ciclo_real(dias_df: pd.DataFrame) -> pd.DataFrame:
    """Solo los embarques cuya fecha de almacén es la REAL (columna
    Fecha_Almacen). Los que usan Fecha_Recibido como respaldo se excluyen de
    los promedios: en el histórico esa fecha suele ser igual a la de llegada,
    así que dan 0-7 días y bajan el promedio (9 d contra 25 d con fecha real
    en la data de sep 2026)."""
    if dias_df.empty or "dias_total" not in dias_df.columns or "AlmacenAprox" not in dias_df.columns:
        return dias_df.iloc[0:0] if not dias_df.empty else dias_df
    return dias_df[dias_df["dias_total"].notna() & (dias_df["AlmacenAprox"] == False)]  # noqa: E712


def _conteo_por(universo: pd.DataFrame, columna: str) -> pd.Series:
    """Embarques por país/categoría, de mayor a menor."""
    return universo[columna].replace("", NO_ESPECIFICADO).value_counts()


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


LLEGADA_CONFIRMADA = "Confirmada"
LLEGADA_PROGRAMADA = "Por llegar"


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _llegadas_mensuales(activos: pd.DataFrame, historico: pd.DataFrame) -> pd.DataFrame:
    """Un renglón por embarque con el MES en que llegó (o llegará) a puerto/aeropuerto.
    Es la base del comportamiento de las importaciones: cuánto entra cada mes.

    - Histórico: siempre llegada confirmada; mes de Fecha_Llegada_Puerto (o el ETA
      si esa columna está vacía en filas viejas).
    - Activos con '¿Llegó?' = SI: confirmada, en el mes del ETA (que ya es la llegada
      real, decisión #4).
    - Activos sin confirmar: «Por llegar», en el mes del ETA. Si el ETA ya pasó sin
      confirmación, se cuenta en el mes en curso: sigue esperándose ahora, no en un
      mes que ya cerró.
    Sin fecha legible, el embarque no entra (no se inventa un mes)."""
    columnas = ["Mes", "Anio", "Estado", "Via", "Categoria", "Pais"]
    mes_actual = date(hoy_rd().year, hoy_rd().month, 1)
    filas = []

    def _via(via_cruda, categoria):
        via_cruda = str(via_cruda or "").strip()
        return VIA_AEREA if (es_aereo(via_cruda) or (not via_cruda and categoria == CATEGORIA_NO_PRODUCTO)) \
            else VIA_MARITIMA

    if historico is not None and not historico.empty:
        paises = unificar_paises(historico[COL_PAIS]) if COL_PAIS in historico.columns \
            else pd.Series([""] * len(historico))
        for (_, r), pais in zip(historico.iterrows(), paises):
            mes = _mes_de(r.get(COL_FECHA_LLEGADA_PUERTO, "")) or _mes_de(r.get(COL_ETA, ""))
            if not mes:
                continue
            cat = r.get("Categoria_Origen", "") or NO_ESPECIFICADO
            filas.append({"Mes": mes, "Estado": LLEGADA_CONFIRMADA, "Via": _via(r.get(COL_VIA, ""), cat),
                          "Categoria": cat, "Pais": pais or NO_ESPECIFICADO})
    if activos is not None and not activos.empty and COL_ETA in activos.columns:
        llego = activos[COL_LLEGO] if COL_LLEGO in activos.columns else pd.Series([""] * len(activos),
                                                                                 index=activos.index)
        for (_, r), ll in zip(activos.iterrows(), llego):
            mes = _mes_de(r.get(COL_ETA, ""))
            if not mes:
                continue
            confirmada = es_llego_si(ll)
            cat = r.get("Categoria", "") or NO_ESPECIFICADO
            filas.append({"Mes": mes if confirmada else max(mes, mes_actual),
                          "Estado": LLEGADA_CONFIRMADA if confirmada else LLEGADA_PROGRAMADA,
                          "Via": _via(r.get(COL_VIA, ""), cat), "Categoria": cat,
                          "Pais": (r.get(COL_PAIS, "") or NO_ESPECIFICADO)})
    if not filas:
        return pd.DataFrame(columns=columnas)
    df = pd.DataFrame(filas)
    df["Anio"] = [m.year for m in df["Mes"]]
    return df[columnas]


def _rango_meses(desde: date, hasta: date) -> list:
    """Todos los meses entre dos fechas (inclusive): un mes sin llegadas es un 0,
    no un hueco que la línea salte."""
    meses, m = [], date(desde.year, desde.month, 1)
    while m <= hasta:
        meses.append(m)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return meses


def _etiqueta_mes(m: date) -> str:
    return f"{MESES_ES_CORTO[m.month]} {m.year % 100:02d}"


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
    serie = _conteo_por(universo, "Pais").head(10).sort_values()
    if serie.empty:
        return None
    unidad = "embarque(s)"
    fig = go.Figure(data=[go.Bar(
        x=serie.values, y=serie.index, orientation="h",
        marker=dict(color=_ACCENT_AZUL, cornerradius=4, line=dict(width=0)),
        text=[f"{v:,.0f}" for v in serie.values], textposition="outside", cliponaxis=False,
        textfont=dict(size=12, color=_TXT, family=_FUENTE),
        hovertemplate=f"<b>%{{y}}</b><br>%{{x:,.0f}} {unidad}<extra></extra>",
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
    """Embarques por categoría, en barras horizontales (antes una dona: comparar
    longitudes es más preciso que comparar ángulos). Clic en una barra = filtro."""
    if universo.empty:
        return None
    base = universo[universo["Categoria"] != CATEGORIA_NO_PRODUCTO]
    serie = _conteo_por(base, "Categoria").sort_values()
    if serie.empty:
        return None
    total = int(serie.sum())
    fig = go.Figure(data=[go.Bar(
        x=serie.values, y=serie.index, orientation="h",
        marker=dict(color=_ACCENT, cornerradius=4, line=dict(width=0)),
        text=[f"{v:,.0f} · {v / total:.0%}" for v in serie.values], textposition="outside", cliponaxis=False,
        textfont=dict(size=12, color=_TXT, family=_FUENTE),
        hovertemplate="<b>%{y}</b><br>%{x:,.0f} embarque(s)<extra></extra>",
    )])
    layout = {**_LAYOUT_BASE, "margin": {**_LAYOUT_BASE["margin"], "l": _margen_izquierdo(serie.index)}}
    fig.update_layout(**layout, height=max(250, 42 * len(serie)), xaxis=_eje_valores(), yaxis=_eje_categorias())
    fig.update_xaxes(range=[0, serie.max() * 1.3])
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
        marker=dict(color=_ACCENT_VERDE, cornerradius=4, line=dict(width=0)),
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
def _figura_tendencia_mensual(mensual: pd.DataFrame, meses: int = 18) -> go.Figure | None:
    """Embarques recibidos en almacén por mes, en barras (meses sin recepciones
    cuentan como 0). Línea gris = promedio de los meses cerrados."""
    if mensual.empty:
        return None
    conteo = mensual["Mes"].value_counts().sort_index()
    if conteo.empty:
        return None
    meses_v = _rango_meses(conteo.index[0], conteo.index[-1])[-meses:]
    conteo = conteo.reindex(meses_v, fill_value=0)
    actual = date(hoy_rd().year, hoy_rd().month, 1)
    fig = go.Figure(data=[go.Bar(
        x=[_etiqueta_mes(m) for m in meses_v], y=conteo.values,
        marker=dict(color=[_ACCENT if m < actual else _ACCENT_CLARO for m in meses_v], cornerradius=3,
                    line=dict(width=0)),
        hovertemplate="%{x}: %{y} recibido(s)<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=310,
                      xaxis=_eje_categorias(tickfont=dict(size=11, color=_TXT_SUAVE)),
                      yaxis=_eje_valores(range=[0, conteo.max() * 1.25 + 1]))
    prom = _promedio_mensual(mensual)
    if prom is not None:
        fig.add_hline(y=prom, line=dict(color=_GRIS_REF, width=1, dash="dash"),
                      annotation_text=f"Promedio {prom:.1f}", annotation_position="top right",
                      annotation_bgcolor="rgba(255,255,255,0.9)",
                      annotation_font=dict(size=11, color=_TXT, family=_FUENTE))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_tiempo_puerto_categoria(dias_real: pd.DataFrame) -> go.Figure | None:
    """Promedio llegada → almacén por categoría (solo fecha de almacén real).
    La mediana y el n van en el tooltip."""
    if dias_real.empty or "dias_total" not in dias_real.columns:
        return None
    base = dias_real[dias_real["Categoria"] != CATEGORIA_NO_PRODUCTO]
    agg = base.groupby("Categoria")["dias_total"].agg(promedio="mean", mediana="median", n="count") \
        .dropna(subset=["promedio"]).sort_values("promedio")
    if agg.empty:
        return None
    fig = go.Figure(data=[go.Bar(
        x=agg["promedio"].values, y=agg.index, orientation="h",
        marker=dict(color=[_ACCENT if n >= 2 else _ACCENT_CLARO for n in agg["n"]], cornerradius=4, line=dict(width=0)),
        text=[f"{v:.0f} d · n={n}" for v, n in zip(agg["promedio"], agg["n"])],
        textposition="outside", cliponaxis=False,
        textfont=dict(size=12, color=_TXT, family=_FUENTE),
        customdata=list(zip(agg["mediana"], agg["n"])),
        hovertemplate="<b>%{y}</b><br>promedio %{x:.1f} d · mediana %{customdata[0]:.0f} d · n=%{customdata[1]}<extra></extra>",
    )])
    layout = {**_LAYOUT_BASE, "margin": {**_LAYOUT_BASE["margin"],
              "l": _margen_izquierdo(agg.index)}}
    fig.update_layout(**layout, height=max(250, 42 * len(agg)),
                      xaxis=_eje_valores(),
                      yaxis=_eje_categorias())
    fig.update_xaxes(range=[0, agg["promedio"].max() * 1.3])
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_ciclo_etapas(dias_df: pd.DataFrame, dias_real: pd.DataFrame) -> go.Figure | None:
    """Promedio de días en cada tramo. Tránsito y puerto salen de todo el
    histórico; el trámite final (declaración → almacén) solo de los embarques
    con fecha de almacén real."""
    if dias_df.empty:
        return None
    etapas, valores, notas, colores, medianas = [], [], [], [], []
    for nombre, serie, color in (
        ("Tránsito<br>(salida → llegada)", dias_df["dias_transito"], _ACCENT_AZUL),
        ("En puerto<br>(llegada → declaración)", dias_df["dias"], _ACCENT_AMBAR),
        ("Trámite final<br>(declaración → almacén)",
         dias_real["dias_tramite"] if not dias_real.empty else pd.Series(dtype=float), _ACCENT_VERDE),
    ):
        prom, med, n = _promedio_n(serie)
        if prom is not None:
            etapas.append(nombre); valores.append(prom); notas.append(n)
            colores.append(color); medianas.append(med)
    if len(etapas) < 2:
        return None
    fig = go.Figure(data=[go.Bar(
        x=etapas, y=valores,
        marker=dict(color=colores, cornerradius=4, line=dict(width=0)),
        text=[f"{v:.0f} d · n={n}" for v, n in zip(valores, notas)], textposition="outside", cliponaxis=False,
        textfont=dict(size=12.5, color=_TXT_FUERTE, family=_FUENTE),
        customdata=medianas,
        hovertemplate="%{x}<br>promedio %{y:.1f} d · mediana %{customdata:.0f} d<extra></extra>",
    )])
    fig.update_layout(**_LAYOUT_BASE, height=300,
                      xaxis=_eje_categorias(),
                      yaxis=_eje_valores())
    fig.update_yaxes(range=[0, max(valores) * 1.25])
    return fig


_ESCALA_AZULES = [[0.0, "#F4F8FD"], [0.25, "#CDE2FB"], [0.6, "#6DA7EC"], [1.0, "#1C5CAB"]]


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


_HOVER_LINEA = dict(hovermode="x unified", hoverlabel=dict(bgcolor="#FFFFFF", bordercolor=_REJILLA,
                                                          font=dict(color=_TXT_FUERTE, size=12, family=_FUENTE)))
_LEYENDA = dict(orientation="h", x=0, xanchor="left", y=1.02, yanchor="bottom",
                font=dict(size=11.5, family=_FUENTE, color=_TXT))


def _ventana_meses(llegadas: pd.DataFrame, atras: int = 18, adelante: int = 3) -> list:
    """Meses a mostrar: hasta `atras` meses cerrados con datos + el actual + los
    próximos con llegadas programadas (máx. `adelante`)."""
    if llegadas.empty:
        return []
    actual = date(hoy_rd().year, hoy_rd().month, 1)
    primero = max(min(llegadas["Mes"]), _rango_meses(date(actual.year - 2, actual.month, 1), actual)[-atras - 1])
    tope = _rango_meses(actual, date(actual.year + 1, actual.month, 1))[adelante]
    ultimo = min(max(max(llegadas["Mes"]), actual), tope)
    return _rango_meses(primero, ultimo)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_llegadas_mes(llegadas: pd.DataFrame) -> go.Figure | None:
    """Línea principal del panel: embarques que llegan a puerto/aeropuerto cada
    mes. Confirmadas en línea continua; lo que viene en camino (por ETA) sigue
    la misma línea, punteada. La línea gris es el promedio de los meses cerrados."""
    meses = _ventana_meses(llegadas)
    if len(meses) < 2:
        return None
    actual = date(hoy_rd().year, hoy_rd().month, 1)
    conf = llegadas[llegadas["Estado"] == LLEGADA_CONFIRMADA]["Mes"].value_counts().reindex(meses, fill_value=0)
    prog = llegadas[llegadas["Estado"] == LLEGADA_PROGRAMADA]["Mes"].value_counts().reindex(meses, fill_value=0)
    x = [_etiqueta_mes(m) for m in meses]
    pasados = [m <= actual for m in meses]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[e for e, p in zip(x, pasados) if p], y=[int(v) for v, p in zip(conf.values, pasados) if p],
        name="Llegadas confirmadas", mode="lines+markers",
        line=dict(color=_ACCENT, width=2), marker=dict(size=8, color="#FFFFFF", line=dict(color=_ACCENT, width=2)),
        hovertemplate="%{y} confirmada(s)<extra></extra>"))
    total_prog = conf.add(prog)
    desde = [i for i, m in enumerate(meses) if m >= actual]
    if desde and prog.sum() > 0:
        fig.add_trace(go.Scatter(
            x=[x[i] for i in desde], y=[int(total_prog.iloc[i]) for i in desde],
            name="Incluyendo lo que viene en camino (ETA)", mode="lines+markers",
            line=dict(color=_ACCENT, width=2, dash="dot"),
            marker=dict(size=8, color="#FFFFFF", line=dict(color=_ACCENT, width=2), symbol="circle-open"),
            customdata=[int(prog.iloc[i]) for i in desde],
            hovertemplate="%{y} en total · %{customdata} por llegar<extra></extra>"))
    cerrados = [int(conf[m]) for m in meses if m < actual]
    if len(cerrados) >= 2:
        prom = sum(cerrados) / len(cerrados)
        fig.add_hline(y=prom, line=dict(color=_GRIS_REF, width=1, dash="dash"),
                      annotation_text=f"Promedio meses cerrados: {prom:.1f}", annotation_position="top right",
                      annotation_bgcolor="rgba(255,255,255,0.9)",
                      annotation_font=dict(size=11, color=_TXT, family=_FUENTE))
    tope = max(int(total_prog.max()), int(conf.max()), 1)
    fig.update_layout(**{**_LAYOUT_BASE, **_HOVER_LINEA, "showlegend": True}, height=330, legend=_LEYENDA,
                      xaxis=_eje_categorias(tickfont=dict(size=11, color=_TXT_SUAVE)),
                      yaxis=_eje_valores(range=[0, tope * 1.2 + 1], title=dict(
                          text="Embarques", font=dict(size=11, color=_TXT_SUAVE))))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_llegadas_via_mes(llegadas: pd.DataFrame) -> go.Figure | None:
    """Barras apiladas: llegadas confirmadas por mes, Marítimo y Aéreo."""
    actual = date(hoy_rd().year, hoy_rd().month, 1)
    conf = llegadas[(llegadas["Estado"] == LLEGADA_CONFIRMADA)] if not llegadas.empty else llegadas
    meses = [m for m in _ventana_meses(llegadas) if m <= actual]
    if conf.empty or len(meses) < 2:
        return None
    x = [_etiqueta_mes(m) for m in meses]
    fig = go.Figure()
    for via, color in ((VIA_MARITIMA, COLOR_MARITIMO), (VIA_AEREA, COLOR_AEREO)):
        serie = conf[conf["Via"] == via]["Mes"].value_counts().reindex(meses, fill_value=0)
        if serie.sum() == 0:
            continue
        fig.add_trace(go.Bar(x=x, y=serie.values, name=via,
                             marker=dict(color=color, line=dict(color="#FFFFFF", width=1.5), cornerradius=3),
                             hovertemplate=f"{via}: %{{y}}<extra></extra>"))
    if not fig.data:
        return None
    fig.update_layout(**{**_LAYOUT_BASE, **_HOVER_LINEA, "showlegend": True}, barmode="stack", height=310,
                      legend={**_LEYENDA, "traceorder": "normal"}, xaxis=_eje_categorias(tickfont=dict(size=11, color=_TXT_SUAVE)),
                      yaxis=_eje_valores())
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_tiempo_mensual(dias_real: pd.DataFrame, meses: int = 18) -> go.Figure | None:
    """Promedio mensual de días puerto → almacén, una línea por vía (mes de
    almacén, solo fecha de almacén real). Mediana y n en el tooltip."""
    if dias_real.empty or "Mes" not in dias_real.columns:
        return None
    base = dias_real.dropna(subset=["Mes", "dias_total"])
    meses_disp = sorted(base["Mes"].unique())[-meses:] if not base.empty else []
    if len(meses_disp) < 2:
        return None
    meses_disp = _rango_meses(meses_disp[0], meses_disp[-1])
    x = [_etiqueta_mes(m) for m in meses_disp]
    fig = go.Figure()
    for via, color in ((VIA_MARITIMA, COLOR_MARITIMO), (VIA_AEREA, COLOR_AEREO)):
        g = base[base["Via"] == via]
        if g.empty:
            continue
        agg = g.groupby("Mes")["dias_total"].agg(["mean", "median", "count"]).reindex(meses_disp)
        fig.add_trace(go.Scatter(
            x=x, y=agg["mean"].values, name=via, mode="lines+markers", connectgaps=True,
            line=dict(color=color, width=2), marker=dict(size=8, color="#FFFFFF", line=dict(color=color, width=2)),
            customdata=list(zip(agg["median"].fillna(0), agg["count"].fillna(0).astype(int))),
            hovertemplate=f"{via}: %{{y:.0f}} d (mediana %{{customdata[0]:.0f}} · n=%{{customdata[1]}})<extra></extra>"))
    if not fig.data:
        return None
    fig.update_layout(**{**_LAYOUT_BASE, **_HOVER_LINEA, "showlegend": True}, height=310, legend=_LEYENDA,
                      xaxis=_eje_categorias(tickfont=dict(size=11, color=_TXT_SUAVE)),
                      yaxis=_eje_valores(title=dict(text="Días (promedio)", font=dict(size=11, color=_TXT_SUAVE))))
    return fig


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_via_barras(dias_real: pd.DataFrame) -> go.Figure | None:
    """Respaldo de la línea mensual cuando hay menos de 2 meses: promedio de
    días puerto → almacén por vía, en barras."""
    if dias_real.empty:
        return None
    filas = []
    for via, color in ((VIA_MARITIMA, COLOR_MARITIMO), (VIA_AEREA, COLOR_AEREO)):
        prom, med, n = _promedio_n(dias_real.loc[dias_real["Via"] == via, "dias_total"])
        if prom is not None:
            filas.append((via, prom, med, n, color))
    if not filas:
        return None
    fig = go.Figure(data=[go.Bar(
        x=[f[0] for f in filas], y=[f[1] for f in filas],
        marker=dict(color=[f[4] for f in filas], cornerradius=4, line=dict(width=0)),
        text=[f"{f[1]:.0f} d · n={f[3]}" for f in filas], textposition="outside", cliponaxis=False,
        textfont=dict(size=12, color=_TXT_FUERTE, family=_FUENTE),
        customdata=[f[2] for f in filas],
        hovertemplate="%{x}: promedio %{y:.1f} d · mediana %{customdata:.0f} d<extra></extra>")])
    fig.update_layout(**_LAYOUT_BASE, height=300, xaxis=_eje_categorias(), yaxis=_eje_valores())
    fig.update_yaxes(range=[0, max(f[1] for f in filas) * 1.25])
    return fig


def _promedio_mensual(mensual: pd.DataFrame):
    """Promedio de embarques recibidos por mes. Solo meses COMPLETOS (el mes en
    curso se excluye: un mes a medias bajaría el promedio); si no queda
    ninguno, usa todos los que haya. None sin datos."""
    if mensual is None or mensual.empty or "Mes" not in mensual.columns:
        return None
    conteo = mensual["Mes"].value_counts()
    en_curso = date(hoy_rd().year, hoy_rd().month, 1)
    completos = conteo[[m < en_curso for m in conteo.index]]
    base = completos if len(completos) else conteo
    return float(base.mean()) if len(base) else None


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _figura_tiempo_por_pais(dias_real: pd.DataFrame, minimo: int = 2, top: int = 8) -> go.Figure | None:
    """Promedio llegada → almacén por país de origen (solo fecha de almacén
    real). Un país con menos de `minimo` embarques no se muestra: un solo
    embarque no es un promedio."""
    if dias_real.empty or "dias_total" not in dias_real.columns:
        return None
    base = dias_real.copy()
    base["Pais"] = base["Pais"].replace("", NO_ESPECIFICADO)
    agg = base.groupby("Pais")["dias_total"].agg(promedio="mean", mediana="median", n="count")
    agg = agg[agg["n"] >= minimo].sort_values("n", ascending=False).head(top).sort_values("promedio")
    if agg.empty:
        return None
    fig = go.Figure(data=[go.Bar(
        x=agg["promedio"].values, y=agg.index, orientation="h",
        marker=dict(color=_ACCENT_AMBAR, cornerradius=4, line=dict(width=0)),
        text=[f"{v:.0f} d · n={n}" for v, n in zip(agg["promedio"], agg["n"])],
        textposition="outside", cliponaxis=False,
        textfont=dict(size=12, color=_TXT, family=_FUENTE),
        customdata=list(zip(agg["mediana"], agg["n"])),
        hovertemplate="<b>%{y}</b><br>promedio %{x:.1f} d · mediana %{customdata[0]:.0f} d · n=%{customdata[1]}<extra></extra>",
    )])
    layout = {**_LAYOUT_BASE, "margin": {**_LAYOUT_BASE["margin"], "l": _margen_izquierdo(agg.index)}}
    fig.update_layout(**layout, height=max(250, 42 * len(agg)), xaxis=_eje_valores(), yaxis=_eje_categorias())
    fig.update_xaxes(range=[0, agg["promedio"].max() * 1.3])
    return fig


def _espera_actual(activos_enriq: pd.DataFrame, paises_sel: list, cats_sel: list) -> tuple:
    """(promedio, máximo, n) de días que llevan en puerto/aeropuerto los
    embarques que llegaron y siguen sin declarar. Foto del presente."""
    if activos_enriq is None or activos_enriq.empty or "EtapaActual" not in activos_enriq.columns \
            or "DiasEnPuerto" not in activos_enriq.columns:
        return None, None, 0
    df = activos_enriq[activos_enriq["EtapaActual"] == ETAPAS_PUERTO[0]]
    if paises_sel and COL_PAIS in df.columns:
        df = df[df[COL_PAIS].isin(paises_sel)]
    if cats_sel and "Categoria" in df.columns:
        df = df[df["Categoria"].replace("", NO_ESPECIFICADO).isin(cats_sel)]
    dias = pd.to_numeric(df["DiasEnPuerto"], errors="coerce").dropna()
    if dias.empty:
        return None, None, 0
    return float(dias.mean()), float(dias.max()), int(len(dias))


def _tarjeta_ahora(en_puerto: int, en_aeropuerto: int, espera: tuple = (None, None, 0)) -> str:
    """Barra compacta con lo que llegó y sigue sin declarar, ahora mismo.
    Reusa las clases CSS de tránsito (.paises/.pfila/...) para que las dos
    cifras se lean juntas de un vistazo."""
    if en_puerto == 0 and en_aeropuerto == 0:
        return ""
    tope = max(en_puerto, en_aeropuerto, 1)
    piezas = ['<div class="paises"><div class="atttl">Ahora mismo: llegado y sin declarar</div>']
    for etiqueta, valor, color in (("En puerto", en_puerto, COLOR_MARITIMO),
                                   ("En aeropuerto", en_aeropuerto, COLOR_AEREO)):
        ancho = max(4.0, (valor / tope) * 100.0)
        piezas.append(
            f'<div class="pfila"><div class="pnom">{esc(etiqueta)}</div>'
            f'<div class="pbarra"><span style="width:{ancho:.1f}%;background:{color};border-radius:6px;"></span></div>'
            f'<div class="pval">{valor}</div></div>'
        )
    prom, maximo, n = espera
    if prom is not None:
        piezas.append(f'<div style="font-size:0.78rem;color:{_TXT_SUAVE};margin-top:6px;">'
                      f'Llevan en promedio <b style="color:{_TXT_FUERTE};">{prom:.0f} d</b> esperando '
                      f'· la más antigua, {maximo:.0f} d (n={n})</div>')
    piezas.append("</div>")
    return "".join(piezas)


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
    st.subheader("Analítica de importaciones")
    st.caption("Comportamiento de las importaciones del Grupo a partir de los embarques en tránsito y del "
               "histórico. Los filtros y el clic sobre las barras de País o Categoría actualizan todo el panel.")

    activos_crudo, historico = datos.get("activos"), datos.get("historico")
    activos = _enriquecer_cacheado(activos_crudo)
    universo = _universo(activos_crudo, historico)
    dias_df = _ciclo_historico(historico)
    mensual = _mensual_historico(historico)
    llegadas = _llegadas_mensuales(activos_crudo, historico)

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
    cats_click = _clics_vigentes("an_categorias", "y", cats_disp)

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

    # Las gráficas clicables (país y categoría) se dibujan solo con los filtros
    # de menú para que siempre muestren todas las opciones (ver docstring).
    universo_menu = _aplicar_filtros(universo, anios_sel, paises_sel_menu, cats_sel_menu)

    universo_f = _aplicar_filtros(universo, anios_sel, paises_sel, cats_sel)
    dias_f = _aplicar_filtros(dias_df, anios_sel, paises_sel, cats_sel)
    mensual_f = _aplicar_filtros(mensual, anios_sel, paises_sel, cats_sel)
    llegadas_f = _aplicar_filtros(llegadas, anios_sel, paises_sel, cats_sel)
    dias_real = _ciclo_real(dias_f)
    en_puerto, en_aeropuerto = _en_puerto_ahora(activos, paises_sel, cats_sel)
    espera = _espera_actual(activos, paises_sel, cats_sel)
    tendencias = _tendencias_kpi(mensual_f, dias_f)

    # ---------------- KPIs ----------------
    ciclo_prom, ciclo_med, ciclo_n = _promedio_n(dias_real["dias_total"]) if not dias_real.empty else (None, None, 0)
    por_via = {}
    for via in (VIA_AEREA, VIA_MARITIMA):
        g = dias_real.loc[dias_real["Via"] == via, "dias_total"] if not dias_real.empty else pd.Series(dtype=float)
        por_via[via] = _promedio_n(g)
    n_aprox = (int((dias_f["dias_total"].notna() & (dias_f["AlmacenAprox"] == True)).sum())  # noqa: E712
               if not dias_f.empty and "AlmacenAprox" in dias_f.columns else 0)

    def _principal(columna: str, base: pd.DataFrame):
        serie = _conteo_por(base, columna) if not base.empty else pd.Series(dtype=float)
        if serie.empty:
            return "—", "", None
        parte = serie.iloc[0] / serie.sum() * 100
        return str(serie.index[0]), f"{parte:.0f}% de los embarques · {int(serie.sum()):,} en total", parte

    pais_nombre, pais_pct, pais_num = _principal("Pais", universo_f)
    cat_nombre, cat_pct, cat_num = _principal("Categoria", universo_f[universo_f["Categoria"] != CATEGORIA_NO_PRODUCTO]
                                     if not universo_f.empty else universo_f)

    prom_mes = _promedio_mensual(mensual_f)
    meses_n = (len([m for m in mensual_f["Mes"].unique() if m < date(hoy_rd().year, hoy_rd().month, 1)])
               or len(mensual_f["Mes"].unique())) if prom_mes is not None else 0

    def _d(v):
        return f"{v:.0f} d" if v is not None else "—"

    # Series para las mini-líneas: últimos 12 meses (meses sin datos = 0 en volumen).
    mes_actual = date(hoy_rd().year, hoy_rd().month, 1)
    serie_rec, serie_dias = [], []
    if not mensual_f.empty:
        conteo = mensual_f["Mes"].value_counts()
        meses12 = _rango_meses(min(conteo.index), mes_actual)[-12:]
        serie_rec = [int(conteo.get(m, 0)) for m in meses12 if m < mes_actual]
    if not dias_real.empty:
        por_mes = dias_real.dropna(subset=["Mes"]).groupby("Mes")["dias_total"].mean().sort_index()
        serie_dias = list(por_mes.values[-12:])

    def _nota_delta(texto):
        return f'<span style="color:{_TXT_FUERTE}; font-weight:600;">{esc(texto)}</span>' if texto else ""

    via_m, via_a = por_via[VIA_MARITIMA], por_via[VIA_AEREA]
    valor_via = (f'<div style="display:flex; gap:18px; align-items:baseline; flex-wrap:wrap;">'
                 f'<div>{_cifra(f"{via_m[0]:.0f}" if via_m[0] is not None else None, "días")}'
                 f'<div style="font-size:0.68rem; color:{COLOR_MARITIMO}; font-weight:700; margin-top:2px;">'
                 f'MARÍTIMO</div></div>'
                 f'<div>{_cifra(f"{via_a[0]:.0f}" if via_a[0] is not None else None, "días")}'
                 f'<div style="font-size:0.68rem; color:{COLOR_AEREO}; font-weight:700; margin-top:2px;">'
                 f'AÉREO</div></div></div>')
    celdas = [
        _celda_kpi("Embarques recibidos", _cifra(f"{len(mensual_f):,}"),
                   _nota_delta(_flecha(tendencias["conteo"])) or "en almacén", _sparkline(serie_rec)),
        _celda_kpi("Promedio mensual", _cifra(f"{prom_mes:.1f}" if prom_mes is not None else None, "embarques"),
                   f"{meses_n} meses cerrados" if prom_mes is not None else ""),
        _celda_kpi("Ciclo puerto → almacén", _cifra(f"{ciclo_prom:.0f}" if ciclo_prom is not None else None, "días"),
                   f"Mediana {_d(ciclo_med)} · {ciclo_n} embarques" if ciclo_n else "", _sparkline(serie_dias)),
        _celda_kpi("Ciclo por vía", valor_via, f"{via_m[2]} marítimos · {via_a[2]} aéreos"),
        _celda_kpi("Origen principal", _cifra(pais_nombre, grande=False), esc(pais_pct), _barra_participacion(pais_num)),
        _celda_kpi("Categoría principal", _cifra(cat_nombre, grande=False), esc(cat_pct), _barra_participacion(cat_num)),
    ]
    alcance = ("Años: " + ", ".join(str(a) for a in sorted(anios_sel)) if anios_sel else "Todos los años")
    if paises_sel or cats_sel:
        alcance += " · con filtros"
    hoy = hoy_rd()
    st.markdown(_bloque_kpis(celdas, f"Corte al {hoy.day} {MESES_ES_CORTO[hoy.month]} {hoy.year}", alcance),
                unsafe_allow_html=True)
    nota_aprox = (f" {n_aprox} embarque(s) archivados sin fecha de almacén real quedan fuera de estos promedios."
                  if n_aprox else "")
    st.caption("Puerto → almacén: días promedio desde la llegada confirmada hasta la entrada a almacén, solo con "
               "fecha de almacén real." + nota_aprox + " La tendencia de «Embarques recibidos» compara el mes "
               "más reciente con el anterior.")

    # ---------------- VOLUMEN ----------------
    _titulo_seccion("Volumen de importaciones")
    with st.container(border=True):
        _encabezado_grafica("", "Llegadas a puerto y aeropuerto por mes",
                            "Embarques que llegaron cada mes · la línea punteada suma lo que viene en camino "
                            "según su ETA")
        fig = _figura_llegadas_mes(llegadas_f)
        if fig:
            st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_llegadas")
            st.caption("Los embarques que ya pasaron su ETA sin confirmar la llegada se cuentan en el mes en curso.")
        else:
            st.caption("Todavía no hay al menos dos meses con llegadas para ver una tendencia.")

    a1, a2 = st.columns(2)
    with a1:
        with st.container(border=True):
            _encabezado_grafica("", "Llegadas por vía", "Confirmadas por mes · Marítimo y Aéreo")
            fig = _figura_llegadas_via_mes(llegadas_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_llegadas_via")
            else:
                st.caption("Todavía no hay al menos dos meses con llegadas confirmadas.")
    with a2:
        with st.container(border=True):
            _encabezado_grafica("", "Embarques recibidos en almacén por mes",
                                "Cierre del ciclo · la línea gris es el promedio de los meses cerrados")
            fig = _figura_tendencia_mensual(mensual_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_tendencia")
            else:
                st.caption("Todavía no hay suficiente histórico mes a mes con estos filtros.")

    # ---------------- ORIGEN Y MEZCLA ----------------
    _titulo_seccion("Origen y mezcla de la carga")
    c1, c2 = st.columns(2)
    with c1:
        with st.container(border=True):
            _encabezado_grafica("", "Embarques por país de origen",
                                "Los 10 orígenes principales · clic en una barra para filtrar el panel")
            fig_paises = _figura_paises(universo_menu)
            if fig_paises:
                st.plotly_chart(fig_paises, width="stretch", config=_config_interactiva(),
                                on_select="rerun", selection_mode="points", key="an_paises")
            else:
                st.caption("Sin datos de país todavía.")
    with c2:
        with st.container(border=True):
            _encabezado_grafica("", "Embarques por categoría",
                                "Cantidad y participación · clic en una barra para filtrar el panel")
            fig_cats = _figura_categorias(universo_menu)
            if fig_cats:
                st.plotly_chart(fig_cats, width="stretch", config=_config_interactiva(),
                                on_select="rerun", selection_mode="points", key="an_categorias")
            else:
                st.caption("Sin datos de categoría todavía.")
    st.caption("«Aéreos» no figura como categoría: es un modo de transporte, no un tipo de producto; "
               "se analiza en las gráficas por vía.")

    # ---------------- TIEMPOS ----------------
    _titulo_seccion("Tiempos del ciclo")
    t1, t2 = st.columns(2)
    with t1:
        with st.container(border=True):
            fig = _figura_tiempo_mensual(dias_real)
            if fig:
                _encabezado_grafica("", "Días de puerto a almacén, por mes",
                                    "Promedio mensual por vía · mes de entrada a almacén")
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_via_tendencia")
            else:
                _encabezado_grafica("", "Días de puerto a almacén, por vía",
                                    "Promedio · hacen falta dos meses con datos para ver la evolución")
                fig = _figura_via_barras(dias_real)
                if fig:
                    st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_via_barras")
                else:
                    st.caption("Todavía no hay embarques con fecha de almacén real para medir esto.")
    with t2:
        with st.container(border=True):
            _encabezado_grafica("", "Días por etapa del ciclo",
                                "Promedio de cada tramo · tránsito, puerto y trámite final")
            fig = _figura_ciclo_etapas(dias_f, dias_real)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_ciclo")
            else:
                st.caption("Todavía no hay suficientes embarques con fechas completas para partir el ciclo.")

    v1, v2 = st.columns(2)
    with v1:
        with st.container(border=True):
            _encabezado_grafica("", "Puerto → almacén por categoría",
                                "Promedio de días · barra clara = un solo embarque, todavía no es un promedio")
            fig = _figura_tiempo_puerto_categoria(dias_real)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_tiempo_visible")
            else:
                st.caption("Todavía no hay embarques con fecha de almacén real para medir esto.")
    with v2:
        with st.container(border=True):
            _encabezado_grafica("", "Puerto → almacén por país de origen",
                                "Promedio de días · países con 2 o más embarques con fecha de almacén real")
            fig = _figura_tiempo_por_pais(dias_real)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_tiempo_pais")
            else:
                st.caption("Todavía no hay países con 2 o más embarques con fecha de almacén real.")

    with st.container(border=True):
        html_ahora = _tarjeta_ahora(en_puerto, en_aeropuerto, espera)
        if html_ahora:
            st.markdown(html_ahora, unsafe_allow_html=True)
            st.caption("Llegada confirmada, todavía sin declarar ante Aduanas. Foto del presente: "
                       "no cambia con el filtro de Año.")
        else:
            st.caption("Nada llegado a puerto o aeropuerto esperando declarar en este momento.")

    # ---------------- DETALLE (colapsado) ----------------
    st.write("")
    with st.expander("Más detalle: país × categoría y productos", expanded=False):
        with st.container(border=True):
            _encabezado_grafica("", "Qué categoría viene de qué país",
                                "Cruce de origen y tipo de carga · dónde se concentra la operación")
            fig = _figura_heatmap_pais_categoria(universo_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_heatmap")
            else:
                st.caption("Sin suficiente cruce de país y categoría todavía.")

        st.write("")
        with st.container(border=True):
            _encabezado_grafica("", "Los 10 productos más importados",
                                "Familias de producto por cantidad de embarques")
            fig = _figura_top_productos(universo_f)
            if fig:
                st.plotly_chart(fig, width="stretch", config=_config_interactiva(), key="an_productos")
            else:
                st.caption("Sin descripciones suficientes para el top de productos.")

    st.caption(f"Última actualización de los datos: {datos['hora'].strftime('%H:%M:%S')}")
