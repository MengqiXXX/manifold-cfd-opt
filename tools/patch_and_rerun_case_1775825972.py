from __future__ import annotations

import shlex
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.ssh import SSHConfig, ssh_exec, ssh_put_file


def _render_control_dict() -> str:
    from jinja2 import Environment, FileSystemLoader
    from evaluators.remote_openfoam_evaluator import _derive_mesh_params
    from evaluators.base import DesignParams

    template_dir = Path("templates/manifold_2d/system").resolve()
    env = Environment(loader=FileSystemLoader(str(template_dir)), keep_trailing_newline=True)
    ctx = _derive_mesh_params(DesignParams(0.0, 0.0, 0.0))
    return env.get_template("controlDict.j2").render(**ctx)


def main() -> None:
    cfg = SSHConfig(host="192.168.110.10", user="liumq", port=22)
    case = "/home/liumq/manifold_cases/manifold_1775825972_2753546/case"
    remote_cd = f"{case}/system/controlDict"
    print("case", case)

    content = _render_control_dict()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
        tf.write(content)
        local_path = Path(tf.name)
    try:
        rc, _, err = ssh_put_file(cfg, local_path, remote_cd, timeout=60)
        print("upload_rc", rc)
        if rc != 0:
            print(err)
            return
    finally:
        try:
            local_path.unlink(missing_ok=True)
        except Exception:
            pass

    restart = f"""
source /opt/openfoam13/etc/bashrc
cd {case}
rm -f solver.pid log.solver
launcher="mpirun -np 8 simpleFoam -parallel"
if command -v setsid >/dev/null 2>&1; then launcher="setsid $launcher"; fi
(nohup bash -lc "$launcher > log.solver 2>&1" < /dev/null > /dev/null 2>&1 & echo $! > solver.pid)
echo LAUNCHED:$(cat solver.pid)
sleep 2
tail -n 60 log.solver 2>/dev/null || true
"""
    rc2, out2, err2 = ssh_exec(cfg, "bash -lc " + shlex.quote(restart), timeout=90)
    print("restart_rc", rc2)
    print(out2)
    if err2.strip():
        print("stderr", err2.strip())

    poll = f"""
cd {case}
echo ===OF_PROCS===
ps aux | grep -E "foamRun|incompressibleFluid|simpleFoam|mpirun -np 8" | grep -v grep || true
echo ===LOG_TAIL===
tail -n 40 log.solver 2>/dev/null || true
"""
    for _ in range(4):
        time.sleep(3)
        _, out3, _ = ssh_exec(cfg, "bash -lc " + shlex.quote(poll), timeout=30)
        print("===POLL===")
        print(out3)


if __name__ == "__main__":
    main()

