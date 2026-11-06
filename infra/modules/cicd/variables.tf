variable "project" {
  description = "Prefijo de los nombres. Debe coincidir con las políticas iac-smartestate-*."
  type        = string
}

variable "account_id" {
  type = string
}

variable "region" {
  type = string
}

variable "github_subject_prefix" {
  description = "Inicio de la identidad OIDC que GitHub envía para el repo: repo:dueño@id/nombre@id."
  type        = string
}

variable "create_oidc_provider" {
  description = "false si la cuenta ya tiene el proveedor de GitHub: AWS admite uno solo por URL."
  type        = bool
  default     = true
}

variable "lake_bucket_arn" {
  type = string
}

variable "ecr_repository_name" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
