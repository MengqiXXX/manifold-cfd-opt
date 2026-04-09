from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from evaluators import DesignParams, RemoteOpenFOAMEvaluator
from optimization import BayesianOptimizer
from storage import ResultDatabase


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def print_banner(cfg: dict) -> None:
    print("=" * 72)
    print("  歧管多出口均匀性 + 压降 联合优化（OpenFOAM 远程评估）")
    print("=" * 72)
    print(f"  模板目录 : {cfg['template_dir']}")
    print(f"  远端主机 : {cfg['ssh_user']}@{cfg['ssh_host']}:{cfg.get('ssh_port', 22)}")
    print(f"  远端基目录: {cfg.get('remote_base', '~/manifold_cases')}")
    print(f"  初始点   : {cfg['n_initial']}")
    print(f"  迭代轮   : {cfg['n_iterations']} × 批次 {cfg['batch_size']}")
    total = cfg['n_initial'] + cfg['n_iterations'] * cfg['batch_size']
    print(f"  总评估   : ~{total} 个设计点")
    print(f"  数据库   : {cfg['db_path']}")
    print("=" * 72)


def print_result_row(i: int, total: int, r) -> None:
    status_icon = "OK" if r.converged else "NG"
    print(
        f"  [{i:3d}/{total}] {status_icon} "
        f"l=[{r.params.logit_1:+.2f},{r.params.logit_2:+.2f},{r.params.logit_3:+.2f}] "
        f"| cv={r.flow_cv:.4g} "
        f"| dp={r.pressure_drop:.3g}Pa "
        f"| obj={r.objective:.4g} "
        f"| {r.runtime_s:.1f}s "
        f"| {r.status}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Manifold CFD optimization (remote OpenFOAM)")
    parser.add_argument("--config", default="config_remote_openfoam.yaml", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只跑初始采样")
    args = parser.parse_args()

    cfg = load_config(args.config)
    print_banner(cfg)

    db = ResultDatabase(cfg["db_path"])
    evaluator = RemoteOpenFOAMEvaluator(
        template_dir=cfg["template_dir"],
        cases_base=cfg.get("cases_base", "of_cases"),
        n_cores=int(cfg.get("n_cores", 8)),
        timeout=int(cfg.get("timeout_s", 900)),
        foam_source=cfg.get("foam_source", "/opt/openfoam13/etc/bashrc"),
        ssh_host=cfg.get("ssh_host", "192.168.110.10"),
        ssh_user=cfg.get("ssh_user", "liumq"),
        ssh_port=int(cfg.get("ssh_port", 22)),
        remote_base=cfg.get("remote_base", "~/manifold_cases"),
    )
    optimizer = BayesianOptimizer(db=db, batch_size=int(cfg["batch_size"]))

    t_start = time.perf_counter()
    run_id = f"init_{uuid.uuid4().hex[:8]}"

    print(f"\n[阶段1] Sobol 初始采样 {cfg['n_initial']} 个设计点...")
    initial_params = optimizer.initial_points(n=int(cfg["n_initial"]))
    initial_results = evaluator.evaluate_batch(initial_params)
    db.save_batch(initial_results, run_id=run_id)

    n_ok = sum(1 for r in initial_results if r.is_valid())
    print(f"  完成: {n_ok}/{cfg['n_initial']} 个有效")
    for i, r in enumerate(initial_results, 1):
        print_result_row(i, cfg["n_initial"], r)

    best = db.get_best()
    if best:
        print(f"\n  当前最优: {best.params!r}  obj={best.objective:.4g}  cv={best.flow_cv:.4g}  dp={best.pressure_drop:.3g}Pa")

    if args.dry_run:
        db.export_csv(cfg.get("csv_output", "results.csv"))
        return

    print(f"\n[阶段2] BO 迭代 {cfg['n_iterations']} 轮...")
    prev_best_obj = best.objective if best else float("-inf")

    for iteration in range(1, int(cfg["n_iterations"]) + 1):
        iter_run_id = f"bo_{iteration:03d}_{uuid.uuid4().hex[:6]}"
        print(f"\n  --- 第 {iteration}/{cfg['n_iterations']} 轮 ---")

        next_params = optimizer.suggest_next_batch()
        results = evaluator.evaluate_batch(next_params)
        db.save_batch(results, run_id=iter_run_id)

        offset = int(cfg["n_initial"]) + (iteration - 1) * int(cfg["batch_size"])
        for i, r in enumerate(results, 1):
            print_result_row(offset + i, offset + int(cfg["batch_size"]), r)

        best = db.get_best()
        if best:
            improvement = best.objective - prev_best_obj
            print(f"\n  当前最优: {best.params!r}  obj={best.objective:.4g}  (Δ={improvement:+.4g})")
            prev_best_obj = best.objective

    elapsed = time.perf_counter() - t_start
    print("\n" + "=" * 72)
    print("  优化完成")
    print("=" * 72)
    print(f"  总耗时   : {elapsed:.1f}s")
    print(f"  总评估数 : {db.count()} 个设计点")
    print(f"  有效评估 : {db.count_valid()} 个")

    best = db.get_best()
    if best:
        print(f"\n  最优设计: {best.params!r}")
        print(f"  cv={best.flow_cv:.4g}  dp={best.pressure_drop:.3g}Pa  obj={best.objective:.4g}")

    csv_path = cfg.get("csv_output", "results.csv")
    db.export_csv(csv_path)
    print(f"\n  结果已保存: {cfg['db_path']}  /  {csv_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()

