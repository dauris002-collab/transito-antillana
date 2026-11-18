"""Declaraciones SIGA (decisión #36): clasificación de ítems y las seis vistas sobre datos sintéticos."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import declaraciones as D

ARNES = str(Path(__file__).with_name("harness_declaraciones.py"))


@pytest.mark.parametrize("partida,desc,esperado", [
    ("8427.20.00", "MONTACARGAS YALE GP050VX AMARILLO NO APLICA", "Montacargas"),
    ("8428.10.00", "MONTACARGAS YALE, 4500Lbs. REF: ERC045VG NO APLICA", "Montacargas Eléctricos"),
    ("8428.10.00", "GOMA DE MONTACARGAS 582177196 NO APLICA", None),            # pieza, no equipo
    ("8428.10.00", "ELEVADOR DE PASAJEROS 8 PERSONAS NO APLICA", "Elevadores"),
    ("8428.10.00", "PLATAFORMA ELEVADORA HIDRAULICA NO APLICA", "Plataformas elevadoras"),
    ("8502.12.00", "GENERADOR ELECRICO 30 KW NO APLICA", "Generadores eléctricos"),
    ("8502.39.00", "TORRE DE ILUMINACION 6 KVA DIESEL", "Torres de iluminación"),
    ("8432.21.00", "CRSG-RASTRA ARADORA CON SUS ACCESORIOS 24X6,00", "Implementos agrícolas"),
    ("8432.39.00", "DISCO PLANO LISO 15 X4,00MM PARA SEMBRADORA DE SEMILLA", None),
    ("8433.59.10", "MANGA PARA COSECHADORA AGRICOLA / 44250", None),
    ("8429.59.00", "MARCO DE TUBO P/ RODILLO / 58071020", None),
    ("8429.52.00", "EXCAVADORA HITACHI ZX200LC-5G TRACCION: ORUGA", "Excavadoras"),
    ("8701.21.00", "TRACTOR DE CARRETERA INTERNATIONAL HX620", "Tractores de carretera"),
    ("8704.31.90", "CAMIONETA TOYOTA HILUX", "Vehículos livianos"),
    ("8421.23.00", "CARTUCHO DE FILTRO REF 4100002975 NO APLICA", None),
])
def test_tipo_equipo(partida, desc, esperado):
    assert D.tipo_equipo(partida, desc) == esperado


def test_marca_y_categoria():
    assert D.marca("GENERADOR ELECTRICO 30 KW MODELO: C30D6", "Generadores eléctricos") == "Cummins"
    assert D.marca("MONTACARGA MODELO: GP30UX", "Montacargas") == "Yale"
    assert D.marca("GENERADOR ELECRICO 30 KW NO APLICA", "Generadores eléctricos") == "Sin marca en la descripción"
    assert D.marca("CAMION INTERNATIONAL HV607") == "International"
    assert D.categoria("Camiones", "8704.22.90") == "Equipos"
    assert D.categoria(None, "8708.99.90") == "Repuestos"
    assert D.categoria(None, "3820.00.00") == "Otros"


def test_pais_y_ids():
    assert D._pais("ESTADOS UNIDOS") == "Estados Unidos"
    assert D._pais("COREA DEL SUR") == "Corea del Sur"
    assert D._pais("") == "Sin dato"
    assert D._norm_id("10030-IC01 2507-00A1") == "10030IC01250700A1"


def _correr(**params):
    at = AppTest.from_file(ARNES, default_timeout=180)
    for k, v in params.items():
        at.query_params[k] = v
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


@pytest.mark.parametrize("v", ["1", "2", "3", "4", "5", "6"])
def test_vistas_sin_errores(v):
    _correr(v=v)


def test_buscador_filtra_y_muestra_ficha():
    at = _correr(v="6", desc="montacargas yale", partida="8427")
    assert any("Declaración" in s.label for s in at.selectbox)
    tablas = at.dataframe
    assert tablas, "debe mostrar la tabla de resultados"
    assert all(str(p).startswith("8427") for p in tablas[0].value["Partida"])


def test_buscador_por_numero():
    at = _correr(v="6", q="BL0000005")
    assert at.dataframe and len(at.dataframe[0].value) >= 1


def test_error_de_lectura_no_tumba():
    at = _correr(error="1")
    assert any("no compartido" in e.value for e in at.error)



def test_mes_de_corte_parcial(monkeypatch):
    """Un mes con muy pocas declaraciones (PDF aún sin bajar) no entra en las comparaciones."""
    import pandas as pd
    from datetime import datetime
    base = pd.DataFrame({"Declaracion": [f"D{i}" for i in range(31)], "Empresa": "Tecnicaribe",
                         "Anio": [2026] * 31, "Mes": [1] * 10 + [2] * 10 + [3] * 10 + [4],
                         "Administracion": "Caucedo", "Tipo_Carga": "En contenedor", "BL": "x",
                         "Fecha_Declaracion": "2026-01-01", "Fecha_Llegada": ""})
    vacio = {"Items": pd.DataFrame(columns=["Declaracion", "Item", "Partida", "Descripcion", "Unidad", "Pais_Origen", "Cantidad"]),
             "Contenedores": pd.DataFrame(columns=["Declaracion", "Contenedor", "Tamano_Pies", "High_Cube"]),
             "Vehiculos": pd.DataFrame(columns=["Declaracion", "Chasis"])}
    monkeypatch.setattr(D, "cargar_historico", lambda: {"tablas": {"Declaraciones": base, **vacio},
                                                         "hora": datetime(2026, 5, 1)})
    D._preparar.clear()
    P = D._preparar("prueba-corte")
    assert P["corte"] == 3 and P["parcial"] == (4, 1)


def test_app_completa_con_historico_configurado(libros):
    """Con HISTORICO_SHEET_ID, Analítica ofrece las dos vistas; si el Sheet no responde (aquí no hay
    Sheet real), Declaraciones muestra el aviso y Embarques sigue funcionando igual."""
    from conftest import SECRETS, ir, sin_errores
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(Path(__file__).with_name("harness_app.py")), default_timeout=120)
    for k, v in {**SECRETS, "HISTORICO_SHEET_ID": "falso-historico"}.items():
        at.secrets[k] = v
    at.run()
    at.text_input[0].set_value("2222")
    at.button[0].click()
    at.run()
    ir(at, "Analítica")
    sin_errores(at)
    assert at.session_state["analitica_fuente"] == "Embarques en tránsito"
    at.session_state["analitica_fuente"] = "Declaraciones SIGA"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.error, "sin Sheet real debe avisar, no romper"


def test_app_sin_historico_queda_igual(libros):
    from conftest import abrir_app, sin_errores
    at = abrir_app("2222", "Analítica")
    sin_errores(at)
    assert "analitica_fuente" not in at.session_state
