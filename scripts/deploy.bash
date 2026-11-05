#!/usr/bin/env bash
# Despliegue manual de la API en la EC2 (inference_pipeline_design.md). Se corre DENTRO de la instancia, por SSM.
# Las credenciales salen del instance profile. Se puede repetir: reemplaza el contenedor anterior.
#
# Uso: sudo bash deploy.bash <run_id> [image_tag]
#   sudo bash deploy.bash 756577598248164 test

set -euo pipefail

RUN_ID="${1:?Falta el run_id. Uso: sudo bash deploy.bash <run_id> [image_tag]}"
IMAGE_TAG="${2:-test}"

REGION="us-east-1"
CONTAINER="inference"
API_PORT=8000
HEALTH_ATTEMPTS=30
HEALTH_WAIT_S=2

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/smartestate-inference:${IMAGE_TAG}"
RUNS_ROOT="s3://smartestate-lake-${ACCOUNT_ID}/runs"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Correr con sudo." >&2
  exit 1
fi

echo "1/4 · Docker"
if ! command -v docker >/dev/null; then
  dnf install -y docker
fi
systemctl enable --now docker

echo "2/4 · Imagen ${IMAGE}"
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"
docker pull "${IMAGE}"

echo "3/4 · Contenedor con run_id ${RUN_ID}"
docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
docker run -d --name "${CONTAINER}" --restart always -p "${API_PORT}:${API_PORT}" \
  --log-driver awslogs \
  --log-opt awslogs-group=/smartestate/inference \
  --log-opt awslogs-region="${REGION}" \
  -e RUN_ID="${RUN_ID}" \
  -e RUNS_ROOT="${RUNS_ROOT}" \
  -e AWS_REGION="${REGION}" \
  "${IMAGE}"

echo "4/4 · Esperando /health"
for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
  if curl -fsS "http://localhost:${API_PORT}/health"; then
    echo
    echo "Listo: API arriba con run_id ${RUN_ID}."
    exit 0
  fi
  sleep "${HEALTH_WAIT_S}"
done

echo "La API no respondió en /health. Últimos logs:" >&2
docker logs --tail 50 "${CONTAINER}" >&2
exit 1
