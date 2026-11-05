"""
T1 · Data pipeline: raw CSVs -> Spots FS offline, in one task with no intermediate tables
(new_arch.md §3.1).

    Ingestion -> Cleaning -> Preprocessing -> Feature engineering -> Storage

Every check runs before the first write: on abort nothing is written. The MLflow run is found by
the `run_id` tag (Databricks `job.run_id`), so a repair run reuses it.

Run (Databricks, bundles/data/resources/data_pipeline_job.yml):
    run_data_pipeline.py --run-id {{job.run_id}} --ingest-date 2026-09-26 \
        --raw-root s3://<lake>/raw --ref-root s3://<lake>/ref --runs-root s3://<lake>/runs \
        --sbert-uri s3://<lake>/models/sbert/paraphrase-multilingual-mpnet-base-v2.tar.gz \
        --fs-table smartestate.feature_store_offline.spots_fs_offline \
        --experiment /Shared/smartestate-train
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

import mlflow
import pyspark
from pyspark.sql import SparkSession

from src.common import logs, run_monitor, spark_probes
from src.common.run_monitor import stage
from src.data_pipeline import cleaning, feature_engineering, ingestion, preprocessing, storage
from src.data_pipeline.contract.schema import RAW_ATTRIBUTES_COLUMNS, RAW_SPOTS_COLUMNS, TAB_DIMS

logger = logging.getLogger(__name__)

SBERT_LOCAL_DIR = Path("/local_disk0/smartestate/sbert")

# Spark's default is 200: ~12 rows per partition here, and each one is a task to prepare (data_pipeline.md).
SHUFFLE_PARTITIONS = 8


@dataclass
class DataPipelineResult:
    stats: dict[str, int]              # one line per run in the log and metrics in MLflow
    fs_version: int                    # Delta version of Spots FS offline written by this run
    warnings: list[str] = field(default_factory=list)


def run(spark: SparkSession, args: argparse.Namespace) -> DataPipelineResult:
    """Reads raw/ingest_date=<date>/, runs every stage in memory, validates, then writes."""
    dt.date.fromisoformat(args.ingest_date)  # fails early on a malformed date
    folder = f"{args.raw_root.rstrip('/')}/ingest_date={args.ingest_date}"

    # Each block ends in an action, so its time is the stage's own work and not the next one's (lazy Spark).
    # localCheckpoint, not cache: it stores the rows and drops the query plan. A task reading a cache still
    # carries that plan, which costs ~1.5 s per task on Databricks (data_pipeline.md).
    with stage("ingestion"):
        ingested = ingestion.transform(
            ingestion.extract(spark, f"{folder}/data.csv", RAW_SPOTS_COLUMNS),
            ingestion.extract(spark, f"{folder}/data_at.csv", RAW_ATTRIBUTES_COLUMNS),
        )
        bronze = ingested.bronze.localCheckpoint()
        warnings = list(ingested.warnings)
        bronze_rows = bronze.count()

    with stage("cleaning"):
        silver = cleaning.transform(bronze, cleaning.read_catalog(spark, args.ref_root)).localCheckpoint()
        warnings += cleaning.validate(silver, bronze_rows)

    with stage("preprocessing"):
        gold, nulled = preprocessing.transform(silver)
        gold = gold.localCheckpoint()
        warnings += preprocessing.validate(gold, silver.count(), nulled)

    with stage("sbert_fetch"):
        model_dir = storage.fetch_archive(spark, args.sbert_uri, SBERT_LOCAL_DIR)
    with stage("sbert_load"):
        encode = feature_engineering.load_encoder(model_dir)

    result = feature_engineering.transform(gold, encode)  # its stages are timed inside

    with stage("fe_validate"):
        features = result.features.localCheckpoint()
        gold_rows = gold.count()
        with_description = gold.filter("has_description").count()
        warnings += result.warnings + feature_engineering.validate(features, gold_rows, with_description)

    with stage("write_quarantine"):
        quarantine_rows = ingested.quarantine.count()
        storage.write_quarantine(ingested.quarantine, f"{args.runs_root.rstrip('/')}/{args.run_id}/quarantine.parquet")
    with stage("write_fs_offline"):
        fs_version = storage.write_fs_offline(features, args.fs_table)

    with stage("candidate_stats"):
        candidates = {
            row["sector_id"]: row["count"]
            for row in features.filter("is_candidate").groupBy("sector_id").count().collect()
        }
    stats = {
        "rows": gold_rows,
        "quarantine_rows": quarantine_rows,
        "with_description": with_description,
        **{f"candidates_sector_{sector}": candidates.get(sector, 0) for sector in (*TAB_DIMS, 15)},
    }
    _log_mlflow(result.models, stats, fs_version, args)
    if args.spark_probes == "true":
        spark_probes.run(gold)
    return DataPipelineResult(stats, fs_version, warnings)


def _start_run(experiment: str, run_id: str) -> mlflow.ActiveRun:
    """Reuses the MLflow run tagged with this run_id (repair run) or creates it."""
    mlflow.set_experiment(experiment)
    found = mlflow.search_runs(filter_string=f"tags.run_id = '{run_id}'", max_results=1, output_format="list")
    if found:
        return mlflow.start_run(run_id=found[0].info.run_id)
    return mlflow.start_run(run_name=f"train-{run_id}", tags={"run_id": run_id})


def _log_mlflow(models: dict, stats: dict[str, int], fs_version: int, args: argparse.Namespace) -> None:
    """FE models, counts, stage times and the Delta version used. Tags and metrics only: a repair run may overwrite them."""
    with _start_run(args.experiment, args.run_id):
        mlflow.set_tags({"ingest_date": args.ingest_date, "fs_offline_table": args.fs_table, "fs_offline_version": fs_version})
        mlflow.log_metrics(stats)
        with stage("mlflow_log_models"):
            for name, model in models.items():
                # Explicit: otherwise MLflow reloads each model to infer them, which takes ~2 min per model.
                mlflow.spark.log_model(
                    model, artifact_path=f"fe_pipeline_{name}", pip_requirements=[f"pyspark=={pyspark.__version__}"]
                )
        mlflow.log_metrics({f"duration_{name}_s": seconds for name, seconds in run_monitor.durations().items()})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="T1 · raw CSVs -> Spots FS offline")
    parser.add_argument("--run-id", required=True, help="Databricks {{job.run_id}}")
    parser.add_argument("--ingest-date", required=True, help="raw/ folder to read, YYYY-MM-DD")
    parser.add_argument("--raw-root", required=True, help="e.g. s3://<bucket>/raw")
    parser.add_argument("--ref-root", required=True, help="e.g. s3://<bucket>/ref")
    parser.add_argument("--runs-root", required=True, help="e.g. s3://<bucket>/runs")
    parser.add_argument("--sbert-uri", required=True, help="SBERT tarball under s3://<bucket>/models/")
    parser.add_argument("--fs-table", required=True)
    parser.add_argument("--experiment", required=True, help="MLflow experiment path")
    parser.add_argument(
        "--spark-probes", choices=("true", "false"), default="false",
        help="After T1 finishes, run the diagnostic experiments of src/common/spark_probes.py",
    )
    args = parser.parse_args(argv)

    logs.configure()
    spark = SparkSession.builder.getOrCreate()
    spark.conf.set("spark.sql.shuffle.partitions", SHUFFLE_PARTITIONS)
    run_monitor.log_environment(spark)
    with run_monitor.sample_resources():
        try:
            result = run(spark, args)
        finally:
            run_monitor.log_summary()  # also when the run fails: shows how far it got

    for warning in result.warnings:
        logger.warning(warning)
    # One line per run: verifies the job without a notebook and doubles as a data-quality monitor.
    logger.info(
        "spots_fs_offline v%d: %s · %d warnings",
        result.fs_version, " · ".join(f"{k} {v}" for k, v in result.stats.items()), len(result.warnings),
    )


if __name__ == "__main__":
    main()
