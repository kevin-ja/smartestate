# El pipeline de datos, explicado sin tecnicismos

## Para qué existe

SmartEstate recomienda inmuebles comerciales (bodegas, oficinas, locales): cuando alguien mira uno,
el sistema le sugiere otros parecidos. Para recomendar bien, primero hay que tener **datos confiables**.

Los datos llegan crudos: con errores, formatos mezclados y huecos. El pipeline de datos es una
**línea de producción** que los recibe así y entrega una tabla limpia, lista para que el modelo la use.

```
  Archivos        Etapa 0          Etapa 2          Etapa 3
  crudos    ──▶   Ingesta    ──▶   Limpieza   ──▶   Preprocesamiento  ──▶  Tabla final
  (raw)          (bronze)         (silver)         (gold)                 lista para el modelo

  "tal cual     "ordenado y       "corregido y     "pulido y fácil
   llegó"        verificado"       marcado"         de leer"
```

Cada etapa guarda su resultado en una tabla propia. Si algo sale mal, se sabe en qué paso fue, y
nunca se pierde el original.

Una regla atraviesa todo el pipeline: **ningún inmueble se borra.** Lo dudoso se **marca**, no se elimina.

---

## Etapa 0 · Ingesta — "recibir y revisar la mercancía"

**Qué entra:** dos archivos CSV. Uno con los datos generales de cada inmueble (ubicación, precio,
área, título…) y otro con atributos extra (luz natural, seguridad, tipo de piso…).

| | |
|---|---|
| **Qué hace** | Une los dos archivos en una sola tabla, un renglón por inmueble. Convierte cada columna a su tipo correcto: números como números y fechas como fechas |
| **Por qué** | Los archivos llegan como texto: `"1,250"` no es un número hasta que alguien lo convierte. Y si el archivo cambió de formato, hay que enterarse antes de seguir |
| **Cómo** | Si el archivo no trae las columnas esperadas, **se detiene todo**. Un inmueble con coordenadas fuera de México o de un sector desconocido va a una tabla aparte (**cuarentena**), sin frenar al resto. Si faltan más datos de lo normal, deja un **aviso** |

**Resultado:** 2.500 inmuebles, 0 en cuarentena.

---

## Etapa 2 · Limpieza — "corregir y etiquetar"

| Qué hace | Por qué | Cómo |
|---|---|---|
| **Corrige un código de sector** | Los datos traen el sector "13", que no existe en el diccionario; el que debería existir es el "12" (comercio) | Cambia 13 por 12 y guarda el valor original, por si negocio dice otra cosa |
| **Marca qué se puede recomendar** | Un "Complex" es un edificio completo, no un espacio que alguien renta. No tiene sentido sugerirlo | Lo marca como *no candidato*. No lo borra |
| **Completa el estado** | Al 37% le falta el estado (Jalisco, CDMX…). Sirve para vigilar la calidad por región | Lo deduce del municipio con el catálogo oficial del INEGI. Si el municipio existe en varios estados, elige el más cercano por ubicación |
| **Detecta precios y áreas absurdos** | Una bodega de 3 m² o un precio 100 veces mayor al normal arruinan cualquier comparación | Compara cada inmueble con los de su mismo tipo. Si se sale mucho del rango normal, lo marca y deja de ser candidato |

**Resultado:** 2.500 inmuebles, de los cuales **1.285 se pueden recomendar**.

---

## Etapa 3 · Preprocesamiento — "dejarlo presentable"

| Qué hace | Por qué | Cómo |
|---|---|---|
| **Limpia el texto** | El título y la descripción traen espacios de más, saltos de línea y caracteres raros | Los ordena sin cambiar las palabras: el modelo de lenguaje entiende mejor el texto natural |
| **Arma un texto único por inmueble** | El modelo lee un solo texto por inmueble | Une título + descripción. Si no hay descripción, usa solo el título |
| **Ordena la seguridad** | Viene escrita de varias formas: `[1, 2]`, `["2", "1"]` | La convierte en una sola forma: una lista de números, ordenada y sin repetidos |
| **Limpia el tipo de piso** | Está escrito a mano de 84 formas distintas | Solo lo limpia. No lo clasifica: más adelante, el modelo de lenguaje entiende que "concreto alta resistencia" y "high strength concrete" significan lo mismo |
| **Borra valores imposibles** | Una luz natural de 500% deformaría las comparaciones | Si un valor está fuera de su rango, lo deja vacío y avisa cuántos hubo |

**Resultado:** la tabla final (`gold`), con los mismos 2.500 inmuebles, legible para cualquier persona.

---

## Cómo se sabe que salió bien

Cada etapa termina con **una línea de resumen** en el registro del proceso. Por ejemplo:

```
silver: 2500 rows · 1285 candidates · 165 price_outliers · 468 area_outliers · 7 state_mismatch · 2 warnings
```

Se lee así: entraron 2.500 inmuebles, 1.285 se pueden recomendar, y hubo 2 avisos para revisar.
Si una cifra cambia mucho de un día a otro, algo pasó en el origen.

Hay tres niveles de reacción ante un problema:

| Nivel | Cuándo | Qué pasa |
|---|---|---|
| 🛑 **Detener** | El problema afecta a todo el lote (por ejemplo, cambió el formato del archivo) | No se escribe nada; la tabla anterior queda intacta |
| 📦 **Apartar** | Un inmueble puntual está mal | Va a cuarentena; los demás siguen |
| ⚠️ **Avisar** | Algo raro, pero no grave | Se anota en el registro y el proceso continúa |

---

## Dónde corre

Todo corre en **Databricks**, una plataforma en la nube para procesar datos, y las tablas se guardan
en **Amazon S3**. Las tres etapas forman un solo trabajo automático que se lanza con un comando.

## Qué sigue después

La tabla final es el punto de partida del **pipeline de ML**, que la convierte en lo que el
recomendador necesita para comparar inmuebles. Eso es otra línea de producción, con su propio documento.
