"""
ui_login.py - Presentacion de la pantalla de acceso (portada B)
================================================================

Solo HTML/CSS: no toca la sesion, el PIN ni Google Sheets (eso sigue en app.py).
Capa: igual que ui_componentes.py, solo puede importar hacia abajo.

Diseno: mitad izquierda gris con el titular y la ruta aereo/maritimo; mitad
derecha blanca con los logos (Antillana principal; TecniCaribe y Motor Iberico debajo, pequenos) y el PIN.

Los logos viven en assets/login_antillana.png, assets/login_tecnicaribe.png y
assets/motor_iberico.png (recorte de un membrete fotografiado: pedir el original).
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
# La mitad blanca arranca en el hueco entre las dos columnas (1.5 : 1, gap grande).
_CORTE = "59%"


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
    f".lg-tag {{ font-family:{_FUENTE}; font-weight:600; font-size:1.05rem;"
    " letter-spacing:0.16em; text-transform:uppercase; color:#C40A22; }"
    f".lg-h1 {{ font-family:{_FUENTE}; font-weight:700;"
    " font-size:clamp(2.6rem, 8vw, 6.5rem); line-height:0.92; color:#1F2430;"
    " margin:0.6rem 0 1.2rem 0; }"
    ".lg-h1 span { color:#C40A22; }"
    ".lg-ruta { width:100%; max-width:700px; height:auto; display:block; overflow:visible; }"
    ".lg-logos { display:flex; flex-direction:column; align-items:center; gap:1.2rem; }"
    ".lg-main { height:clamp(110px, 14vw, 200px); width:auto; }"
    ".lg-sec { height:clamp(24px, 2.6vw, 34px); width:auto; }"
    ".lg-mi { height:clamp(23px, 2.4vw, 31px); width:auto; }"
    ".lg-asoc { display:flex; align-items:center; justify-content:center; gap:12px 22px; flex-wrap:wrap; }"
    ".lg-vs { width:1px; height:28px; background:#D1D5DB; }"
    ".lg-alt { font-weight:700; color:#1F2430; }"
    ".lg-linea { width:48px; height:3px; border-radius:2px; background:#D80C27; }"
    ".lg-hr { height:1px; background:#E3E6EB; margin:1.6rem 0 1.2rem 0; }"
    f".lg-acceso {{ display:flex; align-items:center; gap:8px; font-family:{_FUENTE};"
    " font-weight:600; font-size:1rem; letter-spacing:0.14em; text-transform:uppercase;"
    " color:#6D6E71; margin-bottom:0.4rem; }"
    ".st-key-login_card { background:#FFFFFF; border-radius:18px; padding:1.4rem;"
    " box-shadow:0 24px 60px rgba(31,36,48,0.14); }"
    ".st-key-login_card [data-testid='stBaseButton-primaryFormSubmit'],"
    ".st-key-login_card button[kind='primaryFormSubmit']"
    " { background:#D80C27 !important; border-color:#D80C27 !important; color:#FFFFFF !important; }"
    ".st-key-login_card [data-testid='stBaseButton-primaryFormSubmit']:hover,"
    ".st-key-login_card button[kind='primaryFormSubmit']:hover"
    " { background:#B50A20 !important; border-color:#B50A20 !important; }"
    # Escritorio: fondo partido gris | blanco de borde a borde y el panel sin tarjeta.
    "@media (min-width: 641px) { .stApp { background:linear-gradient(90deg,"
    f" #F3F4F6 {_CORTE}, #E3E6EB {_CORTE}, #E3E6EB calc({_CORTE} + 1px),"
    f" #FFFFFF calc({_CORTE} + 1px)); }}"
    " .st-key-login_card { background:transparent; box-shadow:none; padding:0; } }"
    # Celular: el PIN tiene que quedar a la vista sin bajar, se oculta el dibujo.
    "@media (max-width: 1100px) { .lg-vs { display:none; } }"
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

_CANDADO = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#D80C27" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>'
)


def html_portada() -> str:
    """Columna izquierda: rotulo, titular en 3 lineas y la ruta aereo/maritimo."""
    return (
        '<div class="lg-tag">Logística e Importaciones</div>'
        '<div class="lg-h1" role="heading" aria-level="1">Tránsito<br>y Estatus<br>de <span>Pago</span></div>'
        + _RUTA
    )


def html_tarjeta() -> str:
    """Parte alta del panel derecho: logos (Antillana principal) y rotulo de acceso.
    El formulario del PIN lo arma app.py justo debajo."""
    return (
        '<div class="lg-logos">'
        + _logo("login_antillana.png", "Antillana Comercial", "lg-main")
        + "</div>"
        '<div class="lg-hr"></div>'
        '<div class="lg-asoc">'
        + _logo("login_tecnicaribe.png", "TecniCaribe", "lg-sec")
        + '<span class="lg-vs"></span>'
        + _logo("motor_iberico.png", "Motor Ibérico", "lg-mi")
        + "</div>"
        '<div class="lg-hr"></div>'
        f'<div class="lg-acceso">{_CANDADO}<span>Acceso restringido</span></div>'
    )
