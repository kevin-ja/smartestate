"""MLflow run shared by the tasks of the training job: one run per Databricks job.run_id."""

from __future__ import annotations

import mlflow


def start_run(experiment: str, run_id: str) -> mlflow.ActiveRun:
    """Reuses the MLflow run tagged with this run_id (a later task or a repair run) or creates it."""
    mlflow.set_experiment(experiment)
    found = mlflow.search_runs(filter_string=f"tags.run_id = '{run_id}'", max_results=1, output_format="list")
    if found:
        return mlflow.start_run(run_id=found[0].info.run_id)
    return mlflow.start_run(run_name=f"train-{run_id}", tags={"run_id": run_id})
