variable "bucket_name" {
  description = "Nombre global del bucket."
  type        = string
}

variable "layers" {
  description = "Prefijos de primer nivel. Se materializan como objetos vacíos para que la estructura sea visible en la consola."
  type        = list(string)
}

variable "versioning_enabled" {
  description = "Versionado de objetos. Es por bucket, no por prefijo."
  type        = bool
  default     = true
}

variable "noncurrent_version_expiration_days" {
  description = "Días antes de borrar versiones anteriores. Toda tarea escribe con overwrite, así que sin esto cada corrida deja una versión muerta."
  type        = number
  default     = 30
}

variable "raw_transition_days" {
  description = "Días antes de mover raw/ a Glacier Instant Retrieval. raw es append-only y se consulta poco."
  type        = number
  default     = 90
}

variable "tags" {
  type    = map(string)
  default = {}
}
