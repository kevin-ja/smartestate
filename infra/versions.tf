terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.134"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.14"
    }
  }

  # Estado local. Para migrar a S3 más adelante: añadir aquí un bloque
  # `backend "s3"` y correr `terraform init -migrate-state`.
}

provider "aws" {
  region  = var.region
  profile = var.profile

  default_tags {
    tags = var.tags
  }
}

# Nivel CUENTA de Databricks (workspaces, credenciales, metastores).
# Autentica como el service principal smartestate-databricks (OAuth M2M, perfil en
# ~/.databrickscfg), nunca con el admin humano. Ver infra/bootstrap/databricks-iac-sp.sh.
provider "databricks" {
  alias   = "account"
  profile = var.databricks_profile
}

# DENTRO del workspace (entitlements, y luego Unity Catalog). Mismo service principal:
# el host explícito pisa al del perfil. Es admin del workspace porque lo creó.
provider "databricks" {
  alias   = "workspace"
  host    = module.dbx_workspace.workspace_url
  profile = var.databricks_profile
}
