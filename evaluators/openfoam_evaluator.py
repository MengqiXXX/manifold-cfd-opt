"""
Phase 1 评估器：参数化 OpenFOAM rhoSimpleFoam 算例。

工作流程：
  render_case(params) → blockMesh → decomposePar → mpirun rhoSimpleFoam
  → reconstructPar → extract_result → EvalResult

几何说明（2D 楔形轴对称）：
  r 方向: 0 → R = D/2
  z 方向: 0 → L = L_D * D
  楔角:   1°（标准 OpenFOAM 轴对称）
  冷端出口: z=0, r < r_c * R
  热端出口: z=L, r > 0.5*R（可调）
  切向入口: 管壁 r=R，中间截面注入
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from typing import Any

from .base import DesignParams, EvalResult, Evaluator


def _derive_mesh_params(params: DesignParams, n_r: int = 30, n_z: int = 80) -> dict:
    """由 DesignParams 派生所有模板变量。"""
    R   = params.D / 2.0
    L   = params.L_D * params.D
    r_c = params.r_c * R        # 冷端出口绝对半径 (m)

    # 楔形网格顶点（1° 楔角）
    theta = math.radians(0.5)
    y_pos =  R * math.sin(theta)
    y_neg = -R * math.sin(theta)
    z_cos =  R * math.cos(theta)

    # 网格节点数（随几何尺寸自适应调整）
    n_r_cold = max(4, round(n_r * params.r_c))
    n_r_hot  = n_r - n_r_cold

    return {
        # 基本几何
        "D":    params.D,
        "R":    R,
        "L":    L,
        "r_c":  r_c,
        "r_c_ratio": params.r_c,
        # 楔形顶点坐标
        "y_pos": y_pos,
        "y_neg": y_neg,
        "z_cos": z_cos,
        # 网格分辨率
        "n_r":      n_r,
        "n_z":      n_z,
        "n_r_cold": n_r_cold,
        "n_r_hot":  n_r_hot,
        # 初始/边界条件（标准空气，可覆盖）
        "T_in":     300.0,
        "p_in":     500000.0,   # 5 bar 入口压力
        "p_cold":   101325.0,   # 1 atm 冷端背压
        "p_hot":    101325.0,   # 1 atm 热端背压
        "U_theta":  100.0,      # 切向射流速度 (m/s)
        "k_in":     1.0,        # 湍流动能
        "omega_in": 1000.0,     # 比耗散率
    }


def _render_case(
    template_dir: str | Path,
    case_dir: str | Path,
    params: DesignParams,
) -> Path:
    """将 Jinja2 模板渲染到独立算例目录。"""
    template_dir = Path(template_dir)
    case_dir     = Path(case_dir)
    mesh_params  = _derive_mesh_params(params)

    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        keep_trailing_newline=True,
    )

    # 遍历模板目录，渲染 .j2 文件，直接复制其他文件
    for src in template_dir.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(template_dir)
        if src.suffix == ".j2":
            dst = case_dir / rel.with_suffix("")
        else:
            dst = case_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".j2":
            tmpl = env.get_template(str(rel).replace("\\", "/"))
            dst.write_text(tmpl.render(**mesh_params), encoding="utf-8")
        else:
            shutil.copy2(src, dst)

    return case_dir


def _extract_delta_T(case_dir: Path, params: DesignParams) -> tuple[float, float]:
    """从 postProcessing/ 目录提取 ΔT（冷端温降）和压降。

    返回 (delta_T, pressure_drop)；失败时返回 (nan, nan)。
    """
    try:
        # 查找最新时间步的 fieldAverages 或 surfaces 目录
        pp_dir = case_dir / "postProcessing"
        if not pp_dir.exists():
            return math.nan, math.nan

        # 尝试读取 coldEnd 和 hotEnd 的面平均温度
        cold_T = _read_patch_avg(pp_dir, "coldEnd",  "T")
        inlet_T = 300.0   # 入口温度（固定）
        if math.isnan(cold_T):
            return math.nan, math.nan

        delta_T  = inlet_T - cold_T          # 冷端温降 (K)，越大越好
        cold_p   = _read_patch_avg(pp_dir, "coldEnd", "p")
        inlet_p  = _read_patch_avg(pp_dir, "inlet",   "p")
        pressure_drop = (inlet_p - cold_p) if not math.isnan(cold_p) else math.nan

        return delta_T, pressure_drop
    except Exception:
        return math.nan, math.nan


def _read_patch_avg(pp_dir: Path, patch: str, field: str) -> float:
    """从 postProcessing/patchAvg/{patch}/{field} 读取最后时间步的均值。"""
    candidates = sorted((pp_dir / "patchAvg").glob(f"*/{patch}/{field}*"),
                        key=lambda p: p.stat().st_mtime)
    if not candidates:
        return math.nan
    try:
        lines = candidates[-1].read_text(encoding="utf-8").strip().splitlines()
        # 格式: time  value
        for line in reversed(lines):
            parts = line.split()
            if len(parts) >= 2:
                return float(parts[-1])
    except Exception:
        pass
    return math.nan


class OpenFOAMEvaluator(Evaluator):
    def __init__(
        self,
        template_dir: str | Path = "templates/vortex_tube_2d",
        cases_base: str | Path = "cases",
        n_cores: int = 16,
        max_iter: int = 1000,
        timeout: int = 600,
        max_workers: int | None = None,
        foam_source: str = "/opt/openfoam11/etc/bashrc",
        keep_failed: bool = True,
        llm_client: Any = None,
        llm_model: str | None = None,
    ):
        self.template_dir = str(Path(template_dir).resolve())
        self.cases_base   = str(Path(cases_base).resolve())
        self.n_cores      = n_cores
        self.max_iter     = max_iter
        self.timeout      = timeout
        self.max_workers  = max_workers
        self.foam_source  = foam_source
        self.keep_failed  = keep_failed
        self.llm_client   = llm_client
        self.llm_model    = llm_model

        Path(self.cases_base).mkdir(parents=True, exist_ok=True)

    def _qwen_debug(self, error_log: str, params: DesignParams) -> DesignParams:
        if not self.llm_client or not self.llm_model:
            return params
        
        prompt = f"""The OpenFOAM simulation diverged or crashed.
Current design parameters: D={params.D}, L_D={params.L_D}, r_c={params.r_c}.
Error log tail:
{error_log[-2000:]}

Please diagnose the issue and return a JSON with adjusted parameters to retry the simulation.
Your response must contain only valid JSON and no markdown formatting or explanation outside the JSON.
Example format:
{{"D": 0.02, "L_D": 10, "r_c": 0.3}}
"""
        try:
            resp = self.llm_client.chat.completions.create(
                model=self.llm_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.2,
            )
            content = resp.choices[0].message.content.strip()
            if content.startswith("```json"):
                content = content[7:-3].strip()
            import json
            new_p = json.loads(content)
            print(f"  [Qwen Debug] 诊断完成，建议新参数: {new_p}")
            return DesignParams(
                D=float(new_p.get("D", params.D)),
                L_D=float(new_p.get("L_D", params.L_D)),
                r_c=float(new_p.get("r_c", params.r_c)),
            )
        except Exception as e:
            print(f"  [Qwen Debug] 分析失败: {e}")
            return params

    def evaluate_batch(self, params_list: list[DesignParams]) -> list[EvalResult]:
        if not params_list:
            return []

        n_workers = min(
            len(params_list),
            self.max_workers or max(1, 512 // self.n_cores),
            32,
        )

        results_map: dict[int, EvalResult] = {}
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(self.evaluate, p): idx
                for idx, p in enumerate(params_list)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    results_map[idx] = fut.result()
                except Exception as e:
                    results_map[idx] = EvalResult(
                        params=params_list[idx],
                        efficiency=math.nan, delta_T=math.nan, pressure_drop=0.0,
                        converged=False, runtime_s=0.0, status="ERROR",
                        metadata={"exception": str(e)},
                    )

        return [results_map[i] for i in range(len(params_list))]

    def evaluate(self, params: DesignParams) -> EvalResult:
        import time, uuid, subprocess, math, shutil
        t0 = time.perf_counter()
        
        current_params = params
        max_retries = 2
        
        for retry in range(max_retries + 1):
            case_id  = f"run_{int(t0)}_{retry}_{uuid.uuid4().hex[:4]}"
            case_dir = Path(self.cases_base) / case_id
            
            try:
                _render_case(self.template_dir, case_dir, current_params)
                
                source_prefix = f"source {self.foam_source} && " if self.foam_source else ""

                prep_cmd = f"{source_prefix}blockMesh > log.blockMesh 2>&1 && {source_prefix}decomposePar > log.decomposePar 2>&1"
                subprocess.run(
                    ["bash", "-lc", prep_cmd],
                    cwd=str(case_dir),
                    timeout=300,
                    check=False,
                )

                solver_cmd = f"{source_prefix}mpirun -np {self.n_cores} rhoSimpleFoam -parallel > log.rhoSimpleFoam 2>&1"
                proc = subprocess.Popen(["bash", "-lc", solver_cmd], cwd=str(case_dir))
                
                crashed = False
                error_log = ""
                start_time = time.time()
                
                log_file = case_dir / "log.rhoSimpleFoam"
                
                while True:
                    ret = proc.poll()
                    
                    if time.time() - start_time > self.timeout:
                        proc.kill()
                        crashed = True
                        error_log = "TIMEOUT"
                        break
                        
                    if log_file.exists():
                        try:
                            tail = log_file.read_text(encoding="utf-8", errors="replace")[-2000:]
                            if "FOAM FATAL ERROR" in tail or "Floating point exception" in tail or "segmentation fault" in tail.lower():
                                proc.kill()
                                crashed = True
                                error_log = tail
                                break
                        except Exception:
                            pass
                            
                    if ret is not None:
                        if log_file.exists():
                            end_log = log_file.read_text(encoding="utf-8", errors="replace")[-1000:]
                            if "End" not in end_log and "Finalising" not in end_log and ret != 0:
                                crashed = True
                                error_log = end_log
                        else:
                            crashed = True
                            error_log = "No log generated"
                        break
                        
                    time.sleep(2.0)
                    
                if crashed:
                    print(f"  [Heartbeat] 进程异常或发散 (case={case_id})")
                    if retry < max_retries and self.llm_client and self.llm_model:
                        print(f"  [Heartbeat] 启动 Qwen Debug 诊断...")
                        current_params = self._qwen_debug(error_log, current_params)
                        shutil.rmtree(case_dir, ignore_errors=True)
                        continue
                    else:
                        status = "ERROR"
                        converged = False
                else:
                    subprocess.run(
                        ["bash", "-lc", f"{source_prefix}reconstructPar > log.reconstructPar 2>&1"],
                        cwd=str(case_dir),
                        timeout=300,
                        check=False,
                    )
                    status = "OK"
                    converged = True
                    
                if status == "OK":
                    delta_T, pressure_drop = _extract_delta_T(case_dir, current_params)
                    if not math.isfinite(delta_T):
                        status = "POSTPROCESS_ERROR"
                        converged = False
                else:
                    delta_T, pressure_drop = math.nan, 0.0
                    
                elapsed = time.perf_counter() - t0
                result = EvalResult(
                    params=current_params,
                    efficiency=delta_T if math.isfinite(delta_T) else math.nan,
                    delta_T=delta_T,
                    pressure_drop=pressure_drop if math.isfinite(pressure_drop) else 0.0,
                    converged=converged,
                    runtime_s=elapsed,
                    status=status,
                    metadata={"case_id": case_id, "error": error_log[-200:] if crashed else ""},
                )
                
            except subprocess.TimeoutExpired:
                elapsed = time.perf_counter() - t0
                result = EvalResult(
                    params=current_params, efficiency=math.nan, delta_T=math.nan, pressure_drop=0.0,
                    converged=False, runtime_s=elapsed, status="TIMEOUT",
                    metadata={"case_id": case_id},
                )
            except Exception as e:
                if retry < max_retries and self.llm_client and self.llm_model:
                    print(f"  [Heartbeat] 执行异常 ({e})，启动 Qwen Debug...")
                    current_params = self._qwen_debug(str(e), current_params)
                    shutil.rmtree(case_dir, ignore_errors=True)
                    continue
                elapsed = time.perf_counter() - t0
                result = EvalResult(
                    params=current_params, efficiency=math.nan, delta_T=math.nan, pressure_drop=0.0,
                    converged=False, runtime_s=elapsed, status="ERROR",
                    metadata={"case_id": case_id, "exception": str(e)},
                )
                
            finally:
                if "result" in locals() and result.status == "OK" and not self.keep_failed:
                    shutil.rmtree(case_dir, ignore_errors=True)
                    
            return result
