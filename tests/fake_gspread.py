"""Google Sheets falso, en memoria, para probar la app sin red ni credenciales.

Imita lo justo de gspread que usa sheets_io.py. Dos detalles que importan:
- Cada celda guarda su valor REAL y, opcionalmente, un texto de PANTALLA distinto
  (formato de número/fecha del Sheet). row_values / col_values / get_all_values /
  values_batch_get devuelven el texto de pantalla, igual que la API real por defecto.
- `fallar_en` permite inyectar errores de API (429, 500...) en la N-ésima escritura.
"""
from __future__ import annotations

import re

import gspread
from gspread.utils import a1_to_rowcol, rowcol_to_a1


class _Resp:
    def __init__(self, code):
        self.status_code = code
        self.text = f'{{"error": {{"code": {code}, "message": "falso", "status": "X"}}}}'

    def json(self):
        return {"error": {"code": self.status_code, "message": "error simulado", "status": "X"}}


def api_error(code):
    return gspread.exceptions.APIError(_Resp(code))


class FakeWorksheet:
    def __init__(self, libro, title, filas=None, cols=None, rows=1000, ident=0):
        self.libro, self.title, self.id = libro, title, ident
        self.spreadsheet = libro
        self.celdas = {}      # (fila, col) -> valor real
        self.pantalla = {}    # (fila, col) -> texto mostrado (si difiere)
        filas = filas or []
        for i, f in enumerate(filas, start=1):
            for j, v in enumerate(f, start=1):
                if v != "" and v is not None:
                    self.celdas[(i, j)] = v
        self.col_count = cols or max((len(f) for f in filas), default=1)
        self.row_count = max(rows, len(filas))

    # ---- utilidades de prueba
    def formato(self, fila, col, texto):
        self.pantalla[(fila, col)] = texto

    def real(self, fila, col):
        return self.celdas.get((fila, col), "")

    def matriz(self, real=False):
        n = max((r for r, _ in self.celdas), default=0)
        m = max((c for _, c in self.celdas), default=0)
        return [[self._ver(r, c, real) for c in range(1, m + 1)] for r in range(1, n + 1)]

    def columna(self, nombre):
        cab = [str(self.real(1, c)) for c in range(1, self.col_count + 1)]
        return cab.index(nombre) + 1

    def valor(self, fila, nombre):
        return self.real(fila, self.columna(nombre))

    def _ver(self, r, c, real=False):
        v = self.celdas.get((r, c), "")
        if real:
            return v
        return self.pantalla.get((r, c), v if isinstance(v, str) else _mostrar(v))

    def _poner(self, r, c, v):
        self.libro._escritura()
        if c > self.col_count or r > self.row_count:
            raise api_error(400)
        self.pantalla.pop((r, c), None)
        if v == "" or v is None:
            self.celdas.pop((r, c), None)
        else:
            self.celdas[(r, c)] = v

    # ---- API gspread
    def row_values(self, fila):
        self.libro._lectura()
        m = max((c for r, c in self.celdas if r == fila), default=0)
        return [self._ver(fila, c) for c in range(1, m + 1)]

    def col_values(self, col):
        self.libro._lectura()
        n = max((r for r, c in self.celdas if c == col), default=0)
        return [self._ver(r, col) for r in range(1, n + 1)]

    def get_all_values(self):
        self.libro._lectura()
        return self.matriz()

    def cell(self, fila, col):
        self.libro._lectura()
        v = self._ver(fila, col)
        return type("Celda", (), {"value": v if v != "" else None})()

    def update(self, range_name=None, values=None, value_input_option=None, **_):
        inicio = range_name.split(":")[0]
        r0, c0 = a1_to_rowcol(inicio)
        self.libro._revisar_fallo()
        for i, fila in enumerate(values):
            for j, v in enumerate(fila):
                self._poner(r0 + i, c0 + j, v)

    def batch_update(self, peticiones, value_input_option=None, **_):
        self.libro._revisar_fallo()
        for p in peticiones:
            r0, c0 = a1_to_rowcol(p["range"].split(":")[0])
            for i, fila in enumerate(p["values"]):
                for j, v in enumerate(fila):
                    self._poner(r0 + i, c0 + j, v)

    def append_row(self, fila, value_input_option=None, **_):
        return self.append_rows([fila])

    def append_rows(self, filas, value_input_option=None, **_):
        self.libro._revisar_fallo()
        n = max((r for r, _ in self.celdas), default=0)
        if n + len(filas) > self.row_count:
            self.row_count = n + len(filas)
        for i, fila in enumerate(filas, start=1):
            for j, v in enumerate(fila, start=1):
                self._poner(n + i, j, v)
        ancho = max((len(f) for f in filas), default=1)
        # Misma forma que la respuesta real de values.append de la API de Sheets.
        return {"spreadsheetId": "falso", "updates": {
            "updatedRange": f"'{self.title}'!{rowcol_to_a1(n + 1, 1)}:{rowcol_to_a1(n + len(filas), ancho)}",
            "updatedRows": len(filas)}}

    def delete_rows(self, fila):
        self.libro._revisar_fallo()
        self.libro._escritura()
        nuevas, pant = {}, {}
        for (r, c), v in self.celdas.items():
            if r == fila:
                continue
            nuevas[(r - 1 if r > fila else r, c)] = v
        for (r, c), v in self.pantalla.items():
            if r == fila:
                continue
            pant[(r - 1 if r > fila else r, c)] = v
        self.celdas, self.pantalla = nuevas, pant

    def add_cols(self, n):
        self.col_count += n


def _mostrar(v):
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


class FakeSpreadsheet:
    def __init__(self, hojas: dict | None = None):
        self.hojas = []
        self.lecturas = self.escrituras = 0
        self.fallar_en = {}     # nº de operación de escritura (1-based) -> código HTTP
        self._ops = 0
        for titulo, filas in (hojas or {}).items():
            self.agregar(titulo, filas)

    def agregar(self, titulo, filas, cols=None):
        ws = FakeWorksheet(self, titulo, filas, cols=cols, ident=len(self.hojas) + 1)
        self.hojas.append(ws)
        return ws

    def _lectura(self):
        self.lecturas += 1

    def _escritura(self):
        self.escrituras += 1

    def _revisar_fallo(self):
        self._ops += 1
        codigo = self.fallar_en.pop(self._ops, None)
        if codigo:
            raise api_error(codigo)

    def hoja(self, titulo):
        return next(h for h in self.hojas if h.title == titulo)

    # ---- API gspread
    def worksheets(self):
        return list(self.hojas)

    def worksheet(self, titulo):
        for h in self.hojas:
            if h.title == titulo:
                return h
        raise gspread.exceptions.WorksheetNotFound(titulo)

    def add_worksheet(self, title, rows=1000, cols=26, index=None):
        ws = FakeWorksheet(self, title, [], cols=cols, rows=rows, ident=len(self.hojas) + 1)
        if index is None:
            self.hojas.append(ws)
        else:
            self.hojas.insert(index, ws)
        return ws

    def _hoja_de_rango(self, rango):
        m = re.match(r"^'(.*)'!", rango)
        titulo = m.group(1).replace("''", "'")
        for h in self.hojas:
            if h.title == titulo:
                return h
        raise api_error(400)

    def values_batch_get(self, rangos, params=None):
        self._lectura()
        return {"valueRanges": [{"range": r, "values": self._hoja_de_rango(r).matriz()} for r in rangos]}

    def values_get(self, rango, params=None):
        self._lectura()
        real = (params or {}).get("valueRenderOption") == "UNFORMATTED_VALUE"
        return {"range": rango, "values": self._hoja_de_rango(rango).matriz(real=real)}

    def batch_update(self, body):
        self._revisar_fallo()
        return {}
