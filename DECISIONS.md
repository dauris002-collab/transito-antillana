# DECISIONS.md — Decisiones de diseño ya tomadas

Una decisión de esta lista NO es un bug. Antes de proponer cambiar algo de aquí, pregunta a Dauris.
Fuente: **[PE]** pedido expreso de Dauris (lo dice un comentario del código) · **[CHAT]** confirmado por Dauris en un chat · **[CÓDIGO]** solo consta en un comentario del código, sin confirmación verificada.
"Reconsiderar si: [?]" = condición todavía sin definir. Dauris debe llenarla.
Borrador generado el 2026-10-01.

## 1. Alojamiento y base de datos
- **Decisión:** Streamlit Community Cloud + Google Sheets, costo cero. [CHAT]
- **Por qué:** sin presupuesto para el piloto; la PC de oficina no permite instalar Python (sin permisos de admin); el equipo llena datos en una hoja tipo Excel.
- **Descartado:** PC de oficina como servidor. Un VPS (Hetzner + SQLite) está evaluado desde sep 2026, SIN decidir.
- **Reconsiderar si:** [?]

## 2. Pagos como pestaña dentro de la app de tránsito
- **Decisión:** módulo `pagos.py` + pestaña `Pagos` del mismo Sheet, mismo login y mismo despliegue. El BL es la llave común. [CHAT, CÓDIGO]
- **Por qué:** reusa hosting, login y Sheet; con hoja y módulo propios, un error en pagos no puede tocar tránsito.
- **Descartado:** SharePoint + Power Apps + Power Automate (se llegó a armar la lista; no hay licencia de Power BI y la lógica de cálculo no se podía ejecutar ahí). Hoja `Cargos` con una fila por concepto (diseñada el 6 sep, reemplazada).
- **Reconsiderar si:** [?]

## 3. Tránsito con 2 etapas; el dinero fuera de tránsito
- **Decisión:** etapas "Llegada a puerto" y "Recepción y declaración". Las etapas de pago salieron de tránsito. [CHAT]
- **Por qué:** Finanzas definió otro proceso de pago (por prioridades); tránsito se centra en qué está en tránsito, en puerto, si se declaró y cuántos días lleva.
- **Descartado:** el flujo de 5 etapas con solicitud de pago y pago realizado (existió en la v3).
- **Reconsiderar si:** [?]

## 4. Llegada: casilla `¿Llegó? SI/NO`, sin fecha propia
- **Decisión:** con `SI`, el ETA vale como fecha real de llegada. Vacío ≠ `NO`: vacío = nadie ha revisado; `NO` = revisé y no llegó. [CHAT, CÓDIGO]
- **Por qué:** nadie teclea la misma fecha dos veces, y se distingue un embarque atrasado de uno desatendido.
- **Descartado:** columna `Estatus_Llegada` y una columna de fecha de llegada aparte.
- **Costo aceptado:** si el ETA se mueve después de confirmar, la llegada se mueve con él. Por eso existen las decisiones 5 y 6.
- **Reconsiderar si:** [?]

## 5. Validación del ETA de un embarque confirmado
- **Decisión:** no puede quedar en el futuro, ilegible ni después de la declaración; se valida al confirmar y en cada edición. [CHAT, CÓDIGO]
- **Por qué:** antes, mover el ETA a futuro borraba la confirmación en silencio y el embarque retrocedía de etapa sin que nadie se enterara.
- **Reconsiderar si:** [?]

## 6. Archivar = "Recibido en almacén"
- **Decisión:** archivar pide la fecha REAL de entrada a almacén (por defecto hoy) y congela el ETA como `Fecha_Llegada_Puerto` en el histórico. El mes del histórico se decide por fecha de almacén (`BASE_FECHA_RECIBIDO = "almacen"`). [PE, CÓDIGO]
- **Por qué:** una fecha de hoy grabada en silencio falseaba el ciclo puerto→almacén y movía el embarque de mes; congelar el ETA evita que un ETA movido meses después cambie ciclos ya medidos.
- **Descartado:** fecha de hoy automática; mes por llegada a puerto.
- **Nota:** `Fecha_Almacen` tiene respaldo en `Fecha_Recibido` porque 29 de 47 filas del histórico se archivaron antes de que esa columna existiera.
- **Reconsiderar si:** cambia el criterio de negocio (el código dice que basta cambiar esa constante).

## 7. Sin cálculo de costo por demora en tránsito
- **Decisión:** se eliminó `Costo_Por_Dia` y todo cálculo de costo por demora. [CHAT, CÓDIGO]
- **Por qué:** la columna estaba vacía en las 44 filas; la tarifa real varía por naviera, terminal, volumen y espacio, y un promedio sería indefendible.
- **Descartado:** tarifa fija de RD$2,000 por día y `Costo_Por_Dia` por embarque (existieron en versiones anteriores).
- **Alternativa vigente:** el sobrecosto se observa en Pagos, comparando lo pagado contra la fecha saludable.
- **Reconsiderar si:** [?]

## 8. Pagos: una fila por BL, columna fija por concepto
- **Decisión:** lista cerrada de conceptos (`CONCEPTOS_PAGO`), moneda fija por columna, monto como número puro. Vacío ≠ 0: solo se llenan los conceptos que aplican. [CÓDIGO, CHAT]
- **Por qué:** un expediente no siempre trae los mismos cargos; con moneda fija no hay que interpretar texto libre.
- **Descartado:** una fila por concepto; monto con la moneda pegada al texto.
- **Reconsiderar si:** [?]

## 9. Fecha saludable, pago realizado y sobrecosto
- **Decisión:** `Fecha_SinMora` es solo una fecha fijada por Logística, sin montos. El total se calcula en vivo desde los conceptos. `PagoRealizado_USD/DOP` es el EXTRA pagado de más (a mano; 0 si no hubo), y se suma al total. Estado efectivo = Pagado si `Estado_Pago` dice "Pagado" O hay `Fecha_PagoRealizado`. [CHAT]
- **Por qué:** Logística a veces escribe la fecha directo en el Sheet sin pasar por el botón de la app.
- **Descartado:** congelar montos en SIN MORA (primer diseño).
- **Reconsiderar si:** [?]

## 10. Empresa en Pagos
- **Decisión:** lista cerrada de 3 (`EMPRESAS_PAGO`); la fija Logística a mano. La sincronización con tránsito la deja en blanco a propósito. [CHAT, CÓDIGO]
- **Por qué:** tránsito solo trackea Antillana; quién debe cada expediente lo decide Logística.
- **Nota v5.0:** esa premisa ya no es exacta: tránsito ahora conoce la empresa de cada embarque (decisión 18). La sincronización con Pagos sigue SIN copiarla.
- **Reconsiderar si:** [?]

## 11. Prioridad de pago
- **Decisión:** número 1-4 puesto a mano (formulario de admin o directo en el Sheet). La plataforma lo muestra (borde) y ordena de 1 a 4, con "sin prioridad" debajo. NO hay filtro por prioridad. [PE]
- **Por qué:** el filtro sobraba como control. Vacío ≠ 4: son expedientes a los que nadie ha dado turno.
- **Reconsiderar si:** [?]

## 12. Analítica
- **Decisión:** vive dentro de esta misma app (Streamlit/Plotly), no en Power BI ni Looker Studio. [CHAT] Pagos queda FUERA de Analítica por ahora. Tiempos con mediana, no promedio. [CÓDIGO]
- **Por qué:** hay muy poca data de pagos para que un promedio signifique algo; un embarque trancado tres meses no debe mover el número de toda una categoría.
- **Descartado:** gráficas de % de incumplimiento contra el SLA (Logística no lo usa como criterio real).
- **Reconsiderar si:** haya más data de pagos (umbral [?]).

## 13. Acceso
- **Decisión:** PIN de 4 dígitos por persona, SIN bloqueo por intentos fallidos, con 1 s de espera por intento. Sesión por token en la URL atado a la huella del navegador; 120 min admin, 720 min viewer. [PE]
- **Por qué:** el presidente abre el link desde el celular una vez al día; volver a pedirle el PIN a cada rato es la forma más rápida de que deje de usarla.
- **Descartado:** bloqueo de 15 minutos tras 5 intentos (existió).
- **Reconsiderar si:** [?]

## 14. Versiones fijadas en `requirements.txt`
- **Decisión:** `streamlit`, `gspread`, `google-auth`, `pandas`, `plotly` y `openpyxl` con versión exacta. Se suben a mano, probando. `pandas==2.2.3` aunque se probó con 3.0.2. [CÓDIGO]
- **Por qué:** Streamlit Cloud reconstruye el entorno en cada redespliegue; un cambio incompatible tumbaría la app en producción sin que nadie toque el código.
- **Reconsiderar si:** [?]

## 15. Escritura por número de fila
- **Decisión:** toda escritura localiza la fila por número verificado contra el BL; si el BL está repetido y la pantalla está desactualizada, se niega a escribir. [CHAT, CÓDIGO]
- **Por qué:** los BL se repiten en datos reales (embarques parciales).
- **Descartado:** `ws.find(BL)`, que devuelve la primera coincidencia.

## 16. Decisiones menores
- Navegación con estado propio en vez de `st.tabs`: no ejecuta todas las secciones en cada rerun y permite saltar por código. [CÓDIGO]
- Columnas opcionales por categoría: la app solo las crea cuando el dato trae valor. [CÓDIGO]
- Tema claro forzado y `showErrorDetails = "none"`: los fallos de Google Sheets salen como mensajes en español, no como traceback. [CÓDIGO]
- El respaldo automático corre en Apps Script dentro del Sheet, no en la app. [CÓDIGO]

## 17. Tránsito termina al confirmar la llegada a puerto/aeropuerto
- **Decisión:** un embarque es "en tránsito" mientras no tenga `¿Llegó?` = SI. Al confirmarse sale de Todos y de cada categoría y pasa a la vista Puerto/Aeropuerto, donde se ve su estatus, hasta que se archive al recibirse en almacén. El filtro/KPI rojo "Retrasado" y el orden "Urgencia" se quitaron de la vista; el estado Retrasado se sigue calculando y mostrando en cada fila. [PE, 1 oct 2026]
- **Por qué:** antes las cargas ya llegadas seguían contando como tránsito hasta archivarse.
- **Reconsiderar si:** [?]

## 18. Empresa en tránsito
- **Decisión:** la empresa sale de la categoría en Montacargas y Construcción y Minería (Antillana Comercial), Elevadores y Generadores (Tecnicaribe) y Agrícola (Motor Ibérico). En General, Aéreos, Carga Suelta y Consolidados se llena fila por fila en la columna `Empresa` (Sheet o Excel de carga masiva); Consolidados solo admite Antillana Comercial y Tecnicaribe. Vacía = "Sin empresa". [PE, 1 oct 2026]
- **Por qué:** Logística necesita ver el tránsito por empresa.
- **Costo aceptado:** en las categorías de empresa fija, lo que alguien escriba en la columna `Empresa` se ignora.
- **Reconsiderar si:** [?]

## Limitación, no decisión
- **Repo público:** quedó público por un problema de permisos con repos privados que no se resolvió [CHAT]. No contiene credenciales. Es una limitación, no una elección.
