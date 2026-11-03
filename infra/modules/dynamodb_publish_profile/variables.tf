variable "name" {
  description = "Nombre del rol y del instance profile. Debe empezar con el prefijo del proyecto (política iac-smartestate-data)."
  type        = string
}

variable "account_id" {
  type = string
}

variable "region" {
  type = string
}

variable "table_prefix" {
  description = "Prefijo de las tablas del feature store online (una por corrida)."
  type        = string
  default     = "spots_fs_online_"
}

variable "tags" {
  type    = map(string)
  default = {}
}
