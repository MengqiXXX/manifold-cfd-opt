from __future__ import annotations

import datetime as dt
import math
import os
import sqlite3
from pathlib import Path
from typing import Any

import yaml


def _iso(ts: dt.datetime) -> str:
    return ts.replace(microsecond=0).isoformat() + "Z"


def _file_ts(p: Path) -> str | None:
    if not p.exists():
        return None
    return _iso(dt.datetime.utcfromtimestamp(p.stat().st_mtime))


def _load_yaml(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None


def _config_path(root: Path) -> Path:
    env = os.getenv("VORTEX_CONFIG")
    if env:
        p = Path(env).expanduser()
        return p if p.is_absolute() else (root / p)
    return root / "config.yaml"


def _find_job_dbs(root: Path) -> list[Path]:
    candidates = list(root.glob("results*.sqlite")) + list(root.glob("**/results*.sqlite"))
    seen: set[str] = set()
    out: list[Path] = []
    for p in candidates:
        try:
            rp = str(p.resolve())
        except Exception:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        out.append(p)
    out.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
    return out


def _query_db_summary(db_path: Path) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(results)").fetchall()
                if r and "name" in r.keys()
            }
            row_n = conn.execute("SELECT COUNT(*) AS n FROM results").fetchone()
            n = int(row_n["n"]) if row_n else 0
            row_last = conn.execute("SELECT created_at FROM results ORDER BY id DESC LIMIT 1").fetchone()
            last_ts = row_last["created_at"] if row_last else None
            best: dict[str, Any] | None = None

            if "objective" in cols and "logit_1" in cols:
                row_best = conn.execute(
                    """
                    SELECT logit_1, logit_2, logit_3, objective, created_at
                    FROM results
                    WHERE converged=1 AND status='OK' AND objective IS NOT NULL
                    ORDER BY objective DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row_best:
                    best = {
                        "logit_1": float(row_best["logit_1"]),
                        "logit_2": float(row_best["logit_2"]),
                        "logit_3": float(row_best["logit_3"]),
                        "objective": float(row_best["objective"]) if row_best["objective"] is not None else math.nan,
                        "createdAt": row_best["created_at"],
                    }
            else:
                row_best = conn.execute(
                    """
                    SELECT D, L_D, r_c,
                           COALESCE(delta_T, efficiency) AS objective,
                           created_at
                    FROM results
                    WHERE converged=1 AND status='OK' AND COALESCE(delta_T, efficiency) IS NOT NULL
                    ORDER BY COALESCE(delta_T, efficiency) DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row_best:
                    obj = row_best["objective"]
                    best = {
                        "D": float(row_best["D"]),
                        "L_D": float(row_best["L_D"]),
                        "r_c": float(row_best["r_c"]),
                        "objective": float(obj) if obj is not None else math.nan,
                        "createdAt": row_best["created_at"],
                    }

            return {"evaluated": n, "lastCreatedAt": last_ts, "best": best}
        finally:
            conn.close()
    except Exception:
        return None


def _derive_progress(cfg: dict[str, Any] | None, evaluated: int) -> tuple[int, list[dict[str, Any]]]:
    n_initial = int(cfg.get("n_initial", 0) if cfg else 0)
    n_iterations = cfg.get("n_iterations") if cfg else None
    batch_size = int(cfg.get("batch_size", 1) if cfg else 1)
    total_budget = cfg.get("budget") if cfg else None
    if total_budget is None and n_iterations is not None:
        total_budget = n_initial + int(n_iterations) * batch_size
    if total_budget is None:
        total_budget = max(evaluated, n_initial)
    total_budget = int(total_budget)
    pct = 0
    if total_budget > 0:
        pct = int(min(100, max(0, round(evaluated / total_budget * 100))))

    steps: list[dict[str, Any]] = []
    init_done = evaluated >= n_initial and n_initial > 0
    steps.append(
        {
            "id": "s1",
            "order": 1,
            "name": f"初始采样（{min(evaluated, n_initial)}/{n_initial}）" if n_initial > 0 else "初始采样",
            "status": "completed" if init_done else ("running" if evaluated > 0 else "pending"),
            "progressPct": int(min(100, round((evaluated / max(1, n_initial)) * 100))) if n_initial > 0 else 0,
        }
    )

    if n_iterations is not None:
        iter_total = int(n_iterations)
        after_init = max(0, evaluated - n_initial)
        iter_done = min(iter_total, after_init // max(1, batch_size))
        steps.append(
            {
                "id": "s2",
                "order": 2,
                "name": f"BO 迭代（{iter_done}/{iter_total} 轮）",
                "status": "completed" if iter_done >= iter_total else ("running" if after_init > 0 else "pending"),
                "progressPct": int(min(100, round((iter_done / max(1, iter_total)) * 100))),
            }
        )
    else:
        steps.append(
            {
                "id": "s2",
                "order": 2,
                "name": "优化迭代",
                "status": "running" if evaluated > n_initial else "pending",
                "progressPct": 0,
            }
        )

    steps.append(
        {
            "id": "s3",
            "order": 3,
            "name": "报告生成",
            "status": "pending",
            "progressPct": 0,
        }
    )
    return pct, steps


def _completed_work(root: Path, cfg: dict[str, Any] | None, db_path: Path) -> list[dict[str, Any]]:
    work: list[dict[str, Any]] = []

    def add_item(name: str, kind: str, ref: Path | None) -> None:
        if ref is None:
            return
        p = (root / ref).resolve() if not ref.is_absolute() else ref
        if not p.exists():
            return
        work.append(
            {
                "id": f"{kind}:{name}",
                "name": name,
                "kind": kind,
                "ref": str(p),
                "createdAt": _file_ts(p),
            }
        )

    add_item("结果数据库", "artifact", db_path)
    if cfg:
        csv_path = cfg.get("csv_output")
        report_path = cfg.get("report_path")
        add_item("结果 CSV", "artifact", Path(str(csv_path)) if csv_path else None)
        add_item("优化报告", "report", Path(str(report_path)) if report_path else None)
    return sorted(work, key=lambda x: x.get("createdAt") or "", reverse=True)


def get_job_list(root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for db in _find_job_dbs(root):
        job_id = db.stem
        cfg = _load_yaml(_config_path(root))
        summary = _query_db_summary(db) or {"evaluated": 0, "best": None}
        pct, _ = _derive_progress(cfg, int(summary.get("evaluated", 0)))
        status = "running" if pct < 100 else "completed"
        report_path = (cfg or {}).get("report_path")
        if report_path:
            rp = (root / Path(str(report_path))).resolve()
            if rp.exists() and pct >= 100:
                status = "completed"
        items.append(
            {
                "id": job_id,
                "name": job_id,
                "status": status,
                "startedAt": _file_ts(db) or _iso(dt.datetime.utcnow()),
                "endedAt": _file_ts(db) if status == "completed" else None,
                "durationMs": None,
            }
        )
    return items


def get_job_detail(root: Path, job_id: str) -> dict[str, Any] | None:
    db_path = None
    for db in _find_job_dbs(root):
        if db.stem == job_id:
            db_path = db
            break
    if db_path is None:
        return None

    cfg = _load_yaml(_config_path(root))
    summary = _query_db_summary(db_path) or {"evaluated": 0, "best": None}
    evaluated = int(summary.get("evaluated", 0))
    pct, steps = _derive_progress(cfg, evaluated)

    report_path = (cfg or {}).get("report_path")
    report_file = (root / Path(str(report_path))).resolve() if report_path else None
    if report_file and report_file.exists():
        for s in steps:
            if s["id"] == "s3":
                s["status"] = "completed"
                s["progressPct"] = 100
    status = "running" if pct < 100 else "completed"
    if report_file and report_file.exists() and pct >= 100:
        status = "completed"

    job = {
        "id": job_id,
        "name": job_id,
        "status": status,
        "startedAt": _file_ts(db_path) or _iso(dt.datetime.utcnow()),
        "endedAt": _file_ts(report_file) if report_file and report_file.exists() else None,
        "durationMs": None,
        "progressPct": pct,
        "evaluated": evaluated,
        "best": summary.get("best"),
    }
    return {"job": job, "steps": steps, "completedWork": _completed_work(root, cfg, db_path)}


def _query_history(db_path: Path) -> list[dict[str, Any]]:
    """Query all evaluated results ordered by id for history array."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT D, L_D, r_c, COALESCE(delta_T, efficiency) AS objective
                FROM results
                WHERE converged=1 AND status='OK' AND COALESCE(delta_T, efficiency) IS NOT NULL
                ORDER BY id ASC
                """
            ).fetchall()
            return [
                {
                    "objective": float(r["objective"]),
                    "params": {
                        "D": float(r["D"]),
                        "L_D": float(r["L_D"]),
                        "r_c": float(r["r_c"]),
                    },
                }
                for r in rows
            ]
        finally:
            conn.close()
    except Exception:
        return []


def _infer_current_phase(evaluated: int, n_initial: int, batch_size: int) -> str:
    """Infer current pipeline phase from progress."""
    if evaluated == 0:
        return "suggest"
    if evaluated < n_initial:
        return "cfd"
    after_init = evaluated - n_initial
    # Alternate between suggest and cfd in BO phase
    if after_init % max(1, batch_size) == 0:
        return "suggest"
    return "cfd"


def get_live_job_snapshot(root: Path) -> dict[str, Any]:
    """Return flat job snapshot matching the app.js WebSocket contract."""
    items = get_job_list(root)
    if not items:
        return {
            "status": "paused",
            "evaluated": 0,
            "budget": 0,
            "iteration": 0,
            "batch_size": 8,
            "best_objective": None,
            "best_params": None,
            "history": [],
            "current_phase": "suggest",
        }

    active = items[0]["id"]
    db_path = None
    for db in _find_job_dbs(root):
        if db.stem == active:
            db_path = db
            break
    if db_path is None:
        db_path = _find_job_dbs(root)[0] if _find_job_dbs(root) else root / "results.sqlite"

    cfg = _load_yaml(_config_path(root)) or {}
    summary = _query_db_summary(db_path) or {"evaluated": 0, "best": None}
    evaluated = int(summary.get("evaluated", 0))

    n_initial = int(cfg.get("n_initial", 0))
    n_iterations = cfg.get("n_iterations")
    batch_size = int(cfg.get("batch_size", 8))
    budget = cfg.get("budget")
    if budget is None and n_iterations is not None:
        budget = n_initial + int(n_iterations) * batch_size
    if budget is None:
        budget = max(evaluated, n_initial)
    budget = int(budget)

    after_init = max(0, evaluated - n_initial)
    iteration = after_init // max(1, batch_size) if after_init > 0 else 0

    pct = int(min(100, round(evaluated / max(1, budget) * 100)))
    status = "completed" if pct >= 100 else "running"

    best = summary.get("best")
    best_objective = float(best["objective"]) if best and best.get("objective") is not None else None
    best_params = (
        {"D": best["D"], "L_D": best["L_D"], "r_c": best["r_c"]}
        if best
        else None
    )

    history = _query_history(db_path)
    current_phase = _infer_current_phase(evaluated, n_initial, batch_size)

    return {
        "status": status,
        "evaluated": evaluated,
        "budget": budget,
        "iteration": iteration,
        "batch_size": batch_size,
        "best_objective": best_objective,
        "best_params": best_params,
        "history": history,
        "current_phase": current_phase,
    }
