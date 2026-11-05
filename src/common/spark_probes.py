"""
Controlled Spark experiments, run after T1 has written everything (--spark-probes true).

T1 lost ~26 min preparing tasks (data_pipeline.md). Each probe changes one factor, so comparing their
per-task preparation time tells which factor costs it:

    plain_8 vs plain_200        number of tasks, with no cache and no shuffle
    plain_200 vs cache_200_read reading from a cache
    cache_200_read vs lineage_200_read  a large query plan behind the cache
    lineage_8_read              the same cached data, split as T1 now splits it

Each probe is one Spark job labelled with its stage name, so scripts/t1_spark_watch.py can read its
metrics. A probe that fails is logged and ignored: by this point T1 has already finished its work.
"""

from __future__ import annotations

import logging
import time

from pyspark.sql import DataFrame

from src.common.run_monitor import stage

logger = logging.getLogger(__name__)

ROWS = 2500
MANY_PARTITIONS = 200
FEW_PARTITIONS = 8
HOLD_SECONDS = 60  # keeps the cluster up so the external watcher takes its last snapshot


def _run_probes(lineage: DataFrame) -> None:
    spark = lineage.sparkSession

    with stage("probe_plain_8"):
        spark.range(0, ROWS, 1, FEW_PARTITIONS).selectExpr("sum(id)").first()
    with stage("probe_plain_200"):
        spark.range(0, ROWS, 1, MANY_PARTITIONS).selectExpr("sum(id)").first()

    cached = spark.range(0, ROWS, 1, MANY_PARTITIONS).cache()
    with stage("probe_cache_200_fill"):
        cached.count()
    with stage("probe_cache_200_read"):
        cached.selectExpr("sum(id)").first()
    cached.unpersist()

    with stage("probe_lineage_8_read"):
        lineage.count()

    repartitioned = lineage.repartition(MANY_PARTITIONS).cache()
    with stage("probe_lineage_200_fill"):
        repartitioned.count()
    with stage("probe_lineage_200_read"):
        repartitioned.count()
    repartitioned.unpersist()


def run(lineage: DataFrame, hold_seconds: float = HOLD_SECONDS) -> None:
    """`lineage` is a cached DataFrame with T1's full query plan behind it (gold)."""
    try:
        _run_probes(lineage)
    except Exception as error:  # noqa: BLE001 - T1 already finished: a probe never fails the run
        logger.warning("Spark probes stopped: %s", error)
    time.sleep(hold_seconds)
