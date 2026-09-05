"""
risk_manager.py — Dimensionado de posición.

Una sola responsabilidad: convertir un porcentaje del equity en una
cantidad de contratos que BingX vaya a aceptar. Todo lo que pueda hacer
que la orden se rechace (precisión, mínimos, nocional insuficiente) se
resuelve AQUÍ y se devuelve un motivo legible, en vez de descubrirlo con
un error de la API en mitad de la ejecución.

IMPORTANTE sobre qué NO hace: esto es dimensionado por NOCIONAL, no por
riesgo. qty_pct es el porcentaje del equity que se pone en la posición;
el riesgo real depende de la distancia al stop, que varía en cada
entrada. No se divide por el apalancamiento: el apalancamiento cambia el
MARGEN que BingX inmoviliza, no el tamaño de la posición. Dividir aquí
por LEVERAGE es el bug de doble dimensionado que ya apareció en otros
bots de la flota.
"""
from dataclasses import dataclass


@dataclass
class Sizing:
    ok: bool
    quantity: float = 0.0
    notional: float = 0.0
    reason: str = None


def compute_position_size(
    equity: float,
    qty_pct: float,
    entry_price: float,
    quantity_precision: int,
    trade_min_quantity: float = 0.0,
    trade_min_usdt: float = 0.0,
    min_notional_usdt: float = 0.0,
    max_notional_pct: float = 0.0,
) -> Sizing:
    """Cantidad de contratos para poner qty_pct% del equity en nocional.

    min_notional_usdt fija un SUELO en valor de posición (qty × precio),
    no en margen: con LEVERAGE=10, 9 USDT de nocional inmovilizan 0.9 de
    margen. Sirve para que en cuentas pequeñas las posiciones no salgan
    de céntimos, donde las comisiones se comen cualquier resultado.

    max_notional_pct (% del equity) es el freno del suelo anterior: si
    forzar el mínimo supera ese porcentaje del equity, se descarta la
    señal en vez de abrir una posición desproporcionada para la cuenta.

    quantity_precision viene de /openApi/swap/v2/quote/contracts POR
    SÍMBOLO. Usar una precisión global es lo que en otro bot dejó SL/TP
    a 0.0 en tokens de precio bajo: cada contrato tiene la suya.
    """
    if equity is None or equity <= 0:
        return Sizing(ok=False, reason="equity no disponible o cero")
    if entry_price is None or entry_price <= 0:
        return Sizing(ok=False, reason=f"precio de entrada inválido: {entry_price}")
    if qty_pct <= 0:
        return Sizing(ok=False, reason=f"QTY_PCT inválido: {qty_pct}")

    notional = equity * (qty_pct / 100.0)

    # Suelo de nocional: se aplica ANTES de convertir a contratos, para
    # que el redondeo trabaje ya sobre el tamaño definitivo.
    forzado = False
    if min_notional_usdt and notional < min_notional_usdt:
        tope = equity * (max_notional_pct / 100.0) if max_notional_pct else None
        if tope and min_notional_usdt > tope:
            return Sizing(
                ok=False,
                reason=(f"el mínimo de {min_notional_usdt} USDT supera el tope del "
                        f"{max_notional_pct}% del equity ({tope:.2f} USDT); "
                        f"cuenta demasiado pequeña para este mínimo"),
            )
        notional = min_notional_usdt
        forzado = True

    raw_qty = notional / entry_price

    try:
        precision = int(quantity_precision)
    except (TypeError, ValueError):
        precision = 4
    if precision < 0:
        precision = 0

    if forzado:
        # Hacia ARRIBA: redondear al más cercano podía dejar el nocional
        # otra vez por debajo del mínimo que acabamos de forzar.
        import math
        factor = 10 ** precision
        qty = math.ceil(raw_qty * factor) / factor
    else:
        qty = round(raw_qty, precision)

    # El redondeo a la baja puede dejarlo en cero: pasa con contratos de
    # precisión 0 (cantidades enteras) y precios altos. Devolver 0 y dejar
    # que BingX lo rechace sería un error silencioso.
    if qty <= 0:
        return Sizing(
            ok=False,
            reason=(f"cantidad {raw_qty:.10f} se redondea a 0 con precisión {precision} "
                    f"(nocional {notional:.2f} USDT insuficiente para este símbolo)"),
        )

    min_qty = float(trade_min_quantity or 0)
    if min_qty and qty < min_qty:
        # Subir al mínimo cambiaría el tamaño pedido sin avisar y podría
        # multiplicar la exposición. Mejor descartar la señal.
        return Sizing(
            ok=False,
            reason=(f"cantidad {qty} por debajo del mínimo del contrato ({min_qty}); "
                    f"subir QTY_PCT o descartar el símbolo"),
        )

    notional_real = qty * entry_price

    min_usdt = float(trade_min_usdt or 0)
    if min_usdt and notional_real < min_usdt:
        return Sizing(
            ok=False,
            reason=(f"nocional {notional_real:.2f} USDT por debajo del mínimo "
                    f"del contrato ({min_usdt} USDT)"),
        )

    return Sizing(ok=True, quantity=qty, notional=notional_real)
