"""MLflow run shared by the tasks of the training job: one run per Databricks job.run_id."""

from __future__ import annotations

import os
import platform
from importlib import metadata
from typing import TYPE_CHECKING

import mlflow

if TYPE_CHECKING:
    from pyspark.sql import SparkSession


def start_run(experiment: str, run_id: str, git_commit: str) -> mlflow.ActiveRun:
    """Reuses the MLflow run tagged with this run_id (a later task or a repair run) or creates it.

    Tags the run with the git commit the bundle was deployed from, so a model leads to its code.
    """
    if not git_commit:
        raise ValueError("git_commit is empty: deploy the bundle from a git checkout")
    mlflow.set_experiment(experiment)
    found = mlflow.search_runs(filter_string=f"tags.run_id = '{run_id}'", max_results=1, output_format="list")
    if found:
        run = mlflow.start_run(run_id=found[0].info.run_id)
    else:
        run = mlflow.start_run(run_name=f"train-{run_id}", tags={"run_id": run_id})
    mlflow.set_tag("git_commit", git_commit)
    return run


def move_champion_tag(experiment: str) -> None:
    """
    Tags the active run champion=true and removes the tag from any other run. Only a mirror: the champion
    lives in runs/champion.json, because MLflow's history is lost when the workspace is destroyed.
    """
    active = mlflow.active_run().info.run_id
    client = mlflow.MlflowClient()
    for run in mlflow.search_runs(
        experiment_names=[experiment], filter_string="tags.champion = 'true'", output_format="list"
    ):
        if run.info.run_id != active:
            client.delete_tag(run.info.run_id, "champion")
    mlflow.set_tag("champion", "true")


def package_versions() -> list[str]:
    """Every installed Python package as name==version, sorted: the runtime's and the job's alike."""
    found = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            found.setdefault(name.lower(), f"{name}=={dist.version}")
    return [found[key] for key in sorted(found)]


def log_environment(spark: SparkSession) -> None:
    """
    Runtime and exact packages of this cluster, in the active MLflow run (criterion 4).

    The runtime key ends in ".x": Databricks applies maintenance patches on its own. packages.txt shows
    whether a patch changed anything between two runs.
    """
    mlflow.set_tags({
        "databricks_runtime": os.environ.get("DATABRICKS_RUNTIME_VERSION", "unknown"),
        "databricks_runtime_key": spark.conf.get("spark.databricks.clusterUsageTags.sparkVersion", "unknown"),
        "spark_version": spark.version,
        "python_version": platform.python_version(),
    })
    mlflow.log_text("\n".join(package_versions()) + "\n", "environment/packages.txt")
