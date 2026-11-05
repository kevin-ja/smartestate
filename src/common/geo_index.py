"""
GeoIndex: Filters 1 (zone) and 2 (sector and modality) of the recommender, in RAM (new_arch.md §4.1).

Built by T2 and loaded by the service with joblib, so this module must import without Spark.
"""

from __future__ import annotations

import numpy as np
from sklearn.neighbors import BallTree

EARTH_RADIUS_KM = 6371.0088


class GeoIndex:
    """Candidates' coordinates in a haversine BallTree, next to their spot_id, sector and modality."""

    def __init__(
        self,
        spot_id: np.ndarray,
        latitude: np.ndarray,
        longitude: np.ndarray,
        sector: np.ndarray,
        modality_mask: np.ndarray,
    ) -> None:
        self.tree = BallTree(np.radians(np.column_stack([latitude, longitude])), metric="haversine")
        self.spot_id = np.asarray(spot_id, dtype=np.int64)
        self.sector = np.asarray(sector, dtype=np.int8)
        self.modality_mask = np.asarray(modality_mask, dtype=np.uint8)

    def query(self, latitude: float, longitude: float, radius_km: float, sector: int, modality_mask: int) -> list[int]:
        """spot_ids within the radius, of the same sector and sharing a modality. May include the click."""
        point = np.radians([[latitude, longitude]])
        near = self.tree.query_radius(point, r=radius_km / EARTH_RADIUS_KM)[0]
        keep = (self.sector[near] == sector) & ((self.modality_mask[near] & modality_mask) != 0)
        return self.spot_id[near[keep]].tolist()
