"""
pagos.py — Estatus de Pago de Antillana Comercial.

Módulo nuevo, separado de tránsito a propósito: reusa el mismo Google Sheet,
el mismo login y el mismo Streamlit Cloud, pero su hoja ('Pagos'), sus
columnas y sus escrituras viven aparte porque el dato es distinto (dinero, no
etapas de embarque) y así un error aquí no puede tocar tránsito.

Objetivo del módulo: visibilizar lo que cuesta no pagar a tiempo. No se
tabulan tarifas por día —varían por naviera, terminal, volumen y espacio—;
se compara lo que Logística fija como pago saludable ("SIN MORA") contra lo
que realmente se terminó pagando ("Pago Realizado"), y la diferencia es el
sobrecosto. Ambas ventanas se pueden corregir después si cambia el monto o la
fecha: no son de una sola vez y ya.

Usa Streamlit e importa de sheets_io.py, logica.py y ui_componentes.py.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from sheets_io import (
    CACHE_TTL, CATEGORIAS, COL_ACTUALIZACION, COL_ACTUALIZADO_POR, COL_BL, COL_CANT,
    COL_DESC, COL_EMPRESA, COL_ESTADO_PAGO, COL_ETA, COL_FECHA_LLEGADA_PUERTO,
    COL_FECHA_PAGO_REAL, COL_FECHA_SIN_MORA, COL_PAGO_LLEGADA, COL_PAGOREAL_DOP,
    COL_PAGOREAL_USD, CONCEPTOS_PAGO, EMPRESA_ANTILLANA, EMPRESAS_PAGO,
    ESTADO_PAGO_PAGADO, ESTADO_PAGO_PENDIENTE, MESES_ES_CORTO, MONEDA_CONCEPTO,
    _norm, a_numero, aplicar_selectores_pagos, fecha_llegada_fila, formato_eta,
    guardar_pago, hoy_rd, invalidar_caches, marcar_estado_pago,
    mover_empresa_primera_columna, parsear_fecha, parsear_marca, registrar_log,
    registrar_pago_realizado, registrar_sin_mora, sincronizar_pagos_con_transito,
)
from logica import PALETA_PAISES, enriquecer_pagos, esc, resumen_pagos, totales_conceptos
from ui_componentes import CUSTOM_CSS, _logo_base64, rerun_fragmento


COLOR_SOBRECOSTO = "#991B1B"


COLOR_MORA_PROMEDIO = "#B45309"


# Un color fijo por concepto — reusa la misma paleta "amigable" que ya usan
# los gráficos de país en tránsito, así no se inventa una gama nueva.
# Cicla la paleta en vez de truncar con zip(): antes, un concepto agregado más
# allá del largo de PALETA_PAISES (8 colores) se quedaba sin entrada en este
# diccionario, y buscar su color en _html_expediente() reventaba con KeyError.
# Con el módulo, el color se repite pero nunca falta.
COLOR_CONCEPTO = {c: PALETA_PAISES[i % len(PALETA_PAISES)] for i, c in enumerate(CONCEPTOS_PAGO)}


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _enriquecer_pagos_cacheado(df_pagos: pd.DataFrame, activos: pd.DataFrame,
                               historico: pd.DataFrame) -> pd.DataFrame:
    """Envoltura cacheada de enriquecer_pagos() (logica.py se queda sin
    Streamlit, tal como está diseñado). Misma ventana de 45s que cargar_todo():
    con varios viewers mirando Pagos a la vez, comparten este cálculo en vez de
    que cada sesión lo repita. No cambia qué se calcula, solo cuándo."""
    return enriquecer_pagos(df_pagos, activos, historico)


PAGOS_CSS = """
<style>
.pago-tarjeta { border:1px solid #E5E7EB; border-radius:12px; padding:16px 20px; margin-bottom:14px;
                background:#fff; box-shadow:0 1px 4px rgba(17,24,39,0.06); }
.pago-cabeza { display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }
.pago-bl { font-weight:700; font-size:1.02rem; color:#111827; }
.pago-empresa { display:inline-block; background:#EEF2FF; color:#3730A3; font-size:0.72rem;
                font-weight:700; padding:2px 10px; border-radius:999px; margin-left:8px; }
.pago-estado { display:inline-block; font-size:0.75rem; font-weight:700; color:#fff;
               padding:3px 12px; border-radius:999px; white-space:nowrap; }
.pago-meta { color:#111827; font-size:0.85rem; margin-top:2px; }
.pago-referencia { color:#111827; font-size:0.85rem; margin-top:1px; }
.pago-conceptos { display:flex; flex-wrap:wrap; justify-content:center; gap:8px; margin:14px 0; }
.pago-chip { padding:6px 15px; border-radius:999px; color:#fff; font-size:0.83rem; font-weight:700;
             white-space:nowrap; }
.pago-totales { display:flex; flex-wrap:wrap; justify-content:center; align-items:baseline; gap:28px;
                margin-top:6px; }
.pago-total-etq { font-size:0.68rem; text-transform:uppercase; letter-spacing:0.04em; color:#0C447C;
                  font-weight:700; display:block; text-align:center; }
.pago-total-val { font-size:1.2rem; font-weight:400; color:#111827; display:block; text-align:center; }
.pago-cerrado { text-align:center; color:#111827; font-size:0.78rem; margin-top:10px; }
.pago-sin-extra { color:#166534; font-weight:600; }
.pago-extra { color:#991B1B; font-weight:700; background:#FEF2F2; padding:2px 9px; border-radius:6px; }
</style>
"""


def _fmt(monto, moneda: str) -> str:
    # $ normal a propósito: esto se usa solo dentro de las tarjetas HTML
    # (st.markdown con unsafe_allow_html=True), donde el $ nunca tuvo el
    # problema de renderizarse como fórmula — eso solo pasa en st.button.
    simbolo = "US$" if moneda == "USD" else "RD$"
    return f"{simbolo} {monto:,.2f}"


CATEGORIA_RECIBIDOS = "Recibidos (histórico)"


def _opciones_categoria(activos: pd.DataFrame, historico: pd.DataFrame) -> list:
    """Mismo orden de categorías que usa tránsito (ver CATEGORIAS en sheets_io.py),
    y al final los ya archivados: para cuando el pago se registra después de que
    la carga ya se recibió."""
    disponibles = [c for c in CATEGORIAS
                  if activos is not None and not activos.empty and (activos["Categoria"] == c).any()]
    if historico is not None and not historico.empty:
        disponibles.append(CATEGORIA_RECIBIDOS)
    return disponibles


def _embarques_de_categoria(categoria: str, activos: pd.DataFrame, historico: pd.DataFrame) -> list:
    """BL, Descripción, Cantidad y Llegada de tránsito para elegir de una lista
    — nada de esto se vuelve a teclear. 'llegada_iso' viaja aparte (AAAA-MM-DD)
    para poder guardarla si hay que crear la fila de Pagos desde aquí, antes de
    que la sincronización automática la alcance. Ordenado por BL para que la
    lista sea estable entre refrescos."""
    filas = []
    if categoria == CATEGORIA_RECIBIDOS:
        for _, r in historico.iterrows():
            llegada = (parsear_fecha(r.get(COL_FECHA_LLEGADA_PUERTO, ""))
                      or parsear_fecha(r.get(COL_ETA, "")))
            filas.append({
                "bl": str(r.get(COL_BL, "")).strip(),
                "desc": str(r.get(COL_DESC, "")),
                "cant": str(r.get(COL_CANT, "")),
                "llegada_txt": formato_eta(llegada) if llegada else "—",
                "llegada_iso": llegada.isoformat() if llegada else "",
            })
    else:
        sub = activos[activos["Categoria"] == categoria]
        for _, r in sub.iterrows():
            llegada = fecha_llegada_fila(r)
            if llegada:
                llegada_txt = formato_eta(llegada)
            else:
                # Todavía sin confirmar: se muestra el ETA igual, marcado como
                # estimado, para no dejar la lista en blanco.
                llegada = parsear_fecha(r.get(COL_ETA, ""))
                llegada_txt = f"{formato_eta(llegada)} (ETA, sin confirmar)" if llegada else "sin ETA"
            filas.append({
                "bl": str(r.get(COL_BL, "")).strip(),
                "desc": str(r.get(COL_DESC, "")),
                "cant": str(r.get(COL_CANT, "")),
                "llegada_txt": llegada_txt,
                "llegada_iso": llegada.isoformat() if llegada else "",
            })
    filas = [f for f in filas if f["bl"]]
    filas.sort(key=lambda f: f["bl"])
    return filas


def _hay_bls_sin_sincronizar(activos: pd.DataFrame, historico: pd.DataFrame, pagos_actual: pd.DataFrame) -> bool:
    """Comparación en memoria contra lo que ya cargó cargar_todo() — cero
    llamadas extra a la API. Solo cuando esto da True vale la pena pagar el
    costo de sincronizar_pagos_con_transito() (que sí lee y escribe de verdad)."""
    existentes = ({_norm(b) for b in pagos_actual[COL_BL] if str(b).strip()}
                 if pagos_actual is not None and not pagos_actual.empty else set())
    for fuente in (activos, historico):
        if fuente is None or fuente.empty:
            continue
        for b in fuente[COL_BL]:
            b = str(b).strip()
            if b and _norm(b) not in existentes:
                return True
    return False


# ---------------------------------------------------------------------------
# DASHBOARD (viewer + admin)
# ---------------------------------------------------------------------------
COLOR_ABIERTOS = "#EA580C"


COLOR_PAGADOS = "#2E7D32"  # mismo verde que ya usa el badge "Pagado" de cada tarjeta, ver _html_expediente


def _aplicar_filtro_kpi(df: pd.DataFrame, filtro: str) -> pd.DataFrame:
    """Mismo criterio que arma resumen_pagos, evaluado fila por fila para
    poder filtrar la lista de tarjetas según qué KPI se haya clickeado."""
    if df.empty or filtro == "todos":
        return df
    pagado = df["EstadoEfectivo"] == ESTADO_PAGO_PAGADO
    if filtro == "cerrados":
        return df[pagado]
    if filtro == "abiertos":
        return df[~pagado]
    if filtro == "con_mora":
        return df[pagado & (df["DiasMora"] > 0)]
    if filtro == "con_sobrecosto":
        def _tiene_extra(e):
            e = e or {}
            return any(v is not None and v > 0 for v in e.values())
        return df[pagado & df["MontoExtra"].apply(_tiene_extra)]
    return df


COLOR_POR_PAGAR = "#0C447C"


def _tarjeta_por_pagar(total: dict) -> str:
    """Tarjeta estática (no es botón, así que $ normal es seguro aquí — el
    problema de la 'fórmula' solo pasa en st.button, nunca en HTML)."""
    return (
        f'<div style="background:{COLOR_POR_PAGAR}; color:#fff; border-radius:14px; min-height:92px; '
        f'display:flex; flex-direction:column; align-items:center; justify-content:center; '
        f'padding:14px 10px; box-shadow:0 2px 8px rgba(17,24,39,0.12);">'
        f'<div style="font-size:0.68rem; font-weight:700; letter-spacing:0.05em; '
        f'text-transform:uppercase; opacity:0.92;">Total por pagar</div>'
        f'<div style="font-size:1.35rem; font-weight:800; margin-top:6px;">'
        f'{_fmt(total.get("USD", 0.0), "USD")} · {_fmt(total.get("DOP", 0.0), "DOP")}</div>'
        f'</div>'
    )


def _tarjetas_resumen(resumen: dict, filtro_activo: str) -> str:
    """Las 5 tarjetas de KPI como botones clicables — mismo patrón que las
    categorías de tránsito: un clic filtra la lista de abajo, y clickear la
    misma que ya está activa la vuelve a 'todos'. Una 6ta tarjeta, estática,
    muestra el total que aún se debe. Devuelve el filtro que quedó activo
    después del clic (o el mismo de antes, si no se clickeó nada)."""
    prom = resumen["dias_mora_promedio"]
    sobre = resumen["sobrecosto"]
    kpis = [
        ("Pagados", str(resumen["n_pagados"]), COLOR_PAGADOS, "cerrados"),
        ("Pendientes", str(resumen["n_abiertos"]), COLOR_ABIERTOS, "abiertos"),
        ("Mora promedio", f"{prom:.0f} d" if prom is not None else "—", COLOR_MORA_PROMEDIO, "con_mora"),
        ("Sobrecosto acumulado", f"USD {sobre['USD']:,.0f} · DOP {sobre['DOP']:,.0f}",
         COLOR_SOBRECOSTO, "con_sobrecosto"),
    ]
    estilos = "".join(
        f'.st-key-pagokpi_{slug} button {{background:{color} !important; color:#fff !important; '
        f'border:{"3px solid #111827" if filtro_activo == slug else "none"} !important; '
        f'border-radius:14px !important; width:100% !important; min-height:92px !important; '
        f'padding:14px 10px !important; box-shadow:0 2px 8px rgba(17,24,39,0.12) !important;}} '
        f'.st-key-pagokpi_{slug} button > div {{display:flex !important; flex-direction:column !important; '
        f'align-items:center !important; justify-content:center !important; width:100% !important;}} '
        f'.st-key-pagokpi_{slug} button p {{margin:0 !important; color:#fff !important; '
        f'text-align:center !important; width:100% !important;}} '
        f'.st-key-pagokpi_{slug} button p:first-of-type {{font-size:0.68rem !important; '
        f'font-weight:700 !important; letter-spacing:0.05em !important; text-transform:uppercase !important; '
        f'opacity:0.92 !important;}} '
        f'.st-key-pagokpi_{slug} button p:last-of-type {{font-size:1.35rem !important; '
        f'font-weight:800 !important; margin-top:6px !important;}}'
        for _, _, color, slug in kpis
    )
    st.markdown(f"<style>{estilos}</style>", unsafe_allow_html=True)
    cols = st.columns(len(kpis) + 1)
    for col, (label, valor, _color, slug) in zip(cols, kpis):
        with col:
            with st.container(key=f"pagokpi_{slug}"):
                if st.button(f"{label.upper()}\n\n{valor}", key=f"btn_pagokpi_{slug}", width="stretch"):
                    st.session_state["pago_filtro_estado"] = "todos" if filtro_activo == slug else slug
                    rerun_fragmento()
    with cols[-1]:
        st.markdown(_tarjeta_por_pagar(resumen["total_por_pagar"]), unsafe_allow_html=True)
    return st.session_state.get("pago_filtro_estado", "todos")


def _html_expediente(r) -> str:
    bl = esc(r.get(COL_BL, "")) or "(sin BL)"
    desc = esc(r.get(COL_DESC, ""))
    cant = esc(r.get(COL_CANT, ""))
    llegada = esc(r.get(COL_PAGO_LLEGADA, "")) or "—"
    empresa = esc(r.get("EmpresaEfectiva", "")) or EMPRESA_ANTILLANA
    # El estado que se muestra es el EFECTIVO: Pagado en cuanto hay fecha de
    # pago real, así se haya tecleado directo en el Sheet sin pasar por el
    # botón de "Marcar estado".
    estado = r.get("EstadoEfectivo") or ESTADO_PAGO_PENDIENTE
    pagado = estado == ESTADO_PAGO_PAGADO
    color_estado = "#2E7D32" if pagado else "#B45309"

    # Un concepto vacío NO sale — solo los que de verdad tiene el expediente.
    chips = []
    for concepto in CONCEPTOS_PAGO:
        valor = a_numero(r.get(concepto, ""))
        if valor is None:
            continue
        color = COLOR_CONCEPTO[concepto]
        chips.append(
            f'<span class="pago-chip" style="background:{color};">'
            f'{esc(concepto)}: {_fmt(valor, MONEDA_CONCEPTO[concepto])}</span>'
        )

    total = r.get("TotalActual") or {}
    dias_sin_pagar = r.get("DiasSinPagar")
    dias_sin_pagar_txt = "—" if dias_sin_pagar is None or pd.isna(dias_sin_pagar) else str(int(dias_sin_pagar))
    fecha_saludable = esc(r.get(COL_FECHA_SIN_MORA, "")) or "sin fijar"

    # Quién lo solicitó (OC/EE/Cliente-Stock), leído en vivo de tránsito por
    # enriquecer_pagos(). No todo expediente lo trae -- Carga Suelta/General
    # no usan Cliente/Stock, Tecnicaribe y Motor Ibérico no tienen tránsito
    # propio -- así que la línea entera se omite cuando no hay nada que mostrar.
    referencia = str(r.get("ReferenciaTransito", "") or "").strip()
    referencia_html = f'<div class="pago-referencia">{esc(referencia)}</div>' if referencia else ""

    if pagado:
        # Ya pagado: lo que importa es el costo FINAL (conceptos + el extra
        # que Logística escribió a mano — 0 si no hubo diferencia).
        total_pagado = r.get("TotalPagado") or total
        etiqueta_usd, etiqueta_dop = "Total pagado US$", "Total pagado RD$"
        valor_usd, valor_dop = total_pagado.get("USD") or 0.0, total_pagado.get("DOP") or 0.0
    else:
        etiqueta_usd, etiqueta_dop = "Total a pagar US$", "Total a pagar RD$"
        valor_usd, valor_dop = total.get("USD") or 0.0, total.get("DOP") or 0.0

    pie_cerrado = ""
    fecha_pago = str(r.get(COL_FECHA_PAGO_REAL, "")).strip()
    if pagado and fecha_pago:
        extra = r.get("MontoExtra") or {}
        partes = [_fmt(v, m) for m, v in extra.items() if v is not None and abs(v) > 0.005]
        if partes:
            extra_html = (' · <span class="pago-extra">⚠ Extra pagado de más: '
                          f'{" y ".join(partes)}</span>')
        else:
            extra_html = ' · <span class="pago-sin-extra">Sin diferencia sobre lo saludable</span>'
        pie_cerrado = f'<div class="pago-cerrado">Pagado el {esc(fecha_pago)}{extra_html}</div>'

    return (
        '<div class="pago-tarjeta">'
        f'<div class="pago-cabeza"><span class="pago-bl">{bl}<span class="pago-empresa">{empresa}</span></span>'
        f'<span class="pago-estado" style="background:{color_estado};">{esc(estado)}</span></div>'
        f'<div class="pago-meta">{desc} · {cant} · Llegada: {llegada}</div>'
        f'{referencia_html}'
        f'<div class="pago-conceptos">{"".join(chips)}</div>'
        '<div class="pago-totales">'
        f'<div><span class="pago-total-etq">{etiqueta_usd}</span>'
        f'<span class="pago-total-val">{_fmt(valor_usd, "USD")}</span></div>'
        f'<div><span class="pago-total-etq">{etiqueta_dop}</span>'
        f'<span class="pago-total-val">{_fmt(valor_dop, "DOP")}</span></div>'
        f'<div><span class="pago-total-etq">Fecha saludable</span>'
        f'<span class="pago-total-val" style="font-size:0.95rem;">{fecha_saludable}</span></div>'
        f'<div><span class="pago-total-etq">Días sin pagar</span>'
        f'<span class="pago-total-val">{dias_sin_pagar_txt}</span></div>'
        '</div>'
        f'{pie_cerrado}'
        '</div>'
    )


ESTADO_DISPLAY = {"todos": "Todos", "abiertos": "Pendiente", "cerrados": "Pagado"}


ESTADO_SLUG = {v: k for k, v in ESTADO_DISPLAY.items()}


def mostrar_dashboard_pagos(enriquecido: pd.DataFrame):
    c1, c2 = st.columns(2)
    with c1:
        empresa_sel = st.selectbox("Empresa", ["Todas"] + EMPRESAS_PAGO, key="pago_filtro_empresa")

    # Mismo estado que ya manejan los botones de KPI (Pagados/Pendientes),
    # para que este selector y esos botones nunca se contradigan. La clave
    # del widget incluye el slug actual a propósito: así, si el estado cambia
    # desde un botón, este selector se re-crea con el valor correcto en vez
    # de quedarse pegado en lo que el usuario había elegido antes aquí.
    slug_actual = st.session_state.get("pago_filtro_estado", "todos")
    if slug_actual not in ESTADO_DISPLAY:
        slug_actual = "todos"  # "con_mora"/"con_sobrecosto" no tienen equivalente en este selector
    opciones_estatus = ["Todos", "Pendiente", "Pagado"]
    with c2:
        estatus_sel = st.selectbox("Estatus", opciones_estatus,
                                   index=opciones_estatus.index(ESTADO_DISPLAY[slug_actual]),
                                   key=f"pago_filtro_estatus_{slug_actual}")
    nuevo_slug = ESTADO_SLUG[estatus_sel]
    if nuevo_slug != slug_actual:
        st.session_state["pago_filtro_estado"] = nuevo_slug
        rerun_fragmento()

    vista = enriquecido if empresa_sel == "Todas" or enriquecido.empty \
        else enriquecido[enriquecido["EmpresaEfectiva"] == empresa_sel]

    if vista.empty:
        st.info("No hay expedientes de Pagos para esta selección.")
        return

    con_montos = vista[vista["TieneMontos"]]

    sin_transito = int(vista["BLSinTransito"].sum())
    if sin_transito:
        st.warning(f"{sin_transito} expediente(s) de Pagos ya no tienen un BL coincidente en tránsito "
                   "(activo ni histórico) — puede que se hayan eliminado o cambiado de BL ahí.")

    resumen = resumen_pagos(con_montos)
    filtro_activo = _tarjetas_resumen(resumen, st.session_state.get("pago_filtro_estado", "todos"))

    if con_montos.empty:
        st.info(f"Hay {len(vista)} expediente(s) en esta selección, pero ninguno tiene montos "
                "cargados todavía. Se muestran aquí solo cuando tengan al menos un concepto lleno.")
        return

    filtrado = _aplicar_filtro_kpi(con_montos, filtro_activo)
    st.markdown(PAGOS_CSS, unsafe_allow_html=True)
    if filtrado.empty:
        st.caption("Ningún expediente coincide con este filtro.")
    else:
        st.markdown("".join(_html_expediente(r) for _, r in filtrado.iterrows()), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# FORMULARIOS (solo admin)
# ---------------------------------------------------------------------------
def form_registrar_conceptos(enriquecido: pd.DataFrame, activos: pd.DataFrame, historico: pd.DataFrame):
    st.markdown("**Registrar / editar conceptos de un expediente**")
    st.caption("Esta lista sale de tránsito, que solo trackea Antillana Comercial. Para Tecnicaribe o "
              "Motor Ibérico, agrega la fila directo en la pestaña Pagos del Sheet (BL, Empresa, "
              "Descripción, Cantidad y Llegada a mano) y luego edítala aquí si quieres usar el resto "
              "de estos formularios sobre ella.")

    categorias_disp = _opciones_categoria(activos, historico)
    if not categorias_disp:
        st.info("Todavía no hay embarques en tránsito ni en el histórico para elegir.")
        return

    categoria = st.selectbox("Categoría", categorias_disp, key="pago_categoria_sel")
    embarques = _embarques_de_categoria(categoria, activos, historico)
    if not embarques:
        st.info(f"No hay embarques con BL en '{categoria}'.")
        return

    etiquetas = [f"{e['bl']} · {e['desc'][:40] or 'sin descripción'} · {e['cant']} · Llegada: {e['llegada_txt']}"
                for e in embarques]
    idx = st.selectbox("Expediente", range(len(embarques)), format_func=lambda i: etiquetas[i],
                       key="pago_expediente_sel")
    elegido = embarques[idx]
    bl = elegido["bl"]

    fila_existente = None
    if not enriquecido.empty:
        coincidencias = enriquecido[enriquecido[COL_BL].astype(str).str.strip() == bl]
        if not coincidencias.empty:
            fila_existente = coincidencias.iloc[0]
    if fila_existente is None:
        st.caption("Expediente nuevo en Pagos — todavía sin conceptos registrados.")
    else:
        st.caption("Este expediente ya tiene conceptos registrados; los valores de abajo son los actuales.")

    valores_previos = {c: fila_existente.get(c, "") for c in CONCEPTOS_PAGO} if fila_existente is not None else {}
    seleccionados_previos = [c for c in CONCEPTOS_PAGO if str(valores_previos.get(c, "")).strip()]

    # Empresa no sale de tránsito (ese concepto no existe ahí) — la fija
    # Logística a mano, cada vez. Sin default automático: "Elige..." obliga a
    # una decisión consciente en vez de asumir Antillana solo porque el
    # expediente vino del selector de tránsito.
    empresa_previa = str(fila_existente.get(COL_EMPRESA, "")).strip() if fila_existente is not None else ""
    opciones_empresa = ["— Elige —"] + EMPRESAS_PAGO
    idx_empresa = opciones_empresa.index(empresa_previa) if empresa_previa in opciones_empresa else 0
    empresa_elegida = st.selectbox("Empresa (a quién se le debe este expediente)", opciones_empresa,
                                   index=idx_empresa, key="pago_empresa_sel")

    conceptos_aplican = st.multiselect(
        "Conceptos que aplican a este expediente (deja fuera los que no apliquen — vacío no es cero)",
        CONCEPTOS_PAGO, default=seleccionados_previos, key="pago_conceptos_sel",
    )

    montos = {}
    if conceptos_aplican:
        cols = st.columns(2)
        for i, concepto in enumerate(conceptos_aplican):
            moneda = MONEDA_CONCEPTO[concepto]
            valor_previo = a_numero(valores_previos.get(concepto, "")) or 0.0
            with cols[i % 2]:
                montos[concepto] = st.number_input(
                    f"{concepto} ({moneda})", min_value=0.0, value=float(valor_previo),
                    step=100.0, key=f"pago_monto_{concepto}",
                )

    if st.button("Guardar conceptos", type="primary", key="btn_guardar_conceptos"):
        if not bl:
            st.error("Falta el BL.")
            return
        if empresa_elegida == "— Elige —":
            st.error("Selecciona a qué empresa se le debe este expediente antes de guardar.")
            return
        # Los conceptos NO seleccionados se guardan vacíos a propósito: "no
        # aplica" no es lo mismo que "cero", y así solo se suma lo que de
        # verdad tiene el expediente.
        datos = {c: (montos[c] if c in conceptos_aplican else "") for c in CONCEPTOS_PAGO}
        referencia = {COL_DESC: elegido["desc"], COL_CANT: elegido["cant"],
                     COL_PAGO_LLEGADA: elegido["llegada_iso"]}
        ok, mensaje = guardar_pago(bl, datos, empresa=empresa_elegida, referencia=referencia)
        if ok:
            registrar_log("Conceptos de pago guardados", bl, "", ", ".join(conceptos_aplican) or "(ninguno)")
            invalidar_caches()
            st.success(f"Conceptos guardados para el BL {bl}.")
            st.rerun()
        else:
            st.error(mensaje)


def form_sin_mora(enriquecido: pd.DataFrame):
    """Fecha límite saludable de pago, a criterio de Logística. Ya no calcula
    ni guarda montos — el total sale en vivo de los conceptos."""
    st.markdown("**Registrar fecha saludable (SIN MORA)**")

    if enriquecido.empty:
        st.info("Todavía no hay expedientes con conceptos registrados.")
        return

    opciones = sorted(enriquecido[COL_BL].astype(str).str.strip().unique())
    bl = st.selectbox("Expediente (BL)", opciones, key="sel_bl_sin_mora")
    fila = enriquecido[enriquecido[COL_BL].astype(str).str.strip() == bl].iloc[0]

    ya_registrado = str(fila.get(COL_FECHA_SIN_MORA, "")).strip()
    corregir = False
    if ya_registrado:
        st.info(f"Ya registrado: {ya_registrado}.")
        corregir = st.checkbox("Corregir fecha", key="corregir_sin_mora")
        if not corregir:
            return

    fecha = st.date_input("Fecha límite saludable de pago", value=hoy_rd(), format="DD/MM/YYYY",
                          key="fecha_sin_mora")

    if st.button("Confirmar", type="primary", key="btn_sin_mora"):
        ok, mensaje = registrar_sin_mora(bl, fecha, sobrescribir=corregir)
        if ok:
            registrar_log("Fecha saludable registrada", bl, "", fecha.isoformat())
            invalidar_caches()
            st.success("Guardado.")
            st.rerun()
        else:
            st.error(mensaje)


def form_pago_realizado(enriquecido: pd.DataFrame):
    """Fecha real de pago + el EXTRA pagado de más sobre los conceptos
    originales (0 si no hubo diferencia) — lo escribe Logística a mano, no se
    calcula solo. Marca el expediente como Pagado."""
    st.markdown("**Registrar Pago Realizado**")

    if enriquecido.empty:
        st.info("Todavía no hay expedientes con conceptos registrados.")
        return

    opciones = sorted(enriquecido[COL_BL].astype(str).str.strip().unique())
    bl = st.selectbox("Expediente (BL)", opciones, key="sel_bl_pago_real")
    fila = enriquecido[enriquecido[COL_BL].astype(str).str.strip() == bl].iloc[0]

    total = totales_conceptos(fila)
    if total is None:
        st.warning("Este expediente no tiene ningún concepto con monto todavía.")
        return
    st.caption(f"Total de los conceptos: {_fmt(total['USD'], 'USD')} · {_fmt(total['DOP'], 'DOP')}. "
              "El extra que pongas abajo se le suma a esto para mostrar el costo final.")

    ya_registrado = str(fila.get(COL_FECHA_PAGO_REAL, "")).strip()
    corregir = False
    if ya_registrado:
        st.info(f"Ya registrado: {ya_registrado}.")
        corregir = st.checkbox("Corregir fecha y/o extra", key="corregir_pago_real")
        if not corregir:
            return

    fecha = st.date_input("Fecha en que se pagó", value=hoy_rd(), format="DD/MM/YYYY",
                          key="fecha_pago_real")
    c1, c2 = st.columns(2)
    extra_previo_usd = a_numero(fila.get(COL_PAGOREAL_USD, "")) or 0.0
    extra_previo_dop = a_numero(fila.get(COL_PAGOREAL_DOP, "")) or 0.0
    extra_usd = c1.number_input("Extra pagado de más (USD) — 0 si no hubo diferencia", min_value=0.0,
                                value=float(extra_previo_usd), step=100.0, key="extra_usd_pago_real")
    extra_dop = c2.number_input("Extra pagado de más (DOP) — 0 si no hubo diferencia", min_value=0.0,
                                value=float(extra_previo_dop), step=100.0, key="extra_dop_pago_real")

    if st.button("Confirmar", type="primary", key="btn_pago_real"):
        extra = {"USD": extra_usd, "DOP": extra_dop}
        ok, mensaje = registrar_pago_realizado(bl, fecha, extra, sobrescribir=corregir)
        if ok:
            registrar_log("Pago Realizado registrado", bl, "",
                         f"{fecha.isoformat()} · extra {_fmt(extra_usd, 'USD')} · {_fmt(extra_dop, 'DOP')}")
            invalidar_caches()
            st.success("Guardado — expediente marcado como Pagado.")
            st.rerun()
        else:
            st.error(mensaje)


def form_estado_pago(enriquecido: pd.DataFrame):
    st.markdown("**Marcar estado (Pendiente / Pagado)**")
    if enriquecido.empty:
        st.info("No hay expedientes registrados.")
        return
    opciones = sorted(enriquecido[COL_BL].astype(str).str.strip().unique())
    bl = st.selectbox("Expediente (BL)", opciones, key="sel_bl_estado")
    fila = enriquecido[enriquecido[COL_BL].astype(str).str.strip() == bl].iloc[0]
    actual = str(fila.get(COL_ESTADO_PAGO, "")).strip() or ESTADO_PAGO_PENDIENTE
    estado = st.radio("Estado", [ESTADO_PAGO_PENDIENTE, ESTADO_PAGO_PAGADO],
                      index=0 if actual == ESTADO_PAGO_PENDIENTE else 1, key="radio_estado_pago")
    if st.button("Guardar estado", type="primary", key="btn_estado_pago"):
        ok, mensaje = marcar_estado_pago(bl, estado)
        if ok:
            registrar_log("Estado de pago actualizado", bl, "", estado)
            invalidar_caches()
            st.rerun()
        else:
            st.error(mensaje)


# ---------------------------------------------------------------------------
# PANEL PRINCIPAL — lo único que app.py necesita llamar
# ---------------------------------------------------------------------------
def _sello_actualizacion_pagos(df_pagos: pd.DataFrame) -> dict:
    """Última carga/persona que tocó CUALQUIER fila de Pagos — mismo cálculo
    que cargar_todo() hace para tránsito, pero sobre esta hoja. Aparte a
    propósito: si reusáramos el sello de datos['ultima_carga'] (que viene de
    tránsito), el encabezado de Pagos mostraría una hora que no tiene nada
    que ver con esta pestaña."""
    vacio = {"ultima_carga": None, "ultima_persona": ""}
    if df_pagos is None or df_pagos.empty or COL_ACTUALIZACION not in df_pagos.columns:
        return vacio
    marcas = [m for m in (parsear_marca(v) for v in df_pagos[COL_ACTUALIZACION]) if m]
    if not marcas:
        return vacio
    ultima = max(marcas)
    persona = ""
    if COL_ACTUALIZADO_POR in df_pagos.columns:
        for marca_val, autor in zip(df_pagos[COL_ACTUALIZACION], df_pagos[COL_ACTUALIZADO_POR]):
            if parsear_marca(marca_val) == ultima and str(autor).strip():
                persona = str(autor).strip()
                break
    return {"ultima_carga": ultima, "ultima_persona": persona}


def _encabezado_pagos(sello_info: dict):
    """Mismo encabezado con logo que usa el Dashboard de tránsito — reusa las
    clases CSS que ya trae CUSTOM_CSS (.ant-head, .ant-eyebrow, etc.), sin
    tocar ui_componentes.py — pero con título y sello propios de Pagos."""
    anio = hoy_rd().year
    ultima = sello_info.get("ultima_carga")
    persona = str(sello_info.get("ultima_persona", "") or "").strip()
    if ultima:
        sello = (f"Información actualizada: {ultima.day:02d} {MESES_ES_CORTO[ultima.month]} "
                 f"{ultima.year}, {ultima.strftime('%I:%M %p').lstrip('0').lower()} (hora RD)")
        if persona:
            sello += f" · por {persona}"
    else:
        sello = "Sin registro de la última carga de información en Pagos"
    logo = _logo_base64()
    img = f'<img class="ant-logo" src="{logo}" alt="Antillana Comercial">' if logo else ""
    st.markdown(
        f'<div class="ant-head">{img}'
        f'<span class="ant-eyebrow">'
        f'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#0C447C" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/>'
        f'<path d="M9 21v-6h6v6"/></svg> Logística e Importaciones {anio}</span>'
        f'<div class="ant-title">Estatus de Pagos</div>'
        f'<div class="ant-rule"></div>'
        f'<div class="ant-sub">Antillana Comercial</div>'
        f'<div class="ant-stamp"><span class="ant-dot"></span> {esc(sello)}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


@st.fragment
def panel_pagos(datos: dict, es_admin: bool):
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    activos = datos.get("activos", pd.DataFrame())
    historico = datos.get("historico", pd.DataFrame())
    df_pagos = datos.get("pagos", pd.DataFrame())
    _encabezado_pagos(_sello_actualizacion_pagos(df_pagos))

    # Sincronización automática: solo admin (los viewers nunca deben disparar
    # escrituras), y solo cuando la comparación en memoria encuentra un BL de
    # tránsito que Pagos todavía no tiene — así la mayoría de las veces esto no
    # cuesta ninguna llamada extra a la API.
    if es_admin and _hay_bls_sin_sincronizar(activos, historico, df_pagos):
        ok, mensaje = sincronizar_pagos_con_transito(activos, historico)
        if ok:
            invalidar_caches()
            st.rerun()
        else:
            st.warning(f"No se pudo sincronizar Pagos con tránsito automáticamente: {mensaje}")

    enriquecido = _enriquecer_pagos_cacheado(df_pagos, activos, historico)

    mostrar_dashboard_pagos(enriquecido)

    if not es_admin:
        return

    st.divider()
    with st.expander("Registrar / editar conceptos de un expediente"):
        form_registrar_conceptos(enriquecido, activos, historico)
    with st.expander("Registrar fecha saludable (SIN MORA)"):
        form_sin_mora(enriquecido)
    with st.expander("Registrar Pago Realizado"):
        form_pago_realizado(enriquecido)
    with st.expander("Marcar estado (Pendiente/Pagado)"):
        form_estado_pago(enriquecido)
    with st.expander("Activar selectores en Sheets (fechas y Empresa)"):
        st.caption("Solo hace falta correrlo una vez. Agrega el ícono de calendario nativo de Google "
                  "Sheets en las fechas, y una lista desplegable en Empresa, para elegir con clic en "
                  "vez de teclear.")
        if st.button("Activar selectores", key="btn_selector_fecha"):
            ok, mensaje = aplicar_selectores_pagos()
            (st.success if ok else st.error)(mensaje)
        st.divider()
        st.caption("Puramente cosmético — la app siempre busca las columnas por nombre, nunca por "
                  "posición. Esto solo cambia cómo se VE la hoja al trabajar directo en Sheets.")
        if st.button("Mover 'Empresa' antes de BL", key="btn_mover_empresa"):
            ok, mensaje = mover_empresa_primera_columna()
            (st.success if ok else st.error)(mensaje)
