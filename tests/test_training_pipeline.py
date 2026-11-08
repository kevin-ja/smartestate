"""
Unit tests of the training job (smartestate_train), one section per task:

    T1 · data_pipeline   feature_engineering.py and storage.fs_offline_ddl, on Spark local[*]
    T2 · modeling        GeoIndex and K-means (plan, Phase 3)
    T3 · materialization cleanup of old runs (plan, Phase 4)

They run in Docker before each deploy (tests/Dockerfile, nothing installed on the laptop) and again on
Databricks, in the job's unit_tests task, until CI exists (plan, Phase 2). See tests/README.md.

    docker build -f tests/Dockerfile -t smartestate-tests . && docker run --rm smartestate-tests
"""

from __future__ import annotations

import logging
import math
import time
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

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
    model, _ = modeling.fit_kmeans(vectors, seed=0)
    assert model.n_clusters == 6 and len(model.labels_) == len(vectors)
    assert (modeling.fit_kmeans(vectors, seed=0)[0].labels_ == model.labels_).all()


def _candidates(tabs, texts, sector_id=9):
    return pd.DataFrame({
        "spot_id": range(1, len(texts) + 1),
        "sector_id": sector_id,
        "tab_vector": tabs if tabs is not None else [None] * len(texts),
        "nlp_vector": texts,
    })


def _two_groups(seed=0):
    """Eight spots near tab A with text near T, eight near tab B with text near U."""
    rng = np.random.default_rng(seed)
    tab_a, tab_b, text_t, text_u = np.eye(4)
    noise = lambda: rng.normal(scale=0.01, size=4)  # noqa: E731
    tabs = [tab_a + noise() for _ in range(8)] + [tab_b + noise() for _ in range(8)]
    texts = [text_t + noise() for _ in range(8)] + [text_u + noise() for _ in range(8)]
    return tabs, texts, tab_a, tab_b


def test_spot_without_text_goes_to_its_tab_neighbours():
    modeling = pytest.importorskip("src.ml_pipeline.modeling")
    tabs, texts, tab_a, tab_b = _two_groups()
    # Two spots without text, one next to each group. Zeros would put them in the same place.
    frame = _candidates([*tabs, tab_a, tab_b], [*texts, None, None])
    out = modeling.cluster_candidates(frame).clusters.set_index("spot_id")["cluster_id"]
    assert out.loc[17] in set(out.loc[1:8]) and out.loc[18] in set(out.loc[9:16])
    assert out.loc[17] != out.loc[18]


def test_spot_without_text_does_not_train_the_model():
    modeling = pytest.importorskip("src.ml_pipeline.modeling")
    tabs, texts, tab_a, _ = _two_groups()
    alone = modeling.cluster_candidates(_candidates(tabs, texts))
    with_extra = modeling.cluster_candidates(_candidates([*tabs, *[tab_a] * 30], [*texts, *[None] * 30]))
    np.testing.assert_array_equal(alone.models[9].cluster_centers_, with_extra.models[9].cluster_centers_)


def test_spread_counts_rows_with_and_without_text():
    modeling = pytest.importorskip("src.ml_pipeline.modeling")
    tabs, texts, tab_a, _ = _two_groups()
    spread = modeling.cluster_candidates(_candidates([*tabs, tab_a], [*texts, None])).spread["9"]
    assert sum(c["with_text"] for c in spread.values()) == 16
    assert sum(c["without_text"] for c in spread.values()) == 1


def test_land_clusters_on_text_alone():
    modeling = pytest.importorskip("src.ml_pipeline.modeling")
    _, texts, _, _ = _two_groups()
    result = modeling.cluster_candidates(_candidates(None, texts, sector_id=15))
    assert result.models[15].cluster_centers_.shape[1] == 4 and len(result.clusters) == 16


def test_candidate_without_any_vector_aborts():
    modeling = pytest.importorskip("src.ml_pipeline.modeling")
    _, texts, _, _ = _two_groups()
    with pytest.raises(modeling.ModelingError, match="neither tab_vector nor text"):
        modeling.cluster_candidates(_candidates(None, [*texts, None], sector_id=15))


def test_sector_with_too_few_rows_aborts():
    modeling = pytest.importorskip("src.ml_pipeline.modeling")
    with pytest.raises(modeling.ModelingError, match="too few"):
        modeling.fit_kmeans(np.eye(4))


def test_cluster_id_only_for_candidates(result, gold):
    modeling = pytest.importorskip("src.ml_pipeline.modeling")
    clusters = gold.filter("is_candidate").selectExpr("spot_id", "0 AS cluster_id")
    fs = modeling.with_cluster_id(result.features, clusters)
    assert [(f.name, f.dataType) for f in fs.schema] == [(f.name, f.dataType) for f in FS_OFFLINE_SCHEMA]
    rows = _by_id(fs)
    assert rows[3]["cluster_id"] is None  # spot 3 is the outlier, not a candidate
    assert all(row["cluster_id"] == 0 for spot_id, row in rows.items() if spot_id != 3)


def test_hyperparameters_match_what_the_kmeans_uses():
    modeling = pytest.importorskip("src.ml_pipeline.modeling")
    assert modeling.hyperparameters() == {"kmeans_k_min": 5, "kmeans_k_max": 8, "kmeans_seed": 0, "kmeans_n_init": 10}


# ── T3 · materialization (plan, Phase 4) ──────────────────────────────────────
# Same rule on both sides (P31): spots_fs_online_<run_id> in DynamoDB and runs/<run_id>/ in S3.
# publish_table and the real GetItem (plan 4.3) need AWS: they are runtime checks, not here.


# The real run_ids and order of runs 1-5 (data_pipeline.md): the number does not follow the date.
RUNS_BY_DATE = {"652070532920363": 1.0, "726424880789805": 2.0, "304335789918515": 3.0, "508031956134800": 4.0, "669016108687066": 5.0}


def test_cleanup_orders_by_date_not_by_run_id():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    assert materialization.to_drop(RUNS_BY_DATE, "", 3) == ["652070532920363", "726424880789805"]


def test_cleanup_applies_the_prefix_and_ignores_other_tables():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    tables = {f"spots_fs_online_{run}": when for run, when in RUNS_BY_DATE.items()} | {"other": 0.0, "spots_fs_online_x": 0.0}
    assert materialization.to_drop(tables, "spots_fs_online_", 3) == ["spots_fs_online_652070532920363", "spots_fs_online_726424880789805"]


def test_cleanup_ignores_folders_that_are_not_runs():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    assert materialization.to_drop(RUNS_BY_DATE | {"_probe": 0.0}, "", 3) == ["652070532920363", "726424880789805"]


def test_cleanup_keeps_the_latest_champion_besides_the_last_three():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    # 726... is the latest champion. 652..., an older champion, gets no protection: the caller only passes the latest.
    assert materialization.to_drop(RUNS_BY_DATE, "", 3, protect={"726424880789805"}) == ["652070532920363"]


def test_cleanup_with_a_recent_champion_keeps_only_three():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    assert materialization.to_drop(RUNS_BY_DATE, "", 3, protect={"508031956134800"}) == ["652070532920363", "726424880789805"]


def test_cleanup_with_keep_five_deletes_nothing_from_five_runs():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    assert materialization.to_drop(RUNS_BY_DATE, "", 5) == []


def test_cleanup_drops_old_snapshots_but_not_the_live_table():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    fs = "smartestate.feature_store_offline.spots_fs_offline"
    prefix = materialization.snapshot_table_name(fs, "")
    tables = {materialization.snapshot_table_name(fs, run): when for run, when in RUNS_BY_DATE.items()} | {fs: 0.0}
    assert materialization.to_drop(tables, prefix, 3, protect={"652070532920363"}) == [f"{fs}_726424880789805"]


@pytest.mark.parametrize("retention", [{}, {"keep_runs": 0}, {"keep_runs": "5"}, {"keep_runs": True}, {"keep_runs": 2.5}])
def test_keep_runs_aborts_on_a_bad_value(retention):
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    with pytest.raises(ValueError):
        materialization.keep_runs(retention)


def test_keep_runs_reads_the_value():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    assert materialization.keep_runs({"keep_runs": 5}) == 5


def test_production_run_id_reads_the_pointer():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    pointer = '{"run_id": "726424880789805", "image_tag": "abc"}'
    assert materialization.production_run_id(pointer) == "726424880789805"


@pytest.mark.parametrize("pointer", ['{"run_id": "../other"}', '{"image_tag": "abc"}', "not json"])
def test_production_run_id_aborts_on_a_bad_pointer(pointer):
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    with pytest.raises((ValueError, KeyError)):
        materialization.production_run_id(pointer)


class _Entry:
    def __init__(self, path, modified=0):
        self.path, self.name, self.modificationTime = path, path.rstrip("/").split("/")[-1] + ("/" if path.endswith("/") else ""), modified

    def isDir(self):
        return self.path.endswith("/")


def _fake_dbutils(tree):
    class Fs:
        ls = staticmethod(lambda path: tree[path])

    return type("DBUtils", (), {"fs": Fs})


def test_run_folders_are_dated_by_their_newest_file_at_any_depth(monkeypatch):
    storage = pytest.importorskip("src.ml_pipeline.storage")
    tree = {
        "runs": [_Entry("runs/100/"), _Entry("runs/_probe/")],
        "runs/100/": [_Entry("runs/100/a.joblib", 5000), _Entry("runs/100/q.parquet/")],
        "runs/100/q.parquet/": [_Entry("runs/100/q.parquet/part-0", 9000)],
        "runs/_probe/": [],  # not a run: it must not stop the cleanup
    }
    monkeypatch.setattr(storage, "_dbutils", lambda spark: _fake_dbutils(tree))
    assert storage.list_run_folders(None, "runs") == {"100": 9.0}


def test_run_folder_without_a_date_aborts(monkeypatch):
    storage = pytest.importorskip("src.ml_pipeline.storage")
    tree = {"runs": [_Entry("runs/100/")], "runs/100/": [_Entry("runs/100/a.joblib", 0)]}
    monkeypatch.setattr(storage, "_dbutils", lambda spark: _fake_dbutils(tree))
    with pytest.raises(ValueError, match="No file with a date"):
        storage.list_run_folders(None, "runs")


def test_online_key_is_what_publish_table_writes():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    assert materialization.online_key(np.int64(501)) == {"_feature_store_internal__primary_keys": "[501]"}


def _online_item(materialization, vector):
    # As boto3 returns it (run 669016108687066): no spot_id attribute, and the vector as a list of Decimal.
    return {
        materialization.ONLINE_KEY_ATTRIBUTE: "[501]", "cluster_id": Decimal(2),
        "nlp_vector": [Decimal(str(value)) for value in vector],
    }


def test_smoke_check_accepts_an_item_equal_to_offline():
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    vector = [0.1] * materialization.NLP_DIM
    expected = {"spot_id": 501, "cluster_id": 2, "nlp_vector": vector}
    assert materialization.check_item(_online_item(materialization, vector), expected) == []


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda item: None, "not found"),
        (lambda item: item | {"cluster_id": Decimal(3)}, "cluster_id"),
        (lambda item: {k: v for k, v in item.items() if k != "cluster_id"}, "cluster_id"),
        (lambda item: {k: v for k, v in item.items() if not k.startswith("_feature")}, "online None"),
        (lambda item: item | {"_feature_store_internal__primary_keys": "[999]"}, "online [999]"),
        (lambda item: item | {"nlp_vector": "not a list"}, "does not parse"),
        (lambda item: {k: v for k, v in item.items() if k != "nlp_vector"}, "does not parse"),
        (lambda item: item | {"nlp_vector": [Decimal("0.1"), Decimal("0.2")]}, "shape"),
        (lambda item: item | {"nlp_vector": [Decimal("0.9")] * 768}, "differs"),
    ],
)
def test_smoke_check_reports_each_problem(change, message):
    materialization = pytest.importorskip("src.ml_pipeline.materialization")
    vector = [0.1] * materialization.NLP_DIM
    expected = {"spot_id": 501, "cluster_id": 2, "nlp_vector": vector}
    errors = materialization.check_item(change(_online_item(materialization, vector)), expected)
    assert any(message in error for error in errors)


# ── validate_fs (new_arch.md §3.1) ────────────────────────────────────────────


def _clustered_fs(result, gold):
    modeling = pytest.importorskip("src.ml_pipeline.modeling")
    return modeling.with_cluster_id(result.features, gold.filter("is_candidate").selectExpr("spot_id", "0 AS cluster_id"))


def test_validate_fs_contract_matches_the_offline_schema():
    validate_fs = pytest.importorskip("src.ml_pipeline.validate_fs")
    assert validate_fs.EXPECTED_COLUMNS == tuple((f.name, f.dataType.simpleString()) for f in FS_OFFLINE_SCHEMA.fields)


def test_validate_fs_passes_a_good_table(result, gold):
    validate_fs = pytest.importorskip("src.ml_pipeline.validate_fs")
    assert validate_fs.find_errors(_clustered_fs(result, gold)) == []


@pytest.mark.parametrize(
    "spoil, message",
    [
        (lambda fs: fs.withColumn("cluster_id", F.lit(None).cast("int")), "candidates without cluster_id"),
        (lambda fs: fs.withColumn("cluster_id", F.lit(0)), "non-candidates with cluster_id"),
        (lambda fs: fs.unionByName(fs), "spot_id repeated"),
        (lambda fs: fs.filter("false"), "no rows"),
        (lambda fs: fs.withColumn("latitude", F.lit(None).cast("double")), "candidates without latitude"),
        (lambda fs: fs.drop("cluster_id"), "schema"),
    ],
)
def test_validate_fs_reports_each_problem(result, gold, spoil, message):
    validate_fs = pytest.importorskip("src.ml_pipeline.validate_fs")
    errors = validate_fs.find_errors(spoil(_clustered_fs(result, gold)))
    assert any(message in error for error in errors)


def test_validate_fs_aborts(result):
    validate_fs = pytest.importorskip("src.ml_pipeline.validate_fs")
    with pytest.raises(validate_fs.FsValidationError, match="candidates without cluster_id"):
        validate_fs.validate(result.features)  # as T1 leaves it: cluster_id still null
