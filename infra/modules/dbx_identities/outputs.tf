output "engineers_group_name" {
  description = "Grupo al que se darán los grants de Unity Catalog."
  value       = databricks_group.engineers.display_name
}

output "engineers_acl_principal_id" {
  description = "Identificador del grupo en reglas de acceso de cuenta (p. ej. quién usa un SP como run_as)."
  value       = databricks_group.engineers.acl_principal_id
}
