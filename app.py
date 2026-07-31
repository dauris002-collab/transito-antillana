import time
from datetime import date, datetime

import pandas as pd
import streamlit as st
import gspread
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
    sheet.append_row(ordered, value_input_option="USER_ENTERED")


def append_rows_bulk(df: pd.DataFrame):
    sheet = get_sheet()
    hoy = date.today().isoformat()
    rows = []
    for _, r in df.iterrows():
        row = {c: r.get(c, "") for c in REQUIRED_COLUMNS}
        row["Recibido"] = ""
        row["Fecha_Actualizacion"] = hoy
        rows.append([row.get(c, "") for c in ALL_COLUMNS])
    sheet.append_rows(rows, value_input_option="USER_ENTERED")


# ---------------------------------------------------------------------------
# LÓGICA DE ESTADO DEL EMBARQUE
# ---------------------------------------------------------------------------
def estado_embarque(eta_str: str, recibido: str):
    if str(recibido).strip().lower() in ("si", "sí", "true", "1", "x"):
        return "Llegado", "🟢"
    try:
        eta = datetime.strptime(str(eta_str).strip(), "%Y-%m-%d").date()
    except ValueError:
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
    st.subheader("📦 Estado de embarques")

    if df.empty:
        st.info("Todavía no hay embarques cargados.")
        return

    estados = df.apply(lambda r: estado_embarque(r["ETA"], r.get("Recibido", "")), axis=1)
    df = df.copy()
    df["EstadoTexto"] = [e[0] for e in estados]
    df["EstadoIcono"] = [e[1] for e in estados]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total embarques", len(df))
    col2.metric("En tránsito", int((df["EstadoTexto"] == "En tránsito").sum()))
    col3.metric("Próximos a llegar (7 días)", int((df["EstadoTexto"] == "Próximo a llegar").sum()))
    col4.metric("Retrasados", int((df["EstadoTexto"] == "Retrasado").sum()))

    st.divider()

    paises = ["Todos"] + sorted([p for p in df["Pais_Origen"].unique() if p])
    estados_filtro = ["Todos", "En tránsito", "Próximo a llegar", "Retrasado", "Llegado", "Sin fecha válida"]
    c1, c2 = st.columns(2)
    pais_sel = c1.selectbox("Filtrar por país de origen", paises)
    estado_sel = c2.selectbox("Filtrar por estado", estados_filtro)

    filtrado = df.copy()
    if pais_sel != "Todos":
        filtrado = filtrado[filtrado["Pais_Origen"] == pais_sel]
    if estado_sel != "Todos":
        filtrado = filtrado[filtrado["EstadoTexto"] == estado_sel]

    filtrado = filtrado.sort_values("ETA")

    for _, r in filtrado.iterrows():
        with st.container(border=True):
            top = st.columns([3, 2])
            top[0].markdown(f"**BL: {r['BL']}**  \n{r['Descripcion']}")
            top[1].markdown(f"{r['EstadoIcono']} **{r['EstadoTexto']}**")
            bot = st.columns(4)
            bot[0].markdown(f"**Modelo/Serie**  \n{r['Modelo_Serie']}")
            bot[1].markdown(f"**Cantidad**  \n{r['Cantidad']}")
            bot[2].markdown(f"**País origen**  \n{r['Pais_Origen']}")
            bot[3].markdown(f"**ETA**  \n{r['ETA']}")


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

        enviado = st.form_submit_button("Guardar embarque", type="primary")
        if enviado:
            if not bl or not descripcion:
                st.error("BL y Descripción son obligatorios.")
            else:
                append_row({
                    "BL": bl,
                    "Descripcion": descripcion,
                    "Modelo_Serie": modelo,
                    "Cantidad": cantidad,
                    "Pais_Origen": pais,
                    "ETA": eta.isoformat(),
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
        + ". La fecha ETA puede venir en cualquier formato de fecha reconocible (la app la normaliza sola)."
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

        st.write("Vista previa de lo que se va a cargar:")
        st.dataframe(nuevo, use_container_width=True)

        if st.button(f"Confirmar carga de {len(nuevo)} embarque(s)", type="primary"):
            append_rows_bulk(nuevo)
            st.success(f"{len(nuevo)} embarque(s) cargado(s) correctamente.")
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
