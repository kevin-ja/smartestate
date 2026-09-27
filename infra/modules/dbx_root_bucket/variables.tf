variable "bucket_name" {
  description = "Nombre global del bucket raíz. Debe caer dentro de la política iac-smartestate-data (smartestate-dbx-root-*)."
  type        = string
}

variable "databricks_account_id" {
  description = "Databricks Account ID. Restringe la bucket policy a nuestra cuenta de Databricks."
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
