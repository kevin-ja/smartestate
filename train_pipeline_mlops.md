# MLOps del pipeline de entrenamiento

> Prioridad 1 de la sesión (`HANDOFF.md` §9). Diagnóstico completo contra el marco: mismo lugar.

## Criterio 1 · Versionamiento de código: cada modelo apunta a su commit

**Estado actual (2026-11-08):** implementado; tests en verde en Docker; sin probar en GitHub ni en Databricks.

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