variable "name" {
  type = string
}

variable "metastore_id" {
  type = string
}

variable "aws_account_id" {
  type = string
}

variable "role_name" {
  description = "Rol IAM que asume Unity Catalog. Debe existir en la misma cuenta AWS."
  type        = string
}
