# ─────────────────────────────────────────────────────────────────────────────
# DATABRICKS · identidad del CI/CD del entrenamiento (nivel cuenta)
#
# El workflow "Training" despliega el bundle y lanza el job como este SP (train_pipeline_mlops.md).
# Sin secreto: GitHub se autentica por OIDC y la federation policy solo acepta la rama main.
#
# SP y policy no dependen del workspace: sobreviven al destroy de cada sesión y el client ID que
# guarda GitHub no cambia. La membresía en engineers sí se recrea, porque el grupo se recrea.
# ─────────────────────────────────────────────────────────────────────────────

resource "databricks_service_principal" "cicd" {
  display_name = "${var.project}-cicd"
}

resource "databricks_service_principal_federation_policy" "github" {
  service_principal_id = tonumber(databricks_service_principal.cicd.id)
  policy_id            = "github-main"
  oidc_policy = {
    issuer    = "https://token.actions.githubusercontent.com"
    audiences = [var.databricks_account_id]
    subject   = "${var.github_subject_prefix}:ref:refs/heads/main"
  }
}

# Engineers ya tiene lo que hace falta para desplegar: acceso al workspace, CAN_USE de las policies,
# CAN_MANAGE del bundle y el rol para usar el SP de jobs como run_as.
resource "databricks_group_member" "engineers" {
  group_id  = var.engineers_group_id
  member_id = databricks_service_principal.cicd.id
}
