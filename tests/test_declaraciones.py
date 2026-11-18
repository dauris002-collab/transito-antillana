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


def test_clasificar_no_marca_repuestos_como_equipos():
    """Regresión: con pandas 3 la columna Tipo guarda NaN (verdadero) y todo salía «Equipos»."""
    import pandas as pd
    x = D._clasificar(pd.DataFrame({"Partida": ["8421.23.00", "8427.20.00", "3820.00.00"],
                                    "Descripcion": ["FILTRO DE ACEITE", "MONTACARGAS YALE GP050VX", "COOLANT"]}))
    assert list(x.Cat) == ["Repuestos", "Equipos", "Otros"]
    assert x.Marca.iloc[1] == "Yale" and pd.isna(x.Marca.iloc[0])


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


@pytest.mark.parametrize("v", ["1", "2", "3", "4"])
def test_paginas_sin_errores(v):
    _correr(v=v)


def test_elevadores_son_fujihd():
    assert D.marca("ELEVADOR DE PASAJEROS 8 PERSONAS NO APLICA", "Elevadores") == "FUJIHD"
    assert D.marca("ESCALERA ELECTRICA", "Escaleras eléctricas") == "FUJIHD"
    assert D.marca("MONTACARGAS YALE GP050VX", "Montacargas") == "Yale"
    at = _correr(v="2", **{"s.dsg_cat": "Elevadores"})
    assert not at.exception


@pytest.mark.parametrize("filtros", [
    {"s.dsg_paises": "China"}, {"s.dsg_marca": "Yale"}, {"s.dsg_fam": "Generadores"}, {"s.dsg_adm": "Caucedo"},
    {"s.dsg_carga": "Aérea"}, {"s.dsg_trae": "Repuestos"}, {"s.dsg_emp": "Tecnicaribe"}, {"s.dsg_desde": "2024"},
    {"s.dsg_partida": "8428", "s.dsg_paises": "India|China"}, {"s.dsg_desc": "filtro aceite", "s.dsg_trae": "Equipos"},
    {"s.dsg_paises": "Brasil", "s.dsg_marca": "Hitachi", "s.dsg_carga": "Aérea"},
])
@pytest.mark.parametrize("v", ["1", "2", "3", "4"])
def test_filtros_globales_no_rompen_ninguna_pagina(v, filtros):
    """Combinaciones que dejan pocos datos: cada página debe mostrar algo o el aviso, nunca una excepción."""
    _correr(v=v, **filtros)


def test_filtros_se_aplican_a_todo():
    import pandas as pd
    P = D._preparar("t")
    base = dict(empresa="Todo el Grupo", desde=P["anio_min"], adm=[], carga=[], trae="Todo", fam=[], marca=[],
                paises=[], partida="", texto="")
    todo = D._filtrar(P, base)
    chi = D._filtrar(P, {**base, "paises": ["China"]})
    assert 0 < len(chi.decl) < len(todo.decl) and set(chi.items.Pais) == {"China"}
    assert set(chi.decl.i) == set(chi.items.i)                      # solo declaraciones que traen ítems de China
    assert set(chi.cont.i) <= set(chi.decl.i)
    aereo = D._filtrar(P, {**base, "carga": ["Aérea"]})
    assert set(aereo.decl.Carga) == {"Aérea"} and set(aereo.items.Carga) == {"Aérea"}
    assert all(m == "FUJIHD" for m in D._filtrar(P, {**base, "fam": ["Elevadores"]}).eqn.Marca)


def test_panel_de_copia_no_interpreta_html(monkeypatch):
    capturado = {}
    monkeypatch.setattr(D.st, "iframe", lambda doc, **k: capturado.update(doc=doc))
    D._panel_copia([("Copiar", "a</script><script>alert(1)</script>")], [("Campo", "<img src=x onerror=alert(1)>")])
    doc = capturado["doc"]
    assert "</script><script>alert" not in doc and "<img src=x" not in doc
    assert "\\u003c" in doc


def test_buscador_filtra_y_muestra_ficha():
    at = _correr(v="4", **{"s.dsg_desc": "montacargas yale", "s.dsg_partida": "8427", "s.dsg_modo": "Por ítem"})
    tablas = at.dataframe
    assert tablas, "debe mostrar la tabla de resultados"
    assert all(str(p).startswith("8427") for p in tablas[0].value["Partida"])


def test_buscador_por_numero():
    at = _correr(v="4", **{"s.dsg_q": "BL0000005"})
    assert at.dataframe and len(at.dataframe[0].value) == 1
    assert len(at.dataframe) >= 2, "con un solo resultado se abre la ficha (tabla de ítems)"


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
