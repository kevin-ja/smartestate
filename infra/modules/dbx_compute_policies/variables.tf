variable "project" {
  type = string
}

variable "node_type_id" {
  description = "Instancia única (driver = nodo). m5d.large: 2 vCPU, 8 GB, disco NVMe local."
  type        = string
  default     = "m5d.large"
}

variable "autotermination_minutes" {
  type    = number
  default = 20
}

variable "engineers_group" {
  description = "Grupo que desarrolla: usa la policy dev (y la de jobs para probar)."
  type        = string
}

variable "pipeline_group" {
  description = "Grupo del SP que ejecuta los jobs: usa la policy jobs."
  type        = string
}
