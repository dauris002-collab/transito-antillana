"""
ui_componentes.py — Componentes visuales reutilizables de Antillana Comercial.

Bloques HTML/CSS (encabezado, KPIs, diagrama de flujo, lista de embarques,
ficha del embarque, gráficos) y el panel del dashboard. Usa Streamlit e
importa de logica.py y sheets_io.py.
"""

from __future__ import annotations

import base64
import io
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from sheets_io import (
    CATEGORIAS, COL_ACTUALIZACION, COL_ACTUALIZADO_POR, COL_BL, COL_CANT,
    COL_DESC, COL_EE, COL_ETA, COL_MODELO, COL_OC, COL_PAIS, ETAPAS_PUERTO,
    INDICE_ETAPA, MESES_ES, MESES_ES_CORTO, NO_ESPECIFICADO, SLA_ETAPA_DEFECTO,
    VALOR_RETRASADO, _norm, _slug_css, avanzar_estado_puerto, columnas_extra,
    costos_puerto, eliminar_embarque, es_numero, formato_dinero, formato_eta,
    hoy_rd, invalidar_caches, marcar_como_recibido, marcar_estatus_llegada,
    registrar_log, sla_etapas,
)
from logica import (
    CATEGORIAS_CON_OC_EE, CATEGORIA_AEREA, EST_PROXIMO, EST_PUERTO, EST_RETRASADO,
    EST_SIN_FECHA, EST_TRANSITO, ETIQUETA_CORTA_ETAPA, ICONO_ETAPA, PALETA_PAISES,
    SEMANAS_HORIZONTE, STATUS_COLOR, STATUS_ORDER, UMBRAL_PROXIMO,
    _cumple_filtro_puerto, _en_proceso, _etiquetas_desambiguadas, _monto,
    clave_fila, contar_recibidas_mes, costo_demora_fila, costo_dia_fila,
    enriquecer, es_aereo, esc, etiqueta_costo, etiqueta_etapa,
    fechas_flujo_de_fila, formato_corto, lugar_de, ordenar_vista,
    resumen_atraso_puerto, texto_dias, texto_estado,
)


# ---------------------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# ---------------------------------------------------------------------------
VERSION_APP = "3.1"


VISTA_EN_PROCESO_PUERTO = "Puerto/Aeropuerto · Estatus"


TOPE_PROCESO = 8            # embarques con diagrama visible antes de paginar


COLOR_TOTAL = "#17A2B8"


COLOR_RECIBIDAS_MES = "#2E7D32"


COLOR_ALERTA = "#B45309"


CUSTOM_CSS = """
<style>
:root {
    --ant-azul: #0C447C;
    --ant-borde: #E5E7EB;
    --ant-texto: #111827;
    --ant-suave: #6B7280;
    --ant-hecho: #2E7D32;
    --ant-actual: #F0B90B;
}
/* El menú de secciones es el primer elemento de la página: sin este aire, en
   algunas resoluciones queda medio escondido bajo la barra fija de Streamlit.
   El padding inferior respeta el área segura del iPhone (barra de gestos). */
.block-container {
    padding-top: 3.4rem;
    padding-bottom: calc(2.5rem + env(safe-area-inset-bottom, 0px));
}
html { -webkit-text-size-adjust: 100%; }

.nav-rotulo { font-size:0.70rem; text-transform:uppercase; letter-spacing:0.08em;
              color:#9CA3AF; font-weight:700; margin:0 0 4px 2px; }

/* ---------- Encabezado ---------- */
.ant-head { text-align:center; margin: 0 0 1.1rem 0; }
.ant-eyebrow {
    display:inline-flex; align-items:center; gap:6px;
    background:#E6F1FB; color:var(--ant-azul);
    font-size:0.74rem; font-weight:700; letter-spacing:0.08em; text-transform:uppercase;
    padding:5px 16px; border-radius:999px;
}
.ant-title {
    font-size:2.2rem; font-weight:800; margin:0.6rem 0 0.3rem 0; color:var(--ant-texto);
    letter-spacing:-0.02em;
}
.ant-rule { width:52px; height:4px; background:#2E86DE; border-radius:2px; margin:0.2rem auto 0.6rem auto; }
.ant-sub { font-size:0.95rem; color:var(--ant-suave); }
.ant-stamp { display:inline-flex; align-items:center; gap:7px; font-size:0.76rem;
             color:var(--ant-suave); margin-top:0.5rem; flex-wrap:wrap; justify-content:center; }
.ant-dot { width:8px; height:8px; border-radius:50%; background:#22C55E; display:inline-block; }
.ant-logo { max-height:58px; margin-bottom:0.5rem; }

/* Resumen ejecutivo: una línea que responde "¿cómo vamos hoy?" sin leer nada más */
.resumen { background:#F8FAFC; border:1px solid var(--ant-borde); border-left:4px solid var(--ant-azul);
           border-radius:10px; padding:10px 14px; font-size:0.92rem; color:#1F2937; margin-bottom:12px; }
.resumen b { color:#0C447C; }

/* Panel de confirmación de llegadas */
.conf-titulo { font-size:0.78rem; text-transform:uppercase; letter-spacing:0.06em;
               font-weight:800; color:#92400E; background:#FEF7E6;
               border-left:4px solid #F0B90B; padding:8px 14px; border-radius:8px;
               margin:6px 0 10px 0; }
.conf-fila { display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:6px 2px; }
.conf-bl { font-weight:700; color:#111827; font-size:0.92rem; }
.conf-desc { color:#6B7280; font-size:0.85rem; }

/* ---------- KPIs ---------- */
.kpi-card { border-radius:14px; padding:14px 18px; min-height:92px; height:100%;
            display:flex; flex-direction:column; justify-content:center; align-items:center;
            text-align:center; box-shadow:0 2px 8px rgba(17,24,39,0.12); }
.kpi-label { font-size:0.70rem; font-weight:700; text-transform:uppercase; letter-spacing:0.06em;
             color:rgba(255,255,255,0.92); margin-bottom:6px; }
.kpi-value { font-size:2.0rem; font-weight:800; line-height:1; color:#fff; }
.kpi-sub { font-size:0.70rem; color:rgba(255,255,255,0.88); margin-top:6px; }

/* ---------- Barras por país (HTML puro: no se mueven en celular) ---------- */
.paises { margin:4px 0 10px; }
.pfila { display:grid; grid-template-columns:minmax(72px,26%) 1fr 34px;
         align-items:center; gap:8px; padding:3px 0; }
.pnom { font-size:.8rem; color:#374151; overflow:hidden; text-overflow:ellipsis;
        white-space:nowrap; }
.pbarra { background:rgba(0,0,0,.06); border-radius:6px; height:14px; overflow:hidden; }
.pbarra span { display:block; height:100%; border-radius:6px; }
.pval { font-size:.8rem; font-weight:700; color:#374151; text-align:right; }
@media (max-width: 640px) {
  .pfila { grid-template-columns:minmax(64px,34%) 1fr 28px; gap:6px; }
  .pnom { font-size:.74rem; }
}

/* ---------- Resumen ejecutivo de puerto (filtro clicable, no tarjetas fijas) ----------
   El contador vive en el propio botón del filtro (Todos · N, Atrasados · N,
   Pendientes de pago · N, Costo · RD$X) y clicarlo filtra el detalle de abajo:
   el número no es solo para mirar, también sirve para llegar al embarque.
   El color de cada botón es fijo (rojo atrasados, ámbar pendientes, azul
   costo) y se invierte (relleno en vez de solo borde) cuando está activo. */
.ejec-detalle { border:1px solid var(--ant-borde); border-radius:10px; overflow:hidden; margin-top:8px; }
.ejec-detalle .atttl { padding:8px 14px; background:#F9FAFB; border-bottom:1px solid var(--ant-borde);
                        font-size:0.72rem; text-transform:uppercase; letter-spacing:0.04em;
                        color:#6B7280; font-weight:700; margin-bottom:0; }
.ejec-detalle .atfila { padding:7px 14px; }
@media (max-width: 640px) {
  .ejec-detalle .atmonto { margin-left:0; }
}

/* Filas de detalle por embarque dentro de .ejec-detalle (costo acumulado) */
.atttl { font-size:.72rem; text-transform:uppercase; letter-spacing:.04em;
         color:#6B7280; margin-bottom:6px; }
.atfila { display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 10px;
          padding:5px 0; border-bottom:1px dotted rgba(0,0,0,.08); font-size:.82rem; }
.atfila:last-child { border-bottom:0; }
.atbl { font-weight:700; color:#111827; }
.atoc { font-size:.74rem; color:#4B5563; background:rgba(0,0,0,.05);
        border-radius:5px; padding:1px 6px; }
.atoc.atsin { color:#92400E; background:rgba(146,64,14,.10); }
.atdias { color:#4B5563; }
.atmonto { margin-left:auto; font-weight:700; color:#991B1B; white-space:nowrap;
           text-align:right; }
.attarifa { display:block; font-weight:400; font-size:.68rem; color:#6B7280; }
.atresto { color:#6B7280; font-style:italic; }
.atfila.atok .atmonto { color:#4B5563; }

/* ---------- Chips de resumen por etapa (reemplazan 5 st.metric en fila) ---------- */
.chips { display:flex; flex-wrap:wrap; gap:8px; margin:4px 0 10px 0; }
.chip { display:inline-flex; align-items:center; gap:7px; background:#fff;
        border:1px solid var(--ant-borde); border-radius:999px; padding:6px 13px;
        font-size:0.82rem; color:#374151; }
.chip b { font-size:0.98rem; color:#111827; }
.chip.on { border-color:#F0B90B; background:#FFFBEB; }

/* ---------- Alertas de cuello de botella ---------- */
.alerta-caja { border:1px solid #FDE68A; background:#FFFBEB; border-radius:12px;
               padding:12px 16px; margin-bottom:12px; }
.alerta-titulo { font-size:0.78rem; text-transform:uppercase; letter-spacing:0.05em;
                 font-weight:800; color:#92400E; margin-bottom:8px; }
.alerta-fila { display:flex; gap:10px; align-items:baseline; padding:3px 0;
               font-size:0.87rem; color:#1F2937; flex-wrap:wrap; }
.alerta-fila .dias { font-weight:800; color:#B45309; min-width:64px; }
.alerta-fila .bl { font-weight:700; }
.alerta-fila .que { color:#6B7280; }

/* ---------- Diagrama de flujo (HTML puro, sin Plotly) ---------- */
.flujo { display:flex; align-items:flex-start; margin:8px 0 4px 0; }
.paso { flex:1 1 0; min-width:0; position:relative; display:flex; flex-direction:column;
        align-items:center; text-align:center; padding:0 2px; }
.paso::before { content:""; position:absolute; top:15px; left:-50%; width:100%; height:3px;
                background:var(--ant-borde); z-index:0; }
.paso:first-child::before { display:none; }
.paso.hecho::before, .paso.actual::before { background:var(--ant-hecho); }
.paso .pt { width:32px; height:32px; border-radius:50%; display:flex; align-items:center;
            justify-content:center; background:#E5E7EB; font-size:15px; z-index:1;
            border:2px solid #fff; box-shadow:0 0 0 1px #E5E7EB; }
.paso.hecho .pt { background:var(--ant-hecho); box-shadow:0 0 0 1px var(--ant-hecho); }
.paso.actual .pt { background:var(--ant-actual); box-shadow:0 0 0 3px #FEF3C7; }
.paso .et { font-size:0.68rem; color:#6B7280; margin-top:6px; line-height:1.2; }
.paso.actual .et { color:#111827; font-weight:700; }
.paso .fch { font-size:0.66rem; color:#9CA3AF; }
.flujo-cabeza { display:flex; justify-content:space-between; align-items:baseline;
                gap:10px; flex-wrap:wrap; }
.flujo-bl { font-weight:700; color:#111827; }
.flujo-desc { color:#6B7280; font-size:0.86rem; }
.contador { display:inline-block; font-size:0.76rem; color:#4B5563; background:#F3F4F6;
            border-radius:6px; padding:2px 8px; margin:2px 6px 2px 0; }
.contador.ojo { background:#FEF3C7; color:#92400E; font-weight:700; }
.contador.mal { background:#FEE2E2; color:#991B1B; font-weight:800;
                box-shadow:inset 0 0 0 1px #FCA5A5; }
.contador.cerrado.ojo { background:#FFFBEB; box-shadow:inset 0 0 0 1px #FCD34D; font-weight:600; }
.contador.cerrado.mal { background:#FFF; box-shadow:inset 0 0 0 1px #FCA5A5; font-weight:700; }
.contador.bien { background:#DCFCE7; color:#166534; font-weight:600; }

/* ---------- Lista de embarques: UN solo markup ----------
   Desktop: grid de 7 columnas (se ve como tabla).
   Celular (<=640px): cada fila se convierte en tarjeta y cada celda
   muestra su etiqueta vía data-l. Sin duplicar el DOM.            */
.lista { border:1px solid var(--ant-borde); border-radius:12px; overflow:hidden;
         box-shadow:0 1px 4px rgba(17,24,39,0.06); background:#fff; }
.fila-head, .fila {
    display:grid;
    grid-template-columns: 1.15fr 1.45fr 1.25fr 0.75fr 0.9fr 0.85fr 1.15fr;
    gap:10px; align-items:center;
}
.fila-head { padding:10px 18px; font-size:0.67rem; text-transform:uppercase; letter-spacing:0.05em;
             color:#9CA3AF; background:#F9FAFB; border-bottom:1px solid var(--ant-borde); }
.fila { padding:12px 18px; font-size:0.87rem; background:#fff;
        border-bottom:1px solid #F3F4F6; border-left:4px solid #6B7280; }
.fila:last-child { border-bottom:none; }
.c-bl { font-weight:700; color:var(--ant-texto); word-break:break-all; }
.c-suave { color:var(--ant-suave); }
/* OC y EE van debajo del BL y no como columnas propias: solo las usan Aéreos y
   Carga Suelta, y dos columnas vacías en las demás categorías estropean la
   tabla en pantalla ancha y la tarjeta en celular. */
.c-ref { font-weight:500; font-size:0.78rem; color:var(--ant-suave);
         margin-top:2px; letter-spacing:0.2px; }
.badge { display:inline-block; padding:3px 11px; border-radius:999px;
         font-size:0.73rem; font-weight:700; color:#fff; white-space:nowrap; }
.badge.linea { background:#fff !important; color:#4B5563; border:1px solid var(--ant-borde); }

/* Ficha completa de un embarque */
.ficha { border:1px solid var(--ant-borde); border-radius:12px; overflow:hidden; background:#fff; }
.ficha-fila { display:grid; grid-template-columns: 210px 1fr; gap:12px;
              padding:9px 16px; border-bottom:1px solid #F3F4F6; font-size:0.9rem; }
.ficha-fila:last-child { border-bottom:none; }
.ficha-k { color:#9CA3AF; font-size:0.72rem; text-transform:uppercase; letter-spacing:0.04em;
           font-weight:700; padding-top:2px; }
.ficha-v { color:#1F2937; font-weight:600; word-break:break-word; }
.vacio { padding:26px 18px; text-align:center; color:var(--ant-suave); font-size:0.9rem; background:#fff; }

/* ---------- Selector de sección / categoría ----------
   Streamlit pinta el segmento activo con su color primario; se fuerza aquí y no
   solo en config.toml para que el aspecto no dependa de ese archivo. */
div[data-testid="stButtonGroup"] button {
    border-radius:8px !important; border:1px solid var(--ant-borde) !important;
    color:#4B5563 !important; font-weight:600 !important;
}
div[data-testid="stButtonGroup"] button:hover { background:#F3F7FC !important; color:#0C447C !important; }
div[data-testid="stButtonGroup"] button[aria-checked="true"],
div[data-testid="stButtonGroup"] button[data-testid="stBaseButton-segmented_controlActive"],
div[data-testid="stButtonGroup"] button[kind="segmented_controlActive"],
div[data-testid="stButtonGroup"] button[aria-selected="true"] {
    background:#DCEBFA !important; color:#0C447C !important;
    border:1px solid #2E86DE !important; box-shadow:none !important;
}
div[data-testid="stButtonGroup"] button[aria-checked="true"] p,
div[data-testid="stButtonGroup"] button[data-testid="stBaseButton-segmented_controlActive"] p,
div[data-testid="stButtonGroup"] button[kind="segmented_controlActive"] p { color:#0C447C !important; }

/* Botones primarios (Entrar, Guardar, Confirmar): azul del tablero */
button[kind="primary"], button[data-testid="stBaseButton-primary"],
button[data-testid="stBaseButton-primaryFormSubmit"] {
    background:#2E86DE !important; border-color:#2E86DE !important; color:#ffffff !important;
}
button[kind="primary"]:hover, button[data-testid="stBaseButton-primary"]:hover,
button[data-testid="stBaseButton-primaryFormSubmit"]:hover {
    background:#256FB8 !important; border-color:#256FB8 !important; color:#fff !important;
}

/* Impresión: una hoja limpia para llevar a reunión. */
@media print {
    [data-testid="stSidebar"], [data-testid="stToolbar"], [data-testid="stHeader"],
    .stButton, .stDownloadButton, [data-testid="stExpander"], .solo-pantalla,
    [data-testid="stTextInput"], [data-testid="stSelectbox"], [data-testid="stAlert"] {
        display:none !important;
    }
    .block-container { padding:0 !important; max-width:100% !important; }
    .lista { border:1px solid #999; box-shadow:none; }
    .fila { break-inside:avoid; }
    .flujo { break-inside:avoid; }
    .badge, .kpi-card, .paso .pt { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
}

/* ==========================================================================
   CELULAR (iOS y Android). Tres cosas que rompían la experiencia:
   1) Safari hace zoom automático al enfocar un input de menos de 16px.
   2) Los botones de Streamlit quedan por debajo de los 44px que Apple pide
      como área táctil mínima; con dedo grande se falla el clic.
   3) Cinco columnas de KPI en una pantalla de 380px son ilegibles.
   ========================================================================== */
@media (max-width: 820px) {
    input, textarea, select,
    .stTextInput input, .stNumberInput input, .stDateInput input,
    div[data-baseweb="input"] input, div[data-baseweb="select"] input {
        font-size:16px !important;
    }
}
@media (max-width: 720px) {
    .block-container { padding-left:0.8rem !important; padding-right:0.8rem !important; }
    .stButton button, .stDownloadButton button,
    div[data-testid="stButtonGroup"] button { min-height:44px !important; }
    div[data-testid="stButtonGroup"] { flex-wrap:wrap !important; gap:6px !important; }

    /* Los mismos 44px de área táctil, pero para selects y fechas: antes solo
       los botones los tenían y estos quedaban más bajos y fáciles de fallar
       con el dedo. */
    .stTextInput input, .stNumberInput input, .stDateInput input,
    div[data-baseweb="select"] > div { min-height:44px !important; }

    /* KPIs: rejilla de dos columnas en vez de cinco tiras aplastadas */
    div[class*="st-key-kpirow_"] div[data-testid="stHorizontalBlock"] {
        display:flex !important; flex-direction:row !important; flex-wrap:wrap !important; gap:8px !important;
    }
    div[class*="st-key-kpirow_"] div[data-testid="stColumn"],
    div[class*="st-key-kpirow_"] div[data-testid="column"] {
        flex:1 1 calc(50% - 8px) !important; min-width:calc(50% - 8px) !important;
        width:calc(50% - 8px) !important;
    }
}
@media (max-width: 640px) {
    .fila-head { display:none; }
    .lista { border:none; box-shadow:none; background:transparent; }
    .fila { display:block; border:1px solid var(--ant-borde); border-left-width:5px;
            border-radius:12px; margin-bottom:10px; padding:13px 16px;
            box-shadow:0 1px 4px rgba(17,24,39,0.06); }
    .fila > div { padding:2px 0; }
    .fila > div[data-l]:not(.c-bl):not(.c-badge)::before {
        content: attr(data-l) ": "; font-size:0.68rem; text-transform:uppercase;
        letter-spacing:0.04em; color:#9CA3AF; font-weight:700;
    }
    .c-bl { font-size:1.0rem; margin-bottom:2px; }
    .ant-title { font-size:1.55rem; }
    .kpi-value { font-size:1.55rem; }
    .ficha-fila { grid-template-columns:1fr; gap:2px; }

    /* El diagrama de 5 etapas se vuelve vertical: en horizontal, en 380px de
       ancho, las etiquetas se solapan y no se lee ninguna. */
    .flujo { flex-direction:column; }
    .paso { flex-direction:row; align-items:center; text-align:left; gap:10px; padding:4px 0; }
    .paso::before { top:-10px; left:15px; width:3px; height:22px; }
    .paso .et { margin-top:0; font-size:0.84rem; }
    .paso .fch { font-size:0.74rem; }
}
</style>
"""


def html_atraso_puerto(df, contexto: str = "") -> None:
    """Filtro clicable de lo que está parado en puerto — reemplaza las
    tarjetas fijas: el contador vive en el propio botón (Todos · N,
    Atrasados · N, Pendientes de pago · N, Costo · RD$X) y clicarlo filtra el
    detalle de abajo, así el número también sirve para llegar al embarque, no
    solo para mirarlo. Costo sale vacío ("—") mientras ninguna fila tenga
    Costo_Por_Dia lleno. Si no hay nada en puerto no dibuja nada: un cero
    permanente se vuelve invisible en una semana.

    `contexto` distingue el filtro cuando el mismo bloque aparece en más de
    una vista (Todos, una categoría puntual, la pestaña dedicada de puerto):
    sin esto, el estado de un filtro se pisaría con el de otro."""
    r = resumen_atraso_puerto(df)
    if not r["n_puerto"]:
        return

    clave_estado = f"filtro_puerto_{_slug_css(contexto)}"
    clave_click = f"filtro_puerto_click_{_slug_css(contexto)}"
    actual = st.session_state.get(clave_estado, "todos")

    opciones = [
        ("todos", f'Todos · {r["n_puerto"]}', "#0C447C"),
        ("atrasados", f'Atrasados · {r["n_atrasados"]}', "#D7263D"),
        ("pago", f'Pendientes de pago · {r["n_pendiente_pago"]}', "#B45309"),
        ("costo", "Costo · " + (_monto(r["costo_total"], r["moneda"]) if r["hay_tarifa"] else "—"), "#2E86DE"),
    ]

    slug_ctx = _slug_css(contexto)
    estilos = "".join(
        f'.st-key-fpuerto_{slug_ctx}_{op} button {{'
        f'border:1.5px solid {color} !important; border-radius:999px !important;'
        f'background:{color if actual == op else "#fff"} !important;'
        f'font-weight:700 !important;}} '
        f'.st-key-fpuerto_{slug_ctx}_{op} button p {{'
        f'color:{"#fff" if actual == op else color} !important;}} '
        for op, _, color in opciones
    )
    st.markdown(f"<style>{estilos}</style>", unsafe_allow_html=True)

    cols = st.columns(len(opciones))
    for col, (op, etiqueta, _color) in zip(cols, opciones):
        with col:
            with st.container(key=f"fpuerto_{slug_ctx}_{op}"):
                if st.button(etiqueta, key=f"btn_{clave_estado}_{op}", width="stretch"):
                    st.session_state[clave_estado] = op
                    st.session_state[clave_click] = True
                    rerun_fragmento()

    # El detalle (costo acumulado por embarque) se queda oculto hasta que se
    # clickee alguno de los botones de arriba, aunque sea "Todos": antes se
    # desplegaba de una vez y era la tabla más larga de toda la pantalla, sin
    # que nadie la hubiera pedido todavía. Los contadores siguen viéndose
    # siempre — viven en el propio botón, no en esta tabla.
    if not st.session_state.get(clave_click, False):
        return

    detalle = r["detalle"]
    if actual == "atrasados":
        detalle = [d for d in detalle if d["atrasado"]]
    elif actual == "pago":
        detalle = [d for d in detalle if d.get("pendiente_pago")]
    elif actual == "costo":
        detalle = [d for d in detalle if d["costo"] > 0]

    if not detalle:
        st.caption("No hay embarques que coincidan con este filtro.")
        return

    filas = []
    for d in detalle[:10]:
        ref = esc(d["bl"]) or "&mdash;"
        oc = (f' <span class="atoc">OC {esc(d["oc"])}</span>' if d["oc"]
              else ' <span class="atoc atsin">sin OC</span>')
        if d["costo"] > 0:
            monto = (f'<span class="atmonto">{_monto(d["costo"], r["moneda"])}'
                     f'<span class="attarifa">{_monto(d.get("tarifa", 0), r["moneda"])}/día</span></span>')
        else:
            monto = '<span class="atmonto" style="color:#9CA3AF;font-weight:600;">Costo —</span>'
        exceso = f' · <b>+{d["exceso"]} sobre el plazo</b>' if d["atrasado"] else ""
        filas.append(
            f'<div class="atfila{"" if d["atrasado"] else " atok"}">'
            f'<span class="atbl">{ref}</span>{oc}'
            f'<span class="atdias">{d["dias"]} días en puerto{exceso}</span>'
            f'{monto}</div>'
        )
    resto = len(detalle) - 10
    if resto > 0:
        filas.append(f'<div class="atfila atresto">y {resto} más</div>')

    st.markdown(
        '<div class="ejec-detalle"><div class="atttl" style="text-align:center;">'
        'Costo acumulado por embarque</div>' + "".join(filas) + "</div>",
        unsafe_allow_html=True,
    )


def rerun_fragmento():
    """st.rerun(scope="fragment") solo es válido cuando el rerun lo disparó un
    widget que vive dentro del fragmento; si no, Streamlit lanza una excepción.
    Esta envoltura cae al rerun normal en ese caso. (RerunException hereda de
    BaseException, así que el except Exception no se traga el rerun bueno.)"""
    try:
        st.rerun(scope="fragment")
    except Exception:
        st.rerun()


# ---------------------------------------------------------------------------
# COMPONENTES DE UI
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _logo_base64() -> str:
    """Si existe assets/logo.png (o .jpg) en el repo, se muestra en el encabezado.
    Si no, la app sigue con el ícono vectorial: no se inventa un logo."""
    for nombre in ("assets/logo.png", "assets/logo.jpg", "assets/logo.jpeg", "logo.png"):
        ruta = Path(nombre)
        if ruta.exists():
            tipo = "jpeg" if ruta.suffix.lower() in (".jpg", ".jpeg") else "png"
            return f"data:image/{tipo};base64," + base64.b64encode(ruta.read_bytes()).decode()
    return ""


def encabezado(datos: dict):
    anio = hoy_rd().year
    ultima = datos.get("ultima_carga")
    persona = str(datos.get("ultima_persona", "") or "").strip()
    if ultima:
        sello = (f"Información actualizada: {ultima.day:02d} {MESES_ES_CORTO[ultima.month]} "
                 f"{ultima.year}, {ultima.strftime('%I:%M %p').lstrip('0').lower()} (hora RD)")
        if persona:
            sello += f" · por {persona}"
    else:
        sello = "Sin registro de la última carga de información"
    logo = _logo_base64()
    img = f'<img class="ant-logo" src="{logo}" alt="Antillana Comercial">' if logo else ""
    st.markdown(
        f'<div class="ant-head">{img}'
        f'<span class="ant-eyebrow">'
        f'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0C447C" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/>'
        f'<path d="M9 21v-6h6v6"/></svg> Logística e Importaciones {anio}</span>'
        f'<div class="ant-title">Estatus de Cargas</div>'
        f'<div class="ant-rule"></div>'
        f'<div class="ant-sub">Antillana Comercial</div>'
        f'<div class="ant-stamp"><span class="ant-dot"></span> {esc(sello)}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def tarjeta_kpi(label: str, valor, color: str, sub: str = "") -> str:
    extra = f'<div class="kpi-sub">{esc(sub)}</div>' if sub else ""
    return (
        f'<div class="kpi-card" style="background:{color};">'
        f'<div class="kpi-label">{esc(label)}</div>'
        f'<div class="kpi-value">{valor}</div>{extra}</div>'
    )


def html_flujo(fechas: dict, etapa_actual: str, es_aerea: bool = False) -> str:
    """Diagrama de las 5 etapas en HTML/CSS puro.

    Sustituye al diagrama Plotly: con 20 embarques en puerto se creaban 20
    figuras interactivas por pantalla, que en celular son varios segundos de
    render y mucha batería. Además, en pantalla angosta el CSS lo convierte en
    lista vertical, que sí se lee; el Plotly horizontal se solapaba."""
    idx = INDICE_ETAPA.get(etapa_actual, -1)
    partes = ['<div class="flujo">']
    for i, etapa in enumerate(ETAPAS_PUERTO):
        clase = "hecho" if i < idx else ("actual" if i == idx else "")
        etiqueta = "Llegada al aeropuerto" if (es_aerea and i == 0) else etapa
        icono = "✈️" if (es_aerea and i == 0) else ICONO_ETAPA[etapa]
        fecha = fechas.get(etapa)
        partes.append(
            f'<div class="paso {clase}"><div class="pt">{icono}</div>'
            f'<div class="txt"><div class="et">{esc(etiqueta)}</div>'
            f'<div class="fch">{esc(formato_corto(fecha)) if fecha else "&nbsp;"}</div></div></div>'
        )
    partes.append("</div>")
    return "".join(partes)


def html_chips(conteos: dict, resaltar: str = "") -> str:
    """Resumen por etapa como chips que se acomodan solos. Reemplaza a cinco
    st.metric en fila, que en un celular de 380px quedaban ilegibles."""
    piezas = ['<div class="chips">']
    for etapa in ETAPAS_PUERTO:
        clase = "chip on" if etapa == resaltar else "chip"
        piezas.append(
            f'<span class="{clase}">{ICONO_ETAPA[etapa]} '
            f'{esc(ETIQUETA_CORTA_ETAPA.get(etapa, etapa))} <b>{int(conteos.get(etapa, 0))}</b></span>'
        )
    piezas.append("</div>")
    return "".join(piezas)


def marca(clase: str) -> str:
    if "mal" in clase:
        return "⚠ "
    if "ojo" in clase:
        return "● "
    # El ✓ acompaña al verde por la misma razón que el ⚠ acompaña al rojo: esto
    # se ve en celular y se imprime, y el color solo no alcanza.
    return "✓ " if "bien" in clase else ""


def _clase_contador(dias, limite, cerrado: bool = False) -> str:
    """Gris dentro del plazo, ámbar apenas lo pasa, rojo cuando ya se fue de las
    manos. Dos niveles y no uno para que el rojo signifique algo: si todo lo
    vencido sale rojo, en dos semanas nadie lo mira.

    `cerrado` es para la etapa que ya ocurrió (el pago ya se hizo): conserva el
    color pero con borde en vez de relleno. Que el atraso se vuelva gris al
    marcar "pagado" borraría de la pantalla la única prueba de que Finanzas
    tardó 16 días, que es justo el dato con el que se reclama.

    Una etapa cerrada DENTRO del plazo sale en verde: ahí ya hay un veredicto
    ("esto salió bien") y el gris no lo dice. Mientras la etapa sigue abierta se
    queda en gris, porque todavía no hay nada que juzgar. Sin plazo definido —el
    tránsito— tampoco hay veredicto posible, así que se queda en gris."""
    if not es_numero(dias) or not limite:
        return "contador"
    if dias <= limite:
        return "contador bien" if cerrado else "contador"
    nivel = "mal" if dias > limite * 2 else "ojo"
    return f"contador {nivel} cerrado" if cerrado else f"contador {nivel}"


def _chip(clase: str, etiqueta: str, dias) -> str:
    """Todos los contadores con el mismo formato 'Etiqueta: N días'. Antes cada
    uno tenía su redacción ('16 días tardó en pagarse') y en fila se leían como
    frases sueltas en vez de como una ficha de indicadores."""
    return f'<span class="{clase}">{marca(clase)}{etiqueta}: {texto_dias(dias)}</span>' 


def html_contadores(fila) -> str:
    """Los contadores operativos de un embarque, en línea."""
    piezas = []
    sla = sla_etapas()
    retirado = bool(fila.get("F_Almacen"))
    transito = fila.get("DiasTransito")
    if es_numero(transito):
        etiqueta = "Duración del tránsito" if fila.get("F_Puerto") else "En tránsito"
        piezas.append(_chip("contador", etiqueta, transito))
    dias_puerto = fila.get("DiasEnPuerto")
    if es_numero(dias_puerto):
        # El tiempo total en puerto abarca las cuatro etapas, así que se compara
        # contra la suma de sus plazos, no contra el de una sola. Ya retirado no
        # se esconde: se congela y queda en modo cerrado, que es la prueba de
        # cuánto costó ese despacho.
        lugar = lugar_de(fila.get("Categoria", ""))
        etiqueta = f"Duración en {lugar}" if retirado else f"En {lugar}"
        clase = _clase_contador(dias_puerto, sum(v for k, v in sla.items()
                                                 if k in SLA_ETAPA_DEFECTO), cerrado=retirado)
        piezas.append(_chip(clase, etiqueta, dias_puerto))
    solicitud = fila.get("DiasSolicitudPago")
    if es_numero(solicitud):
        pagado = bool(fila.get("F_Pago"))
        etiqueta = "Duración del pago" if pagado else "Esperando pago"
        clase = _clase_contador(solicitud, sla["Solicitud de pago a finanzas"], cerrado=pagado)
        piezas.append(_chip(clase, etiqueta, solicitud))
    espera = fila.get("DiasPagoDespacho")
    if es_numero(espera):
        etiqueta = "Del pago al retiro" if retirado else "Pagado sin retirar"
        clase = _clase_contador(espera, sla["Pago realizado"], cerrado=retirado)
        piezas.append(_chip(clase, etiqueta, espera))
    # Lo que lleva causado ESTE embarque desde que llegó a puerto. En rojo solo
    # cuando ya se pasó del plazo: antes de eso el gasto es normal, no una alarma.
    cfg_costo = costos_puerto()
    gasto = costo_demora_fila(fila, cfg_costo)
    if gasto:
        dias_p = fila.get("DiasEnPuerto")
        tarde = es_numero(dias_p) and dias_p > cfg_costo["umbral"]
        clase = "contador mal" if tarde else "contador"
        piezas.append(
            f'<span class="{clase}">{marca(clase)}{etiqueta_costo(fila.get("Categoria", ""))}: '
            f'{_monto(gasto, cfg_costo["moneda"])}</span>'
        )
    return "".join(piezas)


def _ref_oc_ee(fila) -> str:
    """OC y EE son los números con los que el equipo realmente rastrea la carga
    suelta y la aérea; no verlos en la lista obligaba a abrir cada embarque."""
    piezas = []
    for columna in (COL_OC, COL_EE):
        valor = str(fila.get(columna, "") or "").strip()
        # "nan" aparece cuando se concatenan categorías cuyas pestañas no tienen
        # la columna; mostrarlo sería peor que no mostrar nada.
        if valor and valor.upper() not in ("N/A", "NA", "NAN", "NONE", "-", "—"):
            piezas.append(f"{columna} {esc(valor)}")
    return f'<div class="c-ref">{" · ".join(piezas)}</div>' if piezas else ""


def render_lista(df: pd.DataFrame):
    """Un solo bloque HTML: tabla en desktop, tarjetas en celular (lo decide el CSS)."""
    if df.empty:
        st.markdown('<div class="lista"><div class="vacio">No hay embarques que coincidan con el filtro.</div></div>',
                    unsafe_allow_html=True)
        return

    partes = [
        '<div class="lista"><div class="fila-head">'
        "<div>BL</div><div>Descripción</div><div>Modelo/Serie</div><div>Cant.</div>"
        "<div>País</div><div>ETA</div><div>Estado</div></div>"
    ]
    for _, r in df.iterrows():
        color = STATUS_COLOR.get(r["EstadoTexto"], "#6B7280")
        etiqueta = texto_estado(r["EstadoTexto"], r["DiasRel"], r.get("Categoria", ""))
        etapa = str(r.get("EtapaActual", "")).strip()
        badge_etapa = ""
        if etapa:
            icono = "✈️" if (etapa == ETAPAS_PUERTO[0] and es_aereo(r.get("Categoria", ""))) \
                else ICONO_ETAPA[etapa]
            badge_etapa = (f'<span class="badge linea">{icono} '
                           f'{esc(etiqueta_etapa(etapa, r.get("Categoria", "")))}</span>')
        alerta = str(r.get("Alerta", "") or "").strip()
        badge_alerta = f'<span class="badge" style="background:{COLOR_ALERTA};">⚠ {esc(alerta)}</span>' if alerta else ""
        if r.get("BLRepetido"):
            badge_alerta += ('<span class="badge" style="background:#7C3AED;" '
                             'title="Este BL aparece en más de una fila">⧉ BL repetido</span>')
        partes.append(
            f'<div class="fila" style="border-left-color:{color};">'
            f'<div class="c-bl" data-l="BL">{esc(r[COL_BL]) if str(r[COL_BL]).strip() else "(sin BL)"}'
            f'{_ref_oc_ee(r)}</div>'
            f'<div class="c-suave" data-l="Descripción">{esc(r[COL_DESC])}</div>'
            f'<div class="c-suave" data-l="Modelo/Serie">{esc(r[COL_MODELO])}</div>'
            f'<div data-l="Cantidad">{esc(r[COL_CANT])}</div>'
            f'<div data-l="País">{esc(r[COL_PAIS])}</div>'
            f'<div data-l="ETA">{esc(formato_eta(r[COL_ETA]))}</div>'
            f'<div class="c-badge" data-l="Estado">'
            f'<span class="badge" style="background:{color};">{esc(etiqueta)}</span> '
            f'{badge_etapa} {badge_alerta}</div>'
            f"</div>"
        )
    partes.append("</div>")
    st.markdown("".join(partes), unsafe_allow_html=True)


def grafico_linea_tiempo(df: pd.DataFrame, key: str):
    """Qué viene encima, semana por semana. Responde la pregunta que un gerente
    hace de verdad ('¿qué me llega en las próximas semanas?')."""
    hoy = hoy_rd()
    inicio_semana = hoy - timedelta(days=hoy.weekday())
    etiquetas, valores, colores = [], [], []

    retrasados = int((df["EstadoTexto"] == EST_RETRASADO).sum())
    if retrasados:
        etiquetas.append("Retrasados")
        valores.append(retrasados)
        colores.append(STATUS_COLOR[EST_RETRASADO])

    vencidos = int((df["EstadoTexto"] == EST_PUERTO).sum())
    if vencidos:
        etiquetas.append("Por confirmar")
        valores.append(vencidos)
        colores.append(STATUS_COLOR[EST_PUERTO])

    con_fecha = df[df["ETAFecha"].notna() & ~df["EstadoTexto"].isin([EST_PUERTO, EST_RETRASADO])]
    for i in range(SEMANAS_HORIZONTE):
        desde = inicio_semana + timedelta(weeks=i)
        hasta = desde + timedelta(days=6)
        n = int(sum(1 for f in con_fecha["ETAFecha"] if desde <= f <= hasta))
        if i == 0:
            etiqueta = "Esta semana"
        elif i == 1:
            etiqueta = "Próxima semana"
        else:
            etiqueta = f"{desde.day} {MESES_ES_CORTO[desde.month]}"
        etiquetas.append(etiqueta)
        valores.append(n)
        colores.append(STATUS_COLOR[EST_PROXIMO] if i == 0 else STATUS_COLOR[EST_TRANSITO])

    lejanos = int(sum(1 for f in con_fecha["ETAFecha"]
                      if f > inicio_semana + timedelta(weeks=SEMANAS_HORIZONTE) - timedelta(days=1)))
    if lejanos:
        etiquetas.append(f"+{SEMANAS_HORIZONTE} sem")
        valores.append(lejanos)
        colores.append("#9CA3AF")

    fig = go.Figure(
        data=[go.Bar(x=etiquetas, y=valores, marker=dict(color=colores),
                     text=[v if v else "" for v in valores], textposition="outside",
                     hovertemplate="%{x}: %{y} embarque(s)<extra></extra>")]
    )
    fig.update_layout(
        margin=dict(t=18, b=10, l=10, r=10), height=260,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#374151", size=11), showlegend=False,
        xaxis=dict(showgrid=False, title=""),
        yaxis=dict(showgrid=True, gridcolor="#F3F4F6", showticklabels=False, title=""),
        bargap=0.35, dragmode=False,
    )
    st.plotly_chart(fig, width="stretch",
                    config={"displayModeBar": False, "staticPlot": True, "responsive": True},
                    key=f"tl_{key}")


def grafico_paises(df: pd.DataFrame, key: str):
    """Barras por país en HTML/CSS, no en Plotly.

    Plotly recalcula el ancho del eje según el largo de las etiquetas y lo vuelve
    a hacer en cada redibujado: en celular eso es lo que hacía que las barras se
    movieran solas al girar el teléfono o al abrir el teclado. Estas barras miden
    en porcentaje del ancho disponible, así que no dependen del texto ni de
    JavaScript, y se imprimen bien.

    Se pierde el clic-para-filtrar que tenía la versión de Plotly; el filtro por
    país sigue disponible en la barra de filtros de abajo."""
    if COL_PAIS not in df.columns or df.empty:
        return
    serie = df[COL_PAIS].replace("", NO_ESPECIFICADO).value_counts()
    if serie.empty:
        return
    tope = int(serie.max()) or 1
    piezas = ['<div class="paises"><div class="atttl">Por país de origen</div>']
    for i, (pais, n) in enumerate(serie.items()):
        ancho = max(4.0, (int(n) / tope) * 100.0)
        color = PALETA_PAISES[i % len(PALETA_PAISES)]
        piezas.append(
            f'<div class="pfila"><div class="pnom">{esc(str(pais))}</div>'
            f'<div class="pbarra"><span style="width:{ancho:.1f}%;background:{color}"></span></div>'
            f'<div class="pval">{int(n)}</div></div>'
        )
    piezas.append("</div>")
    st.markdown("".join(piezas), unsafe_allow_html=True)


def _ficha_embarque(fila):
    """Todos los campos del embarque, incluidas las columnas que alguien haya
    agregado en el Sheet y que la app no gestiona."""
    es_aerea = fila["Categoria"] == CATEGORIA_AEREA
    campos = [
        ("BL", fila[COL_BL]),
        ("Descripción", fila[COL_DESC]),
        ("Modelo / Serie", fila[COL_MODELO]),
        ("Cantidad", fila[COL_CANT]),
        ("País de origen", fila[COL_PAIS]),
        ("Categoría", fila["Categoria"]),
        ("ETA", formato_eta(fila[COL_ETA])),
        ("Estado", texto_estado(fila["EstadoTexto"], fila["DiasRel"], fila["Categoria"])),
    ]
    if fila["Categoria"] in CATEGORIAS_CON_OC_EE:
        if str(fila.get(COL_OC, "")).strip():
            campos.append(("OC", fila[COL_OC]))
        if str(fila.get(COL_EE, "")).strip():
            campos.append(("EE", fila[COL_EE]))
    if fila.get("F_Salida"):
        campos.append(("Fecha de salida", formato_eta(fila["F_Salida"])))
    if es_numero(fila.get("DiasTransito")):
        etiqueta = "Duración del tránsito" if fila.get("F_Puerto") else "En tránsito"
        campos.append((etiqueta, texto_dias(fila["DiasTransito"])))

    etapa = str(fila.get("EtapaActual", "")).strip()
    if etapa:
        etiqueta_etapa = "Llegada al aeropuerto" if (es_aerea and etapa == ETAPAS_PUERTO[0]) else etapa
        campos.append(("Etapa actual", etiqueta_etapa))
        fechas = fechas_flujo_de_fila(fila)
        rotulos = {
            "Llegada a puerto": "Llegada al aeropuerto" if es_aerea else "Llegada a puerto",
            "Recepción y declaración": "Recepción y declaración",
            "Solicitud de pago a finanzas": "Solicitud de pago enviada",
            "Pago realizado": "Pago realizado",
            "Recibido en almacén": "Recibido en almacén",
        }
        for nombre_etapa, fecha in fechas.items():
            if fecha:
                campos.append((rotulos[nombre_etapa], formato_eta(fecha)))
        retirado = bool(fila.get("F_Almacen"))
        if es_numero(fila.get("DiasEnPuerto")):
            lugar = lugar_de(fila.get("Categoria", ""))
            campos.append((f"Duración en {lugar}" if retirado else f"En {lugar}",
                           texto_dias(fila["DiasEnPuerto"])))
        if es_numero(fila.get("DiasSolicitudPago")):
            etiqueta = "Duración del pago" if fila.get("F_Pago") else "Esperando pago"
            campos.append((etiqueta, texto_dias(fila["DiasSolicitudPago"])))
        if es_numero(fila.get("DiasPagoDespacho")):
            campos.append(("Del pago al retiro" if retirado else "Pagado sin retirar",
                           texto_dias(fila["DiasPagoDespacho"])))
        _cfg_costo = costos_puerto()
        _gasto = costo_demora_fila(fila, _cfg_costo)
        if _gasto:
            _es_almacenaje = etiqueta_costo(fila.get("Categoria", "")) == "Costo por Almacenaje"
            campos.append(("Tarifa de almacenaje" if _es_almacenaje else "Tarifa en puerto",
                           f'{_monto(costo_dia_fila(fila, _cfg_costo), _cfg_costo["moneda"])} por día'))
            campos.append(("Costo acumulado por Almacenaje" if _es_almacenaje else "Costo acumulado en puerto",
                           _monto(_gasto, _cfg_costo["moneda"])))
    if str(fila.get("Alerta", "") or "").strip():
        campos.append(("⚠ Atención", fila["Alerta"]))

    for extra in columnas_extra(fila.to_frame().T):
        campos.append((extra.replace("_", " "), fila[extra]))
    if str(fila.get(COL_ACTUALIZACION, "")).strip():
        autor = str(fila.get(COL_ACTUALIZADO_POR, "")).strip()
        campos.append(("Última actualización",
                       f"{fila[COL_ACTUALIZACION]}" + (f" · {autor}" if autor else "")))
    campos.append(("Fila en el Sheet", fila.get("FilaSheet", "—")))

    filas_html = "".join(
        f'<div class="ficha-fila"><div class="ficha-k">{esc(k)}</div>'
        f'<div class="ficha-v">{esc(v)}</div></div>'
        for k, v in campos
    )
    st.markdown(f'<div class="ficha">{filas_html}</div>', unsafe_allow_html=True)
    if etapa:
        st.markdown(html_flujo(fechas_flujo_de_fila(fila), etapa, es_aerea=es_aerea),
                    unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
def selector_horizontal(label: str, opciones: list, key: str, default=None, formato=None,
                        ancho: str = "stretch"):
    """Segmented control cuando la versión de Streamlit lo trae; radio horizontal
    si no. Sustituye a st.tabs para las categorías: con tabs, Streamlit ejecuta el
    cuerpo de TODAS las pestañas en cada rerun aunque el usuario vea una sola."""
    formato = formato or (lambda x: str(x))
    extra = {} if key in st.session_state else {"default": default or opciones[0]}
    if hasattr(st, "segmented_control"):
        elegido = st.segmented_control(
            label, opciones, key=key, format_func=formato,
            label_visibility="collapsed", width=ancho, **extra,
        )
    else:
        elegido = st.radio(label, opciones, key=key, horizontal=True,
                           format_func=formato, label_visibility="collapsed")
    return elegido or (default or opciones[0])


def _resumen_ejecutivo(df: pd.DataFrame, recibidas_mes: int) -> str:
    """Una línea que contesta '¿cómo vamos hoy?' sin obligar a leer el tablero
    entero. Es lo primero que ve el presidente al abrir desde el celular."""
    hoy = hoy_rd()
    fin_semana = hoy + timedelta(days=(6 - hoy.weekday()))
    esta_semana = int(sum(1 for f in df["ETAFecha"] if f and hoy <= f <= fin_semana))
    por_confirmar = int(((df["EstadoTexto"] == EST_PUERTO) &
                         (df["EtapaActual"].astype(str).str.strip() == "")).sum())
    retrasados = int((df["EstadoTexto"] == EST_RETRASADO).sum())
    en_puerto = len(_en_proceso(df))
    trabados = int((df["Alerta"].astype(str).str.strip() != "").sum())

    piezas = [f"<b>{len(df)}</b> embarques activos"]
    if esta_semana:
        piezas.append(f"<b>{esta_semana}</b> con llegada esta semana")
    if en_puerto:
        piezas.append(f"<b>{en_puerto}</b> en proceso en puerto")
    if por_confirmar:
        piezas.append(f"<b>{por_confirmar}</b> por confirmar llegada")
    if retrasados:
        piezas.append(f"<b>{retrasados}</b> retrasados")
    if trabados:
        piezas.append(f"<b>{trabados}</b> pasados de tiempo en su etapa")
    if recibidas_mes:
        piezas.append(f"<b>{recibidas_mes}</b> recibidos en {MESES_ES[hoy.month].lower()}")
    return f'<div class="resumen">{" · ".join(piezas)}.</div>'


def _panel_alertas(df: pd.DataFrame, tope: int = 6):
    """Dónde se está trabando. No es lo mismo saber que hay 14 embarques en
    puerto que saber que 4 llevan más de una semana esperando que Finanzas
    pague: lo primero es un dato, lo segundo es una decisión."""
    con_alerta = df[df["Alerta"].astype(str).str.strip() != ""]
    if con_alerta.empty:
        return
    con_alerta = con_alerta.sort_values("AlertaDias", ascending=False, na_position="last")

    resumen = {}
    for etapa, texto in zip(con_alerta["EtapaActual"], con_alerta["Alerta"]):
        clave = ETIQUETA_CORTA_ETAPA.get(etapa, "Retrasado en tránsito") if etapa else "Retrasado en tránsito"
        resumen[clave] = resumen.get(clave, 0) + 1
    detalle = " · ".join(f"{n} en {nombre.lower()}" for nombre, n in resumen.items())

    filas = []
    for _, r in con_alerta.head(tope).iterrows():
        filas.append(
            f'<div class="alerta-fila"><span class="dias">{int(r["AlertaDias"])} d</span>'
            f'<span class="bl">{esc(r[COL_BL]) or "(sin BL)"}</span>'
            f'<span class="que">{esc(r[COL_DESC])} · {esc(r["Categoria"])} — {esc(r["Alerta"])}</span></div>'
        )
    extra = (f'<div class="alerta-fila"><span class="que">…y {len(con_alerta) - tope} más. '
             f'Ordena la lista por "Más días detenido" para verlos todos.</span></div>'
             if len(con_alerta) > tope else "")
    st.markdown(
        f'<div class="alerta-caja"><div class="alerta-titulo">⚠ Dónde se está trabando · '
        f'{len(con_alerta)} embarque(s) — {esc(detalle)}</div>{"".join(filas)}{extra}</div>',
        unsafe_allow_html=True,
    )


def _archivar(fila, clave: str, etiqueta: str = "Marcar como recibido"):
    """Botón de archivo + rescate cuando faltan etapas.

    El candado que impide archivar sin haber pasado por el flujo se mantiene
    (si no, el histórico se llena de embarques sin trazabilidad, que fue lo que
    pasó con el BL 142-161-355-5). Lo que cambia es que ya no es un callejón sin
    salida: si faltan etapas, la app las pide aquí mismo con la fecha REAL de
    cada una, en vez de rellenarlas solas con la fecha de hoy — que era rápido,
    pero metía datos falsos en los contadores de desempeño."""
    bl = str(fila[COL_BL]).strip()
    categoria = fila["Categoria"]
    n_fila = fila.get("FilaSheet")
    pendiente_key = f"faltan_{clave}"

    if st.button(etiqueta, key=f"rec_{clave}", type="primary", width="stretch"):
        ok, mensaje = marcar_como_recibido(bl, categoria, fila_sugerida=n_fila)
        if ok:
            registrar_log("Recibido", bl, categoria, f"fila {n_fila}")
            invalidar_caches()
            st.rerun()
        elif str(mensaje).startswith("FALTAN_ETAPAS::"):
            st.session_state[pendiente_key] = mensaje.split("::", 1)[1].split("|")
            rerun_fragmento()
        else:
            st.error(mensaje)

    faltan = st.session_state.get(pendiente_key)
    if not faltan:
        return

    with st.form(f"form_faltan_{clave}"):
        st.warning(
            "Este embarque no pasó por todas las etapas. Registra la fecha real en que ocurrió "
            "cada una y se archiva completo, con su trazabilidad."
        )
        fechas = {}
        for etapa in faltan:
            fechas[etapa] = st.date_input(f"{ICONO_ETAPA.get(etapa, '•')} {etapa}", value=hoy_rd(),
                                          format="DD/MM/YYYY", key=f"falta_{clave}_{_slug_css(etapa)}")
        fecha_almacen = st.date_input(f"{ICONO_ETAPA[ETAPAS_PUERTO[-1]]} {ETAPAS_PUERTO[-1]}",
                                      value=hoy_rd(), format="DD/MM/YYYY", key=f"almacen_{clave}")
        c1, c2 = st.columns(2)
        confirmar = c1.form_submit_button("Guardar y archivar", type="primary", width="stretch")
        cancelar = c2.form_submit_button("Cancelar", width="stretch")

    if cancelar:
        st.session_state.pop(pendiente_key, None)
        rerun_fragmento()
    if confirmar:
        ok, mensaje = marcar_como_recibido(bl, categoria, fila_sugerida=n_fila,
                                           fechas_faltantes=fechas, fecha_almacen=fecha_almacen)
        if ok:
            st.session_state.pop(pendiente_key, None)
            registrar_log("Recibido (etapas completadas)", bl, categoria,
                          "; ".join(f"{e}={f.isoformat()}" for e, f in fechas.items()))
            invalidar_caches()
            st.rerun()
        else:
            st.error(mensaje)


def _panel_en_proceso(df: pd.DataFrame, rol: str, contexto: str):
    """Un diagrama por embarque, siempre visible. El admin avanza con un solo
    botón (la etapa siguiente, que es el 95% de los casos) y corrige fechas o
    retrocede desde el desplegable, que es lo raro.

    'contexto' identifica desde dónde se llama: el mismo embarque puede
    aparecer en más de una vista y sin esto las claves de sus widgets chocan.

    Si arriba se activó un filtro del panel de costo/atraso (html_atraso_puerto,
    mismo 'contexto'), los embarques que lo cumplen se muestran PRIMERO: antes
    el filtro de arriba y este diagrama no se hablaban, así que filtrar por
    "Pendientes de pago" arriba no cambiaba en nada el orden de abajo, y había
    que ir dándole "ver más" para encontrar el resto."""
    es_admin = rol == "admin"
    filtro_activo = st.session_state.get(f"filtro_puerto_{_slug_css(contexto)}", "todos")
    cfg_costo = costos_puerto()
    df = df.copy()
    # Con el filtro en "todos" (el estado por defecto, y el que está activo la
    # mayoría de las veces que se teclea en el buscador de arriba porque este
    # panel vive en un @st.fragment) _cumple_filtro_puerto() siempre da True
    # para cualquier fila, así que el resultado es fijo: se evita el .apply()
    # fila por fila -y el parseo de tarifas que dispara el filtro "costo"- en
    # el caso más común, que es también el que se repite en cada tecla.
    if filtro_activo == "todos":
        df["_NoCumpleFiltro"] = False
    else:
        df["_NoCumpleFiltro"] = ~df.apply(lambda f: _cumple_filtro_puerto(f, filtro_activo, cfg_costo), axis=1)
    orden = df.sort_values(["_NoCumpleFiltro", "AlertaDias", "EtapaIdx", COL_ETA],
                           ascending=[True, False, True, True], na_position="last")

    if filtro_activo != "todos":
        n_cumple = int((~orden["_NoCumpleFiltro"]).sum())
        if n_cumple:
            st.caption(f"Mostrando primero los {n_cumple} embarque(s) que cumplen el filtro activo de arriba.")

    clave_ver = f"ver_todos_{_slug_css(contexto)}"
    ver_todos = st.session_state.get(clave_ver, False)
    visibles = orden if ver_todos else orden.head(TOPE_PROCESO)

    fecha_evento = hoy_rd()
    if es_admin:
        fecha_evento = st.date_input(
            "Fecha del evento que vas a confirmar abajo", value=hoy_rd(), format="DD/MM/YYYY",
            key=f"fecha_evento_{_slug_css(contexto)}",
            help="Se usa para la etapa que confirmes con los botones de abajo. Cámbiala si estás "
                 "registrando algo que pasó otro día; así los contadores no mienten.",
        )

    for _, fila in visibles.iterrows():
        bl = str(fila[COL_BL]).strip()
        categoria = fila["Categoria"]
        etapa = str(fila.get("EtapaActual", "")).strip()
        es_aerea = categoria == CATEGORIA_AEREA
        clave = clave_fila(contexto, categoria, bl or "sin_bl", fila.get("FilaSheet", ""))
        alerta = str(fila.get("Alerta", "") or "").strip()
        # Con dos filas del mismo BL es fácil trabajar sobre la equivocada y creer
        # que la app "no guardó". El número de fila del Sheet lo desambigua.
        marca_dup = (f'<span class="badge" style="background:#7C3AED;">⧉ BL repetido · '
                     f'fila {esc(fila.get("FilaSheet", "?"))}</span>'
                     if fila.get("BLRepetido") else "")
        raro = str(fila.get("FlujoRaro", "") or "").strip()

        st.markdown(
            f'<div class="flujo-cabeza"><span class="flujo-bl">{esc(bl) if bl else "(sin BL)"} '
            f'{marca_dup}</span>'
            f'<span class="flujo-desc">{esc(fila[COL_DESC])} · {esc(categoria)}</span></div>'
            + html_flujo(fechas_flujo_de_fila(fila), etapa, es_aerea=es_aerea)
            + html_contadores(fila)
            + (f'<div class="alerta-fila" style="color:#B45309;font-weight:600;">⚠ {esc(alerta)}</div>'
               if alerta else "")
            + (f'<div class="alerta-fila" style="color:#991B1B;font-weight:600;">⚠ Fechas fuera de '
               f'orden: {esc(raro)} Los contadores de este embarque no son confiables.</div>'
               if raro else ""),
            unsafe_allow_html=True,
        )

        if es_admin and bl:
            idx = INDICE_ETAPA.get(etapa, -1)
            siguiente = ETAPAS_PUERTO[idx + 1] if 0 <= idx < len(ETAPAS_PUERTO) - 1 else None
            if idx == len(ETAPAS_PUERTO) - 1:
                # Alguien llenó la fecha de almacén a mano en el Sheet: la fila
                # sigue activa y hay que poder archivarla igual.
                _archivar(fila, clave, etiqueta="Archivar en el histórico")
            elif siguiente == ETAPAS_PUERTO[-1]:
                _archivar(fila, clave, etiqueta=f"{ICONO_ETAPA[siguiente]} {siguiente} (archiva)")
            elif siguiente:
                if st.button(f"{ICONO_ETAPA[siguiente]} Confirmar: {siguiente}",
                             key=f"av_{clave}", type="primary", width="stretch"):
                    ok, mensaje = avanzar_estado_puerto(bl, categoria, siguiente,
                                                        fila_sugerida=fila.get("FilaSheet"),
                                                        fecha=fecha_evento)
                    if ok:
                        registrar_log("Avance de etapa", bl, categoria,
                                      f"{siguiente} el {fecha_evento.isoformat()}")
                        invalidar_caches()
                        st.rerun()
                    else:
                        st.error(mensaje)

            with st.expander("Corregir fecha o retroceder etapa"):
                c1, c2 = st.columns([1.4, 1])
                # El selector solo lista las primeras 4 etapas (la de almacén se
                # maneja aparte, con el botón de archivar) — así que el índice
                # nunca puede pasar de la última posición de esa lista. Sin este
                # tope, un embarque cuya etapa activa YA es "Recibido en almacén"
                # (idx=4, p. ej. porque alguien llenó Fecha_Despacho a mano en el
                # Sheet sin pasar por 'Marcar como recibido') le pasaba index=4 a
                # un selectbox de 4 opciones (0-3) y tumbaba la pantalla.
                indice_selector = min(max(idx, 0), len(ETAPAS_PUERTO) - 2)
                nueva_etapa = c1.selectbox("Etapa", ETAPAS_PUERTO[:-1],
                                           index=indice_selector, key=f"et_{clave}")
                fecha_corregida = c2.date_input("Fecha real", value=hoy_rd(), format="DD/MM/YYYY",
                                                key=f"fc_{clave}")
                st.caption("Elegir una etapa anterior borra las fechas de las etapas posteriores: "
                           "así es como se corrige un avance hecho por error.")
                if st.button("Guardar corrección", key=f"corr_{clave}", width="stretch"):
                    ok, mensaje = avanzar_estado_puerto(bl, categoria, nueva_etapa,
                                                        fila_sugerida=fila.get("FilaSheet"),
                                                        fecha=fecha_corregida, sobrescribir=True)
                    if ok:
                        accion = "Retroceso de etapa" if INDICE_ETAPA[nueva_etapa] < idx else "Corrección de etapa"
                        registrar_log(accion, bl, categoria,
                                      f"{nueva_etapa} el {fecha_corregida.isoformat()}")
                        invalidar_caches()
                        st.rerun()
                    else:
                        st.error(mensaje)
        st.divider()

    if len(orden) > TOPE_PROCESO:
        etiqueta = (f"Mostrar los {len(orden) - TOPE_PROCESO} restantes" if not ver_todos
                    else f"Mostrar solo los {TOPE_PROCESO} más urgentes")
        if st.button(etiqueta, key=f"btn_{clave_ver}", width="stretch"):
            st.session_state[clave_ver] = not ver_todos
            rerun_fragmento()


def _panel_confirmacion(df: pd.DataFrame, tab_key: str):
    """El ETA vencido no dice si la mercancía llegó, solo que la fecha pasó.
    Este panel hace la pregunta directa —¿llegó, sí o no?— y con la respuesta el
    embarque entra al flujo de 5 etapas (Sí) o se marca como retrasado (No).
    Va a la vista, sin desplegable, porque es lo único de la pantalla que exige
    acción hoy."""
    tiene_etapa = df["EtapaActual"].astype(str).str.strip().ne("")
    pendientes = df[(df["EstadoTexto"] == EST_PUERTO) & ~tiene_etapa]
    retrasados = df[(df["EstadoTexto"] == EST_RETRASADO) & ~tiene_etapa]
    if pendientes.empty and retrasados.empty:
        return

    TOPE = 12
    if not pendientes.empty:
        st.markdown(
            f'<div class="conf-titulo">¿Llegó a Puerto/Aeropuerto? · {len(pendientes)} '
            f'embarque(s) con la fecha vencida</div>',
            unsafe_allow_html=True,
        )
        fecha_llegada = st.date_input(
            "Fecha de llegada a registrar", value=hoy_rd(), format="DD/MM/YYYY",
            key=f"fecha_llegada_{_slug_css(tab_key)}",
            help="Se aplica al embarque que confirmes abajo. Si llegó el lunes y lo estás "
                 "registrando el jueves, cámbiala: de aquí sale el contador de tránsito.",
        )
    else:
        fecha_llegada = hoy_rd()

    def _fila_confirmacion(r, ya_retrasado: bool):
        bl = str(r[COL_BL]).strip()
        categoria = r["Categoria"]
        etiqueta = texto_estado(r["EstadoTexto"], r["DiasRel"], r.get("Categoria", ""))
        color = STATUS_COLOR.get(r["EstadoTexto"], "#6B7280")
        c1, c2, c3 = st.columns([3.2, 1.3, 1.5])
        c1.markdown(
            f'<div class="conf-fila"><span class="conf-bl">{esc(bl or "(sin BL)")}</span>'
            f'<span class="conf-desc">{esc(r[COL_DESC])} · {esc(categoria)}</span>'
            f'<span class="badge" style="background:{color};">{esc(etiqueta)}</span></div>',
            unsafe_allow_html=True,
        )
        if not bl:
            c2.caption("Sin BL: no se puede gestionar")
            return
        clave = clave_fila(tab_key, categoria, bl, r.get("FilaSheet", ""))
        es_aerea = categoria == CATEGORIA_AEREA
        texto_si = "Sí, llegó al aeropuerto" if es_aerea else "Sí, llegó a puerto"
        if c2.button(texto_si, key=f"si_llego_{clave}", type="primary", width="stretch"):
            ok, mensaje = avanzar_estado_puerto(bl, categoria, ETAPAS_PUERTO[0],
                                                fila_sugerida=r.get("FilaSheet"), fecha=fecha_llegada)
            if ok:
                registrar_log("Llegada confirmada", bl, categoria, fecha_llegada.isoformat())
                invalidar_caches()
                st.rerun()
            else:
                st.error(mensaje)
        if ya_retrasado:
            c3.button("Sigue retrasado", key=f"sigue_{clave}", width="stretch", disabled=True)
            c3.caption("Actualiza el ETA en Editar")
        elif c3.button("No, está retrasado", key=f"no_llego_{clave}", width="stretch"):
            ok, mensaje = marcar_estatus_llegada(bl, categoria, VALOR_RETRASADO,
                                                 fila_sugerida=r.get("FilaSheet"))
            if ok:
                registrar_log("Marcado como retrasado", bl, categoria, f"ETA {r[COL_ETA]}")
                invalidar_caches()
                st.rerun()
            else:
                st.error(mensaje)

    for _, r in pendientes.head(TOPE).iterrows():
        _fila_confirmacion(r, False)
    if len(pendientes) > TOPE:
        st.caption(f"…y {len(pendientes) - TOPE} más. Filtra por estado 'En Puerto' para verlos todos.")

    if not retrasados.empty:
        st.markdown(
            f'<div class="conf-titulo" style="margin-top:14px;">Ya verificados como retrasados · {len(retrasados)}</div>',
            unsafe_allow_html=True,
        )
        for _, r in retrasados.head(TOPE).iterrows():
            _fila_confirmacion(r, True)
        if len(retrasados) > TOPE:
            st.caption(f"…y {len(retrasados) - TOPE} más.")
    st.write("")


def tabla_exportable(df: pd.DataFrame) -> pd.DataFrame:
    """La vista tal como se está viendo, lista para Excel: sin columnas internas,
    con el estado ya redactado, las fechas legibles y los contadores del flujo —
    que es lo que hace falta para analizar demoras fuera de la app."""
    if df.empty:
        return pd.DataFrame(columns=[COL_BL, "Descripción", "Estado"])
    salida = pd.DataFrame({
        "BL": df[COL_BL],
        "OC": df[COL_OC] if COL_OC in df.columns else "",
        "EE": df[COL_EE] if COL_EE in df.columns else "",
        "Descripción": df[COL_DESC],
        "Modelo/Serie": df[COL_MODELO],
        "Cantidad": df[COL_CANT],
        "País de origen": df[COL_PAIS],
        "ETA": [formato_eta(v) for v in df[COL_ETA]],
        "Estado": [texto_estado(e, d, c) for e, d, c in
                   zip(df["EstadoTexto"], df["DiasRel"], df["Categoria"])],
        "Etapa": df["EtapaActual"],
        "Categoría": df["Categoria"],
        "Salida": [formato_eta(f) if f else "" for f in df["F_Salida"]],
        "Llegada a puerto": [formato_eta(f) if f else "" for f in df["F_Puerto"]],
        "Declaración": [formato_eta(f) if f else "" for f in df["F_Declaracion"]],
        "Solicitud de pago": [formato_eta(f) if f else "" for f in df["F_Solicitud"]],
        "Pago realizado": [formato_eta(f) if f else "" for f in df["F_Pago"]],
        "Días en tránsito": df["DiasTransito"],
        "Días en puerto": df["DiasEnPuerto"],
        "Días de solicitud a pago": df["DiasSolicitudPago"],
        # Columna pensada para el reclamo, no para la pantalla: permite filtrar
        # en Excel cuántos pagos se pasaron del plazo y por cuánto.
        "Pago fuera de plazo": [
            "Sí" if (es_numero(d) and d > sla_etapas()["Solicitud de pago a finanzas"]) else "No"
            for d in df["DiasSolicitudPago"]],
        "Días de pago a retiro": df["DiasPagoDespacho"],
        "Tarifa en puerto/día": [costo_dia_fila(f) for _, f in df.iterrows()],
        "Costo acumulado en puerto": [costo_demora_fila(f) for _, f in df.iterrows()],
        "Alerta operativa": df["Alerta"],
    })
    for extra in columnas_extra(df):
        salida[extra] = df[extra]
    if COL_ACTUALIZACION in df.columns:
        salida["Última actualización"] = df[COL_ACTUALIZACION]
    if COL_ACTUALIZADO_POR in df.columns:
        salida["Actualizado por"] = df[COL_ACTUALIZADO_POR]
    return salida.reset_index(drop=True)


@st.cache_data(show_spinner=False, ttl=600)
def _df_a_excel(df: pd.DataFrame, hoja: str) -> bytes:
    """Cacheado a propósito: st.download_button exige los bytes por adelantado,
    así que sin caché se reconstruía el Excel completo en CADA rerun de la
    pantalla (cada tecla del buscador, cada clic de filtro)."""
    buffer = io.BytesIO()
    nombre = "".join(c for c in hoja if c.isalnum() or c == " ")[:28] or "Datos"
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=nombre)
    return buffer.getvalue()


@st.fragment
def _render_categoria(df: pd.DataFrame, rol: str, tab_key: str, recibidas_mes: int):
    if df.empty:
        st.info("No hay embarques en esta categoría.")
        return

    conteo = df["EstadoTexto"].value_counts().to_dict()
    proximos_n = conteo.get(EST_PROXIMO, 0)
    # "Por confirmar llegada" cuenta solo lo que de verdad falta por responder,
    # no todo lo que tiene el ETA vencido: un embarque con ETA vencido que ya
    # entró al flujo no está pendiente de nada.
    etapa_col = df["EtapaActual"].astype(str).str.strip()
    en_puerto_n = int(((df["EstadoTexto"] == EST_PUERTO) & (etapa_col == "")).sum())
    retrasados_n = conteo.get(EST_RETRASADO, 0)
    sin_fecha_n = conteo.get(EST_SIN_FECHA, 0)

    valores = [v for v in df.get("ValorNum", []) if es_numero(v)]
    valor_total = sum(valores) if valores else None
    valor_puerto = sum(v for v, e, et in zip(df.get("ValorNum", []), df["EstadoTexto"], etapa_col)
                       if es_numero(v) and e == EST_PUERTO and et == "") if valores else None

    st.markdown(_resumen_ejecutivo(df, recibidas_mes), unsafe_allow_html=True)

    # -------------------- KPIs --------------------
    kpis = [
        ("Total en tránsito", len(df), COLOR_TOTAL, "Todos", "total"),
        (f"Próximos {UMBRAL_PROXIMO} días", proximos_n, STATUS_COLOR[EST_PROXIMO], EST_PROXIMO, "proximos"),
        ("Por confirmar llegada", en_puerto_n, STATUS_COLOR[EST_PUERTO], EST_PUERTO, "enpuerto"),
        ("Retrasados", retrasados_n, STATUS_COLOR[EST_RETRASADO], EST_RETRASADO, "retrasados"),
        (f"Recibidas en {MESES_ES[hoy_rd().month]}", recibidas_mes, COLOR_RECIBIDAS_MES, "__historico__", "recibidas"),
    ]
    # OJO con la clave del contenedor: Streamlit la usa TAL CUAL como clase CSS
    # (st-key-<clave>). Con espacios o acentos el selector no engancha, por eso
    # se construye con un slug ASCII.
    clave = _slug_css(tab_key)
    estilos = "".join(
        f".st-key-kpi_{clave}_{slug} button {{"
        f"background:{color} !important; color:#fff !important; border:none !important;"
        f"border-radius:14px !important; width:100% !important; min-height:92px !important;"
        f"text-align:center !important; padding:14px 10px !important;"
        f"box-shadow:0 2px 8px rgba(17,24,39,0.12) !important; transition:filter .15s ease;}} "
        f".st-key-kpi_{clave}_{slug} button > div {{"
        f"display:flex !important; flex-direction:column !important; align-items:center !important;"
        f"justify-content:center !important; width:100% !important;}} "
        f".st-key-kpi_{clave}_{slug} button p {{margin:0 !important; color:#fff !important;"
        f"text-align:center !important; width:100% !important;}} "
        f".st-key-kpi_{clave}_{slug} button p:first-of-type {{"
        f"font-size:0.68rem !important; font-weight:700 !important; letter-spacing:0.05em !important;"
        f"opacity:0.92 !important; line-height:1.2 !important;}} "
        f".st-key-kpi_{clave}_{slug} button p:last-of-type {{"
        f"font-size:1.9rem !important; font-weight:800 !important; line-height:1.05 !important;"
        f"margin-top:6px !important;}} "
        f".st-key-kpi_{clave}_{slug} button:hover {{filter:brightness(0.93); color:#fff !important;}} "
        f".st-key-kpi_{clave}_{slug} button:focus {{color:#fff !important;"
        f"box-shadow:0 0 0 3px rgba(17,24,39,0.15) !important;}}"
        for _, _, color, _, slug in kpis
    )
    st.markdown(f"<style>{estilos}</style>", unsafe_allow_html=True)

    # El contenedor con clave permite que el CSS los ponga en rejilla de 2 en celular.
    with st.container(key=f"kpirow_{clave}"):
        cols = st.columns(len(kpis))
        for col, (label, valor, color, filtro, slug) in zip(cols, kpis):
            with col:
                with st.container(key=f"kpi_{clave}_{slug}"):
                    if st.button(f"{label.upper()}\n\n{valor}", key=f"btn_{clave}_{slug}", width="stretch"):
                        if filtro == "__historico__":
                            st.session_state["seccion"] = "Histórico"
                            st.rerun()
                        st.session_state[f"estado_{tab_key}"] = filtro
                        rerun_fragmento()

    # -------------------- Valor en tránsito (solo si el Sheet lo trae) --------------------
    if valor_total:
        v1, v2 = st.columns(2)
        v1.markdown(
            tarjeta_kpi("Valor en tránsito", formato_dinero(valor_total), "#0C447C",
                        f"{len(valores)} de {len(df)} embarque(s) con valor declarado"),
            unsafe_allow_html=True,
        )
        v2.markdown(
            tarjeta_kpi("Valor detenido en puerto", formato_dinero(valor_puerto or 0), "#B45309",
                        "mercancía llegada y sin confirmar recepción"),
            unsafe_allow_html=True,
        )
        st.write("")

    st.write("")
    _panel_alertas(df)

    if rol == "admin":
        _panel_confirmacion(df, tab_key)
        if sin_fecha_n:
            st.warning(
                f"{sin_fecha_n} embarque(s) tienen un ETA que la app no puede interpretar y quedan fuera de "
                "los conteos por fecha. Revísalos en Herramientas → Normalizar fechas."
            )

    # -------------------- GRÁFICOS --------------------
    with st.container():
        st.markdown('<div class="solo-pantalla">', unsafe_allow_html=True)
        g1, g2 = st.columns([1.5, 1])
        with g1:
            st.caption("Llegadas previstas")
            grafico_linea_tiempo(df, tab_key)
        with g2:
            st.caption("Origen")
            grafico_paises(df, tab_key)
        st.markdown("</div>", unsafe_allow_html=True)

    # -------------------- EN PROCESO EN PUERTO --------------------
    en_proceso = _en_proceso(df)
    if not en_proceso.empty:
        st.markdown("**En proceso en puerto**")
        st.markdown(html_chips(en_proceso["EtapaActual"].value_counts().to_dict()), unsafe_allow_html=True)
        html_atraso_puerto(en_proceso, contexto=tab_key)
        _panel_en_proceso(en_proceso, rol, contexto=tab_key)

    st.divider()

    # -------------------- FILTROS --------------------
    paises = ["Todos"] + sorted({p for p in df[COL_PAIS] if str(p).strip()})
    estados = ["Todos"] + [e for e in STATUS_ORDER]
    etapas = ["Todas", "Sin confirmar llegada"] + ETAPAS_PUERTO[:-1]
    criterios = ["Urgencia", "Más días detenido", "ETA más próximo", "ETA más lejano",
                 "BL", "País", "Descripción"]
    if valor_total:
        criterios.append("Valor")

    f1, f2, f3 = st.columns([2, 1, 1])
    busqueda = f1.text_input("Buscar", key=f"busca_{tab_key}",
                             placeholder="BL, descripción, modelo, OC…", label_visibility="collapsed")
    pais_sel = f2.selectbox("País", paises, key=f"pais_{tab_key}", label_visibility="collapsed")
    estado_sel = f3.selectbox("Estado", estados, key=f"estado_{tab_key}", label_visibility="collapsed")

    f4, f5, f6 = st.columns([1, 1, 1])
    etapa_sel = f4.selectbox("Etapa", etapas, key=f"etapa_filtro_{tab_key}")
    orden_sel = f5.selectbox("Ordenar por", criterios, key=f"orden_{tab_key}")
    with f6:
        st.write("")
        st.write("")
        if st.button("Limpiar filtros", key=f"limpiar_{tab_key}", width="stretch"):
            for k in (f"busca_{tab_key}", f"pais_{tab_key}", f"estado_{tab_key}",
                      f"orden_{tab_key}", f"etapa_filtro_{tab_key}"):
                st.session_state.pop(k, None)
            st.session_state.pop(f"firma_pais_{tab_key}", None)
            rerun_fragmento()

    filtrado = df
    if pais_sel != "Todos":
        filtrado = filtrado[filtrado[COL_PAIS] == pais_sel]
    if estado_sel != "Todos":
        filtrado = filtrado[filtrado["EstadoTexto"] == estado_sel]
    if etapa_sel == "Sin confirmar llegada":
        filtrado = filtrado[filtrado["EtapaActual"].astype(str).str.strip() == ""]
    elif etapa_sel != "Todas":
        filtrado = filtrado[filtrado["EtapaActual"] == etapa_sel]
    if busqueda and busqueda.strip():
        filtrado = filtrado[filtrado["Buscar"].str.contains(_norm(busqueda), regex=False, na=False)]
    filtrado = ordenar_vista(filtrado, orden_sel)

    resumen_valor = ""
    if valor_total:
        parcial = sum(v for v in filtrado.get("ValorNum", []) if es_numero(v))
        if parcial:
            resumen_valor = f" · {formato_dinero(parcial)}"
    st.caption(f"Mostrando {len(filtrado)} de {len(df)} embarque(s){resumen_valor}")

    render_lista(filtrado)

    # -------------------- EXPORTAR Y VER DETALLE --------------------
    e1, e2 = st.columns([1, 2])
    with e1:
        st.download_button(
            "Descargar esta vista",
            data=_df_a_excel(tabla_exportable(filtrado), f"{tab_key}"[:28] or "Vista"),
            file_name=f"embarques_{_slug_css(tab_key)}_{hoy_rd().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_vista_{tab_key}",
            width="stretch",
        )

    if not filtrado.empty:
        with st.expander("Ver ficha completa de un embarque", expanded=False):
            opciones_det = _etiquetas_desambiguadas(filtrado, con_categoria=False, largo_desc=40)
            elegido = st.selectbox("Embarque", opciones_det, key=f"detalle_{tab_key}",
                                   label_visibility="collapsed")
            _ficha_embarque(filtrado.iloc[opciones_det.index(elegido)])

    # -------------------- ACCIONES DE ADMIN --------------------
    if rol == "admin":
        st.write("")
        with st.expander("Acciones sobre un embarque", expanded=False):
            _panel_acciones(filtrado, tab_key)


def _panel_acciones(df: pd.DataFrame, tab_key: str):
    """Un selector y tres botones, en vez de una lista infinita de botones fila
    por fila (que con 50 embarques hacía la página inusable)."""
    con_bl = df[df[COL_BL].astype(str).str.strip() != ""]
    sin_bl = df[df[COL_BL].astype(str).str.strip() == ""]

    if not sin_bl.empty:
        nombres = ", ".join(str(d)[:40] for d in sin_bl[COL_DESC].head(5))
        st.caption(f"{len(sin_bl)} fila(s) sin BL asignado no se pueden gestionar desde aquí ({nombres}).")

    if con_bl.empty:
        st.info("No hay embarques con BL en la vista actual.")
        return

    opciones = _etiquetas_desambiguadas(con_bl)
    elegido = st.selectbox("Embarque", opciones, key=f"sel_accion_{tab_key}")
    fila = con_bl.iloc[opciones.index(elegido)]
    bl, categoria = str(fila[COL_BL]).strip(), fila["Categoria"]
    n_fila = fila.get("FilaSheet")
    etapa = str(fila.get("EtapaActual", "")).strip()
    clave = clave_fila("acc", tab_key, bl, n_fila)

    if etapa:
        st.markdown(html_flujo(fechas_flujo_de_fila(fila), etapa,
                               es_aerea=(categoria == CATEGORIA_AEREA)) + html_contadores(fila),
                    unsafe_allow_html=True)
    elif fila["EstadoTexto"] in (EST_PUERTO, EST_RETRASADO):
        texto = "'Sí, llegó al aeropuerto'" if categoria == CATEGORIA_AEREA else "'Sí, llegó a puerto'"
        st.caption(f"Llegada sin confirmar todavía — usa {texto} en la sección de confirmación, arriba.")
    st.write("")

    _archivar(fila, clave)
    c2, c3 = st.columns(2)
    if c2.button("Editar", key=f"edit_{clave}", width="stretch"):
        st.session_state["editar_bl"] = bl
        st.session_state["editar_fila"] = n_fila
        st.session_state["seccion"] = "Editar"
        st.rerun()
    if c3.button("Eliminar", key=f"del_{clave}", width="stretch"):
        st.session_state[f"confirmar_del_{tab_key}"] = (bl, categoria, n_fila)
        rerun_fragmento()

    pendiente = st.session_state.get(f"confirmar_del_{tab_key}")
    if pendiente:
        bl_pend, cat_pend, fila_pend = pendiente
        st.warning(f"¿Eliminar definitivamente el BL {bl_pend} (fila {fila_pend} de {cat_pend})? "
                   "No se puede deshacer. Si el embarque llegó, usa 'Marcar como recibido' para "
                   "conservarlo en el histórico.")
        d1, d2, _ = st.columns([1, 1, 3])
        if d1.button("Sí, eliminar", key=f"si_del_{tab_key}", type="primary"):
            ok, mensaje = eliminar_embarque(bl_pend, cat_pend, fila_sugerida=fila_pend)
            st.session_state.pop(f"confirmar_del_{tab_key}", None)
            if ok:
                registrar_log("Eliminado", bl_pend, cat_pend, f"fila {fila_pend}")
                invalidar_caches()
                st.rerun()
            else:
                st.error(mensaje)
        if d2.button("Cancelar", key=f"no_del_{tab_key}"):
            st.session_state.pop(f"confirmar_del_{tab_key}", None)
            rerun_fragmento()


def mostrar_dashboard(datos: dict):
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    encabezado(datos)

    # Los avisos técnicos son instrucciones de trabajo: solo los ve quien puede
    # ejecutarlas. Al espectador no le sirven y le restan confianza en el dato.
    es_admin = st.session_state.get("rol") == "admin"
    if datos["error"]:
        st.error(datos["error"] if es_admin
                 else "No se pudieron leer todos los datos en este momento. Intenta recargar en unos segundos.")
    if es_admin:
        for aviso in datos.get("avisos", []):
            st.warning(aviso)

    df_todo = datos["activos"]
    if df_todo.empty:
        st.info("Todavía no hay embarques cargados.")
        return

    df_todo = enriquecer(df_todo)
    recibidas_mes = contar_recibidas_mes(datos["historico"])
    rol = st.session_state.get("rol", "viewer")

    opciones = ["Todos"] + [c for c in CATEGORIAS if (df_todo["Categoria"] == c).any()]
    en_proceso_df = _en_proceso(df_todo)
    if not en_proceso_df.empty:
        opciones.append(VISTA_EN_PROCESO_PUERTO)
    conteos = df_todo["Categoria"].value_counts().to_dict()
    etiquetas = {
        c: (f"{c} · {len(df_todo)}" if c == "Todos"
            else f"{c} · {len(en_proceso_df)}" if c == VISTA_EN_PROCESO_PUERTO
            else f"{c} · {conteos.get(c, 0)}")
        for c in opciones
    }
    seleccion = selector_horizontal(
        "Categoría", opciones, key="categoria_activa", formato=lambda c: etiquetas.get(c, c),
    )

    if seleccion == VISTA_EN_PROCESO_PUERTO:
        st.markdown(html_chips(en_proceso_df["EtapaActual"].value_counts().to_dict()),
                    unsafe_allow_html=True)
        html_atraso_puerto(en_proceso_df, contexto=VISTA_EN_PROCESO_PUERTO)
        _panel_alertas(en_proceso_df)
        st.divider()
        _panel_en_proceso(en_proceso_df, rol, contexto=VISTA_EN_PROCESO_PUERTO)
        return

    sub = df_todo if seleccion == "Todos" else df_todo[df_todo["Categoria"] == seleccion]
    _render_categoria(sub, rol, seleccion, recibidas_mes)
