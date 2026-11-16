variable "name" {
  description = "Prefijo de los recursos. Debe empezar con el prefijo del proyecto."
  type        = string
}

variable "region" {
  type = string
}

variable "alert_email" {
  description = "Recibe las alarmas. AWS pide confirmar la suscripción por correo. Vacío: sin suscripción."
  type        = string
  default     = ""
}

variable "instance_id" {
  type = string
}

variable "public_ip" {
  description = "IP elástica de la API: el latido de Route 53 la llama desde afuera."
  type        = string
}

variable "api_port" {
  type    = number
  default = 8000
}

variable "health_check_regions" {
  description = "Regiones de Route 53 que llaman a /health (mínimo 3). Sus IP deben entrar al security group."
  type        = list(string)
}

variable "log_group_name" {
  description = "Log group de la API: de ahí salen las métricas por request y la Lambda semanal."
  type        = string
}

variable "log_group_arn" {
  type = string
}

variable "lake_bucket_name" {
  type = string
}

variable "lake_bucket_arn" {
  type = string
}

variable "repo_root" {
  description = "Raíz del repo: la Lambda se empaqueta desde src/."
  type        = string
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "tags" {
  type    = map(string)
  default = {}
}
