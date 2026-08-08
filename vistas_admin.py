"""
vistas_admin.py — Formularios y herramientas de administración de Antillana Comercial.

Alta manual, edición, carga masiva, histórico y el panel de Herramientas
(normalización de fechas, salud de los datos, respaldo, bitácora). Usa
Streamlit e importa de sheets_io.py, logica.py y ui_componentes.py.
"""

from __future__ import annotations

import io

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from sheets_io import (
    CATEGORIAS, COL_ACTUALIZACION, COL_BL, COL_CANT, COL_DESC, COL_EE, COL_ETA,
    COL_FECHA_ALMACEN, COL_FECHA_LLEGADA_PUERTO, COL_FECHA_PAGO, COL_FECHA_SALIDA,
    COL_FECHA_SOLICITUD_PAGO, COL_MODELO, COL_OC, COL_PAIS, MESES_ES, MESES_ES_CORTO,
    NO_ESPECIFICADO, REQUIRED_COLUMNS, _con_reintento, _leer_log, _norm,
    _refrescar_estructura, _validar_orden_flujo, actualizar_embarque, ahora_rd,
    append_row, append_rows_bulk, es_numero, get_spreadsheet, hoy_rd,
    invalidar_caches, normalizar_etas, parsear_fecha, quitar_de_recibido,
    registrar_log, sla_etapas,
)
from logica import (
    CATEGORIAS_CON_OC_EE, EST_SIN_FECHA, ETIQUETA_CORTA_ETAPA, _columna_fechas,
    _etiquetas_desambiguadas, analizar_eta, enriquecer, fechas_flujo_de_fila,
)
from ui_componentes import (
    COLOR_RECIBIDAS_MES, COLOR_TOTAL, CUSTOM_CSS, VERSION_APP, _df_a_excel,
    tarjeta_kpi,
)


# ---------------------------------------------------------------------------
# ALTA MANUAL
# ---------------------------------------------------------------------------
def _bls_existentes(datos: dict) -> set:
    activos = set(datos["activos"][COL_BL].astype(str).str.strip()) if not datos["activos"].empty else set()
    historicos = set(datos["historico"][COL_BL].astype(str).str.strip()) if not datos["historico"].empty else set()
    return {b for b in activos | historicos if b}


def form_alta_manual(datos: dict):
    st.subheader("Agregar embarque")
    # La Categoría vive FUERA del form: así, al elegir Aéreos o Carga Suelta, la
    # app puede mostrar los campos OC/EE de una vez (dentro de un st.form los
    # demás widgets no reaccionan hasta el submit).
    categoria = st.selectbox("Categoría", CATEGORIAS, key="alta_categoria")
    con_oc_ee = categoria in CATEGORIAS_CON_OC_EE
    with st.form("form_embarque", clear_on_submit=True):
        c1, c2 = st.columns(2)
        bl = c1.text_input("BL *")
        descripcion = c2.text_input("Descripción del producto *")
        c3, c4 = st.columns(2)
        modelo = c3.text_input("Modelo o serie")
        cantidad = c4.text_input("Cantidad", placeholder="Ej.: 4 unidades, 2 pallets, 113 bultos")
        c5, c6 = st.columns(2)
        pais = c5.text_input("País de origen")
        eta = c6.date_input("Llegada a puerto (ETA)", value=hoy_rd(), format="DD/MM/YYYY")
        oc = ee = ""
        if con_oc_ee:
            c7, c8 = st.columns(2)
            oc = c7.text_input("OC")
            ee = c8.text_input("EE")
        salida = st.date_input("Fecha de salida (opcional)", value=None, format="DD/MM/YYYY",
                               help="Si no la sabes, déjala vacía y agrégala después desde 'Editar'.")
        enviado = st.form_submit_button("Guardar embarque", type="primary")

    if not enviado:
        return

    if not bl.strip() or not descripcion.strip():
        st.error("BL y Descripción son obligatorios.")
        return
    if bl.strip() in _bls_existentes(datos):
        st.error(f"Ya existe un embarque con el BL '{bl.strip()}' (activo o en el histórico). "
                 "Si es un embarque parcial del mismo BL, agrégale un sufijo (por ejemplo "
                 f"'{bl.strip()}-2') para poder distinguirlos.")
        return
    if salida and salida > eta:
        st.error("La fecha de salida no puede ser posterior al ETA.")
        return

    datos_nuevos = {
        COL_BL: bl.strip(),
        COL_DESC: descripcion.strip(),
        COL_MODELO: modelo.strip(),
        COL_CANT: cantidad.strip(),
        COL_PAIS: pais.strip(),
        COL_ETA: eta.isoformat(),
    }
    if salida:
        datos_nuevos[COL_FECHA_SALIDA] = salida.isoformat()
    if con_oc_ee:
        if oc.strip():
            datos_nuevos[COL_OC] = oc.strip()
        if ee.strip():
            datos_nuevos[COL_EE] = ee.strip()

    ok, mensaje = append_row(datos_nuevos, categoria)
    if ok:
        registrar_log("Alta manual", bl.strip(), categoria, f"ETA {eta.isoformat()}")
        invalidar_caches()
        st.success(f"Embarque {bl.strip()} guardado en '{categoria}'.")
        st.rerun()
    else:
        st.error(mensaje)


# ---------------------------------------------------------------------------
# EDICIÓN
# ---------------------------------------------------------------------------
def form_editar(datos: dict):
    st.subheader("Editar embarque")
    df = datos["activos"]
    if df.empty:
        st.info("No hay embarques cargados.")
        return

    con_bl = df[df[COL_BL].astype(str).str.strip() != ""].reset_index(drop=True)
    if con_bl.empty:
        st.info("No hay embarques con BL asignado.")
        return

    opciones = _etiquetas_desambiguadas(con_bl, largo_desc=40)
    indice_default = 0
    preseleccion = st.session_state.pop("editar_bl", None)
    fila_preseleccion = st.session_state.pop("editar_fila", None)
    if preseleccion:
        for i, r in con_bl.iterrows():
            mismo_bl = str(r[COL_BL]).strip() == preseleccion
            misma_fila = (not fila_preseleccion) or str(r.get("FilaSheet")) == str(fila_preseleccion)
            if mismo_bl and misma_fila:
                indice_default = int(i)
                break

    elegido = st.selectbox("Embarque a editar", opciones, index=indice_default, key="sel_editar")
    fila = con_bl.iloc[opciones.index(elegido)]
    categoria = fila["Categoria"]
    bl_original = str(fila[COL_BL]).strip()
    n_fila = fila.get("FilaSheet")
    eta_actual = parsear_fecha(fila[COL_ETA]) or hoy_rd()
    salida_actual = parsear_fecha(fila.get(COL_FECHA_SALIDA, ""))
    sello = str(fila.get(COL_ACTUALIZACION, "")).strip()
    con_oc_ee = categoria in CATEGORIAS_CON_OC_EE

    with st.form("form_editar"):
        c0, c1 = st.columns(2)
        bl_nuevo = c0.text_input("BL", value=bl_original,
                                 help="Solo cámbialo si venía mal escrito. Es el identificador del embarque.")
        descripcion = c1.text_input("Descripción", value=str(fila[COL_DESC]))
        c2, c3 = st.columns(2)
        modelo = c2.text_input("Modelo o serie", value=str(fila[COL_MODELO]))
        cantidad = c3.text_input("Cantidad", value=str(fila[COL_CANT]))
        c4, c5 = st.columns(2)
        pais = c4.text_input("País de origen", value=str(fila[COL_PAIS]))
        eta = c5.date_input("Llegada a puerto (ETA)", value=eta_actual, format="DD/MM/YYYY")
        salida = st.date_input("Fecha de salida", value=salida_actual, format="DD/MM/YYYY")
        oc = ee = ""
        if con_oc_ee:
            c6, c7 = st.columns(2)
            oc = c6.text_input("OC", value=str(fila.get(COL_OC, "")))
            ee = c7.text_input("EE", value=str(fila.get(COL_EE, "")))
        st.caption(f"Fila {n_fila} de '{categoria}' · ETA actual en el Sheet: {fila[COL_ETA] or '(vacío)'}")
        forzar = st.checkbox("Sobrescribir aunque otra persona lo haya cambiado mientras tanto")
        guardar = st.form_submit_button("Guardar cambios", type="primary")

    if not guardar:
        return

    if not bl_nuevo.strip():
        st.error("El BL no puede quedar vacío.")
        return
    if salida and salida > eta:
        st.error("La fecha de salida no puede ser posterior al ETA.")
        return
    if bl_nuevo.strip() != bl_original and bl_nuevo.strip() in _bls_existentes(datos):
        st.error(f"Ya existe otro embarque con el BL '{bl_nuevo.strip()}'.")
        return

    cambios = {
        COL_BL: bl_nuevo.strip(),
        COL_DESC: descripcion.strip(),
        COL_MODELO: modelo.strip(),
        COL_CANT: cantidad.strip(),
        COL_PAIS: pais.strip(),
        COL_ETA: eta.isoformat(),
        COL_FECHA_SALIDA: salida.isoformat() if salida else "",
    }
    if con_oc_ee:
        cambios[COL_OC] = oc.strip()
        cambios[COL_EE] = ee.strip()

    ok, mensaje = actualizar_embarque(bl_original, categoria, cambios, fila_sugerida=n_fila,
                                      sello_esperado=sello, forzar=forzar)
    if ok:
        detalles = []
        if str(fila[COL_ETA]).strip() != eta.isoformat():
            detalles.append(f"ETA {fila[COL_ETA] or 'vacío'} → {eta.isoformat()}")
        if bl_nuevo.strip() != bl_original:
            detalles.append(f"BL {bl_original} → {bl_nuevo.strip()}")
        registrar_log("Edición", bl_nuevo.strip(), categoria, "; ".join(detalles) or "campos varios")
        invalidar_caches()
        st.success("Embarque actualizado.")
        st.rerun()
    else:
        st.error(mensaje)


# ---------------------------------------------------------------------------
# CARGA MASIVA
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _plantilla_excel() -> bytes:
    buffer = io.BytesIO()
    ejemplo = pd.DataFrame(
        [{
            COL_BL: "EGLV142653674620",
            COL_DESC: "Montacargas",
            COL_MODELO: "ERP3.0MXLG / ERP20UXTL",
            COL_CANT: "4 unidades",
            COL_PAIS: "China",
            COL_ETA: "2026-08-25",
            COL_FECHA_SALIDA: "",
        }],
        columns=REQUIRED_COLUMNS + [COL_FECHA_SALIDA],
    )
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        ejemplo.to_excel(writer, index=False, sheet_name="Embarques")
    return buffer.getvalue()


def form_carga_masiva(datos: dict):
    st.subheader("Carga masiva desde Excel")
    st.download_button(
        "Descargar plantilla",
        data=_plantilla_excel(),
        file_name="plantilla_embarques_antillana.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    categoria = st.selectbox("Categoría de destino (todo el archivo se carga aquí)", CATEGORIAS)
    con_oc_ee = categoria in CATEGORIAS_CON_OC_EE
    columnas_opcionales = [COL_FECHA_SALIDA] + ([COL_OC, COL_EE] if con_oc_ee else [])
    extra_txt = f", '{COL_OC}' y '{COL_EE}'" if con_oc_ee else ""
    st.caption("Columnas obligatorias: " + ", ".join(REQUIRED_COLUMNS) +
               f". '{COL_FECHA_SALIDA}'{extra_txt} son opcionales. El ETA puede venir en cualquier "
               "formato reconocible; se guarda como AAAA-MM-DD.")

    archivo = st.file_uploader("Archivo .xlsx", type=["xlsx"])
    if archivo is None:
        return

    try:
        nuevo = pd.read_excel(archivo, dtype=str)
    except Exception as e:
        st.error(f"No se pudo leer el archivo: {e}")
        return

    mapa = {}
    for canon in REQUIRED_COLUMNS + columnas_opcionales:
        for real in nuevo.columns:
            if _norm(real) == _norm(canon):
                mapa[real] = canon
                break
    nuevo = nuevo.rename(columns=mapa)

    faltantes = [c for c in REQUIRED_COLUMNS if c not in nuevo.columns]
    if faltantes:
        st.error(f"Faltan columnas obligatorias: {', '.join(faltantes)}. Usa la plantilla.")
        return

    columnas_a_usar = REQUIRED_COLUMNS + [c for c in columnas_opcionales if c in nuevo.columns]
    nuevo = nuevo[columnas_a_usar].dropna(how="all").copy()
    nuevo = nuevo[nuevo[COL_BL].astype(str).str.strip().ne("") | nuevo[COL_DESC].astype(str).str.strip().ne("")]

    invalidas, normalizadas = [], []
    for i, valor in enumerate(nuevo[COL_ETA]):
        f = parsear_fecha(valor)
        if f is None:
            invalidas.append((i + 2, valor))
            normalizadas.append("")
        else:
            normalizadas.append(f.isoformat())
    if invalidas:
        st.error("Hay fechas ETA que no se pudieron interpretar en las filas: " +
                 ", ".join(f"{fila} ('{val}')" for fila, val in invalidas))
        return
    nuevo[COL_ETA] = normalizadas

    if COL_FECHA_SALIDA in nuevo.columns:
        # Opcional: si viene ilegible se deja en blanco, en vez de bloquear la
        # carga completa por una columna que no es obligatoria.
        nuevo[COL_FECHA_SALIDA] = [
            (parsear_fecha(v).isoformat() if parsear_fecha(v) else "") for v in nuevo[COL_FECHA_SALIDA]
        ]

    existentes = _bls_existentes(datos)
    bl_norm = nuevo[COL_BL].astype(str).str.strip()
    dup_archivo = bl_norm.duplicated(keep="first") & bl_norm.ne("")
    if dup_archivo.any():
        st.warning("El archivo trae BL repetidos internamente; solo se cargará la primera aparición de: " +
                   ", ".join(sorted(set(bl_norm[dup_archivo]))))
    es_dup = bl_norm.isin(existentes) | dup_archivo
    if es_dup.any():
        ya = sorted({b for b in bl_norm[bl_norm.isin(existentes)] if b})
        if ya:
            st.warning("Estos BL ya existen en el sistema y no se cargarán de nuevo: " + ", ".join(ya))

    nuevos = nuevo[~es_dup]
    if nuevos.empty:
        st.info("No hay embarques nuevos que cargar.")
        return

    st.write(f"Vista previa de {len(nuevos)} embarque(s) que se cargarán en **{categoria}**:")
    st.dataframe(nuevos, width="stretch", hide_index=True)

    if st.button(f"Confirmar carga de {len(nuevos)} embarque(s)", type="primary"):
        ok, mensaje = append_rows_bulk(nuevos, categoria)
        if ok:
            registrar_log("Carga masiva", "", categoria, f"{len(nuevos)} embarque(s)")
            invalidar_caches()
            st.success(f"{len(nuevos)} embarque(s) cargado(s) en '{categoria}'.")
            st.rerun()
        else:
            st.error(mensaje)


# ---------------------------------------------------------------------------
# HISTÓRICO
# ---------------------------------------------------------------------------
def _preparar_historico(historico: pd.DataFrame) -> pd.DataFrame:
    """Histórico crudo -> DataFrame con fecha parseada, año, mes y tiempos de
    ciclo. Nada se borra nunca de la pestaña 'Recibido (Mes)': cada recepción
    queda ahí con su fecha, así que en noviembre se puede consultar julio del año
    pasado igual que el mes en curso."""
    df = historico.copy()
    if df.empty:
        return df
    df["FechaParsed"] = [parsear_fecha(v) for v in df.get("Fecha_Recibido", [])]
    df = df[df["FechaParsed"].notna()].copy()
    if df.empty:
        return df
    df["Anio"] = [f.year for f in df["FechaParsed"]]
    df["Mes"] = [f.month for f in df["FechaParsed"]]
    if "Categoria_Origen" in df.columns:
        df["Categoria_Origen"] = df["Categoria_Origen"].replace("", "Sin categoría").fillna("Sin categoría")
    else:
        df["Categoria_Origen"] = "Sin categoría"

    salida = _columna_fechas(df, COL_FECHA_SALIDA)
    puerto = _columna_fechas(df, COL_FECHA_LLEGADA_PUERTO)
    solicitud = _columna_fechas(df, COL_FECHA_SOLICITUD_PAGO)
    pago = _columna_fechas(df, COL_FECHA_PAGO)
    almacen = _columna_fechas(df, COL_FECHA_ALMACEN)
    df["F_Puerto"], df["F_Almacen"] = puerto, almacen
    df["CicloPuertoAlmacen"] = [(a - p).days if (a and p and a >= p) else None
                                for p, a in zip(puerto, almacen)]
    df["CicloSolicitudPago"] = [(pg - s).days if (s and pg and pg >= s) else None
                                for s, pg in zip(solicitud, pago)]
    df["CicloTotal"] = [(a - s).days if (s and a and a >= s) else None
                        for s, a in zip(salida, almacen)]
    return df.sort_values("FechaParsed").reset_index(drop=True)


def _mediana(serie) -> str:
    valores = [v for v in serie if es_numero(v)]
    if not valores:
        return "—"
    return f"{int(pd.Series(valores).median())} d · {len(valores)} emb."


def _grafico_anual(df_anio: pd.DataFrame, anio: int):
    """Los 12 meses del año, incluidos los que van en cero: un mes vacío también
    es información y desaparecerlo del gráfico distorsiona la lectura."""
    conteo = df_anio.groupby("Mes").size().to_dict()
    hoy = hoy_rd()
    etiquetas = [MESES_ES_CORTO[m] for m in range(1, 13)]
    valores = [int(conteo.get(m, 0)) for m in range(1, 13)]
    colores = [COLOR_RECIBIDAS_MES if not (anio == hoy.year and m == hoy.month) else "#1B5E20"
               for m in range(1, 13)]
    fig = go.Figure(data=[go.Bar(
        x=etiquetas, y=valores, marker=dict(color=colores),
        text=[v if v else "" for v in valores], textposition="outside",
        hovertemplate="%{x} " + str(anio) + ": %{y} recibido(s)<extra></extra>",
    )])
    fig.update_layout(
        margin=dict(t=20, b=10, l=10, r=10), height=250,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#374151", size=11), showlegend=False, bargap=0.3, dragmode=False,
        xaxis=dict(showgrid=False),
        yaxis=dict(showgrid=True, gridcolor="#F3F4F6", showticklabels=False),
    )
    st.plotly_chart(fig, width="stretch",
                    config={"displayModeBar": False, "staticPlot": True, "responsive": True},
                    key=f"hist_anual_{anio}")


def _tabla_detalle(df: pd.DataFrame) -> pd.DataFrame:
    """Vista legible del histórico, con la fecha ya formateada en español."""
    if df.empty:
        return pd.DataFrame(columns=["BL", "Descripción", "Modelo/Serie", "Cantidad",
                                     "Origen", "Fecha recibido", "Categoría", "Registrado por"])
    salida = pd.DataFrame({
        "BL": df.get(COL_BL, ""),
        "Descripción": df.get(COL_DESC, ""),
        "Modelo/Serie": df.get(COL_MODELO, ""),
        "Cantidad": df.get(COL_CANT, ""),
        "Origen": df.get(COL_PAIS, ""),
        "Fecha recibido": [f"{f.day:02d} {MESES_ES_CORTO[f.month]} {f.year}" for f in df["FechaParsed"]],
        "Categoría": df.get("Categoria_Origen", ""),
        "Días puerto→almacén": df.get("CicloPuertoAlmacen", ""),
        "Días solicitud→pago": df.get("CicloSolicitudPago", ""),
        "Días salida→almacén": df.get("CicloTotal", ""),
        "Registrado por": df.get("Registrado_Por", ""),
    })
    return salida.reset_index(drop=True)


def mostrar_historico(datos: dict, rol: str):
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.subheader("Histórico de embarques recibidos")

    crudo = datos["historico"]
    df = _preparar_historico(crudo)

    if crudo.empty:
        st.info(
            "Todavía no hay embarques archivados. Cada vez que marques uno como recibido, queda "
            "guardado aquí de forma permanente con la fecha en que llegó, y se puede consultar en "
            "cualquier momento futuro por mes y por año."
        )
        return
    if df.empty:
        st.warning("Hay registros archivados, pero ninguno tiene una fecha de recibido interpretable.")
        return

    descartados = len(crudo) - len(df)
    hoy = hoy_rd()

    mes_ant = (hoy.year - 1, 12) if hoy.month == 1 else (hoy.year, hoy.month - 1)
    n_actual = int(((df["Anio"] == hoy.year) & (df["Mes"] == hoy.month)).sum())
    n_anterior = int(((df["Anio"] == mes_ant[0]) & (df["Mes"] == mes_ant[1])).sum())
    n_anio = int((df["Anio"] == hoy.year).sum())
    n_mismo_mes_anio_pasado = int(((df["Anio"] == hoy.year - 1) & (df["Mes"] == hoy.month)).sum())
    delta = n_actual - n_anterior

    k1, k2, k3 = st.columns(3)
    k1.markdown(tarjeta_kpi(f"{MESES_ES[mes_ant[1]]} {mes_ant[0]} · mes anterior", n_anterior, "#6B7280"),
                unsafe_allow_html=True)
    k2.markdown(
        tarjeta_kpi(
            f"{MESES_ES[hoy.month]} {hoy.year} · mes en curso", n_actual, COLOR_RECIBIDAS_MES,
            f"{'+' if delta >= 0 else ''}{delta} vs. mes anterior"
            + (f" · {n_mismo_mes_anio_pasado} en {hoy.year - 1}" if n_mismo_mes_anio_pasado else ""),
        ),
        unsafe_allow_html=True,
    )
    k3.markdown(tarjeta_kpi(f"Acumulado {hoy.year}", n_anio, COLOR_TOTAL, f"{len(df)} en todo el histórico"),
                unsafe_allow_html=True)
    if descartados:
        st.caption(f"{descartados} registro(s) del archivo no tienen fecha interpretable y no se están contando.")

    st.write("")
    st.divider()

    anios = sorted(df["Anio"].unique(), reverse=True)
    idx_anio = anios.index(hoy.year) if hoy.year in anios else 0
    c_anio, c_info = st.columns([1, 3])
    anio_sel = int(c_anio.selectbox("Año", anios, index=idx_anio, key="hist_anio"))
    df_anio = df[df["Anio"] == anio_sel]
    c_info.markdown(
        f"<div style='padding-top:1.9rem; color:#6B7280; font-size:0.9rem;'>"
        f"{len(df_anio)} embarque(s) recibido(s) en {anio_sel}"
        f"{' · el histórico completo arranca en ' + str(min(anios)) if len(anios) > 1 else ''}</div>",
        unsafe_allow_html=True,
    )

    _grafico_anual(df_anio, anio_sel)

    # -------------------- Tiempos de ciclo --------------------
    # La pregunta que sigue a "¿cuántos recibimos?" es "¿en cuánto tiempo?".
    # Se usa la mediana y no el promedio: un embarque trancado tres meses en
    # puerto le mueve el promedio a todo el año y no representa la operación.
    if df_anio[["CicloPuertoAlmacen", "CicloSolicitudPago", "CicloTotal"]].notna().any().any():
        st.markdown(f"**Tiempos de ciclo {anio_sel} (mediana)**")
        t1, t2, t3 = st.columns(3)
        t1.metric("Puerto → almacén", _mediana(df_anio["CicloPuertoAlmacen"]))
        t2.metric("Solicitud → pago", _mediana(df_anio["CicloSolicitudPago"]))
        t3.metric("Salida → almacén", _mediana(df_anio["CicloTotal"]))
        st.caption("Solo cuenta embarques que tengan ambas fechas registradas. Mientras más completo "
                   "esté el flujo, más confiable es este número — hoy es indicativo, no un estándar.")

    resumen = pd.crosstab(df_anio["Mes"], df_anio["Categoria_Origen"])
    resumen = resumen.reindex(range(1, 13), fill_value=0)
    resumen.insert(len(resumen.columns), "Total", resumen.sum(axis=1))
    resumen.index = [MESES_ES[m] for m in range(1, 13)]
    resumen.index.name = "Mes"
    fila_total = pd.DataFrame([resumen.sum(axis=0)], index=[f"Total {anio_sel}"])
    tabla_resumen = pd.concat([resumen, fila_total])
    st.markdown(f"**Resumen {anio_sel} por mes y categoría**")
    st.dataframe(tabla_resumen, width="stretch")

    d1, d2 = st.columns(2)
    d1.download_button(
        f"Descargar resumen {anio_sel}",
        data=_df_a_excel(tabla_resumen.reset_index().rename(columns={"index": "Mes"}), f"Resumen {anio_sel}"),
        file_name=f"resumen_recibidos_{anio_sel}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_resumen_anio", width="stretch",
    )
    d2.download_button(
        "Descargar histórico completo",
        data=_df_a_excel(_tabla_detalle(df), "Historico"),
        file_name="historico_recibidos_completo.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_historico_total", width="stretch",
    )

    st.divider()

    st.markdown("**Detalle mes por mes**")
    meses_con_datos = sorted(df_anio["Mes"].unique(), reverse=True)
    if not meses_con_datos:
        st.info(f"No hay embarques recibidos registrados en {anio_sel}.")
        return

    idx_mes = meses_con_datos.index(hoy.month) if (anio_sel == hoy.year and hoy.month in meses_con_datos) else 0
    m1, m2 = st.columns([1, 2])
    mes_sel = m1.selectbox("Mes", meses_con_datos, index=idx_mes,
                           format_func=lambda m: MESES_ES[m], key="hist_mes")
    busqueda = m2.text_input("Buscar en el mes", key="hist_busca",
                             placeholder="BL, descripción o modelo…")

    filtrado = df_anio[df_anio["Mes"] == mes_sel]
    if busqueda and busqueda.strip():
        q = _norm(busqueda)
        filtrado = filtrado[filtrado.apply(
            lambda r: q in _norm(f"{r.get(COL_BL,'')} {r.get(COL_DESC,'')} {r.get(COL_MODELO,'')}"), axis=1
        )]

    etiqueta_mes = f"{MESES_ES[mes_sel]} {anio_sel}"
    st.markdown(tarjeta_kpi(f"Recibidos en {etiqueta_mes}", len(filtrado), COLOR_RECIBIDAS_MES),
                unsafe_allow_html=True)
    st.write("")

    tabla = _tabla_detalle(filtrado)
    st.dataframe(tabla, width="stretch", hide_index=True)
    st.download_button(
        f"Descargar {etiqueta_mes} en Excel",
        data=_df_a_excel(tabla, etiqueta_mes),
        file_name=f"recibidos_{anio_sel}_{mes_sel:02d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_mes",
    )

    if rol != "admin" or filtrado.empty:
        return

    st.write("")
    with st.expander("Revertir una recepción", expanded=False):
        opciones = _etiquetas_desambiguadas(filtrado, con_categoria=False, largo_desc=40)
        elegido = st.selectbox("Embarque", opciones, key="sel_revertir")
        fila = filtrado.iloc[opciones.index(elegido)]
        bl = str(fila[COL_BL]).strip()
        guardada = str(fila.get("Categoria_Origen", "")).strip()

        if guardada in CATEGORIAS:
            categoria = guardada
            st.caption(f"Se devolverá a la pestaña '{categoria}' con sus fechas del flujo intactas.")
        else:
            st.caption("Este registro se archivó sin categoría de origen. Elige a dónde devolverlo:")
            categoria = st.selectbox("Categoría de destino", CATEGORIAS, key="cat_revertir")

        if st.button("Quitar de recibido y devolver", key="btn_revertir", type="primary"):
            ok, mensaje = quitar_de_recibido(bl, categoria_manual=categoria,
                                             fila_sugerida=fila.get("FilaSheet"))
            if ok:
                registrar_log("Reversa de recibido", bl, categoria)
                invalidar_caches()
                st.rerun()
            else:
                st.error(mensaje)


# ---------------------------------------------------------------------------
# HERRAMIENTAS DE ADMINISTRACIÓN
# ---------------------------------------------------------------------------
def _herramienta_fechas(df: pd.DataFrame):
    st.markdown("**Normalizar fechas de ETA**")
    st.caption(
        "Google Sheets interpreta las fechas según el locale del archivo, así que una celda escrita "
        "como 06/08/2026 puede quedar guardada como 6 de agosto o como 8 de junio, y quien la lea "
        "después no tiene forma de saber cuál era. Guardar el ETA como texto AAAA-MM-DD elimina el "
        "problema de raíz."
    )
    pendientes = []
    for _, r in df.iterrows():
        diag = analizar_eta(r[COL_ETA])
        if diag["tipo"] == "iso":
            continue
        pendientes.append({
            "categoria": r["Categoria"], "fila": int(r["FilaSheet"]),
            "bl": str(r[COL_BL]).strip() or "(sin BL)",
            "descripcion": str(r[COL_DESC])[:40], **diag,
        })

    if not pendientes:
        st.success("Todos los ETA ya están en formato AAAA-MM-DD. No hay nada que normalizar.")
        return

    ambiguas = [p for p in pendientes if p["tipo"] == "ambigua"]
    convertibles = [p for p in pendientes if p["tipo"] == "convertible"]
    ilegibles = [p for p in pendientes if p["tipo"] == "ilegible"]

    if ilegibles:
        st.error(
            f"{len(ilegibles)} ETA no se pueden interpretar de ninguna forma y hay que corregirlos a mano "
            "en el Sheet: " + ", ".join(f"{p['bl']} (fila {p['fila']} de {p['categoria']}: '{p['crudo']}')"
                                        for p in ilegibles)
        )

    criterio = "Día/Mes/Año (convención dominicana)"
    if ambiguas:
        st.warning(
            f"{len(ambiguas)} fecha(s) admiten dos lecturas porque el día y el mes son ambos ≤ 12. "
            "Elige cómo se escribieron originalmente:"
        )
        criterio = st.radio(
            "Estas fechas se escribieron como:",
            ["Día/Mes/Año (convención dominicana)", "Mes/Día/Año (convención estadounidense)"],
            key="criterio_fechas",
        )
        usar_dm = criterio.startswith("Día")
        st.dataframe(
            pd.DataFrame([{
                "BL": p["bl"], "Categoría": p["categoria"], "Fila": p["fila"],
                "Valor actual": p["crudo"],
                "Se guardará como": (p["dm"] if usar_dm else p["md"]).isoformat(),
                "Otra lectura posible": (p["md"] if usar_dm else p["dm"]).isoformat(),
            } for p in ambiguas]),
            width="stretch", hide_index=True,
        )
    else:
        usar_dm = True

    if convertibles:
        st.info(f"{len(convertibles)} fecha(s) se entienden sin ambigüedad y se convertirán directamente.")
        st.dataframe(
            pd.DataFrame([{
                "BL": p["bl"], "Categoría": p["categoria"], "Fila": p["fila"],
                "Valor actual": p["crudo"], "Se guardará como": p["dm"].isoformat(),
            } for p in convertibles]),
            width="stretch", hide_index=True,
        )

    cambios = [(p["categoria"], p["fila"], p["crudo"], (p["dm"] if usar_dm else p["md"]).isoformat())
               for p in ambiguas]
    cambios += [(p["categoria"], p["fila"], p["crudo"], p["dm"].isoformat()) for p in convertibles]

    if cambios and st.button(f"Convertir {len(cambios)} fecha(s) a AAAA-MM-DD", type="primary"):
        ok, mensaje = normalizar_etas(cambios)
        if ok:
            registrar_log("Normalización de fechas", "", "", f"{len(cambios)} celda(s); criterio={criterio}")
            invalidar_caches()
            st.success(mensaje)
            st.rerun()
        else:
            st.error(mensaje)


def _herramienta_salud(df: pd.DataFrame, historico: pd.DataFrame):
    """Lo que el tablero no muestra pero envenena los números: BLs repetidos,
    campos vacíos, fechas imposibles. Vale más media hora limpiando esto que
    cualquier gráfico nuevo."""
    st.markdown("**Salud de los datos**")
    problemas = []

    bls = df[df[COL_BL].astype(str).str.strip() != ""][COL_BL].astype(str).str.strip()
    repetidos = sorted(set(bls[bls.duplicated(keep=False)]))
    if repetidos:
        problemas.append(
            (f"{len(repetidos)} BL repetido(s) entre embarques activos",
             ", ".join(repetidos[:12]) + ("…" if len(repetidos) > 12 else ""),
             "Si son embarques parciales del mismo BL está bien, pero conviene diferenciarlos "
             "(sufijo -1, -2) para que nadie los confunda al editar.")
        )

    sin_eta = df[df["EstadoTexto"] == EST_SIN_FECHA]
    if not sin_eta.empty:
        problemas.append((f"{len(sin_eta)} embarque(s) con ETA ilegible",
                          ", ".join(f"{r[COL_BL]} ({r['Categoria']} fila {r['FilaSheet']})"
                                    for _, r in sin_eta.head(10).iterrows()),
                          "Quedan fuera de todos los conteos por fecha. Arriba tienes el normalizador."))

    for columna, nombre in ((COL_PAIS, "país de origen"), (COL_DESC, "descripción"),
                            (COL_CANT, "cantidad")):
        vacios = df[df[columna].astype(str).str.strip().isin(["", NO_ESPECIFICADO])]
        if len(vacios):
            problemas.append((f"{len(vacios)} embarque(s) sin {nombre}",
                              ", ".join(str(b) for b in vacios[COL_BL].head(10)),
                              "Campo vacío en el Sheet."))

    # OC y EE son la referencia con la que Compras y Finanzas rastrean la carga
    # aérea y suelta. Sin ellas, el embarque aparece en el tablero pero nadie lo
    # puede amarrar a una orden: es el vacío más caro de los que salen aquí.
    con_oc_ee = df[df["Categoria"].isin(CATEGORIAS_CON_OC_EE)]
    if not con_oc_ee.empty:
        def _vacio(valor):
            texto = str(valor or "").strip().upper()
            return texto in ("", "N/A", "NA", "NAN", "NONE", "-", "—")
        sin_ref = con_oc_ee[[_vacio(o) and _vacio(e)
                             for o, e in zip(con_oc_ee.get(COL_OC, ""), con_oc_ee.get(COL_EE, ""))]]
        if not sin_ref.empty:
            por_cat = sin_ref["Categoria"].value_counts().to_dict()
            problemas.append(
                (f"{len(sin_ref)} embarque(s) de Aéreos/Carga Suelta sin OC ni EE",
                 ", ".join(f"{r[COL_BL] or '(sin BL)'} ({r['Categoria']} fila {r['FilaSheet']})"
                           for _, r in sin_ref.head(12).iterrows())
                 + " · Total por categoría: "
                 + ", ".join(f"{c}: {n}" for c, n in por_cat.items()),
                 "La app sí lee esas columnas: están vacías en el Sheet. Suelen faltar en las "
                 "filas cargadas a mano o importadas sin las columnas OC/EE. Complétalas desde "
                 "'Editar' o vuelve a importar el lote incluyendo ambas columnas.")
            )

    salida_mala = df[[bool(s and e and s > e) for s, e in zip(df["F_Salida"], df["ETAFecha"])]]
    if not salida_mala.empty:
        problemas.append((f"{len(salida_mala)} embarque(s) con fecha de salida posterior al ETA",
                          ", ".join(str(b) for b in salida_mala[COL_BL].head(10)),
                          "Una de las dos fechas está mal escrita."))

    inconsistentes = []
    for _, r in df.iterrows():
        problema = _validar_orden_flujo(fechas_flujo_de_fila(r))
        if problema:
            inconsistentes.append(f"{r[COL_BL]} ({r['Categoria']} fila {r['FilaSheet']})")
    if inconsistentes:
        problemas.append((f"{len(inconsistentes)} embarque(s) con fechas del flujo fuera de orden",
                          ", ".join(inconsistentes[:10]),
                          "Ej.: pago anterior a la declaración. Corrígelo desde 'Corregir fecha' "
                          "en el panel de proceso."))

    if not historico.empty:
        faltos = 0
        for _, r in historico.iterrows():
            if not str(r.get(COL_FECHA_LLEGADA_PUERTO, "")).strip():
                faltos += 1
        if faltos:
            problemas.append((f"{faltos} embarque(s) archivados sin fecha de llegada a puerto",
                              "Registros anteriores al flujo de etapas, o archivados saltándolo.",
                              "No se pueden usar para medir tiempos de ciclo. No hay que corregirlos "
                              "hacia atrás: a partir de ahora entran completos."))

    if not problemas:
        st.success("No se detectaron problemas en los datos cargados.")
        return
    for titulo, detalle, nota in problemas:
        with st.expander(titulo):
            st.write(detalle)
            st.caption(nota)


def respaldo_completo() -> tuple:
    """Copia cruda de TODAS las pestañas del Sheet en un solo .xlsx: valores tal
    cual están, sin enriquecer, sin filtrar y sin reordenar columnas — para que
    sirva para restaurar, no solo para analizar.

    Se lee con get_all_values() y no con la caché de la app a propósito: un
    respaldo tiene que reflejar el Sheet de este momento, no lo que la pantalla
    tenía cargado hace cinco minutos. Cuesta una llamada por pestaña, por eso va
    detrás de un botón y no en cada rerun."""
    try:
        hojas = _con_reintento(lambda: get_spreadsheet().worksheets())
    except Exception as e:  # noqa: BLE001
        return None, f"No se pudo leer el Google Sheet: {e}", 0
    buffer = io.BytesIO()
    filas_totales = 0
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for hoja in hojas:
            try:
                valores = _con_reintento(lambda h=hoja: h.get_all_values())
            except Exception as e:  # noqa: BLE001
                return None, f"Falló la lectura de la pestaña '{hoja.title}': {e}", 0
            # Sin header: la fila 1 se guarda como una fila más, así el respaldo
            # es idéntico al original aunque alguien haya cambiado un encabezado.
            marco = pd.DataFrame(valores) if valores else pd.DataFrame()
            filas_totales += max(len(valores) - 1, 0)
            nombre = "".join(c for c in hoja.title if c.isalnum() or c in " _-")[:31] or "Hoja"
            marco.to_excel(writer, sheet_name=nombre, index=False, header=False)
    return buffer.getvalue(), "", filas_totales


def _herramienta_respaldo():
    st.markdown("**Respaldo**")
    st.caption("Copia cruda de todas las pestañas, tal como están en este momento. "
               "Guárdala fuera de Google Drive: un respaldo dentro de la misma cuenta "
               "no protege contra perder la cuenta.")
    if st.button("Generar respaldo del Sheet completo"):
        with st.spinner("Leyendo todas las pestañas…"):
            contenido, error, filas = respaldo_completo()
        if error:
            st.error(error)
        else:
            st.session_state["respaldo_bytes"] = contenido
            st.session_state["respaldo_nombre"] = (
                f"Respaldo_Embarques_{ahora_rd().strftime('%Y-%m-%d_%H%M')}.xlsx")
            st.session_state["respaldo_filas"] = filas
    if st.session_state.get("respaldo_bytes"):
        st.download_button(
            f"⬇ Descargar {st.session_state['respaldo_nombre']} "
            f"({st.session_state.get('respaldo_filas', 0)} filas)",
            data=st.session_state["respaldo_bytes"],
            file_name=st.session_state["respaldo_nombre"],
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        st.caption("El respaldo automático cada 10 minutos no lo hace esta app: lo hace el "
                   "script de Google Apps Script instalado en el propio Sheet, que corre "
                   "aunque nadie tenga la app abierta.")


def herramientas(datos: dict):
    st.subheader("Herramientas")
    df = datos["activos"]
    if df.empty:
        st.info("No hay embarques cargados.")
        return
    df = enriquecer(df)

    _herramienta_fechas(df)
    st.divider()
    _herramienta_salud(df, datos["historico"])
    st.divider()
    _herramienta_respaldo()
    st.divider()

    st.markdown("**Bitácora**")
    st.caption("Últimos movimientos registrados en la pestaña 'Log' del Sheet: quién cargó, editó, "
               "avanzó una etapa, archivó o borró, y cuándo.")
    if st.button("Ver bitácora"):
        st.session_state["ver_log"] = True
    if st.session_state.get("ver_log"):
        registros = _leer_log()
        if registros.empty:
            st.info("Todavía no hay movimientos registrados.")
        else:
            st.dataframe(registros, width="stretch", hide_index=True, height=340)

    st.divider()
    st.markdown("**Estructura del Google Sheet**")
    st.caption("Usa esto si creaste o renombraste una pestaña y la app todavía no la ve.")
    if st.button("Releer estructura del Sheet"):
        _refrescar_estructura()
        invalidar_caches()
        st.success("Estructura recargada.")
        st.rerun()

    st.caption(f"Versión de la app: {VERSION_APP} · Umbrales de alerta: " +
               ", ".join(f"{ETIQUETA_CORTA_ETAPA[e]} {d} d" for e, d in sla_etapas().items()
                         if e in ETIQUETA_CORTA_ETAPA))
