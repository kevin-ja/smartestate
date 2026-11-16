"""
Recall@K and NDCG@K of one ranked list against the specialists' benchmark (monitor.md, "Cómo se mide").

Pure Python on purpose: the training job, the weekly Lambda and the verification script all import it, and the
Lambda runs with nothing but the standard library.

    ranked     spot_ids the cascade returned, best first (the API's `spot_ids`, or the offline cascade's)
    relevance  {recommended_spot_id: grade} for the clicked spot, from benchmark.csv

NDCG uses the exponential gain 2^grade - 1. Recall counts grades >= RELEVANT_THRESHOLD.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

K = 10
RELEVANT_THRESHOLD = 1
BENCHMARK_COLUMNS = ("spot_id", "recommended_spot_id", "relevance")


class Scores(NamedTuple):
    recall_at_k: float
    ndcg_at_k: float


def gain(grade: int | float) -> float:
    return 2.0 ** grade - 1.0


def recall_at_k(ranked: Sequence[int], relevance: Mapping[int, int], k: int = K, threshold: int = RELEVANT_THRESHOLD) -> float | None:
    """Share of the relevant spots found in the top k. None when the specialist marked nothing relevant."""
    relevant = {spot_id for spot_id, grade in relevance.items() if grade >= threshold}
    if not relevant:
        return None
    return len(relevant.intersection(ranked[:k])) / len(relevant)


def ndcg_at_k(ranked: Sequence[int], relevance: Mapping[int, int], k: int = K) -> float:
    """DCG of the top k over the ideal DCG (the specialist's grades sorted, cut at k). 0 when the ideal is 0."""
    dcg = sum(gain(relevance.get(spot_id, 0)) / math.log2(position + 2) for position, spot_id in enumerate(ranked[:k]))
    ideal = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum(gain(grade) / math.log2(position + 2) for position, grade in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def score(ranked: Sequence[int], relevance: Mapping[int, int], k: int = K, threshold: int = RELEVANT_THRESHOLD) -> Scores | None:
    """Both metrics of one click. None when the click cannot be scored (no relevant spot in the benchmark)."""
    recall = recall_at_k(ranked, relevance, k, threshold)
    if recall is None:
        return None
    return Scores(recall, ndcg_at_k(ranked, relevance, k))


def read_benchmark(rows: Iterable[Mapping[str, str]]) -> dict[int, dict[int, int]]:
    """benchmark.csv rows -> {spot_id: {recommended_spot_id: grade}}. Aborts on a bad row: a silent skip hides errors."""
    benchmark: dict[int, dict[int, int]] = {}
    for row in rows:
        try:
            spot_id, recommended, grade = (int(row[column]) for column in BENCHMARK_COLUMNS)
        except (KeyError, ValueError) as error:
            raise ValueError(f"benchmark.csv: bad row {dict(row)}: {error}") from None
        benchmark.setdefault(spot_id, {})[recommended] = grade
    return benchmark


def read_benchmark_csv(text: str) -> dict[int, dict[int, int]]:
    """The whole benchmark.csv as text (it is small) -> read_benchmark."""
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None or tuple(reader.fieldnames) != BENCHMARK_COLUMNS:
        raise ValueError(f"benchmark.csv: header {reader.fieldnames} is not {BENCHMARK_COLUMNS}")
    return read_benchmark(reader)
