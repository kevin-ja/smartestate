#!/usr/bin/env bash
# Actualiza el workspace_id guardado en el estado de uc/ al del workspace recién creado.
#
# El provider de Databricks (>= 1.134) guarda en cada recurso `provider_config.workspace_id` y
# falla con "workspace_id mismatch" si el workspace cambió. Los objetos de Unity Catalog son del
# metastore, no del workspace, así que reescribir ese id es correcto. Correr tras cada
# `terraform apply` en infra/ y antes del de uc/.
#
# Uso:  bash infra/uc/sync-workspace-id.sh
set -euo pipefail
cd "$(dirname "$0")"

NEW_ID=$(terraform -chdir=.. output -raw databricks_workspace_id)

terraform state pull > terraform.tfstate.pre-sync   # respaldo (ignorado por git)
jq --arg id "$NEW_ID" \
  '.serial += 1 | (.. | objects | select(has("provider_config")) | .provider_config[]?.workspace_id) |= $id' \
  terraform.tfstate.pre-sync > terraform.tfstate.synced
terraform state push terraform.tfstate.synced
rm terraform.tfstate.synced

echo "Estado de uc/ apunta al workspace $NEW_ID. Respaldo: infra/uc/terraform.tfstate.pre-sync"
