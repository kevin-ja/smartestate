output "catalog" {
  value = databricks_catalog.this.name
}

output "external_locations" {
  value = { for k, v in databricks_external_location.layer : k => v.url }
}
