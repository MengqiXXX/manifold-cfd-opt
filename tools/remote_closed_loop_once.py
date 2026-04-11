from __future__ import annotations

import json
import shlex
import tarfile
import tempfile
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.ssh import SSHConfig, ssh_exec, ssh_put_file


def render_case(template_dir: Path, out_dir: Path) -> None:
    from evaluators.remote_openfoam_evaluator import _derive_mesh_params, _render_template_dir
    from evaluators.base import DesignParams

    ctx = _derive_mesh_params(DesignParams(0.0, 0.0, 0.0))
    _render_template_dir(template_dir, out_dir, ctx)


def main() -> None:
    template_dir = Path("templates/manifold_2d").resolve()
    cfg = SSHConfig(host="192.168.110.10", user="liumq", port=22)

    run_tag = f"manual_{int(time.time())}"
    remote_root = f"/home/{cfg.user}/manifold_cases/{run_tag}"
    remote_case = f"{remote_root}/case"
    remote_tar = f"{remote_root}/case.tar.gz"

    with tempfile.TemporaryDirectory() as td:
        local_case = Path(td) / "case"
        local_case.mkdir(parents=True, exist_ok=True)
        render_case(template_dir, local_case)

        tar_path = Path(td) / "case.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(local_case, arcname="case")

        rc, out, err = ssh_exec(cfg, "bash -lc " + shlex.quote(f"mkdir -p {remote_root} && rm -rf {remote_case} && rm -f {remote_tar}"), timeout=30)
        if rc != 0:
            raise SystemExit(f"remote prep failed rc={rc}\n{err}")

        rc2, _, err2 = ssh_put_file(cfg, tar_path, remote_tar, timeout=120)
        if rc2 != 0:
            raise SystemExit(f"upload failed rc={rc2}\n{err2}")

        rc3, out3, err3 = ssh_exec(cfg, "bash -lc " + shlex.quote(f"cd {remote_root} && tar -xzf case.tar.gz && rm -f case.tar.gz"), timeout=60)
        if rc3 != 0:
            raise SystemExit(f"untar failed rc={rc3}\n{err3}")

    bash = f"""
set -e
source /opt/openfoam13/etc/bashrc
cd {remote_case}
echo CASE:$PWD

blockMesh > log.blockMesh 2>&1
checkMesh > log.checkMesh 2>&1
decomposePar -force > log.decomposePar 2>&1

rm -f solver.pid log.solver
launcher="mpirun -np 8 simpleFoam -parallel"
if command -v setsid >/dev/null 2>&1; then launcher="setsid $launcher"; fi
(nohup bash -lc "$launcher > log.solver 2>&1" < /dev/null > /dev/null 2>&1 & echo $! > solver.pid)
echo LAUNCHED:$(cat solver.pid)
sleep 2
ps -p $(cat solver.pid) -o pid,cmd || true
tail -n 20 log.solver || true
"""

    rc4, out4, err4 = ssh_exec(cfg, "bash -lc " + shlex.quote(bash), timeout=240)
    print("===REMOTE_RUN===")
    print("rc", rc4)
    print(out4)
    if err4.strip():
        print("===SSH_STDERR===")
        print(err4)
    if rc4 != 0:
        diag = f"""
cd {remote_case}
echo ===TAIL_blockMesh===
tail -n 30 log.blockMesh 2>/dev/null || true
echo ===TAIL_checkMesh===
tail -n 30 log.checkMesh 2>/dev/null || true
echo ===TAIL_decomposePar===
tail -n 60 log.decomposePar 2>/dev/null || true
echo ===TAIL_solver===
tail -n 80 log.solver 2>/dev/null || true
"""
        _, d_out, d_err = ssh_exec(cfg, "bash -lc " + shlex.quote(diag), timeout=60)
        print("===DIAG===")
        print(d_out)
        if d_err.strip():
            print("===DIAG_ERR===")
            print(d_err)

    poll = f"""
cd {remote_case}
echo ===OF_PROCS===
ps aux | grep -E "simpleFoam|foamRun|rhoSimpleFoam|mpirun -np 8" | grep -v grep || true
echo ===LOG_TAIL===
tail -n 40 log.solver 2>/dev/null || true
"""
    for _ in range(5):
        time.sleep(3)
        rc5, out5, err5 = ssh_exec(cfg, "bash -lc " + shlex.quote(poll), timeout=30)
        print("===POLL===")
        print(out5)
        if err5.strip():
            print("===POLL_ERR===")
            print(err5)

    print("remote_case", remote_case)
    print("tip", json.dumps({"remote_case": remote_case}, ensure_ascii=False))


if __name__ == "__main__":
    main()
