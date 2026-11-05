# CI/CD de la inferencia (inference_pipeline_design.md): GitHub Actions entra a AWS por OIDC, sin llaves.
#
# Dos roles, cada uno con lo suyo:
#   deploy      Construye, prueba y despliega. Solo desde main.
#   terraform   Aprovisiona la inferencia. Solo desde los environments de GitHub, limitados a main.
#
# Este módulo lo aplica Kevin a mano, nunca el workflow: así un workflow no puede ampliar sus propios permisos.

locals {
  oidc_host    = "token.actions.githubusercontent.com"
  oidc_arn     = "arn:aws:iam::${var.account_id}:oidc-provider/${local.oidc_host}"
  instance_arn = "arn:aws:ec2:${var.region}:${var.account_id}:instance/*"

  buckets = {
    tfplans = { versioned = true, expire_days = 90 }
    ansible = { versioned = false, expire_days = 7 } # archivos de paso: Ansible los usa y ya no sirven
  }
}

# ─── OIDC ────────────────────────────────────────────────────────────────────

# AWS admite un solo proveedor por URL en cada cuenta, y esta se comparte con otros proyectos.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
  tags           = var.tags
}

# ─── Buckets privados ────────────────────────────────────────────────────────

resource "aws_s3_bucket" "this" {
  for_each = local.buckets

  bucket = "${var.project}-${each.key}-${var.account_id}"
  tags   = var.tags
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = local.buckets

  bucket                  = aws_s3_bucket.this[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = local.buckets

  bucket = aws_s3_bucket.this[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = { for name, bucket in local.buckets : name => bucket if bucket.versioned }

  bucket = aws_s3_bucket.this[each.key].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = local.buckets

  bucket = aws_s3_bucket.this[each.key].id

  rule {
    id     = "expirar"
    status = "Enabled"

    filter {}

    expiration {
      days = each.value.expire_days
    }

    noncurrent_version_expiration {
      noncurrent_days = each.value.expire_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

# ─── Rol de despliegue ───────────────────────────────────────────────────────

data "aws_iam_policy_document" "deploy_assume_role" {
  statement {
    sid     = "SoloDesdeMain"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/main"]
    }
  }
}

data "aws_iam_policy_document" "deploy" {
  # GetAuthorizationToken no admite recurso.
  statement {
    sid       = "EntrarAEcr"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "SubirYBajarImagen"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = ["arn:aws:ecr:${var.region}:${var.account_id}:repository/${var.ecr_repository_name}"]
  }

  # El test de integración corre la imagen contra los artefactos y las features reales.
  statement {
    sid       = "LeerArtefactos"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.lake_bucket_arn}/runs/*"]
  }

  statement {
    sid       = "ListarArtefactos"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.lake_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["runs/*"]
    }
  }

  statement {
    sid       = "ActualizarQueEstaEnProduccion"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${var.lake_bucket_arn}/runs/production.json"]
  }

  statement {
    sid     = "LeerFeatureStoreOnline"
    effect  = "Allow"
    actions = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
    resources = [
      "arn:aws:dynamodb:${var.region}:${var.account_id}:table/spots_fs_online_*",
    ]
  }

  # Estas lecturas no admiten recurso.
  statement {
    sid       = "EncontrarLaInstancia"
    effect    = "Allow"
    actions   = ["ec2:DescribeInstances", "ssm:DescribeInstanceInformation"]
    resources = ["*"]
  }

  statement {
    sid       = "AbrirSesionEnLaInstancia"
    effect    = "Allow"
    actions   = ["ssm:StartSession"]
    resources = [local.instance_arn]

    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/Project"
      values   = [var.project]
    }
  }

  statement {
    sid       = "UsarElDocumentoDeSesion"
    effect    = "Allow"
    actions   = ["ssm:StartSession"]
    resources = ["arn:aws:ssm:${var.region}:${var.account_id}:document/SSM-SessionManagerRunShell"]
  }

  statement {
    sid       = "CerrarSesiones"
    effect    = "Allow"
    actions   = ["ssm:ResumeSession", "ssm:TerminateSession"]
    resources = ["arn:aws:ssm:${var.region}:${var.account_id}:session/*"]
  }

  # Ansible pasa sus archivos a la instancia por este bucket.
  statement {
    sid       = "ArchivosDeAnsible"
    effect    = "Allow"
    actions   = ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.this["ansible"].arn}/*"]
  }

  statement {
    sid       = "BucketDeAnsible"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation", "s3:ListBucket"]
    resources = [aws_s3_bucket.this["ansible"].arn]
  }
}

resource "aws_iam_role" "deploy" {
  name               = "${var.project}-github-deploy"
  description        = "GitHub Actions: construye, prueba y despliega la inferencia. Solo desde main."
  assume_role_policy = data.aws_iam_policy_document.deploy_assume_role.json
  tags               = var.tags

  depends_on = [aws_iam_openid_connect_provider.github]
}

resource "aws_iam_role_policy" "deploy" {
  name   = "desplegar-inferencia"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# ─── Rol de Terraform ────────────────────────────────────────────────────────

data "aws_iam_policy_document" "terraform_assume_role" {
  statement {
    sid     = "SoloDesdeLosEnvironments"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values = [
        "repo:${var.github_repository}:environment:terraform-plan",
        "repo:${var.github_repository}:environment:production",
      ]
    }
  }
}

resource "aws_iam_role" "terraform" {
  name               = "${var.project}-github-terraform"
  description        = "GitHub Actions: aprovisiona la inferencia. Solo desde los environments de GitHub."
  assume_role_policy = data.aws_iam_policy_document.terraform_assume_role.json
  tags               = var.tags

  depends_on = [aws_iam_openid_connect_provider.github]
}

# Las mismas políticas con las que Kevin corre Terraform: no hay una segunda lista que mantener.
resource "aws_iam_role_policy_attachment" "terraform" {
  for_each = toset(["iac-smartestate-data", "iac-smartestate-cicd"])

  role       = aws_iam_role.terraform.name
  policy_arn = "arn:aws:iam::${var.account_id}:policy/${each.key}"
}
