terraform {
  required_providers {
    databricks = {
      source = "databricks/databricks"
      # databricks           → nivel cuenta (usuarios, grupos, asignación al workspace)
      # databricks.workspace → dentro del workspace (entitlements)
      configuration_aliases = [databricks.workspace]
    }
  }
}
