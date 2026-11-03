# Instance profile de los clusters que publican el feature store online (T3, materialización).
#
# publish_table escribe en DynamoDB desde el cluster (Dedicated, classic): las credenciales salen del
# instance profile, sin llaves en secrets. Solo toca las tablas spots_fs_online_*; no tiene acceso al lake
# (eso va por Unity Catalog con el rol smartestate-data-pipeline).

data "aws_iam_policy_document" "assume_role" {
  statement {
    sid     = "EC2DelCluster"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "dynamodb" {
  # Lista que Databricks documenta para publicar a DynamoDB (fs-authentication). Databricks no la acota
  # por recurso; aquí se acota a las tablas del feature store online.
  statement {
    sid    = "TablasDelFeatureStoreOnline"
    effect = "Allow"
    actions = [
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:ConditionCheckItem",
      "dynamodb:CreateGlobalTable",
      "dynamodb:CreateTable",
      "dynamodb:DeleteItem",
      "dynamodb:DeleteTable",
      "dynamodb:DescribeTable",
      "dynamodb:DescribeTableReplicaAutoScaling",
      "dynamodb:DescribeTimeToLive",
      "dynamodb:GetItem",
      "dynamodb:PartiQLDelete",
      "dynamodb:PartiQLInsert",
      "dynamodb:PartiQLSelect",
      "dynamodb:PartiQLUpdate",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:UpdateGlobalTableSettings",
      "dynamodb:UpdateItem",
      "dynamodb:UpdateTable",
      "dynamodb:UpdateTimeToLive",
    ]
    resources = [
      "arn:aws:dynamodb:${var.region}:${var.account_id}:table/${var.table_prefix}*",
      "arn:aws:dynamodb:${var.region}:${var.account_id}:table/${var.table_prefix}*/index/*",
      "arn:aws:dynamodb::${var.account_id}:global-table/${var.table_prefix}*",
    ]
  }

  # ListTables no admite recurso: lo usan la limpieza de tablas viejas (P31) y el smoke test.
  statement {
    sid       = "ListarTablas"
    effect    = "Allow"
    actions   = ["dynamodb:ListTables"]
    resources = ["*"]
  }
}

resource "aws_iam_role" "this" {
  name               = var.name
  description        = "Clusters de Databricks que publican spots_fs_online_* en DynamoDB. Sin acceso al lake."
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "dynamodb" {
  name   = "publicar-feature-store-online"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.dynamodb.json
}

resource "aws_iam_instance_profile" "this" {
  name = var.name
  role = aws_iam_role.this.name
  tags = var.tags
}
