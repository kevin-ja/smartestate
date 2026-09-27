# data_pipeline/ — `smartestate_data`

Convierte los CSV de origen en una tabla de negocio: `raw → bronze → silver → gold`.
Corre en Databricks; los datos viven en S3. Diseño completo en `arquitectura.md` §3.

| Etapa | Módulo | Lee | Escribe | Estado |
|---|---|---|---|---|
| 0 · Ingesta | `ingestion.py` | `raw/` | `bronze` | 🟡 escrito, sin correr en Databricks |
| 2 · Limpieza | `cleaning.py` | `bronze` | `silver` | ⬜ |
| 3 · Preprocesamiento | `preprocessing.py` | `silver` | `gold` | ⬜ |

`contract/` guarda el contrato de datos:

| Archivo | Contenido |
|---|---|
| `schema.py` | Header esperado de cada CSV, y nombre final, tipo y formato de cada columna |
| `rules.py` | Reglas de validación: clave, cuarentena, umbrales de aviso |
| `null_baseline.json` | D8: tasa de nulos esperada por `sector × columna`, calculada sobre `data/` |

**Correr la ingesta** (desde un notebook en la Git folder):

```python
from src.data_pipeline import ingestion
raw_root = "<SMARTESTATE_RAW_ROOT de .env>"
ingestion.run(spark, raw_root, "2026-09-26",
              "smartestate.bronze.spots", "smartestate.bronze.spots_quarantine")
```

---

## Etapa 0 · Ingesta

Lee los dos CSV, los tipa, los une, los valida y los deja en `bronze`. **No corrige valores**: eso es limpieza.

```
Extract → Schema → Parsing → Join + proyección → Validación → Storage
```

### 1. Extract ✅

No hay carga previa a Databricks: el cluster lee directo de S3 (External Location `raw`).

```python
spark.read.csv(f"{raw_root}/ingest_date={ingest_date}/data.csv", ...)
```

- Cada carga manual crea una carpeta `raw/ingest_date=YYYY-MM-DD/` con `data.csv` y `data_at.csv`.
- **Qué carpeta se lee entra por parámetro:** `--ingest-date 2026-09-26`. Es explícito y permite
  reprocesar una carga vieja.

### 2. Schema ✅

Se escribe en `contract/schema.py`, en dos partes:

1. **Columnas esperadas del CSV**: nombre original, en orden. Si el header cambia, se aborta.
2. **Schema destino** (`StructType`): nombre final, tipo y nulabilidad.

Sobreviven **25 columnas, `spot_id` incluido**: 18 de `data.csv` y 7 de `data_at.csv`
(lista en `EDA.md` · Apéndice). Las 7 descartadas solo se revisan en el header.

| CSV | Nombre final | Tipo |
|---|---|---|
| `Spot ID` / `spot_id` | `spot_id` | long, **no nulo** |
| `Spot Sector ID` | `sector_id` | int |
| `Spot Type ID` | `type_id` | int |
| `Spot Modality` | `modality` | string |
| `Spot Latitude` / `Spot Longitude` | `latitude` / `longitude` | double |
| `Spot Municipality` / `Spot State` | `municipality` / `state` | string |
| `Spot Title` / `Spot Description` | `title` / `description` | string |
| `Spot Area In Sqm` | `area_sqm` | double |
| `Spot Price Sqm Mxn Rent` / `Sale` | `rent_price_sqm` / `sale_price_sqm` | double |
| `Spot Price Total Mxn Rent` / `Sale` | `rent_price_total` / `sale_price_total` | double |
| `Spot Maintenance Cost Mxn ($)` | `maintenance_cost` | double |
| `User ID` | `user_id` | long |
| `Spot Created Date` | `created_date` | date |
| `security_type` | `security_type` | string (el JSON se parsea en `gold`) |
| `floor_material` | `floor_material` | string |
| `charging_ports` · `number_of_elevators` · `parking_spaces` · `building_status` · `natural_light` | igual | int |

### 3. Parsing ✅

Todo se lee como `string` (con `multiLine`: 210 descripciones traen saltos de línea) y se convierte
al tipo del schema. **Un parser por tipo de formato, no código por columna**: el contrato dice de qué
tipo es cada columna.

| Formato | Columnas | Regla | Ejemplo |
|---|---|---|---|
| **number** | ids, área, precios, mantenimiento, atributos enteros | quitar `,` y castear al tipo del schema | `"1,531,035"` → `1531035` |
| **coordinate** | `latitude`, `longitude` | número + hemisferio; `S`/`W`/`O` → negativo | `"99.2° W"` → `-99.2` |
| **date** | `created_date` | formato `MMMM d, yyyy` | `"January 5, 2024"` → `2024-01-05` |
| **text** | el resto | tal cual | — |
| **vacío** | todas | `""` → `null` | — |

- **Solo funciones nativas de Spark** (`regexp_replace`, `try_cast`, `try_to_timestamp`), sin UDFs:
  más rápido, y las `try_*` se comportan igual con ANSI encendido o apagado.
- **Si un valor no parsea, se aborta** (contrato de `EDA.md`). Fallo = valor original no vacío que
  queda `null`. El error dice columna, cuántos y 3 ejemplos.
- **No se limpia nada**: ni `trim`, ni normalización, ni rangos o categorías. Eso es `silver`/`gold`.
- `spot_id` trae coma en **los dos** CSV (`"20,215"`): el join va después del parseo.

Verificado sobre `data/`: las 2.500 filas parsean con estas reglas.

### 4. Join + proyección ✅

- Une `data.csv` × `data_at.csv` por `spot_id`. **Solo aquí**: el resto del pipeline ve una tabla.
- Deja las 25 columnas del schema, ya renombradas.
- **Aviso** si cruza menos del 99% de los `spot_id` (hoy: 100%, 1:1).

### 5. Validación ✅

Schema y Parsing revisan **formato** ("es un número"). Esto revisa **valor** ("es un número correcto").
Va después del join: D8 mide por sector, y los atributos no tienen sector hasta unirse.

Recibe una tabla y devuelve tres cosas: filas buenas, filas en cuarentena y avisos.

| Acción | Regla | Por qué |
|---|---|---|
| **Abortar** | `spot_id` repetido | El lote está mal; un duplicado se multiplica en el join |
| **Cuarentena** · `out_of_bbox` | coordenada fuera de México | Fila mala aislada; la corrida sigue |
| **Cuarentena** · `unknown_sector` | `sector_id` ∉ {9, 11, 13, 15} | Un código nuevo es decisión de negocio |
| **Aviso** | nulos por `sector × columna` > línea base + 15 pp (D8) | Señal del origen; no bloquea en v1 |

- Las reglas se declaran en `contract/` (nombre, condición, acción); `ingestion.py` solo las aplica.
  Regla nueva = una línea en el contrato.
- Los avisos van al log de la corrida.

### 6. Storage ✅

| Tabla | Contenido |
|---|---|
| **`smartestate.bronze.spots`** | Filas buenas |
| **`smartestate.bronze.spots_quarantine`** | Filas en cuarentena + columna `reason` |

Ambas Delta managed de Unity Catalog. `raw/` nunca se toca.

- **Delta managed, no parquet por ruta** (D7, revisada 2026-09-26): es lo que permiten los grants
  de Unity Catalog; cero cambios de infra. UC decide dónde van los archivos; se usa el nombre de tabla.
- **`overwrite` completo** en cada corrida: idempotente y atómico (una corrida fallida no deja la
  tabla a medias).
- Los nombres de tabla entran **por parámetro**, igual que la fecha.
