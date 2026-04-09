"""
贝叶斯优化引擎：BoTorch SingleTaskGP + qEI。

Manifold: 3 个连续参数（3 个 outlet logits），单目标（objective 最大化）
"""

from __future__ import annotations

import torch
from botorch.acquisition import qExpectedImprovement
from botorch.fit import fit_gpytorch_mll
from botorch.models import SingleTaskGP
from botorch.optim import optimize_acqf
from botorch.utils.transforms import normalize, unnormalize
from gpytorch.mlls import ExactMarginalLogLikelihood
from torch.quasirandom import SobolEngine

from evaluators.base import DesignParams

# GPU 支持：GP 拟合和 qEI 优化在有空闲显存时自动使用 CUDA
def _get_device() -> torch.device:
    if torch.cuda.is_available():
        try:
            best = max(range(torch.cuda.device_count()),
                       key=lambda i: torch.cuda.mem_get_info(i)[0])
            free_gb = torch.cuda.mem_get_info(best)[0] / 1e9
            if free_gb > 1.0:
                return torch.device(f"cuda:{best}")
        except Exception:
            pass
    return torch.device("cpu")


# 参数空间（与 database.py 中的 _PARAM_MINS/_PARAM_MAXS 对应）
# 列顺序: [logit_1, logit_2, logit_3]
_BOUNDS_RAW = torch.tensor([
    [-2.0, -2.0, -2.0],
    [ 2.0,  2.0,  2.0],
], dtype=torch.double)

# 归一化后的 bounds（供 optimize_acqf 使用）
_BOUNDS_NORM = torch.zeros_like(_BOUNDS_RAW)
_BOUNDS_NORM[1] = 1.0


def _tensor_to_params(x: torch.Tensor) -> DesignParams:
    """将归一化参数张量（shape [3]）转回 DesignParams。"""
    raw = unnormalize(x.unsqueeze(0), _BOUNDS_RAW).squeeze(0)
    return DesignParams(logit_1=raw[0].item(), logit_2=raw[1].item(), logit_3=raw[2].item())


class BayesianOptimizer:
    """BoTorch 贝叶斯优化器。

    参数:
        db          : ResultDatabase 实例（读取历史数据）
        batch_size  : 每轮推荐的设计点数（默认 8）
        num_restarts: optimize_acqf 多起点数（默认 10）
        raw_samples : optimize_acqf 随机采样数（默认 512）
    """

    def __init__(
        self,
        db,
        batch_size: int = 8,
        num_restarts: int = 10,
        raw_samples: int = 512,
    ):
        self.db           = db
        self.batch_size   = batch_size
        self.num_restarts = num_restarts
        self.raw_samples  = raw_samples

    def initial_points(self, n: int = 16) -> list[DesignParams]:
        """用 Sobol 序列生成初始采样点（空间填充，低差异性）。"""
        sobol = SobolEngine(dimension=3, scramble=True, seed=42)
        samples_norm = sobol.draw(n).to(torch.double)          # [n, 3], in [0,1]
        samples_raw  = unnormalize(samples_norm, _BOUNDS_RAW)  # [n, 3], real units
        return [
            DesignParams(
                logit_1=samples_raw[i, 0].item(),
                logit_2=samples_raw[i, 1].item(),
                logit_3=samples_raw[i, 2].item(),
            )
            for i in range(n)
        ]

    def suggest_next_batch(self) -> list[DesignParams]:
        """基于数据库中的历史数据，用 qEI 推荐下一批设计点。

        若有效数据点 < 3，回退到随机采样（GP 在极少数据下不稳定）。
        自动检测 GPU 空闲显存，优先在 CUDA 上运行 GP 拟合和 qEI 优化。
        """
        train_X, train_Y = self.db.load_training_data()

        if train_X.shape[0] < 3:
            print(f"  [BO] 有效数据点 {train_X.shape[0]} < 3，回退到随机采样")
            return self._random_batch()

        # 选择计算设备
        device = _get_device()
        train_X = train_X.to(device)
        train_Y = train_Y.to(device)
        bounds  = _BOUNDS_NORM.to(device)

        # 拟合 GP
        model = SingleTaskGP(train_X, train_Y).to(device)
        mll   = ExactMarginalLogLikelihood(model.likelihood, model)
        fit_gpytorch_mll(mll)

        # qEI 采集函数（最大化期望改善）
        best_f = train_Y.max()
        qEI    = qExpectedImprovement(model, best_f=best_f)

        # 优化采集函数
        candidates, acqf_val = optimize_acqf(
            acq_function=qEI,
            bounds=bounds,
            q=self.batch_size,
            num_restarts=self.num_restarts,
            raw_samples=self.raw_samples,
        )

        # 结果移回 CPU
        candidates = candidates.cpu()
        print(f"  [BO] qEI={acqf_val.item():.4f}, 推荐 {self.batch_size} 个新设计点 (device={device})")
        return [_tensor_to_params(candidates[i]) for i in range(self.batch_size)]

    def _random_batch(self) -> list[DesignParams]:
        """随机均匀采样（回退策略）。"""
        samples_norm = torch.rand(self.batch_size, 3, dtype=torch.double)
        samples_raw  = unnormalize(samples_norm, _BOUNDS_RAW)
        return [
            DesignParams(
                logit_1=samples_raw[i, 0].item(),
                logit_2=samples_raw[i, 1].item(),
                logit_3=samples_raw[i, 2].item(),
            )
            for i in range(self.batch_size)
        ]
