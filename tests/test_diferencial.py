"""Prueba diferencial: las MISMAS entradas por la versión base (rama principal) y por
la versión en trabajo; se comparan los resultados. Toda diferencia tiene que estar en
CAMBIOS_ESPERADOS con su motivo: lo que no esté ahí es una regresión (o un cambio que
hay que declarar).

Base por defecto: origin/main. Otra:  BASE_REF=<commit o rama> pytest tests/test_diferencial.py
"""
import json
import os
import re
import subprocess
import sys
import tarfile
import io
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
BASE_REF = os.environ.get("BASE_REF", "origin/main")

# (patrón de la ruta del resultado, motivo). Vaciar esta lista cuando el cambio ya esté en la rama base.
CAMBIOS_ESPERADOS = [
    # Vacía: la v5.13 solo cambia la Analítica, que no altera ningún resultado de este volcado.
]


def _diferencias(x, y, ruta=""):
    if isinstance(x, dict) and isinstance(y, dict):
        for k in sorted(set(x) | set(y)):
            yield from _diferencias(x.get(k, "<falta>"), y.get(k, "<falta>"), f"{ruta}/{k}")
    elif isinstance(x, list) and isinstance(y, list) and len(x) == len(y):
        for i, (p, q) in enumerate(zip(x, y)):
            yield from _diferencias(p, q, f"{ruta}[{i}]")
    elif x != y:
        yield ruta, x, y


def _volcar(carpeta: Path, destino: Path):
    r = subprocess.run([sys.executable, str(RAIZ / "tests" / "volcar.py"), str(carpeta), str(destino)],
                       capture_output=True, text=True, cwd=str(carpeta))
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(destino.read_text(encoding="utf-8"))


def test_misma_entrada_mismo_resultado_que_la_base(tmp_path):
    git = subprocess.run(["git", "-C", str(RAIZ), "archive", BASE_REF, "--", "*.py"], capture_output=True)
    if git.returncode != 0:
        pytest.skip(f"no se pudo leer la versión base '{BASE_REF}' (¿sin git o sin esa rama?)")
    base = tmp_path / "base"
    base.mkdir()
    tarfile.open(fileobj=io.BytesIO(git.stdout)).extractall(base, filter="data")

    antes = _volcar(base, tmp_path / "antes.json")
    ahora = _volcar(RAIZ, tmp_path / "ahora.json")
    assert ahora["cargar_todo.error"] is None

    sin_declarar = [(ruta, a, b) for ruta, a, b in _diferencias(antes, ahora)
                    if not any(re.search(p, ruta) for p, _ in CAMBIOS_ESPERADOS)]
    detalle = "\n".join(f"{r}\n    base:  {str(a)[:300]}\n    ahora: {str(b)[:300]}" for r, a, b in sin_declarar[:25])
    assert not sin_declarar, (f"{len(sin_declarar)} resultado(s) cambiaron respecto a {BASE_REF} sin estar "
                              f"declarados en CAMBIOS_ESPERADOS:\n{detalle}")
