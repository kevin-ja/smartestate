# Pipeline de inferencia — diagrama

Así queda el pipeline de inferencia según `new_arch.md` §4.

Hay dos momentos. El arranque pasa una vez, cuando se levanta el contenedor. El clic pasa en cada consulta, y la respuesta tiene que llegar en 1 segundo o menos.

```
════════════════════════════════════════════════════════════════════════════════
 ETAPA 0 · ARRANQUE DEL CONTENEDOR                      (una vez, ECS Fargate)
════════════════════════════════════════════════════════════════════════════════
 ENTRA   run_id  (lo pone el CD como variable de entorno: es la corrida de
                  MLflow con el tag `champion=true`)
 CÓMO    1. Lee de S3  runs/<run_id>/geo_index.joblib      → joblib.load
         2. Lee de S3  runs/<run_id>/reranker_params.json  → w_tab, w_nlp,
                                                             f_bounds, δ
            (el radio r, su tope y el N por defecto no van en el JSON:
             están pendientes, new_arch.md §3.3)
         3. Crea un cliente boto3 de DynamoDB (credenciales del IAM task role)
         4. Arma el nombre de la tabla: spots_fs_online_<run_id>
 SALE    GeoIndex + parámetros en RAM  →  /health responde OK (para el ALB)
════════════════════════════════════════════════════════════════════════════════

                               (servicio listo)
                                      │
════════════════════════════════════════════════════════════════════════════════
 POR CADA CLIC                                             (≤ 1 s de respuesta)
════════════════════════════════════════════════════════════════════════════════
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 1 · REQUEST (FastAPI)                                                        │
│ ENTRA  GET /recommendations/20381?n=10                                       │
│ CÓMO   valida n (tiene un tope)                                              │
│ SALE   spot_id = 20381, n = 10                                               │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 2 · LECTURA DEL CLICKEADO                       DynamoDB · lectura 1         │
│ ENTRA  spot_id                                                               │
│ CÓMO   GetItem en spots_fs_online_<run_id>; trae todo de una vez             │
│        (viene de DynamoDB y no del índice: el clickeado puede ser outlier    │
│         o copia, o sea no candidato)                                         │
│ SALE   lat, lon, sector_id, modality_mask, tab_vector, nlp_vector,           │
│        rent_price_sqm, sale_price_sqm                                        │
│        (si no existe → 404)                                                  │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 3 · FILTROS 1 + 2                      GeoIndex.query() · todo en RAM        │
│ ENTRA  lat, lon, r, sector_id, modality_mask                                 │
│ CÓMO   Filtro 1 · zona:  tree.query_radius(lat, lon, r)  (BallTree haversine)│
│                          → candidatos a ≤ r km                               │
│        Filtro 2 · tipo + modalidad:                                          │
│                          sector == sector_id  AND                            │
│                          (mask_candidato & modality_mask) ≠ 0                │
│        Después: se quita el propio spot_id (sale a distancia 0)              │
│ SALE   una sola columna: spot_id (int64)   ej. [20365, 10322, ...]           │
│        (lat, lon, sector y modality_mask quedan dentro del índice; las       │
│         features las trae la etapa 4)                                        │
│        (el índice ya tiene solo is_candidate = True; aquí no se filtra eso)  │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 4 · LECTURA DE CANDIDATOS                       DynamoDB · lectura 2         │
│ ENTRA  lista de spot_id                                                      │
│ CÓMO   BatchGetItem con proyección (7 columnas, no la fila entera)           │
│        tandas de ≤ 100 ids en paralelo; tope de candidatos                   │
│ SALE   tabla de candidatos: spot_id, tab_vector, nlp_vector,                 │
│        rent_price_sqm, sale_price_sqm, modality_mask, cluster_id             │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 5 · NIVEL 3A · SIMILITUD                                 numpy, una fórmula  │
│ ENTRA  vectores del clickeado (A) + matriz de candidatos (B)                 │
│ CÓMO   cos = producto punto (los vectores vienen normalizados a largo 1)     │
│          cos_tab = M_tab @ A_tab      cos_nlp = M_nlp @ A_nlp                │
│        sim = Σ(wᵢ·cosᵢ) / Σ(wᵢ)   solo con las señales que tienen A y B      │
│          · nlp_vector nulo (sin descripción) → no entra                      │
│          · Land no tiene tab_vector → solo texto                             │
│ SALE   sim ∈ [0,1] por candidato                                             │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 6 · NIVEL 3B · PRECIO f(r)                               numpy, una fórmula  │
│ ENTRA  sim + precios/m² de A y B + modality_mask                             │
│ CÓMO   r = precio_m²(B) / precio_m²(A), en la modalidad que comparten        │
│          r > 1 (más caro) penaliza · r ≈ 1 neutro · r < 1 bonus leve         │
│          f(r) acotado en [0.7, 1.1]                                          │
│          A y B "Rent & Sale" → f = promedio de f(renta) y f(venta)           │
│        score = sim · f(r)                                                    │
│ SALE   score por candidato, ordenado de mayor a menor                        │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 7 · NIVEL 3C · DIVERSIDAD                         greedy (el K-means NO corre)│
│ ENTRA  candidatos ordenados por score + cluster_id                           │
│ CÓMO   arma el top N de uno en uno:                                          │
│          score_ajustado(B) = score(B) · δ^n_c(B)      δ ≈ 0.85               │
│          n_c = cuántos del cluster de B ya entraron                          │
│        si el clustering sale mal, el top N cae al orden de 3B                │
│ SALE   top N de spot_id                                                      │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 8 · RESPONSE                                                                 │
│ SALE   200 {"run_id": "abc123", "spot_ids": [20365, 10322, ...]}             │
│        → el front busca esos spot_id en su propia base                       │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Qué se lee en cada clic, por etapa:**

```
            DynamoDB          RAM (GeoIndex)        numpy
etapa 2     GetItem ◀── 1
etapa 3                       query ◀── 0 lecturas
etapa 4     BatchGet ◀── 2
etapa 5-7                                           3A → 3B → 3C
```

**Ejemplo:** alguien hace clic en una oficina en renta (spot 20381).
1. El `GetItem` trae sus coordenadas, sector 11 y modalidad Rent.
2. El GeoIndex encuentra 40 spots a ≤ r km. De esos, 12 son oficinas que se rentan.
3. El `BatchGetItem` trae las features de esos 12.
4. 3A calcula qué tan parecidos son, 3B baja a los más caros y 3C evita que el top 10 sean todos del mismo cluster.
5. La respuesta devuelve 10 `spot_id`.

Todavía falta calibrar varios parámetros: el radio `r` y su tope, `w_tab`/`w_nlp`, la cota de `f(r)`, `δ`, `k` y el N por defecto.

En resumen: en cada clic se hacen 2 lecturas a DynamoDB, una búsqueda en RAM y tres fórmulas con numpy. No se procesan datos ni se llama a ningún modelo.
