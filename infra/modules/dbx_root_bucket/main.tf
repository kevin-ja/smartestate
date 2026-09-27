# ─────────────────────────────────────────────────────────────────────────────
# INFRA AWS · PARA USO DE DATABRICKS
#
# Bucket raíz del workspace: el "disco interno" de Databricks (resultados de comandos,
# historial de notebooks y jobs, logs, DBFS). NO guarda datos del negocio: eso es el lake.
#
# Va separado del lake a propósito: su bucket policy le da a la cuenta AWS de Databricks
# acceso a TODO el bucket. En el lake eso rompería el acote por prefijo (ml/ denegado).
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "this" {
  bucket = var.bucket_name

  # Solo contiene estado interno del workspace, regenerable. Permite destruir el
  # workspace sin vaciar el bucket a mano (el lake NO tiene esto, a propósito).
  force_destroy = true

  tags = var.tags
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Política exacta que exige Databricks, generada por su provider (no escrita a mano).
# Con databricks_e2_account_id, el acceso queda restringido a NUESTRA cuenta de Databricks,
# no a cualquier cliente que use la cuenta AWS de Databricks.
data "databricks_aws_bucket_policy" "this" {
  bucket                   = var.bucket_name
  databricks_e2_account_id = var.databricks_account_id
}

resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id
  policy = data.databricks_aws_bucket_policy.this.json

  depends_on = [aws_s3_bucket_public_access_block.this]
}
