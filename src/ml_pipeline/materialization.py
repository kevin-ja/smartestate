"""
T3 · Materialization logic, with no I/O (new_arch.md §3.4): which runs to delete and the smoke test's checks.

Cleanup rule (P31, Kevin 2026-11-04; N from config/retention.yml since 2026-11-08), the same in S3, DynamoDB
and the Spots FS offline clones:
    keep the last N runs + the latest champion + the run in production. Older champions are deleted like any other run.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

ONLINE_TABLE_PREFIX = "spots_fs_online_"
ONLINE_KEY_ATTRIBUTE = "_feature_store_internal__primary_keys"  # set by publish_table
PRODUCTION_FILE = "production.json"  # under runs/: what the CD deployed last (inference_pipeline_design.md)
SNAPSHOT_SUFFIX = "_"  # spots_fs_offline -> spots_fs_offline_<run_id>, in the same schema
NLP_DIM = 768
FLOAT_TOLERANCE = 1e-5


def online_table_name(run_id: str) -> str:
    return f"{ONLINE_TABLE_PREFIX}{run_id}"


def snapshot_table_name(fs_table: str, run_id: str) -> str:
    """Frozen copy of Spots FS offline for one run: same catalog and schema, `_<run_id>` appended."""
    return f"{fs_table}{SNAPSHOT_SUFFIX}{run_id}"


def keep_runs(retention: Mapping[str, Any]) -> int:
    """keep_runs of config/retention.yml. Aborts on anything but a positive integer: cleanup deletes."""
    keep = retention.get("keep_runs")
    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
        raise ValueError(f"retention.yml: keep_runs must be a positive integer, got {keep!r}")
    return keep


def production_run_id(pointer: str) -> str:
    """run_id of production.json. Aborts if it cannot read it: the cleanup could delete what is in production."""
    run_id = str(json.loads(pointer)["run_id"])
    if not run_id.isdigit():
        raise ValueError(f"{PRODUCTION_FILE}: run_id is not a number: {run_id!r}")
    return run_id


def to_drop(created: Mapping[str, float], prefix: str, keep: int, protect: Iterable[str] = ()) -> list[str]:
    """
    Names to delete, oldest first. `created`: name -> when it was written, in seconds.

    Runs are ordered by that date, never by run_id: Databricks run_ids do not grow over time (run
    669016108687066 deleted two newer runs that way). A name counts as a run only if it is `prefix` + a
    number; anything else is ignored. Those in `protect` are never deleted.
    """
    runs = sorted(
        (when, name)
        for name, when in created.items()
        if name.startswith(prefix) and name[len(prefix):].isdigit()
    )
    protected = set(protect)
    old = runs[:-keep] if keep else runs
    return [name for _, name in old if name[len(prefix):] not in protected]


def online_key(spot_id: int) -> dict[str, str]:
    """DynamoDB key as publish_table writes it: the primary key values as JSON (new_arch.md §3.4)."""
    return {ONLINE_KEY_ATTRIBUTE: json.dumps([int(spot_id)])}


def check_item(item: Mapping[str, Any] | None, expected: Mapping[str, Any]) -> list[str]:
    """Compares a DynamoDB item with its offline row. `expected`: spot_id, cluster_id and nlp_vector."""
    if item is None:
        return [f"spot {expected['spot_id']}: not found online"]

    errors = []
    # publish_table keeps the primary key only in the combined key: there is no spot_id attribute.
    key = online_key(expected["spot_id"])[ONLINE_KEY_ATTRIBUTE]
    if item.get(ONLINE_KEY_ATTRIBUTE) != key:
        errors.append(f"{ONLINE_KEY_ATTRIBUTE}: online {item.get(ONLINE_KEY_ATTRIBUTE)}, offline {key}")
    if "cluster_id" not in item or int(item["cluster_id"]) != expected["cluster_id"]:
        errors.append(f"cluster_id: online {item.get('cluster_id')}, offline {expected['cluster_id']}")

    try:  # a native DynamoDB list of numbers, which boto3 returns as Decimal
        vector = np.asarray(item["nlp_vector"], dtype=np.float64)
    except (KeyError, TypeError, ValueError) as error:
        return [*errors, f"nlp_vector does not parse: {error!r}"]
    if vector.shape != (NLP_DIM,):
        errors.append(f"nlp_vector: shape {vector.shape}, expected ({NLP_DIM},)")
    elif not np.allclose(vector, np.asarray(expected["nlp_vector"], dtype=np.float64), atol=FLOAT_TOLERANCE):
        errors.append("nlp_vector: differs from offline")
    return errors
