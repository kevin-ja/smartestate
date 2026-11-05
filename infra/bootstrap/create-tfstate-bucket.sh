#!/usr/bin/env bash
# Crea el bucket privado del estado de Terraform. Va fuera de Terraform: debe existir antes de migrar el estado.
# Se puede repetir sin riesgo. Requiere la política iac-smartestate-cicd.
#
# Uso (desde la raíz del repo): bash infra/bootstrap/create-tfstate-bucket.sh
set -euo pipefail

PROFILE=smartestate
REGION=us-east-1

account_id="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
bucket="smartestate-tfstate-${account_id}"

if aws s3api head-bucket --bucket "$bucket" --profile "$PROFILE" 2>/dev/null; then
  echo "Ya existe: $bucket"
else
  # us-east-1 no acepta LocationConstraint.
  aws s3api create-bucket --bucket "$bucket" --region "$REGION" --profile "$PROFILE" >/dev/null
  echo "Creado: $bucket"
fi

aws s3api put-public-access-block --bucket "$bucket" --profile "$PROFILE" \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket "$bucket" --profile "$PROFILE" \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$bucket" --profile "$PROFILE" \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-tagging --bucket "$bucket" --profile "$PROFILE" \
  --tagging 'TagSet=[{Key=Project,Value=smartestate},{Key=Stage,Value=cicd},{Key=ManagedBy,Value=script}]'

echo "Listo: privado, versionado y cifrado."
