# RESEARCH.md — ¿Qué ventaja matemática tiene realmente esta estrategia?

## 1. Lo que el hilo de X vende vs. lo que el script hace

El hilo original vende la idea de "wavelets de Ingrid Daubechies" (real
matemática belga, premio Wolf 2019/Abel-adjacent, inventora de las wavelets
ortogonales de soporte compacto que se usan en JPEG2000 y compresión de
huellas dactilares del FBI) + una estadística concreta ("$80 → $4.900 en 38
días, 71% aciertos, Sharpe 2.44") a cambio de like+follow+DM. Eso es un
patrón clásico de growth-hacking en trading: **la cifra es la carnada, no
un resultado auditable**. Ni el periodo, ni el símbolo, ni el drawdown máximo,
ni si es cuenta real o demo, ni el tamaño de la muestra (38 días es
estadísticamente casi nada en 5m) están verificados. Trátalo como
publicidad, no como evidencia.

Lo que el script Pine implementa **no es una DWT de Daubechies**. Es esto:

```
haar_detail(s, len) = (SMA(s, len) - SMA(s[len], len)) / sqrt(2)
```

Eso es una **diferencia de dos medias móviles desplazadas**, evaluada en 4
escalas (1, 2, 4, 8 barras), elevada al cuadrado y sumada en una ventana de
40 barras para obtener una "energía" por escala. Es matemáticamente
equivalente a un banco de filtros paso-banda muy tosco, mucho más parecido
a un MACD multi-escala o a un oscilador de momentum jerárquico que a una
transformada wavelet ortogonal de verdad. No hay downsampling diádico, no
hay ortogonalidad garantizada, no hay reconstrucción perfecta — es una
versión *redundante* y *causal* de la idea "à trous" (algorithme à trous,
Holschneider et al. 1989), que sí es una técnica real y usada en
procesamiento de señales, pero está muy simplificada aquí.

**Conclusión honesta**: el nombre "wavelet" es marketing. La mecánica real
es "compara la energía del ruido de corto plazo (1-2 barras) contra la
energía de la tendencia de medio plazo (4-8 barras), y solo opera cruces de
precio sobre su SMA(8) cuando domina lo segundo". Eso es un **filtro de
régimen tendencia/ruido**, una idea legítima, pero nada exclusivo de
wavelets — el mismo objetivo lo cumplen:

- **Efficiency Ratio de Kaufman** (`ER = |cambio neto| / suma de |cambios|`)
- **ADX / DMI** (fuerza direccional)
- **Exponente de Hurst** o **dimensión fractal** (persistencia vs. mean-reversion)
- **Ratio de varianza a distintos horizontes** (variance ratio test, Lo-MacKinlay)

Todos miden esencialmente lo mismo: ¿el movimiento reciente tiene estructura
persistente (autocorrelación positiva en retornos) o es ruido browniano?

## 2. ¿Hay una ventaja matemática real aquí?

Sí, una modesta y bien conocida, no la "genialidad oculta" que insinúa el
hilo:

1. **Filtrar régimen antes de operar cruces de MA reduce whipsaws.** Un
   cruce de precio sobre su SMA en un mercado lateral genera muchas señales
   falsas. Exigir que la energía de baja frecuencia domine sobre la de alta
   frecuencia es una forma razonable (aunque burda) de exigir "está
   tendencial" antes de disparar. Esto es la misma lógica de un ADX>25 como
   filtro de un cruce de medias — técnica de décadas, no una ventaja nueva.

2. **La verdadera pregunta estadística** no es "¿el filtro se llama
   wavelet?" sino: **¿los retornos del activo en 5m tienen memoria de corto
   plazo (autocorrelación) que este filtro capture de forma consistente,
   neta de comisión+slippage+funding?** Eso solo se responde con datos, no
   con la estética del método. Los mercados de perpetuos de cripto en 5m
   son extremadamente competidos (HFT, market makers, otros bots) — cualquier
   patrón de autocorrelación simple y público como este tiende a arbitrarse
   rápido si es real y explotable a gran escala. Que exista *algo* de señal
   residual localmente (activo/periodo concretos) es plausible; que sea
   grande y estable en el tiempo es poco probable.

3. **Riesgo real no capturado en el backtest de Pine**: comisión asumida
   0.05% (razonable para taker en BingX con descuento), pero el backtest de
   Strategy Tester de Pine no modela bien: profundidad de libro real en 5m
   para el tamaño de tu cuenta, funding rate acumulado si mantienes
   posición varias horas, latencia webhook→ejecución (normalmente 1-5s, a
   veces más si TradingView tiene cola), ni slippage real en momentos de
   alta volatilidad donde el filtro "coarse>fine" probablemente dispara más
   señales (los saltos de volatilidad SÍ elevan energía en todas las
   escalas, no solo en la gruesa).

## 3. Cómo validar esto de verdad antes de arriesgar capital

En orden de prioridad:

1. **Walk-forward, no solo backtest en una ventana.** Divide el histórico
   en bloques (ej. 3 meses train / 1 mes test, rodante) y comprueba que el
   edge (si existe) se mantiene fuera de muestra, no solo en el periodo que
   usaste para tunear `k_dominance`, `lookback_energy`, `atr_mult_*`.

2. **Compara contra un baseline simple.** Corre la misma estrategia de
   entrada (cruce de precio sobre SMA(8)) **sin** el filtro de energía
   (`is_trending` siempre true) y compara. Si el filtro "wavelet" no mejora
   claramente el Sharpe/profit factor fuera de muestra frente al baseline,
   el valor añadido del aparato matemático es ilusorio y lo único que
   funciona (si algo funciona) es el cruce de SMA con buen SL/TP en ATR.

3. **Multiplicidad de símbolos/periodos.** Si el edge solo aparece en
   BTCUSDT en un rango de fechas concreto, es sobreajuste. Debería mostrar
   *alguna* consistencia direccional (aunque no igual de fuerte) en varios
   perpetuos correlacionados (ETH, SOL) y en distintos años.

4. **Test de significancia estadística real**, no solo "71% de aciertos".
   Con pocas decenas de operaciones, un 71% de win rate no es
   estadísticamente distinguible de azar con sesgo de tamaño de muestra.
   Calcula el intervalo de confianza del win rate (ej. Wilson score) y el
   profit factor con su varianza — no solo el punto estimado.

5. **Forward test en real con capital mínimo** (lo que ya monta este bot en
   modo `AUTO_TRADE=false` primero, y con tamaño mínimo después) durante
   semanas, comparando P&L real vs. lo que decía el backtest. La diferencia
   entre ambos te dice cuánto "edge" era matemático real y cuánto era
   artefacto del backtest.

## 4. Recomendación concreta

- Trátalo como **un filtro de régimen razonable para tu cruce de SMA(8)**,
  no como una técnica con ventaja matemática superior por ser "wavelet".
- El valor real está en la **disciplina de riesgo** que ya tienes
  incorporada en tu arquitectura habitual (position sizing por %, SL/TP por
  ATR, circuit breaker, cooldown) más que en el propio filtro de energía.
- Antes de subir `AUTO_TRADE=true`, corre el punto 2 (comparar contra
  baseline sin filtro) — es la prueba más barata y más informativa de si
  el "wavelet" aporta algo por encima de un cruce de medias con buena
  gestión de riesgo.
- Si quieres una versión con wavelets reales (Daubechies db4, ortogonales,
  con downsampling), eso requiere correrlas fuera de Pine (Python con
  PyWavelets) porque Pine Script no tiene downsampling diádico nativo — se
  podría hacer como un microservicio que TradingView consulta, o mover todo
  el cálculo de señal al propio bot en Python (dejando TradingView solo como
  fuente de velas via su API/exportación, o usando datos de BingX
  directamente). Dilo si quieres que monte esa versión con PyWavelets real
  corriendo dentro del propio bot de Railway en vez de en Pine.
