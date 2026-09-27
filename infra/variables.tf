variable "project" {
  description = "Prefijo de nombres. Debe coincidir con el alcance de la política iac-smartestate-data."
  type        = string
  default     = "smartestate"
}

variable "region" {
  description = "Región AWS."
  type        = string
  default     = "us-east-1"
}

variable "profile" {
  description = "Perfil local de AWS CLI con el que Terraform autentica."
  type        = string
  default     = "smartestate"
}

variable "account_id" {
  description = "ID de la cuenta AWS. Se usa para hacer único el nombre del bucket."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "El account_id son 12 dígitos."
  }
}

variable "trusted_principal_arns" {
  description = <<-EOT
    Principals que pueden asumir el rol del pipeline de datos.
    Vacío = solo la propia cuenta. Cuando se defina Databricks, se agrega aquí
    su ARN sin tocar ningún módulo.
  EOT
  type        = list(string)
  default     = []
}

variable "databricks_account_id" {
  description = "Databricks Account ID. No es secreto: es el ExternalId de los roles que asume Databricks."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.databricks_account_id))
    error_message = "El Databricks Account ID es un UUID."
  }
}

variable "databricks_profile" {
  description = "Perfil de ~/.databrickscfg con el que Terraform autentica a nivel cuenta (service principal)."
  type        = string
  default     = "smartestate-databricks"
}

variable "databricks_iac_sp_name" {
  description = "Nombre del service principal con el que corre Terraform (ver bootstrap/databricks-iac-sp.sh)."
  type        = string
  default     = "smartestate-databricks"
}

variable "daily_user_email" {
  description = "Correo del usuario diario NO admin de Databricks (p. ej. un alias +dev). Se define en .env (TF_VAR_daily_user_email)."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.daily_user_email))
    error_message = "daily_user_email debe ser un correo válido."
  }
}

variable "tags" {
  description = "Tags aplicados a todos los recursos."
  type        = map(string)
  default = {
    Project   = "smartestate"
    Stage     = "data-pipeline"
    ManagedBy = "terraform"
  }
}
