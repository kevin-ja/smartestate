# ARQUITECTURA DEL SISTEMA

**Especificación.** Se empieza con dos CSV crudos, se termina con un Top-10 servido en
milisegundos

- El **qué** y el **por qué** están en `business_tech_req.md` (cascada y KPIs) y `models.md` (qué se entrena)
- El **sustrato** —volumen supuesto (§1.1), stack (§3.1, §4.1), arquitectura en producción (§6)— vive en este documento

## 1. Las dos tablas de origen

- **Cada fila en cada tabla** es una publicacion de un inmueble
- `data.csv` dice *qué inmueble es y cuánto cuesta*;
- `data_at.csv` dice *cómo es por dentro*, aplicable solo para algunos tipos de inmueble

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
   │ ...                │                │ ...                    │
   └────────────────────┘                └────────────────────────┘
```

- El `spot_id` es el primary Key que une ambas tablas

- Nulos en `data_at.csv`:  hay **7 celdas vacías en promedio** por fila. Es **vacío
estructural** ( un terreno no tiene elevadores, una oficina no tiene puertos de carga), y por eso no se imputa


### 1.1 Jsutificacion de uso de Big Data tech (pyspark)

`data/` (2.500 filas) es una **muestra**. Todo se dimensiona contra el inventario proyectado.

| Magnitud | Supuesto |
|---|---|
| Inventario activo | **10⁵ – 10⁶ spots** |
| Crecimiento | ~10⁴ altas/mes |
| Ancho tras el join | ~35 columnas (22 + 11) |
| Ingesta | diaria, incremental |
| Latencia online | milisegundos (`models.md`) |
| PII | no hay. `user_id` es un anunciante, no una persona |

Si se cae —inventario real ≤10⁴— PySpark deja de justificarse y se sustituye por pandas o
DuckDB. Las capas, el contrato y la frontera online no cambian.


## 2. Existen 2 pipelines: Datos y ML

```
   ┌──────────────────────────────┐            ┌──────────────────────────────┐
   │   PIPELINE DE DATOS          │            │   PIPELINE DE ML             │
   │   smartestate_data           │   gold/    │   smartestate_ml             │
   │                              │ ─────────▶ │                              │
   │   raw → bronze → silver →────┼── gold     │   lo lee → ml/artifacts/     │
   │                              │            │                              │
   │   Ingeniería de datos        │            │   Ingeniería de ML           │
   └──────────────────────────────┘            └──────────────────────────────┘
                                        │
                                 CONTRATO DE FEATURES
                        gold no sabe que existe un modelo.
```

- Un pipeline termina, el otro empieza.
- `gold` es un producto terminado que cualquiera puede consumir. Hoy solo lo
consume el recomendador; mañana puede consumirlo un dashboard o un reporte, y ninguno de los dos
tendría que cambiar nada.

```
ENTREGA (lo que es)
───────────────────
pipeline A ──▶ gold
    │
    ▼  
  lo lee
 pipeline B

```
Lo que hay es esto:

| | Pipeline de datos | Pipeline de ML |
|---|---|---|
| **Se dispara** | Cuando llega el volcado diario | Cuando el de datos termina |
| **Corre** | Diario, siempre | Diario (`refresh`) y semanal (`retrain`) |
| **Si falla** | El de ML no arranca | El de datos ya terminó, `gold` está intacto |
| **Dueño** | Ingeniería de datos | Ingeniería de ML |
| **Producto** | `gold/` — una tabla de negocio | `ml/artifacts/` — un candidato |


## 3. Pipeline de datos

```
╔═══════════════════════════════════════════════════════════════════════════════════════╗
║  smartestate_data · Databricks Workflows · schedule + file arrival sobre raw/          ║
║  Solo datos. No entrena nada, no carga ningún modelo.                                  ║
╚═══════════════════════════════════════════════════════════════════════════════════════╝

  PASO 1 ── raw/ingest_date=YYYY-MM-DD/
            ├── data.csv          Los DOS CSV aterrizan tal cual.
            └── data_at.csv       No se tocan nunca. Copia de seguridad de todo.
                    │
                    ▼
  PASO 2 ── smartestate.bronze.spots (Delta)          ◀── AQUÍ SE UNEN, y solo aquí
            src/data_pipeline/ingestion.py

              1. Validar el header de cada tabla POR SEPARADO
                 └─ si el origen cambió de columnas → aborta
              2. Parsear a los tipos del contrato  → si un valor no parsea, aborta
              3. Unir por spot_id y proyectar  ──  join 1:1, aviso si baja de 99%
                 ├─ de data.csv se tiran:     Settlement · Corridor · Region · Address  (D6)
                 └─ de data_at.csv se tiran:  luminaries · floor_level_number · vertical_height
              4. Validar valores: spot_id repetido → aborta;
                 fuera de México / sector desconocido → cuarentena; nulos (D8) → aviso
              Detalle: src/data_pipeline/README.md

              Sale UNA SOLA TABLA de 25 columnas.
              De aquí en adelante "atributos" y "publicación" son lo mismo.
                    │
                    ▼
  PASO 3 ── smartestate.silver.spots (Delta)          Se corrigen valores
            src/data_pipeline/cleaning.py
              ├─ mapear sector 13 → 12 (Retail)
              ├─ marcar los Complex con es_candidato = False
              ├─ completar estado (entidad federativa) con el catálogo INEGI
              └─ recortar outliers de precio/m² y de área
                    │
                    ▼
  PASO 4 ── gold/sector_id=*/intencion=*/base.parquet
            src/data_pipeline/preprocessing.py
              ├─ normalizar texto (título + descripción)
              ├─ parsear el JSON de tipo_seguridad
              └─ particionar por sector × intención

  ════════════ AQUÍ TERMINA EL PIPELINE DE DATOS ════════════
  El producto es una tabla de negocio: legible, que cualquiera entiende al abrirla.
```

- El join entre `data.csv` y `data_at.csv`va en `bronze` y no más adelante ya que sino **cada etapa tendría que arrastrar dos tablas y acordarse de unirlas**. Uniéndolo desde un inicio, el resto del sistema ve un dato y ya.

### 3.1 Stack técnico

- Infra en nube
- Desarrollo en Databricks
- El proceso y el almacenamiento corren
en **Databricks**
- El código vive en GitHub.

| Pieza | Tecnología | Dónde corre |
|---|---|---|
| Transformaciones `raw → gold` | **PySpark** | Databricks|
| Almacenamiento | **Delta** (tablas managed de Unity Catalog) en `bronze`/`silver`; `gold` a definir | **S3** |
| Orquestación | Databricks Workflows | Databricks |
| Definición de jobs | **Databricks Asset Bundles** (YAML) — orquestación como código | Versionado en GitHub |
| Despliegue y disparo | **GitHub Actions** | GitHub actions (worker nativo) |
| Invocación | una *task* por etapa, apuntando a un módulo de `src/`, parametrizada por `run_id` | Databricks |
| Validación de esquema | Pyspark (pyspark.sql.types) | Databricks |
| Desarrollo | En el workspace (Git folder + cluster `dev`). Sin Spark en la laptop; `local[*]` solo en el runner de CI | Databricks |

> ### ⚠️ Guardarraíl — el código no sabe dónde corre
>
> **`src/` son módulos PySpark planos.** Sin `dbutils`, sin notebooks como código, rutas y tablas por parámetro.
> Databricks **orquesta y ejecuta**; la lógica no depende de él.
>


>
> **S3 es el sustrato compartido.** El training en AWS lee el mismo `gold/` y escribe el mismo
> `ml/artifacts/`. El contrato entre capas no cambia. Extiende lo que ya promete `§4` de
> el principio de origen: *el mismo código corre en `local[*]` sin cambios*.


### 3.2 Storage Layer

Todas las capas viven en **S3**, en el mismo bucket, con un prefijo por capa.

| Capa | Destino | Formato | Quién escribe |
|---|---|---|---|
| **raw** | `raw/ingest_date=YYYY-MM-DD/{data.csv, data_at.csv}` | CSV tal cual | aterrizaje, nunca se toca |
| **bronze** | `smartestate.bronze.spots` | tabla Delta managed, 25 columnas | `src/data_pipeline/ingestion.py` |
| **silver** | `smartestate.silver.spots` | tabla Delta managed | `src/data_pipeline/cleaning.py` |
| **gold** | `gold/sector_id=*/intencion=*/base.parquet` | parquet particionado por sector × intención | `src/data_pipeline/preprocessing.py` |

- Solo `gold` está particionado. `bronze` y `silver` son tablas Delta managed: Unity Catalog decide dónde van los archivos dentro de su carpeta; se escriben con `overwrite` completo (D7, revisada 2026-09-26).
- Las rutas entran **por parámetro** al módulo, nunca hardcodeadas: es lo que permite correr el mismo
  código en `local[*]` sobre la muestra.

---

## 4. Pipeline de ML

```
╔═══════════════════════════════════════════════════════════════════════════════════════╗
║  smartestate_ml · AWS Step Functions · dos cadencias (§4.2)                            ║
║  Consume gold/ en solo lectura. Nunca escribe en las capas del pipeline de datos.      ║
╚═══════════════════════════════════════════════════════════════════════════════════════╝

  PASO 5 ── Leer gold, pero no entero
            src/ml_pipeline/features_contract.py  ──▶  pide SOLO las columnas que declaró su contrato
            src/ml_pipeline/dataset.py            ──▶  ml/dataset/v={run_id}/   CONGELA esa lectura

            Por qué congelar: gold cambia todos los días. Sin snapshot no se
            puede reproducir un artefacto, porque no existe el input que lo produjo.
                    │
                    ▼
  PASO 6 ── Convertir el dato en números        ◀── aquí los atributos por fin pagan
            src/ml_pipeline/vectors.py · src/ml_pipeline/embeddings.py

            El vector tabular se arma DISTINTO según el sector:

              Industrial  6 dims  área · mantenimiento · puertos de carga ·
                                  estado_edificio · luz natural · material del piso
              Office      5 dims  área · mantenimiento · estado_edificio ·
                                  seguridad · elevadores
              Retail (13) 4 dims  área · estacionamientos · mantenimiento ·
                                  estado_edificio
              Land        1 dim   solo área — no arma vector, su sim va por NLP

              OJO: `estado_edificio` es la CONDICIÓN del inmueble (building_status).
              La entidad federativa (`estado`) NO entra a ningún vector: es
              diagnóstico. La geografía del modelo es solo lat/lon.

            ¿Por qué pueden tener tamaños distintos? Porque un terreno y una nave
            NUNCA se comparan entre sí: el Nivel 1 los separa antes (§6).

            Después:  escalar cada vector DENTRO de su partición  → scaler_{part}.pkl
                      calcular embeddings SBERT sobre el texto    → embeddings.npy
                    │
                    ▼
  PASO 7 ── Entrenar y construir
            src/ml_pipeline/clustering.py  ──▶  K-means    → centroides.npy + cluster_id
            src/ml_pipeline/indexes.py     ──▶  BallTree   → balltree_{part}.pkl (haversine)
                    │
                    ▼
  PASO 8 ── Medir
            src/ml_pipeline/metrics.py  ──▶  ml/reports/v={run_id}/ MIDE. No decide.

  ════════════ AQUÍ TERMINA EL PIPELINE DE ML ════════════
  El producto es un CANDIDATO: archivos listos para cargarse
  que todavía nadie está sirviendo.
```

> **Trazabilidad del artefacto: pendiente.** Qué se registra de cada corrida (input, código, versión
> de SBERT, hiperparámetros) se resolverá con **MLflow**, no a mano. Se decide **después** de cerrar el
> pipeline — hoy no hay claridad suficiente sobre cómo encaja.

### 4.1 Stack técnico — cerrado 2026-09-21

**Este pipeline corre en AWS, no en Databricks** (§3.1). Deliberado: el contrato entre capas es `S3`,
así que cada pipeline puede vivir en la plataforma que le convenga.

Rige una regla por encima de la tabla: **si un artefacto solo se puede cargar levantando Spark, está
mal elegido.** El consumidor final es la API, y la API no tiene Spark (§6).

| Pieza | Tecnología | Dónde corre | Se persiste como |
|---|---|---|---|
| Snapshot del dataset | **PySpark** | **EMR Serverless** | `parquet` en `ml/dataset/v={run_id}/` |
| Vector tabular + escalado | **scikit-learn** (`StandardScaler` por partición) | EMR Serverless (driver) | `scaler_{part}.pkl` |
| Embeddings de texto | **SBERT** (`sentence-transformers`) vía `pandas_udf` | EMR Serverless — inferencia paralela | `embeddings.npy` / parquet |
| Clustering | **Spark MLlib** k-means | EMR Serverless | `centroides.npy` + columna `cluster_id` |
| Índice espacial | **scikit-learn** BallTree (haversine), uno por partición | EMR Serverless (driver) | `balltree_{part}.pkl` |
| Métricas offline | **pandas / numpy** | EMR Serverless (driver) | `ml/reports/v={run_id}/proxies.json` |
| Orquestación | **Step Functions** — `ml_refresh` y `ml_retrain` (§4.2) | AWS |  |
| Disparo | **EventBridge**: evento S3 sobre `gold/` (encadena tras el pipeline de datos) + schedule semanal | AWS |  |
| Empaquetado | **Docker** en **ECR** — imagen con las dependencias del job | ECR |  |
| Despliegue | **GitHub Actions** → despliega la state machine y la imagen | CI |  |
| Trazabilidad | **MLflow** — *pendiente*, ver nota abajo |  |  |

**Por qué conviven Spark y scikit-learn.** Spark donde el trabajo es paralelo y masivo (embeddings,
k-means sobre 10⁶ filas); scikit-learn donde el resultado tiene que poder cargarse sin Spark
(escaladores, BallTree — MLlib ni siquiera tiene BallTree con haversine).

**Cómo se encadenan los dos pipelines.** Databricks escribe `gold/` en S3 → el evento S3 dispara
EventBridge → arranca la state machine. Ninguna plataforma llama a la otra por API: el acoplamiento
es el bucket.

> **MLflow sigue pendiente, y aquí cuesta más.** En Databricks venía integrado; en AWS hay que
> elegir: servidor MLflow propio (ECS/Fargate) o **SageMaker managed MLflow**. Se decide tras cerrar
> el pipeline, no antes.

### 4.2 Dos cadencias, no una

> **Cadencia = cada cuánto corre un flujo.** Nada más. Se usa la palabra porque aquí conviven dos.

| Flujo | Cada cuánto | Qué corre | Qué NO corre |
|---|---|---|---|
| **`ml_refresh`** | Diario, tras `smartestate_data` | Embeddings de los spots nuevos · `cluster_id` con los centroides vigentes · reconstruir BallTree | No ajusta el scaler. No reentrena k-means |
| **`ml_retrain`** | Semanal, o antes por deriva | Todo, incluido reajustar scaler y reentrenar k-means → nuevos centroides → dispara un `refresh` completo | — |

**Por qué.** Con ~10⁴ altas/mes sobre 10⁵–10⁶ spots entra **0,03%–0,3% del inventario por día**. Un
centroide es el promedio de su grupo: ese porcentaje no lo mueve.

**El argumento más fuerte no es el costo.** K-means con `random_state` fijo **no** garantiza etiquetas
estables si el input cambia: pueden permutarse. Como el Nivel 3C penaliza con `δ^n_cluster`,
reentrenar a diario haría que el Top-10 del mismo `spot_id` cambie de un día a otro **sin que haya
cambiado ni el inventario ni la consulta**.

---

## 6. La arquitectura en producción

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  OFFLINE · nadie está esperando                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝

  S3 — un bucket, un prefijo por capa
   │
   ├─ gold/sector_id=*/intencion=*/base.parquet
   │        │
   │        └──▶ pipeline de ML  (EMR Serverless · diario + semanal)
   │                  │
   │                  ▼ escribe
   └─ ml/artifacts/v={run_id}/
         │
         ├─ embeddings.npy       float32 plano, SIN pickle   ┐
         ├─ vectores_{part}.npy  tabular ya escalado         │  ESTO
         ├─ balltree_{part}.pkl  índice haversine            │  VA ONLINE
         └─ index.parquet        spot_id · cluster_id        ┘
         │
         ├─ scaler_{part}.pkl    lo usa ml_refresh           ┐  ESTO NO
         └─ centroides.npy       lo usa ml_refresh           ┘  SALE DE OFFLINE

         alias  serving/latest ──▶ v={run_id}     ◀── lo mueve CD, no el pipeline

══════════════════════════ FRONTERA DURA ══════════════════════════════════════
   S3 no se toca durante una request. Solo en el arranque.
═══════════════════════════════════════════════════════════════════════════════

╔══════════════════════════════════════════════════════════════════════════════╗
║  ONLINE · instancia de la API (una por shard de sector × intención)          ║
╚══════════════════════════════════════════════════════════════════════════════╝

   ARRANQUE (una vez)                      REQUEST (cada clic)
   ──────────────────                      ───────────────────

   sidecar lee serving/latest                  front ──┐
   y sincroniza a disco local                          │ spot_id
            │                                          ▼
            ▼                              ┌────────────────────────────────┐
   /artifacts/v={run_id}/                  │ N1  filtro sector × intencion  │
   ├─ embeddings.npy ──┐                   │       elige el shard           │
   ├─ vectores_*.npy   │                   │            ▼  ~N               │
   ├─ balltree_*.pkl   │                   │ N2  BallTree.query 5→10→15 km  │
   └─ index.parquet    │                   │            ▼  ~50              │
            │          │                   │ N3A Σ(wᵢ·cosᵢ)/Σ(wᵢ)           │
            │          └── np.memmap ──┐   │ N3B score = sim · f(r)         │
            │                          │   │ N3C argmax · δ^n_cluster       │
            │  el archivo NO se lee    │   └────────────────────────────────┘
            │  entero: el SO trae      │                │
            │  solo las páginas que    │                ▼
            │  la query toca           │      { "resultados": [id_882, …] }
            │                          │                │
            ▼                          ▼                ▼
   BallTree + index                 páginas          el front hace el lookup
   se cargan enteros  ──────────▶   de RAM           en SU base y pinta
   (pesan poco)                        │
                                       │
                            ~3 GB al tope del supuesto,
                            y solo la parte que se usa
```

### 6.1 Por qué RAM y no una base de datos

**El store online es la memoria del proceso.** S3 es el sustrato de arranque, no el store de serving.

**El algoritmo no es un lookup.** Una base de datos, un data warehouse o un feature store responden
*"dame el vector del spot 882"*. El Nivel 2 pregunta otra cosa: *"¿cuáles son los ~50 spots dentro de
5 km?"* — y un `BallTree` no corre dentro de una base de datos. Si los vectores viven afuera, cada
request cruza la red para traerse ~150 KB y recién ahí empieza a calcular.

| Opción | Por qué no |
|---|---|
| **Data warehouse** (Redshift, BigQuery, Athena) | Analítico, responde en segundos. Sirve para las métricas offline, no para servir un clic |
| **Feature store online** (DynamoDB, SageMaker FS) | ~5-10 ms, pero da una fila por id. Resuelve el lookup y no resuelve el Nivel 2 |
| **Vector database** | Tras el Nivel 1 y 2 quedan ~50 candidatos: el coseno exacto es más rápido que un índice aproximado. La ANN paga cuando se busca entre millones por query |
| **Redis / RabbitMQ** | Pondría los datos al otro lado de un socket (decisión 2026-09-21). Vuelve solo como caché de resultados `spot_id → [ids]`, delante del Nivel 1 |

**El volumen cabe.** Al tope del supuesto (10⁶ spots): embeddings SBERT 384 dims × 4 bytes × 2 textos
= 3 KB por spot → **~3 GB**. BallTree, vectores tabulares, ids y `cluster_id` suman < 150 MB. A 10⁵
son 300 MB. A la API no le entra `gold`: le entran vectores, sin texto ni direcciones.

**Cuándo se rompe.** A ~10⁷–10⁸ spots (30–300 GB). Ahí se shardea por `sector_id × intencion` — el
Nivel 1 ya filtra por ahí y `gold` está particionado igual, así que el corte sale gratis. Una vector
database recién aparecería si hubiera que buscar por similitud sobre todo el inventario **sin** el
filtro de sector.

### 6.2 Lecturas del diagrama

**La API devuelve `spot_id`, no el inmueble** (D6, resuelta 2026-09-18). El flujo arranca con un clic
sobre un inmueble **en el front**: el front ya tiene esos inmuebles. Devolver el registro completo
sería releer un dato que el consumidor ya tiene. El contrato del recomendador es *recomendar*, no
servir el catálogo.

**Consecuencia:** ninguna columna viaja como *payload*. `municipio`, `estado`, `renta_total` y
`venta_total` se conservan por otro motivo — **diagnóstico y métricas offline**. Un humano no puede
juzgar si un Top-10 tiene sentido leyendo tres enteros.

**El scaler y los centroides no cruzan la frontera.** La API no escala nada — el vector ya viene
escalado en el artefacto — y no calcula clusters: el Nivel 3C **lee** `cluster_id`. Ambos los usa
`ml_refresh` para procesar los spots nuevos, no el serving.

**El k-means vive en el pipeline de ML y `cluster_id` en online, pero el modelo no cruza.** Es el
principio de `models.md` dibujado: el modelo *prepara una feature*, no decide el ranking.

**Dos ejes distintos, conviene no mezclarlos.** El eje vertical de los §3 y §4 es el **linaje**: de
dónde sale cada dato. Esta frontera horizontal es el **contrato de latencia**: qué puede y qué no
puede ocurrir mientras un usuario espera.


---

## 7. Las dos tablas, en una línea

**Se unen una vez, al entrar** (`bronze`, Paso 2) — **y se separan una vez, al final** (Paso 6),
cuando cada sector elige qué atributos suyos entran a su vector.

```
   data.csv ─┐                                      ┌─ Industrial 6 dims
             ├─▶ JOIN ─▶ una tabla ─▶ gold ─▶ vector┤─ Office     5 dims
  data_at.csv┘   Paso 2                     Paso 6  ├─ Retail     4 dims
                                                    └─ Land       1 dim
```

---

## 8. Correspondencia con las etapas

**Pipeline de datos — `smartestate_data`**

| Paso | Tarea | Etapa | Módulo | Escribe |
|---|---|---|---|---|
| 1 | `aterrizaje_raw` | — | externo | `raw/` |
| 2 | `ingesta` | 0 | `src/data_pipeline/ingestion.py` | `bronze/` |
| 3 | `limpieza` | 2 | `src/data_pipeline/cleaning.py` | `silver/` |
| 4 | `preprocesamiento` | 3 | `src/data_pipeline/preprocessing.py` | `gold/` |

**Pipeline de ML — `smartestate_ml`**

| Paso | Tarea | Etapa | Módulo | Escribe |
|---|---|---|---|---|
| 5 | `contrato_features` | 4 | `src/ml_pipeline/features_contract.py` | — (frontera) |
| 5 | `dataset` | 4 | `src/ml_pipeline/dataset.py` | `ml/dataset/v={run_id}/` |
| 6 | `vectorizacion` | 4 | `src/ml_pipeline/vectors.py` | `ml/artifacts/v={run_id}/` |
| 6 | `embeddings` | 4 | `src/ml_pipeline/embeddings.py` | `ml/artifacts/v={run_id}/` |
| 7 | `clustering` | 5 | `src/ml_pipeline/clustering.py` | `ml/artifacts/v={run_id}/` |
| 7 | `indexado` | 5 | `src/ml_pipeline/indexes.py` | `ml/artifacts/v={run_id}/` |
| 8 | `metricas` | 6 | `src/ml_pipeline/metrics.py` | `ml/reports/v={run_id}/` |

`src/analysis/eda.py` (Etapa 1) no aparece en ningún flujo orquestado: es diagnóstico sobre la muestra, no una etapa
productiva. Corre en pandas desde el notebook.
