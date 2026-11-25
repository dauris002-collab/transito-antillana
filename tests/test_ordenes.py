"""Órdenes de compra (piloto): reglas puras, lectura, carga masiva, edición y la sección completa."""
import io
from datetime import date, timedelta

import pytest
from openpyxl import Workbook

import ordenes as oc
import sheets_io as sio
from conftest import abrir_app, ir, sin_errores

HOY = sio.hoy_rd


def serial(d):
    return (d - sio.EPOCH_SHEETS).days


@pytest.fixture
def ses(libros, monkeypatch):
    """Sheet falso de órdenes conectado a ordenes.py, sesión simulada y bitácora grabada."""
    import streamlit as st
    oc.get_spreadsheet_ordenes = lambda: libros.ORDENES
    # Fuera de `streamlit run` el session_state real no guarda nada: se sustituye por un dict.
    monkeypatch.setattr(st, "session_state", {"rol": "admin", "usuario": "Admin Prueba"})
    monkeypatch.setattr(oc, "ordenes_configurado", lambda: True)
    log = []
    monkeypatch.setattr(sio, "registrar_log", lambda *a, **k: log.append((a, k)))
    libros.log = log
    yield libros


def _t(libros):
    vo, vd, _, _ = oc._leer_valores(libros.ORDENES)
    return oc.tablas(vo, vd)


def _xlsx(filas, cab=None):
    cab = cab or ["Documento compras", "Posicion", "Material", "Texto breve", "Cantidad de pedido",
                  "Cantidad Despachada", "Fecha documento", "ESTATUS", "TRANSPORTISTA", "FECHA DE LLEGADA - ETA",
                  "PROVEEDOR", "RESPONSABLE", "EMPRESA", "Solicitante"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Desglose"
    ws.append(cab)
    for f in filas:
        ws.append(f)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


# ------------------------------------------------------------ reglas puras
def test_normalizar_estatus():
    assert oc.normalizar_estatus("DESPACHADO A INTRANS") == ("DESPACHADO", "INTRANS", True)
    assert oc.normalizar_estatus("Despachado a DHL") == ("DESPACHADO", "DHL", True)
    assert oc.normalizar_estatus("en transito") == ("EN TRÁNSITO", "", True)
    assert oc.normalizar_estatus("Lo Llena Repuesto") == ("", "", True)
    assert oc.normalizar_estatus("Recibido")[0] == "COMPLETO"
    assert oc.normalizar_estatus("cosa rara") == ("COSA RARA", "", False)
    assert oc.normalizar_estatus_orden("") == ("ABIERTA", True)
    assert oc.normalizar_estatus_orden("completa") == ("COMPLETA", True)
    assert oc.normalizar_estatus_orden("zzz")[1] is False


def test_estatus_derivado():
    f = oc.estatus_derivado
    assert f([], 0) == "Sin líneas" and f([], 3) == "Por cerrar"
    assert f(["COLOCADA", "BACKORDER"], 2) == "Backorder"
    assert f(["COLOCADA", "COLOCADA"], 2) == "Colocada"
    assert f(["COLOCADA", "DESPACHADO"], 2) == "Despacho parcial"
    assert f(["CONFIRMADA", "COLOCADA"], 2) == "En camino"
    assert f(["DESPACHADO"], 1, parcial=True) == "Despachado parcial"
    assert f(["SIN ESTATUS"], 1) == "Sin estatus"


def test_fechas_ambiguas_como_texto_se_detectan_y_las_reales_no():
    assert oc._fecha_texto_ambigua("05/11/2026")
    assert not oc._fecha_texto_ambigua("25/11/2026")
    assert not oc._fecha_texto_ambigua("05-nov-2026")
    assert not oc._fecha_texto_ambigua(46000)


def test_celdas_vacias_y_marcadores():
    for v in ("", None, "  ", "Lo Llena Repuesto", "PENDIENTE", float("nan")):
        assert oc._limpio(v) is None
    assert oc._texto(4300000448.0) == "4300000448"
    assert oc._num("1,250.5") == 1250.5
    with pytest.raises(ValueError):
        oc._num("abc")


# ------------------------------------------------------------ lectura
def test_lectura_y_derivados(ses):
    t = _t(ses)
    assert t["avisos"] == [] and t["faltan_duro"] == []
    d = oc.construir(t, HOY())
    o = d["ordenes"].set_index("Doc")
    assert d["cerradas"] == 1 and "4300000457" not in o.index, "orden COMPLETA no se muestra"
    assert set(o.index) == {"4300000448", "4300000455", "4100003641", "9999999999"}
    assert o.loc["4300000448", "Abiertas"] == 2 and o.loc["4300000448", "Lineas"] == 3, "línea COMPLETO oculta"
    assert o.loc["4300000448", "Derivado"] == "Despacho parcial"
    assert o.loc["4300000455", "Derivado"] == "Backorder" and o.loc["4300000455", "PendUnid"] == 11
    assert o.loc["9999999999", "Responsable"] == oc.SIN_ASIGNAR
    assert o.loc["4100003641", "SinSello"] and o.loc["4100003641", "Dias"] == 10
    assert o.loc["4300000455", "Dias"] == 12 and oc.es_viejo(12) and not oc.es_viejo(2)
    assert any("9999999999" in a for a in d["avisos"]), "huérfana avisada"
    l = d["lineas"]
    assert l[(l.Doc == "4300000455") & (l.Pos == "20")].iloc[0].Transp == "INTRANS", "estatus viejo separado"
    assert oc.kpis(d["ordenes"])["viejas"] == 3


def test_orden_con_todas_las_lineas_completas_se_avisa_no_se_cierra(ses):
    w = ses.ORDENES.hoja("Desglose")
    w.celdas[(2, w.columna("ESTATUS"))] = "COMPLETO"
    w.celdas[(3, w.columna("ESTATUS"))] = "CANCELADO"
    d = oc.construir(_t(ses), HOY())
    assert "4300000448" in d["completas_abiertas"]
    assert d["ordenes"].set_index("Doc").loc["4300000448", "Derivado"] == "Por cerrar"


def test_pestana_o_columna_faltante(ses):
    ses.ORDENES.hojas.remove(ses.ORDENES.hoja("Desglose"))
    oc._leer_remoto.clear()
    r = oc.cargar("x")
    assert r["faltan"] == ["Desglose"] and r["error"] is None
    ses.ORDENES.agregar("Desglose", [["Algo", "Otro"]])
    t = oc.tablas(*oc._leer_valores(ses.ORDENES)[:2])
    assert t["faltan_duro"], "sin Documento compras / Posición no se puede trabajar"


def test_error_de_google_no_es_excepcion(ses, monkeypatch):
    from fake_gspread import api_error

    def mal():
        raise api_error(403)
    oc.get_spreadsheet_ordenes = mal
    oc._leer_remoto.clear()
    r = oc.cargar("z")
    assert "Editor" in r["error"] and r["o"] == []


# ------------------------------------------------------------ archivo
def test_leer_archivo_valida_y_rechaza(ses):
    f = _xlsx([
        ["4500001", 10, "M1", "Pieza", 10, 0, date(2026, 11, 10), "colocada", "", "", "Prov A", "Ana", "Antillana", "Taller"],
        ["4500001", 20, "M2", "Pieza 2", 5, "", "05/11/2026", "", "", "", "", "", "", ""],          # fecha ambigua (texto)
        ["4500001", 30, "M3", "Pieza 3", -1, "", "", "", "", "", "", "", "", ""],                   # negativa
        ["4500001", 40, "M4", "Pieza 4", 2, "", "", "volando", "", "", "", "", "", ""],             # estatus raro
        ["4500001", 10, "M1", "Repetida", 2, "", "", "", "", "", "", "", "", ""],                    # repetida
        ["", 50, "M5", "Sin doc", 2, "", "", "", "", "", "", "", "", ""],
        ["4500002", 10, "M6", "Con fecha texto", 3, "", "25/11/2026", "DESPACHADO A DHL", "", "", "", "", "", ""],
        ["4500002", 20, "M7", "Placeholder", 3, "Lo Llena Repuesto", "", "PENDIENTE", "", "", "", "", "", ""],
    ])
    r = oc.leer_archivo(f, "x.xlsx")
    ok = {(x["doc"], x["pos"]): x for x in r["lineas"]}
    assert set(ok) == {("4500001", "10"), ("4500002", "10"), ("4500002", "20")}
    motivos = " | ".join(m for *_, m in r["rechazos"])
    for frag in ("ambigua", "negativa", "no reconocido", "repetida", "falta Documento"):
        assert frag in motivos, frag
    assert ok[("4500001", "10")]["fdoc"] == date(2026, 11, 10)
    assert ok[("4500002", "10")]["fdoc"] == date(2026, 11, 25), "25/11 no es ambigua"
    assert ok[("4500002", "10")]["estatus"] == "DESPACHADO" and ok[("4500002", "10")]["transp"] == "DHL"
    assert ok[("4500002", "20")]["desp"] is None and ok[("4500002", "20")]["estatus"] is None
    assert ok[("4500001", "10")]["emp"] == "Antillana Comercial" or ok[("4500001", "10")]["emp"] is None


def test_archivo_sin_encabezados_clave(ses):
    with pytest.raises(ValueError):
        oc.leer_archivo(_xlsx([], cab=["A", "B"]), "x.xlsx")


def test_csv_con_punto_y_coma(ses):
    csv = "Documento compras;Posición;Material;Cantidad de pedido\n4500009;10;MX;7\n".encode("utf-8-sig")
    r = oc.leer_archivo(csv, "a.csv")
    assert r["lineas"][0]["pedida"] == 7 and r["lineas"][0]["doc"] == "4500009"


# ------------------------------------------------------------ plan y escritura
def test_plan_no_borra_ni_pisa_lo_de_repuesto(ses):
    t = _t(ses)
    w = ses.ORDENES.hoja("Desglose")
    w.celdas[(2, w.columna("No. Factura"))] = "F-1"
    t = _t(ses)
    lineas = [
        {"doc": "4300000448", "pos": "10", "fila_excel": 2, "fact": "F-OTRA", "estatus": "CONFIRMADA", "texto": None,
         "eta": date(2026, 12, 1)},
        {"doc": "4300000448", "pos": "20", "fila_excel": 3, "estatus": "DESPACHADO"},          # igual: sin cambio
    ]
    p = oc.planificar(lineas, t, "Admin", "admin")
    assert len(p["cambios"]) == 1 and p["sin_cambio"] == 1
    c = p["cambios"][0]
    assert set(c["cambios"]) == {"estatus", "eta"} and c["conservados"] == ["fact"], "factura de Repuesto no se pisa"
    assert "texto" not in c["cambios"], "vacío del archivo no borra"


def test_aplicar_carga_escribe_idempotente_y_respeta_formulas(ses):
    f = _xlsx([["4500001", 10, "M1", "Pieza", 10, 0, date(2026, 11, 10), "", "", "", "Prov A", "Ana", "Tecnicaribe", "Taller"],
               ["4300000448", 10, "", "", "", 3, "", "CONFIRMADA", "", "", "", "", "", ""]])
    lineas = oc.leer_archivo(f, "x.xlsx")["lineas"]
    ok, msg = oc.aplicar_carga(lineas, "x.xlsx")
    assert ok, msg
    wd, wo = ses.ORDENES.hoja("Desglose"), ses.ORDENES.hoja("Órdenes")
    # línea nueva en la primera fila libre (10), estatus por defecto, sin tocar PENDIENTE
    assert wd.valor(10, "Documento compras") == "4500001" and wd.valor(10, "Posición") == "10"
    assert wd.valor(10, "ESTATUS") == "COLOCADA" and wd.valor(10, "PENDIENTE / BACKORDER") == ""
    assert wd.valor(10, "Fecha documento") == serial(date(2026, 11, 10)) and wd.valor(10, "Cantidad de pedido") == 10
    # existente actualizada
    assert wd.valor(2, "ESTATUS") == "CONFIRMADA" and wd.valor(2, "Cantidad Despachada") == 3
    # orden nueva con datos del archivo, sello y usuario
    assert wo.valor(6, "DOCUMENTO COMPRAS") == "4500001" and wo.valor(6, "PROVEEDOR") == "Prov A"
    assert wo.valor(6, "ESTATUS ORDEN") == "ABIERTA" and wo.valor(6, "ACTUALIZADO POR") == "Admin Prueba"
    assert wo.valor(6, "FECHA ACTUALIZACIÓN") > serial(HOY()) - 1
    # orden existente: solo sello
    assert wo.valor(2, "ACTUALIZADO POR") == "Admin Prueba" and wo.valor(2, "PROVEEDOR") == "Cummins Inc"
    assert ses.log and ses.log[0][0][0] == "Órdenes: carga masiva"
    # segunda vez: nada nuevo, nada duplicado
    esc = ses.ORDENES.escrituras
    ok, msg = oc.aplicar_carga(lineas, "x.xlsx")
    assert ok and "nada nuevo" in msg and ses.ORDENES.escrituras == esc
    assert wd.valor(11, "Documento compras") == ""


def test_carga_rol_compras_queda_como_responsable(ses):
    import streamlit as st
    st.session_state["rol"], st.session_state["usuario"] = "compras", "David Tejeda"
    lineas = oc.leer_archivo(_xlsx([["4500777", 10, "M", "P", 1, "", "", "", "", "", "", "", "", ""]]), "x.xlsx")["lineas"]
    assert oc.aplicar_carga(lineas, "x.xlsx")[0]
    wo = ses.ORDENES.hoja("Órdenes")
    assert wo.valor(6, "RESPONSABLE") == "David Tejeda"


def test_aplicar_carga_crece_la_hoja_si_hace_falta(ses):
    ses.ORDENES.hoja("Desglose").row_count = 10          # la fila libre es la 10; 3 nuevas necesitan 12
    lineas = oc.leer_archivo(_xlsx([["45%d" % i, 10, "M", "P", 1, "", "", "", "", "", "", "", "", ""] for i in range(3)]),
                             "x.xlsx")["lineas"]
    ok, msg = oc.aplicar_carga(lineas, "x.xlsx")
    assert ok, msg
    assert ses.ORDENES.hoja("Desglose").row_count >= 12


def test_viewer_y_taller_no_escriben(ses):
    import streamlit as st
    lineas = oc.leer_archivo(_xlsx([["4500001", 10, "M", "P", 1, "", "", "", "", "", "", "", "", ""]]), "x.xlsx")["lineas"]
    for rol in ("viewer", "taller"):
        st.session_state["rol"] = rol
        ok, msg = oc.aplicar_carga(lineas, "x.xlsx")
        assert not ok and "permiso" in msg
        assert not oc.guardar_edicion("4300000448", {"coment": (None, "x")}, {})[0]
    assert ses.ORDENES.escrituras == 0


def test_falla_de_google_en_carga_se_explica(ses):
    ses.ORDENES.fallar_en = {1: 429}
    lineas = oc.leer_archivo(_xlsx([["4500001", 10, "M", "P", 1, "", "", "", "", "", "", "", "", ""]]), "x.xlsx")["lineas"]
    ok, msg = oc.aplicar_carga(lineas, "x.xlsx")
    assert ok, "un 429 suelto se reintenta solo"
    ses.ORDENES.fallar_en = {n: 429 for n in range(1, 40)}
    lineas = oc.leer_archivo(_xlsx([["4500002", 10, "M", "P", 1, "", "", "", "", "", "", "", "", ""]]), "x.xlsx")["lineas"]
    antes = ses.ORDENES.hoja("Desglose").matriz()
    ok, msg = oc.aplicar_carga(lineas, "x.xlsx")
    assert not ok and "limitando" in msg
    assert ses.ORDENES.hoja("Desglose").matriz() == antes, "si Google rechaza, no queda nada a medias"


def test_guardar_edicion_ok_y_completar_oculta(ses):
    doc = "4300000455"
    ok, msg = oc.guardar_edicion(doc, {"est": ("ABIERTA", "COMPLETA"), "coment": (None, "Listo")},
                                 {"10": {"estatus": ("BACKORDER", "CONFIRMADA"), "eta": (None, date(2026, 12, 5))}})
    assert ok, msg
    wo, wd = ses.ORDENES.hoja("Órdenes"), ses.ORDENES.hoja("Desglose")
    assert wo.valor(3, "ESTATUS ORDEN") == "COMPLETA" and wo.valor(3, "COMENTARIO PARA TALLER") == "Listo"
    assert wd.valor(5, "ESTATUS") == "CONFIRMADA" and wd.valor(5, "FECHA DE LLEGADA - ETA") == serial(date(2026, 12, 5))
    d = oc.construir(_t(ses), HOY())
    assert doc not in set(d["ordenes"]["Doc"]) and doc not in set(d["lineas"]["Doc"]), "COMPLETA oculta orden y líneas"
    assert ses.log[-1][0][0] == "Órdenes: editar"


def test_guardar_edicion_conflicto_no_escribe_nada(ses):
    wd = ses.ORDENES.hoja("Desglose")
    wd.celdas[(5, wd.columna("ESTATUS"))] = "EN PUERTO"            # otra persona lo cambió mientras editabas
    esc = ses.ORDENES.escrituras
    ok, msg = oc.guardar_edicion("4300000455", {"coment": (None, "x")},
                                 {"10": {"estatus": ("BACKORDER", "CONFIRMADA")}})
    assert not ok and "Alguien cambió" in msg and ses.ORDENES.escrituras == esc
    assert ses.ORDENES.hoja("Órdenes").valor(3, "COMENTARIO PARA TALLER") == ""


def test_editar_orden_sin_fila_en_ordenes_la_crea(ses):
    ok, msg = oc.guardar_edicion("9999999999", {"resp": (None, "Ana Pérez"), "prov": (None, "ACME")}, {})
    assert ok, msg
    wo = ses.ORDENES.hoja("Órdenes")
    assert wo.valor(6, "DOCUMENTO COMPRAS") == "9999999999" and wo.valor(6, "RESPONSABLE") == "Ana Pérez"


def test_plantilla_se_vuelve_a_leer(ses):
    r = oc.leer_archivo(oc.plantilla_excel(), "plantilla.xlsx")
    assert r["lineas"] == [] and r["rechazos"] == []


# ------------------------------------------------------------ la sección completa
def test_sin_secret_no_hay_seccion(libros):
    at = abrir_app("1111")
    sin_errores(at)
    textos = " ".join(str(b.label) for b in at.button) + " " + " ".join(str(r.options) for r in at.radio)
    assert "Órdenes de compra" not in textos
    ir(at, "Órdenes de compra")           # aunque se fuerce, no hay sección: cae en el Dashboard o similar
    sin_errores(at)


@pytest.mark.parametrize("pin", ["1111", "2222", "333333", "444444"])
def test_seccion_se_ve_con_todos_los_roles(libros, pin):
    at = abrir_app(pin, ordenes=True)
    ir(at, "Órdenes de compra")
    sin_errores(at)
    md = " ".join(m.value for m in at.markdown)
    assert "Órdenes abiertas" in md and "4300000448" in md and "Cummins Inc" in md
    assert "4300000457" not in md, "orden COMPLETA oculta"
    pestanas = [t.label for t in at.tabs]
    assert ("Cargar y actualizar" in pestanas) == (pin in ("1111", "444444"))


def test_admin_ve_avisos_de_datos(libros):
    at = abrir_app("1111", ordenes=True)
    ir(at, "Órdenes de compra")
    assert any("Datos por revisar" in str(e.label) for e in at.expander)


def test_responsable_filtra_y_boton_abre_la_orden(libros):
    at = abrir_app("1111", ordenes=True)
    ir(at, "Órdenes de compra")
    b = [x for x in at.button if x.label == "4300000455"]
    assert b, "una tarjeta por responsable con sus números de orden"
    b[0].click().run()
    sin_errores(at)
    md = " ".join(m.value for m in at.markdown)
    assert "Orden 4300000455" in md and "Bomba" in md and "sin fecha del fabricante" in md


def test_falla_del_sheet_de_ordenes_no_tumba_la_app(libros):
    libros.ORDENES.fallar_en = {}
    libros.ORDENES.hojas.clear()
    at = abrir_app("1111", ordenes=True)
    ir(at, "Órdenes de compra")
    sin_errores(at)
    assert any("faltan estas pestañas" in w.value for w in at.warning)
    ir(at, "Dashboard")
    sin_errores(at)
