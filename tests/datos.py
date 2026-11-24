"""Libro de prueba: un Sheet realista y con casos sucios a propósito."""
from __future__ import annotations

from datetime import timedelta

import sheets_io as sio
from fake_gspread import FakeSpreadsheet

CAB = ["BL", "Descripcion", "Cantidad", "Pais_Origen", "Llegada a Puerto (ETA)", "¿Llegó? SI/NO",
       "Fecha_Declaracion", "Via_Transporte", "Dias en puerto", "Fecha_Actualización", "Actualizado_Por"]


def d(dias):
    return (sio.hoy_rd() + timedelta(days=dias)).isoformat()


def libro_principal() -> FakeSpreadsheet:
    sello = "2026-11-01 08:00:00"
    hojas = {
        "Montacargas": [CAB + ["Modelo_Serie", "CLIENTE / STOCK", "Fecha_Salida"],
            ["MON-TRANSITO", "Montacargas Yale 5T", "2", "USA", d(20), "", "", "Marítimo", "", sello, "Dauris", "GLP050", "STOCK", d(-10)],
            ["MON-PROXIMO", "Montacargas Yale 3T", "1", "usa", d(2), "", "", "", "", sello, "Dauris", "GLP030", "Cliente X", ""],
            ["MON-VENCIDO", "Apilador", "4", "China", d(-3), "", "", "Marítimo", "", sello, "Dauris", "", "", ""],
            ["MON-NOLLEGO", "Repuestos Yale", "10", "CHINA", d(-9), "NO", "", "Marítimo", "", sello, "Dauris", "", "", ""],
            ["MON-PUERTO", "Montacargas eléctrico", "1", "China", d(-4), "SI", "", "Marítimo", "", sello, "Dauris", "ERP", "STOCK", d(-40)],
            ["MON-DECL", "Montacargas diesel", "3", "Japón", d(-8), "Sí", d(-5), "Marítimo", "", sello, "Dauris", "GDP", "Cliente Y", ""],
            ["MON-DUP", "Parcial 1", "1", "China", d(-6), "SI", "", "", "", sello, "Dauris", "", "", ""],
            ["MON-DUP", "Parcial 2", "1", "China", d(5), "", "", "", "", sello, "Dauris", "", "", ""],
        ],
        "Construcción y Minería": [CAB, ["CYM-1", "Excavadora Hitachi ZX200", "1", "Japon", d(12), "", "", "Marítimo", "", sello, "Arelys"]],
        "Agrícola": [CAB, ["AGR-1", "Tractor", "2", "Brasil", "fecha mala", "", "", "", "", sello, "Arelys"]],
        "Elevadores": [CAB, ["ELE-1", "Elevador", "1", "Italia", d(-20), "SI", d(-18), "Marítimo", "", sello, "Arelys"]],
        "Generadores": [CAB, ["GEN-1", "Generador Cummins 500kW", "1", "India", d(30), "", "", "Marítimo", "", sello, "Arelys"]],
        "General": [CAB + ["OC", "EE", "Empresa"],
            ["GRL-1", "Lubricantes <b>x</b>", "40", "USA", d(-2), "SI", "", "Marítimo", "", sello, "Dauris", "4500001", "180001", "Tecnicaribe"],
            ["GRL-2", "Filtros", "100", "Mexico", d(7), "", "", "Marítimo", "", sello, "Dauris", "4500002", "", ""]],
        "Aéreos": [CAB + ["OC", "EE", "Empresa"],
            ["AIR-1", "Tarjeta electrónica", "1", "USA", d(-1), "SI", "", "", "", sello, "Dauris", "4500003", "", "antillana"],
            ["AIR-2", "Sensor", "2", "Alemania", d(1), "", "", "Aéreo", "", sello, "Dauris", "", "", "Motor Ibérico"]],
        "Carga Suelta": [CAB + ["OC", "EE", "Empresa"],
            ["RSVKHHSDQ2608001", "MANTENEDORES DE CARGAS", "5", "China", d(-4), "SI", "", "Marítimo", "", sello, "Dauris", "", "", "Antillana Comercial"]],
        "Consolidados": [CAB + ["Empresa"], ["CON-1", "Consolidado Miami", "12", "USA", d(15), "", "", "Marítimo", "", sello, "Dauris", "Tecnicaribe"]],
        "Recibido (Mes)": [list(sio.COLUMNAS_RECIBIDO)] + [
            _rec("HIS-1", "Montacargas", d(-40), d(-38), d(-30), ""),
            _rec("HIS-2", "General", d(-25), d(-22), d(-15), ""),
            _rec("HIS-3", "Generadores", d(-70), d(-66), "", "Venta CIF"),
            _rec("HIS-4", "Aéreos", d(-12), d(-11), "", "Enviado al Cliente"),
            _rec("HIS-5", "Montacargas", d(-90), d(-80), d(-60), ""),
        ],
        "Pagos": [list(sio.COLUMNAS_PAGOS)] + [
            _pago("MON-PUERTO", ADUANAS="1,200,000.50", DPH="350", **{"Fecha_SinMora": d(3), "Prioridad": "1"}),
            _pago("MON-DECL", ADUANAS="800000", FLETE="2,100.00", **{"Conceptos_Pagados": "ADUANAS", "Prioridad": "2"}),
            _pago("HIS-1", ADUANAS="500000", HIT="12000", **{"Estado_Pago": "Pagado", "Fecha_PagoRealizado": d(-28),
                                                              "Fecha_SinMora": d(-33), "PagoRealizado_DOP": "15000"}),
            _pago("HIS-2", FLETE="900", **{"Fecha_PagoRealizado": d(-30)}),
            _pago("mon-vencido", TRANSPORTE="25000"),
            _pago("SIN-TRANSITO", ADUANAS="abc", DPW="-500", **{"Prioridad": "x", "Conceptos_Pagados": "ADUANA"}),
            _pago("GRL-1"),
            _pago("ELE-1", ADUANAS="100", **{"Conceptos_Pagados": "ADUANAS", "Empresa": "TECNICARIBE"}),
        ],
        "Log": [list(sio.COLUMNAS_LOG)],
    }
    return FakeSpreadsheet(hojas)


def _rec(bl, cat, llegada, decl, almacen, tipo):
    base = {c: "" for c in sio.COLUMNAS_RECIBIDO}
    base.update({"BL": bl, "Descripcion": f"Archivado {bl}", "Cantidad": "1", "Pais_Origen": "China",
                 "Llegada a Puerto (ETA)": llegada, "Fecha_Recibido": almacen or decl, "Categoria_Origen": cat,
                 "Registrado_Por": "Dauris", "Fecha_Actualizacion": "2026-10-01 09:00:00", "Actualizado_Por": "Dauris",
                 "Fecha_Llegada_Puerto": llegada, "Fecha_Declaracion": decl, "Fecha_Almacen": almacen,
                 "Tipo_Recepcion": tipo})
    return [base[c] for c in sio.COLUMNAS_RECIBIDO]


def _pago(bl, **kw):
    base = {c: "" for c in sio.COLUMNAS_PAGOS}
    base.update({"BL": bl, "Descripcion": f"Exp {bl}", "Cantidad": "1", "Fecha_Actualizacion": "2026-11-01 08:00:00",
                 "Actualizado_Por": "Dauris"})
    base.update(kw)
    return [base[c] for c in sio.COLUMNAS_PAGOS]


def libro_taller() -> FakeSpreadsheet:
    e = sio.EPOCH_SHEETS
    hoy = sio.hoy_rd()
    s = lambda n: (hoy + timedelta(days=n) - e).days
    cab = ["EMPRESA", "SUPLIDOR", "PRIORIDAD DE PAGO", "CLIENTE", "FECHA DE SOLICITUD", "DÍAS SIN PAGAR",
           "FECHA DE PAGO", "PENDIENTE US", "PENDIENTE RD$", "DEPARTAMENTO", "LOCAL / FÁBRICA", "DESCRIPCIÓN"]
    filas = [cab,
             ["ANTILLANA", "Cummins Inc", 1, "Cliente A", s(-40), 40, "", 1500.5, "", "Repuestos", "FÁBRICA", "Filtros"],
             ["TECNICARIBE", "Proveedor local", "", "", s(-10), 10, "", "", 85000, "Taller", "LOCAL", "Servicio"],
             ["MOTOR IBÉRICO", "Pagado SA", 3, "B", s(-20), "", s(-5), 200, "", "", "", ""],
             ["ANTILLANA", "Sin fecha", 2, "", "", "", s(-1), 10, "", "", "", ""],
             ["ANTILLANA", "Fecha rara", 2, "", "no es fecha", "", "", 10, "", "", "", ""],
             ["", "", "", "", "", "", "", "", "", "", "", ""],
             ]
    libro = FakeSpreadsheet({"Repuesto y Taller": filas})
    libro.hoja("Repuesto y Taller").row_count = 50
    return libro


def libro_analitica(semilla: int = 7) -> FakeSpreadsheet:
    """Libro con ~15 meses de historia (para ver la Analítica con forma real):
    el libro principal + ~160 embarques archivados y ~25 en tránsito, al azar
    pero siempre iguales (semilla fija). Solo para pruebas y capturas."""
    import random
    rnd = random.Random(semilla)
    libro = libro_principal()
    rec = libro.hoja("Recibido (Mes)")
    cats = ["Montacargas", "Construcción y Minería", "Generadores", "General", "Agrícola", "Elevadores", "Aéreos",
            "Carga Suelta"]
    pesos = [5, 3, 4, 6, 1, 1, 4, 2]
    paises = ["USA", "China", "Japón", "Brasil", "India", "Italia", "Alemania", "México"]
    p_pesos = [8, 6, 3, 1, 2, 1, 1, 2]
    n = 1
    for meses_atras in range(15, 0, -1):
        for _ in range(rnd.randint(6, 14)):
            cat = rnd.choices(cats, pesos)[0]
            llegada = -(meses_atras * 30) + rnd.randint(-12, 12)
            if llegada > -3:
                continue
            aereo = cat == "Aéreos" or rnd.random() < 0.08
            decl = llegada + (rnd.randint(0, 3) if aereo else rnd.randint(2, 12))
            alm = min(decl + (rnd.randint(1, 4) if aereo else rnd.randint(3, 20)), -1)
            fila = _rec(f"HA-{n:04d}", cat, d(llegada), d(decl), d(alm), "")
            fila[list(sio.COLUMNAS_RECIBIDO).index("Pais_Origen")] = rnd.choices(paises, p_pesos)[0]
            if aereo:
                fila[list(sio.COLUMNAS_RECIBIDO).index("Via_Transporte")] = "Aéreo"
            rec.append_rows([fila])
            n += 1
    gen = libro.hoja("General")
    for i in range(25):
        eta = rnd.randint(-5, 80)
        gen.append_rows([["TR-%03d" % i, "Repuestos", "1", rnd.choices(paises, p_pesos)[0], d(eta), "", "",
                          rnd.choice(["Marítimo", "Marítimo", "Aéreo"]), "", "2026-11-01 08:00:00", "Dauris"]])
    libro.escrituras = libro.lecturas = 0
    return libro


def libro_ordenes() -> FakeSpreadsheet:
    """Sheet de órdenes de compra: pestañas Órdenes y Desglose con casos sucios a propósito."""
    e = sio.EPOCH_SHEETS
    hoy = sio.hoy_rd()
    s = lambda n: (hoy + timedelta(days=n) - e).days
    stamp = lambda n: s(n) + 0.5
    cab_o = ["DOCUMENTO COMPRAS", "PROVEEDOR", "RESPONSABLE", "EMPRESA", "ESTATUS ORDEN", "COMENTARIO PARA TALLER",
             "FECHA ACTUALIZACIÓN", "ACTUALIZADO POR"]
    cab_d = ["FECHA DE SOLICITUD", "NO. Solicitud de pedido", "Fecha documento", "Posición", "No. Factura",
             "Documento compras", "Material", "MAT. ANTIGUO", "Texto breve", "Cantidad de pedido",
             "Cantidad Despachada", "PENDIENTE / BACKORDER", "Solicitante", "Cliente", "ESTATUS", "TRANSPORTISTA",
             "FECHA DE EMBARQUE", "FECHA DE LLEGADA - ETA", "PUERTO", "ALMACÉN", "COMENTARIO"]
    orden = [cab_o,
             ["4300000448", "Cummins Inc", "David Tejeda", "Antillana Comercial", "ABIERTA", "Llega con la factura",
              stamp(-2), "David Tejeda"],
             ["4300000455", "Yale Materials", "David Tejeda", "Tecnicaribe", "ABIERTA", "", stamp(-12), "David Tejeda"],
             ["4300000457", "Hitachi", "Ana Pérez", "Motor Ibérico", "COMPLETA", "", stamp(-30), "Ana Pérez"],
             ["4100003641", "", "", "", "ABIERTA", "", "", ""]]
    D = lambda *a: list(a)
    desg = [cab_d,
            D(s(-30), "SP-1", s(-28), 10, "", "4300000448", "MAT-A", "", "Filtro de aceite", 10, 0, "", "Taller",
              "Cliente 1", "COLOCADA", "", "", "", "", "", ""),
            D(s(-30), "SP-1", s(-28), 20, "", "4300000448", "MAT-B", "", "Filtro de aire", 4, 4, "", "Taller", "",
              "DESPACHADO", "DHL", s(-3), s(4), "", "", ""),
            D(s(-30), "SP-1", s(-28), 30, "", "4300000448", "MAT-C", "", "Empacadura", 6, 0, "", "Taller", "",
              "COMPLETO", "", "", "", "", "", ""),
            D(s(-20), "SP-2", s(-18), 10, "", "4300000455", "MAT-D", "", "Bomba", 5, 2, "", "Taller", "",
              "BACKORDER", "", "", "", "", "", "sin fecha del fabricante"),
            D(s(-20), "SP-2", s(-18), 20, "", "4300000455", "MAT-E", "", "Sello", 8, 0, "", "Taller", "",
              "DESPACHADO A INTRANS", "", "", "", "", "", ""),
            D(s(-50), "SP-3", s(-48), 10, "", "4300000457", "MAT-F", "", "Correa", 3, 0, "", "Taller", "", "COLOCADA",
              "", "", "", "", "", ""),
            D("", "", s(-10), 10, "", "4100003641", "MAT-G", "", "Turbo", 1, 0, "", "Taller", "", "", "", "", "", "",
              "", ""),
            D("", "", s(-10), 10, "", "9999999999", "MAT-H", "", "Huérfana", 1, 0, "", "Taller", "", "COLOCADA", "",
              "", "", "", "", "")]
    libro = FakeSpreadsheet({"Instrucciones": [["Léeme"]], "Órdenes": orden, "Desglose": desg, "Listas": [["X"]]})
    libro.hoja("Órdenes").row_count = 60
    libro.hoja("Órdenes").col_count = 10
    libro.hoja("Desglose").row_count = 60
    libro.hoja("Desglose").col_count = 25
    return libro
