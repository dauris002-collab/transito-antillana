"""
Antillana Comercial - Visibilidad de embarques en transito
===========================================================
app.py - v3.1

Cambios estructurales frente a versiones anteriores (resumen para
mantenimiento):

 1. Escrituras por numero de fila, no por BL (BLs repetidos ya no arriesgan
    tocar la fila equivocada).
 2. Cada etapa se puede fechar en el dia en que REALMENTE ocurrio, con
    validacion de que las 5 fechas del flujo vayan en orden.
 3. Diagrama de flujo en HTML/CSS en vez de Plotly (mas rapido en celular).
 4. Alertas de cuello de botella con SLA por etapa, ajustables desde Secrets.
 5. Reintentos con espera ante errores 429/500/503 de Google.
 6. Menos llamadas a la API por accion.
 7. Archivar conserva Fecha_Salida, OC y EE ademas de las fechas del flujo.
 8. Fecha_Recibido es la fecha real de llegada a puerto (ETA solo de respaldo).
 9. Historico con tiempos de ciclo (medianas).
10. Bloqueo optimista en la edicion.
11. Ajustes de celular (tipografia 16px, areas tactiles de 44px, KPIs en
    rejilla de dos columnas).
12. Panel "Salud de los datos" y "Bitacora" en Herramientas.
13. Sesion con vida distinta por rol, atada al navegador que la abrio.
14. Rendimiento de pantalla (busqueda precalculada, caches de Excel y de
    parseo de fechas).
15-17. (v3.1) El filtro de arriba y el diagrama de abajo se hablan; la tabla
    de costo acumulado solo aparece tras clickear un filtro; Aereos y Carga
    Suelta muestran "Costo por Almacenaje" en vez de "Costo en puerto".

Este archivo (app.py) es el punto de entrada: configuracion de pagina,
sesion/login y main(). La logica de negocio vive en logica.py, el acceso a
Google Sheets en sheets_io.py, los componentes visuales en ui_componentes.py
y los formularios/herramientas de administracion en vistas_admin.py.
"""

from __future__ import annotations

import hashlib
import time
from secrets import token_urlsafe

import streamlit as st

from sheets_io import cargar_todo, invalidar_caches, registrar_log
from ui_componentes import CUSTOM_CSS, VERSION_APP, mostrar_dashboard, selector_horizontal
from vistas_admin import (
    form_alta_manual, form_carga_masiva, form_editar, herramientas, mostrar_historico,
)


st.set_page_config(
    page_title="Antillana · Embarques en Tránsito",
    page_icon="🚢",
    layout="wide",
    initial_sidebar_state="collapsed",  # en celular la barra lateral tapaba la pantalla
)


LARGO_PIN = 4               # dígitos del PIN


# Vida de sesión por rol. El admin trabaja sentado en la app; el viewer (el
# presidente, gerentes) abre el link desde el celular una vez al día y volver a
# pedirle el PIN cada rato es la forma más rápida de que deje de usarla.
VIDA_SESION_MIN = {"admin": 120, "viewer": 720}


MAX_INTENTOS_SESION = 5


MAX_FALLOS_GLOBAL = 40      # freno global: el bloqueo por sesión se evade en incógnito


VENTANA_FALLOS = 10 * 60


BLOQUEO_SEGUNDOS = 15 * 60


# ---------------------------------------------------------------------------
# ACCESO
# ---------------------------------------------------------------------------
@st.cache_resource
def _sesiones_activas() -> dict:
    """Sesiones vivas, compartidas entre todas las conexiones del servidor.
    Streamlit pierde session_state en cada recarga del navegador, así que para no
    pedir el PIN otra vez se guarda un token en la URL cuyo contenido real vive
    aquí, del lado del servidor. En la URL solo va el identificador, nunca el rol
    ni el PIN. Se vacía cuando la app se reinicia o se redespliega."""
    return {}


def _huella_cliente() -> str:
    """Huella del navegador que abrió la sesión. Sirve para que reenviar el link
    con el token (cosa que pasa: alguien comparte la URL por WhatsApp) no regale
    el acceso. Si Streamlit no expone las cabeceras, la huella queda vacía para
    todos y el comportamiento es el de antes — nunca rechaza de más."""
    try:
        cabeceras = st.context.headers or {}
        base = str(cabeceras.get("User-Agent", ""))
    except Exception:
        base = ""
    return hashlib.sha256(base.encode("utf-8", "ignore")).hexdigest()[:16]


def _purgar_sesiones(ahora: float):
    for token in [t for t, d in _sesiones_activas().items() if d["expira"] < ahora]:
        _sesiones_activas().pop(token, None)


def _vida_sesion(rol: str) -> int:
    return VIDA_SESION_MIN.get(rol, 120)


def _abrir_sesion(rol: str, nombre: str):
    ahora = time.time()
    _purgar_sesiones(ahora)
    token = token_urlsafe(18)
    _sesiones_activas()[token] = {
        "rol": rol, "nombre": nombre, "huella": _huella_cliente(),
        "expira": ahora + _vida_sesion(rol) * 60,
    }
    st.session_state.rol = rol
    st.session_state.usuario = nombre
    st.session_state.token = token
    st.query_params["s"] = token


def restaurar_sesion():
    """Al abrir la página, intenta reanudar la sesión desde el token de la URL.
    Cada recarga vuelve a correr el reloj (ventana deslizante)."""
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
    if datos.get("huella") and datos["huella"] != _huella_cliente():
        # El token viajó a otro navegador: se ignora y se pide el PIN.
        st.query_params.clear()
        return

    datos["expira"] = ahora + _vida_sesion(datos["rol"]) * 60
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
    """Devuelve (rol, nombre) o (None, None). Soporta PIN por persona con la
    tabla [pins] de secrets:  [pins.1234]  nombre = "Dauris"  rol = "admin".
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
        '<div class="ant-head" style="margin-top:2.4rem;">'
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
        # "pulsado". Dentro de un formulario, Enter equivale a pulsar el submit —
        # que en celular es la diferencia entre entrar y quedarse trancado.
        with st.form("form_login", clear_on_submit=True, border=False):
            pin = st.text_input("PIN", type="password", max_chars=LARGO_PIN,
                                label_visibility="collapsed",
                                placeholder=f"PIN de {LARGO_PIN} dígitos")
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
        st.caption(f"Sesión recordada {_vida_sesion(st.session_state.rol)} min de inactividad")
        st.write("")
        if st.button("Cerrar sesión", width="stretch"):
            cerrar_sesion()
            st.rerun()
        st.caption(f"v{VERSION_APP}")

    # La barra lateral llega colapsada en celular: el botón de actualizar tiene
    # que estar también aquí arriba, o desde el teléfono no hay forma de refrescar
    # sin recargar la página entera.
    st.markdown('<div class="nav-rotulo">Sección</div>', unsafe_allow_html=True)
    nav, actualizar = st.columns([5, 1])
    with nav:
        if len(secciones) > 1:
            seccion = selector_horizontal("Sección", secciones, key="seccion_actual", ancho="content")
        else:
            seccion = secciones[0]
    with actualizar:
        if st.button("↻ Actualizar", key="refrescar_top", width="stretch"):
            invalidar_caches()
            st.rerun()

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
