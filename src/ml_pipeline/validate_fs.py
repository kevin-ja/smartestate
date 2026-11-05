"""
Task validate_fs: the data gate before T3 (new_arch.md §3.1). Checks Spots FS offline as written.

The unit tests check the code; this checks each run's real data. If it fails, nothing is published.

Run (Databricks, bundles/data/resources/data_pipeline_job.yml):
    run_validate_fs.py --fs-table smartestate.feature_store_offline.spots_fs_offline
"""

from __future__ import annotations

import argparse
import logging

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from src.common import logs

logger = logging.getLogger(__name__)

# Copied, not imported: ml_pipeline never imports data_pipeline. A test keeps it equal to FS_OFFLINE_SCHEMA.
EXPECTED_COLUMNS = (
    ("spot_id", "bigint"),
    ("sector_id", "int"),
    ("modality_mask", "int"),
    ("latitude", "double"),
    ("longitude", "double"),
    ("tab_vector", "array<float>"),
    ("nlp_vector", "array<float>"),
    ("rent_price_sqm", "double"),
    ("sale_price_sqm", "double"),
    ("is_candidate", "boolean"),
    ("cluster_id", "int"),
)

# What the service reads to filter a candidate (new_arch.md §4.1).
CANDIDATE_REQUIRED = ("sector_id", "modality_mask", "latitude", "longitude")


class FsValidationError(Exception):
    """Spots FS offline must not be published. The run stops before T3."""


def _count(condition) -> F.Column:
    return F.sum(condition.cast("int"))


def find_errors(fs: DataFrame) -> list[str]:
    """Every problem found, empty if the table can be published."""
    actual = tuple((field.name, field.dataType.simpleString()) for field in fs.schema.fields)
    if actual != EXPECTED_COLUMNS:
        return [f"schema: expected {EXPECTED_COLUMNS}, got {actual}"]  # the checks below need these columns

    candidate = F.col("is_candidate")
    has_cluster = F.col("cluster_id").isNotNull()
    stats = fs.agg(
        F.count("*").alias("rows"),
        F.countDistinct("spot_id").alias("distinct_ids"),
        _count(F.col("spot_id").isNull()).alias("null_ids"),
        _count(candidate.isNull()).alias("null_candidate_flag"),
        _count(candidate).alias("candidates"),
        _count(candidate & ~has_cluster).alias("candidates_without_cluster"),
        _count(~candidate & has_cluster).alias("others_with_cluster"),
        *[_count(candidate & F.col(column).isNull()).alias(f"null_{column}") for column in CANDIDATE_REQUIRED],
    ).first()

    checks = {
        "no rows": stats["rows"] == 0,
        f"spot_id repeated: {stats['rows']} rows, {stats['distinct_ids']} distinct": stats["rows"] != stats["distinct_ids"],
        f"spot_id null in {stats['null_ids']} rows": bool(stats["null_ids"]),
        f"is_candidate null in {stats['null_candidate_flag']} rows": bool(stats["null_candidate_flag"]),
        "no candidates": not stats["candidates"],
        f"{stats['candidates_without_cluster']} candidates without cluster_id": bool(stats["candidates_without_cluster"]),
        f"{stats['others_with_cluster']} non-candidates with cluster_id": bool(stats["others_with_cluster"]),
        **{f"{stats[f'null_{c}']} candidates without {c}": bool(stats[f"null_{c}"]) for c in CANDIDATE_REQUIRED},
    }
    return [message for message, failed in checks.items() if failed]


def validate(fs: DataFrame) -> None:
    errors = find_errors(fs)
    if errors:
        raise FsValidationError("Spots FS offline failed validation:\n  " + "\n  ".join(errors))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="validate_fs · checks Spots FS offline before T3")
    parser.add_argument("--fs-table", required=True)
    args = parser.parse_args(argv)

    logs.configure()
    validate(SparkSession.builder.getOrCreate().table(args.fs_table))
    logger.info("validate_fs: %s passed", args.fs_table)


if __name__ == "__main__":
    main()
