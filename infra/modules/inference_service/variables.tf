variable "name" {
  description = "Nombre de los recursos. Debe empezar con el prefijo del proyecto (política iac-smartestate-data)."
  type        = string
}

variable "account_id" {
  type = string
}

variable "region" {
  type = string
}

variable "lake_bucket_arn" {
  description = "Bucket del lake: la instancia lee runs/<run_id>/."
  type        = string
}

variable "ansible_bucket_arn" {
  description = "Bucket por el que Ansible pasa sus archivos a la instancia."
  type        = string
}

variable "allowed_cidrs" {
  description = "IP que pueden llamar a la API, ej. [\"203.0.113.10/32\"]. Vacío: nadie entra desde afuera."
  type        = list(string)
  default     = []
}

variable "instance_type" {
  description = "Debe ser x86: la imagen se construye para linux/amd64."
  type        = string
  default     = "t3.small"
}

variable "root_volume_gb" {
  type    = number
  default = 20
}

variable "api_port" {
  type    = number
  default = 8000
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/24"
}

variable "subnet_cidr" {
  type    = string
  default = "10.20.0.0/25"
}

variable "table_prefix" {
  description = "Prefijo de las tablas del feature store online (una por corrida)."
  type        = string
  default     = "spots_fs_online_"
}

variable "images_to_keep" {
  type    = number
  default = 5
}

variable "log_retention_days" {
  type    = number
  default = 7
}

variable "tags" {
  type    = map(string)
  default = {}
}
