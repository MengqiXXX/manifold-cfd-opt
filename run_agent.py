"""
Phase 1 入口：LangGraph Agent 驱动 OpenFOAM 优化循环。

用法:
  python run_agent.py                              # 默认 config.yaml
  python run_agent.py --config config.yaml --evaluator openfoam
  python run_agent.py --config config.yaml --evaluator java   # 调试用
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from evaluators import DummyEvaluator, JavaEvaluator
from evaluators.openfoam_evaluator import OpenFOAMEvaluator
from optimization import BayesianOptimizer
from storage import ResultDatabase
from agents.graph import build_opt_graph, make_initial_state


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_llm_client(cfg: dict):
    """构建 OpenAI Compatible 客户端（可对接 vLLM/LiteLLM）。"""
    try:
        from openai import OpenAI

        base_url = cfg.get("llm_base_url")
        api_key = cfg.get("llm_api_key", "dummy")
        model = cfg.get("llm_model")
        if not base_url or not model:
            return None

        client = OpenAI(base_url=base_url, api_key=api_key)
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
            temperature=0,
        )
        print(f"  [LLM] 连接成功: {base_url}")
        return client
    except Exception as e:
        print(f"  [LLM] 不可用（{e}），将跳过 LLM 分析功能")
        return None


def main():
    parser = argparse.ArgumentParser(description="涡流管多Agent优化 Phase 1")
    parser.add_argument("--config",    default="config.yaml")
    parser.add_argument("--evaluator", default=None, help="覆盖 evaluator: java / openfoam / remote_openfoam / dummy")
    args = parser.parse_args()

    cfg = load_config(args.config)
    evaluator_type = args.evaluator or cfg.get("evaluator", "java")

    print("=" * 60)
    print("  涡流管多Agent优化 — Phase 1 (LangGraph)")
    print("=" * 60)
    print(f"  评估器  : {evaluator_type}")
    print(f"  预算    : {cfg.get('budget', 200)} 个设计点")
    print(f"  数据库  : {cfg['db_path']}")
    print("=" * 60)

    # 初始化组件
    db = ResultDatabase(cfg["db_path"])

    # LLM 客户端（可选）
    llm_client = build_llm_client(cfg) if cfg.get("llm_base_url") else None

    if evaluator_type == "openfoam":
        evaluator = OpenFOAMEvaluator(
            template_dir=cfg.get("openfoam_template_dir", "templates/vortex_tube_2d"),
            cases_base=cfg.get("openfoam_cases_dir", "cases"),
            n_cores=cfg.get("openfoam_cores_per_case", 16),
            timeout=cfg.get("timeout_s", 600),
            foam_source=cfg.get("foam_source", "/opt/openfoam11/etc/bashrc"),
        )
    elif evaluator_type == "remote_openfoam":
        from evaluators.remote_openfoam_evaluator import RemoteOpenFOAMEvaluator

        evaluator = RemoteOpenFOAMEvaluator(
            template_dir=cfg.get("openfoam_template_dir", "templates/vortex_tube_2d"),
            cases_base=cfg.get("openfoam_cases_dir", "cases"),
            n_cores=cfg.get("openfoam_cores_per_case", 16),
            timeout=cfg.get("timeout_s", 900),
            foam_source=cfg.get("foam_source", "/opt/openfoam13/etc/bashrc"),
            ssh_host=cfg.get("ssh_host", "127.0.0.1"),
            ssh_user=cfg.get("ssh_user", "user"),
            ssh_port=int(cfg.get("ssh_port", 22)),
            remote_base=cfg.get("remote_base", "~/vortex_cases"),
            llm_client=llm_client,
            llm_model=cfg.get("llm_model"),
        )
    elif evaluator_type == "dummy":
        evaluator = DummyEvaluator(
            noise=float(cfg.get("dummy_noise", 0.0)),
            sleep_s=float(cfg.get("dummy_sleep_s", 0.0)),
        )
    else:
        evaluator = JavaEvaluator(
            jar_path=cfg["jar_path"],
            java_bin=cfg.get("java_bin", "java"),
            timeout=cfg.get("timeout_s", 60),
        )

    optimizer = BayesianOptimizer(db=db, batch_size=cfg["batch_size"])

    # 初始 Sobol 采样（若数据库为空）
    if db.count() == 0:
        print(f"\n[初始采样] {cfg['n_initial']} 个 Sobol 点...")
        initial_params  = optimizer.initial_points(n=cfg["n_initial"])
        initial_results = evaluator.evaluate_batch(initial_params)
        db.save_batch(initial_results, run_id="init")
        n_ok = sum(1 for r in initial_results if r.is_valid())
        print(f"  完成: {n_ok}/{cfg['n_initial']} 有效")
    else:
        print(f"\n[恢复运行] 数据库已有 {db.count()} 条记录，跳过初始采样")

    # 构建并运行 LangGraph
    graph = build_opt_graph(evaluator, optimizer, db, cfg, llm_client)

    state = make_initial_state(cfg)
    # 将初始历史注入状态（从数据库恢复）
    best = db.get_best()
    if best:
        state["current_best"] = best

    t_start = time.perf_counter()
    print("\n[Agent] 启动 LangGraph 优化循环...")

    final_state = graph.invoke(state)

    elapsed = time.perf_counter() - t_start
    print("\n" + "=" * 60)
    print(f"  优化完成，总耗时: {elapsed:.1f}s")
    best = db.get_best()
    if best:
        print(f"  最优设计: {best.params!r}")
        print(f"  目标值  : {best.objective:.4f}")
    if final_state.get("report_path"):
        print(f"  报告    : {final_state['report_path']}")
    db.export_csv(cfg.get("csv_output", "results.csv"))
    print("=" * 60)


if __name__ == "__main__":
    main()
