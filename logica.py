"""
logica.py — Reglas de negocio de Antillana Comercial, sin Streamlit.

Estado del embarque, etapas del flujo, costos de demora, formateo de fechas
y textos, y el enriquecimiento del DataFrame que alimenta las vistas. Puede
importar de sheets_io.py (constantes de columnas, utilidades de fecha/texto
y accesores de Secrets) pero nunca al revés.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime

import pandas as pd

from sheets_io import (
    COL_BL, COL_COSTO_DIA, COL_DESC, COL_EE, COL_ESTATUS_LLEGADA,
    COL_ETA, COL_FECHA_ALMACEN, COL_FECHA_DECLARACION, COL_FECHA_LLEGADA_PUERTO,
    COL_FECHA_PAGO, COL_FECHA_SALIDA, COL_FECHA_SOLICITUD_PAGO, COL_MODELO,
    COL_OC, COL_PAIS, ETAPAS_PUERTO, INDICE_ETAPA, MESES_ES_CORTO,
    VALOR_RETRASADO,
    _fecha_de_tokens, _interpretar_tokens, _norm, _slug_css, _tokenizar_fecha,
    a_numero, columna_de_valor, costos_puerto, es_numero, hoy_rd, parsear_fecha,
    sla_etapas, _validar_orden_flujo,
)


CATEGORIAS_CON_OC_EE = ["Aéreos", "Carga Suelta"]


# Estas dos categorías no se despachan desde un puerto marítimo: la carga queda
# en un almacén (aéreo) o donde la deja el consolidador (carga suelta). El
# costo de demora ahí se llama "por almacenaje", no "en puerto" — el resto de
# los contadores (días, SLA) usa exactamente el mismo cálculo.
CATEGORIAS_ALMACENAJE = ["Aéreos", "Carga Suelta"]


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


def etiqueta_costo(categoria) -> str:
    """'Costo por Almacenaje' para Aéreos y Carga Suelta (no pasan por un
    puerto marítimo); 'Costo en puerto' para el resto."""
    return "Costo por Almacenaje" if categoria in CATEGORIAS_ALMACENAJE else "Costo en puerto"


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


TEXTO_ALERTA_ETAPA = {
    "Llegada a puerto": "en {lugar} sin declarar",
    "Recepción y declaración": "declarado y sin solicitar el pago",
    "Solicitud de pago a finanzas": "esperando que Finanzas pague",
    "Pago realizado": "pagado y sin retirar del {lugar}",
}


UMBRAL_PROXIMO = 3          # días para considerar un embarque "Próximo a llegar"


SEMANAS_HORIZONTE = 8


EST_TRANSITO = "En tránsito"


EST_PROXIMO = "Próximo a llegar"


EST_PUERTO = "En Puerto"


EST_RETRASADO = "Retrasado"


EST_SIN_FECHA = "Sin fecha válida"


STATUS_COLOR = {
    EST_TRANSITO: "#2E86DE",
    EST_PROXIMO: "#5C6BC0",
    EST_PUERTO: "#F0B90B",
    EST_RETRASADO: "#D7263D",
    "Recibido": "#2E7D32",
    EST_SIN_FECHA: "#6B7280",
}


STATUS_ORDER = [EST_RETRASADO, EST_PUERTO, EST_PROXIMO, EST_TRANSITO, EST_SIN_FECHA]


PRIORIDAD_ESTADO = {EST_RETRASADO: 0, EST_PUERTO: 1, EST_PROXIMO: 2, EST_TRANSITO: 3, EST_SIN_FECHA: 4}


PALETA_PAISES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


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
    """Tarifa diaria de UNA fila: sale EXCLUSIVAMENTE de Costo_Por_Dia en el
    Sheet. Sin respaldo global ni por categoría — si esa celda está vacía o en
    0, el embarque no tiene tarifa y no suma costo, en vez de heredar un
    promedio que nadie fijó para ese embarque en particular. `cfg` se acepta
    por compatibilidad con las llamadas existentes, aunque ya no se usa aquí."""
    propio = tarifa_a_numero(fila.get(COL_COSTO_DIA))
    return float(propio) if (propio is not None and propio > 0) else 0.0


def costo_demora_fila(fila, cfg=None):
    """Lo que lleva causado ESE embarque, contado DESDE LA LLEGADA A PUERTO.

    El contador no arranca en el día del umbral: el costo se causa desde que la
    carga toca puerto, y el umbral de alerta solo define a partir de cuándo lo
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


def _cumple_filtro_puerto(fila, filtro: str, cfg=None) -> bool:
    """Mismo criterio que arma el detalle de html_atraso_puerto, evaluado fila
    por fila. Se usa para REORDENAR el diagrama de _panel_en_proceso según el
    filtro activo (Atrasados / Pendientes de pago / Costo), en vez de tener dos
    listas —la de arriba y la del diagrama— que no se hablan entre sí."""
    if filtro == "todos":
        return True
    cfg = cfg or costos_puerto()
    dias = fila.get("DiasEnPuerto")
    if not es_numero(dias) or _lleno(fila.get("F_Almacen")):
        return False
    if filtro == "atrasados":
        return dias > cfg["umbral"]
    if filtro == "pago":
        return _lleno(fila.get("F_Solicitud")) and not _lleno(fila.get("F_Pago"))
    if filtro == "costo":
        gasto = costo_demora_fila(fila, cfg)
        return bool(gasto and gasto > 0)
    return True


def resumen_atraso_puerto(df) -> dict:
    """Lo que está en puerto ahora mismo y lo que lleva costado.

    El costo cuenta desde la llegada a puerto, no desde que se pasa del plazo.
    El umbral solo separa lo atrasado de lo que va en tiempo; no mueve el
    contador de dinero."""
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
        pendiente_pago = _lleno(fila.get("F_Solicitud")) and not _lleno(fila.get("F_Pago"))
        if pendiente_pago:
            n_pago += 1
        cobrables = max(0.0, float(dias) - libres)
        costo_fila = cobrables * tarifa
        costo_total += costo_fila
        atrasado = dias > cfg["umbral"]
        if atrasado:
            n_atr += 1
            dias_exc += int(dias) - cfg["umbral"]
            costo_atr += costo_fila
        # El detalle guarda todo lo que tiene algo que contar: costo, atraso o
        # pago pendiente. Un embarque sin ninguna de las tres no aporta nada al
        # filtro de abajo, así que no ocupa espacio en la lista.
        if costo_fila or atrasado or pendiente_pago:
            detalle.append({
                "bl": str(fila.get(COL_BL, "") or ""),
                "oc": str(fila.get(COL_OC, "") or "").strip(),
                "cat": cat,
                "dias": int(dias),
                "exceso": max(0, int(dias) - cfg["umbral"]),
                "atrasado": atrasado,
                "pendiente_pago": pendiente_pago,
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
