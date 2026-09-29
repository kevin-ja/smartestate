# ─────────────────────────────────────────────────────────────────────────────
# DATABRICKS · identidad que EJECUTA el pipeline (nivel cuenta)
#
# Tercera identidad, con un solo trabajo: ser el run_as de los jobs. No aprovisiona (eso es el SP
# de IaC) ni desarrolla (eso es el grupo engineers). Sin admin de nada y sin secreto OAuth: nadie
# se loguea como él; los jobs se despliegan "en nombre de" él (rol Service Principal User).
#
# SP, grupo y rule set no dependen del workspace: sobreviven al destroy de cada sesión y conservan
# su ID, así los grants de Unity Catalog (infra/uc/) no se pierden. Solo la asignación al
# workspace se recrea con él.
# ─────────────────────────────────────────────────────────────────────────────

resource "databricks_service_principal" "jobs" {
  display_name = "${var.project}-jobs"
}

# Los grants de Unity Catalog van al grupo, nunca al SP (misma regla que con los usuarios).
resource "databricks_group" "pipeline" {
  display_name = "${var.project}-pipeline"
}

resource "databricks_group_member" "jobs" {
  group_id  = databricks_group.pipeline.id
  member_id = databricks_service_principal.jobs.id
}

resource "databricks_mws_permission_assignment" "pipeline" {
  workspace_id = var.workspace_id
  principal_id = databricks_group.pipeline.id
  permissions  = ["USER"]
}

# Igual que con engineers: estar asignado no alcanza, el SP necesita "Workspace access" para que
# un job corra como él. Solo eso: sin SQL ni creación de clusters (los define el bundle).
# Vive en el workspace: se recrea con él en cada sesión.
resource "databricks_entitlements" "pipeline" {
  provider = databricks.workspace

  group_id         = databricks_group.pipeline.id
  workspace_access = true

  depends_on = [databricks_mws_permission_assignment.pipeline]
}

# Quién puede usar el SP como run_as. El rule set es AUTORITATIVO: reemplaza al que Databricks
# crea por defecto, así que se repite el manager (el SP de IaC) para no perder el control del SP.
data "databricks_service_principal" "iac" {
  display_name = var.iac_service_principal_name
}

resource "databricks_access_control_rule_set" "jobs" {
  name = "accounts/${var.databricks_account_id}/servicePrincipals/${databricks_service_principal.jobs.application_id}/ruleSets/default"

  grant_rules {
    principals = [data.databricks_service_principal.iac.acl_principal_id]
    role       = "roles/servicePrincipal.manager"
  }

  grant_rules {
    principals = var.run_as_user_groups
    role       = "roles/servicePrincipal.user"
  }
}
