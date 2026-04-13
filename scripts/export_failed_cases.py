from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluators.base import DesignParams
from evaluators.remote_openfoam_evaluator import _derive_mesh_params, _derive_mesh_params_3d


@dataclass(frozen=True)
class CaseRow:
    rowid: int
    logit_1: float
    logit_2: float
    logit_3: float
    status: str
    metadata: dict


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--status", action="append", default=[])
    p.add_argument("--n", type=int, default=3)
    p.add_argument("--include-time-dirs", action="store_true", default=False)
    p.add_argument("--template-is-3d", action="store_true", default=False)
    return p.parse_args()


def _load_rows(db_path: Path, statuses: list[str], n: int) -> list[CaseRow]:
    con = sqlite3.connect(str(db_path))
    if statuses:
        q = ",".join(["?"] * len(statuses))
        rows = con.execute(
            f"select rowid, logit_1,logit_2,logit_3,status,metadata from results where status in ({q}) limit ?",
            (*statuses, int(n)),
        ).fetchall()
    else:
        rows = con.execute(
            "select rowid, logit_1,logit_2,logit_3,status,metadata from results where converged=0 limit ?",
            (int(n),),
        ).fetchall()
    con.close()
    out: list[CaseRow] = []
    for rowid, l1, l2, l3, st, meta_s in rows:
        try:
            md = json.loads(meta_s) if meta_s else {}
        except Exception:
            md = {}
        out.append(CaseRow(int(rowid), float(l1), float(l2), float(l3), str(st), md))
    return out


def _write_model_files(dst_dir: Path, params: DesignParams, template_is_3d: bool) -> dict:
    if template_is_3d:
        ctx = _derive_mesh_params_3d(params, outlet_count=4)
    else:
        ctx = _derive_mesh_params(params, outlet_count=4)
    model = {
        "params": {"logit_1": params.logit_1, "logit_2": params.logit_2, "logit_3": params.logit_3},
        "derived": {k: ctx.get(k) for k in ["L", "H", "W", "thickness", "outlet_count", "outlet_weights", "y_levels", "n_cells_x", "n_cells_y", "n_cells_z"]},
    }
    (dst_dir / "model.json").write_text(json.dumps(model, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return model


def _tar_case(case_dir: Path, out_tar: Path, include_time_dirs: bool) -> None:
    case_root = case_dir.parent
    args = [
        "tar",
        "-czf",
        str(out_tar),
        "-C",
        str(case_root),
    ]
    if not include_time_dirs:
        args.extend(["--exclude=case/[0-9]*", "--exclude=case/[0-9]*.*"])
    args.extend(
        [
            "--exclude=case/processor*",
            "--exclude=case/constant/polyMesh/sets",
            "case/system",
            "case/constant",
            "case/0",
            "case/postProcessing",
        ]
    )
    for name in ["log.blockMesh", "log.checkMesh", "log.decomposePar", "log.solver", "log.reconstructPar", "log.snappyHexMesh", "log.surfaceFeatures"]:
        p = case_root / "case" / name
        if p.exists():
            args.append(f"case/{name}")
    subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    args = _parse_args()
    db = Path(args.db)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = _load_rows(db, statuses=list(args.status or []), n=int(args.n))
    manifest = []
    for r in rows:
        rid = f"{r.rowid:06d}"
        tag = f"{rid}_{r.status}"
        dst = out_dir / tag
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "meta.json").write_text(json.dumps(r.metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        params = DesignParams(r.logit_1, r.logit_2, r.logit_3)
        model = _write_model_files(dst, params, template_is_3d=bool(args.template_is_3d))

        case_path = r.metadata.get("remote_case")
        case_dir = Path(case_path) if case_path else None
        tar_path = None
        if case_dir and case_dir.exists() and (case_dir / "system").exists():
            tar_path = dst / "case_bundle.tgz"
            _tar_case(case_dir, tar_path, include_time_dirs=bool(args.include_time_dirs))
        manifest.append(
            {
                "rowid": r.rowid,
                "status": r.status,
                "params": {"logit_1": r.logit_1, "logit_2": r.logit_2, "logit_3": r.logit_3},
                "remote_case": case_path,
                "bundle": str(tar_path) if tar_path else None,
                "derived": model.get("derived"),
            }
        )

    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(out_dir))


if __name__ == "__main__":
    main()

