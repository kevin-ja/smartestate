output "dev_policy_id" {
  value = databricks_cluster_policy.dev.id
}

output "jobs_policy_id" {
  description = "Lo que va en policy_id de los job clusters de los Asset Bundles."
  value       = databricks_cluster_policy.jobs.id
}
