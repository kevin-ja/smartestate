"""
End-to-end check of the monitoring (monitor.md), after synthetic traffic (tests/performance/smoke.py).

    1. The log: every click left a well-formed JSON line (run_id, spot_id, n, spot_ids in order, at most n).
    2. Production metrics: the Lambda, invoked on the same window, gives the same Recall@10 and NDCG@10 that this
       script computes from the same log with the same function.
    3. Training vs production: for a spot scored in runs/<run_id>/benchmark_scores.csv, the score of what the API
       returned matches the training score. The cascade is deterministic, so both sides must agree.

Read-only, except that it invokes the Lambda, which writes its weekly CSV for that window.

    ./ss/bin/python scripts/verify_monitoring.py --start 2026-11-16T15:00:00Z --end 2026-11-16T15:10:00Z
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root: src.*

from src.common import ranking_metrics  # noqa: E402
from src.monitoring import weekly_metrics as wm  # noqa: E402

LINES_QUERY = """
fields @timestamp, status, path, run_id, spot_id, n, spot_ids
| filter event = "request" and path like "/recommendations/"
| limit 10000
"""
TOLERANCE = 1e-9


def query(logs, log_group: str, start, end, text: str) -> list[dict[str, str]]:
    query_id = logs.start_query(
        logGroupName=log_group, startTime=int(start.timestamp()), endTime=int(end.timestamp()), queryString=text,
    )["queryId"]
    while True:
        response = logs.get_query_results(queryId=query_id)
        if response["status"] == "Complete":
            return wm.flatten(response["results"])
        if response["status"] in ("Failed", "Cancelled", "Timeout"):
            raise wm.QueryFailed(response["status"])
        time.sleep(2)


def check_lines(lines: list[dict[str, str]]) -> list[str]:
    """Problems found in the request lines of the clicks. Empty list: the log is what the monitoring needs."""
    problems = []
    ok = [line for line in lines if line.get("status") == "200"]
    if not ok:
        problems.append("no hay clics con status 200 en la ventana")
    for line in ok:
        missing = [field for field in ("run_id", "spot_id", "n", "spot_ids") if field not in line]
        if missing:
            problems.append(f"línea sin {missing}: {line}")
            continue
        ranked = [value for value in line["spot_ids"].split(",") if value]
        if not all(value.isdigit() for value in ranked) or len(ranked) > int(line["n"]) or str(line["spot_id"]) in ranked:
            problems.append(f"spot_ids mal formado: {line}")
    return problems


def same(a, b) -> bool:
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) <= TOLERANCE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, help="ISO, UTC: start of the synthetic traffic")
    parser.add_argument("--end", required=True, help="ISO, UTC: end of the synthetic traffic")
    parser.add_argument("--profile", default="smartestate")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--log-group", default="/smartestate/inference")
    parser.add_argument("--function", default="smartestate-monitoring-weekly-metrics")
    parser.add_argument("--settle-seconds", type=int, default=60, help="wait for the last lines to reach CloudWatch")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    logs, s3, lam = session.client("logs"), session.client("s3"), session.client("lambda")
    bucket = f"smartestate-lake-{session.client('sts').get_caller_identity()['Account']}"
    start, end = wm.window({"start": args.start, "end": args.end})
    print(f"Ventana {start.isoformat()} → {end.isoformat()}; esperando {args.settle_seconds} s a que lleguen los logs")
    time.sleep(args.settle_seconds)

    failures = 0

    def report(name: str, problems: list[str]) -> None:
        nonlocal failures
        failures += bool(problems)
        print(f"{'✓' if not problems else '✗'} {name}")
        for problem in problems[:10]:
            print(f"    {problem}")

    # 1. The log
    lines = query(logs, args.log_group, start, end, LINES_QUERY)
    statuses = {}
    for line in lines:
        statuses[line.get("status")] = statuses.get(line.get("status"), 0) + 1
    print(f"  líneas de clic: {len(lines)} · por status: {statuses}")
    report("1. Log por request con run_id, spot_id, n y spot_ids", check_lines(lines))

    # 2. Lambda vs local, on the same window and the same benchmark
    benchmark = ranking_metrics.read_benchmark_csv(wm.read_object(s3, bucket, wm.BENCHMARK_KEY))
    local = {run["run_id"]: run for run in wm.weekly_scores(query(logs, args.log_group, start, end, wm.QUERY), benchmark)}
    response = lam.invoke(FunctionName=args.function, Payload=json.dumps({"start": args.start, "end": args.end}).encode())
    payload = json.loads(response["Payload"].read())
    if response.get("FunctionError"):
        report("2. Lambda semanal", [f"la Lambda falló: {payload}"])
        remote = {}
    else:
        remote = {run["run_id"]: run for run in payload["runs"]}
        problems = []
        for run_id, run in local.items():
            got = remote.get(run_id)
            if got is None:
                problems.append(f"{run_id}: la Lambda no lo reporta")
                continue
            for field in ("clicks", "clicks_in_benchmark", "recall_at_10", "ndcg_at_10"):
                if not same(run[field], got[field]):
                    problems.append(f"{run_id}.{field}: local {run[field]} · Lambda {got[field]}")
            print(f"  {run_id}: clics {got['clicks']} · en benchmark {got['clicks_in_benchmark']} · "
                  f"recall@10 {got['recall_at_10']} · ndcg@10 {got['ndcg_at_10']} · {got.get('csv')}")
        report("2. Lambda semanal = cálculo local sobre el mismo log", problems)

    # 3. Training vs production, spot by spot
    problems, compared = [], 0
    for run_id in local:
        try:
            text = wm.read_object(s3, bucket, f"{wm.RUNS_PREFIX}/{run_id}/{wm.SCORES_FILE}")
        except wm.MissingObject:
            problems.append(f"{run_id}: no tiene {wm.SCORES_FILE} (corrida entrenada antes de la prioridad 2)")
            continue
        trained = {int(row["spot_id"]): row for row in csv.DictReader(io.StringIO(text))}
        for line in lines:
            if line.get("status") != "200" or line.get("run_id") != run_id or line.get("n") != str(ranking_metrics.K):
                continue
            spot_id = int(line["spot_id"])
            if spot_id not in trained:
                continue
            ranked = [int(value) for value in line["spot_ids"].split(",") if value]
            scores = ranking_metrics.score(ranked, benchmark[spot_id])
            compared += 1
            row = trained[spot_id]
            if not (same(scores.recall_at_k, row["recall_at_10"]) and same(scores.ndcg_at_k, row["ndcg_at_10"])):
                problems.append(f"{run_id}/{spot_id}: producción {scores} · entrenamiento {row}")
    print(f"  clics comparados con el entrenamiento: {compared}")
    if compared == 0 and not problems:
        problems.append("ningún clic coincidió con un spot de benchmark_scores.csv")
    report("3. Entrenamiento = producción para el mismo run_id y spot_id", problems)

    print("\nTodo en verde." if not failures else f"\n{failures} comprobación(es) fallaron.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
