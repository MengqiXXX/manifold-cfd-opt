from __future__ import annotations

import sys
import time
from pathlib import Path
import shlex

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.ssh import SSHConfig, ssh_exec


def main() -> None:
    cfg = SSHConfig(host="192.168.110.10", user="liumq", port=22)
    script = """
set -e
echo "===TIME==="
date

echo "===OF_PROCS==="
ps aux | grep -E "rhoSimpleFoam|simpleFoam|foamRun|rhoPimpleFoam|pimpleFoam|mpirun" | grep -v grep || true

echo "===PY_PROCS==="
ps aux | grep -E "run_optimizer.py|run_agent.py|run_openfoam_once.py" | grep -v grep || true

echo "===MANIFOLD_CASES_DIR==="
ls -ld ~/manifold_cases 2>/dev/null || true

echo "===RECENT_CASE_ROOTS==="
find ~/manifold_cases -maxdepth 2 -type d -name case -printf "%T@ %p\n" 2>/dev/null | sort -rn | head -5 || true

echo "===RECENT_LOGS==="
find ~/manifold_cases -maxdepth 4 -name "log.*" -printf "%T@ %p\n" 2>/dev/null | sort -rn | head -10 || true

echo "===LATEST_CASE_TAIL==="
latest=$(find ~/manifold_cases -maxdepth 2 -type d -name case -printf "%T@ %p\n" 2>/dev/null | sort -rn | head -1 | cut -d" " -f2-)
echo "LATEST_CASE:$latest"
if [ -n "$latest" ]; then
  cd "$latest"
  echo "===LATEST_CASE_LS==="
  ls -la | head -200
  echo "===LATEST_CASE_DU==="
  du -sh . 2>/dev/null || true
  echo "===LATEST_CASE_SYSTEM==="
  ls -la system 2>/dev/null | head -200 || true
  echo "===LATEST_case_controlDict==="
  sed -n "1,60p" system/controlDict 2>/dev/null || true
  echo "===LATEST_case_decomposeParDict==="
  sed -n "1,80p" system/decomposeParDict 2>/dev/null || true
  for f in log.blockMesh log.checkMesh log.decomposePar log.solver log.reconstructPar solver.pid; do
    echo "---$f---"
    tail -n 80 "$f" 2>/dev/null || echo "missing"
  done
fi
"""
    cmd = "bash -lc " + shlex.quote(script)
    for i in range(8):
        rc, out, err = ssh_exec(cfg, cmd, timeout=60)
        if rc == 0:
            print(out)
            if err.strip():
                print("===SSH_STDERR===")
                print(err)
            return
        time.sleep(1.5)
    print(f"FAILED: rc={rc}\n{err}")


if __name__ == "__main__":
    main()
