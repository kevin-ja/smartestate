variable "role_name" {
  description = "Nombre del rol. Debe empezar con el prefijo del proyecto para caer dentro de la política iac-smartestate-data."
  type        = string
}

variable "account_id" {
  type = string
}

variable "bucket_name" {
  type = string
}

variable "bucket_arn" {
  type = string
}

variable "read_prefixes" {
  description = "Prefijos de solo lectura."
  type        = list(string)
}

variable "read_write_prefixes" {
  description = "Prefijos donde el pipeline escribe sus capas."
  type        = list(string)
}

variable "trusted_principal_arns" {
  description = "Quién puede asumir el rol. Vacío = solo la propia cuenta."
  type        = list(string)
  default     = []
}

variable "external_id" {
  description = "ExternalId para el assume-role entre cuentas. Requerido cuando el que asume vive en otra cuenta (p. ej. Databricks)."
  type        = string
  default     = null
  sensitive   = true
}

variable "assume_role_policy_json" {
  description = "Trust policy completa. Si se da, reemplaza a la que arman trusted_principal_arns y external_id (p. ej. la que exige Unity Catalog)."
  type        = string
  default     = null
}

variable "tags" {
  type    = map(string)
  default = {}
}
