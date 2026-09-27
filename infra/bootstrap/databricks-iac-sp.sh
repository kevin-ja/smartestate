#!/usr/bin/env bash
# Bootstrap de identidad en Databricks — se corre UNA vez, con el perfil account admin.
#
# Crea el service principal `smartestate-databricks` (el que corre Terraform), le da account admin
# y le genera un secreto OAuth M2M. El secreto NO se imprime: va directo a un perfil nuevo de
# ~/.databrickscfg (fuera del repo, permisos 600). Mismo papel que iac-smartestate-data.json en AWS.
#
# Uso:  bash infra/bootstrap/databricks-iac-sp.sh
set -euo pipefail

ADMIN_PROFILE="smartestate-account"   # perfil humano admin (OAuth U2M); solo para este bootstrap
SP_NAME="smartestate-databricks"
NEW_PROFILE="smartestate-databricks"  # perfil que usará Terraform
SECRET_LIFETIME="7776000s"            # 90 días; rotar al vencer
CFG="$HOME/.databrickscfg"

if grep -q "^\[$NEW_PROFILE\]" "$CFG" 2>/dev/null; then
  echo "El perfil [$NEW_PROFILE] ya existe en $CFG. Nada que hacer." >&2
  exit 1
fi

HOST="https://accounts.cloud.databricks.com"
# Valores locales desde .env (plantilla en .env.example)
set -a; . "$(dirname "$0")/../../.env"; set +a
ACCOUNT_ID="${TF_VAR_databricks_account_id:?Falta TF_VAR_databricks_account_id en .env}"

# 1. Service principal (reutiliza si ya existe, para poder reintentar)
SP_JSON=$(databricks account service-principals list -p "$ADMIN_PROFILE" -o json \
  --filter "displayName eq \"$SP_NAME\"" | jq '.[0] // empty')
if [ -z "$SP_JSON" ]; then
  SP_JSON=$(databricks account service-principals create -p "$ADMIN_PROFILE" -o json \
    --display-name "$SP_NAME" --active)
  echo "Service principal creado."
else
  echo "Service principal ya existía; se reutiliza."
fi
SP_ID=$(echo "$SP_JSON" | jq -r '.id')
APP_ID=$(echo "$SP_JSON" | jq -r '.applicationId')

# 2. Rol account admin (necesario para databricks_mws_*: no hay permiso más fino)
databricks account service-principals patch "$SP_ID" -p "$ADMIN_PROFILE" --json '{
  "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
  "Operations": [{"op": "add", "path": "roles", "value": [{"value": "account_admin"}]}]
}' > /dev/null
echo "Rol account_admin asignado."

# 3. Secreto OAuth → directo al perfil, sin pasar por la terminal
SECRET=$(databricks account service-principal-secrets create "$SP_ID" -p "$ADMIN_PROFILE" -o json \
  --lifetime "$SECRET_LIFETIME" | jq -r '.secret')

touch "$CFG" && chmod 600 "$CFG"
cat >> "$CFG" <<EOF

[$NEW_PROFILE]
host          = $HOST
account_id    = $ACCOUNT_ID
client_id     = $APP_ID
client_secret = $SECRET
EOF
unset SECRET
echo "Perfil [$NEW_PROFILE] escrito en $CFG."

# 4. Verificación: el SP se autentica solo y ve la cuenta
databricks account workspaces list -p "$NEW_PROFILE" > /dev/null && echo "OK: el SP se autentica."
echo "SP id=$SP_ID  application_id=$APP_ID"
