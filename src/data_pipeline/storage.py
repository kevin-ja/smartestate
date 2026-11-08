"""
I/O of the data pipeline: the two writes (quarantine, Spots FS offline), the SBERT model fetch and the
raw files' hash. The CSV and catalog readers stay in their stages (`extract`, `read_catalog`).
"""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

from pyspark.sql import DataFrame, SparkSession

from src.data_pipeline.contract.schema import FS_OFFLINE_KEY, FS_OFFLINE_SCHEMA


def write_quarantine(df: DataFrame, path: str) -> None:
    """runs/<run_id>/quarantine.parquet, overwritten: a repair run of the same run_id is idempotent."""
    df.write.mode("overwrite").parquet(path)


def fs_offline_ddl(table: str) -> str:
    """CREATE TABLE from the contract, with the primary key publish_table needs (new_arch.md §3.4)."""
    columns = ", ".join(
        f"{f.name} {f.dataType.simpleString()}{'' if f.nullable else ' NOT NULL'}" for f in FS_OFFLINE_SCHEMA.fields
    )
    pk_name = f"{table.split('.')[-1]}_pk"
    return f"CREATE TABLE IF NOT EXISTS {table} ({columns}, CONSTRAINT {pk_name} PRIMARY KEY ({FS_OFFLINE_KEY})) USING DELTA"


def write_fs_offline(df: DataFrame, table: str) -> int:
    """
    Full rewrite of Spots FS offline. Created once with its primary key; later runs INSERT OVERWRITE,
    which keeps the key (saveAsTable would replace the table and drop it). Returns the Delta version.
    """
    spark = df.sparkSession
    spark.sql(fs_offline_ddl(table))
    df.select(*FS_OFFLINE_SCHEMA.fieldNames()).write.insertInto(table, overwrite=True)
    return spark.sql(f"DESCRIBE HISTORY {table} LIMIT 1").first()["version"]


def fetch_archive(spark: SparkSession, uri: str, local_dir: Path) -> Path:
    """
    Copies a model tarball from models/ to the driver's local disk and extracts it. Returns the folder
    with the SentenceTransformer's modules.json.
    """
    from pyspark.dbutils import DBUtils  # Databricks only: the one allowed exception (new_arch.md §5)

    local_dir.mkdir(parents=True, exist_ok=True)
    archive = local_dir / Path(uri).name
    DBUtils(spark).fs.cp(uri, f"file:{archive}")
    with tarfile.open(archive) as tar:
        # "data" filter: no absolute paths or links out of local_dir (Python 3.11.4+).
        tar.extractall(local_dir, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    archive.unlink()

    found = sorted(local_dir.rglob("modules.json"))
    if not found:
        raise FileNotFoundError(f"No modules.json in {uri}: not a SentenceTransformer folder")
    return found[0].parent


def file_md5(spark: SparkSession, path: str) -> str:
    """
    MD5 of a raw file's bytes. For a single-part upload it equals the S3 ETag, so it names the exact S3
    version that was read: `aws s3api list-object-versions` shows each version's ETag (train_pipeline_mlops.md).
    The raw CSVs are a few MB: reading them whole on the driver is fine.
    """
    content = spark.read.format("binaryFile").load(path).select("content").first()["content"]
    return hashlib.md5(content).hexdigest()
