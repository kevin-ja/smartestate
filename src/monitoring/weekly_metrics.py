"""
Weekly Lambda (monitor.md, "Cálculo de las métricas"): Recall@10 and NDCG@10 of production, from the API's log.

    Logs Insights (clicks of the week, grouped) -> cross with ref/benchmark/benchmark.csv -> score each click
    -> one row per run_id in runs/<run_id>/metrics/weekly_<date>.csv -> metrics in CloudWatch (dimension run_id)

Every click weighs the same, so the mean over clicks is already weighted by frequency. Clicks on spots outside
the benchmark cannot be scored and are reported as coverage. The run's own benchmark_scores.csv (training) is
read when it exists. For the run in runs/production.json, production_ndcg_ratio = production NDCG / training NDCG
goes to CloudWatch without dimensions: the alarm fires below 0.9 (monitor.md, "Alarmas").

Packaged by Terraform (infra/modules/monitoring) with src/common/ranking_metrics.py next to it. Runs on the
EventBridge cron every Sunday; a manual invocation may pass {"start": ISO, "end": ISO} to score any window.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

from src.common import ranking_metrics

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BENCHMARK_KEY = "ref/benchmark/benchmark.csv"
RUNS_PREFIX = "runs"
SCORES_FILE = "benchmark_scores.csv"
PRODUCTION_KEY = "runs/production.json"
RATIO_METRIC = "production_ndcg_ratio"
QUERY = """
fields run_id, spot_id, n, spot_ids
| filter event = "request" and status = 200 and ispresent(spot_id)
| stats count(*) as clicks by run_id, spot_id, n, spot_ids
| limit 10000
"""
QUERY_POLL_SECONDS = 2
QUERY_TIMEOUT_SECONDS = 120
WEEKLY_COLUMNS = (
    "run_id", "week_start", "week_end", "clicks", "clicks_in_benchmark", "spots_in_benchmark", "coverage",
    "recall_at_10", "ndcg_at_10", "benchmark_recall_at_10", "benchmark_ndcg_at_10",
)


class MissingObject(Exception):
    """An S3 object the Lambda needs does not exist."""


class QueryFailed(Exception):
    """Logs Insights did not complete the query."""


# ---------------------------------------------------------------- pure


def flatten(results: Iterable[Iterable[Mapping[str, str]]]) -> list[dict[str, str]]:
    """Logs Insights rows ([{field, value}, ...]) -> dicts, without the @ptr field."""
    return [{cell["field"]: cell["value"] for cell in row if not cell["field"].startswith("@")} for row in results]


def weekly_scores(rows: Iterable[Mapping[str, str]], benchmark: Mapping[int, Mapping[int, int]]) -> list[dict[str, Any]]:
    """One summary per run_id from the grouped log rows. Metrics are None when no click fell in the benchmark."""
    runs: dict[str, dict[str, Any]] = {}
    for row in rows:
        run = runs.setdefault(row["run_id"], {"clicks": 0, "scored": 0, "recall": 0.0, "ndcg": 0.0, "spots": set()})
        clicks = int(row["clicks"])
        run["clicks"] += clicks
        spot_id = int(row["spot_id"])
        if spot_id not in benchmark:
            continue
        # An empty answer logs spot_ids as "", and Logs Insights drops empty fields: missing means no spots.
        ranked = [int(value) for value in row.get("spot_ids", "").split(",") if value]
        scores = ranking_metrics.score(ranked, benchmark[spot_id])
        if scores is None:
            continue
        run["scored"] += clicks
        run["recall"] += scores.recall_at_k * clicks
        run["ndcg"] += scores.ndcg_at_k * clicks
        run["spots"].add(spot_id)
    return [
        {
            "run_id": run_id,
            "clicks": run["clicks"],
            "clicks_in_benchmark": run["scored"],
            "spots_in_benchmark": len(run["spots"]),
            "coverage": run["scored"] / run["clicks"] if run["clicks"] else 0.0,
            "recall_at_10": run["recall"] / run["scored"] if run["scored"] else None,
            "ndcg_at_10": run["ndcg"] / run["scored"] if run["scored"] else None,
        }
        for run_id, run in sorted(runs.items())
    ]


def benchmark_mean(text: str) -> dict[str, float]:
    """Mean recall and NDCG of a run's benchmark_scores.csv (what training measured)."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return {"recall_at_10": 0.0, "ndcg_at_10": 0.0}
    return {
        "recall_at_10": sum(float(row["recall_at_10"]) for row in rows) / len(rows),
        "ndcg_at_10": sum(float(row["ndcg_at_10"]) for row in rows) / len(rows),
    }


def weekly_csv(runs: Iterable[Mapping[str, Any]]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=WEEKLY_COLUMNS)
    writer.writeheader()
    for run in runs:
        writer.writerow({column: ("" if run.get(column) is None else run[column]) for column in WEEKLY_COLUMNS})
    return out.getvalue()


def production_ratio(runs: Iterable[Mapping[str, Any]], production_run_id: str | None) -> float | None:
    """Production NDCG over training NDCG for the run in production. None when either side is missing or zero."""
    for run in runs:
        if run["run_id"] == production_run_id and run.get("ndcg_at_10") is not None and run.get("benchmark_ndcg_at_10"):
            return run["ndcg_at_10"] / run["benchmark_ndcg_at_10"]
    return None


def window(event: Mapping[str, Any], now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end): the event's ISO bounds, or the seven days before today at 00:00 UTC."""
    if "start" in event and "end" in event:
        return _parse(event["start"]), _parse(event["end"])
    today = (now or datetime.now(timezone.utc)).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=7), today


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- AWS


def clients() -> tuple[Any, Any, Any]:
    return boto3.client("logs"), boto3.client("s3"), boto3.client("cloudwatch")


def read_object(s3: Any, bucket: str, key: str) -> str:
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    except MissingObject:
        raise
    except Exception as error:  # botocore raises a ClientError whose code is NoSuchKey
        if "NoSuchKey" in str(error) or "NoSuchKey" in type(error).__name__:
            raise MissingObject(f"s3://{bucket}/{key}") from None
        raise


def query_clicks(logs: Any, log_group: str, start: datetime, end: datetime) -> list[dict[str, str]]:
    query_id = logs.start_query(
        logGroupName=log_group, startTime=int(start.timestamp()), endTime=int(end.timestamp()), queryString=QUERY,
    )["queryId"]
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    while True:
        response = logs.get_query_results(queryId=query_id)
        if response["status"] == "Complete":
            return flatten(response["results"])
        if response["status"] in ("Failed", "Cancelled", "Timeout") or time.monotonic() > deadline:
            raise QueryFailed(f"Logs Insights query {query_id}: {response['status']}")
        time.sleep(QUERY_POLL_SECONDS)


def publish_metrics(cloudwatch: Any, namespace: str, runs: Iterable[Mapping[str, Any]], at: datetime, ratio: float | None) -> None:
    data = [] if ratio is None else [{"MetricName": RATIO_METRIC, "Value": ratio, "Timestamp": at}]
    for run in runs:
        for name in ("clicks", "clicks_in_benchmark", "coverage", "recall_at_10", "ndcg_at_10", "benchmark_recall_at_10", "benchmark_ndcg_at_10"):
            if run.get(name) is not None:
                data.append({
                    "MetricName": name, "Value": float(run[name]), "Timestamp": at,
                    "Dimensions": [{"Name": "run_id", "Value": run["run_id"]}],
                })
    for chunk in range(0, len(data), 20):  # PutMetricData takes 20 at a time
        cloudwatch.put_metric_data(Namespace=namespace, MetricData=data[chunk:chunk + 20])


def handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    logs, s3, cloudwatch = clients()
    log_group, bucket = os.environ["LOG_GROUP"], os.environ["LAKE_BUCKET"]
    namespace = os.environ.get("METRIC_NAMESPACE", "SmartEstate/Model")
    start, end = window(event or {})

    benchmark = ranking_metrics.read_benchmark_csv(read_object(s3, bucket, BENCHMARK_KEY))
    rows = query_clicks(logs, log_group, start, end)
    runs = weekly_scores(rows, benchmark)
    for run in runs:
        run["week_start"], run["week_end"] = start.date().isoformat(), end.date().isoformat()
        try:
            trained = benchmark_mean(read_object(s3, bucket, f"{RUNS_PREFIX}/{run['run_id']}/{SCORES_FILE}"))
            run["benchmark_recall_at_10"], run["benchmark_ndcg_at_10"] = trained["recall_at_10"], trained["ndcg_at_10"]
        except MissingObject:
            run["benchmark_recall_at_10"] = run["benchmark_ndcg_at_10"] = None
        key = f"{RUNS_PREFIX}/{run['run_id']}/metrics/weekly_{run['week_end']}.csv"
        s3.put_object(Bucket=bucket, Key=key, Body=weekly_csv([run]), ContentType="text/csv")
        run["csv"] = f"s3://{bucket}/{key}"

    try:
        production_run_id = str(json.loads(read_object(s3, bucket, PRODUCTION_KEY))["run_id"])
    except MissingObject:
        production_run_id = None
    ratio = production_ratio(runs, production_run_id)
    publish_metrics(cloudwatch, namespace, runs, end, ratio)
    summary = {
        "window": [start.isoformat(), end.isoformat()], "log_rows": len(rows), "production_run_id": production_run_id,
        RATIO_METRIC: ratio, "runs": runs,
    }
    logger.info(json.dumps(summary, default=str))
    return summary
