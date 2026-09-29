"""
Stage 3 · Preprocessing: smartestate.silver.spots -> smartestate.gold.spots.

    Load -> Text -> Security -> Floor -> Domains -> Validation -> Storage

Design in src/data_pipeline/README.md, section 3. Gold is readable business data with nothing
model-specific: no scaling, no embeddings. Never drops a row. Functions take and return Spark
DataFrames, do not print and do not mutate their input; only `load` reads and only `store` writes.

Run:
    python -m src.data_pipeline.preprocessing \
        --silver-table smartestate.silver.spots \
        --gold-table smartestate.gold.spots
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

import pandas as pd
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import ArrayType, StringType, StructType

from src.common import logs
from src.data_pipeline.contract.rules import GOLD_DOMAINS
from src.data_pipeline.contract.schema import GOLD_SCHEMA, SILVER_SCHEMA
from src.data_pipeline.storage import store

logger = logging.getLogger(__name__)

# Joins title and description. Without a description, text is the title alone (concat_ws skips nulls).
TEXT_SEPARATOR = ". "


class PreprocessingError(Exception):
    """Silver breaks what preprocessing expects, or preprocessing broke the table. The run aborts."""


@dataclass
class PreprocessingResult:
    gold: DataFrame
    nulled: dict[str, int] = field(default_factory=dict)  # domain column -> values set to null
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 0 · Load
# --------------------------------------------------------------------------


def check_schema(df: DataFrame, contract: StructType, layer: str) -> None:
    """Abort unless names and types match the layer's contract. Nullability is not compared."""
    # Same check as cleaning.py. Copied, not imported: no stage imports another.
    expected = [(f.name, f.dataType) for f in contract.fields]
    actual = [(f.name, f.dataType) for f in df.schema.fields]
    if actual != expected:
        raise PreprocessingError(f"{layer} does not match its contract. Expected {expected}, got {actual}")


def load(spark: SparkSession, table: str) -> DataFrame:
    df = spark.table(table)
    check_schema(df, SILVER_SCHEMA, "Silver")
    return df


# --------------------------------------------------------------------------
# 1 · Text  +  3 · Floor
# --------------------------------------------------------------------------


@F.pandas_udf(StringType())
def _nfkc(values: pd.Series) -> pd.Series:
    """NFKC: one form per character (m² -> m2, non-breaking space -> space). Nulls stay null."""
    return values.str.normalize("NFKC")


def _clean_text(column: str):
    """
    Unicode NFKC, any run of whitespace (line breaks included) to one space, trimmed; empty -> null.
    No lowercasing, no accent or stopword removal: SBERT reads natural text.
    """
    cleaned = F.trim(F.regexp_replace(_nfkc(F.col(column)), r"\s+", " "))
    return F.when(cleaned != "", cleaned)


def clean_text(df: DataFrame) -> DataFrame:
    """Cleans title and description, then builds text and has_description (D3)."""
    return (
        df.withColumn("title", _clean_text("title"))
        .withColumn("description", _clean_text("description"))
        .withColumn("text", F.concat_ws(TEXT_SEPARATOR, "title", "description"))
        .withColumn("text", F.when(F.col("text") != "", F.col("text")))
        .withColumn("has_description", F.col("description").isNotNull())
    )


def clean_floor(df: DataFrame) -> DataFrame:
    """Same cleaning as the text. No catalog: SBERT reads it in Stage 4 (D18)."""
    return df.withColumn("floor_material", _clean_text("floor_material"))


# --------------------------------------------------------------------------
# 2 · Security
# --------------------------------------------------------------------------


def parse_security(df: DataFrame) -> DataFrame:
    """
    JSON text -> sorted list of distinct integers. Both formats arrive: [1, 2] and ["1", "2"],
    even mixed. Parsing as a list of strings accepts both (Spark keeps a JSON number as its text).
    A value that cannot be read aborts: it is never turned into null silently.
    """
    raw = F.from_json(F.col("security_type"), ArrayType(StringType()))
    # try_cast: an unreadable item becomes null (and aborts below) with or without ANSI mode.
    items = F.expr(
        "transform(from_json(security_type, 'array<string>'), item -> try_cast(trim(item) AS INT))"
    )
    parsed = F.array_sort(F.array_distinct(items))

    unreadable = df.filter(
        F.col("security_type").isNotNull()
        & (raw.isNull() | F.exists(items, lambda item: item.isNull()))
    )
    bad = unreadable.select("spot_id", "security_type").limit(5).collect()
    if bad:
        sample = ", ".join(f"{row.spot_id}: {row.security_type!r}" for row in bad)
        raise PreprocessingError(f"security_type unreadable (first {len(bad)}): {sample}")
    return df.withColumn("security_type", parsed)


# --------------------------------------------------------------------------
# 4 · Domains
# --------------------------------------------------------------------------


def apply_domains(df: DataFrame) -> tuple[DataFrame, dict[str, int]]:
    """
    GOLD_DOMAINS: a non-null value off its domain becomes null. Returns the frame and, per column,
    how many values were nulled. Source nulls are structural and are not counted.
    """
    invalid = {column: F.col(column).isNotNull() & ~F.expr(rule) for column, rule in GOLD_DOMAINS.items()}
    counts = df.agg(
        *[F.sum(condition.cast("int")).alias(column) for column, condition in invalid.items()]
    ).first()
    nulled = {column: counts[column] or 0 for column in GOLD_DOMAINS}

    for column, condition in invalid.items():
        df = df.withColumn(column, F.when(~condition, F.col(column)))
    return df, nulled


# --------------------------------------------------------------------------
# 5 · Validation
# --------------------------------------------------------------------------


def validate(gold: DataFrame, silver_rows: int, nulled: dict[str, int]) -> list[str]:
    """Aborts if gold breaks its contract or rows were lost or invented. Returns warnings."""
    check_schema(gold, GOLD_SCHEMA, "Gold")
    rows = gold.count()
    if rows != silver_rows:
        raise PreprocessingError(f"Validation failed: rows silver {silver_rows}, gold {rows}")

    return [
        f"{column}: {count} values outside `{GOLD_DOMAINS[column]}` set to null"
        for column, count in nulled.items()
        if count
    ]


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def transform(silver: DataFrame) -> tuple[DataFrame, dict[str, int]]:
    """Text -> Security -> Floor -> Domains, with no I/O."""
    df = clean_text(silver)
    df = parse_security(df)
    df = clean_floor(df)
    df, nulled = apply_domains(df)
    return df.select(*GOLD_SCHEMA.fieldNames()), nulled


def run(spark: SparkSession, silver_table: str, gold_table: str) -> PreprocessingResult:
    """Reads silver, preprocesses, validates and writes gold. Nothing is written on abort."""
    silver = load(spark, silver_table)
    gold, nulled = transform(silver)
    warnings = validate(gold, silver.count(), nulled)
    store(gold, gold_table)
    return PreprocessingResult(gold, nulled, warnings)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 3 · silver -> gold")
    parser.add_argument("--silver-table", required=True)
    parser.add_argument("--gold-table", required=True)
    args = parser.parse_args(argv)

    logs.configure()
    spark = SparkSession.builder.getOrCreate()
    result = run(spark, args.silver_table, args.gold_table)

    for warning in result.warnings:
        logger.warning(warning)
    # One line per run, same as silver: verifies the job without a notebook.
    stats = spark.table(args.gold_table).agg(
        F.count("*").alias("rows"),
        F.sum(F.col("has_description").cast("int")).alias("with_description"),
    ).first()
    logger.info(
        "gold: %d rows · %d with_description · %d building_status_nulled · %d natural_light_nulled · %d warnings",
        stats["rows"], stats["with_description"], result.nulled["building_status"],
        result.nulled["natural_light"], len(result.warnings),
    )


if __name__ == "__main__":
    main()
