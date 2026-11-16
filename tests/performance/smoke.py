"""
Synthetic traffic to check the monitoring end to end (monitor.md): a few users, a few minutes, low load.

Same visitor as locustfile.py, but 90 % of the clicks go to spots of ref/benchmark/benchmark.csv, so the weekly
metrics have something to score, and the rest to spots outside it, so the coverage is tested too. The 10 % of
unknown spot_ids (404) stays. Afterwards, scripts/verify_monitoring.py checks the log and the Lambda.

    ./ss/bin/locust -f tests/performance/smoke.py --host "$HOST" --headless -u 2 -r 1 --run-time 5m
"""

from __future__ import annotations

import csv
import io
import os
import random
from urllib.parse import urlparse

import boto3
from locust import events

from locustfile import Visitor, state  # noqa: F401  (Visitor is the user Locust runs)

BENCHMARK_SHARE = float(os.getenv("BENCHMARK_SHARE", "0.9"))
CLICKS_IN_POOL = 1000


def benchmark_spot_ids(runs_root: str) -> set[int]:
    """Clicked spots of benchmark.csv, read from ref/ next to runs/ in the same bucket."""
    bucket = urlparse(runs_root).netloc
    s3 = boto3.Session(profile_name=os.getenv("AWS_PROFILE", "smartestate")).client("s3")
    body = s3.get_object(Bucket=bucket, Key="ref/benchmark/benchmark.csv")["Body"].read().decode("utf-8")
    return {int(row["spot_id"]) for row in csv.DictReader(io.StringIO(body))}


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Runs after locustfile's preflight: replaces its click pool with one weighted toward the benchmark."""
    if not state["spot_ids"]:
        return  # the preflight already aborted
    known = set(state["spot_ids"])
    inside = sorted(known & benchmark_spot_ids(os.environ["RUNS_ROOT"]))
    outside = sorted(known - set(inside))
    if not inside:
        print("[smoke] ningún spot del benchmark está en el GeoIndex de producción. Abortando.")
        environment.runner.quit()
        return
    pool_inside = round(CLICKS_IN_POOL * BENCHMARK_SHARE)
    state["spot_ids"] = random.choices(inside, k=pool_inside) + random.choices(outside or inside, k=CLICKS_IN_POOL - pool_inside)
    print(f"[smoke] {len(inside)} spots del benchmark y {len(outside)} fuera; {BENCHMARK_SHARE:.0%} de los clics al benchmark.")
