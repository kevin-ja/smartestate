"""
Unit tests of the monitoring (monitor.md): the metrics, the weekly Lambda's logic and the benchmark stage of T2.
No AWS: boto3 is replaced by small fakes.

    ./ss/bin/python -m pytest tests/test_monitoring.py -v
"""

from __future__ import annotations

import csv
import io
import json
import math

import pytest

from src.common import ranking_metrics as rm

# --- Metrics, on cases worked out by hand


def test_recall_counts_relevant_spots_in_the_top_k():
    relevance = {1: 3, 2: 2, 3: 1, 9: 0}
    assert rm.recall_at_k([1, 5, 3], relevance, k=10) == pytest.approx(2 / 3)
    assert rm.recall_at_k([5, 6], relevance, k=10) == 0.0
    assert rm.recall_at_k([1, 2, 3], relevance, k=2) == pytest.approx(2 / 3)


def test_recall_is_undefined_without_relevant_spots():
    assert rm.recall_at_k([1, 2], {}, k=10) is None
    assert rm.recall_at_k([1, 2], {1: 0}, k=10) is None
    assert rm.score([1], {1: 0}) is None


def test_ndcg_of_the_ideal_order_is_one():
    assert rm.ndcg_at_k([1, 2, 3], {1: 3, 2: 2, 3: 1}) == pytest.approx(1.0)


def test_ndcg_uses_exponential_gain_and_log_discount():
    # ranked [3, 1]: gains 1 at position 1, 7 at position 2 -> 1/log2(2) + 7/log2(3) ; ideal: 7/1 + 1/log2(3)
    dcg = 1 / math.log2(2) + 7 / math.log2(3)
    idcg = 7 / math.log2(2) + 1 / math.log2(3)
    assert rm.ndcg_at_k([3, 1], {1: 3, 3: 1}) == pytest.approx(dcg / idcg)


def test_ndcg_ignores_spots_the_specialist_did_not_list():
    assert rm.ndcg_at_k([9, 8, 1], {1: 3}) == pytest.approx((7 / math.log2(4)) / 7)


def test_ndcg_is_zero_when_nothing_relevant_exists():
    assert rm.ndcg_at_k([1, 2], {}) == 0.0


def test_ndcg_cuts_both_lists_at_k():
    # with k = 1 only the first position counts on both sides
    assert rm.ndcg_at_k([2, 1], {1: 3, 2: 1}, k=1) == pytest.approx(1 / 7)


def test_score_returns_both_metrics():
    assert rm.score([1, 2], {1: 3, 2: 1}) == rm.Scores(1.0, 1.0)


# --- benchmark.csv


def test_read_benchmark_groups_by_clicked_spot():
    text = "spot_id,recommended_spot_id,relevance\n1,2,3\n1,3,1\n4,5,2\n"
    assert rm.read_benchmark_csv(text) == {1: {2: 3, 3: 1}, 4: {5: 2}}


@pytest.mark.parametrize("text", ["spot_id,relevance\n1,2\n", "spot_id,recommended_spot_id,relevance\n1,x,2\n"])
def test_read_benchmark_aborts_on_a_bad_file(text):
    with pytest.raises(ValueError):
        rm.read_benchmark_csv(text)


# --- Weekly Lambda


from src.monitoring import weekly_metrics as wm  # noqa: E402


def log_row(run_id="r1", spot_id=1, spot_ids="2,3", n=10, clicks=1):
    return {"run_id": run_id, "spot_id": str(spot_id), "n": str(n), "spot_ids": spot_ids, "clicks": str(clicks)}


def test_clicks_are_scored_and_weighted_by_frequency():
    benchmark = {1: {2: 3, 3: 1}, 4: {5: 2}}
    rows = [log_row(spot_id=1, spot_ids="2,3", clicks=3), log_row(spot_id=4, spot_ids="9", clicks=1)]
    result = wm.weekly_scores(rows, benchmark)
    (run,) = result
    assert run["run_id"] == "r1"
    assert run["clicks"] == 4 and run["clicks_in_benchmark"] == 4 and run["spots_in_benchmark"] == 2
    # spot 1: recall 1, ndcg 1 (x3); spot 4: recall 0, ndcg 0 (x1)
    assert run["recall_at_10"] == pytest.approx(0.75) and run["ndcg_at_10"] == pytest.approx(0.75)


def test_clicks_outside_the_benchmark_only_count_as_coverage():
    rows = [log_row(spot_id=7, spot_ids="2", clicks=5), log_row(spot_id=1, spot_ids="2", clicks=1)]
    (run,) = wm.weekly_scores(rows, {1: {2: 1}})
    assert run["clicks"] == 6 and run["clicks_in_benchmark"] == 1 and run["coverage"] == pytest.approx(1 / 6)
    assert run["recall_at_10"] == 1.0


def test_an_empty_answer_scores_zero_not_an_error():
    (run,) = wm.weekly_scores([log_row(spot_id=1, spot_ids="")], {1: {2: 1}})
    assert run["recall_at_10"] == 0.0 and run["ndcg_at_10"] == 0.0


def test_a_row_without_spot_ids_is_an_empty_answer():
    row = log_row(spot_id=1)
    del row["spot_ids"]  # Logs Insights drops the empty string the API logs for an empty answer
    (run,) = wm.weekly_scores([row], {1: {2: 1}})
    assert run["clicks_in_benchmark"] == 1 and run["recall_at_10"] == 0.0


def test_runs_are_reported_separately():
    rows = [log_row(run_id="a", spot_id=1, spot_ids="2"), log_row(run_id="b", spot_id=1, spot_ids="9")]
    result = {run["run_id"]: run for run in wm.weekly_scores(rows, {1: {2: 1}})}
    assert result["a"]["recall_at_10"] == 1.0 and result["b"]["recall_at_10"] == 0.0


def test_a_week_without_clicks_in_the_benchmark_has_no_metrics():
    (run,) = wm.weekly_scores([log_row(spot_id=7, spot_ids="2")], {1: {2: 1}})
    assert run["recall_at_10"] is None and run["ndcg_at_10"] is None and run["clicks"] == 1


def test_insights_rows_become_flat_dicts():
    raw = [[{"field": "run_id", "value": "r1"}, {"field": "spot_id", "value": "1"}, {"field": "@ptr", "value": "x"}]]
    assert wm.flatten(raw) == [{"run_id": "r1", "spot_id": "1"}]


def test_benchmark_mean_of_a_run_reads_its_scores_file():
    text = "run_id,spot_id,recall_at_10,ndcg_at_10\nr1,1,1.0,0.5\nr1,2,0.0,0.7\n"
    assert wm.benchmark_mean(text) == {"recall_at_10": 0.5, "ndcg_at_10": pytest.approx(0.6)}


def test_weekly_csv_has_one_row_per_run():
    runs = [{"run_id": "r1", "week_start": "2026-11-09", "week_end": "2026-11-16", "clicks": 4, "clicks_in_benchmark": 4,
             "spots_in_benchmark": 2, "coverage": 1.0, "recall_at_10": 0.75, "ndcg_at_10": 0.75,
             "benchmark_recall_at_10": 0.8, "benchmark_ndcg_at_10": 0.9}]
    rows = list(csv.DictReader(io.StringIO(wm.weekly_csv(runs))))
    assert [row["run_id"] for row in rows] == ["r1"] and rows[0]["ndcg_at_10"] == "0.75"


class FakeLogs:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def start_query(self, **params):
        self.queries.append(params)
        return {"queryId": "q1"}

    def get_query_results(self, queryId):
        return {"status": "Complete", "results": [[{"field": k, "value": v} for k, v in row.items()] for row in self.rows]}


class FakeS3:
    def __init__(self, objects):
        self.objects, self.written = dict(objects), {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise wm.MissingObject(Key)
        return {"Body": io.BytesIO(self.objects[Key].encode())}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.written[Key] = Body


class FakeCloudWatch:
    def __init__(self):
        self.metrics = []

    def put_metric_data(self, Namespace, MetricData):
        self.metrics.extend(MetricData)


def test_handler_reads_logs_cross_benchmark_writes_csv_and_metrics(monkeypatch):
    logs = FakeLogs([log_row(spot_id=1, spot_ids="2,3", clicks=2)])
    s3 = FakeS3({
        "ref/benchmark/benchmark.csv": "spot_id,recommended_spot_id,relevance\n1,2,3\n1,3,1\n",
        "runs/r1/benchmark_scores.csv": "run_id,spot_id,recall_at_10,ndcg_at_10\nr1,1,1.0,1.0\n",
        "runs/production.json": '{"run_id": "r1", "image_tag": "abc"}',
    })
    cloudwatch = FakeCloudWatch()
    monkeypatch.setattr(wm, "clients", lambda: (logs, s3, cloudwatch))
    monkeypatch.setattr(wm.time, "sleep", lambda seconds: None)
    monkeypatch.setenv("LOG_GROUP", "/smartestate/inference")
    monkeypatch.setenv("LAKE_BUCKET", "lake")
    monkeypatch.setenv("METRIC_NAMESPACE", "SmartEstate/Model")

    result = wm.handler({"start": "2026-11-09T00:00:00Z", "end": "2026-11-16T00:00:00Z"}, None)

    assert logs.queries[0]["logGroupName"] == "/smartestate/inference"
    (run,) = result["runs"]
    assert run["recall_at_10"] == 1.0 and run["clicks"] == 2 and run["benchmark_ndcg_at_10"] == 1.0
    assert "runs/r1/metrics/weekly_2026-11-16.csv" in s3.written
    names = {(metric["MetricName"], metric["Dimensions"][0]["Value"]) for metric in cloudwatch.metrics if "Dimensions" in metric}
    assert names >= {("recall_at_10", "r1"), ("ndcg_at_10", "r1"), ("benchmark_ndcg_at_10", "r1"), ("clicks", "r1")}
    (ratio,) = [metric for metric in cloudwatch.metrics if metric["MetricName"] == wm.RATIO_METRIC]
    assert ratio["Value"] == 1.0 and "Dimensions" not in ratio and result["production_run_id"] == "r1"


def test_handler_without_a_scores_file_skips_the_benchmark_metric(monkeypatch):
    logs = FakeLogs([log_row(spot_id=1, spot_ids="2")])
    s3 = FakeS3({"ref/benchmark/benchmark.csv": "spot_id,recommended_spot_id,relevance\n1,2,1\n"})
    cloudwatch = FakeCloudWatch()
    monkeypatch.setattr(wm, "clients", lambda: (logs, s3, cloudwatch))
    monkeypatch.setattr(wm.time, "sleep", lambda seconds: None)
    monkeypatch.setenv("LOG_GROUP", "g")
    monkeypatch.setenv("LAKE_BUCKET", "lake")

    result = wm.handler({"start": "2026-11-09T00:00:00Z", "end": "2026-11-16T00:00:00Z"}, None)
    (run,) = result["runs"]
    assert run["benchmark_ndcg_at_10"] is None and result[wm.RATIO_METRIC] is None
    assert not any(metric["MetricName"].startswith(("benchmark", "production")) for metric in cloudwatch.metrics)


def test_handler_without_benchmark_aborts(monkeypatch):
    monkeypatch.setattr(wm, "clients", lambda: (FakeLogs([]), FakeS3({}), FakeCloudWatch()))
    monkeypatch.setenv("LOG_GROUP", "g")
    monkeypatch.setenv("LAKE_BUCKET", "lake")
    with pytest.raises(wm.MissingObject):
        wm.handler({"start": "2026-11-09T00:00:00Z", "end": "2026-11-16T00:00:00Z"}, None)


@pytest.mark.parametrize("production, expected", [("r1", 0.5), ("r2", None), (None, None)])
def test_ratio_only_for_the_run_in_production(production, expected):
    runs = [{"run_id": "r1", "ndcg_at_10": 0.4, "benchmark_ndcg_at_10": 0.8},
            {"run_id": "r2", "ndcg_at_10": 0.4, "benchmark_ndcg_at_10": None}]
    assert wm.production_ratio(runs, production) == (pytest.approx(expected) if expected else None)


def test_default_window_is_the_last_seven_days():
    start, end = wm.window({}, now=wm.datetime(2026, 11, 15, 6, 0, tzinfo=wm.timezone.utc))
    assert (end - start).days == 7 and end.isoformat().startswith("2026-11-15T00:00")


# --- T2 benchmark stage (pure)

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")


def test_offline_store_hands_the_cascade_plain_python_values():
    from src.ml_pipeline import benchmark

    spots = pd.DataFrame({
        "spot_id": [1, 2], "sector_id": [11, 11], "modality_mask": [1, 1], "latitude": [19.0, 19.0],
        "longitude": [-99.0, -99.0], "tab_vector": [np.array([1.0, 0.0]), None], "nlp_vector": [None, np.array([0.0, 1.0])],
        "rent_price_sqm": [100.0, float("nan")], "sale_price_sqm": [float("nan"), 5.0],
    })
    store = benchmark.OfflineStore(spots, pd.DataFrame({"spot_id": [2], "cluster_id": [3]}))
    assert store.get_clicked(1) == {
        "spot_id": 1, "sector_id": 11, "modality_mask": 1, "latitude": 19.0, "longitude": -99.0,
        "tab_vector": [1.0, 0.0], "nlp_vector": None, "rent_price_sqm": 100.0, "sale_price_sqm": None, "cluster_id": None,
    }
    (candidate,) = store.get_candidates([2, 404])
    assert candidate["cluster_id"] == 3 and candidate["rent_price_sqm"] is None and candidate["nlp_vector"] == [0.0, 1.0]


def test_score_run_scores_each_benchmark_spot_the_cascade_knows():
    from src.common.geo_index import GeoIndex
    from src.ml_pipeline import benchmark

    km = 1 / 111.2
    spots = pd.DataFrame({
        "spot_id": [1, 2, 3], "sector_id": [11, 11, 11], "modality_mask": [1, 1, 1],
        "latitude": [19.0, 19.0 + km, 19.0 + 2 * km], "longitude": [-99.0] * 3,
        "tab_vector": [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], "nlp_vector": [None] * 3,
        "rent_price_sqm": [100.0] * 3, "sale_price_sqm": [None] * 3,
    })
    clusters = pd.DataFrame({"spot_id": [1, 2, 3], "cluster_id": [0, 1, 2]})
    index = GeoIndex(spots.spot_id.to_numpy(), spots.latitude.to_numpy(), spots.longitude.to_numpy(),
                     spots.sector_id.to_numpy(), spots.modality_mask.to_numpy())
    params = {"w_tab": 0.6, "w_nlp": 0.4, "f_bounds": [0.7, 1.1], "delta": 0.85}

    ground_truth = {1: {2: 3, 3: 1}, 99: {1: 2}, 3: {7: 0}}
    scores = benchmark.score_run("r", ground_truth, index, benchmark.OfflineStore(spots, clusters), params)

    assert list(scores.columns) == list(benchmark.SCORES_COLUMNS)
    assert scores.spot_id.tolist() == [1]  # 99 is unknown to the cascade, 3 has nothing relevant
    assert scores.recall_at_10.iloc[0] == 1.0 and scores.ndcg_at_10.iloc[0] == pytest.approx(1.0)
    assert benchmark.summary(scores, len(ground_truth)) == {
        "benchmark_recall_at_10": 1.0, "benchmark_ndcg_at_10": pytest.approx(1.0),
        "benchmark_spots_scored": 1.0, "benchmark_spots": 3.0,
    }
