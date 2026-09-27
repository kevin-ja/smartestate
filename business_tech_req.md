## 1. Objetivos de Negocio y KPIs

### 1.1 Modelo de Negocio

- **Esquema:** *Success Fee* (solo cobras si se cierra la renta o venta)
- **Enfoque:** B2B (Oficinas, Locales Comerciales, Naves Industriales).

### 1.2 KPIs a Impactar

- **Lead Quality Score:** Asegurar que el inmueble recomendado sea realmente lo que el usuario necesita.

Si un usuario busca una nave industrial con "entrada para tráiler" y el sistema le recomienda una oficina con "buena vista" (solo porque están cerca), el LQS cae a cero

- **Lead Conversion Rate (LCR):** Maximizar el ratio de usuarios que pasan de "ver un anuncio" a "contactar al anunciante".
    - *Nota:* El sistema no busca clics vacíos, sino **intención de cierre**.

---

## 2. Estrategia de Filtrado y Similitud (Cascada)

### 🟢 Nivel 1: Filtro por Tipo de inmueble y Modalidad

- **Lógica:** Filtro doble por tipo de inmueble **y** modalidad (renta / venta).
- **Regla de Negocio:** Un usuario buscando naves industriales no debe recibir recomendaciones de oficinas, sin importar la cercanía. Y un lead de renta que recibe inmuebles solo en venta vale cero.

### 🟡 Nivel 2: Filtro por zona geográfica

ya filtraste por tipo de inmueble, ahora ¿cuáles de estas están cerca del inmueble que el usuario acaba de clickear?

- **Algoritmo:** BallTree o KD-Tree (Distancia de Haversine)

Podrías calcular la distancia del inmueble de referencia a los 50.000 restantes, uno por uno. Funciona, pero es lento y lo tienes que hacer en cada clic.

BallTree:

Es un índice, como el índice de un libro. En vez de leer todas las páginas para encontrar un tema, vas directo.

El BallTree agrupa los inmuebles en "burbujas" por zona geográfica. Cuando preguntas "dame todo lo que esté a 5 km de aquí", el árbol descarta burbujas enteras de un golpe — "esta burbuja completa está en Monterrey, ni la abro" — en vez de revisar punto por punto.

Empiezas pidiendo 5 km. Si en zonas céntricas salen 200 candidatos, perfecto.. Pero una nave industrial en la periferia puede tener 2 vecinos en 5 km. Ahí el radio se abre: 5 → 10 → 15 km, hasta juntar suficientes. Con un tope duro. Si llegas al límite y solo hay 3 inmuebles, devuelves 3. Nunca estiras el radio hasta el infinito para rellenar un Top 10 — un inmueble a 80 km no es una recomendación, es ruido.

### 🔵 Nivel 3: re-Ranker (ordena por relevancia, no filtra)

Llegas aquí con ~50 candidatos. Todos son válidos: del tipo correcto y en la zona. Ninguno está mal. 

La pregunta ya no es "¿sirve?" sino "¿cuál se parece más?".

#### A) Similitud (Sim): decide quién califica

<aside>
📐

> $\mathrm{sim}(A,B)=w_{\mathrm{tab}}\cdot\cos(A_{\mathrm{tab}},B_{\mathrm{tab}})+w_{\mathrm{nlp}}\cdot\cos(A_{\mathrm{nlp}},B_{\mathrm{nlp}}),\qquad w_{\mathrm{tab}}+w_{\mathrm{nlp}}=1$
> 
</aside>

- $\cos(u,v)$ — Similitud coseno entre los vectores $u$ y $v$ (no distancia: aquí más alto = más parecido).
- $A$: Inmueble de referencia (el clickeado). Uno por consulta.
- $B$: Inmueble candidato. Uno de los ~50 que pasaron Capa 1 y 2.
- $A_{\mathrm{tab}}, B_{\mathrm{tab}}$ — Vectores numéricos, **uno por sector** (decidido 2026-09-15): las columnas físicas dependen del sector, no hay vector global. Industrial 6 dimensiones, Office 5, sector 13: 4. Escalados dentro del sector. Land solo tiene área (1 dimensión): no tiene vector tabular y su `sim` usa solo NLP.
- $A_{\mathrm{nlp}}, B_{\mathrm{nlp}}$ — Vectores semánticos (384 dimensiones): título + descripción procesados mediante Sentence-BERT y L2-normalizados.  $A_{\mathrm{nlp}}, B_{\mathrm{nlp}} \in \mathbb{R}^{384}$.
- $w_{\mathrm{tab}}, w_{\mathrm{nlp}}$ — Pesos de diseño, fijos e iguales para todos los candidatos. $w_{\mathrm{tab}}, w_{\mathrm{nlp}} \in [0,1].$
- $\mathrm{sim}(A,B)$ — Parecido total entre el inmueble de referencia y el candidato. Un valor por candidato, con $\mathrm{sim}(A,B) \in [0,1]$

#### B) Precio (r): decide quién va primero

<aside>
📐

$r = P_{m^2}(B) / P_{m^2}(A)$

</aside>

Donde $r$ no se usa directo: se convierte en un factor $f(r)$ por tramos.

$r>1 \quad \rightarrow \quad f(r)<1 \quad \text{(penalización creciente)}$

$r\approx1 \quad \rightarrow \quad f(r)\approx1 \quad \text{(neutro)}$

$r<1 \quad \rightarrow \quad f(r)>1 \quad \text{(bonus leve, se aplana)}$

Se aplana porque un -60% no es ganga, es que no es comparable.

$f(r)$ acotado en $[0.7,\,1.1]$ para que el precio no voltee el orden entre un candidato relevante y uno que no lo es. **Valor inicial, a calibrar** contra la dispersión real de $\mathrm{sim}$: el cociente $1.1/0.7 \approx 1.57$ bloquea el caso extremo, pero no impide adelantos entre candidatos de relevancia parecida.

<aside>
📐

$\mathrm{score}(A,B) = \mathrm{sim}(A,B) \cdot f(r)$

</aside>

Ordenar descendente, cortar en Top N.