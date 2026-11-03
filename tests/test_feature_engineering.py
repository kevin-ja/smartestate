"""
Pure functions of feature_engineering.py and storage.fs_offline_ddl, on Spark local[*]. PySpark is
not installed on the laptop: these run on Databricks until CI exists (plan, Phase 2).

    python -m pytest tests/test_feature_engineering.py   (from the repo root, so `src.` resolves)
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from pyspark.sql import SparkSession

from src.data_pipeline import feature_engineering as fe
from src.data_pipeline.contract.schema import FS_OFFLINE_SCHEMA, GOLD_SCHEMA, NLP_DIM, TAB_DIMS
from src.data_pipeline.storage import fs_offline_ddl


@pytest.fixture(scope="module")
def spark():
    return SparkSession.builder.master("local[2]").appName("test_feature_engineering").getOrCreate()


def _row(spot_id, sector_id, **values):
    base = {
        "spot_id": spot_id, "sector_id": sector_id, "is_candidate": True, "modality": "Rent",
        "latitude": 19.4, "longitude": -99.1, "area_sqm": 100.0, "maintenance_cost": 10.0,
        "charging_ports": 2, "parking_spaces": 3, "number_of_elevators": 2, "building_status": 1,
        "security_type": [1], "natural_light": 50, "text": f"spot {spot_id}", "has_description": True,
    }
    base.update(values)
    return tuple(base.get(name) for name in GOLD_SCHEMA.fieldNames())


@pytest.fixture(scope="module")
def gold(spark):
    rows = [
        _row(1, 9, area_sqm=100.0, floor_material="concreto"),
        _row(2, 9, area_sqm=300.0, maintenance_cost=None, building_status=None, natural_light=None),
        _row(3, 9, area_sqm=1e9, is_candidate=False),  # outlier: must not move the scaler
        _row(4, 11, security_type=[1, 3], modality="Sale"),
        _row(5, 11, security_type=None, number_of_elevators=4, modality="Rent & Sale"),
        _row(6, 12, parking_spaces=None, has_description=False),
        _row(7, 12, parking_spaces=10),
        _row(8, 15, area_sqm=5000.0),
    ]
    return spark.createDataFrame(rows, GOLD_SCHEMA)


class FakeEncoder:
    """Deterministic vectors; remembers the texts it got."""

    def __init__(self):
        self.texts: list[str] = []

    def __call__(self, texts):
        self.texts += texts
        return np.random.default_rng(0).normal(size=(len(texts), NLP_DIM)).astype(np.float32)


@pytest.fixture(scope="module")
def result(gold):
    encoder = FakeEncoder()
    out = fe.transform(gold, encoder)
    out.encoder = encoder
    return out


def _by_id(df):
    return {row["spot_id"]: row for row in df.collect()}


def test_schema_and_validation(result, gold):
    assert result.features.schema.fieldNames() == FS_OFFLINE_SCHEMA.fieldNames()
    with_description = gold.filter("has_description").count()
    assert fe.validate(result.features, gold.count(), with_description) == []


def test_tab_vector_dims_and_norm(result):
    for spot_id, row in _by_id(result.features).items():
        if row["sector_id"] in TAB_DIMS:
            assert len(row["tab_vector"]) == TAB_DIMS[row["sector_id"]]
            assert math.isclose(math.sqrt(sum(x * x for x in row["tab_vector"])), 1.0, abs_tol=1e-6)
        else:
            assert row["tab_vector"] is None, spot_id


def test_scaler_fits_on_candidates_only(result):
    scaler = result.models["industrial"].stages[3]
    assert scaler.mean[0] == pytest.approx(np.mean(np.log1p([100.0, 300.0])))


def test_null_is_zero_after_scaling(result, gold):
    scaled = _by_id(result.models["industrial"].transform(gold.filter("sector_id = 9")).select("spot_id", "_scaled"))
    assert scaled[2]["_scaled"][1] == pytest.approx(0.0)  # maintenance_cost null


def test_binaries(result, gold):
    office = _by_id(result.models["office"].stages[0].transform(gold.filter("sector_id = 11")))
    assert [office[4][c] for c in fe.SECURITY_COLUMNS] == [0, 1, 0, 1]
    assert [office[5][c] for c in fe.SECURITY_COLUMNS] == [1, 0, 0, 0]
    industrial = _by_id(result.models["industrial"].stages[0].transform(gold.filter("sector_id = 9")))
    assert [industrial[2][c] for c in fe.STATUS_COLUMNS] == [1, 0, 0, 0]
    assert industrial[2]["natural_light_pct"] == 0.0
    assert industrial[1]["natural_light_pct"] == 0.5


def test_modality_mask(result):
    rows = _by_id(result.features)
    assert (rows[1]["modality_mask"], rows[4]["modality_mask"], rows[5]["modality_mask"]) == (1, 2, 3)


def test_nlp_vector(result):
    rows = _by_id(result.features)
    assert rows[6]["nlp_vector"] is None
    assert len(rows[1]["nlp_vector"]) == NLP_DIM
    assert "spot 1. concreto" in result.encoder.texts  # floor_material only for Industrial (D18)


def test_cluster_id_is_null(result):
    assert result.features.filter("cluster_id IS NOT NULL").count() == 0


def test_validate_aborts_on_repeated_id(result, gold):
    doubled = result.features.unionByName(result.features.limit(1))
    with pytest.raises(fe.FeatureEngineeringError):
        fe.validate(doubled, gold.count(), gold.filter("has_description").count())


def test_fs_offline_ddl():
    ddl = fs_offline_ddl("smartestate.feature_store_offline.spots_fs_offline")
    assert "spot_id bigint NOT NULL" in ddl
    assert "tab_vector array<float>" in ddl
    assert "CONSTRAINT spots_fs_offline_pk PRIMARY KEY (spot_id)" in ddl
