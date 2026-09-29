"""
Stage 2 · Cleaning: smartestate.bronze.spots -> smartestate.silver.spots.

    Load -> Sector -> Candidates -> State -> Outliers -> Validation -> Storage

Design in src/data_pipeline/README.md. Corrects values and applies business rules; never drops a
row, it flags. Functions take and return Spark DataFrames, do not print and do not mutate their
input; only `load`/`read_catalog` read and only `store` writes.

Run:
    python -m src.data_pipeline.cleaning \
        --bronze-table smartestate.bronze.spots \
        --silver-table smartestate.silver.spots \
        --ref-root "$SMARTESTATE_REF_ROOT"
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql.types import StructType

from src.common import logs
from src.data_pipeline.contract.schema import BRONZE_SCHEMA, SILVER_SCHEMA
from src.data_pipeline.storage import store

logger = logging.getLogger(__name__)

# D1: the dictionary declares 12 -> Retail; the data carries 13 and no 12. Reversible through
# sector_id_original.
SECTOR_RECODE = {13: 12}
SILVER_SECTORS = (9, 11, 12, 15)

# D2: metadatos.pdf limits the use case to Single (1) and Subspace (3). Complex is the container.
COMPLEX_TYPE_ID = 2

# Partition key of Levels 1-3. A "Rent & Sale" spot belongs to both intentions.
INTENTIONS = {
    "Rent": ("rent_price_sqm", ("Rent", "Rent & Sale")),
    "Sale": ("sale_price_sqm", ("Sale", "Rent & Sale")),
}

# D11: IQR over log, same method the EDA used to measure outliers (outliers_precio, k=1.5).
OUTLIER_K = 1.5
# Below this many rows a group's IQR means nothing (Land Rent has 2): its rows are not flagged.
MIN_GROUP_ROWS = 20

# INEGI catalog under ref/ (D9). Local copy at ref/inegi/municipalities.csv, same path as the bucket.
INEGI_PATH = "inegi/municipalities.csv"
INEGI_COLUMNS = ["cvegeo", "state_code", "state_name", "municipality_code", "municipality_name"]

# Solidaridad (Quintana Roo) was renamed Playa del Carmen; the catalog only carries the new name.
MUNICIPALITY_ALIASES = {"solidaridad": "playa del carmen"}

_ACCENTED = "áàäâéèëêíìïîóòöôúùüûñ"
_PLAIN = "aaaaeeeeiiiioooouuuun"


class CleaningError(Exception):
    """Bronze breaks what cleaning expects, or cleaning broke the table. The run aborts."""


@dataclass
class CleaningResult:
    silver: DataFrame
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 0 · Load
# --------------------------------------------------------------------------


def check_schema(df: DataFrame, contract: StructType, layer: str) -> None:
    """Abort unless names and types match the layer's contract. Nullability is not compared."""
    expected = [(f.name, f.dataType) for f in contract.fields]
    actual = [(f.name, f.dataType) for f in df.schema.fields]
    if actual != expected:
        raise CleaningError(f"{layer} does not match its contract. Expected {expected}, got {actual}")


def load(spark: SparkSession, table: str) -> DataFrame:
    df = spark.table(table)
    check_schema(df, BRONZE_SCHEMA, "Bronze")
    return df


def read_catalog(spark: SparkSession, ref_root: str) -> DataFrame:
    """INEGI municipality -> state catalog, as strings. A different header aborts."""
    path = f"{ref_root.rstrip('/')}/{INEGI_PATH}"
    df = spark.read.option("header", True).option("encoding", "UTF-8").csv(path)
    if df.columns != INEGI_COLUMNS:
        raise CleaningError(f"Header of {path} is {df.columns}, expected {INEGI_COLUMNS}")
    return df


# --------------------------------------------------------------------------
# 1 · Sector  +  2 · Candidates
# --------------------------------------------------------------------------


def recode_sector(df: DataFrame) -> DataFrame:
    """D1: 13 -> 12 (Retail). The original code stays in sector_id_original."""
    recoded = F.col("sector_id")
    for source, target in SECTOR_RECODE.items():
        recoded = F.when(F.col("sector_id") == source, F.lit(target)).otherwise(recoded)
    return df.withColumn("sector_id_original", F.col("sector_id")).withColumn("sector_id", recoded)


def flag_candidates(df: DataFrame) -> DataFrame:
    """D2: a Complex is never a candidate. Outliers switch the flag off later (step 4)."""
    return df.withColumn("is_candidate", ~F.col("type_id").eqNullSafe(COMPLEX_TYPE_ID))


# --------------------------------------------------------------------------
# 3 · State
# --------------------------------------------------------------------------


def _name_key(column: str):
    """Join key: lowercase, no accents, single spaces."""
    lowered = F.lower(F.trim(F.col(column)))
    return F.regexp_replace(F.translate(lowered, _ACCENTED, _PLAIN), r"\s+", " ")


def _municipality_key(df: DataFrame) -> DataFrame:
    key = _name_key("municipality")
    for alias, name in MUNICIPALITY_ALIASES.items():
        key = F.when(key == alias, F.lit(name)).otherwise(key)
    return df.withColumn("_mun_key", key)


def fill_state(df: DataFrame, catalog: DataFrame) -> DataFrame:
    """
    Fills empty `state` from the INEGI catalog; a declared state is never overwritten.

    - Name unique in the catalog -> its state.
    - Name in several states -> state of the nearest (municipality, state) centroid observed in the
      batch with a valid declared state. 2-3 candidates per row: linear, no all-pairs distance.
    - Otherwise it stays null.
    Adds state_mismatch: a declared state the catalog does not pair with the municipality.
    """
    pairs = (
        catalog.select(_name_key("municipality_name").alias("_mun_key"), F.col("state_name").alias("state"))
        .distinct()
    )
    unique = (
        pairs.groupBy("_mun_key")
        .agg(F.count("*").alias("_n"), F.first("state").alias("_unique_state"))
        .select("_mun_key", "_unique_state", (F.col("_n") > 1).alias("_ambiguous"))
    )
    keyed = _municipality_key(df)

    valid = keyed.join(pairs, ["_mun_key", "state"], "left_semi")
    centroids = valid.groupBy("_mun_key", F.col("state").alias("_centroid_state")).agg(
        F.avg("latitude").alias("_c_lat"), F.avg("longitude").alias("_c_lon")
    )
    # Equirectangular distance: enough to rank 2-3 centroids of the same name.
    dx = (F.col("longitude") - F.col("_c_lon")) * F.cos(F.radians(F.col("latitude")))
    dy = F.col("latitude") - F.col("_c_lat")
    nearest = (
        keyed.filter(F.col("state").isNull())
        .join(unique.filter("_ambiguous"), "_mun_key")
        .join(centroids, "_mun_key")
        .withColumn("_rank", F.row_number().over(
            Window.partitionBy("spot_id").orderBy(dx * dx + dy * dy, "_centroid_state")
        ))
        .filter("_rank = 1")
        .select("spot_id", F.col("_centroid_state").alias("_nearest_state"))
    )

    declared_valid = pairs.withColumn("_valid", F.lit(True))
    return (
        keyed.join(unique, "_mun_key", "left")
        .join(nearest, "spot_id", "left")
        .join(declared_valid, ["_mun_key", "state"], "left")
        .withColumn(
            "state_mismatch",
            F.col("state").isNotNull() & F.col("_unique_state").isNotNull() & F.col("_valid").isNull(),
        )
        .withColumn(
            "state",
            F.coalesce(
                F.col("state"),
                F.when(~F.col("_ambiguous"), F.col("_unique_state")),
                F.col("_nearest_state"),
            ),
        )
        .select(*df.columns, "state_mismatch")
    )


# --------------------------------------------------------------------------
# 4 · Outliers
# --------------------------------------------------------------------------


def _outlier_flags(values: DataFrame, keys: list[str]) -> DataFrame:
    """
    IQR over log within each `keys` group. `values` has spot_id, keys, is_candidate and `value`
    (non-null). Bounds come from candidates only and every row is checked against them (D11).
    A value <= 0 has no log and counts as an outlier. A group under MIN_GROUP_ROWS gets no bounds.
    Returns spot_id with `_outlier`.
    """
    positive = values.filter("value > 0 AND is_candidate").withColumn("_log", F.log("value"))
    bounds = positive.groupBy(*keys).agg(
        F.expr("percentile(_log, array(0.25, 0.75))").alias("_q"), F.count("*").alias("_n")
    ).filter(F.col("_n") >= MIN_GROUP_ROWS).select(
        *keys,
        (F.col("_q")[0] - OUTLIER_K * (F.col("_q")[1] - F.col("_q")[0])).alias("_low"),
        (F.col("_q")[1] + OUTLIER_K * (F.col("_q")[1] - F.col("_q")[0])).alias("_high"),
    )
    log_value = F.log(F.when(F.col("value") > 0, F.col("value")))
    return values.join(bounds, keys, "left").select(
        "spot_id",
        (
            (F.col("value") <= 0) | (log_value < F.col("_low")) | (log_value > F.col("_high"))
        ).alias("_outlier"),
    )


def flag_outliers(df: DataFrame) -> DataFrame:
    """
    is_price_outlier: price/m2 per sector x intention (a Rent & Sale spot is checked on both).
    is_area_outlier: area per sector. Bounds from candidates only: a Complex (a whole building)
    would stretch them until nothing is flagged. Either flag switches is_candidate off. Null prices
    are structural and never flagged.
    """
    prices = None
    for intention, (column, modalities) in INTENTIONS.items():
        block = df.filter(F.col("modality").isin(*modalities) & F.col(column).isNotNull()).select(
            "spot_id", "sector_id", "is_candidate", F.lit(intention).alias("intention"),
            F.col(column).alias("value"),
        )
        prices = block if prices is None else prices.unionByName(block)

    price_flags = (
        _outlier_flags(prices, ["sector_id", "intention"])
        .groupBy("spot_id")
        .agg(F.max(F.col("_outlier").cast("int")).alias("_price"))
    )
    area_flags = _outlier_flags(
        df.filter(F.col("area_sqm").isNotNull()).select(
            "spot_id", "sector_id", "is_candidate", F.col("area_sqm").alias("value")
        ),
        ["sector_id"],
    ).select("spot_id", F.col("_outlier").cast("int").alias("_area"))

    return (
        df.join(price_flags, "spot_id", "left")
        .join(area_flags, "spot_id", "left")
        .withColumn("is_price_outlier", F.coalesce(F.col("_price") == 1, F.lit(False)))
        .withColumn("is_area_outlier", F.coalesce(F.col("_area") == 1, F.lit(False)))
        .withColumn(
            "is_candidate",
            F.col("is_candidate") & ~F.col("is_price_outlier") & ~F.col("is_area_outlier"),
        )
        .drop("_price", "_area")
    )


# --------------------------------------------------------------------------
# 5 · Validation
# --------------------------------------------------------------------------


def validate(silver: DataFrame, bronze_rows: int) -> list[str]:
    """
    Aborts if silver breaks its contract, rows were lost or invented, or a sector is off the
    catalog. Returns warnings.
    """
    check_schema(silver, SILVER_SCHEMA, "Silver")
    stats = silver.agg(
        F.count("*").alias("rows"),
        F.countDistinct("spot_id").alias("ids"),
        F.sum((~F.col("sector_id").isin(*SILVER_SECTORS) | F.col("sector_id").isNull()).cast("int"))
        .alias("bad_sector"),
        F.sum(F.col("state").isNull().cast("int")).alias("null_state"),
        F.sum(F.col("state_mismatch").cast("int")).alias("mismatch"),
    ).first()

    errors = []
    if stats["rows"] != bronze_rows or stats["ids"] != bronze_rows:
        errors.append(f"rows: bronze {bronze_rows}, silver {stats['rows']} ({stats['ids']} distinct ids)")
    if stats["bad_sector"]:
        errors.append(f"sector_id: {stats['bad_sector']} rows outside {SILVER_SECTORS}")
    if errors:
        raise CleaningError("Validation failed:\n  " + "\n  ".join(errors))

    warnings = []
    if stats["null_state"]:
        warnings.append(f"state: {stats['null_state']} rows still null after INEGI")
    if stats["mismatch"]:
        warnings.append(f"state: {stats['mismatch']} rows where municipality and state disagree")
    return warnings


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def transform(bronze: DataFrame, catalog: DataFrame) -> DataFrame:
    """Sector -> Candidates -> State -> Outliers, with no I/O."""
    df = recode_sector(bronze)
    df = flag_candidates(df)
    df = fill_state(df, catalog)
    return flag_outliers(df)


def run(spark: SparkSession, bronze_table: str, silver_table: str, ref_root: str) -> CleaningResult:
    """Reads bronze and the catalog, cleans, validates and writes silver. Nothing is written on abort."""
    bronze = load(spark, bronze_table)
    silver = transform(bronze, read_catalog(spark, ref_root))
    warnings = validate(silver, bronze.count())
    store(silver, silver_table)
    return CleaningResult(silver, warnings)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 2 · bronze -> silver")
    parser.add_argument("--bronze-table", required=True)
    parser.add_argument("--silver-table", required=True)
    parser.add_argument("--ref-root", required=True, help="e.g. s3://<bucket>/ref")
    args = parser.parse_args(argv)

    logs.configure()
    spark = SparkSession.builder.getOrCreate()
    result = run(spark, args.bronze_table, args.silver_table, args.ref_root)

    for warning in result.warnings:
        logger.warning(warning)
    logger.info("silver: %d rows · warnings: %d", spark.table(args.silver_table).count(), len(result.warnings))


if __name__ == "__main__":
    main()
