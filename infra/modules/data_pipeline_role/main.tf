# Rol que ejecuta el pipeline de datos.
#
# El alcance es lo importante: lee raw/ y ref/, escribe bronze/, silver/ y gold/,
# y NO tiene acceso a ml/. Eso es lo que hace cumplir el principio de arquitectura.md §2 —
# el pipeline de datos y el de ML nunca escriben en las capas del otro.

locals {
  # Sin principals declarados, el rol queda confiado a la propia cuenta.
  # Asumirlo igual exige un sts:AssumeRole explícito en la identidad que lo intente.
  principals = length(var.trusted_principal_arns) > 0 ? var.trusted_principal_arns : ["arn:aws:iam::${var.account_id}:root"]

  all_prefixes = concat(var.read_prefixes, var.read_write_prefixes)
}

data "aws_iam_policy_document" "assume_role" {
  statement {
    sid     = "AssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = local.principals
    }

    dynamic "condition" {
      for_each = var.external_id == null ? [] : [var.external_id]

      content {
        test     = "StringEquals"
        variable = "sts:ExternalId"
        values   = [condition.value]
      }
    }
  }
}

data "aws_iam_policy_document" "lake_access" {
  # Listar solo los prefijos que le incumben. ml/ no aparece.
  statement {
    sid       = "ListarPrefijosPropios"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = concat([for p in local.all_prefixes : "${p}/*"], local.all_prefixes)
    }
  }

  statement {
    sid       = "UbicarBucket"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation"]
    resources = [var.bucket_arn]
  }

  statement {
    sid       = "LeerFuentes"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [for p in var.read_prefixes : "${var.bucket_arn}/${p}/*"]
  }

  statement {
    sid    = "EscribirCapasDerivadas"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = [for p in var.read_write_prefixes : "${var.bucket_arn}/${p}/*"]
  }

  # Unity Catalog exige que el rol pueda asumirse a sí mismo. El ARN va armado como texto:
  # referenciar aws_iam_role.this desde su propia política crearía un ciclo.
  statement {
    sid       = "AsumirseASiMismo"
    effect    = "Allow"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:aws:iam::${var.account_id}:role/${var.role_name}"]
  }
}

resource "aws_iam_role" "this" {
  name        = var.role_name
  description = "Ejecuta el pipeline de datos raw -> gold. Sin acceso a ml/."
  # La trust policy de Unity Catalog, si llega, reemplaza a la genérica.
  assume_role_policy = coalesce(var.assume_role_policy_json, data.aws_iam_policy_document.assume_role.json)
  tags               = var.tags
}

resource "aws_iam_role_policy" "lake_access" {
  name   = "acceso-al-lake"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.lake_access.json
}
