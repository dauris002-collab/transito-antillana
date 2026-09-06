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
    COL_PAGOREAL_DOP, COL_PAGOREAL_USD, COL_PAIS, COL_SINMORA_DOP,
    COL_SINMORA_USD, CONCEPTOS_PAGO, EMPRESA_ANTILLANA, ETAPAS_PUERTO,
    INDICE_ETAPA, MESES_ES_CORTO, MONEDA_CONCEPTO,
    _fecha_de_tokens, _interpretar_tokens, _norm, _slug_css, _tokenizar_fecha,
    a_numero, columna_de_valor, costos_puerto, es_llego_no, es_llego_si,
    es_numero, fecha_llegada_fila, hoy_rd, parsear_fecha, sla_etapas,
    _validar_orden_flujo,
)


# OC (orden de compra) y EE: hay áreas que validan la carga por orden de compra
# en vez de por BL, pero solo estas dos categorías las manejan.
CATEGORIAS_CON_OC_EE = ["Aéreos", "Carga Suelta"]


# Equipos, Generadores y Consolidados son máquinas o lotes con modelo y número
# de serie propios; en Aéreos y Carga Suelta ese dato no existe y pedirlo solo
# genera columnas vacías.
CATEGORIAS_CON_MODELO = ["Equipos", "Generadores", "Consolidados"]


# Dónde se rastrea la carga por cliente y stock disponible.
CATEGORIAS_CON_CLIENTE_STOCK = ["Equipos", "Generadores", "Aéreos"]


# Estas dos categorías no se despachan desde un puerto marítimo: la carga queda
# en un almacén (aéreo) o donde la deja el consolidador (carga suelta). Solo
# cambia el rótulo; los contadores usan exactamente el mismo cálculo.
CATEGORIAS_ALMACENAJE = ["Aéreos", "Carga Suelta"]


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
    if etapa == ETAPAS_PUERTO[0] and es_aereo(categoria):
        return "Llegada al aeropuerto"
    return ETIQUETA_CORTA_ETAPA.get(etapa, etapa)


def columna_referencia(categoria) -> str:
    """Qué mostrar en la 3ra columna de la lista para esta categoría."""
    if categoria in CATEGORIAS_CON_MODELO:
        return COL_MODELO
    if categoria in CATEGORIAS_CON_CLIENTE_STOCK:
        return COL_CLIENTE_STOCK
    return ""


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


def enriquecer(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega estado, fechas parseadas, contadores, alertas de cuello de botella
    y clave de orden operativo. Se llama UNA vez por refresco sobre la tabla
    completa; las vistas por categoría son rebanadas de este resultado."""
    df = df.copy()
    calculadas = ["EstadoTexto", "DiasRel", "ETAFecha", "Prioridad", "OrdenSec", "ValorNum",
                  "DiasTransito", "DiasEnPuerto", "DiasEnEtapa", "EtapaActual", "EtapaIdx",
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
        if etapa and d_etapa is not None and limite is not None and d_etapa > limite:
            base = TEXTO_ALERTA_ETAPA.get(etapa, "detenido").format(lugar=lugar_de(cat))
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
    """Pago Realizado menos SIN MORA, por moneda. Ninguno de los dos se
    recalcula aquí: son los totales YA CONGELADOS al momento de registrar cada
    ventana, así que esto es una resta simple entre lo guardado."""
    extra = {}
    for moneda, col_sm, col_pr in (
        ("USD", COL_SINMORA_USD, COL_PAGOREAL_USD),
        ("DOP", COL_SINMORA_DOP, COL_PAGOREAL_DOP),
    ):
        sm, pr = a_numero(fila.get(col_sm)), a_numero(fila.get(col_pr))
        extra[moneda] = (pr - sm) if (sm is not None and pr is not None) else None
    return extra


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


def enriquecer_pagos(df_pagos: pd.DataFrame, activos: pd.DataFrame,
                     historico: pd.DataFrame) -> pd.DataFrame:
    """Agrega al DataFrame de Pagos lo que no vive directamente en sus celdas:
    si el BL sigue existiendo en tránsito, el total ACTUAL de lo que está
    lleno en los conceptos (en vivo, no depende de haber 'congelado' SIN
    MORA), el contador de días sin pagar (desde la llegada CONFIRMADA hasta
    hoy o hasta que se pague), y los totales/mora derivados de las ventanas
    SIN MORA / Pago Realizado ya congeladas.

    Descripción, Cantidad y Llegada NO se cruzan aquí: viven en la propia hoja
    Pagos, sincronizadas por sincronizar_pagos_con_transito() — se leen tal
    cual de sus columnas. La ÚNICA excepción es la llegada CONFIRMADA que usa
    el contador de días sin pagar: esa sí se recalcula en vivo, porque si se
    queda con el valor sincronizado una vez, nunca se actualiza cuando la
    llegada se confirma después."""
    df = df_pagos.copy()
    calculadas = ["BLSinTransito", "TieneMontos", "TotalActual", "DiasSinPagar",
                  "EmpresaEfectiva", "SinMoraTotales", "PagoRealTotales", "MontoExtra",
                  "DiasMora", "FechaSinMoraParsed", "FechaPagoRealParsed"]
    if df.empty:
        for c in calculadas:
            df[c] = []
        return df

    # Filas sincronizadas ANTES de que existiera la columna Empresa quedan en
    # blanco; como tránsito solo trackea Antillana, blanco vale como Antillana
    # sin necesidad de corregir nada a mano en el Sheet.
    empresas_efectivas = (df[COL_EMPRESA].fillna("").astype(str).str.strip() if COL_EMPRESA in df.columns
                          else pd.Series([""] * len(df), index=df.index))
    empresas_efectivas = empresas_efectivas.replace("", EMPRESA_ANTILLANA)
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

    totales_actuales = [totales_conceptos(r) for _, r in df.iterrows()]
    df["TotalActual"] = totales_actuales
    df["TieneMontos"] = [t is not None for t in totales_actuales]

    df["FechaSinMoraParsed"] = [parsear_fecha(v) for v in df.get(COL_FECHA_SIN_MORA, [])]
    df["FechaPagoRealParsed"] = [parsear_fecha(v) for v in df.get(COL_FECHA_PAGO_REAL, [])]

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

    df["SinMoraTotales"] = [
        {"USD": a_numero(r.get(COL_SINMORA_USD)), "DOP": a_numero(r.get(COL_SINMORA_DOP))}
        for _, r in df.iterrows()
    ]
    df["PagoRealTotales"] = [
        {"USD": a_numero(r.get(COL_PAGOREAL_USD)), "DOP": a_numero(r.get(COL_PAGOREAL_DOP))}
        for _, r in df.iterrows()
    ]
    df["MontoExtra"] = [monto_extra(r) for _, r in df.iterrows()]
    # Positivo = mora (se pagó después del límite saludable). Negativo o cero =
    # a tiempo o antes. Solo se calcula cuando AMBAS fechas existen: mientras
    # falte una, "días transcurridos" todavía no significa nada.
    df["DiasMora"] = [
        (pr - sm).days if (sm and pr) else None
        for sm, pr in zip(df["FechaSinMoraParsed"], df["FechaPagoRealParsed"])
    ]
    return df


def resumen_pagos(df: pd.DataFrame) -> dict:
    """Estadística objetivo del módulo: de los expedientes con SIN MORA y Pago
    Realizado registrados, cuántos se pagaron dentro de la ventana saludable,
    el promedio de días de mora, y el sobrecosto acumulado por moneda (solo se
    suma cuando el extra es positivo; un pago más barato que lo estimado no
    "resta" sobrecosto, simplemente no genera ninguno)."""
    vacio = {"n_pagados": 0, "n_a_tiempo": 0, "dias_mora_promedio": None,
             "sobrecosto": {"USD": 0.0, "DOP": 0.0}}
    if df is None or df.empty or "FechaPagoRealParsed" not in df.columns:
        return vacio
    cerrados = df[df["FechaPagoRealParsed"].notna() & df["FechaSinMoraParsed"].notna()]
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
        "n_a_tiempo": a_tiempo,
        "dias_mora_promedio": (sum(dias) / len(dias)) if dias else None,
        "sobrecosto": sobrecosto,
    }
