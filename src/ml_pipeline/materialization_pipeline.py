"""
T3 · Materialization: Spots FS offline -> spots_fs_online_<run_id> in DynamoDB, then cleanup.

    Publish -> Smoke test -> Cleanup (DynamoDB and S3)

Nothing is deleted unless the new table passed the smoke test. A repair run repeats the whole task:
publishing merges and the cleanup recalculates, so it is idempotent.

Run (Databricks, bundles/data/resources/data_pipeline_job.yml):
    run_materialization.py --run-id {{job.run_id}} --runs-root s3://<lake>/runs --region us-east-1 \
        --fs-table smartestate.feature_store_offline.spots_fs_offline \
        --experiment /Shared/smartestate-train
"""

from __future__ import annotations

import argparse
import logging

import mlflow
from pyspark.sql import SparkSession

from src.common import logs, run_monitor
from src.common.run_monitor import stage
from src.common.tracking import latest_champion_run_id, start_run
from src.ml_pipeline import materialization, online_store, storage

logger = logging.getLogger(__name__)


class SmokeTestError(Exception):
    """The published table does not match Spots FS offline. Nothing is deleted."""


def _smoke_test(spark: SparkSession, args: argparse.Namespace, online_table: str) -> None:
    """Reads one candidate online, by the key the service uses, and compares it with its offline row."""
    expected = storage.read_smoke_row(spark, args.fs_table)
    item = online_store.get_item(online_table, materialization.online_key(expected["spot_id"]), args.region)
    errors = materialization.check_item(item, expected)
    if errors:
        raise SmokeTestError(f"{online_table}:\n  " + "\n  ".join(errors))


def _clean_up(spark: SparkSession, args: argparse.Namespace) -> dict[str, int]:
    """Same rule on both sides: the last 3 runs + the latest champion + production. This run is never deleted (repair runs)."""
    protect = {args.run_id}
    champion = latest_champion_run_id(args.experiment)
    if champion:
        protect.add(champion)
    pointer = storage.read_runs_file(spark, args.runs_root, materialization.PRODUCTION_FILE)
    if pointer is not None:
        protect.add(materialization.production_run_id(pointer))
    logger.info("cleanup: keeping the last %d runs and %s", materialization.KEEP_RUNS, sorted(protect))

    prefix = materialization.ONLINE_TABLE_PREFIX
    tables = materialization.to_drop(online_store.list_tables(prefix, args.region), prefix, protect=protect)
    for table in tables:
        online_store.delete_table(table, args.region)

    folders = materialization.to_drop(storage.list_run_folders(spark, args.runs_root), "", protect=protect)
    for folder in folders:
        storage.delete_run_folder(spark, args.runs_root, folder)

    logger.info("cleanup: deleted tables %s and run folders %s", tables, folders)
    return {"cleanup_tables_deleted": len(tables), "cleanup_run_folders_deleted": len(folders)}


def run(spark: SparkSession, args: argparse.Namespace) -> dict[str, int]:
    online_table = materialization.online_table_name(args.run_id)
    with start_run(args.experiment, args.run_id):
        with stage("publish_table"):
            online_store.publish(args.fs_table, online_table, args.region)
        with stage("smoke_test"):
            _smoke_test(spark, args, online_table)
        mlflow.set_tag("online_table", online_table)
        with stage("cleanup"):
            metrics = _clean_up(spark, args)
        mlflow.log_metrics(metrics)
        mlflow.log_metrics({f"duration_{name}_s": seconds for name, seconds in run_monitor.durations().items()})
    return metrics


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="T3 · Spots FS offline -> DynamoDB, then cleanup")
    parser.add_argument("--run-id", required=True, help="Databricks {{job.run_id}}")
    parser.add_argument("--runs-root", required=True, help="e.g. s3://<bucket>/runs")
    parser.add_argument("--region", required=True, help="AWS region of the DynamoDB tables")
    parser.add_argument("--fs-table", required=True)
    parser.add_argument("--experiment", required=True, help="MLflow experiment path")
    args = parser.parse_args(argv)

    logs.configure()
    spark = SparkSession.builder.getOrCreate()
    run_monitor.log_environment(spark)
    with run_monitor.sample_resources():
        try:
            metrics = run(spark, args)
        finally:
            run_monitor.log_summary()  # also when the run fails: shows how far it got
    logger.info("materialization: %s", " · ".join(f"{k} {v}" for k, v in metrics.items()))


if __name__ == "__main__":
    main()
