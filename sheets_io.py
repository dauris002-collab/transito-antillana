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


COL_MODELO = "Modelo_Serie"


COL_CANT = "Cantidad"


COL_PAIS = "Pais_Origen"


COL_ETA = "Llegada a Puerto (ETA)"


COL_DIAS_PUERTO = "Dias en puerto"     # existe en el Sheet; la app lo calcula en vivo y no lo escribe


COL_ACTUALIZACION = "Fecha_Actualizacion"  # el Sheet lo tiene con tilde; _norm lo resuelve


COL_ACTUALIZADO_POR = "Actualizado_Por"    # la app la crea sola la primera vez que escribe


COL_ESTATUS_LLEGADA = "Estatus_Llegada"    # vacío = sin confirmar; "Retrasado" = se verificó que NO llegó


# Fecha de salida del origen: opcional, la trae quien la conoce (booking del
# forwarder/naviera). Alimenta el Contador 1 (salida -> puerto). Aplica a
# cualquier categoría, no solo a las marítimas.
COL_FECHA_SALIDA = "Fecha_Salida"


# Flujo detallado de 5 etapas, para TODAS las categorías. No hay una columna de
# texto "Estado_Puerto": la etapa activa se calcula sola como la última de estas
# 5 fechas que esté llena (ver _etapa_de_fechas). Vacío en las 5 = todavía no se
# confirmó la llegada, aunque el ETA ya haya vencido. Elegir una etapa ANTERIOR
# vacía las fechas posteriores (es la forma de corregir/retroceder).
COL_FECHA_LLEGADA_PUERTO = "Fecha_Llegada_Puerto"    # llegada física a puerto/aeropuerto


COL_FECHA_DECLARACION = "Fecha_Declaracion"          # inicia la recepción y declaración


COL_FECHA_SOLICITUD_PAGO = "Fecha_Solicitud_Pago"    # cuándo se le pidió el pago a Finanzas


COL_FECHA_PAGO = "Fecha_Pago"                        # Finanzas confirma que ya pagó


# Reutiliza la columna "Fecha_Despacho" que ya existía sin usar en el Sheet real
# (Generadores y Carga Suelta) — "despachado" y "recibido en almacén" son la
# misma acción, así que no hizo falta crear una columna nueva.
COL_FECHA_ALMACEN = "Fecha_Despacho"                 # entrada a almacén = "Marcar como recibido"


# OC (orden de compra) y EE (entrega entrante): propias de Aéreos y Carga
# Suelta, tal como vienen en el formulario/Sheet real de esas dos categorías.
COL_OC = "OC"


COL_EE = "EE"


# Tarifa de demora por embarque, escrita a mano en el Sheet. Opcional: si la
# columna no existe, o la celda está vacía, se usa la tarifa de Secrets. Manda
# siempre la del Sheet, porque la tarifa real la fija el contrato de cada
# naviera y no un promedio de la app. Acepta entero o decimal, con o sin
# separadores de miles ("2000", "2,500.50", "RD$ 3.000").
COL_COSTO_DIA = "Costo_Por_Dia"


# Columnas opcionales: si existen en el Sheet, la app las usa; si no, ni se
# mencionan. Así se pueden agregar Orden_Compra, Cliente, Puerto_Destino o
# Valor_USD desde Google Sheets sin tocar una línea de código.
NOMBRES_VALOR = {"valor_usd", "valor", "monto", "valor_cif", "monto_usd", "valor us$", "valor us"}


MAX_FILAS_LECTURA = 20000


REQUIRED_COLUMNS = [COL_BL, COL_DESC, COL_MODELO, COL_CANT, COL_PAIS, COL_ETA]


COLUMNAS_FLUJO = [COL_FECHA_LLEGADA_PUERTO, COL_FECHA_DECLARACION,
                  COL_FECHA_SOLICITUD_PAGO, COL_FECHA_PAGO, COL_FECHA_ALMACEN]


ALL_COLUMNS = REQUIRED_COLUMNS + [COL_DIAS_PUERTO, COL_ACTUALIZACION, COL_ACTUALIZADO_POR,
                                  COL_ESTATUS_LLEGADA, COL_FECHA_SALIDA,
                                  *COLUMNAS_FLUJO, COL_OC, COL_EE]


# Columnas que la app calcula o gestiona internamente y que no se muestran como
# "campos extra" del embarque. Si se agrega un cálculo nuevo en enriquecer(),
# su nombre TIENE que entrar aquí o aparecerá como columna suelta en la ficha.
COLUMNAS_INTERNAS = {
    "Categoria", "FilaSheet", "EstadoTexto", "DiasRel", "ETAFecha", "Prioridad", "OrdenSec",
    "ValorNum", "Buscar", "EtapaActual", "EtapaIdx", "Alerta", "AlertaDias",
    "DiasTransito", "DiasSolicitudPago", "DiasPagoDespacho", "DiasEnPuerto", "DiasEnEtapa",
    "F_Salida", "F_Puerto", "F_Declaracion", "F_Solicitud", "F_Pago", "F_Almacen",
    "BLRepetido", "FlujoRaro",
    COL_DIAS_PUERTO,
}


CATEGORIAS = ["Equipos", "Generadores", "Aéreos", "Carga Suelta", "Consolidados"]


# 3 contadores operativos:
#   1) Salida -> Llegada a puerto (tránsito; se congela al confirmar la llegada)
#   2) Solicitud de pago -> Pago realizado (se congela al pagar)
#   3) Pago realizado -> hoy (espera de despacho; deja de verse al archivar)
ETAPAS_PUERTO = [
    "Llegada a puerto",
    "Recepción y declaración",
    "Solicitud de pago a finanzas",
    "Pago realizado",
    "Recibido en almacén",
]


INDICE_ETAPA = {e: i for i, e in enumerate(ETAPAS_PUERTO)}


COLUMNA_FECHA_ETAPA = {
    "Llegada a puerto": COL_FECHA_LLEGADA_PUERTO,
    "Recepción y declaración": COL_FECHA_DECLARACION,
    "Solicitud de pago a finanzas": COL_FECHA_SOLICITUD_PAGO,
    "Pago realizado": COL_FECHA_PAGO,
    "Recibido en almacén": COL_FECHA_ALMACEN,
}


# Días tolerados en cada etapa antes de considerar que hay un cuello de botella.
# Son estimaciones de arranque, NO un estándar medido: ajústalos con los datos
# reales una vez que el histórico tenga unos meses (Herramientas muestra las
# medianas). Se pueden sobrescribir desde Secrets sin tocar código.
SLA_ETAPA_DEFECTO = {
    "Llegada a puerto": 3,
    "Recepción y declaración": 2,
    "Solicitud de pago a finanzas": 5,
    "Pago realizado": 3,
}


SLA_RETRASO_DEFECTO = 7   # días de retraso sin actualizar el ETA antes de avisar


# Atraso en puerto y su costo. Son DOS umbrales distintos y confundirlos falsea
# el número: UMBRAL_ATRASO_PUERTO es a partir de cuándo TÚ consideras que un
# embarque está atrasado (criterio interno), y DIAS_LIBRES es a partir de cuándo
# la naviera o la terminal EMPIEZAN A COBRAR (criterio del proveedor, viene en el
# contrato). El conteo de atrasados usa el primero; el costo usa el segundo.
UMBRAL_ATRASO_PUERTO_DEFECTO = 5   # alerta a partir de 5 días en puerto/aeropuerto


DIAS_LIBRES_DEFECTO = 0   # el costo corre desde la llegada a puerto, no desde el día 8


# Sin tarifa de respaldo: el costo de demora sale ÚNICA y EXCLUSIVAMENTE de
# Costo_Por_Dia en el Sheet, embarque por embarque — a pedido explícito, para
# no promediar una tarifa que en realidad varía por naviera, terminal y
# cantidad de contenedores. Sin esa celda llena, ese embarque no suma dinero.
MONEDA_DEFECTO = "RD$"


# Qué fecha manda para decidir a qué mes pertenece un embarque recibido:
#   "llegada" -> fecha real de llegada a puerto (respaldo: ETA)
#   "almacen" -> fecha de entrada a almacén
# Cambia esta sola línea si el criterio del negocio es el otro.
BASE_FECHA_RECIBIDO = "almacen"


RECIBIDO_SHEET = "Recibido (Mes)"


LOG_SHEET = "Log"


COLUMNAS_RECIBIDO = [
    COL_BL, COL_DESC, COL_MODELO, COL_CANT, COL_PAIS, COL_ETA,
    "Fecha_Recibido", "Categoria_Origen", "Registrado_Por", COL_ACTUALIZACION,
    COL_ACTUALIZADO_POR, COL_FECHA_SALIDA, *COLUMNAS_FLUJO, COL_OC, COL_EE,
]


COLUMNAS_LOG = ["Fecha_Hora", "Usuario", "Accion", "BL", "Categoria", "Detalle"]


CACHE_TTL = 45              # segundos de caché de lectura


REINTENTOS_API = 3


VALOR_RETRASADO = "Retrasado"


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
    sin distinguir mayúsculas. Es lo que evita que 'Fecha_Actualización' y
    'Fecha_Actualizacion' se traten como columnas distintas. Con caché porque se
    llama miles de veces por refresco (búsqueda, mapeo de columnas, etapas)."""
    return _norm_cache(str(texto))


def _slug_css(texto) -> str:
    """Convierte un nombre visible ('Aéreos', 'Carga Suelta') en un identificador
    ASCII apto para usarse como clave de widget o clase CSS: 'aereos', 'carga_suelta'."""
    base = _norm(texto)
    limpio = "".join(c if c.isalnum() else "_" for c in base)
    while "__" in limpio:
        limpio = limpio.replace("__", "_")
    return limpio.strip("_") or "x"


@st.cache_resource
def costos_puerto() -> dict:
    """Parámetros del atraso en puerto que NO son la tarifa. Se ajustan desde
    Secrets:

        [costo_puerto]
        umbral = 5
        moneda = "RD$"
        dias_libres = 5

        [costo_puerto.dias_libres_por_categoria]
        Aéreos = 2

    La tarifa diaria no vive aquí: sale exclusivamente de Costo_Por_Dia en el
    Sheet (ver costo_dia_fila). No hay tarifa global ni por categoría de
    respaldo — si esa celda está vacía, ese embarque no suma dinero."""
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
    tocar código:  [sla]  llegada_a_puerto = 4  /  pago_realizado = 2 ..."""
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
    columna). Devuelve la lista final de encabezados."""
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

    Este es el corazón del arreglo de esta versión. Antes se usaba ws.find(BL),
    que devuelve la PRIMERA coincidencia. Con dos embarques parciales del mismo
    BL, la app editaba, archivaba o borraba la fila equivocada en silencio.
    Ahora la pantalla manda el número de fila que está mostrando y aquí se
    verifica contra el Sheet: si esa fila sigue teniendo ese BL, se usa; si el
    Sheet cambió y solo hay una coincidencia, se usa esa; si hay varias y
    ninguna es la sugerida, NO se escribe nada y se dice por qué."""
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
                                                        "Registrado_Por", "FechaParsed", "Anio", "Mes"}
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

    if not objetivos:
        return {**vacio, "error": "El Google Sheet no tiene ninguna de las pestañas esperadas."}

    rangos = [f"'{titulo}'!A1:AZ{MAX_FILAS_LECTURA}" for _, titulo in objetivos]
    try:
        respuesta = _con_reintento(lambda: ss.values_batch_get(rangos))
    except gspread.exceptions.APIError as e:
        return {**vacio, "error": f"Google Sheets no respondió (posible límite de cuota): {e}"}

    bloques = respuesta.get("valueRanges", [])
    frames, historico, avisos = [], pd.DataFrame(columns=COLUMNAS_RECIBIDO), []

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

    return {"activos": activos, "historico": historico, "hora": ahora_rd(),
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
    # Cualquier campo con valor real se asegura como columna, así un campo nuevo
    # que se agregue más adelante no necesita tocar esta función.
    columnas_a_asegurar = [c for c, v in datos.items() if str(v).strip()]
    headers = _asegurar_columnas(ws, columnas_a_asegurar)
    _con_reintento(lambda: ws.append_row(_fila_desde_dict(headers, datos), value_input_option="RAW"))
    return True, ""


@_con_manejo_apierror
def append_rows_bulk(df: pd.DataFrame, categoria: str):
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}' en el Google Sheet."
    opcionales = [c for c in (COL_FECHA_SALIDA, COL_OC, COL_EE)
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

    columnas_a_asegurar = [COL_ACTUALIZACION, COL_ACTUALIZADO_POR, *datos.keys()]
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
    # Si el ETA se movió a futuro, el embarque vuelve a estar en tránsito y la
    # marca de "verificado que no llegó" queda obsoleta.
    eta_nuevo = parsear_fecha(datos.get(COL_ETA, combinado.get(COL_ETA, "")))
    if eta_nuevo and eta_nuevo > hoy_rd():
        combinado[COL_ESTATUS_LLEGADA] = ""

    rango = f"{rowcol_to_a1(fila, 1)}:{rowcol_to_a1(fila, len(headers))}"
    _con_reintento(lambda: ws.update(range_name=rango,
                                     values=[_fila_desde_dict(headers, combinado)],
                                     value_input_option="RAW"))
    return True, ""


@_con_manejo_apierror
def marcar_estatus_llegada(bl: str, categoria: str, valor: str, fila_sugerida=None):
    """Escribe (o limpia) la respuesta a '¿ya llegó?'. valor="" borra la marca,
    valor="Retrasado" deja constancia de que se verificó que NO llegó."""
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}'."
    fila, error = _localizar_fila(ws, bl, fila_sugerida)
    if error:
        return False, error

    headers = _asegurar_columnas(ws, [COL_ESTATUS_LLEGADA, COL_ACTUALIZACION, COL_ACTUALIZADO_POR])
    indices = {_norm(h): i + 1 for i, h in enumerate(headers)}
    peticiones = [
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ESTATUS_LLEGADA)]), "values": [[valor]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZACION)]), "values": [[marca_ahora()]]},
        {"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZADO_POR)]), "values": [[usuario_actual()]]},
    ]
    _con_reintento(lambda: ws.batch_update(peticiones, value_input_option="RAW"))
    return True, ""


def _validar_orden_flujo(final: dict):
    """Las 5 fechas del flujo tienen que ir en orden. Devuelve un mensaje de
    error o "" si todo está bien. Una fecha de pago anterior a la declaración no
    es un dato válido: son contadores que después alguien lee como desempeño."""
    previa_nombre, previa_fecha = None, None
    for etapa in ETAPAS_PUERTO:
        f = final.get(etapa)
        if not f:
            continue
        if previa_fecha and f < previa_fecha:
            return (f"La fecha de '{etapa}' ({formato_eta(f)}) es anterior a la de "
                    f"'{previa_nombre}' ({formato_eta(previa_fecha)}). Corrige una de las dos.")
        previa_nombre, previa_fecha = etapa, f
    return ""


@_con_manejo_apierror
def fijar_fechas_flujo(bl: str, categoria: str, fechas: dict, fila_sugerida=None,
                       etapa_destino: str = "", sobrescribir: bool = False):
    """Escribe una o varias fechas del flujo de una sola vez.

    fechas          {nombre_etapa: date}
    etapa_destino   si viene, vacía las fechas POSTERIORES a esa etapa (es la
                    forma de retroceder/corregir: como la etapa activa es "la
                    última fecha llena", retroceder es vaciar lo de después).
    sobrescribir    False respeta una fecha ya puesta; True la reemplaza.
    """
    ws = get_worksheet(categoria)
    if ws is None:
        return False, f"No existe la pestaña '{categoria}'."
    fila, error = _localizar_fila(ws, bl, fila_sugerida)
    if error:
        return False, error

    headers = _asegurar_columnas(
        ws, [*COLUMNAS_FLUJO, COL_ESTATUS_LLEGADA, COL_ACTUALIZACION, COL_ACTUALIZADO_POR]
    )
    indices = {_norm(h): i + 1 for i, h in enumerate(headers)}
    combinado = _leer_fila(ws, fila, headers)
    combinado_norm = {_norm(k): v for k, v in combinado.items()}

    actuales, final = {}, {}
    for etapa in ETAPAS_PUERTO:
        columna = COLUMNA_FECHA_ETAPA[etapa]
        actual = parsear_fecha(combinado_norm.get(_norm(columna), ""))
        actuales[etapa] = actual
        propuesta = fechas.get(etapa)
        final[etapa] = propuesta if (propuesta and (sobrescribir or not actual)) else actual

    if etapa_destino:
        for etapa in ETAPAS_PUERTO[INDICE_ETAPA[etapa_destino] + 1:]:
            final[etapa] = None

    problema = _validar_orden_flujo(final)
    if problema:
        return False, problema

    peticiones = []
    for etapa in ETAPAS_PUERTO:
        if final[etapa] == actuales[etapa]:
            continue
        columna = COLUMNA_FECHA_ETAPA[etapa]
        valor = final[etapa].isoformat() if final[etapa] else ""
        peticiones.append({"range": rowcol_to_a1(fila, indices[_norm(columna)]), "values": [[valor]]})

    if not peticiones:
        return True, "Sin cambios: las fechas ya estaban así."

    # Si venía marcado "Retrasado" y ahora hay al menos una etapa con fecha,
    # significa que sí llegó: se limpia para que no quede "Retrasado" para siempre.
    if any(final.values()) and _norm(combinado_norm.get(_norm(COL_ESTATUS_LLEGADA), "")) == _norm(VALOR_RETRASADO):
        peticiones.append({"range": rowcol_to_a1(fila, indices[_norm(COL_ESTATUS_LLEGADA)]), "values": [[""]]})
    peticiones.append({"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZACION)]),
                       "values": [[marca_ahora()]]})
    peticiones.append({"range": rowcol_to_a1(fila, indices[_norm(COL_ACTUALIZADO_POR)]),
                       "values": [[usuario_actual()]]})

    _con_reintento(lambda: ws.batch_update(peticiones, value_input_option="RAW"))
    return True, ""


def avanzar_estado_puerto(bl: str, categoria: str, nueva_etapa: str, fila_sugerida=None,
                          fecha=None, sobrescribir: bool = False):
    """Fija la fecha de la etapa elegida y vacía las posteriores (retroceso)."""
    if nueva_etapa not in ETAPAS_PUERTO:
        return False, f"Etapa '{nueva_etapa}' no reconocida."
    return fijar_fechas_flujo(
        bl, categoria, {nueva_etapa: fecha or hoy_rd()}, fila_sugerida=fila_sugerida,
        etapa_destino=nueva_etapa, sobrescribir=sobrescribir,
    )


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
                         fechas_faltantes: dict = None, fecha_almacen=None):
    """Archiva el embarque en 'Recibido (Mes)' y lo saca del tablero activo.

    Candado: no archiva si faltan fechas de las 4 etapas previas. Pero en vez de
    dejar al usuario en un callejón sin salida, la pantalla le ofrece llenarlas
    ahí mismo y esas fechas llegan aquí en 'fechas_faltantes' — se registran
    tal como ocurrieron, no se inventan con la fecha de hoy.

    El mes al que pertenece el embarque lo decide BASE_FECHA_RECIBIDO: por
    defecto la fecha REAL de llegada a puerto, con el ETA solo como respaldo
    cuando esa no existe (antes era siempre el ETA, que es una estimación)."""
    ws_origen = get_worksheet(categoria)
    if ws_origen is None:
        return False, f"No existe la pestaña '{categoria}'."
    fila, error = _localizar_fila(ws_origen, bl, fila_sugerida)
    if error:
        return False, error

    headers_origen = _headers(ws_origen.title)
    datos = _leer_fila(ws_origen, fila, headers_origen)
    datos_norm = {_norm(k): v for k, v in datos.items()}
    fechas_faltantes = fechas_faltantes or {}

    final = {}
    for etapa in ETAPAS_PUERTO:
        columna = COLUMNA_FECHA_ETAPA[etapa]
        final[etapa] = parsear_fecha(datos_norm.get(_norm(columna), "")) or fechas_faltantes.get(etapa)

    faltan = [e for e in ETAPAS_PUERTO[:-1] if not final[e]]
    if faltan:
        return False, "FALTAN_ETAPAS::" + "|".join(faltan)

    final[ETAPAS_PUERTO[-1]] = (fecha_almacen or final[ETAPAS_PUERTO[-1]] or hoy_rd())
    problema = _validar_orden_flujo(final)
    if problema:
        return False, problema

    eta_crudo = str(datos_norm.get(_norm(COL_ETA), "")).strip()
    eta = parsear_fecha(eta_crudo)
    base = final["Llegada a puerto"] if BASE_FECHA_RECIBIDO == "llegada" else final[ETAPAS_PUERTO[-1]]
    fecha_recibido = base or eta
    if fecha_recibido is None:
        return False, (f"El BL '{bl}' no tiene ni fecha de llegada ni un ETA interpretable "
                       f"('{eta_crudo or 'vacío'}'), así que no se puede saber a qué mes pertenece.")

    ws_destino = get_worksheet(RECIBIDO_SHEET)
    if ws_destino is None:
        nombres = ", ".join(f"'{h.title}'" for h in get_spreadsheet().worksheets())
        return False, f"No existe la pestaña '{RECIBIDO_SHEET}'. Las pestañas visibles son: {nombres}."
    headers_destino = _asegurar_columnas(ws_destino, COLUMNAS_RECIBIDO)

    registro = {
        COL_BL: datos_norm.get(_norm(COL_BL), bl),
        COL_DESC: datos_norm.get(_norm(COL_DESC), ""),
        COL_MODELO: datos_norm.get(_norm(COL_MODELO), ""),
        COL_CANT: datos_norm.get(_norm(COL_CANT), ""),
        COL_PAIS: datos_norm.get(_norm(COL_PAIS), ""),
        COL_ETA: eta.isoformat() if eta else eta_crudo,
        "Fecha_Recibido": fecha_recibido.isoformat(),
        "Categoria_Origen": categoria,
        "Registrado_Por": usuario_actual(),
        COL_ACTUALIZACION: marca_ahora(),
        COL_ACTUALIZADO_POR: usuario_actual(),
    }
    # Se conserva TODO el rastro: sin esto, archivar borraba la evidencia de por
    # dónde pasó el embarque y con cuánta demora en cada paso.
    for etapa in ETAPAS_PUERTO:
        if final[etapa]:
            registro[COLUMNA_FECHA_ETAPA[etapa]] = final[etapa].isoformat()
    for columna in (COL_FECHA_SALIDA, COL_OC, COL_EE):
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
    TODO lo que traía (fechas del flujo, salida, OC/EE), no pelado como antes."""
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

    devuelto = {
        COL_BL: datos_norm.get(_norm(COL_BL), bl),
        COL_DESC: datos_norm.get(_norm(COL_DESC), ""),
        COL_MODELO: datos_norm.get(_norm(COL_MODELO), ""),
        COL_CANT: datos_norm.get(_norm(COL_CANT), ""),
        COL_PAIS: datos_norm.get(_norm(COL_PAIS), ""),
        COL_ETA: datos_norm.get(_norm(COL_ETA), ""),
    }
    for columna in (COL_FECHA_SALIDA, COL_OC, COL_EE, *COLUMNAS_FLUJO):
        valor = str(datos_norm.get(_norm(columna), "")).strip()
        if valor:
            devuelto[columna] = valor
    # Vuelve al tablero activo: la última etapa ("Recibido en almacén") se limpia,
    # porque justo eso es lo que se está deshaciendo.
    devuelto[COL_FECHA_ALMACEN] = ""

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
