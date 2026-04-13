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
    p.add_argument("--run-dir", default="/home/liumq/opt_runs/opt2000")
    return p.parse_args()


def _run(cfg: SSHConfig, bash_script: str, timeout: int = 60) -> str:
    cmd = "bash -lc " + shlex.quote(bash_script)
    rc, out, err = ssh_exec(cfg, cmd, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"remote rc={rc}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return out + (("\n" + err) if err.strip() else "")


def main() -> None:
    args = _parse_args()
    cfg = SSHConfig(host=args.host, user=args.user)

    pick_script = f"""
    set -e
    python3 - <<'PY'
import json, sqlite3
db={args.db!r}
con=sqlite3.connect(db)
def pick(st):
  r=con.execute("select rowid,logit_1,logit_2,logit_3,status,metadata from results where status=? limit 1",(st,)).fetchone()
  if not r: return None
  rowid,l1,l2,l3,status,meta=r
  try: md=json.loads(meta) if meta else {{}}
  except Exception: md={{}}
  return {{"rowid":int(rowid),"logits":[l1,l2,l3],"status":status,"remote_case":md.get("remote_case"),"postprocess":md.get("postprocess"),"failure":md.get("failure")}}
print(json.dumps(pick("POSTPROCESS_FAILED"), ensure_ascii=False))
print(json.dumps(pick("RUN_MESH_FAILED"), ensure_ascii=False))
con.close()
PY
    """
    out = _run(cfg, pick_script, timeout=60)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    picks = [json.loads(lines[-2]), json.loads(lines[-1])]

    export_root = f"{args.run_dir}/failure_exports/manual_{__import__('time').strftime('%Y%m%d_%H%M%S')}"
    _run(cfg, f"mkdir -p {shlex.quote(export_root)}", timeout=30)

    for p in picks:
        if not p:
            continue
        tag = f"{p['rowid']:06d}_{p['status']}"
        remote_case = p.get("remote_case")
        dst = f"{export_root}/{tag}"
        _run(cfg, f"mkdir -p {shlex.quote(dst)}", timeout=30)
        _run(cfg, f"python3 -c {shlex.quote('import json; import sys; p='+repr(p)+'; open(\"'+dst+'/pick.json'+'\",\"w\",encoding=\"utf-8\").write(json.dumps(p,ensure_ascii=False,indent=2)+\"\\n\")')}", timeout=60)

        if remote_case:
            case_dir = remote_case
            case_root = str(Path(remote_case).parent)
            bundle = f"{dst}/case_bundle.tgz"
            tar_cmd = (
                f"tar -czf {shlex.quote(bundle)} -C {shlex.quote(case_root)} "
                f"--exclude=case/processor* --exclude=case/[0-9]* --exclude=case/[0-9]*.* "
                f"case/system case/constant case/0 case/postProcessing 2>/dev/null || true"
            )
            _run(cfg, tar_cmd, timeout=120)

            struct_out = f"{dst}/structure.txt"
            struct_script = (
                f"cd {shlex.quote(case_dir)}; "
                f"{{ echo system:; ls -la system; "
                f"echo constant:; ls -la constant; "
                f"echo polyMesh:; ls -la constant/polyMesh 2>/dev/null || true; "
                f"echo postProcessing:; ls -la postProcessing 2>/dev/null || true; "
                f"echo logs:; ls -la log.* 2>/dev/null || true; }} > {shlex.quote(struct_out)} 2>&1"
            )
            _run(cfg, struct_script, timeout=60)

            grep_out = f"{dst}/solver_grep.txt"
            grep_script = (
                f"cd {shlex.quote(case_dir)}; "
                f"{{ for f in log.solver log.simpleFoam log.foamRun; do "
                f"if [ -f \"$f\" ]; then echo \"==== $f\"; "
                f"grep -nE \"outlet1Flow|outlet1P|surfaceFieldValue|functionObject|postProcessing\" \"$f\" | tail -n 200 || true; fi; "
                f"done; }} > {shlex.quote(grep_out)} 2>&1"
            )
            _run(cfg, grep_script, timeout=60)

    print(export_root)


if __name__ == "__main__":
    main()
