"""Pruebas de punta a punta: la app completa (login → secciones → escrituras)
contra un Google Sheet falso en memoria. No tocan la red ni el Sheet real."""
from datetime import timedelta

import pytest

import sheets_io as sio
from conftest import abrir_app, boton, ir, sin_errores

HOY = sio.hoy_rd
ADMIN = ["Dashboard", "Analítica", "Agregar", "Editar", "Carga masiva", "Histórico", "Estatus de Pago", "Herramientas"]
VIEWER = ["Dashboard", "Analítica", "Histórico", "Estatus de Pago"]


def escrituras_fuera_de_log(libro):
    """Estado de todas las pestañas menos la bitácora (para comprobar 'no escribió nada')."""
    return {h.title: (dict(h.celdas), h.col_count) for h in libro.hojas if h.title != "Log"}


def fila_de(hoja, bl):
    col = hoja.columna("BL")
    return next(r for r in range(2, 500) if str(hoja.real(r, col)) == bl)


def filas_con(hoja, bl):
    col = hoja.columna("BL")
    return [r for r in range(2, 500) if str(hoja.real(r, col)).strip().lower() == bl.lower()]


def ver_puerto(at):
    at.session_state["categoria_activa"] = "Puerto/Aeropuerto · Estatus"
    at.run()
    return at


# ------------------------------------------------------------------ acceso
def test_pin_incorrecto_no_entra(libros):
    at = abrir_app("9999")
    assert "rol" not in at.session_state
    assert any("PIN incorrecto" in e.value for e in at.error)


@pytest.mark.parametrize("pin,rol,secciones", [("1111", "admin", ADMIN), ("2222", "viewer", VIEWER),
                                               ("333333", "taller", VIEWER)])
def test_cada_rol_ve_sus_secciones_sin_errores(libros, pin, rol, secciones):
    at = abrir_app(pin)
    assert at.session_state["rol"] == rol
    assert list(at.segmented_control(key="seccion_actual").options) == secciones
    for s in secciones:
        ir(at, s)
        sin_errores(at)


def test_viewer_no_puede_saltar_a_seccion_de_admin(libros):
    at = abrir_app("2222", "Herramientas")
    assert at.session_state["seccion_actual"] == "Dashboard"


def test_viewer_nunca_escribe(libros):
    antes = escrituras_fuera_de_log(libros.LIBRO)
    antes_t = escrituras_fuera_de_log(libros.TALLER)
    at = abrir_app("2222")
    for s in VIEWER:
        ir(at, s)
    ver_puerto(ir(at, "Dashboard"))
    at.session_state["pago_origen_ult"] = "Repuesto y Taller"
    ir(at, "Estatus de Pago")
    sin_errores(at)
    assert escrituras_fuera_de_log(libros.LIBRO) == antes
    assert escrituras_fuera_de_log(libros.TALLER) == antes_t
    etiquetas = " ".join(str(b.label) for b in at.button)
    for prohibido in ("Confirmar declaración", "Guardar", "Eliminar", "Marcar pagado", "Agregar"):
        assert prohibido not in etiquetas


# ------------------------------------------------------- flujo de un embarque
def test_flujo_completo_alta_llegada_declaracion_almacen_reversa(libros):
    libro = libros.LIBRO
    at = abrir_app("1111", "Agregar")
    at.selectbox(key="alta_categoria").set_value("Carga Suelta").run()
    textos = {t.label: t for t in at.text_input}
    textos["BL *"].set_value("NUEVO-001")
    textos["Descripción del producto *"].set_value("Repuestos varios")
    textos["Cantidad"].set_value("3 bultos")
    textos["País de origen"].set_value("china")
    [d for d in at.date_input if d.label == "Llegada (ETA)"][0].set_value(HOY() - timedelta(days=2))
    boton(at, "Guardar embarque").click()
    at.run()
    sin_errores(at)
    cs = libro.hoja("Carga Suelta")
    f = fila_de(cs, "NUEVO-001")
    assert cs.valor(f, "Llegada a Puerto (ETA)") == (HOY() - timedelta(days=2)).isoformat()
    assert cs.valor(f, "Actualizado_Por") == "Admin Prueba"
    assert len(filas_con(libro.hoja("Pagos"), "NUEVO-001")) == 1, "el alta debe reflejarse una sola vez en Pagos"

    # Confirmar llegada desde el tablero
    ir(at, "Dashboard")
    at.session_state["categoria_activa"] = "Carga Suelta"
    at.run()
    [b for b in at.button if str(b.key or "").startswith("si_llego_") and "nuevo_001" in b.key][0].click()
    at.run()
    sin_errores(at)
    assert cs.valor(fila_de(cs, "NUEVO-001"), "¿Llegó? SI/NO") == "SI"

    # Declaración con fecha propia, junto al botón azul
    ver_puerto(at)
    clave = [d.key for d in at.date_input if d.key and d.key.startswith("fdecl_") and "nuevo_001" in d.key][0]
    at.date_input(key=clave).set_value(HOY() - timedelta(days=1))
    [b for b in at.button if "Confirmar declaración" in str(b.label)
     and "nuevo_001" in str(getattr(b, "form_id", ""))][0].click()
    at.run()
    sin_errores(at)
    assert cs.valor(fila_de(cs, "NUEVO-001"), "Fecha_Declaracion") == (HOY() - timedelta(days=1)).isoformat()

    # Recepción en almacén: archiva
    ver_puerto(at)
    [b for b in at.button if "Confirmar recepción en almacén" in str(b.label)
     and "nuevo_001" in str(getattr(b, "form_id", ""))][0].click()
    at.run()
    sin_errores(at)
    assert not filas_con(cs, "NUEVO-001"), "al archivar sale de su categoría"
    rec = libro.hoja("Recibido (Mes)")
    fr = fila_de(rec, "NUEVO-001")
    assert rec.valor(fr, "Fecha_Almacen") == HOY().isoformat()
    assert rec.valor(fr, "Fecha_Llegada_Puerto") == (HOY() - timedelta(days=2)).isoformat()
    assert rec.valor(fr, "Categoria_Origen") == "Carga Suelta"
    assert len(filas_con(rec, "NUEVO-001")) == 1

    # Reversa desde el Histórico
    ir(at, "Histórico")
    sin_errores(at)
    sel = at.selectbox(key="sel_revertir")
    sel.set_value([o for o in sel.options if o.startswith("NUEVO-001")][0]).run()
    at.button(key="btn_revertir").click()
    at.run()
    sin_errores(at)
    assert not filas_con(rec, "NUEVO-001")
    f = fila_de(cs, "NUEVO-001")
    assert cs.valor(f, "¿Llegó? SI/NO") == "SI" and cs.valor(f, "Fecha_Declaracion")


# ----------------------------------------------- correcciones en Puerto/Aeropuerto
def _correccion(at, bl_slug, opcion):
    """Abre «Corregir fechas» del embarque y elige una opción."""
    r = [r for r in at.radio if r.label == "¿Qué quieres corregir?" and bl_slug in r.key][0]
    at.session_state[r.key] = opcion
    at.run()


def test_corregir_fecha_de_llegada_y_declaracion(libros):
    m = libros.LIBRO.hoja("Montacargas")
    at = ver_puerto(abrir_app("1111"))
    _correccion(at, "mon_decl", "declaracion")
    d = [d for d in at.date_input if d.key and d.key.startswith("fdc_") and "mon_decl" in d.key][0]
    d.set_value(HOY() - timedelta(days=6)).run()
    [b for b in at.button if b.key and b.key.startswith("fdc_btn_") and "mon_decl" in b.key][0].click()
    at.run()
    sin_errores(at)
    assert m.valor(7, "Fecha_Declaracion") == (HOY() - timedelta(days=6)).isoformat()

    ver_puerto(at)
    _correccion(at, "mon_decl", "llegada")
    d = [d for d in at.date_input if d.key and d.key.startswith("fll_") and "mon_decl" in d.key][0]
    d.set_value(HOY() - timedelta(days=7)).run()
    [b for b in at.button if b.key and b.key.startswith("fll_btn_") and "mon_decl" in b.key][0].click()
    at.run()
    sin_errores(at)
    assert m.valor(7, "Llegada a Puerto (ETA)") == (HOY() - timedelta(days=7)).isoformat()
    assert m.valor(7, "¿Llegó? SI/NO") == "Sí", "corregir la llegada no toca la confirmación"


def test_llegada_posterior_a_declaracion_se_rechaza(libros):
    m = libros.LIBRO.hoja("Montacargas")
    antes = m.valor(7, "Llegada a Puerto (ETA)")
    at = ver_puerto(abrir_app("1111"))
    _correccion(at, "mon_decl", "llegada")
    d = [d for d in at.date_input if d.key and d.key.startswith("fll_") and "mon_decl" in d.key][0]
    d.set_value(HOY() - timedelta(days=1)).run()
    [b for b in at.button if b.key and b.key.startswith("fll_btn_") and "mon_decl" in b.key][0].click()
    at.run()
    assert any("después de la declaración" in e.value for e in at.error)
    assert m.valor(7, "Llegada a Puerto (ETA)") == antes


def test_quitar_declaracion_conserva_la_llegada(libros):
    m = libros.LIBRO.hoja("Montacargas")
    at = ver_puerto(abrir_app("1111"))
    _correccion(at, "mon_decl", "quitar_decl")
    [b for b in at.button if b.key and b.key.startswith("qd_btn_") and "mon_decl" in b.key][0].click()
    at.run()
    sin_errores(at)
    assert m.valor(7, "Fecha_Declaracion") == ""
    assert sio.es_llego_si(m.valor(7, "¿Llegó? SI/NO"))


def test_deshacer_llegada_devuelve_a_transito(libros):
    m = libros.LIBRO.hoja("Montacargas")
    at = ver_puerto(abrir_app("1111"))
    _correccion(at, "mon_decl", "no_llego")
    [b for b in at.button if b.key and b.key.startswith("undo_") and "mon_decl" in b.key][0].click()
    at.run()
    sin_errores(at)
    assert m.valor(7, "¿Llegó? SI/NO") == "NO" and m.valor(7, "Fecha_Declaracion") == ""


def test_declaracion_futura_se_rechaza(libros):
    cs = libros.LIBRO.hoja("Carga Suelta")
    at = ver_puerto(abrir_app("1111"))
    clave = [d.key for d in at.date_input if d.key and d.key.startswith("fdecl_") and "rsvkhh" in d.key][0]
    at.date_input(key=clave).set_value(HOY() + timedelta(days=3))
    [b for b in at.button if "Confirmar declaración" in str(b.label) and "rsvkhh" in str(getattr(b, "form_id", ""))][0].click()
    at.run()
    assert any("futura" in e.value for e in at.error)
    assert cs.valor(2, "Fecha_Declaracion") == ""


def test_venta_cif_archiva_sin_fecha_de_almacen(libros):
    libro = libros.LIBRO
    at = ver_puerto(abrir_app("1111"))
    r = [r for r in at.radio if r.label == "Destino" and "mon_decl" in r.key][0]
    r.set_value("Venta CIF")
    [b for b in at.button if "Confirmar recepción en almacén" in str(b.label)
     and "mon_decl" in str(getattr(b, "form_id", ""))][0].click()
    at.run()
    sin_errores(at)
    rec = libro.hoja("Recibido (Mes)")
    f = fila_de(rec, "MON-DECL")
    assert rec.valor(f, "Tipo_Recepcion") == "Venta CIF" and rec.valor(f, "Fecha_Almacen") == ""
    assert not filas_con(libro.hoja("Montacargas"), "MON-DECL")


def test_pantalla_vieja_no_pisa_el_cambio_de_otra_persona(libros):
    """Otra persona toca la fila después de que esta pantalla la leyó: la acción se niega."""
    cs = libros.LIBRO.hoja("Carga Suelta")
    at = ver_puerto(abrir_app("1111"))
    cs.celdas[(2, cs.columna("Fecha_Actualización"))] = "2026-11-16 23:59:59"
    cs.celdas[(2, cs.columna("Actualizado_Por"))] = "Arelys"
    [b for b in at.button if "Confirmar declaración" in str(b.label) and "rsvkhh" in str(getattr(b, "form_id", ""))][0].click()
    at.run()
    assert any("Arelys modificó" in e.value for e in at.error)
    assert cs.valor(2, "Fecha_Declaracion") == ""
    _correccion(at, "rsvkhh", "no_llego")
    [b for b in at.button if b.key and b.key.startswith("undo_") and "rsvkhh" in b.key][0].click()
    at.run()
    assert any("Arelys modificó" in e.value for e in at.error)
    assert cs.valor(2, "¿Llegó? SI/NO") == "SI"


# ------------------------------------------------------------------- editar
def test_editar_solo_toca_las_celdas_editadas(libros):
    m = libros.LIBRO.hoja("Montacargas")
    c_dias = m.columna("Dias en puerto")
    m.celdas[(2, c_dias)] = "=HOY()-E2"          # fórmula puesta a mano en el Sheet
    m.formato(2, c_dias, "-20")
    otras = {k: v for k, v in m.celdas.items() if k[0] != 2}
    at = abrir_app("1111", "Editar")
    [t for t in at.text_input if t.label == "Cantidad"][0].set_value("7 unidades")
    boton(at, "Guardar cambios").click()
    at.run()
    sin_errores(at)
    assert not at.error
    assert m.valor(2, "Cantidad") == "7 unidades"
    assert m.real(2, c_dias) == "=HOY()-E2", "la fórmula de una columna no editada debe sobrevivir"
    assert {k: v for k, v in m.celdas.items() if k[0] != 2} == otras, "ninguna otra fila cambia"
    assert m.valor(2, "Actualizado_Por") == "Admin Prueba"


def test_eta_futuro_en_embarque_confirmado_se_rechaza(libros):
    m = libros.LIBRO.hoja("Montacargas")
    at = abrir_app("1111", "Editar")
    sel = at.selectbox(key="sel_editar")
    sel.set_value([o for o in sel.options if o.startswith("MON-PUERTO")][0]).run()
    [d for d in at.date_input if d.label == "Llegada (ETA)"][0].set_value(HOY() + timedelta(days=5))
    boton(at, "Guardar cambios").click()
    at.run()
    assert any("fecha futura" in e.value for e in at.error)
    assert m.valor(6, "Llegada a Puerto (ETA)") == (HOY() - timedelta(days=4)).isoformat()


def test_eliminar_pide_confirmacion(libros):
    libro = libros.LIBRO
    at = abrir_app("1111")
    at.session_state["categoria_activa"] = "Generadores"
    at.run()
    [b for b in at.button if b.key and b.key.startswith("del_")][0].click()
    at.run()
    assert filas_con(libro.hoja("Generadores"), "GEN-1"), "el primer clic no borra"
    at.button(key="si_del_Generadores").click()
    at.run()
    sin_errores(at)
    assert not filas_con(libro.hoja("Generadores"), "GEN-1")


# --------------------------------------------------------------------- pagos
def test_pagos_guardar_conceptos_no_altera_otras_celdas(libros):
    p = libros.LIBRO.hoja("Pagos")
    f = fila_de(p, "MON-PUERTO")
    c_sm, c_dph = p.columna("Fecha_SinMora"), p.columna("DPH")
    p.celdas[(f, c_sm)] = 46350                   # fecha real del Sheet (calendario)
    p.formato(f, c_sm, HOY().strftime("%d/%m/%Y"))
    at = abrir_app("1111", "Estatus de Pago")
    sin_errores(at)
    at.selectbox(key="pago_categoria_sel").set_value("Montacargas").run()
    sel = at.selectbox(key="pago_expediente_sel")
    sel.select_index(next(i for i, o in enumerate(sel.options) if "MON-PUERTO" in str(o))).run()
    emp = [s for s in at.selectbox if s.key and s.key.startswith("pago_empresa_sel_MON-PUERTO")][0]
    emp.set_value("Antillana Comercial").run()
    n = [x for x in at.number_input if x.key and x.key.startswith("pago_monto_DPH_MON-PUERTO")][0]
    n.set_value(400.0).run()
    at.button(key="btn_guardar_conceptos").click()
    at.run()
    sin_errores(at)
    assert not at.error
    assert p.real(f, c_dph) == 400.0
    assert p.valor(f, "Empresa") == "Antillana Comercial"
    assert p.real(f, c_sm) == 46350, "una celda que el formulario no toca conserva su valor real"
    assert p.valor(f, "Prioridad") == "1"


def test_pagos_fecha_saludable_pago_realizado_estado_y_prioridad(libros):
    p = libros.LIBRO.hoja("Pagos")
    at = abrir_app("1111", "Estatus de Pago")
    f = fila_de(p, "MON-DECL")

    at.selectbox(key="sel_bl_sin_mora").set_value("MON-DECL").run()
    at.date_input(key="fecha_sin_mora_MON-DECL").set_value(HOY() + timedelta(days=4)).run()
    at.button(key="btn_sin_mora").click()
    at.run()
    sin_errores(at)
    assert p.valor(f, "Fecha_SinMora") == (HOY() + timedelta(days=4)).isoformat()

    at.selectbox(key="sel_bl_prioridad").set_value("MON-DECL").run()
    [x for x in at.number_input if x.key and x.key.startswith("num_prio_MON-DECL")][0].set_value(9).run()
    at.button(key="btn_prioridad").click()
    at.run()
    sin_errores(at)
    assert p.valor(f, "Prioridad") == 9

    at.selectbox(key="sel_bl_pago_real").set_value("MON-DECL").run()
    [x for x in at.number_input if x.key and x.key.startswith("extra_dop_pago_real_MON-DECL")][0].set_value(1500.0).run()
    at.button(key="btn_pago_real").click()
    at.run()
    sin_errores(at)
    assert p.valor(f, "Fecha_PagoRealizado") == HOY().isoformat()
    assert p.valor(f, "Estado_Pago") == "Pagado" and p.valor(f, "PagoRealizado_DOP") == 1500.0

    # Repetir el pago sin marcar «corregir» no vuelve a escribir (sin doble registro)
    antes = dict(p.celdas)
    at.selectbox(key="sel_bl_pago_real").set_value("MON-DECL").run()
    assert not [b for b in at.button if b.key == "btn_pago_real"], "ya registrado: sin botón hasta marcar corregir"
    assert dict(p.celdas) == antes


def test_pagos_sincroniza_una_sola_vez(libros):
    p = libros.LIBRO.hoja("Pagos")
    at = abrir_app("1111", "Estatus de Pago")
    sin_errores(at)
    col = p.columna("BL")
    bls = [str(p.real(r, col)).lower() for r in range(2, 200) if p.real(r, col)]
    assert len(bls) == len(set(bls)), "ningún BL duplicado tras sincronizar"
    assert "gen-1" in bls and "his-5" in bls
    n = len(bls)
    for _ in range(3):
        ir(at, "Dashboard")
        ir(at, "Estatus de Pago")
    assert len([1 for r in range(2, 200) if p.real(r, col)]) == n


# ---------------------------------------------------------------- taller
def _ir_taller(pin):
    at = abrir_app(pin)
    at.session_state["pago_origen_ult"] = "Repuesto y Taller"
    return ir(at, "Estatus de Pago")


def test_taller_agregar_pagar_y_vaciar(libros):
    t = libros.TALLER.hoja("Repuesto y Taller")
    at = _ir_taller("333333")
    sin_errores(at)
    v = at.session_state["taller_v"] if "taller_v" in at.session_state else 0
    at.selectbox(key=f"tad_{v}_emp").set_value("Tecnicaribe")
    at.text_input(key=f"tad_{v}_prov").set_value("Proveedor Nuevo")
    at.number_input(key=f"tad_{v}_usd").set_value(250.75)
    boton(at, "Agregar").click()
    at.run()
    sin_errores(at)
    assert not at.error, [e.value for e in at.error]
    assert t.real(7, 1) == "TECNICARIBE" and t.real(7, 2) == "Proveedor Nuevo" and t.real(7, 8) == 250.75
    assert t.real(7, 6) == "", "la columna de fórmula DÍAS no se toca"
    assert any("Repuesto y Taller: agregar" in str(v) for v in libros.LIBRO.hoja("Log").celdas.values())

    v = at.session_state["taller_v"]
    at.selectbox(key=f"taller_sel_{v}").set_value(7).run()
    at.button(key=f"tpm_{v}_7").click()
    at.run()
    sin_errores(at)
    assert t.real(7, 7) == (HOY() - sio.EPOCH_SHEETS).days

    v = at.session_state["taller_v"]
    at.selectbox(key=f"taller_sel_{v}").set_value(7).run()
    at.checkbox(key=f"tel_ok_{v}_7").check().run()
    at.button(key=f"tel_{v}_7").click()
    at.run()
    sin_errores(at)
    assert all(t.real(7, c) == "" for c in range(1, 10))


def test_taller_se_niega_si_el_renglon_cambio_en_el_sheet(libros):
    t = libros.TALLER.hoja("Repuesto y Taller")
    at = _ir_taller("1111")
    v = at.session_state["taller_v"] if "taller_v" in at.session_state else 0
    at.selectbox(key=f"taller_sel_{v}").set_value(2).run()
    t.celdas[(2, 8)] = 9999.0                      # alguien cambia el monto directo en el Sheet
    at.button(key=f"tpm_{v}_2").click()
    at.run()
    assert any("cambió en el Sheet" in e.value for e in at.error)
    assert t.real(2, 7) == ""


def test_viewer_en_taller_no_tiene_formularios(libros):
    at = _ir_taller("2222")
    sin_errores(at)
    assert not [b for b in at.button if "Marcar pagado" in str(b.label) or str(b.label) == "Agregar"]


# ----------------------------------------------------------- fallos de Google
def test_fallo_de_lectura_muestra_mensaje_y_no_traza(libros):
    import fake_gspread
    libros.LIBRO.values_batch_get = lambda *a, **k: (_ for _ in ()).throw(fake_gspread.api_error(403))
    at = abrir_app("1111")
    sin_errores(at)
    assert any("Google Sheets no respondió" in e.value for e in at.error)
    at2 = abrir_app("2222")
    sin_errores(at2)
    assert any("No se pudieron leer" in e.value for e in at2.error)


def test_fallo_de_escritura_se_convierte_en_mensaje(libros):
    libro = libros.LIBRO
    at = ver_puerto(abrir_app("1111"))
    libro.fallar_en = {libro._ops + 1: 403}
    [b for b in at.button if "Confirmar declaración" in str(b.label) and "rsvkhh" in str(getattr(b, "form_id", ""))][0].click()
    at.run()
    sin_errores(at)
    assert any("permisos" in e.value for e in at.error)


def test_cuota_429_se_reintenta_y_termina_bien(libros):
    libro = libros.LIBRO
    at = ver_puerto(abrir_app("1111"))
    libro.fallar_en = {libro._ops + 1: 429, libro._ops + 2: 503}
    [b for b in at.button if "Confirmar declaración" in str(b.label) and "rsvkhh" in str(getattr(b, "form_id", ""))][0].click()
    at.run()
    sin_errores(at)
    assert not at.error
    assert libro.hoja("Carga Suelta").valor(2, "Fecha_Declaracion") == HOY().isoformat()


def test_taller_caido_no_tumba_pagos_de_embarques(libros):
    import fake_gspread
    libros.TALLER.values_get = lambda *a, **k: (_ for _ in ()).throw(fake_gspread.api_error(403))
    at = _ir_taller("1111")
    sin_errores(at)
    assert any("rechazó el acceso" in e.value for e in at.error)
    at.session_state["pago_origen_ult"] = "Embarques"
    at.selectbox(key="pago_origen").set_value("Embarques").run()
    sin_errores(at)
    assert any("Pendientes por antigüedad" in m.value for m in at.markdown)


# ------------------------------------------------------------- carga masiva
def _excel(filas, columnas):
    import io
    import pandas as pd
    b = io.BytesIO()
    pd.DataFrame(filas, columns=columnas).to_excel(b, index=False)
    return b.getvalue()


def test_carga_masiva_normaliza_y_no_duplica(libros):
    libro = libros.LIBRO
    g = libro.hoja("General")
    cols = ["BL", "Descripcion", "Cantidad", "Pais Origen", "Llegada a Puerto (ETA)", "Via_Transporte", "oc", "Empresa"]
    contenido = _excel([
        ["MAS-1", "Filtros", "10", "usa", (HOY() + timedelta(days=9)).strftime("%d/%m/%Y"), "AEREO", "4500", "tecnicaribe"],
        ["MAS-2", "Aceite", None, None, (HOY() + timedelta(days=12)).isoformat(), None, None, None],
        ["GRL-1", "ya existe", "1", "USA", HOY().isoformat(), "", "", ""],
        ["MAS-1", "repetido en el archivo", "1", "USA", HOY().isoformat(), "", "", ""],
    ], cols)
    at = abrir_app("1111", "Carga masiva")
    at.selectbox(key="masiva_categoria").set_value("General").run()
    at.file_uploader(key="masiva_archivo").upload("lote.xlsx", contenido,
                                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet").run()
    sin_errores(at)
    assert not at.error, [e.value for e in at.error]
    boton(at, "Confirmar carga de 2").click()
    at.run()
    sin_errores(at)
    f1, f2 = fila_de(g, "MAS-1"), fila_de(g, "MAS-2")
    assert g.valor(f1, "Llegada a Puerto (ETA)") == (HOY() + timedelta(days=9)).isoformat()
    assert g.valor(f1, "Via_Transporte") == "Aéreo" and g.valor(f2, "Via_Transporte") == "Marítimo"
    assert g.valor(f1, "Empresa") == "Tecnicaribe" and g.valor(f1, "OC") == "4500"
    assert g.valor(f1, "Pais_Origen") == "usa", "'Pais Origen' del Excel vale por Pais_Origen"
    assert g.valor(f2, "Cantidad") == "", "celda vacía de Excel = vacío, no 'nan'"
    assert len(filas_con(g, "GRL-1")) == 1 and len(filas_con(g, "MAS-1")) == 1
    p = libro.hoja("Pagos")
    assert len(filas_con(p, "MAS-1")) == 1 and p.valor(fila_de(p, "MAS-1"), "Empresa") == "Tecnicaribe"


def test_carga_masiva_con_eta_ilegible_no_escribe_nada(libros):
    antes = escrituras_fuera_de_log(libros.LIBRO)
    cols = ["BL", "Descripcion", "Cantidad", "Pais_Origen", "Llegada a Puerto (ETA)"]
    at = abrir_app("1111", "Carga masiva")
    at.file_uploader(key="masiva_archivo").upload("lote.xlsx", _excel([["X-1", "a", "1", "USA", "pronto"]], cols),
                                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet").run()
    sin_errores(at)
    assert any("no se pudieron interpretar" in e.value for e in at.error)
    assert escrituras_fuera_de_log(libros.LIBRO) == antes


# ------------------------------------- datos sucios: ninguna pantalla se cae
SUCIOS = ["", " ", "nan", "None", "N/A", "-", "SI", "sí", "NO", "0", "-5", "1.5", "31/02/2026", "2026-13-40", "ayer",
          "46350", "1,200,000.50", "1.200.000", "RD$ 5", "=1+1", "<script>x</script>", "Aéreo", "AEREO", "Pagado",
          "pendiente", "ADUANAS, FLETE", "ADUANA", "Venta CIF", "Enviado al Cliente", "Tecnicaribe", "otra empresa",
          "á" * 300, "12/11/2026", "2026-11-12", "2019-01-01", "2031-05-05", "5 de enero 2026", "'", "%", "$", "\n"]


@pytest.mark.parametrize("semilla", range(int(__import__("os").environ.get("SEMILLAS", 12))))
def test_con_datos_sucios_ninguna_seccion_se_cae(libros, semilla):
    import random
    from fake_gspread import FakeSpreadsheet
    import datos
    rnd = random.Random(semilla)
    base = datos.libro_principal()
    hojas = {}
    for h in base.hojas:
        m = h.matriz()
        cab = m[0]
        filas = []
        for _ in range(rnd.randint(0, 14)):
            fila = [rnd.choice(SUCIOS + [datos.d(rnd.randint(-400, 60))] * 8) if rnd.random() < 0.8 else "" for _ in cab]
            if "BL" in cab and rnd.random() < 0.85:
                fila[cab.index("BL")] = rnd.choice(["A-1", "a-1", "B-2", "C-3", "D-4", " E-5 "])
            filas.append(fila)
        if rnd.random() < 0.3 and len(cab) > 3:
            cab = cab[:rnd.randint(2, len(cab))]            # a la pestaña le faltan columnas
        hojas[h.title] = [cab] + m[1:] * rnd.randint(0, 1) + filas
    if rnd.random() < 0.3:
        hojas.pop(rnd.choice(["Pagos", "Log", "Recibido (Mes)", "Consolidados"]))
    libros.LIBRO = FakeSpreadsheet(hojas)
    t = libros.TALLER.hoja("Repuesto y Taller")
    for r in range(2, 9):
        for c in range(1, 10):
            if rnd.random() < 0.6:
                t.celdas[(r, c)] = rnd.choice(SUCIOS + [46300, 46350.5, -1, 0, 1e12, True])

    for pin, secciones in (("1111", ADMIN), ("2222", VIEWER)):
        at = abrir_app(pin)
        sin_errores(at)
        for s in secciones:
            ir(at, s)
            sin_errores(at)
        ir(at, "Dashboard")
        for cat in list(at.segmented_control(key="categoria_activa").options):
            at.session_state["categoria_activa"] = cat.rsplit(" · ", 1)[0]
            at.run()
            sin_errores(at)
        at.session_state["pago_origen_ult"] = "Repuesto y Taller"
        ir(at, "Estatus de Pago")
        sin_errores(at)


# ---------------- «Acciones sobre un embarque»: sin botón de archivar sin salida
def test_acciones_indica_donde_se_pasa_a_almacen(libros):
    """Las categorías solo listan embarques en tránsito: ahí no se puede archivar,
    así que no hay botón; se indica dónde se hace."""
    antes = escrituras_fuera_de_log(libros.LIBRO)
    at = abrir_app("1111")
    at.session_state["categoria_activa"] = "Montacargas"
    at.run()
    sel = [s for s in at.selectbox if s.key and s.key.startswith("sel_accion_")][0]
    for opcion in sel.options:
        sel.set_value(opcion).run()
        sin_errores(at)
        assert not [b for b in at.button if "recibido" in str(b.label).lower() or "almacén" in str(b.label)]
        assert any("Puerto/Aeropuerto · Estatus" in c.value for c in at.caption)
    assert escrituras_fuera_de_log(libros.LIBRO) == antes


def test_chips_y_textos_dicen_declaracion(libros):
    at = ver_puerto(abrir_app("1111"))
    html = " ".join(m.value for m in at.markdown)
    assert "Recepción/declaración" not in html and "Recepción y declaración" not in html
    assert "Declaración <b>" in html


# ------------------------------------- editar: el selector no salta a otro
@pytest.mark.parametrize("campo,valor", [("Descripción", "Descripción totalmente nueva"), ("BL", "MON-PUERTO-B")])
def test_editar_se_queda_en_el_mismo_embarque(libros, campo, valor):
    at = abrir_app("1111", "Editar")
    sel = at.selectbox(key="sel_editar")
    sel.set_value(next(o for o in sel.options if o.startswith("MON-PUERTO"))).run()
    [t for t in at.text_input if t.label == campo][0].set_value(valor)
    boton(at, "Guardar cambios").click()
    at.run()
    sin_errores(at)
    assert not at.error, [e.value for e in at.error]
    actual = at.selectbox(key="sel_editar").value
    assert valor in actual, f"tras guardar, el selector quedó en {actual!r}"
    m = libros.LIBRO.hoja("Montacargas")
    assert valor in (m.valor(6, "Descripcion"), m.valor(6, "BL"))


# ----------------------------------------------------------------- analítica
def test_analitica_con_historia_dibuja_lineas_y_barras(libros):
    import datos
    libros.LIBRO = datos.libro_analitica()
    at = abrir_app("2222", "Analítica")
    sin_errores(at)
    graficas = at.get("plotly_chart")
    import json
    tipos = [{t["type"] for t in json.loads(g.proto.spec)["data"]} for g in graficas]
    assert sum("scatter" in t for t in tipos) >= 2, "líneas: llegadas por mes y días por mes"
    assert sum("bar" in t for t in tipos) >= 5
    assert not any("pie" in t for t in tipos), "sin donas"
    textos = " ".join(m.value for m in at.markdown)
    for titulo in ("Llegadas a puerto y aeropuerto por mes", "Llegadas por vía", "Embarques por categoría",
                   "Días de puerto a almacén, por mes"):
        assert titulo in textos


def test_analitica_clic_en_categoria_filtra(libros):
    import datos
    libros.LIBRO = datos.libro_analitica()
    at = abrir_app("2222", "Analítica")
    at.session_state["an_categorias"] = {"selection": {"points": [{"y": "Generadores", "x": 23}]}}
    at.run()
    sin_errores(at)
    assert any("Clic en categoría" in m.value and "Generadores" in m.value for m in at.markdown)
