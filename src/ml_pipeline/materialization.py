"""
T3 · Materialization logic, with no I/O (new_arch.md §3.4): which runs to delete and the smoke test's checks.

Cleanup rule (P31, Kevin 2026-11-04), the same in DynamoDB and in S3:
    keep the last 3 runs + the latest champion. Older champions are deleted like any other run.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

ONLINE_TABLE_PREFIX = "spots_fs_online_"
ONLINE_KEY_ATTRIBUTE = "_feature_store_internal__primary_keys"  # set by publish_table
KEEP_RUNS = 3
NLP_DIM = 768
FLOAT_TOLERANCE = 1e-5


def online_table_name(run_id: str) -> str:
    return f"{ONLINE_TABLE_PREFIX}{run_id}"


def to_drop(created: Mapping[str, float], prefix: str, keep: int = KEEP_RUNS, protect: Iterable[str] = ()) -> list[str]:
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
