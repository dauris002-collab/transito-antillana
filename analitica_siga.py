"""
analitica_siga.py -- Vista "Tiempos SIGA 2015-2026" dentro de Analítica.

Tablero HTML interactivo (assets/tablero_tiempos.html) que se dibuja TODO en el navegador: los filtros
(empresa, puerto, año) no regresan al servidor, así que no hacen la app más lenta. Python solo lee los datos
(cacheados 1 h), los convierte a JSON compacto y los inyecta en la plantilla.

Qué mide: días entre la fecha de llegada y la fecha de declaración de cada declaración SIGA (Despacho
Aprobado). NO es la estancia en puerto. La estancia real (llegada a puerto -> almacén) sale de los embarques
archivados en la app (ciclo_real de analitica.py) y se muestra aparte en el mismo tablero.

Seguridad: el HTML se incrusta en un iframe del mismo origen que la app. Por eso NINGÚN texto del Sheet
entra al HTML tal cual: Empresa, Puerto, Sector, Origen y Agencia se convierten a un índice de las listas
fijas de abajo (lo desconocido cae en "Otros"/"Sin dato"), y el resto son números.
Sin montos: el Sheet de origen no los trae.
"""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd
import streamlit as st

from sheets_io import COLUMNAS_SIGA, PESTANA_SIGA, TTL_SIGA, _norm_encabezado, cargar_siga

EMPRESAS = ["Antillana Comercial", "Tecnicaribe Dominicana", "Motor Ibérico"]
SECTORES = ["Camiones y vehículos", "Montacargas", "Construcción y minería", "Generadores", "Agrícola",
            "Elevadores", "Energía solar", "Repuestos", "Otros"]
PUERTOS = ["Santo Domingo", "Haina Oriental", "Caucedo", "Aeropuerto", "Otros"]
ORIGENES = ["Estados Unidos", "Brasil", "China", "México", "India", "Japón", "Corea del Sur", "Reino Unido",
            "Alemania", "Taiwán", "Otros"]
AGENCIAS = ["Propia", "Genuine Express", "Aeropaq", "NFS", "Crowley", "Otras", "Sin dato"]

_PLANTILLA = Path(__file__).parent / "assets" / "tablero_tiempos.html"


def _indice(lista: list, valor, por_defecto: int) -> int:
    v = str(valor or "").strip().casefold()
    for i, nombre in enumerate(lista):
        if nombre.casefold() == v:
            return i
    return por_defecto


def _entero(v):
    try:
        if isinstance(v, bool) or v in ("", None):
            return None
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def filas_desde_valores(valores: list) -> tuple:
    """(filas, descartadas). Cada fila: [empresa, 'AAAA-MM-DD', puerto, sector, dias, origen, agencia, 0].
    Pura (sin Streamlit): se puede probar con datos sintéticos."""
    if not valores or not valores[0]:
        return [], 0
    pos = {_norm_encabezado(h): i for i, h in enumerate(valores[0])}
    faltan = [c for c in COLUMNAS_SIGA if _norm_encabezado(c) not in pos]
    if faltan:
        raise ValueError("Faltan columnas en la pestaña: " + ", ".join(faltan))
    ix = {c: pos[_norm_encabezado(c)] for c in COLUMNAS_SIGA}
    filas, malas = [], 0
    for fila in valores[1:]:
        if not any(str(c).strip() for c in fila):
            continue
        celda = lambda c: fila[ix[c]] if ix[c] < len(fila) else ""      # noqa: E731
        emp = _indice(EMPRESAS, celda("Empresa"), -1)
        a, m, d, dias = _entero(celda("Anio")), _entero(celda("Mes")), _entero(celda("Dia")), _entero(celda("Dias"))
        try:
            f = date(a, m, d)
        except (TypeError, ValueError):
            f = None
        if emp < 0 or f is None or dias is None or not (2015 <= f.year <= 2035):
            malas += 1
            continue
        filas.append([emp, f.isoformat(), _indice(PUERTOS, celda("Puerto"), 4),
                      _indice(SECTORES, celda("Sector"), 8), dias,
                      _indice(ORIGENES, celda("Origen"), 10), _indice(AGENCIAS, celda("Agencia"), 6), 0])
    return filas, malas


def registros_almacen(dias_real: pd.DataFrame) -> list:
    """Embarques archivados en la app con fecha de almacén REAL: [{emp:-1, cat, d}]."""
    if dias_real is None or dias_real.empty or "dias_total" not in dias_real.columns:
        return []
    out = []
    for cat, d in zip(dias_real["Categoria"], dias_real["dias_total"]):
        n = _entero(d)
        if n is not None and n >= 0:
            out.append({"emp": -1, "cat": str(cat)[:40], "d": n})
    return out


@lru_cache(maxsize=1)
def _plantilla() -> str:
    return _PLANTILLA.read_text(encoding="utf-8")


def construir_html(filas: list, rec: list) -> str:
    datos = {"emp": EMPRESAS, "sec": SECTORES, "port": PUERTOS, "orig": ORIGENES, "ag": AGENCIAS,
             "tipos": [], "R": filas, "U": [], "REC": rec}
    js = json.dumps(datos, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return _plantilla().replace("/*DATA*/", js, 1)


@st.cache_data(ttl=TTL_SIGA, show_spinner=False)
def _filas_cacheadas(valores_json: str) -> tuple:
    return filas_desde_valores(json.loads(valores_json))


def panel_tiempos_siga(dias_real: pd.DataFrame):
    r = cargar_siga()
    if r["error"]:
        st.warning(r["error"])
        return
    try:
        filas, malas = _filas_cacheadas(json.dumps(r["valores"], ensure_ascii=False))
    except ValueError as e:
        st.warning(f"La pestaña '{PESTANA_SIGA}' no tiene el formato esperado. {e}")
        return
    if not filas:
        st.info(f"La pestaña '{PESTANA_SIGA}' no tiene filas válidas todavía.")
        return
    st.iframe(construir_html(filas, registros_almacen(dias_real)), height="content")
    nota = (f"{len(filas):,} declaraciones SIGA (Despacho Aprobado). Sin montos. "
            "El plazo llegada → declaración no es la estancia en puerto.")
    if malas:
        nota += f" {malas} filas del Sheet se descartaron por fecha o empresa inválida."
    st.caption(nota)
