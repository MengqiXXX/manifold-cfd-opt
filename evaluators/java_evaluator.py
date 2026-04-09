"""
Phase 0 评估器：通过 subprocess 调用 VortexTube.jar 评估单个设计点。

VortexTube.jar 需支持以下 CLI：
  java -jar VortexTube.jar --D 0.02 --LD 15.0 --rc 0.3 --output-json

stdout 输出单行 JSON：
  {"efficiency":0.423456,"converged":true,"runtime_s":0.031,"D":0.02,"L_D":15.0,"r_c":0.3}
"""

from __future__ import annotations

import json
import math
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .base import DesignParams, EvalResult, Evaluator


def _run_single_java(
    jar_path: str,
    java_bin: str,
    params: DesignParams,
    timeout: int,
) -> EvalResult:
    """在子进程中运行单次 Java 仿真（顶层函数，可被 ProcessPoolExecutor pickle）。"""
    t0 = time.perf_counter()
    cmd = [
        java_bin, "-jar", jar_path,
        "--D",  str(params.D),
        "--LD", str(params.L_D),
        "--rc", str(params.r_c),
        "--output-json",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.perf_counter() - t0

        stdout = proc.stdout.strip()
        if not stdout:
            return EvalResult(
                params=params,
                efficiency=math.nan,
                converged=False,
                runtime_s=elapsed,
                status="ERROR",
                metadata={"stderr": proc.stderr[:500]},
            )

        data = json.loads(stdout)
        efficiency = float(data.get("efficiency", math.nan))
        converged  = bool(data.get("converged", False))
        status     = "OK" if converged else "DIVERGED"

        return EvalResult(
            params=params,
            efficiency=efficiency,
            pressure_drop=float(data.get("pressure_drop", 0.0)),
            converged=converged,
            runtime_s=float(data.get("runtime_s", elapsed)),
            status=status,
            metadata=data,
        )

    except subprocess.TimeoutExpired:
        return EvalResult(
            params=params,
            efficiency=math.nan,
            converged=False,
            runtime_s=float(timeout),
            status="TIMEOUT",
        )
    except json.JSONDecodeError as e:
        elapsed = time.perf_counter() - t0
        return EvalResult(
            params=params,
            efficiency=math.nan,
            converged=False,
            runtime_s=elapsed,
            status="ERROR",
            metadata={"parse_error": str(e), "stdout": proc.stdout[:200]},
        )
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return EvalResult(
            params=params,
            efficiency=math.nan,
            converged=False,
            runtime_s=elapsed,
            status="ERROR",
            metadata={"exception": str(e)},
        )


class JavaEvaluator(Evaluator):
    """调用本地 VortexTube.jar 评估涡流管设计点。

    参数:
        jar_path  : VortexTube.jar 的路径
        java_bin  : java 可执行文件路径（默认 "java"）
        timeout   : 单次评估超时秒数（默认 60s）
        max_workers: 最大并发进程数（默认 min(batch_size, 32)）
    """

    def __init__(
        self,
        jar_path: str | Path,
        java_bin: str = "java",
        timeout: int = 60,
        max_workers: int | None = None,
    ):
        self.jar_path   = str(Path(jar_path).resolve())
        self.java_bin   = java_bin
        self.timeout    = timeout
        self.max_workers = max_workers

        # 启动时验证 jar 存在
        if not Path(self.jar_path).exists():
            raise FileNotFoundError(f"VortexTube.jar not found: {self.jar_path}")

    def evaluate_batch(self, params_list: list[DesignParams]) -> list[EvalResult]:
        """并发评估一批设计点。"""
        if not params_list:
            return []

        n_workers = min(
            len(params_list),
            self.max_workers if self.max_workers else 32,
        )

        # 若只有1个点，直接在当前进程运行（避免 fork 开销）
        if len(params_list) == 1:
            return [_run_single_java(self.jar_path, self.java_bin,
                                     params_list[0], self.timeout)]

        results_map: dict[int, EvalResult] = {}
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(
                    _run_single_java,
                    self.jar_path,
                    self.java_bin,
                    p,
                    self.timeout,
                ): idx
                for idx, p in enumerate(params_list)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    results_map[idx] = fut.result()
                except Exception as e:
                    results_map[idx] = EvalResult(
                        params=params_list[idx],
                        efficiency=math.nan,
                        converged=False,
                        status="ERROR",
                        metadata={"exception": str(e)},
                    )

        return [results_map[i] for i in range(len(params_list))]
