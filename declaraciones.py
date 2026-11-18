"""
declaraciones.py - Analítica e histórico de declaraciones SIGA (decisión #36)
==============================================================================

Segunda vista dentro de Analítica. Lee el Sheet aparte del histórico (secret
HISTORICO_SHEET_ID, sheets_io.cargar_historico), que Logística llena una vez al mes con el
Excel que genera generar_historico_plataforma.py a partir de los PDF de liquidación.

Reglas:
 - SIN montos: el Sheet no los trae y aquí no se calculan.
 - 2026 se compara siempre contra el MISMO PERÍODO de los años anteriores (ene → mes de corte).
   Mes de corte = último mes del año en curso con datos; si ese mes trae menos del 25 % de lo
   normal (los PDF de ese mes aún no están todos) se toma el anterior y se avisa.
 - Unidades = suma de Cantidad de los ítems clasificados como equipo. Repuestos no tienen
   unidades comparables (kg, litros, piezas): se miden por número de declaraciones.
 - Clasificación (tipo de equipo, familia, marca, Equipos/Repuestos/Otros) se hace aquí, por
   partida y descripción, para poder corregirla sin volver a subir el Sheet.

Capa: como taller.py (importa de sheets_io hacia abajo; nada importa este módulo salvo app.py).
"""

from __future__ import annotations

import html
import re
import unicodedata
from types import SimpleNamespace

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from sheets_io import TTL_HISTORICO, cargar_historico, invalidar_historico

# ============================== clasificación de ítems ==============================

# ---------------------------------------------------------------- clasificación de ítems
# Tipo de equipo: por partida (6 dígitos) y palabras de la descripción. Lo que no es
# equipo queda en None y se cuenta como Repuestos u Otros según el capítulo.
def _tiene(d, *pal):
    return any(p in d for p in pal)


_EQUIPO_AL_INICIO = re.compile(
    r"^\s*(MONTA|TRACTOR|CAMION|GENERADOR|GENSET|PLANTA EL|CARRETILLA|EXCAVADORA|RETRO|PALA|CARGADOR|MINICARG|RODILLO|"
    r"COSECHADORA|IMPLEMENTO|RASTRA|ARADO|SEMBRADORA|AUTOBUS|ELEVADOR|PLATAFORMA|TRANSPALETA|MOTOCULTOR|SEMI|"
    r"REMOLQUE|PANEL|MODULO|SOLAR|INVERSOR|TORRE|GRUA|ESCALERA|ASCENSOR|PALLET|PALILLAC|MOTONIVELADORA|TOPADORA|"
    r"PAVIMENTADORA|CHASIS|FURGONETA|JEEP|CAMIONETA|VOLTEO|MINIBUS|CULTIVADOR|SUBSOLADOR|ABONADORA|FUMIGADORA|"
    r"CHAPEADORA|SURCADOR|ROT[AO]VATOR|DESBROZADORA|PERF[OU]RADOR|AGRATOR|ASPERSORA|SEGADORA|EMPACADORA|ENFARDADORA|"
    r"TRILLADORA|ESPARCIDORA|NIVELADORA|SIEMBRA|UTILEV|TRANSPALLET|APILADOR|INVERTER|SOLAR|"
    r"YALE|HYSTER|BALDAN|YANMAR|VALTRA|NEW HOLLAND|JOHN DEERE|CUMMINS|INTERNATIONAL|KACO|JINKO|LONGI|CPS )")
# En estas partidas conviven equipos y piezas: se exige que la descripción EMPIECE por el equipo.
_AMBIGUAS = {"8426", "8427", "8428", "8430", "8467", "8479", "8501", "8504", "8541", "8716"}
# En 8432/8433 (maquinaria agrícola) los implementos traen códigos de modelo al inicio ("CRSG-RASTRA…"),
# así que ahí se descarta solo lo que EMPIEZA por el nombre de una pieza.
_PIEZA_AL_INICIO = re.compile(
    r"^\s*(DISCO|VALVULA|VÁLVULA|TENSOR|SIN FIN|CONDUCTOR|LIMPIADOR|MANGA|PLATO|LINEA|LÍNEA|GOMA|PI[NÑ]ON|EJE|"
    r"CADENA|RUEDA|RESORTE|TUERCA|PERNO|CASQUILLO|SOPORTE|MAZA|AZADA|CUCHILLA|BUJE|RODAMIENTO|ENGRANAJE|PUNTA|"
    r"ZAPATA|BRAZO|FILTRO|CORREA|MUELLE|TORNILLO|ARANDELA|PASADOR|CHUMACERA|DEDO|CONJUNTO|KIT|JUEGO|CILINDRO|"
    r"MANGUERA|SELLO|RETEN|RETÉN|BUJE|GUIA|GUÍA|PLACA|TAPA|BASE|BARRA|PALETA|CUCHARA|DIENTE|REJA|PUNTERA|RESORTES)\b")
_ES_PARTE = re.compile(r"(?<![A-Z])(PARA|P/|PARTES?|REPUESTOS?|PIEZAS?)(?![A-Z])")
_YALE_ELECTRICO = re.compile(r"\b(ERC|ERP|ESC|NDR|NR|NSS|MSL|MPB|MPE|MPC|MPW|MS|OS)\s?-?\d{2,3}")


def tipo_equipo(partida: str, desc: str):
    p = (partida or "")[:7]
    p4 = p[:4]
    d = " " + (desc or "").upper() + " "
    if p4 in ("8432", "8433"):
        if _PIEZA_AL_INICIO.match(d):
            return None
    elif (p4 in _AMBIGUAS or _ES_PARTE.search(d)) and not _EQUIPO_AL_INICIO.match(d):
        return None  # "GOMA DE MONTACARGAS", "MANGA PARA COSECHADORA", "DISCO PARA SEMBRADORA": repuesto
    if p.endswith(".90") and p4 not in ("8427", "8701"):     # xx.90 = partes, salvo los que sí son equipo
        if not (p4 == "8428" and _tiene(d, "PLATAFORMA")):
            return None
    if p4 == "8502":
        if p == "8502.40":
            return "Inversores"
        if _tiene(d, "TORRE", " LUZ", "LUCES", "ILUMINA"):
            return "Torres de iluminación"
        return "Generadores eléctricos"
    if p in ("8501.31", "8501.32", "8501.33", "8501.34", "8501.61", "8501.62", "8501.63", "8501.64") and _tiene(d, "GENERADOR"):
        return "Generadores eléctricos"
    if p4 == "9405" and _tiene(d, "TORRE"):
        return "Torres de iluminación"
    if p4 == "8541" and p[5:7] in ("40", "41", "42", "43") and _tiene(d, "SOLAR", "PANEL", "MODUL"):
        return "Paneles solares"
    if p == "8504.40" and _tiene(d, "INVERSOR", "INVERTER"):
        return "Inversores"
    if p in ("8504.40", "8504.21") and _tiene(d, "CARGADOR") and _tiene(d, "BATER") and \
            _tiene(d, "MONTACARGA", "PCH", "YALE", "INDUSTRIAL", "PLANTA", "TRACCION"):
        return "Cargadores de batería"
    if p4 == "8428":
        if p == "8428.40" or _tiene(d, "ESCALERA"):
            return "Escaleras eléctricas"
        if _tiene(d, "PLATAFORMA", "PLATFORM", "LIFT TABLE"):
            return "Plataformas elevadoras"
        if _tiene(d, "ELEVADOR", "ASCENSOR", "PASAJERO"):
            return "Elevadores"
        if p in ("8428.10", "8428.20", "8428.39", "8428.90"):
            if _tiene(d, "PALLET", "PALILLAC", "PALETIZ", " MPB", " MPC", " MPW"):
                return "Palillac"
            if _tiene(d, "TRANSPALETA") or (_tiene(d, "CARRETILLA") and _tiene(d, "MANUAL")):
                return "Transpaletas manuales"
            electrico = _tiene(d, "ELECTRIC", "ELÉCTRIC") or bool(_YALE_ELECTRICO.search(d))
            if _tiene(d, "CARRETILLA") and electrico and not _tiene(d, "MONTACARGA", "MONTA CARGA"):
                return "Carretillas Eléctricas"
            if _tiene(d, "MONTACARGA", "MONTA CARGA", "FORKLIFT", "CARRETILLA"):
                return "Montacargas Eléctricos" if electrico else "Montacargas"
        return None
    if p4 == "8427":
        if p == "8427.90" or _tiene(d, "TRANSPALETA", "MANUAL"):
            return "Transpaletas manuales"
        if _tiene(d, "CARRETILLA"):
            return "Carretillas Eléctricas"
        if _tiene(d, "PALLET", "PALILLAC"):
            return "Palillac"
        if p == "8427.10" or _tiene(d, "ELECTRIC"):
            return "Montacargas Eléctricos"
        return "Montacargas"
    if p4 == "8429":
        if p in ("8429.11", "8429.19"):
            return "Topadoras"
        if p == "8429.20":
            return "Motoniveladoras"
        if p in ("8429.30",):
            return "Palas Cargadoras"
        if p == "8429.40":
            return "Rodillos compactadores" if _tiene(d, "RODILLO", "COMPACT") else "Excavadoras"
        if p == "8429.51":
            if _tiene(d, "MINICARGADOR", "MINI CARGADOR", "SKID", " L318", " L320", " 316GR", " 318G", " 320G"):
                return "Minicargadores"
            if _tiene(d, "RETRO"):
                return "Retroexcavadoras (retropalas)"
            return "Palas Cargadoras"
        if p == "8429.52":
            return "Excavadoras"
        if p == "8429.59":
            return "Excavadoras" if _tiene(d, "EXCAVADORA") and not _tiene(d, "RETRO") else "Retroexcavadoras (retropalas)"
        return None
    if _tiene(d, "RODILLO") and p in ("8430.61", "8467.19", "8432.80", "8474.20"):
        return "Rodillos compactadores"
    if p4 == "8426" and _tiene(d, "GRUA", "GRANE", "CRANE"):
        return "Grúas"
    if p == "8479.10" and _tiene(d, "PAVIMENT", "ASFALT", "ALFALT"):
        return "Pavimentadoras"
    if p4 == "8432":
        return "Motocultores" if _tiene(d, "MOTOCULTOR") else "Implementos agrícolas"
    if p4 == "8433" and p[5:6] == "5" and _tiene(d, "COSECHADORA", "TRILLADORA", "COMBINADA"):
        return "Cosechadoras"
    if p4 == "8701":
        if p == "8701.10" or _tiene(d, "MOTOCULTOR"):
            return "Motocultores"
        if p[5:6] == "2":
            return "Tractores de carretera"
        if p == "8701.30":
            return "Topadoras"
        return "Tractores agrícolas"
    if p4 == "8702":
        return "Autobuses"
    if p4 == "8703":
        return "Vehículos livianos"
    if p4 == "8704":
        if p == "8704.31" and _tiene(d, "CAMIONETA", "JEEP", "TOYOTA", "PICK", "TACOMA", "HILUX", "RANGER"):
            return "Vehículos livianos"
        return "Camiones"
    if p4 == "8705":
        return "Camiones"
    if p4 == "8716" and p[5:6] in ("3", "4") and _tiene(d, "PLATAFORMA", "REMOLQUE", "SEMI", "CHASIS", "CHASSIS", "CAMA"):
        return "Semirremolques"
    return None


FAMILIA_DE = {
    "Camiones": "Transporte", "Tractores de carretera": "Transporte", "Autobuses": "Transporte",
    "Semirremolques": "Transporte", "Vehículos livianos": "Transporte",
    "Montacargas": "Manejo de materiales", "Montacargas Eléctricos": "Manejo de materiales",
    "Palillac": "Manejo de materiales", "Carretillas Eléctricas": "Manejo de materiales",
    "Transpaletas manuales": "Manejo de materiales", "Plataformas elevadoras": "Manejo de materiales",
    "Cargadores de batería": "Manejo de materiales",
    "Excavadoras": "Construcción y minería", "Retroexcavadoras (retropalas)": "Construcción y minería",
    "Palas Cargadoras": "Construcción y minería", "Minicargadores": "Construcción y minería",
    "Motoniveladoras": "Construcción y minería", "Rodillos compactadores": "Construcción y minería",
    "Topadoras": "Construcción y minería", "Pavimentadoras": "Construcción y minería", "Grúas": "Construcción y minería",
    "Torres de iluminación": "Construcción y minería",
    "Generadores eléctricos": "Generadores",
    "Tractores agrícolas": "Agrícola", "Motocultores": "Agrícola", "Cosechadoras": "Agrícola",
    "Implementos agrícolas": "Agrícola",
    "Elevadores": "Elevadores", "Escaleras eléctricas": "Elevadores",
    "Paneles solares": "Energía solar", "Inversores": "Energía solar",
}

MARCAS = [  # (nombre, palabras que la identifican en la descripción)
    ("Cummins", ["CUMMINS"]), ("International", ["INTERNATIONAL", "NAVISTAR"]), ("Yale", ["YALE"]),
    ("Hyster", ["HYSTER"]), ("Valtra", ["VALTRA"]), ("Yanmar", ["YANMAR"]), ("John Deere", ["JOHN DEERE", "DEERE"]),
    ("New Holland", ["NEW HOLLAND"]), ("Hitachi", ["HITACHI"]), ("Hyundai", ["HYUNDAI"]), ("XCMG", ["XCMG"]),
    ("Bomag", ["BOMAG"]), ("Caterpillar", ["CATERPILLAR"]), ("Toyota", ["TOYOTA"]), ("Ford", ["FORD"]),
    ("Volkswagen", ["VOLKSWAGEN"]), ("Chenglong", ["CHENGLONG"]), ("Gami", ["GAMI"]), ("TVH", ["TVH"]),
    ("Lift-Rite", ["LIFT-RITE", "LIFT RITE", "LIFTRITE"]), ("CTB", ["CTB"]), ("Volmex", ["VOLMEX"]),
    ("National Crane", ["NATIONAL CRANE", "NATIONAL GRANE"]), ("Generac", ["GENERAC"]), ("Baldan", ["BALDAN"]),
    ("Daedong", ["DAEDONG", "KIOTI"]), ("Jinko Solar", ["JINKO", "JKM"]), ("LONGi", ["LONGI"]),
    ("KACO", ["KACO"]), ("CPS (Chint Power)", ["CPS ", "CHINT"]), ("InfiniSolar", ["INFINISOLAR"]),
]


_CUMMINS_MODELO = re.compile(r"\bC\s?\d{2,4}\s?D\s?\d|\bC\d{2,4}\s?N\d|\bDQ[A-Z]{2}\b|\bQSK\d|\bQSX\d")
_YALE_MODELO = re.compile(r"\b(GP|GLP|GDP|ERC|ERP|ESC|NDR|NR|NSS|MSL|MPB|MPE|MPC|MPW)\s?-?\d{2,3}")


def marca(desc: str, tipo=None):
    """Marca leída en la descripción. Además, el modelo identifica la marca en dos casos:
    generadores C30D6/C150D6 (Cummins) y montacargas GP050VX/ERC045 (Yale)."""
    d = " " + re.sub(r"[^A-Z0-9\- ]", " ", (desc or "").upper()) + " "
    for nombre, pals in MARCAS:
        if any(re.search(r"(?<![A-Z0-9])" + re.escape(p.strip()) + r"(?![A-Z0-9])", d) for p in pals):
            return nombre
    if tipo == "Generadores eléctricos" and _CUMMINS_MODELO.search(d):
        return "Cummins"
    if tipo in ("Montacargas", "Montacargas Eléctricos", "Palillac", "Carretillas Eléctricas") and _YALE_MODELO.search(d):
        return "Yale"
    return "Sin marca en la descripción"


def categoria(tipo, partida: str):
    """Equipos / Repuestos / Otros por ítem."""
    if tipo:
        return "Equipos"
    c = (partida or "")[:2]
    p4 = (partida or "")[:4]
    if c in ("84", "85", "87", "40") or p4 in ("7315", "7318", "7320", "6813", "7009", "8302", "9026", "9029",
                                                 "9031", "9032", "9025", "3917", "7412", "7307", "4823", "8301"):
        return "Repuestos"
    return "Otros"


def _clasificar(items: pd.DataFrame) -> pd.DataFrame:
    tipos = [tipo_equipo(p, d) for p, d in zip(items["Partida"], items["Descripcion"])]
    items = items.assign(Tipo=tipos)
    items["Familia"] = items["Tipo"].map(FAMILIA_DE)
    items["Marca"] = [marca(d, t) if t else None for d, t in zip(items["Descripcion"], items["Tipo"])]
    items["Cat"] = [categoria(t, p) for t, p in zip(items["Tipo"], items["Partida"])]
    return items


# ============================== preparación de datos ==============================
MES = ["", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
MES_LARGO = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
             "octubre", "noviembre", "diciembre"]
FAMS = ["Manejo de materiales", "Generadores", "Agrícola", "Transporte", "Construcción y minería", "Elevadores"]
EMPRESAS = ["Antillana Comercial", "Tecnicaribe", "Motor Ibérico"]
_MINUS = {"De", "Del", "La", "Las", "Los", "Y", "E"}


def _pais(p: str) -> str:
    """"ESTADOS UNIDOS" -> "Estados Unidos"; "COREA DEL SUR" -> "Corea del Sur"."""
    pal = str(p or "").strip().title().split()
    return " ".join(w.lower() if i and w in _MINUS else w for i, w in enumerate(pal)) or "Sin dato"


def _sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(s or "")) if unicodedata.category(c) != "Mn").upper()


def _entero(s):
    return pd.to_numeric(s, errors="coerce").fillna(0).astype(int)


@st.cache_data(ttl=TTL_HISTORICO, show_spinner="Preparando el histórico…")
def _preparar(_clave: str) -> dict:
    """_clave = hora de la carga: cuando el Sheet se recarga, esto se recalcula."""
    t = cargar_historico()["tablas"]
    d = t["Declaraciones"].copy()
    d["i"] = d["Declaracion"].astype(str)
    d["Anio"], d["Mes"] = _entero(d["Anio"]), _entero(d["Mes"])
    d = d[d["Anio"] > 0]
    d["Adm"] = d["Administracion"].astype(str)
    d["Carga"] = d["Tipo_Carga"].astype(str)
    base = d[["i", "Empresa", "Anio", "Mes", "Adm"]]

    it = t["Items"].copy()
    it["i"] = it["Declaracion"].astype(str)
    it["Partida"] = it["Partida"].astype(str).str.strip()
    it["Descripcion"] = it["Descripcion"].astype(str)
    it["Cant"] = pd.to_numeric(it["Cantidad"], errors="coerce").fillna(0.0)
    it["Pais"] = it["Pais_Origen"].map(_pais)
    it = _clasificar(it).merge(base, on="i", how="inner")
    it["_desc"] = it["Descripcion"].map(_sin_acentos)

    co = t["Contenedores"].copy()
    co["i"] = co["Declaracion"].astype(str)
    tam = pd.to_numeric(co["Tamano_Pies"], errors="coerce")
    co["Tam"] = [("Sin dato" if pd.isna(x) else f"{int(x)}'") + (" HC" if str(h).startswith("S") else "")
                 for x, h in zip(tam, co["High_Cube"])]
    co = co.merge(base, on="i", how="inner")
    ve = t["Vehiculos"].copy()
    ve["i"] = ve["Declaracion"].astype(str)

    # año en curso y mes de corte (ver docstring)
    act = int(d["Anio"].max())
    por_mes = d[d["Anio"] == act].groupby("Mes").size()
    corte, parcial = int(por_mes.index.max()), None
    if len(por_mes) > 1 and por_mes.iloc[-1] < 0.25 * por_mes.iloc[:-1].median():
        parcial = (corte, int(por_mes.iloc[-1]))
        corte = int(por_mes.index[-2])
    return {"decl": d, "items": it, "cont": co, "veh": ve, "act": act, "corte": corte, "parcial": parcial,
            "anio_min": int(d["Anio"].min())}


# ============================== estilo ==============================
NAVY, AZ, AZ2, GR, GR2, TX, TX2, LN = "#0C447C", "#2A78D6", "#9EC5F4", "#C9CED6", "#E9ECF0", "#0B0B0B", "#52514E", "#E1E0D9"
NAR, VER, AMB = "#EB6834", "#1BAF7A", "#EDA100"
UP, DN = "#1B7F3B", "#B42318"
FUENTE = "system-ui, -apple-system, Segoe UI, sans-serif"
_CSS = f"""<style>
.dsg-cab{{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:2px solid {NAVY};padding-bottom:8px;margin:4px 0 12px}}
.dsg-cab h2{{font-family:{FUENTE};font-size:1.4rem;font-weight:700;color:{TX};margin:0;padding:0}}
.dsg-cab .sub{{font-size:.8rem;color:{TX2};margin-top:3px}}
.dsg-cab .meta{{text-align:right;font-size:.72rem;color:{TX2};line-height:1.5}}
.dsg-sec{{font-family:{FUENTE};font-size:1.05rem;font-weight:700;color:{TX};margin:22px 0 2px}}
.dsg-ssub{{font-size:.8rem;color:{TX2};margin-bottom:10px}}
.dsg-q{{font-family:{FUENTE};font-size:.98rem;font-weight:650;color:{TX};margin:0}}
.dsg-a{{font-size:.84rem;color:{TX2};margin:3px 0 4px;line-height:1.45}} .dsg-a b{{color:{TX}}}
.dsg-nota{{font-size:.7rem;color:#898781;margin-top:2px}}
.dsg-hall{{background:#F7F9FC;border:1px solid #DCE4EE;border-left:4px solid {NAVY};border-radius:8px;padding:14px 18px;margin:12px 0 4px}}
.dsg-hall .t{{font-size:.68rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:{NAVY};margin-bottom:8px}}
.dsg-hall ol{{margin:0;padding-left:18px}} .dsg-hall li{{font-size:.86rem;color:{TX};margin:5px 0;line-height:1.45}}
.dsg-kpis{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border:1px solid #D9DCE1;border-radius:10px;background:#fff;overflow:hidden}}
.dsg-kpi{{padding:14px 18px;border-left:1px solid #E6E8EC}} .dsg-kpi:first-child{{border-left:none}}
.dsg-kpi .l{{font-size:.64rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{TX2}}}
.dsg-kpi .v{{font-family:{FUENTE};font-size:1.8rem;font-weight:700;color:{NAVY};letter-spacing:-.02em;margin-top:6px;line-height:1.1}}
.dsg-kpi .v small{{font-size:.78rem;font-weight:600;color:{TX2};margin-left:3px}}
.dsg-kpi .d{{font-size:.74rem;margin-top:5px;color:{TX2}}}
.dsg-up{{color:{UP};font-weight:700}} .dsg-dn{{color:{DN};font-weight:700}} .dsg-eq{{color:{TX2};font-weight:700}}
.dsg-mult{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}
.dsg-mc{{border:1px solid #D9DCE1;border-radius:10px;padding:14px 16px 10px;background:#fff}}
.dsg-mc .n{{font-size:.8rem;font-weight:700;color:{TX}}} .dsg-mc .big{{font-family:{FUENTE};font-size:1.5rem;font-weight:700;color:{NAVY};margin-top:4px}}
.dsg-mc .big small{{font-size:.75rem;color:{TX2};font-weight:600}} .dsg-mc .d{{font-size:.74rem;color:{TX2};margin-top:2px}}
.dsg-tw{{overflow-x:auto}}
table.dsg-rk{{width:100%;border-collapse:collapse;font-family:{FUENTE};font-size:.82rem}}
table.dsg-rk th{{text-align:right;font-size:.64rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:{TX2};
  padding:8px 10px;border-bottom:1px solid {TX2};white-space:normal;vertical-align:bottom}}
table.dsg-rk th.l{{text-align:left}}
table.dsg-rk td{{padding:7px 10px;border-bottom:1px solid #ECEDEF;text-align:right;color:{TX};white-space:nowrap}}
table.dsg-rk td.l{{text-align:left}} table.dsg-rk tr:hover td{{background:#F7F9FC}}
table.dsg-rk td.nom{{font-weight:600}} table.dsg-rk .barra{{display:inline-block;height:8px;background:{AZ};border-radius:2px;vertical-align:middle}}
table.dsg-rk .pos{{color:#898781;width:28px}}
.dsg-ficha{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px 18px;border:1px solid #D9DCE1;border-radius:10px;padding:14px 18px;background:#fff}}
.dsg-ficha .l{{font-size:.62rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{TX2}}}
.dsg-ficha .v{{font-size:.88rem;color:{TX};margin-top:2px;word-break:break-word}}
@media (max-width:900px){{.dsg-kpis{{grid-template-columns:repeat(2,1fr)}} .dsg-mult{{grid-template-columns:1fr}}
  .dsg-ficha{{grid-template-columns:repeat(2,1fr)}} .dsg-cab{{display:block}} .dsg-cab .meta{{text-align:left;margin-top:6px}}}}
</style>"""
_CFG = {"displayModeBar": False, "responsive": True}
_LAYOUT = dict(margin=dict(t=10, b=8, l=8, r=70), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
               font=dict(family=FUENTE, size=12, color=TX2), showlegend=False, dragmode=False, bargap=0.28,
               hoverlabel=dict(bgcolor="#fff", bordercolor=LN, font=dict(family=FUENTE, size=12, color=TX)))
_EJE_Y = dict(showgrid=True, gridcolor=LN, zeroline=False, tickfont=dict(size=11, color="#898781"), rangemode="tozero")
_EJE_X = dict(showgrid=False, zeroline=False, showline=True, linecolor="#C3C2B7", tickfont=dict(size=11.5, color=TX2),
              type="category")
_LEYENDA = dict(orientation="h", x=0, y=1.02, yanchor="bottom", font=dict(size=11, color=TX2))


def _md(s):
    st.markdown(s, unsafe_allow_html=True)


def fmt(n):
    return f"{n:,.0f}"


def var(a, b):
    return None if not b else (a / b - 1) * 100


def delta(v, pp=False, ref=""):
    if v is None:
        return '<span class="dsg-eq">—</span>'
    if abs(v) < 0.5:
        return f'<span class="dsg-eq">= sin cambio</span> {ref}'
    c, a = ("dsg-up", "▲") if v > 0 else ("dsg-dn", "▼")
    return f'<span class="{c}">{a} {abs(v):.0f}{" p.p." if pp else "%"}</span> {ref}'


def dif(a, b, umbral=10):
    """Con base chica un % engaña (2 → 23 = +1,050 %): se muestra en unidades."""
    if b == 0 and a == 0:
        return '<span class="dsg-eq">—</span>'
    if b < umbral:
        x = a - b
        if x == 0:
            return '<span class="dsg-eq">= igual</span>'
        return f'<span class="{"dsg-up" if x > 0 else "dsg-dn"}">{"▲" if x > 0 else "▼"} {abs(x):,.0f} unid.</span>'
    return delta(var(a, b))


def spark(vals, w=100, h=26, barras=False):
    vals = [float(x) for x in vals]
    if not vals:
        return ""
    hi = max(vals) or 1
    if barras:
        n = len(vals)
        bw = w / n
        r = "".join(f'<rect x="{i * bw + 1:.1f}" y="{h - (v / hi) * (h - 2):.1f}" width="{max(bw - 2, 1):.1f}" '
                    f'height="{(v / hi) * (h - 2):.1f}" fill="{AZ if i == n - 1 else (AZ2 if i == n - 2 else GR)}" rx="1"/>'
                    for i, v in enumerate(vals))
        return f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" style="vertical-align:middle">{r}</svg>'
    lo = min(vals)
    rg = (hi - lo) or 1
    paso = w / (len(vals) - 1 if len(vals) > 1 else 1)
    pts = [(i * paso, h - 3 - (v - lo) / rg * (h - 6)) for i, v in enumerate(vals)]
    x, y = pts[-1]
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" style="vertical-align:middle;overflow:visible">'
            f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in pts)}" fill="none" stroke="{AZ}" stroke-width="1.5"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" fill="{AZ}"/></svg>')


def pregunta(q, a):
    _md(f'<p class="dsg-q">{html.escape(q)}</p><p class="dsg-a">{a}</p>')


def pie(texto):
    _md(f'<div class="dsg-nota">{texto}</div>')


def seccion(t, sub):
    _md(f'<div class="dsg-sec">{t}</div><div class="dsg-ssub">{sub}</div>')


def _graf(fig, h, leyenda=False):
    lay = {**_LAYOUT, "height": h}
    if leyenda:
        lay.update(showlegend=True, legend=_LEYENDA)
    fig.update_layout(**lay)
    st.plotly_chart(fig, width="stretch", config=_CFG)


def ranking(filas, columnas, n_barra, ancho=110):
    """columnas = [(clave, título, clase)]; "#" = posición; "_barra" = barra de participación."""
    tope = max((f[n_barra] for f in filas), default=1) or 1
    th = "".join(f'<th class="{c}">{t}</th>' for _, t, c in columnas)
    trs = []
    for i, f in enumerate(filas, 1):
        tds = []
        for k, _, c in columnas:
            if k == "#":
                tds.append(f'<td class="pos l">{i}</td>')
            elif k == "_barra":
                tds.append(f'<td class="l"><span class="barra" style="width:{max(2, f[n_barra] / tope * ancho):.0f}px"></span></td>')
            else:
                tds.append(f'<td class="{c}">{f[k]}</td>')
        trs.append("<tr>" + "".join(tds) + "</tr>")
    _md(f'<div class="dsg-tw"><table class="dsg-rk"><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table></div>')


def kpis(cajas):
    _md('<div class="dsg-kpis">' + "".join(
        f'<div class="dsg-kpi"><div class="l">{l}</div><div class="v">{v}</div><div class="d">{d}</div></div>'
        for l, v, d in cajas) + "</div>")


# ============================== gráficas reutilizables ==============================
def barras_anio(F, df, valor=None, h=300):
    """Una barra por año: enero → mes de corte (sólido) + resto del año (claro)."""
    b = df.assign(_v=df[valor] if valor else 1)
    a = b[b.Mes <= F.corte].groupby("Anio")._v.sum().reindex(F.anios, fill_value=0)
    c = b[(b.Mes > F.corte) & (b.Anio < F.act)].groupby("Anio")._v.sum().reindex(F.anios, fill_value=0)
    x = [str(y) for y in F.anios[:-1]] + [f"{F.act}*"]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=x, y=a.values, marker=dict(color=[GR if y < F.ant else (AZ2 if y == F.ant else AZ) for y in F.anios],
                                                        cornerradius=3),
                         hovertemplate=f"%{{x}} · ene–{MES[F.corte]}: %{{y:,.0f}}<extra></extra>"))
    fig.add_trace(go.Bar(x=x, y=c.values, marker=dict(color=GR2, cornerradius=3, line=dict(color="#fff", width=1)),
                         hovertemplate=f"%{{x}} · {MES[min(F.corte + 1, 12)]}–dic: %{{y:,.0f}}<extra></extra>"))
    tot = (a + c).values
    fig.add_trace(go.Scatter(x=x, y=tot, mode="text", text=[fmt(t) for t in tot], textposition="top center",
                             textfont=dict(size=11, color=TX2), hoverinfo="skip"))
    fig.update_layout(barmode="stack")
    fig.update_xaxes(**_EJE_X)
    fig.update_yaxes(**_EJE_Y, range=[0, max(max(tot), 1) * 1.15])
    _graf(fig, h)


def lineas(series: dict, x, foco, h=300, sufijo="", titulo_y="", etiquetar_todo=None):
    """Serie foco en color (foco = {nombre: (color, grosor, desplazamiento_etiqueta)}), resto en gris;
    nombre al final de cada línea."""
    fig = go.Figure()
    orden = [k for k in series if k not in foco] + [k for k in series if k in foco]
    etiquetar_todo = len(series) <= 5 if etiquetar_todo is None else etiquetar_todo
    for k in orden:
        c, w, dy = (list(foco.get(k, (GR, 1.3))) + [0])[:3]
        ys = list(series[k])
        fig.add_trace(go.Scatter(x=x[:len(ys)], y=ys, mode="lines", line=dict(color=c, width=w), name=str(k),
                                 hovertemplate=f"{k} · %{{x}}: %{{y:,.0f}}{sufijo}<extra></extra>"))
        if ys and (k in foco or etiquetar_todo):
            fig.add_annotation(x=len(ys) - 1, y=ys[-1], text=f"<b>{k}</b>" if k in foco else str(k), showarrow=False,
                               xanchor="left", xshift=6, yshift=dy, font=dict(size=11.5, color=c if k in foco else "#898781"))
    fig.update_xaxes(**_EJE_X)
    fig.update_yaxes(**_EJE_Y, title=dict(text=titulo_y, font=dict(size=11, color="#898781")))
    _graf(fig, h)


def apiladas(x, series, colores, h=300, pct=False):
    fig = go.Figure()
    for (nombre, ys), c in zip(series.items(), colores):
        fig.add_trace(go.Bar(x=x, y=ys, name=nombre, marker=dict(color=c, line=dict(color="#fff", width=1)),
                             hovertemplate=f"{nombre}: %{{y:,.0f}}{' %' if pct else ''}<extra></extra>"))
    fig.update_layout(barmode="stack")
    fig.update_xaxes(**_EJE_X)
    fig.update_yaxes(**_EJE_Y, **({"range": [0, 100], "ticksuffix": " %"} if pct else {}))
    _graf(fig, h, leyenda=True)


# ============================== vistas ==============================
def _ytd(F, df, y):
    return df[(df.Anio == y) & (df.Mes <= F.corte)]


def _periodo(F):
    return f"ene–{MES[F.corte]}"


def _ref(F):
    return f"vs {_periodo(F)} {F.ant}"


def _mayor_cambio(serie_a: pd.Series, serie_b: pd.Series, n=2):
    d = (serie_b - serie_a).sort_values()
    return d[d > 0].iloc[::-1][:n], d[d < 0][:n]


def v_panorama(F):
    d25, d26 = len(_ytd(F, F.decl, F.ant)), len(_ytd(F, F.decl, F.act))
    u25, u26 = _ytd(F, F.eqn, F.ant).Cant.sum(), _ytd(F, F.eqn, F.act).Cant.sum()
    c25, c26 = len(_ytd(F, F.cont, F.ant)), len(_ytd(F, F.cont, F.act))
    a25 = (_ytd(F, F.decl, F.ant).Carga == "Aérea").mean() * 100 if d25 else 0
    a26 = (_ytd(F, F.decl, F.act).Carga == "Aérea").mean() * 100 if d26 else 0
    hist = F.decl[F.decl.Anio.isin(F.cerrados) & (F.decl.Mes <= F.corte)].groupby("Anio").size()
    prom = hist.mean() if len(hist) else 0
    kpis([
        (f"Declaraciones {F.act}", f"{d26}<small>a {MES[F.corte]}</small>", delta(var(d26, d25), ref=_ref(F))),
        (f"Unidades de equipo {F.act}", f"{u26:,.0f}<small>unid.</small>", delta(var(u26, u25), ref=_ref(F))),
        (f"Contenedores {F.act}", f"{c26}", delta(var(c26, c25), ref=_ref(F))),
        ("Declaraciones aéreas", f"{a26:.0f}<small>%</small>", delta(a26 - a25, pp=True, ref=_ref(F))),
        ("Ritmo vs. histórico", f"{(var(d26, prom) or 0):+.0f}<small>%</small>",
         f"frente al promedio {_periodo(F)} {F.anios[0]}–{F.ant} ({prom:.0f})"),
    ])
    # hallazgos automáticos
    total = F.decl[F.decl.Anio.isin(F.cerrados)].groupby("Anio").size()
    pico = int(total.idxmax()) if len(total) else F.ant
    rep = F.cats[F.cats.Cat == "Repuestos"].drop_duplicates(["i"]).groupby("Anio").size()
    equ = F.cats[F.cats.Cat == "Equipos"].drop_duplicates(["i"]).groupby("Anio").size()
    f25 = _ytd(F, F.eqn, F.ant).groupby("Familia").Cant.sum().reindex(FAMS, fill_value=0)
    f26 = _ytd(F, F.eqn, F.act).groupby("Familia").Cant.sum().reindex(FAMS, fill_value=0)
    suben, bajan = _mayor_cambio(f25, f26)
    a_ini = (F.decl[F.decl.Anio == F.anios[0]].Carga == "Aérea").mean() * 100
    h = [f"<b>{F.act} frente a {F.ant}:</b> {d26} declaraciones de enero a {MES_LARGO[F.corte]} contra {d25} "
         f"({(var(d26, d25) or 0):+.0f} %), y {abs(var(d26, prom) or 0):.0f} % "
         f"{'por debajo' if d26 < prom else 'por encima'} del promedio de ese período. El año completo más alto fue "
         f"{pico} ({fmt(total.get(pico, 0))} declaraciones)."]
    if len(rep) and len(equ):
        h.append(f"<b>Mezcla:</b> las declaraciones con repuestos pasaron de {fmt(rep.get(pico, 0))} en {pico} a "
                 f"{fmt(rep.get(F.ant, 0))} en {F.ant}; las de equipos, de {fmt(equ.get(pico, 0))} a {fmt(equ.get(F.ant, 0))}. "
                 f"Las aéreas eran el {a_ini:.0f} % en {F.anios[0]} y son el {a26:.0f} % en {F.act}.")
    if len(suben) or len(bajan):
        partes = []
        if len(suben):
            partes.append("<b>Crecen en unidades:</b> " + ", ".join(f"{k} (+{v:,.0f})" for k, v in suben.items()))
        if len(bajan):
            partes.append("<b>Caen:</b> " + ", ".join(f"{k} ({v:,.0f})" for k, v in bajan.items()))
        h.append(". ".join(partes) + f" ({_periodo(F)} {F.act} vs. {F.ant}).")
    full = F.eqn[F.eqn.Anio.isin(F.cerrados)].groupby(["Familia", "Anio"]).Cant.sum().groupby("Familia").max()
    record = [f for f in FAMS if f26.get(f, 0) > full.get(f, 0) > 0]
    if record:
        h.append(f"<b>Récord:</b> {', '.join(record)} ya supera{'n' if len(record) > 1 else ''} en {F.act} "
                 f"su mejor año completo, con datos solo hasta {MES_LARGO[F.corte]}.")
    _md('<div class="dsg-hall"><div class="t">Lo más relevante</div><ol>' + "".join(f"<li>{x}</li>" for x in h) + "</ol></div>")

    seccion("Volumen", "Declaraciones presentadas")
    a, b = st.columns([1.15, 1])
    with a, st.container(border=True):
        pregunta("¿Importamos más o menos que antes?",
                 f"<b>{F.act} lleva {d26}</b> declaraciones a {MES_LARGO[F.corte]}; {F.ant} tenía {d25} a la misma fecha. "
                 f"El año más alto fue {pico}.")
        barras_anio(F, F.decl)
        pie(f"Parte oscura: enero–{MES_LARGO[F.corte]}, comparable con {F.act}*. Parte clara: resto del año. "
            f"{F.act}*: datos hasta {MES_LARGO[F.corte]}.")
    with b, st.container(border=True):
        acum = F.decl[F.decl.Mes <= F.corte].pivot_table(index="Mes", columns="Anio", values="i", aggfunc="count",
                                                          fill_value=0).reindex(range(1, F.corte + 1), fill_value=0).cumsum()
        series = {str(y): acum[y].values for y in F.cerrados if y != F.ant and y in acum}
        for y in (F.ant, F.act):
            if y in acum:
                series[str(y)] = acum[y].values
        rk = acum.iloc[-1].sort_values(ascending=False) if len(acum) else pd.Series(dtype=float)
        pos = list(rk.index).index(F.act) + 1 if F.act in rk.index else 0
        pregunta(f"¿Cómo va {F.act} frente a otros años?",
                 f"A {MES_LARGO[F.corte]}, {F.act} es el <b>{pos}.º de {len(rk)}</b> años en declaraciones acumuladas "
                 f"({(var(d26, d25) or 0):+.0f} % frente a {F.ant}).")
        lineas(series, [MES[m] for m in acum.index], {str(F.act): (AZ, 3, -8), str(F.ant): (TX2, 2, 8)},
               titulo_y="Declaraciones acumuladas", etiquetar_todo=False)
        pie(f"Cada línea gris es un año entre {F.anios[0]} y {F.ant - 1}.")
    seccion("Composición", "Qué traen las declaraciones y quién las presenta")
    a, b = st.columns(2)
    with a, st.container(border=True):
        tab = F.cats.drop_duplicates(["i", "Cat"]).pivot_table(index="Anio", columns="Cat", values="i", aggfunc="count",
                                                              fill_value=0).reindex(F.cerrados, fill_value=0)
        for c in ("Repuestos", "Equipos", "Otros"):
            if c not in tab:
                tab[c] = 0
        pregunta("¿Traemos más equipos o más repuestos?",
                 f"Declaraciones con repuestos en {F.ant}: <b>{fmt(tab['Repuestos'].iloc[-1])}</b> "
                 f"(máximo {fmt(tab['Repuestos'].max())} en {tab['Repuestos'].idxmax()}); con equipos: "
                 f"<b>{fmt(tab['Equipos'].iloc[-1])}</b> (máximo {fmt(tab['Equipos'].max())} en {tab['Equipos'].idxmax()}).")
        lineas({c: tab[c].values for c in ["Repuestos", "Equipos", "Otros"]}, [str(y) for y in F.cerrados],
               {"Repuestos": (AZ, 2.5), "Equipos": (NAR, 2.5)}, titulo_y="Declaraciones por año")
        pie(f"Años completos {F.anios[0]}–{F.ant}. Una declaración puede traer equipos y repuestos a la vez.")
    with b, st.container(border=True):
        e25 = _ytd(F, F.decl, F.ant).Empresa.value_counts()
        e26 = _ytd(F, F.decl, F.act).Empresa.value_counts()
        lider = e26.idxmax() if len(e26) else "—"
        pregunta("¿Qué empresa del Grupo importa más?",
                 f"{lider} presenta <b>{(e26.max() / d26 if d26 else 0):.0%}</b> de las declaraciones de {F.act}.")
        filas = []
        for emp in [e for e in EMPRESAS if e in set(F.decl.Empresa)]:
            serie = F.decl[F.decl.Empresa == emp].groupby("Anio").size().reindex(F.cerrados, fill_value=0)
            filas.append({"e": emp, "n": e26.get(emp, 0), "sh": f"{(e26.get(emp, 0) / d26 if d26 else 0):.0%}",
                          "a": fmt(e25.get(emp, 0)), "b": f"<b>{fmt(e26.get(emp, 0))}</b>",
                          "d": delta(var(e26.get(emp, 0), e25.get(emp, 0))), "t": spark(serie.values, w=90)})
        ranking(filas, [("e", "Empresa", "l nom"), ("_barra", f"Part. {F.act}", "l"), ("sh", "", ""),
                        ("a", f"{_periodo(F)} {F.ant}", ""), ("b", f"{_periodo(F)} {F.act}", ""), ("d", "Variación", ""),
                        ("t", f"{F.anios[0]}–{F.ant}", "")], "n", ancho=70)


def v_que(F):
    seccion("Las seis categorías de equipo",
            f"Unidades declaradas por año · azul = {F.act} ({_periodo(F)}), celeste = {F.ant}, gris = años anteriores")
    cards = []
    for f in FAMS:
        g = F.eqn[F.eqn.Familia == f]
        s = g.groupby("Anio").Cant.sum().reindex(F.anios, fill_value=0)
        a25, a26 = _ytd(F, g, F.ant).Cant.sum(), _ytd(F, g, F.act).Cant.sum()
        cards.append(f'<div class="dsg-mc"><div class="n">{f}</div><div class="big">{a26:,.0f} <small>unid. en {F.act}</small></div>'
                     f'<div class="d">{delta(var(a26, a25), ref=_ref(F))}</div><div style="margin-top:8px">'
                     f'{spark(s.values, w=340, h=54, barras=True)}</div><div class="dsg-nota" style="display:flex;'
                     f'justify-content:space-between"><span>{F.anios[0]}</span><span>Total {F.anios[0]}–{F.ant}: '
                     f'{s[F.cerrados].sum():,.0f}</span><span>{F.act}</span></div></div>')
    _md(f'<div class="dsg-mult">{"".join(cards)}</div>')
    sol = F.eq[F.eq.Tipo == "Paneles solares"].groupby("Anio").Cant.sum().reindex(F.anios, fill_value=0)
    if sol.sum():
        inv = F.eq[F.eq.Tipo == "Inversores"].Cant.sum()
        _md(f'<div class="dsg-hall" style="border-left-color:{AMB}"><div class="t" style="color:#8A5A00">Energía solar · '
            f'se muestra aparte</div><div style="display:flex;gap:28px;align-items:center;flex-wrap:wrap">'
            f'<div style="font-size:.86rem;color:{TX}"><b>{sol.sum():,.0f} paneles solares</b> y {inv:,.0f} inversores. '
            f'Año con más paneles: {sol.idxmax()} ({sol.max():,.0f}). Sumados con el resto taparían las demás categorías.'
            f'</div>{spark(sol.values, w=260, h=44, barras=True)}</div></div>')
    seccion("Ranking de tipos de equipo", f"Ordenado por unidades de {_periodo(F)} {F.act} · sin paneles solares ni inversores")
    with st.container(border=True):
        filas = []
        for t, g in F.eqn.groupby("Tipo"):
            a25, a26 = _ytd(F, g, F.ant).Cant.sum(), _ytd(F, g, F.act).Cant.sum()
            s = g.groupby("Anio").Cant.sum().reindex(F.cerrados, fill_value=0)
            filas.append({"Tipo": t, "Cat": g.Familia.iloc[0], "n": a26, "a25": a25, "a": fmt(a25), "b": f"<b>{fmt(a26)}</b>",
                          "d": dif(a26, a25), "t": spark(s.values), "h": fmt(s.sum()),
                          "mx": f"{s.idxmax()} ({fmt(s.max())})" if s.max() else "—"})
        cambio = sorted(filas, key=lambda f: f["n"] - f["a25"])
        suben = [f["Tipo"].lower() for f in cambio[::-1][:3] if f["n"] > f["a25"]]
        bajan = [f["Tipo"].lower() for f in cambio[:2] if f["n"] < f["a25"]]
        pregunta("¿Qué equipos estamos trayendo más este año y cuáles se frenaron?",
                 (f"Más unidades que en {F.ant}: {', '.join(suben)}. " if suben else "")
                 + (f"Menos: {', '.join(bajan)}." if bajan else ""))
        filas = sorted(filas, key=lambda f: (-f["n"], f["Tipo"]))[:15]
        ranking(filas, [("#", "#", "l"), ("Tipo", "Tipo de equipo", "l nom"), ("Cat", "Categoría", "l"),
                        ("_barra", f"{_periodo(F)} {F.act}", "l"), ("b", "", ""), ("a", f"{_periodo(F)} {F.ant}", ""),
                        ("d", "Variación", ""), ("t", f"Tendencia {F.anios[0]}–{F.ant}", ""),
                        ("h", f"Total {F.anios[0]}–{F.ant}", ""), ("mx", "Mejor año", "")], "n")


def _tabla_participacion(F, g, campo, titulo, sin=None):
    gg = g[g[campo] != sin] if sin else g
    tot = gg.Cant.sum() or 1
    filas = []
    for k, x in gg.groupby(campo):
        filas.append({"k": k, "n": x.Cant.sum(), "t": f"{x.Cant.sum():,.0f}", "sh": f"{x.Cant.sum() / tot:.0%}",
                      "b": fmt(_ytd(F, x, F.act).Cant.sum()),
                      "s": spark(x.groupby("Anio").Cant.sum().reindex(F.cerrados, fill_value=0).values, w=90)})
    filas = sorted(filas, key=lambda f: -f["n"])[:6]
    ranking(filas, [("k", titulo, "l nom"), ("_barra", f"{F.anios[0]}–{F.act}", "l"), ("t", "", ""), ("sh", "Part.", ""),
                    (("b", f"{F.act}", "")), ("s", "Tendencia", "")], "n", ancho=90)
    return filas


def v_categoria(F):
    disponibles = [f for f in FAMS if f in set(F.eqn.Familia)] or FAMS
    cat = st.selectbox("Categoría a analizar", disponibles, key="dsg_cat")
    g = F.eqn[F.eqn.Familia == cat]
    s = g.groupby("Anio").Cant.sum().reindex(F.anios, fill_value=0)
    a25, a26 = _ytd(F, g, F.ant).Cant.sum(), _ytd(F, g, F.act).Cant.sum()
    mejor = s[F.cerrados].idxmax() if s[F.cerrados].sum() else F.ant
    con_marca = g[g.Marca != "Sin marca en la descripción"]
    mk = con_marca.groupby("Marca").Cant.sum().sort_values(ascending=False)
    pa = g.groupby("Pais").Cant.sum().sort_values(ascending=False)
    kpis([
        (f"Unidades {F.act}", f"{a26:,.0f}<small>a {MES[F.corte]}</small>", delta(var(a26, a25), ref=_ref(F))),
        ("Mejor año completo", f"{mejor}", f"{s[mejor]:,.0f} unidades" + (f" · {F.act} ya lo supera" if a26 > s[mejor] else "")),
        (f"Unidades {F.anios[0]}–{F.ant}", f"{s[F.cerrados].sum():,.0f}", f"promedio {s[F.cerrados].mean():.0f} por año"),
        ("Marca principal", f'<span style="font-size:1.35rem">{html.escape(mk.index[0]) if len(mk) else "—"}</span>',
         f"{(mk.iloc[0] / mk.sum() if len(mk) else 0):.0%} de las unidades con marca"),
        ("Origen principal", f'<span style="font-size:1.35rem">{html.escape(pa.index[0]) if len(pa) else "—"}</span>',
         f"{(pa.iloc[0] / pa.sum() if len(pa) else 0):.0%} de las unidades"),
    ])
    tipos = g.groupby("Tipo").Cant.sum().sort_values(ascending=False)
    seccion(cat, " · ".join(tipos.index[:7]))
    a, b = st.columns([1.2, 1])
    with a, st.container(border=True):
        pregunta("¿Cómo ha evolucionado la categoría?",
                 f"Mejor año completo: <b>{mejor}</b> ({s[mejor]:,.0f} unidades). {F.act} lleva <b>{a26:,.0f}</b> a "
                 f"{MES_LARGO[F.corte]} contra {a25:,.0f} de {F.ant} a la misma fecha.")
        barras_anio(F, g, "Cant")
        pie(f"Parte oscura: enero–{MES_LARGO[F.corte]}. Parte clara: resto del año. {F.act}*: datos hasta {MES_LARGO[F.corte]}.")
    with b, st.container(border=True):
        tp = g.pivot_table(index="Anio", columns="Tipo", values="Cant", aggfunc="sum", fill_value=0).reindex(F.anios, fill_value=0)
        top = list(tipos.index[:3])
        resto = tp.drop(columns=top).sum(axis=1) if len(tipos) > 3 else None
        pregunta("¿Qué tipos de equipo la componen?",
                 f"<b>{top[0] if top else '—'}</b> es el {(tipos.iloc[0] / tipos.sum() if len(tipos) else 0):.0%} de las "
                 f"unidades desde {F.anios[0]}.")
        series = {t: tp[t].values for t in top}
        if resto is not None:
            series["Otros tipos"] = resto.values
        apiladas([str(y) if y < F.act else f"{y}*" for y in F.anios], series, [AZ, NAR, VER, GR][:len(top)] + [GR])
        pie(f"{F.act}*: enero a {MES_LARGO[F.corte]}.")
    a, b = st.columns(2)
    with a, st.container(border=True):
        pregunta("¿De qué marcas?", f"{mk.index[0]} concentra el {(mk.iloc[0] / mk.sum()):.0%} de las unidades con marca."
                 if len(mk) else "Las descripciones no traen marca.")
        _tabla_participacion(F, g, "Marca", "Marca", sin="Sin marca en la descripción")
        sinm = g[g.Marca == "Sin marca en la descripción"].Cant.sum()
        if sinm:
            pie(f"{sinm:,.0f} unidades no traen la marca en la descripción de la declaración.")
    with b, st.container(border=True):
        pregunta("¿De dónde vienen?", f"{pa.index[0]} y {pa.index[1]} suman el {(pa.iloc[:2].sum() / pa.sum()):.0%}."
                 if len(pa) > 1 else "")
        _tabla_participacion(F, g, "Pais", "País")


def v_origen(F):
    seccion("Países de origen", "Declaraciones con mercancía de cada país (equipos, repuestos y otros)")
    dp = F.cats.drop_duplicates(["i", "Pais"])
    a, b = st.columns([1.25, 1])
    with a, st.container(border=True):
        tot26 = _ytd(F, dp, F.act).i.nunique() or 1
        filas = []
        for p, x in dp.groupby("Pais"):
            a25, a26 = len(_ytd(F, x, F.ant)), len(_ytd(F, x, F.act))
            s = x.groupby("Anio").size().reindex(F.cerrados, fill_value=0)
            filas.append({"p": p, "n": a26, "b": f"<b>{a26}</b>", "sh": f"{a26 / tot26:.0%}", "a": a25,
                          "d": dif(a26, a25), "t": spark(s.values, w=90), "h": fmt(s.sum())})
        filas = sorted(filas, key=lambda f: -f["n"])[:10]
        pregunta("¿De dónde importamos?",
                 f"En {F.act}, <b>{filas[0]['p']}</b> aparece en el {filas[0]['sh']} de las declaraciones; luego "
                 f"{filas[1]['p']} ({filas[1]['sh']}) y {filas[2]['p']} ({filas[2]['sh']})." if len(filas) > 2 else "")
        ranking(filas, [("#", "#", "l"), ("p", "País", "l nom"), ("_barra", f"{_periodo(F)} {F.act}", "l"), ("b", "", ""),
                        ("sh", "Part.", ""), ("a", f"{_periodo(F)} {F.ant}", ""), ("d", "Variación", ""),
                        ("t", f"{F.anios[0]}–{F.ant}", ""), ("h", "Total", "")], "n", ancho=90)
        pie("Part. = % de las declaraciones del período con mercancía de ese país (una declaración puede tener varios).")
    with b, st.container(border=True):
        top = dp.Pais.value_counts().head(4).index.tolist()
        base = dp[dp.Anio.isin(F.cerrados)]
        tab = base.pivot_table(index="Anio", columns="Pais", values="i", aggfunc="count", fill_value=0)
        sh = tab.div(base.groupby("Anio").i.nunique(), axis=0).reindex(F.cerrados).fillna(0) * 100
        y0, y1 = F.cerrados[0], F.cerrados[-1]
        cambios = sorted(top, key=lambda p: sh.loc[y1, p] - sh.loc[y0, p]) if len(top) else []
        txt = ""
        if len(cambios) >= 2:
            sube, baja = cambios[-1], cambios[0]
            txt = (f"<b>{baja}</b> pasó de {sh.loc[y0, baja]:.0f} % a {sh.loc[y1, baja]:.0f} % de las declaraciones; "
                   f"<b>{sube}</b>, de {sh.loc[y0, sube]:.0f} % a {sh.loc[y1, sube]:.0f} % ({y0} → {y1}).")
        pregunta("¿Está cambiando el origen?", txt)
        foco = {}
        if len(cambios) >= 2:
            orden = sorted([cambios[0], cambios[-1]], key=lambda p: -sh.loc[y1, p])
            foco = {orden[0]: (AZ, 2.5, 9), orden[1]: (NAR, 2.5, -9)}
        lineas({p: sh[p].values for p in top}, [str(y) for y in F.cerrados], foco, sufijo=" %",
               titulo_y="% de las declaraciones del año")
        pie("Años completos.")
    seccion("Marcas", "Unidades de equipo por marca leída en la descripción · sin paneles solares ni inversores")
    with st.container(border=True):
        sm = F.eqn[F.eqn.Marca != "Sin marca en la descripción"]
        tot = sm.Cant.sum() or 1
        filas = []
        for m, x in sm.groupby("Marca"):
            s = x.groupby("Anio").Cant.sum().reindex(F.cerrados, fill_value=0)
            filas.append({"m": m, "cat": x.groupby("Familia").Cant.sum().idxmax(), "n": x.Cant.sum(),
                          "t": f"<b>{x.Cant.sum():,.0f}</b>", "sh": f"{x.Cant.sum() / tot:.0%}",
                          "a": fmt(_ytd(F, x, F.ant).Cant.sum()), "b": fmt(_ytd(F, x, F.act).Cant.sum()),
                          "s": spark(s.values), "u": str(x.Anio.max())})
        filas = sorted(filas, key=lambda f: -f["n"])[:10]
        pregunta("¿Qué marcas mueven el volumen?",
                 ", ".join(f"{f['m']} ({f['sh']})" for f in filas[:4]) + " son las cuatro principales." if filas else "")
        ranking(filas, [("#", "#", "l"), ("m", "Marca", "l nom"), ("cat", "Categoría principal", "l"),
                        ("_barra", f"Unidades {F.anios[0]}–{F.act}", "l"), ("t", "", ""), ("sh", "Part.", ""),
                        ("a", f"{_periodo(F)} {F.ant}", ""), ("b", f"{_periodo(F)} {F.act}", ""),
                        ("s", f"Tendencia {F.anios[0]}–{F.ant}", ""), ("u", "Última compra", "")], "n")
        sinm = F.eqn[F.eqn.Marca == "Sin marca en la descripción"].Cant.sum()
        pie(f"{sinm:,.0f} unidades de equipo no traen la marca en la descripción y no entran en este ranking.")


def v_logistica(F):
    seccion("Cómo llega la mercancía", "Tipo de carga y administración de entrada")
    a, b = st.columns(2)
    with a, st.container(border=True):
        tab = F.decl.pivot_table(index="Anio", columns="Carga", values="i", aggfunc="count", fill_value=0).reindex(F.anios, fill_value=0)
        shr = tab.div(tab.sum(axis=1).replace(0, 1), axis=0) * 100
        orden = [c for c in ("Aérea", "En contenedor", "Centro logístico", "Ro-Ro", "Carga suelta") if c in shr]
        ae = lambda y: shr.loc[y, "Aérea"] if "Aérea" in shr else 0  # noqa: E731
        pregunta("¿Avión, contenedor o Ro-Ro?",
                 f"Las aéreas eran el <b>{ae(F.anios[0]):.0f} %</b> de las declaraciones en {F.anios[0]} y son el "
                 f"<b>{ae(F.act):.0f} %</b> en {F.act}.")
        apiladas([str(y) if y < F.act else f"{y}*" for y in F.anios], {c: shr[c].values for c in orden},
                 [NAR, AZ, "#7A5AF8", VER, AMB][:len(orden)], pct=True)
        pie(f"Porcentaje de las declaraciones de cada año. {F.act}*: hasta {MES_LARGO[F.corte]}. Centro logístico = despacho "
            "desde un centro logístico.")
    with b, st.container(border=True):
        ad = F.decl[F.decl.Anio.isin(F.cerrados)].pivot_table(index="Anio", columns="Adm", values="i", aggfunc="count",
                                                              fill_value=0).reindex(F.cerrados, fill_value=0)
        sa = ad.div(ad.sum(axis=1).replace(0, 1), axis=0) * 100
        y0, y1 = F.cerrados[0], F.cerrados[-1]
        cambio = (sa.loc[y1] - sa.loc[y0]).sort_values()
        sube, baja = cambio.index[-1], cambio.index[0]
        pregunta("¿Por qué aduana entra?",
                 f"<b>{sube}</b> pasó de {sa.loc[y0, sube]:.0f} % a {sa.loc[y1, sube]:.0f} % de las declaraciones y "
                 f"<b>{baja}</b> de {sa.loc[y0, baja]:.0f} % a {sa.loc[y1, baja]:.0f} % ({y0} → {y1}).")
        lineas({k: sa[k].values for k in sa.columns}, [str(y) for y in F.cerrados],
               {sube: (AZ, 2.5), baja: (NAR, 2.5)}, sufijo=" %", titulo_y="% de las declaraciones")
        pie("Años completos.")
    seccion("Cuándo llega", "Declaraciones por mes · cada celda es un mes de un año")
    with st.container(border=True):
        hm = F.decl.pivot_table(index="Anio", columns="Mes", values="i", aggfunc="count").reindex(index=F.anios, columns=range(1, 13))
        est = F.decl[F.decl.Anio.isin(F.cerrados)].groupby("Mes").size().reindex(range(1, 13), fill_value=0) / max(len(F.cerrados), 1)
        alto, bajo = est.sort_values(ascending=False).index[:2], est.sort_values().index[:2]
        pregunta("¿Hay meses de más carga?",
                 f"Los más cargados son <b>{MES_LARGO[alto[0]]} y {MES_LARGO[alto[1]]}</b> ({est[alto[0]]:.0f} y "
                 f"{est[alto[1]]:.0f} declaraciones en promedio); los más bajos, {MES_LARGO[bajo[0]]} y {MES_LARGO[bajo[1]]} "
                 f"({est[bajo[0]]:.0f} y {est[bajo[1]]:.0f}). Sirve para planificar personal y espacio en almacén.")
        z = hm.values
        tope = pd.Series(z.ravel()).max() or 1
        fig = go.Figure(go.Heatmap(z=z, x=list(range(12)), y=list(range(len(F.anios))), customdata=[[str(y)] * 12 for y in F.anios],
                                   colorscale=[[0, "#F4F8FD"], [0.4, "#9EC5F4"], [0.75, "#3987E5"], [1, "#184F95"]],
                                   showscale=False, xgap=2, ygap=2, text=[[MES[m] for m in range(1, 13)]] * len(F.anios),
                                   hovertemplate="%{text} %{customdata}: %{z} declaraciones<extra></extra>"))
        for i in range(len(F.anios)):
            for j in range(12):
                v = z[i, j]
                if pd.notna(v):
                    fig.add_annotation(x=j, y=i, text=f"{v:.0f}", showarrow=False,
                                       font=dict(size=10.5, color="#fff" if v / tope > 0.55 else TX))
        fig.update_xaxes(tickvals=list(range(12)), ticktext=[m.capitalize() for m in MES[1:]], side="top", showgrid=False,
                         tickfont=dict(size=11.5, color=TX2))
        fig.update_yaxes(autorange="reversed", showgrid=False, tickfont=dict(size=11.5, color=TX2),
                         tickvals=list(range(len(F.anios))), ticktext=[str(y) for y in F.anios])
        fig.update_layout(**{**_LAYOUT, "margin": dict(t=26, b=8, l=8, r=8)}, height=34 * len(F.anios) + 40)
        st.plotly_chart(fig, width="stretch", config=_CFG)
        pie("Celda vacía = mes sin datos todavía.")
    seccion("Contenedores", "Contenedores declarados por tamaño")
    with st.container(border=True):
        ct = F.cont.pivot_table(index="Anio", columns="Tam", values="i", aggfunc="count", fill_value=0).reindex(F.anios, fill_value=0)
        orden = [c for c in ("20'", "40'", "40' HC", "45'", "45' HC") if c in ct] + [c for c in ct if c not in
                                                                               ("20'", "40'", "40' HC", "45'", "45' HC")]
        c25, c26 = len(_ytd(F, F.cont, F.ant)), len(_ytd(F, F.cont, F.act))
        pregunta("¿Cuántos contenedores traemos y de qué tamaño?",
                 f"{F.act} lleva <b>{c26}</b> contenedores a {MES_LARGO[F.corte]} ({c25} en {F.ant} a la misma fecha). "
                 f"El {(ct[[c for c in orden if c.startswith('40')]].sum().sum() / max(ct.values.sum(), 1)):.0%} son de 40 pies.")
        apiladas([str(y) if y < F.act else f"{y}*" for y in F.anios], {c: ct[c].values for c in orden},
                 [GR, AZ, NAVY, NAR, AMB, VER][:len(orden)], h=280)
        pie("HC = high cube. Contenedores tal como aparecen en la sección Furgones de cada liquidación.")


# ============================== buscador ==============================
_LIMITE_TABLA = 3000


def _norm_id(s: str) -> str:
    return re.sub(r"[\s\-./]", "", _sin_acentos(s))


@st.cache_data(ttl=TTL_HISTORICO, show_spinner=False)
def _indices(_clave: str) -> dict:
    """Número de declaración, BL, contenedor y chasis normalizados (sin guiones ni espacios)."""
    P = _preparar(_clave)
    d, c, v = P["decl"], P["cont"], P["veh"]
    return {"decl": pd.Series(d["i"].map(_norm_id).values, index=d["i"].values),
            "bl": pd.Series(d["BL"].astype(str).map(_norm_id).values, index=d["i"].values),
            "cont": pd.Series(c["Contenedor"].astype(str).map(_norm_id).values, index=c["i"].values),
            "chasis": pd.Series(v["Chasis"].astype(str).map(_norm_id).values, index=v["i"].values)}


def _ficha(d: pd.Series):
    def campo(etq, col, f=lambda x: x):
        v = d.get(col, "")
        v = "" if pd.isna(v) else str(v).strip()
        return f'<div><div class="l">{etq}</div><div class="v">{html.escape(f(v)) if v else "—"}</div></div>'
    _md('<div class="dsg-ficha">' + "".join([
        campo("Declaración", "Declaracion"), campo("Empresa", "Empresa"), campo("Fecha de declaración", "Fecha_Declaracion"),
        campo("Llegada", "Fecha_Llegada"), campo("Administración", "Administracion"), campo("Tipo de carga", "Tipo_Carga"),
        campo("Despacho", "Despacho"), campo("BL / guía", "BL"), campo("Manifiesto", "Manifiesto"),
        campo("Consignatario", "Consignatario"), campo("Agencia", "Agencia"),
        campo("Peso bruto (kg)", "Peso_Bruto_KG", lambda x: f"{float(x):,.2f}" if re.fullmatch(r"[\d.]+", x) else x),
    ]) + "</div>")
    det = str(d.get("Detalle_Manifiesto", "") or "").strip()
    if det:
        pie(f"Detalle del manifiesto: {html.escape(det)}")


def v_buscar(F, clave):
    _md('<div class="dsg-a">Busca una declaración por su número, BL, contenedor o chasis, o filtra los ítems por partida, '
        'país de origen y descripción. Los filtros se combinan.</div>')
    q = st.text_input("Número de declaración, BL, contenedor o chasis", key="dsg_q",
                      placeholder="10030-IC01-2507-…   ·   MSCU1234567   ·   1T0310LX…")
    c1, c2, c3, c4 = st.columns([1, 1.4, 1.6, 1])
    partida = c1.text_input("Partida o capítulo", key="dsg_partida", placeholder="8428.10 · 8428 · 84")
    paises = c2.multiselect("País de origen", sorted(F.items.Pais.unique()), key="dsg_paises", placeholder="Todos")
    texto = c3.text_input("La descripción contiene", key="dsg_desc", placeholder="montacargas yale · filtro aceite")
    tipo = c4.selectbox("Qué trae", ["Todo", "Equipos", "Repuestos", "Otros"], key="dsg_cat_b")
    it = F.items
    activo = False
    if q.strip():
        activo = True
        x = _norm_id(q)
        idx = _indices(clave)
        hits = set()
        for serie in idx.values():
            hits |= set(serie.index[serie.str.contains(re.escape(x), regex=True, na=False)])
        it = it[it.i.isin(hits)]
    if partida.strip():
        activo = True
        p = re.sub(r"[^\d]", "", partida)
        it = it[it.Partida.str.replace(".", "", regex=False).str.startswith(p)]
    if paises:
        activo = True
        it = it[it.Pais.isin(paises)]
    if texto.strip():
        activo = True
        for w in _sin_acentos(texto).split():
            it = it[it._desc.str.contains(re.escape(w), regex=True, na=False)]
    if tipo != "Todo":
        activo = True
        it = it[it.Cat == tipo]
    if not activo:
        pie(f"{len(F.decl):,} declaraciones y {len(F.items):,} ítems disponibles con la empresa y el período elegidos arriba.")
        return
    if it.empty:
        st.info("No hay ítems con esos criterios. Prueba con menos filtros o revisa la empresa y el período de arriba.")
        return
    n_decl = it.i.nunique()
    unidades = it[it.Unidad.astype(str).str.startswith("Unid")].Cant.sum()
    kpis([("Declaraciones", fmt(n_decl), f"de {it.Anio.min()} a {it.Anio.max()}"),
          ("Ítems", fmt(len(it)), "líneas de declaración"),
          ("Unidades", fmt(unidades), "solo ítems medidos en unidades"),
          ("Países de origen", fmt(it.Pais.nunique()), it.Pais.value_counts().index[0]),
          ("Partidas distintas", fmt(it.Partida.nunique()), it.Partida.value_counts().index[0])])
    fechas = F.decl.set_index("i")["Fecha_Declaracion"]
    tabla = it.assign(Fecha=it.i.map(fechas)).sort_values(["Fecha", "i", "Item"], ascending=[False, True, True])
    vista = tabla[["i", "Fecha", "Empresa", "Adm", "Item", "Partida", "Descripcion", "Pais", "Cant", "Unidad", "Tipo"]].rename(
        columns={"i": "Declaración", "Adm": "Administración", "Item": "Ítem", "Descripcion": "Descripción",
                 "Pais": "País", "Cant": "Cantidad", "Tipo": "Tipo de equipo"})
    st.dataframe(vista.head(_LIMITE_TABLA), hide_index=True, width="stretch", height=420,
                 column_config={"Cantidad": st.column_config.NumberColumn(format="%.2f"),
                                "Descripción": st.column_config.TextColumn(width="large")})
    extra = f" Se muestran las primeras {_LIMITE_TABLA:,}; la descarga trae todas." if len(vista) > _LIMITE_TABLA else ""
    pie(f"{len(vista):,} ítems ordenados del más reciente al más antiguo.{extra}")
    st.download_button("Descargar resultado (CSV)", vista.to_csv(index=False).encode("utf-8-sig"),
                       file_name="busqueda_declaraciones.csv", mime="text/csv", key="dsg_csv")

    seccion("Ver una declaración", "Encabezado, ítems, contenedores y vehículos tal como están en la liquidación (sin montos)")
    opciones = list(dict.fromkeys(tabla.i))
    elegido = st.selectbox("Declaración", opciones, key="dsg_ver",
                           format_func=lambda d: f"{d} · {fechas.get(d, '')} · {tabla[tabla.i == d].Empresa.iloc[0]}")
    if elegido:
        d = F.decl[F.decl.i == elegido].iloc[0]
        _ficha(d)
        todos = F.items[F.items.i == elegido].sort_values("Item")
        st.dataframe(todos[["Item", "Partida", "Descripcion", "Pais", "Cant", "Unidad", "Tipo", "Marca"]].rename(
            columns={"Item": "Ítem", "Descripcion": "Descripción", "Pais": "País", "Cant": "Cantidad",
                     "Tipo": "Tipo de equipo"}), hide_index=True, width="stretch",
            column_config={"Cantidad": st.column_config.NumberColumn(format="%.2f")})
        co = F.cont_todos[F.cont_todos.i == elegido]
        ve = F.veh[F.veh.i == elegido]
        a, b = st.columns(2)
        if len(co):
            with a:
                st.caption(f"Contenedores ({len(co)})")
                st.dataframe(co[[c for c in ("Contenedor", "Tam", "Tipo_Empaque", "Sello_1") if c in co]].rename(
                    columns={"Tam": "Tamaño", "Tipo_Empaque": "Tipo de empaque", "Sello_1": "Sello"}), hide_index=True,
                    width="stretch")
        if len(ve):
            with b:
                st.caption(f"Vehículos ({len(ve)})")
                st.dataframe(ve[[c for c in ("Chasis", "Anio_Vehiculo", "Marca", "Color", "Tipo") if c in ve]].rename(
                    columns={"Anio_Vehiculo": "Año"}), hide_index=True, width="stretch")


# ============================== panel ==============================
VISTAS = ["Panorama", "Qué importamos", "Una categoría a fondo", "Origen y marcas", "Cómo y cuándo llega",
          "Buscar declaración"]


def panel_declaraciones(es_admin: bool = False):
    from ui_componentes import selector_horizontal
    _md(_CSS)
    r = cargar_historico()
    if r.get("error"):
        st.error(r["error"])
        return
    clave = r["hora"].isoformat()
    P = _preparar(clave)
    d, it = P["decl"], P["items"]
    act, corte = P["act"], P["corte"]
    parcial = (f" · {MES[P['parcial'][0]]} {act} incompleto ({P['parcial'][1]} declaración"
               f"{'es' if P['parcial'][1] != 1 else ''}), fuera de las comparaciones" if P["parcial"] else "")
    _md(f'<div class="dsg-cab"><div><h2>Comportamiento de las importaciones</h2><div class="sub">Grupo Antillana · '
        f'declaraciones de importación SIGA · sin montos</div></div><div class="meta">Período: <b>{MES[1]} '
        f'{P["anio_min"]} – {MES[corte]} {act}</b>{parcial}<br>{len(d):,} declaraciones · {len(it):,} ítems<br>'
        f'Comparaciones: ene–{MES[corte]} de cada año</div></div>')
    c1, c2, c3 = st.columns([3.6, 1, 1])
    with c1:
        vista = selector_horizontal("Vista", VISTAS, key="dsg_vista", ancho="content")
    empresa = c2.selectbox("Empresa", ["Todo el Grupo"] + EMPRESAS, key="dsg_emp", label_visibility="collapsed")
    anios_todos = list(range(P["anio_min"], act + 1))
    desde = c3.selectbox("Desde", anios_todos[:-2] or anios_todos, key="dsg_desde", label_visibility="collapsed",
                         format_func=lambda y: f"Desde {y}")

    def filtra(df):
        if empresa != "Todo el Grupo":
            df = df[df.Empresa == empresa]
        return df[df.Anio >= desde]

    items = filtra(it)
    eq = items[items.Tipo.notna()]
    F = SimpleNamespace(
        decl=filtra(d), items=items, eq=eq, eqn=eq[eq.Familia != "Energía solar"],
        cats=items[["i", "Cat", "Pais", "Empresa", "Anio", "Mes"]].drop_duplicates(["i", "Cat", "Pais"]),
        cont=filtra(P["cont"]), cont_todos=P["cont"], veh=P["veh"], act=act, ant=act - 1, corte=corte,
        anios=list(range(desde, act + 1)), cerrados=list(range(desde, act)))
    if F.decl.empty or len(F.cerrados) < 2:
        st.info("No hay datos suficientes con esa empresa y ese período.")
        return
    {"Panorama": v_panorama, "Qué importamos": v_que, "Una categoría a fondo": v_categoria,
     "Origen y marcas": v_origen, "Cómo y cuándo llega": v_logistica}.get(vista, lambda F: v_buscar(F, clave))(F)
    st.divider()
    a, b = st.columns([4, 1])
    a.caption(f"Datos del Sheet del histórico, leídos {r['hora']:%d/%m/%Y %H:%M}. Se vuelven a leer una vez al día. "
              "Tipo de equipo, marca y Equipos/Repuestos/Otros se deducen de la partida y la descripción.")
    if es_admin and b.button("↻ Recargar histórico", key="dsg_recargar", width="stretch"):
        invalidar_historico()
        _preparar.clear()
        _indices.clear()
        st.rerun()
