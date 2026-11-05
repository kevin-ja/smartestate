"""I/O of DynamoDB. Credentials come from the cluster's instance profile; AWS and Feature Engineering load only here."""

from __future__ import annotations

from typing import Any


def _client(region: str):
    import boto3

    return boto3.client("dynamodb", region_name=region)


def publish(source_table: str, online_table: str, region: str) -> None:
    """publish_table creates the DynamoDB table if needed. Merge: a repair run of the same run_id is idempotent."""
    from databricks.feature_engineering import FeatureEngineeringClient
    from databricks.ml_features.online_store_spec import AmazonDynamoDBSpec

    FeatureEngineeringClient().publish_table(
        source_table_name=source_table,
        online_store=AmazonDynamoDBSpec(region=region, table_name=online_table),
        mode="merge",
    )


def get_item(table: str, key: dict[str, str], region: str) -> dict[str, Any] | None:
    """Reads the way the service does, by the combined key. Consistent, to see what was just published."""
    import boto3

    return boto3.resource("dynamodb", region_name=region).Table(table).get_item(Key=key, ConsistentRead=True).get("Item")


def list_tables(prefix: str, region: str) -> dict[str, float]:
    """Table name -> creation time in seconds."""
    client = _client(region)
    pages = client.get_paginator("list_tables").paginate()
    names = [name for page in pages for name in page["TableNames"] if name.startswith(prefix)]
    return {name: client.describe_table(TableName=name)["Table"]["CreationDateTime"].timestamp() for name in names}


def delete_table(table: str, region: str) -> None:
    _client(region).delete_table(TableName=table)
