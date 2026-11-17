"""Pruebas de propiedades: miles de entradas generadas y reglas que SIEMPRE deben
cumplirse (no ejemplos sueltos). Si una regla se rompe, Hypothesis muestra el caso mínimo."""
from datetime import date, timedelta

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import logica
import sheets_io as sio
from datos import libro_principal
import fake_gspread
from fake_gspread import FakeSpreadsheet

RAPIDO = settings(max_examples=400, deadline=None, suppress_health_check=list(HealthCheck))
LENTO = settings(max_examples=40, deadline=None, suppress_health_check=list(HealthCheck))

fechas = st.dates(min_value=date(2000, 1, 1), max_value=date(2079, 12, 31))
texto_sucio = st.one_of(
    st.text(max_size=30),
    st.sampled_from(["", " ", "nan", "None", "N/A", "-", "SI", "sí", "No", "0", "1.5", "31/02/2026", "2026-13-40",
                     "ayer", "46350", "1,200,000.50", "1.200.000,50", "RD$ 5", "=1+1", "<b>x</b>", "Aéreo", "aereo",
                     "12/11/2026", "2026-11-12", "12-nov-26", "5 de enero 2026", "Pagado", "pendiente", "ADUANAS, FLETE"]),
    fechas.map(lambda d: d.isoformat()),
    fechas.map(lambda d: d.strftime("%d/%m/%Y")),
    st.floats(allow_nan=False, allow_infinity=False, width=32).map(str),
)


# ------------------------------------------------------------------ fechas
@RAPIDO
@given(texto_sucio)
def test_parsear_fecha_nunca_falla(v):
    r = sio.parsear_fecha(v)
    assert r is None or isinstance(r, date)
    assert isinstance(sio.formato_eta(v), str)
    assert logica.analizar_eta(v)["tipo"] in {"vacia", "iso", "ambigua", "convertible", "ilegible"}


@RAPIDO
@given(fechas)
def test_fecha_iso_va_y_vuelve(d):
    assert sio.parsear_fecha(d.isoformat()) == d
    assert sio.parsear_fecha(d.isoformat() + " 00:00:00") == d
    assert sio.parsear_fecha(d.strftime("%Y%m%d")) == d
    assert sio.parsear_fecha((d - sio.EPOCH_SHEETS).days) == d
    assert sio.parsear_fecha(sio.formato_eta(d)) == d, "lo que la app muestra, la app lo vuelve a leer igual"


@RAPIDO
@given(fechas)
def test_fecha_dia_mes_anio_se_lee_como_dia_primero(d):
    assert sio.parsear_fecha(d.strftime("%d/%m/%Y")) == d
    assert sio.parsear_fecha(f"{d.day} de {sio.MESES_ES[d.month].lower()} de {d.year}") == d
    assert sio.parsear_fecha(f"{d.day:02d}-{sio.MESES_ES_CORTO[d.month]}-{d.year % 100:02d}") == d


# ------------------------------------------------------------------- montos
@RAPIDO
@given(texto_sucio)
def test_a_numero_nunca_falla(v):
    r = sio.a_numero(v)
    assert r is None or (isinstance(r, float) and r == r)


@RAPIDO
@given(st.integers(min_value=0, max_value=10**11).map(lambda n: n / 100))
def test_monto_formateado_va_y_vuelve(x):
    for texto in (f"{x:,.2f}", f"{x:.2f}", f"RD$ {x:,.2f}", f"US$ {x:,.2f}",
                  f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")):   # 1.234.567,89
        assert sio.a_numero(texto) == pytest.approx(x), texto


@RAPIDO
@given(st.integers(min_value=1_000_000, max_value=10**12))
def test_miles_con_puntos_no_se_pierden(n):
    assert sio.a_numero(f"{n:,}".replace(",", ".")) == float(n)     # 1.200.000
    assert sio.a_numero(f"{n:,}") == float(n)                        # 1,200,000


@RAPIDO
@given(st.one_of(st.none(), st.booleans(), st.integers(-5, 10**17), st.floats(), st.text(max_size=5)))
def test_prioridad_valida(n):
    ok = sio.es_prioridad_valida(n)
    if ok:
        assert not isinstance(n, bool) and float(n) == int(float(n)) and 1 <= int(float(n)) <= sio.PRIORIDAD_MAX


# ------------------------------------------------------------- estado/flujo
@RAPIDO
@given(st.one_of(st.none(), fechas, texto_sucio), st.sampled_from(["", "SI", "si", "Sí", "NO", "no", "x", None]), fechas)
def test_estado_embarque(eta, llego, hoy):
    estado, dias = logica.estado_embarque(eta, llego, hoy)
    assert estado in logica.STATUS_ORDER
    f = sio.parsear_fecha(eta)
    if f is None:
        assert estado == logica.EST_SIN_FECHA
    elif sio.es_llego_si(llego):
        assert estado == logica.EST_PUERTO and dias >= 0, "confirmado = en puerto, nunca 'retrasado' ni 'en tránsito'"
    elif estado == logica.EST_RETRASADO:
        assert sio.es_llego_no(llego) and f < hoy
    assert isinstance(logica.texto_estado(estado, dias, "Aéreo"), str)


@RAPIDO
@given(st.one_of(st.none(), fechas), st.one_of(st.none(), fechas), st.one_of(st.none(), fechas))
def test_orden_del_flujo(llegada, decl, alm):
    msg = sio._validar_orden_flujo(llegada, decl, alm)
    presentes = [f for f in (llegada, decl, alm) if f]
    assert (msg == "") == (presentes == sorted(presentes))


@RAPIDO
@given(st.one_of(st.none(), fechas, texto_sucio), st.one_of(st.none(), fechas))
def test_eta_confirmado_valido_implica_no_futuro(eta, decl):
    if sio.validar_eta_confirmado(eta, decl) == "":
        f = sio.parsear_fecha(eta)
        assert f is not None and f <= sio.hoy_rd() and (decl is None or f <= decl)


fila_transito = st.fixed_dictionaries({
    sio.COL_BL: st.one_of(st.sampled_from(["A1", "a1", "B2", ""]), st.text(max_size=8)),
    sio.COL_DESC: texto_sucio, sio.COL_CANT: texto_sucio, sio.COL_PAIS: texto_sucio,
    sio.COL_ETA: texto_sucio, sio.COL_LLEGO: st.sampled_from(["", "SI", "Sí", "NO", "tal vez"]),
    sio.COL_FECHA_DECLARACION: texto_sucio, sio.COL_VIA: st.sampled_from(["", "Aéreo", "AEREO", "Marítimo", "x"]),
    sio.COL_FECHA_SALIDA: texto_sucio, sio.COL_EMPRESA_EMBARQUE: texto_sucio,
    "Categoria": st.sampled_from(sio.CATEGORIAS), "FilaSheet": st.integers(2, 500),
})


@LENTO
@given(st.lists(fila_transito, max_size=12))
def test_enriquecer_aguanta_datos_sucios(filas):
    df = pd.DataFrame(filas, columns=list(fila_transito.wrapped_strategy.mapping) if hasattr(fila_transito, "wrapped_strategy") else None) \
        if filas else pd.DataFrame(columns=[sio.COL_BL, sio.COL_DESC, sio.COL_CANT, sio.COL_PAIS, sio.COL_ETA])
    for c in sio.ALL_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    out = logica.enriquecer(df)
    assert len(out) == len(df)
    for _, r in out.iterrows():
        confirmado = sio.es_llego_si(r[sio.COL_LLEGO])
        assert (r["F_Puerto"] is not None and not pd.isna(r["F_Puerto"])) <= confirmado, \
            "sin '¿Llegó?' = SI no hay fecha de llegada"
        if not confirmado:
            assert r["DiasEnPuerto"] is None or pd.isna(r["DiasEnPuerto"]), "el reloj de puerto no corre sin confirmar"
        assert r["EstadoTexto"] in logica.STATUS_ORDER
        assert r["EmpresaTransito"]
    logica.resumen_atraso_puerto(out)
    logica.contar_activos_por_mes_eta(out)


# -------------------------------------------------------------------- pagos
fila_pago = st.fixed_dictionaries({
    **{c: st.one_of(st.just(""), texto_sucio, st.integers(0, 10**7).map(str)) for c in sio.CONCEPTOS_PAGO},
    sio.COL_BL: st.sampled_from(["A1", "a1 ", "B2", "ZZ", ""]), sio.COL_DESC: texto_sucio,
    sio.COL_EMPRESA: st.sampled_from(["", "tecnicaribe", "Motor Ibérico", "Otra"]),
    sio.COL_ESTADO_PAGO: st.sampled_from(["", "Pagado", "pagado", "Pendiente", "x"]),
    sio.COL_FECHA_SIN_MORA: texto_sucio, sio.COL_FECHA_PAGO_REAL: texto_sucio,
    sio.COL_PAGOREAL_USD: texto_sucio, sio.COL_PAGOREAL_DOP: texto_sucio,
    sio.COL_CONCEPTOS_PAGADOS: st.one_of(texto_sucio, st.lists(st.sampled_from(sio.CONCEPTOS_PAGO + ["ADUANA"]),
                                                               max_size=5).map(", ".join)),
    sio.COL_PRIORIDAD: texto_sucio, sio.COL_PAGO_LLEGADA: texto_sucio,
})


@LENTO
@given(st.lists(fila_pago, max_size=10), st.lists(fila_transito, max_size=6))
def test_enriquecer_pagos_aguanta_datos_sucios(pagos, transito):
    dfp = pd.DataFrame(pagos)
    for c in sio.COLUMNAS_PAGOS:
        if c not in dfp.columns:
            dfp[c] = ""
    act = pd.DataFrame(transito)
    for c in sio.ALL_COLUMNS + ["Categoria", "FilaSheet"]:
        if c not in act.columns:
            act[c] = ""
    hist = pd.DataFrame(columns=sio.COLUMNAS_RECIBIDO)
    out = logica.enriquecer_pagos(dfp, act, hist)
    assert len(out) == len(dfp)
    for _, r in out.iterrows():
        assert r["EstadoEfectivo"] in ("Pagado", "Pendiente")
        if r["FechaPagoRealParsed"] is not None and not pd.isna(r["FechaPagoRealParsed"]):
            assert r["EstadoEfectivo"] == "Pagado", "con fecha de pago real siempre es Pagado (decisión 9)"
        assert set(r["ConceptosPagados"]) <= set(sio.CONCEPTOS_PAGO)
        ta, tp = r["TotalActual"], r["TotalPendiente"]
        assert (ta is None) == (tp is None)
        if ta is not None and not r["ConceptosPagados"]:
            assert ta == tp, "sin cotejo, lo pendiente es el total"
    res = logica.resumen_pagos(out[out["TieneMontos"]])
    assert res["n_pagados"] + res["n_abiertos"] == int(out["TieneMontos"].sum())
    assert res["dias_pago_promedio"] is None or res["dias_pago_promedio"] >= 0, "decisión 32: nunca negativo"
    assert res["sobrecosto"]["USD"] >= 0 and res["sobrecosto"]["DOP"] >= 0
    logica.llegadas_desfasadas(dfp, act, hist)
    logica.pagos_sin_empresa(dfp, act, hist)


@RAPIDO
@given(st.dictionaries(st.sampled_from(sio.CONCEPTOS_PAGO), st.integers(0, 10**9), max_size=13),
       st.sets(st.sampled_from(sio.CONCEPTOS_PAGO)))
def test_cotejo_nunca_aumenta_lo_pendiente(montos, cotejados):
    fila = {c: str(v) for c, v in montos.items()}
    fila[sio.COL_CONCEPTOS_PAGADOS] = "; ".join(cotejados)
    total = logica.totales_conceptos(fila)
    pend = logica.totales_conceptos(fila, excluir=set(logica.conceptos_cotejados(fila)[0]))
    if total is None:
        assert pend is None
        return
    for m in ("USD", "DOP"):
        assert 0 <= pend[m] <= total[m]
        esperado = sum(v for c, v in montos.items() if sio.MONEDA_CONCEPTO[c] == m and c not in cotejados)
        assert pend[m] == pytest.approx(esperado), "USD y DOP nunca se mezclan"


@RAPIDO
@given(st.lists(st.tuples(st.one_of(st.none(), st.integers(1, 50)), st.one_of(st.none(), st.integers(-5, 400))), max_size=15))
def test_orden_de_pago_prioridad_siempre_manda(items):
    import pagos
    orden = sorted(items, key=lambda t: pagos._clave_antiguedad(t[0], t[1], None))
    prios = [p for p, _ in orden]
    con = [p for p in prios if p is not None]
    assert prios == con + [None] * (len(prios) - len(con)), "sin prioridad siempre debajo (decisión 11)"
    assert con == sorted(con)


# -------------------------------- escrituras: reintentar nunca duplica ni pisa
def _libro():
    import streamlit
    streamlit.cache_data.clear()
    streamlit.cache_resource.clear()
    libro = libro_principal()
    sio.get_spreadsheet = lambda: libro
    return libro


def _bls(hoja):
    c = hoja.columna("BL")
    return [sio._norm(hoja.real(r, c)) for r in range(2, 400) if hoja.real(r, c) != ""]


@LENTO
@given(st.integers(1, 4), st.data())
def test_sincronizar_pagos_es_idempotente(veces, data):
    libro = _libro()
    datos = sio.cargar_todo()
    for _ in range(veces):
        ok, _msg = sio.sincronizar_pagos_con_transito(datos["activos"], datos["historico"])
        assert ok
    bls = _bls(libro.hoja("Pagos"))
    assert len(bls) == len(set(bls)), "un BL nunca queda dos veces en Pagos"


ACCIONES = st.sampled_from(["si", "no", "declarar", "quitar_decl", "editar", "archivar", "eliminar"])


@LENTO
@given(st.lists(st.tuples(ACCIONES, st.integers(0, 30), st.integers(-10, 10), st.booleans()), min_size=1, max_size=8))
def test_una_escritura_solo_toca_su_fila(pasos):
    """Cualquier secuencia de acciones sobre una fila al azar: las DEMÁS filas de la
    pestaña quedan idénticas, ningún embarque se duplica ni desaparece sin archivarse."""
    libro = _libro()
    for accion, cual, delta, repetir in pasos:
        sio.invalidar_caches()
        activos = sio.cargar_todo()["activos"]
        if activos.empty:
            break
        fila = activos.iloc[cual % len(activos)]
        cat, bl, n = fila["Categoria"], fila[sio.COL_BL], int(fila["FilaSheet"])
        hoja = libro.hoja(cat)
        ajenas = {k: v for k, v in hoja.celdas.items() if k[0] != n}
        total_antes = sum(len(_bls(libro.hoja(c))) for c in sio.CATEGORIAS) + len(_bls(libro.hoja("Recibido (Mes)")))
        fecha = sio.hoy_rd() + timedelta(days=delta)
        sello = fila[sio.COL_ACTUALIZACION]

        def ejecutar():
            if accion == "si":
                return sio.confirmar_llegada(bl, cat, fila_sugerida=n, sello_esperado=sello)
            if accion == "no":
                return sio.marcar_no_llego(bl, cat, fila_sugerida=n, sello_esperado=sello)
            if accion == "declarar":
                return sio.fijar_fecha_declaracion(bl, cat, fecha=fecha, fila_sugerida=n, sobrescribir=True)
            if accion == "quitar_decl":
                return sio.avanzar_estado_puerto(bl, cat, sio.ETAPA_LLEGADA, fila_sugerida=n)
            if accion == "editar":
                return sio.actualizar_embarque(bl, cat, {sio.COL_CANT: str(delta), sio.COL_ETA: fecha.isoformat()},
                                               fila_sugerida=n, sello_esperado=sello)
            if accion == "archivar":
                return sio.marcar_como_recibido(bl, cat, fila_sugerida=n, fecha_almacen=fecha)
            return sio.eliminar_embarque(bl, cat, fila_sugerida=n)

        ok, _msg = ejecutar()
        if repetir:
            ejecutar()          # doble clic / reintento: no debe duplicar ni romper nada
        salio = ok and accion in ("archivar", "eliminar")
        if salio:
            # la fila se fue: las de abajo suben una posición, con el mismo contenido
            esperado = {((r - 1 if r > n else r), c): v for (r, c), v in ajenas.items()}
            if not repetir:
                assert dict(hoja.celdas) == esperado
        else:
            assert {k: v for k, v in hoja.celdas.items() if k[0] != n} == ajenas, f"{accion} tocó otra fila"
        total = sum(len(_bls(libro.hoja(c))) for c in sio.CATEGORIAS) + len(_bls(libro.hoja("Recibido (Mes)")))
        if not repetir:
            assert total == total_antes - (1 if (ok and accion == "eliminar") else 0), \
                "archivar mueve, no duplica ni pierde"
        # Invariantes del Sheet tras cualquier acción aceptada
        for c in sio.CATEGORIAS:
            h = libro.hoja(c)
            for r in range(2, 60):
                if h.real(r, 1) == "":
                    continue
                llego, decl = h.valor(r, "¿Llegó? SI/NO"), sio.parsear_fecha(h.valor(r, "Fecha_Declaracion"))
                eta = sio.parsear_fecha(h.valor(r, "Llegada a Puerto (ETA)"))
                if ok and (c, r) == (cat, n) and not salio:
                    if sio.es_llego_si(llego):
                        assert eta is not None and eta <= sio.hoy_rd(), "confirmado con ETA futuro o ilegible"
                        assert decl is None or (eta <= decl <= sio.hoy_rd()), "declaración fuera de orden o futura"
                    else:
                        assert decl is None or accion == "editar", "declaración sin llegada confirmada"


@LENTO
@given(st.sampled_from(["MON-DECL", "ELE-1"]), st.integers(1, 3), st.sampled_from([403, 500]))
def test_archivar_con_fallo_a_medias_no_duplica_al_reintentar(bl, intentos, codigo):
    """Archivar son dos escrituras. Si la segunda (borrar del origen) falla, repetir
    la acción no puede dejar el embarque dos veces en el histórico."""
    libro = _libro()
    cat = "Montacargas" if bl.startswith("MON") else "Elevadores"
    rec = libro.hoja("Recibido (Mes)")
    n_fallos = 1 if codigo == 403 else sio.REINTENTOS_API
    libro.fallar_en = {libro._ops + 2 + i: codigo for i in range(n_fallos)}   # 1ª op = append; 2ª = borrar
    ok, msg = sio.marcar_como_recibido(bl, cat, fecha_almacen=sio.hoy_rd())
    assert not ok and "No cambió nada" in msg
    assert _bls(rec).count(sio._norm(bl)) == 0 and _bls(libro.hoja(cat)).count(sio._norm(bl)) == 1
    for _ in range(intentos):
        sio.marcar_como_recibido(bl, cat, fecha_almacen=sio.hoy_rd())
    assert _bls(rec).count(sio._norm(bl)) == 1
    assert _bls(libro.hoja(cat)).count(sio._norm(bl)) == 0


def test_archivar_si_tampoco_se_puede_deshacer_avisa_que_no_se_repita():
    libro = _libro()
    libro.fallar_en = {libro._ops + 2 + i: 403 for i in range(3)}    # falla borrar el origen Y deshacer la copia
    ok, msg = sio.marcar_como_recibido("MON-DECL", "Montacargas", fecha_almacen=sio.hoy_rd())
    assert not ok and "NO repitas la acción" in msg


def test_archivar_no_deshace_la_copia_si_el_borrado_si_se_aplico():
    """Google devuelve error al borrar el origen pero el borrado SÍ ocurrió: la copia del
    archivo no se toca (deshacerla dejaría el embarque fuera de las dos pestañas)."""
    libro = _libro()
    m, rec = libro.hoja("Montacargas"), libro.hoja("Recibido (Mes)")
    original = m.delete_rows

    def borra_y_falla(fila):
        original(fila)
        raise fake_gspread.api_error(403)
    m.delete_rows = borra_y_falla
    ok, msg = sio.marcar_como_recibido("MON-DECL", "Montacargas", fecha_almacen=sio.hoy_rd())
    assert not ok and "NO repitas la acción" in msg
    assert _bls(rec).count("mon-decl") == 1 and _bls(m).count("mon-decl") == 0


@LENTO
@given(st.integers(1, 3))
def test_guardar_pago_repetido_no_crea_filas(veces):
    libro = _libro()
    p = libro.hoja("Pagos")
    antes = len(_bls(p))
    for _ in range(veces):
        assert sio.guardar_pago("BL-NUEVO-X", {"ADUANAS": 100.0, "FLETE": ""}, empresa="Tecnicaribe")[0]
        assert sio.guardar_pago("bl-nuevo-x ", {"ADUANAS": 250.0})[0]     # mismo BL con otra grafía
    assert len(_bls(p)) == antes + 1
    f = _bls(p).index("bl-nuevo-x") + 2
    assert p.valor(f, "ADUANAS") == 250.0 and p.valor(f, "Empresa") == "Tecnicaribe"


@RAPIDO
@given(st.lists(st.sampled_from(["A", "a", "B", "b ", "C", ""]), min_size=1, max_size=8), st.sampled_from(["A", "B", "C", "Z"]),
       st.one_of(st.none(), st.integers(1, 10)))
def test_localizar_fila_nunca_devuelve_otro_bl(bls, objetivo, sugerida):
    import streamlit
    streamlit.cache_data.clear()
    streamlit.cache_resource.clear()
    libro = FakeSpreadsheet({"General": [["BL", "Descripcion"]] + [[b, "x"] for b in bls]})
    sio.get_spreadsheet = lambda: libro
    fila, error = sio._localizar_fila(sio.get_worksheet("General"), objetivo, sugerida)
    coincidencias = [i + 2 for i, b in enumerate(bls) if sio._norm(b) == sio._norm(objetivo)]
    if fila is not None:
        assert fila in coincidencias and error == ""
        assert len(coincidencias) == 1 or fila == sugerida, "con BL repetido solo vale la fila que la pantalla mostró"
    else:
        assert error and (not coincidencias or len(coincidencias) > 1)
