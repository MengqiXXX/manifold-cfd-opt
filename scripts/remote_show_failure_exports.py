from __future__ import annotations

import argparse
import shlex
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.ssh import SSHConfig, ssh_exec


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="192.168.110.10")
    p.add_argument("--user", default="liumq")
    p.add_argument("--export-root", required=True)
    return p.parse_args()


def _run(cfg: SSHConfig, bash_script: str, timeout: int = 60) -> str:
    cmd = "bash -lc " + shlex.quote(bash_script)
    rc, out, err = ssh_exec(cfg, cmd, timeout=timeout)
    if rc != 0:
        return out + ("\n" + err if err.strip() else "")
    return out + ("\n" + err if err.strip() else "")


def main() -> None:
    args = _parse_args()
    cfg = SSHConfig(host=args.host, user=args.user)
    root = args.export_root.rstrip("/")

    print(_run(cfg, f"ls -la {shlex.quote(root)}; echo ---; ls -1 {shlex.quote(root)} | head -50", timeout=30))
    cases = _run(cfg, f"ls -1 {shlex.quote(root)} | grep -E '^[0-9]{{6}}_' | head -20", timeout=30).split()

    for c in cases:
        d = f"{root}/{c}"
        print(f"\n===== {d} =====")
        print(_run(cfg, f"ls -la {shlex.quote(d)}", timeout=30))
        for fn in ["pick.json", "structure.txt", "solver_grep.txt"]:
            p = f"{d}/{fn}"
            print(f"\n--- {p} (tail) ---")
            print(_run(cfg, f"test -f {shlex.quote(p)} && tail -n 120 {shlex.quote(p)} || echo MISSING", timeout=60))


if __name__ == "__main__":
    main()
