# ─────────────────────────────────────────────────────────────────────────────
# DATABRICKS · identidades de trabajo diario (nivel cuenta)
#
# Mínimo privilegio, igual que en AWS: el admin humano y el SP de IaC no se usan a diario.
# Los permisos se dan al GRUPO, nunca al usuario: sumar a alguien es agregarlo al grupo.
#
# Usuario sin contraseña en Terraform: Databricks le manda una invitación al correo y la
# contraseña (o el código de un solo uso) la define la persona al primer login.
# ─────────────────────────────────────────────────────────────────────────────

resource "databricks_group" "engineers" {
  display_name = "${var.project}-engineers"
}

resource "databricks_user" "daily" {
  user_name    = var.daily_user_email
  display_name = var.daily_user_display_name

  # A nivel cuenta el destroy por defecto solo DESACTIVA al usuario, y el siguiente apply
  # choca con "already exists". Como el workspace se destruye y recrea cada sesión, se borra.
  disable_as_user_deletion = false
}

resource "databricks_group_member" "daily" {
  group_id  = databricks_group.engineers.id
  member_id = databricks_user.daily.id
}

# Acceso al workspace como USER, no ADMIN. Lo que pueda leer o escribir en el lake se
# define después con grants de Unity Catalog sobre el grupo.
resource "databricks_mws_permission_assignment" "engineers" {
  workspace_id = var.workspace_id
  principal_id = databricks_group.engineers.id
  permissions  = ["USER"]
}

# Estar asignado al workspace no alcanza: hace falta el entitlement "Workspace access" para
# entrar a la UI (los workspaces nuevos no se lo dan ni al grupo `users`). Se da al grupo.
# Sin allow_cluster_create: los clusters los define el código (Asset Bundles), no la UI.
resource "databricks_entitlements" "engineers" {
  provider = databricks.workspace

  group_id              = databricks_group.engineers.id
  workspace_access      = true
  databricks_sql_access = true

  depends_on = [databricks_mws_permission_assignment.engineers]
}
