# MLOps del pipeline de entrenamiento

> Prioridad 1 de la sesión (`HANDOFF.md` §9). Diagnóstico completo contra el marco: mismo lugar.

## Criterio 1 · Versionamiento de código: cada modelo apunta a su commit

**Estado actual (2026-11-08): probado de punta a punta.** Corrida `317679628158877` (GitHub run `42504181466`, 18 min) en verde; MLflow tiene `git_commit = 9c7a8e2`.

```
Merge a main ──▶ solo tests en GitHub (gratis)
Entrenar     ──▶ workflow "Training" a mano ──▶ tests ──▶ deploy desde main ──▶ job ──▶ MLflow con git_commit
```

Antes, el deploy salía de la laptop (podía llevar cambios sin commit) y ni el job ni MLflow sabían de qué commit venía el modelo.

| Decisión | Por qué |
|---|---|
| El CI **no entrena** en cada merge; solo corre tests | Un cambio de comentario no debe costar 19 min de cluster |
| Deploy y entrenamiento van **juntos y a mano** | El workspace se destruye cada sesión: un deploy en el merge se perdería |
| Filtro por carpetas | Docs, inferencia o infra no disparan el workflow |
| `${bundle.git.commit}` → `--git-commit` → tag `git_commit` en las 3 tasks | Cada modelo apunta a su commit |
| `scripts/bundle.sh deploy` se rechaza con cambios sin commit | Si no, el hash no sería el código que corrió |
| SP `smartestate-cicd` con OIDC de GitHub, solo rama `main`, miembro de engineers | Sin secretos; mismos permisos que un ingeniero |
| Terraform escribe en cada apply las variables de Actions (`DATABRICKS_HOST`, `DATABRICKS_CICD_CLIENT_ID`, `DATABRICKS_JOBS_SP_ID`) | Cero pasos a mano y URL siempre al día. Variables, no secretos: son IDs; protege la federation policy |
| `DATABRICKS_HOST` se borra en `shut_down.sh` | Con el workspace apagado, el workflow falla claro en vez de usar una URL vieja |

**como correr manualmente el deploy y entrenmaiento?**
1. Levantar un  workspace en databricks (bash start_up.sh)
2. Entrar a GitHub → Actions → Training → Run workflow. 
3. Opcional: la fecha de raw/ si quieres otra. Ejecutar (la URL la pone Terraform).

Alternativamente, desde terminal local:
``` bash
gh workflow run training.yml
```
## Criterio 2 · Versionamiento de datos: cada corrida sabe qué datos usó y los conserva

**Estado actual (2026-11-08): probado de punta a punta.** Corrida `1087669519397477` en verde: MLflow registra los dos CSV (digest = ETag de S3) y el clone `spots_fs_offline_1087669519397477` (versión 18 del FS). `raw/` sin expiración aplicado en S3.

```
raw/<fecha>/*.csv ──▶ T1: MD5 de cada CSV ──▶ MLflow (dataset "raw", digest = MD5 = ETag de S3)
spots_fs_offline  ──▶ T3: DEEP CLONE spots_fs_offline_<run_id> ──▶ MLflow (dataset "training") ──▶ DynamoDB
```

| Decisión | Por qué |
|---|---|
| Un solo FS vivo + un clone congelado por corrida (`spots_fs_offline_<run_id>`) | Rollback y auditoría con los datos exactos; un *deep clone* no depende de VACUUM |
| El clone se toma en T3, justo antes de publicar | El clone es exactamente lo que quedó online; creación y limpieza en el mismo lugar |
| FIFO por fecha con N en `config/retention.yml` (`keep_runs: 5`) | N versionado y cambiable; aplica igual a `runs/`, DynamoDB y clones |
| Nunca se borran: producción, último champion y la corrida actual | Producción no se negocia (Kevin) |
| Raw: MD5 como digest en MLflow | Para un upload de una sola parte el MD5 es el ETag: identifica la versión exacta en S3 |
| `raw/` sin expiración de versiones viejas en S3 | Si alguien reemplaza un CSV, el original sigue recuperable |
| T2 pisa el tag `fs_offline_version` de T1 | Aceptado (Kevin): T2 solo agrega `cluster_id`; para reentrenar se quita esa columna |

**Recuperar un CSV exacto:** buscar la versión cuyo ETag coincide con el digest de MLflow.
```bash
aws s3api list-object-versions --bucket smartestate-lake-<cuenta> --prefix raw/ingest_date=<fecha>/data.csv
aws s3api get-object --bucket smartestate-lake-<cuenta> --key raw/ingest_date=<fecha>/data.csv --version-id <id> data.csv
```

## Criterio 3 · Seguimiento de experimentos

**Estado actual (2026-11-08):** completo; test en verde, sin corrida en Databricks todavía.

T2 registra en MLflow los hiperparámetros del K-means (`kmeans_k_min`, `kmeans_k_max`, `kmeans_seed`,
`kmeans_n_init`) desde `modeling.hyperparameters()`. Antes vivían solo en el código: dos corridas no mostraban
por qué sus clusters difieren. Se confirma en la próxima corrida.

## Criterio 4 · Reproducibilidad

**Estado actual (2026-11-08):** implementado; falta la prueba de dos corridas.

| Decisión | Por qué |
|---|---|
| Runtime fijo en `var.spark_version = "18.x-scala2.13"` (18 LTS), en vez de "el último LTS" | El runtime trae Spark, Python, Java y numpy/pandas/scipy: no salta solo a otro LTS |
| MLflow guarda `databricks_runtime`, `spark_version`, `python_version` y `environment/packages.txt` (T1 y T2) | El `.x` son parches que Databricks aplica solo: si uno cambia un paquete, se ve al comparar corridas |
| Ya cubierto por los criterios 1 y 2 | Código exacto (`git_commit`) y datos exactos (MD5 del raw y clone del FS) |

**Prueba:** dos corridas sobre los mismos datos → mismo `cluster_id` y mismos vectores, comparando sus tablas
`spots_fs_online_<run_id>` en DynamoDB.
