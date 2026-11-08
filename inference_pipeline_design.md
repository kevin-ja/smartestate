# Diseño del pipeline de inferencia

Bitácora de creación e implementación del serving. El flujo por clic está en `arch_inference.md`; la arquitectura, en `new_arch.md` §4.

## Tecnología

| Recurso de AWS | Rol en la inferencia |
|---|---|
| EC2 | Corre la API en un contenedor Docker, siempre encendida |
| ECR | Guarda la imagen Docker que descarga EC2 |
| DynamoDB | Entrega las features de los spots en cada clic |
| S3 | Guarda el GeoIndex y los parámetros que la API carga al arrancar |
| VPC con subnet pública | Da a EC2 su red y su salida a internet |
| VPC gateway endpoints | Conectan EC2 con DynamoDB y S3 sin pasar por internet |
| Security group | Deja entrar a la API solo a las IP autorizadas |
| Elastic IP | Da a la API una dirección pública fija |
| IAM instance profile | Da a EC2 permisos de AWS sin guardar llaves en la imagen |
| SSM Session Manager | Permite entrar a administrar EC2 sin abrir el puerto 22 |
| CloudWatch Logs | Guarda los logs de la API |

## Decisiones

- **EC2 en vez de Fargate (Kevin, 2026-11-05):** Kevin prefiere administrar la máquina (parches, Docker, reinicios y despliegues). Con una sola instancia, si se cae, el servicio queda caído hasta que se recupere.
- **Stack de CI/CD (Kevin, 2026-11-05):**

  | Pieza | Rol |
  |---|---|
  | GitHub Actions | Orquesta los workflows |
  | GitHub Environments | Pide aprobación antes de un `terraform apply` |
  | OIDC de GitHub + rol IAM | Da acceso a AWS sin guardar llaves |
  | pytest | Corre los tests |
  | Docker | Empaqueta la imagen |
  | ECR | Guarda las imágenes |
  | Ansible por SSM (`aws_ssm`) | Despliega en EC2 sin abrir el puerto 22; usa un bucket de S3 |
  | Terraform | Aprovisiona la infraestructura |
  | Backend S3 de Terraform | Guarda el estado fuera de la laptop |
- **Identidad OIDC con ID (Kevin, 2026-11-05):** el repo usa `use_immutable_subject`, así que GitHub envía `repo:dueño@id/nombre@id`. Los roles confían en ese formato: un repo recreado con el mismo nombre no heredaría el acceso.
- **Despliegue automático (Kevin, 2026-11-05):** push a `main` → tests unitarios y de integración → si todos pasan, se despliega y se revisa `/health`. Si alguno falla, no se despliega y se avisa.
- **`run_id` en producción (Kevin, 2026-11-05):** vive en un archivo de un bucket S3 privado, porque MLflow se apaga con el workspace. El CD usa ese valor por defecto y lo actualiza tras cada despliegue exitoso. Para un rollback, se indica otro `run_id` al lanzar el workflow. La limpieza de T3 debe respetar ese `run_id`.
- **Confidencialidad del CI/CD (Kevin, 2026-11-05):** el repo es público, así que los logs solo muestran el resumen del plan. El plan completo, el estado de Terraform y lo de Ansible van en buckets S3 privados y separados.
- **Fuentes de cambio del CI/CD (Kevin, 2026-11-05):** dos.
  - **Código:** API, parámetros del servicio, dependencias, Dockerfile e infraestructura. Lo de la API reconstruye la imagen; lo de `infra/` pasa solo por `terraform plan` y `apply`, sin reconstruir la imagen.
  - **Artefactos:** feature store, K-means, GeoIndex y demás, juntos, con el mismo `run_id`.
  - El rollback no es una fuente de cambio: es una operación aparte.
- **Red sin NAT ni ALB (Kevin, 2026-11-05):** VPC propia con subnet pública. Es lo más barato, y con una sola instancia el ALB no reparte tráfico. El security group limita el acceso a las IP autorizadas.
- **Administración por SSM (Kevin, 2026-11-05):** la máquina se administra con Session Manager, sin puerto 22 ni llaves SSH. Se abre sesión solo en instancias con `Project=smartestate`.
- **Política de Terraform (2026-11-05):** `infra/bootstrap/iac-smartestate-data.json` se amplió para este módulo. Lo que modifica instancias, firewalls o rutas exige el tag `Project=smartestate`, porque la cuenta se comparte con otros proyectos.
- **Parámetros de los filtros y de la respuesta (Kevin, 2026-11-05):** valores iniciales, por calibrar. Viven en `src/inference/recommender.py`.

  | Parámetro | Valor |
  |---|---|
  | Radio `r` | 5 → 10 → 15 km; se detiene al juntar el mínimo |
  | Tope del radio | 15 km |
  | Mínimo de candidatos | 50 |
  | Tope de candidatos | 300, los más cercanos |
  | N por defecto | 10 |
  | Tope de `n` | 50 |

  Medido con el GeoIndex de `756577598248164`, clic por clic sobre sus 1.285 candidatos (solo filtros):

  | Sector | Candidatos | Mediana tras filtros | Con <10 | Con 0 |
  |---|---:|---:|---:|---:|
  | Industrial (9) | 625 | 37 | 24 % | 3 % |
  | Office (11) | 484 | 193 | 7 % | 2 % |
  | Retail (12) | 160 | 15 | 44 % | 14 % |
  | Land (15) | 16 | 0 | 100 % | 88 % |
  | **Total** | **1.285** | **52** | **21 %** | **5 %** |

- **Forma de `f(r)` (Kevin, 2026-11-05):** la documentación pide tramos, sin fórmula. Se usa la recta `f(0,5) = 1,1` → `f(1) = 1` → `f(2) = 0,7`, plana fuera de ese rango. Se eligió 0,5 y 2 porque el EDA ya usa ese rango para medir la saturación.
- **Casos que la documentación no cubría:**
  - Sin señal común, `sim = 0`.
  - `sim` se recorta a `[0, 1]`: un coseno negativo subiría al multiplicarlo por un factor menor que 1.
  - Sin precio comparable, `f = 1`.
  - Sin `cluster_id`, no se penaliza por diversidad.
  - Un clic sin coordenadas, sector o modalidad responde `200` con lista vacía.
  - Si tras 3 intentos DynamoDB no entrega parte de una tanda, se responde con lo leído y queda un aviso en el log.

## Pendientes

1. **Champion:** falta la regla para elegir qué corrida nueva pasa a producción.
2. **EC2:** falta definir cómo se recupera si se cae la máquina. Si solo se cae el contenedor, lo levanta `--restart always`.
3. **Latencia: cerrada (2026-11-07).** Medida con la prueba de carga (`tests/performance/`) sobre el tiempo del servidor (`Server-Timing`), sin la red: p50 24 ms y p95 290 ms con 1 usuario; p95 470 ms con 20, el máximo dentro del SLO. Detalle: `docs/performance_report.md`.

## Procedimiento

1. ✅ Parámetros de los filtros y de la respuesta.
2. ✅ API en FastAPI (`src/inference/`), con tests sin AWS (`tests/test_inference.py`).
3. ✅ Prueba contra la tabla real de DynamoDB, desde la laptop.
4. ✅ Dockerfile de la imagen (`src/inference/Dockerfile`), construida y subida a ECR.
5. ✅ Terraform: `infra/modules/inference_service` aplicado (`t3.small`).
6. ✅ Despliegue con `scripts/deploy.bash` (`756577598248164`, imagen `test`): `/health` y `/recommendations` responden desde afuera.
7. Medición de latencia dentro de EC2.
8. CI/CD: código escrito y validado localmente; falta la puesta en marcha y la primera corrida en GitHub.

## Comandos

El `Makefile` de la raíz agrupa la operación (`make help`). Desde cero, tras crear la primera política IAM:

```bash
make check-prerequisites
make state-backend
make init
make apply-cicd
make apply-inference
make set-production-run RUN_ID=<run_id>
make github-setup
make check
```

`make check-prerequisites` y `make check` solo leen. Las políticas de `smartestate-data` se comprueban por lo que autorizan, porque ese usuario no puede listarlas.

Lo que sigue manual, a propósito: responder `yes` en cada `apply`, crear la primera política IAM y llenar `.env`. `make destroy-inference` llama a `shut_down_inference_infra.sh`, que es local y no se versiona.

## Puesta en marcha del CI/CD

**Una sola vez, en este orden.** Desde el paso 3, `infra/` exige el backend: `start_up.sh`, `shut_down.sh` y `shut_down_inference_infra.sh` fallan hasta terminar el paso 3.

**1. Crear la política nueva y adjuntarla a `smartestate-data`.** Es el único paso en la consola, con un usuario administrador: ningún perfil de la CLI administra IAM. En IAM → Policies → Create policy, pegar `infra/bootstrap/iac-smartestate-inference.json` con el ID de la cuenta en lugar de `ACCOUNT_ID` y llamarla `iac-smartestate-inference`. Luego, en Users → `smartestate-data` → Add permissions, adjuntarla.

**2. Revisar si la cuenta ya tiene el proveedor OIDC de GitHub:**

```bash
aws iam list-open-id-connect-providers --profile smartestate
```

Si aparece `token.actions.githubusercontent.com`, agregar `TF_VAR_create_github_oidc_provider=false` a `.env`.

**3. Crear el bucket de estado con Terraform y conectar `infra/`.** `infra/state_backend/` es un stack aparte, con estado local propio: el bucket debe existir antes de que `infra/` guarde su estado en él.

```bash
(cd infra/state_backend && terraform init && terraform apply)
terraform -chdir=infra/state_backend output -raw backend_config > infra/backend.hcl
(cd infra && AWS_PROFILE=smartestate terraform init -migrate-state -backend-config=backend.hcl)
```

Si ya hay estado local, responder `yes` a la copia: sin él, Terraform no sabría qué ya existe e intentaría crearlo de nuevo. Desde cero, no hay nada que copiar y usa `terraform init -backend-config=backend.hcl`.

**4. Crear los roles y buckets, y levantar la inferencia.** El primer `apply` agrega 15 recursos (14 si el proveedor ya existía); el segundo, 17 si la infra estaba apagada. La EC2 cobra desde ese momento:

```bash
(cd infra && set -a && . ../.env && set +a && AWS_PROFILE=smartestate terraform apply -target=module.cicd)
(cd infra && set -a && . ../.env && set +a && AWS_PROFILE=smartestate terraform apply -target=module.inference_service)
```

**5. Registrar el `run_id` de producción.** La imagen `test` se borró al apagar la infra, así que el primer despliegue debe ser un push a `main`, que construye la suya. El lanzamiento manual funciona desde el siguiente:

```bash
echo '{"run_id":"756577598248164","image_tag":"test"}' \
  | aws s3 cp - s3://smartestate-lake-<account_id>/runs/production.json --profile smartestate
```

**6. Configurar GitHub.** Los valores de los secretos son los de `.env`. `INFERENCE_ALLOWED_CIDRS` debe coincidir con `.env`, o Terraform revertirá la regla:

```bash
gh secret set AWS_ACCOUNT_ID
gh secret set DATABRICKS_ACCOUNT_ID
gh secret set DAILY_USER_EMAIL
gh secret set INFERENCE_ALLOWED_CIDRS

R=repos/kevin-ja/smartestate/environments
BRANCHES='"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}'
echo "{$BRANCHES}" | gh api -X PUT "$R/terraform-plan" --input -
echo "{\"reviewers\":[{\"type\":\"User\",\"id\":$(gh api user --jq .id)}],$BRANCHES}" \
  | gh api -X PUT "$R/production" --input -
gh api -X POST "$R/terraform-plan/deployment-branch-policies" -f name=main
gh api -X POST "$R/production/deployment-branch-policies" -f name=main
```

**7. Hacer merge a `main`** con la EC2 encendida. Corren los dos workflows; Infra pide aprobación solo si hay cambios.

## Operación del CI/CD

| Caso | Qué hacer | Qué pasa |
|---|---|---|
| Cambio en la API | Push a `main` | Tests → imagen → integración → ECR → Ansible → `production.json` |
| Cambio en `infra/` (inferencia) | Push a `main` | Plan en bucket privado → aprobación → apply |
| Artefactos nuevos o rollback | Lanzar el workflow Inference con el `run_id` | Mismo despliegue, con la imagen actual |

- `module.cicd` lo aplica Kevin a mano: así un workflow no puede ampliar sus propios permisos.
- Si la EC2 está apagada, el workflow falla antes de desplegar y no cambia `production.json`.
- `scripts/deploy.bash` queda como alternativa manual.

## Cómo correrla

```bash
RUN_ID=<run_id> RUNS_ROOT=s3://<lake>/runs AWS_REGION=us-east-1 ./ss/bin/uvicorn src.inference.api:app
./ss/bin/python -m pytest tests/test_inference.py
```

## Imagen, prueba manual (sin CI/CD)

Se construye para `linux/amd64`, porque `t3.small` es x86. El repositorio `smartestate-inference` lo crea el módulo de Terraform.

**Desde la laptop:**

```bash
aws ecr get-login-password --profile smartestate --region us-east-1 \
  | docker login --username AWS --password-stdin <account_id>.dkr.ecr.us-east-1.amazonaws.com
docker build --platform linux/amd64 -f src/inference/Dockerfile \
  -t <account_id>.dkr.ecr.us-east-1.amazonaws.com/smartestate-inference:test .
docker push <account_id>.dkr.ecr.us-east-1.amazonaws.com/smartestate-inference:test
```

**Desde EC2, por SSM:**

```bash
aws ecr get-login-password --region us-east-1 \
  | sudo docker login --username AWS --password-stdin <account_id>.dkr.ecr.us-east-1.amazonaws.com
sudo docker pull <account_id>.dkr.ecr.us-east-1.amazonaws.com/smartestate-inference:test
sudo docker run -d --name inference --restart always -p 8000:8000 \
  --log-driver awslogs --log-opt awslogs-group=/smartestate/inference --log-opt awslogs-region=us-east-1 \
  -e RUN_ID=<run_id> -e RUNS_ROOT=s3://smartestate-lake-<account_id>/runs -e AWS_REGION=us-east-1 \
  <account_id>.dkr.ecr.us-east-1.amazonaws.com/smartestate-inference:test
curl localhost:8000/health
```

**El módulo `inference_service` ya trae:** Docker instalado al arrancar, lectura de ECR, S3 y DynamoDB en el instance profile, e IMDS con hop limit 2. Sin el hop limit, el contenedor no alcanza las credenciales.

**Costo:** la instancia sigue encendida aunque se destruya el workspace de Databricks. Para apagarla: `bash shut_down_inference_infra.sh`. También borra la imagen de ECR y los logs; las tablas de DynamoDB y el lake se conservan.
