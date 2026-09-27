# ─────────────────────────────────────────────────────────────────────────────
# DATABRICKS · nivel cuenta
#
# Workspace CLÁSICO: control plane en Databricks, cómputo (EC2) en nuestra cuenta AWS.
# Se registran las dos piezas AWS (rol cross-account y bucket raíz) y con ellas se crea
# el workspace. Red: VPC administrada por Databricks (sin network_id).
# Ojo: no se puede migrar después a VPC propia sin recrear el workspace.
# ─────────────────────────────────────────────────────────────────────────────

# IAM es eventualmente consistente: un rol recién creado puede no ser asumible todavía
# y Databricks rechaza la credencial. Esta espera evita ese fallo intermitente.
resource "time_sleep" "iam_propagation" {
  create_duration = "30s"

  triggers = {
    role_arn = var.crossaccount_role_arn
  }
}

resource "databricks_mws_credentials" "this" {
  credentials_name = "${var.name}-credentials"
  role_arn         = time_sleep.iam_propagation.triggers["role_arn"]
}

resource "databricks_mws_storage_configurations" "this" {
  account_id                 = var.databricks_account_id
  storage_configuration_name = "${var.name}-root-storage"
  bucket_name                = var.root_bucket_name
}

resource "databricks_mws_workspaces" "this" {
  account_id     = var.databricks_account_id
  workspace_name = var.name
  aws_region     = var.region

  credentials_id           = databricks_mws_credentials.this.credentials_id
  storage_configuration_id = databricks_mws_storage_configurations.this.storage_configuration_id
}
