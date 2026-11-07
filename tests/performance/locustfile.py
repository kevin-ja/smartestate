"""
Baseline of the inference API: one visitor clicking for a few minutes (tests/performance/README.md).

The virtual user is a visitor of the front: each click on a listing asks for its recommendations.
  - Clicks on real spot_ids: at startup the GeoIndex of the run in production is read from S3 (/version says which).
  - 10 % of the clicks ask for a spot_id that does not exist. The API answers 404 and that is correct.
  - Exponential wait with a mean of THINK_TIME_SECONDS: clicks come in bursts, and bursts are what saturate a service.
  - A /health heartbeat runs in the background. It does almost no work, so its latency is the network floor.

stress.py reuses this same user, so both tests are comparable.
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

import boto3
import gevent
import requests
from locust import FastHttpUser, events, task

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: src.inference

from slo import CLICK_METRIC, HEALTH_METRIC, SERVER_METRIC, baseline_report, measure  # noqa: E402
from src.inference.artifacts import load_geo_index  # noqa: E402

THINK_TIME_SECONDS = float(os.getenv("THINK_TIME_SECONDS", "2"))
UNKNOWN_SHARE = float(os.getenv("UNKNOWN_SHARE", "0.10"))
HEALTH_INTERVAL_SECONDS = float(os.getenv("HEALTH_INTERVAL_SECONDS", "2"))

state: dict = {"spot_ids": [], "run_id": None, "unknown_spot_id": None, "started": None}
_heartbeat = None


def _record(name: str, milliseconds: float, request_type: str, exception: Exception | None = None) -> None:
    """Locust only computes percentiles of what it records as a request: the server time is recorded that way."""
    events.request.fire(
        request_type=request_type, name=name, response_time=milliseconds,
        response_length=0, exception=exception, context={},
    )


def _server_ms(headers) -> float | None:
    value = headers.get("Server-Timing", "")
    return float(value.removeprefix("app;dur=")) if value.startswith("app;dur=") else None


class Visitor(FastHttpUser):
    # Generous on purpose: a slow click must be measured, not hidden behind a client timeout.
    network_timeout = 30.0
    connection_timeout = 10.0

    def wait_time(self):
        return min(random.expovariate(1.0 / THINK_TIME_SECONDS), THINK_TIME_SECONDS * 5)

    @task
    def click(self):
        unknown = random.random() < UNKNOWN_SHARE
        spot_id = state["unknown_spot_id"] if unknown else random.choice(state["spot_ids"])
        with self.client.get(f"/recommendations/{spot_id}", name=CLICK_METRIC, catch_response=True) as response:
            server_ms = _server_ms(response.headers or {})
            if server_ms is not None:
                _record(SERVER_METRIC, server_ms, "TIMING")

            if unknown:
                if response.status_code == 404:
                    response.success()
                else:
                    response.failure(f"spot_id inexistente respondió HTTP {response.status_code}")
                return
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            try:
                body = response.json()
            except Exception as exc:
                response.failure(f"la respuesta no es JSON: {exc}")
                return
            # A deployment in the middle of the test would mix two runs: it is a failure, not a measure.
            if body.get("run_id") != state["run_id"]:
                response.failure(f"run_id {body.get('run_id')} distinto del inicial {state['run_id']}")
                return
            if not isinstance(body.get("spot_ids"), list):
                response.failure("falta spot_ids")
                return
            response.success()  # an empty list is valid: ~5 % of the clicks have no candidates


def _heartbeat_loop(host: str) -> None:
    url = f"{host.rstrip('/')}/health"
    while True:
        start = time.perf_counter()
        try:
            status = requests.get(url, timeout=10).status_code
            error = None if status == 200 else Exception(f"HTTP {status}")
        except Exception as exc:
            error = exc
        _record(HEALTH_METRIC, (time.perf_counter() - start) * 1000, "GET", error)
        gevent.sleep(HEALTH_INTERVAL_SECONDS)


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Checks the API and loads the spot_ids before any load: a wrong setup aborts here, not after 20 minutes."""
    global _heartbeat
    host = (environment.host or "").rstrip("/")
    try:
        version = requests.get(f"{host}/version", timeout=10)
        version.raise_for_status()
        state["run_id"] = version.json()["run_id"]
        runs_root = os.environ["RUNS_ROOT"]
        s3 = boto3.Session(profile_name=os.getenv("AWS_PROFILE", "smartestate")).client("s3")
        geo_index = load_geo_index(s3, runs_root, state["run_id"])
    except Exception as exc:
        print(f"[preflight] no se pudo preparar la prueba contra {host}: {exc!r}. Abortando.")
        if environment.runner is not None:
            environment.runner.quit()
        return

    state["spot_ids"] = [int(spot_id) for spot_id in geo_index.spot_id]
    state["unknown_spot_id"] = max(state["spot_ids"]) + 1_000_000
    state["started"] = time.monotonic()
    print(f"[preflight] {host} sirve run_id {state['run_id']}; {len(state['spot_ids'])} spot_ids para los clics.")
    _heartbeat = gevent.spawn(_heartbeat_loop, host)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    global _heartbeat
    if _heartbeat is not None:
        _heartbeat.kill(block=False)
        _heartbeat = None


@events.quitting.add_listener
def on_quitting(environment, **kwargs):
    if environment.shape_class is not None:  # stress.py prints its own report
        return
    if state["started"] is None:
        environment.process_exit_code = 1
        return
    users = environment.parsed_options.num_users if environment.parsed_options else 0
    result = measure(environment.stats, users, time.monotonic() - state["started"])
    print(baseline_report(result))
    environment.process_exit_code = 0 if result.passed else 1
