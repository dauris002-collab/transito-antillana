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
"""

from __future__ import annotations

import html
import io
import time
import unicodedata
from datetime import date, datetime, timedelta
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

REQUIRED_COLUMNS = [COL_BL, COL_DESC, COL_MODELO, COL_CANT, COL_PAIS, COL_ETA]
ALL_COLUMNS = REQUIRED_COLUMNS + [COL_DIAS_PUERTO, COL_ACTUALIZACION]

CATEGORIAS = ["Equipos", "Generadores", "Aéreos", "Carga Suelta", "Consolidados"]
RECIBIDO_SHEET = "Recibido (Mes)"
LOG_SHEET = "Log"

COLUMNAS_RECIBIDO = [
    COL_BL, COL_DESC, COL_MODELO, COL_CANT, COL_PAIS, COL_ETA,
    "Fecha_Recibido", "Categoria_Origen", "Registrado_Por",
]
COLUMNAS_LOG = ["Fecha_Hora", "Usuario", "Accion", "BL", "Categoria", "Detalle"]

UMBRAL_PROXIMO = 3          # días para considerar un embarque "Próximo a llegar"
CACHE_TTL = 45              # segundos de caché de lectura
MAX_INTENTOS_SESION = 5
MAX_FALLOS_GLOBAL = 25      # freno global: el bloqueo por sesión se evade en incógnito
VENTANA_FALLOS = 10 * 60
BLOQUEO_SEGUNDOS = 15 * 60
SEMANAS_HORIZONTE = 8

EST_TRANSITO = "En tránsito"
EST_PROXIMO = "Próximo a llegar"
EST_PUERTO = "En Puerto"
EST_SIN_FECHA = "Sin fecha válida"

STATUS_COLOR = {
    EST_TRANSITO: "#2E86DE",
    EST_PROXIMO: "#5C6BC0",
    EST_PUERTO: "#F0B90B",
    "Recibido": "#2E7D32",
    EST_SIN_FECHA: "#6B7280",
}
COLOR_TOTAL = "#17A2B8"
COLOR_RECIBIDAS_MES = "#2E7D32"
# Orden operativo: lo que exige acción primero.
STATUS_ORDER = [EST_PUERTO, EST_PROXIMO, EST_TRANSITO, EST_SIN_FECHA]
PRIORIDAD_ESTADO = {EST_PUERTO: 0, EST_PROXIMO: 1, EST_TRANSITO: 2, EST_SIN_FECHA: 3}
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
.block-container { padding-top: 2.2rem; }

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
.kpi-card { border-radius:14px; padding:14px 18px; min-height:86px; height:100%;
            display:flex; flex-direction:column; justify-content:center;
            box-shadow:0 2px 8px rgba(17,24,39,0.12); }
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


def esc(valor) -> str:
    """Escapa cualquier valor que venga del Sheet antes de meterlo en HTML."""
    texto = "" if valor is None else str(valor).strip()
    return html.escape(texto) if texto else "—"


# --- Fechas -----------------------------------------------------------------
EPOCH_SHEETS = date(1899, 12, 30)
FORMATOS_FECHA = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d")


def parsear_fecha(valor):
    """Convierte a date lo que sea que venga del Sheet o del Excel.
    Entiende: date/datetime, ISO, dd/mm/aaaa, dd-mm-aaaa, aaaa/mm/dd,
    '3 de enero de 2027', '03 enero 2027' y seriales numéricos de Sheets/Excel.
    Devuelve None si no puede. Interpreta dd/mm (convención RD), NUNCA mm/dd."""
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
    texto = texto.split(" 00:00:00")[0].strip()

    for fmt in FORMATOS_FECHA:
        try:
            return datetime.strptime(texto.split(" ")[0], fmt).date()
        except ValueError:
            continue

    # Serial numérico llegado como texto ("46181")
    limpio = texto.replace(",", "")
    if limpio.replace(".", "", 1).isdigit():
        try:
            return EPOCH_SHEETS + timedelta(days=int(float(limpio)))
        except (ValueError, OverflowError):
            pass

    # Mes en texto español: "03 enero 2027", "3 de enero de 2027", "3-ene-2027"
    partes = [p for p in texto.replace("-", " ").replace("/", " ").replace(",", " ").split() if p.lower() != "de"]
    if len(partes) == 3:
        dia_s, mes_s, anio_s = partes
        mes = MESES_NUM.get(_norm(mes_s)) or MESES_NUM.get(_norm(mes_s)[:3])
        if mes and dia_s.isdigit() and anio_s.isdigit():
            try:
                return date(int(anio_s), mes, int(dia_s))
            except ValueError:
                return None
    return None


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
    - 'ambigua': texto tipo 06/08/2026 donde día y mes son ambos <= 12, así que
      el valor guardado depende de cómo lo interprete quien lo lea.
    - 'convertible': se entiende sin ambigüedad pero no está en ISO.
    - 'ilegible': no se pudo interpretar de ninguna forma."""
    crudo = "" if valor is None else str(valor).strip()
    if not crudo:
        return {"tipo": "vacia", "crudo": crudo, "dm": None, "md": None}

    try:
        datetime.strptime(crudo, "%Y-%m-%d")
        return {"tipo": "iso", "crudo": crudo, "dm": parsear_fecha(crudo), "md": None}
    except ValueError:
        pass

    partes = crudo.replace("-", "/").replace(".", "/").split("/")
    if len(partes) == 3 and all(p.strip().isdigit() for p in partes):
        a, b, c = (int(p) for p in partes)
        if len(partes[2].strip()) == 4:  # dd/mm/aaaa o mm/dd/aaaa
            dm = md = None
            try:
                dm = date(c, b, a)
            except ValueError:
                pass
            try:
                md = date(c, a, b)
            except ValueError:
                pass
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


@st.cache_data(ttl=600, show_spinner=False)
def _headers(titulo_hoja: str) -> list:
    ws = get_worksheet(titulo_hoja)
    if ws is None:
        return []
    return ws.row_values(1)


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
    """Convierte la matriz cruda de una pestaña en DataFrame con nombres de
    columna canónicos (resolviendo acentos) y todas las columnas esperadas."""
    if not valores or not valores[0]:
        return pd.DataFrame(columns=columnas_canonicas)
    headers_reales = [str(h) for h in valores[0]]
    ancho = len(headers_reales)
    # Las filas pueden venir más cortas (celdas vacías al final) o más largas
    # (alguien escribió a la derecha del último encabezado): se ajustan al ancho.
    filas = [(list(f) + [""] * ancho)[:ancho] for f in valores[1:]]
    df = pd.DataFrame(filas, columns=headers_reales)

    mapa = {}
    for canon in columnas_canonicas:
        for real in df.columns:
            if _norm(real) == _norm(canon):
                mapa[real] = canon
                break
    df = df.rename(columns=mapa)
    df = df.loc[:, ~df.columns.duplicated()]
    for c in columnas_canonicas:
        if c not in df.columns:
            df[c] = ""
    return df


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
            "error": "El Google Sheet no tiene ninguna de las pestañas esperadas.",
        }

    rangos = [f"'{titulo}'!A1:Z5000" for _, titulo in objetivos]
    try:
        respuesta = ss.values_batch_get(rangos)
    except gspread.exceptions.APIError as e:
        return {
            "activos": pd.DataFrame(columns=ALL_COLUMNS + ["Categoria"]),
            "historico": pd.DataFrame(columns=COLUMNAS_RECIBIDO),
            "hora": ahora_rd(),
            "error": f"Google Sheets no respondió (posible límite de cuota): {e}",
        }

    bloques = respuesta.get("valueRanges", [])
    frames, historico = [], pd.DataFrame(columns=COLUMNAS_RECIBIDO)

    for (etiqueta, _titulo), bloque in zip(objetivos, bloques):
        valores = bloque.get("values", [])
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
    else:
        activos = pd.DataFrame(columns=ALL_COLUMNS + ["Categoria", "FilaSheet"])

    if not historico.empty:
        historico = historico[historico[COL_BL].astype(str).str.strip() != ""].reset_index(drop=True)

    return {"activos": activos, "historico": historico, "hora": ahora_rd(), "error": None}


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
    datos[COL_ACTUALIZACION] = hoy_rd().isoformat()
    headers = _headers(ws.title)
    ws.append_row(_fila_desde_dict(headers, datos), value_input_option="RAW")
    return True, ""


@_con_manejo_apierror
def append_rows_bulk(df: pd.DataFrame, categoria: str):
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}' en el Google Sheet."
    headers = _headers(ws.title)
    hoy = hoy_rd().isoformat()
    filas = []
    for _, r in df.iterrows():
        datos = {c: r.get(c, "") for c in REQUIRED_COLUMNS}
        datos[COL_ACTUALIZACION] = hoy
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

    headers = _headers(ws.title)
    actuales = ws.row_values(fila)
    actuales += [""] * (len(headers) - len(actuales))
    combinado = {h: actuales[i] for i, h in enumerate(headers)}
    combinado.update(datos)
    combinado[COL_ACTUALIZACION] = hoy_rd().isoformat()

    rango = f"{rowcol_to_a1(fila, 1)}:{rowcol_to_a1(fila, len(headers))}"
    ws.update(range_name=rango, values=[_fila_desde_dict(headers, combinado)], value_input_option="RAW")
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
def estado_embarque(eta_valor):
    """Devuelve (estado, dias_relativos). dias_relativos es el atraso en días si
    está En Puerto, los días que faltan si está Próximo a llegar, y None si no aplica."""
    eta = parsear_fecha(eta_valor)
    if eta is None:
        return EST_SIN_FECHA, None
    dias = (eta - hoy_rd()).days
    if dias < 0:
        return EST_PUERTO, abs(dias)
    if dias <= UMBRAL_PROXIMO:
        return EST_PROXIMO, dias
    return EST_TRANSITO, None


def texto_estado(estado: str, dias) -> str:
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
    """Agrega estado, fecha parseada y clave de orden operativo."""
    df = df.copy()
    if df.empty:
        for c in ("EstadoTexto", "DiasRel", "ETAFecha", "Prioridad", "OrdenSec"):
            df[c] = []
        return df

    calculado = [estado_embarque(v) for v in df[COL_ETA]]
    df["EstadoTexto"] = [c[0] for c in calculado]
    df["DiasRel"] = [c[1] for c in calculado]
    df["ETAFecha"] = [parsear_fecha(v) for v in df[COL_ETA]]
    df["Prioridad"] = df["EstadoTexto"].map(PRIORIDAD_ESTADO).fillna(9).astype(int)
    # Dentro de "En Puerto", primero el más atrasado; en el resto, el ETA más cercano.
    df["OrdenSec"] = [
        -(dias or 0) if estado == EST_PUERTO else (fecha.toordinal() if fecha else 10**9)
        for estado, dias, fecha in zip(df["EstadoTexto"], df["DiasRel"], df["ETAFecha"])
    ]
    return df.sort_values(["Prioridad", "OrdenSec"], kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------------------
# ACCESO
# ---------------------------------------------------------------------------
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
        '<div class="ant-sub">Acceso restringido. Ingresa tu PIN.</div>'
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
        pin = st.text_input("PIN", type="password", max_chars=8,
                            label_visibility="collapsed", placeholder="PIN")
        entrar = st.button("Entrar", type="primary", width="stretch")

    if not entrar:
        return

    rol, nombre = _resolver_pin(pin)
    if rol:
        st.session_state.rol = rol
        st.session_state.usuario = nombre
        st.session_state.intentos = 0
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
def encabezado(hora_datos: datetime):
    anio = hoy_rd().year
    sello = f"{hora_datos.day:02d} {MESES_ES_CORTO[hora_datos.month]} {hora_datos.year}, {hora_datos.strftime('%I:%M %p').lstrip('0').lower()}"
    st.markdown(
        f'<div class="ant-head">'
        f'<span class="ant-eyebrow">'
        f'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0C447C" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/>'
        f'<path d="M9 21v-6h6v6"/></svg> Logística e Importaciones {anio}</span>'
        f'<div class="ant-title">Estatus de Cargas</div>'
        f'<div class="ant-rule"></div>'
        f'<div class="ant-sub">Antillana Comercial</div>'
        f'<div class="ant-stamp"><span class="ant-dot"></span> Datos actualizados: {esc(sello)} (hora RD)</div>'
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

    vencidos = int((df["EstadoTexto"] == EST_PUERTO).sum())
    if vencidos:
        etiquetas.append("En puerto")
        valores.append(vencidos)
        colores.append(STATUS_COLOR[EST_PUERTO])

    con_fecha = df[df["ETAFecha"].notna() & (df["EstadoTexto"] != EST_PUERTO)]
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
    serie = df[COL_PAIS].replace("", "Sin especificar").value_counts().sort_values()
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
        if pais and pais != "Sin especificar":
            # Plotly conserva la selección entre reruns: sin esta firma, el clic se
            # reaplicaría en cada rerun y anularía cualquier cambio manual posterior.
            firma = f"{key}:{pais}"
            if firma != st.session_state.get(f"firma_pais_{key}"):
                st.session_state[f"pais_{key}"] = pais
                st.session_state[f"firma_pais_{key}"] = firma
                rerun_fragmento()


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
def mostrar_dashboard(datos: dict):
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    encabezado(datos["hora"])

    if datos["error"]:
        st.error(datos["error"])

    df = datos["activos"]
    if df.empty:
        st.info("Todavía no hay embarques cargados.")
        return

    recibidas_mes = contar_recibidas_mes(datos["historico"])
    pestanas = ["Todos"] + CATEGORIAS
    tabs = st.tabs(pestanas)
    for nombre, tab in zip(pestanas, tabs):
        with tab:
            sub = df if nombre == "Todos" else df[df["Categoria"] == nombre]
            _render_categoria(sub, st.session_state.get("rol", "viewer"), nombre, recibidas_mes)


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
def _render_categoria(df_bruto: pd.DataFrame, rol: str, tab_key: str, recibidas_mes: int):
    if df_bruto.empty:
        st.info("No hay embarques en esta categoría.")
        return

    df = enriquecer(df_bruto)
    conteo = df["EstadoTexto"].value_counts().to_dict()
    proximos_n = conteo.get(EST_PROXIMO, 0)
    en_puerto_n = conteo.get(EST_PUERTO, 0)
    sin_fecha_n = conteo.get(EST_SIN_FECHA, 0)

    # -------------------- KPIs --------------------
    kpis = [
        ("Total en tránsito", len(df), COLOR_TOTAL, "Todos", "total"),
        (f"Próximos {UMBRAL_PROXIMO} días", proximos_n, STATUS_COLOR[EST_PROXIMO], EST_PROXIMO, "proximos"),
        ("En puerto sin confirmar", en_puerto_n, STATUS_COLOR[EST_PUERTO], EST_PUERTO, "enpuerto"),
    ]
    # Los KPI son botones (para que sean clicables) maquillados como tarjetas.
    # El label lleva dos párrafos de markdown: rótulo arriba, número abajo.
    estilos = "".join(
        f".st-key-kpi_{tab_key}_{slug} button {{"
        f"background:{color} !important; color:#fff !important; border:none !important;"
        f"border-radius:14px !important; width:100% !important; min-height:86px !important;"
        f"text-align:left !important; padding:14px 18px !important;"
        f"box-shadow:0 2px 8px rgba(17,24,39,0.12) !important; transition:filter .15s ease;}} "
        f".st-key-kpi_{tab_key}_{slug} button > div {{"
        f"display:flex !important; flex-direction:column !important; align-items:flex-start !important;"
        f"justify-content:center !important; width:100% !important;}} "
        f".st-key-kpi_{tab_key}_{slug} button p {{margin:0 !important; color:#fff !important;}} "
        f".st-key-kpi_{tab_key}_{slug} button p:first-of-type {{"
        f"font-size:0.70rem !important; font-weight:700 !important; letter-spacing:0.06em !important;"
        f"opacity:0.92 !important;}} "
        f".st-key-kpi_{tab_key}_{slug} button p:last-of-type {{"
        f"font-size:2.0rem !important; font-weight:800 !important; line-height:1.05 !important;"
        f"margin-top:6px !important;}} "
        f".st-key-kpi_{tab_key}_{slug} button:hover {{filter:brightness(0.93); color:#fff !important;}} "
        f".st-key-kpi_{tab_key}_{slug} button:focus {{color:#fff !important; box-shadow:0 0 0 3px rgba(17,24,39,0.15) !important;}}"
        for _, _, color, _, slug in kpis
    )
    st.markdown(f"<style>{estilos}</style>", unsafe_allow_html=True)

    cols = st.columns(4)
    for col, (label, valor, color, filtro, slug) in zip(cols, kpis):
        with col:
            with st.container(key=f"kpi_{tab_key}_{slug}"):
                if st.button(f"{label.upper()}\n\n{valor}", key=f"btn_{tab_key}_{slug}",
                             width="stretch"):
                    st.session_state[f"estado_{tab_key}"] = filtro
                    rerun_fragmento()
    with cols[3]:
        st.markdown(
            tarjeta_kpi(f"Recibidas en {MESES_ES[hoy_rd().month]}", recibidas_mes, COLOR_RECIBIDAS_MES),
            unsafe_allow_html=True,
        )

    if sin_fecha_n:
        st.warning(
            f"{sin_fecha_n} embarque(s) tienen un ETA que la app no puede interpretar y quedan fuera de "
            "los conteos por fecha. Revísalos en Herramientas → Normalizar fechas."
        )

    st.write("")

    # -------------------- GRÁFICOS --------------------
    g1, g2 = st.columns([1.5, 1])
    with g1:
        st.caption("Llegadas previstas")
        grafico_linea_tiempo(df, tab_key)
    with g2:
        st.caption("Origen")
        grafico_paises(df, tab_key)

    st.divider()

    # -------------------- FILTROS --------------------
    paises = ["Todos"] + sorted({p for p in df[COL_PAIS] if str(p).strip()})
    estados = ["Todos"] + [e for e in STATUS_ORDER]

    f1, f2, f3, f4 = st.columns([1.6, 1.1, 1.1, 0.6])
    busqueda = f1.text_input("Buscar", key=f"busca_{tab_key}",
                             placeholder="BL, descripción o modelo…", label_visibility="collapsed")
    pais_sel = f2.selectbox("País", paises, key=f"pais_{tab_key}", label_visibility="collapsed")
    estado_sel = f3.selectbox("Estado", estados, key=f"estado_{tab_key}", label_visibility="collapsed")
    if f4.button("Limpiar", key=f"limpiar_{tab_key}", width="stretch"):
        for k in (f"busca_{tab_key}", f"pais_{tab_key}", f"estado_{tab_key}"):
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
        mascara = filtrado.apply(
            lambda r: q in _norm(f"{r[COL_BL]} {r[COL_DESC]} {r[COL_MODELO]}"), axis=1
        )
        filtrado = filtrado[mascara]

    st.caption(f"Mostrando {len(filtrado)} de {len(df)} embarque(s) · ordenados por urgencia")
    render_lista(filtrado)

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

    opciones = [
        f"{r[COL_BL]} · {str(r[COL_DESC])[:38] or 'sin descripción'} · {r['Categoria']}"
        for _, r in con_bl.iterrows()
    ]
    elegido = st.selectbox("Embarque", opciones, key=f"sel_accion_{tab_key}")
    fila = con_bl.iloc[opciones.index(elegido)]
    bl, categoria = str(fila[COL_BL]).strip(), fila["Categoria"]

    c1, c2, c3 = st.columns(3)
    if c1.button("Marcar como recibido", key=f"rec_{tab_key}", type="primary", width="stretch"):
        ok, mensaje = marcar_como_recibido(bl, categoria)
        if ok:
            registrar_log("Recibido", bl, categoria, f"ETA {fila[COL_ETA]}")
            invalidar_caches()
            st.rerun()
        else:
            st.error(mensaje)

    if c2.button("Editar", key=f"edit_{tab_key}", width="stretch"):
        st.session_state["editar_bl"] = bl
        st.session_state["editar_categoria"] = categoria
        st.session_state["ir_a_editar"] = True
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
        enviado = st.form_submit_button("Guardar embarque", type="primary")

    if not enviado:
        return

    if not bl.strip() or not descripcion.strip():
        st.error("BL y Descripción son obligatorios.")
        return
    if bl.strip() in _bls_existentes(datos):
        st.error(f"Ya existe un embarque con el BL '{bl.strip()}' (activo o en el histórico).")
        return

    ok, mensaje = append_row(
        {
            COL_BL: bl.strip(),
            COL_DESC: descripcion.strip(),
            COL_MODELO: modelo.strip(),
            COL_CANT: cantidad.strip(),
            COL_PAIS: pais.strip(),
            COL_ETA: eta.isoformat(),
        },
        categoria,
    )
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

    with st.form("form_editar"):
        c1, c2 = st.columns(2)
        descripcion = c1.text_input("Descripción", value=str(fila[COL_DESC]))
        modelo = c2.text_input("Modelo o serie", value=str(fila[COL_MODELO]))
        c3, c4 = st.columns(2)
        cantidad = c3.text_input("Cantidad", value=str(fila[COL_CANT]))
        pais = c4.text_input("País de origen", value=str(fila[COL_PAIS]))
        eta = st.date_input("Llegada a puerto (ETA)", value=eta_actual, format="DD/MM/YYYY")
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
        }],
        columns=REQUIRED_COLUMNS,
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
               ". El ETA puede venir en cualquier formato reconocible; se guarda como AAAA-MM-DD.")

    archivo = st.file_uploader("Archivo .xlsx", type=["xlsx"])
    if archivo is None:
        return

    try:
        nuevo = pd.read_excel(archivo, dtype=str)
    except Exception as e:
        st.error(f"No se pudo leer el archivo: {e}")
        return

    mapa = {}
    for canon in REQUIRED_COLUMNS:
        for real in nuevo.columns:
            if _norm(real) == _norm(canon):
                mapa[real] = canon
                break
    nuevo = nuevo.rename(columns=mapa)

    faltantes = [c for c in REQUIRED_COLUMNS if c not in nuevo.columns]
    if faltantes:
        st.error(f"Faltan columnas obligatorias: {', '.join(faltantes)}. Usa la plantilla.")
        return

    nuevo = nuevo[REQUIRED_COLUMNS].dropna(how="all").copy()
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
def mostrar_historico(datos: dict, rol: str):
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.subheader("Histórico de embarques recibidos")

    df = datos["historico"].copy()
    if df.empty:
        st.info("Todavía no hay embarques archivados como recibidos.")
        return

    df["FechaParsed"] = [parsear_fecha(v) for v in df["Fecha_Recibido"]]
    sin_fecha = int(df["FechaParsed"].isna().sum())
    df = df[df["FechaParsed"].notna()].copy()
    if sin_fecha:
        st.caption(f"{sin_fecha} registro(s) del histórico no tienen fecha interpretable y no se están contando.")
    if df.empty:
        st.info("No hay registros con fecha válida en el histórico.")
        return

    df["MesKey"] = [(f.year, f.month) for f in df["FechaParsed"]]
    hoy = hoy_rd()
    key_actual = (hoy.year, hoy.month)
    key_anterior = (hoy.year - 1, 12) if hoy.month == 1 else (hoy.year, hoy.month - 1)
    n_actual = int((df["MesKey"] == key_actual).sum())
    n_anterior = int((df["MesKey"] == key_anterior).sum())
    delta = n_actual - n_anterior

    c1, c2 = st.columns(2)
    c1.markdown(tarjeta_kpi(f"{MESES_ES[key_anterior[1]]} {key_anterior[0]} (mes anterior)",
                            n_anterior, "#6B7280"), unsafe_allow_html=True)
    c2.markdown(tarjeta_kpi(f"{MESES_ES[key_actual[1]]} {key_actual[0]} (mes en curso)",
                            n_actual, COLOR_RECIBIDAS_MES,
                            f"{'+' if delta >= 0 else ''}{delta} vs. mes anterior"), unsafe_allow_html=True)
    st.write("")

    conteo_mes = df.groupby("MesKey").size().sort_index()
    ultimos = list(conteo_mes.items())[-12:]
    if len(ultimos) > 1:
        fig = go.Figure(data=[go.Bar(
            x=[f"{MESES_ES_CORTO[m]} {str(y)[2:]}" for (y, m), _ in ultimos],
            y=[v for _, v in ultimos],
            marker=dict(color=COLOR_RECIBIDAS_MES),
            text=[v for _, v in ultimos], textposition="outside",
            hovertemplate="%{x}: %{y} recibido(s)<extra></extra>",
        )])
        fig.update_layout(margin=dict(t=18, b=10, l=10, r=10), height=230,
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          font=dict(color="#374151", size=11), showlegend=False,
                          xaxis=dict(showgrid=False), yaxis=dict(showgrid=True, gridcolor="#F3F4F6",
                                                                 showticklabels=False))
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key="hist_meses")

    st.divider()

    meses = sorted(df["MesKey"].unique(), reverse=True)
    etiquetas = [f"{MESES_ES[m]} {y}" for (y, m) in meses]
    seleccion = st.selectbox("Consultar un mes", etiquetas, key="historico_mes")
    y_sel, m_sel = meses[etiquetas.index(seleccion)]
    filtrado = df[df["MesKey"] == (y_sel, m_sel)].sort_values("FechaParsed")

    columnas = [c for c in (COL_BL, COL_DESC, COL_MODELO, COL_CANT, COL_PAIS,
                            "Fecha_Recibido", "Categoria_Origen", "Registrado_Por") if c in filtrado.columns]
    tabla = filtrado[columnas].rename(columns={
        COL_DESC: "Descripción", COL_MODELO: "Modelo/Serie", COL_PAIS: "Origen",
        "Fecha_Recibido": "Fecha recibido", "Categoria_Origen": "Categoría", "Registrado_Por": "Registrado por",
    })
    st.dataframe(tabla, width="stretch", hide_index=True)

    st.download_button(
        f"Descargar {seleccion} en Excel",
        data=_df_a_excel(tabla, seleccion),
        file_name=f"recibidos_{y_sel}_{m_sel:02d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
    if "rol" not in st.session_state:
        login_screen()
        return

    datos = cargar_todo()

    with st.sidebar:
        st.markdown(f"**{st.session_state.get('usuario', 'Usuario')}**")
        st.caption("Administrador" if st.session_state.rol == "admin" else "Solo visualización")
        st.write("")
        if st.button("Actualizar datos", width="stretch"):
            invalidar_caches()
            st.rerun()
        st.caption(f"Última lectura: {datos['hora'].strftime('%H:%M:%S')}")
        st.write("")
        if st.button("Cerrar sesión", width="stretch"):
            st.session_state.clear()
            st.rerun()

    if st.session_state.rol == "admin":
        etiquetas = ["Dashboard", "Agregar", "Editar", "Carga masiva", "Histórico", "Herramientas"]
        if st.session_state.pop("ir_a_editar", False):
            st.info("Ve a la pestaña **Editar** para modificar el embarque seleccionado.")
        t1, t2, t3, t4, t5, t6 = st.tabs(etiquetas)
        with t1:
            mostrar_dashboard(datos)
        with t2:
            form_alta_manual(datos)
        with t3:
            form_editar(datos)
        with t4:
            form_carga_masiva(datos)
        with t5:
            mostrar_historico(datos, "admin")
        with t6:
            herramientas(datos)
    else:
        t1, t2 = st.tabs(["Dashboard", "Histórico"])
        with t1:
            mostrar_dashboard(datos)
        with t2:
            mostrar_historico(datos, "viewer")


if __name__ == "__main__":
    main()
