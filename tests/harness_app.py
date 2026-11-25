"""Guion que AppTest ejecuta: la app REAL (app.main) sobre el Sheet falso."""
import sys
import time
import types
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
for p in (str(RAIZ), str(RAIZ / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import sheets_io  # noqa: E402
import estado_prueba  # noqa: E402

if estado_prueba.LIBRO is None:
    # Arranque a mano (streamlit run tests/harness_app.py): se crea el Sheet de prueba.
    import os  # noqa: E402
    import datos  # noqa: E402
    # TRANSITO_DATOS=analitica: libro con ~15 meses de historia (capturas de Analítica).
    estado_prueba.LIBRO = (datos.libro_analitica() if os.environ.get("TRANSITO_DATOS") == "analitica"
                           else datos.libro_principal())
    estado_prueba.TALLER = datos.libro_taller()
    estado_prueba.ORDENES = datos.libro_ordenes()

sheets_io.get_spreadsheet = lambda: estado_prueba.LIBRO
sheets_io.get_spreadsheet_taller = lambda: estado_prueba.TALLER
import ordenes  # noqa: E402
ordenes.get_spreadsheet_ordenes = lambda: estado_prueba.ORDENES
# Sin esperas reales (reintentos, freno del PIN). OJO: se sustituye el nombre `time` de
# cada módulo, NO time.sleep global: AppTest lo usa para sondear y quedaría en bucle.
_sin_espera = types.SimpleNamespace(sleep=lambda *_: None, time=time.time)
sheets_io.time = _sin_espera

# app.py se ejecuta como guion principal en cada corrida, igual que en producción
# (st.set_page_config y main() incluidos), no como módulo importado una sola vez.
_codigo = (RAIZ / "app.py").read_text(encoding="utf-8")
exec(compile(_codigo, str(RAIZ / "app.py"), "exec"), {"__name__": "__main__", "__file__": str(RAIZ / "app.py")})
