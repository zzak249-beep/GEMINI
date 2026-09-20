# decay — ¿cuánto dura la señal de basis, y llega a pagar el peaje?

Servicio de **medición**. No opera, no pide claves de API, no puede abrir una
posición aunque quisiera. Solo lee endpoints públicos de BingX y escribe CSV.

## Qué mide y por qué existe

El panel de 15 minutos del bot de crowding dejó esto:

```
IC(basis_z -> retorno futuro, transversal)   +0.079 a 15 min
el mismo IC esperando una sola vela          +0.005
```

Toda la ventaja vive **dentro del primer intervalo de captura**, y con capturas
cada 15 minutos no se puede ver dónde muere. Puede ser a los 30 segundos
(precio rancio, no hay nada que hacer) o a los 8 minutos (hay un bot posible,
con ejecución maker). La diferencia entre esas dos respuestas es la diferencia
entre cerrar la línea y abrir otra.

Este servicio captura cada **60 s** en vez de cada 900 y mide el IC a 1, 2, 3,
5, 10, 15, 30 y 60 minutos.

## Por qué cuesta casi nada

`/quote/premiumIndex` devuelve **todos** los símbolos en una sola llamada. No
hacen falta klines ni openInterest por símbolo. Una captura completa del
universo son 1-2 peticiones: ~2.900 al día.

Se guarda el basis **crudo**, no el z-score. La ventana del z es justamente uno
de los parámetros a probar, y guardando el crudo se recalcula con cualquier
ventana sin volver a capturar.

## El coste no se estima, se mide

Se apunta el bid y el ask reales de cada símbolo en cada captura, así que el
umbral que la señal tiene que superar sale del libro de órdenes. Se informan
las dos ejecuciones por separado:

- **taker**: spread completo + 2 × comisión taker
- **maker**: 0 × spread + 2 × comisión maker

El maker asume que te llenan. No siempre te llenan, y justo cuando la señal es
buena es cuando menos te llenan. **Ese sesgo no está medido aquí.**

## El control

Cada IC va acompañado de su nulo empírico: se baraja `basis_z` dentro de cada
instantánea (lo que destruye la relación señal-retorno pero conserva la
estructura transversal de los retornos) y se recalcula `DECAY_BARAJAS` veces.
El p empírico es la fracción de barajas que iguala o supera lo observado.

Probado contra ruido puro generado a propósito, un horizonte sacó p=0,020
—"significativo" al 5%— y la cartera decil a 30 min dio beneficio neto sobre
paseos aleatorios. La corrección de Bonferroni sobre los 8 horizontes lo tumba.
Sin ese control, ese resultado se habría leído como una ventaja.

El t-Student se calcula **solo sobre ventanas que no comparten velas**. Con
capturas de 60 s y horizonte de 60 min hay 60 ventanas solapadas y el t saldría
inflado unas 8 veces.

## Despliegue en Railway

Repo propio y proyecto propio. No comparte código ni volumen con ningún bot.

1. Repo nuevo en GitHub con estos archivos en la raíz.
2. Railway → New Project → Deploy from GitHub repo.
3. **Volumen**: Add Volume, punto de montaje `/data`. Sin esto el servicio no
   arranca (lo comprueba al inicio y avisa por Telegram).
4. Variables: pegar el bloque de abajo en el raw editor.
5. El comando de arranque ya viene fijado en `railway.json` y en el `Procfile`.
   **No lo cambies a mano**: si Railway lo autodetecta acaba eligiendo
   `gunicorn`, que es para aplicaciones web, y el servicio entra en bucle de
   reinicio con `sh: 1: gunicorn: not found`.

```
DECAY_DIR=/data/decay
DECAY_CADA_SEG=60
DECAY_DIAS=7
DECAY_MIN_SIMBOLOS=40
DECAY_MIN_VOL_USDT=2000000
DECAY_Z_VENTANA=240
DECAY_Z_MIN=60
DECAY_INFORME_H=6
DECAY_ANALISIS_H=48
DECAY_MAX_PARES=1200
DECAY_BARAJAS=200
DECAY_COM_TAKER=0.045
DECAY_COM_MAKER=0.020
TG_TOKEN=
TG_CHAT=
```

`TG_TOKEN` y `TG_CHAT` son los mismos del resto de la flota. Si los dejas
vacíos el informe sale por los logs en vez de por Telegram.

## Uso

El servicio captura solo y manda informe cada `DECAY_INFORME_H` horas. El
informe se lanza en subproceso a propósito: tarda hasta 90 s y en el mismo
proceso se comería las capturas de esos segundos, que es justo la resolución
que el servicio existe para medir.

Informe a demanda, desde la consola de Railway:

```
python decay.py informe
```

El primer informe útil necesita **48 horas** de captura. Antes de eso el
z-score no tiene ventana suficiente y el informe lo dice en vez de inventarse
un número.

## Cómo leer el resultado

Tres desenlaces posibles:

- **El IC muere antes del minuto** → precio rancio. Se cierra la línea y
  `basis_z` queda solo como veto direccional sobre BOT14.
- **Vive 3-10 min pero no cubre ni el coste maker** → hay señal, no hay
  negocio. Mismo destino, pero sabiendo por qué.
- **Vive y cubre maker** → el siguiente paso **no es operar**, es medir el
  llenado maker.

La prueba que separa señal de microestructura es la tabla "misma señal,
entrando tarde". Con señal real el IC apenas baja al esperar 5 minutos; con los
datos del panel de 15 min se desplomaba de +0,0399 a +0,0046.

## Tamaño en disco

~180 símbolos × 1.440 capturas/día ≈ 260.000 filas/día, unos 15 MB. Con
`DECAY_DIAS=7` se estabiliza en ~100 MB. El servicio poda los CSV viejos cada
hora; sin eso el volumen se llena y el contenedor muere sin avisar.
