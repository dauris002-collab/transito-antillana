import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
for p in (str(RAIZ), str(RAIZ / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

SECRETS = {
    "SHEET_ID": "falso", "TALLER_SHEET_ID": "falso-taller",
    "gcp_service_account": {"type": "service_account"},
    "pins": {"1111": {"nombre": "Admin Prueba", "rol": "admin"},
             "2222": {"nombre": "Viewer Prueba", "rol": "viewer"},
             "333333": {"nombre": "Taller Prueba", "rol": "taller"}},
}


@pytest.fixture
def libros():
    """Sheet falso nuevo y cachés limpias para cada prueba."""
    import streamlit as st
    import datos
    import estado_prueba
    st.cache_data.clear()
    st.cache_resource.clear()
    estado_prueba.LIBRO = datos.libro_principal()
    estado_prueba.TALLER = datos.libro_taller()
    estado_prueba.ORDENES = datos.libro_ordenes()
    yield estado_prueba
    st.cache_data.clear()
    st.cache_resource.clear()


SECRETS_ORDENES = {"ORDENES_SHEET_ID": "falso-ordenes",
                   "pins": {**SECRETS["pins"], "444444": {"nombre": "David Tejeda", "rol": "compras"}}}


def abrir_app(pin="1111", seccion=None, timeout=120, ordenes=False):
    """Levanta la app completa, entra con el PIN y (opcional) cambia de sección.
    `ordenes=True` agrega el secret ORDENES_SHEET_ID y el PIN 444444 (rol compras)."""
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(RAIZ / "tests" / "harness_app.py"), default_timeout=timeout)
    for k, v in {**SECRETS, **(SECRETS_ORDENES if ordenes else {})}.items():
        at.secrets[k] = v
    at.run()
    at.text_input[0].set_value(pin)
    at.button[0].click()
    at.run()
    if seccion:
        ir(at, seccion)
    return at


def ir(at, seccion):
    at.session_state["seccion"] = seccion
    at.run()
    return at


def boton(at, texto, i=0):
    return [b for b in at.button if texto in str(b.label)][i]


def sin_errores(at):
    """Ni excepción en pantalla ni excepción tragada y mostrada como mensaje."""
    assert not at.exception, [str(e.value)[:400] for e in at.exception]
    tragadas = [e.value[:300] for e in at.error
                if any(t in e.value for t in ("Error inesperado", "Error en la vista", "no pudo cargarse"))]
    assert not tragadas, tragadas


def _acelerar_apptest():
    """AppTest sondea cada milisegundo recorriendo TODOS los eventos de la corrida
    (decenas de miles en esta app) y le quita la CPU al guion. El evento de cierre
    siempre es el último: basta mirar los últimos. Solo afecta a las pruebas."""
    from streamlit.runtime.scriptrunner import ScriptRunnerEvent
    from streamlit.testing.v1.local_script_runner import LocalScriptRunner
    LocalScriptRunner.script_stopped = lambda self: ScriptRunnerEvent.SHUTDOWN in self.events[-5:]


_acelerar_apptest()


@pytest.fixture(autouse=True, scope="session")
def _sin_esperas():
    """Los reintentos ante 429/5xx esperan 2-4-8-16 s: en pruebas se saltan."""
    import time
    import types
    import sheets_io
    sheets_io.time = types.SimpleNamespace(sleep=lambda *_: None, time=time.time)
    yield
