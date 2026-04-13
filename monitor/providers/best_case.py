from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Any

from .ssh_pool import is_connected, last_error, scp_get, ssh_exec


_lock = threading.Lock()
_state: dict[str, Any] = {"sig": None, "payload": None, "error": None}


def _iso(ts: dt.datetime) -> str:
    return ts.replace(microsecond=0).isoformat() + "Z"


def _artifacts_dir(root: Path) -> Path:
    v = os.getenv("MONITOR_ARTIFACTS_DIR")
    if v and v.strip():
        p = Path(v.strip()).expanduser()
        return p if p.is_absolute() else (root / p)
    return root / "monitor_artifacts"


def artifacts_best_case_dir(root: Path) -> Path:
    return _artifacts_dir(root) / "best-case"


def _find_results_dbs(root: Path) -> list[Path]:
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


def _best_row(db_path: Path) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(results)").fetchall() if r and "name" in r.keys()}
        if "objective" in cols:
            row = conn.execute(
                """
                SELECT id, objective, created_at, metadata
                FROM results
                WHERE converged=1 AND status='OK' AND objective IS NOT NULL AND objective = objective
                ORDER BY objective DESC
                LIMIT 1
                """
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id, COALESCE(delta_T, efficiency) AS objective, created_at, metadata
                FROM results
                WHERE converged=1 AND status='OK' AND COALESCE(delta_T, efficiency) IS NOT NULL
                ORDER BY COALESCE(delta_T, efficiency) DESC
                LIMIT 1
                """
            ).fetchone()
        if not row:
            return None
        md: dict[str, Any] = {}
        try:
            if row["metadata"]:
                md = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else {}
        except Exception:
            md = {}
        obj = float(row["objective"]) if row["objective"] is not None else math.nan
        return {
            "rowId": int(row["id"]),
            "objective": obj,
            "createdAt": str(row["created_at"]) if row["created_at"] is not None else None,
            "metadata": md,
        }
    finally:
        conn.close()


def _pick_best(root: Path) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_db: Path | None = None
    for db in _find_results_dbs(root):
        r = _best_row(db)
        if r is None:
            continue
        if best is None:
            best = r
            best_db = db
            continue
        if float(r.get("objective") or math.nan) > float(best.get("objective") or math.nan):
            best = r
            best_db = db
            continue
    if best is None or best_db is None:
        return None
    best["jobId"] = best_db.stem
    best["dbPath"] = str(best_db)
    return best


def _case_ref(case_dir: str) -> str:
    s = (case_dir or "").replace("\\", "/").rstrip("/")
    if s.endswith("/case"):
        s = s[: -len("/case")]
    return s.split("/")[-1] if s else "unknown"


def _is_probably_windows_path(p: str) -> bool:
    return bool(re.match(r"^[a-zA-Z]:\\", p or ""))


def _ensure_local_case(root: Path, case_dir: str) -> tuple[Path | None, str | None]:
    if not case_dir:
        return None, "missing case_dir"

    if _is_probably_windows_path(case_dir) and Path(case_dir).exists():
        return Path(case_dir).resolve(), None
    if (not _is_probably_windows_path(case_dir)) and Path(case_dir).exists():
        return Path(case_dir).resolve(), None

    if not is_connected():
        return None, (last_error() or "ssh not connected")

    case_ref = _case_ref(case_dir)
    out_root = artifacts_best_case_dir(root)
    local_case_root = out_root / "case"
    tar_local = Path(tempfile.gettempdir()) / f"{case_ref}.tgz"
    tar_remote = f"/tmp/{case_ref}.tgz"

    cmd = (
        "bash -lc "
        + repr(
            "set -e; "
            + f"cd {case_dir}; "
            + "latest=$(ls -1d [0-9]* 2>/dev/null | sort -V | tail -n 1); "
            + "if [ -z \"$latest\" ]; then latest=0; fi; "
            + f"tar -czf {tar_remote} constant system 0 $latest 2>/dev/null || tar -czf {tar_remote} .; "
            + f"echo {tar_remote}"
        )
    )
    ssh_exec(cmd, timeout=120)
    ok = scp_get(tar_remote, str(tar_local))
    if not ok or not tar_local.exists():
        return None, (last_error() or "scp_get failed")

    if local_case_root.exists():
        shutil.rmtree(local_case_root, ignore_errors=True)
    local_case_root.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(tar_local, "r:gz") as tf:
            tf.extractall(local_case_root)
    except Exception as e:
        return None, f"extract failed: {type(e).__name__}: {e}"
    finally:
        try:
            tar_local.unlink(missing_ok=True)
        except Exception:
            pass

    if (local_case_root / "case").exists():
        return (local_case_root / "case").resolve(), None
    return local_case_root.resolve(), None


def _foam_file(case_dir: Path) -> Path:
    foam = case_dir / "case.foam"
    if not foam.exists():
        foam.write_text("", encoding="utf-8")
    return foam


def _pvpython() -> str:
    v = os.getenv("PARAVIEW_PVPYTHON")
    return v.strip() if v and v.strip() else "pvpython"


def _render_case(root: Path, case_dir: Path) -> tuple[bool, str | None]:
    out_dir = artifacts_best_case_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    foam = _foam_file(case_dir)
    script = Path(__file__).resolve().parents[1] / "paraview" / "render_case.py"
    size = os.getenv("PARAVIEW_IMAGE_SIZE", "1400x800").strip() or "1400x800"
    timeout_s = int(os.getenv("PARAVIEW_TIMEOUT_S", "180"))
    try:
        import subprocess

        p = subprocess.run(
            [_pvpython(), str(script), "--foam", str(foam), "--out", str(out_dir), "--size", size],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        if int(p.returncode) != 0:
            err = (p.stderr or "")[-800:]
            out = (p.stdout or "")[-800:]
            return False, (err or out or f"pvpython failed (rc={p.returncode})")
        if not (out_dir / "velocity.png").exists() or not (out_dir / "pressure.png").exists():
            return False, "render outputs missing"
        return True, None
    except Exception as e:
        return False, f"render failed: {type(e).__name__}: {e}"


def _sig(best: dict[str, Any]) -> str:
    md = best.get("metadata") or {}
    case_dir = str(md.get("remote_case") or md.get("case_dir") or "")
    return f"{best.get('jobId')}:{best.get('rowId')}:{case_dir}"


def _payload(root: Path, best: dict[str, Any], case_ref: str) -> dict[str, Any]:
    ts = dt.datetime.utcnow()
    return {
        "jobId": best.get("jobId"),
        "caseRef": case_ref,
        "updatedAt": _iso(ts),
        "velocityImageUrl": "/artifacts/best-case/velocity.png",
        "pressureImageUrl": "/artifacts/best-case/pressure.png",
    }


def get_best_case(root: Path) -> dict[str, Any]:
    with _lock:
        if _state.get("payload") is not None:
            return {"ok": True, **(_state["payload"] or {})}
        err = _state.get("error")
    return {"ok": False, "error": err or "not_ready"}


def ensure_best_case_rendered(root: Path) -> dict[str, Any]:
    best = _pick_best(root)
    if best is None:
        with _lock:
            _state["sig"] = None
            _state["payload"] = None
            _state["error"] = "no_results"
        return {"ok": False, "error": "no_results"}

    md = best.get("metadata") or {}
    case_dir = str(md.get("remote_case") or md.get("case_dir") or "")
    case_ref = _case_ref(case_dir)
    sig = _sig(best)

    with _lock:
        if _state.get("sig") == sig and _state.get("payload") is not None:
            return {"ok": True, **(_state["payload"] or {})}

    local_case, err = _ensure_local_case(root, case_dir)
    if local_case is None:
        with _lock:
            _state["sig"] = sig
            _state["payload"] = None
            _state["error"] = err
        return {"ok": False, "error": err or "case_unavailable"}

    ok, render_err = _render_case(root, local_case)
    if not ok:
        with _lock:
            _state["sig"] = sig
            _state["payload"] = None
            _state["error"] = render_err
        return {"ok": False, "error": render_err or "render_failed"}

    payload = _payload(root, best, case_ref)
    with _lock:
        _state["sig"] = sig
        _state["payload"] = payload
        _state["error"] = None
    return {"ok": True, **payload}


async def best_case_loop(root: Path, interval_s: float) -> None:
    interval_s = max(1.0, min(60.0, float(interval_s)))
    while True:
        await asyncio.to_thread(ensure_best_case_rendered, root)
        await asyncio.sleep(interval_s)
