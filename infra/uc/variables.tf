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
  description = "Capas de solo lectura: se leen como archivos (CSV de origen, referencias)."
  type        = list(string)
  default     = ["raw", "ref"]
}

variable "table_layers" {
  description = "Capas donde el pipeline escribe tablas. Cada una es un schema del catálogo."
  type        = list(string)
  default     = ["bronze", "silver", "gold"]
}
