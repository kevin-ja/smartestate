variable "project" {
  type = string
}

variable "workspace_id" {
  description = "Workspace al que el grupo accede como USER."
  type        = string
}

variable "daily_user_email" {
  description = "Correo del usuario diario no admin."
  type        = string
}

variable "daily_user_display_name" {
  type    = string
  default = null
}
