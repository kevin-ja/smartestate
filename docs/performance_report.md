# Reporte de performance de la inferencia

Cómo se mide: `tests/performance/README.md`. SLO: p95 del servidor < 1 s por clic y ≥ 99 % de respuestas correctas.

## Medición 1 · 2026-11-07 · infra base

**Infra:** una EC2 `t3.small` (2 vCPU, 2 GB), un proceso de `uvicorn`. Corrida `756577598248164`, imagen `2418e63`.
Locust desde la laptop (Lima → `us-east-1`).

### Veredicto: aguanta 2 usuarios concurrentes (0,8 req/s)

**Línea base (1 usuario, 3 min, 72 clics):**

| | p50 | p95 | p99 |
|---|---:|---:|---:|
| Servidor | 75 ms | 820 ms | 1.000 ms |
| Cliente (con red) | 240 ms | 990 ms | — |
| Red (`/health`) | 200 ms | — | — |

**Estrés (3 min por escalón, los primeros 20 s no cuentan):**

| Usuarios | req/s | Éxito | Servidor p50 / p95 | SLO |
|---:|---:|---:|---:|:---:|
| 2 | 0,8 | 100 % | 82 / 860 ms | ✓ |
| 5 | 2,0 | 100 % | 180 / 1.200 ms | ✗ |
| 10 | 3,4 | 100 % | 390 / 2.900 ms | ✗ |
| 20 | 3,4 | 100 % | 2.200 / 10.000 ms | ✗ |
| 40 | 3,3 | 99,2 % | 6.000 / 24.000 ms | ✗ |
| 80 | 3,3 | 64,2 % | 18.000 / 28.000 ms | ✗ |

El caudal se estanca en **3,4 req/s** desde 10 usuarios: más usuarios solo alargan la cola. Es la firma de
un solo núcleo de CPU al 100 %.

### Cuello de botella: convertir la respuesta de DynamoDB

Perfil de un clic con 300 candidatos (en local, contra el DynamoDB real):

| Paso | Tiempo |
|---|---:|
| Respuesta de `BatchGetItem` | 5,7 MB (300 ítems × vector de 768 números en texto) |
| botocore convierte cada `{"N": "…"}` | ~390 ms de CPU |
| `_decode` propio | 32 ms |
| Re-ranking | 2 ms |

Los clics con pocos candidatos tardan ~75 ms y los de 300, ~800 ms: de ahí la distancia entre p50 y p95.
Con un solo proceso, Python convierte un clic por vez y usa 1 de las 2 vCPU.

No se pudo ver la CPU ni los créditos de la `t3.small`: `smartestate-data` no tiene permiso para
`cloudwatch:GetMetricStatistics` ni `ssm:SendCommand`.

### Mejoras

| # | Cambio | Ganancia esperada | Estado |
|---|---|---|---|
| 1b | Leer DynamoDB con JSON directo, sin el parser de botocore | CPU por clic de 350 a 154 ms (medido en local, TLS incluido) | Aprobado, implementado |
| 2 | 2 procesos de `uvicorn` | Usa las 2 vCPU: ~2× caudal | Aprobado, implementado |
| 1 | Vectores en binario (`float32`) en DynamoDB | CPU por clic a ~5–10 ms y 1,2 MB por clic | Aprobado, en pausa: exige reemplazar `publish_table` en T3 |
| 3 | Features de la corrida en memoria, sin DynamoDB por clic | Clics de milisegundos | Solo opción: cambia la arquitectura (`new_arch.md`) |
