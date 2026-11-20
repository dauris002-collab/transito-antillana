"""
entrega_provisional.py - Origen «Entrega Provisional» dentro de Estatus de Pagos (v5.17)
=========================================================================================

Pagos de entregas provisionales: una pestaña `Entrega Provisional` del MISMO Sheet
principal (antes de `Pagos`), con una fila por declaración. Los datos se cargan a mano
en el Sheet; la app solo los LEE (no hay formulario ni escritura de datos), salvo la
creación de la pestaña vacía, la primera vez que un administrador abre esta vista.

Reglas (decididas el 19 nov 2026):
 - `Fecha_Limite_Pago` (en pantalla «Fecha de Vencimiento») es la fecha LÍMITE de pago de lo que
   se cargue aquí; no viene de Tránsito. Los días de retraso empiezan a contar cuando
   esa fecha vence: hoy - fecha límite (0 el mismo día del vencimiento; antes de vencer
   no hay contador). Se marcan en rojo cuando pasan de DIAS_ROJO_EP (más de 2).
 - Pagado = hay fecha en `Fecha_PagoRealizado` (misma idea que Pagos, decisión #9, pero
   sin columna `Estado_Pago`: una sola fuente). Un pagado sale de Pendientes y se
   consulta con el filtro Estatus. Sus días de retraso quedan fijos: fecha pagada -
   fecha límite, mínimo 0.
 - Solo pesos (`Monto_DOP`). Total por pagar = suma de los pendientes.
 - La empresa es siempre Antillana Comercial: la columna `Empresa` del Sheet se ignora
   (mismo criterio que las categorías de empresa fija, decisión #18) y no hay filtro.
 - «Pendiente por mes» agrupa por el mes de la fecha de pago (límite) y cuenta las entregas
   PENDIENTES de cada mes.

Capa: como taller.py, importa de sheets_io hacia abajo y NO importa pagos.py. Todas las
constantes viven AQUÍ y no en sheets_io a propósito: un push que a la vez agregue nombres
a sheets_io y los importe desde otro módulo tumba la app con ImportError hasta reiniciarla
(CONTEXT.md, 17 nov 2026). Un módulo nuevo que solo importa nombres que ya existen no
puede caer así. Lectura propia y caché propia: un fallo de esta vista no toca Tránsito ni
Pagos.
"""

from __future__ import annotations

from html import escape as _esc
from numbers import Real

import gspread
import pandas as pd
import streamlit as st

import sheets_io as sio
from sheets_io import (
    EMPRESA_ANTILLANA, MESES_ES_CORTO, _codigo_api, _con_reintento, _df_desde_valores, _norm_encabezado,
    a_numero, ahora_rd, hoy_rd, parsear_fecha,
)
from ui_encabezado import (encabezado_html, recordar_filtro, restaurar_filtro, selectbox_sincronizado,
                           valor_previo)

NOMBRE_EP = "Entrega Provisional"          # nombre visible del Origen y de la pestaña
PESTANA_SIGUIENTE = "Pagos"                # la pestaña nueva se inserta justo antes de esta

COL_EMPRESA = "Empresa"
COL_DECLARACION = "Numero_Declaracion"
COL_BL = "BL"
COL_DESC = "Descripcion"
COL_CANT = "Cantidad"
COL_LLEGADA = "Llegada"
COL_LIMITE = "Fecha_Limite_Pago"
COL_MONTO = "Monto_DOP"
COL_PAGADO = "Fecha_PagoRealizado"
COLUMNAS_EP = [COL_EMPRESA, COL_DECLARACION, COL_BL, COL_DESC, COL_CANT, COL_LLEGADA,
               COL_LIMITE, COL_MONTO, COL_PAGADO]

# Encabezados alternativos aceptados en el Sheet (la app trabaja con los canónicos).
ALIAS_ENCABEZADOS_EP = {
    COL_DECLARACION: ("Numero de Declaracion", "No. Declaracion", "N Declaracion", "Declaracion"),
    COL_LIMITE: ("Fecha de Vencimiento", "Fecha Vencimiento", "Fecha de Pago", "Fecha Limite", "Fecha Limite de Pago", "Fecha Pago"),
    COL_MONTO: ("DOP Pendiente", "Monto RD$", "Monto RD", "Monto DOP", "Pendiente RD$"),
    COL_PAGADO: ("Fecha de Pagado", "Fecha Pagado", "Fecha Pago Realizado", "Pagado el"),
}

DIAS_ROJO_EP = 2          # más de 2 días de retraso: el contador sale en rojo
MAX_FILAS_EP = 5000
TTL_EP = 120              # igual que Taller: toda escritura de la app limpia la caché
_ESTATUS = ["Todos", "Pendiente", "Pagado"]
_COLUMNAS = ["Fila", "Empresa", "Declaracion", "BL", "Descripcion", "Cantidad", "Llegada",
             "Limite", "Pago", "Pagado", "Dias", "DOP"]


# ---------------------------------------------------------------------------
# LECTURA
# ---------------------------------------------------------------------------
def _canonizar_encabezados(headers: list) -> list:
    """Reemplaza los alias por el nombre canónico. Si ya existe la columna canónica, el
    alias no se toca (no se duplica)."""
    headers = [str(h) for h in headers]
    presentes = {_norm_encabezado(h) for h in headers}
    for canon, alias in ALIAS_ENCABEZADOS_EP.items():
        if _norm_encabezado(canon) in presentes:
            continue
        validos = {_norm_encabezado(a) for a in alias}
        for i, h in enumerate(headers):
            if _norm_encabezado(h) in validos:
                headers[i] = canon
                presentes.add(_norm_encabezado(canon))
                break
    return headers


def _vacio() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNAS_EP + ["FilaSheet"])


@st.cache_data(ttl=TTL_EP, show_spinner=False)
def _cargar_ep_remoto(token: str = "") -> dict:
    """`token` no se usa dentro: solo parte la caché. pagos.py pasa la hora de la última lectura
    del Sheet principal, así ↻ Actualizar (y cualquier recarga de Tránsito) también recarga esta
    vista sin tocar sheets_io.invalidar_caches. UNA llamada a la API (más abrir el libro). No atrapa errores de lectura:
    st.cache_data solo guarda retornos exitosos, así que un fallo no queda cacheado.
    Lee valores SIN formato (números y fechas seriales): no depende de la configuración
    regional del Sheet. Si la pestaña no existe, Google contesta 400 y se devuelve
    existe=False (se cachea poco: crear_pestana la limpia)."""
    try:
        ss = sio.get_spreadsheet()
        rango = f"'{NOMBRE_EP}'!A1:Z{MAX_FILAS_EP}"
        params = {"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"}
        respuesta = _con_reintento(lambda: ss.values_get(rango, params=params))
    except gspread.exceptions.APIError as e:
        if _codigo_api(e) == 400:
            return {"existe": False, "df": _vacio(), "hora": ahora_rd()}
        if _codigo_api(e) == 403:
            raise RuntimeError("Google Sheets rechazó el acceso. Verifica que el Sheet siga compartido "
                               "como Editor con la cuenta de servicio de la app.") from e
        raise RuntimeError(f"Google Sheets no respondió al leer '{NOMBRE_EP}' ({e}).") from e
    valores = respuesta.get("values", [])
    if valores and valores[0]:
        valores = [_canonizar_encabezados(valores[0])] + valores[1:]
    df = _df_desde_valores(valores, COLUMNAS_EP)
    df["FilaSheet"] = range(2, len(df) + 2)   # fila real = índice + 2 (encabezado + base 1)
    return {"existe": True, "df": df, "hora": ahora_rd()}


def cargar_ep(token: str = "") -> dict:
    """Lectura cacheada. El FRACASO nunca sube como excepción: vuelve en 'error' para que
    solo esta vista lo muestre."""
    try:
        r = _cargar_ep_remoto(str(token or ""))
    except Exception as e:  # noqa: BLE001 - la UI muestra el mensaje; nunca un traceback
        return {"existe": True, "df": _vacio(), "hora": ahora_rd(), "aviso": "", "error": str(e)}
    aviso = ""
    if len(r["df"]) + 1 >= MAX_FILAS_EP:
        aviso = (f"La pestaña '{NOMBRE_EP}' llegó al tope de {MAX_FILAS_EP:,} filas que la app lee. "
                 "Puede haber renglones más abajo que no se muestran.")
    return {**r, "aviso": aviso, "error": None}


def invalidar_ep():
    """La siguiente vista de Entrega Provisional va a Google sin esperar la caché."""
    _cargar_ep_remoto.clear()


# ---------------------------------------------------------------------------
# CREACIÓN DE LA PESTAÑA (solo admin, solo si no existe)
# ---------------------------------------------------------------------------
def _formato_pestana(ws) -> None:
    """Mejor esfuerzo, igual que Pagos: encabezado en negrita y fijo, fechas y monto con
    formato, BL y declaración como TEXTO (un número de 16 dígitos pegado como número pierde
    el último dígito en Sheets), y listas/calendario nativos con 'strict': False (advierte,
    no rechaza). Si algo falla la pestaña ya existe y funciona: no se propaga."""
    def rango(idx, fila_ini=1):
        return {"sheetId": ws.id, "startRowIndex": fila_ini, "endRowIndex": 1000,
                "startColumnIndex": idx, "endColumnIndex": idx + 1}

    def formato(idx, tipo, patron=None):
        nf = {"type": tipo}
        if patron:
            nf["pattern"] = patron
        return {"repeatCell": {"range": rango(idx), "cell": {"userEnteredFormat": {"numberFormat": nf}},
                               "fields": "userEnteredFormat.numberFormat"}}

    i = {c: n for n, c in enumerate(COLUMNAS_EP)}
    peticiones = [
        {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        formato(i[COL_DECLARACION], "TEXT"), formato(i[COL_BL], "TEXT"),
        formato(i[COL_MONTO], "NUMBER", "#,##0.00"),
    ]
    for col in (COL_LLEGADA, COL_LIMITE, COL_PAGADO):
        # Sin validación de fecha: la app lee cualquier forma (17/10/2026, 17-oct-26, 2026-10-17, fecha
        # real de Sheets...) y DATE_IS_VALID marcaba con triángulo rojo las escritas como texto.
        peticiones.append(formato(i[col], "DATE", "dd/mm/yyyy"))
    peticiones.append({"setDataValidation": {"range": rango(i[COL_EMPRESA]), "rule": {
        "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": EMPRESA_ANTILLANA}]},
        "showCustomUi": True, "strict": False}}})
    ws.spreadsheet.batch_update({"requests": peticiones})


def quitar_validacion_fechas(ws=None) -> None:
    """Pestañas ya creadas con la validación de fecha vieja: la quita de las 3 columnas de fecha
    (los triángulos rojos). Mejor esfuerzo, idempotente, sin efecto en los datos."""
    if ws is None:
        ws = sio.get_spreadsheet().worksheet(NOMBRE_EP)
    peticiones = []
    for col in (COL_LLEGADA, COL_LIMITE, COL_PAGADO):
        n = COLUMNAS_EP.index(col)
        peticiones.append({"setDataValidation": {"range": {
            "sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 1000,
            "startColumnIndex": n, "endColumnIndex": n + 1}}})   # sin "rule" = quita la validación
    _con_reintento(lambda: ws.spreadsheet.batch_update({"requests": peticiones}))


def crear_pestana() -> tuple:
    """Crea la pestaña con sus encabezados justo ANTES de 'Pagos' (o al final si Pagos no
    existe). No pisa nada: si ya existe, no hace nada. Devuelve (ok, mensaje). Solo admin."""
    if st.session_state.get("rol") != "admin":
        return False, "Solo un administrador puede crear la pestaña."
    try:
        ss = sio.get_spreadsheet()
        titulos = [w.title for w in _con_reintento(lambda: ss.worksheets())]
        if any(t.strip().lower() == NOMBRE_EP.lower() for t in titulos):
            return True, "La pestaña ya existe."
        pos = next((n for n, t in enumerate(titulos) if t.strip().lower() == PESTANA_SIGUIENTE.lower()), None)
        extra = {} if pos is None else {"index": pos}
        ws = _con_reintento(lambda: ss.add_worksheet(title=NOMBRE_EP, rows=1000, cols=len(COLUMNAS_EP), **extra))
        _con_reintento(lambda: ws.update(range_name="A1", values=[COLUMNAS_EP], value_input_option="RAW"))
    except gspread.exceptions.APIError as e:
        if _codigo_api(e) == 429:
            return False, "Google Sheets está limitando las peticiones. Espera unos segundos y vuelve a abrir esta vista."
        if _codigo_api(e) == 403:
            return False, "Google Sheets rechazó crear la pestaña por permisos: la cuenta de servicio debe ser Editor."
        return False, f"Google Sheets rechazó crear la pestaña ({e})."
    except Exception as e:  # noqa: BLE001
        return False, f"No se pudo crear la pestaña '{NOMBRE_EP}': {e}"
    try:
        _formato_pestana(ws)
    except Exception:  # noqa: BLE001 - cosmético
        pass
    sio.registrar_log("Pestaña creada", bl="", categoria=NOMBRE_EP, detalle="Creada automáticamente antes de 'Pagos'")
    sio._refrescar_estructura()
    invalidar_ep()
    return True, f"Pestaña '{NOMBRE_EP}' creada."


# ---------------------------------------------------------------------------
# REGLAS DE NEGOCIO (puras)
# ---------------------------------------------------------------------------
def _plano(v):
    return v.item() if hasattr(v, "item") else v


def _texto(v) -> str:
    v = _plano(v)
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _monto(v):
    v = _plano(v)
    if isinstance(v, Real) and not isinstance(v, bool):
        return float(v)
    return a_numero(v)


def dias_de_retraso(limite, pago, hoy):
    """Pendiente: hoy - límite, solo si ya venció (hoy >= límite); antes, None.
    Pagado: pago - límite, mínimo 0. Sin fecha límite: None."""
    if limite is None:
        return None
    if pago is not None:
        return max(0, (pago - limite).days)
    if hoy < limite:
        return None
    return (hoy - limite).days


def enriquecer_ep(df: pd.DataFrame, hoy):
    """Devuelve (DataFrame limpio, avisos sobre los datos del Sheet)."""
    filas, avisos, vistas = [], [], {}
    for _, r in df.iterrows():
        decl, bl, desc = (_texto(r.get(COL_DECLARACION)), _texto(r.get(COL_BL)), _texto(r.get(COL_DESC)))
        dop = _monto(r.get(COL_MONTO))
        if not decl and not bl and not desc and dop is None:
            continue  # fila preparada y todavía en blanco
        n = int(r["FilaSheet"])
        lim_crudo, pag_crudo = _plano(r.get(COL_LIMITE)), _plano(r.get(COL_PAGADO))
        limite, pago = parsear_fecha(lim_crudo), parsear_fecha(pag_crudo)
        if limite is None:
            avisos.append(f"Fila {n}: " + (f"fecha de vencimiento '{_texto(lim_crudo)}' no válida; no se cuentan días."
                                           if _texto(lim_crudo) else "sin fecha de vencimiento; no se cuentan días."))
        if pago is None and _texto(pag_crudo):
            avisos.append(f"Fila {n}: fecha de pago realizado '{_texto(pag_crudo)}' no válida; se toma como pendiente.")
        if dop is None:
            avisos.append(f"Fila {n}: sin monto.")
        if not decl:
            avisos.append(f"Fila {n}: sin número de declaración.")
        elif decl in vistas:
            avisos.append(f"Fila {n}: el número de declaración {decl} ya está en la fila {vistas[decl]}.")
        else:
            vistas[decl] = n
        llegada_crudo = _plano(r.get(COL_LLEGADA))
        llegada = parsear_fecha(llegada_crudo)
        if llegada is None and _texto(llegada_crudo):
            avisos.append(f"Fila {n}: llegada '{_texto(llegada_crudo)}' no válida.")
        filas.append({"Fila": n, "Empresa": EMPRESA_ANTILLANA, "Declaracion": decl, "BL": bl,
                      "Descripcion": desc, "Cantidad": _texto(r.get(COL_CANT)), "Llegada": llegada,
                      "Limite": limite, "Pago": pago, "Pagado": pago is not None,
                      "Dias": dias_de_retraso(limite, pago, hoy), "DOP": dop})
    out = pd.DataFrame(filas, columns=_COLUMNAS)
    for c in ("Dias", "DOP"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out, avisos


def ordenar_pendientes(df: pd.DataFrame) -> pd.DataFrame:
    """Más días de retraso primero; los que aún no vencen (sin contador) después."""
    clave = pd.DataFrame({"s": df["Dias"].isna(), "d": -df["Dias"].fillna(0), "f": df["Fila"]}, index=df.index)
    return df.loc[clave.sort_values(["s", "d", "f"]).index]


def total_por_pagar(pend: pd.DataFrame) -> float:
    return float(pend["DOP"].fillna(0).sum()) if len(pend) else 0.0


# ---------------------------------------------------------------------------
# PRESENTACIÓN
# ---------------------------------------------------------------------------
_CSS = (
    "<style>"
    ".ep-wrap { overflow-x:auto; margin:6px 0 16px; }"
    ".ep-tabla { width:100%; border-collapse:separate; border-spacing:0; background:#fff;"
    " border:1px solid #E5E7EB; border-radius:12px; overflow:hidden; box-shadow:0 1px 4px rgba(17,24,39,0.06); }"
    ".ep-tabla th { background:#DBEAFE; color:#1E3A8A; font-size:0.72rem; font-weight:700; text-transform:uppercase;"
    " letter-spacing:0.05em; padding:11px 14px; text-align:center; white-space:nowrap;"
    " border-bottom:1px solid #BFDBFE; }"
    ".ep-tabla td { padding:9px 14px; text-align:center; font-size:0.88rem; color:#111827;"
    " border-top:1px solid #F3F4F6; }"
    ".ep-tabla tbody tr:nth-child(even) { background:#F9FAFB; }"
    ".ep-tabla td.d { font-weight:800; } .ep-tabla td.v { color:#9CA3AF; }"
    ".ep-tabla td.rojo { font-weight:800; color:#B91C1C; }"
    ".ep-card { border:1px solid #E5E7EB; border-radius:12px; padding:16px 20px; margin-bottom:14px;"
    " background:#fff; box-shadow:0 1px 4px rgba(17,24,39,0.06); }"
    ".ep-cabeza { display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }"
    ".ep-bl { font-weight:700; font-size:1.02rem; color:#111827; }"
    ".ep-emp { display:inline-block; background:#EEF2FF; color:#3730A3; font-size:0.72rem; font-weight:700;"
    " padding:1px 10px; border-radius:999px; margin-left:10px; }"
    ".ep-estado { display:inline-block; font-size:0.75rem; font-weight:700; color:#fff; padding:2px 12px;"
    " border-radius:999px; }"
    ".ep-meta { color:#6B7280; font-size:0.85rem; margin-top:2px; }"
    ".ep-decl { color:#1E3A8A; font-size:0.82rem; font-weight:600; margin-top:2px; }"
    ".ep-totales { display:flex; flex-wrap:wrap; justify-content:center; align-items:baseline; gap:28px;"
    " margin-top:14px; }"
    ".ep-etq { font-size:0.68rem; text-transform:uppercase; letter-spacing:0.04em; color:#6B7280; display:block;"
    " text-align:center; }"
    ".ep-val { font-size:1.2rem; font-weight:800; color:#111827; display:block; text-align:center; }"
    ".ep-val.f { font-size:0.95rem; } .ep-val.rojo { color:#B91C1C; }"
    ".ep-tarjetas { display:grid; grid-template-columns:repeat(2,1fr); gap:1rem; margin:0.4rem 0 0.8rem; }"
    ".ep-t { color:#fff; border-radius:14px; min-height:92px; display:flex; flex-direction:column;"
    " align-items:center; justify-content:center; padding:14px 10px; text-align:center;"
    " box-shadow:0 2px 8px rgba(17,24,39,0.12); }"
    ".ep-t .e { font-size:0.68rem; font-weight:700; letter-spacing:0.05em; text-transform:uppercase; opacity:0.92; }"
    ".ep-t .n { font-size:1.35rem; font-weight:800; margin-top:6px; }"
    "@media (max-width:640px) { .ep-totales { gap:14px 18px; } .ep-tarjetas { grid-template-columns:1fr; } }"
    "</style>"
)


def _fd(d) -> str:
    return "—" if d is None or pd.isna(d) else f"{d.day:02d} {MESES_ES_CORTO[d.month]} {d.year}"


def _fn(d) -> str:
    """Fecha de vencimiento / pago con el mes en letras en el medio: 26/nov/2026 (sin ambigüedad)."""
    return "—" if d is None or pd.isna(d) else f"{d.day:02d}/{MESES_ES_CORTO[d.month]}/{d.year}"


def _dop(x) -> str:
    return "—" if x is None or pd.isna(x) else f"RD$ {x:,.2f}"


def _celda(x, clase: str = "") -> str:
    vacio = x is None or x == "" or (isinstance(x, float) and pd.isna(x))
    return '<td class="v">—</td>' if vacio else f'<td class="{clase}">{_esc(str(x))}</td>'


def _hay(d) -> bool:
    return d is not None and not pd.isna(d)


def _rojo(dias) -> bool:
    return not pd.isna(dias) and dias > DIAS_ROJO_EP


def _tabla_pendientes(df: pd.DataFrame) -> str:
    cab = ["Empresa", "Número de Declaración", "BL", "Descripción", "Fecha de Vencimiento", "Días de retraso",
           "DOP Pendiente"]
    filas = []
    for r in df.itertuples():
        dias = "" if pd.isna(r.Dias) else int(r.Dias)
        celdas = [_celda(r.Empresa), _celda(r.Declaracion), _celda(r.BL), _celda(r.Descripcion),
                  _celda(_fn(r.Limite) if _hay(r.Limite) else ""),
                  _celda(dias, "rojo" if _rojo(r.Dias) else "d"),
                  _celda(_dop(r.DOP) if not pd.isna(r.DOP) else "")]
        filas.append("<tr>" + "".join(celdas) + "</tr>")
    return ('<div class="ep-wrap"><table class="ep-tabla"><thead><tr>'
            + "".join(f"<th>{c}</th>" for c in cab) + "</tr></thead><tbody>" + "".join(filas)
            + "</tbody></table></div>")


def _tarjeta(r) -> str:
    """Desglose de un expediente: BL y empresa, descripción · cantidad · llegada, y totales."""
    pagado = bool(r.Pagado)
    partes = [_esc(r.Descripcion) if r.Descripcion else "",
              _esc(r.Cantidad) if r.Cantidad else "",
              f"Llegada: {_fd(r.Llegada)}" if _hay(r.Llegada) else ""]
    meta = " · ".join(p for p in partes if p)
    dias = "—" if pd.isna(r.Dias) else str(int(r.Dias))
    clase_dias = " rojo" if (not pagado and _rojo(r.Dias)) else ""
    totales = [
        f'<div><span class="ep-etq">{"Monto pagado RD$" if pagado else "Total a pagar RD$"}</span>'
        f'<span class="ep-val">{_dop(r.DOP)}</span></div>',
        f'<div><span class="ep-etq">Fecha de vencimiento</span><span class="ep-val f">{_fn(r.Limite)}</span></div>',
    ]
    if pagado:
        totales.append(f'<div><span class="ep-etq">Pagado el</span><span class="ep-val f">{_fn(r.Pago)}</span></div>')
    totales.append(f'<div><span class="ep-etq">Días de retraso</span><span class="ep-val{clase_dias}">{dias}</span></div>')
    estado = ('<span class="ep-estado" style="background:#2E7D32;">Pagado</span>' if pagado
              else '<span class="ep-estado" style="background:#B45309;">Pendiente</span>')
    return ('<div class="ep-card">'
            f'<div class="ep-cabeza"><span class="ep-bl">{_esc(r.BL) or "(sin BL)"}'
            f'<span class="ep-emp">{_esc(r.Empresa)}</span></span>{estado}</div>'
            + (f'<div class="ep-decl">Declaración {_esc(r.Declaracion)}</div>' if r.Declaracion else "")
            + (f'<div class="ep-meta">{meta}</div>' if meta else "")
            + f'<div class="ep-totales">{"".join(totales)}</div></div>')


def _tarjetas_resumen(pend: pd.DataFrame) -> str:
    t = lambda color, etq, val: (f'<div class="ep-t" style="background:{color};"><div class="e">{etq}</div>'
                                 f'<div class="n">{val}</div></div>')
    return ('<div class="ep-tarjetas">' + t("#2E86DE", "Pendientes", len(pend))
            + t("#0C447C", "Total por pagar", _dop(total_por_pagar(pend))) + "</div>")


def _encabezado(hora) -> None:
    sello = f"Pestaña '{NOMBRE_EP}' leída a las {hora.strftime('%I:%M %p').lstrip('0').lower()} (hora RD)"
    st.markdown(encabezado_html("Estatus de Pagos", "taller", hoy_rd().year, sello, f" · {NOMBRE_EP}"),
                unsafe_allow_html=True)


def _clave_mes(d) -> str:
    return f"{d.year}-{d.month:02d}"


def _etiqueta_mes(clave: str, conteo: dict) -> str:
    if clave == "Todos":
        return "Todos"
    n = conteo.get(clave, 0)
    return f"{MESES_ES_CORTO[int(clave[5:])]} {clave[:4]} · {n} {'entrega' if n == 1 else 'entregas'}"


def panel_entrega_provisional(es_admin: bool, token: str = ""):
    """Contenido de la pestaña «Entrega Provisional» de Estatus de Pagos. `token`: hora de la
    última lectura del Sheet principal (ver _cargar_ep_remoto)."""
    st.markdown(_CSS, unsafe_allow_html=True)
    datos = cargar_ep(token)
    _encabezado(datos["hora"])
    c1, c2, _c3 = st.columns(3)     # 3 columnas como las otras pestañas: los filtros no saltan al cambiar
    if datos["error"]:
        st.error(datos["error"])
        return
    if not datos["existe"]:
        # Solo admin, y una vez por sesión: si falla no se entra en bucle de reintentos.
        if es_admin and not st.session_state.get("ep_creacion_intentada"):
            st.session_state["ep_creacion_intentada"] = True
            ok, mensaje = crear_pestana()
            st.session_state["ep_creacion_mensaje"] = mensaje
            if ok:
                st.rerun()
        mensaje = st.session_state.get("ep_creacion_mensaje")
        if es_admin and mensaje:
            st.warning(f"No se pudo crear la pestaña '{NOMBRE_EP}': {mensaje}")
        else:
            st.info(f"La pestaña '{NOMBRE_EP}' todavía no existe en el Sheet. Un administrador la crea "
                    "al abrir esta vista.")
        return
    if es_admin and not st.session_state.get("ep_validacion_quitada"):
        st.session_state["ep_validacion_quitada"] = True   # una vez por sesión, sin reintentos
        try:
            quitar_validacion_fechas()
        except Exception:  # noqa: BLE001 - cosmético
            pass
    if datos["aviso"]:
        st.warning(datos["aviso"])

    df, avisos = enriquecer_ep(datos["df"], hoy_rd())
    if df.empty:
        st.info(f"La pestaña '{NOMBRE_EP}' todavía no tiene filas con datos. Se cargan directo en el Sheet: "
                f"{', '.join(COLUMNAS_EP)}.")
        return

    # Pendiente por mes: mes de la fecha de vencimiento y cuántas entregas PENDIENTES caen en él.
    # Las etiquetas llevan un conteo que cambia, por eso selectbox_sincronizado (decisión #25).
    claves = sorted({_clave_mes(d) for d in df["Limite"] if _hay(d)})
    conteo = {k: 0 for k in claves}
    for d in df.loc[~df["Pagado"], "Limite"]:
        if _hay(d):
            conteo[_clave_mes(d)] += 1
    opciones_mes = ["Todos"] + claves
    restaurar_filtro("ep_estatus", list(_ESTATUS), _ESTATUS[0])
    with c1:
        estatus = st.selectbox("Estatus", _ESTATUS, key="ep_estatus")
        recordar_filtro("ep_estatus")
    with c2:
        mes = selectbox_sincronizado("Pendiente por mes", opciones_mes, "ep_mes",
                                     lambda k: _etiqueta_mes(k, conteo),
                                     valor_previo("ep_mes", opciones_mes, "Todos"))
    vista = df
    if mes != "Todos":
        vista = vista[vista["Limite"].apply(lambda d: _hay(d) and _clave_mes(d) == mes)]

    pend, pagados = vista[~vista["Pagado"]], vista[vista["Pagado"]]
    st.markdown(_tarjetas_resumen(pend), unsafe_allow_html=True)
    st.caption("Días de retraso: desde la fecha de vencimiento hasta hoy, o hasta el pago realizado. "
               f"En rojo con más de {DIAS_ROJO_EP}. Pagado = tiene fecha en {COL_PAGADO}.")

    if estatus in ("Todos", "Pendiente"):
        if pend.empty:
            st.caption("No hay pendientes con esta selección.")
        else:
            st.markdown(_tabla_pendientes(ordenar_pendientes(pend)), unsafe_allow_html=True)

    pagados = pagados.sort_values("Pago", ascending=False)
    mostrar = {"Todos": pd.concat([ordenar_pendientes(pend), pagados]),
               "Pendiente": ordenar_pendientes(pend),
               "Pagado": pagados}[estatus]
    if estatus == "Pagado" and mostrar.empty:
        st.caption("No hay pagos registrados con esta selección.")
    if not mostrar.empty:
        st.markdown("".join(_tarjeta(r) for r in mostrar.itertuples()), unsafe_allow_html=True)

    if es_admin and avisos:
        with st.expander(f"Datos por revisar en el Sheet ({len(avisos)})"):
            for a in avisos[:50]:
                st.write(a)
            if len(avisos) > 50:
                st.write(f"… y {len(avisos) - 50} más.")
