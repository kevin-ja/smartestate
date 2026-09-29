terraform {
  required_providers {
    databricks = {
      source = "databricks/databricks"
      # databricks           → nivel cuenta (SP, grupo, asignación al workspace, rule set)
      # databricks.workspace → dentro del workspace (entitlements)
      configuration_aliases = [databricks.workspace]
    }
  }
}
