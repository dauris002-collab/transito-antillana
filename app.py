"""
Antillana Comercial - Visibilidad de embarques en transito
===========================================================
app.py - v4.3

Que cambio en v4.3 (resumen para mantenimiento):

 1. CORREGIDO un bug de la v4.1 que bloqueaba TODA escritura en la pestana
    Pagos (guardar pago, fijar prioridad, sincronizacion con transito y el
    boton "Activar selectores"): la pestana se creo con la cuadricula justa
    de columnas y la API de Google no la expande sola, asi que escribir el
    encabezado nuevo "Prioridad" fallaba con APIError 400. Ahora
    _asegurar_columnas agranda la cuadricula (add_cols) antes de escribir
    encabezados nuevos. Aplica a cualquier columna futura en cualquier
    pestana.

Que cambio en v4.2 (resumen para mantenimiento):

 1. Se quita el filtro "Prioridad de pago" de Estatus de Pago (pedido expreso:
    sobraba como control). La prioridad se sigue PONIENDO como numero (1-4)
    desde el formulario de admin o directo en la columna 'Prioridad' del
    Sheet, y la plataforma la sigue mostrando (distintivo con borde) y
    ordenando la lista de la 1 a la 4.

Que cambio en v4.1 (resumen para mantenimiento):

 1. Archivar ya no graba la fecha de almacen "de hoy" en silencio: el boton
    de archivar abre SIEMPRE un mini-formulario pidiendo la fecha REAL de
    entrada a almacen (por defecto hoy). Antes, ese dato inventado falseaba
    el ciclo puerto->almacen de la analitica y movia el embarque de mes en el
    historico (BASE_FECHA_RECIBIDO = "almacen").
 2. Estatus de Pago gana prioridad de pago: columna "Prioridad" (1-4) en la
    pestana Pagos, filtro en la plataforma, lista ordenada de la 1 a la 4
    (sin prioridad al final) y distintivo con borde en cada expediente. Se
    escribe a mano en el Sheet o desde el formulario de admin.

Que cambio en v4.0 (resumen para mantenimiento):

 1. El flujo del tablero baja de 5 etapas a 2: "Llegada a puerto" y "Recepcion
    y declaracion". Las etapas de pago salieron de transito; el dinero se
    maneja aparte, en el modulo de Estatus de Pago.
 2. La llegada ya no tiene columna de fecha propia. Se responde con la casilla
    "Llego? SI/NO" y, al confirmar con SI, el ETA de la fila pasa a valer como
    fecha real de llegada — asi nadie teclea la misma fecha dos veces.
 3. Vacio y "NO" NO son lo mismo: vacio es "nadie ha revisado", NO es "revise y
    sigue sin llegar". Sin esa distincion no se sabe si un embarque esta
    atrasado o simplemente desatendido.
 4. El ETA de un embarque confirmado se valida cada vez que se edita: no puede
    quedar en el futuro ni despues de la declaracion. Antes, mover el ETA a
    futuro borraba la confirmacion en silencio y el embarque retrocedia de
    etapa sin que nadie se enterara.
 5. Se elimino Costo_Por_Dia y todo el calculo de costo por demora. Nunca se
    lleno una sola celda, y la tarifa real varia por naviera, terminal, volumen
    y espacio: estimarla con un promedio daba un numero indefendible. El
    sobrecosto se observara comparando estimado contra pagado en el modulo de
    pagos, no se estimara aqui.
 6. "Recibido en almacen" deja de ser una etapa del tablero y pasa a ser la
    accion de archivar. Su fecha vive solo en la pestana "Recibido (Mes)".
 7. Al archivar se CONGELA el ETA como Fecha_Llegada_Puerto en el historico:
    si alguien mueve un ETA meses despues, los ciclos ya medidos no cambian.
 8. Modelo_Serie, OC, EE y CLIENTE / STOCK pasan a ser opcionales por
    categoria. La app solo crea la columna cuando el dato trae valor, asi
    Carga Suelta no termina con un Modelo_Serie vacio que nadie pidio.

Se conserva de versiones anteriores: escrituras por numero de fila (BLs
repetidos ya no arriesgan tocar la fila equivocada), diagrama de flujo en
HTML/CSS en vez de Plotly, alertas de cuello de botella con SLA por etapa
ajustables desde Secrets, reintentos ante 429/500/503 de Google, bloqueo
optimista en la edicion, ajustes de celular, panel "Salud de los datos",
bitacora y sesion con vida distinta por rol.

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
from pagos import panel_pagos
from analitica import panel_analitica


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
        '<div class="ant-title" style="font-size:1.7rem;">Antillana Comercial · Tránsito y Estatus de Pago</div>'
        '<div class="ant-sub">Acceso restringido.</div>'
        "</div>",
        unsafe_allow_html=True,
    )

    _, centro, _ = st.columns([1, 1.2, 1])
    with centro:
        # El PIN va dentro de un st.form a propósito: con un text_input suelto +
        # st.button, presionar Enter solo dispara un rerun y el botón nunca queda
        # "pulsado". Dentro de un formulario, Enter equivale a pulsar el submit —
        # que en celular es la diferencia entre entrar y quedarse trancado.
        with st.form("form_login", clear_on_submit=True, border=False):
            pin = st.text_input("PIN", type="password", max_chars=LARGO_PIN,
                                label_visibility="collapsed",
                                placeholder="PIN")
            entrar = st.form_submit_button("Entrar", type="primary", width="stretch")

    if not entrar:
        return

    rol, nombre = _resolver_pin(pin)
    if rol:
        _abrir_sesion(rol, nombre)
        registrar_log("Inicio de sesión", detalle=f"rol={rol}")
        st.rerun()
        return

    # Sin bloqueo por intentos fallidos (pedido expreso: fallar el PIN
    # simplemente no deja entrar, sin el castigo de 15 minutos). Lo único que
    # se queda es este freno de 1 segundo por intento fallido — imperceptible
    # para una persona, pero estira probar los 10,000 PINs posibles a horas
    # en vez de minutos.
    time.sleep(1.0)
    with centro:
        st.error("PIN incorrecto.")


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
    secciones = (["Dashboard", "Analítica", "Agregar", "Editar", "Carga masiva", "Histórico",
                  "Estatus de Pago", "Herramientas"]
                 if es_admin else ["Dashboard", "Analítica", "Histórico", "Estatus de Pago"])

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
    elif seccion == "Analítica":
        panel_analitica(datos)
    elif seccion == "Agregar":
        form_alta_manual(datos)
    elif seccion == "Editar":
        form_editar(datos)
    elif seccion == "Carga masiva":
        form_carga_masiva(datos)
    elif seccion == "Histórico":
        mostrar_historico(datos, "admin" if es_admin else "viewer")
    elif seccion == "Estatus de Pago":
        panel_pagos(datos, es_admin)
    elif seccion == "Herramientas":
        herramientas(datos)


if __name__ == "__main__":
    main()
