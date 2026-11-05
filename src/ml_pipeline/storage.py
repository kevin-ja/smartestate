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


def _copy_to_runs(spark: SparkSession, local: Path, destination: str) -> None:
    from pyspark.dbutils import DBUtils  # Databricks only: the one allowed exception (new_arch.md §5)

    DBUtils(spark).fs.cp(f"file:{local}", destination)


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
