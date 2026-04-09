from __future__ import annotations

import sys
from pathlib import Path

import yaml

from agents.report import generate_report
from evaluators.openfoam_evaluator import OpenFOAMEvaluator
from optimization import BayesianOptimizer
from storage import ResultDatabase


def main(cfg_path: str) -> None:
    cfg = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))

    db = ResultDatabase(cfg["db_path"])
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

    evaluator = OpenFOAMEvaluator(
        template_dir=cfg.get("openfoam_template_dir", "templates/vortex_tube_2d"),
        cases_base=cfg.get("openfoam_cases_dir", "cases"),
        n_cores=cfg.get("openfoam_cores_per_case", 8),
        timeout=cfg.get("timeout_s", 600),
        foam_source=cfg.get("foam_source", "/opt/openfoam13/etc/bashrc"),
        llm_client=llm_client,
        llm_model=llm_model,
    )

    batch_size = int(cfg.get("batch_size", 1))
    n_initial = int(cfg.get("n_initial", 1))
    optimizer = BayesianOptimizer(db=db, batch_size=batch_size)
    print(f"[bootstrap] initial points: {n_initial}")

    initial_params = optimizer.initial_points(n=n_initial)
    results = evaluator.evaluate_batch(initial_params)
    db.save_batch(results, run_id="once")

    history_db = ResultDatabase(cfg["db_path"])
    history = list(history_db.iter_all())
    best = history_db.get_best()
    report_path = cfg.get("report_path", "optimization_report_server.md")
    path = generate_report(history, best, report_path, llm_client, llm_model)
    print(f"[report] written: {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")

