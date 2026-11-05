# Tests

**En una línea:** arma 8 spots de prueba, los pasa por el feature engineering con un SBERT falso y revisa que salga exactamente lo que dice el contrato. Lo mismo, a futuro, para el modelado (T2) y la publicación a DynamoDB (T3).

## Para qué

Para encontrar errores del código en segundos y gratis, antes de pagar un cluster en Databricks.

## Cómo

1. Docker arma una máquina igual a la del cluster (Python, Java, Spark).
2. Se crean 8 spots inventados: oficinas, industriales y locales, con y sin descripción.
3. Se procesan con un SBERT falso: rápido y sin bajar el modelo real.
4. Se compara el resultado con lo esperado. Si algo no cuadra, el test falla.

## Qué revisa

Todo está en `test_training_pipeline.py`, una sección por paso del entrenamiento.

**T1 · datos → feature store**

| Test | Pregunta que responde |
|---|---|
| `schema_and_validation` | ¿Las columnas y sus tipos son los del contrato? |
| `tab_vector_dims_and_norm` | ¿Cada sector tiene un vector del largo correcto? |
| `scaler_fits_on_candidates_only` | ¿La escala se calcula sin los outliers? |
| `null_is_zero_after_scaling` | ¿Un dato vacío queda en 0? |
| `binaries` | ¿La seguridad de una oficina pasa a sí/no? |
| `modality_mask` | ¿Renta, venta y ambos quedan como 1, 2 y 3? |
| `nlp_vector` | ¿Sin descripción no hay vector; con descripción, sí? |
| `cluster_id_is_null` | ¿El grupo queda vacío para que lo llene el modelado? |
| `validate_aborts_on_repeated_id` | ¿Se detiene si un spot aparece dos veces? |
| `fs_offline_ddl` | ¿La tabla se crea con su llave y tipos correctos? |
| `encode_with_progress_keeps_order` | ¿Codificar SBERT por bloques conserva el orden de los textos? |
| `stage_records_time_and_keeps_the_error` | ¿Una etapa que falla queda medida y el error sigue subiendo? |
| `sample_resources_logs_machine_state` | ¿El monitoreo registra CPU y memoria durante la etapa? |
| `spark_probes_measure_each_factor` | ¿Corren todas las sondas de diagnóstico? |
| `spark_probes_never_fail_the_run` | ¿Una sonda que falla deja terminar T1? |

**T2 · modelado y T3 · DynamoDB** — T3 todavía sin código: sus tests se saltan solos hasta que exista.

- **T2, en una línea:** ubica 5 spots inventados alrededor de un clic y revisa que solo salgan los cercanos,
  del mismo tipo y con la misma modalidad; además, que los grupos del K-means salgan bien y siempre iguales.
- **T3, en una línea:** le da una lista de corridas viejas y revisa que se borren las correctas y queden las
  3 más nuevas.
- **Lo que no se prueba aquí:** publicar en DynamoDB y leer de vuelta. Eso necesita AWS real y se revisa
  en cada corrida, dentro del código.

| Test | Pregunta que responde |
|---|---|
| `geo_index_radius_sector_and_modality` | ¿Solo salen los cercanos, del mismo tipo y con modalidad compartida? |
| `geo_index_rent_and_sale_click_matches_both` | ¿Un spot de renta y venta encuentra a los dos tipos? |
| `geo_index_roundtrip` | ¿El índice guardado y vuelto a abrir responde igual? |
| `kmeans_k_by_silhouette_and_deterministic` | ¿Elige bien la cantidad de grupos y siempre da lo mismo? |
| `spot_without_text_goes_to_its_tab_neighbours` | ¿Un spot sin texto cae con los que se le parecen en datos? |
| `spot_without_text_does_not_train_the_model` | ¿Los spots sin texto quedan fuera del entrenamiento? |
| `spread_counts_rows_with_and_without_text` | ¿Se cuenta bien cuántos con y sin texto hay por grupo? |
| `land_clusters_on_text_alone` | ¿Land se agrupa solo con texto? |
| `candidate_without_any_vector_aborts` | ¿Se detiene si un candidato no tiene ningún vector? |
| `sector_with_too_few_rows_aborts` | ¿Se detiene si un sector tiene muy pocas filas? |
| `cluster_id_only_for_candidates` | ¿Solo los candidatos reciben grupo? |
| `cleanup_keeps_last_three_by_number` | ¿Se borran las tablas viejas y quedan las 3 últimas? |
| `cleanup_ignores_folders_that_are_not_runs` | ¿La limpieza respeta carpetas que no son corridas? |

## Cuándo y cómo correrlo

Antes de cada deploy, desde la raíz del repo:

```bash
docker build -f tests/Dockerfile -t smartestate-tests .
docker run --rm smartestate-tests
```

En Databricks corren otra vez, como primer paso del job (`unit_tests`).
