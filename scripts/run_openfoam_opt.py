from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.report import generate_report
from evaluators import RemoteOpenFOAMEvaluator
from optimization import BayesianOptimizer
from storage import ResultDatabase


def main(cfg_path: str) -> None:
    cfg = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))

    n_initial = int(cfg.get("n_initial", 0))
    n_iterations = int(cfg.get("n_iterations", 0))
    batch_size = int(cfg.get("batch_size", 1))
    total = n_initial + n_iterations * batch_size

    db = ResultDatabase(cfg["db_path"])
    existing = db.count()

    llm_client = None
    llm_model = None
    try:
        base_url = cfg.get("llm_base_url")
        model = cfg.get("llm_model")
        api_key = cfg.get("llm_api_key", "dummy")
        if base_url and model:
            from openai import OpenAI

            llm_client = OpenAI(base_url=base_url, api_key=api_key)
            llm_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
                temperature=0,
            )
            llm_model = model
            print(f"[LLM] connected: {base_url}")
    except Exception as e:
        print(f"[LLM] unavailable: {e}")

    evaluator = RemoteOpenFOAMEvaluator(
        template_dir=cfg["template_dir"],
        cases_base=cfg.get("cases_base", "of_cases"),
        n_cores=int(cfg.get("n_cores", 8)),
        max_parallel_cases=int(cfg.get("max_parallel_cases", 1)),
        timeout=int(cfg.get("timeout_s", 900)),
        foam_source=cfg.get("foam_source", "/opt/openfoam13/etc/bashrc"),
        ssh_host=cfg.get("ssh_host", "192.168.110.10"),
        ssh_user=cfg.get("ssh_user", "liumq"),
        ssh_port=int(cfg.get("ssh_port", 22)),
        remote_base=cfg.get("remote_base", "~/manifold_cases"),
    )

    optimizer = BayesianOptimizer(db=db, batch_size=batch_size)

    t0 = time.perf_counter()
    print(f"[plan] n_initial={n_initial} n_iterations={n_iterations} batch_size={batch_size} total={total}")

    if existing == 0 and n_initial > 0:
        print(f"[bootstrap] initial points: {n_initial}")
        initial_params = optimizer.initial_points(n=n_initial)
        print(f"[bootstrap] params_ready: {len(initial_params)}")
        initial_results = evaluator.evaluate_batch(initial_params)
        print(f"[bootstrap] eval_done: {len(initial_results)}")
        db.save_batch(initial_results, run_id=f"init_{uuid.uuid4().hex[:8]}")
        print(f"[bootstrap] done, db_count={db.count()} valid={db.count_valid()}")

    it = 0
    while db.count() < total:
        it += 1
        remaining = total - db.count()
        q = batch_size if remaining >= batch_size else remaining
        print(f"[bo] iteration {it} (q={q})")
        params = optimizer.suggest_next_batch()[:q]
        results = evaluator.evaluate_batch(params)
        db.save_batch(results, run_id=f"bo_{it:03d}_{uuid.uuid4().hex[:6]}")
        print(f"[bo] saved, db_count={db.count()} valid={db.count_valid()}")

    csv_path = cfg.get("csv_output", "results.csv")
    db.export_csv(csv_path)

    history = db.load_all()
    best = db.get_best()
    report_path = cfg.get("report_path", "optimization_report_remote.md")
    path = generate_report(history, best, report_path, llm_client, llm_model)

    elapsed = time.perf_counter() - t0
    print(f"[done] elapsed_s={elapsed:.1f} db_count={db.count()} valid={db.count_valid()}")
    print(f"[csv] written: {csv_path}")
    print(f"[report] written: {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config_remote_openfoam.yaml")
