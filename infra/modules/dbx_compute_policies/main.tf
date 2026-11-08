# ─────────────────────────────────────────────────────────────────────────────
# DATABRICKS · cluster policies (dentro del WORKSPACE)
#
# Nadie tiene allow_cluster_create libre: se crean clusters SOLO a través de estas policies
# (CAN_USE). Dos, una por uso:
#   - dev:  cluster interactivo para desarrollar (engineers). Auto-termination fija.
#   - jobs: cluster efímero de un job (pipeline). Nace y muere con cada corrida.
#
# Las dos son un solo nodo: el dataset son dos CSV. dev es spot; jobs, on-demand.
# El nodo de jobs es propio (jobs_node_type_id): escalar T1 no encarece el cluster dev.
# Viven en el workspace: se destruyen y recrean con él en cada sesión (no guardan estado).
# El runtime está fijo en var.spark_version: antes era "el último LTS" y podía cambiar solo (criterio 4).
# ─────────────────────────────────────────────────────────────────────────────


locals {
  # Reglas comunes a las dos policies. "fixed" = el usuario no puede cambiarlo.
  common = {
    "spark_version"                               = { type = "fixed", value = var.spark_version }
    "node_type_id"                                = { type = "fixed", value = var.node_type_id }
    "driver_node_type_id"                         = { type = "fixed", value = var.node_type_id }
    "num_workers"                                 = { type = "fixed", value = 0 }
    "spark_conf.spark.databricks.cluster.profile" = { type = "fixed", value = "singleNode" }
    "spark_conf.spark.master"                     = { type = "fixed", value = "local[*]" }
    "custom_tags.ResourceClass"                   = { type = "fixed", value = "SingleNode" }
    "custom_tags.Project"                         = { type = "fixed", value = var.project }
    # Dedicado (single user): el modo que Unity Catalog exige para leer raw/ como archivos.
    "data_security_mode" = { type = "fixed", value = "SINGLE_USER" }
    "instance_pool_id"   = { type = "forbidden", hidden = true }
    # Zona elegida por Databricks, con reintento en otra: m5d.large no existe en us-east-1e (2026-11-03).
    "aws_attributes.zone_id" = { type = "fixed", value = "auto" }
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
    # Spot: si AWS reclama el nodo, se reinicia el cluster y listo.
    "aws_attributes.availability"    = { type = "fixed", value = "SPOT_WITH_FALLBACK" }
    "aws_attributes.first_on_demand" = { type = "fixed", value = 0 }
  }))
}

resource "databricks_cluster_policy" "jobs" {
  name        = "${var.project}-jobs"
  description = "Cluster efímero de los jobs del pipeline: 1 nodo ${var.jobs_node_type_id}."

  definition = jsonencode(merge(local.common, {
    # Reemplazan a los de local.common (merge: gana el último).
    "node_type_id"        = { type = "fixed", value = var.jobs_node_type_id }
    "driver_node_type_id" = { type = "fixed", value = var.jobs_node_type_id }
    "cluster_type"        = { type = "fixed", value = "job" }
    "custom_tags.Uso"     = { type = "fixed", value = "jobs" }
    # On-demand: el único nodo es el driver, y si AWS lo reclama se pierde la corrida entera
    # (pasó el 2026-09-28). Cuesta centavos más por corrida.
    "aws_attributes.availability"    = { type = "fixed", value = "ON_DEMAND" }
    "aws_attributes.first_on_demand" = { type = "fixed", value = 1 }
    # Fijo: los tasks del job comparten cluster y la publicación a DynamoDB (T3) lo necesita.
    "aws_attributes.instance_profile_arn" = { type = "fixed", value = databricks_instance_profile.dynamodb.id }
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

# ── Instance profile de DynamoDB ─────────────────────────────────────────────
# El rol vive en AWS (modules/dynamodb_publish_profile); aquí se registra en el workspace para que
# los job clusters lo usen al publicar el feature store online. Al registrarlo, Databricks valida
# que el cross-account role pueda pasarlo (PassRole, 4b).
resource "databricks_instance_profile" "dynamodb" {
  instance_profile_arn = var.dynamodb_instance_profile_arn
}

# Mismos grupos que la policy de jobs: pipeline corre los jobs; engineers los despliega y prueba.
data "databricks_group" "instance_profile_users" {
  for_each     = toset([var.pipeline_group, var.engineers_group])
  display_name = each.value
}

resource "databricks_group_role" "dynamodb" {
  for_each = data.databricks_group.instance_profile_users
  group_id = each.value.id
  role     = databricks_instance_profile.dynamodb.id
}
