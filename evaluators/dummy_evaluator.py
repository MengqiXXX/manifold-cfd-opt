from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .base import DesignParams, EvalResult, Evaluator


@dataclass
class DummyEvaluator(Evaluator):
    noise: float = 0.0
    sleep_s: float = 0.0

    def evaluate(self, params: DesignParams) -> EvalResult:
        if self.sleep_s > 0:
            time.sleep(self.sleep_s)

        x = params.L_D
        y = params.r_c
        z = params.D

        base = math.exp(-((x - 30.0) / 10.0) ** 2) * (1.0 - abs(y - 0.35)) * (1.0 - abs(z - 0.02) / 0.02)
        objective = float(base)

        return EvalResult(
            params=params,
            efficiency=objective,
            delta_T=objective,
            pressure_drop=1.0,
            converged=True,
            runtime_s=0.0,
            status="OK",
            metadata={"dummy": True},
        )

