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

from jinja2 import Environment, FileSystemLoader

from .base import DesignParams, EvalResult, Evaluator
from .foam_runner import OpenFOAMRunner
from .post_processor import read_latest_surface_field_value
from infra.ssh import SSHConfig, scp_put, ssh_exec, ssh_put_file


def _render_template_dir(template_dir: Path, out_dir: Path, context: dict) -> None:
    env = Environment(loader=FileSystemLoader(str(template_dir)), keep_trailing_newline=True)
    for src in template_dir.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(template_dir)
        dst = out_dir / (rel.with_suffix("") if src.suffix == ".j2" else rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        template_name = str(rel).replace("\\", "/")
        if src.suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".gz", ".bz2", ".stl"}:
            shutil.copy2(src, dst)
            continue
        try:
            tmpl = env.get_template(template_name)
            dst.write_text(tmpl.render(**context), encoding="utf-8")
        except UnicodeDecodeError:
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
        "end_time": 50,
        "write_interval": 10,
    }


class OpenSSHSession:
    def __init__(self, cfg: SSHConfig):
        self.cfg = cfg

    def exec(self, cmd: str, timeout: int) -> tuple[int, str, str]:
        return ssh_exec(self.cfg, cmd, timeout=int(timeout))

    def close(self) -> None:
        return None


def _is_localhost(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def _latest_value_from_surface_field_value(ssh: OpenSSHSession, case_dir: str, func_name: str, timeout: int) -> float | None:
    cmd = (
        "bash -lc "
        + repr(
            f"cd {case_dir} && "
            f"f=$(ls -1t postProcessing/{func_name}/*/*.dat 2>/dev/null | head -n 1); "
            "if [ -z \"$f\" ]; then exit 2; fi; "
            "tail -n 1 \"$f\" | awk '{print $NF}'"
        )
    )
    code, out, _ = ssh.exec(cmd, timeout=int(timeout))
    if code != 0:
        return None
    try:
        return float(out.strip().splitlines()[-1])
    except Exception:
        return None


def _latest_value_from_surface_field_value_local(case_dir: str, func_name: str) -> float | None:
    try:
        pp = Path(case_dir) / "postProcessing" / func_name
        if not pp.exists():
            return None
        candidates = sorted(pp.glob("*/*.dat"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            return None
        line = candidates[-1].read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if not line:
            return None
        last = line[-1].strip().split()
        if not last:
            return None
        return float(last[-1])
    except Exception:
        return None


def _derive_mesh_params_3d(params: DesignParams, outlet_count: int = 4) -> dict:
    """3D variant: real depth thickness=0.1m, n_cells_z=10, symmetryPlane front/back."""
    d = _derive_mesh_params(params, outlet_count)
    d["thickness"] = 0.1
    d["n_cells_z"] = 10
    d["W"] = 0.1
    return d


@dataclass
class RemoteOpenFOAMEvaluator(Evaluator):
    template_dir: str
    cases_base: str
    n_cores: int = 8
    max_parallel_cases: int = 1
    timeout: int = 900
    foam_source: str = "/opt/openfoam13/etc/bashrc"
    foam_solver: str = "incompressibleFluid"
    ssh_host: str = "192.168.110.10"
    ssh_user: str = "liumq"
    ssh_port: int = 22
    ssh_key_path: str | None = None
    remote_base: str = "~/manifold_cases"
    llm_client: Any = None
    llm_model: str | None = None

    def _connect(self) -> OpenSSHSession:
        import os

        key = self.ssh_key_path or os.getenv("VORTEX_SSH_KEY") or None
        cfg = SSHConfig(host=self.ssh_host, user=self.ssh_user, port=int(self.ssh_port), key_path=key)
        return OpenSSHSession(cfg)

    def _upload_case(self, ssh: OpenSSHSession, local_case: Path, remote_case: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tf:
            tar_path = Path(tf.name)
        try:
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(local_case, arcname="case")

            remote_tar = posixpath.join(remote_case, "case.tar.gz")
            ssh.exec(
                "bash -lc " + repr(f"mkdir -p {remote_case} && rm -rf {remote_case}/case && rm -f {remote_tar}"),
                timeout=self.timeout,
            )
            last_err = ""
            for attempt in range(5):
                _, _, put_err = ssh_put_file(ssh.cfg, tar_path, remote_tar, timeout=max(120, int(self.timeout)))
                ok_code, ok_out, ok_err = ssh.exec("bash -lc " + repr(f"test -s {remote_tar} && echo OK"), timeout=25)
                if ok_code == 0 and "OK" in (ok_out or ""):
                    last_err = ""
                    break
                last_err = (put_err or ok_err or "").strip()
                ssh.exec("bash -lc " + repr(f"rm -f {remote_tar}"), timeout=20)
                time.sleep(1.5 * (attempt + 1))
            if last_err:
                code_scp, _, err_scp = scp_put(ssh.cfg, tar_path, remote_tar)
                ok_code, ok_out, ok_err = ssh.exec("bash -lc " + repr(f"test -s {remote_tar} && echo OK"), timeout=25)
                if code_scp != 0 or ok_code != 0 or "OK" not in (ok_out or ""):
                    raise RuntimeError(f"upload failed: {(err_scp or last_err or ok_err)[-500:]}")
            ssh.exec(
                "bash -lc " + repr(f"cd {remote_case} && tar -xzf case.tar.gz && rm -f case.tar.gz"),
                timeout=self.timeout,
            )
        finally:
            try:
                tar_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _run_case(self, ssh: OpenSSHSession, remote_case_dir: str, timeout: int) -> tuple[bool, str]:
        cmd = (
            f"bash -lc 'set -eo pipefail; "
            f". {self.foam_source} >/dev/null 2>&1 || true; "
            f"cd {remote_case_dir}; "
            "blockMesh > log.blockMesh 2>&1; "
            "checkMesh > log.checkMesh 2>&1; "
            "decomposePar -force > log.decomposePar 2>&1; "
            f"mpirun -np {int(self.n_cores)} foamRun -solver {self.foam_solver} -parallel > log.solver 2>&1; "
            "reconstructPar -latestTime > log.reconstructPar 2>&1; "
            "echo DONE'"
        )
        code, out, err = ssh.exec(cmd, timeout=int(timeout))
        return code == 0 and "DONE" in out, out + "\n" + err

    def _run_case_local(self, remote_case_dir: str, timeout: int) -> tuple[bool, str]:
        import subprocess

        cmd = (
            f"bash -lc 'set -eo pipefail; "
            f". {self.foam_source} >/dev/null 2>&1 || true; "
            f"cd {remote_case_dir}; "
            "blockMesh > log.blockMesh 2>&1; "
            "checkMesh > log.checkMesh 2>&1; "
            "decomposePar -force > log.decomposePar 2>&1; "
            f"mpirun -np {int(self.n_cores)} foamRun -solver {self.foam_solver} -parallel > log.solver 2>&1; "
            "reconstructPar -latestTime > log.reconstructPar 2>&1; "
            "echo DONE'"
        )
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            return proc.returncode == 0 and "DONE" in out, out
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def evaluate_one(self, params: DesignParams) -> EvalResult:
        t0 = time.perf_counter()
        if "3d" in str(self.template_dir).lower():
            ctx = _derive_mesh_params_3d(params=params, outlet_count=4)
        else:
            ctx = _derive_mesh_params(params=params, outlet_count=4)
        ctx["n_cores"] = int(self.n_cores)
        runner = OpenFOAMRunner(
            foam_source=self.foam_source,
            n_cores=int(self.n_cores),
            timeout_s=int(self.timeout),
            solver=self.foam_solver,
        )

        with tempfile.TemporaryDirectory() as tmp:
            local_case = Path(tmp) / "case"
            _render_template_dir(Path(self.template_dir), local_case, ctx)

            run_tag = f"manifold_{int(time.time())}_{abs(hash((params.logit_1, params.logit_2, params.logit_3)))%10_000_000:07d}"
            remote_case_root = posixpath.join(self.remote_base, run_tag)
            remote_case_dir = posixpath.join(remote_case_root, "case")

            ssh = None
            try:
                if _is_localhost(self.ssh_host):
                    remote_case_root = str(Path(self.remote_base).expanduser() / run_tag)
                    remote_case_dir = str(Path(remote_case_root) / "case")
                    _render_template_dir(Path(self.template_dir), Path(remote_case_dir), ctx)
                    run_result = runner.run_local(remote_case_dir)
                else:
                    run_result = None
                    last_exc: str | None = None
                    for attempt in range(2):
                        try:
                            ssh = self._connect()
                            remote_base_abs = self.remote_base
                            if remote_base_abs.startswith("~"):
                                _, home_out, _ = ssh.exec("bash -lc 'echo $HOME'", timeout=10)
                                home = (home_out or "").strip().splitlines()[0].strip() if (home_out or "").strip() else "/home/" + self.ssh_user
                                if remote_base_abs == "~":
                                    remote_base_abs = home
                                elif remote_base_abs.startswith("~/"):
                                    remote_base_abs = posixpath.join(home, remote_base_abs[2:])
                            remote_case_root = posixpath.join(remote_base_abs, run_tag)
                            remote_case_dir = posixpath.join(remote_case_root, "case")

                            self._upload_case(ssh, local_case=local_case, remote_case=remote_case_root)
                            run_result = runner.run_remote(ssh, remote_case_dir)
                            break
                        except Exception as e:
                            last_exc = f"{type(e).__name__}: {e}"
                            try:
                                if ssh:
                                    ssh.close()
                            except Exception:
                                pass
                            ssh = None
                            time.sleep(2 * (attempt + 1))
                    if run_result is None:
                        return EvalResult(
                            params=params,
                            flow_cv=float("nan"),
                            pressure_drop=float("nan"),
                            converged=False,
                            runtime_s=time.perf_counter() - t0,
                            status="SSH_ERROR",
                            metadata={
                                "remote_case": remote_case_dir,
                            "failure": {"kind": "SSH_ERROR", "stage": "connect/upload/run"},
                                "error": last_exc or "Unknown SSH error",
                                "dp_weight": ctx["dp_weight"],
                                "dp_ref": ctx["dp_ref"],
                            },
                        )

                if not run_result.ok:
                    return EvalResult(
                        params=params,
                        flow_cv=float("nan"),
                        pressure_drop=float("nan"),
                        converged=False,
                        runtime_s=time.perf_counter() - t0,
                        status=f"RUN_{run_result.status.value}",
                        metadata={
                            "remote_case": remote_case_dir,
                            "failure": {"kind": run_result.status.value, "stage": "run"},
                            "runner": run_result.details,
                            "step_logs": run_result.logs,
                            "dp_weight": ctx["dp_weight"],
                            "dp_ref": ctx["dp_ref"],
                        },
                    )

                flows = []
                outlet_ps = []
                post_meta: dict[str, Any] = {}
                for name in ctx["outlet_names"]:
                    if _is_localhost(self.ssh_host):
                        f = _latest_value_from_surface_field_value_local(remote_case_dir, f"{name}Flow")
                        p = _latest_value_from_surface_field_value_local(remote_case_dir, f"{name}P")
                    else:
                        f_res = read_latest_surface_field_value(
                            ssh=ssh,
                            case_dir=remote_case_dir,
                            func_name=f"{name}Flow",
                            timeout=self.timeout,
                            foam_source=self.foam_source,
                        )
                        p_res = read_latest_surface_field_value(
                            ssh=ssh,
                            case_dir=remote_case_dir,
                            func_name=f"{name}P",
                            timeout=self.timeout,
                            foam_source=self.foam_source,
                        )
                        f = f_res.value
                        p = p_res.value
                        post_meta[f"{name}Flow"] = {
                            "value": f_res.value,
                            "used_fallback": f_res.used_fallback,
                            "status": f_res.status,
                            "diag": f_res.diag,
                            "source_path": f_res.source_path,
                            "logs": (f_res.logs if (f_res.status != "OK" or f_res.used_fallback) else ""),
                        }
                        post_meta[f"{name}P"] = {
                            "value": p_res.value,
                            "used_fallback": p_res.used_fallback,
                            "status": p_res.status,
                            "diag": p_res.diag,
                            "source_path": p_res.source_path,
                            "logs": (p_res.logs if (p_res.status != "OK" or p_res.used_fallback) else ""),
                        }
                    if f is None or p is None:
                        failing = []
                        if _is_localhost(self.ssh_host):
                            failing = [f"{name}Flow", f"{name}P"]
                        else:
                            if f_res.value is None:
                                failing.append(f"{name}Flow:{f_res.status}")
                            if p_res.value is None:
                                failing.append(f"{name}P:{p_res.status}")
                        return EvalResult(
                            params=params,
                            flow_cv=float("nan"),
                            pressure_drop=float("nan"),
                            converged=False,
                            runtime_s=time.perf_counter() - t0,
                            status="POSTPROCESS_FAILED",
                            metadata={
                                "remote_case": remote_case_dir,
                                "failure": {"kind": "POSTPROCESS_FAILED", "stage": "postprocess", "missing": failing},
                                "dp_weight": ctx["dp_weight"],
                                "dp_ref": ctx["dp_ref"],
                                "step_logs": run_result.logs,
                                "runner": run_result.details,
                                "postprocess": post_meta,
                            },
                        )
                    flows.append(float(f))
                    outlet_ps.append(float(p))

                if _is_localhost(self.ssh_host):
                    inlet_p = _latest_value_from_surface_field_value_local(remote_case_dir, "inletP")
                else:
                    inlet_res = read_latest_surface_field_value(
                        ssh=ssh,
                        case_dir=remote_case_dir,
                        func_name="inletP",
                        timeout=self.timeout,
                        foam_source=self.foam_source,
                    )
                    inlet_p = inlet_res.value
                    post_meta["inletP"] = {
                        "value": inlet_res.value,
                        "used_fallback": inlet_res.used_fallback,
                        "status": inlet_res.status,
                        "diag": inlet_res.diag,
                        "source_path": inlet_res.source_path,
                        "logs": (inlet_res.logs if (inlet_res.status != "OK" or inlet_res.used_fallback) else ""),
                    }
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
                        "step_logs": run_result.logs,
                        "runner": run_result.details,
                        "postprocess": post_meta,
                    },
                )
            finally:
                if ssh:
                    ssh.close()

    def evaluate_batch(self, params_list: list[DesignParams]) -> list[EvalResult]:
        n = len(params_list)
        if n == 0:
            return []

        workers = max(1, min(int(self.max_parallel_cases), n))
        if workers == 1:
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

        from concurrent.futures import ThreadPoolExecutor, as_completed

        out: list[EvalResult | None] = [None] * n
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self.evaluate_one, p): i for i, p in enumerate(params_list)}
            for fut in as_completed(futs):
                i = futs[fut]
                p = params_list[i]
                try:
                    out[i] = fut.result()
                except Exception as e:
                    out[i] = EvalResult(
                        params=p,
                        flow_cv=float("nan"),
                        pressure_drop=float("nan"),
                        converged=False,
                        runtime_s=0.0,
                        status="ERROR",
                        metadata={"error": f"{type(e).__name__}: {e}"},
                    )
        return [r for r in out if r is not None]
