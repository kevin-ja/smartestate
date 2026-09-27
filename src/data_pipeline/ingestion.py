"""
Stage 0 · Ingestion: raw CSVs -> smartestate.bronze.spots.

    Extract -> Schema -> Parsing -> Join + projection -> Validation -> Storage

Design in src/data_pipeline/README.md. Functions take and return Spark DataFrames, do not print and
do not mutate their input; only `extract` reads and only `store` writes. Paths and table names come
in as parameters.

Run:
    python -m src.data_pipeline.ingestion --ingest-date 2026-09-26 \
        --raw-root "$SMARTESTATE_RAW_ROOT" \
        --bronze-table smartestate.bronze.spots \
        --quarantine-table smartestate.bronze.spots_quarantine
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
from dataclasses import dataclass, field

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from src.data_pipeline.contract.rules import (
    JOIN_MIN_MATCH,
    KEY_COLUMN,
    NULL_RATE_TOLERANCE,
    QUARANTINE_RULES,
    load_null_baseline,
)
from src.data_pipeline.contract.schema import (
    ATTRIBUTES_FIELDS,
    BRONZE_SCHEMA,
    RAW_ATTRIBUTES_COLUMNS,
    RAW_SPOTS_COLUMNS,
    SPOTS_FIELDS,
    Field,
)

logger = logging.getLogger(__name__)

# "99.20000000° W" -> (99.20000000, W). No backslashes: the pattern lives inside a SQL literal.
_COORDINATE = "^ *([0-9]+[.]?[0-9]*) *° *([NSEWO]) *$"
_DATE_FORMAT = "MMMM d, yyyy"


class IngestionError(Exception):
    """The batch breaks the contract. The run aborts."""


@dataclass
class IngestionResult:
    bronze: DataFrame
    quarantine: DataFrame
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 1 · Extract  +  2 · Schema (header)
# --------------------------------------------------------------------------


def extract(spark: SparkSession, path: str, expected_columns: list[str]) -> DataFrame:
    """Reads a CSV as strings and checks its header against the contract."""
    df = (
        spark.read.option("header", True)
        .option("multiLine", True)   # 210 descriptions carry line breaks
        .option("escape", '"')       # quotes inside a field come doubled
        .option("mode", "FAILFAST")  # a malformed row aborts, it is not dropped
        .option("encoding", "UTF-8")
        .csv(path)
    )
    if df.columns != expected_columns:
        missing = [c for c in expected_columns if c not in df.columns]
        extra = [c for c in df.columns if c not in expected_columns]
        raise IngestionError(
            f"Header of {path} does not match the contract. Missing: {missing}. Extra: {extra}. "
            "Same columns in another order also counts."
        )
    return df


# --------------------------------------------------------------------------
# 3 · Parsing
# --------------------------------------------------------------------------


def _parse_expression(f: Field) -> str:
    value = f"nullif(`{f.source}`, '')"
    if f.format == "number":
        return f"try_cast(regexp_replace({value}, ',', '') AS {f.dtype.simpleString()})"
    if f.format == "coordinate":
        magnitude = f"try_cast(regexp_extract({value}, '{_COORDINATE}', 1) AS double)"
        hemisphere = f"regexp_extract({value}, '{_COORDINATE}', 2)"
        return f"{magnitude} * CASE WHEN {hemisphere} IN ('S', 'W', 'O') THEN -1 ELSE 1 END"
    if f.format == "date":
        return f"to_date(try_to_timestamp({value}, '{_DATE_FORMAT}'))"
    if f.format == "text":
        return value
    raise ValueError(f"Unknown format {f.format!r} for {f.source!r}")


def parse(df: DataFrame, fields: list[Field]) -> DataFrame:
    """
    Converts each contract column to its type and final name. Aborts if a non-empty value does not
    parse, or if a non-nullable column comes empty. Cleans nothing.
    """
    raw = {f.name: f"__raw_{i}" for i, f in enumerate(fields)}
    staged = df.select(
        *[F.col(f"`{f.source}`").alias(raw[f.name]) for f in fields],
        *[F.expr(_parse_expression(f)).alias(f.name) for f in fields],
    )

    def failed(f: Field):
        original = F.col(raw[f.name])
        return original.isNotNull() & (original != "") & F.col(f.name).isNull()

    checks = [F.sum(failed(f).cast("int")).alias(f.name) for f in fields]
    checks += [
        F.sum(F.col(f.name).isNull().cast("int")).alias(f"__null_{f.name}")
        for f in fields if not f.nullable
    ]
    counts = staged.agg(*checks).first().asDict()

    errors = []
    for f in fields:
        if counts[f.name]:
            examples = [r[0] for r in staged.filter(failed(f)).select(raw[f.name]).limit(3).collect()]
            errors.append(f"{f.source}: {counts[f.name]} values do not parse, e.g. {examples}")
        if not f.nullable and counts[f"__null_{f.name}"]:
            errors.append(f"{f.source}: {counts[f'__null_{f.name}']} empty values, column is not nullable")
    if errors:
        raise IngestionError("Parsing failed:\n  " + "\n  ".join(errors))

    return staged.select(*[f.name for f in fields])


# --------------------------------------------------------------------------
# 4 · Join + projection
# --------------------------------------------------------------------------


def join(spots: DataFrame, attributes: DataFrame) -> tuple[DataFrame, list[str]]:
    """One table with the 25 bronze columns. Left join: a spot without attributes still survives."""
    total = spots.count()
    matched = spots.join(attributes.select(KEY_COLUMN), KEY_COLUMN, "left_semi").count()
    orphans = attributes.join(spots.select(KEY_COLUMN), KEY_COLUMN, "left_anti").count()

    warnings = []
    match_rate = matched / total if total else 0.0
    if match_rate < JOIN_MIN_MATCH:
        warnings.append(
            f"join: only {match_rate:.1%} of spots have attributes ({matched}/{total}), "
            f"expected >= {JOIN_MIN_MATCH:.0%}"
        )
    if orphans:
        warnings.append(f"join: {orphans} attribute rows have no spot")

    joined = spots.join(attributes, KEY_COLUMN, "left").select(*BRONZE_SCHEMA.fieldNames())
    return joined, warnings


# --------------------------------------------------------------------------
# 5 · Validation
# --------------------------------------------------------------------------


def check_unique_key(df: DataFrame) -> None:
    """Abort: a repeated spot_id means the batch is wrong."""
    repeated = df.groupBy(KEY_COLUMN).count().filter("count > 1").limit(10).collect()
    if repeated:
        ids = [r[KEY_COLUMN] for r in repeated]
        raise IngestionError(f"{KEY_COLUMN} is repeated, e.g. {ids}")


def split_quarantine(df: DataFrame) -> tuple[DataFrame, DataFrame]:
    """Returns (good rows, quarantined rows with `reason`). A row can carry several reasons."""
    reasons = F.concat_ws(
        ",", *[F.when(F.expr(condition), F.lit(reason)) for reason, condition in QUARANTINE_RULES.items()]
    )
    flagged = df.withColumn("reason", F.when(reasons != "", reasons))
    good = flagged.filter(F.col("reason").isNull()).drop("reason")
    quarantined = flagged.filter(F.col("reason").isNotNull())
    return good, quarantined


def check_null_rates(df: DataFrame, baseline: dict[int, dict[str, float]]) -> list[str]:
    """D8: warns when a column's null rate in a sector exceeds its baseline by the tolerance."""
    columns = sorted({c for rates in baseline.values() for c in rates})
    rows = (
        df.groupBy("sector_id")
        .agg(*[F.avg(F.col(c).isNull().cast("double")).alias(c) for c in columns])
        .collect()
    )

    warnings = []
    for row in rows:
        for column, expected in baseline.get(row["sector_id"], {}).items():
            actual = row[column]
            if actual - expected > NULL_RATE_TOLERANCE:
                warnings.append(
                    f"nulls: sector {row['sector_id']} · {column} at {actual:.1%}, "
                    f"baseline {expected:.1%}"
                )
    return warnings


def validate(df: DataFrame) -> tuple[DataFrame, DataFrame, list[str]]:
    """Returns (good rows, quarantined rows, warnings). Aborts on a repeated key."""
    check_unique_key(df)
    good, quarantined = split_quarantine(df)
    warnings = check_null_rates(good, load_null_baseline())
    return good, quarantined, warnings


# --------------------------------------------------------------------------
# 6 · Storage
# --------------------------------------------------------------------------


def store(df: DataFrame, table: str) -> None:
    """Full overwrite of a managed Delta table: idempotent and atomic."""
    (
        df.write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", True)  # the contract in code is the source of truth
        .saveAsTable(table)
    )


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def transform(spots_raw: DataFrame, attributes_raw: DataFrame) -> IngestionResult:
    """Parsing -> Join -> Validation, with no I/O."""
    spots = parse(spots_raw, SPOTS_FIELDS)
    attributes = parse(attributes_raw, ATTRIBUTES_FIELDS)
    joined, join_warnings = join(spots, attributes)
    good, quarantined, null_warnings = validate(joined)
    return IngestionResult(good, quarantined, join_warnings + null_warnings)


def run(
    spark: SparkSession,
    raw_root: str,
    ingest_date: str,
    bronze_table: str,
    quarantine_table: str,
) -> IngestionResult:
    """Reads raw/ingest_date=<date>/, transforms and writes both tables. Nothing is written on abort."""
    dt.date.fromisoformat(ingest_date)  # fails early on a malformed date
    folder = f"{raw_root.rstrip('/')}/ingest_date={ingest_date}"

    result = transform(
        extract(spark, f"{folder}/data.csv", RAW_SPOTS_COLUMNS),
        extract(spark, f"{folder}/data_at.csv", RAW_ATTRIBUTES_COLUMNS),
    )
    store(result.quarantine, quarantine_table)
    store(result.bronze, bronze_table)
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 0 · raw CSVs -> bronze")
    parser.add_argument("--ingest-date", required=True, help="raw/ folder to read, YYYY-MM-DD")
    parser.add_argument("--raw-root", required=True, help="e.g. s3://<bucket>/raw")
    parser.add_argument("--bronze-table", required=True)
    parser.add_argument("--quarantine-table", required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    spark = SparkSession.builder.getOrCreate()
    result = run(spark, args.raw_root, args.ingest_date, args.bronze_table, args.quarantine_table)

    for warning in result.warnings:
        logger.warning(warning)
    logger.info(
        "bronze: %d rows · quarantine: %d rows · warnings: %d",
        spark.table(args.bronze_table).count(),
        spark.table(args.quarantine_table).count(),
        len(result.warnings),
    )


if __name__ == "__main__":
    main()
