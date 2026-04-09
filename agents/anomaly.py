"""
异常检测模块：纯规则判断，无需 LLM。
"""
from __future__ import annotations
import math
import statistics
from evaluators.base import EvalResult


class AnomalyDetector:
    """检测优化过程中的异常情况。

    参数:
        diverge_streak    : 连续发散算例数阈值（默认 3）
        jump_sigma        : 目标值跳变的标准差倍数阈值（默认 3.0）
        surrogate_rmse_tol: 代理模型 RMSE 阈值（默认 2.0，单位 K 或无量纲）
    """

    def __init__(
        self,
        diverge_streak: int = 3,
        jump_sigma: float = 3.0,
        surrogate_rmse_tol: float = 2.0,
    ):
        self.diverge_streak    = diverge_streak
        self.jump_sigma        = jump_sigma
        self.surrogate_rmse_tol = surrogate_rmse_tol
        self._consecutive_diverged = 0

    def check(self, new_results: list[EvalResult], history: list[EvalResult]) -> tuple[bool, str]:
        """检查新一批结果是否触发异常。

        返回: (is_anomaly: bool, reason: str)
        """
        # 1. 连续发散检测
        n_diverged = sum(1 for r in new_results if not r.converged)
        if n_diverged == len(new_results):
            self._consecutive_diverged += 1
        else:
            self._consecutive_diverged = 0

        if self._consecutive_diverged >= self.diverge_streak:
            return True, (
                f"连续 {self._consecutive_diverged} 轮全部发散，"
                f"建议检查参数空间边界或 CFD 设置"
            )

        # 2. 目标值异常跳变检测
        valid_history = [r for r in history if r.is_valid()]
        if len(valid_history) >= 5:
            objs = [r.objective for r in valid_history]
            mean_obj = statistics.mean(objs)
            std_obj  = statistics.stdev(objs)
            for r in new_results:
                if r.is_valid() and std_obj > 0:
                    z = abs(r.objective - mean_obj) / std_obj
                    if z > self.jump_sigma:
                        return True, (
                            f"目标值异常跳变: {r.objective:.4f} "
                            f"(均值={mean_obj:.4f}, σ={std_obj:.4f}, z={z:.1f})"
                        )

        return False, ""

    def reset_streak(self) -> None:
        self._consecutive_diverged = 0
