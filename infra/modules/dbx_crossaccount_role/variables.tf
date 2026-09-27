variable "role_name" {
  description = "Nombre del rol. Debe empezar con el prefijo del proyecto para caer dentro de la política iac-smartestate-data."
  type        = string
}

variable "databricks_account_id" {
  description = "Databricks Account ID. Es el ExternalId de la trust policy."
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
