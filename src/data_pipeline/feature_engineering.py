"""
Stage 4 · Feature engineering: gold (in memory) -> Spots FS offline rows.

    Tabular (one pyspark.ml Pipeline per sector) -> modality_mask -> Text (SBERT) -> Validation

Design in new_arch.md §3.2.1. Functions take and return Spark DataFrames, do not print and do not
mutate their input; nothing here reads or writes storage. The SBERT model comes in as an `encode`
callable, so tests can pass a fake one.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from pyspark.ml import Pipeline, PipelineModel
from pyspark.ml.feature import Imputer, Normalizer, SQLTransformer, StandardScaler, VectorAssembler
from pyspark.ml.functions import vector_to_array
from pyspark.sql import DataFrame
from pyspark.sql import functions as F
from pyspark.sql.types import ArrayType, FloatType, LongType, StructField, StructType

from src.data_pipeline.contract.schema import (
    FS_OFFLINE_KEY,
    FS_OFFLINE_SCHEMA,
    NLP_DIM,
    NORM_TOLERANCE,
    TAB_DIMS,
)

Encoder = Callable[[list[str]], np.ndarray]

# Same separator as preprocessing.py. Copied, not imported: no stage imports another.
TEXT_SEPARATOR = ". "

SBERT_MAX_SEQ_LENGTH = 128
SBERT_BATCH_SIZE = 64

MODALITY_BITS = {"Rent": 1, "Sale": 2, "Rent & Sale": 3}

STATUS_COLUMNS = tuple(f"status_{code}" for code in range(4))      # 0 = no data
SECURITY_COLUMNS = tuple(f"security_{code}" for code in range(4))  # 0 = no data
SECURITY_CODES = (1, 2, 3)


class FeatureEngineeringError(Exception):
    """The features break the Spots FS offline contract. The run aborts."""


@dataclass(frozen=True)
class SectorSpec:
    name: str
    log_columns: tuple[str, ...]    # log1p -> impute -> scale
    plain_columns: tuple[str, ...]  # impute -> scale
    binary_columns: tuple[str, ...] # already in [0, 1], appended after scaling


# new_arch.md §3.2.1. Office has no building_status (0.9). Land has no tabular vector.
SECTOR_SPECS = {
    9: SectorSpec(
        "industrial", ("area_sqm", "maintenance_cost", "charging_ports"), (),
        STATUS_COLUMNS + ("natural_light_pct",),
    ),
    11: SectorSpec("office", ("area_sqm", "maintenance_cost"), ("number_of_elevators",), SECURITY_COLUMNS),
    12: SectorSpec("retail", ("area_sqm", "maintenance_cost", "parking_spaces"), (), STATUS_COLUMNS),
}


@dataclass
class FeatureResult:
    features: DataFrame                # FS_OFFLINE_SCHEMA columns, in order
    models: dict[str, PipelineModel]   # sector name -> fitted Pipeline
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 1 · Tabular
# --------------------------------------------------------------------------


def _binary_sql() -> list[str]:
    """One-hot building_status and multi-hot security_type; an empty or null value is code 0."""
    status = [f"CAST(coalesce(building_status, 0) = {code} AS DOUBLE) AS status_{code}" for code in range(4)]
    security = ["CAST(security_type IS NULL OR size(security_type) = 0 AS DOUBLE) AS security_0"] + [
        f"CAST(coalesce(array_contains(security_type, {code}), false) AS DOUBLE) AS security_{code}"
        for code in SECURITY_CODES
    ]
    light = ["coalesce(natural_light / 100.0, 0.0) AS natural_light_pct"]
    return status + security + light


def build_pipeline(spec: SectorSpec) -> Pipeline:
    """
    log1p -> mean imputation -> StandardScaler -> + binaries -> L2. Imputing the mean before scaling
    leaves a null at exactly 0 after scaling (the sector mean), inside the fitted model.
    """
    raw = [f"_{c}_raw" for c in spec.log_columns + spec.plain_columns]
    imputed = [f"_{c}_imputed" for c in spec.log_columns + spec.plain_columns]
    expressions = (
        [f"log1p({c}) AS _{c}_raw" for c in spec.log_columns]
        + [f"CAST({c} AS DOUBLE) AS _{c}_raw" for c in spec.plain_columns]
        + _binary_sql()
    )
    return Pipeline(
        stages=[
            SQLTransformer(statement=f"SELECT *, {', '.join(expressions)} FROM __THIS__"),
            Imputer(strategy="mean", inputCols=raw, outputCols=imputed),
            VectorAssembler(inputCols=imputed, outputCol="_numeric"),
            StandardScaler(withMean=True, withStd=True, inputCol="_numeric", outputCol="_scaled"),
            VectorAssembler(inputCols=["_scaled", *spec.binary_columns], outputCol="_raw_vector"),
            Normalizer(p=2.0, inputCol="_raw_vector", outputCol="_tab_vector"),
        ]
    )


def tab_vectors(gold: DataFrame) -> tuple[DataFrame, dict[str, PipelineModel]]:
    """
    Fits one Pipeline per sector on its candidates (outliers and copies would move mean and std) and
    applies it to every spot of the sector. Returns (spot_id, tab_vector) and the fitted models.
    """
    vectors, models = [], {}
    for sector, spec in SECTOR_SPECS.items():
        rows = gold.filter(F.col("sector_id") == sector)
        model = build_pipeline(spec).fit(rows.filter("is_candidate"))
        models[spec.name] = model
        vectors.append(
            model.transform(rows).select(
                FS_OFFLINE_KEY, vector_to_array("_tab_vector", "float32").alias("tab_vector")
            )
        )
    union = vectors[0]
    for frame in vectors[1:]:
        union = union.unionByName(frame)
    return union, models


def modality_mask(df: DataFrame) -> DataFrame:
    """Rent = 1, Sale = 2, Rent & Sale = 3 (bits of an int). Encoded only here (new_arch.md §3.2.1)."""
    mask = F.lit(None).cast("int")
    for modality, bits in MODALITY_BITS.items():
        mask = F.when(F.col("modality") == modality, F.lit(bits)).otherwise(mask)
    return df.withColumn("modality_mask", mask)


# --------------------------------------------------------------------------
# 2 · Text
# --------------------------------------------------------------------------


def load_encoder(model_dir: Path) -> Encoder:
    """SentenceTransformer from a local folder, offline, on CPU. Loaded once, on the driver."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from sentence_transformers import SentenceTransformer  # heavy import, only where it runs

    model = SentenceTransformer(str(model_dir), device="cpu")
    model.max_seq_length = SBERT_MAX_SEQ_LENGTH

    def encode(texts: list[str]) -> np.ndarray:
        return model.encode(texts, batch_size=SBERT_BATCH_SIZE, convert_to_numpy=True, show_progress_bar=False)

    return encode


def sbert_text(df: DataFrame) -> DataFrame:
    """text (title + description) plus floor_material for Industrial (D18)."""
    floor = F.when(F.col("sector_id") == 9, F.col("floor_material"))
    return df.withColumn("sbert_text", F.concat_ws(TEXT_SEPARATOR, "text", floor))


def l2_normalize(vectors: np.ndarray) -> np.ndarray:
    """In float64, so the float32 copy keeps its norm within NORM_TOLERANCE."""
    vectors = vectors.astype(np.float64)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def nlp_vectors(gold: DataFrame, encode: Encoder) -> DataFrame:
    """
    (spot_id, nlp_vector) for spots with a description; the rest get no row, so null after the join
    (D3). Runs on the driver: the cluster is single node and the batch is ~2,500 texts.
    """
    rows = (
        sbert_text(gold).filter("has_description").select(FS_OFFLINE_KEY, "sbert_text")
        .orderBy(FS_OFFLINE_KEY).toPandas()
    )
    vectors = l2_normalize(encode(rows["sbert_text"].tolist())) if len(rows) else np.empty((0, NLP_DIM))
    schema = StructType([StructField(FS_OFFLINE_KEY, LongType(), False), StructField("nlp_vector", ArrayType(FloatType()))])
    frame = pd.DataFrame({FS_OFFLINE_KEY: rows[FS_OFFLINE_KEY], "nlp_vector": [v.tolist() for v in vectors]})
    return gold.sparkSession.createDataFrame(frame, schema)


# --------------------------------------------------------------------------
# 3 · Validation
# --------------------------------------------------------------------------


def check_security_codes(gold: DataFrame) -> list[str]:
    """A code outside SECURITY_CODES has no column in the multi-hot: it would be dropped silently."""
    known = ", ".join(map(str, SECURITY_CODES))
    unknown = gold.filter(F.exists("security_type", lambda c: ~c.isin(*SECURITY_CODES))).count()
    return [f"security_type: {unknown} rows carry codes outside ({known}), ignored by the multi-hot"] if unknown else []


def _norm_off(column: str):
    """True when the array's L2 norm is off 1 by more than NORM_TOLERANCE."""
    norm = F.sqrt(F.aggregate(column, F.lit(0.0), lambda acc, x: acc + x.cast("double") * x.cast("double")))
    return F.abs(norm - 1.0) > NORM_TOLERANCE


def validate(features: DataFrame, gold_rows: int, with_description: int) -> list[str]:
    """Aborts if the rows break FS_OFFLINE_SCHEMA or the vector rules (plan 2.7). Returns warnings."""
    expected = [(f.name, f.dataType) for f in FS_OFFLINE_SCHEMA.fields]
    actual = [(f.name, f.dataType) for f in features.schema.fields]
    if actual != expected:
        raise FeatureEngineeringError(f"Features do not match FS_OFFLINE_SCHEMA. Expected {expected}, got {actual}")

    tab_sector = F.col("sector_id").isin(*TAB_DIMS)
    tab_dim = F.lit(None).cast("int")
    for sector, dim in TAB_DIMS.items():
        tab_dim = F.when(F.col("sector_id") == sector, F.lit(dim)).otherwise(tab_dim)

    stats = features.agg(
        F.count("*").alias("rows"),
        F.countDistinct(FS_OFFLINE_KEY).alias("ids"),
        F.sum(F.col(FS_OFFLINE_KEY).isNull().cast("int")).alias("null_ids"),
        F.sum((tab_sector & ~F.coalesce(F.size("tab_vector") == tab_dim, F.lit(False))).cast("int")).alias("bad_tab_dim"),
        F.sum((~tab_sector & F.col("tab_vector").isNotNull()).cast("int")).alias("unexpected_tab"),
        F.sum((F.col("tab_vector").isNotNull() & _norm_off("tab_vector")).cast("int")).alias("bad_tab_norm"),
        F.sum(F.col("nlp_vector").isNotNull().cast("int")).alias("with_nlp"),
        F.sum((F.col("nlp_vector").isNotNull() & (F.size("nlp_vector") != NLP_DIM)).cast("int")).alias("bad_nlp_dim"),
        F.sum((F.col("nlp_vector").isNotNull() & _norm_off("nlp_vector")).cast("int")).alias("bad_nlp_norm"),
        F.sum((F.col("is_candidate") & F.col("modality_mask").isNull()).cast("int")).alias("candidates_no_mask"),
    ).first()

    errors = []
    if stats["rows"] != gold_rows or stats["ids"] != gold_rows or stats["null_ids"]:
        errors.append(f"rows: gold {gold_rows}, features {stats['rows']} ({stats['ids']} distinct ids, {stats['null_ids']} null)")
    if stats["bad_tab_dim"]:
        errors.append(f"tab_vector: {stats['bad_tab_dim']} rows with a length other than {TAB_DIMS}")
    if stats["unexpected_tab"]:
        errors.append(f"tab_vector: {stats['unexpected_tab']} rows outside {sorted(TAB_DIMS)} with a vector")
    if stats["bad_tab_norm"]:
        errors.append(f"tab_vector: {stats['bad_tab_norm']} rows with norm off 1 ± {NORM_TOLERANCE}")
    if stats["with_nlp"] != with_description:
        errors.append(f"nlp_vector: {stats['with_nlp']} rows, expected {with_description} (has_description)")
    if stats["bad_nlp_dim"]:
        errors.append(f"nlp_vector: {stats['bad_nlp_dim']} rows with a length other than {NLP_DIM}")
    if stats["bad_nlp_norm"]:
        errors.append(f"nlp_vector: {stats['bad_nlp_norm']} rows with norm off 1 ± {NORM_TOLERANCE}")
    if errors:
        raise FeatureEngineeringError("Validation failed:\n  " + "\n  ".join(errors))

    if stats["candidates_no_mask"]:
        return [f"modality_mask: {stats['candidates_no_mask']} candidates without a modality never pass Filter 2"]
    return []


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def transform(gold: DataFrame, encode: Encoder) -> FeatureResult:
    """Tabular -> modality_mask -> Text, with no I/O. Output columns follow FS_OFFLINE_SCHEMA."""
    tab, models = tab_vectors(gold)
    nlp = nlp_vectors(gold, encode)
    features = (
        modality_mask(gold)
        .join(tab, FS_OFFLINE_KEY, "left")
        .join(nlp, FS_OFFLINE_KEY, "left")
        .withColumn("cluster_id", F.lit(None).cast("int"))
        .select(*FS_OFFLINE_SCHEMA.fieldNames())
    )
    return FeatureResult(features, models, check_security_codes(gold))
