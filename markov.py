"""
bot/markov.py
Motor de cadenas de Markov con ventana deslizante.

3 estados: BULL / BEAR / NEUTRAL
Matriz de transición 3×3 con ventana deslizante de `lookback` observaciones.
Calcula prob_bull y prob_bear desde el estado actual.
"""
import numpy as np
import pandas as pd
from collections import deque


class MarkovEngine:

    BULL    = 0
    BEAR    = 1
    NEUTRAL = 2

    def __init__(self, lookback: int = 200):
        self.lookback = lookback
        self._matrix  = np.zeros(9, dtype=float)
        self._window: deque = deque()

    def _classify(self, slope: float, threshold: float) -> int:
        if slope > threshold:
            return self.BULL
        if slope < -threshold:
            return self.BEAR
        return self.NEUTRAL

    def _add(self, prev_s: int, curr_s: int) -> None:
        self._matrix[prev_s * 3 + curr_s] += 1.0
        self._window.append((prev_s, curr_s))

    def _remove_oldest(self) -> None:
        if self._window:
            p, c = self._window.popleft()
            self._matrix[p * 3 + c] = max(0.0, self._matrix[p * 3 + c] - 1.0)

    def update(self, slope: float, prev_slope: float,
               threshold: float) -> tuple[float, float]:
        curr_s = self._classify(slope, threshold)
        prev_s = self._classify(prev_slope, threshold)
        self._add(prev_s, curr_s)
        if len(self._window) > self.lookback:
            self._remove_oldest()
        return self._get_probs(curr_s)

    def _get_probs(self, curr_s: int) -> tuple[float, float]:
        base  = curr_s * 3
        total = self._matrix[base] + self._matrix[base + 1] + self._matrix[base + 2]
        if total == 0:
            return 0.0, 0.0
        return (
            round(self._matrix[base + self.BULL] / total * 100, 2),
            round(self._matrix[base + self.BEAR] / total * 100, 2),
        )

    def reset(self) -> None:
        self._matrix[:] = 0
        self._window.clear()


def compute_markov_probs(slopes: pd.Series, thresholds: pd.Series,
                         lookback: int = 200) -> pd.DataFrame:
    """Warmup vectorizado sobre toda la serie histórica."""
    engine = MarkovEngine(lookback)
    bulls, bears = [], []
    for i in range(len(slopes)):
        if i == 0:
            bulls.append(0.0); bears.append(0.0)
            continue
        pb, pr = engine.update(
            float(slopes.iloc[i]),
            float(slopes.iloc[i - 1]),
            float(thresholds.iloc[i])
        )
        bulls.append(pb); bears.append(pr)
    return pd.DataFrame({"prob_bull": bulls, "prob_bear": bears}, index=slopes.index)
