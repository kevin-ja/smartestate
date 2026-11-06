#!/usr/bin/env bash
# Comprobaciones de la inferencia y su CI/CD (inference_pipeline_design.md). Solo lee: no crea ni cambia nada.
# Termina con error si alguna falla.
#
# Uso: bash scripts/check_inference.bash prerequisites   Antes de empezar
#      bash scripts/check_inference.bash deployment      Después de aprovisionar o desplegar

set -uo pipefail

MODE="${1:-}"
PROFILE="${PROFILE:-smartestate}"
REGION="${REGION:-us-east-1}"
REPO="${REPO:-kevin-ja/smartestate}"
IAM_USER=smartestate-data
OIDC_HOST=token.actions.githubusercontent.com
NAME=smartestate-inference
API_PORT=8000

failures=0

pass() { printf '  ✓ %s\n' "$1"; }
fail() { printf '  ✗ %s\n' "$1"; failures=$((failures + 1)); }
note() { printf '  · %s\n' "$1"; }
title() { printf '\n%s\n' "$1"; }

aws_p() { aws "$@" --profile "$PROFILE" --region "$REGION" --no-cli-pager 2>/dev/null; }

# Pasa si el comando termina bien. Uso: check "descripción" "qué hacer si falla" comando...
check() {
  local description=$1 fix=$2
  shift 2
  if "$@" >/dev/null 2>&1; then
    pass "$description"
  else
    fail "$description → $fix"
  fi
}

load_env() {
  if [[ ! -f .env ]]; then
    fail ".env no existe → cópialo de .env.example y complétalo"
    return 1
  fi
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
}

check_env_vars() {
  local variable
  for variable in TF_VAR_account_id TF_VAR_databricks_account_id TF_VAR_daily_user_email TF_VAR_inference_allowed_cidrs; do
    if [[ -n "${!variable:-}" ]]; then
      pass "$variable está en .env"
    else
      fail "$variable falta en .env"
    fi
  done
}

check_identity() {
  local arn expected
  expected="arn:aws:iam::${TF_VAR_account_id:-}:user/${IAM_USER}"
  arn="$(aws_p sts get-caller-identity --query Arn --output text)"
  if [[ "$arn" == "$expected" ]]; then
    pass "El perfil $PROFILE es $IAM_USER, en la cuenta de .env"
  else
    fail "El perfil $PROFILE es '${arn:-sin acceso}'; se esperaba $expected"
  fi
}

# Sin permiso para listar las políticas del usuario, se comprueba que funcione lo que cada una autoriza.
check_policies() {
  check "Política iac-smartestate-data activa" "créala y adjúntala a $IAM_USER" \
    aws_p ec2 describe-vpcs --max-results 5
  check "Política iac-smartestate-inference activa" "créala y adjúntala a $IAM_USER" \
    aws_p iam list-open-id-connect-providers
}

check_oidc_provider() {
  local arn exists=false flag audiences
  arn="arn:aws:iam::${TF_VAR_account_id:-}:oidc-provider/${OIDC_HOST}"
  if aws_p iam list-open-id-connect-providers --query 'OpenIDConnectProviderList[].Arn' --output text | tr '\t' '\n' | grep -qx "$arn"; then
    exists=true
  fi
  flag="${TF_VAR_create_github_oidc_provider:-true}"

  if [[ "$exists" == true && "$flag" != false ]]; then
    fail "El proveedor OIDC ya existe → agrega TF_VAR_create_github_oidc_provider=false a .env"
  elif [[ "$exists" == false && "$flag" == false ]]; then
    fail "El proveedor OIDC no existe → quita TF_VAR_create_github_oidc_provider=false de .env"
  elif [[ "$exists" == true ]]; then
    pass "El proveedor OIDC ya existe y .env lo reutiliza"
  else
    pass "El proveedor OIDC no existe: Terraform lo creará"
  fi

  if [[ "$exists" == true ]]; then
    audiences="$(aws_p iam get-open-id-connect-provider --open-id-connect-provider-arn "$arn" --query ClientIDList --output text)"
    if grep -qw sts.amazonaws.com <<< "$audiences"; then
      pass "El proveedor OIDC acepta sts.amazonaws.com"
    else
      fail "El proveedor OIDC no acepta sts.amazonaws.com → cambiarlo puede afectar a otro proyecto; revísalo antes"
    fi
  fi
}

check_authorized_ip() {
  local my_ip
  my_ip="$(curl -s --max-time 5 https://checkip.amazonaws.com | tr -d '[:space:]')"
  if [[ -z "$my_ip" ]]; then
    note "No se pudo consultar tu IP pública"
  elif [[ "${TF_VAR_inference_allowed_cidrs:-}" == *"$my_ip/32"* ]]; then
    pass "Tu IP actual está autorizada en .env"
  else
    fail "Tu IP actual no está en TF_VAR_inference_allowed_cidrs → actualiza .env y corre make apply-inference y make github-setup"
  fi
}

run_prerequisites() {
  local tool
  title "Herramientas"
  for tool in terraform aws jq gh docker; do
    check "$tool instalado" "instálalo" command -v "$tool"
  done
  check "GitHub CLI con sesión" "corre gh auth login" gh auth status
  check "Plugin de SSM instalado" "corre brew install --cask session-manager-plugin" command -v session-manager-plugin

  title "Configuración"
  load_env || return
  check_env_vars
  check_authorized_ip

  title "AWS"
  check_identity
  check_policies
  check_oidc_provider
}

check_state_backend() {
  local bucket="smartestate-tfstate-${TF_VAR_account_id}" versioning
  check "infra/backend.hcl existe" "corre make state-backend" test -f infra/backend.hcl
  check "Bucket de estado existe" "corre make state-backend" aws_p s3api head-bucket --bucket "$bucket"
  versioning="$(aws_p s3api get-bucket-versioning --bucket "$bucket" --query Status --output text)"
  if [[ "$versioning" == Enabled ]]; then
    pass "Bucket de estado versionado"
  else
    fail "Bucket de estado sin versionado → corre make state-backend"
  fi
  check "Bucket de estado privado" "corre make state-backend" \
    aws_p s3api get-public-access-block --bucket "$bucket"
  check "Estado de infra/ en S3" "corre make init" \
    aws_p s3api head-object --bucket "$bucket" --key infra/terraform.tfstate
}

check_cicd() {
  local bucket role policies policy
  for bucket in tfplans ansible; do
    check "Bucket $bucket existe" "corre make apply-cicd" \
      aws_p s3api head-bucket --bucket "smartestate-${bucket}-${TF_VAR_account_id}"
  done
  for role in smartestate-github-deploy smartestate-github-terraform; do
    check "Rol $role existe" "corre make apply-cicd" aws_p iam get-role --role-name "$role"
  done
  policies="$(aws_p iam list-attached-role-policies --role-name smartestate-github-terraform \
    --query 'AttachedPolicies[].PolicyName' --output text)"
  for policy in iac-smartestate-data iac-smartestate-inference; do
    if grep -qw "$policy" <<< "$policies"; then
      pass "Rol de Terraform con $policy"
    else
      fail "Rol de Terraform sin $policy → corre make apply-cicd"
    fi
  done
}

check_github() {
  local secrets secret environment
  secrets="$(gh secret list --repo "$REPO" --json name --jq '.[].name' 2>/dev/null)"
  for secret in AWS_ACCOUNT_ID DATABRICKS_ACCOUNT_ID DAILY_USER_EMAIL INFERENCE_ALLOWED_CIDRS; do
    if grep -qx "$secret" <<< "$secrets"; then
      pass "Secreto $secret"
    else
      fail "Falta el secreto $secret → corre make github-setup"
    fi
  done
  for environment in terraform-plan production; do
    check "Environment $environment" "corre make github-setup" \
      gh api "repos/$REPO/environments/$environment"
  done
}

check_production_run() {
  local pointer run_id
  pointer="$(aws_p s3 cp "s3://smartestate-lake-${TF_VAR_account_id}/runs/production.json" -)"
  run_id="$(jq -r '.run_id // empty' <<< "${pointer:-"{}"}" 2>/dev/null)"
  if [[ ! "$run_id" =~ ^[0-9]+$ ]]; then
    fail "runs/production.json sin run_id → corre make set-production-run RUN_ID=<run_id>"
    return
  fi
  pass "En producción: run_id $run_id"
  check "Tabla de features de esa corrida existe" "la limpieza de T3 pudo borrarla; elige otra corrida" \
    aws_p dynamodb get-item --table-name "spots_fs_online_${run_id}" \
    --key '{"_feature_store_internal__primary_keys":{"S":"[0]"}}' --projection-expression sector_id
  check "GeoIndex de esa corrida existe" "la limpieza de T3 pudo borrarlo; elige otra corrida" \
    aws_p s3api head-object --bucket "smartestate-lake-${TF_VAR_account_id}" --key "runs/${run_id}/geo_index.joblib"
}

check_service() {
  local instance_state public_ip images
  check "Repositorio ECR existe" "corre make apply-inference" \
    aws_p ecr describe-repositories --repository-names "$NAME"

  images="$(aws_p ecr describe-images --repository-name "$NAME" --query 'length(imageDetails)' --output text)"
  if [[ "${images:-0}" =~ ^[1-9] ]]; then
    pass "ECR tiene $images imagen(es)"
  else
    fail "ECR no tiene imágenes → el primer push a main construye la primera"
  fi

  instance_state="$(aws_p ec2 describe-instances \
    --filters "Name=tag:Name,Values=$NAME" Name=instance-state-name,Values=pending,running,stopping,stopped \
    --query 'Reservations[0].Instances[0].State.Name' --output text)"
  if [[ "$instance_state" == running ]]; then
    pass "EC2 encendida (cobra)"
  else
    fail "EC2 no está encendida (estado: ${instance_state:-ninguno}) → corre make apply-inference"
    return
  fi

  public_ip="$(aws_p ec2 describe-addresses --filters "Name=tag:Name,Values=$NAME" --query 'Addresses[0].PublicIp' --output text)"
  check_authorized_ip
  if curl -fsS --max-time 5 "http://${public_ip}:${API_PORT}/health" >/dev/null 2>&1; then
    pass "API responde en /health"
  else
    fail "API no responde en /health → antes del primer despliegue es normal; si no, revisa tu IP y el contenedor"
  fi
}

run_deployment() {
  load_env || return
  title "Estado de Terraform"
  check_state_backend
  title "CI/CD"
  check_cicd
  title "GitHub"
  check_github
  title "Corrida en producción"
  check_production_run
  title "Servicio"
  check_service
}

case "$MODE" in
  prerequisites) run_prerequisites ;;
  deployment) run_deployment ;;
  *)
    echo "Uso: bash scripts/check_inference.bash prerequisites|deployment" >&2
    exit 2
    ;;
esac

echo
if ((failures > 0)); then
  echo "$failures comprobación(es) fallaron."
  exit 1
fi
echo "Todo en orden."
