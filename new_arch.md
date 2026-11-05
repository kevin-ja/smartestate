# Nueva arquitectura — SmartEstate

> **Fuente de verdad de la arquitectura desde 2026-10-02.** Si otro documento la contradice, gana este.

**Por qué:** la arquitectura anterior tiene procesos que corren de forma ineficiente; se reestructura
todo el repo.

**Lo anterior se conserva.** La infra (`infra/`), el pipeline de datos (`src/data_pipeline/`,
`bundles/data/`), el EDA y su documentación siguen como están hasta que este documento los reemplace.
La arquitectura del pipeline de ML anterior se eliminó.

---

## 1. Dos pipelines

| # | Pipeline | Qué hace | Dónde corre |
|---|---|---|---|
| 1 | **Entrenamiento** | Entrena el modelo | Databricks |
| 2 | **Inferencia** | Responde las consultas, en producción | AWS |

**Orden de ejecución (lógico):**

1. **Entrenamiento**, con sus dos subpipelines. Sale: feature store offline, K-means, GeoIndex y
   artefactos de FE (encoders, scalers).
2. **Materialización** del feature store offline al online (§3.4).
3. **Inferencia**, una vez que existe esa salida.

**Relación:** el entrenamiento escribe el feature store offline en Unity Catalog y los artefactos en
S3 (`runs/<run_id>/`). La materialización lo lleva al feature store online (DynamoDB). La inferencia
lee las features del feature store online y carga el GeoIndex de S3. El K-means no se carga: la
inferencia solo lee `cluster_id`. Los artefactos de FE no los usa la inferencia.

**Supuesto:** la data es estática. No hay altas, bajas ni modificaciones de spots.

**El disparo** queda fuera de esta sección: depende de la integración de CI/CD.

## 2. Fuentes de verdad

**Dos feature stores, uno por pipeline.** Pesan lo mismo: los dos son fuente de verdad, cada uno en
su contexto de uso.

* **Offline** (Unity Catalog, Databricks): fuente de verdad para el **entrenamiento**.
* **Online** (DynamoDB, AWS): fuente de verdad para la **inferencia**, el servicio en tiempo real. Se
  materializa desde el offline (§3.4).

```
raw data (CSV en S3) ──▶ data pipeline ──▶ FS offline ──▶ modeling pipeline
                                                 │
                                                 └── materialización ──▶ FS online ──▶ inferencia
```

### 2.1 Datos de origen

Dos CSV en `s3://<lake>/raw/ingest_date=YYYY-MM-DD/`. Cada fila es la publicación de un inmueble, y
`spot_id` une las dos tablas (1:1).

```
        data.csv                              data_at.csv
       2.500 × 22                             2.500 × 11
     "la publicación"                     "los atributos físicos"
   ┌────────────────────┐                ┌────────────────────────┐
   │ Spot ID            │◀── misma ─────▶│ spot_id                │
   │ sector, modalidad  │    llave       │ puertos de carga       │
   │ lat, lon           │                │ elevadores             │
   │ título, descripción│                │ estacionamientos       │
   │ área, precios      │                │ material del piso      │
   │ mantenimiento      │                │ tipo de seguridad      │
   └────────────────────┘                └────────────────────────┘
```

* `data.csv` dice qué inmueble es y cuánto cuesta; `data_at.csv`, cómo es por dentro.
* Los nulos de `data_at.csv` (~7 por fila) son **vacío estructural**: un terreno no tiene elevadores.
  No se imputan en la ingesta.
* Las dos tablas se unen en la ingesta y solo ahí: de ahí en adelante el pipeline ve un solo dato.

### 2.2 Volumen supuesto (por qué PySpark)

`data/` (2.500 filas) es una **muestra**. Todo se dimensiona contra el inventario proyectado.

| Magnitud | Supuesto |
|---|---|
| Inventario activo | **10⁵ – 10⁶ spots** |
| Crecimiento | ~10⁴ altas/mes |
| Ancho tras el join | ~35 columnas (22 + 11) |
| Latencia online | milisegundos |
| PII | No hay. `user_id` es un anunciante, no una persona |

Si el inventario real queda en ≤ 10⁴, PySpark deja de justificarse y se cambia por pandas o DuckDB.
El contrato, el feature store y la inferencia no cambian.

## 3. Pipeline de entrenamiento

**Corre en Databricks.** Procesa por lotes (batch).

**Entrada:** la raw data (los dos CSV en S3). De ahí sale el feature store offline.

**Cuándo se reentrena:**

**A demanda**, cuando cambia el código, las features o los hiperparámetros (el disparo es de CI/CD).
Con data estática, un reentrenamiento periódico daría el mismo modelo.

### 3.1 Dos subpipelines

Corren **en secuencia** y siempre juntos: primero el data pipeline, después el modeling pipeline.
**El modeling pipeline depende del data pipeline:** solo corre si el data pipeline termina bien.

**Si algo falla:**

* **Falla el data pipeline:** no corre nada más y se avisa. Spots FS offline queda como estaba.
* **Falla el modeling pipeline:** no se materializa y se avisa. Spots FS offline queda con la última
  corrida exitosa del data pipeline. Se vuelve a correr **solo el modeling pipeline** (*repair run* de
  Databricks: repite solo las tareas que fallaron) y, si termina bien, se materializa.

**Sin tablas intermedias:** el data pipeline va directo de los CSV al feature store offline. Desaparecen
`bronze`, `silver` y `gold`; ya no hay arquitectura medallion. Sus carpetas en S3 se borraron el 2026-10-20.

**Cuarentena:** las filas que la ingesta aparta se guardan en `runs/<run_id>/quarantine.parquet`, junto
a los modelos de esa corrida. Solo sirve para auditar; ningún pipeline la lee. El log de la
corrida registra cuántas filas fueron a cuarentena.

```
                ┌──────────────── data pipeline ────────────────┐   ┌── modeling pipeline ──┐
CSV (S3) ──▶ ingesta ──▶ limpieza ──▶ preprocesamiento ──▶ FE ──▶ FS offline ──▶ modelado
                                                            │                          │
                                                            ▼                          ▼
                                                     artefactos de FE        K-means + GeoIndex
```

| Subpipeline | Etapas | Sale |
|---|---|---|
| **Data pipeline** | Ingesta, limpieza y preprocesamiento (los tres flujos que ya existen) + feature engineering (nuevo, incluye embeddings del texto con **SBERT**, el embedder puede cambiar, y los encodings como `modality_mask`) | Spots FS offline (`spots_fs_offline`), reescrita completa con `cluster_id` nulo, y los artefactos del feature engineering |
| **Modeling pipeline** | Modelado: lee Spots FS offline, entrena un K-means por sector sobre `tab_vector ‖ nlp_vector` (Land: solo `nlp_vector`), con los candidatos que tienen texto; los que no tienen van al centroide más cercano midiendo solo `tab_vector`. Construye el GeoIndex **solo con candidatos** (`is_candidate = True`). Predice el `cluster_id` de los candidatos en memoria y, al final, si todo salió bien, lo agrega a Spots FS offline (nulo para los no candidatos) | K-means, GeoIndex y `cluster_id` en Spots FS offline |

**Job en Databricks:** un solo job, `smartestate_train`, con tasks en secuencia. Cada una corre
solo si la anterior terminó bien.

```
job smartestate_train
┌────────────┐   ┌──────────────────┐   ┌──────────────┐   ┌─────────────┐   ┌───────────────────┐
│ unit_tests │──▶│ T1 data_pipeline │──▶│ T2 modeling  │──▶│ validate_fs │──▶│ T3 materialization│
└────────────┘   └──────────────────┘   └──────────────┘   └─────────────┘   └───────────────────┘
  código          CSV → spots_fs_offline  K-means+GeoIndex   ¿datos OK?        offline → DynamoDB
```

**Dos puertas (2026-11-03, Kevin):**

* **`unit_tests` (código), primero.** Si el código está roto no se paga T1 ni se escribe la tabla.
  Antes de cada deploy, pytest corre también en local (`./ss`), así el cluster no descubre lo que la
  Mac ya podía ver.
* **`validate_fs` (datos), antes de T3.** Revisa Spots FS offline ya escrita: esquema, PK única,
  nulos y conteos. Si falla, no se publica a DynamoDB. Cualquier task que falle envía un correo
  (`email_notifications` del job, a `kjesus.apari@gmail.com`).

**Retomar donde se quedó:** *repair run* sobre el mismo `run_id` (repite solo lo fallido y lo que
depende de ello). Ej.: `databricks jobs repair-run <run_id> --rerun-all-failed-tasks`.

**`run_id` (2026-10-19, Kevin):** es el **`job.run_id` de Databricks**. El bundle se lo pasa a las tres
tasks con `{{job.run_id}}`: nadie lo crea ni lo pasa a mano, y un repair run conserva el mismo id. El
run de MLflow lo guarda como tag.

### 3.2 Feature store

* De naturaleza de ML. Son **dos**, uno por pipeline:

| | Offline | Online |
|---|---|---|
| **Dónde** | Unity Catalog (Databricks), una tabla Delta: `smartestate.feature_store_offline.spots_fs_offline`, en `s3://<lake>/feature_store_offline/` | DynamoDB (AWS), una tabla por corrida |
| **Lo escribe** | El entrenamiento | La materialización (§3.4) |
| **Lo usa** | El entrenamiento | La inferencia |
| **Papel** | Fuente de verdad para el entrenamiento | Fuente de verdad para la inferencia (tiempo real) |

* **Tabla del offline: una sola, Spots FS offline** (`spots_fs_offline`, llave primaria `spot_id`),
  para todos los spots:
  * El data pipeline la reescribe completa en cada corrida, con **`cluster_id` nulo** (la columna existe
    desde el inicio para que el esquema no cambie entre corridas y se conserve la PK; 2026-10-21, Kevin). Escribe:
    * la columna `spot_id`
    * las columnas de filtrado (`sector_id`, `modality_mask`,
    `latitude`, `longitude`). `modality_mask` es el encoding de `modality` que hace el FE (§3.2.1); `modality` se descarta.
    * las features de la cascada. Se guarda la **columna guardada**; las **columnas involucradas** son
      sus insumos y no se guardan sueltas:

      | Parte | Feature | Columna guardada | Columnas involucradas |
      |---|---|---|---|
      | **3A · Tabular** | Vector tabular, uno por sector, escalado y normalizado a largo 1 | `tab_vector` (`ArrayType`; nulo en Land) | **Industrial (8):**<br>• `area_sqm`<br>• `maintenance_cost`<br>• `charging_ports`<br>• `building_status` → `status_0`, `status_1`, `status_2`, `status_3` (one-hot, ver abajo)<br>• `natural_light`<br>**Office (7):**<br>• `area_sqm`<br>• `maintenance_cost`<br>• `security_type` → `security_0`, `security_1`, `security_2`, `security_3` (multi-hot, ver abajo)<br>• `number_of_elevators`<br>**Retail (7):**<br>• `area_sqm`<br>• `maintenance_cost`<br>• `parking_spaces`<br>• `building_status` → `status_0`, `status_1`, `status_2`, `status_3` (one-hot, ver abajo)<br>**Land:** sin vector tabular |
      | **3A · Texto** | Embedding SBERT (`paraphrase-multilingual-mpnet-base-v2`, 768 dimensiones), L2-normalizado | `nlp_vector` (`ArrayType`; nulo sin descripción, D3) | • `title`<br>• `description`<br>• `floor_material` (solo Industrial, D18) |
      | **3B · Precio** | Precio por m², crudo (`f(r)` es un cociente) | `rent_price_sqm`, `sale_price_sqm` | • `rent_price_sqm`<br>• `sale_price_sqm` |

    * `is_candidate`.
  * El modeling pipeline le agrega `cluster_id` al final (feature de 3C · Diversidad). Solo los
    candidatos lo tienen; los demás, nulo.
  * **Por qué una sola:** los dos subpipelines corren siempre juntos (§3.1), así que separar
    `cluster_id` en otra tabla no protege las features. Una tabla es menos que mantener y rastrear.
* **Tabla del online: `spots_fs_online_<run_id>`** (DynamoDB, una por corrida), para todos los spots. Es copia
  completa de Spots FS offline (`publish_table`, §3.4): mismas columnas, nada se agrega, salvo la llave interna de Databricks.

  | Columna | Tipo en DynamoDB | La usa en el clic |
  |---|---|---|
  | `_feature_store_internal__primary_keys` | String (llave; contiene `spot_id`) | `GetItem`, `BatchGetItem` |
  | `sector_id`, `modality_mask`, `latitude`, `longitude` | Number | Filtros 1 y 2 (del clickeado) |
  | `tab_vector` | List de Number | 3A |
  | `nlp_vector` | List de Number | 3A |
  | `rent_price_sqm`, `sale_price_sqm` | Number | 3B |
  | `cluster_id` | Number (nulo si no es candidato) | 3C |
  | `is_candidate` | Boolean | — (ya aplicado en el GeoIndex) |

* **Versión (P31):** cuando el servicio carga una corrida, ¿cómo se asegura de que el GeoIndex y
  las features en DynamoDB sean de la misma corrida? Usando el mismo `run_id` en todos
  lados. Sin esto, el rollback de §4 no funciona.
  * S3: K-means y GeoIndex en `runs/<run_id>/`.
  * Online: `spots_fs_online_<run_id>` en DynamoDB, una tabla por corrida.
  * Offline: Spots FS offline sigue como tabla única con el historial de Delta. La corrida registra en
    MLflow la versión que usó.
  * Se conservan las **últimas 3 corridas** (S3 y DynamoDB); las más viejas se borran.
* **Por qué se versiona en el online y no con el historial de Delta:** el rollback (§4) lee DynamoDB,
  no el offline. Además, la retención de Delta va por tiempo (7 días por defecto para los archivos
  borrados) y, tras un VACUUM, no se puede volver a esa versión
  ([Delta history](https://docs.databricks.com/aws/en/delta/history)). No asegura las últimas 3.

#### 3.2.1 Feature engineering

Etapa del data pipeline que produce las features de Spots FS offline (§3.2).

**`security_type` (multi-hot):** en el CSV es una lista de códigos (1, 2, 3; un spot puede tener
varios). El FE la abre en una columna binaria por código. El vacío es una categoría propia,
`security_0` = "sin dato", y no queda nulo. Ejemplos: `[1, 3]` → `0 1 0 1`; vacío → `1 0 0 0`.

**`modality` → `modality_mask` (multi-hot de 2 bits en un entero):** `Rent` = 1 (`01`),
`Sale` = 2 (`10`), `Rent & Sale` = 3 (`11`). Se codifica **solo aquí**: el GeoIndex (§4.1) y la
inferencia leen la columna ya codificada, así que no hay dos codificaciones que se desalineen.

**`tab_vector`** (medido sobre los 1.280 candidatos de `data/`):

| Columna | Cómo llega | Transformación | Por qué |
|---|---|---|---|
| `area_sqm`, `maintenance_cost` | skew 5–10; `maintenance_cost` con 48–69 % de ceros | `log1p` → escalar | Baja el skew a ~0–2; `log1p` acepta ceros |
| `charging_ports`, `parking_spaces` | skew 5–9; máx. 202 y 300 | `log1p` → escalar | Mismo motivo |
| `number_of_elevators` | rango 1–6, skew 1,4 | escalar directo | Ya es compacta |
| `natural_light` | 1–100 (%), 65 % nulos | `/ 100` | Ya acotada: queda en [0, 1] |
| `building_status` | {1, 2, 3}; el 1 es el 90 %+; 32–84 % nulos | one-hot con `status_0` = "sin dato" | Sin orden documentado (`metadatos.pdf`: "Status del espacio"); un ordinal inventaría distancias |

* **Escalador:** `StandardScaler`, **uno por sector** (fit en el FE). No `RobustScaler`: con
  58–69 % de ceros en `maintenance_cost`, su IQR queda ≈ 0 y no escala esa columna.
* **Cómo se implementa (2026-10-20, Kevin):** un **Pipeline de `pyspark.ml` por sector**. El `.fit()`
  aprende media y desviación y MLflow registra el `PipelineModel` entero (`mlflow.spark.log_model`, uno
  por sector: autolog con tres fits en el mismo run choca con los params; 2026-10-21, Kevin).
  Sin JSON hecho a mano: reaplicarlo es `model.transform(df)`. El multi-hot de `security_type` va con
  un `SQLTransformer`.
* **Fit solo con candidatos (2026-10-21, Kevin):** outliers y copias moverían media y desviación. El
  `transform` se aplica a todos los spots del sector.
* **Nulos:** quedan en **0 después de escalar** (= media del sector). Se implementa con un `Imputer`
  (media) antes del `StandardScaler`, dentro del `PipelineModel` (2026-10-21, Kevin). Costo: en las
  columnas con muchos nulos la desviación sale algo menor. Sin artefacto extra ni banderas.
  `natural_light` nulo → 0. En `building_status` el nulo ya es categoría (`status_0`).
* **Orden:** `log1p` → `StandardScaler` → nulo = 0 → + one-hot y `natural_light / 100` →
  normalización L2.
* **Office sin `building_status` (2026-10-20, Kevin):** casi no informa (84 % nulos; 74 de 75 = 1).
  Su one-hot solo diría si el campo está lleno, y eso inflaría el coseno entre oficinas sin dato.
  Se quita de ese sector: el vector de Office queda en 7.

### 3.3 Salidas

| Salida | Tipo | Sale de |
|---|---|---|
| Spots FS offline (`spots_fs_offline`) | Tabla Delta en Unity Catalog | Feature engineering (features) y modelado (`cluster_id`) |
| Feature store online: `spots_fs_online_<run_id>` | Tabla en DynamoDB, una por corrida | Materialización |
| `PipelineModel` de FE por sector (`pyspark.ml`, incluye el `StandardScaler`, §3.2.1). Solo en MLflow, para auditar y comparar corridas: la inferencia no lo usa | Modelo de MLflow | Feature engineering |
| K-means | Artefacto serializado | Modelado |
| GeoIndex (BallTree + `spot_id`, `sector_id`, `modality_mask`) | Artefacto serializado | Modelado |
| Parámetros del re-ranker (`reranker_params.json`) | JSON | Modelado (copia del repo) |

**GeoIndex:** se construye con los candidatos de Spots FS offline (`is_candidate = True`) ordenados por
`spot_id`. Copia las columnas sin transformarlas. La clase vive en un paquete propio que instalan
Databricks y la imagen de ECS (joblib la necesita para cargar el objeto). Detalle: §4.1.

**Parámetros del re-ranker** (`w_tab`, `w_nlp`, `f_bounds`, `δ`):

```
reranker_params.yml (repo) ──bundle──▶ modeling pipeline ──┬──▶ runs/<run_id>/reranker_params.json  (lo lee ECS)
                                                           └──▶ MLflow log_params                   (comparar corridas)
```

* Se editan **solo en el repo** (PR). Cambiarlos exige una corrida nueva.
* El código del re-ranker no se serializa: vive en el servicio.

| Parámetro | Etapa | Qué hace | Cómo se usa |
|---|---|---|---|
| `w_tab`, `w_nlp` | 3A · Similitud | Reparten el peso entre lo tabular y el texto | `sim = w_tab · cos(tab) + w_nlp · cos(nlp)`. Sin texto, solo cuenta `w_tab` |
| `f_bounds` (inicial `[0.7, 1.1]`) | 3B · Precio | Acota cuánto premia o castiga el precio | `score = sim · f(r)`, `f(r)` dentro de la cota: el precio reordena entre parecidos, no sube a uno que no se parece |
| `δ` (inicial `0.85`) | 3C · Diversidad | Castiga repetir `cluster_id` en el top N | `score · δ^n`, `n` = cuántos de ese cluster ya entraron. Menor `δ`, más variado |

No van en el JSON: `k` del K-means (es del entrenamiento) ni el radio, el mínimo y el tope de los
Filtros 1 y 2 (pendientes).

**Archivos:** `runs/<run_id>/` en S3 (`geo_index.joblib`, `reranker_params.json` y el K-means), registrados en MLflow.

**Registro:** los artefactos se registran y versionan con **MLflow**.

### 3.4 Materialización: offline → online

Es la integración entre los dos feature stores. Corre después del modeling pipeline, porque necesita
`cluster_id`, y solo si este termina bien (si falla, ver §3.1). Dos pasos:

1. **Publicar:** Spots FS offline → `spots_fs_online_<run_id>` en DynamoDB, con `publish_table` de Databricks (parámetro
   `table_name`,
   [API](https://api-docs.databricks.com/python/feature-engineering/latest/ml_features.online_store_spec.html)).
2. **Limpiar (P31, Kevin 2026-11-04):** en DynamoDB (`spots_fs_online_*`) y en S3 (`runs/<run_id>/`) quedan las últimas 3
   corridas **más el último champion**, para no borrar la que está en producción. Los champions anteriores se borran.
   Solo se limpia si la tabla nueva pasó el smoke test. **El orden es por fecha real** (`CreationDateTime` en DynamoDB,
   fecha de los archivos en S3): el `run_id` de Databricks no crece con el tiempo.

**Nombre en DynamoDB (verificado):** `AmazonDynamoDBSpec(table_name="spots_fs_online_<run_id>")` fija el nombre
online, distinto del offline. Sin `table_name`, usa la base y el nombre de la tabla offline.

**No agrega columnas:** cuando corre, Spots FS offline ya tiene todo: lo del data pipeline (`spot_id`,
filtrado, features, `is_candidate`) y el `cluster_id` del modeling. `publish_table` publica la tabla
completa, tal como está.

**Una sola lectura por spot:** features y `cluster_id` están en la misma fila, de la misma corrida. En
cada clic no se une nada en el servicio.

Lo que dice la documentación de Databricks
([Publish features to a third-party online store](https://docs.databricks.com/aws/en/machine-learning/feature-store/publish-features)):

* DynamoDB está soportado en todas las versiones del cliente de Feature Engineering en Unity Catalog.
* `publish_table()` crea la tabla en DynamoDB: no se crea a mano.
* Requiere cluster clásico en modo **Dedicated**. La policy de jobs ya lo fija (`SINGLE_USER`,
  `infra/modules/dbx_compute_policies/main.tf`).
* Para escribir, recomienda un **instance profile** con permiso sobre DynamoDB (`modules/dynamodb_publish_profile`).
* Verificado en el paquete 0.18.3 (2026-11-04): crea la tabla con cobro `PAY_PER_REQUEST`, y la llave es `json.dumps([spot_id])`.
* Modo merge sí; **overwrite no** está soportado para DynamoDB.
* En DynamoDB la llave se guarda combinada en la columna `_feature_store_internal__primary_keys`, como
  `"[spot_id]"`. **`spot_id` no existe como columna aparte** (verificado 2026-11-04): la inferencia lo saca de esa llave.

**Embedding SBERT (P32), corregido 2026-11-04:** se publica sin problema, pero **no como JSON**: `ArrayType` queda
como lista nativa de números de DynamoDB (paquete 0.18.3 y smoke test de la corrida `669016108687066`). `boto3` la
devuelve como `Decimal`, y el servicio debe convertirla a vector.

---

## 4. Pipeline de inferencia

**Corre en AWS, en producción.** Tolerancia de respuesta: **hasta 1 segundo** por clic.

**Cómo se sirve:** un contenedor propio en **ECS Fargate**, siempre encendido. Al arrancar carga en RAM
el GeoIndex y los parámetros del re-ranker. En cada clic lee las features del feature store online (DynamoDB). Lambda se descarta
porque cada arranque en frío recargaría el GeoIndex desde S3.

**Qué corrida carga (P20):** la resuelve el **CD**, no el servicio.

* El entrenamiento registra la corrida en MLflow (Databricks); la aprobada lleva el **tag de corrida**
  `champion=true` (2026-10-22, Kevin). Tag y no alias: los alias de MLflow son del Model Registry, y
  aquí no se registra ningún modelo. Una sola corrida lleva el tag a la vez.
* Al desplegar, el CD busca en MLflow la corrida con `champion=true`, toma su `run_id` y lo pasa al contenedor como
  variable de entorno. El CD ya tiene credenciales de Databricks: despliega los bundles.
* El servicio nunca habla con MLflow: lee el GeoIndex y `reranker_params.json` de `runs/<run_id>/` en S3 y las
  features de la tabla `spots_fs_online_<run_id>` en DynamoDB.
* **Rollback:** se mueve el tag a la corrida anterior y se vuelve a desplegar. No se publica nada: la tabla de esa corrida
  ya existe (se conservan las últimas 3, §3.2). GeoIndex, parámetros y features son siempre de la misma
  corrida.

**Fuente de verdad:** el feature store online.

**Entrada:** el `spot_id` del spot clickeado.

**Supuesto:** el spot clickeado ya está en el feature store online. Lo normal es leer sus features de ahí,
sin reprocesar.

**Flujo:**

```
spot_id ──▶ FS online ─────▶ Filtro 1 ─▶ Filtro 2 ─▶ Nivel 3 ──────▶ Nivel 3C ─────▶ top N spot_id
            (sus features)    zona        tipo +     re-ranker       corte por
                              geográfica  modalidad  sim · f(r)      diversidad
```

1. Se buscan las features del `spot_id` en el feature store online (ver casuística).
2. Se filtra (Filtros 1 y 2, §4.1) y se ordena con el re-ranker (Nivel 3, §4.1). **Los filtros se
   numeran por orden de ejecución, no como negocio (`business_tech_req.md` §2):** Filtro 1 = zona,
   Filtro 2 = tipo y modalidad.
3. **Nivel 3C:** se arma el top N con un corte greedy que penaliza repetir `cluster_id`:
   `score(B) · δ^n_c(B)`, donde `n_c(B)` es cuántos del cluster de B ya entraron. No toca `sim`: si el
   clustering sale mal, el top N degrada al orden del re-ranker. Es aritmética sobre una columna ya
   guardada; el K-means no corre en el clic.

**No corre ningún procesamiento de datos.** Todos los spots ya pasaron por el data pipeline
en el entrenamiento y sus features están en el feature store online. La inferencia solo lee features y corre
la cascada (lógica del modelo, no procesamiento). Los artefactos de FE (scaler, encoder) no se usan.

**Casuística del spot clickeado:**

| Caso | Qué pasa | Qué hace la inferencia |
|---|---|---|
| **Normal** | El spot está en el feature store y al día | Lee sus features directo |
| **Outlier** (`is_candidate = False`) | El spot está en el feature store, pero no puede ser recomendado | Igual que el normal (ver regla) |

**Casos no verosímiles** (no se atienden):

* **Alta, baja o modificación reciente:** la data es estática (§1).
* **`spot_id` inválido:** es un error del front y no depende del back end.
* **`Complex` clickeado:** el ejercicio solo considera spots Single y Subspace (`metadatos.pdf`, D2).
  Un `Complex` está en el feature store pero no se puede clickear. Salvedad: si entra al alcance,
  su área (la del edificio entero) distorsiona el re-ranker; la salida sería compararlo solo por texto.
* **Excluido por la ingesta:** los spots en cuarentena (fuera de México o con sector desconocido)
  tampoco existen en el front. El data pipeline no descarta ninguna otra fila.

**Regla de `is_candidate`:** separa dos papeles.

* **Pedir recomendaciones** (el spot clickeado): cualquier spot del feature store.
* **Ser recomendado:** solo los spots con `is_candidate = True`. El filtro se aplica en el
  entrenamiento (§3.1): el K-means y el GeoIndex solo contienen candidatos, así que la inferencia no
  filtra nada. El spot clickeado no necesita estar en el índice: sus features salen del feature store.

**Copias de un mismo spot:** se resuelven en el data pipeline. Las copias se marcan con
`is_candidate = False`, salvo una que queda como representante.

* La inferencia no cambia.
* No hace falta una columna nueva: se reutiliza `is_candidate`, que ya excluye outliers.
* El GeoIndex queda más chico.
* Las copias se pueden seguir clickeando, solo que no se recomiendan.

**Requisitos del feature store online:**

* Consultas rápidas.
* Filtrados rápidos: tipo de inmueble (`sector_id`), modalidad (`modality_mask`) y zona geográfica (`latitude`, `longitude`).
* Guardar las columnas que usa el filtrado.
* Guardar todo lo que usa la cascada, ya transformado.
* Guardar **todos** los spots, no solo los candidatos (como `gold`, D17).

**Salida:** un JSON con el top N de inmuebles parecidos, **solo sus `spot_id`**, ordenados por
relevancia después de toda la cascada. Contrato: ver API.

**Fuera de alcance:** el `spot_id` se pasa al front end, que busca los spots en su base de datos.

> ⚠️ **Supuesto fuerte:** las bases de datos del back end y del front end están sincronizadas y al
> día. Si no lo están, un `spot_id` del top N puede no existir en el front y dar error.

**API (FastAPI):** tres endpoints, uno de negocio y dos operativos.

| Endpoint | Para qué |
|---|---|
| `GET /recommendations/{spot_id}?n=10` | Top N de `spot_id` parecidos. `n` es opcional, con un tope |
| `GET /health` | OK solo si el GeoIndex y los parámetros ya están cargados (para el ALB) |
| `GET /version` | El `run_id` cargado y los parámetros del re-ranker (para verificar despliegues y rollbacks) |

```
GET /recommendations/20381?n=10
200: {"run_id": "abc123", "spot_ids": [20365, 10322, ...]}
404: el spot_id no existe en el FS online
```

**Por qué `GET`:** es una consulta sin efectos secundarios, con un solo dato de entrada. Se puede
cachear y probar desde el navegador.

**Fargate y DynamoDB:** DynamoDB no vive en Fargate. Es un servicio aparte que el contenedor consulta
por la red.

```
Tu VPC                                      AWS (fuera de tu VPC)
┌──────────────────────────────┐            ┌──────────────────────────┐
│ Fargate                      │            │ DynamoDB                 │
│  └─ Docker: FastAPI          │ ── HTTPS ─▶│  spots_fs_online_<run_id>│
│      + GeoIndex en RAM       │  (por VPC  │  (gestionado por AWS)    │
└──────────────────────────────┘  endpoint) └──────────────────────────┘
```

| | Fargate | DynamoDB |
|---|---|---|
| Qué es | Donde corre tu código (el contenedor) | Una base de datos que AWS opera por ti |
| Quién la administra | Tú defines la imagen; AWS pone la máquina | AWS completo: servidores, réplicas, backups |
| Qué contiene | FastAPI y el GeoIndex en RAM | Las features de todos los spots |

**Por eso funciona bien:**

* Si se cae una tarea de Fargate, los datos no se pierden: están en DynamoDB.
* Las 2 tareas leen la misma tabla.
* DynamoDB ya replica los datos en 3 zonas de disponibilidad. No hay que montar nada para su
  tolerancia a fallos.

**En una línea:** Fargate corre tu servidor y DynamoDB es un servicio aparte al que tu servidor
consulta por la red.

**Cómo se conecta:** con **boto3**. DynamoDB es una API de AWS sobre HTTPS: no hay contraseñas.

| Pieza | Qué es | Dónde se define |
|---|---|---|
| SDK | `boto3` dentro de la imagen | `requirements` de la imagen |
| Permisos | **IAM task role**: ECS entrega credenciales temporales y boto3 las toma solo. Mínimo: `GetItem` y `BatchGetItem` sobre `spots_fs_online_*` | Terraform |
| Red | Subnet privada + **VPC gateway endpoint** de DynamoDB (sin NAT, sin costo) | Terraform |
| Qué tabla | El nombre se arma con el `run_id`, que llega como variable de entorno | Task definition |

El cliente de boto3 se crea **una vez** al arrancar y se reutiliza en cada clic. Nunca hay llaves en la
imagen.

### 4.1 Filtros 
#### Filtro 1 y 2 (zona geográfica + tipo de inmueble y modalidad):

* Los Filtros 1 y 2 los resuelve **un solo artefacto, `GeoIndex`**, en RAM. 
* La **tabla** es el feature store online (DynamoDB): de ahí salen los datos del spot clickeado.
* **Qué es `GeoIndex`.** Una clase propia (Python, sobre scikit-learn y numpy) que guarda juntos:

| Pieza | Tipo | Contenido |
|---|---|---|
| `tree` | `BallTree` (haversine) | `latitude`, `longitude` de los candidatos, en radianes |
| `spot_id` | `int64` | Llave para leer DynamoDB (es `LongType` en el contrato) |
| `sector` | `int8` | `sector_id` tal cual (9, 11, 12, 15) |
| `modality_mask` | `uint8` | Encoding del FE (§3.2.1) |

El método relevante aqui sera **query()**

**Qué pasa en un clic:**

```
spot_id clickeado
   │
   ▼
FS online (GetItem) ──▶ lat, lon, sector_id, modality_mask
   │
   ▼
geo_index.query(lat, lon, r, sector, mask) ──▶ [20365, 10322]   ← un artefacto, todo en RAM
```

1. `GetItem` en DynamoDB trae `latitude`, `longitude`, `sector_id` y `modality_mask` del clickeado. Sale
   de DynamoDB y no del índice porque el clickeado puede no ser candidato (outlier, copia).
2. `query` aplica el Filtro 1 y después el Filtro 2, y devuelve `spot_id`.
3. El servicio quita el `spot_id` clickeado del resultado (si es candidato, el árbol lo devuelve a
   distancia 0).
4. Los `spot_id` que quedan van al `BatchGetItem` de features y al re-ranker.

#### Filtro 1: zona geográfica

Dentro de `query`: `tree.query_radius(lat, lon, r)` devuelve las posiciones de los candidatos a ≤ `r` km.

#### Filtro 2: tipo de inmueble y modalidad

**Regla de negocio**: un candidato pasa solo si es del **mismo tipo de
inmueble** y **comparte modalidad** con el spot clickeado.

* **Tipo de inmueble**  = `sector_id`
* **Modalidad = `modality`**: `Rent`, `Sale` o `Rent & Sale`.



| | Qué |
|---|---|
| **Entra** | `spot_id` clickeado → del FS online: `latitude`, `longitude`, `sector_id`, `modality_mask` |
| **Sale** | Lista de `spot_id` que pasan los dos filtros, sin el clickeado. Ej.: `[20365, 10322]` |


#### Re-ranker (Nivel 3)

* Ordena la lsta de inmuebles que resultaron del nivel 1 y 2 y devuelve el top N. Tiene **tres partes**, en este orden:

| Parte | Qué decide | Insumo (de DynamoDB) | Modelo entrenado |
|---|---|---|---|
| **3A · Similitud** | Quién se parece más | Vector tabular y embedding SBERT | No: es una fórmula |
| **3B · Precio `f(r)`** | Quién va primero entre parecidos | Precio por m² | No: es una fórmula |
| **3C · Diversidad** | Que el top N no sean clones | `cluster_id` | Sí: el K-means, pero corre en el entrenamiento |

**Dónde corre:** las tres son código Python con numpy dentro del servicio de inferencia (ECS),
sobre los candidatos que trajo el `BatchGetItem`. Ninguna llama a un modelo en el clic.

#### 3A · Similitud

```
sim(A, B) = w_tab · cos(A_tab, B_tab) + w_nlp · cos(A_nlp, B_nlp)
```

* `A` es el spot clickeado y `B` cada candidato.
* `cos` es la similitud coseno: más alto = más parecido.
* **Vector tabular** (`_tab`): uno **por sector**, con dimensiones distintas (Industrial 8, Office 7,
  Retail 7). Como el Filtro 2 deja solo candidatos del mismo sector, `A` y `B` siempre tienen las
  mismas dimensiones. **Land** no tiene vector tabular: su `sim` usa solo texto.
* **Embedding** (`_nlp`): SBERT sobre título + descripción, L2-normalizado.
* **Sin descripción (D3):** el embedding se guarda en **nulo**. Un embedding nulo significa "sin texto"
  y no hace falta otra columna. Los pesos se renormalizan sobre las señales que tienen **los dos** spots:
  `sim = Σ(wᵢ · cosᵢ) / Σ(wᵢ)`. Así `sim` sigue en `[0, 1]`.
* **Cómo se guardan:** el vector tabular y el embedding van cada uno en una columna `ArrayType`
  (`tab_vector`, `nlp_vector`). El largo de `tab_vector` depende del sector.
* **Cómo se calcula:** el FE deja los vectores normalizados a largo 1, así que el coseno es un producto
  punto. Con numpy, todos los candidatos a la vez: `matriz_candidatos @ vector_clic`.
* `w_tab` y `w_nlp`: parámetro a calibrar (Next steps 1).

#### 3B · Precio `f(r)`

```
r = precio_m²(B) / precio_m²(A)        score = sim · f(r)
```

* `r > 1` (más caro): `f(r) < 1`, penaliza. `r ≈ 1`: neutro. `r < 1` (más barato): bonus leve que se
  aplana.
* `f(r)` está acotado (inicial `[0.7, 1.1]`) para que el precio no voltee el orden entre un candidato
  relevante y uno que no lo es. El EDA muestra que esa cota se satura: se calibra (Next steps 2).
* **Qué precio:** el de la modalidad compartida con el clic (`rent_price_sqm` o `sale_price_sqm`).
* **Si los dos son `Rent & Sale` (P34):** se promedian los dos factores,
  `f = (f(r_renta) + f(r_venta)) / 2`. Si a alguno le falta un precio, se usa solo el que tienen los
  dos. Por qué: no se sabe si el usuario busca rentar o comprar, y elegir uno sería inventarle la
  intención. Es la misma lógica que D3 (usar las señales presentes en ambos) y sigue dentro de la cota.

#### 3C · Diversidad

Se ordena por `score` y se arma el top N con un corte greedy que penaliza repetir cluster:
`score(B) · δ^n_c(B)`, donde `n_c(B)` es cuántos del cluster de `B` ya entraron.

* **El K-means no corre aquí.** Se entrena en el modeling pipeline y su resultado, el `cluster_id` de
  cada candidato, ya viene en DynamoDB. En el clic es aritmética sobre esa columna.
* No toca `sim`: si el clustering sale mal, el top N degrada al orden de 3B.
* `δ` (inicial 0.85) y `k` del K-means: parámetros a calibrar (Next steps 6).


**Columnas por subnivel:**

| Subnivel | Columnas | De quién |
|---|---|---|
| 3A · Similitud | `tab_vector`, `nlp_vector` | Clickeado + candidatos |
| 3B · Precio | `rent_price_sqm`, `sale_price_sqm`, `modality_mask` | Clickeado + candidatos |
| 3C · Diversidad | `cluster_id` | Solo candidatos |

`modality_mask` hace falta en 3B para saber qué precio comparar (renta, venta o ambos, P34).

**Flujo completo**

```
spot_id clickeado
   │
   ▼
GetItem ──▶ TODO lo del clickeado de una vez:            ← lectura 1
            lat, lon, sector_id, modality_mask,
            tab_vector, nlp_vector, rent_price_sqm, sale_price_sqm
   │
   ▼
geo_index.query(...) ──▶ [20365, 10322, ...]             ← RAM, sin lectura
   │
   ▼
BatchGetItem (con proyección) ──▶ tabla de candidatos:   ← lectura 2
            spot_id, tab_vector, nlp_vector,
            rent_price_sqm, sale_price_sqm, modality_mask, cluster_id
   │
   ▼
Nivel 3 (3A → 3B → 3C) ──▶ top N
```

**Por qué así:**

* **El clickeado se lee una sola vez.** Si su `GetItem` trae todo, no hace falta volver a DynamoDB para
  3A y 3B.
* **La proyección** pide solo esas siete columnas y no la fila entera. Ahorra ancho de banda y unidades
  de lectura.
* **`BatchGetItem` admite hasta 100 ids por llamada.** Si hay más candidatos, se parte en tandas que
  corren en paralelo. Conviene fijar un tope de candidatos, que es parte del pendiente "Radio del
  Filtro 1 y su tope", para que la latencia quede acotada.

**En una línea:** el `GetItem` del clickeado trae todo de una vez, y un `BatchGetItem` con proyección de
siete columnas arma la tabla del Nivel 3.

---

## 5. Tecnologías

Esto es lo que define hoy este documento, separado en las dos etapas.

**Entrenamiento (Databricks)**

| Paso | Tecnología |
|---|---|
| Orquestación | Databricks Jobs, definidos con Asset Bundles |
| Procesamiento de datos | PySpark (Spark) |
| Feature store offline | Unity Catalog (tabla Delta) + Databricks Feature Engineering |
| Embeddings de texto | SBERT (sentence-transformers): `paraphrase-multilingual-mpnet-base-v2`, revisión `4328cf2`, 768 dim. Se lee de `s3://<lake>/models/sbert/` (sin internet) |
| Clustering | scikit-learn K-means |
| Índice geográfico | scikit-learn BallTree (haversine), envuelto en `GeoIndex` |
| Cálculo en memoria | numpy |
| Serialización | joblib |
| Registro y versionado | MLflow (corridas, tag `champion=true`) |
| Artefactos | S3 (`runs/<run_id>/`) |
| Materialización al online | `publish_table` de Databricks hacia DynamoDB |
| Validación de esquema | `pyspark.sql.types` (contratos en `src/data_pipeline/contract/`) |
| Desarrollo | Workspace de Databricks (Git folder + cluster `dev`). Sin Spark en la laptop; los tests de Spark corren en el job (task `unit_tests`) hasta que exista CI |

**Inferencia (AWS)**

| Paso | Tecnología |
|---|---|
| Contenedor | Docker (imagen con FastAPI + el paquete del proyecto) |
| Servicio | Contenedor en ECS Fargate, con la imagen en ECR |
| API | FastAPI |
| Feature store online | DynamoDB (`GetItem`, `BatchGetItem`) |
| Artefactos | S3: `geo_index.joblib`, `reranker_params.json` |
| Carga del índice | joblib + scikit-learn (BallTree) |
| Filtros y re-ranker | Python + numpy |

**Transversal:** Terraform (infraestructura). Código en GitHub.

> **Regla: el código no sabe dónde corre.** `src/` son módulos PySpark planos: sin notebooks como
> código, sin `dbutils`, y rutas y tablas por parámetro. Databricks orquesta y ejecuta; la lógica no
> depende de él.
> **Única excepción (2026-10-22, Kevin):** `src/data_pipeline/storage.py` usa `dbutils.fs.cp` para
> copiar el modelo SBERT de `models/` al disco local del cluster. Si el código sale de Databricks, se
> cambia solo esa función.

**Para después:** CI/CD (GitHub Actions) y monitoreo.

---

## Next steps

**Pendientes para pasar a implementar** (próxima sesión, en este orden):

*Arquitectura:* cerrada.

*Parámetros del modelo (valor inicial, se calibran al implementar):*

1. Pesos `w_tab` / `w_nlp` del re-ranker.
2. Cota de `f(r)`: `[0.7, 1.1]` se satura según el EDA.
3. Radio del Filtro 1 (5 → 10 → 15 km) y su tope.
4. N por defecto.
5. Esquema del feature store: columnas exactas. ~~Modelo SBERT concreto~~ → cerrado 2026-10-19: `paraphrase-multilingual-mpnet-base-v2` (768 dim).
6. K-means: `δ` (inicial 0.85) y `k` (5–8, por codo o silhouette), con `random_state` fijo.
