"""
ui_encabezado.py - Encabezado compartido de Tránsito y Pagos
============================================================

Solo HTML/CSS: no lee datos ni toca Sheets. Capa igual que ui_login.py:
puede importar hacia abajo (nada de logica ni sheets_io).

Fila de marca (emblema + nombre de Antillana, TecniCaribe y Motor Ibérico, con separadores),
una ruta decorativa, el título, el año y el sello de actualización.

Los logos viven en assets/emblema_antillana.png,
assets/login_tecnicaribe.png y assets/motor_iberico.png. NO se llaman assets/logo.png a propósito (ver
ui_login.py). Si falta una imagen, se muestra el nombre en texto: el
encabezado nunca se rompe por un archivo.

La ruta de Pagos es DECORATIVA: las cuatro etapas van en un solo color a
propósito. Pintar unas en verde y otras en gris diría "este avance es real"
y el encabezado no sabe en qué etapa va ningún expediente.

Ojo al editar: st.markdown con HTML se rompe si hay líneas en blanco o sangría
de 4 espacios; cada bloque va en una sola cadena.
"""

from __future__ import annotations

import base64
import hashlib
from html import escape
from pathlib import Path

import streamlit as st

_ASSETS = Path(__file__).parent / "assets"

_SVG = ('<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{p}</svg>')
_BARCO = '<path d="M3 17l2 3h14l2-3z"/><path d="M6 17V11h12v6"/><path d="M9 11V7h6v4"/>'
_AVION = '<path d="M2 12 L22 3 L15 22 L11 14 Z"/>'
_FACTURA = '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>'
_APROBADO = '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>'
_MONEDA = ('<circle cx="12" cy="12" r="9"/><path d="M14.5 9.5c-.5-1-1.5-1.5-2.5-1.5-1.5 0-2.5.8-2.5 2s1 1.7 '
           '2.5 2 2.5.8 2.5 2-1 2-2.5 2c-1 0-2-.5-2.5-1.5M12 6.5V8M12 16v1.5"/>')

_CSS = (
    "<style>"
    ".eh { text-align:center; margin:0 0 1.1rem 0; }"
    ".eh-marca { display:flex; align-items:center; justify-content:center; gap:12px 24px; flex-wrap:wrap; }"
    ".eh-par { display:flex; align-items:center; gap:12px; }"
    ".eh-emb { height:clamp(46px, 5vw, 54px); width:auto; }"
    ".eh-nombre { font-size:clamp(0.9rem, 1.3vw, 1rem); font-weight:800; letter-spacing:0.14em;"
    " text-transform:uppercase; color:#111827; }"
    ".eh-vs { width:1px; height:30px; background:#D1D5DB; }"
    ".eh-tc { height:clamp(30px, 3vw, 36px); width:auto; display:block; }"
    ".eh-mi { height:clamp(27px, 2.7vw, 34px); width:auto; display:block; }"
    ".eh-ruta { width:min(420px,100%); margin:12px auto 0 auto; display:flex; align-items:center; gap:8px; }"
    ".eh-ruta .l { flex:1; border-top:2px dashed #C7CDD6; }"
    ".eh-ruta .p { width:8px; height:8px; border-radius:50%; flex:none; }"
    ".eh-nodo { position:relative; display:flex; flex:none; }"
    ".eh-nodo b { position:absolute; top:34px; left:50%; transform:translateX(-50%); white-space:nowrap;"
    " font-size:0.68rem; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; color:#6B7280; }"
    ".eh-ruta.rotulos { margin-bottom:32px; }"
    ".eh-titulo { font-size:2.5rem; font-weight:800; letter-spacing:-0.02em; line-height:1.1; color:#111827; margin:10px 0 0 0; }"
    ".eh-sub { margin-top:8px; font-size:0.75rem; font-weight:700; letter-spacing:0.1em; text-transform:uppercase; color:#6B7280; }"
    ".eh-sello { display:inline-flex; align-items:center; gap:7px; font-size:0.76rem; color:#6B7280;"
    " margin-top:8px; flex-wrap:wrap; justify-content:center; }"
    ".eh-dot { width:8px; height:8px; border-radius:50%; background:#22C55E; display:inline-block; }"
    "@media (max-width:900px) { .eh-vs { display:none; } .eh-marca { flex-direction:column; } }"
    "@media (max-width:640px) { .eh-titulo { font-size:2rem; } .eh-nodo b { display:none; }"
    " .eh-ruta.rotulos { margin-bottom:0; } }"
    "</style>"
)


@st.cache_data(show_spinner=False)
def _uri(nombre: str) -> str:
    ruta = _ASSETS / nombre
    if not ruta.exists():
        return ""
    return "data:image/png;base64," + base64.b64encode(ruta.read_bytes()).decode()


def _marca() -> str:
    emb, tc, mi = _uri("emblema_antillana.png"), _uri("login_tecnicaribe.png"), _uri("motor_iberico.png")
    img_emb = f'<img class="eh-emb" src="{emb}" alt="">' if emb else ""
    tc_html = f'<img class="eh-tc" src="{tc}" alt="TecniCaribe">' if tc else '<span class="eh-nombre">TecniCaribe</span>'
    mi_html = f'<img class="eh-mi" src="{mi}" alt="Motor Ibérico">' if mi else '<span class="eh-nombre">Motor Ibérico</span>'
    return (f'<div class="eh-marca"><div class="eh-par">{img_emb}<span class="eh-nombre">Antillana Comercial</span></div>'
            f'<span class="eh-vs"></span>{tc_html}<span class="eh-vs"></span>{mi_html}</div>')


def _punto(color: str) -> str:
    return f'<span class="p" style="background:{color}"></span>'


def _nodo(icono: str, color: str, rotulo: str = "") -> str:
    r = f"<b>{escape(rotulo)}</b>" if rotulo else ""
    return f'<span class="eh-nodo">{_SVG.format(s=26, c=color, p=icono)}{r}</span>'


_L = '<span class="l"></span>'


def _ruta_transito() -> str:
    return (f'<div class="eh-ruta">{_punto("#D80C27")}{_L}{_nodo(_BARCO, "#1F5FA8")}{_L}'
            f'{_nodo(_AVION, "#B45309")}{_L}{_punto("#111827")}</div>')


def _ruta_pagos() -> str:
    c = "#1F5FA8"
    return (f'<div class="eh-ruta rotulos">{_punto("#D80C27")}{_L}{_nodo(_BARCO, c, "Llegada")}{_L}'
            f'{_nodo(_FACTURA, c, "Factura")}{_L}{_nodo(_APROBADO, c, "Aprobación")}{_L}'
            f'{_nodo(_MONEDA, c, "Pagado")}{_L}{_punto("#9CA3AF")}</div>')


def encabezado_html(titulo: str, ruta: str, anio: int, sello: str, subtitulo_extra: str = "") -> str:
    """ruta: 'transito', 'pagos' o 'taller' (sin ruta). El sello ya viene armado por quien llama."""
    bloque_ruta = "" if ruta == "taller" else (_ruta_pagos() if ruta == "pagos" else _ruta_transito())
    return (f'{_CSS}<div class="eh">{_marca()}{bloque_ruta}'
            f'<div class="eh-titulo">{escape(titulo)}</div>'
            f'<div class="eh-sub">Logística e Importaciones {anio}{escape(subtitulo_extra)}</div>'
            f'<div class="eh-sello"><span class="eh-dot"></span> {escape(sello)}</div></div>')


def restaurar_filtro(clave: str, opciones: list, defecto="Todos") -> None:
    """Llamar ANTES de crear el selectbox `clave`. Streamlit borra el estado de un widget
    cuando una corrida termina en st.rerun() sin dibujarlo (botones ↻ Actualizar), y el
    filtro volvía a 'Todas'/'Todos'. Se repone desde `clave + "_ult"`, que no es widget y
    sobrevive. Si el valor ya no está en `opciones`, cae a `defecto` (evita la excepción
    de selectbox con un valor inexistente)."""
    if clave not in st.session_state:
        st.session_state[clave] = st.session_state.get(clave + "_ult", defecto)
    if st.session_state[clave] not in opciones:
        st.session_state[clave] = defecto


def recordar_filtro(clave: str) -> None:
    """Llamar DESPUÉS de crear el selectbox `clave`: guarda su valor en `clave + "_ult"`."""
    st.session_state[clave + "_ult"] = st.session_state[clave]


def valor_previo(clave: str, opciones: list, defecto="Todas"):
    """Valor vigente de un filtro creado con `selectbox_sincronizado`, leído ANTES de
    dibujarlo (para calcular lo que depende de él). Toma lo que el usuario dejó en el
    widget; si el widget ya no existe (↻ Actualizar hace st.rerun() antes de dibujarlo y
    Streamlit borra su estado) cae a la copia `clave + "_ult"`. Si el valor ya no está
    en `opciones`, devuelve `defecto`."""
    clave_widget = st.session_state.get(clave + "_wk")
    if clave_widget and clave_widget in st.session_state:
        valor = st.session_state[clave_widget]
    else:
        valor = st.session_state.get(clave + "_ult", defecto)
    return valor if valor in opciones else defecto


def selectbox_sincronizado(etiqueta: str, opciones: list, clave: str, formato, valor, **kw):
    """selectbox cuyas etiquetas llevan números que cambian (p. ej. 'Todas · 8').

    Problema que resuelve: con la clave fija, el navegador conserva el texto que ya
    mostraba el valor elegido y NO refresca su etiqueta cuando cambia solo el conteo
    ('Todas · 13' seguía diciendo 13 con 8 filas). Aquí la clave del widget lleva una
    firma de las etiquetas: si cambian, nace un widget nuevo con `valor` ya elegido y se
    ve el texto correcto. Además guarda el valor en `clave + "_ult"` (sobrevive a
    ↻ Actualizar). Usar `valor_previo` para leer el valor antes de dibujar."""
    firma = hashlib.md5("|".join(str(formato(o)) for o in opciones).encode("utf-8")).hexdigest()[:8]
    clave_widget = f"{clave}__{firma}"
    elegido = st.selectbox(etiqueta, opciones, index=opciones.index(valor), key=clave_widget,
                           format_func=formato, **kw)
    st.session_state[clave + "_wk"] = clave_widget
    st.session_state[clave + "_ult"] = elegido
    return elegido


def limpiar_filtro(clave: str) -> None:
    """Borra por completo un filtro creado con `selectbox_sincronizado` (widget actual,
    su copia `_ult` y el puntero `_wk`): lo que necesita un botón «Limpiar filtros»."""
    clave_widget = st.session_state.pop(clave + "_wk", None)
    if clave_widget:
        st.session_state.pop(clave_widget, None)
    st.session_state.pop(clave + "_ult", None)


def avisar_tras_recarga(texto: str, tipo: str = "ok") -> None:
    """Deja un aviso para la corrida SIGUIENTE. Un st.success() escrito justo antes de
    st.rerun() nunca llega a verse (la página se redibuja al instante), y quien guardaba
    se quedaba sin saber si había funcionado. tipo: "ok" (confirmación) o "aviso"
    (algo quedó a medias y hay que leerlo). Lo dibuja mostrar_avisos_pendientes()."""
    st.session_state.setdefault("_avisos_pendientes", []).append((tipo, str(texto)))


def mostrar_avisos_pendientes() -> None:
    """Llamar una vez por corrida, arriba de la página. Las confirmaciones salen como
    aviso flotante (se ve aunque la pantalla esté desplazada hacia abajo); los avisos
    de algo a medias quedan además fijos arriba hasta la siguiente acción."""
    for tipo, texto in st.session_state.pop("_avisos_pendientes", []):
        if tipo == "ok":
            st.toast(texto, icon="✅")
        else:
            st.toast(texto, icon="⚠️")
            st.warning(texto)

