"""
T2 · Modeling logic: K-means per sector and GeoIndex, with no I/O (new_arch.md §3.1, plan 0.4).

Who enters each sector's K-means:
    With text      tab_vector ‖ PCA(nlp_vector), or PCA(nlp_vector) alone where the sector has no tab_vector
                   (Land). Only these rows train the model. The PCA is fitted per sector on these rows and
                   its output is brought back to length 1.
    Without text   Nearest centroid measured on tab_vector alone. Zeros in place of the text would make
                   these rows look alike and form an artificial group.

Search per sector: PCA variance (PCA_VARIANCES) × top components dropped (PCA_DROP_TOP) × k (K_VALUES).
Variance 1.0 keeps every component; with nothing dropped, the text goes in as it is (no PCA). Dropping
the top components removes the few directions SBERT embeddings share, which tell texts apart little
("all-but-the-top"). The silhouette is always measured in the original space (tab_vector ‖ nlp_vector):
measured in each PCA space, the lowest variance would win only because it has fewer dimensions.

A cluster_id means something only within its sector, which is all the re-ranker compares (Filter 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import pairwise_distances, silhouette_score

from src.common.geo_index import GeoIndex

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

K_VALUES = range(4, 13)
SEED = 0
KMEANS_RESTARTS = 10
PCA_VARIANCES = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0)  # share of the text variance the PCA keeps
PCA_DROP_TOP = (0, 1, 2, 3)  # top principal components removed from the text


def hyperparameters() -> dict[str, int | str]:
    """What the K-means used, for MLflow: two runs then show why their clusters differ."""
    return {
        "kmeans_k_min": min(K_VALUES),
        "kmeans_k_max": max(K_VALUES),
        "kmeans_seed": SEED,
        "kmeans_n_init": KMEANS_RESTARTS,
        "pca_variances": ",".join(map(str, PCA_VARIANCES)),
        "pca_drop_top": ",".join(map(str, PCA_DROP_TOP)),
    }


class ModelingError(Exception):
    """The candidates cannot be clustered. The run aborts before writing anything."""


@dataclass
class TextReduction:
    variance: float       # share of the text variance kept
    drop_top: int         # top components removed
    pca: PCA | None       # None: the text went in as it is
    components: int       # text columns that reached the K-means


@dataclass
class ClusteringResult:
    clusters: pd.DataFrame            # spot_id, cluster_id: one row per candidate
    models: dict[int, KMeans]         # sector_id -> fitted model
    reductions: dict[int, TextReduction]  # sector_id -> how the sector's text was reduced
    metrics: dict[str, float]         # chosen k, text reduction and silhouette per sector
    spread: dict[str, dict[str, dict[str, int]]]  # sector -> cluster -> rows with and without text


def fit_kmeans(vectors: np.ndarray, seed: int = SEED, distances: np.ndarray | None = None) -> tuple[KMeans, float]:
    """
    Tries each k in K_VALUES and keeps the best silhouette; a tie goes to the smallest k. `distances`:
    the space the silhouette is measured in (pairwise matrix); without it, the training vectors.
    """
    best_model, best_score = None, -np.inf
    for k in K_VALUES:
        if k >= len(vectors):  # silhouette needs fewer clusters than rows
            break
        model = KMeans(n_clusters=k, n_init=KMEANS_RESTARTS, random_state=seed).fit(vectors)
        if len(set(model.labels_)) < 2:  # silhouette needs at least two clusters
            continue
        score = float(
            silhouette_score(distances, model.labels_, metric="precomputed") if distances is not None
            else silhouette_score(vectors, model.labels_)
        )
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


def reduce_text(text: np.ndarray, variance: float, drop_top: int = 0) -> tuple[np.ndarray, TextReduction] | None:
    """
    PCA keeping `variance` of the variance, without its first `drop_top` components, then each row back
    to length 1. Variance 1.0 with nothing dropped returns the text as it is. None: nothing would be left.
    """
    if variance >= 1.0 and drop_top == 0:
        return text, TextReduction(variance, 0, None, text.shape[1])
    pca = PCA(n_components=variance if variance < 1.0 else None, svd_solver="full").fit(text)
    if pca.n_components_ <= drop_top:
        return None
    reduced = pca.transform(text)[:, drop_top:]
    norms = np.linalg.norm(reduced, axis=1, keepdims=True)
    return reduced / np.where(norms > 0, norms, 1.0), TextReduction(variance, drop_top, pca, reduced.shape[1])


def _cluster_sector(sector: int, rows: pd.DataFrame, seed: int) -> tuple[np.ndarray, KMeans, TextReduction, float]:
    """cluster_id of each row, in order, plus the model, the text reduction and the silhouette."""
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
    tab = _matrix(rows.loc[with_text, "tab_vector"]) if uses_tab else None
    original = np.hstack([tab, text]) if uses_tab else text
    distances = pairwise_distances(original)  # computed once, reused by every (variance, drop, k)

    model, reduction, score = None, None, -np.inf
    for variance in PCA_VARIANCES:  # a tie keeps the lower variance, then the fewer dropped
        for drop_top in PCA_DROP_TOP:
            reduced = reduce_text(text, variance, drop_top)
            if reduced is None:
                continue
            training = np.hstack([tab, reduced[0]]) if uses_tab else reduced[0]
            candidate, candidate_score = fit_kmeans(training, seed, distances)
            if candidate_score > score:
                model, reduction, score = candidate, reduced[1], candidate_score

    labels = np.empty(len(rows), dtype=np.int32)
    labels[with_text] = model.labels_
    if not with_text.all():
        labels[~with_text] = nearest_by_tab(model, _matrix(rows.loc[~with_text, "tab_vector"]))
    return labels, model, reduction, score


def cluster_candidates(candidates: pd.DataFrame, seed: int = SEED) -> ClusteringResult:
    """One K-means per sector. `candidates`: spot_id, sector_id, tab_vector and nlp_vector, sorted by spot_id."""
    parts, models, reductions, metrics, spread = [], {}, {}, {}, {}
    for sector, rows in candidates.groupby("sector_id", sort=True):
        sector = int(sector)
        labels, models[sector], reductions[sector], score = _cluster_sector(sector, rows, seed)
        parts.append(pd.DataFrame({"spot_id": rows["spot_id"].to_numpy(), "cluster_id": labels}))
        metrics[f"kmeans_k_sector_{sector}"] = models[sector].n_clusters
        metrics[f"silhouette_sector_{sector}"] = score
        metrics[f"pca_variance_sector_{sector}"] = reductions[sector].variance
        metrics[f"pca_drop_top_sector_{sector}"] = reductions[sector].drop_top
        metrics[f"pca_components_sector_{sector}"] = reductions[sector].components

        with_text = rows["nlp_vector"].notna().to_numpy()
        spread[str(sector)] = {
            str(cluster): {
                "with_text": int(((labels == cluster) & with_text).sum()),
                "without_text": int(((labels == cluster) & ~with_text).sum()),
            }
            for cluster in range(models[sector].n_clusters)
        }
    clusters = pd.concat(parts).sort_values("spot_id", ignore_index=True)
    return ClusteringResult(clusters, models, reductions, metrics, spread)


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
