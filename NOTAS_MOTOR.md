# Auditoría de `signal_engine.py`

Medido ejecutando el motor, no leyéndolo.

## 1. NO repinta ✅

La vela `t` da el mismo veredicto hoy que con 15 barras más detrás.
`rolling()` y `shift()` miran solo hacia atrás. Los resultados que
midas son alcanzables en vivo.

## 2. El filtro de régimen no filtra ❌

`is_trending = coarse > k_dominance * fine`, con las energías **sin
normalizar por escala**.

Sobre 300 series sintéticas por régimen:

| Régimen | Ratio mediano | % del tiempo encendido |
|---|---|---|
| **Ruido puro** | 3.01 | **93%** |
| Tendencia | 5.75 | 99% |
| Tendencia fuerte | 11.27 | 100% |
| Oscilante | 8.22 | 100% |

Con `k_dominance=1.5` se enciende el 93% del tiempo sobre un paseo
aleatorio puro. No distingue tendencia de aleatoriedad.

La causa es matemática: la diferencia entre dos medias de 8 barras
tiene mucha más varianza que entre dos de 1 barra, así que `coarse`
arranca inflado. En un análisis wavelet la energía se normaliza por
escala antes de compararla.

## 3. Qué opera de verdad el bot

Sobre 3000 barras de ruido puro:

- `is_trending` encendido: 91%
- Cruces de precio sobre SMA(8) al alza: 10%
- **Señales LONG completas: 3.5%**

El régimen elimina el 66% de los cruces — pero eso lo hace el filtro
`h8 > 0` (la pendiente), no el ratio de energía. Sin el envoltorio
wavelet, la estrategia es:

> **cruce de precio sobre SMA(8), con la pendiente de la escala gruesa
> a favor, y enfriamiento de 4 barras.**

## Qué hacer con esto

**NADA, todavía.** Los +17.14 USDT en 39 operaciones salieron de ESTE
motor tal cual. Cambiar el filtro ahora invalida esa muestra y te deja
otra vez en cero operaciones medidas.

Cuando llegues a 150 operaciones y tengas veredicto, entonces sí:
normalizar la energía por escala (dividir cada `e_j` por su longitud) y
recalibrar el umbral es la mejora obvia. Está medida en el otro bot:
con normalización, el ruido puro pasa de mediana 3.01 a 0.70.

Mientras tanto, sabes algo que antes no: **tu bot no es un filtro
wavelet, es un cruce de medias con un adorno.** Eso no lo hace malo
—+17 USDT es +17 USDT— pero cambia qué esperar y qué comparar.
