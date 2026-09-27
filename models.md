# Modelos de Machine Learning

Complemento de `business_tech_req.md`. Define qué se entrena, para qué, y por qué conviene.

---

## Principio: nada se entrena en el clic

| Momento | Qué ocurre |
| --- | --- |
| **Offline** (pipeline de ML) | Se entrena el modelo y se guarda su salida como **columna** |
| **Online** (evento del front) | Solo lectura de esa columna + aritmética de ranking |

**Offline no es una sola cadencia** (`arquitectura.md` §4.2). Lo que depende del inventario corre
diario —embeddings de los spots nuevos, `cluster_id` con los centroides vigentes, BallTree
reconstruido—; lo que se ajusta sobre la distribución —scaler y k-means— corre semanal.

Entrenar toma minutos; el clic responde en milisegundos. El modelo no decide el ranking: **prepara una feature que el ranking consume**.

---

## Dónde entra

```
Nivel 1  →  filtro tipo + modalidad      sin ML
Nivel 2  →  BallTree                     sin ML
Nivel 3A →  sim(A,B)                     SBERT pre-entrenado, solo inferencia
Nivel 3B →  f(r)                         función por tramos, sin ML
Nivel 3C →  corte del Top N              ← K-MEANS
```

Un solo modelo entrenado por nosotros, y en el punto donde aporta sin poner en riesgo el ranking.

---

## K-means vectorial

**Qué hace:** agrupa el inventario sobre el vector combinado (tabular + embedding) en 5-8 arquetipos.

**Qué guarda:** una columna `cluster_id`.

### Conveniencia — dos usos

- **Diversidad en el Top N.** El coseno es demasiado bueno: si hay 10 bodegas casi idénticas del mismo desarrollo, el Top 10 son clones. El usuario ve una, no le sirve, y se va. Penalizar la repetición de cluster al armar el corte hace que vea opciones genuinamente distintas → impacta LCR. Es un **re-ranking al final**; no toca `sim`.
- **Vocabulario de negocio.** Los centroides se leen y se nombran ("nave logística periférica", "oficina corporativa centro"). Sirve para el EDA y para explicar el inventario. En el ranking aporta poco — conviene ser honesto con eso.

### Mecánica del corte (Nivel 3C)

Selección **greedy** sobre la lista ya ordenada por `score = sim · f(r)`. El descuento es progresivo:
cada vez que se elige un candidato, los que quedan de su mismo cluster valen un poco menos.

> $\mathrm{score}_{3C}(B) = \mathrm{score}(B) \cdot \delta^{\,n_c(B)}$

donde $n_c(B)$ es cuántos inmuebles del cluster de $B$ **ya entraron** al Top-N, y $\delta \in (0,1)$
es el factor de diversidad. Valor inicial **0.85**, a calibrar.

```
seleccionados = []
mientras len(seleccionados) < N y quedan candidatos:
    B* = argmax  score(B) · δ ** conteo[cluster_id(B)]
    seleccionados.append(B*)
    conteo[cluster_id(B*)] += 1
```

**Por qué progresivo y no un tope duro** (máx. *m* por cluster): en particiones donde casi todo cae en
un mismo arquetipo, el tope duro deja el Top-N a medio llenar. El descuento degrada suave — si no hay
alternativa, el segundo del mismo cluster entra igual, solo que después. Nunca devuelve menos de lo
que hay.

**Cómo leer δ:** con `δ = 0.85`, el segundo candidato de un cluster necesita superar al siguiente
cluster por >15% de score para conservar su posición; el tercero, por >28%. Clones (score ≈ idéntico)
siempre ceden; un candidato genuinamente mejor, no.

| Parámetro | Inicial | Criterio de calibración |
| --- | --- | --- |
| $\delta$ | 0.85 | Tasa de clones en el Top-N (hoy 21.0% del inventario) vs. caída de `sim` medio |
| $k$ | 5–8 | Codo / silhouette; centroides nombrables |

**Costo online:** $O(N \cdot C)$ con $C \approx 50$ candidatos — aritmética sobre una columna ya
persistida. No se calcula nada del modelo en el clic.

**Pendiente de medir en etapa 6:** si el clon exacto (mismo usuario, ≤1 km, área ±5%) merece un
descuento propio más agresivo que el de cluster, o si `δ` sobre cluster basta.

### Por qué encaja aquí

- **Barato de entrenar** y de re-entrenar. Aun así **no se reentrena a diario**: con ~10⁴ altas/mes
  sobre 10⁵–10⁶ spots los centroides no se mueven, y reentrenar hace inestables las etiquetas
  (`cluster_id` puede permutarse). Como el Nivel 3C penaliza con `δ^n_cluster`, eso movería el Top-10
  del mismo `spot_id` sin que cambie ni el inventario ni la consulta. Ver `arquitectura.md` §4.2.
- **No supervisado:** no hay label, así que no hay fuga entre train y test ni riesgo de sobreajuste a un precio memorizado.
- **No puede romper el ranking.** Actúa después de ordenar, como ajuste del corte. Si el clustering sale mal, el Top N sigue siendo el que dictó `sim · f(r)` — degrada, no rompe.

### Validación

- Justificar `k` con codo o silhouette.
- Fijar `random_state` para que la partición sea reproducible entre corridas.
- Revisar los centroides a mano: si los grupos no son nombrables en lenguaje de negocio, el `k` está mal elegido.

---

## Lo que NO se entrena

| Qué | Por qué no |
| --- | --- |
| **Regresión de precio (GBM)** | Cara de entrenar y propensa al sobreajuste; ni con k-fold garantiza ganarle a la mediana por `(categoría, modalidad, zona)`. Depender de un baseline que puede rechazar el modelo es un riesgo de calendario que no podemos asumir. Una **regresión simple y robusta** sería lo único reconsiderable más adelante. |
| Los pesos `w_tab` / `w_nlp` | Requieren datos de conversión. Hoy son constantes de diseño justificadas a mano. |
| Un ranker aprendido (LambdaMART y similares) | Necesita clics y contactos reales. |
| La cota de `f(r)` | Se calibra contra la dispersión observada de `sim`, no se aprende. |

Todo esto entra cuando el sistema lleve tiempo en producción y haya señal de conversión. Es v2 — inventar un problema de ML sin labels no mejora el producto.
