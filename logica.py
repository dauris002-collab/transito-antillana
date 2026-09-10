"""
logica.py — Reglas de negocio de Antillana Comercial, sin Streamlit.

Estado del embarque, etapas del flujo, formateo de fechas y textos, y el
enriquecimiento del DataFrame que alimenta las vistas. Puede importar de
sheets_io.py (constantes de columnas, utilidades de fecha/texto y accesores
de Secrets) pero nunca al revés.
"""

from __future__ import annotations

import html
from datetime import date, datetime

import pandas as pd

from sheets_io import (
    COL_BL, COL_CLIENTE_STOCK, COL_DESC, COL_EE, COL_EMPRESA, COL_ETA,
    COL_FECHA_DECLARACION, COL_FECHA_LLEGADA_PUERTO, COL_FECHA_PAGO_REAL,
    COL_FECHA_SALIDA, COL_FECHA_SIN_MORA, COL_LLEGO, COL_MODELO, COL_OC,
    COL_PAGOREAL_DOP, COL_PAGOREAL_USD, COL_PAIS, COL_ESTADO_PAGO, COL_VIA,
    ESTADO_PAGO_PAGADO, VIA_AEREA,
    CONCEPTOS_PAGO, EMPRESA_ANTILLANA, EMPRESAS_PAGO, ETAPAS_PUERTO,
    INDICE_ETAPA, MESES_ES_CORTO, MONEDA_CONCEPTO,
    _fecha_de_tokens, _interpretar_tokens, _norm, _slug_css, _tokenizar_fecha,
    a_numero, columna_de_valor, costos_puerto, es_llego_no, es_llego_si,
    es_numero, fecha_llegada_fila, hoy_rd, parsear_fecha, sla_etapas,
    _validar_orden_flujo,
)


# OC (orden de compra) y EE: hay áreas que validan la carga por orden de compra
# en vez de por BL, pero solo estas categorías las manejan.
CATEGORIAS_CON_OC_EE = ["Carga Suelta", "General", "Aéreos"]


# Estas categorías son máquinas o lotes con modelo y número de serie propios;
# en Carga Suelta, General y Aéreos ese dato no existe y pedirlo solo genera
# columnas vacías.
CATEGORIAS_CON_MODELO = ["Montacargas", "Construcción y Minería", "Agrícola", "Elevadores",
                         "Generadores", "Consolidados"]


# Dónde se rastrea la carga por cliente y stock disponible.
CATEGORIAS_CON_CLIENTE_STOCK = ["Montacargas", "Construcción y Minería", "Agrícola", "Elevadores",
                                "Generadores", "Aéreos"]


def es_aereo(via) -> bool:
    """True si el modo de transporte de ESTA fila es aéreo. Ya NO depende de la
    categoría del embarque -- antes 'Aéreos' era una categoría/pestaña propia;
    ahora el modo de transporte es un dato de la fila (columna Via_Transporte,
    ver COL_VIA), así que cualquier categoría puede tener embarques por avión
    o por barco. Vacío (filas de antes de que existiera esta columna) se trata
    como marítimo, no como aéreo.

    Para decidir qué mostrarle al usuario sobre una fila real, no se llama
    esta función directamente: se usa es_aereo_fila(), que tiene un respaldo
    para cuando Via_Transporte viene vacío. Esta función se deja tal cual
    para los pocos lugares que necesitan el dato CRUDO de la columna (por
    ejemplo, mostrar la Vía guardada en la ficha del embarque)."""
    return str(via).strip() == VIA_AEREA


def es_aereo_fila(via, categoria="") -> bool:
    """Como es_aereo(), pero con un respaldo: si Via_Transporte viene vacío
    (una fila de antes de que existiera esa columna, o que nunca se llegó a
    llenar), se asume aérea cuando la CATEGORÍA de la fila es 'Aéreos' -- esa
    categoría es exclusivamente de embarques por avión, así que no hace falta
    el campo lleno para saberlo. Con Via_Transporte lleno, manda ese dato
    siempre, sin importar la categoría (la vía es por fila, no por categoría,
    desde la reorganización que separó las dos cosas).

    Esta es la función que hay que usar para decidir qué le dice la pantalla
    al usuario (etiquetas, iconos, textos de estado) sobre una fila real."""
    if str(via).strip():
        return es_aereo(via)
    return str(categoria).strip() == "Aéreos"


def lugar_de(es_aerea) -> str:
    """"aeropuerto" si la fila es aérea, "puerto" para todo lo demás. Recibe
    el booleano ya resuelto (ver es_aereo_fila()), no el valor crudo de
    Via_Transporte -- así el respaldo por categoría se decide en un solo
    lugar y no en cada función que arma un texto. Solo cambia textos que ve
    el usuario: las claves internas (ETAPAS_PUERTO, EST_PUERTO, nombres de
    columnas del Sheet) siguen diciendo "puerto" para no romper los datos ya
    guardados ni los filtros."""
    return "aeropuerto" if es_aerea else "puerto"


def etiqueta_etapa(etapa: str, es_aerea: bool = False) -> str:
    """Nombre corto de la etapa, con "aeropuerto" cuando la fila es aérea
    (booleano ya resuelto, ver es_aereo_fila())."""
    if etapa == ETAPAS_PUERTO[0] and es_aerea:
        return "Llegada al aeropuerto"
    return ETIQUETA_CORTA_ETAPA.get(etapa, etapa)


def etiqueta_etapa_grupo(etapa: str, hay_aereo: bool, hay_maritimo: bool) -> str:
    """Nombre de una etapa para un RESUMEN o FILTRO que puede agrupar varias
    filas a la vez (chips de conteo, el selector "Etapa" de la lista) -- a
    diferencia de etiqueta_etapa(), que es por una sola fila y por eso puede
    decidir con certeza si dice "puerto" o "aeropuerto". Un resumen que junta
    filas de los dos modos de transporte no puede: si son todas aéreas dice
    "aeropuerto", si hay de las dos dice las dos formas, y si no hay ninguna
    aérea se queda como está."""
    if etapa != ETAPAS_PUERTO[0]:
        return ETIQUETA_CORTA_ETAPA.get(etapa, etapa)
    if hay_aereo and hay_maritimo:
        return "Llegada a puerto/aeropuerto"
    if hay_aereo:
        return "Llegada al aeropuerto"
    return ETIQUETA_CORTA_ETAPA.get(etapa, etapa)


ETIQUETA_CORTA_ETAPA = {
    "Llegada a puerto": "Llegada a puerto",
    "Recepción y declaración": "Recepción/declaración",
}


ICONO_ETAPA = {
    "Llegada a puerto": "🚢",
    "Recepción y declaración": "📄",
}


ICONO_ALMACEN = "🏬"


TEXTO_ALERTA_ETAPA = {
    "Llegada a puerto": "en {lugar} sin declarar",
    "Recepción y declaración": "declarado y sin retirar del {lugar}",
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


def _lleno(v) -> bool:
    """Una celda vacía llega como None, como NaN o como cadena vacía según de
    dónde venga la columna. NaN es truthy, así que `if fila["F_Declaracion"]` da
    True en una fila SIN declaración y el conteo sale mal sin avisar."""
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
    filtro activo, en vez de tener dos listas —la de arriba y la del diagrama—
    que no se hablan entre sí."""
    if filtro == "todos":
        return True
    cfg = cfg or costos_puerto()
    dias = fila.get("DiasEnPuerto")
    if not es_numero(dias):
        return False
    if filtro == "atrasados":
        return dias > cfg["umbral"]
    if filtro == "sin_declarar":
        return not _lleno(fila.get("F_Declaracion"))
    return True


def resumen_atraso_puerto(df) -> dict:
    """Lo que está en puerto ahora mismo y cuánto lleva ahí.

    No calcula dinero. El costo de la demora no se estima con una tarifa por día
    —varía por naviera, terminal, volumen y espacio— sino que se observa
    comparando el monto estimado con el realmente pagado; eso vive en el módulo
    de Estatus de Pago. Aquí solo se cuentan días, que es lo que sí se sabe con
    certeza desde tránsito."""
    cfg = costos_puerto()
    vacio = {"n_puerto": 0, "n_atrasados": 0, "n_sin_declarar": 0,
             "dias_excedidos": 0, "dias_promedio": 0.0,
             "umbral": cfg["umbral"], "detalle": []}
    if df is None or df.empty or "DiasEnPuerto" not in df.columns:
        return vacio

    n_puerto = n_atr = n_sin_dec = dias_exc = 0
    suma_dias = 0
    detalle = []
    for _, fila in df.iterrows():
        dias = fila.get("DiasEnPuerto")
        if not es_numero(dias):
            continue
        n_puerto += 1
        suma_dias += int(dias)
        sin_declarar = not _lleno(fila.get("F_Declaracion"))
        if sin_declarar:
            n_sin_dec += 1
        atrasado = dias > cfg["umbral"]
        if atrasado:
            n_atr += 1
            dias_exc += int(dias) - cfg["umbral"]
        if atrasado or sin_declarar:
            detalle.append({
                "bl": str(fila.get(COL_BL, "") or ""),
                "oc": str(fila.get(COL_OC, "") or "").strip(),
                "cat": fila.get("Categoria", ""),
                "dias": int(dias),
                "exceso": max(0, int(dias) - cfg["umbral"]),
                "atrasado": atrasado,
                "sin_declarar": sin_declarar,
            })

    detalle.sort(key=lambda d: d["dias"], reverse=True)
    return {
        "n_puerto": n_puerto, "n_atrasados": n_atr, "n_sin_declarar": n_sin_dec,
        "dias_excedidos": dias_exc,
        "dias_promedio": (suma_dias / n_puerto) if n_puerto else 0.0,
        "umbral": cfg["umbral"], "detalle": detalle,
    }


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
def estado_embarque(eta_valor, llego=None, hoy: date = None):
    """Devuelve (estado, dias_relativos).

    El ETA por sí solo no dice si la carga llegó: solo que la fecha pasó. Quien
    lo decide es la casilla '¿Llegó? SI/NO':
      SI     -> la carga está en puerto; los días se cuentan desde el ETA
      NO     -> se verificó que no llegó: retrasado
      vacío  -> nadie ha revisado: 'En Puerto' es en realidad 'por confirmar'
    """
    eta = parsear_fecha(eta_valor)
    if eta is None:
        return EST_SIN_FECHA, None
    dias = (eta - (hoy or hoy_rd())).days
    if dias < 0:
        if es_llego_no(llego):
            return EST_RETRASADO, abs(dias)
        return EST_PUERTO, abs(dias)
    if es_llego_si(llego):
        # Confirmada con ETA de hoy: ya está en puerto aunque no haya días.
        return EST_PUERTO, 0
    if dias <= UMBRAL_PROXIMO:
        return EST_PROXIMO, dias
    return EST_TRANSITO, None


def texto_estado(estado: str, dias, es_aerea: bool = False) -> str:
    if estado == EST_RETRASADO and dias is not None:
        return f"Retrasado {texto_dias(dias)}"
    if estado == EST_PUERTO and dias is not None:
        donde = "Aeropuerto" if es_aerea else "Puerto"
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
    """fechas = valores (date o None) en el orden de ETAPAS_PUERTO. Devuelve la
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


def _clave_mes_eta(f) -> str:
    """Clave estable y ordenable para el mes de una ETA: 'AAAA-MM', o 'sin_eta'
    si la fecha no se pudo interpretar. Una sola fuente de verdad tanto para
    agrupar el conteo por mes como para filtrar la lista por ese mismo mes."""
    return f"{f.year:04d}-{f.month:02d}" if f else "sin_eta"


def _etiqueta_mes_eta(clave: str) -> str:
    """'2026-09' -> 'sep 2026'. 'sin_eta' se muestra tal cual la etiqueta fija."""
    if clave == "sin_eta":
        return "Sin ETA"
    anio, mes = clave.split("-")
    return f"{MESES_ES_CORTO[int(mes)]} {anio}"


def contar_activos_por_mes_eta(df: pd.DataFrame) -> dict:
    """Cuenta embarques ACTIVOS por mes de ETA, en vivo -- no se guarda un mes
    fijo en ningún lado: si la ETA de una fila cambia de fecha, el conteo se
    recalcula solo la próxima vez que se llame esta función.

    Cuenta TODO lo activo sin importar la etapa (sin declarar, en trámite,
    etc.) -- lo único que sale del conteo es lo ya archivado, porque eso ya
    salió del tablero. Un embarque atrasado con ETA de un mes anterior sigue
    contando en SU mes, no en un bucket aparte de 'vencidos': así el número
    de un mes pasado que se mantiene alto es la señal de que hay atraso
    acumulado ahí. Lo que no tiene un ETA interpretable va bajo la clave
    'sin_eta', nunca se pierde del total.

    Devuelve {clave_mes: cantidad, ...} ya ordenado cronológicamente, con
    'sin_eta' (si existe) al final. clave_mes es 'AAAA-MM' -- usa
    _etiqueta_mes_eta() para el texto que se le muestra al usuario."""
    if df is None or df.empty or COL_ETA not in df.columns:
        return {}
    conteos = {}
    for valor in df[COL_ETA]:
        clave = _clave_mes_eta(parsear_fecha(valor))
        conteos[clave] = conteos.get(clave, 0) + 1
    meses = sorted(k for k in conteos if k != "sin_eta")
    ordenado = {k: conteos[k] for k in meses}
    if "sin_eta" in conteos:
        ordenado["sin_eta"] = conteos["sin_eta"]
    return ordenado


def enriquecer(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega estado, fechas parseadas, contadores, alertas de cuello de botella
    y clave de orden operativo. Se llama UNA vez por refresco sobre la tabla
    completa; las vistas por categoría son rebanadas de este resultado."""
    df = df.copy()
    calculadas = ["EstadoTexto", "DiasRel", "ETAFecha", "MesETA", "EsAerea", "Prioridad", "OrdenSec",
                  "ValorNum", "DiasTransito", "DiasEnPuerto", "DiasEnEtapa", "EtapaActual", "EtapaIdx",
                  "Alerta", "AlertaDias", "Buscar", "F_Salida", "F_Puerto", "F_Declaracion",
                  "BLRepetido", "FlujoRaro"]
    if df.empty:
        for c in calculadas:
            df[c] = []
        return df

    hoy = hoy_rd()
    sla = sla_etapas()

    llegos = list(df[COL_LLEGO]) if COL_LLEGO in df.columns else [""] * len(df)
    etas = [parsear_fecha(v) for v in df[COL_ETA]]
    calculado = [estado_embarque(f, l, hoy) for f, l in zip(etas, llegos)]
    df["ETAFecha"] = etas
    df["EstadoTexto"] = [c[0] for c in calculado]
    df["DiasRel"] = [c[1] for c in calculado]
    # Mes de la ETA, en vivo -- alimenta tanto las pastillas de "en tránsito
    # por mes" en el Dashboard como el filtro "Mes ETA" de la lista. No se
    # guarda en el Sheet: se recalcula cada vez que se enriquece, así que si
    # alguien mueve la ETA de una fila, el mes al que pertenece se mueve solo.
    df["MesETA"] = [_clave_mes_eta(f) for f in etas]
    # Aérea o no, con respaldo por categoría cuando Via_Transporte viene
    # vacío (ver es_aereo_fila) -- una sola vez aquí, en vez de que cada
    # función de UI vuelva a decidir el mismo dato con criterios distintos.
    categorias_col = df["Categoria"] if "Categoria" in df.columns else [""] * len(df)
    vias = df[COL_VIA] if COL_VIA in df.columns else [""] * len(df)
    es_aereas = [es_aereo_fila(v, c) for v, c in zip(vias, categorias_col)]
    df["EsAerea"] = es_aereas
    df["Prioridad"] = df["EstadoTexto"].map(PRIORIDAD_ESTADO).fillna(9).astype(int)

    salidas = _columna_fechas(df, COL_FECHA_SALIDA)
    # La llegada NO es una columna de fecha propia: es el ETA, y solo cuenta si
    # alguien confirmó con SI. Mientras nadie confirme, no hay llegada y ningún
    # contador de puerto arranca — que es justo lo que evita medir días de
    # almacenaje sobre carga que todavía está navegando.
    puertos = [
        parsear_fecha(eta) if es_llego_si(l) else None
        for eta, l in zip(df[COL_ETA], llegos)
    ]
    declaraciones = _columna_fechas(df, COL_FECHA_DECLARACION)
    df["F_Salida"], df["F_Puerto"], df["F_Declaracion"] = salidas, puertos, declaraciones

    fechas_por_fila = list(zip(puertos, declaraciones))
    df["EtapaActual"] = [_etapa_de_fechas(f) for f in fechas_por_fila]
    df["EtapaIdx"] = [INDICE_ETAPA.get(e, -1) for e in df["EtapaActual"]]

    # Dos avisos que cuestan tiempo real: editar el embarque equivocado porque
    # dos filas comparten BL, y confiar en contadores calculados sobre fechas
    # que van al revés.
    bls_norm = df[COL_BL].astype(str).str.strip().str.upper()
    veces = bls_norm.value_counts().to_dict()
    df["BLRepetido"] = [bool(b) and veces.get(b, 0) > 1 for b in bls_norm]
    df["FlujoRaro"] = [_validar_orden_flujo(p, d) for p, d in fechas_por_fila]

    # Contador 1: salida -> llegada confirmada. Congelado en cuanto llegó, para
    # que el número diga "cuánto tardó" y no "cuánto lleva sin llegar".
    df["DiasTransito"] = [
        (ll - sal).days if (sal and ll) else ((hoy - sal).days if sal else None)
        for sal, ll in zip(salidas, puertos)
    ]
    # Contador 2: días en puerto desde la llegada confirmada. Corre contra hoy
    # porque al archivar el embarque sale del tablero activo; el ciclo cerrado
    # queda en el histórico.
    df["DiasEnPuerto"] = [(hoy - p).days if p else None for p in puertos]

    dias_etapa, alertas, alerta_dias = [], [], []
    for etapa, fechas, estado, dias_rel, es_aerea in zip(df["EtapaActual"], fechas_por_fila,
                                                         df["EstadoTexto"], df["DiasRel"], es_aereas):
        d_etapa = None
        if etapa:
            fecha_etapa = fechas[INDICE_ETAPA[etapa]]
            d_etapa = (hoy - fecha_etapa).days if fecha_etapa else None
        dias_etapa.append(d_etapa)

        texto, dias_alerta = "", None
        limite = sla.get(etapa)
        if etapa and d_etapa is not None and limite is not None and d_etapa > limite:
            base = TEXTO_ALERTA_ETAPA.get(etapa, "detenido").format(lugar=lugar_de(es_aerea))
            texto = f"{base[0].upper()}{base[1:]} hace {texto_dias(d_etapa)}"
            dias_alerta = d_etapa
        elif not etapa and estado == EST_RETRASADO and dias_rel is not None \
                and dias_rel > sla["__retraso__"]:
            texto = f"Retrasado {texto_dias(dias_rel)} y sin ETA nuevo"
            dias_alerta = int(dias_rel)
        elif not etapa and estado == EST_PUERTO and dias_rel is not None:
            # Sin umbral a propósito: en cuanto el ETA vence y nadie ha
            # respondido '¿llegó?', ya hay algo que hacer. Poner un plazo de
            # gracia aquí hacía que este aviso contara menos embarques que el
            # KPI "Por confirmar llegada" —que nunca tuvo umbral—, y dos
            # números con la misma etiqueta y distinto valor en la misma
            # pantalla solo generan desconfianza en el tablero.
            cuando = "hoy" if dias_rel == 0 else f"hace {texto_dias(dias_rel)}"
            texto = f"ETA vencido {cuando} y nadie ha confirmado si llegó"
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
    def _col(nombre):
        return df[nombre] if nombre in df.columns else [""] * len(df)
    modelo, oc, ee, cliente = _col(COL_MODELO), _col(COL_OC), _col(COL_EE), _col(COL_CLIENTE_STOCK)
    df["Buscar"] = [
        _norm(f"{bl} {desc} {m} {pais} {o} {e} {c}")
        for bl, desc, m, pais, o, e, c in zip(df[COL_BL], df[COL_DESC], modelo,
                                              df[COL_PAIS], oc, ee, cliente)
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


# ---------------------------------------------------------------------------
# ESTATUS DE PAGO
# ---------------------------------------------------------------------------
def totales_conceptos(fila) -> dict | None:
    """Suma los conceptos llenos de una fila de Pagos, separados por moneda.
    Vacío no es cero: un concepto que no aplica a este expediente no debe
    inflar ni desinflar el total de la moneda contraria. Devuelve None si la
    fila no tiene ningún concepto con monto todavía."""
    totales = {"USD": 0.0, "DOP": 0.0}
    hay_datos = False
    for concepto in CONCEPTOS_PAGO:
        monto = a_numero(fila.get(concepto, ""))
        if monto is None:
            continue
        hay_datos = True
        totales[MONEDA_CONCEPTO[concepto]] += monto
    return totales if hay_datos else None


def monto_extra(fila) -> dict:
    """El EXTRA pagado de más sobre los conceptos originales — lo escribe
    Logística a mano en PagoRealizado_USD/DOP (0 si no hubo diferencia). Ya no
    es una resta entre dos totales congelados: es el número que Logística
    puso, leído tal cual."""
    return {
        "USD": a_numero(fila.get(COL_PAGOREAL_USD)),
        "DOP": a_numero(fila.get(COL_PAGOREAL_DOP)),
    }


def _llegadas_confirmadas(activos: pd.DataFrame, historico: pd.DataFrame) -> dict:
    """BL -> fecha de llegada CONFIRMADA, leída en vivo de tránsito (no del
    valor guardado en Pagos, que se sincroniza una sola vez y puede quedar
    desactualizado si la llegada se confirma después). Un archivo en
    'Recibido (Mes)' siempre cuenta como confirmado — no se archiva sin haber
    pasado por la confirmación de llegada."""
    mapa = {}
    if activos is not None and not activos.empty:
        for _, r in activos.iterrows():
            bl = str(r.get(COL_BL, "")).strip()
            if not bl:
                continue
            llegada = fecha_llegada_fila(r)
            if llegada:
                mapa[bl] = llegada
    if historico is not None and not historico.empty:
        for _, r in historico.iterrows():
            bl = str(r.get(COL_BL, "")).strip()
            if not bl or bl in mapa:
                continue
            llegada = parsear_fecha(r.get(COL_FECHA_LLEGADA_PUERTO, ""))
            if llegada:
                mapa[bl] = llegada
    return mapa


def _referencia_transito(activos: pd.DataFrame, historico: pd.DataFrame) -> dict:
    """BL -> texto de OC / EE / Cliente-Stock tal como está en tránsito, para
    que Pagos muestre quién solicitó el expediente sin que Logística tenga
    que volver a teclearlo. Un BL puede traer más de uno lleno a la vez
    (Aéreos, por ejemplo, admite OC, EE y Cliente/Stock los tres) -- se
    muestran todos los que tengan valor, no solo el primero.
    Activos manda sobre histórico, igual que _llegadas_confirmadas."""
    mapa = {}
    for fuente in (activos, historico):
        if fuente is None or fuente.empty:
            continue
        for _, r in fuente.iterrows():
            bl = str(r.get(COL_BL, "")).strip()
            if not bl or bl in mapa:
                continue
            piezas = []
            oc = str(r.get(COL_OC, "") or "").strip()
            ee = str(r.get(COL_EE, "") or "").strip()
            cliente = str(r.get(COL_CLIENTE_STOCK, "") or "").strip()
            if oc:
                piezas.append(f"OC {oc}")
            if ee:
                piezas.append(f"EE {ee}")
            if cliente:
                piezas.append(f"Cliente/Stock: {cliente}")
            if piezas:
                mapa[bl] = " · ".join(piezas)
    return mapa


def enriquecer_pagos(df_pagos: pd.DataFrame, activos: pd.DataFrame,
                     historico: pd.DataFrame) -> pd.DataFrame:
    """Agrega al DataFrame de Pagos lo que no vive directamente en sus celdas:
    si el BL sigue existiendo en tránsito, el total ACTUAL de lo que está
    lleno en los conceptos (en vivo), el contador de días sin pagar (desde la
    llegada CONFIRMADA hasta hoy o hasta que se pague), si el expediente ya
    está Pagado o sigue Pendiente, — para los ya pagados — el costo final
    (conceptos + el extra que Logística escribió a mano), y quién lo solicitó
    (OC/EE/Cliente-Stock, leído en vivo de tránsito).

    Descripción, Cantidad y Llegada NO se cruzan aquí: viven en la propia hoja
    Pagos, sincronizadas por sincronizar_pagos_con_transito() — se leen tal
    cual de sus columnas. Las excepciones son la llegada CONFIRMADA (para el
    contador de días sin pagar) y la referencia de quién solicitó: esas sí se
    recalculan en vivo, porque si se quedan con el valor sincronizado una
    sola vez, no se actualizan si tránsito cambia después."""
    df = df_pagos.copy()
    calculadas = ["BLSinTransito", "TieneMontos", "TotalActual", "DiasSinPagar",
                  "EmpresaEfectiva", "EstadoEfectivo", "MontoExtra", "ReferenciaTransito",
                  "TotalPagado", "DiasMora", "FechaSinMoraParsed", "FechaPagoRealParsed"]
    if df.empty:
        for c in calculadas:
            df[c] = []
        return df

    # Filas sincronizadas ANTES de que existiera la columna Empresa quedan en
    # blanco; como tránsito solo trackea Antillana, blanco vale como Antillana
    # sin necesidad de corregir nada a mano en el Sheet.
    #
    # Se canoniza contra EMPRESAS_PAGO sin importar mayúsculas/acentos: quien
    # escriba "TECNICARIBE" o "Tecnicaribe" a mano en el Sheet (antes de que
    # el selector de lista estuviera activo) tiene que calzar igual contra el
    # filtro de la app, que compara con el nombre canónico exacto.
    _empresas_norm = {_norm(e): e for e in EMPRESAS_PAGO}

    def _canonizar_empresa(v):
        v = str(v or "").strip()
        if not v:
            return EMPRESA_ANTILLANA
        return _empresas_norm.get(_norm(v), v)

    empresas_crudas = (df[COL_EMPRESA].fillna("").astype(str) if COL_EMPRESA in df.columns
                       else pd.Series([""] * len(df), index=df.index))
    empresas_efectivas = empresas_crudas.apply(_canonizar_empresa)
    df["EmpresaEfectiva"] = empresas_efectivas.tolist()

    bls_transito = set()
    for fuente in (activos, historico):
        if fuente is not None and not fuente.empty:
            bls_transito |= {str(b).strip() for b in fuente[COL_BL] if str(b).strip()}
    # Tecnicaribe y Motor Ibérico nunca van a estar en tránsito (esta app solo
    # trackea embarques de Antillana): avisar "sin tránsito" para ellos sería
    # una alarma falsa, no un dato útil.
    df["BLSinTransito"] = [
        bool(bl) and emp == EMPRESA_ANTILLANA and bl not in bls_transito
        for bl, emp in zip(df[COL_BL].astype(str).str.strip(), empresas_efectivas)
    ]

    referencia_map = _referencia_transito(activos, historico)
    df["ReferenciaTransito"] = [referencia_map.get(bl, "") for bl in df[COL_BL].astype(str).str.strip()]

    totales_actuales = [totales_conceptos(r) for _, r in df.iterrows()]
    df["TotalActual"] = totales_actuales
    df["TieneMontos"] = [t is not None for t in totales_actuales]

    df["FechaSinMoraParsed"] = [parsear_fecha(v) for v in df.get(COL_FECHA_SIN_MORA, [])]
    df["FechaPagoRealParsed"] = [parsear_fecha(v) for v in df.get(COL_FECHA_PAGO_REAL, [])]

    # Pagado si Logística lo marcó así O si ya tiene fecha de pago real puesta
    # (que es como de verdad se trabaja: se teclea la fecha directo en el
    # Sheet, sin pasar por un botón aparte para "marcar como pagado").
    estados_crudos = (df[COL_ESTADO_PAGO].astype(str).str.strip() if COL_ESTADO_PAGO in df.columns
                      else pd.Series([""] * len(df), index=df.index))
    df["EstadoEfectivo"] = [
        ESTADO_PAGO_PAGADO if (estado == ESTADO_PAGO_PAGADO or fecha_pago is not None) else "Pendiente"
        for estado, fecha_pago in zip(estados_crudos, df["FechaPagoRealParsed"])
    ]

    llegadas_confirmadas = _llegadas_confirmadas(activos, historico)
    hoy = hoy_rd()
    dias_sin_pagar = []
    for bl, fecha_pago in zip(df[COL_BL].astype(str).str.strip(), df["FechaPagoRealParsed"]):
        llegada = llegadas_confirmadas.get(bl)
        if not llegada:
            dias_sin_pagar.append(None)  # todavía sin confirmar: el contador no arranca
            continue
        referencia = fecha_pago or hoy   # ya pagado -> se congela ahí; si no, corre hasta hoy
        dias_sin_pagar.append((referencia - llegada).days)
    df["DiasSinPagar"] = dias_sin_pagar

    df["MontoExtra"] = [monto_extra(r) for _, r in df.iterrows()]
    # Costo final = conceptos + el extra que Logística escribió a mano. Solo
    # tiene sentido una vez pagado; para lo pendiente queda en None, porque
    # todavía no hay un extra que sumar.
    total_pagado = []
    for pagado, total, extra in zip(df["EstadoEfectivo"] == ESTADO_PAGO_PAGADO,
                                    df["TotalActual"], df["MontoExtra"]):
        if not pagado:
            total_pagado.append(None)
            continue
        base = total or {"USD": 0.0, "DOP": 0.0}
        total_pagado.append({
            "USD": (base.get("USD") or 0.0) + (extra.get("USD") or 0.0),
            "DOP": (base.get("DOP") or 0.0) + (extra.get("DOP") or 0.0),
        })
    df["TotalPagado"] = total_pagado

    # Positivo = mora (se pagó después del límite saludable). Negativo o cero =
    # a tiempo o antes. Solo se calcula cuando AMBAS fechas existen: mientras
    # falte una, "días transcurridos" todavía no significa nada.
    df["DiasMora"] = [
        (pr - sm).days if (sm and pr) else None
        for sm, pr in zip(df["FechaSinMoraParsed"], df["FechaPagoRealParsed"])
    ]
    return df


def resumen_pagos(df: pd.DataFrame) -> dict:
    """Estadística objetivo del módulo: de los expedientes con montos, cuántos
    ya están Pagados y cuántos siguen Pendientes, cuánto suma lo que TODAVÍA
    se debe (los pendientes), cuántos de los pagados se pagaron dentro de la
    ventana saludable, el promedio de días de mora, y el sobrecosto acumulado
    por moneda (el extra que Logística escribió a mano; solo se suma cuando es
    positivo — un pago más barato que lo estimado no "resta" sobrecosto,
    simplemente no genera ninguno)."""
    vacio = {"n_pagados": 0, "n_abiertos": 0, "n_a_tiempo": 0, "dias_mora_promedio": None,
             "sobrecosto": {"USD": 0.0, "DOP": 0.0}, "total_por_pagar": {"USD": 0.0, "DOP": 0.0}}
    if df is None or df.empty or "EstadoEfectivo" not in df.columns:
        return vacio

    cerrado_mask = df["EstadoEfectivo"] == ESTADO_PAGO_PAGADO
    cerrados = df[cerrado_mask]
    abiertos = df[~cerrado_mask]
    vacio["n_abiertos"] = int((~cerrado_mask).sum())

    # "Total por pagar" solo suma lo ABIERTO (Pendiente): un expediente ya
    # pagado no es algo que "todavía se deba".
    total_por_pagar = {"USD": 0.0, "DOP": 0.0}
    for total in abiertos.get("TotalActual", []):
        total = total or {}
        for moneda in ("USD", "DOP"):
            v = total.get(moneda)
            if v is not None:
                total_por_pagar[moneda] += v
    vacio["total_por_pagar"] = total_por_pagar

    if cerrados.empty:
        return vacio

    dias = [d for d in cerrados["DiasMora"] if d is not None]
    a_tiempo = sum(1 for d in dias if d <= 0)
    sobrecosto = {"USD": 0.0, "DOP": 0.0}
    for extra in cerrados["MontoExtra"]:
        for moneda in ("USD", "DOP"):
            v = extra.get(moneda)
            if v is not None and v > 0:
                sobrecosto[moneda] += v

    return {
        "n_pagados": len(cerrados),
        "n_abiertos": vacio["n_abiertos"],
        "n_a_tiempo": a_tiempo,
        "dias_mora_promedio": (sum(dias) / len(dias)) if dias else None,
        "sobrecosto": sobrecosto,
        "total_por_pagar": total_por_pagar,
    }
