# -*- coding: utf-8 -*-
"""
Genera "Historico_Importaciones_Plataforma.xlsx" a partir de los PDF de liquidación SIGA
(Reporte de Liquidación de Impuestos) guardados en esta carpeta.

Uso (Windows):
    pip install pymupdf openpyxl
    python generar_historico_plataforma.py            # procesa la carpeta donde está este archivo
    python generar_historico_plataforma.py "C:\\ruta"  # u otra carpeta

Estructura esperada:  <Empresa>, Importaciones <Año>\\<Administración>\\DOC-xxxxx.pdf
NO extrae montos (FOB, CIF, gravamen, ITBIS, tasas ni totales): solo datos descriptivos.
"""
import collections
import datetime as dt
import re
import sys
import time
import unicodedata
from pathlib import Path

import pymupdf
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

SALIDA = "Historico_Importaciones_Plataforma.xlsx"

ADMINISTRACIONES = {  # código de la administración (primeros 5 dígitos de la declaración)
    "10010": "Santo Domingo", "10030": "Haina Oriental", "10150": "Caucedo",
    "20050": "Aeropuerto", "20040": "Aeropuerto",
}

# Encabezado: tres columnas de "Etiqueta : Valor". Por columna: (inicio, fin) en puntos y rango donde
# caen sus dos puntos ":"; el valor empieza justo después de los dos puntos (varía según el año del PDF).
COLUMNAS_ENC = [((28, 220), (60, 130)), ((220, 404), (255, 290)), ((404, 590), (430, 460))]
CAMPOS = {  # etiqueta normalizada -> nombre de columna
    "declaracion": "Declaracion", "fecha decl.": "Fecha_Declaracion", "regimen": "Regimen",
    "estado": "Estado", "manifiesto": "Manifiesto", "consignatario": "Consignatario",
    "fecha llegada": "Fecha_Llegada", "numero b/l": "BL", "endosado": "Endosado", "adm": "Adm_Texto",
    "importador": "Importador", "documento": "RNC_Importador", "agencia": "Agencia",
    "despacho": "Despacho", "dep fiscal": "Deposito_Fiscal", "ded madre": "Declaracion_Madre",
}


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def lineas(palabras, tol=2.6):
    """Agrupa palabras en líneas por el centro vertical."""
    out = []
    for w in sorted(palabras, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        yc = (w[1] + w[3]) / 2
        if out and abs(out[-1][0] - yc) <= tol:
            out[-1][1].append(w)
        else:
            out.append([yc, [w]])
    return [(y, sorted(ws, key=lambda w: w[0])) for y, ws in out]


def texto(ws):
    return " ".join(w[4] for _, ln in lineas(ws) for w in ln).strip()


def fecha(s):
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    return dt.date(int(m[3]), int(m[2]), int(m[1])) if m else None


def numero(s):
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def encabezado(pal, y_fin):
    res = {}
    pal = [w for w in pal if 100 < (w[1] + w[3]) / 2 < y_fin - 2]
    for (a, d), (ca, cb) in COLUMNAS_ENC:
        dos = sorted(w[2] for w in pal if w[4].endswith(":") and ca <= w[0] <= cb)
        if not dos:
            continue
        c = collections.Counter(round(x) for x in dos).most_common(1)[0][0] + 1
        etq = [w for w in pal if a <= w[0] < c]
        val = [w for w in pal if c <= w[0] < d]
        filas = lineas(etq)
        for k, (y, ws) in enumerate(filas):
            y_sig = filas[k + 1][0] if k + 1 < len(filas) else y_fin
            nombre = norm(texto(ws).replace(":", "")).strip()
            v = [w for w in val if y - 3.5 <= (w[1] + w[3]) / 2 < y_sig - 3.5]
            if nombre in CAMPOS and CAMPOS[nombre] not in res:
                res[CAMPOS[nombre]] = texto(v)
    return res


def tabla_simple(filas, cab, nombres):
    """Filas de VEHICULOS / FURGONES. cab = [(x0, x1)] de cada título; el límite entre dos columnas
    es el punto medio entre el final de un título y el inicio del siguiente (los títulos van centrados)."""
    xs = [cab[0][0] - 10] + [(cab[i - 1][1] + cab[i][0]) / 2 for i in range(1, len(cab))]
    out = []
    for _, ws in filas:
        celdas = [[] for _ in xs]
        for w in ws:
            i = max([j for j, x in enumerate(xs) if w[0] >= x] or [0])
            celdas[i].append(w[4])
        celdas = [" ".join(c) for c in celdas]
        if re.fullmatch(r"\d+", celdas[0] or ""):
            out.append(dict(zip(nombres, celdas)))
        elif out:  # renglón que continúa la fila anterior
            for n, c in zip(nombres, celdas):
                if c:
                    out[-1][n] = (out[-1][n] + " " + c).strip()
    return out


def leer_pdf(ruta):
    doc = pymupdf.open(ruta)
    paginas = [(p.get_text("words"), p.get_drawings(), p.rect.height) for p in doc]
    doc.close()
    pal0 = paginas[0][0]
    problemas = []

    y_det = next(((w[1] + w[3]) / 2 for w in pal0 if w[4] == "DETALLE"), None)
    y_obs = next(((w[1] + w[3]) / 2 for w in pal0 if w[4] == "OBSERVACIONES"), None)
    enc = encabezado(pal0, y_det or 230)

    # Detalle del manifiesto y observaciones
    det = obs = ""
    if y_det:
        col = next((w for w in pal0 if w[4] == ":" and abs((w[1] + w[3]) / 2 - y_det) < 3 and w[0] > 150), None)
        x0 = col[2] if col else 0
        det = texto([w for w in pal0 if y_det - 3 <= (w[1] + w[3]) / 2 < (y_obs or y_det + 20) - 3 and
                     (abs((w[1] + w[3]) / 2 - y_det) > 3 or w[0] > x0)])

    # ---- tabla de ítems (puede ocupar varias páginas) ----
    items = []
    en_tabla = False
    bordes = None
    tras_items = []  # (pagina, palabras) después de "Totales"
    for np_, (pal, dibujos, alto) in enumerate(paginas):
        cab = None
        filas_pal = lineas(pal)
        for y, ws in filas_pal:
            t = [w[4] for w in ws]
            if "ITEM" in t and "ARANCEL" in t:
                cab = (y, ws)
                break
        if cab:
            en_tabla = True
            y_ini = cab[1][0][3] + 0.5
            if np_ == 0 and y_obs:
                obs = texto([w for w in pal if y_obs + 3 < (w[1] + w[3]) / 2 < cab[0] - 3])
            # límites de columna: celdas grises del encabezado
            grises = sorted({(round(d["rect"].x0, 1), round(d["rect"].x1, 1)) for d in dibujos
                             if d.get("fill") and abs(d["fill"][0] - 0.8) < 0.05 and abs(d["rect"].y0 - cab[1][0][1]) < 6
                             and d["rect"].height > 5})
            hx = {w[4]: (w[0] + w[2]) / 2 for w in cab[1]}
            bordes = {}
            for nombre in ("ITEM", "ARANCEL", "DESCRIPCION", "UNID.", "ORIGEN", "CANT.", "FOB"):
                c = next(((a, b) for a, b in grises if a - 1 <= hx.get(nombre, -99) <= b + 1), None)
                bordes[nombre] = c
            if any(v is None for v in bordes.values()):  # respaldo: puntos medios entre títulos
                orden = ["ITEM", "ARANCEL", "DESCRIPCION", "UNID.", "ORIGEN", "CANT.", "FOB"]
                cx = [hx[n] for n in orden]
                lim = [cx[0] - 12] + [(cx[i] + cx[i + 1]) / 2 for i in range(len(cx) - 1)] + [cx[-1] + 15]
                lim[-2] = cx[-1] - 18  # CANT. termina antes del FOB
                bordes = {n: (lim[i], lim[i + 1]) for i, n in enumerate(orden)}
                problemas.append("columnas por respaldo")
        elif en_tabla:
            y_ini = 25
        else:
            continue
        if not en_tabla:
            continue
        tot = [(w[1] + w[3]) / 2 for w in pal if w[4] == "Totales" and (w[1] + w[3]) / 2 > y_ini]
        y_fin = min(tot) - 3 if tot else alto - 20
        x_item = bordes["ITEM"]
        seps = sorted({round(d["rect"].y0, 1) for d in dibujos
                       if d["rect"].height < 1.6 and abs(d["rect"].x0 - x_item[0]) < 3 and abs(d["rect"].x1 - x_item[1]) < 3
                       and y_ini + 1 < d["rect"].y0 < y_fin + 2})
        cortes = [y_ini] + [s for s in seps if s > y_ini + 2] + [y_fin + 3]
        x_min, x_max = bordes["ITEM"][0] - 4, bordes["FOB"][0]
        zona = [w for w in pal if y_ini < (w[1] + w[3]) / 2 < y_fin and x_min <= (w[0] + w[2]) / 2 < x_max]
        for k in range(len(cortes) - 1):
            ws = [w for w in zona if cortes[k] <= (w[1] + w[3]) / 2 < cortes[k + 1]]
            if not ws:
                continue
            cel = {}
            for n in ("ITEM", "ARANCEL", "DESCRIPCION", "UNID.", "ORIGEN", "CANT."):
                a, b = bordes[n]
                cel[n] = texto([w for w in ws if a - 3 <= (w[0] + w[2]) / 2 < b + (0 if n != "CANT." else -1)])
            borde_pag = (k == 0 and not cab) or (k == len(cortes) - 2 and not tot)
            if (re.fullmatch(r"\d+", cel["ITEM"]) and items and borde_pag
                    and int(cel["ITEM"]) not in (int(items[-1]["ITEM"]), int(items[-1]["ITEM"]) + 1)):
                continue  # copia recortada de una fila cortada entre páginas (el número sale mutilado)
            if re.fullmatch(r"\d+", cel["ITEM"]) and not (items and items[-1]["ITEM"] == cel["ITEM"]):
                items.append(cel)
            elif items and items[-1]["ITEM"] == cel["ITEM"]:
                # fila cortada entre páginas: SIGA repite la línea del ítem (recortada) en ambas páginas
                ant = items[-1]
                for n in ("DESCRIPCION", "ORIGEN"):
                    if cel[n]:
                        ant[n] = (ant[n] + " " + cel[n]).strip()
                if re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", cel["ARANCEL"]):
                    ant["ARANCEL"] = cel["ARANCEL"]
                if numero(cel["CANT."]) is not None and "." in cel["CANT."]:
                    ant["CANT."] = cel["CANT."]
                ant["UNID."] = ant["UNID."] or cel["UNID."]
            elif items:  # fila partida entre páginas
                for n in ("DESCRIPCION", "ORIGEN", "UNID."):
                    if cel[n]:
                        items[-1][n] = (items[-1][n] + " " + cel[n]).strip()
                if cel["ARANCEL"] and not items[-1]["ARANCEL"]:
                    items[-1]["ARANCEL"] = cel["ARANCEL"]
                if cel["CANT."] and not items[-1]["CANT."]:
                    items[-1]["CANT."] = cel["CANT."]
        if tot:
            en_tabla = False
            tras_items.append((np_, [w for w in pal if (w[1] + w[3]) / 2 > min(tot) + 3]))
            for np2 in range(np_ + 1, len(paginas)):
                tras_items.append((np2, paginas[np2][0]))
            break

    # ---- peso, vehículos, furgones, disposiciones ----
    flujo = [(np_, y, ws) for np_, pal in tras_items for y, ws in lineas(pal)]
    seccion, peso_b, peso_n = None, None, None
    veh_f, fur_f, disp = [], [], []
    veh_cab = fur_cab = None
    for np_, y, ws in flujo:
        t = texto(ws)
        tn = norm(t)
        if "rd$" in tn and "us$" in tn or tn.startswith("total fob"):
            break
        if tn.startswith("peso bruto"):
            peso_b = numero(ws[-1][4]); continue
        if tn.startswith("peso neto"):
            peso_n = numero(ws[-1][4]); continue
        if tn == "vehiculos":
            seccion = "veh"; continue
        if tn == "furgones":
            seccion = "fur"; continue
        if tn == "disposiciones":
            seccion = "disp"; continue
        if seccion == "veh" and veh_cab is None and "chasis" in tn and "marca" in tn:
            t_ = [(w[0], w[2]) for w in ws]  # No. | Chasis | Ano | Marca | Color | Vehiculo CC | Tipo
            veh_cab = t_[:5] + [(t_[5][0], t_[6][1]), t_[7]] if len(t_) >= 8 else None
            continue
        if seccion == "fur" and fur_cab is None and "furgon" in tn and "sello" in tn:
            t_ = [(w[0], w[2]) for w in ws]  # No. | Furgon | Sello 1 | Sello 2 | Tipo de Empaque
            fur_cab = [t_[0], t_[1], (t_[2][0], t_[3][1]), (t_[4][0], t_[5][1]), (t_[6][0], t_[-1][1])] if len(t_) >= 9 else None
            continue
        if seccion == "veh" and veh_cab:
            veh_f.append((y, ws))
        elif seccion == "fur" and fur_cab:
            fur_f.append((y, ws))
        elif seccion == "disp" and not tn.startswith("no. descripcion"):
            disp.append(t)
    vehiculos = tabla_simple(veh_f, veh_cab, ["No", "Chasis", "Anio", "Marca", "Color", "CC", "Tipo"]) if veh_cab else []
    furgones = tabla_simple(fur_f, fur_cab, ["No", "Contenedor", "Sello_1", "Sello_2", "Tipo_Empaque"]) if fur_cab else []

    nums = [int(i["ITEM"]) for i in items]
    if not items:
        problemas.append("sin ítems")
    elif nums != list(range(1, len(nums) + 1)):
        problemas.append(f"ítems no consecutivos ({len(nums)} leídos, último {nums[-1]})")
    for i in items:
        if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", i["ARANCEL"]):
            problemas.append(f"ítem {i['ITEM']}: arancel '{i['ARANCEL']}'")
        if numero(i["CANT."]) is None:
            problemas.append(f"ítem {i['ITEM']}: cantidad '{i['CANT.']}'")
    return dict(enc=enc, detalle=det, obs=obs, items=items, vehiculos=vehiculos, furgones=furgones,
                disposiciones=" | ".join(disp), peso_b=peso_b, peso_n=peso_n, paginas=len(paginas), problemas=problemas)


# ============================== armado del Excel ==============================
def administracion(cod, texto_adm):
    if cod in ADMINISTRACIONES:
        return ADMINISTRACIONES[cod]
    t = norm(texto_adm).upper()
    for clave, nombre in (("AEROPUERTO", "Aeropuerto"), ("CAUCEDO", "Caucedo"), ("HAINA", "Haina Oriental"),
                          ("SANTO DOMINGO", "Santo Domingo")):
        if clave in t:
            return nombre
    return texto_adm or cod


def empresa_de(carpeta):
    n = norm(carpeta)
    if n.startswith("antillana"):
        return "Antillana Comercial"
    if n.startswith("tecnicaribe"):
        return "Tecnicaribe"
    if n.startswith("motor"):
        return "Motor Ibérico"
    return carpeta.split(",")[0].strip()


def tamano(tipo):
    t = (tipo or "").upper()
    m = re.search(r"(?<!\d)(20|40|45)\s*(?:'|PIES|FT|PIE|HC|\b)", t)
    return int(m[1]) if m else None


def tipo_carga(cod_adm, adm_txt, n_fur, n_veh, despacho=""):
    if cod_adm in ("20050", "20040") or "AEROPUERTO" in (adm_txt or "").upper():
        return "Aérea"
    if n_fur:
        return "En contenedor"
    if "LOGISTICO" in norm(despacho).upper():
        return "Centro logístico"
    if n_veh:
        return "Ro-Ro"
    return "Carga suelta"


UNIDADES = ["Unidades", "Kilogramos", "Litros", "Metros Cuadrados", "Metros Cubicos", "Metros", "Toneladas Metricas",
            "Pares", "Docenas", "Galones"]


def subsecuencia(corto, largo):
    it = iter(largo.lower())
    return all(ch in it for ch in corto.lower() if ch != " ")


def limpiar_unidad(u):
    if u in UNIDADES or not u:
        return u
    return next((x for x in UNIDADES if u.startswith(x)), None) or next((x for x in UNIDADES if subsecuencia(u, x)), u)


def limpiador_paises(valores):
    """Las filas cortadas entre páginas dejan restos ("ESTADOS OS UNIDOS", "TAIWÁN Á", "CHINA CHINA"). Se corrige
    al país frecuente (5 o más ítems, sin palabras repetidas de otro país) cuyas palabras estén todas en el texto
    leído; si no hay ninguno (p. ej. "PALAOS", que sale una vez), se deja igual."""
    cuenta = collections.Counter(valores)
    canon = []
    for v, n in cuenta.most_common():  # los más frecuentes primero: "ESTADOS UNIDOS" antes que sus restos
        if n >= 5 and v and not any(set(c.split()) <= set(v.split()) for c in canon):
            canon.append(v)
    canon.sort(key=lambda v: -len(v.split()))
    cache = {}

    def limpiar(v):
        if v in cache:
            return cache[v]
        r = v
        if v and v not in canon:
            toks = set(v.split())
            junto = v.replace(" ", "")
            r = next((c for c in canon if set(c.split()) <= toks), None) or \
                next((c for c in canon if c.replace(" ", "") in junto), v)
        cache[v] = r
        return r
    return limpiar


def procesar(raiz):
    pdfs = sorted(p for p in raiz.glob("*/**/*.pdf") if ", Importaciones" in p.relative_to(raiz).parts[0])
    print(f"{len(pdfs)} PDF encontrados en {raiz}")
    por_decl, problemas = {}, []
    t0 = time.time()
    for k, ruta in enumerate(pdfs, 1):
        rel = ruta.relative_to(raiz)
        try:
            r = leer_pdf(ruta)
        except Exception as e:  # PDF dañado o con otro formato
            problemas.append((str(rel), "", f"no se pudo leer: {e}"))
            continue
        m = re.match(r"(\S+)\s*\((\d+\.\d+)\)", r["enc"].get("Declaracion", ""))
        if not m:
            problemas.append((str(rel), "", "sin número de declaración"))
            continue
        decl, ver = m[1], m[2]
        r["archivo"], r["version"], r["empresa"] = str(rel), ver, empresa_de(rel.parts[0])
        for p in r["problemas"]:
            problemas.append((str(rel), decl, p))
        previo = por_decl.get(decl)
        if previo is None or float(ver) > float(previo["version"]):
            if previo is not None:
                problemas.append((previo["archivo"], decl, f"versión {previo['version']} reemplazada por {ver}"))
            por_decl[decl] = r
        else:
            problemas.append((str(rel), decl, f"versión {ver} descartada (ya hay {previo['version']})"))
        if k % 250 == 0 or k == len(pdfs):
            print(f"  {k}/{len(pdfs)}  ({time.time() - t0:.0f} s)")
    return por_decl, problemas


def escribir(por_decl, problemas, destino):
    hojas = {
        "Declaraciones": ["Declaracion", "Version", "Empresa", "Anio", "Mes", "Fecha_Declaracion", "Fecha_Llegada",
                          "Cod_Adm", "Administracion", "Tipo_Carga", "Despacho", "Regimen", "Estado", "BL",
                          "Manifiesto", "Importador", "RNC_Importador", "Consignatario", "Agencia", "Endosado",
                          "Declaracion_Madre", "Deposito_Fiscal", "Detalle_Manifiesto", "Observaciones",
                          "Disposiciones", "Peso_Bruto_KG", "Peso_Neto_KG", "Items", "Contenedores", "Vehiculos",
                          "Paginas", "Archivo"],
        "Items": ["Declaracion", "Item", "Partida", "Capitulo", "Partida_4", "Descripcion", "Unidad",
                  "Pais_Origen", "Cantidad"],
        "Contenedores": ["Declaracion", "No", "Contenedor", "Tamano_Pies", "High_Cube", "Tipo_Empaque",
                         "Sello_1", "Sello_2"],
        "Vehiculos": ["Declaracion", "No", "Chasis", "Anio_Vehiculo", "Marca", "Color", "CC", "Tipo"],
        "Control": ["Archivo", "Declaracion", "Observacion"],
    }
    filas = {h: [] for h in hojas}
    pais = limpiador_paises([i["ORIGEN"] for r in por_decl.values() for i in r["items"]])
    for decl in sorted(por_decl, key=lambda d: (fecha(por_decl[d]["enc"].get("Fecha_Declaracion")) or dt.date(1900, 1, 1), d)):
        r = por_decl[decl]
        e = r["enc"]
        fd, fl = fecha(e.get("Fecha_Declaracion")), fecha(e.get("Fecha_Llegada"))
        cod = decl[:5]
        filas["Declaraciones"].append([
            decl, r["version"], r["empresa"], fd.year if fd else None, fd.month if fd else None, fd, fl,
            cod, administracion(cod, e.get("Adm_Texto", "")),
            tipo_carga(cod, e.get("Adm_Texto"), len(r["furgones"]), len(r["vehiculos"]), e.get("Despacho", "")),
            e.get("Despacho", ""), e.get("Regimen", ""), e.get("Estado", ""), e.get("BL", ""), e.get("Manifiesto", ""),
            e.get("Importador", ""), e.get("RNC_Importador", ""), e.get("Consignatario", ""), e.get("Agencia", ""),
            e.get("Endosado", ""), e.get("Declaracion_Madre", ""), e.get("Deposito_Fiscal", ""),
            r["detalle"].replace("\\r\\n", " "), r["obs"], r["disposiciones"], r["peso_b"], r["peso_n"],
            len(r["items"]), len(r["furgones"]), len(r["vehiculos"]), r["paginas"], r["archivo"]])
        for i in r["items"]:
            part = i["ARANCEL"]
            dig = re.sub(r"\D", "", part)
            filas["Items"].append([decl, int(i["ITEM"]), part, dig[:2], dig[:4], i["DESCRIPCION"],
                                   limpiar_unidad(i["UNID."]), pais(i["ORIGEN"]), numero(i["CANT."])])
        for c in r["furgones"]:
            te = c["Tipo_Empaque"]
            m = re.match(r"([A-Z]{4}\s?\d{6,7})\s+(.+)", c["Contenedor"])
            if m and not c["Sello_1"]:  # sello largo pegado al número del contenedor
                c["Contenedor"], c["Sello_1"] = m[1], m[2]
            filas["Contenedores"].append([decl, int(c["No"]), c["Contenedor"], tamano(te),
                                          "Sí" if re.search(r"HIGH\s*CUBE|\bHC\b", te.upper()) else "No", te,
                                          c["Sello_1"], c["Sello_2"]])
        for v in r["vehiculos"]:
            filas["Vehiculos"].append([decl, int(v["No"]), v["Chasis"], v["Anio"], v["Marca"], v["Color"], v["CC"], v["Tipo"]])
    filas["Control"] = [list(p) for p in problemas]

    TEXTO = {"Declaracion", "Version", "BL", "Manifiesto", "RNC_Importador", "Partida", "Capitulo", "Partida_4",
             "Contenedor", "Sello_1", "Sello_2", "Chasis", "Cod_Adm", "Declaracion_Madre", "CC", "Anio_Vehiculo"}
    wb = Workbook()
    wb.remove(wb.active)
    cab_f, cab_r = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0C447C")
    for h, cols in hojas.items():
        ws = wb.create_sheet(h)
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = cab_f, cab_r
        for fila in filas[h]:
            ws.append([("" if v is None else str(v)) if cols[j] in TEXTO else v for j, v in enumerate(fila)])
        for j, col in enumerate(cols, 1):
            letra = get_column_letter(j)
            if col.startswith("Fecha"):
                for c in ws[letra][1:]:
                    c.number_format = "yyyy-mm-dd"
            if col in TEXTO:
                for c in ws[letra][1:]:
                    c.number_format = "@"
            ancho = max([len(col)] + [len(str(f[j - 1] or "")) for f in filas[h][:300]])
            ws.column_dimensions[letra].width = min(max(ancho + 2, 9), 60)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    wb.save(destino)
    return {h: len(f) for h, f in filas.items()}


def main():
    raiz = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    por_decl, problemas = procesar(raiz)
    destino = raiz / SALIDA
    n = escribir(por_decl, problemas, destino)
    print(f"\nListo: {destino}")
    for h, c in n.items():
        print(f"  {h}: {c:,} filas")


if __name__ == "__main__":
    main()
