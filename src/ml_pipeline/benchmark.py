"""
T2 · Scores of a run against the specialists' benchmark, with no I/O (monitor.md, "Cómo se mide").

Runs the same cascade the API runs (src/inference/recommender.py) on every clicked spot of benchmark.csv, with
the Spots FS offline rows as the feature store, and keeps Recall@10 and NDCG@10 per spot. Only a record of the
run's quality: promotion stays with the silhouette (evaluation.py). The weekly Lambda measures production the
same way from the API's log, so for one run_id and spot_id both sides must agree.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from src.common import ranking_metrics
from src.inference import recommender

SCORES_FILE = "benchmark_scores.csv"  # under runs/<run_id>/
SCORES_COLUMNS = ("run_id", "spot_id", "recall_at_10", "ndcg_at_10")
STORE_COLUMNS = (
    "spot_id", "sector_id", "modality_mask", "latitude", "longitude", "tab_vector", "nlp_vector",
    "rent_price_sqm", "sale_price_sqm",
)


def _plain(value: Any) -> Any:
    """What the API gets from DynamoDB: lists for vectors, None for nulls, Python numbers otherwise."""
    if value is None:
        return None
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return value


class OfflineStore:
    """recommender.FeatureStore over Spots FS offline rows (STORE_COLUMNS) and the run's cluster_id."""

    def __init__(self, spots: pd.DataFrame, clusters: pd.DataFrame) -> None:
        rows = spots.merge(clusters, on="spot_id", how="left")
        self.items: dict[int, dict[str, Any]] = {}
        for record in rows.to_dict("records"):
            item = {column: _plain(value) for column, value in record.items()}
            item["spot_id"] = int(item["spot_id"])
            item["cluster_id"] = None if item["cluster_id"] is None else int(item["cluster_id"])
            self.items[item["spot_id"]] = item

    def get_clicked(self, spot_id: int) -> dict[str, Any] | None:
        return self.items.get(spot_id)

    def get_candidates(self, spot_ids: Sequence[int]) -> list[dict[str, Any]]:
        return [self.items[spot_id] for spot_id in spot_ids if spot_id in self.items]


def score_run(
    run_id: str,
    benchmark: Mapping[int, Mapping[int, int]],
    index: recommender.SpotIndex,
    store: recommender.FeatureStore,
    params: Mapping[str, Any],
) -> pd.DataFrame:
    """One row per benchmark spot the cascade can answer (SCORES_COLUMNS). Unknown or unscorable spots are left out."""
    rows = []
    for spot_id in sorted(benchmark):
        ranked = recommender.recommend(spot_id, ranking_metrics.K, index, store, params)
        if ranked is None:
            continue
        scores = ranking_metrics.score(ranked, benchmark[spot_id])
        if scores is not None:
            rows.append((run_id, spot_id, scores.recall_at_k, scores.ndcg_at_k))
    return pd.DataFrame(rows, columns=SCORES_COLUMNS)


def summary(scores: pd.DataFrame, benchmark_spots: int) -> dict[str, float]:
    """What goes to MLflow: the means and how much of the benchmark was scored."""
    return {
        "benchmark_recall_at_10": float(scores["recall_at_10"].mean()) if len(scores) else 0.0,
        "benchmark_ndcg_at_10": float(scores["ndcg_at_10"].mean()) if len(scores) else 0.0,
        "benchmark_spots_scored": float(len(scores)),
        "benchmark_spots": float(benchmark_spots),
    }
