"""
服务器端自治优化 Agent — 在 192.168.110.10 上直接运行。

架构：
  BoTorch (GPU) → OpenFOAM (512核CPU) → Qwen-72B调试/分析 (GPU)
  结果写入 ~/vortex_opt/results.sqlite，本地 dashboard 通过 SFTP 读取。

用法（服务器上）：
  cd ~/vortex_opt
  python3 run_server_agent.py
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from evaluators.base import DesignParams, EvalResult
from evaluators.openfoam_evaluator import _derive_mesh_params
from optimization.bayesian import BayesianOptimizer
from storage.database import ResultDatabase

# ── 配置 ──────────────────────────────────────────────────────────────────
CFG_PATH   = Path(__file__).parent / "config_server.yaml"
FOAM_SRC   = "/opt/openfoam13/etc/bashrc"
CASES_DIR  = Path(__file__).parent / "of_cases"
TMPL_DIR   = Path(__file__).parent / "templates/vortex_tube_2d"
CORES_PER  = 16          # 每个算例 MPI 进程数
MAX_ITER   = 500         # OpenFOAM 最大迭代步
TIMEOUT    = 900         # 单算例超时 (s)
LLM_URL    = "http://127.0.0.1:8001/v1"
LLM_MODEL  = "qwen2.5-72b"
LLM_KEY    = "dummy"
MAX_DEBUG_RETRIES = 2    # Qwen 调试重试次数


def load_cfg() -> dict:
    with open(CFG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── LLM 客户端 ─────────────────────────────────────────────────────────────
def build_llm():
    try:
        from openai import OpenAI
        client = OpenAI(base_url=LLM_URL, api_key=LLM_KEY)
        client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5, temperature=0,
        )
        print("  [LLM] Qwen-72B 连接成功")
        return client
    except Exception as e:
        print(f"  [LLM] 不可用: {e}")
        return None


# ── OpenFOAM 运行 ──────────────────────────────────────────────────────────
_SOLVER_CANDIDATES = [
    "rhoSimpleFoam",
    "foamRun -solver fluid",
    "rhoPimpleFoam",
]

def _source(cmd: str) -> str:
    return f"source {FOAM_SRC} && {cmd}"


def _run_cmd(cmd: str, cwd: Path, timeout: int = TIMEOUT) -> tuple[int, str, str]:
    r = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        cwd=str(cwd), timeout=timeout,
        executable="/bin/bash",
    )
    return r.returncode, r.stdout, r.stderr


def render_case(case_dir: Path, params: DesignParams, max_iter: int) -> None:
    """渲染 Jinja2 模板到 case 目录，包含 max_iter 参数。"""
    import shutil as _shutil
    from jinja2 import Environment, FileSystemLoader, Undefined

    mesh_params = _derive_mesh_params(params)
    mesh_params["max_iter"] = max_iter

    env = Environment(
        loader=FileSystemLoader(str(TMPL_DIR)),
        keep_trailing_newline=True,
        undefined=Undefined,
    )

    for src in TMPL_DIR.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(TMPL_DIR)
        if src.suffix == ".j2":
            dst = case_dir / rel.with_suffix("")
        else:
            dst = case_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".j2":
            tmpl = env.get_template(str(rel).replace("\\", "/"))
            dst.write_text(tmpl.render(**mesh_params), encoding="utf-8")
        else:
            _shutil.copy2(src, dst)


def detect_solver() -> str:
    """探测 OF13 可用的稳态可压缩求解器。"""
    for sol in _SOLVER_CANDIDATES:
        binary = sol.split()[0]
        rc, _, _ = _run_cmd(_source(f"which {binary}"), Path("/tmp"), timeout=5)
        if rc == 0:
            print(f"  [OF] 求解器: {sol}")
            return sol
    print("  [OF] 未找到合适求解器，默认使用 rhoSimpleFoam")
    return "rhoSimpleFoam"


SOLVER = None   # 延迟检测


def run_foam_case(
    case_dir: Path,
    n_cores: int,
    solver: str,
    llm_client,
    max_debug: int = MAX_DEBUG_RETRIES,
) -> tuple[bool, str]:
    """运行 OpenFOAM 算例，失败时交给 Qwen 诊断修复，最多重试 max_debug 次。"""
    log_file = case_dir / "run.log"

    def write_log(text: str) -> None:
        with open(log_file, "a") as f:
            f.write(text + "\n")

    steps = [
        ("blockMesh",    _source("blockMesh")),
        ("decomposePar", _source("decomposePar")),
        ("solver",       _source(f"mpirun -np {n_cores} {solver} -parallel")),
        ("reconstruct",  _source("reconstructPar -latestTime")),
    ]

    for attempt in range(max_debug + 1):
        write_log(f"\n{'='*50}\n attempt {attempt}\n{'='*50}")
        failed_step = None
        failed_log  = ""

        for step_name, cmd in steps:
            rc, out, err = _run_cmd(cmd, case_dir, timeout=TIMEOUT)
            write_log(f"\n=== {step_name} ===\n{out}\n{err}")
            if rc != 0:
                failed_step = step_name
                failed_log  = (out + err)[-3000:]
                break

        if failed_step is None:
            return True, "converged"

        if llm_client is None or attempt >= max_debug:
            return False, f"{failed_step} failed after {attempt+1} attempts"

        # ── 请 Qwen 诊断 ────────────────────────────────────────────
        print(f"    [Qwen] 诊断 {failed_step} 失败...")
        fix = qwen_debug(llm_client, failed_step, failed_log, case_dir)
        if not fix or not fix.get("can_retry"):
            return False, f"{failed_step}: Qwen 建议放弃 — {fix.get('diagnosis','')}"

        # 应用 Qwen 建议的文件修改
        applied = apply_fixes(fix.get("file_patches", []), case_dir)
        print(f"    [Qwen] 已应用 {applied} 处修改，重试...")

    return False, "exceeded debug retries"


def qwen_debug(llm_client, step: str, error_log: str, case_dir: Path) -> dict:
    """将 OpenFOAM 错误日志发给 Qwen，获取诊断和修复建议。"""
    # 收集相关文件内容作为上下文
    extra = ""
    for fname in ["system/controlDict", "system/blockMeshDict",
                  "constant/thermophysicalProperties",
                  "constant/physicalProperties"]:
        p = case_dir / fname
        if p.exists():
            extra += f"\n--- {fname} ---\n{p.read_text()[:800]}\n"

    prompt = f"""你是 OpenFOAM 专家。以下是 OpenFOAM 13（使用 /opt/openfoam13）在步骤 [{step}] 的错误日志：

```
{error_log}
```

相关文件：
{extra}

请诊断问题并给出精确的修复方案。以 JSON 格式回复（只输出 JSON，不要其他文字）：
{{
  "diagnosis": "问题描述（1-2句）",
  "can_retry": true/false,
  "file_patches": [
    {{
      "relative_path": "system/controlDict",
      "action": "replace_line",
      "old": "application     rhoSimpleFoam;",
      "new": "application     foamRun;"
    }}
  ]
}}
"""
    try:
        resp = llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500, temperature=0.1,
        )
        text = resp.choices[0].message.content.strip()
        # 提取 JSON（可能包含 markdown 代码块）
        m = re.search(r'\{[\s\S]+\}', text)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"    [Qwen] 解析失败: {e}")
    return {"diagnosis": "parse error", "can_retry": False, "file_patches": []}


def apply_fixes(patches: list[dict], case_dir: Path) -> int:
    """应用 Qwen 建议的文件修改，返回成功修改数。"""
    applied = 0
    for patch in patches:
        try:
            p = case_dir / patch["relative_path"]
            if not p.exists():
                continue
            text = p.read_text()
            action = patch.get("action", "replace_line")
            if action in ("replace_line", "replace"):
                new_text = text.replace(patch["old"], patch["new"], 1)
            elif action == "append":
                new_text = text + "\n" + patch["new"]
            elif action == "prepend":
                new_text = patch["new"] + "\n" + text
            else:
                continue
            if new_text != text:
                p.write_text(new_text)
                applied += 1
        except Exception:
            continue
    return applied


def extract_delta_T(case_dir: Path) -> tuple[float, float]:
    """从 postProcessing 提取冷端温降 ΔT (K) 和压降 (Pa)。"""
    pp = case_dir / "postProcessing"
    if not pp.exists():
        return math.nan, math.nan

    def read_avg(obj_name: str, field: str) -> float:
        for d in sorted(pp.glob(f"{obj_name}/*"), reverse=True):
            for fname in sorted(d.glob(f"*{field}*"), reverse=True):
                try:
                    for line in reversed(fname.read_text().splitlines()):
                        parts = line.split()
                        if len(parts) >= 2:
                            return float(parts[-1])
                except Exception:
                    pass
        return math.nan

    cold_T  = read_avg("coldEndAvg",  "T")
    inlet_T = read_avg("inletAvg",    "T")
    inlet_p = read_avg("inletAvg",    "p")
    cold_p  = read_avg("coldEndAvg",  "p")

    t_in = inlet_T if math.isfinite(inlet_T) else 300.0
    delta_T = t_in - cold_T if math.isfinite(cold_T) else math.nan
    dp      = inlet_p - cold_p if (math.isfinite(inlet_p) and math.isfinite(cold_p)) else math.nan
    return delta_T, dp


# ── 单算例完整流程 ──────────────────────────────────────────────────────────
def evaluate_one(
    params: DesignParams,
    solver: str,
    llm_client,
    max_iter: int = MAX_ITER,
) -> EvalResult:
    t0 = time.perf_counter()
    case_id  = f"of_{uuid.uuid4().hex[:8]}"
    case_dir = CASES_DIR / case_id

    # 渲染模板（包含 max_iter）
    render_case(case_dir, params, max_iter)

    try:
        converged, msg = run_foam_case(case_dir, CORES_PER, solver, llm_client)
        elapsed = time.perf_counter() - t0

        if converged:
            delta_T, dp = extract_delta_T(case_dir)
            status = "OK" if math.isfinite(delta_T) else "POSTPROCESS_ERROR"
        else:
            delta_T, dp = math.nan, math.nan
            status = "DIVERGED"

        result = EvalResult(
            params=params,
            efficiency=math.nan,
            delta_T=delta_T,
            pressure_drop=dp if math.isfinite(dp) else 0.0,
            converged=converged and math.isfinite(delta_T),
            runtime_s=elapsed,
            status=status,
            metadata={"case_id": case_id, "msg": msg},
        )
    except Exception as e:
        elapsed = time.perf_counter() - t0
        result = EvalResult(
            params=params, efficiency=math.nan, delta_T=math.nan,
            converged=False, runtime_s=elapsed, status="ERROR",
            metadata={"case_id": case_id, "exception": str(e)},
        )
    finally:
        # 保留失败算例供人工检查，清理成功算例
        if result.status == "OK":
            shutil.rmtree(case_dir, ignore_errors=True)

    return result


def evaluate_batch(
    params_list: list[DesignParams],
    solver: str,
    llm_client,
) -> list[EvalResult]:
    """并发运行一批算例（ThreadPool，每个算例独立 MPI 进程组）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    n = len(params_list)
    # 最大并发 = 512核 // 每算例核数，但不超过 batch_size
    max_workers = min(n, max(1, 512 // CORES_PER))
    results: list[EvalResult | None] = [None] * n

    print(f"  [OF] 并行启动 {n} 个算例（{max_workers} 并发 × {CORES_PER} 核）")

    def _run(idx: int, p: DesignParams) -> tuple[int, EvalResult]:
        time.sleep(idx * 0.5)   # 错开启动避免 blockMesh 冲突
        return idx, evaluate_one(p, solver, llm_client)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_run, i, p): i for i, p in enumerate(params_list)}
        for fut in as_completed(futs):
            try:
                idx, res = fut.result()
                results[idx] = res
                icon = "✓" if res.status == "OK" else "✗"
                dT   = f"ΔT={res.delta_T:.2f}K" if math.isfinite(res.delta_T) else res.status
                print(f"    [{icon}] case {idx}: {dT}")
            except Exception as e:
                idx = futs[fut]
                results[idx] = EvalResult(
                    params=params_list[idx], efficiency=math.nan, delta_T=math.nan,
                    converged=False, status="ERROR", metadata={"exception": str(e)},
                )

    return [r for r in results if r is not None]


# ── 主循环 ──────────────────────────────────────────────────────────────────
def main() -> None:
    global SOLVER
    cfg = load_cfg()
    CASES_DIR.mkdir(parents=True, exist_ok=True)

    db_path = Path(cfg.get("db_path", "/home/liumq/vortex_opt/results.sqlite"))
    db = ResultDatabase(str(db_path))
    optimizer = BayesianOptimizer(db=db, batch_size=cfg["batch_size"])
    llm = build_llm()
    SOLVER = detect_solver()

    n_initial    = cfg["n_initial"]
    n_iterations = cfg["n_iterations"]
    batch_size   = cfg["batch_size"]
    budget       = n_initial + n_iterations * batch_size

    print(f"\n{'='*60}")
    print(f"  涡流管 OpenFOAM 优化 — 服务器自治 Agent")
    print(f"  求解器: {SOLVER}")
    print(f"  预算: {budget} 个设计点  |  {n_initial} 初始 + {n_iterations}×{batch_size} BO")
    print(f"  并发: {512 // CORES_PER} 算例 × {CORES_PER} 核 = 512 核")
    print(f"{'='*60}\n")

    # 初始 Sobol 采样
    if db.count() < n_initial:
        remaining = n_initial - db.count()
        print(f"[初始采样] {remaining} 个 Sobol 点...")
        params = optimizer.initial_points(n=remaining)
        results = evaluate_batch(params, SOLVER, llm)
        db.save_batch(results, run_id="init")
        n_ok = sum(1 for r in results if r.is_valid())
        print(f"  完成: {n_ok}/{remaining} 有效")
    else:
        print(f"[恢复] 数据库已有 {db.count()} 条，跳过初始采样")

    # BO 迭代
    conv_count = 0
    best_obj   = db.get_best().objective if db.get_best() else 0.0

    for it in range(1, n_iterations + 1):
        print(f"\n[BO 第 {it}/{n_iterations} 轮]  已评估: {db.count()}/{budget}")
        params  = optimizer.suggest_next_batch()
        results = evaluate_batch(params, SOLVER, llm)
        db.save_batch(results, run_id=f"bo_{it:03d}")

        best = db.get_best()
        if best:
            delta = best.objective - best_obj
            print(f"  最优 ΔT = {best.objective:.3f} K  (Δ={delta:+.3f})")
            tol = cfg.get("convergence_tol", 0.001)
            conv_count = conv_count + 1 if delta < tol else 0
            if conv_count >= cfg.get("convergence_patience", 5):
                print(f"  [收敛] 连续 {conv_count} 轮改善 < {tol}K，停止")
                break
            best_obj = best.objective

    # 最终报告（Qwen 撰写）
    if llm:
        print("\n[报告] Qwen 正在生成优化报告...")
        from agents.report import generate_report
        generate_report(
            history=list(db._conn_all()),
            best=db.get_best(),
            output_path=str(Path(__file__).parent / "optimization_report.md"),
            llm_client=llm,
            llm_model=LLM_MODEL,
        )

    db.export_csv(cfg.get("csv_output", "results.csv"))
    best = db.get_best()
    print(f"\n{'='*60}")
    print(f"  完成  |  总评估 {db.count()}  |  最优 ΔT={best.objective:.3f}K" if best else "  完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
