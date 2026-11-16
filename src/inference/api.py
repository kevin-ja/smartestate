"""
FastAPI service (new_arch.md §4). Loads one run at startup and answers each click.

    RUN_ID=<run_id> RUNS_ROOT=s3://<lake>/runs AWS_REGION=us-east-1 uvicorn src.inference.api:app
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

import boto3
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from pydantic import BaseModel

from src.inference import artifacts, recommender
from src.inference.online_store import JsonDynamoClient, OnlineStore
from src.ml_pipeline.materialization import online_table_name


# One JSON line per request on stdout, which Docker ships to CloudWatch Logs. The monitoring reads it (monitor.md):
# the metric filters take `status` and `latency_ms`; the weekly Lambda takes `run_id`, `spot_id`, `n` and `spot_ids`.
# `spot_ids` goes as a comma-separated string, not an array, so Logs Insights can group by it.
REQUEST_LOGGER = "smartestate.requests"
request_log = logging.getLogger(REQUEST_LOGGER)


def configure_request_log() -> None:
    """Plain JSON on stdout, once per process, never through the root logger's format."""
    if not request_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        request_log.addHandler(handler)
    request_log.setLevel(logging.INFO)
    request_log.propagate = False


def request_record(method: str, path: str, status: int, latency_ms: float, recommendation: dict[str, Any] | None) -> dict:
    record: dict[str, Any] = {
        "event": "request", "method": method, "path": path, "status": status, "latency_ms": round(latency_ms, 1),
    }
    if recommendation is not None:
        record.update(recommendation)
        record["spot_ids"] = ",".join(str(spot_id) for spot_id in recommendation["spot_ids"])
    return record


@dataclass(frozen=True)
class Service:
    run_id: str
    geo_index: recommender.SpotIndex
    params: dict[str, Any]
    store: recommender.FeatureStore


class Recommendations(BaseModel):
    run_id: str
    spot_ids: list[int]


class Health(BaseModel):
    status: str


class Version(BaseModel):
    run_id: str
    params: dict[str, Any]


def load_service() -> Service:
    """GeoIndex, parameters and features all come from the same run_id, set by the deployment."""
    run_id, runs_root, region = os.environ["RUN_ID"], os.environ["RUNS_ROOT"], os.environ["AWS_REGION"]
    session = boto3.Session(region_name=region)
    s3 = session.client("s3")
    return Service(
        run_id=run_id,
        geo_index=artifacts.load_geo_index(s3, runs_root, run_id),
        params=artifacts.load_reranker_params(s3, runs_root, run_id),
        store=OnlineStore(online_table_name(run_id), JsonDynamoClient(session, region)),
    )


def ready_service(request: Request) -> Service:
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Service is not ready")
    return service


ReadyService = Annotated[Service, Depends(ready_service)]


def create_app(load: Callable[[], Service] = load_service) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.service = load()
        yield

    app = FastAPI(title="SmartEstate", lifespan=lifespan)
    configure_request_log()

    # Time spent inside the service, without the network: the Server-Timing header for the load test
    # (tests/performance/) and the request line for the monitoring.
    @app.middleware("http")
    async def server_timing(request: Request, call_next: Callable) -> Any:
        start = time.perf_counter()
        response = await call_next(request)
        latency_ms = (time.perf_counter() - start) * 1000
        response.headers["Server-Timing"] = f"app;dur={latency_ms:.1f}"
        recommendation = getattr(request.state, "recommendation", None)
        request_log.info(json.dumps(request_record(request.method, request.url.path, response.status_code, latency_ms, recommendation)))
        return response

    # Plain `def`: FastAPI runs them in a thread pool, so the blocking DynamoDB reads do not stop other requests.
    @app.get("/recommendations/{spot_id}")
    def get_recommendations(
        request: Request,
        service: ReadyService,
        spot_id: Annotated[int, Path()],
        n: Annotated[int, Query(ge=1, le=recommender.MAX_N)] = recommender.DEFAULT_N,
    ) -> Recommendations:
        spot_ids = recommender.recommend(spot_id, n, service.geo_index, service.store, service.params)
        if spot_ids is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"spot_id {spot_id} not found")
        request.state.recommendation = {"run_id": service.run_id, "spot_id": spot_id, "n": n, "spot_ids": spot_ids}
        return Recommendations(run_id=service.run_id, spot_ids=spot_ids)

    @app.get("/health")
    def get_health(_: ReadyService) -> Health:
        return Health(status="ok")

    @app.get("/version")
    def get_version(service: ReadyService) -> Version:
        return Version(run_id=service.run_id, params=service.params)

    return app


app = create_app()
