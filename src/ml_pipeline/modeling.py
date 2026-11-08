"""
T2 · Modeling logic: K-means per sector and GeoIndex, with no I/O (new_arch.md §3.1, plan 0.4).

Who enters each sector's K-means:
    With text      tab_vector ‖ nlp_vector, or nlp_vector alone where the sector has no tab_vector (Land).
                   Only these rows train the model.
    Without text   Nearest centroid measured on tab_vector alone. Zeros in place of the text would make
                   these rows look alike and form an artificial group.

A cluster_id means something only within its sector, which is all the re-ranker compares (Filter 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

from src.common.geo_index import GeoIndex

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

K_VALUES = range(5, 9)
SEED = 0
KMEANS_RESTARTS = 10


def hyperparameters() -> dict[str, int]:
    """What the K-means used, for MLflow: two runs then show why their clusters differ."""
    return {
        "kmeans_k_min": min(K_VALUES),
        "kmeans_k_max": max(K_VALUES),
        "kmeans_seed": SEED,
        "kmeans_n_init": KMEANS_RESTARTS,
    }


class ModelingError(Exception):
    """The candidates cannot be clustered. The run aborts before writing anything."""


@dataclass
class ClusteringResult:
    clusters: pd.DataFrame            # spot_id, cluster_id: one row per candidate
    models: dict[int, KMeans]         # sector_id -> fitted model
    metrics: dict[str, float]         # k and silhouette per sector
    spread: dict[str, dict[str, dict[str, int]]]  # sector -> cluster -> rows with and without text


def fit_kmeans(vectors: np.ndarray, seed: int = SEED) -> tuple[KMeans, float]:
    """Tries each k in K_VALUES and keeps the best silhouette; a tie goes to the smallest k."""
    best_model, best_score = None, -np.inf
    for k in K_VALUES:
        if k >= len(vectors):  # silhouette needs fewer clusters than rows
            break
        model = KMeans(n_clusters=k, n_init=KMEANS_RESTARTS, random_state=seed).fit(vectors)
        score = float(silhouette_score(vectors, model.labels_))
        if score > best_score:
            best_model, best_score = model, score
    if best_model is None:
        raise ModelingError(f"{len(vectors)} rows are too few for k in {list(K_VALUES)}")
    return best_model, best_score


def nearest_by_tab(model: KMeans, tab: np.ndarray) -> np.ndarray:
    """Nearest centroid using only the tab_vector columns, which come first in the training vector."""
    centroids = model.cluster_centers_[:, : tab.shape[1]]
    return ((tab[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2).argmin(axis=1)


def _matrix(column: pd.Series) -> np.ndarray:
    return np.vstack(column.to_numpy()).astype(np.float64)


def _cluster_sector(sector: int, rows: pd.DataFrame, seed: int) -> tuple[np.ndarray, KMeans, float]:
    """cluster_id of each row, in order, plus the model and its silhouette."""
    with_tab = rows["tab_vector"].notna().to_numpy()
    with_text = rows["nlp_vector"].notna().to_numpy()
    uses_tab = with_tab.any()
    if uses_tab and not with_tab.all():
        raise ModelingError(f"Sector {sector}: {(~with_tab).sum()} candidates without tab_vector")
    if not uses_tab and not with_text.all():
        raise ModelingError(f"Sector {sector}: {(~with_text).sum()} candidates have neither tab_vector nor text")
    if not with_text.any():
        raise ModelingError(f"Sector {sector}: no candidate has text to train on")

    text = _matrix(rows.loc[with_text, "nlp_vector"])
    training = np.hstack([_matrix(rows.loc[with_text, "tab_vector"]), text]) if uses_tab else text
    model, score = fit_kmeans(training, seed)

    labels = np.empty(len(rows), dtype=np.int32)
    labels[with_text] = model.labels_
    if not with_text.all():
        labels[~with_text] = nearest_by_tab(model, _matrix(rows.loc[~with_text, "tab_vector"]))
    return labels, model, score


def cluster_candidates(candidates: pd.DataFrame, seed: int = SEED) -> ClusteringResult:
    """One K-means per sector. `candidates`: spot_id, sector_id, tab_vector and nlp_vector, sorted by spot_id."""
    parts, models, metrics, spread = [], {}, {}, {}
    for sector, rows in candidates.groupby("sector_id", sort=True):
        sector = int(sector)
        labels, models[sector], score = _cluster_sector(sector, rows, seed)
        parts.append(pd.DataFrame({"spot_id": rows["spot_id"].to_numpy(), "cluster_id": labels}))
        metrics[f"kmeans_k_sector_{sector}"] = models[sector].n_clusters
        metrics[f"silhouette_sector_{sector}"] = score

        with_text = rows["nlp_vector"].notna().to_numpy()
        spread[str(sector)] = {
            str(cluster): {
                "with_text": int(((labels == cluster) & with_text).sum()),
                "without_text": int(((labels == cluster) & ~with_text).sum()),
            }
            for cluster in range(models[sector].n_clusters)
        }
    clusters = pd.concat(parts).sort_values("spot_id", ignore_index=True)
    return ClusteringResult(clusters, models, metrics, spread)


def build_geo_index(candidates: pd.DataFrame) -> GeoIndex:
    """`candidates`: spot_id, latitude, longitude, sector_id and modality_mask, copied untransformed."""
    return GeoIndex(
        spot_id=candidates["spot_id"].to_numpy(),
        latitude=candidates["latitude"].to_numpy(),
        longitude=candidates["longitude"].to_numpy(),
        sector=candidates["sector_id"].to_numpy(),
        modality_mask=candidates["modality_mask"].to_numpy(),
    )


def with_cluster_id(fs: DataFrame, clusters: DataFrame) -> DataFrame:
    """Same columns and order as `fs`; cluster_id comes from `clusters`, null for anyone not in it."""
    assigned = clusters.select("spot_id", clusters["cluster_id"].cast("int").alias("cluster_id"))
    return fs.drop("cluster_id").join(assigned, "spot_id", "left").select(*fs.columns)
