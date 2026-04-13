from __future__ import annotations

import argparse
import json
import shlex
import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.ssh import SSHConfig, ssh_exec


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="192.168.110.10")
    p.add_argument("--user", default="liumq")
    p.add_argument("--repo", default="/home/liumq/manifold-cfd-opt")
    p.add_argument("--run-dir", default="/home/liumq/opt_runs")
    p.add_argument("--n-cores", type=int, default=8)
    p.add_argument("--timeout-s", type=int, default=1200)
    return p.parse_args()


def _run(cfg: SSHConfig, bash_script: str, timeout: int) -> tuple[int, str, str]:
    cmd = "bash -lc " + shlex.quote(bash_script)
    return ssh_exec(cfg, cmd, timeout=timeout)


def _must(cfg: SSHConfig, bash_script: str, timeout: int) -> str:
    rc, out, err = _run(cfg, bash_script, timeout)
    if rc != 0:
        raise RuntimeError(f"remote rc={rc}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return (out or "") + (("\n" + err) if err else "")


def main() -> None:
    args = _parse_args()
    cfg = SSHConfig(host=args.host, user=args.user)
    repo = args.repo.rstrip("/")
    run_base = args.run_dir.rstrip("/")
    ts = time.strftime("%Y%m%d_%H%M%S")

    remote_file = f"{repo}/evaluators/foam_runner.py"
    backup_file = f"{repo}/evaluators/foam_runner.py.bak_{ts}"

    _must(cfg, f"set -e; test -f {shlex.quote(remote_file)}; cp {shlex.quote(remote_file)} {shlex.quote(backup_file)}; echo {shlex.quote(backup_file)}", timeout=30)

    patch_py = r"""
import pathlib
p = pathlib.Path(%r)
s = p.read_text(encoding="utf-8")
old = "f\"launcher='mpirun -np {self.n_cores} foamRun -solver {self.solver} -parallel'; \""
new = "f'launcher=\"mpirun -np {self.n_cores} foamRun -solver {self.solver} -parallel\"; '"
s2 = s
if old in s2:
    s2 = s2.replace(old, new, 1)
s2 = "".join([ln for ln in s2.splitlines(True) if 'launcher=\\"setsid $launcher\\"' not in ln])
if s2 != s:
    p.write_text(s2, encoding="utf-8")
    print("PATCHED", p)
else:
    print("NO_CHANGE", p)
""" % (remote_file,)
    _must(cfg, f"python3 - <<'PY'\n{patch_py}\nPY", timeout=30)
    _must(cfg, f"python3 -m py_compile {shlex.quote(remote_file)}", timeout=30)

    smoke_dir = f"{run_base}/smoke_runnerfix_{ts}"
    cfg_path = f"{smoke_dir}/config_smoke.yaml"
    db_path = f"{smoke_dir}/smoke.sqlite"
    report_path = f"{smoke_dir}/smoke_report.md"
    remote_base = f"/home/{args.user}/manifold_cases_smoke_{ts}"

    yaml_cfg = "\n".join(
        [
            "evaluator: remote_openfoam",
            "n_initial: 1",
            "batch_size: 1",
            f"template_dir: {repo}/templates/manifold_2d",
            "cases_base: of_cases",
            "ssh_host: 127.0.0.1",
            f"ssh_user: {args.user}",
            "ssh_port: 22",
            f"remote_base: {remote_base}",
            "foam_source: /opt/openfoam13/etc/bashrc",
            f"n_cores: {int(args.n_cores)}",
            f"timeout_s: {int(args.timeout_s)}",
            f"db_path: {db_path}",
            f"report_path: {report_path}",
        ]
    )

    _must(cfg, f"set -e; mkdir -p {shlex.quote(smoke_dir)}; cat > {shlex.quote(cfg_path)} <<'EOF'\n{yaml_cfg}\nEOF\n", timeout=30)

    run_out = _must(cfg, f"set -e; cd {shlex.quote(repo)}; python3 -u scripts/run_openfoam_once.py {shlex.quote(cfg_path)}", timeout=int(args.timeout_s) + 300)

    q_py = r"""
import sqlite3
db=%r
con=sqlite3.connect(db)
row=con.execute("select rowid, status, converged, metadata from results order by rowid desc limit 1").fetchone()
con.close()
print(row[0])
print(row[1])
print(int(row[2]))
print(row[3])
""" % (db_path,)
    q_out = _must(cfg, f"python3 - <<'PY'\n{q_py}\nPY", timeout=30).splitlines()
    rowid = q_out[0].strip()
    status = q_out[1].strip()
    converged = q_out[2].strip()
    meta_s = "\n".join(q_out[3:]).strip()
    meta = json.loads(meta_s) if meta_s else {}
    case_dir = meta.get("remote_case") or ""

    check = _must(
        cfg,
        "\n".join(
            [
                "set -e",
                f"echo SMOKE_DIR:{smoke_dir}",
                f"echo DB:{db_path}",
                f"echo REPORT:{report_path}",
                f"echo ROWID:{rowid}",
                f"echo STATUS:{status}",
                f"echo CONVERGED:{converged}",
                f"echo CASE_DIR:{case_dir}",
                f"test -d {shlex.quote(case_dir)} || (echo CASE_MISSING; exit 2)",
                f"cd {shlex.quote(case_dir)}",
                f"ls -la {shlex.quote(case_dir)} | head -60",
                f"echo HAS_LOG_SOLVER:; test -f {shlex.quote(case_dir)}/log.solver && echo YES || echo NO",
                f"echo TIME_DIRS:; ls -1 {shlex.quote(case_dir)} | egrep '^[0-9]+(\\.[0-9]+)?$' | head -20 || true",
                f"echo POSTPROCESS_TOP:; find {shlex.quote(case_dir)}/postProcessing -maxdepth 3 -type f -name '*.dat' 2>/dev/null | head -40 || true",
                f"echo OUTLET1FLOW:; find {shlex.quote(case_dir)}/postProcessing -path '*outlet1Flow*' -type f -name '*.dat' 2>/dev/null | head -20 || true",
                f"echo OUTLET1P:; find {shlex.quote(case_dir)}/postProcessing -path '*outlet1P*' -type f -name '*.dat' 2>/dev/null | head -20 || true",
                "echo SOLVER_TAIL:; tail -n 120 log.solver 2>/dev/null || true",
                "echo SOLVER_ERRORS:; grep -nE \"FOAM FATAL|Fatal error|Floating point|Segmentation fault|MPI_ABORT|\\berror\\b\" log.solver 2>/dev/null | tail -n 80 || true",
                "has_log=0; test -f log.solver && has_log=1; echo CHK_HAS_LOG_SOLVER=$has_log",
                "has_time=0; ls -1 | egrep -q '^[0-9]+(\\.[0-9]+)?$' && has_time=1; echo CHK_HAS_TIME_DIRS=$has_time",
                "has_f=0; find postProcessing -path '*outlet1Flow*' -type f -name '*.dat' 2>/dev/null | head -1 | grep -q . && has_f=1; echo CHK_HAS_OUTLET1FLOW_DAT=$has_f",
                "has_p=0; find postProcessing -path '*outlet1P*' -type f -name '*.dat' 2>/dev/null | head -1 | grep -q . && has_p=1; echo CHK_HAS_OUTLET1P_DAT=$has_p",
            ]
        ),
        timeout=60,
    )

    print("BACKUP", backup_file)
    print("SMOKE_DIR", smoke_dir)
    print("CASE_DIR", case_dir)
    print("STATUS", status, "CONVERGED", converged)
    print("REMOTE_CHECK_BEGIN")
    print(check.strip())
    print("REMOTE_CHECK_END")

    chk = {}
    for line in check.splitlines():
        if line.startswith("CHK_") and "=" in line:
            k, v = line.split("=", 1)
            chk[k.strip()] = v.strip()

    files_ok = all(
        chk.get(k) == "1"
        for k in ["CHK_HAS_LOG_SOLVER", "CHK_HAS_TIME_DIRS", "CHK_HAS_OUTLET1FLOW_DAT", "CHK_HAS_OUTLET1P_DAT"]
    )
    db_ok = (status == "OK") and (converged == "1")
    print("ACCEPT_FILES", "PASS" if files_ok else "FAIL")
    print("ACCEPT_DB", "PASS" if db_ok else "FAIL")
    missing = [k for k, v in chk.items() if k.startswith("CHK_") and v != "1"]
    if missing:
        print("MISSING", ",".join(sorted(missing)))
    if not files_ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
