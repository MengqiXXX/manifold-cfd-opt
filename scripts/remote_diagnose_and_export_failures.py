from __future__ import annotations

import argparse
import shlex
import textwrap
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.ssh import SSHConfig, ssh_exec


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="192.168.110.10")
    p.add_argument("--user", default="liumq")
    p.add_argument("--db", default="/home/liumq/opt_runs/opt2000/results_opt_2000.sqlite")
    p.add_argument("--export-dir", default="/home/liumq/opt_runs/opt2000/failure_exports")
    p.add_argument("--n-per-status", type=int, default=3)
    return p.parse_args()


def _run(cfg: SSHConfig, script: str, timeout: int) -> tuple[int, str, str]:
    cmd = "bash -lc " + shlex.quote(script)
    return ssh_exec(cfg, cmd, timeout=timeout)


def main() -> None:
    args = _parse_args()
    cfg = SSHConfig(host=args.host, user=args.user)

    header = "\n".join(
        [
            "set -e",
            f"export DB={shlex.quote(args.db)}",
            f"export EXPORT_BASE={shlex.quote(args.export_dir)}",
            f"export N_PER_STATUS={int(args.n_per_status)}",
            "TS=$(date +%Y%m%d_%H%M%S)",
            'export OUTDIR="$EXPORT_BASE/$TS"',
            'mkdir -p "$OUTDIR"',
        ]
    )

    remote_py = (header + "\n\n" + textwrap.dedent(
        """
        python3 - <<'PY'
import json, math, os, sqlite3, subprocess, time
from collections import Counter
from pathlib import Path

DB = os.environ.get("DB")
OUTDIR = Path(os.environ.get("OUTDIR"))
N_PER_STATUS = int(os.environ.get("N_PER_STATUS", "3"))

def keyify(x):
    try:
        return json.dumps(x, ensure_ascii=False, sort_keys=True)
    except Exception:
        try:
            return str(x)
        except Exception:
            return "<unprintable>"

def softmax(xs):
    m = max(xs)
    ex = [math.exp(x - m) for x in xs]
    s = sum(ex)
    return [v / s for v in ex]

def derive_model(logit1, logit2, logit3, H=0.2, L=2.0, thickness=0.1, outlet_count=4, n_cells_x=200, n_cells_y_total=48, n_cells_z=1):
    logits = [float(logit1), float(logit2), float(logit3), 0.0]
    w = softmax(logits)
    y_levels = [0.0]
    acc = 0.0
    for i in range(outlet_count):
        acc += w[i]
        y_levels.append(H * acc)
    n_cells_y = [max(1, int(round(n_cells_y_total * wi))) for wi in w]
    diff = n_cells_y_total - sum(n_cells_y)
    if diff != 0:
        n_cells_y[0] += diff
    return {
        "params": {"logit_1": float(logit1), "logit_2": float(logit2), "logit_3": float(logit3)},
        "derived": {
            "L": L,
            "H": H,
            "thickness": thickness,
            "outlet_count": outlet_count,
            "outlet_weights": w,
            "y_levels": y_levels,
            "n_cells_x": n_cells_x,
            "n_cells_y": n_cells_y,
            "n_cells_z": n_cells_z,
        },
    }

def tar_case(case_dir: Path, out_tar: Path):
    case_root = case_dir.parent
    args = [
        "tar","-czf",str(out_tar),
        "-C",str(case_root),
        "--exclude=case/processor*",
        "--exclude=case/[0-9]*",
        "--exclude=case/[0-9]*.*",
        "case/system",
        "case/constant",
        "case/0",
        "case/postProcessing",
    ]
    for name in ["log.blockMesh","log.checkMesh","log.decomposePar","log.solver","log.reconstructPar"]:
        p = case_root / "case" / name
        if p.exists():
            args.append(f"case/{name}")
    subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

con = sqlite3.connect(DB)

rows = con.execute("select status, metadata from results where converged=0").fetchall()
status_counts = Counter()

missing_counts = Counter()
diag_counts = Counter()
for st, meta_s in rows:
    status_counts[keyify(st)] += 1
    try:
        md = json.loads(meta_s) if meta_s else {{}}
    except Exception:
        md = {{}}
    failure = md.get("failure") if isinstance(md.get("failure"), dict) else {{}}
    for m in (failure.get("missing") or []):
        missing_counts[keyify(m)] += 1
    pp = md.get("postprocess") if isinstance(md.get("postprocess"), dict) else {{}}
    for k,v in pp.items():
        if isinstance(v, dict) and v.get("diag"):
            d = v.get("diag")
            diag_counts[f"{keyify(k)}:{keyify(d)}"] += 1

summary = {{
    "status_counts": status_counts.most_common(20),
    "missing_top": missing_counts.most_common(30),
    "diag_top": diag_counts.most_common(30),
}}
(OUTDIR / "failure_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
print("OUTDIR", str(OUTDIR))
print("STATUS_COUNTS", summary["status_counts"][:10])
print("MISSING_TOP", summary["missing_top"][:10])
print("DIAG_TOP", summary["diag_top"][:10])

def pick_and_export(status: str, limit: int):
    picked = con.execute(
        "select rowid, logit_1,logit_2,logit_3,status,metadata from results where status=? limit ?",
        (status, int(limit)),
    ).fetchall()
    for rowid, l1, l2, l3, st, meta_s in picked:
        try:
            md = json.loads(meta_s) if meta_s else {{}}
        except Exception:
            md = {{}}
        tag = f"{int(rowid):06d}_{st}"
        dst = OUTDIR / tag
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "meta.json").write_text(json.dumps(md, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
        model = derive_model(l1,l2,l3)
        (dst / "model.json").write_text(json.dumps(model, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")

        case_path = md.get("remote_case")
        if case_path and Path(case_path).exists():
            case_dir = Path(case_path)
            (dst / "remote_case_path.txt").write_text(str(case_dir) + "\\n", encoding="utf-8")
            tar_case(case_dir, dst / "case_bundle.tgz")
            try:
                sys_dir = case_dir / "system"
                poly = case_dir / "constant" / "polyMesh"
                lines = []
                if sys_dir.exists():
                    lines.append("system/:")
                    lines.extend(sorted(p.name for p in sys_dir.glob("*"))[:50])
                if poly.exists():
                    lines.append("constant/polyMesh/:")
                    lines.extend(sorted(p.name for p in poly.glob("*"))[:50])
                (dst / "structure.txt").write_text("\\n".join(lines) + "\\n", encoding="utf-8")
            except Exception:
                pass

pick_and_export("POSTPROCESS_FAILED", N_PER_STATUS)
pick_and_export("RUN_MESH_FAILED", N_PER_STATUS)

con.close()
PY
        """
    ).strip())

    rc, out, err = _run(cfg, remote_py, timeout=300)
    print(out)
    if err.strip():
        print(err)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
