from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.ssh import SSHConfig, ssh_exec


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="192.168.110.10")
    p.add_argument("--user", default="liumq")
    p.add_argument("--db", default="/home/liumq/opt_runs/opt2000/results_opt_2000.sqlite")
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--out-md", default="docs/opt2000_postprocess_failed_audit.md")
    p.add_argument("--run-dir", default="/home/liumq/opt_runs/opt2000")
    return p.parse_args()


def _ssh(cfg: SSHConfig, bash_script: str, timeout: int) -> tuple[int, str, str]:
    cmd = "bash -lc " + shlex.quote(bash_script)
    return ssh_exec(cfg, cmd, timeout=timeout)


def _fetch_samples(cfg: SSHConfig, db: str, n: int) -> dict:
    remote = f"""set -e
python3 - <<'PY'
import json, os, re, sqlite3
from pathlib import Path

DB = {db!r}
N = int({n})

time_re = re.compile(r"^\\d+(?:\\.\\d+)?$")

def evidence(case_dir: Path):
  ev = {{
    "case_dir": str(case_dir),
    "exists": case_dir.exists(),
    "has_log_solver": (case_dir / "log.solver").exists(),
    "has_log_pipeline": (case_dir / "log.pipeline").exists(),
    "has_log_reconstruct": (case_dir / "log.reconstructPar").exists(),
    "has_postProcessing": (case_dir / "postProcessing").exists(),
    "has_processor0": (case_dir / "processor0").exists(),
    "time_dirs": [],
    "reconstruct_tail": "",
    "pipeline_tail": "",
    "solver_tail": "",
  }}
  if not case_dir.exists():
    return ev
  try:
    names = [p.name for p in case_dir.iterdir() if p.is_dir()]
  except Exception:
    names = []
  td = []
  for nm in names:
    if nm in ("system","constant","postProcessing") or nm.startswith("processor"):
      continue
    if time_re.match(nm):
      try:
        if float(nm) <= 0.0:
          continue
      except Exception:
        continue
      td.append(nm)
  td_sorted = sorted(td, key=lambda x: float(x))
  ev["time_dirs"] = td_sorted[:50]
  def tail(p: Path, n_lines: int = 80):
    try:
      lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
      return "\\n".join(lines[-n_lines:])
    except Exception:
      return ""
  if (case_dir / "log.reconstructPar").exists():
    ev["reconstruct_tail"] = tail(case_dir / "log.reconstructPar", 120)
  if (case_dir / "log.pipeline").exists():
    ev["pipeline_tail"] = tail(case_dir / "log.pipeline", 200)
  if (case_dir / "log.solver").exists():
    ev["solver_tail"] = tail(case_dir / "log.solver", 200)
  return ev

con = sqlite3.connect(DB)
rows = con.execute("select rowid, logit_1,logit_2,logit_3,status,metadata from results where status='POSTPROCESS_FAILED' order by rowid limit ?", (N,)).fetchall()
out = []
for rowid, l1, l2, l3, status, meta_s in rows:
  try:
    md = json.loads(meta_s) if meta_s else {{}}
  except Exception:
    md = {{}}
  case = md.get("remote_case")
  case_dir = Path(case) if case else Path("/nonexistent")
  fail = md.get("failure") if isinstance(md.get("failure"), dict) else {{}}
  out.append({{
    "rowid": int(rowid),
    "status": status,
    "logits": [float(l1), float(l2), float(l3)],
    "missing": fail.get("missing"),
    "remote_case": case,
    "evidence": evidence(case_dir),
  }})
con.close()

print(json.dumps({{"db": DB, "n": N, "samples": out}}, ensure_ascii=False))
PY
"""
    rc, out, err = _ssh(cfg, remote, timeout=180)
    if rc != 0:
        raise RuntimeError(f"remote rc={rc}\n{out}\n{err}")
    return json.loads(out.strip().splitlines()[-1])


def _analyze(samples: list[dict]) -> dict:
    stat = {
        "n": len(samples),
        "no_time_dirs": 0,
        "no_log_solver": 0,
        "no_postProcessing": 0,
        "reconstruct_no_time_selected_constant": 0,
        "missing_counter": {},
    }
    miss = {}
    for s in samples:
        ev = s.get("evidence") or {}
        if not (ev.get("time_dirs") or []):
            stat["no_time_dirs"] += 1
        if not ev.get("has_log_solver"):
            stat["no_log_solver"] += 1
        if not ev.get("has_postProcessing"):
            stat["no_postProcessing"] += 1
        rt = (ev.get("reconstruct_tail") or "")
        if "No time specified or available, selecting 'constant'" in rt:
            stat["reconstruct_no_time_selected_constant"] += 1
        missing = s.get("missing") or []
        if isinstance(missing, list):
            for m in missing:
                k = str(m)
                miss[k] = miss.get(k, 0) + 1
    stat["missing_counter"] = dict(sorted(miss.items(), key=lambda kv: (-kv[1], kv[0])))
    return stat


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", "<br>")


def _render_md(db: str, samples: list[dict], stat: dict) -> str:
    lines: list[str] = []
    lines.append("# opt2000：POSTPROCESS_FAILED 抽样审计（10例）")
    lines.append("")
    lines.append(f"- 数据库：`{db}`")
    lines.append(f"- 抽样数：`{stat['n']}`（按 rowid 最小的前 N 条）")
    lines.append("")
    lines.append("## 统计结论")
    lines.append("")
    lines.append(f"- 无时间步目录（无 `1/2/…` 或 `0.5/…`）：`{stat['no_time_dirs']}/{stat['n']}`")
    lines.append(f"- 缺失 `log.solver`：`{stat['no_log_solver']}/{stat['n']}`")
    lines.append(f"- `reconstructPar` 提示无可用时间步并选择 constant：`{stat['reconstruct_no_time_selected_constant']}/{stat['n']}`")
    lines.append(f"- 缺失 `postProcessing/`：`{stat['no_postProcessing']}/{stat['n']}`")
    lines.append("")
    lines.append("**missing 字段出现频次（来自 failure.missing）**")
    lines.append("")
    if stat["missing_counter"]:
        for k, v in list(stat["missing_counter"].items())[:20]:
            lines.append(f"- `{k}`: {v}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 样本证据（逐例）")
    lines.append("")
    lines.append("| rowid | logits | case_dir | time_dirs | has log.solver | has postProcessing | reconstructPar 关键提示 |")
    lines.append("|---:|---|---|---|---|---|---|")
    for s in samples:
        ev = s.get("evidence") or {}
        logits = s.get("logits") or []
        lt = ", ".join(f"{x:.3f}" for x in logits) if logits else ""
        td = ev.get("time_dirs") or []
        td_show = ", ".join(td[:8]) + ("…" if len(td) > 8 else "")
        rt = ev.get("reconstruct_tail") or ""
        hint = ""
        if "No time specified or available, selecting 'constant'" in rt:
            hint = "No time… selecting constant"
        elif rt:
            hint = "有 log.reconstructPar（无 constant 提示）"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(s.get("rowid")),
                    _md_escape(lt),
                    _md_escape(str(ev.get("case_dir") or "")),
                    _md_escape(td_show),
                    "Y" if ev.get("has_log_solver") else "N",
                    "Y" if ev.get("has_postProcessing") else "N",
                    _md_escape(hint),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## 代表性证据片段（节选）")
    lines.append("")
    for s in samples[:3]:
        ev = s.get("evidence") or {}
        lines.append(f"### rowid {s.get('rowid')}")
        lines.append("")
        lines.append("**reconstructPar tail**")
        lines.append("")
        lines.append("```")
        lines.append((ev.get("reconstruct_tail") or "")[-1800:])
        lines.append("```")
        lines.append("")
        if ev.get("solver_tail"):
            lines.append("**solver tail**")
            lines.append("")
            lines.append("```")
            lines.append((ev.get("solver_tail") or "")[-1800:])
            lines.append("```")
            lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    args = _parse_args()
    cfg = SSHConfig(host=args.host, user=args.user)
    payload = _fetch_samples(cfg, db=args.db, n=int(args.n))
    samples = payload.get("samples") or []
    stat = _analyze(samples)

    md = _render_md(payload.get("db") or args.db, samples, stat)
    out_path = (Path(__file__).resolve().parents[1] / args.out_md).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")

    remote_md = f"{args.run_dir.rstrip('/')}/postprocess_failed_audit.md"
    put_cmd = "python3 -c " + shlex.quote(
        "import sys; p=sys.argv[1]; s=sys.stdin.read(); open(p,'w',encoding='utf-8').write(s)"
    )
    rc, out, err = _ssh(cfg, f"cat > {shlex.quote(remote_md)} <<'EOF'\n{md}\nEOF\n", timeout=60)
    if rc != 0:
        raise RuntimeError(f"write remote md failed rc={rc}\n{out}\n{err}")

    print(str(out_path))
    print(remote_md)


if __name__ == "__main__":
    main()
