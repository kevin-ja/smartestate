"""
Unit tests of the training job (smartestate_train), one section per task:

    T1 · data_pipeline   feature_engineering.py and storage.fs_offline_ddl, on Spark local[*]
    T2 · modeling        GeoIndex and K-means (plan, Phase 3)
    T3 · materialization cleanup of old runs (plan, Phase 4)

T2 and T3 are not written yet: their tests skip themselves (importorskip) until the module exists,
and then they run as the contract of its API. Names follow the plan; rename here if the code differs.

They run in Docker before each deploy (tests/Dockerfile, nothing installed on the laptop) and again on
Databricks, in the job's unit_tests task, until CI exists (plan, Phase 2). See tests/README.md.

    docker build -f tests/Dockerfile -t smartestate-tests . && docker run --rm smartestate-tests
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path

import numpy as np
import pytest
from pyspark.sql import SparkSession

from src.common import run_monitor, spark_probes
from src.data_pipeline import feature_engineering as fe
from src.data_pipeline.contract.schema import FS_OFFLINE_SCHEMA, GOLD_SCHEMA, NLP_DIM, TAB_DIMS
from src.data_pipeline.storage import fs_offline_ddl


@pytest.fixture(scope="module")
def spark():
    return SparkSession.builder.master("local[2]").appName("test_training_pipeline").getOrCreate()


# ── T1 · data_pipeline ────────────────────────────────────────────────────────


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


def test_encode_with_progress_keeps_order(monkeypatch):
    monkeypatch.setattr(fe, "SBERT_PROGRESS_CHUNK", 3)
    chunks = []

    def encode(texts):
        chunks.append(len(texts))
        return np.array([[float(text)] for text in texts])

    vectors = fe.encode_with_progress(encode, [str(i) for i in range(8)])
    assert chunks == [3, 3, 2]
    assert vectors.ravel().tolist() == [float(i) for i in range(8)]


def test_stage_records_time_and_keeps_the_error(caplog):
    caplog.set_level(logging.INFO, logger=run_monitor.__name__)
    with pytest.raises(ValueError):
        with run_monitor.stage("test_failing_stage"):
            raise ValueError("boom")
    assert run_monitor.durations()["test_failing_stage"] >= 0
    assert "stage failed: test_failing_stage" in caplog.text
    run_monitor.log_summary()
    assert "test_failing_stage" in caplog.text


def test_spark_probes_measure_each_factor(spark):
    lineage = spark.range(0, 20, 1, 2).cache()
    spark_probes.run(lineage, hold_seconds=0)
    names = run_monitor.durations()
    for probe in ("plain_8", "plain_200", "cache_200_fill", "cache_200_read", "lineage_8_read", "lineage_200_fill", "lineage_200_read"):
        assert f"probe_{probe}" in names


def test_spark_probes_never_fail_the_run(caplog):
    class BrokenFrame:
        @property
        def sparkSession(self):
            raise RuntimeError("boom")

    caplog.set_level(logging.WARNING, logger=spark_probes.__name__)
    spark_probes.run(BrokenFrame(), hold_seconds=0)
    assert "Spark probes stopped: boom" in caplog.text


@pytest.mark.skipif(not Path("/proc/meminfo").exists(), reason="/proc only exists on Linux")
def test_sample_resources_logs_machine_state(caplog):
    caplog.set_level(logging.INFO, logger=run_monitor.__name__)
    with run_monitor.sample_resources(interval=0.05):
        with run_monitor.stage("test_sampled_stage"):
            time.sleep(0.3)
    assert "resources stage=test_sampled_stage" in caplog.text
    assert "Resource sampling stopped" not in caplog.text


# ── T2 · modeling (plan, Phase 3) ─────────────────────────────────────────────
# Expected API (not written yet):
#   src/common/geo_index.py      GeoIndex(spot_id=, latitude=, longitude=, sector=, modality_mask=)
#                                .query(lat, lon, r_km, sector, mask) -> list[int]
#   src/ml_pipeline/modeling.py  cluster_sector(vectors, seed=0) -> (labels, k), k in 5–8 by silhouette
#                                with_cluster_id(fs, clusters) -> fs with cluster_id (null if not candidate)

KM = 1 / 111.0  # degrees of latitude per km, enough for a test


@pytest.fixture(scope="module")
def geo_index():
    geo = pytest.importorskip("src.common.geo_index")
    spots = [  # spot_id, km north of the click, sector_id, modality_mask
        (101, 0.5, 11, 1),   # Rent: passes
        (102, 1.0, 11, 3),   # Rent & Sale: shares Rent, passes
        (103, 1.0, 11, 2),   # Sale only: fails Filter 2 for a Rent click
        (104, 1.0, 9, 1),    # other sector: fails Filter 2
        (105, 20.0, 11, 1),  # too far: fails Filter 1
    ]
    ids, km, sector, mask = (np.array(column) for column in zip(*spots))
    return geo.GeoIndex(
        spot_id=ids.astype(np.int64), latitude=19.4 + km * KM, longitude=np.full(len(ids), -99.1),
        sector=sector.astype(np.int8), modality_mask=mask.astype(np.uint8),
    )


def test_geo_index_radius_sector_and_modality(geo_index):
    assert sorted(geo_index.query(19.4, -99.1, 5.0, 11, 1)) == [101, 102]


def test_geo_index_rent_and_sale_click_matches_both(geo_index):
    assert sorted(geo_index.query(19.4, -99.1, 5.0, 11, 3)) == [101, 102, 103]


def test_geo_index_roundtrip(geo_index, tmp_path):
    joblib = pytest.importorskip("joblib")
    path = tmp_path / "geo_index.joblib"
    joblib.dump(geo_index, path)
    assert sorted(joblib.load(path).query(19.4, -99.1, 5.0, 11, 1)) == [101, 102]


def test_kmeans_k_by_silhouette_and_deterministic():
    modeling = pytest.importorskip("src.ml_pipeline.modeling")
    rng = np.random.default_rng(0)
    centers = np.eye(7)[:6] * 10  # six far-apart groups of 10 spots
    vectors = np.vstack([c + rng.normal(scale=0.1, size=(10, 7)) for c in centers])
    labels, k = modeling.cluster_sector(vectors, seed=0)
    assert k == 6 and len(labels) == len(vectors)
    assert (modeling.cluster_sector(vectors, seed=0)[0] == labels).all()


def test_cluster_id_only_for_candidates(result, gold):
    modeling = pytest.importorskip("src.ml_pipeline.modeling")
    clusters = gold.filter("is_candidate").selectExpr("spot_id", "0 AS cluster_id")
    fs = modeling.with_cluster_id(result.features, clusters)
    assert [(f.name, f.dataType) for f in fs.schema] == [(f.name, f.dataType) for f in FS_OFFLINE_SCHEMA]
    rows = _by_id(fs)
    assert rows[3]["cluster_id"] is None  # spot 3 is the outlier, not a candidate
    assert all(row["cluster_id"] == 0 for spot_id, row in rows.items() if spot_id != 3)


# ── T3 · materialization (plan, Phase 4) ──────────────────────────────────────
# Expected API (not written yet):
#   src/ml_pipeline/materialization.py  to_drop(names, prefix, keep=3) -> names to delete, oldest first
# Same rule on both sides (P31): spots_fs_online_<run_id> in DynamoDB and runs/<run_id>/ in S3.
# publish_table and the GetItem smoke test (plan 4.3) need real AWS: they are runtime checks, not here.


def test_cleanup_keeps_last_three_by_number():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    tables = ["spots_fs_online_9", "spots_fs_online_100", "spots_fs_online_25", "spots_fs_online_1000", "other"]
    # As text, "9" would sort as the newest; run_id is a number.
    assert materialization.to_drop(tables, "spots_fs_online_") == ["spots_fs_online_9"]


def test_cleanup_ignores_folders_that_are_not_runs():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    assert materialization.to_drop(["9", "100", "25", "1000", "_probe"], "") == ["9"]
