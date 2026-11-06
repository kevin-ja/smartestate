# Operación de la inferencia y su CI/CD (inference_pipeline_design.md). Se corre desde la raíz del repo.
# Los valores salen de .env (plantilla: .env.example). Cada apply muestra su plan y pide `yes`.
#
# Desde cero: make check-prerequisites → make state-backend → make init → make apply-cicd → make apply-inference
#             → make set-production-run RUN_ID=<run_id> → make github-setup → make check

SHELL := /bin/bash
.DEFAULT_GOAL := help

PROFILE ?= smartestate
REGION ?= us-east-1
REPO ?= kevin-ja/smartestate
ENVIRONMENTS_API := repos/$(REPO)/environments

# Terraform en infra/, con .env cargado solo dentro de un subshell: exportarlo en tu terminal pisaría los perfiles.
TF_INFRA = cd infra && set -a && . ../.env && set +a && AWS_PROFILE=$(PROFILE) terraform

.PHONY: help check-env check-prerequisites check state-backend init plan-cicd apply-cicd plan-inference apply-inference \
	destroy-inference set-production-run github-setup test test-docker

help: ## Muestra esta ayuda
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | column -t -s "$$(printf '\t')"

check-env:
	@test -f .env || { echo "Falta .env (plantilla: .env.example)." >&2; exit 1; }

check-prerequisites: ## Comprueba herramientas, .env, identidad, políticas y OIDC. Solo lee
	@PROFILE=$(PROFILE) REGION=$(REGION) REPO=$(REPO) bash scripts/check_inference.bash prerequisites

check: ## Comprueba estado, CI/CD, GitHub, corrida en producción, EC2 y API. Solo lee
	@PROFILE=$(PROFILE) REGION=$(REGION) REPO=$(REPO) bash scripts/check_inference.bash deployment

state-backend: ## Crea el bucket de estado y genera infra/backend.hcl
	cd infra/state_backend && AWS_PROFILE=$(PROFILE) terraform init && AWS_PROFILE=$(PROFILE) terraform apply
	terraform -chdir=infra/state_backend output -raw backend_config > infra/backend.hcl.tmp
	mv infra/backend.hcl.tmp infra/backend.hcl

init: check-env ## Conecta infra/ con el backend de S3
	@test -f infra/backend.hcl || { echo "Falta infra/backend.hcl: corre make state-backend." >&2; exit 1; }
	$(TF_INFRA) init -backend-config=backend.hcl


# útil cuando solo quieres ver qué cambiaría, sin riesgo de aplicar nada por error. 
# También sirve para compartir la salida o comprobar que no haya cambios pendientes.
plan-cicd: check-env ## Revisa los roles y buckets del CI/CD
	$(TF_INFRA) plan -target=module.cicd

# Crea en AWS lo que GitHub Actions necesita para entrar y desplegar
# (roles y buckets del CI/CD)
apply-cicd: check-env
	$(TF_INFRA) apply -target=module.cicd

plan-inference: check-env ## Revisa la EC2 y su red
	$(TF_INFRA) plan -target=module.inference_service

# Levanta en AWS la infraestructura donde correrá la API ((red, firewall, IP fija, rol, ECR y logs))
# Muestra el plan y aplica solo si respondes "yes". La EC2 empieza a cobrar desde aquí.
apply-inference: check-env ## Levanta la EC2 y su red. Desde aquí cobra
	$(TF_INFRA) apply -target=module.inference_service

destroy-inference: check-env ## Apaga la inferencia y borra la imagen de ECR y los logs. No pide confirmación
	@test -f shut_down_inference_infra.sh || { echo "Falta shut_down_inference_infra.sh: es local y no se versiona." >&2; exit 1; }
	bash shut_down_inference_infra.sh

# Conserva image_tag si ya existe. Solo registra el valor: no despliega nada.
set-production-run: ## Registra el run_id en producción: make set-production-run RUN_ID=<run_id>
	@[[ "$(RUN_ID)" =~ ^[0-9]+$$ ]] || { echo "Uso: make set-production-run RUN_ID=<número>" >&2; exit 1; }
	@set -euo pipefail; \
	account_id="$$(aws sts get-caller-identity --profile $(PROFILE) --query Account --output text)"; \
	pointer="s3://smartestate-lake-$$account_id/runs/production.json"; \
	current="$$(aws s3 cp "$$pointer" - --profile $(PROFILE) 2>/dev/null || echo '{}')"; \
	jq --arg run_id "$(RUN_ID)" '.run_id = $$run_id' <<< "$$current" \
		| aws s3 cp - "$$pointer" --profile $(PROFILE) --content-type application/json; \
	echo "runs/production.json → run_id $(RUN_ID)"

# Los secretos van por stdin: como argumento quedarían a la vista en la lista de procesos.
github-setup: check-env ## Configura los secretos y environments de GitHub. Se puede repetir
	@set -euo pipefail; set -a; . ./.env; set +a; \
	: "$${TF_VAR_inference_allowed_cidrs:?Falta TF_VAR_inference_allowed_cidrs en .env}"; \
	gh secret set AWS_ACCOUNT_ID --repo $(REPO) <<< "$$TF_VAR_account_id"; \
	gh secret set DATABRICKS_ACCOUNT_ID --repo $(REPO) <<< "$$TF_VAR_databricks_account_id"; \
	gh secret set DAILY_USER_EMAIL --repo $(REPO) <<< "$$TF_VAR_daily_user_email"; \
	gh secret set INFERENCE_ALLOWED_CIDRS --repo $(REPO) <<< "$$TF_VAR_inference_allowed_cidrs"; \
	branches='"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}'; \
	reviewer="$$(gh api user --jq .id)"; \
	echo "{$$branches}" | gh api -X PUT "$(ENVIRONMENTS_API)/terraform-plan" --input - >/dev/null; \
	echo "{\"reviewers\":[{\"type\":\"User\",\"id\":$$reviewer}],$$branches}" \
		| gh api -X PUT "$(ENVIRONMENTS_API)/production" --input - >/dev/null; \
	for environment in terraform-plan production; do \
		existing="$$(gh api "$(ENVIRONMENTS_API)/$$environment/deployment-branch-policies" \
			--jq '[.branch_policies[].name] | index("main")')"; \
		if [[ "$$existing" == "null" ]]; then \
			gh api -X POST "$(ENVIRONMENTS_API)/$$environment/deployment-branch-policies" -f name=main >/dev/null; \
		fi; \
	done; \
	echo "GitHub listo: 4 secretos y environments terraform-plan y production, solo desde main."

test: ## Corre los tests de la inferencia
	./ss/bin/python -m pytest tests/test_inference.py tests/test_inference_integration.py -q -p no:cacheprovider

test-docker: ## Corre todos los tests en Docker, con Spark
	docker build -q -f tests/Dockerfile -t smartestate-tests .
	docker run --rm smartestate-tests
