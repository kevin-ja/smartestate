output "bucket_name" {
  description = "Nombre del bucket del lake."
  value       = module.lake.bucket_name
}

output "bucket_arn" {
  description = "ARN del bucket del lake."
  value       = module.lake.bucket_arn
}

output "data_pipeline_role_arn" {
  description = "ARN del rol que ejecuta el pipeline de datos. Es lo que se configura en Databricks."
  value       = module.data_pipeline_role.role_arn
}

output "databricks_workspace_url" {
  description = "URL del workspace de SmartEstate."
  value       = module.dbx_workspace.workspace_url
}

output "databricks_workspace_id" {
  value = module.dbx_workspace.workspace_id
}

output "databricks_metastore_id" {
  value = module.dbx_metastore.metastore_id
}

output "databricks_crossaccount_role_arn" {
  value = module.dbx_crossaccount_role.role_arn
}

output "databricks_root_bucket" {
  value = module.dbx_root_bucket.bucket_name
}

output "databricks_engineers_group" {
  value = module.dbx_identities.engineers_group_name
}

output "databricks_lake_credential" {
  description = "Storage Credential del lake. La usan las External Locations de infra/uc/."
  value       = module.dbx_storage_credential.name
}

output "databricks_pipeline_group" {
  value = module.dbx_pipeline_identity.group_name
}

output "databricks_jobs_sp_application_id" {
  description = "run_as de los jobs (Asset Bundles)."
  value       = module.dbx_pipeline_identity.application_id
}

output "databricks_jobs_policy_id" {
  description = "policy_id de los job clusters (Asset Bundles). Cambia en cada recreación del workspace."
  value       = module.dbx_compute_policies.jobs_policy_id
}
