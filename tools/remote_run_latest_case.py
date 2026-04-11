from __future__ import annotations

import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.ssh import SSHConfig, ssh_exec


def _find_latest_case(cfg: SSHConfig) -> str:
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
    case = _find_latest_case(cfg)
    print("case", case)

    run = f"""
source /opt/openfoam13/etc/bashrc || exit 90
cd {case} || exit 91

checkMesh > log.checkMesh 2>&1
rc1=$?
echo CHECKMESH_RC:$rc1
tail -n 40 log.checkMesh 2>/dev/null || true
if [ $rc1 -ne 0 ]; then exit $rc1; fi

decomposePar -force > log.decomposePar 2>&1
rc2=$?
echo DECOMP_RC:$rc2
tail -n 80 log.decomposePar 2>/dev/null || true
if [ $rc2 -ne 0 ]; then exit $rc2; fi

rm -f solver.pid log.solver
launcher="mpirun -np 4 simpleFoam -parallel"
if command -v setsid >/dev/null 2>&1; then launcher="setsid $launcher"; fi
(nohup bash -lc "$launcher > log.solver 2>&1" < /dev/null > /dev/null 2>&1 & echo $! > solver.pid)
echo LAUNCHED:$(cat solver.pid 2>/dev/null || true)
sleep 2
tail -n 80 log.solver 2>/dev/null || true
"""
    rc2, out2, err2 = ssh_exec(cfg, "bash -lc " + shlex.quote(run), timeout=240)
    print("rc", rc2)
    print(out2.strip())
    if err2.strip():
        print("stderr", err2.strip())

    poll = f"""
cd {case}
echo ===OF_PROCS===
ps aux | grep -E "simpleFoam|foamRun|rhoSimpleFoam|mpirun -np 4" | grep -v grep || true
echo ===LOG_TAIL===
tail -n 30 log.solver 2>/dev/null || true
"""
    for _ in range(6):
        time.sleep(3)
        rc3, out3, err3 = ssh_exec(cfg, "bash -lc " + shlex.quote(poll), timeout=30)
        print("===POLL===")
        print(out3)
        if err3.strip():
            print("stderr", err3.strip())


if __name__ == "__main__":
    main()
