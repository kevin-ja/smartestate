variable "databricks_profile" {
  description = "Perfil de ~/.databrickscfg (service principal de IaC). El mismo que usa infra/."
  type        = string
  default     = "smartestate-databricks"
}

variable "catalog_name" {
  type    = string
  default = "smartestate"
}

variable "read_layers" {
  description = "Capas de solo lectura: se leen como archivos (CSV de origen, referencias, modelos preentrenados)."
  type        = list(string)
  default     = ["raw", "ref", "models"]
}

variable "file_layers" {
  description = "Capas donde el pipeline escribe archivos (no tablas): salidas de cada corrida en runs/<run_id>/."
  type        = list(string)
  default     = ["runs"]
}

variable "table_layers" {
  description = "Capas donde el pipeline escribe tablas. Cada una es un schema del catálogo."
  type        = list(string)
  default     = ["feature_store_offline"]
}

variable "account_id" {
  description = "Cuenta AWS: nombra el bucket del estado de infra/. Sale de TF_VAR_account_id en .env."
  type        = string
}

variable "aws_profile" {
  description = "Perfil AWS con acceso al bucket del estado de infra/."
  type        = string
  default     = "smartestate"
}
