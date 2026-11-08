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

## Medición 2 · 2026-11-07 · con 1b y 2

**Cambios:** lectura de DynamoDB con JSON directo (1b) y 2 procesos de `uvicorn` (2). Imagen `2581f68`.
Misma EC2, misma corrida y la misma prueba.

### Veredicto: aguanta 20 usuarios concurrentes (8,6 req/s), 10 veces más que antes

**Línea base (1 usuario, 3 min, 83 clics):** servidor p50 24 ms, p95 290 ms y p99 380 ms (antes 75 / 820 / 1.000).

| Usuarios | req/s | Éxito | Servidor p50 / p95 | SLO |
|---:|---:|---:|---:|:---:|
| 2 | 0,9 | 100 % | 38 / 250 ms | ✓ |
| 5 | 2,2 | 100 % | 50 / 290 ms | ✓ |
| 10 | 4,5 | 100 % | 47 / 350 ms | ✓ |
| 20 | 8,6 | 100 % | 72 / 470 ms | ✓ |
| 40 | 15,6 | 100 % | 230 / 1.300 ms | ✗ |
| 80 | 15,9 | 100 % | 2.300 / 6.300 ms | ✗ |
| 160 | 14,6 | 100 % | 7.300 / 16.000 ms | ✗ |

| | Medición 1 | Medición 2 |
|---|---:|---:|
| Usuarios dentro del SLO | 2 | **20** |
| Techo de caudal | 3,4 req/s | **~16 req/s** |
| Errores con 80 usuarios | 36 % | **0 %** |

El techo de ~16 req/s equivale a ~125 ms de CPU por clic en 2 vCPU: el cuello sigue siendo leer y
convertir los vectores de cada clic. La versión 2.0 lo elimina.

## Próximos pasos · versión 2.0

Decidido por Kevin el 2026-11-07: queda para la versión 2.0, no se implementa ahora.

**Problema de costo:** cada clic lee de DynamoDB hasta 300 candidatos (~6 MB) y los re-rankea. Para
1.000 usuarios concurrentes (~400 req/s) habría que pagar ~2.650 USD/mes de cómputo y, con tráfico 24/7,
~97.000 USD/mes de lecturas (~94 USD por millón de clics). Son precios de lista de `us-east-1`, aproximados.

**Propuesta A (recomendada): precalcular las recomendaciones por corrida.** La respuesta depende solo del
aviso clicado y de la corrida, sin datos del usuario. T3 corre la cascada para los ~2.500 avisos y guarda
el top 50 de cada uno en `runs/<run_id>/` (~1 MB). La API lo carga en memoria al arrancar, como hoy el
GeoIndex, y cada clic pasa a ser una búsqueda: ~1 ms y ninguna lectura de DynamoDB.

```
hoy:  clic → DynamoDB (300 ítems, ~6 MB) → parsear vectores → re-ranking → top N
2.0:  T3 → top 50 por aviso en S3      ·   clic → memoria → top N
```

- Costo para 1.000 usuarios: **~30–130 USD/mes**.
- Cambia `new_arch.md` §5: DynamoDB sale del camino del clic.
- Un aviso nuevo tiene recomendaciones desde la corrida siguiente, igual que hoy.

**Plan B:** caché en memoria en la API. Cada candidato se lee una vez de DynamoDB, y como el `run_id` es
fijo nunca queda desactualizado. Solo cambia `online_store.py`. Para 1.000 usuarios: ~130–300 USD/mes.

**Con cualquiera de las dos:** Graviton (`t4g`/`c7g`, ~20 % menos), un Savings Plan a 1 año (~30 % menos
sobre lo que quede prendido 24/7) y Auto Scaling para pagar los picos solo cuando ocurren.

Los vectores en binario (mejora 1) dejan de hacer falta con A o B.
