import time
from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import gspread
import gspread.exceptions
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Antillana · Embarques en Tránsito",
    page_icon="🚢",
    layout="wide",
)

REQUIRED_COLUMNS = ["BL", "Descripcion", "Modelo_Serie", "Cantidad", "Pais_Origen", "ETA"]
ALL_COLUMNS = REQUIRED_COLUMNS + ["Recibido", "Fecha_Actualizacion"]

# Cada categoría es una PESTAÑA distinta dentro del mismo Google Sheet.
# El nombre de la pestaña debe coincidir exactamente (tal cual, con tilde donde aplique).
CATEGORIAS = ["Equipos", "Generadores", "Repuestos", "Aéreos", "Pedidos de Emergencia"]

# Paleta alineada al reporte de Power BI: bloques de color sólido, planos, alto contraste.
STATUS_COLOR = {
    "En tránsito": "#2E86DE",       # azul (igual al KPI "Total")
    "Próximo a llegar": "#5C6BC0",  # morado (igual al KPI "Promedio en puerto")
    "Recibido": "#2E7D32",          # verde — solo para consulta histórica
    "Sin fecha válida": "#6b7280",
}
COLOR_TOTAL = "#17A2B8"  # teal, distintivo para el total general
STATUS_ORDER = ["Próximo a llegar", "En tránsito", "Recibido", "Sin fecha válida"]
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
    """Devuelve la pestaña (hoja) correspondiente a esa categoría, o None si no existe todavía."""
    ss = get_spreadsheet()
    try:
        return ss.worksheet(categoria)
    except gspread.exceptions.WorksheetNotFound:
        return None


@st.cache_data(ttl=20, show_spinner=False)
def load_data() -> pd.DataFrame:
    """Lee las 5 pestañas de categoría y las combina en una sola tabla,
    agregando la columna Categoria según de qué pestaña vino cada fila."""
    frames = []
    for categoria in CATEGORIAS:
        ws = get_worksheet(categoria)
        if ws is None:
            continue
        records = ws.get_all_records()
        df_cat = pd.DataFrame(records)
        if df_cat.empty:
            df_cat = pd.DataFrame(columns=ALL_COLUMNS)
        for col in ALL_COLUMNS:
            if col not in df_cat.columns:
                df_cat[col] = ""
        df_cat["Categoria"] = categoria
        frames.append(df_cat)
    if not frames:
        return pd.DataFrame(columns=ALL_COLUMNS + ["Categoria"])
    return pd.concat(frames, ignore_index=True)


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
    row["Fecha_Actualizacion"] = date.today().isoformat()
    row.setdefault("Recibido", "")
    ordered = _fila_desde_dict(ws, row)
    ws.append_row(ordered, value_input_option="RAW")
    return True


def append_rows_bulk(df: pd.DataFrame, categoria: str) -> bool:
    ws = get_worksheet(categoria)
    if ws is None:
        return False
    hoy = date.today().isoformat()
    rows = []
    for _, r in df.iterrows():
        row = {c: r.get(c, "") for c in REQUIRED_COLUMNS}
        row["Recibido"] = ""
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


def marcar_como_recibido(bl: str, categoria: str) -> bool:
    ws = get_worksheet(categoria)
    fila = _fila_por_bl(bl, categoria)
    if ws is None or fila is None:
        return False
    headers = _headers(ws)
    if "Recibido" not in headers or "Fecha_Actualizacion" not in headers:
        return False
    col_recibido = headers.index("Recibido") + 1
    col_fecha = headers.index("Fecha_Actualizacion") + 1
    ws.update_cell(fila, col_recibido, "Si")
    ws.update_cell(fila, col_fecha, date.today().isoformat())
    return True


def quitar_recibido(bl: str, categoria: str) -> bool:
    ws = get_worksheet(categoria)
    fila = _fila_por_bl(bl, categoria)
    if ws is None or fila is None:
        return False
    headers = _headers(ws)
    if "Recibido" not in headers:
        return False
    col_recibido = headers.index("Recibido") + 1
    ws.update_cell(fila, col_recibido, "")
    if "Fecha_Actualizacion" in headers:
        col_fecha = headers.index("Fecha_Actualizacion") + 1
        ws.update_cell(fila, col_fecha, date.today().isoformat())
    return True


# ---------------------------------------------------------------------------
# LÓGICA DE ESTADO DEL EMBARQUE
# ---------------------------------------------------------------------------
def estado_embarque(eta_str: str, recibido: str):
    if str(recibido).strip().lower() in ("si", "sí", "true", "1", "x"):
        return "Recibido", "🟢"
    eta = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            eta = datetime.strptime(str(eta_str).strip().split(" ")[0], fmt).date()
            break
        except ValueError:
            continue
    if eta is None:
        return "Sin fecha válida", "⚪"
    hoy = date.today()
    # Ya no se distingue "retrasado" como categoría aparte: si la ETA ya pasó
    # o está a 7 días o menos, se trata como "Próximo a llegar" (es lo urgente).
    if (eta - hoy).days <= 7:
        return "Próximo a llegar", "🟡"
    else:
        return "En tránsito", "🔵"


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
        '<h1 style="text-align:center; font-size:2.6rem; font-weight:800; '
        'margin:0.5rem 0 1.5rem 0; color:#111827;">🚢 Estatus de Cargas</h1>',
        unsafe_allow_html=True,
    )

    if df.empty:
        st.info("Todavía no hay embarques cargados.")
        return

    tabs_categorias = ["Todos"] + CATEGORIAS
    tabs = st.tabs(tabs_categorias)
    for nombre_tab, tab in zip(tabs_categorias, tabs):
        with tab:
            if nombre_tab == "Todos":
                df_categoria = df
            else:
                df_categoria = df[df["Categoria"] == nombre_tab]
            _render_categoria(df_categoria, st.session_state.get("rol", "viewer"), nombre_tab)


def _render_categoria(df: pd.DataFrame, rol: str, tab_key: str):
    if df.empty:
        st.info("No hay embarques en esta categoría.")
        return

    estados = df.apply(lambda r: estado_embarque(r["ETA"], r.get("Recibido", "")), axis=1)
    df = df.copy()
    df["EstadoTexto"] = [e[0] for e in estados]
    df["EstadoIcono"] = [e[1] for e in estados]

    conteo = df["EstadoTexto"].value_counts().to_dict()
    proximos_n = conteo.get("Próximo a llegar", 0)

    # -------------------- KPIs --------------------
    kpis = [
        ("TOTAL EN TRÁNSITO", len(df), COLOR_TOTAL, None),
        ("PRÓXIMOS 7 DÍAS", proximos_n, STATUS_COLOR["Próximo a llegar"], None),
    ]
    cols = st.columns(len(kpis))
    for col, (label, valor, color, sub) in zip(cols, kpis):
        sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
        html = f'<div class="kpi-card" style="background:{color};"><div class="kpi-label">{label}</div><div class="kpi-value">{valor}</div>{sub_html}</div>'
        col.markdown(html, unsafe_allow_html=True)

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
                xaxis=dict(showgrid=False, title="Embarques por país de origen (clic para filtrar)"),
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
                    st.session_state[f"pais_{tab_key}"] = pais_clic

    st.divider()

    # -------------------- FILTROS --------------------
    # 'Recibido' queda fuera de la vista por defecto (histórico), y solo el
    # administrador puede consultarlo explícitamente seleccionándolo en el filtro.
    paises = ["Todos"] + sorted([p for p in df["Pais_Origen"].unique() if p])
    if rol == "admin":
        estados_filtro = ["Todos"] + STATUS_ORDER
    else:
        estados_filtro = ["Todos"] + [e for e in STATUS_ORDER if e != "Recibido"]

    c1, c2 = st.columns(2)
    pais_sel = c1.selectbox("Filtrar por país de origen", paises, key=f"pais_{tab_key}")
    estado_sel = c2.selectbox("Filtrar por estado", estados_filtro, key=f"estado_{tab_key}")

    filtrado = df.copy()
    if pais_sel != "Todos":
        filtrado = filtrado[filtrado["Pais_Origen"] == pais_sel]
    if estado_sel == "Todos":
        filtrado = filtrado[filtrado["EstadoTexto"] != "Recibido"]
    else:
        filtrado = filtrado[filtrado["EstadoTexto"] == estado_sel]

    filtrado = filtrado.sort_values("ETA")

    if rol == "admin" and estado_sel == "Recibido":
        st.caption("📜 Consultando histórico de embarques recibidos.")

    # -------------------- LISTA DE EMBARQUES (TABLA COMPACTA) --------------------
    if not filtrado.empty:
        st.markdown(
            '<div class="tbl-wrap"><div class="tbl-header">'
            '<div>BL</div><div>Descripción</div><div>Modelo/Serie</div><div>Cant.</div><div>País</div><div>Fecha</div><div>Estado</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        for _, r in filtrado.iterrows():
            color = STATUS_COLOR.get(r["EstadoTexto"], "#6b7280")
            es_recibido = r["EstadoTexto"] == "Recibido"
            ultimo_valor = r["Fecha_Actualizacion"] if es_recibido else r["ETA"]
            fila_html = (
                f'<div class="tbl-row" style="border-left-color:{color};">'
                f'<div class="tbl-bl">{r["BL"]}</div>'
                f'<div class="tbl-desc">{r["Descripcion"] or "—"}</div>'
                f'<div class="tbl-desc">{r["Modelo_Serie"] or "—"}</div>'
                f'<div>{r["Cantidad"] or "—"}</div>'
                f'<div>{r["Pais_Origen"] or "—"}</div>'
                f'<div>{ultimo_valor or "—"}</div>'
                f'<div><span class="status-badge" style="background:{color};">{r["EstadoIcono"]} {r["EstadoTexto"]}</span></div>'
                f'</div>'
            )
            st.markdown(fila_html, unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

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
            else:
                ac0, ac1, ac2, _ = st.columns([1.3, 1.4, 1, 2.3])
                ac0.markdown(f"**{bl_actual}**")
                if r["EstadoTexto"] == "Recibido":
                    if ac1.button("↩ Quitar Recibido", key=f"quitar_recibido_{tab_key}_{bl_actual}"):
                        quitar_recibido(bl_actual, categoria_actual)
                        load_data.clear()
                        st.rerun()
                else:
                    if ac1.button("✅ Marcar como Recibido", key=f"recibido_{tab_key}_{bl_actual}"):
                        marcar_como_recibido(bl_actual, categoria_actual)
                        load_data.clear()
                        st.rerun()
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
        eta = c6.date_input("Fecha estimada de llegada")
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
                        "ETA": eta.isoformat(),
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
        for i, val in enumerate(nuevo["ETA"]):
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

        nuevo["ETA"] = etas_normalizadas

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
        tab1, tab2, tab3 = st.tabs(["📊 Dashboard", "➕ Agregar embarque", "📤 Carga masiva"])
        with tab1:
            mostrar_dashboard(df)
        with tab2:
            form_alta_manual()
        with tab3:
            form_carga_masiva()
    else:
        mostrar_dashboard(df)


if __name__ == "__main__":
    main()
