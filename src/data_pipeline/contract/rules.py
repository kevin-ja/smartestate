"""
Validation rules for bronze (src/data_pipeline/README.md, section 5) and gold (section 3).

Rules are data: a new rule is a new entry here, not new code in ingestion.py or preprocessing.py.
Conditions are Spark SQL expressions over the layer's column names.
"""

from __future__ import annotations

import json
from pathlib import Path

# Same box as src/analysis/eda.py. Copied, not imported: data_pipeline does not depend on analysis.
BBOX_MEXICO = {"lat_min": 14.0, "lat_max": 33.0, "lon_min": -118.5, "lon_max": -86.0}

# Observed codes. 13 is mapped to 12 (Retail) in silver, not here.
KNOWN_SECTORS = (9, 11, 13, 15)

# Abort: the batch is wrong.
KEY_COLUMN = "spot_id"

# Quarantine: one row is wrong, the run goes on. A row may match several reasons.
# A missing coordinate cannot be placed inside the box, so it counts as out_of_bbox.
QUARANTINE_RULES = {
    "out_of_bbox": (
        "NOT coalesce("
        f"latitude BETWEEN {BBOX_MEXICO['lat_min']} AND {BBOX_MEXICO['lat_max']} "
        f"AND longitude BETWEEN {BBOX_MEXICO['lon_min']} AND {BBOX_MEXICO['lon_max']}, false)"
    ),
    "unknown_sector": (
        f"sector_id IS NULL OR sector_id NOT IN ({', '.join(map(str, KNOWN_SECTORS))})"
    ),
}

# Warnings: something is off, but it does not block (v1).
JOIN_MIN_MATCH = 0.99
NULL_RATE_TOLERANCE = 0.15  # D8: +15 pp over the baseline, to be calibrated

_BASELINE_PATH = Path(__file__).with_name("null_baseline.json")


def load_null_baseline(path: Path = _BASELINE_PATH) -> dict[int, dict[str, float]]:
    """D8 baseline: expected null rate per sector and column. Only columns that apply to the sector."""
    raw = json.loads(path.read_text())
    return {int(sector): rates for sector, rates in raw.items()}


# Gold domains: column -> condition a non-null value must meet. A value that fails becomes null
# and is counted in a warning; the row stays (preprocessing, step 4).
GOLD_DOMAINS = {
    "building_status": "building_status IN (1, 2, 3)",
    "natural_light": "natural_light BETWEEN 1 AND 100",  # percentage, metadatos.pdf
}
