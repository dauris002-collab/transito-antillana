/**
 * Sheet «Seguimiento de Órdenes de Compra» — sello automático de actualización.
 *
 * Qué hace: cuando alguien EDITA A MANO el Sheet (no la app), en CUALQUIER pestaña de responsable (Daniel
 * Mojica, Juan de Dios, Yokairis Martes, David Tejeda… o el «Desglose» heredado) o en «Órdenes», pone FECHA
 * ACTUALIZACIÓN y ACTUALIZADO POR en la fila de esa orden en la pestaña «Órdenes». Con ese sello la plataforma cuenta los «días sin
 * actualizar». La app, cuando escribe ella misma, sella por su cuenta (una escritura por API no dispara
 * onEdit).
 *
 * Instalación (una vez, con la cuenta que es dueña del Sheet):
 *   1. En el Sheet: Extensiones → Apps Script. Pegar este archivo completo. Guardar.
 *   2. Archivo del Sheet → Configuración → Zona horaria: «(GMT-04:00) Santo Domingo». Idioma/región:
 *      República Dominicana (fechas día/mes/año).
 *   3. En Apps Script: Activadores (reloj) → Añadir activador → función `alEditar`, origen «De la hoja de
 *      cálculo», evento «Al editar». Autorizar los permisos. Tiene que ser un activador INSTALABLE (el
 *      simple no puede leer el correo de quien edita).
 *
 * Límites (a propósito): solo reacciona a ediciones de UNA celda (o un pegado pequeño); si se pega o se
 * borra un bloque grande no sella (los pegados masivos se hacen con la carga masiva de la app, que sí
 * sella). No hace nada en las columnas de sello ni en las pestañas Instrucciones y Listas. Una pestaña sin
 * columna «Orden de Compras» (o «Documento compras») no se toma como de responsable y se ignora.
 */

var PEST_ORDENES = 'Órdenes';
var PEST_IGNORAR = ['Instrucciones', 'Listas'];   // pestañas que nunca sellan
var COLS_DOC_LINEAS = ['Orden de Compras', 'Documento compras', 'Doc. compras', 'No. Orden'];
var MAX_CELDAS_SELLADAS = 60;     // un pegado de más celdas no se sella fila por fila

function norm_(t) {
  return String(t == null ? '' : t).toUpperCase()
    .normalize('NFD').replace(/[̀-ͯ]/g, '')
    .replace(/[^A-Z0-9]/g, '');
}

/** {ENCABEZADO NORMALIZADO: nº de columna (1..)} de la fila 1. */
function mapa_(hoja) {
  var fila = hoja.getRange(1, 1, 1, Math.max(hoja.getLastColumn(), 1)).getValues()[0];
  var m = {};
  for (var i = 0; i < fila.length; i++) {
    var k = norm_(fila[i]);
    if (k && !(k in m)) m[k] = i + 1;
  }
  return m;
}

function col_(m, nombres) {
  for (var i = 0; i < nombres.length; i++) {
    var c = m[norm_(nombres[i])];
    if (c) return c;
  }
  return 0;
}

function alEditar(e) {
  if (!e || !e.range) return;
  var hoja = e.range.getSheet();
  var nombre = hoja.getName();
  if (PEST_IGNORAR.indexOf(nombre) >= 0) return;
  var r0 = e.range.getRow();
  var nFilas = e.range.getNumRows();
  if (r0 < 2 || nFilas * e.range.getNumColumns() > MAX_CELDAS_SELLADAS) return;

  var ss = e.source;
  var ordenes = ss.getSheetByName(PEST_ORDENES);
  if (!ordenes) return;
  var mo = mapa_(ordenes);
  var cDocO = col_(mo, ['DOCUMENTO COMPRAS']);
  var cFecha = col_(mo, ['FECHA ACTUALIZACIÓN', 'FECHA ACTUALIZACION']);
  var cPor = col_(mo, ['ACTUALIZADO POR']);
  if (!cDocO || !cFecha) return;

  // Editar solo las columnas de sello no re-sella (evita el bucle de «actualicé el sello»).
  if (nombre === PEST_ORDENES) {
    var c0 = e.range.getColumn(), c1 = c0 + e.range.getNumColumns() - 1;
    var soloSello = true;
    for (var c = c0; c <= c1; c++) if (c !== cFecha && c !== cPor) soloSello = false;
    if (soloSello) return;
  }

  var docs = [];
  if (nombre === PEST_ORDENES) {
    for (var i = 0; i < nFilas; i++) docs.push({ fila: r0 + i });
  } else {
    var md = mapa_(hoja);
    var cDocD = col_(md, COLS_DOC_LINEAS);
    if (!cDocD) return;
    var valores = hoja.getRange(r0, cDocD, nFilas, 1).getValues();
    var vistos = {};
    for (var j = 0; j < valores.length; j++) {
      var d = String(valores[j][0]).trim();
      if (d && !vistos[d]) { vistos[d] = true; docs.push({ doc: d }); }
    }
  }
  if (!docs.length) return;

  var ultima = ordenes.getLastRow();
  var colDocs = ultima >= 2 ? ordenes.getRange(2, cDocO, ultima - 1, 1).getValues() : [];
  var quien = '';
  try { quien = Session.getActiveUser().getEmail() || ''; } catch (err) { quien = ''; }
  var ahora = new Date();

  docs.forEach(function (x) {
    var fila = x.fila || 0;
    if (!fila) {
      for (var k = 0; k < colDocs.length; k++) {
        if (String(colDocs[k][0]).trim() === x.doc) { fila = k + 2; break; }
      }
    }
    // Una orden con líneas en una pestaña de responsable pero sin fila en «Órdenes» no se crea aquí: la plataforma
    // la muestra como «Sin asignar» y la lista entre los datos por revisar del admin.
    if (!fila) return;
    if (String(ordenes.getRange(fila, cDocO).getValue()).trim() === '') return;
    ordenes.getRange(fila, cFecha).setValue(ahora);
    if (cPor && quien) ordenes.getRange(fila, cPor).setValue(quien);
  });
}
