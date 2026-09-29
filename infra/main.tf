# ─────────────────────────────────────────────────────────────────────────────
# SmartEstate · infraestructura del pipeline de DATOS
#
# Alcance: S3 e IAM del pipeline de datos, y el workspace de Databricks que lo ejecuta.
# Unity Catalog (External Locations, catálogo, grants) vive en infra/uc/, con estado propio.
# El pipeline de ML se agrega como módulos aparte, sin tocar lo que hay acá.
# ─────────────────────────────────────────────────────────────────────────────

module "lake" {
  source = "./modules/lake"

  bucket_name = "${var.project}-lake-${var.account_id}"
  layers      = ["raw", "bronze", "silver", "gold", "ref", "ml"]
  tags        = var.tags
}

locals {
  # Lo usan el rol y la Storage Credential que lo registra en Unity Catalog.
  pipeline_role_name = "${var.project}-data-pipeline"
}

module "data_pipeline_role" {
  source = "./modules/data_pipeline_role"

  role_name              = local.pipeline_role_name
  account_id             = var.account_id
  bucket_name            = module.lake.bucket_name
  bucket_arn             = module.lake.bucket_arn
  read_prefixes          = ["raw", "ref"]
  read_write_prefixes    = ["bronze", "silver", "gold"]
  trusted_principal_arns = var.trusted_principal_arns
  tags                   = var.tags

  # Quien asume el rol es Unity Catalog (ver module.dbx_storage_credential).
  assume_role_policy_json = module.dbx_storage_credential.assume_role_policy_json
}

# ─────────────────────────────────────────────────────────────────────────────
# Databricks · workspace CLÁSICO en us-east-1 (cómputo en esta cuenta AWS)
#
# Orden: infra AWS para Databricks (bucket raíz + rol cross-account)
#        → workspace (nivel cuenta) → metastore → Storage Credential → identidades → cluster policies.
# El workspace se destruye cada sesión (destroy -target=module.dbx_workspace): lo que debe persistir
# no depende de él. Ver README.md, "Dos estados, dos ciclos de vida".
# ─────────────────────────────────────────────────────────────────────────────

locals {
  dbx_tags = merge(var.tags, { Stage = "databricks-workspace" })
}

module "dbx_root_bucket" {
  source    = "./modules/dbx_root_bucket"
  providers = { databricks = databricks.account }

  bucket_name           = "${var.project}-dbx-root-${var.account_id}"
  databricks_account_id = var.databricks_account_id
  tags                  = local.dbx_tags
}

module "dbx_crossaccount_role" {
  source    = "./modules/dbx_crossaccount_role"
  providers = { databricks = databricks.account }

  role_name             = "${var.project}-databricks-crossaccount"
  databricks_account_id = var.databricks_account_id
  tags                  = local.dbx_tags
}

module "dbx_workspace" {
  source    = "./modules/dbx_workspace"
  providers = { databricks = databricks.account }

  name                  = var.project
  region                = var.region
  databricks_account_id = var.databricks_account_id
  crossaccount_role_arn = module.dbx_crossaccount_role.role_arn
  root_bucket_name      = module.dbx_root_bucket.bucket_name

  # La bucket policy debe existir antes de que Databricks valide el bucket.
  depends_on = [module.dbx_root_bucket]
}

module "dbx_metastore" {
  source    = "./modules/dbx_metastore"
  providers = { databricks = databricks.account }

  name         = "${var.project}-${var.region}"
  region       = var.region
  workspace_id = module.dbx_workspace.workspace_id
}

# Conexión del lake, parte de nivel CUENTA: registra el rol del pipeline en Unity Catalog.
# Lo que vive dentro del workspace (External Locations, catálogo, grants) está en infra/uc/,
# con su propio estado, para que el destroy del workspace no lo arrastre.
module "dbx_storage_credential" {
  source    = "./modules/dbx_storage_credential"
  providers = { databricks = databricks.account }

  name           = "${var.project}-lake"
  metastore_id   = module.dbx_metastore.metastore_id
  aws_account_id = var.account_id
  role_name      = local.pipeline_role_name
}

module "dbx_identities" {
  source = "./modules/dbx_identities"
  providers = {
    databricks           = databricks.account
    databricks.workspace = databricks.workspace
  }

  project          = var.project
  workspace_id     = module.dbx_workspace.workspace_id
  daily_user_email = var.daily_user_email

  # La asignación al workspace necesita el metastore ya asignado (identity federation).
  depends_on = [module.dbx_metastore]
}

# Identidad que EJECUTA el pipeline (run_as de los jobs). Sin depends_on a nivel módulo: así solo
# su asignación al workspace entra en el destroy de cada sesión; SP y grupo persisten.
module "dbx_pipeline_identity" {
  source = "./modules/dbx_pipeline_identity"
  providers = {
    databricks           = databricks.account
    databricks.workspace = databricks.workspace
  }

  project                    = var.project
  databricks_account_id      = var.databricks_account_id
  workspace_id               = module.dbx_metastore.assigned_workspace_id
  iac_service_principal_name = var.databricks_iac_sp_name
  run_as_user_groups         = [module.dbx_identities.engineers_acl_principal_id]
}

# Cómputo: los clusters se crean solo a través de estas policies. Van después de las identidades:
# los grupos tienen que estar asignados al workspace para recibir CAN_USE.
module "dbx_compute_policies" {
  source    = "./modules/dbx_compute_policies"
  providers = { databricks = databricks.workspace }

  project         = var.project
  engineers_group = module.dbx_identities.engineers_group_name
  pipeline_group  = module.dbx_pipeline_identity.group_name

  depends_on = [module.dbx_identities, module.dbx_pipeline_identity]
}
