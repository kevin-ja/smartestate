"""
T3 · Materialization: Spots FS offline -> spots_fs_online_<run_id> in DynamoDB, then cleanup.

    Snapshot -> Publish -> Smoke test -> Promote -> Cleanup (S3, DynamoDB and snapshots)

Promote (criterion 7): if T2's metrics.json says this run beats the champion, runs/champion.json points to
it, with champion=true in MLflow as a mirror. Only after the smoke test: a champion always has its table.
Production stays manual (make set-production-run).

The snapshot is a frozen copy of Spots FS offline, spots_fs_offline_<run_id>, logged to MLflow as the
run's dataset: exactly what this run published, kept for rollback and audits (train_pipeline_mlops.md).

Nothing is deleted unless the new table passed the smoke test. A repair run repeats the whole task:
publishing merges and the cleanup recalculates, so it is idempotent.

Run (Databricks, bundles/data/resources/data_pipeline_job.yml):
    run_materialization.py --run-id {{job.run_id}} --runs-root s3://<lake>/runs --region us-east-1 \
        --fs-table smartestate.feature_store_offline.spots_fs_offline \
        --experiment /Shared/smartestate-train
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import mlflow
import mlflow.data
import yaml
from pyspark.sql import SparkSession

from src.common import logs, run_monitor
from src.common.run_monitor import stage
from src.common.tracking import move_champion_tag, start_run
from src.ml_pipeline import evaluation, materialization, online_store, storage

logger = logging.getLogger(__name__)

RETENTION = Path(__file__).resolve().parents[2] / "config" / "retention.yml"


class SmokeTestError(Exception):
    """The published table does not match Spots FS offline. Nothing is deleted."""


def _smoke_test(spark: SparkSession, args: argparse.Namespace, online_table: str) -> None:
    """Reads one candidate online, by the key the service uses, and compares it with its offline row."""
    expected = storage.read_smoke_row(spark, args.fs_table)
    item = online_store.get_item(online_table, materialization.online_key(expected["spot_id"]), args.region)
    errors = materialization.check_item(item, expected)
    if errors:
        raise SmokeTestError(f"{online_table}:\n  " + "\n  ".join(errors))


def _promote(spark: SparkSession, args: argparse.Namespace) -> bool:
    """Points runs/champion.json to this run if T2's evaluation says it wins. Returns whether it did."""
    runs_root = args.runs_root.rstrip("/")
    report = json.loads(storage.read_text(spark, f"{runs_root}/{args.run_id}/{evaluation.METRICS_FILE}"))
    if not report["wins"]:
        logger.info("promote: %s does not beat the champion, which stays", args.run_id)
        return False
    storage.write_text(spark, f"{runs_root}/{materialization.CHAMPION_FILE}", json.dumps({"run_id": args.run_id}))
    move_champion_tag(args.experiment)
    logger.info("promote: %s is the new champion", args.run_id)
    return True


def _clean_up(spark: SparkSession, args: argparse.Namespace) -> dict[str, int]:
    """Same rule everywhere: the last N runs + the champion + production. This run is never deleted (repair runs)."""
    keep = materialization.keep_runs(yaml.safe_load(RETENTION.read_text()))
    protect = {args.run_id}
    champion = storage.read_runs_file(spark, args.runs_root, materialization.CHAMPION_FILE)
    if champion is not None:
        protect.add(materialization.champion_run_id(champion))
    pointer = storage.read_runs_file(spark, args.runs_root, materialization.PRODUCTION_FILE)
    if pointer is not None:
        protect.add(materialization.production_run_id(pointer))
    logger.info("cleanup: keeping the last %d runs and %s", keep, sorted(protect))

    prefix = materialization.ONLINE_TABLE_PREFIX
    tables = materialization.to_drop(online_store.list_tables(prefix, args.region), prefix, keep, protect=protect)
    for table in tables:
        online_store.delete_table(table, args.region)

    folders = materialization.to_drop(storage.list_run_folders(spark, args.runs_root), "", keep, protect=protect)
    for folder in folders:
        storage.delete_run_folder(spark, args.runs_root, folder)

    snapshot_prefix = materialization.snapshot_table_name(args.fs_table, "")
    snapshots = materialization.to_drop(storage.list_snapshots(spark, args.fs_table), snapshot_prefix, keep, protect=protect)
    for snapshot in snapshots:
        storage.drop_table(spark, snapshot)

    logger.info("cleanup: deleted tables %s, run folders %s and snapshots %s", tables, folders, snapshots)
    return {
        "cleanup_tables_deleted": len(tables),
        "cleanup_run_folders_deleted": len(folders),
        "cleanup_snapshots_deleted": len(snapshots),
    }


def run(spark: SparkSession, args: argparse.Namespace) -> dict[str, int]:
    online_table = materialization.online_table_name(args.run_id)
    snapshot = materialization.snapshot_table_name(args.fs_table, args.run_id)
    with start_run(args.experiment, args.run_id, args.git_commit):
        with stage("snapshot_fs_offline"):
            source_version = storage.snapshot_fs_offline(spark, args.fs_table, snapshot)
        # The run's dataset: MLflow shows the table, its version and a digest under "Inputs".
        mlflow.log_input(mlflow.data.load_delta(table_name=snapshot, name="spots_fs_offline"), context="training")
        mlflow.set_tags({"fs_offline_snapshot": snapshot, "fs_offline_snapshot_source_version": source_version})
        with stage("publish_table"):
            online_store.publish(args.fs_table, online_table, args.region)
        with stage("smoke_test"):
            _smoke_test(spark, args, online_table)
        mlflow.set_tag("online_table", online_table)
        with stage("promote"):
            promoted = _promote(spark, args)
        with stage("cleanup"):
            metrics = _clean_up(spark, args)
        metrics["promoted_champion"] = int(promoted)
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
    parser.add_argument("--git-commit", required=True, help="${bundle.git.commit}: code the bundle was deployed from")
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
