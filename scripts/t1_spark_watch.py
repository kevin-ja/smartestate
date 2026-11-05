#!/usr/bin/env python3
"""
Captures Spark's own metrics during a T1 run, from the laptop, through the driver proxy. Read-only.

    ./ss/bin/python scripts/t1_spark_watch.py watch [<run_id>|latest]   run it while T1 runs
    ./ss/bin/python scripts/t1_spark_watch.py report <run_id>           tables for data_pipeline.md

Saves to logs/t1_run_<run_id>/spark_api/. The cluster's metrics die with it, so a file is only replaced
by a valid, non-empty response: a failed request never erases what was already captured. Standard
library only. --api-url reads a Spark API directly, with no Databricks (used to test this script).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROFILE = "smartestate-dev"
SPARK_UI_PORT = 40001
THREAD_SECONDS = 5
SNAPSHOT_SECONDS = 20
TASK_LISTS_PER_SNAPSHOT = 25
TERMINAL = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}
SNAPSHOTS = ("stages", "jobs", "sql", "executors", "storage/rdd", "environment")


def databricks(*args: str) -> dict | list | None:
    done = subprocess.run(
        ["databricks", *args], env={**os.environ, "DATABRICKS_CONFIG_PROFILE": PROFILE},
        capture_output=True, text=True, timeout=60,
    )
    return _parse(done.stdout) if done.returncode == 0 else None


def _parse(text: str) -> dict | list | None:
    try:
        return json.loads(text) or None
    except json.JSONDecodeError:  # the proxy answers with an HTML page when the cluster is gone
        return None


class SparkApi:
    def __init__(self, base: str, direct: bool):
        self.base, self.direct = base.rstrip("/"), direct

    def get(self, path: str) -> dict | list | None:
        url = f"{self.base}/{path}"
        try:
            if self.direct:
                with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - local URL given by the user
                    return _parse(response.read().decode())
            return databricks("api", "get", url)
        except (OSError, subprocess.TimeoutExpired):
            return None


def save(path: Path, data: dict | list | None) -> bool:
    if not data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(path)
    return True


def _latest_run() -> str:
    jobs = databricks("jobs", "list", "-o", "json") or []
    job = next(j for j in (jobs if isinstance(jobs, list) else jobs["jobs"]) if "smartestate_train" in j["settings"]["name"])
    runs = databricks("jobs", "list-runs", "--job-id", str(job["job_id"]), "-o", "json") or []
    return str((runs if isinstance(runs, list) else runs["runs"])[0]["run_id"])


def _proxy_base(run: dict) -> str | None:
    cluster = next((t["cluster_instance"]["cluster_id"] for t in run.get("tasks", []) if t.get("cluster_instance")), None)
    if not cluster:
        return None
    org = run["run_page_url"].split("?o=")[1].split("#")[0]
    return f"/driver-proxy-api/o/{org}/{cluster}/{SPARK_UI_PORT}/api/v1"


def _sample_threads(api: SparkApi, app: str, file: Path) -> None:
    """What every task thread is doing right now, next to the stages that are active."""
    threads = api.get(f"applications/{app}/executors/driver/threads")
    if not threads:
        return
    active = api.get(f"applications/{app}/stages?status=active") or []
    sample = {
        "at": time.time(),
        "labels": sorted({str(s.get("description")) for s in active}),
        # Both ends of the stack: what it is doing right now and which phase of the task it belongs to.
        "threads": [
            {"state": t["threadState"], "stack": t["stackTrace"]["elems"][:40] + t["stackTrace"]["elems"][40:][-25:]}
            for t in threads if "task launch worker" in t["threadName"]
        ],
    }
    if sample["threads"]:
        with file.open("a") as out:
            out.write(json.dumps(sample) + "\n")


def _sample_threads_forever(api: SparkApi, app: str, file: Path) -> None:
    """In its own thread: a snapshot takes tens of seconds and would leave gaps in the samples."""
    while True:
        _sample_threads(api, app, file)
        time.sleep(THREAD_SECONDS)


def _snapshot(api: SparkApi, app: str, out: Path, listed: set[tuple[int, int]]) -> None:
    for name in SNAPSHOTS:
        save(out / f"{name.replace('/', '_')}.json", api.get(f"applications/{app}/{name}"))
    stages = _parse((out / "stages.json").read_text()) if (out / "stages.json").exists() else None
    pending = [s for s in stages or [] if s["status"] == "COMPLETE" and (s["stageId"], s["attemptId"]) not in listed]
    for stage in sorted(pending, key=lambda s: -s.get("executorDeserializeTime", 0))[:TASK_LISTS_PER_SNAPSHOT]:
        key = (stage["stageId"], stage["attemptId"])
        tasks = api.get(f"applications/{app}/stages/{key[0]}/{key[1]}/taskList?length=1000")
        if save(out / "tasks" / f"{key[0]}_{key[1]}.json", tasks):
            listed.add(key)


def watch(run_id: str, api_url: str | None) -> None:
    run_id = _latest_run() if run_id == "latest" else run_id
    out = REPO / "logs" / f"t1_run_{run_id}" / "spark_api"
    out.mkdir(parents=True, exist_ok=True)
    print(f"Run {run_id} -> {out}", flush=True)

    api, app, listed, snapshot_at = (SparkApi(api_url, True) if api_url else None), None, set(), 0.0
    while True:
        if not api_url:
            run = databricks("jobs", "get-run", run_id, "-o", "json") or {}
            state = run.get("state", {}).get("life_cycle_state")
            base = _proxy_base(run)
            api = SparkApi(base, False) if base else None
        else:
            state = "RUNNING"
        if api and not app:
            applications = api.get("applications")
            app = applications[0]["id"] if applications else None
            if app:
                print(f"Spark app {app}", flush=True)
                threading.Thread(
                    target=_sample_threads_forever, args=(api, app, out / "threads.jsonl"), daemon=True
                ).start()
        if app:
            if time.time() - snapshot_at >= SNAPSHOT_SECONDS:
                _snapshot(api, app, out, listed)
                snapshot_at = time.time()
                print(f"{time.strftime('%H:%M:%S')} snapshot · {state} · task lists {len(listed)}", flush=True)
        if state in TERMINAL:
            print(f"Run ended: {state}. Saved in {out}", flush=True)
            return
        time.sleep(THREAD_SECONDS)


def phase(stack: list[str]) -> str:
    """
    Spark counts as preparation both deserializing the Task and, already inside runTask, the RDD and
    function it carries. Real work is the part that iterates the RDD.
    """
    text = "\n".join(stack)
    if "RDD.iterator" in text or "RDD.computeOrReadCheckpoint" in text:
        return "running task"
    if "Executor$TaskRunner.run" in text and ("runTask" in text or "deserialize" in text.lower() or "ClassLoader" in text):
        return "preparing task"
    return "idle" if "ThreadPoolExecutor.getTask" in text else "other"


def _data_pipeline_stages(out: Path) -> list[dict]:
    """unit_tests shares the Spark app and calls the same labelled code: its stages are left out."""
    stages = json.loads((out / "stages.json").read_text())
    run = json.loads((out.parent / "run.json").read_text())
    start_ms = next(t["start_time"] for t in run["tasks"] if t["task_key"] == "data_pipeline")
    started = datetime.fromtimestamp(start_ms / 1e3, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return [s for s in stages if s.get("submissionTime", "") >= started]


def report(run_id: str) -> None:
    out = REPO / "logs" / f"t1_run_{run_id}" / "spark_api"
    stages = _data_pipeline_stages(out)
    per_label: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for stage in stages:
        if stage["status"] in ("COMPLETE", "ACTIVE"):
            totals = per_label[str(stage.get("description"))]
            totals["stages"] += 1
            totals["tasks"] += stage.get("numCompleteTasks", 0)
            totals["run"] += stage.get("executorRunTime", 0) / 1e3
            totals["prepare"] += stage.get("executorDeserializeTime", 0) / 1e3
            totals["prepare_cpu"] += stage.get("executorDeserializeCpuTime", 0) / 1e9
            totals["gc"] += stage.get("jvmGcTime", 0) / 1e3

    print("| Etapa | Stages | Tasks | Trabajo (s) | Preparación (s) | Preparación CPU (s) | GC (s) | ms preparación/task |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    grand: collections.Counter = collections.Counter()
    for label, totals in per_label.items():
        grand.update(totals)
        per_task = 1e3 * totals["prepare"] / max(totals["tasks"], 1)
        print(f"| {label} | {totals['stages']} | {totals['tasks']} | {totals['run']:.0f} | {totals['prepare']:.0f} | {totals['prepare_cpu']:.0f} | {totals['gc']:.0f} | {per_task:.0f} |")
    print(f"| **Total** | {grand['stages']} | {grand['tasks']} | {grand['run']:.0f} | {grand['prepare']:.0f} | {grand['prepare_cpu']:.0f} | {grand['gc']:.0f} | {1e3 * grand['prepare'] / max(grand['tasks'], 1):.0f} |")

    labels = {f"{s['stageId']}_{s['attemptId']}": str(s.get("description")) for s in stages}
    prepare: dict[str, list[float]] = collections.defaultdict(list)
    for file in (out / "tasks").glob("*.json"):
        if file.stem not in labels:
            continue
        for task in json.loads(file.read_text()):
            metrics = task.get("taskMetrics")
            if metrics:
                prepare[labels.get(file.stem, "?")].append(metrics["executorDeserializeTime"])
    if prepare:
        print("\n| Etapa | Tasks medidas | Preparación p50 (ms) | p95 (ms) | Máx (ms) |")
        print("|---|---:|---:|---:|---:|")
        for label, values in prepare.items():
            values.sort()
            print(f"| {label} | {len(values)} | {statistics.median(values):.0f} | {values[int(0.95 * (len(values) - 1))]:.0f} | {values[-1]:.0f} |")

    threads = out / "threads.jsonl"
    if threads.exists():
        phases: collections.Counter = collections.Counter()
        leaves: collections.Counter = collections.Counter()
        anywhere: collections.Counter = collections.Counter()
        for line in threads.read_text().splitlines():
            for thread in json.loads(line)["threads"]:
                current = phase(thread["stack"])
                phases[current] += 1
                if current == "preparing task":
                    leaves[thread["stack"][0].split("(")[0]] += 1
                    anywhere.update({frame.split("(")[0] for frame in thread["stack"]})
        print(f"\nHilos de task, por fase: {dict(phases)}")
        print("Al preparar tasks, frame en ejecución:")
        for frame, count in leaves.most_common(15):
            print(f"  {count:>5}  {frame}")
        print("Al preparar tasks, frames presentes en el stack (Databricks y carga de clases primero):")
        relevant = [(f, c) for f, c in anywhere.most_common() if "databricks" in f.lower() or "ClassLoader" in f]
        for frame, count in (relevant + anywhere.most_common(10))[:25]:
            print(f"  {count:>5}  {frame}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("watch", "report"))
    parser.add_argument("run_id", nargs="?", default="latest")
    parser.add_argument("--api-url", help="Spark API URL, e.g. http://localhost:4040/api/v1")
    args = parser.parse_args()
    if args.command == "watch":
        watch(args.run_id, args.api_url)
    else:
        report(args.run_id)


if __name__ == "__main__":
    main()
