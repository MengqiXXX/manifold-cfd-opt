"""
统一评估器接口定义。

Manifold CFD optimization:
- 目标 1：出口质量流量均匀性（标准差 / 变异系数最小）
- 目标 2：压降（入口与出口之间 ΔP 最小）

两者实现相同的 Evaluator ABC，优化循环和 Agent 层代码不感知底层评估器类型。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class DesignParams:
    """歧管（manifold）几何/开口参数化输入。

    采用 4 个出口开口高度的 softmax 参数化（保证 >0 且和为 1）：
      outlet_logits = [logit_1, logit_2, logit_3, 0]

    这样 BO 的输入仍然是简单 box bounds（logit_i ∈ [min, max]），
    同时输出可直接映射为 4 个出口的相对开口高度（或等价的“曲线控制点”）。
    """

    logit_1: float
    logit_2: float
    logit_3: float

    def to_dict(self) -> dict:
        return {"logit_1": self.logit_1, "logit_2": self.logit_2, "logit_3": self.logit_3}

    def __repr__(self) -> str:
        return (
            "DesignParams("
            f"logit_1={self.logit_1:+.3f}, "
            f"logit_2={self.logit_2:+.3f}, "
            f"logit_3={self.logit_3:+.3f}"
            ")"
        )


@dataclass
class EvalResult:
    """单个设计点的评估结果。

    flow_cv      : 出口质量流量变异系数 std(m_dot)/mean(m_dot)（越小越好）
    pressure_drop: 压降 ΔP (Pa)（越小越好）
    converged    : 求解是否收敛
    runtime_s    : 评估耗时 (秒)
    status       : 'OK' | 'DIVERGED' | 'ANOMALY' | 'TIMEOUT' | 'ERROR'
    metadata     : 扩展字段（原始 JSON 输出等）
    """
    params: DesignParams
    flow_cv: float
    pressure_drop: float = 0.0
    converged: bool = False
    runtime_s: float = 0.0
    status: str = "OK"
    metadata: dict = field(default_factory=dict)

    @property
    def objective(self) -> float:
        """BO 最大化目标（越大越好）。

        将“要最小化的代价”转成负值以便最大化：
          cost = flow_cv + dp_weight * (pressure_drop / dp_ref)
          objective = -cost
        """
        dp_weight = float(self.metadata.get("dp_weight", 1.0e-5))
        dp_ref = float(self.metadata.get("dp_ref", 1.0))
        cost = float(self.flow_cv) + dp_weight * (float(self.pressure_drop) / dp_ref)
        return -cost

    def is_valid(self) -> bool:
        return self.converged and self.status == "OK" and math.isfinite(self.objective)

    def __repr__(self) -> str:
        return (
            f"EvalResult({self.params!r}, "
            f"cv={self.flow_cv:.4g}, dp={self.pressure_drop:.3g}Pa, "
            f"obj={self.objective:.4g}, status={self.status}, "
            f"t={self.runtime_s:.2f}s)"
        )


class Evaluator(ABC):
    """评估器抽象基类。所有具体评估器必须实现 evaluate_batch。"""

    @abstractmethod
    def evaluate_batch(self, params_list: list[DesignParams]) -> list[EvalResult]:
        """并发评估一批设计点，返回与输入等长的结果列表。

        失败的设计点应返回 EvalResult(converged=False, status='ERROR')，
        不应抛出异常，保证批量任务不因单点失败而中断。
        """
        ...
