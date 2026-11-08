"""
Reads of the online feature store (DynamoDB). Credentials come from the EC2 instance profile.

JsonDynamoClient replaces boto3's client on the hot path: botocore walks every {"N": "..."} of the 768-number vectors
through its parser, ~390 ms of CPU for 300 candidates; json.loads plus _decode take ~100 ms
(docs/performance_report.md). Same request and response format, so OnlineStore works with either.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from src.ml_pipeline.materialization import ONLINE_KEY_ATTRIBUTE, online_key

log = logging.getLogger(__name__)

BATCH_SIZE = 100  # BatchGetItem limit
MAX_BATCH_ATTEMPTS = 3
RETRY_WAIT_S = 0.05
MAX_REQUEST_ATTEMPTS = 3  # throttling and 5xx, as botocore's default retry did
RETRYABLE_ERRORS = ("ThrottlingException", "ProvisionedThroughputExceededException", "RequestLimitExceeded")

CLICKED_COLUMNS = (
    "latitude", "longitude", "sector_id", "modality_mask", "tab_vector", "nlp_vector", "rent_price_sqm", "sale_price_sqm",
)
CANDIDATE_COLUMNS = (
    ONLINE_KEY_ATTRIBUTE, "tab_vector", "nlp_vector", "rent_price_sqm", "sale_price_sqm", "modality_mask", "cluster_id",
)


class DynamoError(Exception):
    """DynamoDB answered with an error that retrying does not fix."""


class JsonDynamoClient:
    """GetItem and BatchGetItem over DynamoDB's JSON API: signed with botocore, parsed with json."""

    def __init__(self, session: Any, region: str) -> None:
        self.credentials = session.get_credentials()  # refreshable: rotates the instance profile's keys
        self.region = region
        self.url = f"https://dynamodb.{region}.amazonaws.com/"
        self.http = urllib3.PoolManager(maxsize=32)  # thread-safe: FastAPI and the batches share it

    def get_item(self, **params: Any) -> dict[str, Any]:
        return self._call("GetItem", params)

    def batch_get_item(self, **params: Any) -> dict[str, Any]:
        return self._call("BatchGetItem", params)

    def _call(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(params)
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            request = AWSRequest(method="POST", url=self.url, data=body, headers={
                "Content-Type": "application/x-amz-json-1.0",
                "X-Amz-Target": f"DynamoDB_20120810.{operation}",
            })
            SigV4Auth(self.credentials.get_frozen_credentials(), "dynamodb", self.region).add_auth(request)
            response = self.http.request("POST", self.url, body=body, headers=dict(request.headers))
            payload = json.loads(response.data) if response.data else {}
            if response.status == 200:
                return payload
            error = str(payload.get("__type", "")).rsplit("#", 1)[-1]
            retryable = response.status >= 500 or error in RETRYABLE_ERRORS
            if not retryable or attempt == MAX_REQUEST_ATTEMPTS - 1:
                raise DynamoError(f"{operation}: HTTP {response.status} {error}: {payload.get('message', '')}")
            time.sleep(RETRY_WAIT_S * (attempt + 1))
        raise AssertionError("unreachable")


def _projection(columns: Sequence[str]) -> dict[str, Any]:
    return {
        "ProjectionExpression": ", ".join(f"#c{position}" for position in range(len(columns))),
        "ExpressionAttributeNames": {f"#c{position}": column for position, column in enumerate(columns)},
    }


def _decode(attribute: dict[str, Any]) -> Any:
    """DynamoDB wire value -> Python. The low-level client skips Decimal, which is slow on 768-number vectors."""
    if "N" in attribute:
        return float(attribute["N"])
    if "L" in attribute:
        return [_decode(value) for value in attribute["L"]]
    if "NULL" in attribute:
        return None
    return next(iter(attribute.values()))


def _decode_item(item: dict[str, Any]) -> dict[str, Any]:
    return {column: _decode(attribute) for column, attribute in item.items()}


def _wire_key(spot_id: int) -> dict[str, dict[str, str]]:
    return {column: {"S": value} for column, value in online_key(spot_id).items()}


def _batches(spot_ids: Sequence[int]) -> Iterator[Sequence[int]]:
    for start in range(0, len(spot_ids), BATCH_SIZE):
        yield spot_ids[start:start + BATCH_SIZE]


class OnlineStore:
    def __init__(self, table: str, client: Any) -> None:
        self.table = table
        self.client = client

    def get_clicked(self, spot_id: int) -> dict[str, Any] | None:
        """Lecture 1: everything about the click at once. None if it does not exist."""
        response = self.client.get_item(TableName=self.table, Key=_wire_key(spot_id), **_projection(CLICKED_COLUMNS))
        return _decode_item(response["Item"]) if "Item" in response else None

    def get_candidates(self, spot_ids: Sequence[int]) -> list[dict[str, Any]]:
        """Lecture 2: batches in parallel, each item with its spot_id taken from the combined key."""
        with ThreadPoolExecutor(max_workers=max(1, len(spot_ids) // BATCH_SIZE + 1)) as pool:
            batches = list(pool.map(self._read_batch, _batches(spot_ids)))
        return [self._candidate(item) for batch in batches for item in batch]

    def _read_batch(self, spot_ids: Sequence[int]) -> list[dict[str, Any]]:
        """DynamoDB may return part of a batch: retries the rest and, within the latency budget, serves what it got."""
        request = {
            self.table: {"Keys": [_wire_key(spot_id) for spot_id in spot_ids], **_projection(CANDIDATE_COLUMNS)}
        }
        items: list[dict[str, Any]] = []
        for attempt in range(MAX_BATCH_ATTEMPTS):
            response = self.client.batch_get_item(RequestItems=request)
            items += response.get("Responses", {}).get(self.table, [])
            request = response.get("UnprocessedKeys") or {}
            if not request:
                return items
            if attempt < MAX_BATCH_ATTEMPTS - 1:
                time.sleep(RETRY_WAIT_S * (attempt + 1))
        log.warning("%s: %d candidates unread", self.table, len(request[self.table]["Keys"]))
        return items

    @staticmethod
    def _candidate(item: dict[str, Any]) -> dict[str, Any]:
        decoded = _decode_item(item)
        decoded["spot_id"] = json.loads(decoded.pop(ONLINE_KEY_ATTRIBUTE))[0]
        return decoded
