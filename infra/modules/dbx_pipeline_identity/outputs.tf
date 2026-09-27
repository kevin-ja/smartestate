output "application_id" {
  description = "Lo que va en run_as.service_principal_name de los Asset Bundles."
  value       = databricks_service_principal.jobs.application_id
}

output "group_name" {
  description = "Grupo al que se dan los grants de Unity Catalog del pipeline."
  value       = databricks_group.pipeline.display_name
}
