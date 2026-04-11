from __future__ import annotations

import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.ssh import SSHConfig, ssh_exec


def _latest_remote_case(cfg: SSHConfig) -> str:
    script = r"""
set -e
latest=$(find ~/manifold_cases -maxdepth 2 -type d -name case -printf "%T@ %p\n" 2>/dev/null | sort -rn | head -1 | cut -d" " -f2-)
echo "$latest"
"""
    rc, out, err = ssh_exec(cfg, "bash -lc " + shlex.quote(script), timeout=30)
    if rc != 0:
        raise RuntimeError(err.strip() or f"find latest case failed rc={rc}")
    return (out or "").strip().splitlines()[0].strip()


def main() -> None:
    cfg = SSHConfig(host="192.168.110.10", user="liumq", port=22)
    case = _latest_remote_case(cfg)
    n = 8

    bash = f"""
source /opt/openfoam13/etc/bashrc || exit 90
cd {case} || exit 91
echo CASE:$PWD

rm -f log.decomposePar
decomposePar -force > log.decomposePar 2>&1
rc=$?
if [ $rc -ne 0 ]; then
  echo DECOMP_FAIL_RC:$rc
  tail -n 120 log.decomposePar 2>/dev/null || true
  exit $rc
fi
echo DECOMP_DONE
tail -n 30 log.decomposePar 2>/dev/null || true

rm -f solver.pid log.solver
launcher="mpirun -np {n} simpleFoam -parallel"
if command -v setsid >/dev/null 2>&1; then launcher="setsid $launcher"; fi
(nohup bash -lc "$launcher > log.solver 2>&1" < /dev/null > /dev/null 2>&1 & echo $! > solver.pid)
echo LAUNCHED:$(cat solver.pid 2>/dev/null || true)
sleep 1
pid=$(cat solver.pid 2>/dev/null || true)
if [ -n "$pid" ]; then
  ps -p "$pid" -o pid,cmd || true
fi
tail -n 80 log.solver 2>/dev/null || true
"""
    cmd = "bash -lc " + shlex.quote(bash)
    rc, out, err = ssh_exec(cfg, cmd, timeout=240)
    print("rc", rc)
    print(out)
    if err.strip():
        print("===SSH_STDERR===")
        print(err)

    for _ in range(5):
        time.sleep(2)
        rc2, out2, err2 = ssh_exec(
            cfg,
            "bash -lc "
            + shlex.quote(
                f'ps aux | grep -E "simpleFoam|foamRun|rhoSimpleFoam|mpirun -np {n}" | grep -v grep || true; '
                f"cd {case} && tail -n 20 log.solver 2>/dev/null || true"
            ),
            timeout=30,
        )
        if out2.strip():
            print("===POLL===")
            print(out2)
        if err2.strip():
            print("===POLL_ERR===")
            print(err2)


if __name__ == "__main__":
    main()
