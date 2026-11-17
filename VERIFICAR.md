# VERIFICAR.md — cómo comprobar un cambio antes de subirlo

Para una persona o para un agente (Claude). Nada de esto toca el Google Sheet real, ni la red, ni los secrets.

## Preparar (una vez)

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r tests/requirements-dev.txt
```

## Verificación completa (≈ 1-2 min)

```
bash tests/verificar.sh                 # compara contra origin/main
BASE_REF=<commit> bash tests/verificar.sh
```

Corre cinco bloques y termina en `VERIFICACIÓN OK` o dice cuántos fallaron:

1. **Compila** todos los módulos.
2. **Punta a punta** (`tests/test_e2e.py`): la app completa sobre un Sheet falso. Login por rol, cada sección, alta → llegada → declaración → almacén → reversa, correcciones, Pagos, Repuesto y Taller, fallos de Google y Sheets con datos sucios.
3. **Propiedades** (`tests/test_propiedades.py`): entradas generadas y reglas que siempre deben cumplirse (p. ej. «reintentar archivar nunca deja el embarque dos veces en el histórico», «una acción solo toca su fila», «sin prioridad siempre va debajo»).
4. **Diferencial** (`tests/test_diferencial.py`): las mismas entradas por la versión base y por la nueva. Toda diferencia tiene que estar en `CAMBIOS_ESPERADOS` con su motivo; si no, es una regresión.
5. **Las pruebas de la rama base contra el código nuevo**: así un cambio no puede aprobarse a sí mismo aflojando sus propias pruebas.

Más datos sucios: `SEMILLAS=100 python -m pytest tests/test_e2e.py -k sucios`.

## Ver la app en el navegador con datos de prueba

```
python tests/navegador.py capturas/      # requiere: pip install playwright + Chrome instalado
```

Levanta un servidor Streamlit real con el Sheet falso (PIN de prueba 1111), lo recorre en escritorio y celular y guarda capturas. Para usarla a mano hay que crear un `.streamlit/secrets.toml` de prueba en una carpeta aparte (el script lo hace solo en una carpeta temporal).

## Reglas de trabajo

- Un cambio no se sube si `tests/verificar.sh` no termina en OK.
- Un fallo corregido lleva una prueba que falla antes del cambio y pasa después.
- Si un cambio altera un resultado a propósito, se declara en `CAMBIOS_ESPERADOS` (y en DECISIONS.md si cambia el comportamiento). La lista se vacía cuando el cambio ya está en `main`.
- Si un agente no logra dejar una prueba en verde en 3 intentos, se detiene y lo reporta con lo que probó; no afloja la prueba para que pase.
- Las pruebas no reemplazan la comprobación con el Sheet real: lo que cambia una ESCRITURA se prueba además con un BL de prueba en producción.

## Lo que estas pruebas NO cubren

El Google Sheet real (permisos, cuotas, formatos de celda, fórmulas, validaciones), Apps Script, los secrets y el despliegue en Streamlit Cloud.
