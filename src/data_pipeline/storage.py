"""
Writes to the data pipeline's layers (bronze, silver, gold). Every stage writes through here, so no
stage imports another.
"""

from __future__ import annotations

from pyspark.sql import DataFrame


def store(df: DataFrame, table: str) -> None:
    """Full overwrite of a managed Delta table: idempotent and atomic."""
    (
        df.write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", True)  # the contract in code is the source of truth
        .saveAsTable(table)
    )
