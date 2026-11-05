# Rendimiento de T1 (data_pipeline)

**Fuente de verdad de la demora de T1.** Cada corrida, hallazgo y decisión se registra aquí.
Los logs crudos viven en `logs/` (no versionado); lo que debe conservarse se copia a este documento.

| | |
|---|---|
| **Meta de Kevin** | T1 completo en ≤15 min, ojalá 10 |
| **Presupuesto** | Máquina de hasta el triple de costo y menos de 1 USD/hora |
| **Estado** | Corrida 4: **18,0 min**. Faltan ~3 min para la meta de 15 |
| **Pregunta abierta** | ¿Qué hace la JVM en los ~1.500 ms que preparaba una task con el plan de `gold`, y por qué en Docker no ocurre? Ya no bloquea: `localCheckpoint` eliminó ese costo |

## Reporte ejecutivo (2026-11-04)

### Cuál era el cuello de botella

**Spark gastaba casi todo su tiempo preparando tasks, no procesando datos.** En la corrida 2 sumó 6.259 s-task de
preparación contra 214 de trabajo real: ~26 de sus 28,6 min de reloj. Dos factores se multiplicaban:

```
Costo unitario   Cada task que lee el cache de gold carga su plan: ~1,5 s de preparación
       ×
Multiplicador    200 particiones por defecto, para solo 2.500 filas (~12 por partición)
       ×
Repetición       Muchas acciones de validación vuelven a leer esos caches
       =
~26 min preparando tasks
```

- **Costo unitario, medido con sondas:** 8 tasks con el plan de `gold` tardan 1.260–1.752 ms cada una; 200 sin ese
  plan, 5–10 ms; el mismo dato tras un shuffle, 16 ms.
- **No era:** RAM, disco, GC ni SBERT. Tampoco el número de tasks por sí solo.
- **Sin confirmar:** qué ocurre dentro de esos ~1,5 s y por qué no pasa en Docker.

### Por qué bajó de 43 a ~18,6 min (20 con sondas)

| Tramo | Corrida 2 (min) | Corrida 3 (min) | Diferencia (min) |
|---|---:|---:|---:|
| Spark | 28,6 | 3,2 | **−25,4** |
| Crear cluster | 5,4 | 5,4 | 0 |
| `unit_tests` | 2,9 | 3,1 | +0,2 |
| SBERT | 4,4 | 4,6 | +0,2 |
| MLflow | 1,4 | 1,7 | +0,3 |
| Otros | ~0,4 | ~0,6 | +0,2 |
| **Total sin sondas** | **43,1** | **~18,6** | **−24,5 (−57 %)** |

**El único cambio de código entre esas corridas que afecta a T1 fue `spark.sql.shuffle.partitions` de 200 a 8.** Las
sondas corren al final y se excluyen. Se usó la misma máquina y los conteos no cambiaron.

La caída desde 1 h 54 min hasta 43 min vino antes, con otros cambios juntos: más máquina, arranque más corto y
`pip_requirements`. Esa parte no se puede repartir con exactitud porque la corrida 1 no tenía tiempos por etapa.

### Qué hacer para bajar a 15 min

Plan actualizado con la corrida 4, en "Siguientes pasos". Resumen: Spark ya quedó en 1,4 min, así que la ganancia
restante está en SBERT (~4,6 min) y en el arranque, las librerías y los tests (~9,4 min).

**Riesgo:** crear el cluster tardó 11,4 min en la corrida 1 y 5,4–5,9 en las siguientes. Una repetición del caso
lento se comería toda la ganancia.

## Historial de corridas

| # | Fecha | Run | Máquina | Cambio probado | Resultado | Total |
|---|---|---|---|---|---|---|
| 1 | 2026-11-03 | `652070532920363` | `m5d.large` (2 vCPU, 8 GB) | Primera corrida | SUCCESS | **1 h 54 min** |
| – | 2026-11-04 | `1020156852167102` | `r5d.xlarge` | `cluster_log_conf` a DBFS | FAILED en 2 s, sin crear cluster | – |
| 2 | 2026-11-04 | `726424880789805` | `r5d.xlarge` (4 vCPU, 32 GB) | Máquina, `pip_requirements`, monitoreo | SUCCESS | **43 min** |
| 3 | 2026-11-04 | `304335789918515` | `r5d.xlarge` | `shuffle.partitions = 8` + sondas | SUCCESS, `spots_fs_offline` v5 | **20 min** (~18,6 sin sondas) |
| 4 | 2026-11-04 | `508031956134800` | `r5d.xlarge` | torch con 4 hilos, `localCheckpoint`, sin sondas | SUCCESS, `spots_fs_offline` v6 | **18,0 min** |

### Tiempos por tramo (min)

| Tramo | Corrida 1 | Corrida 2 | Corrida 3 | Corrida 4 |
|---|---:|---:|---:|---:|
| Crear cluster | 11,4 | 5,4 | 5,4 | 5,9 |
| `unit_tests` | 6,6 | 2,9 | 3,1 | 3,5 |
| Spark (etapas abajo) | sin desglose | 28,6 | 3,2 | **1,4** |
| SBERT: bajar, cargar y encode | sin desglose | 4,4 | 4,6 | 5,3 |
| MLflow | ~7 | 1,4 | 1,7 | 1,5 |
| Arranque de la task y otros | – | ~0,4 | ~0,6 | ~0,5 |
| **Total sin sondas** | **~114** | **43,1** | **~18,6** | **18,0** |

La corrida 1 no tenía tiempos por etapa: sus ~88 min antes de MLflow no están desglosados. Las sondas de la
corrida 3 suman 85 s: 25 s de experimentos y 60 s de espera final.

### Corrida 1 · log

`logs/2026-11-03_t1_run_652070532920363.log`

```
data_pipeline  start=20:07:41Z end=21:43:30Z setup=1s   exec=5747s SUCCESS
unit_tests     start=19:49:42Z end=20:07:41Z setup=682s exec=396s  SUCCESS
CREATING 19:49:43 · RUNNING 20:00:51 · DRIVER_HEALTHY 20:01:25
DRIVER_NOT_RESPONDING 21:31:22 · DRIVER_HEALTHY 21:32:24 · TERMINATING 21:43:31
Primer log de MLflow 21:36:23 · inferencia de pip fallida en los 3 modelos: 5 min 45 s
10 tests en 171,76 s
```

### Corrida · error de logs del cluster

```
INVALID_PARAMETER_VALUE: Invalid cluster log storage info, please refer to the API doc
about what storage is supported for cluster log delivery.
```

### Corrida 2 · log

`logs/t1_run_726424880789805/`

```
unit_tests     start=16:51:39Z end=16:59:53Z setup=321s exec=173s  SUCCESS
data_pipeline  start=16:59:53Z end=17:34:46Z setup=1s   exec=2091s SUCCESS
CREATING 16:51:40 · RUNNING 16:56:50 · DRIVER_HEALTHY 16:57:51 · TERMINATING 17:34:47
spots_fs_offline v3: rows 2500 · quarantine_rows 0 · with_description 2092
sbert: torch 2.14.1+cpu, 2 threads · 2092/2092 texts · 215 s · 9.7 texts/s
```

| Etapa | Duración (s) | CPU % | `iowait` % | Carga | RAM libre mín. (GB) | Tasks | Trabajo (s-task) | Preparación (s-task) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ingestion | 17,7 | 63 | 0 | 3,8 | 18,1 | 24 | 5 | 1 |
| cleaning | 124,3 | 85 | 0 | 6,4 | 15,2 | 430 | 30 | 397 |
| preprocessing | 294,7 | 91 | 0 | 6,6 | 14,8 | 1003 | 37 | 1090 |
| sbert_fetch | 34,4 | 27 | 0 | 5,9 | 15,0 | – | – | – |
| sbert_load | 13,2 | 30 | 0 | 4,5 | 14,7 | – | – | – |
| fe_tabular_fit | 392,8 | 90 | 0 | 7,3 | 14,2 | 1236 | 30 | 1457 |
| fe_sbert_collect | 126,1 | 91 | 0 | 7,4 | 14,2 | 410 | 6 | 489 |
| fe_sbert_encode | 214,8 | 58 | 0 | 4,7 | 13,6 | – | – | – |
| fe_sbert_to_spark | 0,4 | – | – | – | – | – | – | – |
| fe_security_check | 61,7 | 89 | 0 | 4,5 | 13,7 | 206 | 3 | 237 |
| fe_validate | 485,2 | 92 | 0 | 6,7 | 13,7 | 1408 | 24 | 1874 |
| write_quarantine | 5,0 | – | – | – | – | 5 | 2 | 0 |
| write_fs_offline | 111,2 | 83 | 0 | 7,4 | 14,0 | 235 | 44 | 351 |
| candidate_stats | 94,8 | 88 | 0 | 7,5 | 13,9 | 201 | 4 | 354 |
| mlflow_log_models | 85,9 | 20 | 0 | 5,2 | 13,9 | 18 | 7 | 0 |
| otros | – | – | – | – | – | 119 | 22 | 9 |
| **Total** | **2062,1** | | | | | **5295** | **214** | **6259** |

Las columnas de tasks son una foto de la API de Spark a las 17:34 UTC, unos 40 s antes del fin; pueden faltar las
últimas tasks. Incluyen también los stages de `unit_tests`, que comparte la aplicación de Spark y las etiquetas,
y no se pueden separar: los JSON crudos se perdieron porque un comando los sobrescribió con el cluster ya apagado.
El capturador actual ya no puede hacerlo: solo reemplaza un archivo con una respuesta válida.

### Corrida 3 · log

`logs/t1_run_304335789918515/` (métricas de Spark, tasks e hilos en `spark_api/`)

```
unit_tests     start=17:51:32Z end=17:59:56Z setup=321s exec=183s SUCCESS · 15 tests en 63,07 s
data_pipeline  start=17:59:56Z end=18:11:30Z setup=1s   exec=692s SUCCESS
CREATING 17:51:33 · RUNNING 17:56:43 · DRIVER_HEALTHY 17:56:51
DRIVER_NOT_RESPONDING 18:03:50 · DRIVER_HEALTHY 18:04:50 (durante el encode de SBERT) · TERMINATING 18:11:31
spots_fs_offline v5: rows 2500 · quarantine_rows 0 · with_description 2092 · candidatos 625 / 484 / 160 / 16
sbert: torch 2.14.1+cpu, 2 threads · 2092/2092 texts · 231 s · 9.1 texts/s
spark.sql.shuffle.partitions=8 · spark.sql.adaptive.enabled=true · sin errores en las sondas
```

Solo stages de `data_pipeline`:

| Etapa | Duración (s) | Tasks | Trabajo (s-task) | Preparación (s-task) | Preparación p50 (ms) |
|---|---:|---:|---:|---:|---:|
| ingestion | 17,1 | 24 | 4 | 1 | 16 |
| cleaning | 27,2 | 46 | 22 | 22 | 210 |
| preprocessing | 24,6 | 43 | 12 | 44 | 1046 |
| sbert_fetch | 31,7 | – | – | – | – |
| sbert_load | 14,8 | – | – | – | – |
| fe_tabular_fit | 43,9 | 54 | 11 | 71 | 1228 |
| fe_sbert_collect | 9,5 | 17 | 1 | 30 | 1554 |
| fe_sbert_encode | 230,6 | – | – | – | – |
| fe_sbert_to_spark | 0,4 | – | – | – | – |
| fe_security_check | 5,2 | 9 | 0 | 10 | 1237 |
| fe_validate | 31,3 | 64 | 9 | 90 | 1444 |
| write_quarantine | 5,0 | 5 | 2 | 0 | 12 |
| write_fs_offline | 23,1 | 21 | 15 | 18 | 69 |
| candidate_stats | 6,5 | 9 | 1 | 17 | 1703 |
| mlflow_log_models | 100,3 | 30 | 11 | 2 | 12 |
| **Total** | **571,4** | **322** (incl. otros) | **88** | **304** | |

### Corrida 3 · sondas (mismo cluster y mismas filas)

| Sonda y stage | Tasks | Preparación p50 (ms) | Trabajo p50 (ms) |
|---|---:|---:|---:|
| `plain_8` | 8 | 6 | 43 |
| `plain_200` | 200 | 5 | 9 |
| `cache_200_read` | 200 | 10 | 12 |
| `lineage_8_read` en `unit_tests`, con plan trivial | 2 | 7 | 45 |
| **`lineage_8_read`, lee el cache de `gold`** | **8** | **1752** | **9** |
| **`lineage_200_fill`, lee el cache de `gold`** | **8** | **1260** | **90** |
| `lineage_200_fill`, tras el shuffle | 200 | 16 | 16 |
| `lineage_200_read`, cache de `gold` ya repartido | 200 | 17 | 8 |

### Corrida 4 · log

`logs/t1_run_508031956134800/`

```
unit_tests     start=18:37:08Z end=18:46:31Z setup=351s exec=212s SUCCESS · 15 tests en 76,75 s
data_pipeline  start=18:46:32Z end=18:55:09Z setup=1s   exec=516s SUCCESS
CREATING 18:37:09 · RUNNING 18:42:45 · DRIVER_HEALTHY 18:42:50 · TERMINATING 18:55:10 · sin DRIVER_NOT_RESPONDING
spots_fs_offline v6: rows 2500 · quarantine_rows 0 · with_description 2092 · candidatos 625 / 484 / 160 / 16
sbert: torch 2.14.1+cpu, 4 threads · 2092/2092 texts · 267 s · 7.8 texts/s
```

| Etapa | Corrida 3 (s) | Corrida 4 (s) | Preparación corrida 3 (s-task) | Preparación corrida 4 (s-task) |
|---|---:|---:|---:|---:|
| ingestion | 17,1 | 18,7 | 1 | 0 |
| cleaning | 27,2 | 27,4 | 22 | 4 |
| preprocessing | 24,6 | 6,1 | 44 | 0 |
| sbert_fetch | 31,7 | 33,6 | – | – |
| sbert_load | 14,8 | 14,0 | – | – |
| fe_tabular_fit | 43,9 | 9,1 | 71 | 0 |
| fe_sbert_collect | 9,5 | 0,8 | 30 | 0 |
| **fe_sbert_encode** | **230,6** | **267,2** | – | – |
| fe_security_check | 5,2 | 1,0 | 10 | 0 |
| fe_validate | 31,3 | 5,2 | 90 | 1 |
| write_quarantine | 5,0 | 5,1 | 0 | 0 |
| write_fs_offline | 23,1 | 11,3 | 18 | 0 |
| candidate_stats | 6,5 | 1,1 | 17 | 0 |
| mlflow_log_models | 100,3 | 87,5 | 2 | 0 |
| **Total** | **571,4** | **488,5** | **304** | **6** |

Durante el encode de la corrida 4: CPU 89 %, carga 7,6. En la corrida 3: CPU 62 %, carga 4,0.

### Referencia en Docker (2 CPU, 8 GB; SBERT, S3 y Delta simulados)

| Prueba | Spark total | Preparación por task |
|---|---:|---:|
| Con 200 particiones | 47 s | no medida |
| Con 8 particiones, sin sondas | ~26 s | ~1 ms (total del run, sondas incluidas) |

## Qué sabemos

| Hallazgo | Evidencia | Certeza |
|---|---|---|
| Cortar el plan elimina el costo de preparar tasks | `localCheckpoint`: de 304 a 6 s-task; Spark de 3,2 a 1,4 min, con los mismos conteos | Medido (comparando corridas) |
| torch con 4 hilos empeoró SBERT en este nodo | 7,8 contra 9,1 textos/s, con más CPU y carga; una sola comparación | Medido. **Revertido** |
| Bajar a 8 particiones fue la mayor ganancia | Spark de 28,6 a 3,2 min, con las mismas filas y conteos | Medido (comparando corridas) |
| El costo viene de tasks que llevan el plan de `gold`, no del número de tasks | 200 tasks sin ese plan: 5–10 ms; 8 tasks con él: 1.260–1.752 ms | Medido (sondas) |
| Tras un shuffle, el mismo dato deja de costar | En la misma sonda: 1.260 ms al leer `gold` y 16 ms tras el shuffle | Medido (sondas) |
| La corrida 2 fue lenta por multiplicación | 200 particiones repitieron un costo de ~1,2 s por task en miles de tasks | Inferido, consistente con lo medido |
| El tiempo de Spark se fue en preparar tasks | Corrida 2: 6.259 contra 214 s-task. Corrida 3: 304 contra 88 | Medido |
| No es RAM, disco ni GC | RAM libre ≥13,6 GB, `iowait` 0 %, sin swap; GC 8 s en la corrida 3 | Medido |
| SBERT es ahora la etapa más larga | Encode de 230,6 s a 9,1 textos/s, con 2 hilos de torch | Medido |
| `pip_requirements` ahorró tiempo en MLflow | ~7 min → 1,4–1,7 min, ya sin "Inferring pip" | Medido |
| El costo aparece en Databricks y no en Docker | ~1.500 ms contra ~1–25 ms por task | Medido, con entornos distintos |
| Este costo explica buena parte de los 88 min de la corrida 1 | 6.259 s-task entre 2 vCPU darían ~52 min | **Inferido** |

### Veredicto de las hipótesis

| Hipótesis | Prueba | Veredicto |
|---|---|---|
| Costo fijo por task | `plain_8` 6 ms, `plain_200` 5 ms | ❌ Descartada |
| Contención con muchas tasks | `plain_200` 5 ms | ❌ Descartada |
| El cache lo encarece | `cache_200_read` 10 ms | ❌ Descartada |
| Lo encarece el plan que carga la task | `lineage_8_read`: 7 ms con plan trivial, 1.752 ms con `gold` | ✅ Confirmada |

## Qué falta confirmar

**¿Qué hay dentro de esos ~1.500 ms?** Alrededor de 1 s es CPU (`lineage_8_read`: 1.037 ms). Los volcados de
hilos dan pistas, no una conclusión: de 17 muestras preparando tasks, 7 pasan por Janino, 6 por carga de clases
y 4 por búsquedas en jars (`ZipFile`), con clases de Databricks en el stack. Faltan muestras, y el stack
recortado puede clasificar mal algunas. **Tampoco está confirmado por qué en Docker no ocurre.** Ya no vale la
pena investigarlo por rendimiento: con `localCheckpoint`, esta preparación bajó a 6 s-task.

**¿Por qué MLflow tarda ~1,5 min?** Solo ~10 s son de Spark y la CPU va al 15 %, así que parece espera de red o de
la API. No está medido.

**¿Son librerías los ~2,3 min de `unit_tests` fuera de pytest?** 212 s de ejecución contra 77 s de pytest. Es inferido.

**Resultado de la corrida 3 contra su criterio:** Spark bajó de 28,6 a 3,2 min (criterio: ≤5) sin alterar filas ni
conteos (2.500 filas, 2.092 con descripción, candidatos 625 / 484 / 160 / 16). ✅ Validado.

## Decisiones

| Fecha | Decisión | Por qué |
|---|---|---|
| 2026-11-04 | Job en `r5d.xlarge`; `dev` en `m5d.large` | Tope de costo; 0,288 contra 0,113 USD/h en EC2 (2,55 veces, sin DBU) |
| 2026-11-04 | `pip_requirements` explícitos | Evita la inferencia fallida |
| 2026-11-04 | Sin `cluster_log_conf` | Databricks rechazó DBFS; Volume o S3 requieren permisos nuevos |
| 2026-11-04 | `spark.sql.shuffle.partitions = 8`, fijado en `pipeline.py` | 2 por vCPU; 2.500 filas no necesitan 200 particiones |
| 2026-11-04 | Sondas activas por parámetro del job (`spark_probes`) | Medir solo tras terminar T1. **Volver a `false` al cerrar el diagnóstico** |
| 2026-11-04 | Captura externa de métricas | No toca el código de producción y ya funcionó por el proxy del driver |
| 2026-11-04 | `localCheckpoint` en lugar de `cache` en `bronze`, `silver`, `gold` y `features` | Guarda las filas y corta el plan que encarece cada task. Se pierde si cae el nodo, igual que la corrida en un solo nodo |
| 2026-11-04 | torch con todos los vCPU: probado y **revertido** | La corrida 4 fue más lenta en SBERT. El código ya no lo tiene; entra con el próximo deploy, y aún no se corrió en Databricks |
| 2026-11-04 | Pausa en ~18 min | Kevin cierra por ahora. El reuso de embeddings queda como siguiente paso, sin decisión |
| 2026-11-04 | `spark_probes` en `false` | Hipótesis resueltas |

## Cómo correr y medir

```bash
# Terminal 1: lanzar T1
bash scripts/bundle.sh data run data_pipeline -t dev --params ingest_date=2026-09-26

# Terminal 2: capturar Spark, justo después de lanzar. Termina solo.
./ss/bin/python scripts/t1_spark_watch.py watch latest

# Al terminar, antes de shut_down.sh
bash scripts/t1_logs.sh <run_id>
./ss/bin/python scripts/t1_spark_watch.py report <run_id>
```

Para comparar con corridas anteriores, usa la suma de las etapas sin `probe_*`: las sondas alargan la corrida 3
hasta ~7 min, incluida una espera final de 60 s.

## Siguientes pasos

**Base estimada: ~17,4 min**, los 18,0 medidos menos los ~0,6 que costó torch con 4 hilos. Faltan ~2,4 min.
Los ahorros y totales son **estimados, sin medir**:

| Opción | Ahorro | Total | Costo o riesgo |
|---|---:|---:|---|
| Reusar embeddings de textos sin cambios | ~4,6 min | ~12,8 min desde la segunda corrida | Estado nuevo que mantener; la primera corrida no mejora |
| Imagen con librerías y sacar `unit_tests` del job | ~3,6 min | ~13,8 min | Mantener la imagen; el job pierde esa puerta hasta tener CI; asume que los ~2,3 min fuera de pytest son librerías |
| Máquina de 8 vCPU | ~2 min | ~15,4 min | `c5d.2xlarge` (RAM y precio por confirmar); pasa el triple por hora; no llega sola |

**Recomendación:** reusar embeddings. Es la mayor ganancia, no quita la puerta de tests y escala con el inventario.
Requiere cerrar el diseño antes de escribir código.

**Bajar de 10 min:** arranque, librerías y tests ya suman ~9,4 min. No es realista sin pool o serverless.

## Escalabilidad (estimación, sin medir)

Proyección con millones de filas desde la corrida 3, con 2 hilos de torch. **Nunca se ha corrido T1 con ese volumen.**

| Tramo | Cómo crece | 2.500 filas | 1 M de filas, misma máquina | 10 M de filas, misma máquina |
|---|---|---:|---:|---:|
| Arranque y tests | Fijo | ~9,4 min | ~9,4 min | ~9,4 min |
| SBERT encode (`toPandas` en el driver) | Lineal | 3,8 min | ~30 h | ~13 días, y no cabría en memoria |
| Spark: trabajo real | Lineal o algo más (joins y shuffles) | ~0,4 min | ~2–3 h | Probablemente no cabe en 4 vCPU |

- **No es exponencial:** es casi lineal, pero una línea con esa pendiente sigue siendo inviable.
- **El límite es SBERT:** 1 M de textos a ~9 textos/s son ~30 h en este nodo, si la RAM del driver alcanzara.
- **Se rompe antes por memoria:** `toPandas`, `createDataFrame` y `localCheckpoint` concentran todo en un solo nodo.

**Lo que cambiaría a esa escala:** reusar embeddings (solo se calculan los textos nuevos), SBERT distribuido con
`pandas_udf` o en GPU, un cluster con workers en lugar de un solo nodo, y `shuffle.partitions` acorde al volumen
(8 es solo para este tamaño). Con eso, ~1 M de filas podría correr en decenas de minutos a pocas horas, por validar.

## Mejoras tentativas

Ideas registradas, sin decisión ni diseño.

### Pipeline en tiempo real (Kevin, 2026-11-04)

**Idea:** además del lote, un pipeline que reaccione a cada cambio en raw. Procesa un inmueble en milisegundos y
agrega, actualiza o elimina su fila en el feature store según el caso.

| Caso | Acción en el feature store |
|---|---|
| Inmueble nuevo | Agregar |
| Inmueble modificado | Actualizar |
| Inmueble dado de baja | Eliminar |

**Por resolver si se retoma:**
- **Latencia:** los cambios sin texto pueden apuntar a milisegundos. Un texto nuevo pasa por SBERT, que en lote va a
  ~9 textos/s en CPU (~100 ms por texto), sin contar la carga del modelo.
- **Consistencia con el lote:** en línea se aplican los modelos ya entrenados (escaladores, K-means). Falta definir
  cuándo se reentrenan y cómo se actualiza `is_candidate`, cuyos límites de outliers dependen de todo el lote.
- **Datos derivados:** las filas viven en la tabla offline y en DynamoDB, y además está el GeoIndex. Un cambio debe
  llegar a todos, sin dejar versiones distintas.
- **Origen de los cambios:** hoy raw son CSV en S3. Hace falta una fuente de eventos.
