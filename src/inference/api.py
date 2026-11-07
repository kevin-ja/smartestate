"""
FastAPI service (new_arch.md §4). Loads one run at startup and answers each click.

    RUN_ID=<run_id> RUNS_ROOT=s3://<lake>/runs AWS_REGION=us-east-1 uvicorn src.inference.api:app
"""

from __future__ import annotations

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
from src.inference.online_store import OnlineStore
from src.ml_pipeline.materialization import online_table_name


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
    s3 = boto3.client("s3", region_name=region)
    return Service(
        run_id=run_id,
        geo_index=artifacts.load_geo_index(s3, runs_root, run_id),
        params=artifacts.load_reranker_params(s3, runs_root, run_id),
        store=OnlineStore(online_table_name(run_id), boto3.client("dynamodb", region_name=region)),
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

    # Time spent inside the service, without the network. The load test (tests/performance/) reads it.
    @app.middleware("http")
    async def server_timing(request: Request, call_next: Callable) -> Any:
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - start) * 1000:.1f}"
        return response

    # Plain `def`: FastAPI runs them in a thread pool, so the blocking DynamoDB reads do not stop other requests.
    @app.get("/recommendations/{spot_id}")
    def get_recommendations(
        service: ReadyService,
        spot_id: Annotated[int, Path()],
        n: Annotated[int, Query(ge=1, le=recommender.MAX_N)] = recommender.DEFAULT_N,
    ) -> Recommendations:
        spot_ids = recommender.recommend(spot_id, n, service.geo_index, service.store, service.params)
        if spot_ids is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"spot_id {spot_id} not found")
        return Recommendations(run_id=service.run_id, spot_ids=spot_ids)

    @app.get("/health")
    def get_health(_: ReadyService) -> Health:
        return Health(status="ok")

    @app.get("/version")
    def get_version(service: ReadyService) -> Version:
        return Version(run_id=service.run_id, params=service.params)

    return app


app = create_app()
