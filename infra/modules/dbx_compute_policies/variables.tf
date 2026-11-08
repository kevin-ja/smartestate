variable "project" {
  type = string
}

variable "spark_version" {
  description = "Runtime de Databricks, fijo para que el entrenamiento sea reproducible (train_pipeline_mlops.md, criterio 4). El \".x\" son los parches de mantenimiento, que Databricks aplica solo; los paquetes exactos de cada corrida quedan en MLflow."
  type        = string
  default     = "18.x-scala2.13" # 18 LTS: Spark 4.1.0, Python 3.12.3
}

variable "node_type_id" {
  description = "Instancia del cluster dev (driver = nodo). m5d.large: 2 vCPU, 8 GB, disco NVMe local."
  type        = string
  default     = "m5d.large"
}

variable "jobs_node_type_id" {
  description = "Instancia de los job clusters. r5d.xlarge: 4 vCPU, 32 GB, NVMe; 0,288 USD/h en EC2, 2,55 veces m5d.large (2026-11-04). T1 tardó 1 h 54 min en m5d.large."
  type        = string
  default     = "r5d.xlarge"
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

variable "dynamodb_instance_profile_arn" {
  description = "Instance profile con permiso sobre DynamoDB (spots_fs_online_*). Lo llevan todos los job clusters."
  type        = string
}
