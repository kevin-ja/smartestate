"""
Model evaluation and champion rule, with no I/O (train_pipeline_mlops.md, criteria 6 and 7).

    T2: new run's silhouette vs the champion's, both on the same candidates -> metrics.json (wins or not)
    T3: if it wins and its table passed the smoke test -> runs/champion.json = this run

Score: silhouette per sector, averaged with each sector's candidates as weight. The new run wins when it
beats the champion by more than PROMOTION_MARGIN, or when there is no champion to compare with. Losing is
not an error: its metrics stay and the champion does not change. Production is always a manual step.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PROMOTION_MARGIN = 0.01  # ~3x the noise seen between configurations (0.003), ~4 % of the score
METRICS_FILE = "metrics.json"  # under runs/<run_id>/


def weighted_silhouette(scores: Mapping[int, float], candidates: Mapping[int, int]) -> float:
    """Silhouette per sector averaged with its candidates as weight."""
    total = sum(candidates[sector] for sector in scores)
    if total <= 0:
        raise ValueError("No candidates to weight the silhouette with")
    return sum(scores[sector] * candidates[sector] for sector in scores) / total


def wins(new: float, champion: float | None, margin: float = PROMOTION_MARGIN) -> bool:
    """True when there is no champion score or the new one beats it by more than `margin`."""
    return champion is None or new > champion + margin


def report(
    run_id: str,
    scores: Mapping[int, float],
    candidates: Mapping[int, int],
    champion_run_id: str | None,
    champion_scores: Mapping[int, float] | None,
) -> dict[str, Any]:
    """Content of metrics.json: both scores on the same candidates and whether the new run wins."""
    new = weighted_silhouette(scores, candidates)
    champion = weighted_silhouette(champion_scores, candidates) if champion_scores is not None else None
    return {
        "run_id": run_id,
        "silhouette_weighted": new,
        "sectors": {
            str(sector): {"silhouette": scores[sector], "candidates": candidates[sector]} for sector in sorted(scores)
        },
        "champion": {"run_id": champion_run_id, "silhouette_weighted": champion} if champion_run_id else None,
        "margin": PROMOTION_MARGIN,
        "wins": wins(new, champion),
    }
