terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    # Solo para los data sources que generan las políticas exactas que exige Databricks.
    databricks = {
      source = "databricks/databricks"
    }
  }
}
