from __future__ import annotations

import math
import posixpath
import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import paramiko
from jinja2 import Environment, FileSystemLoader

from .base import DesignParams, EvalResult, Evaluator


def _render_template_dir(template_dir: Path, out_dir: Path, context: dict) -> None:
    env = Environment(loader=FileSystemLoader(str(template_dir)), keep_trailing_newline=True)
    for src in template_dir.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(template_dir)
        dst = out_dir / (rel.with_suffix("") if src.suffix == ".j2" else rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".j2":
            tmpl = env.get_template(str(rel).replace("\\", "/"))
            dst.write_text(tmpl.render(**context), encoding="utf-8")
        else:
            shutil.copy2(src, dst)


def _softmax(xs: list[float]) -> list[float]:
    m = max(xs)
    exps = [math.exp(x - m) for x in xs]
    s = sum(exps)
    return [e / s for e in exps]


def _derive_mesh_params(params: DesignParams, outlet_count: int = 4) -> dict:
    if outlet_count != 4:
        raise ValueError("Current template supports outlet_count=4 only.")

    logits = [params.logit_1, params.logit_2, params.logit_3, 0.0]
    weights = _softmax(logits)

    L = 1.0
    H = 0.2
    thickness = 0.01

    y_levels = [0.0]
    acc = 0.0
    for w in weights:
        acc += w * H
        y_levels.append(acc)
    y_levels[-1] = H

    outlet_names = [f"outlet{i+1}" for i in range(outlet_count)]

    n_cells_y_total = 48
    n_cells_y = [max(4, int(round(w * n_cells_y_total))) for w in weights]
    diff = n_cells_y_total - sum(n_cells_y)
    n_cells_y[-1] += diff

    return {
        "L": L,
        "H": H,
        "thickness": thickness,
        "outlet_count": outlet_count,
        "outlet_names": outlet_names,
        "outlet_weights": weights,
        "y_levels": y_levels,
        "U_in": 10.0,
        "nu": 1.5e-5,
        "p_out": 0.0,
        "p_in_ref": 0.0,
        "dp_weight": 1.0e-5,
        "dp_ref": 1.0,
        "n_cells_x": 120,
        "n_cells_y": n_cells_y,
        "n_cells_z": 1,
        "end_time": 300,
        "write_interval": 100,
    }


def _ssh_exec(ssh: paramiko.SSHClient, cmd: str, timeout: int) -> tuple[int, str, str]:
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout, get_pty=False)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), out, err


def _latest_value_from_surface_field_value(ssh: paramiko.SSHClient, case_dir: str, func_name: str, timeout: int) -> float | None:
    cmd = (
        "bash -lc "
        + repr(
            f"cd {case_dir} && "
            f"f=$(ls -1t postProcessing/{func_name}/*/*.dat 2>/dev/null | head -n 1); "
            "if [ -z \"$f\" ]; then exit 2; fi; "
            "tail -n 1 \"$f\" | awk '{print $NF}'"
        )
    )
    code, out, _ = _ssh_exec(ssh, cmd, timeout=timeout)
    if code != 0:
        return None
    try:
        return float(out.strip().splitlines()[-1])
    except Exception:
        return None


@dataclass
class RemoteOpenFOAMEvaluator(Evaluator):
    template_dir: str
    cases_base: str
    n_cores: int = 8
    timeout: int = 900
    foam_source: str = "/opt/openfoam13/etc/bashrc"
    ssh_host: str = "192.168.110.10"
    ssh_user: str = "liumq"
    ssh_port: int = 22
    remote_base: str = "~/manifold_cases"
    llm_client: Any = None
    llm_model: str | None = None

    def _connect(self) -> paramiko.SSHClient:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(self.ssh_host, port=self.ssh_port, username=self.ssh_user, timeout=15)
        return ssh

    def _upload_case(self, ssh: paramiko.SSHClient, local_case: Path, remote_case: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tf:
            tar_path = Path(tf.name)
        try:
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(local_case, arcname="case")

            sftp = ssh.open_sftp()
            remote_tar = posixpath.join(remote_case, "case.tar.gz")

            _ssh_exec(
                ssh,
                "bash -lc " + repr(f"mkdir -p {remote_case} && rm -rf {remote_case}/case && rm -f {remote_tar}"),
                timeout=self.timeout,
            )

            sftp.put(str(tar_path), remote_tar)
            sftp.close()

            _ssh_exec(
                ssh,
                "bash -lc " + repr(f"cd {remote_case} && tar -xzf case.tar.gz && rm -f case.tar.gz"),
                timeout=self.timeout,
            )
        finally:
            try:
                tar_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _run_case(self, ssh: paramiko.SSHClient, remote_case_dir: str, timeout: int) -> tuple[bool, str]:
        solver_cmd = "simpleFoam"
        cmd = (
            f"bash -lc 'set -euo pipefail; "
            f"source {self.foam_source}; "
            f"cd {remote_case_dir}; "
            "blockMesh > log.blockMesh 2>&1; "
            "checkMesh > log.checkMesh 2>&1; "
            "decomposePar -force > log.decomposePar 2>&1; "
            f"mpirun -np {int(self.n_cores)} {solver_cmd} -parallel > log.solver 2>&1; "
            "reconstructPar -latestTime > log.reconstructPar 2>&1; "
            "echo DONE'"
        )
        code, out, err = _ssh_exec(ssh, cmd, timeout=timeout)
        return code == 0 and "DONE" in out, out + "\n" + err

    def evaluate_one(self, params: DesignParams) -> EvalResult:
        t0 = time.perf_counter()
        ctx = _derive_mesh_params(params=params, outlet_count=4)
        ctx["n_cores"] = int(self.n_cores)

        with tempfile.TemporaryDirectory() as tmp:
            local_case = Path(tmp) / "case"
            _render_template_dir(Path(self.template_dir), local_case, ctx)

            run_tag = f"manifold_{int(time.time())}_{abs(hash((params.logit_1, params.logit_2, params.logit_3)))%10_000_000:07d}"
            remote_case_root = posixpath.join(self.remote_base, run_tag)
            remote_case_dir = posixpath.join(remote_case_root, "case")

            ssh = None
            try:
                ssh = self._connect()
                self._upload_case(ssh, local_case=local_case, remote_case=remote_case_root)

                ok, logs = self._run_case(ssh, remote_case_dir=remote_case_dir, timeout=self.timeout)
                if not ok:
                    return EvalResult(
                        params=params,
                        flow_cv=float("nan"),
                        pressure_drop=float("nan"),
                        converged=False,
                        runtime_s=time.perf_counter() - t0,
                        status="ERROR",
                        metadata={"remote_case": remote_case_dir, "logs": logs, "dp_weight": ctx["dp_weight"], "dp_ref": ctx["dp_ref"]},
                    )

                flows = []
                outlet_ps = []
                for name in ctx["outlet_names"]:
                    f = _latest_value_from_surface_field_value(ssh, remote_case_dir, f"{name}Flow", timeout=self.timeout)
                    p = _latest_value_from_surface_field_value(ssh, remote_case_dir, f"{name}P", timeout=self.timeout)
                    if f is None or p is None:
                        return EvalResult(
                            params=params,
                            flow_cv=float("nan"),
                            pressure_drop=float("nan"),
                            converged=False,
                            runtime_s=time.perf_counter() - t0,
                            status="ERROR",
                            metadata={"remote_case": remote_case_dir, "logs": "Missing postProcessing outputs", "dp_weight": ctx["dp_weight"], "dp_ref": ctx["dp_ref"]},
                        )
                    flows.append(float(f))
                    outlet_ps.append(float(p))

                inlet_p = _latest_value_from_surface_field_value(ssh, remote_case_dir, "inletP", timeout=self.timeout)
                if inlet_p is None:
                    inlet_p = 0.0

                mean_flow = sum(flows) / len(flows)
                std_flow = math.sqrt(sum((x - mean_flow) ** 2 for x in flows) / len(flows))
                flow_cv = std_flow / (abs(mean_flow) + 1e-12)
                pressure_drop = float(inlet_p) - (sum(outlet_ps) / len(outlet_ps))

                return EvalResult(
                    params=params,
                    flow_cv=flow_cv,
                    pressure_drop=pressure_drop,
                    converged=True,
                    runtime_s=time.perf_counter() - t0,
                    status="OK",
                    metadata={
                        "remote_case": remote_case_dir,
                        "flows": flows,
                        "outlet_ps": outlet_ps,
                        "inlet_p": inlet_p,
                        "outlet_weights": ctx["outlet_weights"],
                        "y_levels": ctx["y_levels"],
                        "dp_weight": ctx["dp_weight"],
                        "dp_ref": ctx["dp_ref"],
                    },
                )
            finally:
                if ssh:
                    ssh.close()

    def evaluate_batch(self, params_list: list[DesignParams]) -> list[EvalResult]:
        results: list[EvalResult] = []
        for p in params_list:
            try:
                results.append(self.evaluate_one(p))
            except Exception as e:
                results.append(
                    EvalResult(
                        params=p,
                        flow_cv=float("nan"),
                        pressure_drop=float("nan"),
                        converged=False,
                        runtime_s=0.0,
                        status="ERROR",
                        metadata={"error": f"{type(e).__name__}: {e}"},
                    )
                )
        return results
