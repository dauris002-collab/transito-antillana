"""
ordenes.py - Sección «Órdenes de compra» (piloto)
=========================================================

Seguimiento de las órdenes de compra internacionales para que Taller/Repuesto vea en qué va cada
una y quién la lleva, sin preguntarle al analista.

FUENTE: un Sheet APARTE (secret `ORDENES_SHEET_ID`, misma cuenta de servicio, como Editor) con dos
pestañas que la app lee:
 - `Órdenes`  : una fila por «Documento compras». Proveedor, Responsable, Empresa, Estatus de la
                orden (ABIERTA / COMPLETA / CANCELADA), comentario para Taller y el sello de la
                última actualización.
 - `Desglose` : una fila por LÍNEA (Documento compras + Posición). Es el formato del Excel de
                Repuesto: material, cantidades, estatus de la línea, transportista, fechas.
Las pestañas `Instrucciones` y `Listas` del libro no se leen.

REGLAS (decididas el 24 nov 2026):
 - Llave de una línea = Documento compras + Posición. Una celda sin dato se deja VACÍA («Lo Llena
   Repuesto» y «PENDIENTE» se tratan como vacío).
 - Una línea COMPLETO o CANCELADO no se ve. Una orden COMPLETA o CANCELADA no se ve, con todas sus
   líneas. El estatus lo pone una persona: la app nunca cierra una orden sola (solo avisa al admin).
 - Pendiente = pedida - despachada (se calcula; la columna del Sheet es solo para verla).
   «Backorder / parcial» = línea en BACKORDER, o ya despachada en parte (despachada > 0 y pendiente > 0).
 - «Sin actualizar» cuenta desde FECHA ACTUALIZACIÓN de la orden (sin sello, desde la fecha del
   documento más antigua). Más de DIAS_SIN_ACTUALIZAR días sale en rojo.
 - Fechas siempre día/mes/año. Un TEXTO con día y mes ambos <= 12 (05/11/2026) se rechaza en la
   carga masiva en vez de adivinarlo; fechas reales de Excel/Sheets no tienen ese problema.

CARGA: (1) directo en el Sheet; (2) masiva por Excel/CSV desde la app (rol admin o compras): vista
previa y, al guardar, la comparación se repite EN VIVO bajo candado. Nunca borra: una celda vacía
del Excel no vacía la del Sheet, y lo que Repuesto ya llenó (solicitud, factura, material antiguo,
cliente) no se pisa. Toda escritura va por posición de encabezado, en RAW, y queda en la bitácora.

Capa: como entrega_provisional.py, importa de sheets_io hacia abajo y SOLO nombres que ya existen
(CONTEXT.md, 17 nov 2026: un módulo nuevo que importa nombres nuevos de sheets_io tumba la app con
ImportError tras un push). Constantes, lectura y caché propias: un fallo aquí no toca Tránsito,
Pagos ni Taller.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import threading
from datetime import date, datetime
from html import escape as _esc
from numbers import Real

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

import sheets_io as sio
from sheets_io import (
    EMPRESA_ANTILLANA, EMPRESA_MOTOR_IBERICO, EMPRESA_TECNICARIBE, EPOCH_SHEETS, _codigo_api, _con_reintento,
    _norm, _norm_encabezado, a_numero, ahora_rd, hoy_rd, parsear_fecha,
)
from ui_encabezado import avisar_tras_recarga, encabezado_html, recordar_filtro, restaurar_filtro

NOMBRE_SECCION = "Órdenes de compra"
PESTANA_ORDENES = "Órdenes"
PESTANA_DESGLOSE = "Desglose"   # LEGADO: si existe, se sigue leyendo (sin responsable propio)
PESTANA_CARGA = "Carga"          # nombre de la hoja de la plantilla de carga masiva
PESTANA_LISTAS = "Listas"       # su columna RESPONSABLE dice qué pestañas de responsable hay
ROLES_ESCRITURA = ("admin", "compras")
CATEGORIA_LOG = "Órdenes de compra"

DIAS_SIN_ACTUALIZAR = 7          # más de 7 días sin actualizar: rojo
MAX_FILAS = 5000                 # tope de filas que la app lee por pestaña
TTL = 120                        # igual que Taller / Entrega Provisional
MAX_BOTONES_TARJETA = 40         # números de orden clicables por responsable
MAX_FILAS_TABLA = 300            # filas que se dibujan por tabla (la descarga CSV trae todas)

# ---------------------------------------------------------------------------
# COLUMNAS: (clave interna, encabezado canónico, alias aceptados). Se comparan sin acentos,
# mayúsculas, espacios, guion bajo ni barra (`_norm_encabezado`), así que el Sheet puede escribirlos
# como quiera.
# ---------------------------------------------------------------------------
COLUMNAS_D = [
    ("sol", "FECHA DE SOLICITUD", ("Fecha Solicitud", "Fecha de la solicitud")),
    ("nosol", "NO. Solicitud de pedido", ("No Solicitud", "Solicitud de pedido", "Solicitud")),
    ("solicitante", "Solicitante", ("Solicitado por",)),
    ("cliente", "Cliente", ()),
    ("fdoc", "Fecha documento", ("Fecha de documento", "Fecha doc", "Fecha doc.")),
    ("pos", "Posición", ("Pos", "Pos.", "Posicion", "Posición de pedido")),
    ("doc", "Orden de Compras", ("Documento compras", "Doc. compras", "Doc compras", "Documento de compras", "Pedido",
                                 "No. Orden", "Orden", "Orden de compra", "Documento")),
    ("mat", "Material", ("Numero de Material", "No. Material")),
    ("matant", "MAT. ANTIGUO", ("Material antiguo", "Mat Antiguo")),
    ("nparte", "No. Parte", ("Numero de Parte", "Parte")),
    ("texto", "Texto breve", ("Descripción", "Descripcion", "Texto")),
    ("pedida", "Cantidad de pedido", ("Cantidad pedido", "Cant. pedido", "Cantidad pedida", "Pedida")),
    ("desp", "Cantidad Despachada", ("Despachada", "Cant. despachada", "Cantidad Enviada")),
    ("pend", "PENDIENTE / BACKORDER", ("Pendiente", "Backorder")),
    ("estatus", "ESTATUS", ("Estatus linea", "Estado")),
    ("fact", "No. Factura", ("Factura", "No Factura", "Numero de Factura")),
    # El 2.º «ESTATUS» de la hoja (DESPACHADO / COMPLETA / PARCIAL): `_mapa_encabezados` da el 1.º a `estatus` y el
    # 2.º a esta clave porque el primero ya quedó usado.
    ("estatus2", "ESTATUS", ()),
    # «ALMACÉN» (1.º) = el courier/consolidador a donde va la mercancía (INTRANS, LOCCITEC…).
    ("transp", "ALMACÉN", ("TRANSPORTISTA", "Courier", "Via")),
    ("embarque", "FECHA DE EMBARQUE", ("Fecha Embarque",)),
    ("eta", "FECHA DE LLEGADA - ETA", ("ETA", "Fecha de llegada", "Fecha Llegada")),
    ("puerto", "PUERTO / Aeropuerto", ("PUERTO", "Aeropuerto")),
    ("almacen", "ALMACÉN NUESTRO", ("Almacen nuestro", "Almacén", "Almacen")),
    ("coment", "COMENTARIOS", ("COMENTARIO", "Observaciones")),
]
OPCIONALES_D = {"nparte", "estatus2"}   # si el Sheet no las trae no se avisa (solo falla si se intenta escribirlas)
ESTATUS_DESPACHO = ["DESPACHADO", "COMPLETA", "PARCIAL"]
COLUMNAS_O = [
    ("doc", "DOCUMENTO COMPRAS", ("Documento compras", "Orden de Compras", "No. Orden", "Orden", "Documento")),
    ("prov", "PROVEEDOR", ("Suplidor", "Nombre del proveedor")),
    ("resp", "RESPONSABLE", ("Analista", "Analista de compras")),
    ("emp", "EMPRESA", ()),
    ("est", "ESTATUS ORDEN", ("Estatus de la orden",)),
    ("coment", "COMENTARIO PARA TALLER", ("Comentario Taller", "Comentarios para Taller")),
    ("fecha", "FECHA ACTUALIZACIÓN", ("Fecha Actualizacion", "Actualizado")),
    ("por", "ACTUALIZADO POR", ("Actualizado_Por",)),
]
# Columnas del ARCHIVO de carga masiva: el Desglose, más los datos de la orden que puede traer.
_EXTRA_ARCHIVO = [("prov", "PROVEEDOR", ("Suplidor", "Nombre del proveedor", "Proveedor/Centro suministrador")),
                  ("resp", "RESPONSABLE", ("Analista", "Analista de compras")),
                  ("emp", "EMPRESA", ())]
COLUMNAS_ARCHIVO = COLUMNAS_D + _EXTRA_ARCHIVO

CLAVES_FECHA = {"sol", "fdoc", "embarque", "eta"}
CLAVES_NUM = {"pedida", "desp"}
ESCRIBIBLES_D = [k for k, _, _ in COLUMNAS_D if k not in ("doc", "pos", "pend")]
# Lo llena Repuesto: si el Sheet ya tiene el dato, la carga masiva no lo pisa.
PROTEGIDAS = {"sol", "nosol", "fact", "matant", "cliente"}
NOMBRE_D = {k: n for k, n, _ in COLUMNAS_D}
NOMBRE_O = {k: n for k, n, _ in COLUMNAS_O}

ESTATUS_LINEA = ["COLOCADA", "CONFIRMADA", "BACKORDER", "DESPACHADO", "EN TRÁNSITO", "EN PUERTO",
                 "COMPLETO", "CANCELADO"]
CERRADOS_LINEA = {"COMPLETO", "CANCELADO"}
AVANZADOS = {"DESPACHADO", "EN TRÁNSITO", "EN PUERTO"}
ESTATUS_ORDEN = ["ABIERTA", "COMPLETA", "CANCELADA"]
CERRADOS_ORDEN = {"COMPLETA", "CANCELADA"}
EMPRESAS = [EMPRESA_ANTILLANA, EMPRESA_TECNICARIBE, EMPRESA_MOTOR_IBERICO]
SIN_ESTATUS = "SIN ESTATUS"
SIN_ASIGNAR = "Sin asignar"

_ALIAS_ESTATUS = {
    "colocada": "COLOCADA", "colocado": "COLOCADA",
    "confirmada": "CONFIRMADA", "confirmado": "CONFIRMADA",
    "backorder": "BACKORDER", "back order": "BACKORDER", "bo": "BACKORDER",
    "despachado": "DESPACHADO", "despachada": "DESPACHADO", "despacho": "DESPACHADO",
    "en transito": "EN TRÁNSITO", "transito": "EN TRÁNSITO", "en intransito": "EN TRÁNSITO",
    "en puerto": "EN PUERTO", "puerto": "EN PUERTO",
    "completo": "COMPLETO", "completa": "COMPLETO", "recibido": "COMPLETO", "recibida": "COMPLETO",
    "entregado": "COMPLETO",
    "cancelado": "CANCELADO", "cancelada": "CANCELADO", "anulado": "CANCELADO",
}
_ALIAS_ESTATUS_ORDEN = {"abierta": "ABIERTA", "abierto": "ABIERTA", "completa": "COMPLETA", "completo": "COMPLETA",
                        "cancelada": "CANCELADA", "cancelado": "CANCELADA"}
_VACIOS = {"", "lo llena repuesto", "pendiente", "n/a", "na", "-", "nan", "none", "nat"}


# ---------------------------------------------------------------------------
# VALORES: limpieza y comparación
# ---------------------------------------------------------------------------
def _plano(v):
    return v.item() if hasattr(v, "item") and not isinstance(v, (str, bytes)) else v


def _limpio(v):
    """None si la celda está vacía o trae un marcador («Lo Llena Repuesto», «PENDIENTE»)."""
    v = _plano(v)
    if v is None:
        return None
    if isinstance(v, float) and v != v:
        return None
    if isinstance(v, str):
        s = v.replace("\xa0", " ").strip()
        return None if _norm(s) in _VACIOS else s
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _texto(v) -> str:
    v = _limpio(v)
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (datetime, date)):
        return v.strftime("%d/%m/%Y")
    return str(v).strip()


def _num(v):
    """float, o None si está vacío. Lanza ValueError si hay algo que no es un número."""
    v = _limpio(v)
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError("no es un número")
    if isinstance(v, Real):
        return float(v)
    n = a_numero(v)
    if n is None:
        raise ValueError("no es un número")
    return n


def _fecha_texto_ambigua(v) -> bool:
    """True si es un TEXTO con día y mes ambos <= 12 escritos en número (05/11/2026)."""
    if not isinstance(v, str):
        return False
    t = sio._interpretar_tokens(sio._tokenizar_fecha(v.strip()))
    return bool(t and t[3])


def _fecha(v):
    """date, o None si está vacío. Lanza ValueError si no se puede leer."""
    v = _limpio(v)
    if v is None:
        return None
    f = parsear_fecha(v)
    if f is None:
        raise ValueError("fecha no válida")
    return f


def normalizar_estatus(raw):
    """(estatus canónico, transportista, reconocido). «DESPACHADO A INTRANS» (el formato viejo del
    Excel) se separa en DESPACHADO + INTRANS. Un estatus que no se reconoce vuelve en mayúsculas y
    reconocido=False."""
    s = _limpio(raw)
    if s is None:
        return "", "", True
    s = str(s)
    m = re.match(r"(?i)^despachad[oa]\s+(?:a\s+)?(.+)$", s)
    if m:
        return "DESPACHADO", m.group(1).strip().upper(), True
    canon = _ALIAS_ESTATUS.get(_norm(s))
    return (canon, "", True) if canon else (s.upper(), "", False)


def normalizar_estatus_orden(raw):
    s = _limpio(raw)
    if s is None:
        return "ABIERTA", True
    canon = _ALIAS_ESTATUS_ORDEN.get(_norm(str(s)))
    return (canon, True) if canon else (str(s).upper(), False)


def _canonico(clave: str, raw, orden: bool = False):
    """Valor comparable de una celda: date, float o texto. Lo usan la lectura, el diff y la escritura."""
    if clave in CLAVES_FECHA and not orden:
        try:
            return _fecha(raw)
        except ValueError:
            return None
    if clave in CLAVES_NUM and not orden:
        try:
            return _num(raw)
        except ValueError:
            return None
    if clave == "estatus" and not orden:
        return normalizar_estatus(raw)[0] or None
    if clave in ("transp", "estatus2") and not orden:
        return (_texto(raw).upper() or None)
    if clave == "est" and orden:
        return normalizar_estatus_orden(raw)[0]
    if clave == "fecha" and orden:
        return parsear_fecha(_limpio(raw)) if _limpio(raw) is not None else None
    t = _texto(raw)
    return t or None


def _a_celda(valor):
    """Lo que se escribe en la celda (RAW): fecha -> serial; número entero -> int; None -> vacío."""
    if valor is None:
        return ""
    if isinstance(valor, datetime):
        return (valor.date() - EPOCH_SHEETS).days
    if isinstance(valor, date):
        return (valor - EPOCH_SHEETS).days
    if isinstance(valor, float) and valor.is_integer():
        return int(valor)
    return valor


def _sello_ahora() -> float:
    """Serial con hora (hora RD): lo que guarda Sheets cuando una celda es fecha y hora."""
    ahora = ahora_rd().replace(tzinfo=None)
    return round((ahora - datetime(1899, 12, 30)).total_seconds() / 86400, 6)


# ---------------------------------------------------------------------------
# ACCESO AL SHEET (propio: no usa sheets_io.get_spreadsheet)
# ---------------------------------------------------------------------------
def ordenes_configurado() -> bool:
    try:
        return bool(str(st.secrets.get("ORDENES_SHEET_ID", "") or "").strip())
    except Exception:  # noqa: BLE001
        return False


@st.cache_resource
def get_spreadsheet_ordenes():
    creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"],
                                                  scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(str(st.secrets["ORDENES_SHEET_ID"]).strip())


@st.cache_resource
def _lock():
    """Candado de proceso: serializa leer-verificar-escribir entre sesiones."""
    return threading.Lock()


def _mensaje_error(e: Exception) -> str:
    if isinstance(e, gspread.exceptions.SpreadsheetNotFound):
        return "No se encontró el Sheet de órdenes. Verifica el ID en el secret ORDENES_SHEET_ID."
    if isinstance(e, gspread.exceptions.APIError):
        c = _codigo_api(e)
        if c == 403:
            return ("Google Sheets rechazó el acceso al Sheet de órdenes. Verifica que esté compartido como "
                    "Editor con la cuenta de servicio de la app.")
        if c == 404:
            return "No se encontró el Sheet de órdenes. Verifica el ID en el secret ORDENES_SHEET_ID."
        if c == 429:
            return "Google Sheets está limitando las peticiones. Espera unos segundos y pulsa ↻ Actualizar."
        return f"Google Sheets no respondió al leer las órdenes ({e})."
    return str(e)


RESERVADAS = {"instrucciones", "ordenes", "listas"}   # pestañas que NO son de responsable (comparadas con _norm)


def _nombres_responsables(valores_listas: list) -> list:
    """Nombres de la columna RESPONSABLE de la pestaña Listas, en orden, sin vacíos ni repetidos."""
    if not valores_listas or not valores_listas[0]:
        return []
    cab = [_norm_encabezado(h) for h in valores_listas[0]]
    if _norm_encabezado("RESPONSABLE") not in cab:
        return []
    c = cab.index(_norm_encabezado("RESPONSABLE"))
    vistos, nombres = set(), []
    for f in valores_listas[1:]:
        n = _texto(f[c]) if c < len(f) else ""
        if n and _norm(n) not in vistos:
            vistos.add(_norm(n))
            nombres.append(n)
    return nombres


def _hoja_de(nombre: str, titulos: list):
    """Pestaña que corresponde a un responsable: título igual (sin acentos ni mayúsculas) o, si no hay,
    el único título que empieza igual en uno u otro sentido («Yokairis» ↔ «Yokairis Martes»)."""
    n = _norm(nombre or "")
    if not n:
        return None
    for t in titulos:
        if _norm(t) == n:
            return t
    cand = [t for t in titulos if _norm(t) and (_norm(t).startswith(n) or n.startswith(_norm(t)))]
    return cand[0] if len(cand) == 1 else None


def _asignar_hojas(titulos: list, nombres: list):
    """({título de pestaña: nombre del responsable}, [nombres de Listas sin pestaña]). El nombre que manda
    es el TÍTULO de la pestaña. Las pestañas reservadas y el Desglose heredado no cuentan."""
    libres = [t for t in titulos if _norm(t) not in RESERVADAS and _norm(t) != _norm(PESTANA_DESGLOSE)]
    asignadas, sin_hoja = {}, []
    for n in nombres:
        h = _hoja_de(n, [t for t in libres if t not in asignadas])
        if h is None:
            sin_hoja.append(n)
        else:
            asignadas[h] = h
    return asignadas, sin_hoja


def _rango_a1(titulo: str, ultima_col: str, filas: int) -> str:
    return "'" + titulo.replace("'", "''") + f"'!A1:{ultima_col}{filas}"


def _leer_valores(ss) -> dict:
    """Lee Órdenes, Listas y todas las pestañas candidatas en UNA petición, SIN formato (números y fechas
    seriales). {'o': matriz Órdenes, 't_o': título o None, 'd': {título: matriz} (pestañas de responsable y,
    si existe, el Desglose heredado), 'resp': {título: responsable o None}, 'sin_hoja': [nombres de Listas
    sin pestaña], 'hay_listas': bool}."""
    titulos = {_norm(w.title): w.title for w in _con_reintento(lambda: ss.worksheets())}
    t_o, t_l = titulos.get(_norm(PESTANA_ORDENES)), titulos.get(_norm(PESTANA_LISTAS))
    t_leg = titulos.get(_norm(PESTANA_DESGLOSE))
    candidatas = [t for n, t in titulos.items() if n not in RESERVADAS]
    if t_o is None:
        return {"o": [], "t_o": None, "d": {}, "resp": {}, "sin_hoja": [], "hay_listas": t_l is not None}
    rangos = [_rango_a1(t_o, "Z", MAX_FILAS)] + ([_rango_a1(t_l, "J", 300)] if t_l else []) \
        + [_rango_a1(t, "Z", MAX_FILAS) for t in candidatas]
    params = {"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"}
    rv = _con_reintento(lambda: ss.values_batch_get(rangos, params=params)).get("valueRanges", [])
    vals = [(r.get("values", []) if r else []) for r in rv] + [[]] * (len(rangos) - len(rv))
    vo, resto = vals[0], vals[1:]
    vl = resto.pop(0) if t_l else []
    por_titulo = dict(zip(candidatas, resto))
    nombres = _nombres_responsables(vl)
    asignadas, sin_hoja = _asignar_hojas(candidatas, nombres)
    d, resp = {}, {}
    for t in candidatas:
        if t in asignadas:
            d[t], resp[t] = por_titulo[t], asignadas[t]
    if t_leg:
        d[t_leg], resp[t_leg] = por_titulo.get(t_leg, []), None
    return {"o": vo, "t_o": t_o, "d": d, "resp": resp, "sin_hoja": sin_hoja, "hay_listas": t_l is not None}


@st.cache_data(ttl=TTL, show_spinner=False)
def _leer_remoto(token: str = "") -> dict:
    """`token` solo parte la caché (la app pasa la hora de la última lectura del Sheet principal, así
    ↻ Actualizar también recarga esta vista). No atrapa errores: un fallo no queda cacheado."""
    ss = get_spreadsheet_ordenes()
    return {**_leer_valores(ss), "hora": ahora_rd(), "libro": str(getattr(ss, "title", "") or "")}


def cargar(token: str = "") -> dict:
    """Lectura cacheada. El fracaso nunca sube como excepción: vuelve en 'error'."""
    try:
        r = _leer_remoto(str(token or ""))
    except Exception as e:  # noqa: BLE001 - la UI muestra el mensaje; nunca un traceback
        return {"error": _mensaje_error(e), "hora": ahora_rd(), "faltan": [], "o": [], "d": {}, "resp": {},
                "sin_hoja": []}
    faltan = []
    if r["t_o"] is None:
        faltan.append(PESTANA_ORDENES)
    if not r["d"]:
        faltan.append("una pestaña por responsable (sus nombres salen de Listas → RESPONSABLE)")
    return {**r, "error": None, "faltan": faltan}


def invalidar():
    _leer_remoto.clear()


# ---------------------------------------------------------------------------
# TABLAS: matriz cruda -> registros con claves internas
# ---------------------------------------------------------------------------
def _mapa_encabezados(headers: list, defs: list) -> dict:
    """{clave: nº de columna (1..)} comparando encabezados normalizados; el primero que coincide gana."""
    pos, usados = {}, set()
    norm = [_norm_encabezado(h) for h in headers]
    for clave, canon, alias in defs:
        validos = {_norm_encabezado(canon)} | {_norm_encabezado(a) for a in alias}
        for i, h in enumerate(norm):
            if i not in usados and h in validos:
                pos[clave] = i + 1
                usados.add(i)
                break
    return pos


def _registros(valores: list, defs: list, datos: list):
    """(pos, [{'Fila': n, clave: valor crudo}], ultima_fila). `datos` = claves que cuentan como dato
    (las fórmulas del Sheet no cuentan para saber si un renglón está ocupado)."""
    if not valores or not valores[0]:
        return {}, [], 1
    pos = _mapa_encabezados([str(h) for h in valores[0]], defs)
    filas, ultima = [], 1
    for i, f in enumerate(valores[1:], start=2):
        reg = {"Fila": i}
        for clave, c in pos.items():
            reg[clave] = f[c - 1] if c - 1 < len(f) else ""
        if any(_limpio(reg.get(k)) is not None for k in datos):
            ultima = i
            filas.append(reg)
    return pos, filas, ultima


def tablas(valores_o: list, hojas_d, resp_hojas: dict | None = None) -> dict:
    """Todo lo que se sabe del Sheet, sin interpretar: posiciones de columnas, índices por llave y avisos
    de datos. Lo usan la vista (con datos cacheados) y la escritura (con datos en vivo).
    `hojas_d` = {título de pestaña: matriz}; `resp_hojas` = {título: responsable o None}. Una matriz suelta
    se toma como el Desglose heredado (sin responsable propio)."""
    if not isinstance(hojas_d, dict):
        hojas_d = {PESTANA_DESGLOSE: hojas_d}
    resp_hojas = resp_hojas if resp_hojas is not None else {h: None for h in hojas_d}
    pos_o, regs_o, ult_o = _registros(valores_o, COLUMNAS_O, ["doc", "prov", "resp", "emp", "coment"])
    avisos, faltan_duro = [], []
    if valores_o and "doc" not in pos_o:
        faltan_duro.append(f"{PESTANA_ORDENES}: {NOMBRE_O['doc']}")
    for k, n in NOMBRE_O.items():
        if valores_o and k not in pos_o and k != "doc":
            avisos.append(f"A la pestaña {PESTANA_ORDENES} le falta la columna {n}.")

    ordenes, idx_o = [], {}
    for r in regs_o:
        doc = _texto(r.get("doc"))
        if not doc:
            avisos.append(f"{PESTANA_ORDENES} fila {r['Fila']}: tiene datos pero no Documento compras.")
            continue
        if doc in idx_o:
            avisos.append(f"{PESTANA_ORDENES} fila {r['Fila']}: el documento {doc} ya está en la fila "
                          f"{idx_o[doc]['Fila']}; se usa la primera.")
            continue
        est, ok = normalizar_estatus_orden(r.get("est"))
        if not ok:
            avisos.append(f"{PESTANA_ORDENES} fila {r['Fila']}: estatus de orden '{_texto(r.get('est'))}' no "
                          "reconocido; se toma como ABIERTA.")
            est = "ABIERTA"
        reg = {"Fila": r["Fila"], "doc": doc, "prov": _texto(r.get("prov")), "resp": _texto(r.get("resp")),
               "emp": _texto(r.get("emp")), "est": est, "coment": _texto(r.get("coment")),
               "fecha": _canonico("fecha", r.get("fecha"), True), "por": _texto(r.get("por"))}
        idx_o[doc] = reg
        ordenes.append(reg)

    hojas, lineas, idx_d, hoja_de_doc = {}, [], {}, {}
    for titulo, valores in hojas_d.items():
        pos_d, regs_d, ult_d = _registros(valores, COLUMNAS_D, [k for k, _, _ in COLUMNAS_D if k != "pend"])
        hojas[titulo] = {"pos": pos_d, "ultima": ult_d, "resp": resp_hojas.get(titulo)}
        if valores:
            faltan_duro += [f"{titulo}: {NOMBRE_D[k]}" for k in ("doc", "pos") if k not in pos_d]
            for k, n in NOMBRE_D.items():
                if k not in pos_d and k not in ("doc", "pos", "pend") and k not in OPCIONALES_D:
                    avisos.append(f"A la pestaña {titulo} le falta la columna {n}.")
        for r in regs_d:
            n = r["Fila"]
            doc, pos = _texto(r.get("doc")), _texto(r.get("pos"))
            if not doc or not pos:
                avisos.append(f"{titulo} fila {n}: falta " + ("Orden de Compras" if not doc else "Posición")
                              + "; no se muestra.")
                continue
            if (doc, pos) in idx_d:
                ya = idx_d[(doc, pos)]
                donde = f"la fila {ya['Fila']}" + ("" if ya["hoja"] == titulo else f" de {ya['hoja']}")
                avisos.append(f"{titulo} fila {n}: la línea {doc}/{pos} ya está en {donde}; se usa la primera.")
                continue
            reg = {"Fila": n, "doc": doc, "pos": pos, "hoja": titulo, "resp_hoja": resp_hojas.get(titulo)}
            for k in ESCRIBIBLES_D:
                reg[k] = _canonico(k, r.get(k))
            est_raw = r.get("estatus")
            est, transp, ok = normalizar_estatus(est_raw)
            if not ok:
                avisos.append(f"{titulo} fila {n}: estatus '{_texto(est_raw)}' no reconocido.")
            reg["estatus"] = est or None
            if not reg.get("transp") and transp:
                reg["transp"] = transp
            for k in CLAVES_FECHA | CLAVES_NUM:
                crudo = _limpio(r.get(k))
                if crudo is not None and reg[k] is None:
                    avisos.append(f"{titulo} fila {n}: {NOMBRE_D[k]} '{_texto(crudo)}' no es válido.")
            idx_d[(doc, pos)] = reg
            lineas.append(reg)
            hoja_de_doc.setdefault(doc, titulo)
    return {"pos_o": pos_o, "hojas": hojas, "ordenes": ordenes, "lineas": lineas, "idx_o": idx_o, "idx_d": idx_d,
            "ultima_o": ult_o, "hoja_de_doc": hoja_de_doc, "avisos": avisos, "faltan_duro": faltan_duro}


# ---------------------------------------------------------------------------
# REGLAS DE NEGOCIO (puras)
# ---------------------------------------------------------------------------
def _etq(est: str) -> str:
    return est.capitalize() if est else "Sin estatus"


def estatus_derivado(ests: list, total_lineas: int, parcial: bool = False) -> str:
    """Estatus de la ORDEN a partir de sus líneas abiertas. `ests`: estatus de las líneas abiertas
    (SIN_ESTATUS para las vacías)."""
    if total_lineas == 0:
        return "Sin líneas"
    if not ests:
        return "Por cerrar"
    s = set(ests)
    if "BACKORDER" in s:
        return "Backorder"
    if len(s) == 1:
        etiqueta = _etq(next(iter(s)))
    elif s & AVANZADOS and s - AVANZADOS:
        return "Despacho parcial"
    else:
        etiqueta = "En camino"
    if parcial and (s & AVANZADOS) and etiqueta != "Sin estatus":
        return etiqueta + " parcial"
    return etiqueta


def construir(t: dict, hoy: date) -> dict:
    """Órdenes y líneas ABIERTAS, listas para mostrar. {'ordenes': DataFrame, 'lineas': DataFrame,
    'cerradas': n, 'completas_abiertas': [docs], 'avisos': [...]}."""
    avisos = list(t["avisos"])
    por_doc: dict = {}
    for ln in t["lineas"]:
        por_doc.setdefault(ln["doc"], []).append(ln)
    cerradas = {d for d, o in t["idx_o"].items() if o["est"] in CERRADOS_ORDEN}
    docs = [o["doc"] for o in t["ordenes"] if o["doc"] not in cerradas]
    huerfanas = [d for d in por_doc if d not in t["idx_o"]]
    docs += huerfanas
    if huerfanas:
        avisos.append("Documentos con líneas pero sin fila en Órdenes (salen como "
                      f"«{SIN_ASIGNAR}»): " + ", ".join(huerfanas[:10]) + ("…" if len(huerfanas) > 10 else ""))

    filas_o, filas_l, completas_abiertas = [], [], []
    for doc in docs:
        o = t["idx_o"].get(doc) or {"Fila": None, "doc": doc, "prov": "", "resp": "", "emp": "", "est": "ABIERTA",
                                    "coment": "", "fecha": None, "por": ""}
        todas = por_doc.get(doc, [])
        tabs = list(dict.fromkeys(ln["resp_hoja"] for ln in todas if ln.get("resp_hoja")))
        resp = tabs[0] if tabs else o["resp"]
        if len(tabs) > 1:
            avisos.append(f"La orden {doc} tiene líneas en varias pestañas de responsable ({', '.join(tabs)}); "
                          f"sale con {tabs[0]}. Muévelas a una sola pestaña.")
        elif tabs and o["resp"] and _hoja_de(o["resp"], tabs) is None:
            avisos.append(f"La orden {doc}: Órdenes dice responsable «{o['resp']}» pero sus líneas están en la "
                          f"pestaña {tabs[0]}; se usa {tabs[0]}.")
        abiertas = [ln for ln in todas if (ln.get("estatus") or "") not in CERRADOS_LINEA]
        ests = [ln.get("estatus") or SIN_ESTATUS for ln in abiertas]
        pend_u, parcial, backorder = 0.0, False, False
        for ln in abiertas:
            ped, desp = ln.get("pedida"), ln.get("desp")
            pend = None if ped is None else max(ped - (desp or 0), 0)
            ln["pend"] = pend
            est = ln.get("estatus") or ""
            hay_parcial = est in AVANZADOS and (desp or 0) > 0 and (pend or 0) > 0
            ln["parcial"] = hay_parcial
            parcial = parcial or hay_parcial
            backorder = backorder or est == "BACKORDER" or hay_parcial
            pend_u += pend or 0
            if est in AVANZADOS and not (desp or 0) and ped:
                avisos.append(f"{ln['hoja']} fila {ln['Fila']}: {doc}/{ln['pos']} dice {est} pero no tiene "
                              "cantidad despachada.")
            e2 = ln.get("estatus2") or ""
            if e2 == "COMPLETA" and (pend or 0) > 0:
                avisos.append(f"{ln['hoja']} fila {ln['Fila']}: {doc}/{ln['pos']} dice COMPLETA en el 2.º ESTATUS "
                              f"pero faltan {_cant(pend)} por despachar.")
            elif e2 == "PARCIAL" and ped and not (pend or 0):
                avisos.append(f"{ln['hoja']} fila {ln['Fila']}: {doc}/{ln['pos']} dice PARCIAL en el 2.º ESTATUS "
                              "pero ya se despachó todo.")
        if todas and not abiertas and o["est"] not in CERRADOS_ORDEN:
            completas_abiertas.append(doc)
        refs = [ln.get("fdoc") for ln in todas if ln.get("fdoc")] or [ln.get("sol") for ln in todas if ln.get("sol")]
        ref = o["fecha"] or (min(refs) if refs else None)
        etas = [ln["eta"] for ln in abiertas if ln.get("eta")]
        facturas = sorted({ln["fact"] for ln in todas if ln.get("fact")})
        solicitantes = sorted({ln["solicitante"] for ln in todas if ln.get("solicitante")})
        clientes = sorted({ln["cliente"] for ln in todas if ln.get("cliente")})
        derivado = estatus_derivado(ests, len(todas), parcial)
        haystack = _norm(" ".join([doc, o["prov"], resp, *facturas, *solicitantes, *clientes]
                                  + [str(ln.get(k) or "") for ln in todas for k in ("mat", "matant", "nparte", "texto", "nosol")]))
        filas_o.append({
            "Doc": doc, "Proveedor": o["prov"], "Responsable": resp or SIN_ASIGNAR, "Empresa": o["emp"],
            "EstatusOrden": o["est"], "Comentario": o["coment"], "Actualizado": o["fecha"], "Por": o["por"],
            "FilaO": o["Fila"], "Lineas": len(todas), "Abiertas": len(abiertas), "PendUnid": pend_u,
            "Derivado": derivado, "ETA": min(etas) if etas else None, "Facturas": ", ".join(facturas),
            "Solicitantes": ", ".join(solicitantes), "Recepcion": min([ln["sol"] for ln in todas if ln.get("sol")],
                                                                      default=None),
            "Colocacion": min([ln["fdoc"] for ln in todas if ln.get("fdoc")], default=None),
            "SinSello": o["fecha"] is None, "Dias": (hoy - ref).days if ref else None,
            "Backorder": backorder, "Camino": any(e in AVANZADOS for e in ests),
            "SinDespachar": bool(abiertas) and not any(e in AVANZADOS for e in ests), "Haystack": haystack,
            "PorPestana": bool(tabs),
        })
        for ln in abiertas:
            filas_l.append({
                "Doc": doc, "Pos": ln["pos"], "Material": ln.get("mat") or "", "MatAnt": ln.get("matant") or "",
                "NParte": ln.get("nparte") or "",
                "Texto": ln.get("texto") or "", "Pedida": ln.get("pedida"), "Desp": ln.get("desp"),
                "Pend": ln.get("pend"), "Parcial": ln.get("parcial", False), "Solicitante": ln.get("solicitante") or "",
                "Cliente": ln.get("cliente") or "", "Estatus": ln.get("estatus") or "", "Transp": ln.get("transp") or "",
                "Embarque": ln.get("embarque"), "ETA": ln.get("eta"), "Puerto": ln.get("puerto") or "",
                "Almacen": ln.get("almacen") or "", "Coment": ln.get("coment") or "", "Factura": ln.get("fact") or "",
                "Fila": ln["Fila"], "Responsable": resp or SIN_ASIGNAR, "Proveedor": o["prov"],
                "Despacho": ln.get("estatus2") or "", "Hoja": ln["hoja"],
            })
    cols_o = ["Doc", "Proveedor", "Responsable", "Empresa", "EstatusOrden", "Comentario", "Actualizado", "Por", "FilaO",
              "Lineas", "Abiertas", "PendUnid", "Derivado", "ETA", "Facturas", "Solicitantes", "Recepcion",
              "Colocacion", "SinSello", "Dias", "Backorder", "Camino", "SinDespachar", "Haystack", "PorPestana"]
    cols_l = ["Doc", "Pos", "Material", "MatAnt", "NParte", "Texto", "Pedida", "Desp", "Pend", "Parcial", "Solicitante", "Cliente",
              "Estatus", "Transp", "Embarque", "ETA", "Puerto", "Almacen", "Coment", "Factura", "Fila", "Responsable",
              "Proveedor", "Despacho", "Hoja"]
    df_o = pd.DataFrame(filas_o, columns=cols_o)
    df_l = pd.DataFrame(filas_l, columns=cols_l)
    if len(df_o):
        df_o["_v"] = df_o["Dias"].fillna(-1)
        df_o = df_o.sort_values(["_v", "Doc"], ascending=[False, True]).drop(columns="_v").reset_index(drop=True)
    return {"ordenes": df_o, "lineas": df_l, "cerradas": len(cerradas), "completas_abiertas": completas_abiertas,
            "avisos": avisos}


def es_viejo(dias) -> bool:
    return dias is not None and not pd.isna(dias) and dias > DIAS_SIN_ACTUALIZAR


def kpis(df: pd.DataFrame) -> dict:
    return {"abiertas": len(df), "sin_despachar": int(df["SinDespachar"].sum()) if len(df) else 0,
            "backorder": int(df["Backorder"].sum()) if len(df) else 0,
            "camino": int(df["Camino"].sum()) if len(df) else 0,
            "viejas": int(sum(es_viejo(d) for d in df["Dias"])) if len(df) else 0}


# ---------------------------------------------------------------------------
# CARGA MASIVA: archivo -> líneas validadas -> plan -> escritura
# ---------------------------------------------------------------------------
def _hoja_del_archivo(contenido: bytes, nombre: str) -> pd.DataFrame:
    if nombre.lower().endswith(".csv"):
        try:
            return pd.read_csv(io.BytesIO(contenido), header=None, dtype=object, sep=None, engine="python",
                               encoding="utf-8-sig", keep_default_na=False)
        except UnicodeDecodeError:
            return pd.read_csv(io.BytesIO(contenido), header=None, dtype=object, sep=None, engine="python",
                               encoding="latin-1", keep_default_na=False)
    libro = pd.ExcelFile(io.BytesIO(contenido))
    hoja = next((h for h in libro.sheet_names if _norm(h) in (_norm(PESTANA_CARGA), _norm(PESTANA_DESGLOSE))),
                libro.sheet_names[0])
    return libro.parse(hoja, header=None, dtype=object)


def leer_archivo(contenido: bytes, nombre: str) -> dict:
    """Lee un Excel/CSV con el formato del Desglose (encabezados con o sin acentos, mayúsculas, etc.).
    Devuelve {'lineas': [dict con claves internas + 'fila_excel'], 'rechazos': [(fila, doc, pos, motivo)],
    'avisos': [...]}. Lanza ValueError si no encuentra los encabezados."""
    try:
        crudo = _hoja_del_archivo(contenido, nombre)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"No se pudo abrir el archivo ({e}). Usa un .xlsx o .csv.") from e
    pos, cab = None, None
    for i in range(min(15, len(crudo))):
        m = _mapa_encabezados([_texto(x) for x in crudo.iloc[i].tolist()], COLUMNAS_ARCHIVO)
        if "doc" in m and "pos" in m:
            pos, cab = m, i
            break
    if pos is None:
        raise ValueError("No encontré los encabezados «Documento compras» y «Posición» en las primeras 15 filas.")
    lineas, rechazos, avisos, vistas = [], [], [], {}
    for i in range(cab + 1, len(crudo)):
        f = crudo.iloc[i].tolist()
        n = i + 1
        celdas = {k: (f[c - 1] if c - 1 < len(f) else None) for k, c in pos.items()}
        if all(_limpio(celdas.get(k)) is None for k in pos if k != "pend"):
            continue
        doc, p = _texto(celdas.get("doc")), _texto(celdas.get("pos"))
        if not doc or not p:
            rechazos.append((n, doc, p, "falta " + ("Documento compras" if not doc else "Posición")))
            continue
        reg, motivo = {"fila_excel": n, "doc": doc, "pos": p}, None
        for k in ESCRIBIBLES_D + ["prov", "resp", "emp"]:
            if k not in pos:
                continue
            crudo_k = celdas.get(k)
            try:
                if k in CLAVES_NUM:
                    v = _num(crudo_k)
                    if v is not None and v < 0:
                        raise ValueError("no puede ser negativa")
                elif k in CLAVES_FECHA:
                    if _fecha_texto_ambigua(_limpio(crudo_k)):
                        raise ValueError(f"'{_texto(crudo_k)}' es ambigua (día y mes ≤ 12 escritos como texto); "
                                         "escríbela con el mes en letras (05-nov-2026) o como fecha de Excel")
                    v = _fecha(crudo_k)
                elif k == "estatus":
                    est, tr, ok = normalizar_estatus(crudo_k)
                    if not ok:
                        raise ValueError(f"estatus '{_texto(crudo_k)}' no reconocido (válidos: "
                                         + ", ".join(ESTATUS_LINEA) + ")")
                    v = est or None
                    if tr and not _limpio(celdas.get("transp")):
                        reg["transp"] = tr
                elif k in ("transp", "estatus2"):
                    v = _texto(crudo_k).upper() or None
                elif k == "emp":
                    t = _texto(crudo_k)
                    v = next((e for e in EMPRESAS if _norm(e) == _norm(t) or _norm(e).split()[0] == _norm(t)), None) \
                        if t else None
                    if t and v is None:
                        avisos.append(f"Fila {n}: empresa '{t}' no es una de {', '.join(EMPRESAS)}; se ignora.")
                else:
                    v = _texto(crudo_k) or None
            except ValueError as e:
                prefijo = {"pedida": "cantidad de pedido", "desp": "cantidad despachada"}.get(k, NOMBRE_D.get(k, k))
                motivo = f"{prefijo}: {e}"
                break
            if v is not None or k not in reg:
                reg[k] = v
        if motivo:
            rechazos.append((n, doc, p, motivo))
            continue
        if (doc, p) in vistas:
            rechazos.append((n, doc, p, f"repetida en el archivo (ya está en la fila {vistas[(doc, p)]})"))
            continue
        vistas[(doc, p)] = n
        if reg.get("pedida") is not None and (reg.get("desp") or 0) > reg["pedida"]:
            avisos.append(f"Fila {n}: {doc}/{p} despachó {_cant(reg['desp'])} de {_cant(reg['pedida'])} pedidas.")
        lineas.append(reg)
    return {"lineas": lineas, "rechazos": rechazos, "avisos": avisos}


def _destino(ln: dict, t: dict, usuario: str, rol: str, por_doc: dict):
    """Pestaña donde va una línea NUEVA: 1) donde ya viven otras líneas de esa orden; 2) la pestaña del
    RESPONSABLE que trae el archivo; 3) la de «RESPONSABLE» en la hoja Órdenes; 4) la del usuario de Compras.
    Si el Sheet solo tiene el Desglose heredado, va ahí. None = no hay a dónde mandarla."""
    doc = ln["doc"]
    if doc in t["hoja_de_doc"]:
        return t["hoja_de_doc"][doc]
    if doc in por_doc:
        return por_doc[doc]
    con_resp = [h for h, i in t["hojas"].items() if i["resp"]]
    if not con_resp:
        return next(iter(t["hojas"]), None)
    o = t["idx_o"].get(doc)
    candidatos = [ln.get("resp"), o["resp"] if o else None, usuario if rol == "compras" else None]
    for c in candidatos:
        h = _hoja_de(c, con_resp) if c else None
        if h:
            return h
    return None


def planificar(lineas: list, t: dict, usuario: str, rol: str) -> dict:
    """Qué haría esta carga contra el estado `t` del Sheet. Pura. Nunca borra: un valor vacío del
    archivo no vacía el del Sheet, y lo que ya trae `PROTEGIDAS` no se pisa. Las líneas nuevas van a la
    pestaña del responsable (`_destino`); las que no tienen destino se devuelven en `sin_destino`."""
    nuevas, cambios, sin_cambio, sin_destino = [], [], 0, []
    ord_nuevas, ord_completar, tocadas, por_doc = {}, {}, set(), {}
    for ln in lineas:
        key = (ln["doc"], ln["pos"])
        doc = ln["doc"]
        actual = t["idx_d"].get(key)
        if actual is None:
            hoja = _destino(ln, t, usuario, rol, por_doc)
            if hoja is None:
                sin_destino.append((ln["fila_excel"], doc, ln["pos"],
                                    "no se sabe de qué responsable es: pon su nombre en la columna RESPONSABLE "
                                    "(" + ", ".join(h for h, i in t["hojas"].items() if i["resp"]) + ")"))
                continue
            por_doc.setdefault(doc, hoja)
            vals = {k: ln[k] for k in ("doc", "pos", *ESCRIBIBLES_D) if ln.get(k) is not None}
            vals.setdefault("estatus", "COLOCADA")
            nuevas.append({"key": key, "vals": vals, "fila_excel": ln["fila_excel"], "hoja": hoja})
            tocadas.add(doc)
        else:
            hoja = actual["hoja"]
            cam, conservados = {}, []
            for k in ESCRIBIBLES_D:
                nuevo = ln.get(k)
                if nuevo is None:
                    continue
                antes = actual.get(k)
                if antes == nuevo:
                    continue
                if k in PROTEGIDAS and antes is not None:
                    conservados.append(k)
                    continue
                cam[k] = (antes, nuevo)
            if cam:
                cambios.append({"key": key, "fila": actual["Fila"], "cambios": cam, "conservados": conservados,
                                "fila_excel": ln["fila_excel"], "hoja": hoja})
                tocadas.add(doc)
            else:
                sin_cambio += 1
        # Datos de la ORDEN. Con pestaña de responsable, el responsable ES la pestaña.
        extra = {k: ln[k] for k in ("prov", "resp", "emp") if ln.get(k)}
        resp_hoja = t["hojas"][hoja]["resp"]
        if resp_hoja:
            extra["resp"] = resp_hoja
        if doc not in t["idx_o"]:
            base = ord_nuevas.setdefault(doc, {})
            for k, v in extra.items():
                base.setdefault(k, v)
            if rol == "compras" and usuario and not resp_hoja:
                base.setdefault("resp", usuario)
        else:
            o = t["idx_o"][doc]
            comp = ord_completar.setdefault(doc, {})
            for k, v in extra.items():
                if not o.get(k):
                    comp.setdefault(k, (o.get(k) or None, v))
    for doc in list(ord_completar):
        if not ord_completar[doc]:
            del ord_completar[doc]
    tocadas |= set(ord_nuevas) | set(ord_completar)
    n_celdas = sum(len(c["cambios"]) for c in cambios) + sum(len(n["vals"]) for n in nuevas)
    return {"nuevas": nuevas, "cambios": cambios, "sin_cambio": sin_cambio, "sin_destino": sin_destino,
            "ord_nuevas": ord_nuevas, "ord_completar": ord_completar, "tocadas": sorted(tocadas), "celdas": n_celdas,
            "hay_cambios": bool(nuevas or cambios or ord_nuevas or ord_completar)}


def _cant(x) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.2f}"


def _en_vivo():
    """Lee el Sheet de órdenes SIN caché. Devuelve (spreadsheet, ws_ordenes, tablas)."""
    ss = get_spreadsheet_ordenes()
    v = _leer_valores(ss)
    if v["t_o"] is None:
        raise ValueError(f"Falta la pestaña {PESTANA_ORDENES} en el Sheet de órdenes.")
    if not v["d"]:
        raise ValueError("El Sheet de órdenes no tiene pestañas de responsable (sus nombres salen de "
                         "Listas → RESPONSABLE).")
    t = tablas(v["o"], v["d"], v["resp"])
    if t["faltan_duro"]:
        raise ValueError("Faltan columnas en el Sheet de órdenes: " + ", ".join(t["faltan_duro"]))
    return ss, ss.worksheet(v["t_o"]), t


def _asegurar_filas(ws, hasta: int):
    if hasta > MAX_FILAS:
        raise ValueError(f"La pestaña {ws.title} llegaría al tope de {MAX_FILAS:,} filas que la app lee.")
    if hasta > ws.row_count:
        _con_reintento(lambda: ws.add_rows(hasta - ws.row_count + 50))


def _peticiones_fila_nueva(fila: int, pos: dict, valores: dict):
    """Una petición por tramo de columnas CONSECUTIVAS con valor. Una columna sin valor (p. ej. la fórmula
    PENDIENTE) nunca se toca: un hueco parte el tramo."""
    por_col = {pos[k]: _a_celda(v) for k, v in valores.items() if k in pos and _a_celda(v) != ""}
    tramos = []
    for c in sorted(por_col):
        if tramos and c == tramos[-1][-1] + 1:
            tramos[-1].append(c)
        else:
            tramos.append([c])
    return [{"range": f"{rowcol_to_a1(fila, t[0])}:{rowcol_to_a1(fila, t[-1])}",
             "values": [[por_col[c] for c in t]]} for t in tramos]


def _peticiones_celdas(fila: int, pos: dict, valores: dict):
    return [{"range": rowcol_to_a1(fila, pos[k]), "values": [[_a_celda(v)]]} for k, v in valores.items() if k in pos]


def _faltantes(pos: dict, claves, nombres: dict, hoja: str):
    falta = [nombres[k] for k in claves if k not in pos]
    if falta:
        raise ValueError(f"A la pestaña {hoja} le faltan las columnas: " + ", ".join(falta))


def _escribir_planes(ss, ws_o, t, plan: dict, usuario: str):
    """Escribe líneas (cada una en SU pestaña) y luego órdenes. Primero las pestañas de líneas: si una
    escritura posterior falla, repetir la carga completa lo que falte (no duplica). Las columnas de TODAS
    las pestañas implicadas se comprueban antes de escribir nada."""
    sello = _sello_ahora()
    por_hoja: dict = {}
    for c in plan["cambios"]:
        por_hoja.setdefault(c["hoja"], {"cambios": [], "nuevas": []})["cambios"].append(c)
    for n in plan["nuevas"]:
        por_hoja.setdefault(n["hoja"], {"cambios": [], "nuevas": []})["nuevas"].append(n)
    claves_o = {"fecha", "por"} | {k for v in plan["ord_nuevas"].values() for k in v} \
        | {k for v in plan["ord_completar"].values() for k in v}
    if plan["ord_nuevas"]:
        claves_o |= {"doc", "est"}
    for hoja, g in por_hoja.items():
        claves = {k for n in g["nuevas"] for k in n["vals"]} | {k for c in g["cambios"] for k in c["cambios"]}
        _faltantes(t["hojas"][hoja]["pos"], claves, NOMBRE_D, hoja)
    _faltantes(t["pos_o"], claves_o, NOMBRE_O, PESTANA_ORDENES)

    hechas = []
    for hoja, g in por_hoja.items():
        info, pet = t["hojas"][hoja], []
        for c in g["cambios"]:
            pet += _peticiones_celdas(c["fila"], info["pos"], {k: nv for k, (_, nv) in c["cambios"].items()})
        ws = ss.worksheet(hoja)
        if g["nuevas"]:
            _asegurar_filas(ws, info["ultima"] + len(g["nuevas"]))
        for i, n in enumerate(g["nuevas"], start=1):
            pet += _peticiones_fila_nueva(info["ultima"] + i, info["pos"], n["vals"])
        if pet:
            try:
                _con_reintento(lambda: ws.batch_update(pet, value_input_option="RAW"))
            except Exception as e:  # noqa: BLE001
                if not hechas:
                    raise
                raise RuntimeError(f"Se guardaron las líneas de {', '.join(hechas)} pero falló {hoja} ({e}). "
                                   "Repite la carga: no duplica nada y completa lo que falta.") from e
            hechas.append(hoja)

    pet_o, ult_o = [], t["ultima_o"]
    nuevas_o = sorted(plan["ord_nuevas"])
    if nuevas_o:
        _asegurar_filas(ws_o, ult_o + len(nuevas_o))
    for i, doc in enumerate(nuevas_o, start=1):
        vals = {"doc": doc, "est": "ABIERTA", "fecha": sello, "por": usuario, **plan["ord_nuevas"][doc]}
        pet_o += _peticiones_fila_nueva(ult_o + i, t["pos_o"], vals)
    for doc in plan["tocadas"]:
        if doc in plan["ord_nuevas"]:
            continue
        o = t["idx_o"].get(doc)
        if o is None:
            continue
        vals = {"fecha": sello, "por": usuario}
        vals.update({k: nv for k, (_, nv) in plan["ord_completar"].get(doc, {}).items()})
        pet_o += _peticiones_celdas(o["Fila"], t["pos_o"], vals)
    if pet_o:
        try:
            _con_reintento(lambda: ws_o.batch_update(pet_o, value_input_option="RAW"))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("Las líneas SÍ se guardaron, pero no se pudo actualizar la pestaña Órdenes "
                               f"({e}). Repite la carga: no duplica nada y completa lo que falta.") from e


def _verificar_rol():
    if st.session_state.get("rol") not in ROLES_ESCRITURA:
        return "Tu rol no tiene permiso para modificar las órdenes de compra."
    if not ordenes_configurado():
        return "El Sheet de órdenes de compra no está configurado."
    return None


def _resultado(f):
    def envoltura(*a, **kw):
        try:
            return f(*a, **kw)
        except ValueError as e:
            return False, str(e)
        except RuntimeError as e:
            invalidar()
            return False, str(e)
        except gspread.exceptions.APIError as e:
            c = _codigo_api(e)
            if c == 429:
                return False, "Google Sheets está limitando las peticiones. Espera unos segundos y repite."
            if c == 403:
                return False, "Google Sheets rechazó la operación por permisos (la cuenta de servicio debe ser Editor)."
            return False, f"Google Sheets rechazó la operación ({e}). Espera unos segundos e intenta de nuevo."
        except Exception as e:  # noqa: BLE001
            return False, f"Error inesperado: {e}"
    envoltura.__name__ = f.__name__
    return envoltura


@_resultado
def aplicar_carga(lineas: list, nombre_archivo: str = ""):
    """Aplica una carga masiva. La comparación se repite EN VIVO bajo candado (no se confía en la vista
    previa). Devuelve (ok, mensaje)."""
    error = _verificar_rol()
    if error:
        return False, error
    usuario, rol = sio.usuario_actual(), st.session_state.get("rol")
    with _lock():
        ss, ws_o, t = _en_vivo()
        plan = planificar(lineas, t, usuario, rol)
        if not plan["hay_cambios"]:
            extra = (f" {len(plan['sin_destino'])} línea(s) quedaron sin cargar por no tener responsable."
                     if plan["sin_destino"] else "")
            return True, "El archivo no trae nada nuevo: el Sheet ya tiene esos datos." + extra
        _escribir_planes(ss, ws_o, t, plan, usuario)
    invalidar()
    resumen = (f"{len(plan['nuevas'])} líneas nuevas, {len(plan['cambios'])} actualizadas, "
               f"{len(plan['ord_nuevas'])} órdenes nuevas"
               + (f", {len(plan['sin_destino'])} sin cargar (sin responsable)" if plan["sin_destino"] else ""))
    sio.registrar_log("Órdenes: carga masiva", bl=f"{len(plan['tocadas'])} órdenes", categoria=CATEGORIA_LOG,
                      detalle=f"{nombre_archivo}: {resumen}; {plan['celdas']} celdas. Órdenes: "
                              + ", ".join(plan["tocadas"][:12]) + ("…" if len(plan["tocadas"]) > 12 else ""))
    return True, f"Carga aplicada: {resumen}."


def _comparable(clave: str, valor, orden: bool = False):
    """Normaliza un valor de la pantalla para compararlo con el del Sheet."""
    if valor is None or (isinstance(valor, float) and valor != valor):
        return None
    if clave in CLAVES_FECHA and not orden:
        if isinstance(valor, pd.Timestamp):
            return valor.date()
        return valor
    if clave in CLAVES_NUM and not orden:
        return float(valor)
    if isinstance(valor, str):
        valor = valor.strip()
        return valor or None
    return valor


@_resultado
def guardar_edicion(doc: str, cambios_orden: dict, cambios_lineas: dict):
    """Cambios hechos en pantalla sobre UNA orden. `cambios_orden` = {clave: (antes, nuevo)};
    `cambios_lineas` = {pos: {clave: (antes, nuevo)}}. Bajo candado se vuelve a leer el Sheet: si alguna
    celda ya no vale lo que la pantalla creía (otra persona la cambió), no se escribe NADA."""
    error = _verificar_rol()
    if error:
        return False, error
    usuario = sio.usuario_actual()
    if not cambios_orden and not any(cambios_lineas.values()):
        return True, "No hay cambios que guardar."
    with _lock():
        ss, ws_o, t = _en_vivo()
        o = t["idx_o"].get(doc)
        conflictos, pet_por_hoja, pet_o, claves_hoja = [], {}, [], {}
        sello = _sello_ahora()
        if "resp" in cambios_orden and any(ln["doc"] == doc and ln.get("resp_hoja") for ln in t["lineas"]):
            return False, ("El responsable lo define la pestaña donde están las líneas: para cambiarlo, mueve las "
                           "filas de la orden a la pestaña de la otra persona en el Sheet.")
        for pos, cam in cambios_lineas.items():
            vivo = t["idx_d"].get((doc, pos))
            if vivo is None:
                conflictos.append(f"la línea {doc}/{pos} ya no existe")
                continue
            for k, (antes, nuevo) in cam.items():
                if _comparable(k, vivo.get(k)) != _comparable(k, antes):
                    conflictos.append(f"{doc}/{pos} · {NOMBRE_D[k]}")
            pet_por_hoja.setdefault(vivo["hoja"], []).extend(
                _peticiones_celdas(vivo["Fila"], t["hojas"][vivo["hoja"]]["pos"], {k: nv for k, (_, nv) in cam.items()}))
            claves_hoja.setdefault(vivo["hoja"], set()).update(cam)
        if cambios_orden:
            if o is None:
                conflictos += [f"la orden {doc} no tiene fila en la pestaña Órdenes"] if any(
                    antes for antes, _ in cambios_orden.values()) else []
            else:
                for k, (antes, nuevo) in cambios_orden.items():
                    if _comparable(k, o.get(k), True) != _comparable(k, antes, True):
                        conflictos.append(f"{NOMBRE_O[k]} de la orden {doc}")
        if conflictos:
            return False, ("Alguien cambió el Sheet mientras editabas (" + "; ".join(conflictos[:5])
                           + "). No se guardó nada: pulsa ↻ Actualizar y repite.")
        for hoja, claves in claves_hoja.items():
            _faltantes(t["hojas"][hoja]["pos"], claves, NOMBRE_D, hoja)
        _faltantes(t["pos_o"], set(cambios_orden) | {"fecha", "por"}, NOMBRE_O, PESTANA_ORDENES)
        for hoja, pet in pet_por_hoja.items():
            ws = ss.worksheet(hoja)
            _con_reintento(lambda: ws.batch_update(pet, value_input_option="RAW"))
        if o is None:
            _asegurar_filas(ws_o, t["ultima_o"] + 1)
            vals = {"doc": doc, "est": "ABIERTA", "fecha": sello, "por": usuario,
                    **{k: nv for k, (_, nv) in cambios_orden.items()}}
            pet_o = _peticiones_fila_nueva(t["ultima_o"] + 1, t["pos_o"], vals)
        else:
            vals = {"fecha": sello, "por": usuario, **{k: nv for k, (_, nv) in cambios_orden.items()}}
            pet_o = _peticiones_celdas(o["Fila"], t["pos_o"], vals)
        _con_reintento(lambda: ws_o.batch_update(pet_o, value_input_option="RAW"))
    invalidar()

    def _r(cam):
        return ", ".join(f"{k}={_texto(nv)}" for k, (_, nv) in cam.items())
    detalle = "; ".join([f"orden: {_r(cambios_orden)}"] * bool(cambios_orden)
                        + [f"{p}: {_r(c)}" for p, c in cambios_lineas.items() if c])
    sio.registrar_log("Órdenes: editar", bl=doc, categoria=CATEGORIA_LOG, detalle=detalle[:480])
    return True, f"Orden {doc} actualizada."


def plantilla_excel() -> bytes:
    """Excel vacío con los encabezados del Desglose y una pestaña de instrucciones."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = PESTANA_CARGA
    cols = [n for k, n, _ in COLUMNAS_D if k != "pend"] + ["PROVEEDOR", "RESPONSABLE", "EMPRESA"]
    for j, n in enumerate(cols, start=1):
        c = ws.cell(1, j, n)
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0C447C")
        ws.column_dimensions[c.column_letter].width = max(14, len(n) + 2)
    ws.freeze_panes = "A2"
    ins = wb.create_sheet("Instrucciones")
    for i, t in enumerate([
        "Una fila por línea de la orden. Obligatorias: Orden de Compras y Posición (juntas son la llave).",
        "Del reporte de SAP (ME2N) basta con Orden de Compras, Posición, Material, Texto breve, Cantidad de pedido, "
        "Fecha documento y Solicitante.",
        "RESPONSABLE (columna del final): la línea nueva se guarda en la pestaña de esa persona. Sin responsable no "
        "se carga, salvo que la orden ya exista (entonces va a la pestaña donde ya está).",
        "Celda sin dato = vacía. «Lo Llena Repuesto» y «PENDIENTE» se leen como vacío.",
        "ESTATUS: " + ", ".join(ESTATUS_LINEA) + ". «DESPACHADO A INTRANS» se separa en estatus y transportista.",
        "Las líneas nuevas entran como COLOCADA si no traen estatus.",
        "Fechas como fecha de Excel, o con el mes en letras (05-nov-2026). 05/11/2026 como TEXTO se rechaza.",
        "La carga nunca borra: una celda vacía no vacía la del Sheet, y lo que Repuesto ya llenó no se pisa.",
        "PROVEEDOR y EMPRESA son opcionales y solo se usan para órdenes nuevas o con esos datos vacíos.",
        "Las dos columnas ESTATUS: la 1.ª es el estatus de la línea; la 2.ª es informativa (DESPACHADO / COMPLETA / "
        "PARCIAL). ALMACÉN = transportista; ALMACÉN NUESTRO = almacén de Antillana.",
    ], start=1):
        ins.cell(i, 1, t)
    ins.column_dimensions["A"].width = 120
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PRESENTACIÓN
# ---------------------------------------------------------------------------
_CSS = (
    "<style>"
    ".oc-k { display:grid; grid-template-columns:repeat(5,1fr); gap:14px; margin:6px 0 14px; }"
    ".oc-k > div { color:#fff; border-radius:14px; min-height:92px; display:flex; flex-direction:column;"
    " align-items:center; justify-content:center; padding:14px 10px; text-align:center;"
    " box-shadow:0 2px 8px rgba(17,24,39,0.12); }"
    ".oc-k .e { font-size:0.82rem; font-weight:700; opacity:0.95; }"
    ".oc-k .n { font-size:1.9rem; font-weight:800; line-height:1.1; margin-top:6px; }"
    ".oc-k .s { font-size:0.7rem; opacity:0.9; margin-top:4px; }"
    ".oc-res { background:#F8FAFC; border:1px solid #E5E7EB; border-left:4px solid #0C447C; border-radius:10px;"
    " padding:10px 14px; font-size:0.9rem; color:#1F2937; margin:8px 0 12px; text-align:center; }"
    ".oc-res b { color:#0C447C; } .oc-res .r { color:#B91C1C; font-weight:700; }"
    ".oc-wrap { overflow-x:auto; margin:6px 0 10px; }"
    ".oc-t { width:100%; border-collapse:separate; border-spacing:0; background:#fff; border:1px solid #E5E7EB;"
    " border-radius:12px; overflow:hidden; font-size:0.84rem; box-shadow:0 1px 4px rgba(17,24,39,0.06); }"
    ".oc-t thead tr { background:linear-gradient(90deg,#2E86DE,#17A2B8); }"
    ".oc-t th { color:#fff; font-size:0.8rem; padding:13px 12px;"
    " text-align:center; vertical-align:middle; font-weight:700; white-space:nowrap; }"
    ".oc-t td { padding:11px 12px; border-top:1px solid #EEF2F6; text-align:center; vertical-align:middle;"
    " color:#111827; line-height:1.35; }"
    ".oc-t tbody tr:nth-child(even) { background:#FBFCFE; }"
    ".oc-t tbody tr:hover { background:#EFF6FF; }"
    ".oc-t td.oc { font-weight:800; color:#0C447C; white-space:nowrap; }"
    ".oc-t td.n { font-variant-numeric:tabular-nums; }"
    ".oc-t td.c { text-align:center; } .oc-t td.s { color:#6B7280; max-width:320px; }"
    ".oc-t td.ruta { padding:9px 14px; min-width:190px; }"
    ".oc-rw { display:flex; flex-direction:column; align-items:center; gap:6px; }"
    ".oc-ruta { display:inline-flex; align-items:center; }"
    ".oc-ruta .rp { flex:none; width:15px; height:15px; box-sizing:border-box; border-radius:50%; background:#fff;"
    " border:2px solid #CBD5E1; }"
    ".oc-ruta .rc { flex:none; width:13px; height:3px; background:#E2E8F0; }"
    ".oc-ruta .rc.ok { background:#16A34A; }"
    ".oc-ruta .rp.ok { background:#16A34A; border-color:#16A34A; }"
    ".oc-ruta .rp.act { background:#2563EB; border-color:#2563EB; box-shadow:0 0 0 4px rgba(37,99,235,0.22); }"
    ".oc-ruta .rp.bo { background:#F59E0B; border-color:#F59E0B; box-shadow:0 0 0 4px rgba(245,158,11,0.25); }"
    ".oc-rl { font-size:0.74rem; font-weight:800; color:#1D4ED8; white-space:nowrap; }"
    ".oc-rl.bo { color:#B45309; } .oc-rl.sin { color:#9CA3AF; font-weight:600; }"
    ".oc-ley { display:flex; flex-wrap:wrap; justify-content:center; align-items:center; gap:6px 8px; margin:8px 0 2px;"
    " font-size:0.78rem; color:#374151; }"
    ".oc-ley .s { background:#F1F5F9; border:1px solid #E2E8F0; border-radius:999px; padding:3px 11px; font-weight:700; }"
    ".oc-ley i { font-style:normal; color:#9CA3AF; }"
    ".oc-ley .k { display:inline-flex; align-items:center; gap:5px; margin-left:10px; color:#6B7280; }"
    ".oc-ley .k u { display:inline-block; width:11px; height:11px; border-radius:50%; text-decoration:none; }"
    ".oc-ley .k .g { background:#16A34A; } .oc-ley .k .a { background:#2563EB; } .oc-ley .k .m { background:#F59E0B; }"
    ".oc-ley .k .p { background:#fff; border:2px solid #CBD5E1; box-sizing:border-box; }"
    ".oc-t .rojo { color:#B91C1C; font-weight:700; } .oc-t .ambar { color:#B45309; font-weight:700; }"
    ".oc-t .v { color:#9CA3AF; }"
    ".oc-chip { display:inline-block; padding:3px 9px; border-radius:999px; font-size:0.72rem; font-weight:700;"
    " white-space:nowrap; border:1px solid transparent; }"
    ".e-col { background:#EEF2FF; color:#3730A3; border-color:#E0E7FF; }"
    ".e-conf { background:#E0F2FE; color:#075985; border-color:#BAE6FD; }"
    ".e-bo { background:#FEF3C7; color:#92400E; border-color:#FDE68A; }"
    ".e-des { background:#E8F1FC; color:#1D5FA8; border-color:#CFE2F8; }"
    ".e-pto { background:#F3E8FF; color:#6B21A8; border-color:#E9D5FF; }"
    ".e-par { background:#DCFCE7; color:#166534; border-color:#BBF7D0; }"
    ".e-comp { background:#F3F4F6; color:#374151; border-color:#E5E7EB; }"
    ".oc-emp { display:inline-block; padding:3px 9px; border-radius:8px; font-size:0.74rem; font-weight:700;"
    " border:1px solid transparent; white-space:nowrap; }"
    ".emp-a { background:#E8F1FC; color:#1D5FA8; border-color:#CFE2F8; }"
    ".emp-t { background:#E2F5F2; color:#0F766E; border-color:#C4EBE5; }"
    ".emp-m { background:#FCEFD9; color:#9A5B0B; border-color:#F6DDB4; }"
    ".oc-ficha { border:1px solid #E5E7EB; border-left:5px solid #2E86DE; border-radius:12px; padding:16px 20px;"
    " margin:10px 0 14px; background:#fff; }"
    ".oc-fh { display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap;"
    " margin-bottom:6px; }"
    ".oc-ft { font-size:1.3rem; font-weight:800; color:#0C447C; } .oc-ft span { font-weight:600; color:#374151;"
    " font-size:1rem; }"
    ".oc-etapas { display:flex; gap:8px; flex-wrap:wrap; margin:14px 0 6px; align-items:center; }"
    ".oc-fr { display:flex; align-items:center; gap:8px; flex-wrap:wrap; justify-content:flex-end; }"
    ".oc-eta { display:inline-block; padding:4px 12px; border-radius:999px; font-size:0.8rem; font-weight:700;"
    " background:#E0F2FE; color:#075985; border:1px solid #BAE6FD; white-space:nowrap; }"
    ".oc-eta.sin { background:#FEF3C7; color:#92400E; border-color:#FDE68A; }"
    ".oc-j { display:flex; flex-wrap:wrap; align-items:center; gap:14px 26px; margin:6px 0 4px; }"
    ".oc-gauge { flex:none; width:250px; height:auto; }"
    ".oc-cifras { flex:none; display:grid; grid-template-columns:repeat(2,minmax(128px,1fr)); gap:10px; }"
    ".oc-cf { border-radius:12px; padding:9px 16px; }"
    ".oc-cf .k { font-size:0.8rem; font-weight:700; }"
    ".oc-cf .b { font-size:1.7rem; font-weight:800; line-height:1.15; }"
    ".oc-cf .u { font-size:0.7rem; font-weight:600; opacity:0.85; }"
    ".oc-cf.c-ped { background:#EFF6FF; color:#1E40AF; } .oc-cf.c-des { background:#ECFDF5; color:#15803D; }"
    ".oc-cf.c-pen { background:#FFFBEB; color:#B45309; } .oc-cf.c-lin { background:#F5F3FF; color:#5B21B6; }"
    ".oc-chips { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; padding-top:12px;"
    " border-top:1px solid #EEF2F6; }"
    ".oc-chp { background:#EEF4FB; border:1px solid #D6E4F5; border-radius:999px; padding:5px 13px;"
    " font-size:0.84rem; font-weight:600; color:#1F2937; white-space:nowrap; }"
    ".oc-chp b { font-weight:800; }"
    ".oc-flow { display:flex; flex:1 1 540px; min-width:0; margin:0; padding:0; }"
    ".oc-fs { flex:1; min-width:84px; text-align:center; padding:0 4px; }"
    ".oc-fs .who { min-height:34px; display:flex; align-items:flex-end; justify-content:center; margin-bottom:6px; }"
    ".oc-flow.sq .who { min-height:0; margin:0; }"
    ".oc-who { display:inline-flex; align-items:center; gap:6px; background:#0C447C; color:#fff; border-radius:999px;"
    " padding:3px 11px 3px 4px; font-size:0.74rem; font-weight:700; }"
    ".oc-who i { font-style:normal; width:22px; height:22px; border-radius:50%; background:#fff; color:#0C447C;"
    " display:flex; align-items:center; justify-content:center; font-size:0.68rem; font-weight:800; }"
    ".oc-fs .nm { font-size:0.82rem; font-weight:800; color:#0F172A; letter-spacing:0.01em; }"
    ".oc-fs.pend .nm { color:#9CA3AF; }"
    ".oc-fs .nd { position:relative; height:34px; margin-top:6px; }"
    ".oc-fs:not(:first-child) .nd::before { content:''; position:absolute; top:calc(50% - 2px); left:-50%; width:100%;"
    " height:4px; border-radius:2px; background:#E5E7EB; }"
    ".oc-fs.ok .nd::before, .oc-fs.ok + .oc-fs.act .nd::before, .oc-fs.act + .oc-fs.act .nd::before"
    " { background:linear-gradient(90deg,#22C55E,#16A34A); }"
    ".oc-dot { position:absolute; z-index:1; left:50%; top:50%; transform:translate(-50%,-50%); width:30px; height:30px;"
    " border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:0.95rem; background:#fff;"
    " border:2px dashed #D1D5DB; color:#9CA3AF; }"
    ".oc-fs.pend .oc-dot { opacity:0.55; filter:grayscale(1); }"
    ".oc-fs.ok .oc-dot { background:linear-gradient(135deg,#4ADE80,#16A34A); border:2px solid #fff; color:#fff;"
    " font-weight:800; box-shadow:0 2px 8px rgba(22,163,74,0.35); }"
    ".oc-fs.act .oc-dot { background:linear-gradient(135deg,#60A5FA,#2563EB); border:2px solid #fff;"
    " box-shadow:0 0 0 0 rgba(37,99,235,0.5); animation:oc-pulso 2s infinite; }"
    "@keyframes oc-pulso { 0% { box-shadow:0 0 0 0 rgba(37,99,235,0.45); } 70% { box-shadow:0 0 0 14px rgba(37,99,235,0); }"
    " 100% { box-shadow:0 0 0 0 rgba(37,99,235,0); } }"
    ".oc-fs .ct { margin-top:4px; white-space:nowrap; font-size:0.76rem; font-weight:700; color:#1D4ED8; }"
    ".oc-fs .ct small { display:inline; font-weight:600; color:#1D4ED8; font-size:0.76rem; }"
    ".oc-fs .ct small::before { content:\" · \"; }"
    ".oc-fs.ok .ct { color:#15803D; } .oc-fs.pend .ct { color:#C4C9D1; font-weight:600; }"
    ".oc-nota { background:#FFFBEB; border:1px solid #FDE68A; border-left:4px solid #F0B90B; border-radius:10px;"
    " padding:9px 14px; font-size:0.88rem; margin-top:10px; }"
    ".oc-av { width:42px; height:42px; border-radius:50%; background:#0C447C; color:#fff; font-weight:800;"
    " display:flex; align-items:center; justify-content:center; flex:none; }"
    ".oc-ch { display:flex; gap:12px; align-items:center; margin-bottom:10px; }"
    ".oc-nom { font-weight:800; font-size:1.02rem; } .oc-rol { font-size:0.76rem; color:#6B7280; }"
    ".oc-mini { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:10px; }"
    ".oc-mini div { background:#F8FAFC; border:1px solid #EEF2F6; border-radius:10px; padding:6px; text-align:center; }"
    ".oc-mini b { display:block; font-size:1.25rem; } .oc-mini span { font-size:0.74rem; color:#6B7280; }"
    ".oc-mini .a { color:#B45309; } .oc-mini .r { color:#B91C1C; }"
    "div[class*='st-key-oc_old_'] button { border-color:#FCA5A5 !important; background:#FEF2F2 !important;"
    " color:#991B1B !important; }"
    "div[class*='st-key-oc_old_'] button p { color:#991B1B !important; font-weight:700; }"
    "@media (max-width:900px) { .oc-k { grid-template-columns:repeat(2,1fr); } }"
    "</style>"
)
_CLASE_EST = {"COLOCADA": "e-col", "CONFIRMADA": "e-conf", "BACKORDER": "e-bo", "DESPACHADO": "e-des",
              "EN TRÁNSITO": "e-des", "EN PUERTO": "e-pto", "COMPLETO": "e-comp", "CANCELADO": "e-comp"}


def _fd(d) -> str:
    return "—" if d is None or (not isinstance(d, (date, datetime)) and pd.isna(d)) else f"{d.day:02d}/{d.month:02d}/{d.year}"


def _chip_linea(est: str) -> str:
    return f'<span class="oc-chip {_CLASE_EST.get(est, "e-comp")}">{_esc(_etq(est))}</span>' if est else \
        '<span class="v">—</span>'


def _chip_derivado(d: str) -> str:
    base = d.replace(" parcial", "")
    if d == "Despacho parcial" or d.endswith(" parcial"):
        clase = "e-par"
    elif d == "En camino":
        clase = "e-des"
    else:
        clase = _CLASE_EST.get(base.upper().replace("EN TRANSITO", "EN TRÁNSITO"), "e-comp")
    return f'<span class="oc-chip {clase}">{_esc(d)}</span>'


def _chip_emp(e: str) -> str:
    if not e:
        return '<span class="v">—</span>'
    clase = "emp-t" if "tecni" in _norm(e) else "emp-m" if "motor" in _norm(e) else "emp-a"
    return f'<span class="oc-emp {clase}">{_esc(e)}</span>'


def _celda(x, clase: str = "") -> str:
    vacio = x is None or x == "" or (isinstance(x, float) and pd.isna(x))
    return '<td class="v c">—</td>' if vacio else f'<td class="{clase}">{x if clase.endswith("raw") else _esc(str(x))}</td>'


def _dias_html(dias, sin_sello: bool = False) -> str:
    if dias is None or pd.isna(dias):
        return '<span class="v">—</span>'
    d = int(dias)
    txt = "hoy" if d == 0 else f"hace {d} d"
    if sin_sello:
        txt += "*"
    clase = "rojo" if es_viejo(d) else ("ambar" if d >= DIAS_SIN_ACTUALIZAR - 2 else "")
    return f'<span class="{clase}">{txt}</span>'


def _tabla_ordenes(df: pd.DataFrame) -> str:
    cab = ["No. Orden", "Proveedor", "Factura", "Recepción", "Colocación", "Estatus", "Responsable", "Empresa", "ETA",
           "Actualizado", "Líneas", "Comentarios"]
    filas = []
    for r in df.head(MAX_FILAS_TABLA).itertuples():
        filas.append(
            "<tr>" + f'<td class="oc">{_esc(r.Doc)}</td>' + _celda(r.Proveedor) + _celda(r.Facturas)
            + _celda(_fd(r.Recepcion) if r.Recepcion else "") + _celda(_fd(r.Colocacion) if r.Colocacion else "")
            + f"<td>{_chip_derivado(r.Derivado)}</td>" + _celda(r.Responsable)
            + f"<td>{_chip_emp(r.Empresa)}</td>" + _celda(_fd(r.ETA) if r.ETA else "")
            + f"<td>{_dias_html(r.Dias, r.SinSello)}</td>"
            + f'<td class="c">{r.Abiertas}/{r.Lineas}</td>' + _celda(r.Comentario, "s") + "</tr>")
    return ('<div class="oc-wrap"><table class="oc-t"><thead><tr>' + "".join(f"<th>{c}</th>" for c in cab)
            + "</tr></thead><tbody>" + "".join(filas) + "</tbody></table></div>")


def _tabla_lineas(df: pd.DataFrame, con_orden: bool = True) -> str:
    cab = (["No. Orden"] if con_orden else []) + ["Pos.", "Material", "Mat. antiguo", "No. Parte", "Descripción", "Pedida",
                                                 "Despachada", "Pendiente", "Estatus", "Transportista", "Embarque",
                                                 "ETA", "Puerto", "Almacén", "Solicitante", "Cliente", "Comentario"]
    filas = []
    for r in df.head(MAX_FILAS_TABLA).itertuples():
        rojo = r.Pend is not None and not pd.isna(r.Pend) and r.Pend > 0 and (r.Estatus in AVANZADOS or r.Estatus == "BACKORDER")
        filas.append(
            "<tr>" + (f'<td class="oc">{_esc(r.Doc)}</td>' if con_orden else "") + f'<td class="c">{_esc(r.Pos)}</td>'
            + _celda(r.Material) + _celda(r.MatAnt) + _celda(r.NParte) + _celda(r.Texto)
            + f'<td class="n">{_cant(r.Pedida)}</td><td class="n">{_cant(r.Desp)}</td>'
            + f'<td class="n {"rojo" if rojo else ""}">{_cant(r.Pend)}</td>'
            + f'<td class="ruta">{_ruta_linea(r.Estatus)}</td>' + _celda(r.Transp) + _celda(_fd(r.Embarque) if r.Embarque else "")
            + _celda(_fd(r.ETA) if r.ETA else "") + _celda(r.Puerto) + _celda(r.Almacen) + _celda(r.Solicitante)
            + _celda(r.Cliente)
            + _celda(r.Coment, "s") + "</tr>")
    # La leyenda solo va en las listas de varias órdenes: la ficha ya trae el recorrido grande arriba.
    return ((_leyenda_ruta() if con_orden else "")
            + '<div class="oc-wrap"><table class="oc-t"><thead><tr>' + "".join(f"<th>{c}</th>" for c in cab)
            + "</tr></thead><tbody>" + "".join(filas) + "</tbody></table></div>")


def _tarjetas_kpi(k: dict) -> str:
    t = lambda color, e, n, s: (f'<div style="background:{color};"><div class="e">{e}</div><div class="n">{n}</div>'
                                f'<div class="s">{s}</div></div>')
    return ('<div class="oc-k">' + t("#0C447C", "Órdenes abiertas", k["abiertas"], "con líneas pendientes")
            + t("#2E86DE", "Sin despachar", k["sin_despachar"], "colocadas o confirmadas")
            + t("#B45309", "Backorder / parcial", k["backorder"], "con línea en espera")
            + t("#17A2B8", "En camino", k["camino"], "despachadas, en tránsito o puerto")
            + t("#B91C1C", f"Sin actualizar +{DIAS_SIN_ACTUALIZAR} d", k["viejas"], "Taller no sabe de ellas")
            + "</div>")


def _encabezado(hora, libro: str = "") -> None:
    """`libro` = nombre del Sheet que la app está leyendo: si lo que se escribe en el Sheet no aparece, lo primero es
    comprobar que este nombre es el del Sheet en el que se trabaja (el secret ORDENES_SHEET_ID decide cuál)."""
    sello = f"Sheet de órdenes «{libro}» leído a las " if libro else "Sheet de órdenes leído a las "
    sello += f"{hora.strftime('%I:%M %p').lstrip('0').lower()} (hora RD)"
    st.markdown(encabezado_html(NOMBRE_SECCION, "taller", hoy_rd().year, sello, " · Compras internacionales"),
                unsafe_allow_html=True)


def _csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def _filtro(etiqueta: str, opciones: list, clave: str, defecto=None):
    restaurar_filtro(clave, opciones, defecto if defecto in opciones else opciones[0])
    v = st.selectbox(etiqueta, opciones, key=clave)
    recordar_filtro(clave)
    return v


ETAPAS = [("COLOCADA", "Colocada"), ("CONFIRMADA", "Confirmada"), ("DESPACHADO", "Despachada"),
          ("EN TRÁNSITO", "En tránsito"), ("EN PUERTO", "En puerto"), ("COMPLETO", "Recibida")]


def _rango_etapa(est: str) -> int:
    """Posición de un estatus de línea en el recorrido. BACKORDER = confirmada pero en espera del proveedor;
    sin estatus = -1."""
    if est == "BACKORDER":
        return 1
    for i, (k, _) in enumerate(ETAPAS):
        if k == est:
            return i
    return -1


def _ruta_linea(est: str) -> str:
    """Camino por puntos de UNA línea (mismas etapas y colores que el recorrido de la ficha): verde = ya pasó,
    azul = está aquí, gris = falta. BACKORDER = punto ámbar en «Confirmada» (espera al proveedor).
    Sin estatus: todo en gris."""
    rango = _rango_etapa(est)
    bo = est == "BACKORDER"
    piezas = []
    for i, (_, nombre) in enumerate(ETAPAS):
        if i:
            piezas.append(f'<span class="rc{" ok" if i <= rango else ""}"></span>')
        estado = "pend" if rango < 0 or i > rango else ("ok" if i < rango else ("bo" if bo else "act"))
        piezas.append(f'<span class="rp {estado}" title="{nombre}"></span>')
    if bo:
        etiqueta, clase = "Backorder", " bo"
    elif rango >= 0:
        etiqueta, clase = ETAPAS[rango][1], ""
    else:
        etiqueta, clase = _etq(est), " sin"
    return (f'<div class="oc-rw"><div class="oc-ruta">{"".join(piezas)}</div>'
            f'<div class="oc-rl{clase}">{_esc(etiqueta)}</div></div>')


def _leyenda_ruta() -> str:
    """Cómo leer el camino por puntos: etapas en orden y qué significa cada color."""
    pasos = '<i>›</i>'.join(f'<span class="s">{_ICONO_ETAPA[k]} {n}</span>' for k, n in ETAPAS)
    return ('<div class="oc-ley"><b>Camino de la mercancía:</b> ' + pasos
            + '<span class="k"><u class="g"></u>ya pasó</span><span class="k"><u class="a"></u>está aquí</span>'
            + '<span class="k"><u class="m"></u>backorder</span><span class="k"><u class="p"></u>falta</span></div>')


def _falta(txt: str) -> str:
    return f'<span class="falta">{txt}</span>'


def _lista(serie) -> list:
    return sorted({str(x) for x in serie if x is not None and str(x).strip() and not pd.isna(x)})


def _medidor(pct: int) -> str:
    """Medidor semicircular con el % de unidades despachadas sobre las pedidas (líneas abiertas)."""
    largo = math.pi * 110
    return ('<svg class="oc-gauge" viewBox="0 0 260 150" role="img" aria-label="' + f"{pct}% despachado" + '">'
            '<defs><linearGradient id="ocg" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#2E86DE"/>'
            '<stop offset="1" stop-color="#17A2B8"/></linearGradient></defs>'
            '<path d="M20 130 A110 110 0 0 1 240 130" fill="none" stroke="#EEF2F6" stroke-width="20" '
            'stroke-linecap="round"/>'
            f'<path d="M20 130 A110 110 0 0 1 240 130" fill="none" stroke="url(#ocg)" stroke-width="20" '
            f'stroke-linecap="round" stroke-dasharray="{largo * pct / 100:.1f} {largo:.1f}"/>'
            f'<text x="130" y="104" text-anchor="middle" font-size="54" font-weight="800" fill="#0C447C">{pct}%</text>'
            '<text x="130" y="130" text-anchor="middle" font-size="15" font-weight="700" fill="#6B7280">'
            'despachado</text></svg>')


def _tarjetas_seguimiento(o: pd.Series, lineas: pd.DataFrame) -> str:
    """Medidor del % despachado y cuatro cifras (pedidas, despachadas, pendientes, líneas). Se calcula sobre las
    líneas ABIERTAS (las completas o canceladas no se muestran). Fechas, personas y ETA van en `_chips_ficha`
    y `_chip_eta`; puerto, almacén y transportista están en la tabla de líneas."""
    ped = float(lineas["Pedida"].fillna(0).sum()) if len(lineas) else 0.0
    desp = float(lineas["Desp"].fillna(0).sum()) if len(lineas) else 0.0
    pct = 0 if not ped else min(100, round(100 * desp / ped))
    en_bo = int((lineas["Estatus"] == "BACKORDER").sum()) if len(lineas) else 0
    cerradas = max(int(o.Lineas) - int(o.Abiertas), 0)

    def t(clase, icono, k, valor, sub=""):
        u = f'<div class="u">{_esc(sub)}</div>' if sub else ""
        return f'<div class="oc-cf {clase}"><div class="k">{icono} {k}</div><div class="b">{valor}</div>{u}</div>'

    return (_medidor(pct) + '<div class="oc-cifras">'
            + t("c-ped", "📦", "Pedidas", _cant(ped))
            + t("c-pen", "⏳", "Pendientes", _cant(o.PendUnid), f"{en_bo} línea(s) en backorder" if en_bo else "")
            + t("c-des", "🚚", "Despachadas", _cant(desp))
            + t("c-lin", "🧾", "Líneas", _cant(len(lineas)), f"{cerradas} cerrada(s) no se muestran" if cerradas else "")
            + "</div>")


def _chip_eta(lineas: pd.DataFrame) -> str:
    """ETA de la orden para la cabecera. Una orden trae varias líneas con fechas distintas (o ninguna): un solo
    «ETA» sin más parecería valer para toda la orden, así que se dice la primera, el rango y cuántas líneas tienen
    fecha. Sin ETA y con algo ya en camino: aviso ámbar. Sin nada en camino no hay nada que avisar."""
    n = len(lineas)
    etas = [x for x in lineas["ETA"] if x is not None and not pd.isna(x)] if n else []
    if not etas:
        en_camino = n and lineas["Estatus"].isin(["DESPACHADO", "EN TRÁNSITO", "EN PUERTO"]).any()
        return '<span class="oc-eta sin">🛬 ETA sin fecha</span>' if en_camino else ""
    menor, mayor = min(etas), max(etas)
    todas = len(etas) == n
    if mayor != menor:
        txt = f"ETA {_fd(menor)} → {_fd(mayor)}"
    else:
        txt = f"{'ETA' if todas else 'Próx. ETA'} {_fd(menor)}"
    if not todas:
        txt += f" · {len(etas)} de {n} líneas"
    return f'<span class="oc-eta">🛬 {_esc(txt)}</span>'


def _chips_ficha(o: pd.Series) -> str:
    """Quién y cuándo, como etiquetas con icono. Lo que no tiene dato no se dibuja."""
    c = [("👤", "Resp.", o.Responsable), ("🙋", "Solicita", o.Solicitantes),
         ("🗓️", "Solicitada", _fd(o.Recepcion) if o.Recepcion else ""),
         ("📝", "Colocada", _fd(o.Colocacion) if o.Colocacion else "")]
    return ('<div class="oc-chips">' + "".join(f'<span class="oc-chp">{i} {k} <b>{_esc(str(v))}</b></span>'
                                               for i, k, v in c if v) + "</div>")


_ICONO_ETAPA = {"COLOCADA": "📝", "CONFIRMADA": "🤝", "DESPACHADO": "📦", "EN TRÁNSITO": "🚚", "EN PUERTO": "⚓",
                "COMPLETO": "🏁"}


def _quien(nombre: str) -> str:
    """Chip con iniciales: quién hizo la etapa. Vacío si no se sabe."""
    if not nombre:
        return ""
    ini = "".join(w[0] for w in nombre.split()[:2]).upper()
    return f'<span class="oc-who"><i>{_esc(ini)}</i>{_esc(nombre)}</span>'


def _seguimiento_html(o: pd.Series, lineas: pd.DataFrame, quien: dict | None = None) -> str:
    """Tarjetas de resumen + recorrido de la orden con lo que el Sheet sabe HOY (estatus actual de cada línea).
    `quien`: {clave de etapa: nombre} de quien hizo esa etapa; el Sheet no guarda cuándo pasó de una a otra."""
    quien = quien or {}
    rangos = [_rango_etapa(e) for e in lineas["Estatus"]] if len(lineas) else []
    cerradas = max(int(o.Lineas) - int(o.Abiertas), 0)
    minimo = min(rangos) if rangos else len(ETAPAS) - 1   # sin líneas abiertas: todo lo anterior a «Recibida» pasó
    pasos = []
    for i, (clave, nombre) in enumerate(ETAPAS):
        aqui = sum(1 for r in rangos if r == i) if clave != "COMPLETO" else cerradas
        # act = hay líneas en esta etapa · ok = TODAS las líneas abiertas ya la pasaron · pend = aún no
        estado = "act" if aqui else ("ok" if minimo > i else "pend")
        enlas = lineas[[_rango_etapa(e) == i for e in lineas["Estatus"]]] if len(lineas) and clave != "COMPLETO" else None
        if estado == "act":
            uds = ""
            if enlas is not None and len(enlas):
                campo = "Pend" if i <= 1 else "Desp"
                uds = f"<small>{_cant(enlas[campo].fillna(0).sum())} uds</small>"
            cuenta = f"{aqui} línea(s){uds}"
        else:
            cuenta = "hecho" if estado == "ok" else "—"
        punto = "✓" if estado == "ok" else _ICONO_ETAPA[clave]
        pasos.append(f'<div class="oc-fs {estado}"><div class="who">{_quien(quien.get(clave, ""))}</div>'
                     f'<div class="nm">{nombre}</div><div class="nd"><div class="oc-dot">{punto}</div></div>'
                     f'<div class="ct">{cuenta}</div></div>')
    return ('<div class="oc-j">' + _tarjetas_seguimiento(o, lineas)
            + f'<div class="oc-flow{"" if any(quien.values()) else " sq"}">' + "".join(pasos) + "</div></div>")


def _ficha(o: pd.Series, lineas: pd.DataFrame) -> None:
    """Orden completa: % despachado, cifras, camino por etapas, ETA y chips con quién y cuándo."""
    nota = (f'<div class="oc-nota"><b>Comentario para Taller:</b> {_esc(o.Comentario)}</div>' if o.Comentario else "")
    st.markdown(
        f'<div class="oc-ficha"><div class="oc-fh"><div class="oc-ft">Orden {_esc(o.Doc)} '
        f'<span>· {_esc(o.Proveedor or "proveedor sin registrar")}</span></div>'
        f'<div class="oc-fr">{_chip_eta(lineas)}{_chip_derivado(o.Derivado)}{_chip_emp(o.Empresa)}</div></div>'
        f'{_seguimiento_html(o, lineas)}{_chips_ficha(o)}{nota}</div>', unsafe_allow_html=True)
    st.markdown(_tabla_lineas(lineas, con_orden=False), unsafe_allow_html=True)
    if len(lineas) > MAX_FILAS_TABLA:
        st.caption(f"Se muestran {MAX_FILAS_TABLA} de {len(lineas)} líneas.")


def _mostrar_orden(doc: str, ordenes: pd.DataFrame, lineas: pd.DataFrame, hay_cerradas: int = 0) -> None:
    sel = ordenes[ordenes["Doc"] == doc]
    if sel.empty:
        st.info(f"La orden {doc} no está abierta (completa, cancelada o ya no existe).")
        return
    ls = lineas[lineas["Doc"] == doc]
    _ficha(sel.iloc[0], ls)
    if len(ls) < int(sel.iloc[0].Lineas):
        st.caption(f"{int(sel.iloc[0].Lineas) - len(ls)} línea(s) completas o canceladas no se muestran.")


# ------------------------------- pestañas -------------------------------------
def _vista_general(d: dict, usuario: str, rol: str) -> None:
    o, l = d["ordenes"], d["lineas"]
    st.markdown(_tarjetas_kpi(kpis(o)), unsafe_allow_html=True)
    if o.empty:
        st.info("No hay órdenes abiertas. Se cargan en el Sheet de órdenes o con la carga masiva.")
        return
    resps = ["Todos"] + sorted(o["Responsable"].unique())
    estados = ["Todos"] + sorted(o["Derivado"].unique())
    provs = ["Todos"] + sorted({p for p in o["Proveedor"] if p})
    emps = ["Todas"] + sorted({e for e in o["Empresa"] if e})
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        r = _filtro("Responsable", resps, "oc_g_resp", usuario if rol == "compras" else "Todos")
    with c2:
        p = _filtro("Proveedor", provs, "oc_g_prov")
    with c3:
        e = _filtro("Estatus", estados, "oc_g_est")
    with c4:
        em = _filtro("Empresa", emps, "oc_g_emp", "Todas")
    with c5:
        q = st.text_input("Buscar orden, factura, material, descripción…", key="oc_g_q")
    v = o
    if r != "Todos":
        v = v[v["Responsable"] == r]
    if p != "Todos":
        v = v[v["Proveedor"] == p]
    if e != "Todos":
        v = v[v["Derivado"] == e]
    if em != "Todas":
        v = v[v["Empresa"] == em]
    if q.strip():
        v = v[v["Haystack"].str.contains(_norm(q), regex=False)]
    k = kpis(v)
    viejas = v[v["Dias"].apply(es_viejo)]
    por_resp = ", ".join(f"{n} {c}" for n, c in viejas["Responsable"].value_counts().items())
    st.markdown(
        f'<div class="oc-res"><b>{k["abiertas"]} órdenes abiertas</b> · {k["backorder"]} con backorder o parcial · '
        f'<span class="r">{k["viejas"]} sin actualizar en más de {DIAS_SIN_ACTUALIZAR} días</span>'
        + (f" ({_esc(por_resp)})" if por_resp else "")
        + f". {d['cerradas']} orden(es) Completa/Cancelada no se muestran.</div>", unsafe_allow_html=True)
    if v.empty:
        st.caption("No hay órdenes con esta selección.")
        return
    st.markdown(_tabla_ordenes(v), unsafe_allow_html=True)
    st.caption(f"Mostrando {min(len(v), MAX_FILAS_TABLA)} de {len(v)} · orden: más tiempo sin actualizar primero. "
               "* = sin sello de actualización: se cuenta desde la fecha del documento.")
    st.download_button("⬇ Descargar CSV", _csv(v.drop(columns=["Haystack"])), "ordenes_de_compra.csv", "text/csv",
                       key="oc_g_csv")
    docs = ["—"] + list(v["Doc"])
    abrir = st.selectbox("Ver el desglose de una orden", docs, key="oc_g_abrir")
    if abrir != "—":
        _mostrar_orden(abrir, o, l)


def _slug(t: str) -> str:
    return re.sub(r"\W+", "_", str(t))


def _vista_responsable(d: dict, usuario: str, rol: str) -> None:
    o, l = d["ordenes"], d["lineas"]
    if o.empty:
        st.info("No hay órdenes abiertas.")
        return
    st.markdown('<div class="oc-res">Cada analista con sus órdenes abiertas. En <span class="r">rojo</span> las que llevan '
                f'más de {DIAS_SIN_ACTUALIZAR} días sin actualizar. <b>Un clic en un número</b> abre la orden con su '
                "desglose; <b>Ver todo lo pendiente</b> lista todas las líneas de esa persona.</div>",
                unsafe_allow_html=True)
    resps = sorted(o["Responsable"].unique(), key=lambda x: (x == SIN_ASIGNAR, x))
    if rol == "compras" and usuario in resps:
        resps = [usuario] + [x for x in resps if x != usuario]
    for fila in range(0, len(resps), 3):
        cols = st.columns(3)
        for col, nombre in zip(cols, resps[fila:fila + 3]):
            sub = o[o["Responsable"] == nombre]
            k = kpis(sub)
            with col:
                with st.container(border=True, key=f"oc_card_{_slug(nombre)}"):
                    ini = "".join(p[0] for p in nombre.split()[:2]).upper() or "?"
                    st.markdown(
                        f'<div class="oc-ch"><div class="oc-av">{_esc(ini)}</div><div><div class="oc-nom">{_esc(nombre)}'
                        f'</div><div class="oc-rol">Compras internacionales</div></div></div>'
                        f'<div class="oc-mini"><div><b>{k["abiertas"]}</b><span>Abiertas</span></div>'
                        f'<div><b class="a">{k["backorder"]}</b><span>Backorder / parcial</span></div>'
                        f'<div><b class="r">{k["viejas"]}</b><span>+{DIAS_SIN_ACTUALIZAR} d sin actualizar</span></div>'
                        "</div>", unsafe_allow_html=True)
                    docs = list(sub["Doc"])
                    for i in range(0, min(len(docs), MAX_BOTONES_TARJETA), 3):
                        bs = st.columns(3)
                        for b, doc in zip(bs, docs[i:i + 3]):
                            viejo = es_viejo(sub.loc[sub["Doc"] == doc, "Dias"].iloc[0])
                            with b:
                                if st.button(doc, key=f"{'oc_old_' if viejo else 'oc_b_'}{_slug(nombre)}_{_slug(doc)}",
                                             width="stretch",
                                             type="primary" if st.session_state.get("oc_sel_doc") == doc else "secondary"):
                                    st.session_state["oc_sel_doc"], st.session_state["oc_sel_resp"] = doc, nombre
                    if len(docs) > MAX_BOTONES_TARJETA:
                        st.caption(f"y {len(docs) - MAX_BOTONES_TARJETA} más: usa «Ver todo lo pendiente».")
                    if st.button("Ver todo lo pendiente", key=f"oc_todo_{_slug(nombre)}", width="stretch"):
                        st.session_state["oc_sel_resp"] = nombre
                        st.session_state.pop("oc_sel_doc", None)
    doc, resp = st.session_state.get("oc_sel_doc"), st.session_state.get("oc_sel_resp")
    if doc and doc in set(o["Doc"]):
        _mostrar_orden(doc, o, l)
    elif resp in resps:
        sub = l[l["Responsable"] == resp]
        st.markdown(f'<div class="oc-res">Todo lo pendiente de <b>{_esc(resp)}</b>: {len(sub)} líneas en '
                    f'{sub["Doc"].nunique()} órdenes.</div>', unsafe_allow_html=True)
        st.markdown(_tabla_lineas(sub), unsafe_allow_html=True)
        st.download_button("⬇ Descargar CSV", _csv(sub.drop(columns=["Responsable"])),
                           f"pendientes_{_slug(resp)}.csv", "text/csv", key="oc_r_csv")


def _vista_desglose(d: dict, usuario: str, rol: str) -> None:
    o, l = d["ordenes"], d["lineas"]
    if l.empty:
        st.info("No hay líneas abiertas en la pestaña Desglose.")
        return
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        r = _filtro("Responsable", ["Todos"] + sorted(l["Responsable"].unique()), "oc_d_resp",
                    usuario if rol == "compras" else "Todos")
    docs_r = sorted(l["Doc"].unique() if r == "Todos" else l.loc[l["Responsable"] == r, "Doc"].unique())
    with c2:
        dc = _filtro("Orden", ["Todas"] + docs_r, "oc_d_doc", "Todas")
    with c3:
        es = _filtro("Estatus", ["Todos"] + [e for e in ESTATUS_LINEA if e in set(l["Estatus"])]
                     + (["SIN ESTATUS"] if (l["Estatus"] == "").any() else []), "oc_d_est")
    with c4:
        tr = _filtro("Transportista", ["Todos"] + sorted({x for x in l["Transp"] if x}), "oc_d_tr")
    with c5:
        q = st.text_input("Buscar material, descripción, cliente…", key="oc_d_q")
    v = l
    if r != "Todos":
        v = v[v["Responsable"] == r]
    if dc != "Todas":
        v = v[v["Doc"] == dc]
    if es != "Todos":
        v = v[v["Estatus"] == ("" if es == "SIN ESTATUS" else es)]
    if tr != "Todos":
        v = v[v["Transp"] == tr]
    if q.strip():
        n = _norm(q)
        v = v[v.apply(lambda x: n in _norm(" ".join(str(x[c]) for c in
                                                    ("Doc", "Material", "MatAnt", "NParte", "Texto", "Cliente", "Solicitante", "Factura"))), axis=1)]
    if dc != "Todas":
        _mostrar_orden(dc, o, l)
        return
    pend = v["Pend"].fillna(0)
    st.markdown(f'<div class="oc-res"><b>{len(v)} líneas</b> en {v["Doc"].nunique()} órdenes · '
                f'{_cant(v["Pedida"].fillna(0).sum())} unidades pedidas · {_cant(v["Desp"].fillna(0).sum())} despachadas · '
                f'<span class="r">{_cant(pend.sum())} pendientes</span></div>', unsafe_allow_html=True)
    if v.empty:
        st.caption("No hay líneas con esta selección.")
        return
    st.markdown(_tabla_lineas(v), unsafe_allow_html=True)
    st.caption(f"Mostrando {min(len(v), MAX_FILAS_TABLA)} de {len(v)}. Elige una orden arriba para ver su ficha.")
    st.download_button("⬇ Descargar CSV", _csv(v.drop(columns=["Responsable", "Proveedor"])), "desglose_ordenes.csv",
                       "text/csv", key="oc_d_csv")


# ------------------------------- carga y edición ------------------------------
def _ui_carga_masiva(d: dict, t: dict, usuario: str, rol: str) -> None:
    st.markdown("#### Carga masiva desde Excel o CSV")
    st.caption("Mismas columnas que las pestañas de responsable (sirve el Excel de SAP ME2N). Cada línea NUEVA se guarda "
               "en la pestaña de su RESPONSABLE (o donde ya está su orden). Nunca borra: una celda vacía no vacía la del "
               "Sheet y lo que Repuesto ya llenó no se pisa. Líneas nuevas entran como COLOCADA si no traen estatus.")
    st.download_button("⬇ Descargar plantilla", plantilla_excel(), "plantilla_ordenes_compra.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="oc_plantilla")
    archivo = st.file_uploader("Archivo", type=["xlsx", "csv"], key="oc_archivo", label_visibility="collapsed")
    if archivo is None:
        return
    try:
        leido = leer_archivo(archivo.getvalue(), archivo.name)
    except ValueError as e:
        st.error(str(e))
        return
    plan = planificar(leido["lineas"], t, usuario, rol)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Líneas nuevas", len(plan["nuevas"]))
    c2.metric("Líneas con cambios", len(plan["cambios"]))
    c3.metric("Sin cambio", plan["sin_cambio"])
    rechazos = list(leido["rechazos"]) + list(plan["sin_destino"])
    c4.metric("Rechazadas", len(rechazos))
    if plan["ord_nuevas"]:
        st.caption("Órdenes nuevas que se crean en la pestaña Órdenes: " + ", ".join(sorted(plan["ord_nuevas"])[:15]))
    destinos = {}
    for n in plan["nuevas"]:
        destinos[n["hoja"]] = destinos.get(n["hoja"], 0) + 1
    if destinos:
        st.caption("Líneas nuevas por pestaña: " + " · ".join(f"{h} {c}" for h, c in sorted(destinos.items())))
    if rechazos:
        with st.expander(f"Líneas rechazadas ({len(rechazos)}) — no se cargan", expanded=True):
            st.dataframe(pd.DataFrame(rechazos, columns=["Fila del archivo", "Documento", "Posición", "Motivo"]),
                         hide_index=True, width="stretch")
    if leido["avisos"]:
        with st.expander(f"Avisos ({len(leido['avisos'])})"):
            for a in leido["avisos"][:40]:
                st.write(a)
    if plan["cambios"]:
        with st.expander(f"Cambios en líneas existentes ({len(plan['cambios'])})"):
            filas = [{"Pestaña": c["hoja"], "Línea": f"{c['key'][0]}/{c['key'][1]}", "Campo": NOMBRE_D[k], "Antes": _texto(a), "Ahora": _texto(n),
                      "Conservado": ""} for c in plan["cambios"] for k, (a, n) in c["cambios"].items()][:300]
            st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch")
    if not plan["hay_cambios"]:
        st.info("El archivo no trae nada nuevo respecto al Sheet.")
        return
    st.caption("Al guardar, la comparación se repite con el Sheet en ese momento.")
    if st.button(f"Aplicar carga ({len(plan['nuevas'])} nuevas, {len(plan['cambios'])} actualizadas)", type="primary",
                 key="oc_aplicar"):
        ok, msg = aplicar_carga(leido["lineas"], archivo.name)
        if ok:
            avisar_tras_recarga(msg)
            st.rerun()
        else:
            st.error(msg)


def _ui_editar(d: dict, usuario: str) -> None:
    st.markdown("#### Actualizar una orden")
    o, l = d["ordenes"], d["lineas"]
    if o.empty:
        st.info("No hay órdenes abiertas para actualizar.")
        return
    docs = list(o["Doc"])
    etiqueta = {x.Doc: f"{x.Doc} · {x.Proveedor or 'sin proveedor'} · {x.Responsable}" for x in o.itertuples()}
    doc = st.selectbox("Orden", docs, format_func=lambda x: etiqueta[x], key="oc_e_doc")
    ord_ = o[o["Doc"] == doc].iloc[0]
    lin = l[l["Doc"] == doc].reset_index(drop=True)
    firma = hashlib.md5(repr((ord_.Proveedor, ord_.Responsable, ord_.Empresa, ord_.EstatusOrden, ord_.Comentario,
                              lin[["Pos", "Desp", "Estatus", "Despacho", "Transp", "Embarque", "ETA", "Puerto", "Almacen",
                                   "Coment"]].astype(str).values.tolist())).encode()).hexdigest()[:8]
    resp_actual = "" if ord_.Responsable == SIN_ASIGNAR else ord_.Responsable
    with st.form(f"oc_form_{_slug(doc)}_{firma}"):
        c1, c2, c3, c4 = st.columns(4)
        prov = c1.text_input("Proveedor", ord_.Proveedor)
        con_pestana = bool(ord_.PorPestana)
        resp = c2.text_input("Responsable", resp_actual, disabled=con_pestana,
                             help="Lo define la pestaña donde están las líneas. Para cambiarlo, mueve las filas de la "
                                  "orden a la pestaña de la otra persona en el Sheet." if con_pestana else None)
        emp = c3.selectbox("Empresa", [""] + EMPRESAS, index=([""] + EMPRESAS).index(ord_.Empresa)
                           if ord_.Empresa in EMPRESAS else 0)
        est = c4.selectbox("Estatus de la orden", ESTATUS_ORDEN, index=ESTATUS_ORDEN.index(ord_.EstatusOrden)
                           if ord_.EstatusOrden in ESTATUS_ORDEN else 0,
                           help="COMPLETA o CANCELADA: la orden y sus líneas dejan de verse en la plataforma.")
        coment = st.text_area("Comentario para Taller", ord_.Comentario, height=80)
        st.caption("Líneas (la cantidad pedida no se edita aquí). Un estatus COMPLETO o CANCELADO oculta esa línea.")
        base = pd.DataFrame({
            "Pos": lin["Pos"], "Descripción": lin["Texto"], "Pedida": lin["Pedida"], "Despachada": lin["Desp"],
            "Estatus": lin["Estatus"].replace("", None), "Despacho": lin["Despacho"].replace("", None),
            "Transportista": lin["Transp"].replace("", None),
            "Embarque": lin["Embarque"], "ETA": lin["ETA"], "Puerto": lin["Puerto"].replace("", None),
            "Almacén": lin["Almacen"].replace("", None), "Comentario": lin["Coment"].replace("", None)})
        editado = st.data_editor(
            base, hide_index=True, width="stretch", key=f"oc_ed_{_slug(doc)}_{firma}",
            disabled=["Pos", "Descripción", "Pedida"],
            column_config={
                "Despachada": st.column_config.NumberColumn(min_value=0, format="%g"),
                "Estatus": st.column_config.SelectboxColumn(options=ESTATUS_LINEA),
                "Despacho": st.column_config.SelectboxColumn(options=ESTATUS_DESPACHO,
                                                             help="2.º ESTATUS del Sheet: informativo."),
                "Embarque": st.column_config.DateColumn(format="DD/MM/YYYY"),
                "ETA": st.column_config.DateColumn(format="DD/MM/YYYY")})
        guardar = st.form_submit_button("Guardar cambios", type="primary")
    if not guardar:
        return
    nuevos_o = {"prov": prov.strip() or None, "resp": resp.strip() or None, "emp": emp or None, "est": est,
                "coment": coment.strip() or None}
    antes_o = {"prov": ord_.Proveedor or None, "resp": resp_actual or None, "emp": ord_.Empresa or None,
               "est": ord_.EstatusOrden, "coment": ord_.Comentario or None}
    cam_o = {k: (antes_o[k], nuevos_o[k]) for k in nuevos_o if _comparable(k, nuevos_o[k], True) != _comparable(k, antes_o[k], True)}
    mapa = {"Despachada": "desp", "Estatus": "estatus", "Despacho": "estatus2", "Transportista": "transp", "Embarque": "embarque", "ETA": "eta",
            "Puerto": "puerto", "Almacén": "almacen", "Comentario": "coment"}
    cam_l = {}
    for i in range(len(base)):
        pos = base.loc[i, "Pos"]
        cam = {}
        for col, k in mapa.items():
            a, n = base.loc[i, col], editado.loc[i, col]
            a, n = _comparable(k, a if not pd.isna(a) else None), _comparable(k, n if not pd.isna(n) else None)
            if k == "transp" and isinstance(n, str):
                n = n.upper()
            if a != n:
                cam[k] = (a, n)
        if cam:
            cam_l[pos] = cam
    if "emp" in cam_o and cam_o["emp"][1] is None and cam_o["emp"][0] is None:
        del cam_o["emp"]
    ok, msg = guardar_edicion(doc, cam_o, cam_l)
    if ok:
        avisar_tras_recarga(msg)
        st.rerun()
    else:
        st.error(msg)


def _vista_cargar(d: dict, t: dict, usuario: str, rol: str) -> None:
    _ui_carga_masiva(d, t, usuario, rol)
    st.divider()
    _ui_editar(d, usuario)


def _aislar(fn, *a) -> None:
    try:
        fn(*a)
    except Exception as e:  # noqa: BLE001 - un fallo de una pestaña no tumba las demás
        st.error(f"Error en esta vista de órdenes de compra: {e}")


def panel_ordenes(rol: str, token: str = "") -> None:
    """Contenido de la sección «Órdenes de compra». `token` = hora de la última lectura del Sheet principal."""
    st.markdown(_CSS, unsafe_allow_html=True)
    datos = cargar(token)
    _encabezado(datos["hora"], datos.get("libro", ""))
    if datos["error"]:
        st.error(datos["error"])
        return
    if datos["faltan"]:
        st.warning("Al Sheet de órdenes le falta: " + ", ".join(datos["faltan"])
                   + f". La pestaña de órdenes debe llamarse «{PESTANA_ORDENES}»; cada responsable de la columna "
                     f"RESPONSABLE de «{PESTANA_LISTAS}» debe tener una pestaña con su nombre.")
        return
    t = tablas(datos["o"], datos["d"], datos["resp"])
    if t["faltan_duro"]:
        st.error("Faltan columnas en el Sheet de órdenes: " + ", ".join(t["faltan_duro"]))
        return
    usuario = sio.usuario_actual()
    puede = rol in ROLES_ESCRITURA
    d = construir(t, hoy_rd())
    nombres = ["Vista general", "Por responsable", "Desglose"] + (["Cargar y actualizar"] if puede else [])
    tabs = st.tabs(nombres)
    with tabs[0]:
        _aislar(_vista_general, d, usuario, rol)
    with tabs[1]:
        _aislar(_vista_responsable, d, usuario, rol)
    with tabs[2]:
        _aislar(_vista_desglose, d, usuario, rol)
    if puede:
        with tabs[3]:
            _aislar(_vista_cargar, d, t, usuario, rol)
    if rol == "admin":
        avisos = list(d["avisos"])
        if datos.get("sin_hoja"):
            avisos.append("Responsables de Listas que no tienen pestaña (créala con su nombre): "
                          + ", ".join(datos["sin_hoja"]))
        if d["completas_abiertas"]:
            avisos.append("Órdenes con TODAS sus líneas completas que siguen ABIERTA (ciérralas con COMPLETA): "
                          + ", ".join(d["completas_abiertas"][:15]))
        if avisos:
            with st.expander(f"Datos por revisar en el Sheet ({len(avisos)})"):
                for a in avisos[:60]:
                    st.write(a)
                if len(avisos) > 60:
                    st.write(f"… y {len(avisos) - 60} más.")
