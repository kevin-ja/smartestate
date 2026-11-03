# Plan de implementación — Pipeline de entrenamiento

> **Fuente de verdad para implementar el entrenamiento.** El diseño manda en `new_arch.md` §1–§3; este
> archivo dice **en qué orden** y **cómo se verifica** cada paso. Si choca con `new_arch.md`, se
> corrige este archivo.

**Alcance:** data pipeline (ingesta → limpieza → preprocesamiento → FE → Spots FS offline), modeling
pipeline (K-means + GeoIndex + `cluster_id`) y materialización (offline → DynamoDB). La inferencia, el
CI/CD y el monitoreo quedan fuera.

```
CSV (S3) ─▶ [T1 data_pipeline] ─▶ spots_fs_offline ─▶ [T2 modeling] ─▶ runs/<run_id>/ + cluster_id
                                                                         │
                                                       [T3 materialization] ─▶ spots_fs_online_<run_id>
```

---

## Reglas de ejecución

1. **Una fase a la vez.** No se empieza la siguiente sin cumplir el "Hecho cuando" de la actual.
2. **Diseño antes que código:** cada decisión abierta de la Fase 0 se cierra con Kevin antes de tocarla.
3. **Infra la aplica Kevin.** Yo escribo el Terraform y le paso el comando; nunca uso perfiles ajenos.
4. **Solo target `dev`.** Nada se despliega a `prod`.
5. **Entorno:** `./ss/bin/python`. PySpark solo en Databricks (no en la laptop).
6. **Código:** nombres en inglés; funciones puras sobre DataFrames; solo los módulos de I/O leen o
   escriben. Docs en español, breves.
7. **Git:** sin atribución a Claude. Commit por fase, solo cuando Kevin lo pida.
8. **Costo:** al cerrar cada sesión, `bash shut_down.sh`. Nunca `export DATABRICKS_*`.

---

## Fase 0 · Precondiciones y decisiones bloqueantes

| # | Tema | Pregunta | Propuesta |
|---|---|---|---|
| 0.1 | **Workspace** | El trial venció ~2026-10-07. ¿Sigue activo el workspace o hay que pasar a pago? | ✅ **Cerrada 2026-10-21 (Kevin):** la cuenta sigue activa y crea workspaces. Es **pay-as-you-go por AWS Marketplace** (plan Premium): los DBU llegan en la factura de AWS y **no** los cubren los créditos; EC2, NAT y S3 sí. Estimado para lo que queda: **~USD 3–6,5** (10 h de cluster dev ≈ 1,9; 20 corridas del job ≈ 1,0; SQL por accidente ≈ 2–3,5). Workspace del trial borrado |
| 0.2 | **`run_id`** | ¿De dónde sale? | ✅ **Cerrada 2026-10-19 (Kevin):** el **`job.run_id` de Databricks**, pasado a las tres tasks con `{{job.run_id}}` en el bundle. Nadie lo crea ni lo pasa; un repair run conserva el mismo id. El run de MLflow lo guarda como tag y el CD lo busca por ahí |
| 0.3 | **`champion`** | Los alias de MLflow son del **Model Registry**, no de corridas, y descartamos pyfunc | ✅ **Cerrada 2026-10-22 (Kevin):** **tag de corrida** `champion=true`, una sola corrida a la vez; el CD busca la corrida con ese tag. Rollback = mover el tag. Anotado en `new_arch.md` §4 y §5 |
| 0.4 | **Entrada del K-means** | `new_arch.md` no fija qué vectores usa | **Un K-means por sector** sobre `[tab_vector ‖ nlp_vector]` (Land: solo `nlp_vector`) |
| 0.5 | **Modelo SBERT** | Next steps 5 | ✅ **Cerrada 2026-10-19 (Kevin):** `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`, revisión `4328cf2`, **768 dim**, `max_seq_length` 128. BETO crudo descartado (no está entrenado para similitud). Subido a `models/sbert/paraphrase-multilingual-mpnet-base-v2.tar.gz` (solo safetensors + config; carga sin internet) |
| 0.6 | **`StandardScaler`** | ¿sklearn (toPandas) o Spark? | ✅ **Cerrada 2026-10-20 (Kevin):** **Pipeline de `pyspark.ml` por sector**; el `.fit()` aprende media y desviación y MLflow registra el `PipelineModel` (`mlflow.spark.log_model`, uno por sector; 2026-10-21 reemplazó a `autolog`, que choca con tres fits en el mismo run). Sin `fe_scalers.json`. Descartado el `groupBy` a mano: mismos números, más código propio y un JSON escrito por nosotros |
| 0.7 | **Reescritura del FS offline** | `write_table` de Feature Engineering hace *merge* | Verificar en dev: `overwrite` conservando la PK `spot_id` |
| 0.8 | **Schema de UC** | Dónde vive `spots_fs_offline` | ✅ **Cerrada 2026-10-20 (Kevin):** schema nuevo **`smartestate.feature_store_offline`**, mismo nombre que su carpeta `s3://<lake>/feature_store_offline/` (el Terraform usa el nombre de la capa para el schema). Tabla: `smartestate.feature_store_offline.spots_fs_offline` |
| 0.9 | **`building_status` en Office** | ¿Se quita? (§3.2.1) | ✅ **Cerrada 2026-10-20 (Kevin): se quita.** 84 % nulos y 74 de 75 = 1: el one-hot solo diría "hay dato o no" e inflaría el coseno. Office queda en **7** dimensiones |

**Hecho cuando:** las decisiones están cerradas y anotadas en `new_arch.md`, salvo dos que se resuelven después (acordado 2026-10-21): la **0.4** se cierra antes de empezar la Fase 3, y la **0.7** se verifica en dev en la primera corrida de T1.

---

## Fase 1 · Infra (Terraform, la aplica Kevin)

| Paso | Qué | Dónde |
|---|---|---|
| 1.1 | Schema `smartestate.feature_store_offline` + External Location + grants. Terraform escrito 2026-10-20 (`table_layers`, `storage_root` del catálogo); la parte AWS ya está aplicada (prefijo y escritura del rol). Falta: `DROP SCHEMA … CASCADE` de bronze/silver/gold y `apply` de `infra/uc/` con el workspace arriba (el catálogo probablemente se reemplaza: revisar el plan) | `infra/uc/` |
| 1.2 | Prefijo `runs/` en el lake + External Location **de escritura** + grants `READ_FILES`/`WRITE_FILES` al grupo pipeline | `infra/modules/lake`, `infra/uc/` |
| 1.3 | Rol IAM + **instance profile** con `dynamodb:CreateTable/DescribeTable/PutItem/BatchWriteItem/UpdateItem/DeleteTable/ListTables` sobre `spots_fs_online_*` | `infra/modules/` (nuevo) |
| 1.4 | Registrar el instance profile en Databricks y habilitarlo en la policy de jobs | `infra/modules/dbx_compute_policies` |
| 1.5 | Experimento MLflow `/Shared/smartestate-train` (por bundle; sin subcarpeta, así no depende de que exista `/Shared/smartestate`) | `bundles/` |

**Hecho cuando:** `terraform plan` sin destrucciones, `apply` hecho por Kevin, y desde un notebook en
dev: escribir/leer un archivo en `runs/_probe/` y `ListTables` en DynamoDB funcionan.

---

## Fase 2 · Data pipeline (T1): CSV → Spots FS offline

Sin tablas intermedias (`new_arch.md` §3.1): **una sola task** que encadena las etapas en memoria.

| Paso | Qué | Archivos |
|---|---|---|
| 2.1 | Separar en `ingestion`, `cleaning` y `preprocessing` la lógica (`transform`) de la escritura. La lógica ya existe: no se reescribe | `src/data_pipeline/*.py` |
| 2.2 | Cuarentena a `runs/<run_id>/quarantine.parquet` (no a tabla) | `ingestion.py`, `storage.py` |
| 2.3 | **Nuevo `feature_engineering.py`**: un Pipeline de `pyspark.ml` por sector (0.6), en el orden de §3.2.1: `log1p` → escalado por sector → nulo = 0 → one-hot `building_status` (no en Office, 0.9) y multi-hot `security_type` → `natural_light / 100` → L2 → `tab_vector`; `modality_mask`; se descarta `modality` | `src/data_pipeline/feature_engineering.py` |
| 2.4 | SBERT → `nlp_vector` **en el driver** (2026-10-21, Kevin: el cluster es single-node y con `pandas_udf` cada worker de Python cargaría su copia de ~1 GB). El driver copia el tar de `models/sbert/` a disco local, carga el modelo una vez y codifica en batch los ~2.500 textos; L2 en float64, nulo sin descripción (D3) | mismo módulo |
| 2.5 | Escribir `spots_fs_offline` (PK `spot_id`) completa, con `cluster_id` nulo (`CREATE TABLE IF NOT EXISTS` con PK + `INSERT OVERWRITE`, que conserva la PK); los `PipelineModel` de FE quedan en el run de MLflow (no van a S3: ningún servicio los lee) | `storage.py` |
| 2.6 | `jobs/run_data_pipeline.py`: recibe `run_id` (`{{job.run_id}}`), abre el run de MLflow con ese tag y encadena 2.1–2.5 | `src/data_pipeline/jobs/` |
| 2.7 | Contrato `FS_OFFLINE_SCHEMA` y validación (dimensiones de `tab_vector` por sector: Industrial 8, Office 7, Retail 7; Land nulo; `nlp_vector` de 768; `spot_id` único) | `contract/schema.py` |

**Verificación:**

* Tests de las funciones puras con Spark `local[*]` (se corren en Databricks hasta que exista el CI).
* Corrida en dev sobre `data/`: 2.500 filas leídas, la cuarentena cuadra con la ingesta actual, y los
  conteos de candidatos por sector son 634/466/164/16.
* Las normas L2 de `tab_vector` y `nlp_vector` valen 1 ± 1e-6.

**Hecho cuando:** `spots_fs_offline` existe con el esquema del contrato y la validación pasa.

---

## Fase 3 · Modeling pipeline (T2)

| Paso | Qué | Archivos |
|---|---|---|
| 3.1 | **`GeoIndex`** (clase de `new_arch.md` §4.1) en un paquete compartido, instalable en Databricks y en ECS | `src/common/geo_index.py` |
| 3.2 | Leer candidatos (`is_candidate = True`) ordenados por `spot_id` → `toPandas` | `src/ml_pipeline/modeling.py` |
| 3.3 | K-means por sector (según 0.4), `k` por silhouette en 5–8, con `random_state` fijo | mismo |
| 3.4 | Construir el GeoIndex; `joblib.dump` → `runs/<run_id>/geo_index.joblib` + K-means | mismo |
| 3.5 | Copiar `reranker_params.yml` (repo) → `runs/<run_id>/reranker_params.json` + `mlflow.log_params` | `config/reranker_params.yml` |
| 3.6 | Agregar `cluster_id` a `spots_fs_offline` (merge por `spot_id`; nulo en no candidatos) **al final** | mismo |
| 3.7 | Registrar en MLflow: params, métricas (silhouette por sector, conteos) y rutas de artefactos | mismo |

**Verificación:**

* Test local (`./ss`, sin Spark) de `GeoIndex.query`: el caso del Nivel 1/2 (`[20365, 10322]`), la
  regla de bits y el radio.
* Round-trip: `joblib.load` en un proceso limpio, con solo el paquete instalado, devuelve lo mismo.
* Todo candidato tiene `cluster_id`; ningún no candidato lo tiene.

**Hecho cuando:** `runs/<run_id>/` tiene los tres archivos y el run de MLflow los referencia.

---

## Fase 4 · Materialización (T3)

| Paso | Qué |
|---|---|
| 4.1 | `publish_table(spots_fs_offline → AmazonDynamoDBSpec(table_name="spots_fs_online_<run_id>"))`, en cluster Dedicated con el instance profile |
| 4.2 | Limpieza (P31): dejar las últimas 3 corridas **en los dos lados**: borrar las `spots_fs_online_*` más viejas en DynamoDB y los `runs/<run_id>/` más viejos en S3 |
| 4.3 | Smoke test: `GetItem` de un `spot_id` conocido; `nlp_vector` se parsea a 768 floats; existe la llave interna |

**Hecho cuando:** la tabla existe, el ítem de prueba coincide con el offline y hay ≤ 3 tablas.

---

## Fase 5 · Job y bundle

| Paso | Qué |
|---|---|
| 5.1 | El job `smartestate_data` pasa a `smartestate_train` con tres tasks: `data_pipeline → modeling → materialization` (cada una `depends_on` de la anterior) |
| 5.2 | Librerías de las tasks pineadas (`sentence-transformers`, `scikit-learn`, `databricks-feature-engineering`, `mlflow`, `joblib`) y también en `requirements.txt` |
| 5.3 | Borrar los parámetros y tablas de `bronze`/`silver`/`gold` del job (las tablas se borran recién después de 6.1) |
| 5.4 | `bundle validate` y `deploy -t dev` |

**Hecho cuando:** `bundle validate` sin errores y el job aparece con las tres tasks.

---

## Fase 6 · Verificación de punta a punta

| # | Chequeo |
|---|---|
| 6.1 | Corrida completa en dev en verde |
| 6.2 | **Repair run:** forzar una falla en T2 y verificar que se repite solo T2, sin materializar (§3.1) |
| 6.3 | El mismo `run_id` aparece en MLflow, `runs/<run_id>/` y `spots_fs_online_<run_id>` |
| 6.4 | Reproducibilidad: dos corridas sobre la misma data dan el mismo `cluster_id` y los mismos vectores |
| 6.5 | Tiempo y costo de la corrida anotados |

---

## Fase 7 · Cierre

1. Actualizar `new_arch.md` con lo que cambie al implementar.
2. Actualizar `src/data_pipeline/README.md`, `HANDOFF.md` (§2, §9) y su regla "`ml_pipeline` solo lee
   `gold`", que pasa a "solo lee `spots_fs_offline`".
3. ~~Borrar las tablas `bronze`, `silver` y `gold`~~ Adelantado 2026-10-20 (Kevin): datos y prefijos borrados en S3; queda el `DROP SCHEMA` en UC (paso 1.1).
4. `bash shut_down.sh`.

---

## Riesgos

| Riesgo | Mitigación |
|---|---|
| Costo de DBU (Marketplace, sin créditos) | Jobs Compute para el pipeline; cluster dev con auto-apagado de 20 min; no abrir SQL Editor ni Catalog sin necesidad; `shut_down.sh` al cerrar |
| `publish_table` falla por permisos o el modo del cluster | Fase 1 (smoke test de DynamoDB) antes de la Fase 4 |
| SBERT lento o sin internet en el cluster | Sin internet: resuelto, el modelo se lee de `models/sbert/`. Lentitud: medir en 2.4 |
| `overwrite` rompe la PK del feature store | Se verifica en 0.7 antes de 2.5 |
| El GeoIndex no carga en ECS | Round-trip de 3.4 en un proceso limpio |
