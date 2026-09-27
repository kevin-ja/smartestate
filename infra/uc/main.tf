# ─────────────────────────────────────────────────────────────────────────────
# SmartEstate · Unity Catalog del lake (nivel WORKSPACE, estado propio)
#
# Cadena: rol IAM → Storage Credential (infra/) → External Location por capa → catálogo y
# schemas → grants al grupo. Una External Location por capa y no una sobre todo el bucket: el
# rol solo lista sus propios prefijos, y así cada capa tiene su propio control.
#
# Permisos del grupo: los mismos que el rol IAM. Lee raw/ y ref/ como archivos; lee y escribe
# tablas en bronze, silver y gold. ml/ no aparece (arquitectura.md §2).
# ─────────────────────────────────────────────────────────────────────────────

locals {
  infra      = data.terraform_remote_state.infra.outputs
  bucket     = local.infra.bucket_name
  credential = local.infra.databricks_lake_credential

  # Mismos grants de datos para los dos grupos: el que desarrolla (engineers) y el que ejecuta los
  # jobs (pipeline). Grupos separados: un permiso extra para explorar no llega al pipeline.
  groups = [local.infra.databricks_engineers_group, local.infra.databricks_pipeline_group]

  layers = merge(
    { for l in var.read_layers : l => { read_only = true } },
    { for l in var.table_layers : l => { read_only = false } },
  )
}

resource "databricks_external_location" "layer" {
  for_each = local.layers

  name = "${var.catalog_name}-${each.key}"
  # Sin "/" final: la API la quita, y con ella el catálogo y los schemas que referencian esta
  # URL fallan con "Provider produced inconsistent final plan".
  url             = "s3://${local.bucket}/${each.key}"
  credential_name = local.credential
  read_only       = each.value.read_only
  comment         = "Capa ${each.key} del lake. Gestionado por Terraform."

  # Sin file events: exigirían SNS/SQS en el rol, y el pipeline no los usa.
  enable_file_events = false
}

# El metastore no tiene storage_root (infra/modules/dbx_metastore), así que el catálogo necesita
# uno. Se usa gold/, pero no guarda nada propio: cada schema declara su capa.
resource "databricks_catalog" "this" {
  name         = var.catalog_name
  storage_root = databricks_external_location.layer["gold"].url
  comment      = "Lakehouse de SmartEstate. Un schema por capa. Gestionado por Terraform."

  # Sin force_destroy: borrar el catálogo con tablas adentro debe fallar, no pasar en silencio.
}

resource "databricks_schema" "layer" {
  for_each = toset(var.table_layers)

  catalog_name = databricks_catalog.this.name
  name         = each.key
  storage_root = databricks_external_location.layer[each.key].url
  comment      = "Capa ${each.key}. Gestionado por Terraform."
}

# ── Grants ───────────────────────────────────────────────────────────────────
# databricks_grants es AUTORITATIVO: lo que no está acá se quita del objeto. Los grants se dan
# a grupos, nunca a usuarios ni SPs. El service principal no necesita grants: es owner de todo.

resource "databricks_grants" "catalog" {
  catalog = databricks_catalog.this.name

  dynamic "grant" {
    for_each = local.groups
    content {
      principal  = grant.value
      privileges = ["USE_CATALOG"]
    }
  }
}

resource "databricks_grants" "schema" {
  for_each = databricks_schema.layer

  schema = each.value.id

  dynamic "grant" {
    for_each = local.groups
    content {
      principal  = grant.value
      privileges = ["USE_SCHEMA", "SELECT", "MODIFY", "CREATE_TABLE"]
    }
  }
}

# Leer los CSV de raw/ y ref/ directamente como archivos. Las capas de tablas no llevan
# grants de archivos: se accede por tabla, no por ruta.
resource "databricks_grants" "read_location" {
  for_each = toset(var.read_layers)

  external_location = databricks_external_location.layer[each.key].id

  dynamic "grant" {
    for_each = local.groups
    content {
      principal  = grant.value
      privileges = ["READ_FILES"]
    }
  }
}
