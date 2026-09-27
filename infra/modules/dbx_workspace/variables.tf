variable "name" {
  description = "Nombre del workspace; prefijo de sus credenciales y storage."
  type        = string
}

variable "region" {
  description = "Región AWS del workspace. Co-localizada con el lake."
  type        = string
}

variable "databricks_account_id" {
  type = string
}

variable "crossaccount_role_arn" {
  description = "Rol que Databricks asume para lanzar clusters en nuestra cuenta."
  type        = string
}

variable "root_bucket_name" {
  description = "Bucket raíz del workspace (no el lake)."
  type        = string
}
