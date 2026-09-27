output "metastore_id" {
  value = databricks_metastore.this.id
}

output "assigned_workspace_id" {
  description = "Workspace con el metastore ya asignado. Usarlo (y no el del workspace) ordena después de la asignación."
  value       = databricks_metastore_assignment.this.workspace_id
}
