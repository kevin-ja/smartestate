# ─────────────────────────────────────────────────────────────────────────────
# INFRA AWS · PARA USO DE DATABRICKS
#
# Rol cross-account: el que asume el control plane de Databricks para lanzar y apagar
# las EC2 de los clusters DENTRO de nuestra cuenta AWS (workspace clásico, no serverless).
#
# Es distinto del rol smartestate-data-pipeline: este maneja CÓMPUTO (EC2, VPC) y no
# toca el lake; aquel maneja DATOS (S3 por prefijo) y no toca EC2.
# ─────────────────────────────────────────────────────────────────────────────

# Trust policy: solo la cuenta AWS de Databricks, y solo con nuestro Account ID como ExternalId.
data "databricks_aws_assume_role_policy" "this" {
  external_id = var.databricks_account_id
}

# Permisos: los mínimos que Databricks publica para VPC administrada por Databricks ("managed").
data "databricks_aws_crossaccount_policy" "this" {
  policy_type = "managed"
}

resource "aws_iam_role" "this" {
  name               = var.role_name
  description        = "Databricks lanza los clusters del workspace SmartEstate en esta cuenta. Sin acceso al lake."
  assume_role_policy = data.databricks_aws_assume_role_policy.this.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "this" {
  name   = "databricks-compute"
  role   = aws_iam_role.this.id
  policy = data.databricks_aws_crossaccount_policy.this.json
}
