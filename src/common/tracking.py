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


def latest_champion_run_id(experiment: str) -> str | None:
    """job.run_id of the most recent run tagged champion=true, or None. Only that one is protected from cleanup."""
    found = mlflow.search_runs(
        experiment_names=[experiment],
        filter_string="tags.champion = 'true'",
        order_by=["attributes.start_time DESC"],
        max_results=1,
        output_format="list",
    )
    if not found:
        return None
    run_id = found[0].data.tags.get("run_id")
    if not run_id:  # deleting without knowing which run is in production could take the service down
        raise ValueError(f"Champion MLflow run {found[0].info.run_id} has no run_id tag")
    return run_id
