# EDA — Columnas por nivel de la cascada

> Derivado de `notebook.ipynb` §1 (EDA, completa). Mapea cada columna al nivel de la cascada
> donde se usa y marca cuáles entran a la **Etapa 2 (Limpieza)**.
>
> **Limpieza ≠ preprocesamiento.** Aquí solo va lo que corrige, recorta o deriva sobre el dataset.
> Normalización de texto, parseo de JSON y escalado son Etapa 3.
>
> **Última actualización:** 2026-09-21 (absorbió el contrato de datos, antes `arquitectura.md` §3.2)

---

## Contrato de datos — la frontera `raw → bronze`

El esquema **no se infiere: se declara y se impone** (header exacto, parseo a los tipos de `StructType`,
`FAILFAST` para filas malformadas). Implementado en `src/data_pipeline/ingestion.py`; reglas en
`src/data_pipeline/contract/`. Diseño paso a paso en `src/data_pipeline/README.md`.

| Validación | Acción si falla |
|---|---|
| Columnas esperadas presentes | **Aborta.** El origen cambió de contrato |
| Tipos convertibles | **Aborta.** No se castea en silencio a `null` |
| `spot_id` único y no nulo | **Aborta.** Es la llave del join |
| `latitude`/`longitude` dentro de `BBOX_MEXICO` | **Cuarentena** de la fila (`smartestate.bronze.spots_quarantine`), sigue la corrida |
| `sector_id` en el catálogo conocido | **Cuarentena** + alerta. Un código nuevo es decisión de negocio, no error de dato |
| Join `spots`×`atributos` al 100% | Warning bajo 99%. Verificado: join **1:1**, cero huérfanos |

**La distinción que hay que respetar:** un **cambio de contrato aborta**; una **fila mala va a
cuarentena**. Una fila corrupta no debe tumbar la corrida de 10⁶ spots, pero un origen que cambió sí
— si pasa, `gold` se llena de basura en silencio y se descubre semanas después en el Top-10.

**Nulos estructurales.** Se aceptan donde el EDA los declaró estructurales (venta en inmuebles de
renta, atributos que no aplican al sector). No se imputan y **no cuentan como error de calidad**.

> **✅ Decidido 2026-09-26 — cómo se mide la tasa de nulos (D8, opción C).** En `data_at.csv` hay ~7
> celdas vacías por fila (§1) y son estructurales: un chequeo global **fallará siempre**. Se mide contra
> una **línea base por `sector × columna`** que sale del EDA, solo sobre las columnas que aplican al
> sector. Si una corrida se desvía de la línea base → **warning** (v1; referencia inicial +15 pp, a
> calibrar). El **abort** llega cuando haya historial. **Sin cuarentena:** la tasa es del lote, no de la
> fila. **La línea base vive en el repo**, versionada junto a este contrato (cambios por PR, testeable
> sin bucket); pasa a tabla en Unity Catalog cuando haya historial de corridas. Ver `HANDOFF.md` §6 · D8.

---

## Nivel 1 — Filtro por categoría y modalidad

| CSV crudo | Notebook | ¿Limpieza? | Qué se hace |
|---|---|---|---|
| `Spot Sector ID` | `sector_id` | ✅ **Sí** | Mapear `13 → 12 (Retail)`. Conservar `sector_id_original` para trazabilidad |
| `Spot Type ID` | `tipo_id` | ✅ **Sí** | Derivar `es_candidato = False` para los `Complex` (43.6%). No se borran filas |
| `Spot Modality` | `modalidad` | ❌ No | 3 valores limpios, 0% nulos. `Rent & Sale` se resuelve con `in` en inferencia |

**A limpiar: 2 de 3.** Ninguna se corrige en sitio — se derivan columnas nuevas, ambas reversibles.

---

## Nivel 2 — Zona geográfica (BallTree)

| CSV crudo | Notebook | ¿Limpieza? | Qué se hace |
|---|---|---|---|
| `Spot Latitude` | `lat` | ❌ No | 0% nulos, 0% fuera de México. El redondeo a 2 decimales es irreversible → restricción de Etapa 5 |
| `Spot Longitude` | `lon` | ❌ No | Ídem |
| `Spot Municipality` | `municipio` | ❌ No | 0% nulos. Solo texto de salida de la API |
| `Spot State` | `estado` | ✅ **Sí** | Completar 923 vacíos vía catálogo INEGI (CSV estático). El estado que no cuadra con el municipio se marca, no se corrige (D12) |
| `Spot Settlement` | `colonia` | ⛔ Descartar | 48.7% nulos |
| `Spot Corridor` | `corredor` | ⛔ Descartar | 31.0% nulos; inferible desde `lat`/`lon` solo al 87% (<90% exigido) |

**A limpiar: 1** (`estado`, y ni siquiera entra al modelo). Las dos columnas que el BallTree realmente
consume están limpias de origen.

---

## Nivel 3 — Reranker de similitud

### 3A · NLP

| CSV crudo | Notebook | ¿Limpieza? | Nota |
|---|---|---|---|
| `Spot Title` | `titulo` | ❌ No | Normalización para SBERT es Etapa 3 |
| `Spot Description` | `descripcion` | ⚠️ **Decisión pendiente** | 16.3% global, **36.1% en Industrial Rent**. Ver D3 en `HANDOFF.md` |

### 3A · Vector tabular (`data_at.csv`)

| Columna | Notebook | ¿Limpieza? | Nota |
|---|---|---|---|
| `security_type` | `tipo_seguridad` | ❌ No | Llega como lista JSON en texto → parseo en Etapa 3 |
| `floor_material` | `material_piso` | ❌ No | Texto libre sucio → normalización en Etapa 3 |
| `charging_ports` | `puertos_carga` | ❌ No | |
| `number_of_elevators` | `elevadores` | ❌ No | |
| `parking_spaces` | `estacionamientos` | ❌ No | |
| `building_status` | `estado_edificio` | ❌ No | |
| `natural_light` | `luz_natural` | ❌ No | |

Los vacíos aquí son **estructurales**, no datos faltantes: una oficina no tiene puertos de carga, un
terreno no tiene elevadores. **No imputar** — es regla explícita de la Etapa 2. El vector tabular es
distinto por sector (Industrial 6 · Office 5 · sector 13: 4 · Land 1), y eso se resuelve en Etapa 4.

### 3B · Precio — `f(r)`

| CSV crudo | Notebook | ¿Limpieza? | Qué se hace |
|---|---|---|---|
| `Spot Area In Sqm` | `area_m2` | ✅ **Sí** | Recortar outliers: máx. 1.000.000 m², mín. 3 m² |
| `Spot Price Sqm Mxn Rent` | `renta_m2` | ✅ **Sí** | Recortar outliers **por partición** (máx. observado 1.531.035 MXN/m²) |
| `Spot Price Sqm Mxn Sale` | `venta_m2` | ✅ **Sí** | Ídem |
| `Spot Price Total Mxn Rent` | `renta_total` | ❌ No | Identidad coherente al 99.7%. Nulos estructurales: **no imputar** |
| `Spot Price Total Mxn Sale` | `venta_total` | ❌ No | Identidad coherente al 100%. Ídem |
| `Spot Maintenance Cost Mxn ($)` | `mantenimiento` | ❌ No | Entra al vector tabular, sin hallazgo de calidad |

Criterio decidido (D11, 2026-09-28): **IQR sobre log**, k=1,5, por partición. No se recorta: se marca y la
fila deja de ser candidata. El EDA los cuantifica en 2.4%–12.5% por partición.

### 3C · Diversidad (clones)

| CSV crudo | Notebook | ¿Limpieza? | Nota |
|---|---|---|---|
| `User ID` | `user_id` | ❌ No | Insumo para detectar clones (21.0% del inventario). Se resuelve con k-means en Etapa 5 |

---

## Resumen

**Limpiar en Etapa 2 — 6 columnas**

| # | Columna | Acción |
|---|---|---|
| 1 | `sector_id` | Mapear `13 → 12` |
| 2 | `tipo_id` | Derivar `es_candidato` |
| 3 | `estado` | Completar con INEGI; marcar el que no cuadra (D12) |
| 4 | `area_m2` | Recortar outliers |
| 5 | `renta_m2` | Recortar outliers por partición |
| 6 | `venta_m2` | Recortar outliers por partición |

**Descartar — 2:** `colonia`, `corredor`.

**Decisión abierta — 1:** `descripcion` (D3 en `HANDOFF.md`).

### Dos lecturas

1. **El Nivel 2 casi no necesita limpieza.** `lat`/`lon` están al 100% y la única columna sucia
   (`estado`) ni siquiera entra al modelo — viaja como texto de salida de la API.
2. **Toda la carga real está en el Nivel 3B.** Precio y área son justamente los insumos que rompen
   `f(r)`: el cociente `r = P_m²(B)/P_m²(A)` se distorsiona entero si un outlier cae en el
   denominador. Por eso hay que limpiar **antes** de calibrar la cota.

---

## Fuera de alcance de este documento

Hallazgos del EDA que **no** se traducen en limpieza de columnas:

- **Coordenadas redondeadas a 2 decimales** (celda ~1.1 km, 65% comparte coordenada). Irreversible;
  es una restricción de diseño en Etapa 5, no un tratamiento de datos.
- **`(Land, Rent)` con 2 inmuebles.** Limitación de inventario. Se resuelve en el contrato del
  endpoint: el Top-10 es un tope, no un requisito.
- **Cobertura geográfica 87.1%.** Se reporta como métrica en Etapa 6.
- **Cota `[0.7, 1.1]` de `f(r)` saturada.** Recalibración en Etapa 5, después de limpiar precios.

---

## Apéndice — Proyección de columnas a `bronze`

La lista cerrada que sale del EDA. **Es el contrato de `bronze`** (Etapa 0): **25 columnas, `spot_id` incluido** (18 de `data.csv` + 7 atributos de `data_at.csv`).
Coincide con `arquitectura.md` Paso 2. Los 7 atributos usables son exactamente los de §3A.

> **Nombres en inglés** desde 2026-09-26: son los de `bronze` (`src/data_pipeline/contract/schema.py`).
> El resto de este documento usa el vocabulario del notebook del EDA, en español.

### Desde `data.csv` — 18 de 22

| Origen | Destino | Rol |
|---|---|---|
| `Spot ID` | `spot_id` | Llave primaria · join |
| `Spot Sector ID` | `sector_id` | Nivel 1 · clave de partición |
| `Spot Type ID` | `type_id` | Nivel 1 · elegibilidad (`es_candidato`) |
| `Spot Modality` | `modality` | Nivel 1 · clave de partición (`intencion`) |
| `Spot Latitude` | `latitude` | Nivel 2 · BallTree |
| `Spot Longitude` | `longitude` | Nivel 2 · BallTree |
| `Spot Municipality` | `municipality` | Diagnóstico y monitoreo |
| `Spot State` | `state` | Diagnóstico y monitoreo · se corrige en `silver` con INEGI |
| `Spot Title` | `title` | Nivel 3A · NLP |
| `Spot Description` | `description` | Nivel 3A · NLP (nulo tolerado, D3) |
| `Spot Area In Sqm` | `area_sqm` | Nivel 3A tabular · Nivel 3B |
| `Spot Price Sqm Mxn Rent` | `rent_price_sqm` | Nivel 3B · `f(r)` |
| `Spot Price Sqm Mxn Sale` | `sale_price_sqm` | Nivel 3B · `f(r)` |
| `Spot Price Total Mxn Rent` | `rent_price_total` | Métrica de coherencia de precio |
| `Spot Price Total Mxn Sale` | `sale_price_total` | Métrica de coherencia de precio |
| `Spot Maintenance Cost Mxn ($)` | `maintenance_cost` | Nivel 3A tabular (los 4 sectores) |
| `User ID` | `user_id` | Nivel 3C · detección de clones |
| `Spot Created Date` | `created_date` | Etapa 4 · antigüedad |

**Descartadas — 4:** `Spot Settlement` (48.7% nulos) · `Spot Corridor` (31.0% nulos, inferible solo
al 87%) · `Spot Region` (redundante con `state`, D6) · `Spot Address` (texto libre, D6).

### Desde `data_at.csv` — 7 + `spot_id`

Conservan su nombre de origen: `security_type` · `floor_material` · `charging_ports` ·
`number_of_elevators` · `parking_spaces` · `building_status` · `natural_light`.

**Descartadas — 3:** `luminaries`, `floor_level_number`, `vertical_height`. Sin cobertura usable en
ningún sector.

### Vector tabular resultante

| Sector | Dim | Columnas |
|---|---|---|
| 9 · Industrial | 6 | `area_sqm` `maintenance_cost` `charging_ports` `building_status` `natural_light` `floor_material` |
| 11 · Office | 5 | `area_sqm` `maintenance_cost` `building_status` `security_type` `number_of_elevators` |
| 13 · Retail | 4 | `area_sqm` `parking_spaces` `maintenance_cost` `building_status` |
| 15 · Land | 1 | `area_sqm` — sin vector tabular; su `sim` usa solo NLP |

> `building_status` (en el EDA, `estado_edificio`) es la **condición del inmueble**. La entidad
> federativa (`state`) no entra a ningún vector.

