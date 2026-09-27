# ─────────────────────────────────────────────────────────────────────────────
# DATABRICKS · Unity Catalog
#
# Metastore regional: uno por región, compartido por todos los workspaces de esa región.
# Por eso va en su propio módulo y no dentro del workspace: tiene otro ciclo de vida.
# El único que existía (metastore_aws_us_west_2) no sirve para un workspace en us-east-1.
#
# Sin storage_root a nivel metastore (recomendación actual de Databricks): cada catálogo
# declarará su propia ubicación en el lake, vía External Location.
# ─────────────────────────────────────────────────────────────────────────────

resource "databricks_metastore" "this" {
  name   = var.name
  region = var.region

  # No guarda datos (sin storage_root): borrarlo no pierde tablas del lake.
  force_destroy = true
}

resource "databricks_metastore_assignment" "this" {
  metastore_id = databricks_metastore.this.id
  workspace_id = var.workspace_id
}
