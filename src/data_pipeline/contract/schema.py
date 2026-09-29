"""
Schema contract for the raw CSVs and the bronze, silver and gold tables.

Two parts per CSV:
  - RAW_*_COLUMNS: the header exactly as it arrives, in order. A different header aborts the run.
  - *_FIELDS: the columns that survive into bronze, with final name, type and parsing format.

Formats: "number" (strip thousands separator), "coordinate" (hemisphere to sign),
"date" (MMMM d, yyyy), "text" (as is). See src/data_pipeline/README.md, section 3.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DataType,
    DateType,
    DoubleType,
    IntegerType,
    LongType,
    StringType,
    StructField,
    StructType,
)


@dataclass(frozen=True)
class Field:
    source: str       # column name in the CSV
    name: str         # column name in bronze
    dtype: DataType
    format: str       # number | coordinate | date | text
    nullable: bool = True


RAW_SPOTS_COLUMNS = [
    "Spot ID", "Spot Sector ID", "Spot Type ID", "Spot Settlement", "Spot Municipality",
    "Spot State", "Spot Region", "Spot Corridor", "Spot Address", "Spot Title",
    "Spot Description", "Spot Latitude", "Spot Longitude", "Spot Area In Sqm",
    "Spot Price Sqm Mxn Rent", "Spot Price Total Mxn Rent", "Spot Price Sqm Mxn Sale",
    "Spot Price Total Mxn Sale", "Spot Maintenance Cost Mxn ($)", "Spot Modality", "User ID",
    "Spot Created Date",
]

RAW_ATTRIBUTES_COLUMNS = [
    "spot_id", "natural_light", "luminaries", "charging_ports", "security_type",
    "floor_level_number", "number_of_elevators", "vertical_height", "parking_spaces",
    "building_status", "floor_material",
]

# 18 of 22. Dropped: Settlement, Region, Corridor, Address (EDA.md, Appendix).
SPOTS_FIELDS = [
    Field("Spot ID", "spot_id", LongType(), "number", nullable=False),
    Field("Spot Sector ID", "sector_id", IntegerType(), "number"),
    Field("Spot Type ID", "type_id", IntegerType(), "number"),
    Field("Spot Modality", "modality", StringType(), "text"),
    Field("Spot Latitude", "latitude", DoubleType(), "coordinate"),
    Field("Spot Longitude", "longitude", DoubleType(), "coordinate"),
    Field("Spot Municipality", "municipality", StringType(), "text"),
    Field("Spot State", "state", StringType(), "text"),
    Field("Spot Title", "title", StringType(), "text"),
    Field("Spot Description", "description", StringType(), "text"),
    Field("Spot Area In Sqm", "area_sqm", DoubleType(), "number"),
    Field("Spot Price Sqm Mxn Rent", "rent_price_sqm", DoubleType(), "number"),
    Field("Spot Price Sqm Mxn Sale", "sale_price_sqm", DoubleType(), "number"),
    Field("Spot Price Total Mxn Rent", "rent_price_total", DoubleType(), "number"),
    Field("Spot Price Total Mxn Sale", "sale_price_total", DoubleType(), "number"),
    Field("Spot Maintenance Cost Mxn ($)", "maintenance_cost", DoubleType(), "number"),
    Field("User ID", "user_id", LongType(), "number"),
    Field("Spot Created Date", "created_date", DateType(), "date"),
]

# 7 of 10, plus the key. Dropped: luminaries, floor_level_number, vertical_height.
ATTRIBUTES_FIELDS = [
    Field("spot_id", "spot_id", LongType(), "number", nullable=False),
    Field("security_type", "security_type", StringType(), "text"),
    Field("floor_material", "floor_material", StringType(), "text"),
    Field("charging_ports", "charging_ports", IntegerType(), "number"),
    Field("number_of_elevators", "number_of_elevators", IntegerType(), "number"),
    Field("parking_spaces", "parking_spaces", IntegerType(), "number"),
    Field("building_status", "building_status", IntegerType(), "number"),
    Field("natural_light", "natural_light", IntegerType(), "number"),
]

# Bronze: 25 columns, spot_id included (18 + 7).
BRONZE_SCHEMA = StructType(
    [StructField(f.name, f.dtype, f.nullable) for f in SPOTS_FIELDS]
    + [StructField(f.name, f.dtype, f.nullable) for f in ATTRIBUTES_FIELDS if f.name != "spot_id"]
)

# Silver: bronze as is, plus the cleaning flags (30 columns). Order is the order cleaning adds them.
SILVER_SCHEMA = StructType(
    list(BRONZE_SCHEMA.fields)
    + [
        StructField("sector_id_original", IntegerType()),
        StructField("is_candidate", BooleanType()),
        StructField("state_mismatch", BooleanType()),
        StructField("is_price_outlier", BooleanType()),
        StructField("is_area_outlier", BooleanType()),
    ]
)

# Gold: silver with security_type parsed to a list, plus the text for SBERT (32 columns).
GOLD_SCHEMA = StructType(
    [
        StructField("security_type", ArrayType(IntegerType())) if f.name == "security_type" else f
        for f in SILVER_SCHEMA.fields
    ]
    + [
        StructField("text", StringType()),
        StructField("has_description", BooleanType()),
    ]
)
