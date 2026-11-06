# Servicio de inferencia: una instancia EC2 que corre la API en Docker (inference_pipeline_design.md).
#
# Red propia con subnet pública, sin NAT ni ALB. ECR, Logs y SSM van por internet; S3 y DynamoDB, por gateway endpoints.
# Se administra por SSM: sin puerto 22 ni llaves. Permisos por instance profile: sin llaves en la imagen.
#
# Todo recurso de EC2 lleva el tag Project (default_tags del provider): la política iac-smartestate-data lo exige.

# ─── Red ─────────────────────────────────────────────────────────────────────

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(var.tags, { Name = var.name })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = var.name })
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.subnet_cidr
  availability_zone       = "${var.region}a"
  map_public_ip_on_launch = true
  tags                    = merge(var.tags, { Name = "${var.name}-public" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-public" })

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# Sin costo. Docker también baja las capas de ECR desde S3 por aquí.
resource "aws_vpc_endpoint" "gateway" {
  for_each = toset(["s3", "dynamodb"])

  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
  tags              = merge(var.tags, { Name = "${var.name}-${each.key}" })
}

# Reglas en línea, no como recursos aparte: las reglas sueltas se etiquetan por separado y la política no lo permite.
resource "aws_security_group" "this" {
  name        = var.name
  description = "API de inferencia: entra solo desde las IP autorizadas"
  vpc_id      = aws_vpc.this.id
  tags        = merge(var.tags, { Name = var.name })

  dynamic "ingress" {
    for_each = length(var.allowed_cidrs) > 0 ? [1] : []
    content {
      description = "API desde las IP autorizadas"
      protocol    = "tcp"
      from_port   = var.api_port
      to_port     = var.api_port
      cidr_blocks = var.allowed_cidrs
    }
  }

  egress {
    description = "HTTPS hacia AWS: ECR, S3, DynamoDB, Logs y SSM"
    protocol    = "tcp"
    from_port   = 443
    to_port     = 443
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ─── Imagen y logs ───────────────────────────────────────────────────────────

resource "aws_ecr_repository" "this" {
  name                 = var.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true # las imágenes se pueden reconstruir desde el repo
  tags                 = var.tags

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Conservar las últimas ${var.images_to_keep} imágenes"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = var.images_to_keep
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/smartestate/inference"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

# ─── Permisos de la instancia ────────────────────────────────────────────────

data "aws_iam_policy_document" "assume_role" {
  statement {
    sid     = "InstanciaDeInferencia"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "service" {
  statement {
    sid       = "LeerArtefactos"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.lake_bucket_arn}/runs/*"]
  }

  statement {
    sid     = "LeerFeatureStoreOnline"
    effect  = "Allow"
    actions = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
    resources = [
      "arn:aws:dynamodb:${var.region}:${var.account_id}:table/${var.table_prefix}*",
    ]
  }

  # GetAuthorizationToken no admite recurso.
  statement {
    sid       = "EntrarAEcr"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "BajarImagen"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.this.arn]
  }

  statement {
    sid       = "EscribirLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }

  statement {
    sid       = "ArchivosDeAnsible"
    effect    = "Allow"
    actions   = ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"]
    resources = ["${var.ansible_bucket_arn}/*"]
  }

  statement {
    sid       = "BucketDeAnsible"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation", "s3:GetEncryptionConfiguration"]
    resources = [var.ansible_bucket_arn]
  }
}

resource "aws_iam_role" "this" {
  name               = var.name
  description        = "Instancia de la API de inferencia. Solo lectura de artefactos y features."
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "service" {
  name   = "servicio-de-inferencia"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.service.json
}

# Lo que necesita el agente de SSM para abrir sesiones.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "this" {
  name = var.name
  role = aws_iam_role.this.name
  tags = var.tags
}

# ─── Instancia ───────────────────────────────────────────────────────────────

# Amazon Linux 2023 ya trae el agente de SSM y el AWS CLI.
data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }
}

resource "aws_instance" "this" {
  ami                    = data.aws_ami.amazon_linux.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.this.id]
  iam_instance_profile   = aws_iam_instance_profile.this.name
  tags                   = merge(var.tags, { Name = var.name })

  # Solo instala Docker. El contenedor se lanza a mano hasta que exista CD.
  user_data = <<-EOT
    #!/bin/bash
    set -euxo pipefail
    dnf install -y docker
    systemctl enable --now docker
  EOT

  # Hop limit 2: el contenedor es un salto más; con 1 no alcanza las credenciales del instance profile.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
    encrypted   = true
    tags        = merge(var.tags, { Name = var.name })
  }

  lifecycle {
    ignore_changes = [ami] # una AMI nueva no debe reemplazar la máquina en un apply cualquiera
  }

  # La salida a internet debe existir antes de que corra user_data.
  depends_on = [aws_route_table_association.public, aws_iam_role_policy_attachment.ssm]
}

resource "aws_eip" "this" {
  domain     = "vpc"
  instance   = aws_instance.this.id
  tags       = merge(var.tags, { Name = var.name })
  depends_on = [aws_internet_gateway.this]
}
