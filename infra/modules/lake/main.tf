# Bucket único del lakehouse. Una capa por prefijo.
# La frontera entre pipelines no la da el bucket: la da IAM, acotando por prefijo.

resource "aws_s3_bucket" "this" {
  bucket = var.bucket_name
  tags   = var.tags
}

# Sin acceso público bajo ninguna forma: es inventario comercial de clientes.
resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Sin ACLs: el dueño del bucket es dueño de todo objeto.
resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# SSE-S3: sin costo. KMS solo si aparece un requisito de cumplimiento.
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = var.versioning_enabled ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket     = aws_s3_bucket.this.id
  depends_on = [aws_s3_bucket_versioning.this]

  # raw es append-only e íntegro, pero se lee poco: se enfría.
  rule {
    id     = "raw-a-glacier-ir"
    status = "Enabled"

    filter {
      prefix = "raw/"
    }

    transition {
      days          = var.raw_transition_days
      storage_class = "GLACIER_IR"
    }
  }

  # Las capas derivadas se reescriben en cada corrida: las versiones viejas caducan.
  rule {
    id     = "expirar-versiones-anteriores"
    status = var.versioning_enabled ? "Enabled" : "Disabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }
  }

  # Limpieza de multipart abortados: de otro modo se cobran en silencio.
  rule {
    id     = "abortar-multipart-huerfanos"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# S3 no tiene carpetas. Estos objetos vacíos hacen visible la estructura de capas.
resource "aws_s3_object" "layer" {
  for_each = toset(var.layers)

  bucket       = aws_s3_bucket.this.id
  key          = "${each.value}/"
  content_type = "application/x-directory"

  depends_on = [aws_s3_bucket_ownership_controls.this]
}
