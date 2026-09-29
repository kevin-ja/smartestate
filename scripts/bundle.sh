#!/usr/bin/env bash
# Corre `databricks bundle <args>` sobre el bundle de un pipeline (bundles/<pipeline>/), con el
# host y las variables de .env solo para este proceso. Así nunca queda un DATABRICKS_* exportado
# en la terminal donde corre Terraform.
#
# Uso:  bash scripts/bundle.sh data validate
#       bash scripts/bundle.sh data deploy -t dev
#       bash scripts/bundle.sh data run ingestion -t dev --params ingest_date=2026-09-26
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"

pipeline="${1:?Uso: bash scripts/bundle.sh <pipeline> <args de databricks bundle>}"
shift
bundle_dir="$repo/bundles/$pipeline"
[[ -f "$bundle_dir/databricks.yml" ]] || { echo "No existe $bundle_dir/databricks.yml" >&2; exit 1; }

set -a; . "$repo/.env"; set +a
# Perfil explícito del usuario diario: sin él, el CLI usa el default_profile de ~/.databrickscfg,
# que es el SP de IaC (admin de cuenta). Se crea en cada workspace nuevo con:
#   databricks auth login --host "$SMARTESTATE_WORKSPACE_HOST" --profile smartestate-dev
export DATABRICKS_CONFIG_PROFILE=smartestate-dev
export DATABRICKS_HOST="$SMARTESTATE_WORKSPACE_HOST"
export BUNDLE_VAR_raw_root="$SMARTESTATE_RAW_ROOT"
export BUNDLE_VAR_jobs_sp_id="$SMARTESTATE_JOBS_SP_ID"

cd "$bundle_dir"
exec databricks bundle "$@"
