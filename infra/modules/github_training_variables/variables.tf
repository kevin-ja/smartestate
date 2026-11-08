variable "repository" {
  description = "Nombre del repo (sin dueño; el dueño va en el provider)."
  type        = string
}

variable "workspace_url" {
  description = "URL del workspace. Cambia en cada sesión."
  type        = string
}

variable "cicd_client_id" {
  description = "application_id del SP smartestate-cicd."
  type        = string
}

variable "jobs_sp_id" {
  description = "application_id del SP smartestate-jobs (run_as del job)."
  type        = string
}
