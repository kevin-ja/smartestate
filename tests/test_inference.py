"""
Unit tests of the inference service. No AWS: S3 and DynamoDB are replaced by small fakes.

    ./ss/bin/python -m pytest tests/test_inference.py -v

The API tests skip where FastAPI is not installed (Docker and the Databricks unit_tests task).
"""

from __future__ import annotations

import io
import json

import joblib
import numpy as np
import pytest

from src.common.geo_index import GeoIndex
from src.inference import artifacts, online_store, recommender
from src.inference.online_store import OnlineStore

PARAMS = {"w_tab": 0.6, "w_nlp": 0.4, "f_bounds": [0.7, 1.1], "delta": 0.85}
KM_IN_LATITUDE_DEGREES = 1 / 111.2


def spot(spot_id=None, tab=(1.0, 0.0), nlp=(1.0, 0.0), rent=100.0, sale=None, mask=1, cluster=0):
    item = {
        "tab_vector": list(tab) if tab else None,
        "nlp_vector": list(nlp) if nlp else None,
        "rent_price_sqm": rent,
        "sale_price_sqm": sale,
        "modality_mask": mask,
        "cluster_id": cluster,
    }
    return item if spot_id is None else {"spot_id": spot_id, **item}


def clicked_at_origin(**overrides):
    return {"latitude": 19.0, "longitude": -99.0, "sector_id": 11, "modality_mask": 1, **overrides}


def index_at_km(distances_km: dict[int, float]) -> GeoIndex:
    """Candidates due north of the origin, same sector and modality."""
    size = len(distances_km)
    latitudes = [19.0 + km * KM_IN_LATITUDE_DEGREES for km in distances_km.values()]
    return GeoIndex(list(distances_km), latitudes, [-99.0] * size, [11] * size, [1] * size)


# --- Filters 1 + 2


def test_geo_index_returns_nearest_first():
    index = index_at_km({1: 4.0, 2: 1.0, 3: 2.0})
    assert index.query(19.0, -99.0, 5.0, 11, 1) == [2, 3, 1]


def test_radius_stops_at_the_first_with_enough_candidates():
    index = index_at_km({spot_id: 4.0 for spot_id in range(1, 51)} | {99: 8.0})
    assert 99 not in recommender.find_candidates(index, 0, clicked_at_origin())


def test_radius_widens_and_never_passes_its_limit():
    index = index_at_km({1: 4.0, 2: 9.0, 3: 14.0, 4: 16.0})
    assert recommender.find_candidates(index, 0, clicked_at_origin()) == [1, 2, 3]


def test_candidates_exclude_the_click():
    index = index_at_km({1: 0.0, 2: 1.0})
    assert recommender.find_candidates(index, 1, clicked_at_origin()) == [2]


def test_candidates_keep_the_nearest_within_the_cap():
    index = index_at_km({spot_id: spot_id / 100 for spot_id in range(1, 351)})
    found = recommender.find_candidates(index, 0, clicked_at_origin())
    assert found == list(range(1, 301))


# --- 3A


def test_similarity_uses_both_signals():
    sim = recommender.similarity(spot(), [spot(tab=(1.0, 0.0), nlp=(0.0, 1.0))], 0.6, 0.4)
    assert sim == pytest.approx([0.6])


def test_similarity_reweights_when_the_candidate_has_no_text():
    sim = recommender.similarity(spot(), [spot(nlp=None)], 0.6, 0.4)
    assert sim == pytest.approx([1.0])


def test_similarity_of_land_uses_text_only():
    clicked, candidate = spot(tab=None), spot(tab=None, nlp=(0.6, 0.8))
    assert recommender.similarity(clicked, [candidate], 0.6, 0.4) == pytest.approx([0.6])


def test_similarity_without_a_shared_signal_is_zero():
    clicked, candidate = spot(nlp=None), spot(tab=None)
    assert recommender.similarity(clicked, [candidate], 0.6, 0.4) == pytest.approx([0.0])


def test_similarity_never_goes_below_zero():
    assert recommender.similarity(spot(), [spot(tab=(-1.0, 0.0), nlp=(-1.0, 0.0))], 0.6, 0.4) == pytest.approx([0.0])


# --- 3B


@pytest.mark.parametrize(
    ("ratio", "factor"),
    [(0.25, 1.1), (0.5, 1.1), (0.75, 1.05), (1.0, 1.0), (1.5, 0.85), (2.0, 0.7), (50.0, 0.7)],
)
def test_price_factor_stretches(ratio, factor):
    assert recommender.price_factor(ratio, [0.7, 1.1]) == pytest.approx(factor)


def test_price_uses_the_shared_modality():
    clicked = spot(rent=100.0, sale=100.0, mask=3)
    candidate = spot(rent=200.0, sale=50.0, mask=1)
    assert recommender.price_factors(clicked, [candidate], [0.7, 1.1]) == pytest.approx([0.7])


def test_price_averages_when_both_are_rent_and_sale():
    clicked = spot(rent=100.0, sale=100.0, mask=3)
    candidate = spot(rent=200.0, sale=50.0, mask=3)
    assert recommender.price_factors(clicked, [candidate], [0.7, 1.1]) == pytest.approx([0.9])


def test_price_uses_the_one_both_have():
    clicked = spot(rent=100.0, sale=None, mask=3)
    candidate = spot(rent=200.0, sale=50.0, mask=3)
    assert recommender.price_factors(clicked, [candidate], [0.7, 1.1]) == pytest.approx([0.7])


@pytest.mark.parametrize(("clicked_rent", "candidate_rent"), [(None, 100.0), (100.0, None), (0.0, 100.0)])
def test_price_is_neutral_without_a_usable_pair(clicked_rent, candidate_rent):
    factors = recommender.price_factors(spot(rent=clicked_rent), [spot(rent=candidate_rent)], [0.7, 1.1])
    assert factors == pytest.approx([1.0])


# --- 3C


def test_diversity_penalizes_a_repeated_cluster():
    top = recommender.diversify([1, 2, 3], np.array([1.0, 0.9, 0.8]), [0, 0, 1], 0.85, 2)
    assert top == [1, 3]


def test_diversity_with_delta_one_keeps_the_score_order():
    top = recommender.diversify([1, 2, 3], np.array([0.8, 1.0, 0.9]), [0, 0, 0], 1.0, 3)
    assert top == [2, 3, 1]


def test_diversity_breaks_ties_by_spot_id():
    assert recommender.diversify([3, 1, 2], np.array([0.5, 0.5, 0.5]), [0, 1, 2], 0.85, 3) == [1, 2, 3]


def test_diversity_does_not_group_spots_without_a_cluster():
    top = recommender.diversify([1, 2, 3], np.array([1.0, 0.9, 0.8]), [None, None, 0], 0.5, 2)
    assert top == [1, 2]


# --- Cascade


class FakeStore:
    def __init__(self, items):
        self.items = items

    def get_clicked(self, spot_id):
        return self.items.get(spot_id)

    def get_candidates(self, spot_ids):
        return [{"spot_id": spot_id, **self.items[spot_id]} for spot_id in spot_ids]


def cascade_store():
    return FakeStore(
        {
            0: {**spot(), **clicked_at_origin()},
            1: spot(tab=(0.0, 1.0), nlp=(0.0, 1.0), cluster=0),
            2: spot(cluster=1),
            3: spot(rent=200.0, cluster=2),
        }
    )


def test_recommend_orders_the_candidates_and_cuts_at_n():
    index = index_at_km({0: 0.0, 1: 1.0, 2: 1.0, 3: 1.0})
    assert recommender.recommend(0, 2, index, cascade_store(), PARAMS) == [2, 3]


def test_recommend_returns_none_for_an_unknown_click():
    assert recommender.recommend(404, 2, index_at_km({1: 1.0}), cascade_store(), PARAMS) is None


def test_recommend_is_empty_without_candidates():
    assert recommender.recommend(0, 2, index_at_km({1: 50.0}), cascade_store(), PARAMS) == []


def test_recommend_is_empty_when_the_click_has_no_coordinates():
    store = FakeStore({0: {**spot(), **clicked_at_origin(latitude=None)}})
    assert recommender.recommend(0, 2, index_at_km({1: 1.0}), store, PARAMS) == []


# --- DynamoDB


KEY = online_store.ONLINE_KEY_ATTRIBUTE


def wire_candidate(spot_id):
    return {
        KEY: {"S": json.dumps([spot_id])},
        "tab_vector": {"L": [{"N": "0.6"}, {"N": "0.8"}]},
        "nlp_vector": {"NULL": True},
        "cluster_id": {"N": "2"},
    }


class FakeDynamo:
    """batch_get_item answers `unprocessed_rounds` times leaving the last key of each batch out."""

    def __init__(self, item=None, unprocessed_rounds=0):
        self.item = item
        self.unprocessed_rounds = unprocessed_rounds
        self.batch_sizes = []
        self.get_item_calls = []

    def get_item(self, **kwargs):
        self.get_item_calls.append(kwargs)
        return {"Item": self.item} if self.item else {}

    def batch_get_item(self, RequestItems):
        (table, request), = RequestItems.items()
        self.batch_sizes.append(len(request["Keys"]))
        keys = request["Keys"]
        pending = {}
        if self.unprocessed_rounds > 0:
            self.unprocessed_rounds -= 1
            keys, pending = keys[:-1], {table: {**request, "Keys": keys[-1:]}}
        items = [wire_candidate(json.loads(key[KEY]["S"])[0]) for key in keys]
        return {"Responses": {table: items}, "UnprocessedKeys": pending}


def test_get_clicked_decodes_and_uses_the_combined_key():
    client = FakeDynamo(item=wire_candidate(7))
    item = OnlineStore("t", client).get_clicked(7)
    assert item["tab_vector"] == [0.6, 0.8]
    assert item["nlp_vector"] is None
    assert client.get_item_calls[0]["Key"] == {KEY: {"S": "[7]"}}


def test_get_clicked_returns_none_when_missing():
    assert OnlineStore("t", FakeDynamo()).get_clicked(7) is None


def test_get_candidates_splits_in_batches_and_reads_spot_id_from_the_key():
    client = FakeDynamo()
    items = OnlineStore("t", client).get_candidates(list(range(250)))
    assert sorted(client.batch_sizes) == [50, 100, 100]
    assert sorted(item["spot_id"] for item in items) == list(range(250))
    assert KEY not in items[0]


def test_get_candidates_retries_unprocessed_keys():
    client = FakeDynamo(unprocessed_rounds=1)
    items = OnlineStore("t", client).get_candidates([1, 2])
    assert client.batch_sizes == [2, 1]
    assert sorted(item["spot_id"] for item in items) == [1, 2]


def test_get_candidates_serves_what_it_got_after_the_retries(monkeypatch):
    monkeypatch.setattr(online_store, "RETRY_WAIT_S", 0)
    client = FakeDynamo(unprocessed_rounds=online_store.MAX_BATCH_ATTEMPTS)
    items = OnlineStore("t", client).get_candidates([1, 2, 3, 4])
    assert [item["spot_id"] for item in items] == [1, 2, 3]


# --- S3


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[f"{Bucket}/{Key}"])}


def test_artifacts_load_from_the_run_folder():
    buffer = io.BytesIO()
    joblib.dump(index_at_km({1: 1.0}), buffer)
    s3 = FakeS3(
        {
            "lake/runs/abc/geo_index.joblib": buffer.getvalue(),
            "lake/runs/abc/reranker_params.json": json.dumps(PARAMS).encode(),
        }
    )
    assert artifacts.load_geo_index(s3, "s3://lake/runs", "abc").query(19.0, -99.0, 5.0, 11, 1) == [1]
    assert artifacts.load_reranker_params(s3, "s3://lake/runs/", "abc") == PARAMS


# --- API


@pytest.fixture
def app():
    pytest.importorskip("fastapi")
    from src.inference.api import Service, create_app

    index = index_at_km({0: 0.0, 1: 1.0, 2: 1.0, 3: 1.0})
    return create_app(lambda: Service("abc", index, PARAMS, cascade_store()))


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


def test_recommendations_returns_the_run_and_the_spots(client):
    response = client.get("/recommendations/0?n=2")
    assert response.status_code == 200
    assert response.json() == {"run_id": "abc", "spot_ids": [2, 3]}


def test_recommendations_uses_the_default_n(client):
    assert client.get("/recommendations/0").json()["spot_ids"] == [2, 3, 1]


def test_recommendations_unknown_spot_is_404(client):
    assert client.get("/recommendations/404").status_code == 404


@pytest.mark.parametrize("n", [0, recommender.MAX_N + 1, "x"])
def test_recommendations_rejects_an_invalid_n(client, n):
    assert client.get(f"/recommendations/0?n={n}").status_code == 422


def test_every_response_reports_the_time_spent_in_the_service(client):
    header = client.get("/recommendations/0?n=2").headers["Server-Timing"]
    assert header.startswith("app;dur=")
    assert float(header.removeprefix("app;dur=")) >= 0


def test_health_and_version_when_loaded(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/version").json() == {"run_id": "abc", "params": PARAMS}


def test_nothing_answers_before_the_artifacts_load(app):
    from fastapi.testclient import TestClient

    not_started = TestClient(app)  # without `with`, the startup does not run
    assert not_started.get("/health").status_code == 503
    assert not_started.get("/recommendations/0").status_code == 503
