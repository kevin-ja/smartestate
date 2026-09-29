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
export BUNDLE_VAR_ref_root="$SMARTESTATE_REF_ROOT"
export BUNDLE_VAR_jobs_sp_id="$SMARTESTATE_JOBS_SP_ID"

# La caché local (.databricks/bundle/) guarda IDs de jobs y policies del workspace donde se
# desplegó, pero no su host. Cada sesión recrea el workspace: con IDs viejos el deploy falla con
# 403 PERMISSION_DENIED. Se anota el host y, si cambió, se borra la caché antes de correr.
cache_dir="$bundle_dir/.databricks/bundle"
host_marker="$bundle_dir/.databricks/workspace_host"
if [[ -d "$cache_dir" && "$(cat "$host_marker" 2>/dev/null)" != "$DATABRICKS_HOST" ]]; then
  rm -rf "$cache_dir"
  echo "Caché del bundle borrada: era de otro workspace." >&2
fi
mkdir -p "$bundle_dir/.databricks"
echo "$DATABRICKS_HOST" > "$host_marker"

cd "$bundle_dir"
exec databricks bundle "$@"
