output "name" {
  value = databricks_storage_credential.this.name
}

output "assume_role_policy_json" {
  description = "Trust policy que el rol debe tener para que Unity Catalog lo asuma."
  value       = data.databricks_aws_unity_catalog_assume_role_policy.this.json
}
