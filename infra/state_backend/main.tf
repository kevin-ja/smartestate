# Bucket del estado de Terraform de infra/. Es el primer paso de un despliegue desde cero.
#
# Va en un stack aparte, con estado local propio, porque el bucket debe existir antes de que infra/ pueda guardar
# su estado en él.
#
#   cd infra/state_backend
#   terraform init && terraform apply
#   terraform output -raw backend_config > ../backend.hcl
#
# Requiere la política iac-smartestate-inference.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "project" {
  description = "Prefijo del nombre. Debe coincidir con la política iac-smartestate-inference."
  type        = string
  default     = "smartestate"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "profile" {
  description = "Perfil local de AWS CLI con el que Terraform autentica."
  type        = string
  default     = "smartestate"
}

provider "aws" {
  region  = var.region
  profile = var.profile

  default_tags {
    tags = {
      Project   = var.project
      Stage     = "terraform-state"
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "state" {
  bucket = "${var.project}-tfstate-${data.aws_caller_identity.current.account_id}"

  # Perder este bucket deja a Terraform sin saber qué infraestructura existe.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

output "backend_config" {
  description = "Contenido de infra/backend.hcl, que no se versiona porque lleva el ID de la cuenta."
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.state.id}"
    key          = "infra/terraform.tfstate"
    region       = "${var.region}"
    encrypt      = true
    use_lockfile = true
  EOT
}
