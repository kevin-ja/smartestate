"""
Recommendation cascade of one click, with no I/O (new_arch.md §4.1):

    clicked spot -> Filters 1 + 2 (GeoIndex, widening radius) -> 3A similarity -> 3B price -> 3C diversity -> top N
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import numpy as np

# Approved by Kevin on 2026-11-05, see inference_pipeline_design.md. Start values, to calibrate.
RADII_KM = (5.0, 10.0, 15.0)
MIN_CANDIDATES = 50
MAX_CANDIDATES = 300
DEFAULT_N = 10
MAX_N = 50

# f(r) reaches its upper bound at r = 0.5 and its lower bound at r = 2, and stays flat beyond them.
FULL_BONUS_RATIO = 0.5
FULL_PENALTY_RATIO = 2.0

PRICE_BY_MODALITY_BIT = {1: "rent_price_sqm", 2: "sale_price_sqm"}
CLICK_FILTER_COLUMNS = ("latitude", "longitude", "sector_id", "modality_mask")

Item = Mapping[str, Any]


class SpotIndex(Protocol):
    def query(self, latitude: float, longitude: float, radius_km: float, sector: int, modality_mask: int) -> list[int]: ...


class FeatureStore(Protocol):
    def get_clicked(self, spot_id: int) -> Item | None: ...

    def get_candidates(self, spot_ids: Sequence[int]) -> list[Item]: ...


def find_candidates(index: SpotIndex, spot_id: int, clicked: Item) -> list[int]:
    """Filters 1 + 2: widens the radius until enough candidates, then keeps the nearest. Never includes the click."""
    found: list[int] = []
    for radius_km in RADII_KM:
        near = index.query(
            clicked["latitude"], clicked["longitude"], radius_km, int(clicked["sector_id"]), int(clicked["modality_mask"])
        )
        found = [candidate for candidate in near if candidate != spot_id]
        if len(found) >= MIN_CANDIDATES:
            break
    return found[:MAX_CANDIDATES]


def similarity(clicked: Item, candidates: Sequence[Item], w_tab: float, w_nlp: float) -> np.ndarray:
    """3A: weighted cosine over the signals both spots have. Without a shared signal, sim is 0."""
    weighted = np.zeros(len(candidates))
    weights = np.zeros(len(candidates))
    for column, weight in (("tab_vector", w_tab), ("nlp_vector", w_nlp)):
        reference = clicked.get(column)
        rows = [row for row, candidate in enumerate(candidates) if candidate.get(column)]
        if not reference or not rows:
            continue
        # Vectors have length 1, so the dot product is the cosine.
        matrix = np.array([candidates[row][column] for row in rows], dtype=float)
        weighted[rows] += weight * (matrix @ np.array(reference, dtype=float))
        weights[rows] += weight
    sim = np.divide(weighted, weights, out=np.zeros_like(weighted), where=weights > 0)
    # sim is documented in [0, 1]. A negative one would rise when f(r) or delta shrink it.
    return np.clip(sim, 0.0, 1.0)


def price_factor(ratio: float, f_bounds: Sequence[float]) -> float:
    """3B: f(r) in stretches. Bonus for cheaper, penalty for dearer, flat past the bounds."""
    low, high = f_bounds
    return float(np.interp(ratio, [FULL_BONUS_RATIO, 1.0, FULL_PENALTY_RATIO], [high, 1.0, low]))


def price_factors(clicked: Item, candidates: Sequence[Item], f_bounds: Sequence[float]) -> np.ndarray:
    """3B: mean f(r) over the shared modalities where both spots have a price. Neutral if there is none (P34)."""
    clicked_mask = int(clicked["modality_mask"])
    factors = np.ones(len(candidates))
    for row, candidate in enumerate(candidates):
        shared = clicked_mask & int(candidate.get("modality_mask") or 0)
        values = []
        for bit, column in PRICE_BY_MODALITY_BIT.items():
            reference, price = clicked.get(column), candidate.get(column)
            if shared & bit and reference and price:
                values.append(price_factor(price / reference, f_bounds))
        if values:
            factors[row] = sum(values) / len(values)
    return factors


def diversify(spot_ids: Sequence[int], scores: np.ndarray, clusters: Sequence[Any], delta: float, n: int) -> list[int]:
    """3C: greedy top N with score * delta^(spots of that cluster already in). Ties go to the lowest spot_id."""
    remaining = np.lexsort((np.asarray(spot_ids), -scores)).tolist()
    in_top: Counter = Counter()
    top: list[int] = []

    def adjusted(row: int) -> float:
        cluster = clusters[row]
        return scores[row] * delta ** (0 if cluster is None else in_top[cluster])

    while remaining and len(top) < n:
        best = max(remaining, key=adjusted)
        remaining.remove(best)
        top.append(spot_ids[best])
        if clusters[best] is not None:
            in_top[clusters[best]] += 1
    return top


def rerank(clicked: Item, candidates: Sequence[Item], params: Mapping[str, Any], n: int) -> list[int]:
    """Level 3: 3A -> 3B -> 3C."""
    scores = similarity(clicked, candidates, params["w_tab"], params["w_nlp"]) * price_factors(
        clicked, candidates, params["f_bounds"]
    )
    spot_ids = [candidate["spot_id"] for candidate in candidates]
    clusters = [candidate.get("cluster_id") for candidate in candidates]
    return diversify(spot_ids, scores, clusters, params["delta"], n)


def recommend(spot_id: int, n: int, index: SpotIndex, store: FeatureStore, params: Mapping[str, Any]) -> list[int] | None:
    """Top N spot_ids. None if the click is not in the store; empty if nothing passes the filters."""
    clicked = store.get_clicked(spot_id)
    if clicked is None:
        return None
    if any(clicked.get(column) is None for column in CLICK_FILTER_COLUMNS):
        return []
    candidate_ids = find_candidates(index, spot_id, clicked)
    if not candidate_ids:
        return []
    return rerank(clicked, store.get_candidates(candidate_ids), params, n)
