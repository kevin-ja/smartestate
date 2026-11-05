"""I/O of the modeling pipeline: read the candidates, write the artifacts and cluster_id."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from pyspark.sql import DataFrame, SparkSession

CANDIDATE_COLUMNS = ("spot_id", "sector_id", "modality_mask", "latitude", "longitude", "tab_vector", "nlp_vector")


def read_candidates(spark: SparkSession, table: str) -> pd.DataFrame:
    """Candidates sorted by spot_id, so the same data always gives the same K-means and GeoIndex."""
    return spark.table(table).filter("is_candidate").select(*CANDIDATE_COLUMNS).orderBy("spot_id").toPandas()


def read_smoke_row(spark: SparkSession, table: str) -> dict[str, Any]:
    """First candidate with text, by spot_id: spot_id, cluster_id and nlp_vector for the T3 smoke test."""
    row = (
        spark.table(table).filter("is_candidate AND nlp_vector IS NOT NULL")
        .select("spot_id", "cluster_id", "nlp_vector").orderBy("spot_id").first()
    )
    if row is None:
        raise ValueError(f"{table} has no candidate with text to run the smoke test on")
    return row.asDict()


def _dbutils(spark: SparkSession):
    from pyspark.dbutils import DBUtils  # Databricks only: the one allowed exception (new_arch.md §5)

    return DBUtils(spark)


def _newest_file_ms(dbutils, path: str) -> int:
    """S3 folders have no date of their own: only the files inside do, at any depth."""
    return max(
        (_newest_file_ms(dbutils, entry.path) if entry.isDir() else entry.modificationTime for entry in dbutils.fs.ls(path)),
        default=0,
    )


def list_run_folders(spark: SparkSession, runs_root: str) -> dict[str, float]:
    """Run folder under runs/ -> its newest file, in seconds. Aborts on a run it cannot date: cleanup deletes."""
    dbutils = _dbutils(spark)
    folders = {}
    for entry in dbutils.fs.ls(runs_root):
        name = entry.name.rstrip("/")
        if entry.isDir() and name.isdigit():  # others, like _probe, are not runs
            newest_ms = _newest_file_ms(dbutils, entry.path)
            if newest_ms <= 0:
                raise ValueError(f"No file with a date under {entry.path}")
            folders[name] = newest_ms / 1000
    return folders


def delete_run_folder(spark: SparkSession, runs_root: str, folder: str) -> None:
    _dbutils(spark).fs.rm(f"{runs_root.rstrip('/')}/{folder}", recurse=True)


def _copy_to_runs(spark: SparkSession, local: Path, destination: str) -> None:
    _dbutils(spark).fs.cp(f"file:{local}", destination)


def write_joblib(spark: SparkSession, value: Any, local: Path, destination: str) -> None:
    """Serializes on the driver's disk, then copies to runs/<run_id>/."""
    local.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(value, local)
    _copy_to_runs(spark, local, destination)


def write_json(spark: SparkSession, value: Any, local: Path, destination: str) -> None:
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps(value, indent=2))
    _copy_to_runs(spark, local, destination)


def write_fs_offline(df: DataFrame, table: str) -> int:
    """INSERT OVERWRITE keeps the table's primary key, as in T1. Returns the Delta version written."""
    df.write.insertInto(table, overwrite=True)
    return df.sparkSession.sql(f"DESCRIBE HISTORY {table} LIMIT 1").first()["version"]
