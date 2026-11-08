output "application_id" {
  description = "Client ID que el workflow usa para entrar por OIDC (secreto DATABRICKS_CICD_CLIENT_ID)."
  value       = databricks_service_principal.cicd.application_id
}
