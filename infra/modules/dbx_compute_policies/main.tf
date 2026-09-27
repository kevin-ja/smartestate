# ─────────────────────────────────────────────────────────────────────────────
# DATABRICKS · cluster policies (dentro del WORKSPACE)
#
# Nadie tiene allow_cluster_create libre: se crean clusters SOLO a través de estas policies
# (CAN_USE). Dos, una por uso:
#   - dev:  cluster interactivo para desarrollar (engineers). Auto-termination fija.
#   - jobs: cluster efímero de un job (pipeline). Nace y muere con cada corrida.
#
# Las dos son un solo nodo, instancia chica y spot con respaldo on-demand: el dataset son dos CSV.
# Si algún día hace falta escalar, se cambia la policy de jobs, no el código.
# Viven en el workspace: se destruyen y recrean con él en cada sesión (no guardan estado).
# ─────────────────────────────────────────────────────────────────────────────

data "databricks_spark_version" "lts" {
  long_term_support = true
}

locals {
  # Reglas comunes a las dos policies. "fixed" = el usuario no puede cambiarlo.
  common = {
    "spark_version"                               = { type = "fixed", value = data.databricks_spark_version.lts.id }
    "node_type_id"                                = { type = "fixed", value = var.node_type_id }
    "driver_node_type_id"                         = { type = "fixed", value = var.node_type_id }
    "num_workers"                                 = { type = "fixed", value = 0 }
    "spark_conf.spark.databricks.cluster.profile" = { type = "fixed", value = "singleNode" }
    "spark_conf.spark.master"                     = { type = "fixed", value = "local[*]" }
    "custom_tags.ResourceClass"                   = { type = "fixed", value = "SingleNode" }
    "custom_tags.Project"                         = { type = "fixed", value = var.project }
    # Un solo nodo: first_on_demand = 0 deja que el driver (el único nodo) sea spot.
    "aws_attributes.availability"    = { type = "fixed", value = "SPOT_WITH_FALLBACK" }
    "aws_attributes.first_on_demand" = { type = "fixed", value = 0 }
    # Dedicado (single user): el modo que Unity Catalog exige para leer raw/ como archivos.
    "data_security_mode" = { type = "fixed", value = "SINGLE_USER" }
    "instance_pool_id"   = { type = "forbidden", hidden = true }
  }
}

resource "databricks_cluster_policy" "dev" {
  name                  = "${var.project}-dev"
  description           = "Cluster interactivo de desarrollo: 1 nodo ${var.node_type_id}, se apaga a los ${var.autotermination_minutes} min."
  max_clusters_per_user = 1

  definition = jsonencode(merge(local.common, {
    "cluster_type"            = { type = "fixed", value = "all-purpose" }
    "autotermination_minutes" = { type = "fixed", value = var.autotermination_minutes }
    "custom_tags.Uso"         = { type = "fixed", value = "dev" }
  }))
}

resource "databricks_cluster_policy" "jobs" {
  name        = "${var.project}-jobs"
  description = "Cluster efímero de los jobs del pipeline: 1 nodo ${var.node_type_id}."

  definition = jsonencode(merge(local.common, {
    "cluster_type"    = { type = "fixed", value = "job" }
    "custom_tags.Uso" = { type = "fixed", value = "jobs" }
  }))
}

resource "databricks_permissions" "dev" {
  cluster_policy_id = databricks_cluster_policy.dev.id

  access_control {
    group_name       = var.engineers_group
    permission_level = "CAN_USE"
  }
}

# El pipeline la usa al correr; engineers también, para desplegar y probar jobs (target dev).
resource "databricks_permissions" "jobs" {
  cluster_policy_id = databricks_cluster_policy.jobs.id

  dynamic "access_control" {
    for_each = [var.pipeline_group, var.engineers_group]
    content {
      group_name       = access_control.value
      permission_level = "CAN_USE"
    }
  }
}
