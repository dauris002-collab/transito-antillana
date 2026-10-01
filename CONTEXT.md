# CONTEXT.md — Plataforma de tránsito y pagos (Antillana Comercial)

Generado el 2026-10-01 leyendo el repo en el commit `93b2c6f`.
Fuente de cada dato: **[L]** leído en el código · **[D]** lo dijo Dauris en chats previos · **[?]** pendiente de confirmar.
Si algo de aquí contradice el código, manda el código: corrige este archivo.
Repo PÚBLICO: aquí no van credenciales, PINs, correos de cuentas de servicio ni URLs de la app.

## Qué es

App Streamlit (v4.3 [L]) sobre un Google Sheet vía gspread. Secciones: Dashboard (tránsito), Analítica (solo lectura), Histórico, Estatus de Pago y, solo para admin, Agregar, Editar, Carga masiva y Herramientas.
Despliegue: Streamlit Community Cloud, gratis, rama `main` [D]. No hay README.

## Capas (los imports solo van hacia abajo) [L]

```
sheets_io.py     constantes de columnas/pestañas, acceso a Sheets, caché, reintentos, bitácora. No importa módulos propios.
  ← logica.py        reglas de negocio puras, sin Streamlit.
  ← ui_componentes.py  HTML/CSS, dashboard, ficha del embarque.
  ← vistas_admin.py · pagos.py · analitica.py
  ← app.py           login, sesión, navegación.
```

Nombres de columnas y pestañas: por diseño viven solo como constantes en `sheets_io.py` [L]. Se cambian ahí, no como texto suelto en otro archivo.
Otros archivos: `test_baseline.py`, `requirements.txt` (versiones fijadas), `.streamlit/config.toml` (tema claro, sin tracebacks).

## Google Sheet

Pestañas que usa la app [L]:
- 9 categorías de embarques activos: Montacargas, Construcción y Minería, Agrícola, Elevadores, Generadores, General, Aéreos, Carga Suelta, Consolidados.
- `Recibido (Mes)` (histórico), `Pagos` y `Log` (bitácora). Pagos y Log se crean solas si no existen.
- Aéreo vs. marítimo es un dato de la FILA (`Via_Transporte`), no de la pestaña; vacío se trata como marítimo.
- Existe una pestaña vacía "En Puerto" en el Sheet que el código no usa [D]. [?] ¿se borra?

Columnas: la fuente de verdad son las constantes de `sheets_io.py` (`REQUIRED_COLUMNS`, `ALL_COLUMNS`, `OPCIONALES_CATEGORIA`, `COLUMNAS_RECIBIDO`, `COLUMNAS_PAGOS`, `COLUMNAS_LOG`, `CONCEPTOS_PAGO`, `MONEDA_CONCEPTO`). Los encabezados se comparan sin distinguir acentos, mayúsculas, espacios, guion bajo ni barra.
- Obligatorias en cada categoría: `BL`, `Descripcion`, `Cantidad`, `Pais_Origen`, `Llegada a Puerto (ETA)`.
- Opcionales por categoría (la app solo crea la columna si el dato trae valor): `Modelo_Serie`, `Fecha_Salida`, `OC`, `EE`, `CLIENTE / STOCK`.
- Del flujo: `¿Llegó? SI/NO`, `Fecha_Declaracion`, `Via_Transporte`, `Fecha_Actualizacion`, `Actualizado_Por`. `Dias en puerto` existe en el Sheet pero la app lo calcula en vivo y no lo escribe.
- Pagos: una fila por BL; 13 conceptos con moneda fija por columna; además `Empresa`, `Estado_Pago`, `Prioridad`, `Fecha_SinMora`, `Fecha_PagoRealizado`, `PagoRealizado_USD/DOP`, `Llegada`.
- [?] Fila 1 real de cada pestaña: no se puede leer desde el código. Pegarla aquí.

## Lectura y escritura [L]

- Lectura: UNA llamada `values_batch_get` trae todas las pestañas (`A1:AZ20000`), con caché de 45 s (`CACHE_TTL`). Un error de lectura no se cachea. Tope de 20.000 filas por pestaña, con aviso.
- Reintentos ante 429/500/502/503: hasta 5 intentos con esperas de 2, 4, 8 y 16 s.
- Escritura: por número de fila del Sheet, verificado contra el BL (`_localizar_fila`). Si el BL está repetido y la pantalla quedó desactualizada, se niega a escribir. Después de escribir se invalidan las cachés. Encabezados: caché de 120 s.
- Estado en memoria del servidor (`st.cache_resource`): cliente gspread, índice de hojas y sesiones activas. Se pierde al reiniciar o redesplegar.
- [?] Cuotas de la API de Sheets: no están en el código. Verificar los valores vigentes en la documentación de Google.

## Acceso [L]

- Login por PIN de 4 dígitos. Roles `admin` (8 secciones) y `viewer` (Dashboard, Analítica, Histórico, Estatus de Pago).
- Secrets esperados (solo nombres): `gcp_service_account`, `SHEET_ID`, tabla `pins` (por PIN: nombre y rol; respaldo: `ADMIN_PIN` / `VIEWER_PIN`), `sla` (opcional: `llegada_a_puerto`, `recepcion_y_declaracion`, `retraso`).
- Sesión: token en la URL cuyo contenido vive en el servidor, atado a una huella del navegador. Vida 120 min (admin) / 720 min (viewer), con ventana deslizante.
- Sin bloqueo por intentos fallidos, solo 1 s de espera por intento (ver DECISIONS.md).

## Reglas de negocio que más se tocan [L]

- Etapas activas: 2 (Llegada a puerto; Recepción y declaración). "Recibido en almacén" no es etapa: es la acción de archivar a `Recibido (Mes)`.
- `¿Llegó?` vacío ≠ `NO`. Con `SI`, el ETA vale como fecha real de llegada.
- Pagado = `Estado_Pago` dice "Pagado" O hay fecha en `Fecha_PagoRealizado` (`logica.py:719-720`).
- KPIs y totales de Pagos usan solo expedientes con montos (`TieneMontos`); los demás se cuentan aparte en un aviso (`pagos.py:534-556`).
- `Prioridad` 1-4; vacío queda debajo de todos, no equivale a 4.

## Pruebas (ejecutado el 2026-10-01 con las versiones fijadas)

- Los 8 `.py` compilan (`py_compile`).
- `test_baseline.py` corre: vuelca 21 grupos de resultados de funciones puras (fechas, pagos y más) con datos sintéticos a un JSON. No tiene aserciones ni baseline guardado en el repo: sirve para comparar el JSON de antes y después de un cambio (`python3 test_baseline.py salida.json`).
- Sin pruebas de: escrituras a Google Sheets, UI, login, Apps Script, despliegue.

## Apps Script [?]

No hay ningún `.gs` en el repo. El código solo lo menciona: un respaldo automático "cada 10 minutos" que corre dentro del Sheet (`vistas_admin.py:958`). El último diseño del que hay registro (ago 2026 [D]) era por hora (7-19 h). [?] Confirmar qué está instalado y subir el código al repo (o versionarlo con `clasp`).

## Comentarios del código que NO hay que creer (desactualizados) [L]

- `sheets_io.py`, `COL_EMPRESA`: dice que la sincronización marca "Antillana Comercial". Falso: `sincronizar_pagos_con_transito` deja Empresa en blanco a propósito.
- `sheets_io.py`, `COL_ESTADO_PAGO`: dice que Pagado no se deriva de la fecha de pago. Falso: ver la regla de arriba.
- `_cargar_todo_remoto`: dice "5 pestañas de categoría"; son 9.
- Docstring inicial de `sheets_io.py`: redacción confusa sobre quién importa a quién. La regla real es la del diagrama de capas.

## Riesgos conocidos (no son bugs) [D]

Google Sheets como base de datos (sin escrituras concurrentes reales); Community Cloud puede reaprovisionar la app; todo vive sobre cuentas personales de GitHub y Google Cloud; repo público.

## Pendiente de confirmar por Dauris

1. Fila 1 real de cada pestaña del Sheet.
2. Qué Apps Script está instalado y con qué frecuencia respalda.
3. Cuotas vigentes de la API de Sheets.
4. Pestaña "En Puerto": ¿se borra?
