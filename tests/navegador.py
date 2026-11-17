"""Opcional: levanta la app en un servidor Streamlit REAL (con el Sheet falso), la
recorre en Chrome sin ventana y guarda capturas (escritorio y celular).

    pip install playwright        # y tener Google Chrome/Chromium instalado
    python tests/navegador.py [carpeta_de_capturas]

Sale con código 1 si alguna pantalla muestra una excepción. No toca el Sheet real ni
los secrets reales: corre en una carpeta temporal con PINs de prueba.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SALIDA = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="capturas_")).resolve()
PUERTO = int(os.environ.get("PUERTO", "8765"))
CHROME = os.environ.get("CHROME") or next((p for p in ("/usr/bin/google-chrome", "/usr/bin/chromium",
                                                       "/usr/bin/chromium-browser") if Path(p).exists()), None)
SECRETS = '''SHEET_ID = "falso"
TALLER_SHEET_ID = "falso-taller"
[gcp_service_account]
type = "service_account"
[pins.1111]
nombre = "Admin Prueba"
rol = "admin"
'''


async def _errores(pg) -> int:
    """Excepciones en pantalla. Con showErrorDetails = "none" (config de producción)
    Streamlit no muestra stException sino un aviso genérico: se cuentan los dos."""
    return (await pg.locator('[data-testid="stException"]').count()
            + await pg.get_by_text("This app has encountered an error").count())


async def recorrer(pw, ancho, alto, nombre) -> int:
    nav = await pw.chromium.launch(**({"executable_path": CHROME} if CHROME else {}), args=["--no-sandbox"])
    pg = await (await nav.new_context(viewport={"width": ancho, "height": alto})).new_page()
    await pg.goto(f"http://localhost:{PUERTO}", wait_until="networkidle")
    await pg.fill('input[type="password"]', "1111")
    await pg.keyboard.press("Enter")
    await pg.wait_for_selector("text=Estatus de Cargas", timeout=30000)
    await pg.screenshot(path=str(SALIDA / f"{nombre}_1_dashboard.png"))
    await pg.get_by_text("Puerto/Aeropuerto · Estatus").first.click()
    await pg.wait_for_selector("text=Confirmar declaración", timeout=30000)
    await pg.wait_for_timeout(800)
    await pg.screenshot(path=str(SALIDA / f"{nombre}_2_puerto.png"), full_page=True)
    antes = await pg.get_by_role("button", name="Confirmar declaración").count()
    await pg.get_by_role("button", name="Confirmar declaración").first.click()
    await pg.wait_for_function(
        "n => [...document.querySelectorAll('button')].filter(b => b.innerText.includes('Confirmar declaración')).length == n",
        arg=antes - 1, timeout=30000)
    fallos = 0
    for seccion in ("Estatus de Pago", "Histórico", "Analítica", "Editar", "Herramientas"):
        await pg.get_by_text(seccion, exact=True).first.click()
        await pg.wait_for_timeout(2500)
        n = await _errores(pg)
        fallos += n
        print(f"{nombre:10} {seccion:16} excepciones en pantalla: {n}")
        await pg.screenshot(path=str(SALIDA / f"{nombre}_3_{seccion.replace(' ', '_')}.png"),
                            full_page=seccion == "Analítica")
    # Repuesto y Taller (otro Sheet): se entra por Estatus de Pago → origen.
    await pg.get_by_text("Estatus de Pago", exact=True).first.click()
    await pg.wait_for_timeout(2000)
    await pg.get_by_label("Origen").first.click()
    await pg.get_by_role("option", name="Repuesto y Taller").first.click()
    await pg.wait_for_selector("text=Repuesto y Taller", timeout=30000)
    await pg.wait_for_timeout(3000)
    n = await _errores(pg)
    fallos += n
    print(f"{nombre:10} {'Repuesto y Taller':16} excepciones en pantalla: {n}")
    await pg.screenshot(path=str(SALIDA / f"{nombre}_4_taller.png"), full_page=True)
    await nav.close()
    return fallos


async def main() -> int:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        return await recorrer(pw, 1600, 1000, "escritorio") + await recorrer(pw, 390, 844, "celular")


if __name__ == "__main__":
    SALIDA.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="transito_srv_"))
    (tmp / ".streamlit").mkdir()
    shutil.copy(RAIZ / ".streamlit" / "config.toml", tmp / ".streamlit" / "config.toml")
    (tmp / ".streamlit" / "secrets.toml").write_text(SECRETS, encoding="utf-8")
    for recurso in ("assets", "logo.png"):
        if (RAIZ / recurso).exists():
            os.symlink(RAIZ / recurso, tmp / recurso)
    servidor = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(RAIZ / "tests" / "harness_app.py"), "--server.headless", "true",
         "--server.port", str(PUERTO), "--browser.gatherUsageStats", "false"],
        cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                if urllib.request.urlopen(f"http://localhost:{PUERTO}/_stcore/health", timeout=2).status == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        fallos = asyncio.run(main())
    finally:
        servidor.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"Capturas en {SALIDA}")
    sys.exit(1 if fallos else 0)
