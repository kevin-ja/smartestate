"""
Run monitoring for job entrypoints: how long each stage takes and what the machine is doing meanwhile.

    stage(name)         logs start and end, keeps the duration, labels the Spark jobs of the stage
    sample_resources()  background thread: CPU, memory and page faults every SAMPLE_SECONDS
    log_summary()       one table at the end of the run, also when the run fails

Monitoring never breaks a run: its own errors are logged and ignored. Resources come from /proc, with
no new dependency; outside Linux there is no sampling.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pyspark.sql import SparkSession

logger = logging.getLogger(__name__)

SAMPLE_SECONDS = 10
TOP_PROCESSES = 3
GB_IN_KB = 1024 * 1024

SPARK_SETTINGS = (
    "spark.databricks.clusterUsageTags.clusterNodeType",
    "spark.databricks.clusterUsageTags.sparkVersion",
    "spark.master",
    "spark.driver.memory",
    "spark.sql.shuffle.partitions",
    "spark.sql.adaptive.enabled",
)

_durations: dict[str, float] = {}
_min_available_gb: dict[str, float] = {}
_current_stage: str | None = None


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


def _label_spark_jobs(name: str | None) -> None:
    """The Spark UI and the event log show each job under its stage."""
    try:
        session = SparkSession.getActiveSession()
        if session is not None:
            session.sparkContext.setJobDescription(name)
    except Exception as error:  # noqa: BLE001 - monitoring never breaks a run
        logger.debug("Could not label Spark jobs: %s", error)


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Times a block. Spark is lazy: the block must end in an action to measure the real work."""
    global _current_stage
    previous, _current_stage = _current_stage, name
    _label_spark_jobs(name)
    logger.info("stage start: %s", name)
    started = time.perf_counter()
    try:
        yield
    except BaseException:
        logger.error("stage failed: %s after %.1f s", name, time.perf_counter() - started)
        raise
    finally:
        seconds = time.perf_counter() - started
        _durations[name] = _durations.get(name, 0.0) + seconds
        _current_stage = previous
        _label_spark_jobs(previous)
    logger.info("stage end: %s · %.1f s", name, seconds)


def durations() -> dict[str, float]:
    return dict(_durations)


def log_summary() -> None:
    """Stage, seconds, share of the run and lowest available memory seen during the stage."""
    total = sum(_durations.values())
    lines = [f"{'stage':<28}{'seconds':>10}{'share':>8}{'min_avail_gb':>14}"]
    for name, seconds in _durations.items():
        available = _min_available_gb.get(name)
        lines.append(
            f"{name:<28}{seconds:>10.1f}{seconds / total if total else 0:>8.0%}"
            f"{'-' if available is None else format(available, '.1f'):>14}"
        )
    lines.append(f"{'total':<28}{total:>10.1f}")
    logger.info("stage summary\n%s", "\n".join(lines))


# --------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------


def _read_cpu() -> list[int]:
    """user, nice, system, idle, iowait, irq, softirq, steal (cumulative ticks)."""
    return [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]


def _read_major_faults() -> int:
    for line in Path("/proc/vmstat").read_text().splitlines():
        if line.startswith("pgmajfault "):
            return int(line.split()[1])
    return 0


def _read_memory_kb() -> dict[str, int]:
    return {
        line.split(":")[0]: int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith(("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"))
    }


def _top_processes() -> str:
    """The processes using the most memory: tells the driver's JVM from the Python of SBERT."""
    found = []
    for status in Path("/proc").glob("[0-9]*/status"):
        try:
            fields = dict(line.split(":", 1) for line in status.read_text().splitlines() if ":" in line)
            found.append((int(fields["VmRSS"].split()[0]), fields["Name"].strip()))
        except (OSError, KeyError, ValueError):
            continue  # the process ended or has no memory of its own
    return ",".join(f"{name}:{rss / GB_IN_KB:.1f}GB" for rss, name in sorted(found, reverse=True)[:TOP_PROCESSES])


def _sample(previous_cpu: list[int], previous_faults: int, seconds: float) -> tuple[list[int], int]:
    cpu, faults = _read_cpu(), _read_major_faults()
    memory = _read_memory_kb()
    deltas = [now - before for now, before in zip(cpu, previous_cpu)]
    ticks = sum(deltas) or 1
    available = memory["MemAvailable"] / GB_IN_KB
    name = _current_stage or "-"
    if _current_stage:
        _min_available_gb[name] = min(available, _min_available_gb.get(name, available))
    logger.info(
        "resources stage=%s cpu=%.0f%% iowait=%.0f%% steal=%.0f%% load=%.1f mem_available=%.1f/%.1fGB "
        "swap_used=%.1fGB major_faults_per_s=%.0f top_rss=%s",
        name,
        100 * (ticks - deltas[3] - deltas[4]) / ticks,
        100 * deltas[4] / ticks,
        100 * deltas[7] / ticks,
        os.getloadavg()[0],
        available,
        memory["MemTotal"] / GB_IN_KB,
        (memory["SwapTotal"] - memory["SwapFree"]) / GB_IN_KB,
        (faults - previous_faults) / max(seconds, 1e-3),
        _top_processes(),
    )
    return cpu, faults


def _sampling_loop(stop: threading.Event, interval: float) -> None:
    """A gap between two lines longer than the interval is itself a finding: the machine froze."""
    try:
        cpu, faults = _read_cpu(), _read_major_faults()
        sampled_at = time.perf_counter()
        while not stop.wait(interval):
            now = time.perf_counter()
            cpu, faults = _sample(cpu, faults, now - sampled_at)
            sampled_at = now
    except Exception as error:  # noqa: BLE001 - monitoring never breaks a run
        logger.warning("Resource sampling stopped: %s", error)


@contextmanager
def sample_resources(interval: float = SAMPLE_SECONDS) -> Iterator[None]:
    stop = threading.Event()
    thread = threading.Thread(target=_sampling_loop, args=(stop, interval), daemon=True, name="resource-sampler")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


def log_environment(spark: SparkSession) -> None:
    """What the run had available, so two runs on different machines can be compared."""
    def setting(key: str) -> str:
        # Without a default: passing None returns null for a setting left at Spark's own default.
        try:
            return spark.conf.get(key)
        except Exception:  # noqa: BLE001 - a setting that does not exist here
            return "unset"

    try:
        settings = " · ".join(f"{key}={setting(key)}" for key in SPARK_SETTINGS)
        logger.info("environment: cpus=%s python=%s · %s", os.cpu_count(), sys.version.split()[0], settings)
    except Exception as error:  # noqa: BLE001 - monitoring never breaks a run
        logger.warning("Could not log the environment: %s", error)
