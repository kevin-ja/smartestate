"""
Integration test of the inference image: a real container against the real S3 and DynamoDB. Runs in the CD, before
pushing the image (.github/workflows/inference.yml). Elsewhere it skips.

    INFERENCE_URL=http://localhost:8000 RUN_ID=<run_id> RUNS_ROOT=s3://<lake>/runs AWS_REGION=us-east-1 \
        ./ss/bin/python -m pytest tests/test_inference_integration.py -v
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif("INFERENCE_URL" not in os.environ, reason="needs a running container")

OFFICE_SECTOR = 11  # the sector with the most candidates
RADIUS_KM = 5.0
UNKNOWN_SPOT_ID = 0


def click_with_neighbours(geo_index) -> int:
    """First office with another candidate nearby: ~2 % have none, and an empty answer would prove nothing."""
    import numpy as np

    coordinates = np.degrees(np.asarray(geo_index.tree.data))
    for row in np.flatnonzero(geo_index.sector == OFFICE_SECTOR):
        latitude, longitude = coordinates[row]
        near = geo_index.query(latitude, longitude, RADIUS_KM, OFFICE_SECTOR, int(geo_index.modality_mask[row]))
        if len(near) > 1:
            return int(geo_index.spot_id[row])
    raise AssertionError("No office has a neighbour: the GeoIndex looks wrong")


@pytest.fixture(scope="module")
def client():
    httpx = pytest.importorskip("httpx")
    with httpx.Client(base_url=os.environ["INFERENCE_URL"], timeout=30) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def geo_index():
    boto3 = pytest.importorskip("boto3")
    from src.inference import artifacts

    s3 = boto3.client("s3", region_name=os.environ["AWS_REGION"])
    return artifacts.load_geo_index(s3, os.environ["RUNS_ROOT"], os.environ["RUN_ID"])


def test_health_says_it_is_ready(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_is_the_expected_run(client):
    assert client.get("/version").json()["run_id"] == os.environ["RUN_ID"]


def test_recommendations_are_real_candidates(client, geo_index):
    spot_id = click_with_neighbours(geo_index)

    response = client.get(f"/recommendations/{spot_id}", params={"n": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == os.environ["RUN_ID"]
    assert 0 < len(body["spot_ids"]) <= 5
    assert spot_id not in body["spot_ids"]
    assert set(body["spot_ids"]) <= set(geo_index.spot_id.tolist())


def test_unknown_spot_is_404(client):
    assert client.get(f"/recommendations/{UNKNOWN_SPOT_ID}").status_code == 404


def test_invalid_n_is_422(client):
    assert client.get("/recommendations/1", params={"n": 0}).status_code == 422
