"""
ui_login.py - Presentacion de la pantalla de acceso (portada B2)
=================================================================

Solo HTML/CSS: no toca la sesion, el PIN ni Google Sheets (eso sigue en app.py).
Capa: igual que ui_componentes.py, solo puede importar hacia abajo.

Los logos viven en assets/login_antillana.png y assets/login_tecnicaribe.png.
Los nombres NO son assets/logo.png a proposito: ui_componentes._logo_base64()
busca ese nombre para el encabezado del dashboard y lo cambiaria.
Si un logo falta, se muestra el nombre de la empresa en texto: el login nunca
se rompe por una imagen.

Ojo al editar: st.markdown con HTML se rompe si el texto tiene lineas en blanco
o sangria de 4 espacios (lo trata como codigo). Por eso cada bloque va en una
sola cadena, sin saltos de linea.
"""

from __future__ import annotations

import base64
from pathlib import Path

import streamlit as st

_ASSETS = Path(__file__).parent / "assets"
_FUENTE = "'Barlow Condensed','Arial Narrow',sans-serif"


@st.cache_data(show_spinner=False)
def _logo_uri(nombre: str) -> str:
    ruta = _ASSETS / nombre
    if not ruta.exists():
        return ""
    return "data:image/png;base64," + base64.b64encode(ruta.read_bytes()).decode()


def _logo(nombre: str, alt: str, clase: str) -> str:
    uri = _logo_uri(nombre)
    if not uri:
        return f'<span class="lg-alt">{alt}</span>'
    return f'<img class="{clase}" src="{uri}" alt="{alt}">'


LOGIN_CSS = (
    "<style>"
    "@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&display=swap');"
    ".stApp { background:#F3F4F6; }"
    ".lg-band { display:flex; align-items:center; justify-content:space-between; gap:1rem;"
    " background:#FFFFFF; border-bottom:4px solid #D80C27; border-radius:14px;"
    " padding:0.8rem 1.6rem; margin:0.4rem 0 1.4rem 0; }"
    ".lg-main { height:clamp(64px, 11vw, 120px); width:auto; }"
    ".lg-sec { height:clamp(22px, 3vw, 36px); width:auto; }"
    ".lg-alt { font-weight:700; color:#1F2430; }"
    f".lg-tag {{ font-family:{_FUENTE}; font-weight:600; font-size:1.05rem;"
    " letter-spacing:0.16em; text-transform:uppercase; color:#C40A22; }"
    f".lg-h1 {{ font-family:{_FUENTE}; font-weight:700;"
    " font-size:clamp(2.4rem, 5vw, 4rem); line-height:0.95; color:#1F2430;"
    " margin:0.5rem 0 1rem 0; }"
    ".lg-h1 span { color:#C40A22; }"
    ".lg-ruta { width:100%; max-width:700px; height:auto; display:block; overflow:visible; }"
    f".lg-acceso {{ font-family:{_FUENTE}; font-weight:600; font-size:1rem;"
    " letter-spacing:0.14em; text-transform:uppercase; color:#6D6E71; }"
    f".lg-pin-titulo {{ font-family:{_FUENTE}; font-weight:700; font-size:2.1rem;"
    " line-height:1; color:#1F2430; margin:0.3rem 0 0.8rem 0; }"
    ".st-key-login_card { background:#FFFFFF; border-radius:18px;"
    " box-shadow:0 24px 60px rgba(31,36,48,0.14); }"
    ".st-key-login_card [data-testid='stBaseButton-primaryFormSubmit'],"
    ".st-key-login_card button[kind='primaryFormSubmit']"
    " { background:#D80C27 !important; border-color:#D80C27 !important; color:#FFFFFF !important; }"
    ".st-key-login_card [data-testid='stBaseButton-primaryFormSubmit']:hover,"
    ".st-key-login_card button[kind='primaryFormSubmit']:hover"
    " { background:#B50A20 !important; border-color:#B50A20 !important; }"
    # En celular el PIN tiene que quedar a la vista sin bajar: se oculta el dibujo.
    "@media (max-width: 640px) { .lg-ruta { display:none; } .lg-h1 { font-size:2.3rem; } }"
    "</style>"
)

_TXT = f'font-family="{_FUENTE}" font-weight="700" font-size="24" letter-spacing="3"'
_TXT2 = f'font-family="{_FUENTE}" font-weight="600" font-size="22" letter-spacing="2" fill="#4A4F5C"'

_RUTA = (
    '<svg class="lg-ruta" viewBox="-70 0 1040 300" role="img" '
    'aria-label="Ruta aérea y marítima desde el puerto de origen hasta Santo Domingo">'
    '<path d="M70 250 C240 290 520 290 690 230" fill="none" stroke="#6D6E71" stroke-width="4" '
    'stroke-linecap="round" stroke-dasharray="1 10"/>'
    '<path d="M70 250 C200 60 540 20 690 230" fill="none" stroke="#D80C27" stroke-width="4" '
    'stroke-linecap="round" stroke-dasharray="14 9"/>'
    '<circle cx="70" cy="250" r="9" fill="#FFFFFF" stroke="#1F2430" stroke-width="4"/>'
    '<circle cx="690" cy="230" r="18" fill="none" stroke="#D80C27" stroke-width="2" opacity="0.4"/>'
    '<circle cx="690" cy="230" r="9" fill="#D80C27"/>'
    '<g transform="translate(360 76) rotate(-4)"><path d="M2 12 L24 2 L16 24 L11 15 Z" fill="#D80C27"/></g>'
    '<g transform="translate(380 268) scale(1.4)">'
    '<rect x="-14" y="-9" width="9" height="9" fill="#D80C27"/>'
    '<rect x="-4" y="-9" width="9" height="9" fill="#F7941D"/>'
    '<rect x="6" y="-9" width="9" height="9" fill="#6D6E71"/>'
    '<path d="M-20 0 H20 L14 10 H-14 Z" fill="#1F2430"/></g>'
    f'<text x="372" y="50" text-anchor="middle" {_TXT} fill="#C40A22">AÉREO</text>'
    f'<text x="380" y="232" text-anchor="middle" {_TXT} fill="#4A4F5C">MARÍTIMO</text>'
    f'<text x="52" y="258" text-anchor="end" {_TXT2}>ORIGEN</text>'
    f'<text x="716" y="238" {_TXT2}>SANTO DOMINGO</text>'
    "</svg>"
)


def html_cabecera() -> str:
    """Franja blanca: Antillana grande (principal), TecniCaribe pequena."""
    return (
        '<div class="lg-band">'
        + _logo("login_antillana.png", "Antillana Comercial", "lg-main")
        + _logo("login_tecnicaribe.png", "TecniCaribe", "lg-sec")
        + "</div>"
    )


def html_portada() -> str:
    """Columna izquierda: rotulo, titular y la ruta aereo/maritimo."""
    return (
        '<div class="lg-tag">Logística e Importaciones</div>'
        '<div class="lg-h1" role="heading" aria-level="1">Tránsito<br>y Estatus de <span>Pago</span></div>'
        + _RUTA
    )


def html_tarjeta() -> str:
    """Titulo de la tarjeta del PIN (el formulario lo arma app.py)."""
    return (
        '<div class="lg-acceso">Acceso restringido</div>'
        '<div class="lg-pin-titulo">Ingresa tu PIN</div>'
    )
