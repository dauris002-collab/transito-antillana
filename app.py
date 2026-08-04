"""
Antillana Comercial · Visibilidad de embarques en tránsito
===========================================================

Cambios estructurales frente a la versión anterior (resumen para mantenimiento):

1.  Encabezados tolerantes a acentos/mayúsculas/espacios. Antes, el Sheet decía
    "Fecha_Actualización" (con tilde) y el código escribía "Fecha_Actualizacion":
    la columna nunca se llenaba y nadie se enteraba. Ahora todo el mapeo
    columna<->dato pasa por _norm(), así que el acento deja de importar.
2.  UNA sola llamada a la API de Google por refresco (values_batch_get) en vez de
    5-10. El cuello de cuota estaba ahí y en _asegurar_columna, que leía la fila
    de encabezados una vez por columna.
3.  Fechas: parser único que entiende ISO, dd/mm, dd-mm, mes en texto español y
    seriales de Excel/Sheets. Todo lo que la app escribe sale en ISO (AAAA-MM-DD)
    con value_input_option="RAW", que es el único formato que no depende del
    locale del archivo. Hay una herramienta de admin para normalizar lo viejo.
4.  Orden cronológico real (por fecha parseada) y prioridad operativa: primero lo
    atrasado, después lo inminente, después lo lejano.
5.  Auditoría: cada alta, edición, recepción, reversa y borrado queda registrada
    en la pestaña "Log" con usuario, fecha/hora RD y detalle.
6.  Escapado HTML de todo valor que venga del Sheet (una descripción con "<"
    rompía el render).
7.  Un solo bloque de HTML para la lista, que el CSS convierte en tabla (desktop)
    o tarjetas (celular). Antes se renderizaban las dos y el celular descargaba
    ambas.
8.  Fecha_Salida (opcional, todas las categorías) + flujo detallado de puerto
    en categorías marítimas (todas menos Aéreos): Estado_Puerto con 4 etapas
    ("Llegada a puerto" -> "Recepción y declaración" -> "Solicitud de pago a
    finanzas" -> "Pago realizado"), cada una con su propia fecha (ver
    COLUMNA_FECHA_ETAPA) y diagrama con ícono por etapa. 3 contadores: (1)
    salida -> llegada a puerto, se congela ahí; (2) solicitud de pago -> pago
    realizado; (3) pago realizado -> hoy, mientras espera despacho. No hay
    etapa "Despachado": equivale a la entrada a almacén, así que "Marcar como
    recibido" (bloqueado hasta llegar a "Pago realizado" en categorías
    marítimas) es la misma acción, no un paso aparte. El botón "¿Ya llegó?"
    para categorías marítimas ya no archiva directo: confirma solo la llegada
    a puerto.
"""

from __future__ import annotations

import base64
import html
import io
import re
import time
import unicodedata
from secrets import token_urlsafe
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
import gspread.exceptions
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

# ---------------------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Antillana · Embarques en Tránsito",
    page_icon="🚢",
    layout="wide",
)

ZONA_RD = ZoneInfo("America/Santo_Domingo")

COL_BL = "BL"
COL_DESC = "Descripcion"
COL_MODELO = "Modelo_Serie"
COL_CANT = "Cantidad"
COL_PAIS = "Pais_Origen"
COL_ETA = "Llegada a Puerto (ETA)"
COL_DIAS_PUERTO = "Dias en puerto"
COL_ACTUALIZACION = "Fecha_Actualizacion"  # el Sheet lo tiene con tilde; _norm lo resuelve
COL_ACTUALIZADO_POR = "Actualizado_Por"    # la app la crea sola la primera vez que escribe
COL_ESTATUS_LLEGADA = "Estatus_Llegada"    # vacío = sin confirmar; "Retrasado" = se verificó que NO llegó
# Fecha de salida del origen: opcional, la trae quien la conoce (booking del
# forwarder/naviera). Alimenta el Contador 1 (salida -> puerto). Aplica a
# cualquier categoría, no solo a las marítimas.
COL_FECHA_SALIDA = "Fecha_Salida"
# Flujo detallado de puerto: solo tiene sentido en categorías marítimas (ver
# CATEGORIAS_PUERTO más abajo). Vacío = todavía no se confirmó la llegada
# física a puerto, aunque el ETA ya haya vencido. Cada etapa estampa su propia
# fecha la primera vez que se marca (ver COLUMNA_FECHA_ETAPA). No hay columna
# de fecha de despacho: "despachado" ya no es una etapa del flujo, es la
# misma acción que archivar como recibido (ver ETAPAS_PUERTO más abajo).
COL_ESTADO_PUERTO = "Estado_Puerto"
COL_FECHA_LLEGADA_PUERTO = "Fecha_Llegada_Puerto"    # llegada física a puerto
COL_FECHA_DECLARACION = "Fecha_Declaracion"          # inicia la recepción y declaración
COL_FECHA_SOLICITUD_PAGO = "Fecha_Solicitud_Pago"    # cuándo se le pidió el pago a Finanzas
COL_FECHA_PAGO = "Fecha_Pago"                        # Finanzas confirma que ya pagó

# Columnas opcionales: si existen en el Sheet, la app las usa; si no, ni se
# mencionan. Así se pueden agregar Orden_Compra, Cliente, Puerto_Destino o
# Valor_USD desde Google Sheets sin tocar una línea de código.
NOMBRES_VALOR = {"valor_usd", "valor", "monto", "valor_cif", "monto_usd", "valor us$", "valor us"}
MAX_FILAS_LECTURA = 20000

REQUIRED_COLUMNS = [COL_BL, COL_DESC, COL_MODELO, COL_CANT, COL_PAIS, COL_ETA]
ALL_COLUMNS = REQUIRED_COLUMNS + [COL_DIAS_PUERTO, COL_ACTUALIZACION, COL_ACTUALIZADO_POR,
                                  COL_ESTATUS_LLEGADA, COL_FECHA_SALIDA, COL_ESTADO_PUERTO,
                                  COL_FECHA_LLEGADA_PUERTO, COL_FECHA_DECLARACION,
                                  COL_FECHA_SOLICITUD_PAGO, COL_FECHA_PAGO]
# Columnas que la app calcula o gestiona internamente y que no se muestran como
# "campos extra" del embarque.
COLUMNAS_INTERNAS = {"Categoria", "FilaSheet", "EstadoTexto", "DiasRel", "ETAFecha",
                     "Prioridad", "OrdenSec", COL_DIAS_PUERTO, "DiasTransito",
                     "DiasSolicitudPago", "DiasPagoDespacho"}

CATEGORIAS = ["Equipos", "Generadores", "Aéreos", "Carga Suelta", "Consolidados"]
# El flujo detallado de puerto solo aplica a carga marítima. Aéreos pasa por
# aeropuerto, no por Caucedo/Río Haina, y mantiene el comportamiento simple de
# siempre (ETA vencido -> confirmar sí/no llegó).
CATEGORIA_AEREA = "Aéreos"
CATEGORIAS_PUERTO = [c for c in CATEGORIAS if c != CATEGORIA_AEREA]

# 3 contadores operativos:
#   1) Salida -> Llegada a puerto (Contador de tránsito, se congela ahí)
#   2) Solicitud de pago -> Pago realizado (Contador de pago)
#   3) Pago realizado -> hoy (Contador de espera de despacho; corre mientras
#      siga activo, y deja de verse solo porque el embarque se archiva al
#      marcarlo como recibido — no hay una etapa "Despachado" que lo detenga)
# "Despachado" YA NO es una etapa: equivale a la entrada a almacén, así que es
# la misma acción de siempre, "Marcar como recibido" — no un paso intermedio.
ETAPAS_PUERTO = [
    "Llegada a puerto",
    "Recepción y declaración",
    "Solicitud de pago a finanzas",
    "Pago realizado",
]
INDICE_ETAPA = {e: i for i, e in enumerate(ETAPAS_PUERTO)}
# Cada etapa estampa su propia columna de fecha la primera vez que se marca.
COLUMNA_FECHA_ETAPA = {
    "Llegada a puerto": COL_FECHA_LLEGADA_PUERTO,
    "Recepción y declaración": COL_FECHA_DECLARACION,
    "Solicitud de pago a finanzas": COL_FECHA_SOLICITUD_PAGO,
    "Pago realizado": COL_FECHA_PAGO,
}
COLOR_ETAPA_PENDIENTE = "#E5E7EB"
COLOR_ETAPA_ACTUAL = "#F0B90B"
COLOR_ETAPA_HECHA = "#2E7D32"
# Nombres completos para la ficha y el panel de acciones, donde hay espacio;
# versión corta solo para el badge de la lista compacta.
ETIQUETA_CORTA_ETAPA = {
    "Llegada a puerto": "Llegada a puerto",
    "Recepción y declaración": "Recepción/declaración",
    "Solicitud de pago a finanzas": "Solicitud de pago",
    "Pago realizado": "Pago realizado",
}
# Un ícono por etapa en el diagrama: barco al llegar, hoja de documentos al
# iniciar la declaración — pedido explícito.
ICONO_ETAPA = {
    "Llegada a puerto": "🚢",
    "Recepción y declaración": "📄",
    "Solicitud de pago a finanzas": "💰",
    "Pago realizado": "✅",
}
# Vista adicional en el dashboard (no es una pestaña del Sheet): cruza todas las
# categorías marítimas y muestra todo lo que ya confirmó llegada a puerto y
# sigue activo (no archivado). Nace de un pedido explícito de Dauris: ver de un
# vistazo, sin entrar categoría por categoría, todo lo que está en puerto.
VISTA_EN_PROCESO_PUERTO = "En proceso (puerto)"

RECIBIDO_SHEET = "Recibido (Mes)"
LOG_SHEET = "Log"

COLUMNAS_RECIBIDO = [
    COL_BL, COL_DESC, COL_MODELO, COL_CANT, COL_PAIS, COL_ETA,
    "Fecha_Recibido", "Categoria_Origen", "Registrado_Por", COL_ACTUALIZACION,
    COL_ACTUALIZADO_POR,
]
COLUMNAS_LOG = ["Fecha_Hora", "Usuario", "Accion", "BL", "Categoria", "Detalle"]

UMBRAL_PROXIMO = 3          # días para considerar un embarque "Próximo a llegar"
CACHE_TTL = 45              # segundos de caché de lectura
LARGO_PIN = 4               # dígitos del PIN; si algún día usas PIN más largos, cámbialo aquí
VIDA_SESION_MINUTOS = 5     # la sesión se recuerda mientras no pasen 5 minutos sin actividad
MAX_INTENTOS_SESION = 5
MAX_FALLOS_GLOBAL = 25      # freno global: el bloqueo por sesión se evade en incógnito
VENTANA_FALLOS = 10 * 60
BLOQUEO_SEGUNDOS = 15 * 60
SEMANAS_HORIZONTE = 8

EST_TRANSITO = "En tránsito"
EST_PROXIMO = "Próximo a llegar"
EST_PUERTO = "En Puerto"
# "En Puerto" y "Retrasado" son cosas distintas y antes se confundían: el ETA
# vencido solo dice que la fecha pasó, no si la mercancía llegó. Cuando alguien
# responde "No, no llegó", el embarque pasa a Retrasado y deja de contarse como
# mercancía esperando confirmación en puerto.
EST_RETRASADO = "Retrasado"
EST_SIN_FECHA = "Sin fecha válida"
VALOR_RETRASADO = "Retrasado"

STATUS_COLOR = {
    EST_TRANSITO: "#2E86DE",
    EST_PROXIMO: "#5C6BC0",
    EST_PUERTO: "#F0B90B",
    EST_RETRASADO: "#D7263D",
    "Recibido": "#2E7D32",
    EST_SIN_FECHA: "#6B7280",
}
COLOR_TOTAL = "#17A2B8"
COLOR_RECIBIDAS_MES = "#2E7D32"
# Orden operativo: lo que exige acción primero.
STATUS_ORDER = [EST_RETRASADO, EST_PUERTO, EST_PROXIMO, EST_TRANSITO, EST_SIN_FECHA]
PRIORIDAD_ESTADO = {EST_RETRASADO: 0, EST_PUERTO: 1, EST_PROXIMO: 2, EST_TRANSITO: 3, EST_SIN_FECHA: 4}
PALETA_PAISES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}
MESES_ES_CORTO = {
    1: "ene", 2: "feb", 3: "mar", 4: "abr", 5: "may", 6: "jun",
    7: "jul", 8: "ago", 9: "sep", 10: "oct", 11: "nov", 12: "dic",
}
MESES_NUM = {}
for _n, _nombre in MESES_ES.items():
    MESES_NUM[_nombre.lower()] = _n
    MESES_NUM[MESES_ES_CORTO[_n]] = _n

CUSTOM_CSS = """
<style>
:root {
    --ant-azul: #0C447C;
    --ant-borde: #E5E7EB;
    --ant-texto: #111827;
    --ant-suave: #6B7280;
}
/* El menú de secciones es el primer elemento de la página: sin este aire, en
   algunas resoluciones queda medio escondido bajo la barra fija de Streamlit. */
.block-container { padding-top: 4.2rem; }

.nav-rotulo { font-size:0.70rem; text-transform:uppercase; letter-spacing:0.08em;
              color:#9CA3AF; font-weight:700; margin:0 0 4px 2px; }

/* Panel de confirmación de llegadas */
.conf-titulo { font-size:0.78rem; text-transform:uppercase; letter-spacing:0.06em;
               font-weight:800; color:#92400E; background:#FEF7E6;
               border-left:4px solid #F0B90B; padding:8px 14px; border-radius:8px;
               margin:6px 0 10px 0; }
.conf-fila { display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:6px 2px; }
.conf-bl { font-weight:700; color:#111827; font-size:0.92rem; }
.conf-desc { color:#6B7280; font-size:0.85rem; }

/* ---------- Encabezado ---------- */
.ant-head { text-align:center; margin: 0 0 1.4rem 0; }
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
.ant-stamp {
    display:inline-flex; align-items:center; gap:7px;
    font-size:0.76rem; color:var(--ant-suave); margin-top:0.5rem;
}
.ant-dot { width:8px; height:8px; border-radius:50%; background:#22C55E; display:inline-block; }

/* ---------- KPIs ---------- */
.kpi-card { border-radius:14px; padding:14px 18px; min-height:92px; height:100%;
            display:flex; flex-direction:column; justify-content:center; align-items:center;
            text-align:center; box-shadow:0 2px 8px rgba(17,24,39,0.12); }
.kpi-label {
    font-size:0.70rem; font-weight:700; text-transform:uppercase; letter-spacing:0.06em;
    color:rgba(255,255,255,0.92); margin-bottom:6px;
}
.kpi-value { font-size:2.0rem; font-weight:800; line-height:1; color:#fff; }
.kpi-sub { font-size:0.70rem; color:rgba(255,255,255,0.88); margin-top:6px; }

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
.fila-head {
    padding:10px 18px; font-size:0.67rem; text-transform:uppercase; letter-spacing:0.05em;
    color:#9CA3AF; background:#F9FAFB; border-bottom:1px solid var(--ant-borde);
}
.fila {
    padding:12px 18px; font-size:0.87rem; background:#fff;
    border-bottom:1px solid #F3F4F6; border-left:4px solid #6B7280;
}
.fila:last-child { border-bottom:none; }
.c-bl { font-weight:700; color:var(--ant-texto); word-break:break-all; }
.c-suave { color:var(--ant-suave); }
.badge {
    display:inline-block; padding:3px 11px; border-radius:999px;
    font-size:0.73rem; font-weight:700; color:#fff; white-space:nowrap;
}
/* Ficha completa de un embarque */
.ficha { border:1px solid var(--ant-borde); border-radius:12px; overflow:hidden; background:#fff; }
.ficha-fila { display:grid; grid-template-columns: 210px 1fr; gap:12px;
              padding:9px 16px; border-bottom:1px solid #F3F4F6; font-size:0.9rem; }
.ficha-fila:last-child { border-bottom:none; }
.ficha-k { color:#9CA3AF; font-size:0.72rem; text-transform:uppercase; letter-spacing:0.04em;
           font-weight:700; padding-top:2px; }
.ficha-v { color:#1F2937; font-weight:600; word-break:break-word; }
@media (max-width: 640px) { .ficha-fila { grid-template-columns:1fr; gap:2px; } }

.ant-logo { max-height:58px; margin-bottom:0.5rem; }

/* ---------- Selector de sección / categoría ----------
   Streamlit pinta el segmento activo con su color primario, que por defecto es
   rojo y con texto rojo sobre blanco casi no se lee. Se fuerza aquí en el CSS y
   no solo en config.toml para que el aspecto no dependa de que ese archivo esté
   bien puesto en el repo. */
div[data-testid="stButtonGroup"] button {
    border-radius:8px !important;
    border:1px solid var(--ant-borde) !important;
    color:#4B5563 !important;
    font-weight:600 !important;
}
div[data-testid="stButtonGroup"] button:hover { background:#F3F7FC !important; color:#0C447C !important; }
div[data-testid="stButtonGroup"] button[aria-checked="true"],
div[data-testid="stButtonGroup"] button[data-testid="stBaseButton-segmented_controlActive"],
div[data-testid="stButtonGroup"] button[kind="segmented_controlActive"],
div[data-testid="stButtonGroup"] button[aria-selected="true"] {
    background:#DCEBFA !important;
    color:#0C447C !important;
    border:1px solid #2E86DE !important;
    box-shadow:none !important;
}
div[data-testid="stButtonGroup"] button[aria-checked="true"] p,
div[data-testid="stButtonGroup"] button[data-testid="stBaseButton-segmented_controlActive"] p,
div[data-testid="stButtonGroup"] button[kind="segmented_controlActive"] p { color:#0C447C !important; }

/* Botones primarios (Entrar, Guardar, Confirmar): azul del tablero, no el rojo
   por defecto. Los KPI tienen su propia regla y no se ven afectados. */
button[kind="primary"], button[data-testid="stBaseButton-primary"],
button[data-testid="stBaseButton-primaryFormSubmit"] {
    background:#2E86DE !important;
    border-color:#2E86DE !important;
    color:#ffffff !important;
}
button[kind="primary"]:hover, button[data-testid="stBaseButton-primary"]:hover,
button[data-testid="stBaseButton-primaryFormSubmit"]:hover {
    background:#256FB8 !important; border-color:#256FB8 !important; color:#fff !important;
}

/* Impresión: una hoja limpia para llevar a reunión. Se van los gráficos, los
   botones, la barra lateral y los filtros; queda el encabezado y la lista. */
@media print {
    [data-testid="stSidebar"], [data-testid="stToolbar"], [data-testid="stHeader"],
    .stButton, .stDownloadButton, [data-testid="stExpander"], .solo-pantalla,
    [data-testid="stTextInput"], [data-testid="stSelectbox"], [data-testid="stAlert"] {
        display:none !important;
    }
    .block-container { padding:0 !important; max-width:100% !important; }
    .lista { border:1px solid #999; box-shadow:none; }
    .fila { break-inside:avoid; }
    .badge { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
    .kpi-card { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
}

.vacio { padding:26px 18px; text-align:center; color:var(--ant-suave); font-size:0.9rem; background:#fff; }

@media (max-width: 640px) {
    .fila-head { display:none; }
    .lista { border:none; box-shadow:none; background:transparent; }
    .fila {
        display:block; border:1px solid var(--ant-borde); border-left-width:5px;
        border-radius:12px; margin-bottom:10px; padding:13px 16px;
        box-shadow:0 1px 4px rgba(17,24,39,0.06);
    }
    .fila > div { padding:2px 0; }
    .fila > div[data-l]:not(.c-bl):not(.c-badge)::before {
        content: attr(data-l) ": ";
        font-size:0.68rem; text-transform:uppercase; letter-spacing:0.04em; color:#9CA3AF;
        font-weight:700;
    }
    .c-bl { font-size:1.0rem; margin-bottom:2px; }
    .ant-title { font-size:1.6rem; }
    .kpi-value { font-size:1.55rem; }
}
</style>
"""


# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------
def hoy_rd() -> date:
    """Fecha de HOY en hora de Santo Domingo (UTC-4). Streamlit Cloud corre en UTC:
    con date.today() directo, en las últimas horas del día (y sobre todo el último
    día del mes) el servidor ya 'cree' que es el día siguiente aunque en RD no lo sea."""
    return datetime.now(ZONA_RD).date()


def ahora_rd() -> datetime:
    return datetime.now(ZONA_RD)


def _norm(texto) -> str:
    """Normaliza un nombre de columna/pestaña: sin acentos, sin dobles espacios,
    sin distinguir mayúsculas. Es lo que evita que 'Fecha_Actualización' y
    'Fecha_Actualizacion' se traten como columnas distintas."""
    s = " ".join(str(texto).split()).strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.casefold()


def _slug_css(texto) -> str:
    """Convierte un nombre visible ('Aéreos', 'Carga Suelta') en un identificador
    ASCII apto para usarse como clase CSS: 'aereos', 'carga_suelta'."""
    base = _norm(texto)
    limpio = "".join(c if c.isalnum() else "_" for c in base)
    while "__" in limpio:
        limpio = limpio.replace("__", "_")
    return limpio.strip("_") or "x"


def esc(valor) -> str:
    """Escapa cualquier valor que venga del Sheet antes de meterlo en HTML."""
    texto = "" if valor is None else str(valor).strip()
    return html.escape(texto) if texto else "—"


# --- Fechas -----------------------------------------------------------------
EPOCH_SHEETS = date(1899, 12, 30)

# Nombres de mes que el parser reconoce. Se guardan normalizados (sin acentos,
# minúsculas), en español e inglés, completos y abreviados, porque los correos de
# navieras y proveedores llegan en los dos idiomas.
_MESES_EN = ["january", "february", "march", "april", "may", "june",
             "july", "august", "september", "october", "november", "december"]
_MESES_TEXTO = {}
for _n, _nombre_es in MESES_ES.items():
    _MESES_TEXTO[_norm(_nombre_es)] = _n
    _MESES_TEXTO[MESES_ES_CORTO[_n]] = _n
for _i, _nombre_en in enumerate(_MESES_EN, start=1):
    _MESES_TEXTO[_nombre_en] = _i
    _MESES_TEXTO[_nombre_en[:3]] = _i
_MESES_TEXTO.update({"setiembre": 9, "set": 9, "sept": 9, "sep": 9, "ene": 1, "abr": 4, "dic": 12})

_SEPARADORES = re.compile(r"[\s/\\\-\.,;_|]+")
_ORDINALES = re.compile(r"^(\d+)(ro|er|ero|do|to|mo|vo|no|st|nd|rd|th)$")
# Palabras que la gente intercala y que no aportan nada: "6 DE julio DEL 2026".
_RELLENO = {"de", "del", "dia", "el", "la", "los", "las", "ano", "a", "al", "of", "the"}


def _tokenizar_fecha(texto: str) -> list:
    """Parte cualquier forma de escribir una fecha en tres piezas.
    Tolera separadores mezclados, palabras de relleno y ordinales:
    '02-05-2026', '6 de julio 2026', 'julio 28 del 2026', '1ro de mayo/2026'."""
    base = _norm(texto).split(" 00:00:00")[0]
    tokens = []
    for pieza in _SEPARADORES.split(base):
        pieza = pieza.strip("º°ª'\"()[]")
        pieza = _ORDINALES.sub(r"\1", pieza)
        if pieza and pieza not in _RELLENO:
            tokens.append(pieza)
    return tokens


def _normalizar_anio(n: int) -> int:
    """Año de dos dígitos -> siglo razonable. '26' es 2026, no 26 d.C."""
    if n >= 100:
        return n
    return 2000 + n if n < 80 else 1900 + n


def _interpretar_tokens(tokens: list, dia_primero: bool = True):
    """Devuelve (dia, mes, anio, ambigua) o None.
    'ambigua' es True solo cuando día y mes son ambos <= 12 y están escritos en
    número, que es el único caso donde el orden realmente no se puede deducir."""
    if len(tokens) != 3:
        return None

    # Caso 1: uno de los tres es un nombre de mes -> no hay ambigüedad posible.
    for i, t in enumerate(tokens):
        mes = _MESES_TEXTO.get(t) or _MESES_TEXTO.get(t[:3]) if not t.isdigit() else None
        if mes:
            resto = [tokens[j] for j in range(3) if j != i]
            if not all(r.isdigit() for r in resto):
                return None
            a, b = int(resto[0]), int(resto[1])
            if len(resto[0]) == 4 or a > 31:
                anio, dia = a, b
            else:
                dia, anio = a, b
            return dia, mes, _normalizar_anio(anio), False

    if not all(t.isdigit() for t in tokens):
        return None
    a, b, c = (int(t) for t in tokens)

    # Caso 2: año al frente (2026-08-25, 2026/8/25).
    if len(tokens[0]) == 4:
        return c, b, a, False

    # Caso 3: año al final. Si uno de los dos primeros pasa de 12, ese es el día.
    anio = _normalizar_anio(c)
    if a > 12 and b <= 12:
        return a, b, anio, False
    if b > 12 and a <= 12:
        return b, a, anio, False
    if a <= 12 and b <= 12:
        dia, mes = (a, b) if dia_primero else (b, a)
        return dia, mes, anio, True
    return None


def _fecha_de_tokens(tokens: list, dia_primero: bool = True):
    resultado = _interpretar_tokens(tokens, dia_primero)
    if resultado is None:
        return None
    dia, mes, anio, _ = resultado
    try:
        return date(anio, mes, dia)
    except ValueError:
        return None


def parsear_fecha(valor):
    """Convierte a date lo que sea que venga del Sheet, del Excel o tecleado a mano.
    Entiende date/datetime, ISO (2026-08-25), compacto (20260825), dd/mm/aaaa,
    dd-mm-aa, aaaa/mm/dd, mes en texto en cualquier posición y en español o
    inglés ('6 de julio 2026', 'julio 28 del 2026', '28-Jul-26', 'July 28, 2026')
    y seriales numéricos de Sheets/Excel (46181).
    Ante un número puro ambiguo (02-05-2026) asume día/mes, la convención local.
    Devuelve None solo si de verdad no hay forma de interpretarlo."""
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        try:
            return EPOCH_SHEETS + timedelta(days=int(valor))
        except (ValueError, OverflowError):
            return None

    texto = str(valor).strip()
    if not texto:
        return None

    # ISO exacto: el formato en que la app escribe siempre.
    try:
        return datetime.strptime(texto[:10], "%Y-%m-%d").date()
    except ValueError:
        pass

    solo_digitos = texto.replace(",", "").replace(" ", "")
    if solo_digitos.replace(".", "", 1).isdigit():
        entero = solo_digitos.split(".")[0]
        if len(entero) == 8:  # 20260825
            try:
                return datetime.strptime(entero, "%Y%m%d").date()
            except ValueError:
                pass
        try:
            numero = int(float(solo_digitos))
        except (ValueError, OverflowError):
            return None
        # Rango de seriales plausibles de Sheets/Excel (aprox. 1954 a 2119).
        if 20000 <= numero <= 80000:
            return EPOCH_SHEETS + timedelta(days=numero)
        return None

    return _fecha_de_tokens(_tokenizar_fecha(texto), dia_primero=True)


def formato_eta(valor) -> str:
    """Cómo se muestra un ETA en pantalla: '25 ago 2026'. Si no se pudo parsear,
    devuelve el texto crudo para que se vea que ese dato está sucio."""
    f = parsear_fecha(valor)
    if f is None:
        crudo = str(valor).strip()
        return crudo if crudo else "—"
    return f"{f.day:02d} {MESES_ES_CORTO[f.month]} {f.year}"


def analizar_eta(valor) -> dict:
    """Diagnóstico de un ETA para la herramienta de normalización.
    - 'iso': ya está en AAAA-MM-DD, no hay nada que hacer.
    - 'ambigua': número puro tipo 02-05-2026 donde día y mes son ambos <= 12; el
      valor real depende de quién lo escribió, así que se pregunta.
    - 'convertible': se entiende sin ambigüedad pero no está en ISO.
    - 'ilegible': no hay forma de interpretarlo."""
    crudo = "" if valor is None else str(valor).strip()
    if not crudo:
        return {"tipo": "vacia", "crudo": crudo, "dm": None, "md": None}

    try:
        datetime.strptime(crudo, "%Y-%m-%d")
        return {"tipo": "iso", "crudo": crudo, "dm": parsear_fecha(crudo), "md": None}
    except ValueError:
        pass

    tokens = _tokenizar_fecha(crudo)
    lectura = _interpretar_tokens(tokens, dia_primero=True)
    if lectura is not None and lectura[3]:
        dm = _fecha_de_tokens(tokens, dia_primero=True)
        md = _fecha_de_tokens(tokens, dia_primero=False)
        if dm and md and dm != md:
            return {"tipo": "ambigua", "crudo": crudo, "dm": dm, "md": md}
        if dm or md:
            return {"tipo": "convertible", "crudo": crudo, "dm": dm or md, "md": None}

    f = parsear_fecha(crudo)
    if f:
        return {"tipo": "convertible", "crudo": crudo, "dm": f, "md": None}
    return {"tipo": "ilegible", "crudo": crudo, "dm": None, "md": None}


# ---------------------------------------------------------------------------
# CAPA GOOGLE SHEETS
# ---------------------------------------------------------------------------
@st.cache_resource
def get_spreadsheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=scopes
    )
    client = gspread.authorize(creds)
    return client.open_by_key(st.secrets["SHEET_ID"])


@st.cache_resource
def _indice_hojas() -> dict:
    """Mapa nombre-normalizado -> Worksheet, con UNA sola llamada de metadata.
    Antes, cada get_worksheet() disparaba una llamada a la API; con 6 pestañas y
    varios reruns por clic, eso solo era la cuota que se iba en aire."""
    return {_norm(h.title): h for h in get_spreadsheet().worksheets()}


def get_worksheet(nombre: str):
    return _indice_hojas().get(_norm(nombre))


def _refrescar_estructura():
    _indice_hojas.clear()
    _headers.clear()


@st.cache_data(ttl=60, show_spinner=False)
def _headers(titulo_hoja: str) -> list:
    ws = get_worksheet(titulo_hoja)
    if ws is None:
        return []
    return ws.row_values(1)


def marca_ahora() -> str:
    """Sello que se estampa en Fecha_Actualizacion cada vez que alguien carga o
    modifica información. Lleva hora, no solo fecha, porque es lo que el tablero
    muestra como 'información actualizada'."""
    return ahora_rd().strftime("%Y-%m-%d %H:%M")


def parsear_marca(valor):
    """Lee un sello de Fecha_Actualizacion como datetime. Acepta los registros
    viejos que solo tienen fecha (se asumen a medianoche)."""
    texto = "" if valor is None else str(valor).strip()
    if not texto:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(texto, fmt)
        except ValueError:
            continue
    f = parsear_fecha(texto)
    return datetime(f.year, f.month, f.day) if f else None


def _fila_desde_dict(headers: list, datos: dict) -> list:
    """Arma la fila respetando el orden REAL de columnas de la pestaña y
    tolerando diferencias de acento/mayúsculas en los encabezados."""
    normalizado = {_norm(k): v for k, v in datos.items()}
    return [normalizado.get(_norm(h), "") for h in headers]


def _asegurar_columnas(ws, nombres: list) -> list:
    """Agrega de una sola vez las columnas que falten (una llamada, no una por
    columna como antes). Devuelve la lista final de encabezados."""
    headers = _headers(ws.title)
    existentes = {_norm(h) for h in headers}
    faltan = [n for n in nombres if _norm(n) not in existentes]
    if not faltan:
        return headers
    inicio = len(headers) + 1
    rango = f"{rowcol_to_a1(1, inicio)}:{rowcol_to_a1(1, inicio + len(faltan) - 1)}"
    ws.update(range_name=rango, values=[faltan], value_input_option="RAW")
    _headers.clear()
    return headers + faltan


def _buscar_fila_por_bl(ws, bl: str):
    """Número de fila (1-indexado) del BL dentro de esa pestaña, o None."""
    if ws is None:
        return None
    headers = _headers(ws.title)
    idx = next((i for i, h in enumerate(headers) if _norm(h) == _norm(COL_BL)), None)
    if idx is None:
        return None
    try:
        celda = ws.find(str(bl).strip(), in_column=idx + 1)
    except Exception:
        # gspread 5 lanza CellNotFound; gspread 6 devuelve None. Cubrimos ambos.
        return None
    return celda.row if celda else None


def _df_desde_valores(valores: list, columnas_canonicas: list) -> pd.DataFrame:
    """Convierte la matriz cruda de una pestaña en DataFrame.
    Las columnas conocidas se renombran al nombre canónico (resolviendo acentos y
    mayúsculas); las demás se conservan tal cual venían, para que agregar una
    columna nueva en Google Sheets (Orden_Compra, Cliente, Valor_USD...) baste
    para que la app la reconozca sin cambiar código."""
    if not valores or not valores[0]:
        return pd.DataFrame(columns=columnas_canonicas)
    headers_reales = [str(h) for h in valores[0]]
    ancho = len(headers_reales)
    # Las filas pueden venir más cortas (celdas vacías al final) o más largas
    # (alguien escribió a la derecha del último encabezado): se ajustan al ancho.
    filas = [(list(f) + [""] * ancho)[:ancho] for f in valores[1:]]

    # Encabezados vacíos o repetidos: se les pone un nombre único para que pandas
    # no reviente y para que se vean en el detalle como lo que son, columnas sueltas.
    vistos, limpios = {}, []
    for i, h in enumerate(headers_reales):
        nombre = h.strip() or f"Columna {i + 1}"
        if _norm(nombre) in vistos:
            vistos[_norm(nombre)] += 1
            nombre = f"{nombre} ({vistos[_norm(nombre)]})"
        else:
            vistos[_norm(nombre)] = 1
        limpios.append(nombre)

    df = pd.DataFrame(filas, columns=limpios)

    mapa = {}
    for canon in columnas_canonicas:
        for real in df.columns:
            if _norm(real) == _norm(canon) and real not in mapa:
                mapa[real] = canon
                break
    df = df.rename(columns=mapa)
    df = df.loc[:, ~df.columns.duplicated()]
    for c in columnas_canonicas:
        if c not in df.columns:
            df[c] = ""
    return df


NO_ESPECIFICADO = "Sin especificar"
# Equivalencias de país. Solo las que son inequívocamente el mismo lugar escrito
# de otra forma o en otro idioma; nada de agrupaciones regionales, que ya serían
# criterio de negocio. El nombre a la derecha es el que se muestra.
_ALIAS_PAISES_CRUDO = {
    "Estados Unidos": ["usa", "us", "u.s.a", "u.s.a.", "u.s.", "eeuu", "ee.uu", "ee.uu.", "eu",
                       "united states", "united states of america", "estados unidos de america",
                       "usa.", "america"],
    "China": ["china", "prc", "p.r. china", "republica popular china", "cn", "china."],
    "Corea del Sur": ["corea", "korea", "south korea", "corea del sur", "republica de corea", "kr"],
    "India": ["india", "in"],
    "Japón": ["japon", "japan", "jp"],
    "Alemania": ["alemania", "germany", "de"],
    "Brasil": ["brasil", "brazil", "br"],
    "España": ["espana", "spain", "es"],
    "Italia": ["italia", "italy", "it"],
    "Turquía": ["turquia", "turkey", "turkiye", "tr"],
    "México": ["mexico", "mx"],
    "Colombia": ["colombia", "co"],
    "Países Bajos": ["paises bajos", "holanda", "netherlands", "holland", "nl"],
    "Reino Unido": ["reino unido", "united kingdom", "uk", "inglaterra", "england", "gb"],
    "Canadá": ["canada", "ca"],
    "Taiwán": ["taiwan", "taiwan roc", "tw"],
    "Vietnam": ["vietnam", "viet nam", "vn"],
    "Tailandia": ["tailandia", "thailand", "th"],
    "Panamá": ["panama", "pa"],
    "Francia": ["francia", "france", "fr"],
}
ALIAS_PAISES = {}
for _canon, _formas in _ALIAS_PAISES_CRUDO.items():
    ALIAS_PAISES[_norm(_canon)] = _canon
    for _f in _formas:
        ALIAS_PAISES[_norm(_f)] = _canon
_VACIOS_PAIS = {"", "n/a", "na", "n.a.", "-", "--", "s/d", "nd", "no aplica", "pendiente", "?"}


def unificar_paises(serie: pd.Series) -> pd.Series:
    """'China', 'CHINA' y 'china ' son el mismo país y no deben salir como tres
    barras distintas en el gráfico ni como tres opciones del filtro. Se agrupan
    por nombre normalizado y se muestra la grafía más usada del propio Sheet, sin
    inventar equivalencias: 'USA' y 'Estados Unidos' siguen separados porque
    unificarlos es una decisión de negocio, no de formato."""
    valores = [str(v or "").strip() for v in serie]
    grupos = {}
    for v in valores:
        clave = _norm(v)
        if clave in _VACIOS_PAIS or clave in ALIAS_PAISES:
            continue
        grupos.setdefault(clave, {})
        grupos[clave][v] = grupos[clave].get(v, 0) + 1
    # Para lo que no está en la tabla de equivalencias, se usa la grafía más
    # frecuente del propio Sheet en lugar de imponer un formato.
    canonico = {c: max(op.items(), key=lambda kv: (kv[1], -len(kv[0])))[0] for c, op in grupos.items()}

    def resolver(v):
        clave = _norm(v)
        if clave in _VACIOS_PAIS:
            return NO_ESPECIFICADO
        return ALIAS_PAISES.get(clave) or canonico.get(clave, NO_ESPECIFICADO)

    return pd.Series([resolver(v) for v in valores], index=serie.index)


def columnas_extra(df: pd.DataFrame) -> list:
    """Columnas que el usuario agregó en el Sheet y que la app no gestiona."""
    conocidas = set(ALL_COLUMNS) | COLUMNAS_INTERNAS | {"Fecha_Recibido", "Categoria_Origen", "Registrado_Por"}
    return [c for c in df.columns if c not in conocidas]


def columna_de_valor(df: pd.DataFrame):
    """Detecta si el Sheet trae una columna de valor monetario, sin obligar a que
    se llame de una forma concreta."""
    for c in df.columns:
        if _norm(c) in NOMBRES_VALOR:
            return c
    return None


def es_numero(v) -> bool:
    """NaN es truthy en Python, así que 'if valor' NO sirve para descartar celdas
    vacías de una columna numérica de pandas."""
    return v is not None and pd.notna(v)


def a_numero(valor):
    """'US$ 145,300.50' -> 145300.5. Devuelve None si no hay número."""
    texto = str(valor or "").strip()
    if not texto:
        return None
    limpio = re.sub(r"[^0-9,.\-]", "", texto)
    if not limpio:
        return None
    # Si hay coma y punto, el último separador que aparece es el decimal.
    if "," in limpio and "." in limpio:
        if limpio.rfind(",") > limpio.rfind("."):
            limpio = limpio.replace(".", "").replace(",", ".")
        else:
            limpio = limpio.replace(",", "")
    elif "," in limpio:
        entero, _, decimales = limpio.rpartition(",")
        limpio = f"{entero.replace(',', '')}.{decimales}" if len(decimales) in (1, 2) else limpio.replace(",", "")
    try:
        return float(limpio)
    except ValueError:
        return None


def formato_dinero(monto: float) -> str:
    if monto >= 1_000_000:
        return f"US$ {monto / 1_000_000:,.2f} M"
    if monto >= 10_000:
        return f"US$ {monto / 1_000:,.0f} K"
    return f"US$ {monto:,.0f}"


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def cargar_todo() -> dict:
    """UNA sola llamada a la API trae las 5 pestañas de categoría + el histórico.
    Devuelve {'activos': df, 'historico': df, 'hora': datetime, 'error': str|None}."""
    try:
        ss = get_spreadsheet()
        indice = _indice_hojas()
    except Exception as e:
        return {
            "activos": pd.DataFrame(columns=ALL_COLUMNS + ["Categoria"]),
            "historico": pd.DataFrame(columns=COLUMNAS_RECIBIDO),
            "hora": ahora_rd(),
            "ultima_carga": None, "ultima_persona": "", "avisos": [],
            "error": f"No se pudo conectar con el Google Sheet: {e}",
        }

    objetivos = []  # (etiqueta, titulo_real)
    for cat in CATEGORIAS:
        ws = indice.get(_norm(cat))
        if ws is not None:
            objetivos.append((cat, ws.title))
    ws_rec = indice.get(_norm(RECIBIDO_SHEET))
    if ws_rec is not None:
        objetivos.append((RECIBIDO_SHEET, ws_rec.title))

    if not objetivos:
        return {
            "activos": pd.DataFrame(columns=ALL_COLUMNS + ["Categoria"]),
            "historico": pd.DataFrame(columns=COLUMNAS_RECIBIDO),
            "hora": ahora_rd(),
            "ultima_carga": None, "ultima_persona": "", "avisos": [],
            "error": "El Google Sheet no tiene ninguna de las pestañas esperadas.",
        }

    rangos = [f"'{titulo}'!A1:AZ{MAX_FILAS_LECTURA}" for _, titulo in objetivos]
    try:
        respuesta = ss.values_batch_get(rangos)
    except gspread.exceptions.APIError as e:
        return {
            "activos": pd.DataFrame(columns=ALL_COLUMNS + ["Categoria"]),
            "historico": pd.DataFrame(columns=COLUMNAS_RECIBIDO),
            "hora": ahora_rd(),
            "ultima_carga": None, "ultima_persona": "", "avisos": [],
            "error": f"Google Sheets no respondió (posible límite de cuota): {e}",
        }

    bloques = respuesta.get("valueRanges", [])
    frames, historico, avisos = [], pd.DataFrame(columns=COLUMNAS_RECIBIDO), []

    for (etiqueta, _titulo), bloque in zip(objetivos, bloques):
        valores = bloque.get("values", [])
        # Si una pestaña llega justo al tope del rango pedido, es muy probable que
        # haya filas más abajo que la app no está viendo. Truncar en silencio es
        # peor que cualquier error visible: se toman decisiones con datos a medias.
        if len(valores) >= MAX_FILAS_LECTURA:
            avisos.append(
                f"La pestaña '{etiqueta}' llegó al tope de {MAX_FILAS_LECTURA:,} filas que la app lee. "
                "Puede haber embarques más abajo que no se están mostrando; hay que archivar lo viejo "
                "o subir el límite en el código."
            )
        if etiqueta == RECIBIDO_SHEET:
            historico = _df_desde_valores(valores, COLUMNAS_RECIBIDO)
            continue
        df_cat = _df_desde_valores(valores, ALL_COLUMNS)
        df_cat["Categoria"] = etiqueta
        # Fila real = índice + 2 (encabezado + base 1). Se guarda antes de filtrar
        # para poder señalar la fila exacta en el Sheet cuando algo esté sucio.
        df_cat["FilaSheet"] = range(2, len(df_cat) + 2)
        frames.append(df_cat)

    if frames:
        activos = pd.concat(frames, ignore_index=True)
        for c in (COL_BL, COL_DESC):
            activos[c] = activos[c].astype(str).str.strip()
        # Se descartan solo filas realmente vacías (ni BL ni Descripción): notas
        # sueltas en una celda o filas en blanco dentro del rango usado.
        activos = activos[(activos[COL_BL] != "") | (activos[COL_DESC] != "")]
        activos = activos.reset_index(drop=True)
        activos[COL_PAIS] = unificar_paises(activos[COL_PAIS])
    else:
        activos = pd.DataFrame(columns=ALL_COLUMNS + ["Categoria", "FilaSheet"])

    if not historico.empty:
        historico = historico[historico[COL_BL].astype(str).str.strip() != ""].reset_index(drop=True)

    # "Última carga" = la marca más reciente escrita por alguien al agregar,
    # editar, cargar en masa o archivar un embarque. Es distinto de "última
    # lectura", que es cuándo la app fue a buscar los datos: al presidente le
    # importa cuándo se movió la información, no cuándo él abrió la página.
    marcas = []
    for cuadro in (activos, historico):
        if not cuadro.empty and COL_ACTUALIZACION in cuadro.columns:
            marcas += [m for m in (parsear_marca(v) for v in cuadro[COL_ACTUALIZACION]) if m]
    ultima_carga = max(marcas) if marcas else None

    # Quién hizo esa última carga, para que el sello no sea solo una hora.
    ultima_persona = ""
    if ultima_carga is not None:
        for cuadro in (activos, historico):
            if cuadro.empty or COL_ACTUALIZACION not in cuadro.columns:
                continue
            columna_autor = COL_ACTUALIZADO_POR if COL_ACTUALIZADO_POR in cuadro.columns else None
            if columna_autor is None and "Registrado_Por" in cuadro.columns:
                columna_autor = "Registrado_Por"
            if columna_autor is None:
                continue
            for marca, autor in zip(cuadro[COL_ACTUALIZACION], cuadro[columna_autor]):
                if parsear_marca(marca) == ultima_carga and str(autor).strip():
                    ultima_persona = str(autor).strip()
                    break
            if ultima_persona:
                break

    return {"activos": activos, "historico": historico, "hora": ahora_rd(),
            "ultima_carga": ultima_carga, "ultima_persona": ultima_persona,
            "avisos": avisos, "error": None}


def invalidar_caches():
    cargar_todo.clear()


# --- Escrituras -------------------------------------------------------------
def _con_manejo_apierror(func):
    """Convierte un APIError de gspread (cuota, permisos) en (False, mensaje)
    legible en vez de tumbar la página con un traceback."""
    def envoltura(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            return False, f"Google Sheets rechazó la operación ({e}). Espera unos segundos e intenta de nuevo."
        except Exception as e:  # noqa: BLE001 - último cortafuegos antes de la UI
            return False, f"Error inesperado: {e}"
    envoltura.__name__ = func.__name__
    return envoltura


def rerun_fragmento():
    """st.rerun(scope="fragment") solo es válido cuando el rerun lo disparó un
    widget que vive dentro del fragmento. Si se llama en un rerun de página
    completa, Streamlit lanza StreamlitAPIException. Esta envoltura cae al rerun
    normal en ese caso. (RerunException hereda de BaseException, así que el
    except Exception no se traga el rerun bueno.)"""
    try:
        st.rerun(scope="fragment")
    except Exception:
        st.rerun()


def usuario_actual() -> str:
    return st.session_state.get("usuario", "desconocido")


def registrar_log(accion: str, bl: str = "", categoria: str = "", detalle: str = ""):
    """Bitácora de quién hizo qué. Nunca debe romper la operación principal:
    si el log falla, la acción ya se ejecutó y eso es lo que importa."""
    try:
        ws = get_worksheet(LOG_SHEET)
        if ws is None:
            ss = get_spreadsheet()
            ws = ss.add_worksheet(title=LOG_SHEET, rows=2000, cols=len(COLUMNAS_LOG))
            ws.update(range_name="A1", values=[COLUMNAS_LOG], value_input_option="RAW")
            _refrescar_estructura()
            ws = get_worksheet(LOG_SHEET)
        ws.append_row(
            [
                ahora_rd().strftime("%Y-%m-%d %H:%M:%S"),
                usuario_actual(),
                accion,
                str(bl),
                categoria,
                detalle,
            ],
            value_input_option="RAW",
        )
    except Exception:
        pass


@_con_manejo_apierror
def append_row(datos: dict, categoria: str):
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}' en el Google Sheet."
    datos = dict(datos)
    datos[COL_ACTUALIZACION] = marca_ahora()
    datos[COL_ACTUALIZADO_POR] = usuario_actual()
    columnas_a_asegurar = [COL_ACTUALIZACION, COL_ACTUALIZADO_POR]
    if str(datos.get(COL_FECHA_SALIDA, "")).strip():
        columnas_a_asegurar.append(COL_FECHA_SALIDA)
    headers = _asegurar_columnas(ws, columnas_a_asegurar)
    ws.append_row(_fila_desde_dict(headers, datos), value_input_option="RAW")
    return True, ""


@_con_manejo_apierror
def append_rows_bulk(df: pd.DataFrame, categoria: str):
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}' en el Google Sheet."
    trae_salida = COL_FECHA_SALIDA in df.columns and df[COL_FECHA_SALIDA].astype(str).str.strip().ne("").any()
    columnas_a_asegurar = [COL_ACTUALIZACION, COL_ACTUALIZADO_POR]
    if trae_salida:
        columnas_a_asegurar.append(COL_FECHA_SALIDA)
    headers = _asegurar_columnas(ws, columnas_a_asegurar)
    sello, autor = marca_ahora(), usuario_actual()
    columnas_fila = REQUIRED_COLUMNS + ([COL_FECHA_SALIDA] if trae_salida else [])
    filas = []
    for _, r in df.iterrows():
        datos = {c: r.get(c, "") for c in columnas_fila}
        datos[COL_ACTUALIZACION] = sello
        datos[COL_ACTUALIZADO_POR] = autor
        filas.append(_fila_desde_dict(headers, datos))
    ws.append_rows(filas, value_input_option="RAW")
    return True, ""


@_con_manejo_apierror
def actualizar_embarque(bl_original: str, categoria: str, datos: dict):
    """Edición de un embarque ya cargado. Lee la fila actual para no pisar
    columnas que la app no gestiona, y reescribe la fila completa de una vez."""
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}'."
    fila = _buscar_fila_por_bl(ws, bl_original)
    if fila is None:
        return False, f"El BL '{bl_original}' ya no está en '{categoria}' — puede que alguien lo movió."

    columnas_a_asegurar = [COL_ACTUALIZACION, COL_ACTUALIZADO_POR]
    if COL_FECHA_SALIDA in datos:
        columnas_a_asegurar.append(COL_FECHA_SALIDA)
    headers = _asegurar_columnas(ws, columnas_a_asegurar)
    actuales = ws.row_values(fila)
    actuales += [""] * (len(headers) - len(actuales))
    combinado = {h: actuales[i] for i, h in enumerate(headers)}
    combinado.update(datos)
    combinado[COL_ACTUALIZACION] = marca_ahora()
    combinado[COL_ACTUALIZADO_POR] = usuario_actual()
    # Si el ETA se movió a futuro, el embarque vuelve a estar en tránsito y la
    # marca de "verificado que no llegó" queda obsoleta.
    eta_nuevo = parsear_fecha(datos.get(COL_ETA, combinado.get(COL_ETA, "")))
    if eta_nuevo and eta_nuevo > hoy_rd():
        combinado[COL_ESTATUS_LLEGADA] = ""

    rango = f"{rowcol_to_a1(fila, 1)}:{rowcol_to_a1(fila, len(headers))}"
    ws.update(range_name=rango, values=[_fila_desde_dict(headers, combinado)], value_input_option="RAW")
    return True, ""


@_con_manejo_apierror
def marcar_estatus_llegada(bl: str, categoria: str, valor: str):
    """Escribe (o limpia) la respuesta a '¿ya llegó?'. valor="" borra la marca,
    valor="Retrasado" deja constancia de que se verificó que NO llegó."""
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}'."
    fila = _buscar_fila_por_bl(ws, bl)
    if fila is None:
        return False, f"No se encontró el BL '{bl}' en '{categoria}'."

    headers = _asegurar_columnas(ws, [COL_ESTATUS_LLEGADA, COL_ACTUALIZACION, COL_ACTUALIZADO_POR])
    indices = {_norm(h): i + 1 for i, h in enumerate(headers)}
    peticiones = [
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ESTATUS_LLEGADA)]), "values": [[valor]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZACION)]), "values": [[marca_ahora()]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZADO_POR)]), "values": [[usuario_actual()]]},
    ]
    ws.batch_update(peticiones, value_input_option="RAW")
    return True, ""


@_con_manejo_apierror
def avanzar_estado_puerto(bl: str, categoria: str, nueva_etapa: str):
    """Mueve el sub-estado del flujo de puerto (solo categorías marítimas).
    Cada etapa en COLUMNA_FECHA_ETAPA estampa su columna de fecha la primera
    vez que se marca, sin pisarla si ya estaba puesta — así una corrección de
    etapa no borra la fecha real en que ocurrió cada hito."""
    if nueva_etapa not in ETAPAS_PUERTO:
        return False, f"Etapa '{nueva_etapa}' no reconocida."
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}'."
    fila = _buscar_fila_por_bl(ws, bl)
    if fila is None:
        return False, f"No se encontró el BL '{bl}' en '{categoria}'."

    columnas_fecha = list(COLUMNA_FECHA_ETAPA.values())
    headers = _asegurar_columnas(
        ws, [COL_ESTADO_PUERTO, *columnas_fecha, COL_ACTUALIZACION, COL_ACTUALIZADO_POR]
    )
    indices = {_norm(h): i + 1 for i, h in enumerate(headers)}
    actuales = ws.row_values(fila)
    actuales += [""] * (len(headers) - len(actuales))
    combinado = {h: actuales[i] for i, h in enumerate(headers)}

    peticiones = [
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ESTADO_PUERTO)]), "values": [[nueva_etapa]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZACION)]), "values": [[marca_ahora()]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZADO_POR)]), "values": [[usuario_actual()]]},
    ]
    columna_fecha = COLUMNA_FECHA_ETAPA.get(nueva_etapa)
    if columna_fecha and not str(combinado.get(columna_fecha, "")).strip():
        peticiones.append({"range": rowcol_to_a1(fila, indices[_norm(columna_fecha)]),
                           "values": [[hoy_rd().isoformat()]]})

    ws.batch_update(peticiones, value_input_option="RAW")
    return True, ""


@_con_manejo_apierror
def eliminar_embarque(bl: str, categoria: str):
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}'."
    fila = _buscar_fila_por_bl(ws, bl)
    if fila is None:
        return False, f"No se encontró el BL '{bl}' en '{categoria}'."
    ws.delete_rows(fila)
    return True, ""


@_con_manejo_apierror
def marcar_como_recibido(bl: str, categoria: str):
    """Archiva el embarque en 'Recibido (Mes)' y lo saca del tablero activo.
    La fecha de recibido es el ETA del embarque, no el día del clic: el mes que
    cuenta es aquel en que la mercancía llegó, no aquel en que alguien se acordó
    de confirmarlo en la app."""
    ws_origen = get_worksheet(categoria)
    if ws_origen is None:
        return False, f"No existe la pestaña '{categoria}'."

    fila = _buscar_fila_por_bl(ws_origen, bl)
    if fila is None:
        return False, f"No se encontró el BL '{bl}' en '{categoria}' — puede que ya se haya movido o editado."

    headers_origen = _headers(ws_origen.title)
    valores = ws_origen.row_values(fila)
    valores += [""] * (len(headers_origen) - len(valores))
    datos = {h: valores[i] for i, h in enumerate(headers_origen)}
    datos_norm = {_norm(k): v for k, v in datos.items()}

    eta_crudo = str(datos_norm.get(_norm(COL_ETA), "")).strip()
    fecha_llegada = parsear_fecha(eta_crudo)
    if fecha_llegada is None:
        return False, (
            f"El BL '{bl}' no tiene un ETA interpretable ('{eta_crudo or 'vacío'}'), así que no se "
            "puede archivar sin saber a qué mes pertenece. Corrígelo en Herramientas → Normalizar fechas."
        )

    ws_destino = get_worksheet(RECIBIDO_SHEET)
    if ws_destino is None:
        nombres = ", ".join(f"'{h.title}'" for h in get_spreadsheet().worksheets())
        return False, (
            f"No existe la pestaña '{RECIBIDO_SHEET}'. Las pestañas visibles son: {nombres}."
        )
    headers_destino = _asegurar_columnas(ws_destino, COLUMNAS_RECIBIDO)

    registro = {
        COL_BL: datos_norm.get(_norm(COL_BL), bl),
        COL_DESC: datos_norm.get(_norm(COL_DESC), ""),
        COL_MODELO: datos_norm.get(_norm(COL_MODELO), ""),
        COL_CANT: datos_norm.get(_norm(COL_CANT), ""),
        COL_PAIS: datos_norm.get(_norm(COL_PAIS), ""),
        COL_ETA: fecha_llegada.isoformat(),
        "Fecha_Recibido": fecha_llegada.isoformat(),
        "Categoria_Origen": categoria,
        "Registrado_Por": usuario_actual(),
        COL_ACTUALIZACION: marca_ahora(),
        COL_ACTUALIZADO_POR: usuario_actual(),
    }
    ws_destino.append_row(_fila_desde_dict(headers_destino, registro), value_input_option="RAW")
    ws_origen.delete_rows(fila)
    return True, ""


@_con_manejo_apierror
def quitar_de_recibido(bl: str, categoria_manual: str | None = None):
    """Reversa de 'Marcar como Recibido': devuelve el embarque a su categoría."""
    ws_recibido = get_worksheet(RECIBIDO_SHEET)
    if ws_recibido is None:
        return False, f"No existe la pestaña '{RECIBIDO_SHEET}'."

    fila = _buscar_fila_por_bl(ws_recibido, bl)
    if fila is None:
        return False, f"No se encontró el BL '{bl}' en '{RECIBIDO_SHEET}'."

    headers = _headers(ws_recibido.title)
    valores = ws_recibido.row_values(fila)
    valores += [""] * (len(headers) - len(valores))
    datos_norm = {_norm(h): valores[i] for i, h in enumerate(headers)}

    categoria = (categoria_manual or datos_norm.get(_norm("Categoria_Origen"), "")).strip()
    if categoria not in CATEGORIAS:
        return False, f"'{categoria or 'vacía'}' no es una categoría válida. Elige una del menú antes de confirmar."

    ok, mensaje = append_row(
        {
            COL_BL: datos_norm.get(_norm(COL_BL), bl),
            COL_DESC: datos_norm.get(_norm(COL_DESC), ""),
            COL_MODELO: datos_norm.get(_norm(COL_MODELO), ""),
            COL_CANT: datos_norm.get(_norm(COL_CANT), ""),
            COL_PAIS: datos_norm.get(_norm(COL_PAIS), ""),
            COL_ETA: datos_norm.get(_norm(COL_ETA), ""),
        },
        categoria,
    )
    if not ok:
        return False, mensaje or f"No se pudo escribir de vuelta en '{categoria}'."

    ws_recibido.delete_rows(fila)
    return True, ""


@_con_manejo_apierror
def normalizar_etas(cambios: list):
    """cambios = [(categoria, fila_sheet, iso)]. Reescribe los ETA en formato
    ISO agrupando por pestaña: una llamada por pestaña, no una por celda."""
    if not cambios:
        return True, "Sin cambios."
    por_categoria = {}
    for categoria, fila, iso in cambios:
        por_categoria.setdefault(categoria, []).append((fila, iso))

    total = 0
    for categoria, lista in por_categoria.items():
        ws = get_worksheet(categoria)
        if ws is None:
            continue
        headers = _headers(ws.title)
        idx = next((i for i, h in enumerate(headers) if _norm(h) == _norm(COL_ETA)), None)
        if idx is None:
            continue
        cuerpo = [
            {"range": rowcol_to_a1(fila, idx + 1), "values": [[iso]]}
            for fila, iso in lista
        ]
        ws.batch_update(cuerpo, value_input_option="RAW")
        total += len(cuerpo)
    return True, f"{total} fecha(s) normalizada(s) a formato AAAA-MM-DD."


# ---------------------------------------------------------------------------
# LÓGICA DE ESTADO
# ---------------------------------------------------------------------------
def estado_embarque(eta_valor, hoy: date = None):
    """Devuelve (estado, dias_relativos). dias_relativos es el atraso en días si
    está En Puerto, los días que faltan si está Próximo a llegar, y None si no aplica.
    'hoy' se recibe por parámetro para no consultar el reloj una vez por fila."""
    eta = parsear_fecha(eta_valor)
    if eta is None:
        return EST_SIN_FECHA, None
    dias = (eta - (hoy or hoy_rd())).days
    if dias < 0:
        return EST_PUERTO, abs(dias)
    if dias <= UMBRAL_PROXIMO:
        return EST_PROXIMO, dias
    return EST_TRANSITO, None


def texto_estado(estado: str, dias) -> str:
    if estado == EST_RETRASADO and dias is not None:
        d = int(dias)
        return f"Retrasado {d} día{'s' if d != 1 else ''}"
    if estado == EST_PUERTO and dias is not None:
        d = int(dias)
        return f"En Puerto hace {d} día{'s' if d != 1 else ''}"
    if estado == EST_PROXIMO and dias is not None:
        d = int(dias)
        if d == 0:
            return "Llega hoy"
        if d == 1:
            return "Llega mañana"
        return f"Llega en {d} días"
    return estado


def enriquecer(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega estado, fecha parseada, valor numérico y clave de orden operativo.
    Se llama UNA vez por refresco sobre la tabla completa; las vistas por
    categoría trabajan sobre rebanadas de este resultado en vez de recalcular."""
    df = df.copy()
    if df.empty:
        for c in ("EstadoTexto", "DiasRel", "ETAFecha", "Prioridad", "OrdenSec", "ValorNum",
                  "DiasTransito", "DiasSolicitudPago", "DiasPagoDespacho"):
            df[c] = []
        return df

    hoy = hoy_rd()
    etas = [parsear_fecha(v) for v in df[COL_ETA]]
    calculado = [estado_embarque(f, hoy) for f in etas]
    df["ETAFecha"] = etas
    estados = [c[0] for c in calculado]
    if COL_ESTATUS_LLEGADA in df.columns:
        estados = [
            EST_RETRASADO if (e == EST_PUERTO and _norm(f) == _norm(VALOR_RETRASADO)) else e
            for e, f in zip(estados, df[COL_ESTATUS_LLEGADA])
        ]
    df["EstadoTexto"] = estados
    df["DiasRel"] = [c[1] for c in calculado]
    df["Prioridad"] = df["EstadoTexto"].map(PRIORIDAD_ESTADO).fillna(9).astype(int)

    # Contador 1: salida -> llegada a puerto. Si ya se confirmó la llegada, se
    # congela en (llegada - salida) en vez de seguir creciendo con el reloj de
    # hoy; así el número queda como "cuánto tardó" y no como "cuánto lleva sin
    # llegar" una vez que ya llegó.
    salidas = [parsear_fecha(v) for v in df[COL_FECHA_SALIDA]] if COL_FECHA_SALIDA in df.columns \
        else [None] * len(df)
    llegadas_puerto = [parsear_fecha(v) for v in df[COL_FECHA_LLEGADA_PUERTO]] \
        if COL_FECHA_LLEGADA_PUERTO in df.columns else [None] * len(df)
    solicitudes_pago = [parsear_fecha(v) for v in df[COL_FECHA_SOLICITUD_PAGO]] \
        if COL_FECHA_SOLICITUD_PAGO in df.columns else [None] * len(df)
    pagos = [parsear_fecha(v) for v in df[COL_FECHA_PAGO]] if COL_FECHA_PAGO in df.columns \
        else [None] * len(df)

    df["DiasTransito"] = [
        (ll - sal).days if (sal and ll) else ((hoy - sal).days if sal else None)
        for sal, ll in zip(salidas, llegadas_puerto)
    ]
    # Contador 2: solicitud de pago -> pago realizado. Se congela al pagar;
    # mientras no haya pago, sigue el reloj de hoy (cuánto lleva sin pagarse).
    df["DiasSolicitudPago"] = [
        (pg - sol).days if (sol and pg) else ((hoy - sol).days if sol else None)
        for sol, pg in zip(solicitudes_pago, pagos)
    ]
    # Contador 3: pago realizado -> hoy, mientras espera despacho. No se
    # congela con una fecha de despacho porque "despachado" ya no es una
    # etapa: el embarque sale del tablero activo al marcarlo como recibido, y
    # ahí deja de verse (no hace falta una fecha aparte para detener esto).
    df["DiasPagoDespacho"] = [(hoy - pg).days if pg else None for pg in pagos]
    # Dentro de "En Puerto", primero el más atrasado; en el resto, el ETA más cercano.
    df["OrdenSec"] = [
        -(dias or 0) if estado == EST_PUERTO else (fecha.toordinal() if fecha else 10**9)
        for estado, dias, fecha in zip(df["EstadoTexto"], df["DiasRel"], df["ETAFecha"])
    ]
    col_valor = columna_de_valor(df)
    df["ValorNum"] = [a_numero(v) for v in df[col_valor]] if col_valor else [None] * len(df)
    return df.sort_values(["Prioridad", "OrdenSec"], kind="stable").reset_index(drop=True)


def ordenar_vista(df: pd.DataFrame, criterio: str) -> pd.DataFrame:
    """Ordena la lista según lo que el usuario elija. El orden por defecto es
    operativo (lo atrasado primero), no alfabético."""
    if df.empty:
        return df
    if criterio == "Urgencia":
        return df.sort_values(["Prioridad", "OrdenSec"], kind="stable")
    if criterio == "ETA más próximo":
        return df.sort_values("OrdenSec", key=lambda s: s.where(s > 0, 10**9), kind="stable")
    if criterio == "ETA más lejano":
        return df.sort_values("OrdenSec", ascending=False, kind="stable")
    if criterio == "BL":
        return df.sort_values(COL_BL, kind="stable")
    if criterio == "País":
        return df.sort_values([COL_PAIS, "Prioridad"], kind="stable")
    if criterio == "Descripción":
        return df.sort_values(COL_DESC, kind="stable")
    if criterio == "Valor" and "ValorNum" in df.columns:
        return df.sort_values("ValorNum", ascending=False, na_position="last", kind="stable")
    return df


# ---------------------------------------------------------------------------
# ACCESO
# ---------------------------------------------------------------------------
@st.cache_resource
def _sesiones_activas() -> dict:
    """Sesiones vivas, compartidas entre todas las conexiones del servidor.
    Streamlit pierde session_state en cada recarga del navegador (cada F5 abre
    una sesión nueva), así que para no pedir el PIN otra vez hay que guardar una
    referencia fuera de la sesión: un token que viaja en la URL y cuyo contenido
    real vive aquí, del lado del servidor. En la URL solo va el identificador,
    nunca el rol ni el PIN.
    Se vacía cuando la app se reinicia o se redespliega: eso obliga a teclear el
    PIN otra vez, que es el comportamiento correcto."""
    return {}


def _purgar_sesiones(ahora: float):
    for token in [t for t, d in _sesiones_activas().items() if d["expira"] < ahora]:
        _sesiones_activas().pop(token, None)


def _abrir_sesion(rol: str, nombre: str):
    """Crea el token de sesión y lo deja en la URL para sobrevivir a las recargas."""
    ahora = time.time()
    _purgar_sesiones(ahora)
    token = token_urlsafe(18)
    _sesiones_activas()[token] = {
        "rol": rol,
        "nombre": nombre,
        "expira": ahora + VIDA_SESION_MINUTOS * 60,
    }
    st.session_state.rol = rol
    st.session_state.usuario = nombre
    st.session_state.token = token
    st.query_params["s"] = token


def restaurar_sesion():
    """Al abrir la página, intenta reanudar la sesión desde el token de la URL.
    No hay límite de recargas: mientras no pasen VIDA_SESION_MINUTOS desde la
    última vez que se abrió la página, se entra directo, y cada recarga vuelve a
    correr el reloj. Pasado ese tiempo, se pide el PIN otra vez."""
    if "rol" in st.session_state:
        return
    token = st.query_params.get("s")
    if not token:
        return

    ahora = time.time()
    _purgar_sesiones(ahora)
    datos = _sesiones_activas().get(token)
    if not datos or datos["expira"] < ahora:
        _sesiones_activas().pop(token, None)
        st.query_params.clear()
        return

    datos["expira"] = ahora + VIDA_SESION_MINUTOS * 60  # ventana deslizante
    st.session_state.rol = datos["rol"]
    st.session_state.usuario = datos["nombre"]
    st.session_state.token = token


def cerrar_sesion():
    _sesiones_activas().pop(st.session_state.get("token", ""), None)
    st.session_state.clear()
    st.query_params.clear()


@st.cache_resource
def _registro_fallos() -> dict:
    """Contador de fallos COMPARTIDO entre sesiones. El bloqueo por session_state
    se evade abriendo una pestaña de incógnito; este no."""
    return {"marcas": [], "bloqueo_hasta": 0.0}


def _resolver_pin(pin: str):
    """Devuelve (rol, nombre) o (None, None).
    Soporta PIN por persona si en secrets existe una tabla [pins]:
        [pins.1234]
        nombre = "Dauris"
        rol = "admin"
    Si no existe, cae al esquema anterior de ADMIN_PIN / VIEWER_PIN."""
    try:
        tabla = st.secrets.get("pins", None)
    except Exception:
        tabla = None
    if tabla:
        for clave, datos in tabla.items():
            if str(pin) == str(clave):
                rol = str(datos.get("rol", "viewer")).lower()
                return ("admin" if rol == "admin" else "viewer"), str(datos.get("nombre", "usuario"))
    if pin and pin == st.secrets.get("ADMIN_PIN", None):
        return "admin", "Administrador"
    if pin and pin == st.secrets.get("VIEWER_PIN", None):
        return "viewer", "Visualización"
    return None, None


def login_screen():
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.markdown(
        '<div class="ant-head" style="margin-top:3rem;">'
        '<div style="font-size:2.6rem;">🚢</div>'
        '<div class="ant-title" style="font-size:1.7rem;">Antillana Comercial · Cargas en Tránsito</div>'
        '<div class="ant-sub">Acceso restringido.</div>'
        "</div>",
        unsafe_allow_html=True,
    )

    st.session_state.setdefault("intentos", 0)
    st.session_state.setdefault("bloqueado_hasta", 0.0)

    registro = _registro_fallos()
    ahora = time.time()
    registro["marcas"] = [m for m in registro["marcas"] if ahora - m < VENTANA_FALLOS]

    _, centro, _ = st.columns([1, 1.2, 1])
    bloqueo = max(st.session_state.bloqueado_hasta, registro["bloqueo_hasta"])
    if ahora < bloqueo:
        restante = int(bloqueo - ahora)
        with centro:
            st.error(f"Demasiados intentos fallidos. Intenta de nuevo en {restante // 60} min {restante % 60} seg.")
        return

    with centro:
        # El PIN va dentro de un st.form a propósito: con un text_input suelto +
        # st.button, presionar Enter solo dispara un rerun y el botón nunca queda
        # "pulsado", así que había que hacer clic obligatoriamente. Dentro de un
        # formulario, Enter equivale a pulsar el submit.
        with st.form("form_login", clear_on_submit=True, border=False):
            pin = st.text_input("PIN", type="password", max_chars=LARGO_PIN,
                                label_visibility="collapsed",
                                placeholder=f"PIN {LARGO_PIN} dígitos")
            entrar = st.form_submit_button("Entrar", type="primary", width="stretch")

    if not entrar:
        return

    rol, nombre = _resolver_pin(pin)
    if rol:
        st.session_state.intentos = 0
        _abrir_sesion(rol, nombre)
        registrar_log("Inicio de sesión", detalle=f"rol={rol}")
        st.rerun()
        return

    time.sleep(1.0)  # freno artificial contra fuerza bruta
    st.session_state.intentos += 1
    registro["marcas"].append(ahora)
    restantes = MAX_INTENTOS_SESION - st.session_state.intentos

    if len(registro["marcas"]) >= MAX_FALLOS_GLOBAL:
        registro["bloqueo_hasta"] = ahora + BLOQUEO_SEGUNDOS
        registro["marcas"] = []
        with centro:
            st.error("Demasiados intentos fallidos desde varios accesos. Bloqueado por 15 minutos.")
    elif restantes <= 0:
        st.session_state.bloqueado_hasta = ahora + BLOQUEO_SEGUNDOS
        st.session_state.intentos = 0
        with centro:
            st.error("PIN incorrecto. Acceso bloqueado por 15 minutos.")
    else:
        with centro:
            st.error(f"PIN incorrecto. Te quedan {restantes} intento(s).")


# ---------------------------------------------------------------------------
# COMPONENTES DE UI
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _logo_base64() -> str:
    """Si existe assets/logo.png (o .jpg) en el repo, se muestra en el encabezado.
    Si no, no pasa nada: la app sigue con el ícono vectorial. No invento un logo
    ni lo traigo de internet."""
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
    st.markdown(
        f'<div class="ant-head">'
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
        etiqueta = texto_estado(r["EstadoTexto"], r["DiasRel"])
        etapa_puerto = str(r.get(COL_ESTADO_PUERTO, "")).strip()
        if etapa_puerto:
            etiqueta = f"{etiqueta} · {ETIQUETA_CORTA_ETAPA.get(etapa_puerto, etapa_puerto)}"
        partes.append(
            f'<div class="fila" style="border-left-color:{color};">'
            f'<div class="c-bl" data-l="BL">{esc(r[COL_BL]) if str(r[COL_BL]).strip() else "(sin BL)"}</div>'
            f'<div class="c-suave" data-l="Descripción">{esc(r[COL_DESC])}</div>'
            f'<div class="c-suave" data-l="Modelo/Serie">{esc(r[COL_MODELO])}</div>'
            f'<div data-l="Cantidad">{esc(r[COL_CANT])}</div>'
            f'<div data-l="País">{esc(r[COL_PAIS])}</div>'
            f'<div data-l="ETA">{esc(formato_eta(r[COL_ETA]))}</div>'
            f'<div class="c-badge" data-l="Estado"><span class="badge" style="background:{color};">{esc(etiqueta)}</span></div>'
            f"</div>"
        )
    partes.append("</div>")
    st.markdown("".join(partes), unsafe_allow_html=True)


def grafico_linea_tiempo(df: pd.DataFrame, key: str):
    """Qué viene encima, semana por semana. Responde la pregunta que un gerente
    hace de verdad ('¿qué me llega en las próximas semanas?'), que la dona por
    estado no respondía porque repetía exactamente lo que ya dicen los KPIs."""
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
        bargap=0.35,
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=f"tl_{key}")


def grafico_paises(df: pd.DataFrame, key: str):
    serie = df[COL_PAIS].replace("", NO_ESPECIFICADO).value_counts().sort_values()
    if serie.empty:
        return
    colores = [PALETA_PAISES[i % len(PALETA_PAISES)] for i in range(len(serie))]
    fig = go.Figure(
        data=[go.Bar(x=serie.values, y=serie.index, orientation="h",
                     marker=dict(color=colores), text=serie.values, textposition="outside",
                     hovertemplate="%{y}: %{x} embarque(s)<extra></extra>")]
    )
    fig.update_layout(
        margin=dict(t=18, b=10, l=10, r=24), height=260,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#374151", size=11),
        xaxis=dict(showgrid=False, showticklabels=False,
                   title=dict(text="Por país de origen (clic para filtrar)", font=dict(size=10))),
        yaxis=dict(showgrid=False),
    )
    seleccion = st.plotly_chart(
        fig, width="stretch", config={"displayModeBar": False},
        on_select="rerun", selection_mode="points", key=f"pais_chart_{key}",
    )
    puntos = (seleccion or {}).get("selection", {}).get("points", [])
    if puntos:
        pais = puntos[0].get("y")
        if pais and pais != NO_ESPECIFICADO:
            # Plotly conserva la selección entre reruns: sin esta firma, el clic se
            # reaplicaría en cada rerun y anularía cualquier cambio manual posterior.
            firma = f"{key}:{pais}"
            if firma != st.session_state.get(f"firma_pais_{key}"):
                st.session_state[f"pais_{key}"] = pais
                st.session_state[f"firma_pais_{key}"] = firma
                rerun_fragmento()


def _envolver_etiqueta(texto: str) -> str:
    """Corta una etiqueta larga en dos líneas balanceadas por palabras (no por
    el primer espacio, que para 'Recepción y declaración de la mercancía'
    dejaba una primera línea de una palabra y una segunda carguísima)."""
    if len(texto) <= 16:
        return texto
    palabras = texto.split(" ")
    medio = (len(palabras) + 1) // 2
    return " ".join(palabras[:medio]) + "<br>" + " ".join(palabras[medio:])


def grafico_flujo_puerto(etapa_actual: str, key: str):
    """Diagrama de las etapas del proceso en puerto (solo carga marítima):
    hecho en verde, la etapa actual en ámbar, lo que falta en gris. etapa_actual
    puede venir vacía (recepción aún sin confirmar)."""
    idx_actual = INDICE_ETAPA.get(etapa_actual, -1)
    n = len(ETAPAS_PUERTO)
    xs = list(range(n))

    colores_punto, colores_linea = [], []
    for i in range(n):
        if i < idx_actual:
            colores_punto.append(COLOR_ETAPA_HECHA)
        elif i == idx_actual:
            colores_punto.append(COLOR_ETAPA_ACTUAL)
        else:
            colores_punto.append(COLOR_ETAPA_PENDIENTE)
    for i in range(n - 1):
        colores_linea.append(COLOR_ETAPA_HECHA if i < idx_actual else COLOR_ETAPA_PENDIENTE)

    fig = go.Figure()
    for i in range(n - 1):
        fig.add_trace(go.Scatter(
            x=[xs[i], xs[i + 1]], y=[0, 0], mode="lines",
            line=dict(color=colores_linea[i], width=5), hoverinfo="skip", showlegend=False,
        ))
    fig.add_trace(go.Scatter(
        x=xs, y=[0] * n, mode="markers+text", showlegend=False,
        marker=dict(size=32, color=colores_punto, line=dict(color="#FFFFFF", width=2)),
        text=[ICONO_ETAPA.get(etapa, str(i + 1)) for i, etapa in enumerate(ETAPAS_PUERTO)],
        textfont=dict(size=16), hovertext=ETAPAS_PUERTO, hovertemplate="%{hovertext}<extra></extra>",
    ))
    anotaciones = [
        dict(x=xs[i], y=-0.55, text=_envolver_etiqueta(etapa), showarrow=False,
             font=dict(size=10, color="#111827" if i == idx_actual else "#6B7280"), align="center")
        for i, etapa in enumerate(ETAPAS_PUERTO)
    ]
    fig.update_layout(
        margin=dict(t=10, b=10, l=20, r=20), height=150,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(visible=False, range=[-0.4, n - 0.6]),
        yaxis=dict(visible=False, range=[-1.2, 0.5]),
        annotations=anotaciones,
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=f"flujo_{key}")


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
def selector_horizontal(label: str, opciones: list, key: str, default=None, formato=None,
                        ancho: str = "stretch"):
    """Segmented control cuando la versión de Streamlit lo trae; radio horizontal
    si no. Sustituye a st.tabs para las categorías: con tabs, Streamlit ejecuta el
    cuerpo de TODAS las pestañas en cada rerun aunque el usuario vea una sola, y
    eso significaba armar seis bloques de KPI, doce gráficos y seis listas para
    mostrar la sexta parte."""
    formato = formato or (lambda x: str(x))
    # Si la clave ya tiene valor en session_state, pasar default= además dispara
    # una advertencia de Streamlit; el estado guardado manda.
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


def _filtro_en_proceso_puerto(df: pd.DataFrame) -> pd.DataFrame:
    """Cruza todas las categorías marítimas: todo lo que ya confirmó llegada a
    puerto y sigue activo (no se ha archivado como recibido). Aéreos nunca
    aparece aquí porque no usa este flujo."""
    if df.empty or COL_ESTADO_PUERTO not in df.columns:
        return df.iloc[0:0]
    es_maritimo = df["Categoria"].isin(CATEGORIAS_PUERTO)
    etapa = df[COL_ESTADO_PUERTO].astype(str).str.strip()
    en_proceso = es_maritimo & (etapa != "")
    return df[en_proceso]


def _resumen_etapas_puerto(df: pd.DataFrame):
    """Conteo por etapa para la vista cruzada — de un vistazo, cuántos
    embarques hay en declaración vs. cuántos esperando que Finanzas pague."""
    conteo = df[COL_ESTADO_PUERTO].value_counts().to_dict()
    cols = st.columns(len(ETAPAS_PUERTO))
    for col, etapa in zip(cols, ETAPAS_PUERTO):
        col.metric(ETIQUETA_CORTA_ETAPA.get(etapa, etapa), conteo.get(etapa, 0))


def _panel_en_proceso_puerto(df: pd.DataFrame, rol: str, contexto: str):
    """Un diagrama por embarque, siempre visible — no hay que ir a buscarlo en
    la ficha ni volver a elegir el BL en 'Acciones'. Nace de un problema real:
    el diagrama se veía una vez al confirmar la llegada y después 'desaparecía'
    porque solo se mostraba si volvías a seleccionar ese embarque a mano.
    Admins pueden avanzar o retroceder la etapa aquí mismo.

    contexto identifica DESDE DÓNDE se llama (Todos / una categoría puntual /
    la pestaña dedicada 'En proceso (puerto)'): el mismo embarque puede
    aparecer en más de un lugar, y sin esto las claves de sus widgets
    colisionarían — una selección de etapa sin guardar en una vista se vería
    reflejada en otra, porque Streamlit ignora el valor por defecto una vez
    que una clave ya tiene algo en session_state."""
    es_admin = rol == "admin"
    for _, fila in df.sort_values(["Categoria", COL_ETA]).iterrows():
        bl = str(fila[COL_BL]).strip()
        categoria = fila["Categoria"]
        etapa_actual = str(fila.get(COL_ESTADO_PUERTO, "")).strip()
        clave = (f"{_slug_css(contexto)}_{_slug_css(categoria)}_"
                f"{_slug_css(bl or str(fila.get('FilaSheet', '')))}")

        st.markdown(f"**{esc(bl) or '(sin BL)'}** · {esc(fila[COL_DESC])} · {esc(categoria)}")
        grafico_flujo_puerto(etapa_actual, f"proceso_{clave}")

        dias_solicitud = fila.get("DiasSolicitudPago")
        if es_numero(dias_solicitud):
            etiqueta_pago = "tardó en pagarse" if str(fila.get(COL_FECHA_PAGO, "")).strip() \
                else "lleva sin pagarse"
            st.caption(f"{int(dias_solicitud)} día(s) {etiqueta_pago} (desde que se solicitó el pago)")
        dias_espera = fila.get("DiasPagoDespacho")
        if es_numero(dias_espera):
            st.caption(f"{int(dias_espera)} día(s) desde que se pagó, esperando despacho")

        if es_admin:
            idx_default = INDICE_ETAPA.get(etapa_actual, 0)
            a1, a2 = st.columns([3, 1])
            nueva_etapa = a1.selectbox("Etapa", ETAPAS_PUERTO, index=idx_default,
                                       key=f"etapa_{clave}", label_visibility="collapsed")
            a2.write("")
            if a2.button("Guardar", key=f"guardar_{clave}", width="stretch"):
                ok, mensaje = avanzar_estado_puerto(bl, categoria, nueva_etapa)
                if ok:
                    accion = "Avance en puerto" if INDICE_ETAPA.get(nueva_etapa, 0) >= idx_default \
                        else "Retroceso en puerto"
                    registrar_log(accion, bl, categoria, nueva_etapa)
                    invalidar_caches()
                    st.rerun()
                else:
                    st.error(mensaje)

            # "Despachado" no es una etapa: equivale a la entrada a almacén,
            # así que archivar como recibido solo se habilita en la última
            # etapa del flujo — pedido explícito, no una sugerencia blanda.
            if etapa_actual == ETAPAS_PUERTO[-1]:
                if st.button("Despachado — marcar como recibido", key=f"recibido_{clave}",
                             type="primary", width="stretch"):
                    ok, mensaje = marcar_como_recibido(bl, categoria)
                    if ok:
                        registrar_log("Recibido", bl, categoria, f"ETA {fila[COL_ETA]}")
                        invalidar_caches()
                        st.rerun()
                    else:
                        st.error(mensaje)
            else:
                st.caption(f"Falta llegar a '{ETAPAS_PUERTO[-1]}' para poder marcarlo como recibido.")
        st.divider()


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

    # Enriquecimiento único: estados, fechas y valores se calculan una sola vez
    # por refresco y las vistas por categoría son rebanadas de este resultado.
    df_todo = enriquecer(df_todo)
    recibidas_mes = contar_recibidas_mes(datos["historico"])

    opciones = ["Todos"] + [c for c in CATEGORIAS if (df_todo["Categoria"] == c).any()]
    en_proceso_df = _filtro_en_proceso_puerto(df_todo)
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

    if seleccion == "Todos":
        sub = df_todo
        _render_categoria(sub, st.session_state.get("rol", "viewer"), seleccion, recibidas_mes)
    elif seleccion == VISTA_EN_PROCESO_PUERTO:
        _resumen_etapas_puerto(en_proceso_df)
        st.divider()
        _panel_en_proceso_puerto(en_proceso_df, st.session_state.get("rol", "viewer"),
                                 contexto=VISTA_EN_PROCESO_PUERTO)
    else:
        sub = df_todo[df_todo["Categoria"] == seleccion]
        _render_categoria(sub, st.session_state.get("rol", "viewer"), seleccion, recibidas_mes)


def contar_recibidas_mes(historico: pd.DataFrame) -> int:
    if historico.empty:
        return 0
    hoy = hoy_rd()
    total = 0
    for valor in historico.get("Fecha_Recibido", []):
        f = parsear_fecha(valor)
        if f and f.year == hoy.year and f.month == hoy.month:
            total += 1
    return total


@st.fragment
def _render_categoria(df: pd.DataFrame, rol: str, tab_key: str, recibidas_mes: int):
    if df.empty:
        st.info("No hay embarques en esta categoría.")
        return

    conteo = df["EstadoTexto"].value_counts().to_dict()
    proximos_n = conteo.get(EST_PROXIMO, 0)
    en_puerto_n = conteo.get(EST_PUERTO, 0)
    retrasados_n = conteo.get(EST_RETRASADO, 0)
    sin_fecha_n = conteo.get(EST_SIN_FECHA, 0)

    valores = [v for v in df.get("ValorNum", []) if es_numero(v)]
    valor_total = sum(valores) if valores else None
    valor_puerto = sum(v for v, e in zip(df.get("ValorNum", []), df["EstadoTexto"])
                       if es_numero(v) and e == EST_PUERTO) if valores else None

    # -------------------- KPIs --------------------
    kpis = [
        ("Total en tránsito", len(df), COLOR_TOTAL, "Todos", "total"),
        (f"Próximos {UMBRAL_PROXIMO} días", proximos_n, STATUS_COLOR[EST_PROXIMO], EST_PROXIMO, "proximos"),
        ("Por confirmar llegada", en_puerto_n, STATUS_COLOR[EST_PUERTO], EST_PUERTO, "enpuerto"),
        ("Retrasados", retrasados_n, STATUS_COLOR[EST_RETRASADO], EST_RETRASADO, "retrasados"),
        (f"Recibidas en {MESES_ES[hoy_rd().month]}", recibidas_mes, COLOR_RECIBIDAS_MES, "__historico__", "recibidas"),
    ]
    # OJO con la clave del contenedor: Streamlit la usa TAL CUAL como clase CSS
    # (st-key-<clave>). Si lleva un espacio ("Carga Suelta") el navegador la parte
    # en dos clases y el selector no engancha; con acentos ("Aéreos") tampoco es
    # fiable. Por eso la clave se construye con un slug ASCII.
    clave = _slug_css(tab_key)
    estilos = "".join(
        f".st-key-kpi_{clave}_{slug} button {{"
        f"background:{color} !important; color:#fff !important; border:none !important;"
        f"border-radius:14px !important; width:100% !important; min-height:92px !important;"
        f"text-align:center !important; padding:14px 18px !important;"
        f"box-shadow:0 2px 8px rgba(17,24,39,0.12) !important; transition:filter .15s ease;}} "
        f".st-key-kpi_{clave}_{slug} button > div {{"
        f"display:flex !important; flex-direction:column !important; align-items:center !important;"
        f"justify-content:center !important; width:100% !important;}} "
        f".st-key-kpi_{clave}_{slug} button p {{margin:0 !important; color:#fff !important;"
        f"text-align:center !important; width:100% !important;}} "
        f".st-key-kpi_{clave}_{slug} button p:first-of-type {{"
        f"font-size:0.70rem !important; font-weight:700 !important; letter-spacing:0.06em !important;"
        f"opacity:0.92 !important;}} "
        f".st-key-kpi_{clave}_{slug} button p:last-of-type {{"
        f"font-size:2.0rem !important; font-weight:800 !important; line-height:1.05 !important;"
        f"margin-top:6px !important;}} "
        f".st-key-kpi_{clave}_{slug} button:hover {{filter:brightness(0.93); color:#fff !important;}} "
        f".st-key-kpi_{clave}_{slug} button:focus {{color:#fff !important;"
        f"box-shadow:0 0 0 3px rgba(17,24,39,0.15) !important;}}"
        for _, _, color, _, slug in kpis
    )
    st.markdown(f"<style>{estilos}</style>", unsafe_allow_html=True)

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

    if rol == "admin":
        _panel_confirmacion(df, tab_key)

    if sin_fecha_n and rol == "admin":
        st.warning(
            f"{sin_fecha_n} embarque(s) tienen un ETA que la app no puede interpretar y quedan fuera de "
            "los conteos por fecha. Revísalos en Herramientas → Normalizar fechas."
        )

    st.write("")

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
    # Mismo filtro que la pestaña dedicada "En proceso (puerto)", pero aquí
    # aparece directo en la vista donde ya estás (Todos o una categoría
    # puntual) — pedido explícito: no obligar a navegar a otra pestaña para
    # ver qué hay en puerto y en qué etapa está cada uno.
    en_proceso_aqui = _filtro_en_proceso_puerto(df)
    if not en_proceso_aqui.empty:
        st.markdown("**En proceso en puerto**")
        _resumen_etapas_puerto(en_proceso_aqui)
        _panel_en_proceso_puerto(en_proceso_aqui, rol, contexto=tab_key)

    st.divider()

    # -------------------- FILTROS --------------------
    paises = ["Todos"] + sorted({p for p in df[COL_PAIS] if str(p).strip()})
    estados = ["Todos"] + [e for e in STATUS_ORDER]
    criterios = ["Urgencia", "ETA más próximo", "ETA más lejano", "BL", "País", "Descripción"]
    if valor_total:
        criterios.append("Valor")

    f1, f2, f3 = st.columns([2, 1, 1])
    busqueda = f1.text_input("Buscar", key=f"busca_{tab_key}",
                             placeholder="BL, descripción o modelo…", label_visibility="collapsed")
    pais_sel = f2.selectbox("País", paises, key=f"pais_{tab_key}", label_visibility="collapsed")
    estado_sel = f3.selectbox("Estado", estados, key=f"estado_{tab_key}", label_visibility="collapsed")

    f4, f5 = st.columns([1, 1])
    orden_sel = f4.selectbox("Ordenar por", criterios, key=f"orden_{tab_key}")
    with f5:
        st.write("")
        st.write("")
        if st.button("Limpiar filtros", key=f"limpiar_{tab_key}", width="stretch"):
            for k in (f"busca_{tab_key}", f"pais_{tab_key}", f"estado_{tab_key}", f"orden_{tab_key}"):
                st.session_state.pop(k, None)
            st.session_state.pop(f"firma_pais_{tab_key}", None)
            rerun_fragmento()

    filtrado = df
    if pais_sel != "Todos":
        filtrado = filtrado[filtrado[COL_PAIS] == pais_sel]
    if estado_sel != "Todos":
        filtrado = filtrado[filtrado["EstadoTexto"] == estado_sel]
    if busqueda and busqueda.strip():
        q = _norm(busqueda)
        filtrado = filtrado[filtrado.apply(
            lambda r: q in _norm(f"{r[COL_BL]} {r[COL_DESC]} {r[COL_MODELO]}"), axis=1
        )]
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
            opciones_det = [
                f"{r[COL_BL] or '(sin BL)'} · {str(r[COL_DESC])[:40]}"
                for _, r in filtrado.iterrows()
            ]
            elegido = st.selectbox("Embarque", opciones_det, key=f"detalle_{tab_key}",
                                   label_visibility="collapsed")
            _ficha_embarque(filtrado.iloc[opciones_det.index(elegido)])

    # -------------------- ACCIONES DE ADMIN --------------------
    if rol == "admin":
        st.write("")
        with st.expander("Acciones sobre un embarque", expanded=False):
            _panel_acciones(filtrado, tab_key)


def _panel_confirmacion(df: pd.DataFrame, tab_key: str):
    """El ETA vencido no dice si la mercancía llegó, solo que la fecha pasó.
    Este panel hace la pregunta directa —¿llegó, sí o no?— y con la respuesta el
    embarque se archiva como recibido o se marca como retrasado. Va a la vista,
    sin desplegable, porque es lo único de la pantalla que exige acción hoy."""
    # En categorías marítimas, una vez confirmada la llegada a puerto el
    # embarque pasa al flujo detallado (ver _panel_acciones) y esta pregunta
    # ya no aplica: preguntar "¿ya llegó?" indefinidamente sería ruido, porque
    # ETA vencido + EstadoTexto no cambian mientras avanza por las etapas.
    es_maritimo = df["Categoria"].isin(CATEGORIAS_PUERTO)
    tiene_etapa = df.get(COL_ESTADO_PUERTO, pd.Series([""] * len(df), index=df.index)) \
        .astype(str).str.strip().ne("")
    ya_en_flujo = es_maritimo & tiene_etapa

    pendientes = df[(df["EstadoTexto"] == EST_PUERTO) & ~ya_en_flujo]
    retrasados = df[(df["EstadoTexto"] == EST_RETRASADO) & ~ya_en_flujo]
    if pendientes.empty and retrasados.empty:
        return

    TOPE = 12
    if not pendientes.empty:
        st.markdown(
            f'<div class="conf-titulo">¿Ya llegó? · {len(pendientes)} embarque(s) con la fecha vencida</div>',
            unsafe_allow_html=True,
        )

    def _fila_confirmacion(r, marcados_como_retrasados: bool):
        bl = str(r[COL_BL]).strip()
        categoria = r["Categoria"]
        etiqueta = texto_estado(r["EstadoTexto"], r["DiasRel"])
        color = STATUS_COLOR.get(r["EstadoTexto"], "#6B7280")
        c1, c2, c3 = st.columns([3.2, 1.2, 1.6])
        c1.markdown(
            f'<div class="conf-fila"><span class="conf-bl">{esc(bl or "(sin BL)")}</span>'
            f'<span class="conf-desc">{esc(r[COL_DESC])}</span>'
            f'<span class="badge" style="background:{color};">{esc(etiqueta)}</span></div>',
            unsafe_allow_html=True,
        )
        if not bl:
            c2.caption("Sin BL: no se puede gestionar")
            return
        clave = f"{_slug_css(tab_key)}_{_slug_css(bl)}"
        es_maritimo_fila = categoria in CATEGORIAS_PUERTO
        etiqueta_boton = "Sí, llegó a puerto" if es_maritimo_fila else "Sí, llegó"
        if c2.button(etiqueta_boton, key=f"si_llego_{clave}", type="primary", width="stretch"):
            if es_maritimo_fila:
                # No se archiva todavía: entra al flujo de puerto (declaración,
                # pago, despacho) y el archivo final se hace desde "Acciones"
                # una vez despachado y recibido en almacén.
                ok, mensaje = avanzar_estado_puerto(bl, categoria, ETAPAS_PUERTO[0])
                accion_log = "Llegada a puerto confirmada"
            else:
                ok, mensaje = marcar_como_recibido(bl, categoria)
                accion_log = "Llegada confirmada"
            if ok:
                registrar_log(accion_log, bl, categoria, f"ETA {r[COL_ETA]}")
                invalidar_caches()
                st.rerun()
            else:
                st.error(mensaje)
        if marcados_como_retrasados:
            if c3.button("Sigue retrasado", key=f"sigue_{clave}", width="stretch", disabled=True):
                pass
            c3.caption("Actualiza el ETA en Editar")
        else:
            if c3.button("No, está retrasado", key=f"no_llego_{clave}", width="stretch"):
                ok, mensaje = marcar_estatus_llegada(bl, categoria, VALOR_RETRASADO)
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
    con el estado ya redactado y el ETA en formato legible."""
    if df.empty:
        return pd.DataFrame(columns=[COL_BL, "Descripción", "Estado"])
    salida = pd.DataFrame({
        "BL": df[COL_BL],
        "Descripción": df[COL_DESC],
        "Modelo/Serie": df[COL_MODELO],
        "Cantidad": df[COL_CANT],
        "País de origen": df[COL_PAIS],
        "ETA": [formato_eta(v) for v in df[COL_ETA]],
        "Estado": [texto_estado(e, d) for e, d in zip(df["EstadoTexto"], df["DiasRel"])],
        "Categoría": df["Categoria"],
    })
    for extra in columnas_extra(df):
        salida[extra] = df[extra]
    if COL_ACTUALIZACION in df.columns:
        salida["Última actualización"] = df[COL_ACTUALIZACION]
    if COL_ACTUALIZADO_POR in df.columns:
        salida["Actualizado por"] = df[COL_ACTUALIZADO_POR]
    return salida.reset_index(drop=True)


def _ficha_embarque(fila):
    """Todos los campos del embarque, incluidas las columnas que alguien haya
    agregado en el Sheet y que la app no gestiona."""
    campos = [
        ("BL", fila[COL_BL]),
        ("Descripción", fila[COL_DESC]),
        ("Modelo / Serie", fila[COL_MODELO]),
        ("Cantidad", fila[COL_CANT]),
        ("País de origen", fila[COL_PAIS]),
        ("Categoría", fila["Categoria"]),
        ("ETA", formato_eta(fila[COL_ETA])),
        ("Estado", texto_estado(fila["EstadoTexto"], fila["DiasRel"])),
    ]
    if str(fila.get(COL_FECHA_SALIDA, "")).strip():
        campos.append(("Fecha de salida", formato_eta(fila[COL_FECHA_SALIDA])))
    dias_transito = fila.get("DiasTransito")
    if es_numero(dias_transito):
        d = int(dias_transito)
        etiqueta_transito = "Tardó en tránsito" if str(fila.get(COL_FECHA_LLEGADA_PUERTO, "")).strip() \
            else "Lleva en tránsito"
        campos.append((etiqueta_transito, f"{d} día{'s' if d != 1 else ''}"))

    es_maritimo = fila["Categoria"] in CATEGORIAS_PUERTO
    etapa_puerto = str(fila.get(COL_ESTADO_PUERTO, "")).strip()
    if es_maritimo and etapa_puerto:
        campos.append(("Etapa en puerto", etapa_puerto))
        if str(fila.get(COL_FECHA_LLEGADA_PUERTO, "")).strip():
            campos.append(("Llegada a puerto", formato_eta(fila[COL_FECHA_LLEGADA_PUERTO])))
        if str(fila.get(COL_FECHA_DECLARACION, "")).strip():
            campos.append(("Recepción y declaración", formato_eta(fila[COL_FECHA_DECLARACION])))
        if str(fila.get(COL_FECHA_SOLICITUD_PAGO, "")).strip():
            campos.append(("Solicitud de pago enviada", formato_eta(fila[COL_FECHA_SOLICITUD_PAGO])))
        dias_solicitud = fila.get("DiasSolicitudPago")
        if es_numero(dias_solicitud):
            d = int(dias_solicitud)
            etiqueta_pago = "Tardó en pagarse" if str(fila.get(COL_FECHA_PAGO, "")).strip() \
                else "Lleva sin pagarse"
            campos.append((etiqueta_pago, f"{d} día{'s' if d != 1 else ''}"))
        if str(fila.get(COL_FECHA_PAGO, "")).strip():
            campos.append(("Pago realizado", formato_eta(fila[COL_FECHA_PAGO])))
            dias_espera = fila.get("DiasPagoDespacho")
            if es_numero(dias_espera):
                d = int(dias_espera)
                campos.append(("Esperando despacho", f"{d} día{'s' if d != 1 else ''}"))

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

    if es_maritimo and etapa_puerto:
        st.caption("Flujo en puerto")
        clave = f"{_slug_css(str(fila['Categoria']))}_{_slug_css(str(fila[COL_BL]) or fila.get('FilaSheet', ''))}"
        grafico_flujo_puerto(etapa_puerto, f"ficha_{clave}")


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

    opciones = [
        f"{r[COL_BL]} · {str(r[COL_DESC])[:38] or 'sin descripción'} · {r['Categoria']}"
        for _, r in con_bl.iterrows()
    ]
    elegido = st.selectbox("Embarque", opciones, key=f"sel_accion_{tab_key}")
    fila = con_bl.iloc[opciones.index(elegido)]
    bl, categoria = str(fila[COL_BL]).strip(), fila["Categoria"]

    es_maritimo_sel = categoria in CATEGORIAS_PUERTO
    etapa_actual = str(fila.get(COL_ESTADO_PUERTO, "")).strip()
    if es_maritimo_sel and (etapa_actual or fila["EstadoTexto"] in (EST_PUERTO, EST_RETRASADO)):
        st.markdown("**Flujo de puerto**")
        if etapa_actual:
            grafico_flujo_puerto(etapa_actual, f"acciones_{tab_key}")
            dias_solicitud = fila.get("DiasSolicitudPago")
            if es_numero(dias_solicitud):
                etiqueta_pago = "tardó en pagarse" if str(fila.get(COL_FECHA_PAGO, "")).strip() \
                    else "lleva sin pagarse"
                st.caption(f"{int(dias_solicitud)} día(s) {etiqueta_pago} (desde que se solicitó el pago).")
            dias_espera = fila.get("DiasPagoDespacho")
            if es_numero(dias_espera):
                st.caption(f"{int(dias_espera)} día(s) desde que se pagó, esperando despacho.")
            idx_default = INDICE_ETAPA.get(etapa_actual, 0)
            clave_bl = f"{_slug_css(tab_key)}_{_slug_css(bl)}"
            a1, a2 = st.columns([2, 1])
            nueva_etapa = a1.selectbox("Etapa", ETAPAS_PUERTO, index=idx_default,
                                       key=f"etapa_{clave_bl}", label_visibility="collapsed")
            a2.write("")
            if a2.button("Guardar etapa", key=f"guardar_etapa_{clave_bl}", width="stretch"):
                ok, mensaje = avanzar_estado_puerto(bl, categoria, nueva_etapa)
                if ok:
                    registrar_log("Avance en puerto", bl, categoria, nueva_etapa)
                    invalidar_caches()
                    st.rerun()
                else:
                    st.error(mensaje)
        else:
            st.caption("Llegada a puerto sin confirmar todavía — usa 'Sí, llegó a puerto' arriba, "
                       "en la sección de confirmación.")
        st.write("")

    # "Despachado" no es una etapa: equivale a la entrada a almacén. Para
    # categorías marítimas que ya entraron al flujo, el archivo como recibido
    # solo se habilita en la última etapa ("Pago realizado") — candado duro,
    # no una sugerencia que se pueda ignorar sin querer.
    bloqueado = bool(es_maritimo_sel and etapa_actual and etapa_actual != ETAPAS_PUERTO[-1])
    c1, c2, c3 = st.columns(3)
    if c1.button("Marcar como recibido", key=f"rec_{tab_key}", type="primary", width="stretch",
                 disabled=bloqueado):
        ok, mensaje = marcar_como_recibido(bl, categoria)
        if ok:
            registrar_log("Recibido", bl, categoria, f"ETA {fila[COL_ETA]}")
            invalidar_caches()
            st.rerun()
        else:
            st.error(mensaje)
    if bloqueado:
        c1.caption(f"Falta llegar a '{ETAPAS_PUERTO[-1]}' (etapa actual: '{etapa_actual}').")

    if c2.button("Editar", key=f"edit_{tab_key}", width="stretch"):
        st.session_state["editar_bl"] = bl
        st.session_state["seccion"] = "Editar"
        st.rerun()

    if c3.button("Eliminar", key=f"del_{tab_key}", width="stretch"):
        st.session_state[f"confirmar_del_{tab_key}"] = (bl, categoria)
        rerun_fragmento()

    pendiente = st.session_state.get(f"confirmar_del_{tab_key}")
    if pendiente:
        bl_pend, cat_pend = pendiente
        st.warning(f"¿Eliminar definitivamente el BL {bl_pend}? No se puede deshacer. "
                   "Si el embarque llegó, usa 'Marcar como recibido' para conservarlo en el histórico.")
        d1, d2, _ = st.columns([1, 1, 3])
        if d1.button("Sí, eliminar", key=f"si_del_{tab_key}", type="primary"):
            ok, mensaje = eliminar_embarque(bl_pend, cat_pend)
            st.session_state.pop(f"confirmar_del_{tab_key}", None)
            if ok:
                registrar_log("Eliminado", bl_pend, cat_pend)
                invalidar_caches()
                st.rerun()
            else:
                st.error(mensaje)
        if d2.button("Cancelar", key=f"no_del_{tab_key}"):
            st.session_state.pop(f"confirmar_del_{tab_key}", None)
            rerun_fragmento()


# ---------------------------------------------------------------------------
# ALTA MANUAL
# ---------------------------------------------------------------------------
def _bls_existentes(datos: dict) -> set:
    activos = set(datos["activos"][COL_BL].astype(str).str.strip()) if not datos["activos"].empty else set()
    historicos = set(datos["historico"][COL_BL].astype(str).str.strip()) if not datos["historico"].empty else set()
    return {b for b in activos | historicos if b}


def form_alta_manual(datos: dict):
    st.subheader("Agregar embarque")
    with st.form("form_embarque", clear_on_submit=True):
        c1, c2 = st.columns(2)
        bl = c1.text_input("BL *")
        descripcion = c2.text_input("Descripción del producto *")
        c3, c4 = st.columns(2)
        modelo = c3.text_input("Modelo o serie")
        cantidad = c4.text_input("Cantidad", placeholder="Ej.: 4 unidades, 2 pallets, 113 bultos")
        c5, c6 = st.columns(2)
        pais = c5.text_input("País de origen")
        eta = c6.date_input("Llegada a puerto (ETA)", value=hoy_rd(), format="DD/MM/YYYY")
        categoria = st.selectbox("Categoría", CATEGORIAS)
        salida = st.date_input("Fecha de salida (opcional)", value=None, format="DD/MM/YYYY",
                               help="Si no la sabes, déjala vacía y agrégala después desde 'Editar'.")
        enviado = st.form_submit_button("Guardar embarque", type="primary")

    if not enviado:
        return

    if not bl.strip() or not descripcion.strip():
        st.error("BL y Descripción son obligatorios.")
        return
    if bl.strip() in _bls_existentes(datos):
        st.error(f"Ya existe un embarque con el BL '{bl.strip()}' (activo o en el histórico).")
        return

    datos_nuevos = {
        COL_BL: bl.strip(),
        COL_DESC: descripcion.strip(),
        COL_MODELO: modelo.strip(),
        COL_CANT: cantidad.strip(),
        COL_PAIS: pais.strip(),
        COL_ETA: eta.isoformat(),
    }
    if salida:
        datos_nuevos[COL_FECHA_SALIDA] = salida.isoformat()

    ok, mensaje = append_row(datos_nuevos, categoria)
    if ok:
        registrar_log("Alta manual", bl.strip(), categoria, f"ETA {eta.isoformat()}")
        invalidar_caches()
        st.success(f"Embarque {bl.strip()} guardado en '{categoria}'.")
        st.rerun()
    else:
        st.error(mensaje)


# ---------------------------------------------------------------------------
# EDICIÓN
# ---------------------------------------------------------------------------
def form_editar(datos: dict):
    st.subheader("Editar embarque")
    df = datos["activos"]
    if df.empty:
        st.info("No hay embarques cargados.")
        return

    con_bl = df[df[COL_BL].astype(str).str.strip() != ""].reset_index(drop=True)
    if con_bl.empty:
        st.info("No hay embarques con BL asignado.")
        return

    opciones = [f"{r[COL_BL]} · {str(r[COL_DESC])[:40]} · {r['Categoria']}" for _, r in con_bl.iterrows()]
    indice_default = 0
    preseleccion = st.session_state.pop("editar_bl", None)
    if preseleccion:
        for i, r in con_bl.iterrows():
            if str(r[COL_BL]).strip() == preseleccion:
                indice_default = int(i)
                break

    elegido = st.selectbox("Embarque a editar", opciones, index=indice_default, key="sel_editar")
    fila = con_bl.iloc[opciones.index(elegido)]
    categoria = fila["Categoria"]
    eta_actual = parsear_fecha(fila[COL_ETA]) or hoy_rd()
    salida_actual = parsear_fecha(fila.get(COL_FECHA_SALIDA, ""))

    with st.form("form_editar"):
        c1, c2 = st.columns(2)
        descripcion = c1.text_input("Descripción", value=str(fila[COL_DESC]))
        modelo = c2.text_input("Modelo o serie", value=str(fila[COL_MODELO]))
        c3, c4 = st.columns(2)
        cantidad = c3.text_input("Cantidad", value=str(fila[COL_CANT]))
        pais = c4.text_input("País de origen", value=str(fila[COL_PAIS]))
        c5, c6 = st.columns(2)
        eta = c5.date_input("Llegada a puerto (ETA)", value=eta_actual, format="DD/MM/YYYY")
        salida = c6.date_input("Fecha de salida", value=salida_actual, format="DD/MM/YYYY")
        st.caption(f"ETA actual en el Sheet: {fila[COL_ETA] or '(vacío)'}")
        guardar = st.form_submit_button("Guardar cambios", type="primary")

    if not guardar:
        return

    cambios = {
        COL_DESC: descripcion.strip(),
        COL_MODELO: modelo.strip(),
        COL_CANT: cantidad.strip(),
        COL_PAIS: pais.strip(),
        COL_ETA: eta.isoformat(),
        COL_FECHA_SALIDA: salida.isoformat() if salida else "",
    }
    ok, mensaje = actualizar_embarque(str(fila[COL_BL]).strip(), categoria, cambios)
    if ok:
        detalles = []
        if str(fila[COL_ETA]).strip() != eta.isoformat():
            detalles.append(f"ETA {fila[COL_ETA] or 'vacío'} → {eta.isoformat()}")
        registrar_log("Edición", str(fila[COL_BL]).strip(), categoria, "; ".join(detalles) or "campos varios")
        invalidar_caches()
        st.success("Embarque actualizado.")
        st.rerun()
    else:
        st.error(mensaje)


# ---------------------------------------------------------------------------
# CARGA MASIVA
# ---------------------------------------------------------------------------
def _plantilla_excel() -> bytes:
    buffer = io.BytesIO()
    ejemplo = pd.DataFrame(
        [{
            COL_BL: "EGLV142653674620",
            COL_DESC: "Montacargas",
            COL_MODELO: "ERP3.0MXLG / ERP20UXTL",
            COL_CANT: "4 unidades",
            COL_PAIS: "China",
            COL_ETA: "2026-08-25",
            COL_FECHA_SALIDA: "",
        }],
        columns=REQUIRED_COLUMNS + [COL_FECHA_SALIDA],
    )
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        ejemplo.to_excel(writer, index=False, sheet_name="Embarques")
    return buffer.getvalue()


def form_carga_masiva(datos: dict):
    st.subheader("Carga masiva desde Excel")
    st.download_button(
        "Descargar plantilla",
        data=_plantilla_excel(),
        file_name="plantilla_embarques_antillana.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    categoria = st.selectbox("Categoría de destino (todo el archivo se carga aquí)", CATEGORIAS)
    st.caption("Columnas obligatorias: " + ", ".join(REQUIRED_COLUMNS) +
               f". '{COL_FECHA_SALIDA}' es opcional. El ETA puede venir en cualquier formato "
               "reconocible; se guarda como AAAA-MM-DD.")

    archivo = st.file_uploader("Archivo .xlsx", type=["xlsx"])
    if archivo is None:
        return

    try:
        nuevo = pd.read_excel(archivo, dtype=str)
    except Exception as e:
        st.error(f"No se pudo leer el archivo: {e}")
        return

    mapa = {}
    for canon in REQUIRED_COLUMNS + [COL_FECHA_SALIDA]:
        for real in nuevo.columns:
            if _norm(real) == _norm(canon):
                mapa[real] = canon
                break
    nuevo = nuevo.rename(columns=mapa)

    faltantes = [c for c in REQUIRED_COLUMNS if c not in nuevo.columns]
    if faltantes:
        st.error(f"Faltan columnas obligatorias: {', '.join(faltantes)}. Usa la plantilla.")
        return

    trae_salida = COL_FECHA_SALIDA in nuevo.columns
    columnas_a_usar = REQUIRED_COLUMNS + ([COL_FECHA_SALIDA] if trae_salida else [])
    nuevo = nuevo[columnas_a_usar].dropna(how="all").copy()
    nuevo = nuevo[nuevo[COL_BL].astype(str).str.strip().ne("") | nuevo[COL_DESC].astype(str).str.strip().ne("")]

    invalidas, normalizadas = [], []
    for i, valor in enumerate(nuevo[COL_ETA]):
        f = parsear_fecha(valor)
        if f is None:
            invalidas.append((i + 2, valor))
            normalizadas.append("")
        else:
            normalizadas.append(f.isoformat())
    if invalidas:
        st.error("Hay fechas ETA que no se pudieron interpretar en las filas: " +
                 ", ".join(f"{fila} ('{val}')" for fila, val in invalidas))
        return
    nuevo[COL_ETA] = normalizadas

    if trae_salida:
        # Opcional: si viene ilegible, se deja en blanco en vez de bloquear la
        # carga completa por una columna que no es obligatoria.
        nuevo[COL_FECHA_SALIDA] = [
            (parsear_fecha(v).isoformat() if parsear_fecha(v) else "") for v in nuevo[COL_FECHA_SALIDA]
        ]

    existentes = _bls_existentes(datos)
    bl_norm = nuevo[COL_BL].astype(str).str.strip()
    dup_archivo = bl_norm.duplicated(keep="first") & bl_norm.ne("")
    if dup_archivo.any():
        st.warning("El archivo trae BL repetidos internamente; solo se cargará la primera aparición de: " +
                   ", ".join(sorted(set(bl_norm[dup_archivo]))))
    es_dup = bl_norm.isin(existentes) | dup_archivo
    if es_dup.any():
        ya = sorted({b for b in bl_norm[bl_norm.isin(existentes)] if b})
        if ya:
            st.warning("Estos BL ya existen en el sistema y no se cargarán de nuevo: " + ", ".join(ya))

    nuevos = nuevo[~es_dup]
    if nuevos.empty:
        st.info("No hay embarques nuevos que cargar.")
        return

    st.write(f"Vista previa de {len(nuevos)} embarque(s) que se cargarán en **{categoria}**:")
    st.dataframe(nuevos, width="stretch", hide_index=True)

    if st.button(f"Confirmar carga de {len(nuevos)} embarque(s)", type="primary"):
        ok, mensaje = append_rows_bulk(nuevos, categoria)
        if ok:
            registrar_log("Carga masiva", "", categoria, f"{len(nuevos)} embarque(s)")
            invalidar_caches()
            st.success(f"{len(nuevos)} embarque(s) cargado(s) en '{categoria}'.")
            st.rerun()
        else:
            st.error(mensaje)


# ---------------------------------------------------------------------------
# HISTÓRICO
# ---------------------------------------------------------------------------
def _preparar_historico(historico: pd.DataFrame) -> pd.DataFrame:
    """Historico crudo -> DataFrame con fecha parseada, año y mes.
    Nada se borra nunca de la pestaña 'Recibido (Mes)': cada recepción queda ahí
    con su fecha, así que en noviembre se puede consultar julio del año pasado
    igual que el mes en curso."""
    df = historico.copy()
    if df.empty:
        return df
    df["FechaParsed"] = [parsear_fecha(v) for v in df.get("Fecha_Recibido", [])]
    df = df[df["FechaParsed"].notna()].copy()
    if df.empty:
        return df
    df["Anio"] = [f.year for f in df["FechaParsed"]]
    df["Mes"] = [f.month for f in df["FechaParsed"]]
    if "Categoria_Origen" in df.columns:
        df["Categoria_Origen"] = df["Categoria_Origen"].replace("", "Sin categoría").fillna("Sin categoría")
    else:
        df["Categoria_Origen"] = "Sin categoría"
    return df.sort_values("FechaParsed").reset_index(drop=True)


def _grafico_anual(df_anio: pd.DataFrame, anio: int):
    """Los 12 meses del año, incluidos los que van en cero: un mes vacío también
    es información y desaparecerlo del gráfico distorsiona la lectura."""
    conteo = df_anio.groupby("Mes").size().to_dict()
    hoy = hoy_rd()
    etiquetas = [MESES_ES_CORTO[m] for m in range(1, 13)]
    valores = [int(conteo.get(m, 0)) for m in range(1, 13)]
    colores = [
        COLOR_RECIBIDAS_MES if not (anio == hoy.year and m == hoy.month) else "#1B5E20"
        for m in range(1, 13)
    ]
    fig = go.Figure(data=[go.Bar(
        x=etiquetas, y=valores, marker=dict(color=colores),
        text=[v if v else "" for v in valores], textposition="outside",
        hovertemplate="%{x} " + str(anio) + ": %{y} recibido(s)<extra></extra>",
    )])
    fig.update_layout(
        margin=dict(t=20, b=10, l=10, r=10), height=250,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#374151", size=11), showlegend=False, bargap=0.3,
        xaxis=dict(showgrid=False),
        yaxis=dict(showgrid=True, gridcolor="#F3F4F6", showticklabels=False),
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=f"hist_anual_{anio}")


def mostrar_historico(datos: dict, rol: str):
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.subheader("Histórico de embarques recibidos")

    crudo = datos["historico"]
    df = _preparar_historico(crudo)

    if crudo.empty:
        st.info(
            "Todavía no hay embarques archivados. Cada vez que marques uno como recibido, "
            "queda guardado aquí de forma permanente con la fecha en que llegó, y se puede "
            "consultar en cualquier momento futuro por mes y por año."
        )
        return
    if df.empty:
        st.warning("Hay registros archivados, pero ninguno tiene una fecha de recibido interpretable.")
        return

    descartados = len(crudo) - len(df)
    hoy = hoy_rd()

    # -------------------- Indicadores del momento --------------------
    mes_ant = (hoy.year - 1, 12) if hoy.month == 1 else (hoy.year, hoy.month - 1)
    n_actual = int(((df["Anio"] == hoy.year) & (df["Mes"] == hoy.month)).sum())
    n_anterior = int(((df["Anio"] == mes_ant[0]) & (df["Mes"] == mes_ant[1])).sum())
    n_anio = int((df["Anio"] == hoy.year).sum())
    n_mismo_mes_anio_pasado = int(((df["Anio"] == hoy.year - 1) & (df["Mes"] == hoy.month)).sum())
    delta = n_actual - n_anterior

    k1, k2, k3 = st.columns(3)
    k1.markdown(
        tarjeta_kpi(f"{MESES_ES[mes_ant[1]]} {mes_ant[0]} · mes anterior", n_anterior, "#6B7280"),
        unsafe_allow_html=True,
    )
    k2.markdown(
        tarjeta_kpi(
            f"{MESES_ES[hoy.month]} {hoy.year} · mes en curso", n_actual, COLOR_RECIBIDAS_MES,
            f"{'+' if delta >= 0 else ''}{delta} vs. mes anterior"
            + (f" · {n_mismo_mes_anio_pasado} en {hoy.year - 1}" if n_mismo_mes_anio_pasado else ""),
        ),
        unsafe_allow_html=True,
    )
    k3.markdown(
        tarjeta_kpi(f"Acumulado {hoy.year}", n_anio, COLOR_TOTAL, f"{len(df)} en todo el histórico"),
        unsafe_allow_html=True,
    )
    if descartados:
        st.caption(f"{descartados} registro(s) del archivo no tienen fecha interpretable y no se están contando.")

    st.write("")
    st.divider()

    # -------------------- Año a consultar --------------------
    anios = sorted(df["Anio"].unique(), reverse=True)
    idx_anio = anios.index(hoy.year) if hoy.year in anios else 0
    c_anio, c_info = st.columns([1, 3])
    anio_sel = int(c_anio.selectbox("Año", anios, index=idx_anio, key="hist_anio"))
    df_anio = df[df["Anio"] == anio_sel]
    c_info.markdown(
        f"<div style='padding-top:1.9rem; color:#6B7280; font-size:0.9rem;'>"
        f"{len(df_anio)} embarque(s) recibido(s) en {anio_sel}"
        f"{' · el histórico completo arranca en ' + str(min(anios)) if len(anios) > 1 else ''}</div>",
        unsafe_allow_html=True,
    )

    _grafico_anual(df_anio, anio_sel)

    # -------------------- Resumen mes por mes y por categoría --------------------
    resumen = pd.crosstab(df_anio["Mes"], df_anio["Categoria_Origen"])
    resumen = resumen.reindex(range(1, 13), fill_value=0)
    resumen.insert(len(resumen.columns), "Total", resumen.sum(axis=1))
    resumen.index = [MESES_ES[m] for m in range(1, 13)]
    resumen.index.name = "Mes"
    fila_total = pd.DataFrame([resumen.sum(axis=0)], index=[f"Total {anio_sel}"])
    tabla_resumen = pd.concat([resumen, fila_total])
    st.markdown(f"**Resumen {anio_sel} por mes y categoría**")
    st.dataframe(tabla_resumen, width="stretch")

    d1, d2 = st.columns(2)
    d1.download_button(
        f"Descargar resumen {anio_sel}",
        data=_df_a_excel(tabla_resumen.reset_index().rename(columns={"index": "Mes"}), f"Resumen {anio_sel}"),
        file_name=f"resumen_recibidos_{anio_sel}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_resumen_anio",
    )
    d2.download_button(
        "Descargar histórico completo",
        data=_df_a_excel(_tabla_detalle(df), "Historico"),
        file_name="historico_recibidos_completo.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_historico_total",
    )

    st.divider()

    # -------------------- Detalle de un mes concreto --------------------
    st.markdown("**Detalle mes por mes**")
    meses_con_datos = sorted(df_anio["Mes"].unique(), reverse=True)
    if not meses_con_datos:
        st.info(f"No hay embarques recibidos registrados en {anio_sel}.")
        return

    idx_mes = meses_con_datos.index(hoy.month) if (anio_sel == hoy.year and hoy.month in meses_con_datos) else 0
    m1, m2 = st.columns([1, 2])
    mes_sel = m1.selectbox(
        "Mes", meses_con_datos, index=idx_mes,
        format_func=lambda m: MESES_ES[m], key="hist_mes",
    )
    busqueda = m2.text_input("Buscar en el mes", key="hist_busca",
                             placeholder="BL, descripción o modelo…")

    filtrado = df_anio[df_anio["Mes"] == mes_sel]
    if busqueda and busqueda.strip():
        q = _norm(busqueda)
        filtrado = filtrado[filtrado.apply(
            lambda r: q in _norm(f"{r.get(COL_BL,'')} {r.get(COL_DESC,'')} {r.get(COL_MODELO,'')}"), axis=1
        )]

    etiqueta_mes = f"{MESES_ES[mes_sel]} {anio_sel}"
    st.markdown(
        tarjeta_kpi(f"Recibidos en {etiqueta_mes}", len(filtrado), COLOR_RECIBIDAS_MES),
        unsafe_allow_html=True,
    )
    st.write("")

    tabla = _tabla_detalle(filtrado)
    st.dataframe(tabla, width="stretch", hide_index=True)
    st.download_button(
        f"Descargar {etiqueta_mes} en Excel",
        data=_df_a_excel(tabla, etiqueta_mes),
        file_name=f"recibidos_{anio_sel}_{mes_sel:02d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_mes",
    )

    if rol != "admin" or filtrado.empty:
        return

    st.write("")
    with st.expander("Revertir una recepción", expanded=False):
        opciones = [f"{r[COL_BL]} · {str(r[COL_DESC])[:40]}" for _, r in filtrado.iterrows()]
        elegido = st.selectbox("Embarque", opciones, key="sel_revertir")
        fila = filtrado.iloc[opciones.index(elegido)]
        bl = str(fila[COL_BL]).strip()
        guardada = str(fila.get("Categoria_Origen", "")).strip()

        if guardada in CATEGORIAS:
            categoria = guardada
            st.caption(f"Se devolverá a la pestaña '{categoria}'.")
        else:
            st.caption("Este registro se archivó sin categoría de origen. Elige a dónde devolverlo:")
            categoria = st.selectbox("Categoría de destino", CATEGORIAS, key="cat_revertir")

        if st.button("Quitar de recibido y devolver", key="btn_revertir", type="primary"):
            ok, mensaje = quitar_de_recibido(bl, categoria_manual=categoria)
            if ok:
                registrar_log("Reversa de recibido", bl, categoria)
                invalidar_caches()
                st.rerun()
            else:
                st.error(mensaje)


def _tabla_detalle(df: pd.DataFrame) -> pd.DataFrame:
    """Vista legible del histórico, con la fecha ya formateada en español."""
    if df.empty:
        return pd.DataFrame(columns=["BL", "Descripción", "Modelo/Serie", "Cantidad",
                                     "Origen", "Fecha recibido", "Categoría", "Registrado por"])
    salida = pd.DataFrame({
        "BL": df.get(COL_BL, ""),
        "Descripción": df.get(COL_DESC, ""),
        "Modelo/Serie": df.get(COL_MODELO, ""),
        "Cantidad": df.get(COL_CANT, ""),
        "Origen": df.get(COL_PAIS, ""),
        "Fecha recibido": [f"{f.day:02d} {MESES_ES_CORTO[f.month]} {f.year}" for f in df["FechaParsed"]],
        "Categoría": df.get("Categoria_Origen", ""),
        "Registrado por": df.get("Registrado_Por", ""),
    })
    return salida.reset_index(drop=True)


def _df_a_excel(df: pd.DataFrame, hoja: str) -> bytes:
    buffer = io.BytesIO()
    nombre = "".join(c for c in hoja if c.isalnum() or c == " ")[:28] or "Datos"
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=nombre)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# HERRAMIENTAS DE ADMINISTRACIÓN
# ---------------------------------------------------------------------------
def herramientas(datos: dict):
    st.subheader("Herramientas")

    st.markdown("**Normalizar fechas de ETA**")
    st.caption(
        "Google Sheets interpreta las fechas según el locale del archivo, así que una celda escrita "
        "como 06/08/2026 puede quedar guardada como 6 de agosto o como 8 de junio, y quien la lea "
        "después no tiene forma de saber cuál era. Guardar el ETA como texto AAAA-MM-DD elimina el "
        "problema de raíz. Aquí puedes revisar y convertir lo que ya está cargado."
    )

    df = datos["activos"]
    if df.empty:
        st.info("No hay embarques cargados.")
        return

    pendientes = []
    for _, r in df.iterrows():
        diag = analizar_eta(r[COL_ETA])
        if diag["tipo"] in ("iso",):
            continue
        pendientes.append({
            "categoria": r["Categoria"],
            "fila": int(r["FilaSheet"]),
            "bl": str(r[COL_BL]).strip() or "(sin BL)",
            "descripcion": str(r[COL_DESC])[:40],
            **diag,
        })

    if not pendientes:
        st.success("Todos los ETA ya están en formato AAAA-MM-DD. No hay nada que normalizar.")
        return

    ambiguas = [p for p in pendientes if p["tipo"] == "ambigua"]
    convertibles = [p for p in pendientes if p["tipo"] == "convertible"]
    ilegibles = [p for p in pendientes if p["tipo"] == "ilegible"]

    if ilegibles:
        st.error(
            f"{len(ilegibles)} ETA no se pueden interpretar de ninguna forma y hay que corregirlos a mano "
            "en el Sheet: " + ", ".join(f"{p['bl']} (fila {p['fila']} de {p['categoria']}: '{p['crudo']}')"
                                        for p in ilegibles)
        )

    criterio = "Día/Mes/Año (convención dominicana)"
    if ambiguas:
        st.warning(
            f"{len(ambiguas)} fecha(s) admiten dos lecturas porque el día y el mes son ambos ≤ 12. "
            "Elige cómo se escribieron originalmente:"
        )
        criterio = st.radio(
            "Estas fechas se escribieron como:",
            ["Día/Mes/Año (convención dominicana)", "Mes/Día/Año (convención estadounidense)"],
            key="criterio_fechas",
        )
        usar_dm = criterio.startswith("Día")
        st.dataframe(
            pd.DataFrame([{
                "BL": p["bl"], "Categoría": p["categoria"], "Fila": p["fila"],
                "Valor actual": p["crudo"],
                "Se guardará como": (p["dm"] if usar_dm else p["md"]).isoformat(),
                "Otra lectura posible": (p["md"] if usar_dm else p["dm"]).isoformat(),
            } for p in ambiguas]),
            width="stretch", hide_index=True,
        )
    else:
        usar_dm = True

    if convertibles:
        st.info(f"{len(convertibles)} fecha(s) se entienden sin ambigüedad y se convertirán directamente.")
        st.dataframe(
            pd.DataFrame([{
                "BL": p["bl"], "Categoría": p["categoria"], "Fila": p["fila"],
                "Valor actual": p["crudo"], "Se guardará como": p["dm"].isoformat(),
            } for p in convertibles]),
            width="stretch", hide_index=True,
        )

    cambios = [(p["categoria"], p["fila"], (p["dm"] if usar_dm else p["md"]).isoformat()) for p in ambiguas]
    cambios += [(p["categoria"], p["fila"], p["dm"].isoformat()) for p in convertibles]

    if cambios and st.button(f"Convertir {len(cambios)} fecha(s) a AAAA-MM-DD", type="primary"):
        ok, mensaje = normalizar_etas(cambios)
        if ok:
            registrar_log("Normalización de fechas", "", "", f"{len(cambios)} celda(s); criterio={criterio}")
            invalidar_caches()
            st.success(mensaje)
            st.rerun()
        else:
            st.error(mensaje)

    st.divider()
    st.markdown("**Estructura del Google Sheet**")
    st.caption("Usa esto si creaste o renombraste una pestaña y la app todavía no la ve.")
    if st.button("Releer estructura del Sheet"):
        _refrescar_estructura()
        invalidar_caches()
        st.success("Estructura recargada.")
        st.rerun()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    restaurar_sesion()

    if "rol" not in st.session_state:
        login_screen()
        return

    with st.spinner("Leyendo los embarques…"):
        datos = cargar_todo()

    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    es_admin = st.session_state.rol == "admin"
    secciones = (["Dashboard", "Agregar", "Editar", "Carga masiva", "Histórico", "Herramientas"]
                 if es_admin else ["Dashboard", "Histórico"])

    # Navegación con estado propio en vez de st.tabs: además de no ejecutar el
    # cuerpo de todas las secciones en cada rerun, permite saltar por código
    # (el KPI de recibidas lleva al histórico; "Editar" se abre desde el dashboard).
    destino = st.session_state.pop("seccion", None)
    if destino in secciones:
        st.session_state["seccion_actual"] = destino
    if st.session_state.get("seccion_actual") not in secciones:
        st.session_state["seccion_actual"] = "Dashboard"

    with st.sidebar:
        st.markdown(f"**{st.session_state.get('usuario', 'Usuario')}**")
        st.caption("Administrador" if es_admin else "Solo visualización")
        st.write("")
        if st.button("Actualizar datos", width="stretch"):
            invalidar_caches()
            st.rerun()
        st.caption(f"Última lectura: {datos['hora'].strftime('%H:%M:%S')}")
        st.caption(f"Sesión recordada {VIDA_SESION_MINUTOS} min al recargar")
        st.write("")
        if st.button("Cerrar sesión", width="stretch"):
            cerrar_sesion()
            st.rerun()

    if len(secciones) > 1:
        # ancho="content": estirado a toda la pantalla, con dos o tres opciones,
        # parecía una barra de color suelta arriba de la página en vez de un menú.
        st.markdown('<div class="nav-rotulo">Sección</div>', unsafe_allow_html=True)
        seccion = selector_horizontal("Sección", secciones, key="seccion_actual", ancho="content")
    else:
        seccion = secciones[0]

    if seccion == "Dashboard":
        mostrar_dashboard(datos)
    elif seccion == "Agregar":
        form_alta_manual(datos)
    elif seccion == "Editar":
        form_editar(datos)
    elif seccion == "Carga masiva":
        form_carga_masiva(datos)
    elif seccion == "Histórico":
        mostrar_historico(datos, "admin" if es_admin else "viewer")
    elif seccion == "Herramientas":
        herramientas(datos)


if __name__ == "__main__":
    main()
