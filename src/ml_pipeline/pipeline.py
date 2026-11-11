"""
T2 · Modeling pipeline: Spots FS offline -> K-means, GeoIndex, re-ranker params and cluster_id.

    Read candidates -> K-means per sector -> GeoIndex -> Artifacts -> MLflow -> cluster_id

cluster_id is written last: if anything fails before, the table keeps T1's output and a repair run
repeats this task alone (new_arch.md §3.1).

Run (Databricks, bundles/data/resources/data_pipeline_job.yml):
    run_modeling.py --run-id {{job.run_id}} --runs-root s3://<lake>/runs \
        --fs-table smartestate.feature_store_offline.spots_fs_offline \
        --experiment /Shared/smartestate-train
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import mlflow
import yaml
from pyspark.sql import SparkSession

from src.common import logs, run_monitor
from src.common.run_monitor import stage
from src.common.tracking import log_environment, start_run
from src.ml_pipeline import modeling, storage

logger = logging.getLogger(__name__)

RERANKER_PARAMS = Path(__file__).resolve().parents[2] / "config" / "reranker_params.yml"
LOCAL_DIR = Path("/local_disk0/smartestate/modeling")

# Spark's default is 200, for a table of ~2,500 rows (data_pipeline.md). Set per task, as T1 does.
SHUFFLE_PARTITIONS = 8


def run(spark: SparkSession, args: argparse.Namespace) -> dict[str, float]:
    """Returns the run's metrics."""
    run_dir = f"{args.runs_root.rstrip('/')}/{args.run_id}"
    reranker_params = yaml.safe_load(RERANKER_PARAMS.read_text())

    with stage("read_candidates"):
        candidates = storage.read_candidates(spark, args.fs_table)

    with stage("kmeans_fit"):
        result = modeling.cluster_candidates(candidates)
    with stage("build_geo_index"):
        geo_index = modeling.build_geo_index(candidates)

    with stage("write_artifacts"):
        storage.write_joblib(spark, geo_index, LOCAL_DIR / "geo_index.joblib", f"{run_dir}/geo_index.joblib")
        storage.write_joblib(spark, result.models, LOCAL_DIR / "kmeans.joblib", f"{run_dir}/kmeans.joblib")
        storage.write_joblib(spark, result.reductions, LOCAL_DIR / "pca.joblib", f"{run_dir}/pca.joblib")
        storage.write_json(spark, reranker_params, LOCAL_DIR / "reranker_params.json", f"{run_dir}/reranker_params.json")

    metrics = {"modeling_candidates": len(candidates), **result.metrics}
    with start_run(args.experiment, args.run_id, args.git_commit):
        mlflow.log_params(reranker_params)
        mlflow.log_params(modeling.hyperparameters())
        log_environment(spark)  # T2 may run alone on a new cluster (repair run)
        mlflow.log_metrics(metrics)
        # Shows whether rows without text pile up in a few clusters (plan 0.4).
        mlflow.log_dict(result.spread, "cluster_spread.json")
        mlflow.set_tag("artifacts_uri", run_dir)

        with stage("write_cluster_id"):
            clusters = spark.createDataFrame(result.clusters, "spot_id long, cluster_id int")
            # localCheckpoint: the write must not depend on the table it overwrites.
            fs = modeling.with_cluster_id(spark.table(args.fs_table), clusters).localCheckpoint()
            fs_version = storage.write_fs_offline(fs, args.fs_table)

        mlflow.set_tag("fs_offline_version", fs_version)
        mlflow.log_metrics({f"duration_{name}_s": seconds for name, seconds in run_monitor.durations().items()})
    return {**metrics, "fs_offline_version": fs_version}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="T2 · Spots FS offline -> K-means, GeoIndex and cluster_id")
    parser.add_argument("--run-id", required=True, help="Databricks {{job.run_id}}")
    parser.add_argument("--runs-root", required=True, help="e.g. s3://<bucket>/runs")
    parser.add_argument("--fs-table", required=True)
    parser.add_argument("--experiment", required=True, help="MLflow experiment path")
    parser.add_argument("--git-commit", required=True, help="${bundle.git.commit}: code the bundle was deployed from")
    args = parser.parse_args(argv)

    logs.configure()
    spark = SparkSession.builder.getOrCreate()
    spark.conf.set("spark.sql.shuffle.partitions", SHUFFLE_PARTITIONS)
    run_monitor.log_environment(spark)
    with run_monitor.sample_resources():
        try:
            metrics = run(spark, args)
        finally:
            run_monitor.log_summary()  # also when the run fails: shows how far it got
    logger.info("modeling: %s", " · ".join(f"{k} {v}" for k, v in metrics.items()))


if __name__ == "__main__":
    main()
