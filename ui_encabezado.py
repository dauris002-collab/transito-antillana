"""
ui_encabezado.py - Encabezado compartido de Tránsito y Pagos
============================================================

Solo HTML/CSS: no lee datos ni toca Sheets. Capa igual que ui_login.py:
puede importar hacia abajo (nada de logica ni sheets_io).

Fila de marca (emblema + nombre de Antillana, "+" y TecniCaribe en cápsula),
una ruta decorativa, el título, el año y el sello de actualización.

Los dos primeros logos viven en assets/emblema_antillana.png y
assets/login_tecnicaribe.png. NO se llaman assets/logo.png a propósito (ver
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
    ".eh-marca { display:flex; align-items:center; justify-content:center; gap:12px; flex-wrap:wrap; }"
    ".eh-emb { height:40px; width:auto; }"
    ".eh-nombre { font-size:0.9rem; font-weight:800; letter-spacing:0.14em; text-transform:uppercase; color:#111827; }"
    ".eh-mas { font-size:0.7rem; font-weight:700; color:#9CA3AF; }"
    ".eh-tc { display:inline-flex; align-items:center; padding:5px 12px; border:1px solid #E5E7EB; border-radius:999px; }"
    ".eh-tc img { height:26px; width:auto; display:block; }"
    ".eh-ruta { width:min(420px,100%); margin:12px auto 0 auto; display:flex; align-items:center; gap:8px; }"
    ".eh-ruta .l { flex:1; border-top:2px dashed #C7CDD6; }"
    ".eh-ruta .p { width:8px; height:8px; border-radius:50%; flex:none; }"
    ".eh-nodo { position:relative; display:flex; flex:none; }"
    ".eh-nodo b { position:absolute; top:30px; left:50%; transform:translateX(-50%); white-space:nowrap;"
    " font-size:0.68rem; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; color:#6B7280; }"
    ".eh-ruta.rotulos { margin-bottom:28px; }"
    ".eh-titulo { font-size:2.5rem; font-weight:800; letter-spacing:-0.02em; line-height:1.1; color:#111827; margin:10px 0 0 0; }"
    ".eh-sub { margin-top:8px; font-size:0.75rem; font-weight:700; letter-spacing:0.1em; text-transform:uppercase; color:#6B7280; }"
    ".eh-sello { display:inline-flex; align-items:center; gap:7px; font-size:0.76rem; color:#6B7280;"
    " margin-top:8px; flex-wrap:wrap; justify-content:center; }"
    ".eh-dot { width:8px; height:8px; border-radius:50%; background:#22C55E; display:inline-block; }"
    "@media (max-width:640px) { .eh-titulo { font-size:2rem; } .eh-nodo b { display:none; }"
    " .eh-ruta.rotulos { margin-bottom:0; } .eh-mas { display:none; } }"
    "</style>"
)


@st.cache_data(show_spinner=False)
def _uri(nombre: str) -> str:
    ruta = _ASSETS / nombre
    if not ruta.exists():
        return ""
    return "data:image/png;base64," + base64.b64encode(ruta.read_bytes()).decode()


def _marca() -> str:
    emb, tc = _uri("emblema_antillana.png"), _uri("login_tecnicaribe.png")
    img_emb = f'<img class="eh-emb" src="{emb}" alt="">' if emb else ""
    tc_html = (f'<span class="eh-tc"><img src="{tc}" alt="TecniCaribe"></span>' if tc
               else '<span class="eh-nombre">TecniCaribe</span>')
    return (f'<div class="eh-marca">{img_emb}<span class="eh-nombre">Antillana Comercial</span>'
            f'<span class="eh-mas">+</span>{tc_html}</div>')


def _punto(color: str) -> str:
    return f'<span class="p" style="background:{color}"></span>'


def _nodo(icono: str, color: str, rotulo: str = "") -> str:
    r = f"<b>{escape(rotulo)}</b>" if rotulo else ""
    return f'<span class="eh-nodo">{_SVG.format(s=22, c=color, p=icono)}{r}</span>'


_L = '<span class="l"></span>'


def _ruta_transito() -> str:
    return (f'<div class="eh-ruta">{_punto("#D80C27")}{_L}{_nodo(_BARCO, "#1F5FA8")}{_L}'
            f'{_nodo(_AVION, "#B45309")}{_L}{_punto("#111827")}</div>')


def _ruta_pagos() -> str:
    c = "#1F5FA8"
    return (f'<div class="eh-ruta rotulos">{_punto("#D80C27")}{_L}{_nodo(_BARCO, c, "Llegada")}{_L}'
            f'{_nodo(_FACTURA, c, "Factura")}{_L}{_nodo(_APROBADO, c, "Aprobación")}{_L}'
            f'{_nodo(_MONEDA, c, "Pagado")}{_L}{_punto("#9CA3AF")}</div>')


def encabezado_html(titulo: str, ruta: str, anio: int, sello: str) -> str:
    """ruta: 'transito' o 'pagos'. El sello ya viene armado por quien llama."""
    bloque_ruta = _ruta_pagos() if ruta == "pagos" else _ruta_transito()
    return (f'{_CSS}<div class="eh">{_marca()}{bloque_ruta}'
            f'<div class="eh-titulo">{escape(titulo)}</div>'
            f'<div class="eh-sub">Logística e Importaciones {anio}</div>'
            f'<div class="eh-sello"><span class="eh-dot"></span> {escape(sello)}</div></div>')
