# ─────────────────────────────────────────────────────────────────────────────
# DATABRICKS · Storage Credential del lake (nivel CUENTA)
#
# Registra en Unity Catalog el rol del pipeline de datos: "para tocar el lake, asumí este rol".
# Va a nivel cuenta, colgada del metastore y no del workspace: así sobrevive al destroy del
# workspace de cada sesión y la trust policy del rol no cambia en cada ciclo.
#
# También genera la trust policy que el rol necesita (Unity Catalog + auto-asunción), con el
# ExternalId que devuelve la credencial. El rol se referencia por ARN armado como texto: si se
# usara el output del módulo del rol habría un ciclo (rol → credencial → rol).
# ─────────────────────────────────────────────────────────────────────────────

locals {
  role_arn = "arn:aws:iam::${var.aws_account_id}:role/${var.role_name}"
}

resource "databricks_storage_credential" "this" {
  name         = var.name
  metastore_id = var.metastore_id
  comment      = "Acceso de Unity Catalog al lake vía ${var.role_name}. Gestionado por Terraform."

  aws_iam_role {
    role_arn = local.role_arn
  }

  # Al crearla, el rol todavía no confía en Unity Catalog (la trust policy necesita el
  # ExternalId que sale de acá). La validación real ocurre al crear las External Locations.
  skip_validation = true
}

data "databricks_aws_unity_catalog_assume_role_policy" "this" {
  aws_account_id = var.aws_account_id
  role_name      = var.role_name
  external_id    = databricks_storage_credential.this.aws_iam_role[0].external_id
}
