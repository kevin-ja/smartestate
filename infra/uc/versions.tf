terraform {
  required_version = ">= 1.9"

  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.134"
    }
  }

  # Estado local y PROPIO, separado del de infra/. El destroy del workspace de cada sesión
  # corre en infra/ y no toca este estado: catálogo, tablas y grants persisten en el metastore.
}

# Lee del estado de infra/ la URL del workspace (cambia en cada recreación), el bucket,
# la credencial y el grupo. Nada se copia a mano. Ese estado vive en S3 (infra/state_backend/).
data "terraform_remote_state" "infra" {
  backend = "s3"
  config = {
    bucket  = "smartestate-tfstate-${var.account_id}"
    key     = "infra/terraform.tfstate"
    region  = "us-east-1"
    profile = var.aws_profile
  }
}

# Mismo service principal que infra/: el host explícito pisa al del perfil (nivel cuenta).
provider "databricks" {
  host    = data.terraform_remote_state.infra.outputs.databricks_workspace_url
  profile = var.databricks_profile
}
