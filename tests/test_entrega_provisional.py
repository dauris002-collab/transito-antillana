"""Entrega Provisional (v5.17): reglas puras y la vista completa sobre el Sheet falso."""
from datetime import date, timedelta

import pandas as pd

import entrega_provisional as ep
import sheets_io as sio
from conftest import abrir_app, ir, sin_errores

HOY = sio.hoy_rd


def serial(d):
    return (d - sio.EPOCH_SHEETS).days


# ------------------------------------------------------------ reglas puras
def test_dias_de_retraso_pendiente_cuenta_desde_el_vencimiento():
    h = date(2026, 11, 19)
    assert ep.dias_de_retraso(h + timedelta(days=3), None, h) is None, "antes de vencer no hay contador"
    assert ep.dias_de_retraso(h, None, h) == 0, "el día del vencimiento aún es 0"
    assert ep.dias_de_retraso(h - timedelta(days=2), None, h) == 2
    assert ep.dias_de_retraso(h - timedelta(days=42), None, h) == 42
    assert ep.dias_de_retraso(None, None, h) is None, "sin fecha límite no se cuentan días"


def test_dias_de_retraso_pagado_queda_fijo():
    lim = date(2026, 10, 1)
    assert ep.dias_de_retraso(lim, date(2026, 10, 6), date(2027, 1, 1)) == 5, "no sigue corriendo"
    assert ep.dias_de_retraso(lim, date(2026, 9, 20), date(2027, 1, 1)) == 0, "pagado antes: mínimo 0"


def test_rojo_solo_con_mas_de_dos_dias():
    assert not ep._rojo(0) and not ep._rojo(2)
    assert ep._rojo(3) and not ep._rojo(float("nan"))


def test_alias_de_encabezados_no_duplica_canonicos():
    h = ep._canonizar_encabezados(["Empresa", "Número de Declaración", "BL", "Fecha de Pago", "DOP Pendiente",
                                   "Fecha de Pagado"])
    assert h == ["Empresa", "Numero_Declaracion", "BL", "Fecha_Limite_Pago", "Monto_DOP", "Fecha_PagoRealizado"]
    # si la columna canónica ya existe, el alias queda como está
    h = ep._canonizar_encabezados(["Fecha_Limite_Pago", "Fecha de Pago"])
    assert h == ["Fecha_Limite_Pago", "Fecha de Pago"]


def _df(filas):
    d = pd.DataFrame(filas, columns=ep.COLUMNAS_EP)
    d["FilaSheet"] = range(2, len(d) + 2)
    return d


def test_enriquecer_clasifica_y_avisa():
    h = date(2026, 11, 19)
    df = _df([
        ["Tecnicaribe", "D-1", "BL-1", "GEN", 2, serial(date(2026, 7, 31)), serial(h - timedelta(days=10)), 900000, ""],
        ["Antillana", "D-1", "BL-2", "REPETIDA", 1, "", serial(h - timedelta(days=1)), "", ""],
        ["Otra SA", "D-3", "BL-3", "RARA", 1, "", "no es fecha", 50, serial(h)],
        ["", "", "", "", "", "", "", "", ""],
    ])
    out, avisos = ep.enriquecer_ep(df, h)
    assert len(out) == 3, "la fila en blanco no cuenta"
    assert out.loc[0, "Dias"] == 10 and not out.loc[0, "Pagado"]
    assert out.loc[1, "Empresa"] == "Antillana Comercial"
    assert out.loc[2, "Pagado"] and out.loc[2, "Empresa"] == "Sin empresa" and pd.isna(out.loc[2, "Dias"])
    texto = " | ".join(avisos)
    for esperado in ("ya está en la fila 2", "sin monto", "'Otra SA' no reconocida", "'no es fecha' no válida"):
        assert esperado in texto, esperado
    assert ep.total_por_pagar(out[~out["Pagado"]]) == 900000


def test_el_nombre_del_origen_coincide_en_pagos():
    import pagos
    assert pagos._NOMBRE_EP == ep.NOMBRE_EP


# ------------------------------------------------------------ vista completa
def _ir(pin):
    at = abrir_app(pin)
    at.session_state["pago_origen_ult"] = ep.NOMBRE_EP
    return ir(at, "Estatus de Pago")


def _datos(libros):
    h = HOY()
    filas = [ep.COLUMNAS_EP,
             ["Tecnicaribe", "D-1", "BL-A", "GENERADOR 350KW", 2, serial(h - timedelta(days=20)),
              serial(h - timedelta(days=10)), 900000, ""],
             ["Antillana Comercial", "D-2", 1300850722600962, "ALMACEN", 4, serial(date(2026, 7, 31)),
              serial(h - timedelta(days=2)), 699325.6, ""],
             ["Motor Ibérico", "D-3", "BL-C", "RODAMIENTOS", 1, "", serial(h + timedelta(days=5)), 1000, ""],
             ["Tecnicaribe", "D-4", "BL-D", "PAGADO SA", 1, "", serial(h - timedelta(days=30)), 500,
              serial(h - timedelta(days=25))]]
    libros.LIBRO.agregar(ep.NOMBRE_EP, filas)


def _html(at):
    return "\n".join(m.value for m in at.markdown)


def test_admin_crea_la_pestana_antes_de_pagos(libros):
    assert ep.NOMBRE_EP not in [h.title for h in libros.LIBRO.hojas]
    at = _ir("1111")
    sin_errores(at)
    titulos = [h.title for h in libros.LIBRO.hojas]
    assert titulos.index(ep.NOMBRE_EP) == titulos.index("Pagos") - 1, titulos
    hoja = libros.LIBRO.hoja(ep.NOMBRE_EP)
    assert [hoja.real(1, c) for c in range(1, 10)] == ep.COLUMNAS_EP
    assert any("Pestaña creada" in str(v) for v in libros.LIBRO.hoja("Log").celdas.values())
    assert "no tiene filas con datos" in " ".join(i.value for i in at.info)


def test_viewer_no_crea_nada(libros):
    antes = [h.title for h in libros.LIBRO.hojas]
    at = _ir("2222")
    sin_errores(at)
    assert [h.title for h in libros.LIBRO.hojas] == antes
    assert any("todavía no existe" in i.value for i in at.info)


def test_vista_con_datos_pendientes_total_y_rojo(libros):
    _datos(libros)
    antes = {h.title: dict(h.celdas) for h in libros.LIBRO.hojas if h.title != "Log"}
    at = _ir("2222")
    sin_errores(at)
    html = _html(at)
    assert 'Pendientes</div><div class="n">3</div>' in html
    assert "RD$ 1,600,325.60" in html, "total por pagar = suma de los 3 pendientes"
    assert "Pendientes por antigüedad" in " ".join(m.value for m in at.markdown)
    assert html.count('td class="rojo"') == 1, "solo el de 10 días; el de 2 no es rojo"
    assert "1300850722600962" in html and "Llegada: 31 jul 2026" in html
    assert "GENERADOR 350KW · 2 · Llegada:" in html
    assert "D-4" in html, "con Estatus Todos también se ve el pagado"
    cuerpo_tabla = html[html.index("<tbody>"):html.index("</tbody>")]
    assert "D-4" not in cuerpo_tabla, "el pagado no está en Pendientes por antigüedad"
    assert cuerpo_tabla.index("D-1") < cuerpo_tabla.index("D-2") < cuerpo_tabla.index("D-3"), "más retraso arriba"
    assert {h.title: dict(h.celdas) for h in libros.LIBRO.hojas if h.title != "Log"} == antes, "viewer: cero escrituras"


def test_filtro_pagado_y_empresa(libros):
    _datos(libros)
    at = _ir("1111")
    at.selectbox(key="ep_estatus").set_value("Pagado").run()
    sin_errores(at)
    html = _html(at)
    assert "D-4" in html and "D-1" not in html and "Pagado</span>" in html
    assert "Pendientes por antigüedad" not in html
    at.selectbox(key="ep_estatus").set_value("Pendiente").run()
    at.selectbox(key="ep_empresa").set_value("Tecnicaribe").run()
    html = _html(at)
    assert "D-1" in html and "D-2" not in html and "D-4" not in html
    assert 'Pendientes</div><div class="n">1</div>' in html


def test_alias_de_columnas_en_el_sheet_se_leen(libros):
    h = HOY()
    libros.LIBRO.agregar(ep.NOMBRE_EP, [
        ["Empresa", "Número de Declaración", "BL", "Descripción", "Cantidad", "Llegada", "Fecha de Pago",
         "DOP Pendiente", "Fecha de Pagado"],
        ["Tecnicaribe", "X-1", "BL-X", "ALIAS", 1, "", serial(h - timedelta(days=5)), 123.45, ""]])
    at = _ir("1111")
    sin_errores(at)
    html = _html(at)
    assert "X-1" in html and "RD$ 123.45" in html


def test_pestana_caida_no_tumba_pagos_de_embarques(libros):
    import fake_gspread
    _datos(libros)
    original = libros.LIBRO.values_get
    libros.LIBRO.values_get = lambda *a, **k: (_ for _ in ()).throw(fake_gspread.api_error(403))
    at = _ir("1111")
    sin_errores(at)
    assert any("rechazó el acceso" in e.value for e in at.error)
    libros.LIBRO.values_get = original
    at.session_state["pago_origen_ult"] = "Embarques"
    at.selectbox(key="pago_origen").set_value("Embarques").run()
    sin_errores(at)
    assert any("Pendientes por antigüedad" in m.value for m in at.markdown)


def test_opciones_de_origen_con_entrega_provisional_debajo_de_embarques(libros):
    at = abrir_app("1111")
    ir(at, "Estatus de Pago")
    assert list(at.selectbox(key="pago_origen").options) == ["Embarques", "Entrega Provisional", "Repuesto y Taller"]
