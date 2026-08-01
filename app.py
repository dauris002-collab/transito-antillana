import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import gspread
import gspread.exceptions
from google.oauth2.service_account import Credentials

ZONA_RD = ZoneInfo("America/Santo_Domingo")


def hoy_rd() -> date:
    """Fecha de HOY en hora de Santo Domingo (UTC-4), no la del servidor.
    Streamlit Cloud corre en UTC — usar date.today() directo hace que, en las
    últimas horas de cada día (y sobre todo el último día del mes), el
    servidor ya 'crea' que es el día/mes siguiente aunque en RD no lo sea."""
    return datetime.now(ZONA_RD).date()

# ---------------------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Antillana · Embarques en Tránsito",
    page_icon="🚢",
    layout="wide",
)

COL_ETA = "Llegada a Puerto (ETA)"
COL_DIAS_PUERTO = "Dias en puerto"

REQUIRED_COLUMNS = ["BL", "Descripcion", "Modelo_Serie", "Cantidad", "Pais_Origen", COL_ETA]
ALL_COLUMNS = REQUIRED_COLUMNS + [COL_DIAS_PUERTO, "Fecha_Actualizacion"]

# Cada categoría es una PESTAÑA distinta dentro del mismo Google Sheet.
# El nombre de la pestaña debe coincidir exactamente (tal cual, con tilde donde aplique).
# "En Puerto" y "Recibido (por mes)" NO son categorías de producto — son estados/histórico
# que la app calcula y archiva automáticamente, no pestañas donde se cargan embarques nuevos.
CATEGORIAS = ["Equipos", "Generadores", "Aéreos", "Carga Suelta", "Consolidados"]
RECIBIDO_SHEET = "Recibido (Mes)"

# Paleta alineada al reporte de Power BI: bloques de color sólido, planos, alto contraste.
STATUS_COLOR = {
    "En tránsito": "#2E86DE",       # azul (igual al KPI "Total")
    "Próximo a llegar": "#5C6BC0",  # morado
    "En Puerto": "#F0B90B",         # amarillo — llegó pero falta confirmar recibido
    "Recibido": "#2E7D32",          # verde — solo para consulta histórica
    "Sin fecha válida": "#6b7280",
}
COLOR_TOTAL = "#17A2B8"  # teal, distintivo para el total general
COLOR_RECIBIDAS_MES = "#2E7D32"
STATUS_ORDER = ["Próximo a llegar", "En Puerto", "En tránsito", "Sin fecha válida"]
PALETA_PAISES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

CUSTOM_CSS = """
<style>
.kpi-card {
    border-radius: 14px;
    padding: 18px 20px;
    height: 100%;
    box-shadow: 0 2px 8px rgba(17, 24, 39, 0.12);
}
.kpi-label {
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: rgba(255,255,255,0.9);
    margin-bottom: 6px;
}
.kpi-value { font-size: 2.1rem; font-weight: 800; line-height: 1; color: #ffffff; }
.kpi-sub { font-size: 0.72rem; color: rgba(255,255,255,0.9); margin-top: 6px; }

.ship-card {
    border-radius: 12px;
    padding: 14px 20px;
    margin-bottom: 12px;
    background: #ffffff;
    border: 1px solid #E5E7EB;
    border-left: 5px solid #6b7280;
    box-shadow: 0 1px 4px rgba(17, 24, 39, 0.06);
}
.ship-top { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
.ship-bl { font-size: 1.02rem; font-weight: 700; color: #111827; }
.ship-desc { font-size: 0.85rem; color: #6B7280; }
.status-badge {
    display: inline-block;
    padding: 3px 12px;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 700;
    color: #ffffff;
}
.ship-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 10px;
    margin-top: 10px;
}
.ship-field-label { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.04em; color: #9CA3AF; }
.ship-field-value { font-size: 0.92rem; font-weight: 600; color: #1F2937; }

.tbl-wrap {
    border: 1px solid #E5E7EB;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(17, 24, 39, 0.06);
}
.tbl-header {
    display: grid;
    grid-template-columns: 1.2fr 1.4fr 1.3fr 0.7fr 0.9fr 0.9fr 1.1fr;
    padding: 10px 18px;
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #9CA3AF;
    background: #F9FAFB;
    border-bottom: 1px solid #E5E7EB;
}
.tbl-row {
    display: grid;
    grid-template-columns: 1.2fr 1.4fr 1.3fr 0.7fr 0.9fr 0.9fr 1.1fr;
    padding: 12px 18px;
    font-size: 0.88rem;
    align-items: center;
    background: #ffffff;
    border-bottom: 1px solid #F3F4F6;
    border-left: 4px solid #6b7280;
}
.tbl-bl { font-weight: 700; color: #111827; }
.tbl-desc { color: #6B7280; }

/* Por defecto (desktop/tablet ancho): tabla visible, tarjetas ocultas */
.ship-cards { display: none; }

/* Celular / viewport angosto: tabla oculta, tarjetas visibles */
@media (max-width: 640px) {
    .tbl-wrap { display: none; }
    .ship-cards { display: block; }
    h1 { font-size: 1.7rem !important; }
    .kpi-value { font-size: 1.6rem; }
}
</style>
"""

MAX_INTENTOS = 5
BLOQUEO_SEGUNDOS = 15 * 60  # 15 minutos


# ---------------------------------------------------------------------------
# CONEXIÓN A GOOGLE SHEETS (una pestaña por categoría)
# ---------------------------------------------------------------------------
@st.cache_resource
def get_spreadsheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=scopes
    )
    client = gspread.authorize(creds)
    return client.open_by_key(st.secrets["SHEET_ID"])


def get_worksheet(categoria: str):
    """Devuelve la pestaña (hoja) correspondiente a esa categoría, o None si no existe todavía.
    Primero intenta el nombre exacto; si falla, busca tolerando espacios extra y
    mayúsculas/minúsculas distintas, para no depender de que el nombre de la
    pestaña en Google Sheets coincida carácter por carácter con el código."""
    ss = get_spreadsheet()
    try:
        return ss.worksheet(categoria)
    except gspread.exceptions.WorksheetNotFound:
        pass
    objetivo = " ".join(categoria.split()).strip().casefold()
    for hoja in ss.worksheets():
        if " ".join(hoja.title.split()).strip().casefold() == objetivo:
            return hoja
    return None


@st.cache_data(ttl=20, show_spinner=False)
def load_data() -> pd.DataFrame:
    """Lee las 5 pestañas de categoría y las combina en una sola tabla,
    agregando la columna Categoria según de qué pestaña vino cada fila.
    Si una pestaña falla al leer (error de la API de Google), se salta esa
    pestaña con una advertencia en vez de tumbar todo el dashboard."""
    frames = []
    for categoria in CATEGORIAS:
        ws = get_worksheet(categoria)
        if ws is None:
            continue
        try:
            records = ws.get_all_records()
        except gspread.exceptions.APIError as e:
            st.warning(f"⚠️ No se pudo leer la pestaña '{categoria}' en este momento ({e}). Se omitió temporalmente.")
            continue
        df_cat = pd.DataFrame(records)
        if df_cat.empty:
            df_cat = pd.DataFrame(columns=ALL_COLUMNS)
        for col in ALL_COLUMNS:
            if col not in df_cat.columns:
                df_cat[col] = ""
        df_cat["Categoria"] = categoria
        # Descarta solo filas realmente vacías (ni BL ni Descripción): notas
        # sueltas escritas en alguna celda, o filas en blanco que Google Sheets
        # sigue devolviendo dentro del rango usado. Una fila con Descripción
        # pero sin BL todavía cuenta como carga real (BL pendiente de asignar).
        df_cat["BL"] = df_cat["BL"].astype(str).str.strip()
        df_cat["Descripcion"] = df_cat["Descripcion"].astype(str).str.strip()
        df_cat = df_cat[(df_cat["BL"] != "") | (df_cat["Descripcion"] != "")]
        frames.append(df_cat)
    if not frames:
        return pd.DataFrame(columns=ALL_COLUMNS + ["Categoria"])
    resultado = pd.concat(frames, ignore_index=True)
    # Filas con BL primero que las que todavía no tienen BL asignado.
    return resultado.sort_values(
        by="BL", key=lambda s: s.eq(""), kind="stable"
    ).reset_index(drop=True)


def _headers(sheet):
    return sheet.row_values(1)


def _fila_desde_dict(sheet, row: dict):
    """Arma la fila a escribir respetando el orden REAL de columnas de esa pestaña,
    no el orden asumido en el código (evita escribir en la columna equivocada
    si el usuario agregó columnas en otro orden)."""
    headers = _headers(sheet)
    return [row.get(h, "") for h in headers]


def append_row(row: dict, categoria: str) -> bool:
    ws = get_worksheet(categoria)
    if ws is None:
        return False
    row["Fecha_Actualizacion"] = hoy_rd().isoformat()
    ordered = _fila_desde_dict(ws, row)
    ws.append_row(ordered, value_input_option="RAW")
    return True


def append_rows_bulk(df: pd.DataFrame, categoria: str) -> bool:
    ws = get_worksheet(categoria)
    if ws is None:
        return False
    hoy = hoy_rd().isoformat()
    rows = []
    for _, r in df.iterrows():
        row = {c: r.get(c, "") for c in REQUIRED_COLUMNS}
        row["Fecha_Actualizacion"] = hoy
        rows.append(_fila_desde_dict(ws, row))
    ws.append_rows(rows, value_input_option="RAW")
    return True


def _fila_por_bl(bl: str, categoria: str):
    """Devuelve el número de fila (1-indexado, con encabezado) del BL dado
    dentro de la pestaña de esa categoría, o None si no existe."""
    ws = get_worksheet(categoria)
    if ws is None:
        return None
    headers = _headers(ws)
    if "BL" not in headers:
        return None
    col_bl = headers.index("BL") + 1
    try:
        cell = ws.find(str(bl).strip(), in_column=col_bl)
    except gspread.exceptions.CellNotFound:
        return None
    return cell.row


def eliminar_embarque(bl: str, categoria: str) -> bool:
    ws = get_worksheet(categoria)
    fila = _fila_por_bl(bl, categoria)
    if ws is None or fila is None:
        return False
    ws.delete_rows(fila)
    return True


@st.cache_data(ttl=20, show_spinner=False)
def cargar_historico_recibidos() -> pd.DataFrame:
    """Trae todo el histórico de la pestaña 'Recibido (por mes)', con la fecha
    ya interpretada como objeto date para poder agrupar por mes calendario."""
    ws = get_worksheet(RECIBIDO_SHEET)
    columnas = ["BL", "Descripcion", "Cantidad", "Fecha_Recibido"]
    if ws is None:
        return pd.DataFrame(columns=columnas)
    registros = ws.get_all_records()
    df = pd.DataFrame(registros)
    if df.empty:
        return pd.DataFrame(columns=columnas)
    for c in columnas:
        if c not in df.columns:
            df[c] = ""

    def _parsear(fecha_str):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(str(fecha_str).strip().split(" ")[0], fmt).date()
            except ValueError:
                continue
        return None

    df["FechaParsed"] = df["Fecha_Recibido"].apply(_parsear)
    return df[df["FechaParsed"].notna()].reset_index(drop=True)


MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}


def mostrar_historico():
    st.subheader("📜 Histórico de embarques recibidos")
    df = cargar_historico_recibidos()
    if df.empty:
        st.info("Todavía no hay embarques archivados como recibidos.")
        return

    df["MesKey"] = df["FechaParsed"].apply(lambda d: (d.year, d.month))
    meses_disponibles = sorted(df["MesKey"].unique(), reverse=True)
    opciones = [f"{MESES_ES[m]} {y}" for (y, m) in meses_disponibles]
    seleccion = st.selectbox("Mes", opciones, key="historico_mes")
    y_sel, m_sel = meses_disponibles[opciones.index(seleccion)]

    filtrado = df[df["MesKey"] == (y_sel, m_sel)].sort_values("FechaParsed")

    st.markdown(
        f'<div class="kpi-card" style="background:{COLOR_RECIBIDAS_MES}; max-width:280px;">'
        f'<div class="kpi-label">Recibidas en {seleccion}</div>'
        f'<div class="kpi-value">{len(filtrado)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.write("")

    tabla = filtrado[["BL", "Descripcion", "Cantidad", "Fecha_Recibido"]].rename(
        columns={"Fecha_Recibido": "Fecha recibido"}
    )
    st.dataframe(tabla, use_container_width=True, hide_index=True)


def _asegurar_columna(ws, nombre: str):
    """Si la pestaña no tiene una columna con ese encabezado exacto, la agrega
    al final de la fila 1. Evita depender de que alguien la haya creado a mano
    en el Sheet — la app se autorepara."""
    headers = _headers(ws)
    if nombre in headers:
        return headers
    ws.update_cell(1, len(headers) + 1, nombre)
    return headers + [nombre]


def marcar_como_recibido(bl: str, categoria: str):
    """Devuelve (exito: bool, mensaje_error: str). El mensaje solo importa
    cuando exito es False — se muestra en pantalla en vez de fallar en silencio."""
    ws_origen = get_worksheet(categoria)
    if ws_origen is None:
        return False, f"No se encontró la pestaña '{categoria}' en el Google Sheet."

    fila = _fila_por_bl(bl, categoria)
    if fila is None:
        return False, f"No se encontró el BL '{bl}' en la pestaña '{categoria}' — puede que ya se haya movido o editado."

    headers_origen = _headers(ws_origen)
    valores_fila = ws_origen.row_values(fila)
    datos = dict(zip(headers_origen, valores_fila))

    # "El mes que llegó" es la fecha de Llegada a Puerto (ETA) de ese embarque
    # específico, no el día en que un admin presiona el botón (que puede ser
    # días o semanas después de la llegada real).
    fecha_llegada = None
    eta_raw = str(datos.get(COL_ETA, "")).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            fecha_llegada = datetime.strptime(eta_raw.split(" ")[0], fmt).date()
            break
        except ValueError:
            continue

    if fecha_llegada is None:
        return False, (
            f"El BL '{bl}' no tiene una fecha de Llegada a Puerto (ETA) válida "
            f"('{eta_raw or 'vacía'}'), así que no se puede archivar en el histórico mensual "
            "sin saber a qué mes pertenece. Corrige la fecha en el Sheet y vuelve a intentar."
        )

    ws_destino = get_worksheet(RECIBIDO_SHEET)
    if ws_destino is None:
        ss = get_spreadsheet()
        nombres_reales = ", ".join(f"'{h.title}'" for h in ss.worksheets())
        return False, (
            f"No se encontró la pestaña '{RECIBIDO_SHEET}' en el Google Sheet. "
            f"Las pestañas que la app sí ve son: {nombres_reales}."
        )
    _asegurar_columna(ws_destino, "Fecha_Recibido")

    registro = {
        "BL": datos.get("BL", bl),
        "Descripcion": datos.get("Descripcion", ""),
        "Cantidad": datos.get("Cantidad", ""),
        "Fecha_Recibido": fecha_llegada.isoformat(),
    }
    fila_destino = _fila_desde_dict(ws_destino, registro)
    ws_destino.append_row(fila_destino, value_input_option="RAW")

    ws_origen.delete_rows(fila)
    return True, ""


@st.cache_data(ttl=20, show_spinner=False)
def contar_recibidas_mes_actual() -> int:
    """Cuenta cuántos embarques se archivaron como recibidos en el mes calendario actual,
    leyendo la fecha de la columna 'Fecha_Recibido' de la pestaña 'Recibido (por mes)'."""
    ws = get_worksheet(RECIBIDO_SHEET)
    if ws is None:
        return 0
    hoy = hoy_rd()
    total = 0
    for registro in ws.get_all_records():
        fecha_str = str(registro.get("Fecha_Recibido", "")).strip()
        fecha = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                fecha = datetime.strptime(fecha_str.split(" ")[0], fmt).date()
                break
            except ValueError:
                continue
        if fecha and fecha.year == hoy.year and fecha.month == hoy.month:
            total += 1
    return total


# ---------------------------------------------------------------------------
# LÓGICA DE ESTADO DEL EMBARQUE
# ---------------------------------------------------------------------------
def estado_embarque(eta_str: str):
    """Devuelve (texto_estado, icono, dias_en_puerto). dias_en_puerto es None
    salvo cuando el estado es 'En Puerto' (ETA ya pasó y nadie lo ha confirmado
    como recibido)."""
    eta = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            eta = datetime.strptime(str(eta_str).strip().split(" ")[0], fmt).date()
            break
        except ValueError:
            continue
    if eta is None:
        return "Sin fecha válida", "⚪", None

    hoy = hoy_rd()
    dias_restantes = (eta - hoy).days

    if dias_restantes < 0:
        return "En Puerto", "🟠", abs(dias_restantes)
    elif dias_restantes <= 3:
        return "Próximo a llegar", "🟡", dias_restantes
    else:
        return "En tránsito", "🔵", None


def texto_badge_estado(fila) -> str:
    """Arma el texto del badge de estado, incluyendo el contador de días
    para 'Próximo a llegar' y 'En Puerto' (los dos estados con urgencia)."""
    icono, estado, dias = fila["EstadoIcono"], fila["EstadoTexto"], fila["DiasEnPuerto"]
    if estado == "En Puerto" and pd.notna(dias):
        d = int(dias)
        return f'{icono} En Puerto hace {d} día{"s" if d != 1 else ""}'
    if estado == "Próximo a llegar" and pd.notna(dias):
        d = int(dias)
        if d == 0:
            return f'{icono} Próximo a llegar (hoy)'
        return f'{icono} Próximo a llegar en {d} día{"s" if d != 1 else ""}'
    return f'{icono} {estado}'


# ---------------------------------------------------------------------------
# LOGIN CON PIN
# ---------------------------------------------------------------------------
def login_screen():
    st.markdown(
        '<div style="text-align:center; margin-top:3rem;">'
        '<div style="font-size:3rem;">🚢</div>'
        '<div style="font-size:1.8rem; font-weight:800; margin-top:0.3rem;">Antillana Comercial · Cargas en Tránsito</div>'
        '<div style="font-size:0.95rem; color:#6B7280; margin-top:0.4rem;">Acceso restringido. Ingresa tu PIN de 4 dígitos.</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.write("")

    if "intentos" not in st.session_state:
        st.session_state.intentos = 0
    if "bloqueado_hasta" not in st.session_state:
        st.session_state.bloqueado_hasta = 0

    ahora = time.time()
    _, col_centro, _ = st.columns([1, 1.2, 1])

    if ahora < st.session_state.bloqueado_hasta:
        restante = int(st.session_state.bloqueado_hasta - ahora)
        with col_centro:
            st.error(f"Demasiados intentos fallidos. Intenta de nuevo en {restante // 60} min {restante % 60} seg.")
        return

    with col_centro:
        pin = st.text_input("PIN", type="password", max_chars=4, label_visibility="collapsed", placeholder="PIN")
        entrar = st.button("Entrar", type="primary", use_container_width=True)

    if entrar:
        if pin == st.secrets.get("ADMIN_PIN", ""):
            st.session_state.rol = "admin"
            st.session_state.intentos = 0
            st.rerun()
        elif pin == st.secrets.get("VIEWER_PIN", ""):
            st.session_state.rol = "viewer"
            st.session_state.intentos = 0
            st.rerun()
        else:
            st.session_state.intentos += 1
            restantes = MAX_INTENTOS - st.session_state.intentos
            if restantes <= 0:
                st.session_state.bloqueado_hasta = time.time() + BLOQUEO_SEGUNDOS
                st.session_state.intentos = 0
                with col_centro:
                    st.error("PIN incorrecto. Acceso bloqueado por 15 minutos.")
            else:
                with col_centro:
                    st.error(f"PIN incorrecto. Te quedan {restantes} intento(s).")


# ---------------------------------------------------------------------------
# DASHBOARD (VISTA PRINCIPAL)
# ---------------------------------------------------------------------------
def mostrar_dashboard(df: pd.DataFrame):
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.markdown(
        '<div style="text-align:center; margin:0.5rem 0 1.5rem 0;">'
        '<span style="display:inline-flex; align-items:center; gap:6px; background:#E6F1FB; color:#0C447C; '
        'font-size:0.8rem; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; '
        'padding:5px 16px; border-radius:999px;">'
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#0C447C" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/>'
        '<path d="M9 21v-6h6v6"/></svg> Logística e Importaciones 2026</span>'
        '<h1 style="font-size:2.3rem; font-weight:800; margin:0.6rem 0 0.4rem 0; color:#111827; '
        'display:flex; align-items:center; justify-content:center; gap:10px;">'
        '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#2E86DE" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="2"/><line x1="12" y1="7" x2="12" y2="21"/>'
        '<path d="M5 12a7 7 0 0 0 14 0"/><line x1="5" y1="12" x2="5" y2="9"/><line x1="19" y1="12" x2="19" y2="9"/></svg>'
        ' Estatus de Cargas</h1>'
        '<div style="width:52px; height:4px; background:#2E86DE; border-radius:2px; margin:0 auto 0.6rem auto;"></div>'
        '<div style="font-size:1rem; color:#6B7280; font-weight:400;">Antillana Comercial</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    if df.empty:
        st.info("Todavía no hay embarques cargados.")
        return

    recibidas_mes = contar_recibidas_mes_actual()
    mes_actual_txt = hoy_rd().strftime("%B %Y").capitalize()
    st.markdown(
        f'<div class="kpi-card" style="background:{COLOR_RECIBIDAS_MES}; max-width:340px; margin:0 auto 1.2rem auto;">'
        f'<div class="kpi-label">Recibidas este mes ({mes_actual_txt})</div>'
        f'<div class="kpi-value">{recibidas_mes}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    tabs_categorias = ["Todos"] + CATEGORIAS
    tabs = st.tabs(tabs_categorias)
    for nombre_tab, tab in zip(tabs_categorias, tabs):
        with tab:
            if nombre_tab == "Todos":
                df_categoria = df
            else:
                df_categoria = df[df["Categoria"] == nombre_tab]
            _render_categoria(df_categoria, st.session_state.get("rol", "viewer"), nombre_tab)


@st.fragment
def _render_categoria(df: pd.DataFrame, rol: str, tab_key: str):
    if df.empty:
        st.info("No hay embarques en esta categoría.")
        return

    estados = df.apply(lambda r: estado_embarque(r[COL_ETA]), axis=1)
    df = df.copy()
    df["EstadoTexto"] = [e[0] for e in estados]
    df["EstadoIcono"] = [e[1] for e in estados]
    df["DiasEnPuerto"] = [e[2] for e in estados]

    conteo = df["EstadoTexto"].value_counts().to_dict()
    proximos_n = conteo.get("Próximo a llegar", 0)
    en_puerto_n = conteo.get("En Puerto", 0)

    # -------------------- KPIs (clicables: filtran la tabla por ese estado) --------------------
    kpis = [
        ("TOTAL EN TRÁNSITO", len(df), COLOR_TOTAL, "Todos", "total"),
        ("PRÓXIMOS 3 DÍAS", proximos_n, STATUS_COLOR["Próximo a llegar"], "Próximo a llegar", "proximos"),
        ("EN PUERTO (SIN CONFIRMAR)", en_puerto_n, STATUS_COLOR["En Puerto"], "En Puerto", "enpuerto"),
    ]
    estilos_kpi = "".join(
        f'.st-key-kpi_{tab_key}_{slug} button {{'
        f'background:{color} !important; color:#fff !important; border:none !important; '
        f'border-radius:10px !important; width:100% !important; height:74px !important; '
        f'font-weight:800 !important; font-size:1rem !important; text-align:left !important; '
        f'padding:0 16px !important; box-shadow:none !important;}} '
        f'.st-key-kpi_{tab_key}_{slug} button:hover {{filter:brightness(0.92); color:#fff !important;}}'
        for _, _, color, _, slug in kpis
    )
    st.markdown(f"<style>{estilos_kpi}</style>", unsafe_allow_html=True)

    cols = st.columns(len(kpis))
    for col, (label, valor, color, valor_filtro, slug) in zip(cols, kpis):
        with col:
            with st.container(key=f"kpi_{tab_key}_{slug}"):
                if st.button(f"{label} — {valor}", key=f"btn_kpi_{tab_key}_{slug}", use_container_width=True):
                    st.session_state[f"estado_{tab_key}"] = valor_filtro
                    st.rerun(scope="fragment")

    st.write("")

    # -------------------- GRÁFICOS --------------------
    g1, g2 = st.columns([1, 1.4])

    with g1:
        estados_presentes = [e for e in STATUS_ORDER if conteo.get(e, 0) > 0]
        if estados_presentes:
            fig_dona = go.Figure(
                data=[
                    go.Pie(
                        labels=estados_presentes,
                        values=[conteo[e] for e in estados_presentes],
                        hole=0.6,
                        marker=dict(colors=[STATUS_COLOR[e] for e in estados_presentes]),
                        textinfo="value",
                        hovertemplate="%{label}: %{value}<extra></extra>",
                    )
                ]
            )
            fig_dona.update_layout(
                showlegend=True,
                legend=dict(orientation="h", yanchor="bottom", y=-0.25, font=dict(size=11)),
                margin=dict(t=10, b=10, l=10, r=10),
                height=280,
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#374151"),
            )
            st.plotly_chart(
                fig_dona,
                use_container_width=True,
                config={"displayModeBar": False},
                key=f"donut_{tab_key}",
            )

    with g2:
        por_pais = df["Pais_Origen"].replace("", "Sin especificar").value_counts().sort_values()
        if not por_pais.empty:
            colores_barras = [PALETA_PAISES[i % len(PALETA_PAISES)] for i in range(len(por_pais))]
            fig_barras = go.Figure(
                data=[
                    go.Bar(
                        x=por_pais.values,
                        y=por_pais.index,
                        orientation="h",
                        marker=dict(color=colores_barras),
                        text=por_pais.values,
                        textposition="outside",
                    )
                ]
            )
            fig_barras.update_layout(
                margin=dict(t=10, b=10, l=10, r=20),
                height=280,
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#374151"),
                xaxis=dict(showgrid=False, showticklabels=False, title="Embarques por país de origen (clic para filtrar)"),
                yaxis=dict(showgrid=False),
            )
            seleccion = st.plotly_chart(
                fig_barras,
                use_container_width=True,
                config={"displayModeBar": False},
                on_select="rerun",
                selection_mode="points",
                key=f"bar_paises_{tab_key}",
            )
            puntos = (seleccion or {}).get("selection", {}).get("points", [])
            if puntos:
                pais_clic = puntos[0].get("y")
                if pais_clic and pais_clic != "Sin especificar":
                    # Plotly mantiene la selección activa entre reruns, así que sin este
                    # control el clic se reaplicaría en cada rerun y anularía cualquier
                    # cambio manual que el usuario haga luego en el selectbox.
                    firma_clic = f"{tab_key}:{pais_clic}"
                    key_firma = f"ultima_firma_pais_{tab_key}"
                    if firma_clic != st.session_state.get(key_firma):
                        st.session_state[f"pais_{tab_key}"] = pais_clic
                        st.session_state[key_firma] = firma_clic

    st.divider()

    # -------------------- FILTROS --------------------
    paises = ["Todos"] + sorted([p for p in df["Pais_Origen"].unique() if p])
    estados_filtro = ["Todos"] + STATUS_ORDER

    c1, c2 = st.columns(2)
    pais_sel = c1.selectbox("Filtrar por país de origen", paises, key=f"pais_{tab_key}")
    estado_sel = c2.selectbox("Filtrar por estado", estados_filtro, key=f"estado_{tab_key}")

    filtrado = df.copy()
    if pais_sel != "Todos":
        filtrado = filtrado[filtrado["Pais_Origen"] == pais_sel]
    if estado_sel != "Todos":
        filtrado = filtrado[filtrado["EstadoTexto"] == estado_sel]

    filtrado = filtrado.sort_values(COL_ETA)

    # -------------------- LISTA DE EMBARQUES --------------------
    # Se renderizan las DOS vistas (tabla y tarjetas) en el mismo HTML; el CSS
    # (@media max-width:640px) decide cuál se muestra según el ancho real de
    # pantalla del dispositivo que abre la app. Streamlit no puede detectar el
    # ancho del navegador del lado del servidor, así que el toggle se hace en CSS.
    if not filtrado.empty:
        # --- Vista tabla (desktop / tablet) ---
        tabla_html = (
            '<div class="tbl-wrap"><div class="tbl-header">'
            '<div>BL</div><div>Descripción</div><div>Modelo/Serie</div><div>Cant.</div><div>País</div><div>Fecha</div><div>Estado</div>'
            '</div>'
        )
        for _, r in filtrado.iterrows():
            color = STATUS_COLOR.get(r["EstadoTexto"], "#6b7280")
            texto_badge = texto_badge_estado(r)
            tabla_html += (
                f'<div class="tbl-row" style="border-left-color:{color};">'
                f'<div class="tbl-bl">{r["BL"]}</div>'
                f'<div class="tbl-desc">{r["Descripcion"] or "—"}</div>'
                f'<div class="tbl-desc">{r["Modelo_Serie"] or "—"}</div>'
                f'<div>{r["Cantidad"] or "—"}</div>'
                f'<div>{r["Pais_Origen"] or "—"}</div>'
                f'<div>{r[COL_ETA] or "—"}</div>'
                f'<div><span class="status-badge" style="background:{color};">{texto_badge}</span></div>'
                f'</div>'
            )
        tabla_html += '</div>'
        st.markdown(tabla_html, unsafe_allow_html=True)

        # --- Vista tarjetas (celular) ---
        cards_html = '<div class="ship-cards">'
        for _, r in filtrado.iterrows():
            color = STATUS_COLOR.get(r["EstadoTexto"], "#6b7280")
            texto_badge = texto_badge_estado(r)
            cards_html += (
                f'<div class="ship-card" style="border-left-color:{color};">'
                f'<div class="ship-top">'
                f'<div class="ship-bl">{r["BL"]}</div>'
                f'<span class="status-badge" style="background:{color};">{texto_badge}</span>'
                f'</div>'
                f'<div class="ship-desc">{r["Descripcion"] or "—"}</div>'
                f'<div class="ship-grid">'
                f'<div><div class="ship-field-label">Modelo/Serie</div><div class="ship-field-value">{r["Modelo_Serie"] or "—"}</div></div>'
                f'<div><div class="ship-field-label">Cant.</div><div class="ship-field-value">{r["Cantidad"] or "—"}</div></div>'
                f'<div><div class="ship-field-label">País</div><div class="ship-field-value">{r["Pais_Origen"] or "—"}</div></div>'
                f'<div><div class="ship-field-label">ETA</div><div class="ship-field-value">{r[COL_ETA] or "—"}</div></div>'
                f'</div>'
                f'</div>'
            )
        cards_html += '</div>'
        st.markdown(cards_html, unsafe_allow_html=True)

    st.write("")

    # -------------------- ACCIONES DE ADMINISTRADOR --------------------
    if rol == "admin" and not filtrado.empty:
        st.caption("Acciones (selecciona el BL a modificar)")
        for _, r in filtrado.iterrows():
            bl_actual = r["BL"]
            categoria_actual = r["Categoria"]
            key_confirmar = f"confirmar_del_{tab_key}_{bl_actual}"

            if st.session_state.get(key_confirmar):
                st.warning(f"¿Eliminar definitivamente el embarque BL {bl_actual}? Esta acción no se puede deshacer.")
                cc1, cc2, _ = st.columns([1, 1, 3])
                if cc1.button("Sí, eliminar", key=f"si_del_{tab_key}_{bl_actual}", type="primary"):
                    eliminar_embarque(bl_actual, categoria_actual)
                    st.session_state.pop(key_confirmar, None)
                    load_data.clear()
                    st.rerun()
                if cc2.button("Cancelar", key=f"cancel_del_{tab_key}_{bl_actual}"):
                    st.session_state.pop(key_confirmar, None)
                    st.rerun()
            elif not str(bl_actual).strip():
                st.caption(f"⚠️ \"{r['Descripcion'] or 'Sin descripción'}\" no tiene BL asignado todavía — asígnale un BL en el Sheet para poder gestionarlo desde aquí.")
            else:
                ac0, ac1, ac2, _ = st.columns([1.3, 1.4, 1, 2.3])
                ac0.markdown(f"**{bl_actual}**")
                if ac1.button("✅ Marcar como Recibido", key=f"recibido_{tab_key}_{bl_actual}"):
                    ok, mensaje = marcar_como_recibido(bl_actual, categoria_actual)
                    if ok:
                        load_data.clear()
                        contar_recibidas_mes_actual.clear()
                        cargar_historico_recibidos.clear()
                        st.rerun()
                    else:
                        st.error(mensaje)
                if ac2.button("🗑 Eliminar", key=f"eliminar_{tab_key}_{bl_actual}"):
                    st.session_state[key_confirmar] = True
                    st.rerun()


# ---------------------------------------------------------------------------
# ALTA MANUAL (SOLO ADMIN)
# ---------------------------------------------------------------------------
def form_alta_manual():
    st.subheader("➕ Agregar embarque")
    with st.form("form_embarque", clear_on_submit=True):
        c1, c2 = st.columns(2)
        bl = c1.text_input("BL")
        descripcion = c2.text_input("Descripción del producto")
        c3, c4 = st.columns(2)
        modelo = c3.text_input("Modelo o serie del equipo")
        cantidad = c4.number_input("Cantidad de equipos", min_value=1, step=1)
        c5, c6 = st.columns(2)
        pais = c5.text_input("País de origen")
        eta = c6.date_input("Fecha estimada de llegada", value=hoy_rd())
        categoria = st.selectbox("Categoría", CATEGORIAS)

        enviado = st.form_submit_button("Guardar embarque", type="primary")
        if enviado:
            if not bl or not descripcion:
                st.error("BL y Descripción son obligatorios.")
            else:
                existentes = load_data()
                bls_existentes = set(existentes["BL"].astype(str).str.strip())
                if bl.strip() in bls_existentes:
                    st.error(f"Ya existe un embarque con el BL '{bl}'. Revisa el dashboard antes de guardarlo de nuevo.")
                else:
                    ok = append_row({
                        "BL": bl,
                        "Descripcion": descripcion,
                        "Modelo_Serie": modelo,
                        "Cantidad": cantidad,
                        "Pais_Origen": pais,
                        COL_ETA: eta.isoformat(),
                    }, categoria)
                    if ok:
                        st.success("Embarque guardado correctamente.")
                        load_data.clear()
                        st.rerun()
                    else:
                        st.error(
                            f"No existe la pestaña '{categoria}' en el Google Sheet. "
                            "Créala primero (ver instrucciones) y vuelve a intentar."
                        )


# ---------------------------------------------------------------------------
# CARGA MASIVA VÍA EXCEL (SOLO ADMIN)
# ---------------------------------------------------------------------------
def form_carga_masiva():
    st.subheader("📤 Carga masiva desde Excel")
    categoria_destino = st.selectbox("Categoría de destino (todo el archivo se carga en esta pestaña)", CATEGORIAS)
    st.caption(
        "El archivo debe tener exactamente estas columnas: "
        + ", ".join(REQUIRED_COLUMNS)
        + ". La fecha ETA puede venir en cualquier formato reconocible."
    )

    archivo = st.file_uploader("Sube el archivo .xlsx", type=["xlsx"])
    if archivo is not None:
        try:
            nuevo = pd.read_excel(archivo, dtype=str)
        except Exception as e:
            st.error(f"No se pudo leer el archivo: {e}")
            return

        faltantes = [c for c in REQUIRED_COLUMNS if c not in nuevo.columns]
        if faltantes:
            st.error(f"Faltan columnas obligatorias: {', '.join(faltantes)}. Revisa la plantilla.")
            return

        nuevo = nuevo[REQUIRED_COLUMNS].dropna(how="all").copy()

        # Normalizar fechas ETA: Excel suele entregarlas con hora incluida
        # (ej. "2026-08-25 00:00:00") aunque en la celda se vean como AAAA-MM-DD.
        # Se acepta cualquier formato de fecha reconocible y se guarda como AAAA-MM-DD.
        fechas_invalidas = []
        etas_normalizadas = []
        for i, val in enumerate(nuevo[COL_ETA]):
            parsed = pd.to_datetime(val, errors="coerce")
            if pd.isna(parsed):
                fechas_invalidas.append((i + 2, val))  # +2: encabezado + índice base 1
                etas_normalizadas.append("")
            else:
                etas_normalizadas.append(parsed.strftime("%Y-%m-%d"))

        if fechas_invalidas:
            st.error(
                "Hay fechas ETA que no se pudieron interpretar en las filas: "
                + ", ".join(f"{fila} ('{val}')" for fila, val in fechas_invalidas)
            )
            return

        nuevo[COL_ETA] = etas_normalizadas

        existentes = load_data()
        bls_existentes = set(existentes["BL"].astype(str).str.strip())
        es_duplicado = nuevo["BL"].astype(str).str.strip().isin(bls_existentes)
        duplicados = nuevo[es_duplicado]
        nuevos_ok = nuevo[~es_duplicado]

        if not duplicados.empty:
            st.warning(
                "Estos BL ya existen en el sistema y NO se van a cargar de nuevo (para evitar duplicados): "
                + ", ".join(duplicados["BL"].astype(str))
            )

        if nuevos_ok.empty:
            st.info("No hay embarques nuevos que cargar — todos los BL de este archivo ya están en el sistema.")
            return

        st.write(f"Vista previa de los {len(nuevos_ok)} embarque(s) nuevo(s) que se van a cargar en **{categoria_destino}**:")
        st.dataframe(nuevos_ok, use_container_width=True)

        if st.button(f"Confirmar carga de {len(nuevos_ok)} embarque(s)", type="primary"):
            ok = append_rows_bulk(nuevos_ok, categoria_destino)
            if ok:
                st.success(f"{len(nuevos_ok)} embarque(s) cargado(s) correctamente en '{categoria_destino}'.")
                load_data.clear()
                st.rerun()
            else:
                st.error(f"No existe la pestaña '{categoria_destino}' en el Google Sheet. Créala primero.")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    if "rol" not in st.session_state:
        login_screen()
        return

    with st.sidebar:
        st.markdown(f"**Sesión:** {'Administrador' if st.session_state.rol == 'admin' else 'Visualización'}")
        if st.button("Cerrar sesión"):
            st.session_state.clear()
            st.rerun()

    df = load_data()

    if st.session_state.rol == "admin":
        tab1, tab2, tab3, tab4 = st.tabs(["📊 Dashboard", "➕ Agregar embarque", "📤 Carga masiva", "📜 Histórico"])
        with tab1:
            mostrar_dashboard(df)
        with tab2:
            form_alta_manual()
        with tab3:
            form_carga_masiva()
        with tab4:
            mostrar_historico()
    else:
        tab1, tab2 = st.tabs(["📊 Dashboard", "📜 Histórico"])
        with tab1:
            mostrar_dashboard(df)
        with tab2:
            mostrar_historico()


if __name__ == "__main__":
    main()
