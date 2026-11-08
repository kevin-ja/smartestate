# ─────────────────────────────────────────────────────────────────────────────
# GITHUB · variables de Actions del workflow "Training" (train_pipeline_mlops.md)
#
# Terraform las escribe en cada apply desde sus outputs: nadie corre `gh secret set`.
# Variables, no secretos: son IDs y una URL; lo que protege es la federation policy (solo main).
#
# DATABRICKS_HOST depende del workspace: se borra con él en shut_down.sh, así el workflow falla
# claro ("workspace apagado") en vez de apuntar a una URL vieja. Los IDs de los SP persisten.
# ─────────────────────────────────────────────────────────────────────────────

resource "github_actions_variable" "host" {
  repository    = var.repository
  variable_name = "DATABRICKS_HOST"
  value         = var.workspace_url
}

resource "github_actions_variable" "cicd_client_id" {
  repository    = var.repository
  variable_name = "DATABRICKS_CICD_CLIENT_ID"
  value         = var.cicd_client_id
}

resource "github_actions_variable" "jobs_sp_id" {
  repository    = var.repository
  variable_name = "DATABRICKS_JOBS_SP_ID"
  value         = var.jobs_sp_id
}
