variable "project" {
  type = string
}

variable "databricks_account_id" {
  type = string
}

variable "workspace_id" {
  description = "Workspace al que el grupo accede como USER. Debe tener el metastore ya asignado."
  type        = string
}

variable "iac_service_principal_name" {
  description = "SP con el que corre Terraform. Queda como manager del SP de jobs."
  type        = string
}

variable "run_as_user_groups" {
  description = "acl_principal_id de los grupos que pueden desplegar jobs con run_as = este SP."
  type        = list(string)
}
