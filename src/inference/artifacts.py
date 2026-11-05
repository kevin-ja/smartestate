"""Reads a run's artifacts from S3 at startup: the GeoIndex and the re-ranker parameters."""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.parse import urlparse

import joblib

GEO_INDEX_FILE = "geo_index.joblib"
RERANKER_PARAMS_FILE = "reranker_params.json"


def _read(s3: Any, runs_root: str, run_id: str, filename: str) -> bytes:
    location = urlparse(runs_root)
    key = f"{location.path.strip('/')}/{run_id}/{filename}"
    return s3.get_object(Bucket=location.netloc, Key=key)["Body"].read()


def load_geo_index(s3: Any, runs_root: str, run_id: str) -> Any:
    return joblib.load(io.BytesIO(_read(s3, runs_root, run_id, GEO_INDEX_FILE)))


def load_reranker_params(s3: Any, runs_root: str, run_id: str) -> dict[str, Any]:
    return json.loads(_read(s3, runs_root, run_id, RERANKER_PARAMS_FILE))
