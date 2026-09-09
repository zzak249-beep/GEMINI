

# --- Mejoras medidas sobre el histórico de 128 operaciones ---
#
# 1) SIN TP NO SE OPERA. El ratio realizado fue 0,54 frente al 1,67 de
#    diseño (SL 1.5 ATR / TP 2.5 ATR). Con 46% de aciertos, 1,67 da
#    +0,23 R por operación y 0,54 da -0,29: la diferencia entre ganar y
#    perder. Una posición con stop y sin objetivo tiene pago asimétrico
#    —la pérdida se corta, la ganancia no se cobra— y antes solo se
#    avisaba por Telegram dejándola abierta.
CERRAR_SIN_TP = _bool("CERRAR_SIN_TP", "true")

# 2) TOPE DIRECCIONAL. El 09/09 salieron nueve señales LONG seguidas. En
#    un desplome las alts se mueven juntas: nueve largos no son nueve
#    apuestas, son una repetida nueve veces. 0 = desactivado.
MAX_MISMA_DIRECCION = int(_str("MAX_MISMA_DIRECCION", "2"))

# 3) PATRIMONIO MÍNIMO. Con equity 0 el dimensionado por riesgo da
#    cantidad 0 y la señal moría con un "qty tras redondeo es 0" que
#    parece un problema de precisión y no lo es. Peor:
#    check_circuit_breaker(0) no puede medir drawdown, así que el límite
#    diario quedaba desactivado sin avisar.
EQUITY_MINIMO = float(_str("EQUITY_MINIMO", "5"))
