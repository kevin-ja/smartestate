# infra/ — SmartEstate

Infraestructura del **pipeline de datos** (`raw → bronze → silver → gold`): el lake en S3 y el
workspace **clásico** de Databricks que lo procesa. Todo en Terraform.

**AWS** `<aws-account-id>` · `us-east-1` · **Databricks** workspace `smartestate` + metastore `smartestate-us-east-1`

ML, deployment y CI/CD no están aquí todavía (diseño en `arquitectura.md`).

---

## Cómo se conecta todo

```
                         DATABRICKS                                              AWS
 ┌───────────────────────────────────────────────────────────────┐   ┌───────────────────────┐
 │                                                               │   │ S3 smartestate-lake-* │
 │  Kevin ───▶ grupo engineers ──┐                               │   │   raw/    ref/        │
 │                               ├─ grants ─▶ catálogo smartestate   │   bronze/ silver/ gold│
 │  SP jobs ─▶ grupo pipeline ───┘            bronze·silver·gold │   │   ml/                 │
 │                                                  │            │   └───────────▲───────────┘
 │                                                  ▼            │               │
 │                              External Locations (1 por capa)  │               │
 │                                                  │            │               │
 │                                                  ▼            │   ┌───────────┴───────────┐
 │                              Storage Credential smartestate-lake ─▶ rol IAM                │
 │                                                               │   │ smartestate-data-     │
 │                                                               │   │ pipeline              │
 │  Clusters ── solo vía cluster policies dev / jobs ────────────────▶ EC2 (rol cross-account)│
 └───────────────────────────────────────────────────────────────┘   └───────────────────────┘

 SP smartestate-databricks ──▶ Terraform: crea todo esto. No ejecuta nada.
```

**En una frase:** los datos están en S3; Databricks los ve como un catálogo; los grants a grupos
deciden quién lee o escribe; y los clusters solo salen de plantillas con límites.

## Las piezas

| Pieza | Qué es | Módulo |
|---|---|---|
| Lake | Un bucket, una carpeta por capa | `lake` |
| Rol del pipeline | Lee `raw`/`ref`, escribe `bronze`/`silver`/`gold`. **Sin acceso a `ml/`** | `data_pipeline_role` |
| Bucket raíz + rol cross-account | AWS que Databricks necesita para existir y lanzar EC2 (no es el lake) | `dbx_root_bucket`, `dbx_crossaccount_role` |
| Workspace | Donde se trabaja. Red administrada por Databricks | `dbx_workspace` |
| Metastore | Registro central de Unity Catalog de la región | `dbx_metastore` |
| Storage Credential | El rol del pipeline, registrado en Unity Catalog | `dbx_storage_credential` |
| External Locations | "Esta carpeta de S3 se abre con esa credencial". `raw` y `ref` solo lectura | `uc/` |
| Catálogo `smartestate` | Tablas. Un schema por capa, cada uno en su carpeta | `uc/` |
| Cluster policies | `dev`: interactivo, 1 nodo `m5d.large` spot, se apaga a los 20 min, 1 por usuario · `jobs`: 1 nodo, nace y muere con el job | `dbx_compute_policies` |

## Identidades: una por trabajo

| Identidad | Trabajo | Admin |
|---|---|---|
| SP `smartestate-databricks` | **Crear** infra (Terraform) | Sí |
| Usuario diario (`TF_VAR_daily_user_email`) → grupo `smartestate-engineers` | **Desarrollar** | No |
| SP `smartestate-jobs` → grupo `smartestate-pipeline` | **Ejecutar** los jobs (`run_as`). Sin secreto | No |

Los permisos van a **grupos**, nunca a personas o SPs. Los dos grupos tienen los mismos permisos de
datos, pero separados: lo que se le dé a engineers para explorar no llega al pipeline.

| | engineers | pipeline |
|---|---|---|
| `raw/`, `ref/` | leer archivos | leer archivos |
| `bronze`, `silver`, `gold` | leer, escribir, crear tablas | leer, escribir, crear tablas |
| `ml/` | — | — |
| Cluster policy | `dev` y `jobs` | `jobs` |
| Usar `smartestate-jobs` como `run_as` | sí | — |

---

## Dos estados, dos ciclos de vida

El workspace se **destruye al cerrar cada sesión**: su red trae un NAT Gateway (~$32/mes, fuera del
trial). Por eso el código está partido en dos estados:

```
 infra/  (estado 1)                                    infra/uc/  (estado 2, NUNCA se destruye)
 ├─ PERSISTE                                            ├─ External Locations
 │   lake, rol del pipeline, bucket raíz,               ├─ catálogo + schemas (y sus tablas)
 │   rol cross-account, metastore,                      └─ grants
 │   Storage Credential, SP jobs + grupo pipeline
 └─ SE DESTRUYE Y RECREA cada sesión          lee URL, bucket, credencial
     workspace, grupo engineers + usuario,    y grupos del estado 1 ─────────────▶
     cluster policies, asignaciones al
     workspace, permiso run_as
```

Lo que persiste no depende del workspace en el código, así que el `destroy -target` no lo arrastra.

## Cada sesión

Los valores de cuenta salen de `.env` (raíz del repo; plantilla en `.env.example`). Se cargan en un
subshell `( … )` para que no queden exportados en la terminal.

```bash
# Arrancar
cd infra && (set -a; . ../.env; set +a; AWS_PROFILE=smartestate terraform apply)   # ~3 min. La URL del workspace cambia
bash uc/sync-workspace-id.sh                            # el estado de uc/ guarda el id del workspace anterior
cd uc && terraform apply                               # reaplica grants: el grupo engineers es nuevo

# Cerrar
cd infra && (set -a; . ../.env; set +a; AWS_PROFILE=smartestate terraform destroy -target=module.dbx_workspace)
# Si falla con INVALID_STATE, esperar ~2 min y repetir (el borrado es asíncrono).
# Revisar en AWS (VPC → NAT Gateways, us-east-1) que no quede uno huérfano.
```

**Primera vez:** `cp .env.example .env` y completarlo; correr antes `terraform init` en `infra/` y en `uc/`, y el bootstrap
(`bootstrap/iac-smartestate-data.json` en IAM, `bootstrap/databricks-iac-sp.sh` una vez).

## Ver permisos

```bash
# desde infra/. Las variables van DELANTE del comando, nunca con export (ver "Cuidado")
DATABRICKS_HOST=$(terraform output -raw databricks_workspace_url) \
DATABRICKS_CLIENT_ID=<id> DATABRICKS_CLIENT_SECRET=<secret> \
  databricks grants get-effective schema smartestate.bronze --principal smartestate-engineers
```
`<id>`/`<secret>`: los del perfil `smartestate-databricks` en `~/.databrickscfg`.

---

## Decisiones

| Decisión | Por qué |
|---|---|
| Un bucket, un prefijo por capa | La frontera entre pipelines la pone IAM, no el bucket |
| Bucket: sin acceso público, sin ACLs, SSE-S3 | Inventario de clientes. KMS solo con requisito de cumplimiento |
| Versionado; versiones viejas caducan a 30 d | Cada corrida reescribe con `overwrite` |
| `raw/` a Glacier IR a los 90 d | Se escribe una vez y se lee poco |
| Trust del rol = Unity Catalog + auto-asunción | La genera el provider con el ExternalId de la credencial |
| Storage Credential a nivel cuenta | Cuelga del metastore: sobrevive al destroy y el rol no cambia |
| Una External Location por capa | El rol solo lista sus prefijos; cada capa con su control |
| Nadie tiene `allow_cluster_create` | Solo se crean clusters vía policy: tamaño y apagado garantizados |
| Workspace clásico, VPC administrada | Cómputo en esta cuenta. No se migra a VPC propia sin recrearlo |
| Estado local | Una persona, sin CI. Migrar: `backend "s3"` + `terraform init -migrate-state` |

## Cuidado

- **Nunca `export DATABRICKS_*`** en la terminal de Terraform: pisa el perfil, Terraform le habla al
  workspace en vez de a la cuenta y planea **recrear el workspace**. Un plan que dice que el workspace
  *"has been deleted"* es eso, no un borrado real.
- **URLs de External Location sin `/` final**: la API la quita y el provider falla.
- **`terraform destroy` sin `-target`** borra también el lake y todo lo persistente. No usarlo.

## Estructura

```
infra/
├── main.tf · variables.tf · outputs.tf · versions.tf   (valores: ../.env)
├── bootstrap/          identidades que corren Terraform (AWS: política IAM · Databricks: SP)
├── modules/            un módulo por pieza (ver "Las piezas")
└── uc/                 Unity Catalog, estado propio
```

**Agregar un módulo:** `modules/<nombre>/` + bloque en `main.tf`. Si crea un tipo de recurso AWS
nuevo, ampliar `bootstrap/iac-smartestate-data.json` y actualizar la política en la consola.
