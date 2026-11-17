#!/usr/bin/env bash
# Verificación completa de la plataforma, sin red y sin tocar el Sheet real.
#   bash tests/verificar.sh            (base = origin/main)
#   BASE_REF=<commit> bash tests/verificar.sh
# Sale con código distinto de 0 si algo falla.
set -u
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
BASE_REF="${BASE_REF:-origin/main}"
export BASE_REF
fallos=0
paso() { echo; echo "== $1"; }

paso "1/5 Compilar todos los módulos"
$PY -m py_compile ./*.py tests/*.py && echo OK || fallos=$((fallos+1))

paso "2/5 Punta a punta (app completa sobre un Sheet falso)"
$PY -m pytest tests/test_e2e.py -q -p no:cacheprovider || fallos=$((fallos+1))

paso "3/5 Propiedades (miles de entradas generadas)"
$PY -m pytest tests/test_propiedades.py -q -p no:cacheprovider || fallos=$((fallos+1))

paso "4/5 Diferencial contra $BASE_REF (mismas entradas, versión base vs. esta)"
$PY -m pytest tests/test_diferencial.py -q -p no:cacheprovider -rs || fallos=$((fallos+1))

paso "5/5 Las pruebas de $BASE_REF contra ESTE código (un cambio no puede aflojar sus propios controles)"
tmp="$(mktemp -d)"
if git archive "$BASE_REF" -- tests 2>/dev/null | tar -x -C "$tmp" 2>/dev/null && [ -f "$tmp/tests/test_e2e.py" ]; then
  cp ./*.py requirements.txt "$tmp"/ && cp -r assets "$tmp"/ 2>/dev/null
  # El diferencial se excluye: aquí no hay historial de git (ya corrió en el paso 4).
  (cd "$tmp" && $PY -m pytest tests -q -p no:cacheprovider --deselect tests/test_diferencial.py) || fallos=$((fallos+1))
else
  echo "(la versión base todavía no trae pruebas: se omite)"
fi
rm -rf "$tmp"

echo
if [ "$fallos" -eq 0 ]; then echo "VERIFICACIÓN OK"; else echo "VERIFICACIÓN CON $fallos BLOQUE(S) FALLIDO(S)"; fi
exit "$fallos"
