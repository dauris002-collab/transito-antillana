"""Arnés de la vista Declaraciones SIGA con datos SINTÉTICOS (el repo es público: aquí no van
declaraciones reales). Lo usa tests/test_declaraciones.py vía AppTest."""
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

import declaraciones as D  # noqa: E402

MUESTRAS = [  # (partida, descripción, unidad)
    ("8427.20.00", "MONTACARGAS YALE GP050VX AMARILLO NO APLICA", "Unidades"),
    ("8428.10.00", "MONTACARGA ELECTRICO YALE ERC045VG NO APLICA", "Unidades"),
    ("8502.12.00", "GENERADOR ELECTRICO 150 KW MODELO C150D6 NO APLICA", "Unidades"),
    ("8704.22.90", "CAMION INTERNATIONAL HV607 SERIE: BASE NO APLICA", "Unidades"),
    ("8701.92.00", "TRACTOR AGRICOLA VALTRA A114 NO APLICA", "Unidades"),
    ("8432.21.00", "CRSG-RASTRA ARADORA CON SUS ACCESORIOS NO APLICA", "Unidades"),
    ("8429.52.00", "EXCAVADORA HITACHI ZX200LC-5G NO APLICA", "Unidades"),
    ("8428.10.00", "ELEVADOR DE PASAJEROS 8 PERSONAS NO APLICA", "Unidades"),
    ("8421.23.00", "FILTRO DE ACEITE REF 123 NO APLICA", "Unidades"),
    ("8708.99.90", "BOMBA DE AGUA P/ CAMION NO APLICA", "Unidades"),
    ("4016.93.00", "SELLO DE GOMA NO APLICA", "Kilogramos"),
    ("3820.00.00", "COOLANT 5 GLS NO APLICA", "Litros"),
    ("8428.10.00", "GOMA DE MONTACARGAS 5821 NO APLICA", "Unidades"),
    ("8541.43.00", "PANELES SOLARES JKM 550 NO APLICA", "Unidades"),
]
PAISES = ["ESTADOS UNIDOS", "CHINA", "BRASIL", "MÉXICO", "INDIA", "COREA DEL SUR"]
ADM = [("20050", "Aeropuerto", "Aérea"), ("10150", "Caucedo", "En contenedor"),
       ("10030", "Haina Oriental", "En contenedor"), ("10010", "Santo Domingo", "Carga suelta")]


@st.cache_data
def tablas():
    rnd = random.Random(7)
    d, it, co, ve = [], [], [], []
    n = 0
    for anio in range(2021, 2027):
        for mes in range(1, 13 if anio < 2026 else 10):
            for _ in range(rnd.randint(4, 9)):
                n += 1
                cod, adm, carga = rnd.choice(ADM)
                dec = f"{cod}-IC01-{anio % 100:02d}{mes:02d}-{n:06X}"
                d.append({"Declaracion": dec, "Empresa": rnd.choice(D.EMPRESAS), "Anio": anio, "Mes": mes,
                          "Fecha_Declaracion": f"{anio}-{mes:02d}-15", "Fecha_Llegada": f"{anio}-{mes:02d}-10",
                          "Administracion": adm, "Tipo_Carga": carga, "BL": f"BL{n:07d}", "Despacho": "GENERAL",
                          "Manifiesto": f"IGMM{n}", "Consignatario": "EMPRESA DE PRUEBA", "Agencia": "AGENCIA X",
                          "Peso_Bruto_KG": rnd.randint(10, 9000), "Detalle_Manifiesto": "1 X 40 STC"})
                for k in range(1, rnd.randint(2, 6)):
                    p, desc, u = rnd.choice(MUESTRAS)
                    it.append({"Declaracion": dec, "Item": k, "Partida": p, "Descripcion": desc, "Unidad": u,
                               "Pais_Origen": rnd.choice(PAISES), "Cantidad": rnd.randint(1, 6)})
                if carga == "En contenedor":
                    co.append({"Declaracion": dec, "Contenedor": f"MSCU{n:07d}", "Tamano_Pies": rnd.choice([20, 40]),
                               "High_Cube": rnd.choice(["Sí", "No"]), "Tipo_Empaque": "CONTENEDOR", "Sello_1": "S1"})
                if rnd.random() < 0.1:
                    ve.append({"Declaracion": dec, "Chasis": f"CH{n:08d}", "Anio_Vehiculo": anio, "Marca": "YALE",
                               "Color": "AMARILLO", "Tipo": "Normal"})
    return {"Declaraciones": pd.DataFrame(d), "Items": pd.DataFrame(it), "Contenedores": pd.DataFrame(co),
            "Vehiculos": pd.DataFrame(ve)}


if st.query_params.get("error"):
    D.cargar_historico = lambda: {"tablas": None, "hora": None, "error": "Sheet no compartido (prueba)"}
else:
    D.cargar_historico = lambda: {"tablas": tablas(), "hora": datetime(2026, 11, 17, 21, 0)}
# ?v=<1-4> abre una página; ?s.<clave>=<valor> fija un widget (listas con "|")
v = st.query_params.get("v")
if v and "dsg_pagina" not in st.session_state:
    st.session_state["dsg_pagina"] = D.PAGINAS[int(v) - 1]
for k in list(st.query_params):
    if k.startswith("s.") and k[2:] not in st.session_state:
        x = st.query_params.get(k)
        st.session_state[k[2:]] = (x.split("|") if k[2:] in ("dsg_adm", "dsg_carga", "dsg_fam", "dsg_marca", "dsg_paises")
                                   else int(x) if k[2:] == "dsg_desde" else x)
D.panel_declaraciones(es_admin=True)
