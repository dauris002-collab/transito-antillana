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

REQUIRED_COLUMNS = ["BL", "Descripcion", "Modelo_Serie", "Cantidad", "Pais_Origen", "ETA", "Categoria"]
ALL_COLUMNS = REQUIRED_COLUMNS + ["Recibido", "Fecha_Actualizacion"]

CATEGORIAS = ["Equipos", "Generadores", "Repuestos", "Aéreos", "Pedidos de Emergencia"]

# Paleta alineada al reporte de Power BI: bloques de color sólido, planos, alto contraste.
STATUS_COLOR = {
    "En tránsito": "#2E86DE",       # azul (igual al KPI "Total")
    "Próximo a llegar": "#5C6BC0",  # morado (igual al KPI "Promedio en puerto")
    "Retrasado": "#E53935",         # rojo (igual al KPI "Cargo por demora")
    "Llegado": "#2E7D32",           # verde (igual al KPI "Total monto")
    "Sin fecha válida": "#6b7280",
}
COLOR_TOTAL = "#17A2B8"  # teal, distintivo para el total general
STATUS_ORDER = ["Retrasado", "Próximo a llegar", "En tránsito", "Llegado", "Sin fecha válida"]

CUSTOM_CSS = """
<style>
.kpi-card {
    border-radius: 14px;
    padding: 18px 20px;
    height: 100%;
}
.kpi-label {
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: rgba(255,255,255,0.85);
    margin-bottom: 6px;
}
.kpi-value { font-size: 2.1rem; font-weight: 800; line-height: 1; color: #ffffff; }

.ship-card {
    border-radius: 12px;
    padding: 14px 20px;
    margin-bottom: 10px;
    background: rgba(255,255,255,0.025);
    border-left: 5px solid #6b7280;
}
.ship-top { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
.ship-bl { font-size: 1.02rem; font-weight: 700; }
.ship-desc { font-size: 0.85rem; color: rgba(255,255,255,0.65); }
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
.ship-field-label { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.04em; color: rgba(255,255,255,0.45); }
.ship-field-value { font-size: 0.92rem; font-weight: 600; }
</style>
"""

MAX_INTENTOS = 5
BLOQUEO_SEGUNDOS = 15 * 60  # 15 minutos


# ---------------------------------------------------------------------------
# CONEXIÓN A GOOGLE SHEETS
# ---------------------------------------------------------------------------
@st.cache_resource
def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=scopes
    )
    client = gspread.authorize(creds)
    return client.open_by_key(st.secrets["SHEET_ID"]).sheet1


def load_data() -> pd.DataFrame:
    sheet = get_sheet()
    records = sheet.get_all_records()
    df = pd.DataFrame(records)
    if df.empty:
        df = pd.DataFrame(columns=ALL_COLUMNS)
    for col in ALL_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df


def append_row(row: dict):
    sheet = get_sheet()
    row["Fecha_Actualizacion"] = date.today().isoformat()
    row.setdefault("Recibido", "")
    ordered = [row.get(c, "") for c in ALL_COLUMNS]
    sheet.append_row(ordered, value_input_option="RAW")


def append_rows_bulk(df: pd.DataFrame):
    sheet = get_sheet()
    hoy = date.today().isoformat()
    rows = []
    for _, r in df.iterrows():
        row = {c: r.get(c, "") for c in REQUIRED_COLUMNS}
        row["Recibido"] = ""
        row["Fecha_Actualizacion"] = hoy
        rows.append([row.get(c, "") for c in ALL_COLUMNS])
    sheet.append_rows(rows, value_input_option="RAW")


def _fila_por_bl(bl: str):
    """Devuelve el número de fila (1-indexado, con encabezado) del BL dado, o None si no existe."""
    sheet = get_sheet()
    try:
        cell = sheet.find(str(bl).strip(), in_column=1)
    except gspread.exceptions.CellNotFound:
        return None
    return cell.row


def eliminar_embarque(bl: str) -> bool:
    sheet = get_sheet()
    fila = _fila_por_bl(bl)
    if fila is None:
        return False
    sheet.delete_rows(fila)
    return True


def marcar_como_llegado(bl: str) -> bool:
    sheet = get_sheet()
    fila = _fila_por_bl(bl)
    if fila is None:
        return False
    col_recibido = ALL_COLUMNS.index("Recibido") + 1
    col_fecha = ALL_COLUMNS.index("Fecha_Actualizacion") + 1
    sheet.update_cell(fila, col_recibido, "Si")
    sheet.update_cell(fila, col_fecha, date.today().isoformat())
    return True


# ---------------------------------------------------------------------------
# LÓGICA DE ESTADO DEL EMBARQUE
# ---------------------------------------------------------------------------
def estado_embarque(eta_str: str, recibido: str):
    if str(recibido).strip().lower() in ("si", "sí", "true", "1", "x"):
        return "Llegado", "🟢"
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
    if eta < hoy:
        return "Retrasado", "🔴"
    elif (eta - hoy).days <= 7:
        return "Próximo a llegar", "🟡"
    else:
        return "En tránsito", "🔵"


# ---------------------------------------------------------------------------
# LOGIN CON PIN
# ---------------------------------------------------------------------------
def login_screen():
    st.title("🚢 Antillana Comercial · Embarques en Tránsito")
    st.caption("Acceso restringido. Ingresa tu PIN de 4 dígitos.")

    if "intentos" not in st.session_state:
        st.session_state.intentos = 0
    if "bloqueado_hasta" not in st.session_state:
        st.session_state.bloqueado_hasta = 0

    ahora = time.time()
    if ahora < st.session_state.bloqueado_hasta:
        restante = int(st.session_state.bloqueado_hasta - ahora)
        st.error(f"Demasiados intentos fallidos. Intenta de nuevo en {restante // 60} min {restante % 60} seg.")
        return

    pin = st.text_input("PIN", type="password", max_chars=4)
    if st.button("Entrar", type="primary"):
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
                st.error("PIN incorrecto. Acceso bloqueado por 15 minutos.")
            else:
                st.error(f"PIN incorrecto. Te quedan {restantes} intento(s).")


# ---------------------------------------------------------------------------
# DASHBOARD (VISTA PRINCIPAL)
# ---------------------------------------------------------------------------
def mostrar_dashboard(df: pd.DataFrame):
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.subheader("📦 Estado de embarques")

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
                df_categoria = df[df.get("Categoria", "") == nombre_tab]
            _render_categoria(df_categoria, st.session_state.get("rol", "viewer"))


def _render_categoria(df: pd.DataFrame, rol: str):
    if df.empty:
        st.info("No hay embarques en esta categoría.")
        return

    estados = df.apply(lambda r: estado_embarque(r["ETA"], r.get("Recibido", "")), axis=1)
    df = df.copy()
    df["EstadoTexto"] = [e[0] for e in estados]
    df["EstadoIcono"] = [e[1] for e in estados]

    conteo = df["EstadoTexto"].value_counts().to_dict()

    # -------------------- KPIs --------------------
    kpis = [
        ("TOTAL EMBARQUES", len(df), COLOR_TOTAL),
        ("EN TRÁNSITO", conteo.get("En tránsito", 0), STATUS_COLOR["En tránsito"]),
        ("PRÓXIMOS A LLEGAR (7 DÍAS)", conteo.get("Próximo a llegar", 0), STATUS_COLOR["Próximo a llegar"]),
        ("RETRASADOS", conteo.get("Retrasado", 0), STATUS_COLOR["Retrasado"]),
        ("LLEGADOS", conteo.get("Llegado", 0), STATUS_COLOR["Llegado"]),
    ]
    cols = st.columns(len(kpis))
    for col, (label, valor, color) in zip(cols, kpis):
        col.markdown(
            f"""<div class="kpi-card" style="background:{color};">
                <div class="kpi-label">{label}</div>
                <div class="kpi-value">{valor}</div>
                </div>""",
            unsafe_allow_html=True,
        )

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
                font=dict(color="#e5e7eb"),
            )
            st.plotly_chart(fig_dona, use_container_width=True, config={"displayModeBar": False})

    with g2:
        por_pais = df["Pais_Origen"].replace("", "Sin especificar").value_counts().sort_values()
        if not por_pais.empty:
            fig_barras = go.Figure(
                data=[
                    go.Bar(
                        x=por_pais.values,
                        y=por_pais.index,
                        orientation="h",
                        marker=dict(color=STATUS_COLOR["En tránsito"]),
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
                font=dict(color="#e5e7eb"),
                xaxis=dict(showgrid=False, title="Embarques por país de origen"),
                yaxis=dict(showgrid=False),
            )
            st.plotly_chart(fig_barras, use_container_width=True, config={"displayModeBar": False})

    st.divider()

    # -------------------- FILTROS --------------------
    # 'Llegado' queda fuera de la vista por defecto (histórico), y solo el
    # administrador puede consultarlo explícitamente seleccionándolo en el filtro.
    paises = ["Todos"] + sorted([p for p in df["Pais_Origen"].unique() if p])
    if rol == "admin":
        estados_filtro = ["Todos"] + STATUS_ORDER
    else:
        estados_filtro = ["Todos"] + [e for e in STATUS_ORDER if e != "Llegado"]

    c1, c2 = st.columns(2)
    pais_sel = c1.selectbox("Filtrar por país de origen", paises, key=f"pais_{id(df)}")
    estado_sel = c2.selectbox("Filtrar por estado", estados_filtro, key=f"estado_{id(df)}")

    filtrado = df.copy()
    if pais_sel != "Todos":
        filtrado = filtrado[filtrado["Pais_Origen"] == pais_sel]
    if estado_sel == "Todos":
        filtrado = filtrado[filtrado["EstadoTexto"] != "Llegado"]
    else:
        filtrado = filtrado[filtrado["EstadoTexto"] == estado_sel]

    filtrado = filtrado.sort_values("ETA")

    if rol == "admin" and estado_sel == "Llegado":
        st.caption("📜 Consultando histórico de embarques ya llegados.")

    # -------------------- LISTA DE EMBARQUES --------------------
    for _, r in filtrado.iterrows():
        color = STATUS_COLOR.get(r["EstadoTexto"], "#6b7280")
        st.markdown(
            f"""
            <div class="ship-card" style="border-left-color:{color};">
                <div class="ship-top">
                    <div>
                        <div class="ship-bl">BL: {r['BL']}</div>
                        <div class="ship-desc">{r['Descripcion']}</div>
                    </div>
                    <span class="status-badge" style="background:{color};">{r['EstadoIcono']} {r['EstadoTexto']}</span>
                </div>
                <div class="ship-grid">
                    <div><div class="ship-field-label">Modelo/Serie</div><div class="ship-field-value">{r['Modelo_Serie'] or '—'}</div></div>
                    <div><div class="ship-field-label">Cantidad</div><div class="ship-field-value">{r['Cantidad'] or '—'}</div></div>
                    <div><div class="ship-field-label">País origen</div><div class="ship-field-value">{r['Pais_Origen'] or '—'}</div></div>
                    <div><div class="ship-field-label">ETA</div><div class="ship-field-value">{r['ETA'] or '—'}</div></div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if rol == "admin":
            bl_actual = r["BL"]
            key_confirmar = f"confirmar_del_{bl_actual}"

            if st.session_state.get(key_confirmar):
                st.warning(f"¿Eliminar definitivamente el embarque BL {bl_actual}? Esta acción no se puede deshacer.")
                cc1, cc2, _ = st.columns([1, 1, 3])
                if cc1.button("Sí, eliminar", key=f"si_del_{bl_actual}", type="primary"):
                    eliminar_embarque(bl_actual)
                    st.session_state.pop(key_confirmar, None)
                    st.cache_resource.clear()
                    st.rerun()
                if cc2.button("Cancelar", key=f"cancel_del_{bl_actual}"):
                    st.session_state.pop(key_confirmar, None)
                    st.rerun()
            else:
                ac1, ac2, _ = st.columns([1.4, 1, 2.6])
                if r["EstadoTexto"] != "Llegado":
                    if ac1.button("✅ Marcar como llegado", key=f"llegado_{bl_actual}"):
                        marcar_como_llegado(bl_actual)
                        st.cache_resource.clear()
                        st.rerun()
                if ac2.button("🗑 Eliminar", key=f"eliminar_{bl_actual}"):
                    st.session_state[key_confirmar] = True
                    st.rerun()

        st.write("")


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
                    append_row({
                        "BL": bl,
                        "Descripcion": descripcion,
                        "Modelo_Serie": modelo,
                        "Cantidad": cantidad,
                        "Pais_Origen": pais,
                        "ETA": eta.isoformat(),
                        "Categoria": categoria,
                    })
                    st.success("Embarque guardado correctamente.")
                    st.cache_resource.clear()
                    st.rerun()


# ---------------------------------------------------------------------------
# CARGA MASIVA VÍA EXCEL (SOLO ADMIN)
# ---------------------------------------------------------------------------
def form_carga_masiva():
    st.subheader("📤 Carga masiva desde Excel")
    st.caption(
        "El archivo debe tener exactamente estas columnas: "
        + ", ".join(REQUIRED_COLUMNS)
        + ". La fecha ETA puede venir en cualquier formato reconocible. "
        + "La columna Categoria debe usar exactamente uno de estos valores: "
        + ", ".join(CATEGORIAS) + "."
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

        # Validar categoría
        categorias_invalidas = [
            (i + 2, val) for i, val in enumerate(nuevo["Categoria"])
            if str(val).strip() not in CATEGORIAS
        ]
        if categorias_invalidas:
            st.error(
                "La columna Categoria debe usar exactamente uno de estos valores: "
                + ", ".join(CATEGORIAS)
                + ". Filas con valor inválido: "
                + ", ".join(f"{fila} ('{val}')" for fila, val in categorias_invalidas)
            )
            return

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

        st.write(f"Vista previa de los {len(nuevos_ok)} embarque(s) nuevo(s) que se van a cargar:")
        st.dataframe(nuevos_ok, use_container_width=True)

        if st.button(f"Confirmar carga de {len(nuevos_ok)} embarque(s)", type="primary"):
            append_rows_bulk(nuevos_ok)
            st.success(f"{len(nuevos_ok)} embarque(s) cargado(s) correctamente.")
            st.cache_resource.clear()
            st.rerun()


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
