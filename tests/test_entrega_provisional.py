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
    assert set(out["Empresa"]) == {"Antillana Comercial"}, "la empresa es siempre Antillana; la columna se ignora"
    assert out.loc[2, "Pagado"] and pd.isna(out.loc[2, "Dias"])
    texto = " | ".join(avisos)
    for esperado in ("ya está en la fila 2", "sin monto", "'no es fecha' no válida"):
        assert esperado in texto, esperado
    assert "empresa" not in texto.lower()
    assert ep.total_por_pagar(out[~out["Pagado"]]) == 900000


def test_el_nombre_del_origen_coincide_en_pagos():
    import pagos
    assert pagos._NOMBRE_EP == ep.NOMBRE_EP


# ------------------------------------------------------------ vista completa
def _ir(pin):
    at = abrir_app(pin)
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


def _html(at, pestana=1):
    """Markdown de UNA pestaña de Estatus de Pagos (0 Embarques, 1 Entrega Provisional, 2 Taller)."""
    return "\n".join(m.value for m in at.tabs[pestana].markdown)


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
    assert "Pendientes por antigüedad" not in html and "Desglose por expediente" not in html
    assert ".ep-tabla th { background:#DBEAFE;" in html, "encabezado de la tabla en azul claro, no negro"
    assert html.count('td class="rojo"') == 1, "solo el de 10 días; el de 2 no es rojo"
    assert "1300850722600962" in html and "Llegada: 31 jul 2026" in html
    assert "GENERADOR 350KW · 2 · Llegada:" in html
    assert 'class="ep-decl">Declaración D-1' in html, "la declaración va debajo del BL"
    assert 'Número de declaración</span>' not in html, "ya no está en los totales del desglose"
    assert html.count('class="ep-emp">Antillana Comercial</span>') == 4, "las 4 entregas salen como Antillana Comercial"
    assert 'ep-emp">Tecnicaribe' not in html and 'ep-emp">Motor Ibérico' not in html
    assert "D-4" in html, "con Estatus Todos también se ve el pagado"
    cuerpo_tabla = html[html.index("<tbody>"):html.index("</tbody>")]
    assert "D-4" not in cuerpo_tabla, "el pagado no está en Pendientes por antigüedad"
    assert cuerpo_tabla.index("D-1") < cuerpo_tabla.index("D-2") < cuerpo_tabla.index("D-3"), "más retraso arriba"
    assert {h.title: dict(h.celdas) for h in libros.LIBRO.hojas if h.title != "Log"} == antes, "viewer: cero escrituras"


def test_filtro_pagado_y_pendiente_por_mes(libros):
    _datos(libros)
    at = _ir("1111")
    assert [x.label for x in at.tabs[1].selectbox] == ["Estatus", "Pendiente por mes"], "sin filtro Empresa"
    at.selectbox(key="ep_estatus").set_value("Pagado").run()
    sin_errores(at)
    html = _html(at)
    assert "D-4" in html and "D-1" not in html and "Pagado</span>" in html
    assert "<tbody>" not in html, "sin pendientes en la selección no hay tabla"

    # Pendiente por mes: mes de la fecha de pago y cantidad de entregas PENDIENTES de ese mes
    h = HOY()
    pend = [h - timedelta(days=10), h - timedelta(days=2), h + timedelta(days=5)]
    clave = lambda d: f"{d.year}-{d.month:02d}"
    mes_d1 = clave(pend[0])
    esperados = [d for d in pend if clave(d) == mes_d1]
    at.selectbox(key="ep_estatus").set_value("Pendiente").run()
    sel = at.selectbox(key=at.session_state["ep_mes_wk"])
    assert sel.label == "Pendiente por mes"
    etiqueta = f"{ep.MESES_ES_CORTO[int(mes_d1[5:])]} {mes_d1[:4]} · {len(esperados)} " \
               f"{'entrega' if len(esperados) == 1 else 'entregas'}"
    assert etiqueta in list(sel.options), (etiqueta, list(sel.options))
    sel.set_value(mes_d1).run()
    sin_errores(at)
    html = _html(at)
    assert f'Pendientes</div><div class="n">{len(esperados)}</div>' in html
    assert "D-1" in html and "D-4" not in html


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


def test_pestana_caida_no_tumba_las_otras_pestanas(libros):
    import fake_gspread
    _datos(libros)
    libros.TALLER.values_get = lambda *a, **k: (_ for _ in ()).throw(fake_gspread.api_error(403))
    at = _ir("1111")
    sin_errores(at)
    assert any("rechazó el acceso" in e.value for e in at.error), "Taller avisa dentro de su pestaña"
    html = _html(at)
    assert "Pendientes por antigüedad" in _html(at, 0), "Embarques sigue en pie"
    assert 'Pendientes</div><div class="n">3</div>' in html, "Entrega Provisional sigue en pie"


def test_pestanas_de_origen_en_orden_y_todas_se_dibujan_en_una_pasada(libros):
    """El cambio de origen es una pestaña del navegador (sin ida y vuelta al servidor): las tres
    vistas ya están dibujadas en la misma pasada, con Entrega Provisional debajo de Embarques."""
    _datos(libros)
    at = abrir_app("1111")
    ir(at, "Estatus de Pago")
    sin_errores(at)
    assert [t.label for t in at.tabs][:3] == ["Embarques", "Entrega Provisional", "Repuesto y Taller"]
    assert not [x for x in at.selectbox if x.label == "Origen"], "ya no hay desplegable Origen"
    assert "Pendientes por antigüedad" in _html(at, 0)                      # Embarques
    assert 'Pendientes</div><div class="n">3</div>' in _html(at, 1)          # Entrega Provisional
    assert "Sheet de Repuesto y Taller leído" in _html(at, 2)                # Repuesto y Taller


def test_sin_taller_solo_dos_pestanas(libros, monkeypatch):
    import sheets_io
    at = abrir_app("1111")
    at.secrets["TALLER_SHEET_ID"] = ""
    ir(at, "Estatus de Pago")
    assert [t.label for t in at.tabs] == ["Embarques", "Entrega Provisional"]


def test_fechas_en_cualquier_forma_se_leen_igual(libros):
    """Texto dd/mm/aaaa (lo que Sheets deja como texto en configuración regional US), mes en letras,
    ISO, fecha real (serie) y año de 2 dígitos: todas dan la misma fecha y ningún aviso."""
    objetivo = date(2026, 11, 26)
    formas = ["26/11/2026", " 26/11/2026 ", "26-11-2026", "26/11/26", "26 nov 2026", "26-nov-26",
              "26 de noviembre de 2026", "2026-11-26", serial(objetivo), float(serial(objetivo)), str(serial(objetivo))]
    filas = [ep.COLUMNAS_EP] + [["Antillana Comercial", f"D-{n}", f"BL-{n}", "X", 1, f, f, 100, ""]
                                for n, f in enumerate(formas)]
    libros.LIBRO.agregar(ep.NOMBRE_EP, filas)
    df = ep.cargar_ep("t")["df"]
    out, avisos = ep.enriquecer_ep(df, HOY())
    assert set(out["Limite"]) == {objetivo} and set(out["Llegada"]) == {objetivo}, out[["Limite", "Llegada"]]
    assert not [a for a in avisos if "no válida" in a], avisos


def test_admin_quita_la_validacion_de_fechas_una_vez_y_el_viewer_no(libros):
    _datos(libros)
    peticiones = []
    original = libros.LIBRO.batch_update
    libros.LIBRO.batch_update = lambda body, *a, **k: (peticiones.append(body), original(body, *a, **k))[1]
    _ir("1111")
    quitadas = [r for b in peticiones for r in b.get("requests", []) if "setDataValidation" in r
                and "rule" not in r["setDataValidation"]]
    assert len(quitadas) == 3, quitadas
    peticiones.clear()
    _ir("2222")
    assert not peticiones, "el viewer nunca escribe"


def test_en_pantalla_dice_fecha_de_vencimiento_y_el_sheet_acepta_ese_encabezado(libros):
    _datos(libros)
    at = _ir("1111")
    html = _html(at)
    assert "Fecha de Vencimiento" in html and "Fecha de vencimiento" in html
    assert "Fecha de Pago" not in html and "Fecha de pago" not in html
    assert ep._canonizar_encabezados(["Fecha de Vencimiento"]) == [ep.COL_LIMITE]


def test_vencimiento_en_pantalla_como_numero_y_texto_dia_mes(libros):
    assert ep._fn(date(2026, 11, 26)) == "26 Noviembre 2026" and ep._fn(None) == "—"
    filas = [ep.COLUMNAS_EP,
             ["Antillana Comercial", "D-1", "BL-1", "GRÚA", 1, "26/10/2026", "12/02/2026", 3159249.26, ""],
             ["Antillana Comercial", "D-2", "BL-2", "X", 1, "", "01/05/2027", 100, ""],
             ["Antillana Comercial", "D-3", "BL-3", "Y", 1, "", "26/11/2026", 100, ""]]
    libros.LIBRO.agregar(ep.NOMBRE_EP, filas)
    at = _ir("1111")
    sin_errores(at)
    html = _html(at)
    for esperado in ("12 Febrero 2026", "01 Mayo 2027", "26 Noviembre 2026"):
        assert esperado in html, esperado
    assert "26/11/2026" not in html and "26/nov/2026" not in html and "26 nov 2026" not in html
