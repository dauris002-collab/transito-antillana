"""
sheets_io.py — Capa de acceso a Google Sheets para Antillana Comercial.

Conexión, lectura, escritura, caché de datos, reintentos ante errores de la
API, bitácora y accesores de configuración (Secrets). Sin lógica de negocio:
solo constantes de columnas/pestañas y las funciones que hablan directamente
con el Google Sheet. No debe ser importado por logica.py, ui_componentes.py,
vistas_admin.py ni causar dependencias circulares.
"""

from __future__ import annotations

import re
import time
import unicodedata
from functools import lru_cache
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import gspread.exceptions
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1


ZONA_RD = ZoneInfo("America/Santo_Domingo")


COL_BL = "BL"


COL_DESC = "Descripcion"


COL_CANT = "Cantidad"


COL_PAIS = "Pais_Origen"


# El ETA es la fecha que el equipo mantiene actualizada y la que todos conocen.
# Es ESTIMADA mientras nadie confirme la llegada; una vez confirmada (ver
# COL_LLEGO) pasa a valerse como la fecha real de llegada a puerto.
COL_ETA = "Llegada a Puerto (ETA)"


# Confirmación de llegada: "SI" (llegó), "NO" (se verificó que NO llegó) o vacío
# (nadie ha revisado). Sustituye a la vieja columna Estatus_Llegada.
#
# La fecha de llegada NO se teclea aparte: cuando esto dice SI, la fecha de
# llegada es el ETA. Así nadie escribe la misma fecha dos veces. El precio es
# que si el ETA se mueve DESPUÉS de confirmar, la fecha de llegada se mueve con
# él; por eso al archivar se congela en Fecha_Llegada_Puerto (ver
# marcar_como_recibido), que es lo que después se lee como histórico.
COL_LLEGO = "¿Llegó? SI/NO"


LLEGO_SI = "SI"


LLEGO_NO = "NO"


COL_DIAS_PUERTO = "Dias en puerto"     # existe en el Sheet; la app lo calcula en vivo y no lo escribe


COL_ACTUALIZACION = "Fecha_Actualizacion"  # el Sheet lo tiene con tilde; _norm lo resuelve


COL_ACTUALIZADO_POR = "Actualizado_Por"    # la app la crea sola la primera vez que escribe


# --- Columnas opcionales por categoría --------------------------------------
# Ninguna de estas aplica a las 5 pestañas, y por eso NO están en
# REQUIRED_COLUMNS: si la pestaña no la trae, la app ni la pide ni la crea.
COL_MODELO = "Modelo_Serie"          # Equipos, Generadores, Consolidados


COL_FECHA_SALIDA = "Fecha_Salida"    # la trae quien la conoce (booking del forwarder/naviera)


COL_OC = "OC"                        # Aéreos y Carga Suelta


COL_EE = "EE"                        # Aéreos y Carga Suelta


COL_CLIENTE_STOCK = "CLIENTE / STOCK"   # Equipos, Generadores, Aéreos


OPCIONALES_CATEGORIA = [COL_MODELO, COL_FECHA_SALIDA, COL_OC, COL_EE, COL_CLIENTE_STOCK]


# Congelado de la llegada. Esta columna NO vive en las pestañas de categoría
# (ahí la llegada es COL_LLEGO + el ETA): solo se escribe al archivar, para que
# el histórico conserve la fecha que valía en ese momento aunque el ETA se
# mueva después.
COL_FECHA_LLEGADA_PUERTO = "Fecha_Llegada_Puerto"


COL_FECHA_DECLARACION = "Fecha_Declaracion"          # recepción y declaración ante Aduanas


# Entrada a almacén. Tampoco es una etapa del tablero activo: es el sello del
# momento de archivar, y por eso vive únicamente en "Recibido (Mes)".
COL_FECHA_ALMACEN = "Fecha_Almacen"


# Columnas opcionales libres: si existen en el Sheet, la app las usa; si no, ni
# se mencionan. Así se puede agregar Puerto_Destino o Valor_USD desde Google
# Sheets sin tocar una línea de código.
NOMBRES_VALOR = {"valor_usd", "valor", "monto", "valor_cif", "monto_usd", "valor us$", "valor us"}


MAX_FILAS_LECTURA = 20000


# Lo único que TODA pestaña de categoría debe traer.
REQUIRED_COLUMNS = [COL_BL, COL_DESC, COL_CANT, COL_PAIS, COL_ETA]


# Fechas del flujo que sí se escriben en la pestaña de categoría. La llegada no
# está aquí porque no tiene columna de fecha propia: es COL_LLEGO + el ETA.
COLUMNAS_FLUJO = [COL_FECHA_DECLARACION]


ALL_COLUMNS = REQUIRED_COLUMNS + [COL_DIAS_PUERTO, COL_ACTUALIZACION, COL_ACTUALIZADO_POR,
                                  COL_LLEGO, *COLUMNAS_FLUJO, *OPCIONALES_CATEGORIA]


# Columnas que la app calcula o gestiona internamente y que no se muestran como
# "campos extra" del embarque. Si se agrega un cálculo nuevo en enriquecer(),
# su nombre TIENE que entrar aquí o aparecerá como columna suelta en la ficha.
COLUMNAS_INTERNAS = {
    "Categoria", "FilaSheet", "EstadoTexto", "DiasRel", "ETAFecha", "Prioridad", "OrdenSec",
    "ValorNum", "Buscar", "EtapaActual", "EtapaIdx", "Alerta", "AlertaDias",
    "DiasTransito", "DiasEnPuerto", "DiasEnEtapa",
    "F_Salida", "F_Puerto", "F_Declaracion",
    "BLRepetido", "FlujoRaro",
    COL_DIAS_PUERTO,
}


CATEGORIAS = ["Equipos", "Generadores", "Aéreos", "Carga Suelta", "Consolidados"]


# 2 contadores operativos:
#   1) Salida -> Llegada confirmada (tránsito; se congela al confirmar)
#   2) Llegada confirmada -> Declaración (y de ahí a hoy, hasta archivar)
# El reloj del dinero (días en puerto) arranca en la LLEGADA CONFIRMADA, nunca
# en el ETA sin confirmar: mientras nadie diga SI, la carga puede seguir en agua.
ETAPAS_PUERTO = [
    "Llegada a puerto",
    "Recepción y declaración",
]


INDICE_ETAPA = {e: i for i, e in enumerate(ETAPAS_PUERTO)}


# Solo las etapas que tienen columna de fecha propia. "Llegada a puerto" no está
# aquí a propósito: se resuelve con COL_LLEGO + el ETA (ver fecha_llegada_fila).
COLUMNA_FECHA_ETAPA = {
    "Recepción y declaración": COL_FECHA_DECLARACION,
}


ETAPA_LLEGADA = ETAPAS_PUERTO[0]


# Etiqueta de la acción de archivar. No está en ETAPAS_PUERTO a propósito: no es
# una etapa del tablero activo, es la salida del tablero.
ETAPA_ALMACEN = "Recibido en almacén"


# Días tolerados en cada etapa antes de considerar que hay un cuello de botella.
# Son estimaciones de arranque, NO un estándar medido: ajústalos con los datos
# reales una vez que el histórico tenga unos meses (Herramientas muestra las
# medianas). Se pueden sobrescribir desde Secrets sin tocar código.
SLA_ETAPA_DEFECTO = {
    "Llegada a puerto": 3,
    "Recepción y declaración": 2,
}


SLA_RETRASO_DEFECTO = 7   # días de retraso sin actualizar el ETA antes de avisar


# Atraso en puerto. Son DOS umbrales distintos y confundirlos falsea el número:
# UMBRAL_ATRASO_PUERTO es a partir de cuándo TÚ consideras que un embarque está
# atrasado (criterio interno), y DIAS_LIBRES es a partir de cuándo la naviera o
# la terminal EMPIEZAN A COBRAR (criterio del proveedor, viene en el contrato).
# El conteo de atrasados usa el primero; el módulo de pagos usará el segundo.
UMBRAL_ATRASO_PUERTO_DEFECTO = 5   # alerta a partir de 5 días en puerto/aeropuerto


DIAS_LIBRES_DEFECTO = 0   # sin días libres declarados, el reloj corre desde la llegada


MONEDA_DEFECTO = "RD$"


# Qué fecha manda para decidir a qué mes pertenece un embarque recibido:
#   "llegada" -> fecha de llegada confirmada (respaldo: ETA)
#   "almacen" -> fecha de entrada a almacén
# Cambia esta sola línea si el criterio del negocio es el otro.
BASE_FECHA_RECIBIDO = "almacen"


RECIBIDO_SHEET = "Recibido (Mes)"


LOG_SHEET = "Log"


COLUMNAS_RECIBIDO = [
    COL_BL, COL_DESC, COL_CANT, COL_PAIS, COL_ETA,
    "Fecha_Recibido", "Categoria_Origen", "Registrado_Por", COL_ACTUALIZACION,
    COL_ACTUALIZADO_POR, COL_FECHA_LLEGADA_PUERTO, *COLUMNAS_FLUJO, COL_FECHA_ALMACEN,
    *OPCIONALES_CATEGORIA,
]


COLUMNAS_LOG = ["Fecha_Hora", "Usuario", "Accion", "BL", "Categoria", "Detalle"]


# --- Estatus de Pago ---------------------------------------------------------
# Pestaña aparte, una fila por BL/expediente (no por concepto): un expediente
# no siempre trae los mismos cargos, así que cada fila solo llena las columnas
# de concepto que de verdad aplican; el resto queda vacío, nunca en cero.
PAGOS_SHEET = "Pagos"


# Lista cerrada de conceptos. Moneda FIJA por columna: así el monto se guarda
# como número puro (sin "dolares"/"pesos" pegado al texto) y no hay que parsear
# texto libre para saber en qué moneda está cada celda.
CONCEPTOS_PAGO = ["ADUANAS", "DPH", "DPW", "TRANSPORTE", "HIT", "FDA", "VECONINTER", "ALMACENAJE"]


MONEDA_CONCEPTO = {
    "ADUANAS": "DOP", "DPH": "USD", "DPW": "DOP", "TRANSPORTE": "DOP",
    "HIT": "DOP", "FDA": "DOP", "VECONINTER": "USD", "ALMACENAJE": "DOP",
}


# Pendiente/Pagado: campo MANUAL que marca Logística a criterio propio. No se
# deriva de si hay fecha de Pago Realizado, porque son preguntas distintas
# ("¿ya se pagó?" no siempre coincide con "¿ya quedó todo conciliado?").
COL_ESTADO_PAGO = "Estado_Pago"


ESTADO_PAGO_PENDIENTE = "Pendiente"


ESTADO_PAGO_PAGADO = "Pagado"


# Fecha límite saludable que fija Logística a criterio propio. Ya NO congela
# montos: el total se calcula en vivo desde los conceptos (ver TotalActual en
# logica.py), así que no hace falta guardar una foto de esos números aquí —
# solo la fecha, para poder medir después si el pago llegó a tiempo.
COL_FECHA_SIN_MORA = "Fecha_SinMora"


# "Pago Realizado": la fecha real en que se pagó, y el EXTRA que se pagó de
# más sobre los conceptos originales (por mora, ajustes, etc.) — lo escribe
# Logística a mano, en pesos y/o dólares; 0 si no hubo diferencia. No es un
# total congelado: es la diferencia, y la plataforma se la suma al total de
# los conceptos para mostrar el costo final.
COL_FECHA_PAGO_REAL = "Fecha_PagoRealizado"


COL_PAGOREAL_USD = "PagoRealizado_USD"


COL_PAGOREAL_DOP = "PagoRealizado_DOP"


# Descripción y Cantidad reusan las mismas columnas canónicas de tránsito
# (mismo significado, mismo nombre). Llegada es propia de Pagos: guarda la
# fecha ya resuelta (confirmada si existe, ETA si no) para no repetir la
# lógica de COL_LLEGO + ETA en una hoja que no la necesita.
COL_PAGO_LLEGADA = "Llegada"


# Razón social del expediente. Tránsito solo trackea embarques de Antillana
# Comercial, así que todo lo que llega por sincronizar_pagos_con_transito()
# se marca así de una vez. Tecnicaribe y Motor Ibérico no tienen tránsito
# propio en esta app: sus expedientes se agregan directo en el Sheet.
COL_EMPRESA = "Empresa"


EMPRESA_ANTILLANA = "Antillana Comercial"


EMPRESAS_PAGO = [EMPRESA_ANTILLANA, "Tecnicaribe", "Motor Ibérico"]


COLUMNAS_PAGOS = [
    COL_EMPRESA, COL_BL, COL_DESC, COL_CANT, COL_PAGO_LLEGADA, *CONCEPTOS_PAGO, COL_ESTADO_PAGO,
    COL_FECHA_SIN_MORA, COL_FECHA_PAGO_REAL, COL_PAGOREAL_USD, COL_PAGOREAL_DOP,
    COL_ACTUALIZACION, COL_ACTUALIZADO_POR,
]


CACHE_TTL = 45              # segundos de caché de lectura


REINTENTOS_API = 3


MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}


MESES_ES_CORTO = {
    1: "ene", 2: "feb", 3: "mar", 4: "abr", 5: "may", 6: "jun",
    7: "jul", 8: "ago", 9: "sep", 10: "oct", 11: "nov", 12: "dic",
}


# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------
def hoy_rd() -> date:
    """Fecha de HOY en hora de Santo Domingo (UTC-4). Streamlit Cloud corre en UTC:
    con date.today() directo, en las últimas horas del día (y sobre todo el último
    día del mes) el servidor ya 'cree' que es el día siguiente aunque en RD no lo sea."""
    return datetime.now(ZONA_RD).date()


def ahora_rd() -> datetime:
    return datetime.now(ZONA_RD)


@lru_cache(maxsize=4096)
def _norm_cache(texto: str) -> str:
    s = " ".join(texto.split()).strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.casefold()


def _norm(texto) -> str:
    """Normaliza un nombre de columna/pestaña: sin acentos, sin dobles espacios,
    sin distinguir mayúsculas. Es lo que hace que '¿Llegó? SI/NO' y
    '¿Llego? Si/No' se traten como la misma columna. Con caché porque se llama
    miles de veces por refresco (búsqueda, mapeo de columnas, etapas)."""
    return _norm_cache(str(texto))


def _slug_css(texto) -> str:
    """Convierte un nombre visible ('Aéreos', 'Carga Suelta') en un identificador
    ASCII apto para usarse como clave de widget o clase CSS: 'aereos', 'carga_suelta'."""
    base = _norm(texto)
    limpio = "".join(c if c.isalnum() else "_" for c in base)
    while "__" in limpio:
        limpio = limpio.replace("__", "_")
    return limpio.strip("_") or "x"


def es_llego_si(valor) -> bool:
    """True solo si la casilla dice SI. Tolera 'si', 'Sí', 'SÍ', espacios."""
    return _norm(valor) in {"si", "s", "yes", "y"}


def es_llego_no(valor) -> bool:
    """True solo si se verificó explícitamente que NO llegó. Vacío no es NO:
    vacío significa que nadie ha revisado, y son cosas distintas."""
    return _norm(valor) in {"no", "n"}


@st.cache_resource
def costos_puerto() -> dict:
    """Parámetros del atraso en puerto. Se ajustan desde Secrets:

        [costo_puerto]
        umbral = 5
        moneda = "RD$"
        dias_libres = 5

        [costo_puerto.dias_libres_por_categoria]
        Aéreos = 2

    Aquí NO hay tarifa diaria. El costo de la demora no se estima con una tarifa
    por día — que en la práctica varía por naviera, terminal, volumen y espacio —
    sino que se observa: se registra el monto estimado con su fecha de pago
    saludable y luego el monto realmente pagado, y el sobrecosto es la
    diferencia. Eso vive en el módulo de Estatus de Pago, no en tránsito."""
    cfg = {}
    try:
        cfg = st.secrets.get("costo_puerto", None) or {}
    except Exception:
        cfg = {}

    def _num(valor, defecto):
        try:
            return float(valor)
        except (TypeError, ValueError):
            return defecto

    return {
        "umbral": int(_num(cfg.get("umbral"), UMBRAL_ATRASO_PUERTO_DEFECTO)),
        "moneda": str(cfg.get("moneda") or MONEDA_DEFECTO),
        "dias_libres": _num(cfg.get("dias_libres"), DIAS_LIBRES_DEFECTO),
        "libres_por_cat": dict(cfg.get("dias_libres_por_categoria", {}) or {}),
    }


@st.cache_resource
def sla_etapas() -> dict:
    """Umbrales de días por etapa. Se pueden ajustar desde Streamlit Secrets sin
    tocar código:  [sla]  llegada_a_puerto = 4  /  recepcion_y_declaracion = 3 ..."""
    valores = dict(SLA_ETAPA_DEFECTO)
    valores["__retraso__"] = SLA_RETRASO_DEFECTO
    try:
        cfg = st.secrets.get("sla", None) or {}
        for etapa in SLA_ETAPA_DEFECTO:
            clave = _slug_css(etapa)
            if clave in cfg:
                valores[etapa] = int(cfg[clave])
        if "retraso" in cfg:
            valores["__retraso__"] = int(cfg["retraso"])
    except Exception:
        pass
    return valores


# --- Fechas -----------------------------------------------------------------
EPOCH_SHEETS = date(1899, 12, 30)


_MESES_EN = ["january", "february", "march", "april", "may", "june",
             "july", "august", "september", "october", "november", "december"]


_MESES_TEXTO = {}
for _n, _nombre_es in MESES_ES.items():
    _MESES_TEXTO[_norm(_nombre_es)] = _n
    _MESES_TEXTO[MESES_ES_CORTO[_n]] = _n
for _i, _nombre_en in enumerate(_MESES_EN, start=1):
    _MESES_TEXTO[_nombre_en] = _i
    _MESES_TEXTO[_nombre_en[:3]] = _i
_MESES_TEXTO.update({"setiembre": 9, "set": 9, "sept": 9, "sep": 9, "ene": 1, "abr": 4, "dic": 12})


_SEPARADORES = re.compile(r"[\s/\\\-\.,;_|]+")


_ORDINALES = re.compile(r"^(\d+)(ro|er|ero|do|to|mo|vo|no|st|nd|rd|th)$")


_RELLENO = {"de", "del", "dia", "el", "la", "los", "las", "ano", "a", "al", "of", "the"}


def _tokenizar_fecha(texto: str) -> list:
    """Parte cualquier forma de escribir una fecha en tres piezas. Tolera
    separadores mezclados, palabras de relleno y ordinales:
    '02-05-2026', '6 de julio 2026', 'julio 28 del 2026', '1ro de mayo/2026'."""
    base = _norm(texto).split(" 00:00:00")[0]
    tokens = []
    for pieza in _SEPARADORES.split(base):
        pieza = pieza.strip("º°ª'\"()[]")
        pieza = _ORDINALES.sub(r"\1", pieza)
        if pieza and pieza not in _RELLENO:
            tokens.append(pieza)
    return tokens


def _normalizar_anio(n: int) -> int:
    """Año de dos dígitos -> siglo razonable. '26' es 2026, no 26 d.C."""
    if n >= 100:
        return n
    return 2000 + n if n < 80 else 1900 + n


def _interpretar_tokens(tokens: list, dia_primero: bool = True):
    """Devuelve (dia, mes, anio, ambigua) o None. 'ambigua' es True solo cuando
    día y mes son ambos <= 12 y están escritos en número, que es el único caso
    donde el orden realmente no se puede deducir."""
    if len(tokens) != 3:
        return None

    for i, t in enumerate(tokens):
        mes = _MESES_TEXTO.get(t) or _MESES_TEXTO.get(t[:3]) if not t.isdigit() else None
        if mes:
            resto = [tokens[j] for j in range(3) if j != i]
            if not all(r.isdigit() for r in resto):
                return None
            a, b = int(resto[0]), int(resto[1])
            if len(resto[0]) == 4 or a > 31:
                anio, dia = a, b
            else:
                dia, anio = a, b
            return dia, mes, _normalizar_anio(anio), False

    if not all(t.isdigit() for t in tokens):
        return None
    a, b, c = (int(t) for t in tokens)

    if len(tokens[0]) == 4:                      # 2026-08-25
        return c, b, a, False

    anio = _normalizar_anio(c)                   # año al final
    if a > 12 and b <= 12:
        return a, b, anio, False
    if b > 12 and a <= 12:
        return b, a, anio, False
    if a <= 12 and b <= 12:
        dia, mes = (a, b) if dia_primero else (b, a)
        return dia, mes, anio, True
    return None


def _fecha_de_tokens(tokens: list, dia_primero: bool = True):
    resultado = _interpretar_tokens(tokens, dia_primero)
    if resultado is None:
        return None
    dia, mes, anio, _ = resultado
    try:
        return date(anio, mes, dia)
    except ValueError:
        return None


@lru_cache(maxsize=8192)
def _parsear_texto(texto: str):
    """Parte de texto del parser, cacheada: enriquecer() y el render recorren
    las mismas cadenas decenas de veces por refresco."""
    try:
        return datetime.strptime(texto[:10], "%Y-%m-%d").date()
    except ValueError:
        pass

    solo_digitos = texto.replace(",", "").replace(" ", "")
    if solo_digitos.replace(".", "", 1).isdigit():
        entero = solo_digitos.split(".")[0]
        if len(entero) == 8:  # 20260825
            try:
                return datetime.strptime(entero, "%Y%m%d").date()
            except ValueError:
                pass
        try:
            numero = int(float(solo_digitos))
        except (ValueError, OverflowError):
            return None
        # Rango de seriales plausibles de Sheets/Excel (aprox. 1954 a 2119).
        if 20000 <= numero <= 80000:
            return EPOCH_SHEETS + timedelta(days=numero)
        return None

    return _fecha_de_tokens(_tokenizar_fecha(texto), dia_primero=True)


def parsear_fecha(valor):
    """Convierte a date lo que sea que venga del Sheet, del Excel o tecleado a mano:
    date/datetime, ISO (2026-08-25), compacto (20260825), dd/mm/aaaa, dd-mm-aa,
    aaaa/mm/dd, mes en texto en español o inglés ('6 de julio 2026', '28-Jul-26')
    y seriales numéricos de Sheets/Excel (46181). Ante un número puro ambiguo
    (02-05-2026) asume día/mes, la convención local."""
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        try:
            return EPOCH_SHEETS + timedelta(days=int(valor))
        except (ValueError, OverflowError):
            return None
    texto = str(valor).strip()
    if not texto:
        return None
    return _parsear_texto(texto)


def formato_eta(valor) -> str:
    """Cómo se muestra una fecha en pantalla: '25 ago 2026'. Si no se pudo
    parsear, devuelve el texto crudo para que se vea que ese dato está sucio."""
    f = parsear_fecha(valor)
    if f is None:
        crudo = str(valor).strip()
        return crudo if crudo else "—"
    return f"{f.day:02d} {MESES_ES_CORTO[f.month]} {f.year}"


def fecha_llegada_fila(fila) -> date | None:
    """Fecha de llegada a puerto de una fila (dict o Series de pandas).

    Es el ETA, pero SOLO si alguien confirmó la llegada con SI. Sin confirmar
    devuelve None, y eso es deliberado: es lo que impide que un embarque que
    todavía está navegando empiece a acumular días en puerto —y, más adelante,
    sobrecosto por demora— solo porque su ETA ya venció."""
    try:
        obtener = fila.get
    except AttributeError:
        return None
    llego = obtener(COL_LLEGO, "")
    if not es_llego_si(llego):
        return None
    return parsear_fecha(obtener(COL_ETA, ""))


# ---------------------------------------------------------------------------
# CAPA GOOGLE SHEETS
# ---------------------------------------------------------------------------
@st.cache_resource
def get_spreadsheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=scopes
    )
    client = gspread.authorize(creds)
    return client.open_by_key(st.secrets["SHEET_ID"])


def _codigo_api(e) -> int:
    """Código HTTP de un APIError de gspread, sin asumir la forma del objeto."""
    try:
        return int(e.response.status_code)
    except Exception:
        pass
    try:
        return int(e.args[0].get("code", 0))
    except Exception:
        return 0


def _con_reintento(fn, intentos: int = REINTENTOS_API):
    """Reintenta ante 429 (cuota) y 5xx (fallo temporal de Google) con espera
    creciente. Con cuatro personas trabajando a la vez la cuota se toca, y antes
    eso era un error rojo en pantalla en medio de una carga."""
    ultimo = None
    for intento in range(intentos):
        try:
            return fn()
        except gspread.exceptions.APIError as e:
            codigo = _codigo_api(e)
            if codigo in (429, 500, 502, 503) and intento < intentos - 1:
                time.sleep(1.2 * (intento + 1))
                ultimo = e
                continue
            raise
    if ultimo:
        raise ultimo


@st.cache_resource
def _indice_hojas() -> dict:
    """Mapa nombre-normalizado -> Worksheet, con UNA sola llamada de metadata.
    Antes, cada get_worksheet() disparaba una llamada a la API."""
    return {_norm(h.title): h for h in _con_reintento(lambda: get_spreadsheet().worksheets())}


def get_worksheet(nombre: str):
    return _indice_hojas().get(_norm(nombre))


def _refrescar_estructura():
    _indice_hojas.clear()
    _headers.clear()


@st.cache_data(ttl=120, show_spinner=False)
def _headers(titulo_hoja: str) -> list:
    ws = get_worksheet(titulo_hoja)
    if ws is None:
        return []
    return _con_reintento(lambda: ws.row_values(1))


def _columna_indice(headers: list, nombre: str):
    """Posición 1-indexada de una columna, tolerando acentos y mayúsculas."""
    return next((i + 1 for i, h in enumerate(headers) if _norm(h) == _norm(nombre)), None)


def marca_ahora() -> str:
    """Sello que se estampa en Fecha_Actualizacion cada vez que alguien carga o
    modifica información. Lleva hora, no solo fecha, porque es lo que el tablero
    muestra como 'información actualizada'."""
    return ahora_rd().strftime("%Y-%m-%d %H:%M")


def parsear_marca(valor):
    """Lee un sello de Fecha_Actualizacion como datetime. Acepta los registros
    viejos que solo tienen fecha (se asumen a medianoche)."""
    texto = "" if valor is None else str(valor).strip()
    if not texto:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(texto, fmt)
        except ValueError:
            continue
    f = parsear_fecha(texto)
    return datetime(f.year, f.month, f.day) if f else None


def _fila_desde_dict(headers: list, datos: dict) -> list:
    """Arma la fila respetando el orden REAL de columnas de la pestaña y
    tolerando diferencias de acento/mayúsculas en los encabezados."""
    normalizado = {_norm(k): v for k, v in datos.items()}
    return [normalizado.get(_norm(h), "") for h in headers]


def _asegurar_columnas(ws, nombres: list) -> list:
    """Agrega de una sola vez las columnas que falten (una llamada, no una por
    columna). Devuelve la lista final de encabezados.

    OJO al mantener el Sheet a mano: si borras una columna que la app escribe,
    esta función la vuelve a crear (vacía, al final) la próxima vez que alguien
    guarde. Para eliminar una columna de verdad hay que quitarla también de las
    constantes de arriba y desplegar, y solo después borrarla del Sheet.

    Por eso las columnas opcionales por categoría (Modelo_Serie, OC, EE,
    CLIENTE / STOCK, Fecha_Salida) nunca se pasan a esta función 'por si
    acaso': solo cuando traen un valor real. Así Carga Suelta no termina con una
    columna Modelo_Serie vacía que nadie pidió."""
    headers = _headers(ws.title)
    existentes = {_norm(h) for h in headers}
    faltan = []
    for n in nombres:
        if _norm(n) not in existentes and _norm(n) not in {_norm(f) for f in faltan}:
            faltan.append(n)
    if not faltan:
        return headers
    inicio = len(headers) + 1
    rango = f"{rowcol_to_a1(1, inicio)}:{rowcol_to_a1(1, inicio + len(faltan) - 1)}"
    _con_reintento(lambda: ws.update(range_name=rango, values=[faltan], value_input_option="RAW"))
    # Solo esta pestaña cambió: limpiar el caché completo de _headers (como se
    # hacía antes) obligaba a releer por API los encabezados de TODAS las
    # demás pestañas la próxima vez que alguien las pidiera dentro del TTL,
    # aunque no se hubieran tocado.
    _headers.clear(ws.title)
    return headers + faltan


def _localizar_fila(ws, bl: str, fila_sugerida=None):
    """Ubica la fila de un BL dentro de una pestaña. Devuelve (fila, error).

    Antes se usaba ws.find(BL), que devuelve la PRIMERA coincidencia. Con dos
    embarques parciales del mismo BL, la app editaba, archivaba o borraba la
    fila equivocada en silencio. Ahora la pantalla manda el número de fila que
    está mostrando y aquí se verifica contra el Sheet: si esa fila sigue
    teniendo ese BL, se usa; si el Sheet cambió y solo hay una coincidencia, se
    usa esa; si hay varias y ninguna es la sugerida, NO se escribe nada."""
    if ws is None:
        return None, "No se encontró la pestaña en el Google Sheet."
    headers = _headers(ws.title)
    columna = _columna_indice(headers, COL_BL)
    if columna is None:
        return None, f"La pestaña '{ws.title}' no tiene columna {COL_BL}."
    try:
        valores = _con_reintento(lambda: ws.col_values(columna))
    except gspread.exceptions.APIError as e:
        return None, f"Google Sheets no respondió al buscar el BL ({e})."

    objetivo = _norm(bl)
    coincidencias = [i + 1 for i, v in enumerate(valores) if i >= 1 and _norm(v) == objetivo]
    if not coincidencias:
        return None, (f"El BL '{bl}' ya no está en '{ws.title}'. Puede que otra persona lo haya "
                      "archivado, movido o editado. Actualiza los datos y vuelve a intentarlo.")
    try:
        sugerida = int(fila_sugerida) if fila_sugerida else 0
    except (TypeError, ValueError):
        sugerida = 0
    if sugerida in coincidencias:
        return sugerida, ""
    if len(coincidencias) == 1:
        return coincidencias[0], ""
    return None, (f"Hay {len(coincidencias)} filas con el BL '{bl}' en '{ws.title}' (filas "
                  f"{', '.join(str(c) for c in coincidencias)}) y la pantalla está desactualizada, "
                  "así que la app no puede saber cuál tocar. Pulsa 'Actualizar datos' y repite la acción.")


def _leer_fila(ws, fila: int, headers: list) -> dict:
    """Contenido completo de una fila como {encabezado: valor}."""
    valores = _con_reintento(lambda: ws.row_values(fila)) or []
    valores += [""] * (len(headers) - len(valores))
    return {h: valores[i] for i, h in enumerate(headers)}


def _df_desde_valores(valores: list, columnas_canonicas: list) -> pd.DataFrame:
    """Convierte la matriz cruda de una pestaña en DataFrame. Las columnas
    conocidas se renombran al nombre canónico (resolviendo acentos y mayúsculas);
    las demás se conservan tal cual, para que agregar una columna nueva en Google
    Sheets baste para que la app la reconozca sin cambiar código."""
    if not valores or not valores[0]:
        return pd.DataFrame(columns=columnas_canonicas)
    headers_reales = [str(h) for h in valores[0]]
    ancho = len(headers_reales)
    filas = [(list(f) + [""] * ancho)[:ancho] for f in valores[1:]]

    vistos, limpios = {}, []
    for i, h in enumerate(headers_reales):
        nombre = h.strip() or f"Columna {i + 1}"
        if _norm(nombre) in vistos:
            vistos[_norm(nombre)] += 1
            nombre = f"{nombre} ({vistos[_norm(nombre)]})"
        else:
            vistos[_norm(nombre)] = 1
        limpios.append(nombre)

    df = pd.DataFrame(filas, columns=limpios)
    mapa = {}
    for canon in columnas_canonicas:
        for real in df.columns:
            if _norm(real) == _norm(canon) and real not in mapa:
                mapa[real] = canon
                break
    df = df.rename(columns=mapa)
    df = df.loc[:, ~df.columns.duplicated()]
    for c in columnas_canonicas:
        if c not in df.columns:
            df[c] = ""
    return df


NO_ESPECIFICADO = "Sin especificar"


_ALIAS_PAISES_CRUDO = {
    "Estados Unidos": ["usa", "us", "u.s.a", "u.s.a.", "u.s.", "eeuu", "ee.uu", "ee.uu.", "eu",
                       "united states", "united states of america", "estados unidos de america",
                       "usa.", "america"],
    "China": ["china", "prc", "p.r. china", "republica popular china", "cn", "china."],
    "Corea del Sur": ["corea", "korea", "south korea", "corea del sur", "republica de corea", "kr"],
    "India": ["india", "in"],
    "Japón": ["japon", "japan", "jp"],
    "Alemania": ["alemania", "germany", "de"],
    "Brasil": ["brasil", "brazil", "br"],
    "España": ["espana", "spain", "es"],
    "Italia": ["italia", "italy", "it"],
    "Turquía": ["turquia", "turkey", "turkiye", "tr"],
    "México": ["mexico", "mx"],
    "Colombia": ["colombia", "co"],
    "Países Bajos": ["paises bajos", "holanda", "netherlands", "holland", "nl"],
    "Reino Unido": ["reino unido", "united kingdom", "uk", "inglaterra", "england", "gb"],
    "Canadá": ["canada", "ca"],
    "Taiwán": ["taiwan", "taiwan roc", "tw"],
    "Vietnam": ["vietnam", "viet nam", "vn"],
    "Tailandia": ["tailandia", "thailand", "th"],
    "Panamá": ["panama", "pa"],
    "Francia": ["francia", "france", "fr"],
}


ALIAS_PAISES = {}


for _canon, _formas in _ALIAS_PAISES_CRUDO.items():
    ALIAS_PAISES[_norm(_canon)] = _canon
    for _f in _formas:
        ALIAS_PAISES[_norm(_f)] = _canon


_VACIOS_PAIS = {"", "n/a", "na", "n.a.", "-", "--", "s/d", "nd", "no aplica", "pendiente", "?"}


_RX_NO_PAIS = re.compile(r"^[\d\s/:.\-]+$")


def _no_parece_pais(v) -> bool:
    """Filas con las columnas corridas meten fechas y números en País_Origen.
    Sin esto, "2026-08-04 19:58:00" se dibuja como si fuera un país y estira el
    eje del gráfico hasta deformarlo en pantallas de celular."""
    t = str(v or "").strip()
    return bool(t) and bool(_RX_NO_PAIS.match(t))


def unificar_paises(serie: pd.Series) -> pd.Series:
    """'China', 'CHINA' y 'china ' son el mismo país y no deben salir como tres
    barras distintas en el gráfico ni como tres opciones del filtro. Para lo que
    no está en la tabla de equivalencias se usa la grafía más usada del propio
    Sheet, sin inventar equivalencias de negocio."""
    valores = [str(v or "").strip() for v in serie]
    grupos = {}
    for v in valores:
        clave = _norm(v)
        if clave in _VACIOS_PAIS or clave in ALIAS_PAISES:
            continue
        grupos.setdefault(clave, {})
        grupos[clave][v] = grupos[clave].get(v, 0) + 1
    canonico = {c: max(op.items(), key=lambda kv: (kv[1], -len(kv[0])))[0] for c, op in grupos.items()}

    def resolver(v):
        clave = _norm(v)
        if clave in _VACIOS_PAIS or _no_parece_pais(v):
            return NO_ESPECIFICADO
        return ALIAS_PAISES.get(clave) or canonico.get(clave, NO_ESPECIFICADO)

    return pd.Series([resolver(v) for v in valores], index=serie.index)


def columnas_extra(df: pd.DataFrame) -> list:
    """Columnas que el usuario agregó en el Sheet y que la app no gestiona."""
    conocidas = set(ALL_COLUMNS) | COLUMNAS_INTERNAS | {"Fecha_Recibido", "Categoria_Origen",
                                                        "Registrado_Por", "FechaParsed", "Anio", "Mes",
                                                        COL_FECHA_ALMACEN, COL_FECHA_LLEGADA_PUERTO}
    return [c for c in df.columns if c not in conocidas]


def columna_de_valor(df: pd.DataFrame):
    """Detecta si el Sheet trae una columna de valor monetario, sin obligar a que
    se llame de una forma concreta."""
    for c in df.columns:
        if _norm(c) in NOMBRES_VALOR:
            return c
    return None


def es_numero(v) -> bool:
    """NaN es truthy en Python, así que 'if valor' NO sirve para descartar celdas
    vacías de una columna numérica de pandas."""
    return v is not None and pd.notna(v)


def a_numero(valor):
    """'US$ 145,300.50' -> 145300.5. Devuelve None si no hay número."""
    texto = str(valor or "").strip()
    if not texto:
        return None
    limpio = re.sub(r"[^0-9,.\-]", "", texto)
    if not limpio:
        return None
    if "," in limpio and "." in limpio:
        if limpio.rfind(",") > limpio.rfind("."):
            limpio = limpio.replace(".", "").replace(",", ".")
        else:
            limpio = limpio.replace(",", "")
    elif "," in limpio:
        entero, _, decimales = limpio.rpartition(",")
        limpio = f"{entero.replace(',', '')}.{decimales}" if len(decimales) in (1, 2) else limpio.replace(",", "")
    try:
        return float(limpio)
    except ValueError:
        return None


def formato_dinero(monto: float) -> str:
    if monto >= 1_000_000:
        return f"US$ {monto / 1_000_000:,.2f} M"
    if monto >= 10_000:
        return f"US$ {monto / 1_000:,.0f} K"
    return f"US$ {monto:,.0f}"


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def cargar_todo() -> dict:
    """UNA sola llamada a la API trae las 5 pestañas de categoría + el histórico."""
    vacio = {
        "activos": pd.DataFrame(columns=ALL_COLUMNS + ["Categoria", "FilaSheet"]),
        "historico": pd.DataFrame(columns=COLUMNAS_RECIBIDO),
        "pagos": pd.DataFrame(columns=COLUMNAS_PAGOS + ["FilaSheet"]),
        "hora": ahora_rd(), "ultima_carga": None, "ultima_persona": "", "avisos": [], "error": None,
    }
    try:
        ss = get_spreadsheet()
        indice = _indice_hojas()
    except Exception as e:
        return {**vacio, "error": f"No se pudo conectar con el Google Sheet: {e}"}

    objetivos = []  # (etiqueta, titulo_real)
    for cat in CATEGORIAS:
        ws = indice.get(_norm(cat))
        if ws is not None:
            objetivos.append((cat, ws.title))
    ws_rec = indice.get(_norm(RECIBIDO_SHEET))
    if ws_rec is not None:
        objetivos.append((RECIBIDO_SHEET, ws_rec.title))
    # Pagos es opcional a propósito: si nadie ha registrado un cargo todavía,
    # la pestaña ni existe. Se crea sola con el primer guardar_pago().
    ws_pagos = indice.get(_norm(PAGOS_SHEET))
    if ws_pagos is not None:
        objetivos.append((PAGOS_SHEET, ws_pagos.title))

    if not objetivos:
        return {**vacio, "error": "El Google Sheet no tiene ninguna de las pestañas esperadas."}

    rangos = [f"'{titulo}'!A1:AZ{MAX_FILAS_LECTURA}" for _, titulo in objetivos]
    try:
        respuesta = _con_reintento(lambda: ss.values_batch_get(rangos))
    except gspread.exceptions.APIError as e:
        return {**vacio, "error": f"Google Sheets no respondió (posible límite de cuota): {e}"}

    bloques = respuesta.get("valueRanges", [])
    frames, historico, avisos = [], pd.DataFrame(columns=COLUMNAS_RECIBIDO), []
    pagos = pd.DataFrame(columns=COLUMNAS_PAGOS)

    for (etiqueta, _titulo), bloque in zip(objetivos, bloques):
        valores = bloque.get("values", [])
        if len(valores) >= MAX_FILAS_LECTURA:
            avisos.append(
                f"La pestaña '{etiqueta}' llegó al tope de {MAX_FILAS_LECTURA:,} filas que la app lee. "
                "Puede haber embarques más abajo que no se están mostrando; hay que archivar lo viejo "
                "o subir el límite en el código."
            )
        if etiqueta == RECIBIDO_SHEET:
            historico = _df_desde_valores(valores, COLUMNAS_RECIBIDO)
            historico["FilaSheet"] = range(2, len(historico) + 2)
            continue
        if etiqueta == PAGOS_SHEET:
            pagos = _df_desde_valores(valores, COLUMNAS_PAGOS)
            pagos["FilaSheet"] = range(2, len(pagos) + 2)
            continue
        df_cat = _df_desde_valores(valores, ALL_COLUMNS)
        df_cat["Categoria"] = etiqueta
        # Fila real = índice + 2 (encabezado + base 1). Se guarda ANTES de filtrar
        # porque es lo que las escrituras usan para tocar la fila correcta.
        df_cat["FilaSheet"] = range(2, len(df_cat) + 2)
        frames.append(df_cat)

    if frames:
        activos = pd.concat(frames, ignore_index=True)
        for c in (COL_BL, COL_DESC):
            activos[c] = activos[c].astype(str).str.strip()
        activos = activos[(activos[COL_BL] != "") | (activos[COL_DESC] != "")]
        activos = activos.reset_index(drop=True)
        activos[COL_PAIS] = unificar_paises(activos[COL_PAIS])
    else:
        activos = pd.DataFrame(columns=ALL_COLUMNS + ["Categoria", "FilaSheet"])

    if not historico.empty:
        historico = historico[historico[COL_BL].astype(str).str.strip() != ""].reset_index(drop=True)

    if not pagos.empty:
        pagos = pagos[pagos[COL_BL].astype(str).str.strip() != ""].reset_index(drop=True)

    # "Última carga" = la marca más reciente escrita por alguien al agregar,
    # editar, cargar en masa o archivar. Es distinto de "última lectura".
    marcas = []
    for cuadro in (activos, historico):
        if not cuadro.empty and COL_ACTUALIZACION in cuadro.columns:
            marcas += [m for m in (parsear_marca(v) for v in cuadro[COL_ACTUALIZACION]) if m]
    ultima_carga = max(marcas) if marcas else None

    ultima_persona = ""
    if ultima_carga is not None:
        for cuadro in (activos, historico):
            if cuadro.empty or COL_ACTUALIZACION not in cuadro.columns:
                continue
            columna_autor = COL_ACTUALIZADO_POR if COL_ACTUALIZADO_POR in cuadro.columns else None
            if columna_autor is None and "Registrado_Por" in cuadro.columns:
                columna_autor = "Registrado_Por"
            if columna_autor is None:
                continue
            for marca, autor in zip(cuadro[COL_ACTUALIZACION], cuadro[columna_autor]):
                if parsear_marca(marca) == ultima_carga and str(autor).strip():
                    ultima_persona = str(autor).strip()
                    break
            if ultima_persona:
                break

    return {"activos": activos, "historico": historico, "pagos": pagos, "hora": ahora_rd(),
            "ultima_carga": ultima_carga, "ultima_persona": ultima_persona,
            "avisos": avisos, "error": None}


def invalidar_caches():
    cargar_todo.clear()


# --- Escrituras -------------------------------------------------------------
def _con_manejo_apierror(func):
    """Convierte un APIError de gspread (cuota, permisos) en (False, mensaje)
    legible en vez de tumbar la página con un traceback."""
    def envoltura(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            codigo = _codigo_api(e)
            if codigo == 429:
                return False, ("Google Sheets está limitando las peticiones (demasiadas a la vez). "
                               "Espera unos segundos y repite la acción.")
            if codigo == 403:
                return False, ("Google Sheets rechazó la operación por permisos. Verifica que el Sheet "
                               "siga compartido como Editor con la cuenta de servicio de la app.")
            return False, f"Google Sheets rechazó la operación ({e}). Espera unos segundos e intenta de nuevo."
        except Exception as e:  # noqa: BLE001 - último cortafuegos antes de la UI
            return False, f"Error inesperado: {e}"
    envoltura.__name__ = func.__name__
    return envoltura


def usuario_actual() -> str:
    return st.session_state.get("usuario", "desconocido")


def registrar_log(accion: str, bl: str = "", categoria: str = "", detalle: str = ""):
    """Bitácora de quién hizo qué. Nunca debe romper la operación principal:
    si el log falla, la acción ya se ejecutó y eso es lo que importa."""
    try:
        ws = get_worksheet(LOG_SHEET)
        if ws is None:
            ss = get_spreadsheet()
            ws = ss.add_worksheet(title=LOG_SHEET, rows=5000, cols=len(COLUMNAS_LOG))
            ws.update(range_name="A1", values=[COLUMNAS_LOG], value_input_option="RAW")
            _refrescar_estructura()
            ws = get_worksheet(LOG_SHEET)
        ws.append_row(
            [ahora_rd().strftime("%Y-%m-%d %H:%M:%S"), usuario_actual(), accion,
             str(bl), categoria, detalle],
            value_input_option="RAW",
        )
        _leer_log.clear()
    except Exception:
        pass


@st.cache_data(ttl=60, show_spinner=False)
def _leer_log(maximo: int = 300) -> pd.DataFrame:
    ws = get_worksheet(LOG_SHEET)
    if ws is None:
        return pd.DataFrame(columns=COLUMNAS_LOG)
    valores = _con_reintento(lambda: ws.get_all_values()) or []
    df = _df_desde_valores(valores, COLUMNAS_LOG)
    return df.tail(maximo).iloc[::-1].reset_index(drop=True)


@_con_manejo_apierror
def append_row(datos: dict, categoria: str):
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}' en el Google Sheet."
    datos = dict(datos)
    datos[COL_ACTUALIZACION] = marca_ahora()
    datos[COL_ACTUALIZADO_POR] = usuario_actual()
    # Solo se asegura la columna de los campos que traen valor real: así una
    # categoría que no usa OC o Modelo_Serie no termina con esa columna vacía.
    columnas_a_asegurar = [c for c, v in datos.items() if str(v).strip()]
    headers = _asegurar_columnas(ws, columnas_a_asegurar)
    _con_reintento(lambda: ws.append_row(_fila_desde_dict(headers, datos), value_input_option="RAW"))
    return True, ""


@_con_manejo_apierror
def append_rows_bulk(df: pd.DataFrame, categoria: str):
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}' en el Google Sheet."
    opcionales = [c for c in OPCIONALES_CATEGORIA
                  if c in df.columns and df[c].astype(str).str.strip().ne("").any()]
    headers = _asegurar_columnas(ws, [COL_ACTUALIZACION, COL_ACTUALIZADO_POR, *opcionales])
    sello, autor = marca_ahora(), usuario_actual()
    columnas_fila = REQUIRED_COLUMNS + opcionales
    filas = []
    for _, r in df.iterrows():
        datos = {c: r.get(c, "") for c in columnas_fila}
        datos[COL_ACTUALIZACION] = sello
        datos[COL_ACTUALIZADO_POR] = autor
        filas.append(_fila_desde_dict(headers, datos))
    _con_reintento(lambda: ws.append_rows(filas, value_input_option="RAW"))
    return True, ""


@_con_manejo_apierror
def actualizar_embarque(bl_original: str, categoria: str, datos: dict,
                        fila_sugerida=None, sello_esperado: str = "", forzar: bool = False):
    """Edición de un embarque ya cargado. Lee la fila actual para no pisar
    columnas que la app no gestiona, y reescribe la fila completa de una vez.

    Bloqueo optimista: si el sello de Fecha_Actualizacion de la fila cambió
    respecto al que tenía la pantalla, otra persona la editó mientras tanto y se
    aborta en vez de pisarle el cambio en silencio."""
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}'."
    fila, error = _localizar_fila(ws, bl_original, fila_sugerida)
    if error:
        return False, error

    columnas_a_asegurar = [COL_ACTUALIZACION, COL_ACTUALIZADO_POR,
                           *[c for c, v in datos.items() if str(v).strip()]]
    headers = _asegurar_columnas(ws, columnas_a_asegurar)
    combinado = _leer_fila(ws, fila, headers)
    combinado_norm = {_norm(k): v for k, v in combinado.items()}

    sello_actual = str(combinado_norm.get(_norm(COL_ACTUALIZACION), "")).strip()
    if not forzar and sello_esperado and sello_actual and sello_actual != str(sello_esperado).strip():
        autor = str(combinado_norm.get(_norm(COL_ACTUALIZADO_POR), "")).strip() or "otra persona"
        return False, (f"{autor} modificó este embarque el {sello_actual}, después de que abriste esta "
                       "pantalla. Actualiza los datos y revisa antes de guardar, o marca la casilla de "
                       "sobrescritura si estás seguro.")

    combinado.update(datos)
    combinado[COL_ACTUALIZACION] = marca_ahora()
    combinado[COL_ACTUALIZADO_POR] = usuario_actual()

    # Si la fila queda confirmada, el ETA resultante ES la fecha de llegada, así
    # que tiene que aguantar las mismas reglas que al confirmarla. Se valida
    # también cuando solo se edita el ETA de una fila ya confirmada: cambiar esa
    # fecha es cambiar la llegada, aunque la casilla no se toque.
    #
    # Antes esto se resolvía vaciando la confirmación cuando el ETA se iba a
    # futuro. Era peor: el embarque retrocedía de etapa sin que nadie lo supiera.
    combinado_final = {_norm(k): v for k, v in combinado.items()}
    if es_llego_si(combinado_final.get(_norm(COL_LLEGO), "")):
        problema = validar_eta_confirmado(combinado_final.get(_norm(COL_ETA), ""),
                                          combinado_final.get(_norm(COL_FECHA_DECLARACION), ""))
        if problema:
            return False, problema

    rango = f"{rowcol_to_a1(fila, 1)}:{rowcol_to_a1(fila, len(headers))}"
    _con_reintento(lambda: ws.update(range_name=rango,
                                     values=[_fila_desde_dict(headers, combinado)],
                                     value_input_option="RAW"))
    return True, ""


@_con_manejo_apierror
def marcar_llegada(bl: str, categoria: str, valor: str, fila_sugerida=None):
    """Escribe la respuesta a '¿ya llegó?'.

    valor = "SI"  -> llegó; la fecha de llegada pasa a ser el ETA de la fila
    valor = "NO"  -> se verificó que NO llegó (deja constancia de la revisión)
    valor = ""    -> borra la marca, vuelve a 'sin revisar'

    Vacío y "NO" NO son lo mismo: vacío es 'nadie ha mirado', NO es 'miré y
    sigue sin llegar'. Sin esa distinción no se puede saber si un embarque está
    atrasado o simplemente desatendido.

    Poner "NO" o vaciar la marca borra también la fecha de declaración: no se
    puede haber declarado algo que no ha llegado.

    Confirmar con SI valida antes el ETA de la fila (ver validar_eta_confirmado):
    ese valor pasa a ser la fecha real de llegada, así que no puede ser futuro
    ni ilegible."""
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}'."
    fila, error = _localizar_fila(ws, bl, fila_sugerida)
    if error:
        return False, error

    headers = _asegurar_columnas(ws, [COL_LLEGO, COL_ACTUALIZACION, COL_ACTUALIZADO_POR])
    indices = {_norm(h): i + 1 for i, h in enumerate(headers)}

    if es_llego_si(valor):
        actual = _leer_fila(ws, fila, headers)
        actual_norm = {_norm(k): v for k, v in actual.items()}
        problema = validar_eta_confirmado(actual_norm.get(_norm(COL_ETA), ""),
                                          actual_norm.get(_norm(COL_FECHA_DECLARACION), ""))
        if problema:
            return False, problema

    peticiones = [
        {"range": rowcol_to_a1(fila, indices[_norm(COL_LLEGO)]), "values": [[valor]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZACION)]), "values": [[marca_ahora()]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZADO_POR)]), "values": [[usuario_actual()]]},
    ]
    if not es_llego_si(valor):
        columna_dec = indices.get(_norm(COL_FECHA_DECLARACION))
        if columna_dec:
            peticiones.append({"range": rowcol_to_a1(fila, columna_dec), "values": [[""]]})
    _con_reintento(lambda: ws.batch_update(peticiones, value_input_option="RAW"))
    return True, ""


def confirmar_llegada(bl: str, categoria: str, fila_sugerida=None):
    """Un clic: 'esta carga ya llegó a puerto'. A partir de aquí el ETA de la
    fila vale como fecha real de llegada y el reloj de días en puerto arranca."""
    return marcar_llegada(bl, categoria, LLEGO_SI, fila_sugerida=fila_sugerida)


def marcar_no_llego(bl: str, categoria: str, fila_sugerida=None):
    """'Revisé y todavía no ha llegado'. Deja constancia de la revisión sin
    arrancar ningún contador."""
    return marcar_llegada(bl, categoria, LLEGO_NO, fila_sugerida=fila_sugerida)


def validar_eta_confirmado(eta_crudo, declaracion=None):
    """Reglas que debe cumplir el ETA de una fila con la llegada confirmada en SI.
    Devuelve un mensaje de error o "" si todo está bien.

    Existe porque, con este modelo, el ETA no es solo una estimación: en cuanto
    alguien confirma la llegada, ESE valor pasa a ser la fecha real de llegada a
    puerto y con él arrancan los días en puerto. Por eso hay que validarlo
    cuando se confirma Y cada vez que se edita una fila que ya está confirmada;
    si no, un ETA movido a futuro dejaría un embarque 'llegado' con fecha que
    todavía no ocurrió, y los contadores saldrían en negativo.

    La alternativa —borrar la confirmación en silencio cuando el ETA se mueve—
    es peor: nadie se entera de que el embarque retrocedió de etapa."""
    fecha = parsear_fecha(eta_crudo)
    if fecha is None:
        crudo = str(eta_crudo or "").strip()
        return ("La llegada está confirmada, pero el ETA "
                f"({crudo or 'vacío'}) no es una fecha que se pueda leer. Corrige el ETA, "
                "porque es el que vale como fecha real de llegada.")
    if fecha > hoy_rd():
        return (f"No se puede dar por llegada una carga con ETA {formato_eta(fecha)}, que es una "
                "fecha futura. Si todavía no ha llegado, marca '¿Llegó?' en NO; si ya llegó, "
                "corrige el ETA a la fecha real.")
    dec = parsear_fecha(declaracion)
    if dec and fecha > dec:
        return (f"El ETA {formato_eta(fecha)} quedaría después de la declaración "
                f"({formato_eta(dec)}). Corrige una de las dos fechas.")
    return ""


def _validar_orden_flujo(llegada, declaracion, almacen=None):
    """Las fechas del flujo tienen que ir en orden. Devuelve un mensaje de error
    o "" si todo está bien. Una declaración anterior a la llegada no es un dato
    válido: son contadores que después alguien lee como desempeño."""
    secuencia = [("Llegada a puerto", llegada),
                 ("Recepción y declaración", declaracion)]
    if almacen:
        secuencia.append((ETAPA_ALMACEN, almacen))
    previa_nombre, previa_fecha = None, None
    for etapa, f in secuencia:
        if not f:
            continue
        if previa_fecha and f < previa_fecha:
            return (f"La fecha de '{etapa}' ({formato_eta(f)}) es anterior a la de "
                    f"'{previa_nombre}' ({formato_eta(previa_fecha)}). Corrige una de las dos.")
        previa_nombre, previa_fecha = etapa, f
    return ""


@_con_manejo_apierror
def fijar_fecha_declaracion(bl: str, categoria: str, fecha=None, fila_sugerida=None,
                            sobrescribir: bool = False):
    """Registra la recepción y declaración. Exige que la llegada esté confirmada:
    no se declara una carga que, según el propio Sheet, todavía no llegó."""
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}'."
    fila, error = _localizar_fila(ws, bl, fila_sugerida)
    if error:
        return False, error

    headers = _asegurar_columnas(
        ws, [COL_LLEGO, COL_FECHA_DECLARACION, COL_ACTUALIZACION, COL_ACTUALIZADO_POR]
    )
    indices = {_norm(h): i + 1 for i, h in enumerate(headers)}
    combinado = _leer_fila(ws, fila, headers)
    combinado_norm = {_norm(k): v for k, v in combinado.items()}

    if not es_llego_si(combinado_norm.get(_norm(COL_LLEGO), "")):
        return False, ("Este embarque todavía no tiene la llegada confirmada. Marca primero "
                       "'¿Llegó?' en SI y después registra la declaración.")

    llegada = parsear_fecha(combinado_norm.get(_norm(COL_ETA), ""))
    actual = parsear_fecha(combinado_norm.get(_norm(COL_FECHA_DECLARACION), ""))
    nueva = fecha or hoy_rd()
    final = nueva if (sobrescribir or not actual) else actual
    if final == actual:
        return True, "Sin cambios: la declaración ya estaba registrada."

    problema = _validar_orden_flujo(llegada, final)
    if problema:
        return False, problema

    peticiones = [
        {"range": rowcol_to_a1(fila, indices[_norm(COL_FECHA_DECLARACION)]),
         "values": [[final.isoformat()]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZACION)]), "values": [[marca_ahora()]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZADO_POR)]), "values": [[usuario_actual()]]},
    ]
    _con_reintento(lambda: ws.batch_update(peticiones, value_input_option="RAW"))
    return True, ""


def avanzar_estado_puerto(bl: str, categoria: str, nueva_etapa: str, fila_sugerida=None,
                          fecha=None, sobrescribir: bool = False):
    """Mueve el embarque a la etapa indicada. Elegir una etapa ANTERIOR deshace
    las posteriores, que es la forma de corregir un registro equivocado."""
    if nueva_etapa not in ETAPAS_PUERTO:
        return False, f"Etapa '{nueva_etapa}' no reconocida."
    if nueva_etapa == ETAPA_LLEGADA:
        # Retroceder a 'llegada' implica borrar la declaración: eso lo hace
        # marcar_llegada al escribir SI de nuevo no es necesario, así que aquí
        # solo se confirma la llegada y se limpia lo posterior.
        ok, msg = marcar_llegada(bl, categoria, LLEGO_NO, fila_sugerida=fila_sugerida)
        if not ok:
            return ok, msg
        return marcar_llegada(bl, categoria, LLEGO_SI, fila_sugerida=fila_sugerida)
    return fijar_fecha_declaracion(bl, categoria, fecha=fecha, fila_sugerida=fila_sugerida,
                                   sobrescribir=sobrescribir)


@_con_manejo_apierror
def eliminar_embarque(bl: str, categoria: str, fila_sugerida=None):
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}'."
    fila, error = _localizar_fila(ws, bl, fila_sugerida)
    if error:
        return False, error
    _con_reintento(lambda: ws.delete_rows(fila))
    return True, ""


@_con_manejo_apierror
def marcar_como_recibido(bl: str, categoria: str, fila_sugerida=None,
                         fecha_declaracion=None, fecha_almacen=None):
    """Archiva el embarque en 'Recibido (Mes)' y lo saca del tablero activo.

    Candado: exige llegada confirmada y fecha de declaración. Si falta la
    declaración, la pantalla la pide ahí mismo y llega en 'fecha_declaracion'
    —se registra tal como ocurrió, no se inventa con la fecha de hoy.

    Aquí se CONGELA la llegada: el ETA vigente en este momento se escribe en
    Fecha_Llegada_Puerto del archivo. Es lo que protege el histórico de que
    alguien mueva el ETA meses después y cambie los días en puerto ya medidos."""
    ws_origen = get_worksheet(categoria)
    if ws_origen is None:
        return False, f"No existe la pestaña '{categoria}'."
    fila, error = _localizar_fila(ws_origen, bl, fila_sugerida)
    if error:
        return False, error

    headers_origen = _headers(ws_origen.title)
    datos = _leer_fila(ws_origen, fila, headers_origen)
    datos_norm = {_norm(k): v for k, v in datos.items()}

    if not es_llego_si(datos_norm.get(_norm(COL_LLEGO), "")):
        return False, "FALTAN_ETAPAS::" + ETAPA_LLEGADA

    eta_crudo = str(datos_norm.get(_norm(COL_ETA), "")).strip()
    llegada = parsear_fecha(eta_crudo)
    declaracion = parsear_fecha(datos_norm.get(_norm(COL_FECHA_DECLARACION), "")) or \
        parsear_fecha(fecha_declaracion)
    if not declaracion:
        return False, "FALTAN_ETAPAS::" + "Recepción y declaración"

    almacen = parsear_fecha(fecha_almacen) or hoy_rd()
    problema = _validar_orden_flujo(llegada, declaracion, almacen)
    if problema:
        return False, problema

    base = llegada if BASE_FECHA_RECIBIDO == "llegada" else almacen
    fecha_recibido = base or llegada
    if fecha_recibido is None:
        return False, (f"El BL '{bl}' no tiene un ETA interpretable ('{eta_crudo or 'vacío'}'), "
                       "así que no se puede saber a qué mes pertenece.")

    ws_destino = get_worksheet(RECIBIDO_SHEET)
    if ws_destino is None:
        nombres = ", ".join(f"'{h.title}'" for h in get_spreadsheet().worksheets())
        return False, f"No existe la pestaña '{RECIBIDO_SHEET}'. Las pestañas visibles son: {nombres}."
    headers_destino = _asegurar_columnas(ws_destino, COLUMNAS_RECIBIDO)

    registro = {
        COL_BL: datos_norm.get(_norm(COL_BL), bl),
        COL_DESC: datos_norm.get(_norm(COL_DESC), ""),
        COL_CANT: datos_norm.get(_norm(COL_CANT), ""),
        COL_PAIS: datos_norm.get(_norm(COL_PAIS), ""),
        COL_ETA: llegada.isoformat() if llegada else eta_crudo,
        "Fecha_Recibido": fecha_recibido.isoformat(),
        "Categoria_Origen": categoria,
        "Registrado_Por": usuario_actual(),
        COL_ACTUALIZACION: marca_ahora(),
        COL_ACTUALIZADO_POR: usuario_actual(),
        COL_FECHA_DECLARACION: declaracion.isoformat(),
        COL_FECHA_ALMACEN: almacen.isoformat(),
    }
    if llegada:
        registro[COL_FECHA_LLEGADA_PUERTO] = llegada.isoformat()
    # Se conserva TODO el rastro: sin esto, archivar borraba la evidencia de por
    # dónde pasó el embarque y con cuánta demora en cada paso.
    for columna in OPCIONALES_CATEGORIA:
        valor = str(datos_norm.get(_norm(columna), "")).strip()
        if valor:
            registro[columna] = valor

    _con_reintento(lambda: ws_destino.append_row(_fila_desde_dict(headers_destino, registro),
                                                 value_input_option="RAW"))
    try:
        _con_reintento(lambda: ws_origen.delete_rows(fila))
    except Exception as e:  # noqa: BLE001
        return False, (f"El embarque quedó archivado en '{RECIBIDO_SHEET}', pero NO se pudo borrar de "
                       f"'{categoria}' (fila {fila}): {e}. Bórrala a mano en el Sheet para que no quede "
                       "duplicado, o vuelve a intentarlo.")
    return True, ""


@_con_manejo_apierror
def quitar_de_recibido(bl: str, categoria_manual: str = None, fila_sugerida=None):
    """Reversa de 'Marcar como Recibido': devuelve el embarque a su categoría con
    lo que traía (llegada confirmada, declaración, y las opcionales que tuviera).

    La fecha de almacén no vuelve: justo eso es lo que se está deshaciendo, y
    esa columna solo existe en la pestaña de archivo."""
    ws_recibido = get_worksheet(RECIBIDO_SHEET)
    if ws_recibido is None:
        return False, f"No existe la pestaña '{RECIBIDO_SHEET}'."
    fila, error = _localizar_fila(ws_recibido, bl, fila_sugerida)
    if error:
        return False, error

    headers = _headers(ws_recibido.title)
    datos_norm = {_norm(k): v for k, v in _leer_fila(ws_recibido, fila, headers).items()}

    categoria = (categoria_manual or datos_norm.get(_norm("Categoria_Origen"), "")).strip()
    if categoria not in CATEGORIAS:
        return False, f"'{categoria or 'vacía'}' no es una categoría válida. Elige una del menú antes de confirmar."

    # El ETA que vuelve es el congelado al archivar, no el que pudiera haberse
    # movido: es la fecha con la que se midió este embarque.
    eta_vuelta = (str(datos_norm.get(_norm(COL_FECHA_LLEGADA_PUERTO), "")).strip()
                  or str(datos_norm.get(_norm(COL_ETA), "")).strip())

    devuelto = {
        COL_BL: datos_norm.get(_norm(COL_BL), bl),
        COL_DESC: datos_norm.get(_norm(COL_DESC), ""),
        COL_CANT: datos_norm.get(_norm(COL_CANT), ""),
        COL_PAIS: datos_norm.get(_norm(COL_PAIS), ""),
        COL_ETA: eta_vuelta,
        COL_LLEGO: LLEGO_SI,
    }
    for columna in (*OPCIONALES_CATEGORIA, COL_FECHA_DECLARACION):
        valor = str(datos_norm.get(_norm(columna), "")).strip()
        if valor:
            devuelto[columna] = valor

    ok, mensaje = append_row(devuelto, categoria)
    if not ok:
        return False, mensaje or f"No se pudo escribir de vuelta en '{categoria}'."
    try:
        _con_reintento(lambda: ws_recibido.delete_rows(fila))
    except Exception as e:  # noqa: BLE001
        return False, (f"El embarque volvió a '{categoria}', pero no se pudo borrar de "
                       f"'{RECIBIDO_SHEET}' (fila {fila}): {e}. Bórralo a mano para que no quede duplicado.")
    return True, ""


@_con_manejo_apierror
def normalizar_etas(cambios: list):
    """cambios = [(categoria, fila_sheet, valor_esperado, iso)]. Reescribe los ETA
    en ISO agrupando por pestaña: una llamada por pestaña, no una por celda.
    Antes de escribir compara con el valor que la pantalla vio: si alguien movió
    filas mientras tanto, esa celda se salta en vez de escribir sobre otra cosa."""
    if not cambios:
        return True, "Sin cambios."
    por_categoria = {}
    for categoria, fila, esperado, iso in cambios:
        por_categoria.setdefault(categoria, []).append((fila, esperado, iso))

    total, saltadas = 0, 0
    for categoria, lista in por_categoria.items():
        ws = get_worksheet(categoria)
        if ws is None:
            continue
        headers = _headers(ws.title)
        columna = _columna_indice(headers, COL_ETA)
        if columna is None:
            continue
        actuales = _con_reintento(lambda: ws.col_values(columna)) or []
        cuerpo = []
        for fila, esperado, iso in lista:
            visto = actuales[fila - 1] if len(actuales) >= fila else ""
            if str(visto).strip() != str(esperado).strip():
                saltadas += 1
                continue
            cuerpo.append({"range": rowcol_to_a1(fila, columna), "values": [[iso]]})
        if cuerpo:
            _con_reintento(lambda c=cuerpo: ws.batch_update(c, value_input_option="RAW"))
            total += len(cuerpo)
    extra = f" {saltadas} se saltaron porque el Sheet cambió; actualiza y repite." if saltadas else ""
    return True, f"{total} fecha(s) normalizada(s) a formato AAAA-MM-DD.{extra}"


# --- Escrituras: Estatus de Pago --------------------------------------------
def _buscar_fila_pago(ws, bl: str):
    """Ubica la fila de un BL en la pestaña Pagos. Devuelve (fila, ambiguo).

    A propósito NO reusa _localizar_fila(): esa función trata 'no encontrado'
    como un error, porque en tránsito toda fila ya existe de antemano. Aquí
    'no encontrado' es el caso normal la primera vez que se registra un
    expediente, así que se resuelve aparte en vez de forzar ese significado."""
    headers = _headers(ws.title)
    columna = _columna_indice(headers, COL_BL)
    if columna is None:
        return None, False
    valores = _con_reintento(lambda: ws.col_values(columna)) or []
    objetivo = _norm(bl)
    coincidencias = [i + 1 for i, v in enumerate(valores) if i >= 1 and _norm(v) == objetivo]
    if not coincidencias:
        return None, False
    if len(coincidencias) == 1:
        return coincidencias[0], False
    return None, True


def _aplicar_validaciones_pagos(ws):
    """Mejor esfuerzo: agrega los selectores nativos de Google Sheets —
    calendario en Fecha_SinMora / Fecha_PagoRealizado, lista desplegable en
    Empresa — para que esas columnas se elijan con clic en vez de tecleo
    libre.

    'strict': False en ambos casos a propósito: si algo no calza exactamente,
    Sheets lo marca con una advertencia visual en vez de RECHAZAR la
    escritura — un candado duro aquí podría bloquear una escritura real de la
    app o de Logística. Devuelve (ok, mensaje): quien llame desde la creación
    automática de la pestaña puede ignorar el resultado (es cosmético, no
    debe tumbar una escritura real de datos), pero un disparo manual sí
    necesita saber si de verdad funcionó."""
    try:
        headers = ws.row_values(1)
        requests = []
        for col_nombre in (COL_FECHA_SIN_MORA, COL_FECHA_PAGO_REAL):
            idx = _columna_indice(headers, col_nombre)
            if not idx:
                continue
            requests.append({
                "setDataValidation": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 1,
                        "endRowIndex": 2000,
                        "startColumnIndex": idx - 1,
                        "endColumnIndex": idx,
                    },
                    "rule": {
                        "condition": {"type": "DATE_IS_VALID"},
                        "showCustomUi": True,
                        "strict": False,
                    },
                },
            })
        idx_empresa = _columna_indice(headers, COL_EMPRESA)
        if idx_empresa:
            requests.append({
                "setDataValidation": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 1,
                        "endRowIndex": 2000,
                        "startColumnIndex": idx_empresa - 1,
                        "endColumnIndex": idx_empresa,
                    },
                    "rule": {
                        "condition": {"type": "ONE_OF_LIST",
                                      "values": [{"userEnteredValue": e} for e in EMPRESAS_PAGO]},
                        "showCustomUi": True,
                        "strict": False,
                    },
                },
            })
        if not requests:
            return False, "No se encontraron las columnas esperadas (fechas / Empresa) en la pestaña."
        get_spreadsheet().batch_update({"requests": requests})
        return True, "Selectores aplicados: calendario en las fechas, lista desplegable en Empresa."
    except Exception as e:  # noqa: BLE001
        return False, f"No se pudo aplicar los selectores: {e}"


def _obtener_o_crear_ws_pagos():
    ws = get_worksheet(PAGOS_SHEET)
    if ws is not None:
        return ws
    ss = get_spreadsheet()
    ws = ss.add_worksheet(title=PAGOS_SHEET, rows=2000, cols=len(COLUMNAS_PAGOS))
    _con_reintento(lambda: ws.update(range_name="A1", values=[COLUMNAS_PAGOS], value_input_option="RAW"))
    _refrescar_estructura()
    ws = get_worksheet(PAGOS_SHEET)
    _aplicar_validaciones_pagos(ws)  # mejor esfuerzo, no bloquea si falla
    return ws


def aplicar_selectores_pagos():
    """Aplica (o reintenta) los selectores de calendario y de Empresa sobre la
    pestaña Pagos que YA EXISTE. Para correrlo a mano una vez sobre una
    pestaña creada antes de que estos selectores existieran — la creación
    automática solo los aplica a pestañas nuevas. Se asegura primero de que
    las columnas existan (por si Empresa nunca se llegó a crear)."""
    ws = get_worksheet(PAGOS_SHEET)
    if ws is None:
        return False, f"No existe la pestaña '{PAGOS_SHEET}' todavía."
    _asegurar_columnas(ws, COLUMNAS_PAGOS)
    return _aplicar_validaciones_pagos(ws)


def mover_empresa_primera_columna():
    """Mueve la columna Empresa a la posición A (antes de BL) en una pestaña
    Pagos que ya existía de antes. Puramente cosmético: la app siempre busca
    las columnas por NOMBRE, nunca por posición, así que esto no cambia nada
    funcionalmente — es solo para que se vea como Logística lo pidió al
    trabajar directo en el Sheet. Pensado para correrlo una sola vez.

    Se asegura primero de que la columna exista: si nadie ha guardado nada
    desde que Empresa se agregó al esquema, la columna todavía no está en el
    Sheet, y sin esto el botón no encontraba nada que mover."""
    ws = get_worksheet(PAGOS_SHEET)
    if ws is None:
        return False, f"No existe la pestaña '{PAGOS_SHEET}' todavía."
    try:
        headers = _asegurar_columnas(ws, COLUMNAS_PAGOS)
        idx = _columna_indice(headers, COL_EMPRESA)
        if not idx:
            return False, "No se pudo crear ni encontrar la columna 'Empresa'."
        idx0 = idx - 1
        if idx0 == 0:
            return True, "'Empresa' ya es la primera columna."
        get_spreadsheet().batch_update({"requests": [{
            "moveDimension": {
                "source": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": idx0, "endIndex": idx0 + 1},
                "destinationIndex": 0,
            },
        }]})
        _refrescar_estructura()
        return True, "'Empresa' movida a la primera columna (A)."
    except Exception as e:  # noqa: BLE001
        return False, f"No se pudo mover la columna: {e}"


@_con_manejo_apierror
def guardar_pago(bl: str, conceptos: dict, estado: str = None, empresa: str = None, referencia: dict = None):
    """Crea o actualiza la fila de Pagos de un BL. `conceptos` trae únicamente
    los que aplican a este expediente (los que no, se guardan vacíos: 'no
    aplica' no es lo mismo que 'cero'). `empresa`, si viene, se aplica SIEMPRE
    (crear o editar) — a diferencia de Descripción/Cantidad/Llegada, Empresa
    no tiene otra fuente de verdad que pisar: es Logística quien la fija, así
    que corregirla aquí es exactamente lo que se espera. `referencia`
    (Descripción/Cantidad/Llegada) solo se usa AL CREAR la fila — si ya
    existe, no se toca, porque esos vienen de sincronizar_pagos_con_transito().
    No toca las ventanas SIN MORA ni Pago Realizado — esas se fijan aparte,
    con registrar_sin_mora() y registrar_pago_realizado()."""
    bl = str(bl or "").strip()
    if not bl:
        return False, "Falta el BL."

    ws = _obtener_o_crear_ws_pagos()
    fila, ambiguo = _buscar_fila_pago(ws, bl)
    if ambiguo:
        return False, (f"Hay más de un registro de pago para el BL '{bl}'. Corrígelo a mano en el "
                       f"Sheet '{PAGOS_SHEET}' antes de continuar.")

    headers = _asegurar_columnas(ws, COLUMNAS_PAGOS)
    datos = {c: v for c, v in conceptos.items() if c in CONCEPTOS_PAGO}
    if estado:
        datos[COL_ESTADO_PAGO] = estado
    if empresa:
        datos[COL_EMPRESA] = empresa
    datos[COL_ACTUALIZACION] = marca_ahora()
    datos[COL_ACTUALIZADO_POR] = usuario_actual()

    if fila is None:
        nuevo = {COL_BL: bl, **(referencia or {}), **datos}
        _con_reintento(lambda: ws.append_row(_fila_desde_dict(headers, nuevo), value_input_option="RAW"))
        return True, ""

    combinado = _leer_fila(ws, fila, headers)
    combinado.update(datos)
    rango = f"{rowcol_to_a1(fila, 1)}:{rowcol_to_a1(fila, len(headers))}"
    _con_reintento(lambda: ws.update(range_name=rango, values=[_fila_desde_dict(headers, combinado)],
                                     value_input_option="RAW"))
    return True, ""


@_con_manejo_apierror
def registrar_sin_mora(bl: str, fecha, sobrescribir: bool = False):
    """Fija la fecha límite saludable de pago, a criterio de Logística. Ya NO
    guarda montos: el total sale en vivo de los conceptos, así que aquí solo
    hace falta la fecha — sirve para medir después si el pago llegó a tiempo.
    Con sobrescribir=True se corrige una fecha ya puesta."""
    ws = get_worksheet(PAGOS_SHEET)
    if ws is None:
        return False, f"No existe la pestaña '{PAGOS_SHEET}'. Registra primero los conceptos del expediente."
    fila, ambiguo = _buscar_fila_pago(ws, bl)
    if ambiguo:
        return False, f"Hay más de un registro de pago para el BL '{bl}'."
    if fila is None:
        return False, f"El BL '{bl}' no tiene conceptos registrados todavía en '{PAGOS_SHEET}'."

    headers = _asegurar_columnas(ws, [COL_FECHA_SIN_MORA, COL_ACTUALIZACION, COL_ACTUALIZADO_POR])
    actual = _leer_fila(ws, fila, headers)
    ya_registrado = str(actual.get(COL_FECHA_SIN_MORA, "")).strip()
    if ya_registrado and not sobrescribir:
        return False, (f"Este expediente ya tiene una fecha saludable registrada ({ya_registrado}). "
                       "Marca la casilla de corrección para cambiarla.")

    indices = {_norm(h): i + 1 for i, h in enumerate(headers)}
    peticiones = [
        {"range": rowcol_to_a1(fila, indices[_norm(COL_FECHA_SIN_MORA)]), "values": [[fecha.isoformat()]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZACION)]), "values": [[marca_ahora()]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZADO_POR)]), "values": [[usuario_actual()]]},
    ]
    _con_reintento(lambda: ws.batch_update(peticiones, value_input_option="RAW"))
    return True, ""


@_con_manejo_apierror
def registrar_pago_realizado(bl: str, fecha, extra: dict, sobrescribir: bool = False):
    """Registra que el expediente ya se pagó: la fecha real y el EXTRA pagado
    de más sobre los conceptos originales (por mora, ajuste, etc.) — lo
    escribe Logística a mano, en USD y/o DOP; 0 si no hubo diferencia. No es
    un total: es la diferencia, y se le suma al total de los conceptos para
    mostrar el costo final. Marca el expediente como Pagado automáticamente.
    Con sobrescribir=True se corrige un pago ya registrado."""
    ws = get_worksheet(PAGOS_SHEET)
    if ws is None:
        return False, f"No existe la pestaña '{PAGOS_SHEET}'."
    fila, ambiguo = _buscar_fila_pago(ws, bl)
    if ambiguo:
        return False, f"Hay más de un registro de pago para el BL '{bl}'."
    if fila is None:
        return False, f"El BL '{bl}' no tiene conceptos registrados todavía en '{PAGOS_SHEET}'."

    headers = _asegurar_columnas(ws, [COL_FECHA_PAGO_REAL, COL_PAGOREAL_USD, COL_PAGOREAL_DOP,
                                       COL_ESTADO_PAGO, COL_ACTUALIZACION, COL_ACTUALIZADO_POR])
    actual = _leer_fila(ws, fila, headers)
    ya_registrado = str(actual.get(COL_FECHA_PAGO_REAL, "")).strip()
    if ya_registrado and not sobrescribir:
        return False, (f"Este expediente ya tiene un pago registrado ({ya_registrado}). "
                       "Marca la casilla de corrección para cambiarlo.")

    indices = {_norm(h): i + 1 for i, h in enumerate(headers)}
    peticiones = [
        {"range": rowcol_to_a1(fila, indices[_norm(COL_FECHA_PAGO_REAL)]), "values": [[fecha.isoformat()]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_PAGOREAL_USD)]), "values": [[extra.get("USD", 0.0)]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_PAGOREAL_DOP)]), "values": [[extra.get("DOP", 0.0)]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ESTADO_PAGO)]), "values": [[ESTADO_PAGO_PAGADO]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZACION)]), "values": [[marca_ahora()]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZADO_POR)]), "values": [[usuario_actual()]]},
    ]
    _con_reintento(lambda: ws.batch_update(peticiones, value_input_option="RAW"))
    return True, ""


@_con_manejo_apierror
def marcar_estado_pago(bl: str, estado: str):
    """Pendiente/Pagado: campo manual, lo marca Logística a criterio propio."""
    if estado not in (ESTADO_PAGO_PENDIENTE, ESTADO_PAGO_PAGADO):
        return False, f"Estado '{estado}' no reconocido."
    ws = get_worksheet(PAGOS_SHEET)
    if ws is None:
        return False, f"No existe la pestaña '{PAGOS_SHEET}'."
    fila, ambiguo = _buscar_fila_pago(ws, bl)
    if ambiguo:
        return False, f"Hay más de un registro de pago para el BL '{bl}'."
    if fila is None:
        return False, f"El BL '{bl}' no tiene conceptos registrados todavía."
    headers = _asegurar_columnas(ws, [COL_ESTADO_PAGO, COL_ACTUALIZACION, COL_ACTUALIZADO_POR])
    indices = {_norm(h): i + 1 for i, h in enumerate(headers)}
    peticiones = [
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ESTADO_PAGO)]), "values": [[estado]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZACION)]), "values": [[marca_ahora()]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZADO_POR)]), "values": [[usuario_actual()]]},
    ]
    _con_reintento(lambda: ws.batch_update(peticiones, value_input_option="RAW"))
    return True, ""


@_con_manejo_apierror
def sincronizar_pagos_con_transito(activos: pd.DataFrame, historico: pd.DataFrame):
    """Agrega a Pagos, con BL/Descripción/Cantidad/Llegada ya llenos, los
    expedientes de tránsito (activos + histórico) que todavía no tienen fila
    ahí. Pensado para que Logística pueda abrir el Google Sheet directamente y
    encontrar la fila ya lista para escribir los montos, sin tener que crearla
    a mano ni pasar por la app.

    No pisa ni borra nada de lo que ya exista en Pagos: un BL que ya tiene
    fila se deja tal cual, aunque su descripción o llegada haya cambiado en
    tránsito después de sincronizado. Sincronizar solo AGREGA lo que falta."""
    ws = _obtener_o_crear_ws_pagos()
    headers = _asegurar_columnas(ws, COLUMNAS_PAGOS)
    columna_bl = _columna_indice(headers, COL_BL)
    existentes = set()
    if columna_bl:
        valores = _con_reintento(lambda: ws.col_values(columna_bl)) or []
        existentes = {_norm(v) for v in valores[1:] if str(v).strip()}

    sello, autor = marca_ahora(), usuario_actual()
    nuevas, vistos = [], set()
    for fuente, es_hist in ((activos, False), (historico, True)):
        if fuente is None or fuente.empty:
            continue
        for _, r in fuente.iterrows():
            bl = str(r.get(COL_BL, "")).strip()
            clave = _norm(bl)
            if not bl or clave in existentes or clave in vistos:
                continue
            if es_hist:
                llegada = (parsear_fecha(r.get(COL_FECHA_LLEGADA_PUERTO, ""))
                          or parsear_fecha(r.get(COL_ETA, "")))
            else:
                llegada = fecha_llegada_fila(r) or parsear_fecha(r.get(COL_ETA, ""))
            nuevas.append({
                COL_BL: bl,
                COL_DESC: str(r.get(COL_DESC, "")),
                COL_CANT: str(r.get(COL_CANT, "")),
                COL_PAGO_LLEGADA: llegada.isoformat() if llegada else "",
                COL_ACTUALIZACION: sello,
                COL_ACTUALIZADO_POR: autor,
            })
            # Empresa se deja en blanco a propósito: tránsito no tiene ese
            # concepto (solo trackea Antillana), y quién debe cada expediente
            # lo decide Logística a mano, no la sincronización.
            vistos.add(clave)

    if not nuevas:
        return True, "0 expedientes nuevos: Pagos ya tenía todos los BL de tránsito."

    filas = [_fila_desde_dict(headers, n) for n in nuevas]
    _con_reintento(lambda: ws.append_rows(filas, value_input_option="RAW"))
    return True, f"{len(nuevas)} expediente(s) agregado(s) a Pagos desde tránsito."
