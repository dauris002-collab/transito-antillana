"""
Antillana Comercial · Visibilidad de embarques en tránsito
===========================================================
app.py — v3.0

Cambios estructurales frente a la versión anterior (resumen para mantenimiento):

 1. ESCRITURAS POR NÚMERO DE FILA, NO POR BL. Era el bug de fondo que quedaba
    pendiente: todas las escrituras ubicaban la fila con ws.find(BL), que
    devuelve la PRIMERA coincidencia. Con BLs repetidos —y en los datos reales
    los hay, por embarques parciales— se podía editar, archivar o borrar la
    fila equivocada sin que nadie se enterara. Ahora la interfaz manda el
    número de fila real del Sheet, la app verifica que esa fila siga teniendo
    ese BL y, si no coincide, resuelve: una sola coincidencia se usa; varias,
    se niega a escribir y lo dice. Nunca adivina.
 2. Cada etapa se puede fechar en el día en que REALMENTE ocurrió, no solo hoy.
    Antes, registrar el viernes algo que pasó el miércoles metía dos días de
    error en los contadores. Se valida además que las fechas del flujo vayan en
    orden (una etapa no puede ser anterior a la que la precede).
 3. Diagrama de flujo en HTML/CSS en vez de Plotly. Con 20 embarques en puerto
    se estaban creando 20 gráficos Plotly por pantalla: en celular eso es
    medio megabyte de JavaScript y varios segundos de render. El diagrama nuevo
    es texto y CSS: instantáneo, imprimible, y en celular se vuelve vertical
    (legible) en vez de cinco puntos aplastados.
 4. Alertas de cuello de botella con SLA por etapa. La app ya no solo muestra
    dónde está cada embarque: dice cuáles llevan demasiado tiempo detenidos y
    en qué etapa. Los umbrales se ajustan desde Streamlit Secrets, sección
    [sla], sin tocar código (claves: llegada_a_puerto, recepcion_y_declaracion,
    solicitud_de_pago_a_finanzas, pago_realizado, retraso).
 5. Reintentos con espera ante errores 429/500/503 de Google. Con cuatro
    personas cargando a la vez, la cuota de la API se toca; antes eso era un
    error en pantalla, ahora se reintenta solo.
 6. Menos llamadas a la API por acción: se eliminó el ws.find() de cada
    escritura y se dejó de releer la fila cuando ya se tiene el dato en memoria.
 7. Archivar ya no pierde información: al marcar recibido se conservan también
    Fecha_Salida, OC y EE además de las fechas del flujo, y revertir una
    recepción devuelve TODO eso a la categoría de origen (antes volvía pelado).
 8. Fecha_Recibido deja de ser el ETA (una estimación) y pasa a ser la fecha
    real de llegada a puerto, con el ETA solo como respaldo cuando no existe.
    Se controla con BASE_FECHA_RECIBIDO por si se prefiere la entrada a almacén.
 9. Histórico con tiempos de ciclo (mediana de puerto→almacén, solicitud→pago y
    salida→almacén). Es la pregunta que sigue después de "¿cuántos recibimos?".
10. Bloqueo optimista en la edición: si otra persona tocó el embarque después
    de que se cargó la pantalla, avisa en vez de pisar el cambio en silencio.
11. Celular: tipografía de 16px en los campos (evita el zoom automático de iOS
    al enfocar un input), áreas táctiles de 44px, KPIs en rejilla de dos
    columnas, respeto del área segura del notch y menos widgets por pantalla.
12. Panel "Salud de los datos" y "Bitácora" en Herramientas: BLs duplicados,
    ETA ilegibles, fechas de flujo inconsistentes y quién hizo qué.
13. Sesión con vida distinta por rol (el presidente entra desde el celular y no
    debería teclear el PIN cada cinco minutos) y atada al navegador que la
    abrió, para que reenviar el link no regale el acceso.
14. Rendimiento de pantalla: búsqueda sobre una columna precalculada en vez de
    un .apply por tecla, caché del Excel de descarga (antes se regeneraba en
    cada rerun), y caché del parser de fechas.

Se mantiene igual: encabezados tolerantes a acentos, una sola llamada a la API
por refresco (values_batch_get), fechas escritas siempre en ISO con RAW, orden
operativo (primero lo atrasado), auditoría en la pestaña "Log", escapado HTML de
todo lo que venga del Sheet, y un solo bloque de HTML para la lista que el CSS
convierte en tabla (desktop) o tarjetas (celular).
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import re
import time
import unicodedata
from functools import lru_cache
from secrets import token_urlsafe
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
import gspread.exceptions
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

# ---------------------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# ---------------------------------------------------------------------------
VERSION_APP = "3.0"

st.set_page_config(
    page_title="Antillana · Embarques en Tránsito",
    page_icon="🚢",
    layout="wide",
    initial_sidebar_state="collapsed",  # en celular la barra lateral tapaba la pantalla
)

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
CATEGORIAS_CON_OC_EE = ["Aéreos", "Carga Suelta"]

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
# El flujo de 5 etapas aplica a TODAS las categorías, sin importar el modo de
# llegada. Lo único que cambia es el rótulo/ícono de la primera etapa: "Llegada
# a puerto" (🚢) para carga marítima, "Llegada al aeropuerto" (✈️) para Aéreos.
CATEGORIA_AEREA = "Aéreos"


def es_aereo(categoria) -> bool:
    return str(categoria).strip() == CATEGORIA_AEREA


def lugar_de(categoria) -> str:
    """"aeropuerto" para carga aérea, "puerto" para todo lo demás. Solo cambia
    textos que ve el usuario: las claves internas (ETAPAS_PUERTO, EST_PUERTO,
    nombres de columnas del Sheet) siguen diciendo "puerto" para no romper los
    datos ya guardados ni los filtros."""
    return "aeropuerto" if es_aereo(categoria) else "puerto"


def etiqueta_etapa(etapa: str, categoria="") -> str:
    """Nombre corto de la etapa, con "aeropuerto" cuando el embarque es aéreo."""
    if etapa == "Llegada a puerto" and es_aereo(categoria):
        return "Llegada al aeropuerto"
    return ETIQUETA_CORTA_ETAPA.get(etapa, etapa)

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
ETIQUETA_CORTA_ETAPA = {
    "Llegada a puerto": "Llegada a puerto",
    "Recepción y declaración": "Recepción/declaración",
    "Solicitud de pago a finanzas": "Solicitud de pago",
    "Pago realizado": "Pago realizado",
    "Recibido en almacén": "Recibido en almacén",
}
ICONO_ETAPA = {
    "Llegada a puerto": "🚢",
    "Recepción y declaración": "📄",
    "Solicitud de pago a finanzas": "💰",
    "Pago realizado": "✅",
    "Recibido en almacén": "🏬",
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
UMBRAL_ATRASO_PUERTO_DEFECTO = 7
DIAS_LIBRES_DEFECTO = 0   # el costo corre desde la llegada a puerto, no desde el día 8
# RD$2,000 por día es la tarifa que definió Dauris. Es una tarifa PLANA de
# referencia, no la factura: no distingue naviera de terminal, no escalona, y no
# sabe cuántos contenedores trae un embarque. En 0 el bloque de dinero se apaga.
COSTO_DIA_DEFECTO = 2000.0
MONEDA_DEFECTO = "RD$"
TEXTO_ALERTA_ETAPA = {
    "Llegada a puerto": "en {lugar} sin declarar",
    "Recepción y declaración": "declarado y sin solicitar el pago",
    "Solicitud de pago a finanzas": "esperando que Finanzas pague",
    "Pago realizado": "pagado y sin retirar del {lugar}",
}

# Qué fecha manda para decidir a qué mes pertenece un embarque recibido:
#   "llegada" -> fecha real de llegada a puerto (respaldo: ETA)
#   "almacen" -> fecha de entrada a almacén
# Cambia esta sola línea si el criterio del negocio es el otro.
BASE_FECHA_RECIBIDO = "llegada"

VISTA_EN_PROCESO_PUERTO = "Puerto/Aeropuerto · Estatus"
RECIBIDO_SHEET = "Recibido (Mes)"
LOG_SHEET = "Log"

COLUMNAS_RECIBIDO = [
    COL_BL, COL_DESC, COL_MODELO, COL_CANT, COL_PAIS, COL_ETA,
    "Fecha_Recibido", "Categoria_Origen", "Registrado_Por", COL_ACTUALIZACION,
    COL_ACTUALIZADO_POR, COL_FECHA_SALIDA, *COLUMNAS_FLUJO, COL_OC, COL_EE,
]
COLUMNAS_LOG = ["Fecha_Hora", "Usuario", "Accion", "BL", "Categoria", "Detalle"]

UMBRAL_PROXIMO = 3          # días para considerar un embarque "Próximo a llegar"
CACHE_TTL = 45              # segundos de caché de lectura
LARGO_PIN = 4               # dígitos del PIN
# Vida de sesión por rol. El admin trabaja sentado en la app; el viewer (el
# presidente, gerentes) abre el link desde el celular una vez al día y volver a
# pedirle el PIN cada rato es la forma más rápida de que deje de usarla.
VIDA_SESION_MIN = {"admin": 120, "viewer": 720}
MAX_INTENTOS_SESION = 5
MAX_FALLOS_GLOBAL = 40      # freno global: el bloqueo por sesión se evade en incógnito
VENTANA_FALLOS = 10 * 60
BLOQUEO_SEGUNDOS = 15 * 60
SEMANAS_HORIZONTE = 8
TOPE_PROCESO = 8            # embarques con diagrama visible antes de paginar
REINTENTOS_API = 3

EST_TRANSITO = "En tránsito"
EST_PROXIMO = "Próximo a llegar"
EST_PUERTO = "En Puerto"
EST_RETRASADO = "Retrasado"
EST_SIN_FECHA = "Sin fecha válida"
VALOR_RETRASADO = "Retrasado"

STATUS_COLOR = {
    EST_TRANSITO: "#2E86DE",
    EST_PROXIMO: "#5C6BC0",
    EST_PUERTO: "#F0B90B",
    EST_RETRASADO: "#D7263D",
    "Recibido": "#2E7D32",
    EST_SIN_FECHA: "#6B7280",
}
COLOR_TOTAL = "#17A2B8"
COLOR_RECIBIDAS_MES = "#2E7D32"
COLOR_ALERTA = "#B45309"
STATUS_ORDER = [EST_RETRASADO, EST_PUERTO, EST_PROXIMO, EST_TRANSITO, EST_SIN_FECHA]
PRIORIDAD_ESTADO = {EST_RETRASADO: 0, EST_PUERTO: 1, EST_PROXIMO: 2, EST_TRANSITO: 3, EST_SIN_FECHA: 4}
PALETA_PAISES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}
MESES_ES_CORTO = {
    1: "ene", 2: "feb", 3: "mar", 4: "abr", 5: "may", 6: "jun",
    7: "jul", 8: "ago", 9: "sep", 10: "oct", 11: "nov", 12: "dic",
}

CUSTOM_CSS = """
<style>
:root {
    --ant-azul: #0C447C;
    --ant-borde: #E5E7EB;
    --ant-texto: #111827;
    --ant-suave: #6B7280;
    --ant-hecho: #2E7D32;
    --ant-actual: #F0B90B;
}
/* El menú de secciones es el primer elemento de la página: sin este aire, en
   algunas resoluciones queda medio escondido bajo la barra fija de Streamlit.
   El padding inferior respeta el área segura del iPhone (barra de gestos). */
.block-container {
    padding-top: 3.4rem;
    padding-bottom: calc(2.5rem + env(safe-area-inset-bottom, 0px));
}
html { -webkit-text-size-adjust: 100%; }

.nav-rotulo { font-size:0.70rem; text-transform:uppercase; letter-spacing:0.08em;
              color:#9CA3AF; font-weight:700; margin:0 0 4px 2px; }

/* ---------- Encabezado ---------- */
.ant-head { text-align:center; margin: 0 0 1.1rem 0; }
.ant-eyebrow {
    display:inline-flex; align-items:center; gap:6px;
    background:#E6F1FB; color:var(--ant-azul);
    font-size:0.74rem; font-weight:700; letter-spacing:0.08em; text-transform:uppercase;
    padding:5px 16px; border-radius:999px;
}
.ant-title {
    font-size:2.2rem; font-weight:800; margin:0.6rem 0 0.3rem 0; color:var(--ant-texto);
    letter-spacing:-0.02em;
}
.ant-rule { width:52px; height:4px; background:#2E86DE; border-radius:2px; margin:0.2rem auto 0.6rem auto; }
.ant-sub { font-size:0.95rem; color:var(--ant-suave); }
.ant-stamp { display:inline-flex; align-items:center; gap:7px; font-size:0.76rem;
             color:var(--ant-suave); margin-top:0.5rem; flex-wrap:wrap; justify-content:center; }
.ant-dot { width:8px; height:8px; border-radius:50%; background:#22C55E; display:inline-block; }
.ant-logo { max-height:58px; margin-bottom:0.5rem; }

/* Resumen ejecutivo: una línea que responde "¿cómo vamos hoy?" sin leer nada más */
.resumen { background:#F8FAFC; border:1px solid var(--ant-borde); border-left:4px solid var(--ant-azul);
           border-radius:10px; padding:10px 14px; font-size:0.92rem; color:#1F2937; margin-bottom:12px; }
.resumen b { color:#0C447C; }

/* Panel de confirmación de llegadas */
.conf-titulo { font-size:0.78rem; text-transform:uppercase; letter-spacing:0.06em;
               font-weight:800; color:#92400E; background:#FEF7E6;
               border-left:4px solid #F0B90B; padding:8px 14px; border-radius:8px;
               margin:6px 0 10px 0; }
.conf-fila { display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:6px 2px; }
.conf-bl { font-weight:700; color:#111827; font-size:0.92rem; }
.conf-desc { color:#6B7280; font-size:0.85rem; }

/* ---------- KPIs ---------- */
.kpi-card { border-radius:14px; padding:14px 18px; min-height:92px; height:100%;
            display:flex; flex-direction:column; justify-content:center; align-items:center;
            text-align:center; box-shadow:0 2px 8px rgba(17,24,39,0.12); }
.kpi-label { font-size:0.70rem; font-weight:700; text-transform:uppercase; letter-spacing:0.06em;
             color:rgba(255,255,255,0.92); margin-bottom:6px; }
.kpi-value { font-size:2.0rem; font-weight:800; line-height:1; color:#fff; }
.kpi-sub { font-size:0.70rem; color:rgba(255,255,255,0.88); margin-top:6px; }

/* ---------- Barras por país (HTML puro: no se mueven en celular) ---------- */
.paises { margin:4px 0 10px; }
.pfila { display:grid; grid-template-columns:minmax(72px,26%) 1fr 34px;
         align-items:center; gap:8px; padding:3px 0; }
.pnom { font-size:.8rem; color:#374151; overflow:hidden; text-overflow:ellipsis;
        white-space:nowrap; }
.pbarra { background:rgba(0,0,0,.06); border-radius:6px; height:14px; overflow:hidden; }
.pbarra span { display:block; height:100%; border-radius:6px; }
.pval { font-size:.8rem; font-weight:700; color:#374151; text-align:right; }
@media (max-width: 640px) {
  .pfila { grid-template-columns:minmax(64px,34%) 1fr 28px; gap:6px; }
  .pnom { font-size:.74rem; }
}

/* ---------- Atraso acumulado en puerto y su costo ---------- */
.atraso { display:flex; flex-wrap:wrap; gap:18px 28px; align-items:flex-start;
          background:#FFFBEB; box-shadow:inset 0 0 0 1px #FCD34D;
          border-radius:10px; padding:14px 16px; margin:6px 0 10px; }
.atraso.grave { background:#FEF2F2; box-shadow:inset 0 0 0 1px #FCA5A5; }
.atbloque { min-width:150px; }
.atnum { font-size:1.55rem; font-weight:700; line-height:1.15; color:#92400E; }
.atraso.grave .atnum { color:#991B1B; }
.atnum.atapagado { color:#9CA3AF; }
.atlbl { font-size:.78rem; color:#4B5563; margin-top:2px; }
.atdetalle { flex-basis:100%; border-top:1px solid rgba(0,0,0,.10); padding-top:10px; }
.atttl { font-size:.72rem; text-transform:uppercase; letter-spacing:.04em;
         color:#6B7280; margin-bottom:6px; }
.atfila { display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 10px;
          padding:5px 0; border-bottom:1px dotted rgba(0,0,0,.08); font-size:.82rem; }
.atfila:last-child { border-bottom:0; }
.atbl { font-weight:700; color:#111827; }
.atoc { font-size:.74rem; color:#4B5563; background:rgba(0,0,0,.05);
        border-radius:5px; padding:1px 6px; }
.atoc.atsin { color:#92400E; background:rgba(146,64,14,.10); }
.atdias { color:#4B5563; }
.atmonto { margin-left:auto; font-weight:700; color:#991B1B; white-space:nowrap;
           text-align:right; }
.attarifa { display:block; font-weight:400; font-size:.68rem; color:#6B7280; }
.atresto { color:#6B7280; font-style:italic; }
.atfila.atok .atmonto { color:#4B5563; }
@media (max-width: 640px) {
  .atraso { gap:12px 16px; padding:12px; }
  .atbloque { min-width:calc(50% - 8px); }
  .atnum { font-size:1.25rem; }
  .atmonto { margin-left:0; }
}

/* ---------- Chips de resumen por etapa (reemplazan 5 st.metric en fila) ---------- */
.chips { display:flex; flex-wrap:wrap; gap:8px; margin:4px 0 10px 0; }
.chip { display:inline-flex; align-items:center; gap:7px; background:#fff;
        border:1px solid var(--ant-borde); border-radius:999px; padding:6px 13px;
        font-size:0.82rem; color:#374151; }
.chip b { font-size:0.98rem; color:#111827; }
.chip.on { border-color:#F0B90B; background:#FFFBEB; }

/* ---------- Alertas de cuello de botella ---------- */
.alerta-caja { border:1px solid #FDE68A; background:#FFFBEB; border-radius:12px;
               padding:12px 16px; margin-bottom:12px; }
.alerta-titulo { font-size:0.78rem; text-transform:uppercase; letter-spacing:0.05em;
                 font-weight:800; color:#92400E; margin-bottom:8px; }
.alerta-fila { display:flex; gap:10px; align-items:baseline; padding:3px 0;
               font-size:0.87rem; color:#1F2937; flex-wrap:wrap; }
.alerta-fila .dias { font-weight:800; color:#B45309; min-width:64px; }
.alerta-fila .bl { font-weight:700; }
.alerta-fila .que { color:#6B7280; }

/* ---------- Diagrama de flujo (HTML puro, sin Plotly) ---------- */
.flujo { display:flex; align-items:flex-start; margin:8px 0 4px 0; }
.paso { flex:1 1 0; min-width:0; position:relative; display:flex; flex-direction:column;
        align-items:center; text-align:center; padding:0 2px; }
.paso::before { content:""; position:absolute; top:15px; left:-50%; width:100%; height:3px;
                background:var(--ant-borde); z-index:0; }
.paso:first-child::before { display:none; }
.paso.hecho::before, .paso.actual::before { background:var(--ant-hecho); }
.paso .pt { width:32px; height:32px; border-radius:50%; display:flex; align-items:center;
            justify-content:center; background:#E5E7EB; font-size:15px; z-index:1;
            border:2px solid #fff; box-shadow:0 0 0 1px #E5E7EB; }
.paso.hecho .pt { background:var(--ant-hecho); box-shadow:0 0 0 1px var(--ant-hecho); }
.paso.actual .pt { background:var(--ant-actual); box-shadow:0 0 0 3px #FEF3C7; }
.paso .et { font-size:0.68rem; color:#6B7280; margin-top:6px; line-height:1.2; }
.paso.actual .et { color:#111827; font-weight:700; }
.paso .fch { font-size:0.66rem; color:#9CA3AF; }
.flujo-cabeza { display:flex; justify-content:space-between; align-items:baseline;
                gap:10px; flex-wrap:wrap; }
.flujo-bl { font-weight:700; color:#111827; }
.flujo-desc { color:#6B7280; font-size:0.86rem; }
.contador { display:inline-block; font-size:0.76rem; color:#4B5563; background:#F3F4F6;
            border-radius:6px; padding:2px 8px; margin:2px 6px 2px 0; }
.contador.ojo { background:#FEF3C7; color:#92400E; font-weight:700; }
.contador.mal { background:#FEE2E2; color:#991B1B; font-weight:800;
                box-shadow:inset 0 0 0 1px #FCA5A5; }
.contador.cerrado.ojo { background:#FFFBEB; box-shadow:inset 0 0 0 1px #FCD34D; font-weight:600; }
.contador.cerrado.mal { background:#FFF; box-shadow:inset 0 0 0 1px #FCA5A5; font-weight:700; }
.contador.bien { background:#DCFCE7; color:#166534; font-weight:600; }

/* ---------- Lista de embarques: UN solo markup ----------
   Desktop: grid de 7 columnas (se ve como tabla).
   Celular (<=640px): cada fila se convierte en tarjeta y cada celda
   muestra su etiqueta vía data-l. Sin duplicar el DOM.            */
.lista { border:1px solid var(--ant-borde); border-radius:12px; overflow:hidden;
         box-shadow:0 1px 4px rgba(17,24,39,0.06); background:#fff; }
.fila-head, .fila {
    display:grid;
    grid-template-columns: 1.15fr 1.45fr 1.25fr 0.75fr 0.9fr 0.85fr 1.15fr;
    gap:10px; align-items:center;
}
.fila-head { padding:10px 18px; font-size:0.67rem; text-transform:uppercase; letter-spacing:0.05em;
             color:#9CA3AF; background:#F9FAFB; border-bottom:1px solid var(--ant-borde); }
.fila { padding:12px 18px; font-size:0.87rem; background:#fff;
        border-bottom:1px solid #F3F4F6; border-left:4px solid #6B7280; }
.fila:last-child { border-bottom:none; }
.c-bl { font-weight:700; color:var(--ant-texto); word-break:break-all; }
.c-suave { color:var(--ant-suave); }
/* OC y EE van debajo del BL y no como columnas propias: solo las usan Aéreos y
   Carga Suelta, y dos columnas vacías en las demás categorías estropean la
   tabla en pantalla ancha y la tarjeta en celular. */
.c-ref { font-weight:500; font-size:0.78rem; color:var(--ant-suave);
         margin-top:2px; letter-spacing:0.2px; }
.badge { display:inline-block; padding:3px 11px; border-radius:999px;
         font-size:0.73rem; font-weight:700; color:#fff; white-space:nowrap; }
.badge.linea { background:#fff !important; color:#4B5563; border:1px solid var(--ant-borde); }

/* Ficha completa de un embarque */
.ficha { border:1px solid var(--ant-borde); border-radius:12px; overflow:hidden; background:#fff; }
.ficha-fila { display:grid; grid-template-columns: 210px 1fr; gap:12px;
              padding:9px 16px; border-bottom:1px solid #F3F4F6; font-size:0.9rem; }
.ficha-fila:last-child { border-bottom:none; }
.ficha-k { color:#9CA3AF; font-size:0.72rem; text-transform:uppercase; letter-spacing:0.04em;
           font-weight:700; padding-top:2px; }
.ficha-v { color:#1F2937; font-weight:600; word-break:break-word; }
.vacio { padding:26px 18px; text-align:center; color:var(--ant-suave); font-size:0.9rem; background:#fff; }

/* ---------- Selector de sección / categoría ----------
   Streamlit pinta el segmento activo con su color primario; se fuerza aquí y no
   solo en config.toml para que el aspecto no dependa de ese archivo. */
div[data-testid="stButtonGroup"] button {
    border-radius:8px !important; border:1px solid var(--ant-borde) !important;
    color:#4B5563 !important; font-weight:600 !important;
}
div[data-testid="stButtonGroup"] button:hover { background:#F3F7FC !important; color:#0C447C !important; }
div[data-testid="stButtonGroup"] button[aria-checked="true"],
div[data-testid="stButtonGroup"] button[data-testid="stBaseButton-segmented_controlActive"],
div[data-testid="stButtonGroup"] button[kind="segmented_controlActive"],
div[data-testid="stButtonGroup"] button[aria-selected="true"] {
    background:#DCEBFA !important; color:#0C447C !important;
    border:1px solid #2E86DE !important; box-shadow:none !important;
}
div[data-testid="stButtonGroup"] button[aria-checked="true"] p,
div[data-testid="stButtonGroup"] button[data-testid="stBaseButton-segmented_controlActive"] p,
div[data-testid="stButtonGroup"] button[kind="segmented_controlActive"] p { color:#0C447C !important; }

/* Botones primarios (Entrar, Guardar, Confirmar): azul del tablero */
button[kind="primary"], button[data-testid="stBaseButton-primary"],
button[data-testid="stBaseButton-primaryFormSubmit"] {
    background:#2E86DE !important; border-color:#2E86DE !important; color:#ffffff !important;
}
button[kind="primary"]:hover, button[data-testid="stBaseButton-primary"]:hover,
button[data-testid="stBaseButton-primaryFormSubmit"]:hover {
    background:#256FB8 !important; border-color:#256FB8 !important; color:#fff !important;
}

/* Impresión: una hoja limpia para llevar a reunión. */
@media print {
    [data-testid="stSidebar"], [data-testid="stToolbar"], [data-testid="stHeader"],
    .stButton, .stDownloadButton, [data-testid="stExpander"], .solo-pantalla,
    [data-testid="stTextInput"], [data-testid="stSelectbox"], [data-testid="stAlert"] {
        display:none !important;
    }
    .block-container { padding:0 !important; max-width:100% !important; }
    .lista { border:1px solid #999; box-shadow:none; }
    .fila { break-inside:avoid; }
    .flujo { break-inside:avoid; }
    .badge, .kpi-card, .paso .pt { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
}

/* ==========================================================================
   CELULAR (iOS y Android). Tres cosas que rompían la experiencia:
   1) Safari hace zoom automático al enfocar un input de menos de 16px.
   2) Los botones de Streamlit quedan por debajo de los 44px que Apple pide
      como área táctil mínima; con dedo grande se falla el clic.
   3) Cinco columnas de KPI en una pantalla de 380px son ilegibles.
   ========================================================================== */
@media (max-width: 820px) {
    input, textarea, select,
    .stTextInput input, .stNumberInput input, .stDateInput input,
    div[data-baseweb="input"] input, div[data-baseweb="select"] input {
        font-size:16px !important;
    }
}
@media (max-width: 720px) {
    .block-container { padding-left:0.8rem !important; padding-right:0.8rem !important; }
    .stButton button, .stDownloadButton button,
    div[data-testid="stButtonGroup"] button { min-height:44px !important; }
    div[data-testid="stButtonGroup"] { flex-wrap:wrap !important; gap:6px !important; }

    /* KPIs: rejilla de dos columnas en vez de cinco tiras aplastadas */
    div[class*="st-key-kpirow_"] div[data-testid="stHorizontalBlock"] {
        display:flex !important; flex-direction:row !important; flex-wrap:wrap !important; gap:8px !important;
    }
    div[class*="st-key-kpirow_"] div[data-testid="stColumn"],
    div[class*="st-key-kpirow_"] div[data-testid="column"] {
        flex:1 1 calc(50% - 8px) !important; min-width:calc(50% - 8px) !important;
        width:calc(50% - 8px) !important;
    }
}
@media (max-width: 640px) {
    .fila-head { display:none; }
    .lista { border:none; box-shadow:none; background:transparent; }
    .fila { display:block; border:1px solid var(--ant-borde); border-left-width:5px;
            border-radius:12px; margin-bottom:10px; padding:13px 16px;
            box-shadow:0 1px 4px rgba(17,24,39,0.06); }
    .fila > div { padding:2px 0; }
    .fila > div[data-l]:not(.c-bl):not(.c-badge)::before {
        content: attr(data-l) ": "; font-size:0.68rem; text-transform:uppercase;
        letter-spacing:0.04em; color:#9CA3AF; font-weight:700;
    }
    .c-bl { font-size:1.0rem; margin-bottom:2px; }
    .ant-title { font-size:1.55rem; }
    .kpi-value { font-size:1.55rem; }
    .ficha-fila { grid-template-columns:1fr; gap:2px; }

    /* El diagrama de 5 etapas se vuelve vertical: en horizontal, en 380px de
       ancho, las etiquetas se solapan y no se lee ninguna. */
    .flujo { flex-direction:column; }
    .paso { flex-direction:row; align-items:center; text-align:left; gap:10px; padding:4px 0; }
    .paso::before { top:-10px; left:15px; width:3px; height:22px; }
    .paso .et { margin-top:0; font-size:0.84rem; }
    .paso .fch { font-size:0.74rem; }
}
</style>
"""


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


def clave_fila(*partes) -> str:
    """Clave única y estable de widget para una fila. Incluye SIEMPRE el número
    de fila del Sheet: los BLs se repiten en los datos reales (embarques
    parciales del mismo BL) y sin esto dos filas generan la misma clave, lo que
    en Streamlit tumba la página con StreamlitDuplicateElementKey."""
    return "_".join(_slug_css(p) for p in partes if str(p).strip())


def _etiquetas_desambiguadas(df: pd.DataFrame, con_categoria: bool = True, largo_desc: int = 38) -> list:
    """Etiquetas 'BL · descripción [· categoría]' para selectbox. Cuando una
    etiqueta se repite se le agrega el número de fila del Sheet, que sí es único:
    sin esto, elegir la segunda opción de la lista operaba sobre la primera."""
    base = []
    for _, r in df.iterrows():
        desc = str(r[COL_DESC])[:largo_desc] or "sin descripción"
        etq = f"{r[COL_BL]} · {desc} · {r['Categoria']}" if con_categoria else f"{r[COL_BL]} · {desc}"
        base.append(etq)
    conteo = pd.Series(base).value_counts() if base else pd.Series(dtype=int)
    return [
        f"{etq} · fila {r.get('FilaSheet', '?')}" if conteo.get(etq, 0) > 1 else etq
        for etq, (_, r) in zip(base, df.iterrows())
    ]


def esc(valor) -> str:
    """Escapa cualquier valor que venga del Sheet antes de meterlo en HTML."""
    texto = "" if valor is None else str(valor).strip()
    return html.escape(texto) if texto else "—"


def texto_dias(n) -> str:
    d = int(n)
    return f"{d} día" if d == 1 else f"{d} días"


@st.cache_resource
def costos_puerto() -> dict:
    """Parámetros del costo de atraso en puerto. Se ajustan desde Secrets:

        [costo_puerto]
        umbral = 7
        moneda = "US$"
        dias_libres = 5
        costo_dia = 0

        [costo_puerto.costo_dia_por_categoria]
        Equipos = 150
        "Carga Suelta" = 60

        [costo_puerto.dias_libres_por_categoria]
        Aéreos = 2

    Mientras costo_dia sea 0 en todo, la app cuenta los atrasados pero no
    muestra ninguna cifra de dinero."""
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
        "costo_dia": _num(cfg.get("costo_dia"), COSTO_DIA_DEFECTO),
        "costo_por_cat": dict(cfg.get("costo_dia_por_categoria", {}) or {}),
        "libres_por_cat": dict(cfg.get("dias_libres_por_categoria", {}) or {}),
    }


_RX_MILES_PUNTO = re.compile(r"^\s*[^\d\-]*-?\d{1,3}(\.\d{3})+\s*$")


def tarifa_a_numero(valor):
    """Lee la tarifa escrita a mano en el Sheet.

    No usa a_numero() directo por un problema real: en RD "4.500" son cuatro mil
    quinientos, pero a_numero lo lee como 4.50 porque trata el punto como
    decimal. En un campo de dinero eso es un error de mil veces, y salió en la
    prueba de este módulo. Aquí, un punto seguido de exactamente tres dígitos y
    sin comas se trata como separador de miles."""
    texto = str(valor or "").strip()
    if not texto:
        return None
    if "," not in texto and _RX_MILES_PUNTO.match(texto):
        texto = texto.replace(".", "")
    return a_numero(texto)


def costo_dia_fila(fila, cfg=None) -> float:
    """Tarifa diaria de UNA fila. Prioridad: lo que diga el Sheet en
    Costo_Por_Dia, luego la tarifa por categoría de Secrets, luego la global."""
    cfg = cfg or costos_puerto()
    propio = tarifa_a_numero(fila.get(COL_COSTO_DIA))
    if propio is not None and propio > 0:
        return float(propio)
    return float(cfg["costo_por_cat"].get(fila.get("Categoria", ""), cfg["costo_dia"]))


def costo_demora_fila(fila, cfg=None):
    """Lo que lleva causado ESE embarque, contado DESDE LA LLEGADA A PUERTO.

    El contador no arranca en el día 8: el costo se causa desde que la carga
    toca puerto, y el umbral de 7 días solo define a partir de cuándo lo
    consideramos atrasado. Son dos cosas distintas y el dinero sigue al primero.

    Devuelve None si no aplica: no ha llegado, ya se recibió en almacén, o no
    hay tarifa."""
    cfg = cfg or costos_puerto()
    dias = fila.get("DiasEnPuerto")
    if not es_numero(dias) or _lleno(fila.get("F_Almacen")):
        return None
    tarifa = costo_dia_fila(fila, cfg)
    if tarifa <= 0:
        return None
    libres = float(cfg["libres_por_cat"].get(fila.get("Categoria", ""), cfg["dias_libres"]))
    cobrables = max(0.0, float(dias) - libres)
    return cobrables * tarifa if cobrables else None


def _lleno(v) -> bool:
    """Una celda de fecha vacía llega como None, como NaN o como cadena vacía
    según de dónde venga la columna. NaN es truthy, así que `if fila["F_Pago"]`
    da True en una fila SIN pago y el conteo sale en cero sin avisar. Pasó en la
    prueba de este mismo módulo."""
    if v is None:
        return False
    try:
        if pd.isna(v):
            return False
    except (TypeError, ValueError):
        pass
    return str(v).strip() != ""


def resumen_atraso_puerto(df) -> dict:
    """Lo que está en puerto ahora mismo y lo que lleva costado.

    El costo cuenta desde la llegada a puerto, no desde que se pasa del plazo.
    El umbral de 7 días solo separa lo atrasado de lo que va en tiempo; no mueve
    el contador de dinero."""
    cfg = costos_puerto()
    vacio = {"n_puerto": 0, "n_atrasados": 0, "n_pendiente_pago": 0,
             "dias_excedidos": 0, "costo_total": 0.0, "costo_atrasados": 0.0,
             "costo_promedio": 0.0, "umbral": cfg["umbral"], "moneda": cfg["moneda"],
             "hay_tarifa": False, "detalle": []}
    if df is None or df.empty or "DiasEnPuerto" not in df.columns:
        return vacio

    n_puerto = n_atr = n_pago = dias_exc = 0
    costo_total = costo_atr = 0.0
    detalle = []
    for _, fila in df.iterrows():
        dias = fila.get("DiasEnPuerto")
        # Congelado = ya se recibió: ese atraso es histórico, no pendiente.
        if not es_numero(dias) or _lleno(fila.get("F_Almacen")):
            continue
        cat = fila.get("Categoria", "")
        libres = float(cfg["libres_por_cat"].get(cat, cfg["dias_libres"]))
        tarifa = costo_dia_fila(fila, cfg)
        n_puerto += 1
        if _lleno(fila.get("F_Solicitud")) and not _lleno(fila.get("F_Pago")):
            n_pago += 1
        cobrables = max(0.0, float(dias) - libres)
        costo_fila = cobrables * tarifa
        costo_total += costo_fila
        atrasado = dias > cfg["umbral"]
        if atrasado:
            n_atr += 1
            dias_exc += int(dias) - cfg["umbral"]
            costo_atr += costo_fila
        if costo_fila or atrasado:
            detalle.append({
                "bl": str(fila.get(COL_BL, "") or ""),
                "oc": str(fila.get(COL_OC, "") or "").strip(),
                "cat": cat,
                "dias": int(dias),
                "exceso": max(0, int(dias) - cfg["umbral"]),
                "atrasado": atrasado,
                "costo": costo_fila,
                "tarifa": tarifa,
            })

    detalle.sort(key=lambda d: d["costo"], reverse=True)
    return {
        "n_puerto": n_puerto, "n_atrasados": n_atr, "n_pendiente_pago": n_pago,
        "dias_excedidos": dias_exc, "costo_total": costo_total,
        "costo_atrasados": costo_atr,
        "costo_promedio": (costo_total / n_puerto) if (n_puerto and costo_total) else 0.0,
        "umbral": cfg["umbral"], "moneda": cfg["moneda"],
        "hay_tarifa": costo_total > 0, "detalle": detalle,
    }


def _monto(valor: float, moneda: str) -> str:
    return f"{moneda}{valor:,.0f}"


def _atbloque(numero: str, etiqueta: str, apagado: bool = False) -> str:
    return (f'<div class="atbloque"><div class="atnum{" atapagado" if apagado else ""}">'
            f'{numero}</div><div class="atlbl">{etiqueta}</div></div>')


def html_atraso_puerto(df) -> str:
    """Tablero de lo que está parado en puerto y lo que cuesta. Si no hay nada en
    puerto no dibuja nada: un cero permanente se vuelve invisible en una semana."""
    r = resumen_atraso_puerto(df)
    if not r["n_puerto"]:
        return ""
    grave = r["n_atrasados"] > 0
    piezas = [f'<div class="atraso{" grave" if grave else ""}">']
    piezas.append(_atbloque(str(r["n_pendiente_pago"]),
                            "pendientes de que Finanzas pague"))
    piezas.append(_atbloque(str(r["n_atrasados"]),
                            f'atrasados (+{r["umbral"]} días en puerto)'))
    if r["hay_tarifa"]:
        piezas.append(_atbloque(_monto(r["costo_total"], r["moneda"]),
                                "acumulado desde la llegada a puerto"))
        piezas.append(_atbloque(
            _monto(r["costo_atrasados"], r["moneda"]),
            f'de eso, en los atrasados · {_monto(r["costo_promedio"], r["moneda"])} '
            f'promedio por embarque en puerto'))
    else:
        piezas.append(_atbloque("—", "costo apagado: tarifa en 0 en Secrets", True))

    if r["detalle"]:
        filas = []
        for d in r["detalle"][:10]:
            ref = esc(d["bl"]) or "&mdash;"
            oc = f' <span class="atoc">OC {esc(d["oc"])}</span>' if d["oc"] else \
                 ' <span class="atoc atsin">sin OC</span>'
            monto = (f'<span class="atmonto">{_monto(d["costo"], r["moneda"])}'
                     f'<span class="attarifa">{_monto(d.get("tarifa", 0), r["moneda"])}/día</span>'
                     f'</span>' if r["hay_tarifa"] else "")
            exceso = (f' · <b>+{d["exceso"]} sobre el plazo</b>' if d["atrasado"] else "")
            filas.append(
                f'<div class="atfila{"" if d["atrasado"] else " atok"}">'
                f'<span class="atbl">{ref}</span>{oc}'
                f'<span class="atdias">{d["dias"]} días en puerto{exceso}</span>'
                f'{monto}</div>'
            )
        resto = len(r["detalle"]) - 10
        if resto > 0:
            filas.append(f'<div class="atfila atresto">y {resto} más</div>')
        piezas.append('<div class="atdetalle"><div class="atttl">Costo acumulado '
                      'por embarque</div>' + "".join(filas) + "</div>")
    piezas.append("</div>")
    return "".join(piezas)


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


def formato_corto(f) -> str:
    return f"{f.day:02d} {MESES_ES_CORTO[f.month]}" if f else ""


def analizar_eta(valor) -> dict:
    """Diagnóstico de una fecha para la herramienta de normalización.
    'iso' = ya está en AAAA-MM-DD · 'ambigua' = número puro donde día y mes son
    ambos <= 12 · 'convertible' = se entiende pero no está en ISO · 'ilegible'."""
    crudo = "" if valor is None else str(valor).strip()
    if not crudo:
        return {"tipo": "vacia", "crudo": crudo, "dm": None, "md": None}

    try:
        datetime.strptime(crudo, "%Y-%m-%d")
        return {"tipo": "iso", "crudo": crudo, "dm": parsear_fecha(crudo), "md": None}
    except ValueError:
        pass

    tokens = _tokenizar_fecha(crudo)
    lectura = _interpretar_tokens(tokens, dia_primero=True)
    if lectura is not None and lectura[3]:
        dm = _fecha_de_tokens(tokens, dia_primero=True)
        md = _fecha_de_tokens(tokens, dia_primero=False)
        if dm and md and dm != md:
            return {"tipo": "ambigua", "crudo": crudo, "dm": dm, "md": md}
        if dm or md:
            return {"tipo": "convertible", "crudo": crudo, "dm": dm or md, "md": None}

    f = parsear_fecha(crudo)
    if f:
        return {"tipo": "convertible", "crudo": crudo, "dm": f, "md": None}
    return {"tipo": "ilegible", "crudo": crudo, "dm": None, "md": None}


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
    _headers.clear()
    return headers + faltan


def _localizar_fila(ws, bl: str, fila_sugerida=None):
    """Ubica la fila de un BL dentro de una pestaña. Devuelve (fila, error).

    Este es el corazón del arreglo de esta versión. Antes se usaba ws.find(BL),
    que devuelve la PRIMERA coincidencia: con dos embarques parciales del mismo
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


def rerun_fragmento():
    """st.rerun(scope="fragment") solo es válido cuando el rerun lo disparó un
    widget que vive dentro del fragmento; si no, Streamlit lanza una excepción.
    Esta envoltura cae al rerun normal en ese caso. (RerunException hereda de
    BaseException, así que el except Exception no se traga el rerun bueno.)"""
    try:
        st.rerun(scope="fragment")
    except Exception:
        st.rerun()


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


# ---------------------------------------------------------------------------
# LÓGICA DE ESTADO
# ---------------------------------------------------------------------------
def estado_embarque(eta_valor, hoy: date = None):
    """Devuelve (estado, dias_relativos): atraso en días si está En Puerto, días
    que faltan si está Próximo a llegar, None si no aplica. 'hoy' se recibe por
    parámetro para no consultar el reloj una vez por fila."""
    eta = parsear_fecha(eta_valor)
    if eta is None:
        return EST_SIN_FECHA, None
    dias = (eta - (hoy or hoy_rd())).days
    if dias < 0:
        return EST_PUERTO, abs(dias)
    if dias <= UMBRAL_PROXIMO:
        return EST_PROXIMO, dias
    return EST_TRANSITO, None


def texto_estado(estado: str, dias, categoria="") -> str:
    if estado == EST_RETRASADO and dias is not None:
        return f"Retrasado {texto_dias(dias)}"
    if estado == EST_PUERTO and dias is not None:
        donde = "Aeropuerto" if es_aereo(categoria) else "Puerto"
        return f"En {donde} hace {texto_dias(dias)}"
    if estado == EST_PROXIMO and dias is not None:
        d = int(dias)
        if d == 0:
            return "Llega hoy"
        if d == 1:
            return "Llega mañana"
        return f"Llega en {d} días"
    return estado


def _etapa_de_fechas(fechas) -> str:
    """fechas = 5 valores (date o None) en el orden de ETAPAS_PUERTO. Devuelve la
    última etapa con fecha, o "" si ninguna la tiene. Es la ÚNICA fuente de
    verdad de en qué etapa está un embarque: no hay columna de texto que pueda
    desincronizarse de las fechas reales."""
    etapa = ""
    for nombre, fecha in zip(ETAPAS_PUERTO, fechas):
        if fecha:
            etapa = nombre
    return etapa


def _columna_fechas(df: pd.DataFrame, columna: str) -> list:
    if columna not in df.columns:
        return [None] * len(df)
    return [parsear_fecha(v) for v in df[columna]]


def enriquecer(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega estado, fechas parseadas, contadores, alertas de cuello de botella
    y clave de orden operativo. Se llama UNA vez por refresco sobre la tabla
    completa; las vistas por categoría son rebanadas de este resultado."""
    df = df.copy()
    calculadas = ["EstadoTexto", "DiasRel", "ETAFecha", "Prioridad", "OrdenSec", "ValorNum",
                  "DiasTransito", "DiasSolicitudPago", "DiasPagoDespacho", "DiasEnPuerto",
                  "DiasEnEtapa", "EtapaActual", "EtapaIdx", "Alerta", "AlertaDias", "Buscar",
                  "F_Salida", "F_Puerto", "F_Declaracion", "F_Solicitud", "F_Pago", "F_Almacen"]
    if df.empty:
        for c in calculadas:
            df[c] = []
        return df

    hoy = hoy_rd()
    sla = sla_etapas()

    etas = [parsear_fecha(v) for v in df[COL_ETA]]
    calculado = [estado_embarque(f, hoy) for f in etas]
    df["ETAFecha"] = etas
    estados = [c[0] for c in calculado]
    if COL_ESTATUS_LLEGADA in df.columns:
        estados = [
            EST_RETRASADO if (e == EST_PUERTO and _norm(f) == _norm(VALOR_RETRASADO)) else e
            for e, f in zip(estados, df[COL_ESTATUS_LLEGADA])
        ]
    df["EstadoTexto"] = estados
    df["DiasRel"] = [c[1] for c in calculado]
    df["Prioridad"] = df["EstadoTexto"].map(PRIORIDAD_ESTADO).fillna(9).astype(int)

    salidas = _columna_fechas(df, COL_FECHA_SALIDA)
    puertos = _columna_fechas(df, COL_FECHA_LLEGADA_PUERTO)
    declaraciones = _columna_fechas(df, COL_FECHA_DECLARACION)
    solicitudes = _columna_fechas(df, COL_FECHA_SOLICITUD_PAGO)
    pagos = _columna_fechas(df, COL_FECHA_PAGO)
    almacenes = _columna_fechas(df, COL_FECHA_ALMACEN)
    df["F_Salida"], df["F_Puerto"], df["F_Declaracion"] = salidas, puertos, declaraciones
    df["F_Solicitud"], df["F_Pago"], df["F_Almacen"] = solicitudes, pagos, almacenes

    fechas_por_fila = list(zip(puertos, declaraciones, solicitudes, pagos, almacenes))
    df["EtapaActual"] = [_etapa_de_fechas(f) for f in fechas_por_fila]
    df["EtapaIdx"] = [INDICE_ETAPA.get(e, -1) for e in df["EtapaActual"]]

    # Dos avisos que antes solo salían en Herramientas y que cuestan tiempo real:
    # editar el embarque equivocado porque dos filas comparten BL, y confiar en
    # contadores calculados sobre fechas que van al revés.
    bls_norm = df[COL_BL].astype(str).str.strip().str.upper()
    veces = bls_norm.value_counts().to_dict()
    df["BLRepetido"] = [bool(b) and veces.get(b, 0) > 1 for b in bls_norm]
    df["FlujoRaro"] = [
        _validar_orden_flujo(dict(zip(ETAPAS_PUERTO, t))) for t in fechas_por_fila
    ]

    # Contador 1: salida -> llegada a puerto. Congelado en cuanto llegó, para que
    # el número diga "cuánto tardó" y no "cuánto lleva sin llegar" una vez llegó.
    df["DiasTransito"] = [
        (ll - sal).days if (sal and ll) else ((hoy - sal).days if sal else None)
        for sal, ll in zip(salidas, puertos)
    ]
    # Contador 2: solicitud de pago -> pago realizado. Se congela al pagar.
    df["DiasSolicitudPago"] = [
        (pg - sol).days if (sol and pg) else ((hoy - sol).days if sol else None)
        for sol, pg in zip(solicitudes, pagos)
    ]
    # Contador 3: pago realizado -> retiro del almacén. Se congela al recibirse,
    # igual que los dos anteriores; mientras no se retire, corre contra hoy.
    # Antes solo corría contra hoy, así que al archivar seguía creciendo y por eso
    # la pantalla lo escondía: el dato de cuánto se tardó en retirar tras pagar
    # —el que se le reclama a almacén— nunca llegaba a existir.
    df["DiasPagoDespacho"] = [
        (al - pg).days if (pg and al) else ((hoy - pg).days if pg else None)
        for pg, al in zip(pagos, almacenes)
    ]
    # Días en puerto: desde la llegada física hasta el retiro, sin importar la
    # etapa. Es lo que la columna "Dias en puerto" del Sheet nunca llegó a tener.
    # Congelado al recibirse: ahí deja de ser "cuánto lleva" y pasa a ser el
    # ciclo total de despacho de ese embarque.
    df["DiasEnPuerto"] = [
        (al - p).days if (p and al) else ((hoy - p).days if p else None)
        for p, al in zip(puertos, almacenes)
    ]

    dias_etapa, alertas, alerta_dias = [], [], []
    cats = df["Categoria"] if "Categoria" in df.columns else [""] * len(df)
    for etapa, fechas, estado, dias_rel, cat in zip(df["EtapaActual"], fechas_por_fila,
                                                    df["EstadoTexto"], df["DiasRel"], cats):
        d_etapa = None
        if etapa:
            fecha_etapa = fechas[INDICE_ETAPA[etapa]]
            d_etapa = (hoy - fecha_etapa).days if fecha_etapa else None
        dias_etapa.append(d_etapa)

        texto, dias_alerta = "", None
        limite = sla.get(etapa)
        if etapa and etapa != ETAPAS_PUERTO[-1] and d_etapa is not None and limite is not None \
                and d_etapa > limite:
            base = TEXTO_ALERTA_ETAPA.get(etapa, "detenido").format(lugar=lugar_de(cat))
            texto = f"{base[0].upper()}{base[1:]} hace {texto_dias(d_etapa)}"
            dias_alerta = d_etapa
        elif not etapa and estado == EST_RETRASADO and dias_rel is not None \
                and dias_rel > sla["__retraso__"]:
            texto = f"Retrasado {texto_dias(dias_rel)} y sin ETA nuevo"
            dias_alerta = int(dias_rel)
        alertas.append(texto)
        alerta_dias.append(dias_alerta)
    df["DiasEnEtapa"] = dias_etapa
    df["Alerta"] = alertas
    df["AlertaDias"] = alerta_dias

    # Dentro de "En Puerto", primero el más atrasado; en el resto, el ETA más cercano.
    df["OrdenSec"] = [
        -(dias or 0) if estado == EST_PUERTO else (fecha.toordinal() if fecha else 10**9)
        for estado, dias, fecha in zip(df["EstadoTexto"], df["DiasRel"], df["ETAFecha"])
    ]
    col_valor = columna_de_valor(df)
    df["ValorNum"] = [a_numero(v) for v in df[col_valor]] if col_valor else [None] * len(df)

    # Columna de búsqueda precalculada: antes cada tecla disparaba un .apply que
    # normalizaba tres campos por fila; ahora es un contains sobre texto ya listo.
    oc = df[COL_OC] if COL_OC in df.columns else [""] * len(df)
    ee = df[COL_EE] if COL_EE in df.columns else [""] * len(df)
    df["Buscar"] = [
        _norm(f"{bl} {desc} {modelo} {pais} {o} {e}")
        for bl, desc, modelo, pais, o, e in zip(df[COL_BL], df[COL_DESC], df[COL_MODELO],
                                                df[COL_PAIS], oc, ee)
    ]
    return df.sort_values(["Prioridad", "OrdenSec"], kind="stable").reset_index(drop=True)


def ordenar_vista(df: pd.DataFrame, criterio: str) -> pd.DataFrame:
    """Ordena la lista según lo que el usuario elija. El orden por defecto es
    operativo (lo atrasado primero), no alfabético."""
    if df.empty:
        return df
    if criterio == "Urgencia":
        return df.sort_values(["Prioridad", "OrdenSec"], kind="stable")
    if criterio == "Más días detenido":
        return df.sort_values("AlertaDias", ascending=False, na_position="last", kind="stable")
    if criterio == "ETA más próximo":
        return df.sort_values("OrdenSec", key=lambda s: s.where(s > 0, 10**9), kind="stable")
    if criterio == "ETA más lejano":
        return df.sort_values("OrdenSec", ascending=False, kind="stable")
    if criterio == "BL":
        return df.sort_values(COL_BL, kind="stable")
    if criterio == "País":
        return df.sort_values([COL_PAIS, "Prioridad"], kind="stable")
    if criterio == "Descripción":
        return df.sort_values(COL_DESC, kind="stable")
    if criterio == "Valor" and "ValorNum" in df.columns:
        return df.sort_values("ValorNum", ascending=False, na_position="last", kind="stable")
    return df


def fechas_flujo_de_fila(fila) -> dict:
    """{etapa: date|None} de una fila ya enriquecida."""
    return {
        "Llegada a puerto": fila.get("F_Puerto"),
        "Recepción y declaración": fila.get("F_Declaracion"),
        "Solicitud de pago a finanzas": fila.get("F_Solicitud"),
        "Pago realizado": fila.get("F_Pago"),
        "Recibido en almacén": fila.get("F_Almacen"),
    }


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
# COMPONENTES DE UI
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _logo_base64() -> str:
    """Si existe assets/logo.png (o .jpg) en el repo, se muestra en el encabezado.
    Si no, la app sigue con el ícono vectorial: no se inventa un logo."""
    for nombre in ("assets/logo.png", "assets/logo.jpg", "assets/logo.jpeg", "logo.png"):
        ruta = Path(nombre)
        if ruta.exists():
            tipo = "jpeg" if ruta.suffix.lower() in (".jpg", ".jpeg") else "png"
            return f"data:image/{tipo};base64," + base64.b64encode(ruta.read_bytes()).decode()
    return ""


def encabezado(datos: dict):
    anio = hoy_rd().year
    ultima = datos.get("ultima_carga")
    persona = str(datos.get("ultima_persona", "") or "").strip()
    if ultima:
        sello = (f"Información actualizada: {ultima.day:02d} {MESES_ES_CORTO[ultima.month]} "
                 f"{ultima.year}, {ultima.strftime('%I:%M %p').lstrip('0').lower()} (hora RD)")
        if persona:
            sello += f" · por {persona}"
    else:
        sello = "Sin registro de la última carga de información"
    logo = _logo_base64()
    img = f'<img class="ant-logo" src="{logo}" alt="Antillana Comercial">' if logo else ""
    st.markdown(
        f'<div class="ant-head">{img}'
        f'<span class="ant-eyebrow">'
        f'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0C447C" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/>'
        f'<path d="M9 21v-6h6v6"/></svg> Logística e Importaciones {anio}</span>'
        f'<div class="ant-title">Estatus de Cargas</div>'
        f'<div class="ant-rule"></div>'
        f'<div class="ant-sub">Antillana Comercial</div>'
        f'<div class="ant-stamp"><span class="ant-dot"></span> {esc(sello)}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def tarjeta_kpi(label: str, valor, color: str, sub: str = "") -> str:
    extra = f'<div class="kpi-sub">{esc(sub)}</div>' if sub else ""
    return (
        f'<div class="kpi-card" style="background:{color};">'
        f'<div class="kpi-label">{esc(label)}</div>'
        f'<div class="kpi-value">{valor}</div>{extra}</div>'
    )


def html_flujo(fechas: dict, etapa_actual: str, es_aerea: bool = False) -> str:
    """Diagrama de las 5 etapas en HTML/CSS puro.

    Sustituye al diagrama Plotly: con 20 embarques en puerto se creaban 20
    figuras interactivas por pantalla, que en celular son varios segundos de
    render y mucha batería. Además, en pantalla angosta el CSS lo convierte en
    lista vertical, que sí se lee; el Plotly horizontal se solapaba."""
    idx = INDICE_ETAPA.get(etapa_actual, -1)
    partes = ['<div class="flujo">']
    for i, etapa in enumerate(ETAPAS_PUERTO):
        clase = "hecho" if i < idx else ("actual" if i == idx else "")
        etiqueta = "Llegada al aeropuerto" if (es_aerea and i == 0) else etapa
        icono = "✈️" if (es_aerea and i == 0) else ICONO_ETAPA[etapa]
        fecha = fechas.get(etapa)
        partes.append(
            f'<div class="paso {clase}"><div class="pt">{icono}</div>'
            f'<div class="txt"><div class="et">{esc(etiqueta)}</div>'
            f'<div class="fch">{esc(formato_corto(fecha)) if fecha else "&nbsp;"}</div></div></div>'
        )
    partes.append("</div>")
    return "".join(partes)


def html_chips(conteos: dict, resaltar: str = "") -> str:
    """Resumen por etapa como chips que se acomodan solos. Reemplaza a cinco
    st.metric en fila, que en un celular de 380px quedaban ilegibles."""
    piezas = ['<div class="chips">']
    for etapa in ETAPAS_PUERTO:
        clase = "chip on" if etapa == resaltar else "chip"
        piezas.append(
            f'<span class="{clase}">{ICONO_ETAPA[etapa]} '
            f'{esc(ETIQUETA_CORTA_ETAPA.get(etapa, etapa))} <b>{int(conteos.get(etapa, 0))}</b></span>'
        )
    piezas.append("</div>")
    return "".join(piezas)


def marca(clase: str) -> str:
    if "mal" in clase:
        return "⚠ "
    if "ojo" in clase:
        return "● "
    # El ✓ acompaña al verde por la misma razón que el ⚠ acompaña al rojo: esto
    # se ve en celular y se imprime, y el color solo no alcanza.
    return "✓ " if "bien" in clase else ""


def _clase_contador(dias, limite, cerrado: bool = False) -> str:
    """Gris dentro del plazo, ámbar apenas lo pasa, rojo cuando ya se fue de las
    manos. Dos niveles y no uno para que el rojo signifique algo: si todo lo
    vencido sale rojo, en dos semanas nadie lo mira.

    `cerrado` es para la etapa que ya ocurrió (el pago ya se hizo): conserva el
    color pero con borde en vez de relleno. Que el atraso se vuelva gris al
    marcar "pagado" borraría de la pantalla la única prueba de que Finanzas
    tardó 16 días, que es justo el dato con el que se reclama.

    Una etapa cerrada DENTRO del plazo sale en verde: ahí ya hay un veredicto
    ("esto salió bien") y el gris no lo dice. Mientras la etapa sigue abierta se
    queda en gris, porque todavía no hay nada que juzgar. Sin plazo definido —el
    tránsito— tampoco hay veredicto posible, así que se queda en gris."""
    if not es_numero(dias) or not limite:
        return "contador"
    if dias <= limite:
        return "contador bien" if cerrado else "contador"
    nivel = "mal" if dias > limite * 2 else "ojo"
    return f"contador {nivel} cerrado" if cerrado else f"contador {nivel}"


def _chip(clase: str, etiqueta: str, dias) -> str:
    """Todos los contadores con el mismo formato 'Etiqueta: N días'. Antes cada
    uno tenía su redacción ('16 días tardó en pagarse') y en fila se leían como
    frases sueltas en vez de como una ficha de indicadores."""
    return f'<span class="{clase}">{marca(clase)}{etiqueta}: {texto_dias(dias)}</span>' 


def html_contadores(fila) -> str:
    """Los contadores operativos de un embarque, en línea."""
    piezas = []
    sla = sla_etapas()
    retirado = bool(fila.get("F_Almacen"))
    transito = fila.get("DiasTransito")
    if es_numero(transito):
        etiqueta = "Duración del tránsito" if fila.get("F_Puerto") else "En tránsito"
        piezas.append(_chip("contador", etiqueta, transito))
    dias_puerto = fila.get("DiasEnPuerto")
    if es_numero(dias_puerto):
        # El tiempo total en puerto abarca las cuatro etapas, así que se compara
        # contra la suma de sus plazos, no contra el de una sola. Ya retirado no
        # se esconde: se congela y queda en modo cerrado, que es la prueba de
        # cuánto costó ese despacho.
        lugar = lugar_de(fila.get("Categoria", ""))
        etiqueta = f"Duración en {lugar}" if retirado else f"En {lugar}"
        clase = _clase_contador(dias_puerto, sum(v for k, v in sla.items()
                                                 if k in SLA_ETAPA_DEFECTO), cerrado=retirado)
        piezas.append(_chip(clase, etiqueta, dias_puerto))
    solicitud = fila.get("DiasSolicitudPago")
    if es_numero(solicitud):
        pagado = bool(fila.get("F_Pago"))
        etiqueta = "Duración del pago" if pagado else "Esperando pago"
        clase = _clase_contador(solicitud, sla["Solicitud de pago a finanzas"], cerrado=pagado)
        piezas.append(_chip(clase, etiqueta, solicitud))
    espera = fila.get("DiasPagoDespacho")
    if es_numero(espera):
        etiqueta = "Del pago al retiro" if retirado else "Pagado sin retirar"
        clase = _clase_contador(espera, sla["Pago realizado"], cerrado=retirado)
        piezas.append(_chip(clase, etiqueta, espera))
    # Lo que lleva causado ESTE embarque desde que llegó a puerto. En rojo solo
    # cuando ya se pasó del plazo: antes de eso el gasto es normal, no una alarma.
    cfg_costo = costos_puerto()
    gasto = costo_demora_fila(fila, cfg_costo)
    if gasto:
        dias_p = fila.get("DiasEnPuerto")
        tarde = es_numero(dias_p) and dias_p > cfg_costo["umbral"]
        clase = "contador mal" if tarde else "contador"
        piezas.append(
            f'<span class="{clase}">{marca(clase)}Costo en puerto: '
            f'{_monto(gasto, cfg_costo["moneda"])}</span>'
        )
    return "".join(piezas)


def _ref_oc_ee(fila) -> str:
    """OC y EE son los números con los que el equipo realmente rastrea la carga
    suelta y la aérea; no verlos en la lista obligaba a abrir cada embarque."""
    piezas = []
    for columna in (COL_OC, COL_EE):
        valor = str(fila.get(columna, "") or "").strip()
        # "nan" aparece cuando se concatenan categorías cuyas pestañas no tienen
        # la columna; mostrarlo sería peor que no mostrar nada.
        if valor and valor.upper() not in ("N/A", "NA", "NAN", "NONE", "-", "—"):
            piezas.append(f"{columna} {esc(valor)}")
    return f'<div class="c-ref">{" · ".join(piezas)}</div>' if piezas else ""


def render_lista(df: pd.DataFrame):
    """Un solo bloque HTML: tabla en desktop, tarjetas en celular (lo decide el CSS)."""
    if df.empty:
        st.markdown('<div class="lista"><div class="vacio">No hay embarques que coincidan con el filtro.</div></div>',
                    unsafe_allow_html=True)
        return

    partes = [
        '<div class="lista"><div class="fila-head">'
        "<div>BL</div><div>Descripción</div><div>Modelo/Serie</div><div>Cant.</div>"
        "<div>País</div><div>ETA</div><div>Estado</div></div>"
    ]
    for _, r in df.iterrows():
        color = STATUS_COLOR.get(r["EstadoTexto"], "#6B7280")
        etiqueta = texto_estado(r["EstadoTexto"], r["DiasRel"], r.get("Categoria", ""))
        etapa = str(r.get("EtapaActual", "")).strip()
        badge_etapa = ""
        if etapa:
            icono = "✈️" if (etapa == ETAPAS_PUERTO[0] and es_aereo(r.get("Categoria", ""))) \
                else ICONO_ETAPA[etapa]
            badge_etapa = (f'<span class="badge linea">{icono} '
                           f'{esc(etiqueta_etapa(etapa, r.get("Categoria", "")))}</span>')
        alerta = str(r.get("Alerta", "") or "").strip()
        badge_alerta = f'<span class="badge" style="background:{COLOR_ALERTA};">⚠ {esc(alerta)}</span>' if alerta else ""
        if r.get("BLRepetido"):
            badge_alerta += ('<span class="badge" style="background:#7C3AED;" '
                             'title="Este BL aparece en más de una fila">⧉ BL repetido</span>')
        partes.append(
            f'<div class="fila" style="border-left-color:{color};">'
            f'<div class="c-bl" data-l="BL">{esc(r[COL_BL]) if str(r[COL_BL]).strip() else "(sin BL)"}'
            f'{_ref_oc_ee(r)}</div>'
            f'<div class="c-suave" data-l="Descripción">{esc(r[COL_DESC])}</div>'
            f'<div class="c-suave" data-l="Modelo/Serie">{esc(r[COL_MODELO])}</div>'
            f'<div data-l="Cantidad">{esc(r[COL_CANT])}</div>'
            f'<div data-l="País">{esc(r[COL_PAIS])}</div>'
            f'<div data-l="ETA">{esc(formato_eta(r[COL_ETA]))}</div>'
            f'<div class="c-badge" data-l="Estado">'
            f'<span class="badge" style="background:{color};">{esc(etiqueta)}</span> '
            f'{badge_etapa} {badge_alerta}</div>'
            f"</div>"
        )
    partes.append("</div>")
    st.markdown("".join(partes), unsafe_allow_html=True)


def grafico_linea_tiempo(df: pd.DataFrame, key: str):
    """Qué viene encima, semana por semana. Responde la pregunta que un gerente
    hace de verdad ('¿qué me llega en las próximas semanas?')."""
    hoy = hoy_rd()
    inicio_semana = hoy - timedelta(days=hoy.weekday())
    etiquetas, valores, colores = [], [], []

    retrasados = int((df["EstadoTexto"] == EST_RETRASADO).sum())
    if retrasados:
        etiquetas.append("Retrasados")
        valores.append(retrasados)
        colores.append(STATUS_COLOR[EST_RETRASADO])

    vencidos = int((df["EstadoTexto"] == EST_PUERTO).sum())
    if vencidos:
        etiquetas.append("Por confirmar")
        valores.append(vencidos)
        colores.append(STATUS_COLOR[EST_PUERTO])

    con_fecha = df[df["ETAFecha"].notna() & ~df["EstadoTexto"].isin([EST_PUERTO, EST_RETRASADO])]
    for i in range(SEMANAS_HORIZONTE):
        desde = inicio_semana + timedelta(weeks=i)
        hasta = desde + timedelta(days=6)
        n = int(sum(1 for f in con_fecha["ETAFecha"] if desde <= f <= hasta))
        if i == 0:
            etiqueta = "Esta semana"
        elif i == 1:
            etiqueta = "Próxima semana"
        else:
            etiqueta = f"{desde.day} {MESES_ES_CORTO[desde.month]}"
        etiquetas.append(etiqueta)
        valores.append(n)
        colores.append(STATUS_COLOR[EST_PROXIMO] if i == 0 else STATUS_COLOR[EST_TRANSITO])

    lejanos = int(sum(1 for f in con_fecha["ETAFecha"]
                      if f > inicio_semana + timedelta(weeks=SEMANAS_HORIZONTE) - timedelta(days=1)))
    if lejanos:
        etiquetas.append(f"+{SEMANAS_HORIZONTE} sem")
        valores.append(lejanos)
        colores.append("#9CA3AF")

    fig = go.Figure(
        data=[go.Bar(x=etiquetas, y=valores, marker=dict(color=colores),
                     text=[v if v else "" for v in valores], textposition="outside",
                     hovertemplate="%{x}: %{y} embarque(s)<extra></extra>")]
    )
    fig.update_layout(
        margin=dict(t=18, b=10, l=10, r=10), height=260,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#374151", size=11), showlegend=False,
        xaxis=dict(showgrid=False, title=""),
        yaxis=dict(showgrid=True, gridcolor="#F3F4F6", showticklabels=False, title=""),
        bargap=0.35, dragmode=False,
    )
    st.plotly_chart(fig, width="stretch",
                    config={"displayModeBar": False, "staticPlot": True, "responsive": True},
                    key=f"tl_{key}")


def grafico_paises(df: pd.DataFrame, key: str):
    """Barras por país en HTML/CSS, no en Plotly.

    Plotly recalcula el ancho del eje según el largo de las etiquetas y lo vuelve
    a hacer en cada redibujado: en celular eso es lo que hacía que las barras se
    movieran solas al girar el teléfono o al abrir el teclado. Estas barras miden
    en porcentaje del ancho disponible, así que no dependen del texto ni de
    JavaScript, y se imprimen bien.

    Se pierde el clic-para-filtrar que tenía la versión de Plotly; el filtro por
    país sigue disponible en la barra de filtros de abajo."""
    if COL_PAIS not in df.columns or df.empty:
        return
    serie = df[COL_PAIS].replace("", NO_ESPECIFICADO).value_counts()
    if serie.empty:
        return
    tope = int(serie.max()) or 1
    piezas = ['<div class="paises"><div class="atttl">Por país de origen</div>']
    for i, (pais, n) in enumerate(serie.items()):
        ancho = max(4.0, (int(n) / tope) * 100.0)
        color = PALETA_PAISES[i % len(PALETA_PAISES)]
        piezas.append(
            f'<div class="pfila"><div class="pnom">{esc(str(pais))}</div>'
            f'<div class="pbarra"><span style="width:{ancho:.1f}%;background:{color}"></span></div>'
            f'<div class="pval">{int(n)}</div></div>'
        )
    piezas.append("</div>")
    st.markdown("".join(piezas), unsafe_allow_html=True)


def _ficha_embarque(fila):
    """Todos los campos del embarque, incluidas las columnas que alguien haya
    agregado en el Sheet y que la app no gestiona."""
    es_aerea = fila["Categoria"] == CATEGORIA_AEREA
    campos = [
        ("BL", fila[COL_BL]),
        ("Descripción", fila[COL_DESC]),
        ("Modelo / Serie", fila[COL_MODELO]),
        ("Cantidad", fila[COL_CANT]),
        ("País de origen", fila[COL_PAIS]),
        ("Categoría", fila["Categoria"]),
        ("ETA", formato_eta(fila[COL_ETA])),
        ("Estado", texto_estado(fila["EstadoTexto"], fila["DiasRel"], fila["Categoria"])),
    ]
    if fila["Categoria"] in CATEGORIAS_CON_OC_EE:
        if str(fila.get(COL_OC, "")).strip():
            campos.append(("OC", fila[COL_OC]))
        if str(fila.get(COL_EE, "")).strip():
            campos.append(("EE", fila[COL_EE]))
    if fila.get("F_Salida"):
        campos.append(("Fecha de salida", formato_eta(fila["F_Salida"])))
    if es_numero(fila.get("DiasTransito")):
        etiqueta = "Duración del tránsito" if fila.get("F_Puerto") else "En tránsito"
        campos.append((etiqueta, texto_dias(fila["DiasTransito"])))

    etapa = str(fila.get("EtapaActual", "")).strip()
    if etapa:
        etiqueta_etapa = "Llegada al aeropuerto" if (es_aerea and etapa == ETAPAS_PUERTO[0]) else etapa
        campos.append(("Etapa actual", etiqueta_etapa))
        fechas = fechas_flujo_de_fila(fila)
        rotulos = {
            "Llegada a puerto": "Llegada al aeropuerto" if es_aerea else "Llegada a puerto",
            "Recepción y declaración": "Recepción y declaración",
            "Solicitud de pago a finanzas": "Solicitud de pago enviada",
            "Pago realizado": "Pago realizado",
            "Recibido en almacén": "Recibido en almacén",
        }
        for nombre_etapa, fecha in fechas.items():
            if fecha:
                campos.append((rotulos[nombre_etapa], formato_eta(fecha)))
        retirado = bool(fila.get("F_Almacen"))
        if es_numero(fila.get("DiasEnPuerto")):
            lugar = lugar_de(fila.get("Categoria", ""))
            campos.append((f"Duración en {lugar}" if retirado else f"En {lugar}",
                           texto_dias(fila["DiasEnPuerto"])))
        if es_numero(fila.get("DiasSolicitudPago")):
            etiqueta = "Duración del pago" if fila.get("F_Pago") else "Esperando pago"
            campos.append((etiqueta, texto_dias(fila["DiasSolicitudPago"])))
        if es_numero(fila.get("DiasPagoDespacho")):
            campos.append(("Del pago al retiro" if retirado else "Pagado sin retirar",
                           texto_dias(fila["DiasPagoDespacho"])))
        _cfg_costo = costos_puerto()
        _gasto = costo_demora_fila(fila, _cfg_costo)
        if _gasto:
            campos.append(("Tarifa en puerto",
                           f'{_monto(costo_dia_fila(fila, _cfg_costo), _cfg_costo["moneda"])} por día'))
            campos.append(("Costo acumulado en puerto", _monto(_gasto, _cfg_costo["moneda"])))
    if str(fila.get("Alerta", "") or "").strip():
        campos.append(("⚠ Atención", fila["Alerta"]))

    for extra in columnas_extra(fila.to_frame().T):
        campos.append((extra.replace("_", " "), fila[extra]))
    if str(fila.get(COL_ACTUALIZACION, "")).strip():
        autor = str(fila.get(COL_ACTUALIZADO_POR, "")).strip()
        campos.append(("Última actualización",
                       f"{fila[COL_ACTUALIZACION]}" + (f" · {autor}" if autor else "")))
    campos.append(("Fila en el Sheet", fila.get("FilaSheet", "—")))

    filas_html = "".join(
        f'<div class="ficha-fila"><div class="ficha-k">{esc(k)}</div>'
        f'<div class="ficha-v">{esc(v)}</div></div>'
        for k, v in campos
    )
    st.markdown(f'<div class="ficha">{filas_html}</div>', unsafe_allow_html=True)
    if etapa:
        st.markdown(html_flujo(fechas_flujo_de_fila(fila), etapa, es_aerea=es_aerea),
                    unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
def selector_horizontal(label: str, opciones: list, key: str, default=None, formato=None,
                        ancho: str = "stretch"):
    """Segmented control cuando la versión de Streamlit lo trae; radio horizontal
    si no. Sustituye a st.tabs para las categorías: con tabs, Streamlit ejecuta el
    cuerpo de TODAS las pestañas en cada rerun aunque el usuario vea una sola."""
    formato = formato or (lambda x: str(x))
    extra = {} if key in st.session_state else {"default": default or opciones[0]}
    if hasattr(st, "segmented_control"):
        elegido = st.segmented_control(
            label, opciones, key=key, format_func=formato,
            label_visibility="collapsed", width=ancho, **extra,
        )
    else:
        elegido = st.radio(label, opciones, key=key, horizontal=True,
                           format_func=formato, label_visibility="collapsed")
    return elegido or (default or opciones[0])


def _en_proceso(df: pd.DataFrame) -> pd.DataFrame:
    """Todo lo que ya confirmó llegada a puerto/aeropuerto y sigue activo."""
    if df.empty or "EtapaActual" not in df.columns:
        return df.iloc[0:0]
    return df[df["EtapaActual"].astype(str).str.strip() != ""]


def contar_recibidas_mes(historico: pd.DataFrame) -> int:
    if historico.empty:
        return 0
    hoy = hoy_rd()
    total = 0
    for valor in historico.get("Fecha_Recibido", []):
        f = parsear_fecha(valor)
        if f and f.year == hoy.year and f.month == hoy.month:
            total += 1
    return total


def _resumen_ejecutivo(df: pd.DataFrame, recibidas_mes: int) -> str:
    """Una línea que contesta '¿cómo vamos hoy?' sin obligar a leer el tablero
    entero. Es lo primero que ve el presidente al abrir desde el celular."""
    hoy = hoy_rd()
    fin_semana = hoy + timedelta(days=(6 - hoy.weekday()))
    esta_semana = int(sum(1 for f in df["ETAFecha"] if f and hoy <= f <= fin_semana))
    por_confirmar = int(((df["EstadoTexto"] == EST_PUERTO) &
                         (df["EtapaActual"].astype(str).str.strip() == "")).sum())
    retrasados = int((df["EstadoTexto"] == EST_RETRASADO).sum())
    en_puerto = len(_en_proceso(df))
    trabados = int((df["Alerta"].astype(str).str.strip() != "").sum())

    piezas = [f"<b>{len(df)}</b> embarques activos"]
    if esta_semana:
        piezas.append(f"<b>{esta_semana}</b> con llegada esta semana")
    if en_puerto:
        piezas.append(f"<b>{en_puerto}</b> en proceso en puerto")
    if por_confirmar:
        piezas.append(f"<b>{por_confirmar}</b> por confirmar llegada")
    if retrasados:
        piezas.append(f"<b>{retrasados}</b> retrasados")
    if trabados:
        piezas.append(f"<b>{trabados}</b> pasados de tiempo en su etapa")
    if recibidas_mes:
        piezas.append(f"<b>{recibidas_mes}</b> recibidos en {MESES_ES[hoy.month].lower()}")
    return f'<div class="resumen">{" · ".join(piezas)}.</div>'


def _panel_alertas(df: pd.DataFrame, tope: int = 6):
    """Dónde se está trabando. No es lo mismo saber que hay 14 embarques en
    puerto que saber que 4 llevan más de una semana esperando que Finanzas
    pague: lo primero es un dato, lo segundo es una decisión."""
    con_alerta = df[df["Alerta"].astype(str).str.strip() != ""]
    if con_alerta.empty:
        return
    con_alerta = con_alerta.sort_values("AlertaDias", ascending=False, na_position="last")

    resumen = {}
    for etapa, texto in zip(con_alerta["EtapaActual"], con_alerta["Alerta"]):
        clave = ETIQUETA_CORTA_ETAPA.get(etapa, "Retrasado en tránsito") if etapa else "Retrasado en tránsito"
        resumen[clave] = resumen.get(clave, 0) + 1
    detalle = " · ".join(f"{n} en {nombre.lower()}" for nombre, n in resumen.items())

    filas = []
    for _, r in con_alerta.head(tope).iterrows():
        filas.append(
            f'<div class="alerta-fila"><span class="dias">{int(r["AlertaDias"])} d</span>'
            f'<span class="bl">{esc(r[COL_BL]) or "(sin BL)"}</span>'
            f'<span class="que">{esc(r[COL_DESC])} · {esc(r["Categoria"])} — {esc(r["Alerta"])}</span></div>'
        )
    extra = (f'<div class="alerta-fila"><span class="que">…y {len(con_alerta) - tope} más. '
             f'Ordena la lista por "Más días detenido" para verlos todos.</span></div>'
             if len(con_alerta) > tope else "")
    st.markdown(
        f'<div class="alerta-caja"><div class="alerta-titulo">⚠ Dónde se está trabando · '
        f'{len(con_alerta)} embarque(s) — {esc(detalle)}</div>{"".join(filas)}{extra}</div>',
        unsafe_allow_html=True,
    )


def _archivar(fila, clave: str, etiqueta: str = "Marcar como recibido"):
    """Botón de archivo + rescate cuando faltan etapas.

    El candado que impide archivar sin haber pasado por el flujo se mantiene
    (si no, el histórico se llena de embarques sin trazabilidad, que fue lo que
    pasó con el BL 142-161-355-5). Lo que cambia es que ya no es un callejón sin
    salida: si faltan etapas, la app las pide aquí mismo con la fecha REAL de
    cada una, en vez de rellenarlas solas con la fecha de hoy — que era rápido,
    pero metía datos falsos en los contadores de desempeño."""
    bl = str(fila[COL_BL]).strip()
    categoria = fila["Categoria"]
    n_fila = fila.get("FilaSheet")
    pendiente_key = f"faltan_{clave}"

    if st.button(etiqueta, key=f"rec_{clave}", type="primary", width="stretch"):
        ok, mensaje = marcar_como_recibido(bl, categoria, fila_sugerida=n_fila)
        if ok:
            registrar_log("Recibido", bl, categoria, f"fila {n_fila}")
            invalidar_caches()
            st.rerun()
        elif str(mensaje).startswith("FALTAN_ETAPAS::"):
            st.session_state[pendiente_key] = mensaje.split("::", 1)[1].split("|")
            rerun_fragmento()
        else:
            st.error(mensaje)

    faltan = st.session_state.get(pendiente_key)
    if not faltan:
        return

    with st.form(f"form_faltan_{clave}"):
        st.warning(
            "Este embarque no pasó por todas las etapas. Registra la fecha real en que ocurrió "
            "cada una y se archiva completo, con su trazabilidad."
        )
        fechas = {}
        for etapa in faltan:
            fechas[etapa] = st.date_input(f"{ICONO_ETAPA.get(etapa, '•')} {etapa}", value=hoy_rd(),
                                          format="DD/MM/YYYY", key=f"falta_{clave}_{_slug_css(etapa)}")
        fecha_almacen = st.date_input(f"{ICONO_ETAPA[ETAPAS_PUERTO[-1]]} {ETAPAS_PUERTO[-1]}",
                                      value=hoy_rd(), format="DD/MM/YYYY", key=f"almacen_{clave}")
        c1, c2 = st.columns(2)
        confirmar = c1.form_submit_button("Guardar y archivar", type="primary", width="stretch")
        cancelar = c2.form_submit_button("Cancelar", width="stretch")

    if cancelar:
        st.session_state.pop(pendiente_key, None)
        rerun_fragmento()
    if confirmar:
        ok, mensaje = marcar_como_recibido(bl, categoria, fila_sugerida=n_fila,
                                           fechas_faltantes=fechas, fecha_almacen=fecha_almacen)
        if ok:
            st.session_state.pop(pendiente_key, None)
            registrar_log("Recibido (etapas completadas)", bl, categoria,
                          "; ".join(f"{e}={f.isoformat()}" for e, f in fechas.items()))
            invalidar_caches()
            st.rerun()
        else:
            st.error(mensaje)


def _panel_en_proceso(df: pd.DataFrame, rol: str, contexto: str):
    """Un diagrama por embarque, siempre visible. El admin avanza con un solo
    botón (la etapa siguiente, que es el 95% de los casos) y corrige fechas o
    retrocede desde el desplegable, que es lo raro.

    'contexto' identifica desde dónde se llama: el mismo embarque puede
    aparecer en más de una vista y sin esto las claves de sus widgets chocan."""
    es_admin = rol == "admin"
    orden = df.sort_values(["AlertaDias", "EtapaIdx", COL_ETA],
                           ascending=[False, True, True], na_position="last")

    clave_ver = f"ver_todos_{_slug_css(contexto)}"
    ver_todos = st.session_state.get(clave_ver, False)
    visibles = orden if ver_todos else orden.head(TOPE_PROCESO)

    fecha_evento = hoy_rd()
    if es_admin:
        fecha_evento = st.date_input(
            "Fecha del evento que vas a confirmar abajo", value=hoy_rd(), format="DD/MM/YYYY",
            key=f"fecha_evento_{_slug_css(contexto)}",
            help="Se usa para la etapa que confirmes con los botones de abajo. Cámbiala si estás "
                 "registrando algo que pasó otro día; así los contadores no mienten.",
        )

    for _, fila in visibles.iterrows():
        bl = str(fila[COL_BL]).strip()
        categoria = fila["Categoria"]
        etapa = str(fila.get("EtapaActual", "")).strip()
        es_aerea = categoria == CATEGORIA_AEREA
        clave = clave_fila(contexto, categoria, bl or "sin_bl", fila.get("FilaSheet", ""))
        alerta = str(fila.get("Alerta", "") or "").strip()
        # Con dos filas del mismo BL es fácil trabajar sobre la equivocada y creer
        # que la app "no guardó". El número de fila del Sheet lo desambigua.
        marca_dup = (f'<span class="badge" style="background:#7C3AED;">⧉ BL repetido · '
                     f'fila {esc(fila.get("FilaSheet", "?"))}</span>'
                     if fila.get("BLRepetido") else "")
        raro = str(fila.get("FlujoRaro", "") or "").strip()

        st.markdown(
            f'<div class="flujo-cabeza"><span class="flujo-bl">{esc(bl) if bl else "(sin BL)"} '
            f'{marca_dup}</span>'
            f'<span class="flujo-desc">{esc(fila[COL_DESC])} · {esc(categoria)}</span></div>'
            + html_flujo(fechas_flujo_de_fila(fila), etapa, es_aerea=es_aerea)
            + html_contadores(fila)
            + (f'<div class="alerta-fila" style="color:#B45309;font-weight:600;">⚠ {esc(alerta)}</div>'
               if alerta else "")
            + (f'<div class="alerta-fila" style="color:#991B1B;font-weight:600;">⚠ Fechas fuera de '
               f'orden: {esc(raro)} Los contadores de este embarque no son confiables.</div>'
               if raro else ""),
            unsafe_allow_html=True,
        )

        if es_admin and bl:
            idx = INDICE_ETAPA.get(etapa, -1)
            siguiente = ETAPAS_PUERTO[idx + 1] if 0 <= idx < len(ETAPAS_PUERTO) - 1 else None
            if idx == len(ETAPAS_PUERTO) - 1:
                # Alguien llenó la fecha de almacén a mano en el Sheet: la fila
                # sigue activa y hay que poder archivarla igual.
                _archivar(fila, clave, etiqueta="Archivar en el histórico")
            elif siguiente == ETAPAS_PUERTO[-1]:
                _archivar(fila, clave, etiqueta=f"{ICONO_ETAPA[siguiente]} {siguiente} (archiva)")
            elif siguiente:
                if st.button(f"{ICONO_ETAPA[siguiente]} Confirmar: {siguiente}",
                             key=f"av_{clave}", type="primary", width="stretch"):
                    ok, mensaje = avanzar_estado_puerto(bl, categoria, siguiente,
                                                        fila_sugerida=fila.get("FilaSheet"),
                                                        fecha=fecha_evento)
                    if ok:
                        registrar_log("Avance de etapa", bl, categoria,
                                      f"{siguiente} el {fecha_evento.isoformat()}")
                        invalidar_caches()
                        st.rerun()
                    else:
                        st.error(mensaje)

            with st.expander("Corregir fecha o retroceder etapa"):
                c1, c2 = st.columns([1.4, 1])
                nueva_etapa = c1.selectbox("Etapa", ETAPAS_PUERTO[:-1],
                                           index=max(idx, 0), key=f"et_{clave}")
                fecha_corregida = c2.date_input("Fecha real", value=hoy_rd(), format="DD/MM/YYYY",
                                                key=f"fc_{clave}")
                st.caption("Elegir una etapa anterior borra las fechas de las etapas posteriores: "
                           "así es como se corrige un avance hecho por error.")
                if st.button("Guardar corrección", key=f"corr_{clave}", width="stretch"):
                    ok, mensaje = avanzar_estado_puerto(bl, categoria, nueva_etapa,
                                                        fila_sugerida=fila.get("FilaSheet"),
                                                        fecha=fecha_corregida, sobrescribir=True)
                    if ok:
                        accion = "Retroceso de etapa" if INDICE_ETAPA[nueva_etapa] < idx else "Corrección de etapa"
                        registrar_log(accion, bl, categoria,
                                      f"{nueva_etapa} el {fecha_corregida.isoformat()}")
                        invalidar_caches()
                        st.rerun()
                    else:
                        st.error(mensaje)
        st.divider()

    if len(orden) > TOPE_PROCESO:
        etiqueta = (f"Mostrar los {len(orden) - TOPE_PROCESO} restantes" if not ver_todos
                    else f"Mostrar solo los {TOPE_PROCESO} más urgentes")
        if st.button(etiqueta, key=f"btn_{clave_ver}", width="stretch"):
            st.session_state[clave_ver] = not ver_todos
            rerun_fragmento()


def _panel_confirmacion(df: pd.DataFrame, tab_key: str):
    """El ETA vencido no dice si la mercancía llegó, solo que la fecha pasó.
    Este panel hace la pregunta directa —¿llegó, sí o no?— y con la respuesta el
    embarque entra al flujo de 5 etapas (Sí) o se marca como retrasado (No).
    Va a la vista, sin desplegable, porque es lo único de la pantalla que exige
    acción hoy."""
    tiene_etapa = df["EtapaActual"].astype(str).str.strip().ne("")
    pendientes = df[(df["EstadoTexto"] == EST_PUERTO) & ~tiene_etapa]
    retrasados = df[(df["EstadoTexto"] == EST_RETRASADO) & ~tiene_etapa]
    if pendientes.empty and retrasados.empty:
        return

    TOPE = 12
    if not pendientes.empty:
        st.markdown(
            f'<div class="conf-titulo">¿Llegó a Puerto/Aeropuerto? · {len(pendientes)} '
            f'embarque(s) con la fecha vencida</div>',
            unsafe_allow_html=True,
        )
        fecha_llegada = st.date_input(
            "Fecha de llegada a registrar", value=hoy_rd(), format="DD/MM/YYYY",
            key=f"fecha_llegada_{_slug_css(tab_key)}",
            help="Se aplica al embarque que confirmes abajo. Si llegó el lunes y lo estás "
                 "registrando el jueves, cámbiala: de aquí sale el contador de tránsito.",
        )
    else:
        fecha_llegada = hoy_rd()

    def _fila_confirmacion(r, ya_retrasado: bool):
        bl = str(r[COL_BL]).strip()
        categoria = r["Categoria"]
        etiqueta = texto_estado(r["EstadoTexto"], r["DiasRel"], r.get("Categoria", ""))
        color = STATUS_COLOR.get(r["EstadoTexto"], "#6B7280")
        c1, c2, c3 = st.columns([3.2, 1.3, 1.5])
        c1.markdown(
            f'<div class="conf-fila"><span class="conf-bl">{esc(bl or "(sin BL)")}</span>'
            f'<span class="conf-desc">{esc(r[COL_DESC])} · {esc(categoria)}</span>'
            f'<span class="badge" style="background:{color};">{esc(etiqueta)}</span></div>',
            unsafe_allow_html=True,
        )
        if not bl:
            c2.caption("Sin BL: no se puede gestionar")
            return
        clave = clave_fila(tab_key, categoria, bl, r.get("FilaSheet", ""))
        es_aerea = categoria == CATEGORIA_AEREA
        texto_si = "Sí, llegó al aeropuerto" if es_aerea else "Sí, llegó a puerto"
        if c2.button(texto_si, key=f"si_llego_{clave}", type="primary", width="stretch"):
            ok, mensaje = avanzar_estado_puerto(bl, categoria, ETAPAS_PUERTO[0],
                                                fila_sugerida=r.get("FilaSheet"), fecha=fecha_llegada)
            if ok:
                registrar_log("Llegada confirmada", bl, categoria, fecha_llegada.isoformat())
                invalidar_caches()
                st.rerun()
            else:
                st.error(mensaje)
        if ya_retrasado:
            c3.button("Sigue retrasado", key=f"sigue_{clave}", width="stretch", disabled=True)
            c3.caption("Actualiza el ETA en Editar")
        elif c3.button("No, está retrasado", key=f"no_llego_{clave}", width="stretch"):
            ok, mensaje = marcar_estatus_llegada(bl, categoria, VALOR_RETRASADO,
                                                 fila_sugerida=r.get("FilaSheet"))
            if ok:
                registrar_log("Marcado como retrasado", bl, categoria, f"ETA {r[COL_ETA]}")
                invalidar_caches()
                st.rerun()
            else:
                st.error(mensaje)

    for _, r in pendientes.head(TOPE).iterrows():
        _fila_confirmacion(r, False)
    if len(pendientes) > TOPE:
        st.caption(f"…y {len(pendientes) - TOPE} más. Filtra por estado 'En Puerto' para verlos todos.")

    if not retrasados.empty:
        st.markdown(
            f'<div class="conf-titulo" style="margin-top:14px;">Ya verificados como retrasados · {len(retrasados)}</div>',
            unsafe_allow_html=True,
        )
        for _, r in retrasados.head(TOPE).iterrows():
            _fila_confirmacion(r, True)
        if len(retrasados) > TOPE:
            st.caption(f"…y {len(retrasados) - TOPE} más.")
    st.write("")


def tabla_exportable(df: pd.DataFrame) -> pd.DataFrame:
    """La vista tal como se está viendo, lista para Excel: sin columnas internas,
    con el estado ya redactado, las fechas legibles y los contadores del flujo —
    que es lo que hace falta para analizar demoras fuera de la app."""
    if df.empty:
        return pd.DataFrame(columns=[COL_BL, "Descripción", "Estado"])
    salida = pd.DataFrame({
        "BL": df[COL_BL],
        "OC": df[COL_OC] if COL_OC in df.columns else "",
        "EE": df[COL_EE] if COL_EE in df.columns else "",
        "Descripción": df[COL_DESC],
        "Modelo/Serie": df[COL_MODELO],
        "Cantidad": df[COL_CANT],
        "País de origen": df[COL_PAIS],
        "ETA": [formato_eta(v) for v in df[COL_ETA]],
        "Estado": [texto_estado(e, d, c) for e, d, c in
                   zip(df["EstadoTexto"], df["DiasRel"], df["Categoria"])],
        "Etapa": df["EtapaActual"],
        "Categoría": df["Categoria"],
        "Salida": [formato_eta(f) if f else "" for f in df["F_Salida"]],
        "Llegada a puerto": [formato_eta(f) if f else "" for f in df["F_Puerto"]],
        "Declaración": [formato_eta(f) if f else "" for f in df["F_Declaracion"]],
        "Solicitud de pago": [formato_eta(f) if f else "" for f in df["F_Solicitud"]],
        "Pago realizado": [formato_eta(f) if f else "" for f in df["F_Pago"]],
        "Días en tránsito": df["DiasTransito"],
        "Días en puerto": df["DiasEnPuerto"],
        "Días de solicitud a pago": df["DiasSolicitudPago"],
        # Columna pensada para el reclamo, no para la pantalla: permite filtrar
        # en Excel cuántos pagos se pasaron del plazo y por cuánto.
        "Pago fuera de plazo": [
            "Sí" if (es_numero(d) and d > sla_etapas()["Solicitud de pago a finanzas"]) else "No"
            for d in df["DiasSolicitudPago"]],
        "Días de pago a retiro": df["DiasPagoDespacho"],
        "Tarifa en puerto/día": [costo_dia_fila(f) for _, f in df.iterrows()],
        "Costo acumulado en puerto": [costo_demora_fila(f) for _, f in df.iterrows()],
        "Alerta operativa": df["Alerta"],
    })
    for extra in columnas_extra(df):
        salida[extra] = df[extra]
    if COL_ACTUALIZACION in df.columns:
        salida["Última actualización"] = df[COL_ACTUALIZACION]
    if COL_ACTUALIZADO_POR in df.columns:
        salida["Actualizado por"] = df[COL_ACTUALIZADO_POR]
    return salida.reset_index(drop=True)


@st.cache_data(show_spinner=False, ttl=600)
def _df_a_excel(df: pd.DataFrame, hoja: str) -> bytes:
    """Cacheado a propósito: st.download_button exige los bytes por adelantado,
    así que sin caché se reconstruía el Excel completo en CADA rerun de la
    pantalla (cada tecla del buscador, cada clic de filtro)."""
    buffer = io.BytesIO()
    nombre = "".join(c for c in hoja if c.isalnum() or c == " ")[:28] or "Datos"
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=nombre)
    return buffer.getvalue()


@st.fragment
def _render_categoria(df: pd.DataFrame, rol: str, tab_key: str, recibidas_mes: int):
    if df.empty:
        st.info("No hay embarques en esta categoría.")
        return

    conteo = df["EstadoTexto"].value_counts().to_dict()
    proximos_n = conteo.get(EST_PROXIMO, 0)
    # "Por confirmar llegada" cuenta solo lo que de verdad falta por responder,
    # no todo lo que tiene el ETA vencido: un embarque con ETA vencido que ya
    # entró al flujo no está pendiente de nada.
    etapa_col = df["EtapaActual"].astype(str).str.strip()
    en_puerto_n = int(((df["EstadoTexto"] == EST_PUERTO) & (etapa_col == "")).sum())
    retrasados_n = conteo.get(EST_RETRASADO, 0)
    sin_fecha_n = conteo.get(EST_SIN_FECHA, 0)

    valores = [v for v in df.get("ValorNum", []) if es_numero(v)]
    valor_total = sum(valores) if valores else None
    valor_puerto = sum(v for v, e, et in zip(df.get("ValorNum", []), df["EstadoTexto"], etapa_col)
                       if es_numero(v) and e == EST_PUERTO and et == "") if valores else None

    st.markdown(_resumen_ejecutivo(df, recibidas_mes), unsafe_allow_html=True)

    # -------------------- KPIs --------------------
    kpis = [
        ("Total en tránsito", len(df), COLOR_TOTAL, "Todos", "total"),
        (f"Próximos {UMBRAL_PROXIMO} días", proximos_n, STATUS_COLOR[EST_PROXIMO], EST_PROXIMO, "proximos"),
        ("Por confirmar llegada", en_puerto_n, STATUS_COLOR[EST_PUERTO], EST_PUERTO, "enpuerto"),
        ("Retrasados", retrasados_n, STATUS_COLOR[EST_RETRASADO], EST_RETRASADO, "retrasados"),
        (f"Recibidas en {MESES_ES[hoy_rd().month]}", recibidas_mes, COLOR_RECIBIDAS_MES, "__historico__", "recibidas"),
    ]
    # OJO con la clave del contenedor: Streamlit la usa TAL CUAL como clase CSS
    # (st-key-<clave>). Con espacios o acentos el selector no engancha, por eso
    # se construye con un slug ASCII.
    clave = _slug_css(tab_key)
    estilos = "".join(
        f".st-key-kpi_{clave}_{slug} button {{"
        f"background:{color} !important; color:#fff !important; border:none !important;"
        f"border-radius:14px !important; width:100% !important; min-height:92px !important;"
        f"text-align:center !important; padding:14px 10px !important;"
        f"box-shadow:0 2px 8px rgba(17,24,39,0.12) !important; transition:filter .15s ease;}} "
        f".st-key-kpi_{clave}_{slug} button > div {{"
        f"display:flex !important; flex-direction:column !important; align-items:center !important;"
        f"justify-content:center !important; width:100% !important;}} "
        f".st-key-kpi_{clave}_{slug} button p {{margin:0 !important; color:#fff !important;"
        f"text-align:center !important; width:100% !important;}} "
        f".st-key-kpi_{clave}_{slug} button p:first-of-type {{"
        f"font-size:0.68rem !important; font-weight:700 !important; letter-spacing:0.05em !important;"
        f"opacity:0.92 !important; line-height:1.2 !important;}} "
        f".st-key-kpi_{clave}_{slug} button p:last-of-type {{"
        f"font-size:1.9rem !important; font-weight:800 !important; line-height:1.05 !important;"
        f"margin-top:6px !important;}} "
        f".st-key-kpi_{clave}_{slug} button:hover {{filter:brightness(0.93); color:#fff !important;}} "
        f".st-key-kpi_{clave}_{slug} button:focus {{color:#fff !important;"
        f"box-shadow:0 0 0 3px rgba(17,24,39,0.15) !important;}}"
        for _, _, color, _, slug in kpis
    )
    st.markdown(f"<style>{estilos}</style>", unsafe_allow_html=True)

    # El contenedor con clave permite que el CSS los ponga en rejilla de 2 en celular.
    with st.container(key=f"kpirow_{clave}"):
        cols = st.columns(len(kpis))
        for col, (label, valor, color, filtro, slug) in zip(cols, kpis):
            with col:
                with st.container(key=f"kpi_{clave}_{slug}"):
                    if st.button(f"{label.upper()}\n\n{valor}", key=f"btn_{clave}_{slug}", width="stretch"):
                        if filtro == "__historico__":
                            st.session_state["seccion"] = "Histórico"
                            st.rerun()
                        st.session_state[f"estado_{tab_key}"] = filtro
                        rerun_fragmento()

    # -------------------- Valor en tránsito (solo si el Sheet lo trae) --------------------
    if valor_total:
        v1, v2 = st.columns(2)
        v1.markdown(
            tarjeta_kpi("Valor en tránsito", formato_dinero(valor_total), "#0C447C",
                        f"{len(valores)} de {len(df)} embarque(s) con valor declarado"),
            unsafe_allow_html=True,
        )
        v2.markdown(
            tarjeta_kpi("Valor detenido en puerto", formato_dinero(valor_puerto or 0), "#B45309",
                        "mercancía llegada y sin confirmar recepción"),
            unsafe_allow_html=True,
        )
        st.write("")

    st.write("")
    _panel_alertas(df)

    if rol == "admin":
        _panel_confirmacion(df, tab_key)
        if sin_fecha_n:
            st.warning(
                f"{sin_fecha_n} embarque(s) tienen un ETA que la app no puede interpretar y quedan fuera de "
                "los conteos por fecha. Revísalos en Herramientas → Normalizar fechas."
            )

    # -------------------- GRÁFICOS --------------------
    with st.container():
        st.markdown('<div class="solo-pantalla">', unsafe_allow_html=True)
        g1, g2 = st.columns([1.5, 1])
        with g1:
            st.caption("Llegadas previstas")
            grafico_linea_tiempo(df, tab_key)
        with g2:
            st.caption("Origen")
            grafico_paises(df, tab_key)
        st.markdown("</div>", unsafe_allow_html=True)

    # -------------------- EN PROCESO EN PUERTO --------------------
    en_proceso = _en_proceso(df)
    if not en_proceso.empty:
        st.markdown("**En proceso en puerto**")
        st.markdown(html_chips(en_proceso["EtapaActual"].value_counts().to_dict()), unsafe_allow_html=True)
        st.markdown(html_atraso_puerto(en_proceso), unsafe_allow_html=True)
        _panel_en_proceso(en_proceso, rol, contexto=tab_key)

    st.divider()

    # -------------------- FILTROS --------------------
    paises = ["Todos"] + sorted({p for p in df[COL_PAIS] if str(p).strip()})
    estados = ["Todos"] + [e for e in STATUS_ORDER]
    etapas = ["Todas", "Sin confirmar llegada"] + ETAPAS_PUERTO[:-1]
    criterios = ["Urgencia", "Más días detenido", "ETA más próximo", "ETA más lejano",
                 "BL", "País", "Descripción"]
    if valor_total:
        criterios.append("Valor")

    f1, f2, f3 = st.columns([2, 1, 1])
    busqueda = f1.text_input("Buscar", key=f"busca_{tab_key}",
                             placeholder="BL, descripción, modelo, OC…", label_visibility="collapsed")
    pais_sel = f2.selectbox("País", paises, key=f"pais_{tab_key}", label_visibility="collapsed")
    estado_sel = f3.selectbox("Estado", estados, key=f"estado_{tab_key}", label_visibility="collapsed")

    f4, f5, f6 = st.columns([1, 1, 1])
    etapa_sel = f4.selectbox("Etapa", etapas, key=f"etapa_filtro_{tab_key}")
    orden_sel = f5.selectbox("Ordenar por", criterios, key=f"orden_{tab_key}")
    with f6:
        st.write("")
        st.write("")
        if st.button("Limpiar filtros", key=f"limpiar_{tab_key}", width="stretch"):
            for k in (f"busca_{tab_key}", f"pais_{tab_key}", f"estado_{tab_key}",
                      f"orden_{tab_key}", f"etapa_filtro_{tab_key}"):
                st.session_state.pop(k, None)
            st.session_state.pop(f"firma_pais_{tab_key}", None)
            rerun_fragmento()

    filtrado = df
    if pais_sel != "Todos":
        filtrado = filtrado[filtrado[COL_PAIS] == pais_sel]
    if estado_sel != "Todos":
        filtrado = filtrado[filtrado["EstadoTexto"] == estado_sel]
    if etapa_sel == "Sin confirmar llegada":
        filtrado = filtrado[filtrado["EtapaActual"].astype(str).str.strip() == ""]
    elif etapa_sel != "Todas":
        filtrado = filtrado[filtrado["EtapaActual"] == etapa_sel]
    if busqueda and busqueda.strip():
        filtrado = filtrado[filtrado["Buscar"].str.contains(_norm(busqueda), regex=False, na=False)]
    filtrado = ordenar_vista(filtrado, orden_sel)

    resumen_valor = ""
    if valor_total:
        parcial = sum(v for v in filtrado.get("ValorNum", []) if es_numero(v))
        if parcial:
            resumen_valor = f" · {formato_dinero(parcial)}"
    st.caption(f"Mostrando {len(filtrado)} de {len(df)} embarque(s){resumen_valor}")

    render_lista(filtrado)

    # -------------------- EXPORTAR Y VER DETALLE --------------------
    e1, e2 = st.columns([1, 2])
    with e1:
        st.download_button(
            "Descargar esta vista",
            data=_df_a_excel(tabla_exportable(filtrado), f"{tab_key}"[:28] or "Vista"),
            file_name=f"embarques_{_slug_css(tab_key)}_{hoy_rd().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_vista_{tab_key}",
            width="stretch",
        )

    if not filtrado.empty:
        with st.expander("Ver ficha completa de un embarque", expanded=False):
            opciones_det = _etiquetas_desambiguadas(filtrado, con_categoria=False, largo_desc=40)
            elegido = st.selectbox("Embarque", opciones_det, key=f"detalle_{tab_key}",
                                   label_visibility="collapsed")
            _ficha_embarque(filtrado.iloc[opciones_det.index(elegido)])

    # -------------------- ACCIONES DE ADMIN --------------------
    if rol == "admin":
        st.write("")
        with st.expander("Acciones sobre un embarque", expanded=False):
            _panel_acciones(filtrado, tab_key)


def _panel_acciones(df: pd.DataFrame, tab_key: str):
    """Un selector y tres botones, en vez de una lista infinita de botones fila
    por fila (que con 50 embarques hacía la página inusable)."""
    con_bl = df[df[COL_BL].astype(str).str.strip() != ""]
    sin_bl = df[df[COL_BL].astype(str).str.strip() == ""]

    if not sin_bl.empty:
        nombres = ", ".join(str(d)[:40] for d in sin_bl[COL_DESC].head(5))
        st.caption(f"{len(sin_bl)} fila(s) sin BL asignado no se pueden gestionar desde aquí ({nombres}).")

    if con_bl.empty:
        st.info("No hay embarques con BL en la vista actual.")
        return

    opciones = _etiquetas_desambiguadas(con_bl)
    elegido = st.selectbox("Embarque", opciones, key=f"sel_accion_{tab_key}")
    fila = con_bl.iloc[opciones.index(elegido)]
    bl, categoria = str(fila[COL_BL]).strip(), fila["Categoria"]
    n_fila = fila.get("FilaSheet")
    etapa = str(fila.get("EtapaActual", "")).strip()
    clave = clave_fila("acc", tab_key, bl, n_fila)

    if etapa:
        st.markdown(html_flujo(fechas_flujo_de_fila(fila), etapa,
                               es_aerea=(categoria == CATEGORIA_AEREA)) + html_contadores(fila),
                    unsafe_allow_html=True)
    elif fila["EstadoTexto"] in (EST_PUERTO, EST_RETRASADO):
        texto = "'Sí, llegó al aeropuerto'" if categoria == CATEGORIA_AEREA else "'Sí, llegó a puerto'"
        st.caption(f"Llegada sin confirmar todavía — usa {texto} en la sección de confirmación, arriba.")
    st.write("")

    _archivar(fila, clave)
    c2, c3 = st.columns(2)
    if c2.button("Editar", key=f"edit_{clave}", width="stretch"):
        st.session_state["editar_bl"] = bl
        st.session_state["editar_fila"] = n_fila
        st.session_state["seccion"] = "Editar"
        st.rerun()
    if c3.button("Eliminar", key=f"del_{clave}", width="stretch"):
        st.session_state[f"confirmar_del_{tab_key}"] = (bl, categoria, n_fila)
        rerun_fragmento()

    pendiente = st.session_state.get(f"confirmar_del_{tab_key}")
    if pendiente:
        bl_pend, cat_pend, fila_pend = pendiente
        st.warning(f"¿Eliminar definitivamente el BL {bl_pend} (fila {fila_pend} de {cat_pend})? "
                   "No se puede deshacer. Si el embarque llegó, usa 'Marcar como recibido' para "
                   "conservarlo en el histórico.")
        d1, d2, _ = st.columns([1, 1, 3])
        if d1.button("Sí, eliminar", key=f"si_del_{tab_key}", type="primary"):
            ok, mensaje = eliminar_embarque(bl_pend, cat_pend, fila_sugerida=fila_pend)
            st.session_state.pop(f"confirmar_del_{tab_key}", None)
            if ok:
                registrar_log("Eliminado", bl_pend, cat_pend, f"fila {fila_pend}")
                invalidar_caches()
                st.rerun()
            else:
                st.error(mensaje)
        if d2.button("Cancelar", key=f"no_del_{tab_key}"):
            st.session_state.pop(f"confirmar_del_{tab_key}", None)
            rerun_fragmento()


def mostrar_dashboard(datos: dict):
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    encabezado(datos)

    # Los avisos técnicos son instrucciones de trabajo: solo los ve quien puede
    # ejecutarlas. Al espectador no le sirven y le restan confianza en el dato.
    es_admin = st.session_state.get("rol") == "admin"
    if datos["error"]:
        st.error(datos["error"] if es_admin
                 else "No se pudieron leer todos los datos en este momento. Intenta recargar en unos segundos.")
    if es_admin:
        for aviso in datos.get("avisos", []):
            st.warning(aviso)

    df_todo = datos["activos"]
    if df_todo.empty:
        st.info("Todavía no hay embarques cargados.")
        return

    df_todo = enriquecer(df_todo)
    recibidas_mes = contar_recibidas_mes(datos["historico"])
    rol = st.session_state.get("rol", "viewer")

    opciones = ["Todos"] + [c for c in CATEGORIAS if (df_todo["Categoria"] == c).any()]
    en_proceso_df = _en_proceso(df_todo)
    if not en_proceso_df.empty:
        opciones.append(VISTA_EN_PROCESO_PUERTO)
    conteos = df_todo["Categoria"].value_counts().to_dict()
    etiquetas = {
        c: (f"{c} · {len(df_todo)}" if c == "Todos"
            else f"{c} · {len(en_proceso_df)}" if c == VISTA_EN_PROCESO_PUERTO
            else f"{c} · {conteos.get(c, 0)}")
        for c in opciones
    }
    seleccion = selector_horizontal(
        "Categoría", opciones, key="categoria_activa", formato=lambda c: etiquetas.get(c, c),
    )

    if seleccion == VISTA_EN_PROCESO_PUERTO:
        st.markdown(html_chips(en_proceso_df["EtapaActual"].value_counts().to_dict()),
                    unsafe_allow_html=True)
        st.markdown(html_atraso_puerto(en_proceso_df), unsafe_allow_html=True)
        _panel_alertas(en_proceso_df)
        st.divider()
        _panel_en_proceso(en_proceso_df, rol, contexto=VISTA_EN_PROCESO_PUERTO)
        return

    sub = df_todo if seleccion == "Todos" else df_todo[df_todo["Categoria"] == seleccion]
    _render_categoria(sub, rol, seleccion, recibidas_mes)


# ---------------------------------------------------------------------------
# ALTA MANUAL
# ---------------------------------------------------------------------------
def _bls_existentes(datos: dict) -> set:
    activos = set(datos["activos"][COL_BL].astype(str).str.strip()) if not datos["activos"].empty else set()
    historicos = set(datos["historico"][COL_BL].astype(str).str.strip()) if not datos["historico"].empty else set()
    return {b for b in activos | historicos if b}


def form_alta_manual(datos: dict):
    st.subheader("Agregar embarque")
    # La Categoría vive FUERA del form: así, al elegir Aéreos o Carga Suelta, la
    # app puede mostrar los campos OC/EE de una vez (dentro de un st.form los
    # demás widgets no reaccionan hasta el submit).
    categoria = st.selectbox("Categoría", CATEGORIAS, key="alta_categoria")
    con_oc_ee = categoria in CATEGORIAS_CON_OC_EE
    with st.form("form_embarque", clear_on_submit=True):
        c1, c2 = st.columns(2)
        bl = c1.text_input("BL *")
        descripcion = c2.text_input("Descripción del producto *")
        c3, c4 = st.columns(2)
        modelo = c3.text_input("Modelo o serie")
        cantidad = c4.text_input("Cantidad", placeholder="Ej.: 4 unidades, 2 pallets, 113 bultos")
        c5, c6 = st.columns(2)
        pais = c5.text_input("País de origen")
        eta = c6.date_input("Llegada a puerto (ETA)", value=hoy_rd(), format="DD/MM/YYYY")
        oc = ee = ""
        if con_oc_ee:
            c7, c8 = st.columns(2)
            oc = c7.text_input("OC")
            ee = c8.text_input("EE")
        salida = st.date_input("Fecha de salida (opcional)", value=None, format="DD/MM/YYYY",
                               help="Si no la sabes, déjala vacía y agrégala después desde 'Editar'.")
        enviado = st.form_submit_button("Guardar embarque", type="primary")

    if not enviado:
        return

    if not bl.strip() or not descripcion.strip():
        st.error("BL y Descripción son obligatorios.")
        return
    if bl.strip() in _bls_existentes(datos):
        st.error(f"Ya existe un embarque con el BL '{bl.strip()}' (activo o en el histórico). "
                 "Si es un embarque parcial del mismo BL, agrégale un sufijo (por ejemplo "
                 f"'{bl.strip()}-2') para poder distinguirlos.")
        return
    if salida and salida > eta:
        st.error("La fecha de salida no puede ser posterior al ETA.")
        return

    datos_nuevos = {
        COL_BL: bl.strip(),
        COL_DESC: descripcion.strip(),
        COL_MODELO: modelo.strip(),
        COL_CANT: cantidad.strip(),
        COL_PAIS: pais.strip(),
        COL_ETA: eta.isoformat(),
    }
    if salida:
        datos_nuevos[COL_FECHA_SALIDA] = salida.isoformat()
    if con_oc_ee:
        if oc.strip():
            datos_nuevos[COL_OC] = oc.strip()
        if ee.strip():
            datos_nuevos[COL_EE] = ee.strip()

    ok, mensaje = append_row(datos_nuevos, categoria)
    if ok:
        registrar_log("Alta manual", bl.strip(), categoria, f"ETA {eta.isoformat()}")
        invalidar_caches()
        st.success(f"Embarque {bl.strip()} guardado en '{categoria}'.")
        st.rerun()
    else:
        st.error(mensaje)


# ---------------------------------------------------------------------------
# EDICIÓN
# ---------------------------------------------------------------------------
def form_editar(datos: dict):
    st.subheader("Editar embarque")
    df = datos["activos"]
    if df.empty:
        st.info("No hay embarques cargados.")
        return

    con_bl = df[df[COL_BL].astype(str).str.strip() != ""].reset_index(drop=True)
    if con_bl.empty:
        st.info("No hay embarques con BL asignado.")
        return

    opciones = _etiquetas_desambiguadas(con_bl, largo_desc=40)
    indice_default = 0
    preseleccion = st.session_state.pop("editar_bl", None)
    fila_preseleccion = st.session_state.pop("editar_fila", None)
    if preseleccion:
        for i, r in con_bl.iterrows():
            mismo_bl = str(r[COL_BL]).strip() == preseleccion
            misma_fila = (not fila_preseleccion) or str(r.get("FilaSheet")) == str(fila_preseleccion)
            if mismo_bl and misma_fila:
                indice_default = int(i)
                break

    elegido = st.selectbox("Embarque a editar", opciones, index=indice_default, key="sel_editar")
    fila = con_bl.iloc[opciones.index(elegido)]
    categoria = fila["Categoria"]
    bl_original = str(fila[COL_BL]).strip()
    n_fila = fila.get("FilaSheet")
    eta_actual = parsear_fecha(fila[COL_ETA]) or hoy_rd()
    salida_actual = parsear_fecha(fila.get(COL_FECHA_SALIDA, ""))
    sello = str(fila.get(COL_ACTUALIZACION, "")).strip()
    con_oc_ee = categoria in CATEGORIAS_CON_OC_EE

    with st.form("form_editar"):
        c0, c1 = st.columns(2)
        bl_nuevo = c0.text_input("BL", value=bl_original,
                                 help="Solo cámbialo si venía mal escrito. Es el identificador del embarque.")
        descripcion = c1.text_input("Descripción", value=str(fila[COL_DESC]))
        c2, c3 = st.columns(2)
        modelo = c2.text_input("Modelo o serie", value=str(fila[COL_MODELO]))
        cantidad = c3.text_input("Cantidad", value=str(fila[COL_CANT]))
        c4, c5 = st.columns(2)
        pais = c4.text_input("País de origen", value=str(fila[COL_PAIS]))
        eta = c5.date_input("Llegada a puerto (ETA)", value=eta_actual, format="DD/MM/YYYY")
        salida = st.date_input("Fecha de salida", value=salida_actual, format="DD/MM/YYYY")
        oc = ee = ""
        if con_oc_ee:
            c6, c7 = st.columns(2)
            oc = c6.text_input("OC", value=str(fila.get(COL_OC, "")))
            ee = c7.text_input("EE", value=str(fila.get(COL_EE, "")))
        st.caption(f"Fila {n_fila} de '{categoria}' · ETA actual en el Sheet: {fila[COL_ETA] or '(vacío)'}")
        forzar = st.checkbox("Sobrescribir aunque otra persona lo haya cambiado mientras tanto")
        guardar = st.form_submit_button("Guardar cambios", type="primary")

    if not guardar:
        return

    if not bl_nuevo.strip():
        st.error("El BL no puede quedar vacío.")
        return
    if salida and salida > eta:
        st.error("La fecha de salida no puede ser posterior al ETA.")
        return
    if bl_nuevo.strip() != bl_original and bl_nuevo.strip() in _bls_existentes(datos):
        st.error(f"Ya existe otro embarque con el BL '{bl_nuevo.strip()}'.")
        return

    cambios = {
        COL_BL: bl_nuevo.strip(),
        COL_DESC: descripcion.strip(),
        COL_MODELO: modelo.strip(),
        COL_CANT: cantidad.strip(),
        COL_PAIS: pais.strip(),
        COL_ETA: eta.isoformat(),
        COL_FECHA_SALIDA: salida.isoformat() if salida else "",
    }
    if con_oc_ee:
        cambios[COL_OC] = oc.strip()
        cambios[COL_EE] = ee.strip()

    ok, mensaje = actualizar_embarque(bl_original, categoria, cambios, fila_sugerida=n_fila,
                                      sello_esperado=sello, forzar=forzar)
    if ok:
        detalles = []
        if str(fila[COL_ETA]).strip() != eta.isoformat():
            detalles.append(f"ETA {fila[COL_ETA] or 'vacío'} → {eta.isoformat()}")
        if bl_nuevo.strip() != bl_original:
            detalles.append(f"BL {bl_original} → {bl_nuevo.strip()}")
        registrar_log("Edición", bl_nuevo.strip(), categoria, "; ".join(detalles) or "campos varios")
        invalidar_caches()
        st.success("Embarque actualizado.")
        st.rerun()
    else:
        st.error(mensaje)


# ---------------------------------------------------------------------------
# CARGA MASIVA
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _plantilla_excel() -> bytes:
    buffer = io.BytesIO()
    ejemplo = pd.DataFrame(
        [{
            COL_BL: "EGLV142653674620",
            COL_DESC: "Montacargas",
            COL_MODELO: "ERP3.0MXLG / ERP20UXTL",
            COL_CANT: "4 unidades",
            COL_PAIS: "China",
            COL_ETA: "2026-08-25",
            COL_FECHA_SALIDA: "",
        }],
        columns=REQUIRED_COLUMNS + [COL_FECHA_SALIDA],
    )
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        ejemplo.to_excel(writer, index=False, sheet_name="Embarques")
    return buffer.getvalue()


def form_carga_masiva(datos: dict):
    st.subheader("Carga masiva desde Excel")
    st.download_button(
        "Descargar plantilla",
        data=_plantilla_excel(),
        file_name="plantilla_embarques_antillana.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    categoria = st.selectbox("Categoría de destino (todo el archivo se carga aquí)", CATEGORIAS)
    con_oc_ee = categoria in CATEGORIAS_CON_OC_EE
    columnas_opcionales = [COL_FECHA_SALIDA] + ([COL_OC, COL_EE] if con_oc_ee else [])
    extra_txt = f", '{COL_OC}' y '{COL_EE}'" if con_oc_ee else ""
    st.caption("Columnas obligatorias: " + ", ".join(REQUIRED_COLUMNS) +
               f". '{COL_FECHA_SALIDA}'{extra_txt} son opcionales. El ETA puede venir en cualquier "
               "formato reconocible; se guarda como AAAA-MM-DD.")

    archivo = st.file_uploader("Archivo .xlsx", type=["xlsx"])
    if archivo is None:
        return

    try:
        nuevo = pd.read_excel(archivo, dtype=str)
    except Exception as e:
        st.error(f"No se pudo leer el archivo: {e}")
        return

    mapa = {}
    for canon in REQUIRED_COLUMNS + columnas_opcionales:
        for real in nuevo.columns:
            if _norm(real) == _norm(canon):
                mapa[real] = canon
                break
    nuevo = nuevo.rename(columns=mapa)

    faltantes = [c for c in REQUIRED_COLUMNS if c not in nuevo.columns]
    if faltantes:
        st.error(f"Faltan columnas obligatorias: {', '.join(faltantes)}. Usa la plantilla.")
        return

    columnas_a_usar = REQUIRED_COLUMNS + [c for c in columnas_opcionales if c in nuevo.columns]
    nuevo = nuevo[columnas_a_usar].dropna(how="all").copy()
    nuevo = nuevo[nuevo[COL_BL].astype(str).str.strip().ne("") | nuevo[COL_DESC].astype(str).str.strip().ne("")]

    invalidas, normalizadas = [], []
    for i, valor in enumerate(nuevo[COL_ETA]):
        f = parsear_fecha(valor)
        if f is None:
            invalidas.append((i + 2, valor))
            normalizadas.append("")
        else:
            normalizadas.append(f.isoformat())
    if invalidas:
        st.error("Hay fechas ETA que no se pudieron interpretar en las filas: " +
                 ", ".join(f"{fila} ('{val}')" for fila, val in invalidas))
        return
    nuevo[COL_ETA] = normalizadas

    if COL_FECHA_SALIDA in nuevo.columns:
        # Opcional: si viene ilegible se deja en blanco, en vez de bloquear la
        # carga completa por una columna que no es obligatoria.
        nuevo[COL_FECHA_SALIDA] = [
            (parsear_fecha(v).isoformat() if parsear_fecha(v) else "") for v in nuevo[COL_FECHA_SALIDA]
        ]

    existentes = _bls_existentes(datos)
    bl_norm = nuevo[COL_BL].astype(str).str.strip()
    dup_archivo = bl_norm.duplicated(keep="first") & bl_norm.ne("")
    if dup_archivo.any():
        st.warning("El archivo trae BL repetidos internamente; solo se cargará la primera aparición de: " +
                   ", ".join(sorted(set(bl_norm[dup_archivo]))))
    es_dup = bl_norm.isin(existentes) | dup_archivo
    if es_dup.any():
        ya = sorted({b for b in bl_norm[bl_norm.isin(existentes)] if b})
        if ya:
            st.warning("Estos BL ya existen en el sistema y no se cargarán de nuevo: " + ", ".join(ya))

    nuevos = nuevo[~es_dup]
    if nuevos.empty:
        st.info("No hay embarques nuevos que cargar.")
        return

    st.write(f"Vista previa de {len(nuevos)} embarque(s) que se cargarán en **{categoria}**:")
    st.dataframe(nuevos, width="stretch", hide_index=True)

    if st.button(f"Confirmar carga de {len(nuevos)} embarque(s)", type="primary"):
        ok, mensaje = append_rows_bulk(nuevos, categoria)
        if ok:
            registrar_log("Carga masiva", "", categoria, f"{len(nuevos)} embarque(s)")
            invalidar_caches()
            st.success(f"{len(nuevos)} embarque(s) cargado(s) en '{categoria}'.")
            st.rerun()
        else:
            st.error(mensaje)


# ---------------------------------------------------------------------------
# HISTÓRICO
# ---------------------------------------------------------------------------
def _preparar_historico(historico: pd.DataFrame) -> pd.DataFrame:
    """Histórico crudo -> DataFrame con fecha parseada, año, mes y tiempos de
    ciclo. Nada se borra nunca de la pestaña 'Recibido (Mes)': cada recepción
    queda ahí con su fecha, así que en noviembre se puede consultar julio del año
    pasado igual que el mes en curso."""
    df = historico.copy()
    if df.empty:
        return df
    df["FechaParsed"] = [parsear_fecha(v) for v in df.get("Fecha_Recibido", [])]
    df = df[df["FechaParsed"].notna()].copy()
    if df.empty:
        return df
    df["Anio"] = [f.year for f in df["FechaParsed"]]
    df["Mes"] = [f.month for f in df["FechaParsed"]]
    if "Categoria_Origen" in df.columns:
        df["Categoria_Origen"] = df["Categoria_Origen"].replace("", "Sin categoría").fillna("Sin categoría")
    else:
        df["Categoria_Origen"] = "Sin categoría"

    salida = _columna_fechas(df, COL_FECHA_SALIDA)
    puerto = _columna_fechas(df, COL_FECHA_LLEGADA_PUERTO)
    solicitud = _columna_fechas(df, COL_FECHA_SOLICITUD_PAGO)
    pago = _columna_fechas(df, COL_FECHA_PAGO)
    almacen = _columna_fechas(df, COL_FECHA_ALMACEN)
    df["F_Puerto"], df["F_Almacen"] = puerto, almacen
    df["CicloPuertoAlmacen"] = [(a - p).days if (a and p and a >= p) else None
                                for p, a in zip(puerto, almacen)]
    df["CicloSolicitudPago"] = [(pg - s).days if (s and pg and pg >= s) else None
                                for s, pg in zip(solicitud, pago)]
    df["CicloTotal"] = [(a - s).days if (s and a and a >= s) else None
                        for s, a in zip(salida, almacen)]
    return df.sort_values("FechaParsed").reset_index(drop=True)


def _mediana(serie) -> str:
    valores = [v for v in serie if es_numero(v)]
    if not valores:
        return "—"
    return f"{int(pd.Series(valores).median())} d · {len(valores)} emb."


def _grafico_anual(df_anio: pd.DataFrame, anio: int):
    """Los 12 meses del año, incluidos los que van en cero: un mes vacío también
    es información y desaparecerlo del gráfico distorsiona la lectura."""
    conteo = df_anio.groupby("Mes").size().to_dict()
    hoy = hoy_rd()
    etiquetas = [MESES_ES_CORTO[m] for m in range(1, 13)]
    valores = [int(conteo.get(m, 0)) for m in range(1, 13)]
    colores = [COLOR_RECIBIDAS_MES if not (anio == hoy.year and m == hoy.month) else "#1B5E20"
               for m in range(1, 13)]
    fig = go.Figure(data=[go.Bar(
        x=etiquetas, y=valores, marker=dict(color=colores),
        text=[v if v else "" for v in valores], textposition="outside",
        hovertemplate="%{x} " + str(anio) + ": %{y} recibido(s)<extra></extra>",
    )])
    fig.update_layout(
        margin=dict(t=20, b=10, l=10, r=10), height=250,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#374151", size=11), showlegend=False, bargap=0.3, dragmode=False,
        xaxis=dict(showgrid=False),
        yaxis=dict(showgrid=True, gridcolor="#F3F4F6", showticklabels=False),
    )
    st.plotly_chart(fig, width="stretch",
                    config={"displayModeBar": False, "staticPlot": True, "responsive": True},
                    key=f"hist_anual_{anio}")


def _tabla_detalle(df: pd.DataFrame) -> pd.DataFrame:
    """Vista legible del histórico, con la fecha ya formateada en español."""
    if df.empty:
        return pd.DataFrame(columns=["BL", "Descripción", "Modelo/Serie", "Cantidad",
                                     "Origen", "Fecha recibido", "Categoría", "Registrado por"])
    salida = pd.DataFrame({
        "BL": df.get(COL_BL, ""),
        "Descripción": df.get(COL_DESC, ""),
        "Modelo/Serie": df.get(COL_MODELO, ""),
        "Cantidad": df.get(COL_CANT, ""),
        "Origen": df.get(COL_PAIS, ""),
        "Fecha recibido": [f"{f.day:02d} {MESES_ES_CORTO[f.month]} {f.year}" for f in df["FechaParsed"]],
        "Categoría": df.get("Categoria_Origen", ""),
        "Días puerto→almacén": df.get("CicloPuertoAlmacen", ""),
        "Días solicitud→pago": df.get("CicloSolicitudPago", ""),
        "Días salida→almacén": df.get("CicloTotal", ""),
        "Registrado por": df.get("Registrado_Por", ""),
    })
    return salida.reset_index(drop=True)


def mostrar_historico(datos: dict, rol: str):
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.subheader("Histórico de embarques recibidos")

    crudo = datos["historico"]
    df = _preparar_historico(crudo)

    if crudo.empty:
        st.info(
            "Todavía no hay embarques archivados. Cada vez que marques uno como recibido, queda "
            "guardado aquí de forma permanente con la fecha en que llegó, y se puede consultar en "
            "cualquier momento futuro por mes y por año."
        )
        return
    if df.empty:
        st.warning("Hay registros archivados, pero ninguno tiene una fecha de recibido interpretable.")
        return

    descartados = len(crudo) - len(df)
    hoy = hoy_rd()

    mes_ant = (hoy.year - 1, 12) if hoy.month == 1 else (hoy.year, hoy.month - 1)
    n_actual = int(((df["Anio"] == hoy.year) & (df["Mes"] == hoy.month)).sum())
    n_anterior = int(((df["Anio"] == mes_ant[0]) & (df["Mes"] == mes_ant[1])).sum())
    n_anio = int((df["Anio"] == hoy.year).sum())
    n_mismo_mes_anio_pasado = int(((df["Anio"] == hoy.year - 1) & (df["Mes"] == hoy.month)).sum())
    delta = n_actual - n_anterior

    k1, k2, k3 = st.columns(3)
    k1.markdown(tarjeta_kpi(f"{MESES_ES[mes_ant[1]]} {mes_ant[0]} · mes anterior", n_anterior, "#6B7280"),
                unsafe_allow_html=True)
    k2.markdown(
        tarjeta_kpi(
            f"{MESES_ES[hoy.month]} {hoy.year} · mes en curso", n_actual, COLOR_RECIBIDAS_MES,
            f"{'+' if delta >= 0 else ''}{delta} vs. mes anterior"
            + (f" · {n_mismo_mes_anio_pasado} en {hoy.year - 1}" if n_mismo_mes_anio_pasado else ""),
        ),
        unsafe_allow_html=True,
    )
    k3.markdown(tarjeta_kpi(f"Acumulado {hoy.year}", n_anio, COLOR_TOTAL, f"{len(df)} en todo el histórico"),
                unsafe_allow_html=True)
    if descartados:
        st.caption(f"{descartados} registro(s) del archivo no tienen fecha interpretable y no se están contando.")

    st.write("")
    st.divider()

    anios = sorted(df["Anio"].unique(), reverse=True)
    idx_anio = anios.index(hoy.year) if hoy.year in anios else 0
    c_anio, c_info = st.columns([1, 3])
    anio_sel = int(c_anio.selectbox("Año", anios, index=idx_anio, key="hist_anio"))
    df_anio = df[df["Anio"] == anio_sel]
    c_info.markdown(
        f"<div style='padding-top:1.9rem; color:#6B7280; font-size:0.9rem;'>"
        f"{len(df_anio)} embarque(s) recibido(s) en {anio_sel}"
        f"{' · el histórico completo arranca en ' + str(min(anios)) if len(anios) > 1 else ''}</div>",
        unsafe_allow_html=True,
    )

    _grafico_anual(df_anio, anio_sel)

    # -------------------- Tiempos de ciclo --------------------
    # La pregunta que sigue a "¿cuántos recibimos?" es "¿en cuánto tiempo?".
    # Se usa la mediana y no el promedio: un embarque trancado tres meses en
    # puerto le mueve el promedio a todo el año y no representa la operación.
    if df_anio[["CicloPuertoAlmacen", "CicloSolicitudPago", "CicloTotal"]].notna().any().any():
        st.markdown(f"**Tiempos de ciclo {anio_sel} (mediana)**")
        t1, t2, t3 = st.columns(3)
        t1.metric("Puerto → almacén", _mediana(df_anio["CicloPuertoAlmacen"]))
        t2.metric("Solicitud → pago", _mediana(df_anio["CicloSolicitudPago"]))
        t3.metric("Salida → almacén", _mediana(df_anio["CicloTotal"]))
        st.caption("Solo cuenta embarques que tengan ambas fechas registradas. Mientras más completo "
                   "esté el flujo, más confiable es este número — hoy es indicativo, no un estándar.")

    resumen = pd.crosstab(df_anio["Mes"], df_anio["Categoria_Origen"])
    resumen = resumen.reindex(range(1, 13), fill_value=0)
    resumen.insert(len(resumen.columns), "Total", resumen.sum(axis=1))
    resumen.index = [MESES_ES[m] for m in range(1, 13)]
    resumen.index.name = "Mes"
    fila_total = pd.DataFrame([resumen.sum(axis=0)], index=[f"Total {anio_sel}"])
    tabla_resumen = pd.concat([resumen, fila_total])
    st.markdown(f"**Resumen {anio_sel} por mes y categoría**")
    st.dataframe(tabla_resumen, width="stretch")

    d1, d2 = st.columns(2)
    d1.download_button(
        f"Descargar resumen {anio_sel}",
        data=_df_a_excel(tabla_resumen.reset_index().rename(columns={"index": "Mes"}), f"Resumen {anio_sel}"),
        file_name=f"resumen_recibidos_{anio_sel}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_resumen_anio", width="stretch",
    )
    d2.download_button(
        "Descargar histórico completo",
        data=_df_a_excel(_tabla_detalle(df), "Historico"),
        file_name="historico_recibidos_completo.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_historico_total", width="stretch",
    )

    st.divider()

    st.markdown("**Detalle mes por mes**")
    meses_con_datos = sorted(df_anio["Mes"].unique(), reverse=True)
    if not meses_con_datos:
        st.info(f"No hay embarques recibidos registrados en {anio_sel}.")
        return

    idx_mes = meses_con_datos.index(hoy.month) if (anio_sel == hoy.year and hoy.month in meses_con_datos) else 0
    m1, m2 = st.columns([1, 2])
    mes_sel = m1.selectbox("Mes", meses_con_datos, index=idx_mes,
                           format_func=lambda m: MESES_ES[m], key="hist_mes")
    busqueda = m2.text_input("Buscar en el mes", key="hist_busca",
                             placeholder="BL, descripción o modelo…")

    filtrado = df_anio[df_anio["Mes"] == mes_sel]
    if busqueda and busqueda.strip():
        q = _norm(busqueda)
        filtrado = filtrado[filtrado.apply(
            lambda r: q in _norm(f"{r.get(COL_BL,'')} {r.get(COL_DESC,'')} {r.get(COL_MODELO,'')}"), axis=1
        )]

    etiqueta_mes = f"{MESES_ES[mes_sel]} {anio_sel}"
    st.markdown(tarjeta_kpi(f"Recibidos en {etiqueta_mes}", len(filtrado), COLOR_RECIBIDAS_MES),
                unsafe_allow_html=True)
    st.write("")

    tabla = _tabla_detalle(filtrado)
    st.dataframe(tabla, width="stretch", hide_index=True)
    st.download_button(
        f"Descargar {etiqueta_mes} en Excel",
        data=_df_a_excel(tabla, etiqueta_mes),
        file_name=f"recibidos_{anio_sel}_{mes_sel:02d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_mes",
    )

    if rol != "admin" or filtrado.empty:
        return

    st.write("")
    with st.expander("Revertir una recepción", expanded=False):
        opciones = _etiquetas_desambiguadas(filtrado, con_categoria=False, largo_desc=40)
        elegido = st.selectbox("Embarque", opciones, key="sel_revertir")
        fila = filtrado.iloc[opciones.index(elegido)]
        bl = str(fila[COL_BL]).strip()
        guardada = str(fila.get("Categoria_Origen", "")).strip()

        if guardada in CATEGORIAS:
            categoria = guardada
            st.caption(f"Se devolverá a la pestaña '{categoria}' con sus fechas del flujo intactas.")
        else:
            st.caption("Este registro se archivó sin categoría de origen. Elige a dónde devolverlo:")
            categoria = st.selectbox("Categoría de destino", CATEGORIAS, key="cat_revertir")

        if st.button("Quitar de recibido y devolver", key="btn_revertir", type="primary"):
            ok, mensaje = quitar_de_recibido(bl, categoria_manual=categoria,
                                             fila_sugerida=fila.get("FilaSheet"))
            if ok:
                registrar_log("Reversa de recibido", bl, categoria)
                invalidar_caches()
                st.rerun()
            else:
                st.error(mensaje)


# ---------------------------------------------------------------------------
# HERRAMIENTAS DE ADMINISTRACIÓN
# ---------------------------------------------------------------------------
def _herramienta_fechas(df: pd.DataFrame):
    st.markdown("**Normalizar fechas de ETA**")
    st.caption(
        "Google Sheets interpreta las fechas según el locale del archivo, así que una celda escrita "
        "como 06/08/2026 puede quedar guardada como 6 de agosto o como 8 de junio, y quien la lea "
        "después no tiene forma de saber cuál era. Guardar el ETA como texto AAAA-MM-DD elimina el "
        "problema de raíz."
    )
    pendientes = []
    for _, r in df.iterrows():
        diag = analizar_eta(r[COL_ETA])
        if diag["tipo"] == "iso":
            continue
        pendientes.append({
            "categoria": r["Categoria"], "fila": int(r["FilaSheet"]),
            "bl": str(r[COL_BL]).strip() or "(sin BL)",
            "descripcion": str(r[COL_DESC])[:40], **diag,
        })

    if not pendientes:
        st.success("Todos los ETA ya están en formato AAAA-MM-DD. No hay nada que normalizar.")
        return

    ambiguas = [p for p in pendientes if p["tipo"] == "ambigua"]
    convertibles = [p for p in pendientes if p["tipo"] == "convertible"]
    ilegibles = [p for p in pendientes if p["tipo"] == "ilegible"]

    if ilegibles:
        st.error(
            f"{len(ilegibles)} ETA no se pueden interpretar de ninguna forma y hay que corregirlos a mano "
            "en el Sheet: " + ", ".join(f"{p['bl']} (fila {p['fila']} de {p['categoria']}: '{p['crudo']}')"
                                        for p in ilegibles)
        )

    criterio = "Día/Mes/Año (convención dominicana)"
    if ambiguas:
        st.warning(
            f"{len(ambiguas)} fecha(s) admiten dos lecturas porque el día y el mes son ambos ≤ 12. "
            "Elige cómo se escribieron originalmente:"
        )
        criterio = st.radio(
            "Estas fechas se escribieron como:",
            ["Día/Mes/Año (convención dominicana)", "Mes/Día/Año (convención estadounidense)"],
            key="criterio_fechas",
        )
        usar_dm = criterio.startswith("Día")
        st.dataframe(
            pd.DataFrame([{
                "BL": p["bl"], "Categoría": p["categoria"], "Fila": p["fila"],
                "Valor actual": p["crudo"],
                "Se guardará como": (p["dm"] if usar_dm else p["md"]).isoformat(),
                "Otra lectura posible": (p["md"] if usar_dm else p["dm"]).isoformat(),
            } for p in ambiguas]),
            width="stretch", hide_index=True,
        )
    else:
        usar_dm = True

    if convertibles:
        st.info(f"{len(convertibles)} fecha(s) se entienden sin ambigüedad y se convertirán directamente.")
        st.dataframe(
            pd.DataFrame([{
                "BL": p["bl"], "Categoría": p["categoria"], "Fila": p["fila"],
                "Valor actual": p["crudo"], "Se guardará como": p["dm"].isoformat(),
            } for p in convertibles]),
            width="stretch", hide_index=True,
        )

    cambios = [(p["categoria"], p["fila"], p["crudo"], (p["dm"] if usar_dm else p["md"]).isoformat())
               for p in ambiguas]
    cambios += [(p["categoria"], p["fila"], p["crudo"], p["dm"].isoformat()) for p in convertibles]

    if cambios and st.button(f"Convertir {len(cambios)} fecha(s) a AAAA-MM-DD", type="primary"):
        ok, mensaje = normalizar_etas(cambios)
        if ok:
            registrar_log("Normalización de fechas", "", "", f"{len(cambios)} celda(s); criterio={criterio}")
            invalidar_caches()
            st.success(mensaje)
            st.rerun()
        else:
            st.error(mensaje)


def _herramienta_salud(df: pd.DataFrame, historico: pd.DataFrame):
    """Lo que el tablero no muestra pero envenena los números: BLs repetidos,
    campos vacíos, fechas imposibles. Vale más media hora limpiando esto que
    cualquier gráfico nuevo."""
    st.markdown("**Salud de los datos**")
    problemas = []

    bls = df[df[COL_BL].astype(str).str.strip() != ""][COL_BL].astype(str).str.strip()
    repetidos = sorted(set(bls[bls.duplicated(keep=False)]))
    if repetidos:
        problemas.append(
            (f"{len(repetidos)} BL repetido(s) entre embarques activos",
             ", ".join(repetidos[:12]) + ("…" if len(repetidos) > 12 else ""),
             "Si son embarques parciales del mismo BL está bien, pero conviene diferenciarlos "
             "(sufijo -1, -2) para que nadie los confunda al editar.")
        )

    sin_eta = df[df["EstadoTexto"] == EST_SIN_FECHA]
    if not sin_eta.empty:
        problemas.append((f"{len(sin_eta)} embarque(s) con ETA ilegible",
                          ", ".join(f"{r[COL_BL]} ({r['Categoria']} fila {r['FilaSheet']})"
                                    for _, r in sin_eta.head(10).iterrows()),
                          "Quedan fuera de todos los conteos por fecha. Arriba tienes el normalizador."))

    for columna, nombre in ((COL_PAIS, "país de origen"), (COL_DESC, "descripción"),
                            (COL_CANT, "cantidad")):
        vacios = df[df[columna].astype(str).str.strip().isin(["", NO_ESPECIFICADO])]
        if len(vacios):
            problemas.append((f"{len(vacios)} embarque(s) sin {nombre}",
                              ", ".join(str(b) for b in vacios[COL_BL].head(10)),
                              "Campo vacío en el Sheet."))

    # OC y EE son la referencia con la que Compras y Finanzas rastrean la carga
    # aérea y suelta. Sin ellas, el embarque aparece en el tablero pero nadie lo
    # puede amarrar a una orden: es el vacío más caro de los que salen aquí.
    con_oc_ee = df[df["Categoria"].isin(CATEGORIAS_CON_OC_EE)]
    if not con_oc_ee.empty:
        def _vacio(valor):
            texto = str(valor or "").strip().upper()
            return texto in ("", "N/A", "NA", "NAN", "NONE", "-", "—")
        sin_ref = con_oc_ee[[_vacio(o) and _vacio(e)
                             for o, e in zip(con_oc_ee.get(COL_OC, ""), con_oc_ee.get(COL_EE, ""))]]
        if not sin_ref.empty:
            por_cat = sin_ref["Categoria"].value_counts().to_dict()
            problemas.append(
                (f"{len(sin_ref)} embarque(s) de Aéreos/Carga Suelta sin OC ni EE",
                 ", ".join(f"{r[COL_BL] or '(sin BL)'} ({r['Categoria']} fila {r['FilaSheet']})"
                           for _, r in sin_ref.head(12).iterrows())
                 + " · Total por categoría: "
                 + ", ".join(f"{c}: {n}" for c, n in por_cat.items()),
                 "La app sí lee esas columnas: están vacías en el Sheet. Suelen faltar en las "
                 "filas cargadas a mano o importadas sin las columnas OC/EE. Complétalas desde "
                 "'Editar' o vuelve a importar el lote incluyendo ambas columnas.")
            )

    salida_mala = df[[bool(s and e and s > e) for s, e in zip(df["F_Salida"], df["ETAFecha"])]]
    if not salida_mala.empty:
        problemas.append((f"{len(salida_mala)} embarque(s) con fecha de salida posterior al ETA",
                          ", ".join(str(b) for b in salida_mala[COL_BL].head(10)),
                          "Una de las dos fechas está mal escrita."))

    inconsistentes = []
    for _, r in df.iterrows():
        problema = _validar_orden_flujo(fechas_flujo_de_fila(r))
        if problema:
            inconsistentes.append(f"{r[COL_BL]} ({r['Categoria']} fila {r['FilaSheet']})")
    if inconsistentes:
        problemas.append((f"{len(inconsistentes)} embarque(s) con fechas del flujo fuera de orden",
                          ", ".join(inconsistentes[:10]),
                          "Ej.: pago anterior a la declaración. Corrígelo desde 'Corregir fecha' "
                          "en el panel de proceso."))

    if not historico.empty:
        faltos = 0
        for _, r in historico.iterrows():
            if not str(r.get(COL_FECHA_LLEGADA_PUERTO, "")).strip():
                faltos += 1
        if faltos:
            problemas.append((f"{faltos} embarque(s) archivados sin fecha de llegada a puerto",
                              "Registros anteriores al flujo de etapas, o archivados saltándolo.",
                              "No se pueden usar para medir tiempos de ciclo. No hay que corregirlos "
                              "hacia atrás: a partir de ahora entran completos."))

    if not problemas:
        st.success("No se detectaron problemas en los datos cargados.")
        return
    for titulo, detalle, nota in problemas:
        with st.expander(titulo):
            st.write(detalle)
            st.caption(nota)


def respaldo_completo() -> tuple:
    """Copia cruda de TODAS las pestañas del Sheet en un solo .xlsx: valores tal
    cual están, sin enriquecer, sin filtrar y sin reordenar columnas — para que
    sirva para restaurar, no solo para analizar.

    Se lee con get_all_values() y no con la caché de la app a propósito: un
    respaldo tiene que reflejar el Sheet de este momento, no lo que la pantalla
    tenía cargado hace cinco minutos. Cuesta una llamada por pestaña, por eso va
    detrás de un botón y no en cada rerun."""
    try:
        hojas = _con_reintento(lambda: get_spreadsheet().worksheets())
    except Exception as e:  # noqa: BLE001
        return None, f"No se pudo leer el Google Sheet: {e}", 0
    buffer = io.BytesIO()
    filas_totales = 0
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for hoja in hojas:
            try:
                valores = _con_reintento(lambda h=hoja: h.get_all_values())
            except Exception as e:  # noqa: BLE001
                return None, f"Falló la lectura de la pestaña '{hoja.title}': {e}", 0
            # Sin header: la fila 1 se guarda como una fila más, así el respaldo
            # es idéntico al original aunque alguien haya cambiado un encabezado.
            marco = pd.DataFrame(valores) if valores else pd.DataFrame()
            filas_totales += max(len(valores) - 1, 0)
            nombre = "".join(c for c in hoja.title if c.isalnum() or c in " _-")[:31] or "Hoja"
            marco.to_excel(writer, sheet_name=nombre, index=False, header=False)
    return buffer.getvalue(), "", filas_totales


def _herramienta_respaldo():
    st.markdown("**Respaldo**")
    st.caption("Copia cruda de todas las pestañas, tal como están en este momento. "
               "Guárdala fuera de Google Drive: un respaldo dentro de la misma cuenta "
               "no protege contra perder la cuenta.")
    if st.button("Generar respaldo del Sheet completo"):
        with st.spinner("Leyendo todas las pestañas…"):
            contenido, error, filas = respaldo_completo()
        if error:
            st.error(error)
        else:
            st.session_state["respaldo_bytes"] = contenido
            st.session_state["respaldo_nombre"] = (
                f"Respaldo_Embarques_{ahora_rd().strftime('%Y-%m-%d_%H%M')}.xlsx")
            st.session_state["respaldo_filas"] = filas
    if st.session_state.get("respaldo_bytes"):
        st.download_button(
            f"⬇ Descargar {st.session_state['respaldo_nombre']} "
            f"({st.session_state.get('respaldo_filas', 0)} filas)",
            data=st.session_state["respaldo_bytes"],
            file_name=st.session_state["respaldo_nombre"],
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        st.caption("El respaldo automático cada 10 minutos no lo hace esta app: lo hace el "
                   "script de Google Apps Script instalado en el propio Sheet, que corre "
                   "aunque nadie tenga la app abierta.")


def herramientas(datos: dict):
    st.subheader("Herramientas")
    df = datos["activos"]
    if df.empty:
        st.info("No hay embarques cargados.")
        return
    df = enriquecer(df)

    _herramienta_fechas(df)
    st.divider()
    _herramienta_salud(df, datos["historico"])
    st.divider()
    _herramienta_respaldo()
    st.divider()

    st.markdown("**Bitácora**")
    st.caption("Últimos movimientos registrados en la pestaña 'Log' del Sheet: quién cargó, editó, "
               "avanzó una etapa, archivó o borró, y cuándo.")
    if st.button("Ver bitácora"):
        st.session_state["ver_log"] = True
    if st.session_state.get("ver_log"):
        registros = _leer_log()
        if registros.empty:
            st.info("Todavía no hay movimientos registrados.")
        else:
            st.dataframe(registros, width="stretch", hide_index=True, height=340)

    st.divider()
    st.markdown("**Estructura del Google Sheet**")
    st.caption("Usa esto si creaste o renombraste una pestaña y la app todavía no la ve.")
    if st.button("Releer estructura del Sheet"):
        _refrescar_estructura()
        invalidar_caches()
        st.success("Estructura recargada.")
        st.rerun()

    st.caption(f"Versión de la app: {VERSION_APP} · Umbrales de alerta: " +
               ", ".join(f"{ETIQUETA_CORTA_ETAPA[e]} {d} d" for e, d in sla_etapas().items()
                         if e in ETIQUETA_CORTA_ETAPA))


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
