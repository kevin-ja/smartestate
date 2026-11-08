variable "project" {
  type = string
}

variable "databricks_account_id" {
  description = "Audiencia que la policy exige en el token de GitHub."
  type        = string
}

variable "github_subject_prefix" {
  description = "Identidad OIDC del repo (repo:dueño@id/nombre@id). La misma que usa AWS."
  type        = string
}

variable "engineers_group_id" {
  description = "Grupo que despliega bundles. Se recrea con el workspace."
  type        = string
}
