from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class RunStatus(str, Enum):
    OK = "OK"
    SSH_ERROR = "SSH_ERROR"
    MESH_FAILED = "MESH_FAILED"
    CHECKMESH_FAILED = "CHECKMESH_FAILED"
    DECOMP_FAILED = "DECOMP_FAILED"
    SOLVER_TIMEOUT = "SOLVER_TIMEOUT"
    SOLVER_DIVERGED = "SOLVER_DIVERGED"
    SOLVER_FAILED = "SOLVER_FAILED"
    RECONSTRUCT_FAILED = "RECONSTRUCT_FAILED"


@dataclass
class RunResult:
    status: RunStatus
    runtime_s: float
    logs: dict[str, str]
    details: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.status == RunStatus.OK


class SSHExecutor(Protocol):
    def exec(self, cmd: str, timeout: int) -> tuple[int, str, str]: ...


def _ssh_exec(ssh: SSHExecutor, cmd: str, timeout: int) -> tuple[int, str, str]:
    return ssh.exec(cmd, timeout=int(timeout))


def _bash_lc(script: str) -> str:
    return "bash -lc " + shlex.quote(script)


def _tail_remote(ssh: SSHExecutor, case_dir: str, filename: str, timeout: int, n: int = 80) -> str:
    cmd = _bash_lc(f"cd {case_dir} && tail -n {int(n)} {filename} 2>/dev/null || true")
    code, out, err = _ssh_exec(ssh, cmd, timeout=timeout)
    return (out or "") + ("\n" + err if err else "")


def _parse_checkmesh(log_text: str) -> tuple[bool, str | None]:
    if re.search(r"Mesh\s+OK\.", log_text):
        return True, None
    if re.search(r"\bFailed\b", log_text, flags=re.IGNORECASE):
        return False, "checkMesh Failed"
    if re.search(r"Fatal\s+error", log_text, flags=re.IGNORECASE):
        return False, "checkMesh Fatal error"
    if re.search(r"FOAM\s+FATAL\s+ERROR", log_text, flags=re.IGNORECASE):
        return False, "checkMesh FOAM FATAL ERROR"
    if re.search(r"\bCannot\s+(read|open)\b", log_text, flags=re.IGNORECASE):
        return False, "checkMesh Cannot read/open"
    return False, "checkMesh Mesh OK not found"


_DIVERGED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Floating point exception", re.IGNORECASE), "Floating point exception"),
    (re.compile(r"FOAM FATAL ERROR", re.IGNORECASE), "FOAM FATAL ERROR"),
    (re.compile(r"SIGFPE", re.IGNORECASE), "SIGFPE"),
    (re.compile(r"nan\b", re.IGNORECASE), "NaN encountered"),
    (re.compile(r"\binf\b", re.IGNORECASE), "Inf encountered"),
    (re.compile(r"Segmentation fault", re.IGNORECASE), "Segmentation fault"),
    (re.compile(r"stack trace", re.IGNORECASE), "Stack trace"),
    (re.compile(r"cannot find file", re.IGNORECASE), "Missing file"),
]


def _parse_solver_failure(log_text: str) -> str | None:
    for pat, msg in _DIVERGED_PATTERNS:
        if pat.search(log_text):
            return msg
    return None


def _kill_remote_solver_group(ssh: SSHExecutor, case_dir: str, timeout: int) -> str:
    cmd = "bash -lc " + repr(
        f"cd {case_dir} && "
        "pid=$(cat solver.pid 2>/dev/null || true); "
        "if [ -z \"$pid\" ]; then echo NO_PID; exit 0; fi; "
        "kill -TERM -$pid 2>/dev/null || true; "
        "sleep 3; "
        "kill -KILL -$pid 2>/dev/null || true; "
        "echo KILLED_GRP:$pid"
    )
    _, out, err = _ssh_exec(ssh, cmd, timeout=timeout)
    return (out or "") + ("\n" + err if err else "")


def _kill_remote_pipeline_group(ssh: SSHExecutor, case_dir: str, timeout: int) -> str:
    cmd = "bash -lc " + repr(
        f"cd {case_dir} && "
        "pid=$(cat pipeline.pid 2>/dev/null || true); "
        "if [ -z \"$pid\" ]; then echo NO_PIPELINE_PID; exit 0; fi; "
        "kill -TERM -$pid 2>/dev/null || true; "
        "sleep 2; "
        "kill -KILL -$pid 2>/dev/null || true; "
        "echo KILLED_PIPELINE_GRP:$pid"
    )
    _, out, err = _ssh_exec(ssh, cmd, timeout=timeout)
    return (out or "") + ("\n" + err if err else "")


def _kill_local_solver_group(case_dir: str, timeout: int) -> str:
    import subprocess

    cmd = "bash -lc " + repr(
        f"cd {case_dir} && "
        "pid=$(cat solver.pid 2>/dev/null || true); "
        "if [ -z \"$pid\" ]; then echo NO_PID; exit 0; fi; "
        "kill -TERM -$pid 2>/dev/null || true; "
        "sleep 3; "
        "kill -KILL -$pid 2>/dev/null || true; "
        "echo KILLED_GRP:$pid"
    )
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=int(timeout))
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return out
    except Exception as e:
        return f"{type(e).__name__}: {e}"


class OpenFOAMRunner:
    def __init__(self, foam_source: str, n_cores: int, timeout_s: int, solver: str = "incompressibleFluid"):
        self.foam_source = foam_source
        self.n_cores = int(n_cores)
        self.timeout_s = int(timeout_s)
        self.solver = str(solver)

    def run_remote(self, ssh: SSHExecutor, case_dir: str) -> RunResult:
        t0 = time.perf_counter()
        logs: dict[str, str] = {}
        details: dict[str, Any] = {"case_dir": case_dir, "n_cores": self.n_cores}

        try:
            inner = "\n".join(
                [
                    "set -eo pipefail",
                    f". {self.foam_source} >/dev/null 2>&1 || true",
                    f"cd {case_dir}",
                    "echo __STEP__=blockMesh",
                    "blockMesh > log.blockMesh 2>&1",
                    "echo __STEP__=checkMesh",
                    "checkMesh > log.checkMesh 2>&1",
                    "echo __STEP__=decomposePar",
                    "decomposePar -force > log.decomposePar 2>&1",
                    "echo __STEP__=solver",
                    "rm -f solver.pid",
                    "if command -v setsid >/dev/null 2>&1; then",
                    f"  setsid mpirun -np {self.n_cores} foamRun -solver {self.solver} -parallel > log.solver 2>&1 &",
                    "else",
                    f"  mpirun -np {self.n_cores} foamRun -solver {self.solver} -parallel > log.solver 2>&1 &",
                    "fi",
                    "echo $! > solver.pid",
                    "wait $!",
                    "echo __STEP__=reconstructPar",
                    "reconstructPar -latestTime > log.reconstructPar 2>&1",
                    "echo __STATUS__=OK",
                    "echo OK > pipeline.done",
                ]
            )
            outer = (
                f"cd {case_dir} && "
                "rm -f pipeline.pid pipeline.done log.pipeline solver.pid "
                "log.blockMesh log.checkMesh log.decomposePar log.solver log.reconstructPar; "
                f"(nohup bash -lc {shlex.quote(inner)} > log.pipeline 2>&1 < /dev/null & echo $! > pipeline.pid); "
                "cat pipeline.pid 2>/dev/null || true"
            )
            start_cmd = _bash_lc(outer)
            code, out, err = _ssh_exec(ssh, start_cmd, timeout=min(self.timeout_s, 60))
            details["pipeline_start_rc"] = code
            details["pipeline_start_err"] = (err or "")[-500:]
            pid = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
            details["pipeline_pid"] = pid

            deadline = time.perf_counter() + float(self.timeout_s)
            last_pipeline_tail = ""
            last_solver_tail = ""
            while True:
                if time.perf_counter() >= deadline:
                    details["kill_pipeline"] = _kill_remote_pipeline_group(ssh, case_dir, timeout=min(self.timeout_s, 30))
                    details["kill_solver"] = _kill_remote_solver_group(ssh, case_dir, timeout=min(self.timeout_s, 30))
                    logs["pipeline"] = _tail_remote(ssh, case_dir, "log.pipeline", timeout=min(self.timeout_s, 30), n=200)
                    logs["solver"] = _tail_remote(ssh, case_dir, "log.solver", timeout=min(self.timeout_s, 30), n=200)
                    return RunResult(RunStatus.SOLVER_TIMEOUT, time.perf_counter() - t0, logs, details)

                poll_cmd = _bash_lc(
                    f"cd {case_dir} && "
                    "if [ -f pipeline.done ]; then echo DONE; "
                    "else pid=$(cat pipeline.pid 2>/dev/null || true); "
                    "if [ -n \"$pid\" ] && kill -0 $pid 2>/dev/null; then echo RUNNING; else echo EXITED; fi; "
                    "fi; "
                    "echo ===PIPELINE===; tail -n 200 log.pipeline 2>/dev/null || true; "
                    "echo ===SOLVER===; tail -n 200 log.solver 2>/dev/null || true"
                )
                try:
                    _, p_out, p_err = _ssh_exec(ssh, poll_cmd, timeout=30)
                except Exception:
                    time.sleep(3)
                    continue

                merged = (p_out or "") + ("\n" + p_err if p_err else "")
                lines = merged.splitlines()
                state = lines[0].strip() if lines else ""
                sec = None
                buf: list[str] = []
                pipe_lines: list[str] = []
                solver_lines: list[str] = []
                for line in lines[1:]:
                    if line.strip() == "===PIPELINE===":
                        if sec == "solver":
                            solver_lines = buf[:]
                        buf = []
                        sec = "pipeline"
                        continue
                    if line.strip() == "===SOLVER===":
                        if sec == "pipeline":
                            pipe_lines = buf[:]
                        buf = []
                        sec = "solver"
                        continue
                    buf.append(line)
                if sec == "pipeline":
                    pipe_lines = buf
                elif sec == "solver":
                    solver_lines = buf

                last_pipeline_tail = "\n".join(pipe_lines).strip()
                last_solver_tail = "\n".join(solver_lines).strip()

                diverged = _parse_solver_failure(last_solver_tail)
                if diverged is not None:
                    details["diverged"] = diverged
                    logs["pipeline"] = last_pipeline_tail
                    logs["solver"] = last_solver_tail
                    return RunResult(RunStatus.SOLVER_DIVERGED, time.perf_counter() - t0, logs, details)

                if state in {"DONE", "EXITED"}:
                    break
                time.sleep(5)

            logs["pipeline"] = last_pipeline_tail
            logs["solver"] = last_solver_tail
            logs["blockMesh"] = _tail_remote(ssh, case_dir, "log.blockMesh", timeout=30, n=200)
            logs["checkMesh"] = _tail_remote(ssh, case_dir, "log.checkMesh", timeout=30, n=200)
            logs["decomposePar"] = _tail_remote(ssh, case_dir, "log.decomposePar", timeout=30, n=200)
            logs["reconstructPar"] = _tail_remote(ssh, case_dir, "log.reconstructPar", timeout=30, n=200)

            if "__STATUS__=OK" in last_pipeline_tail:
                return RunResult(RunStatus.OK, time.perf_counter() - t0, logs, details)

            steps = re.findall(r"__STEP__=([A-Za-z0-9_]+)", last_pipeline_tail)
            step = steps[-1] if steps else None
            if step == "blockMesh":
                return RunResult(RunStatus.MESH_FAILED, time.perf_counter() - t0, logs, details)
            if step == "checkMesh":
                ok_mesh, mesh_diag = _parse_checkmesh(logs.get("checkMesh", ""))
                details["checkMesh"] = {"diag": mesh_diag}
                return RunResult(RunStatus.CHECKMESH_FAILED, time.perf_counter() - t0, logs, details)
            if step == "decomposePar":
                return RunResult(RunStatus.DECOMP_FAILED, time.perf_counter() - t0, logs, details)
            if step == "solver":
                return RunResult(RunStatus.SOLVER_FAILED, time.perf_counter() - t0, logs, details)
            if step == "reconstructPar":
                return RunResult(RunStatus.RECONSTRUCT_FAILED, time.perf_counter() - t0, logs, details)

            return RunResult(RunStatus.SSH_ERROR, time.perf_counter() - t0, logs, details)
        except Exception as e:
            details["exception"] = f"{type(e).__name__}: {e}"
            return RunResult(RunStatus.SSH_ERROR, time.perf_counter() - t0, logs, details)

    def run_local(self, case_dir: str) -> RunResult:
        import subprocess

        t0 = time.perf_counter()
        logs: dict[str, str] = {}
        details: dict[str, Any] = {"case_dir": case_dir, "n_cores": self.n_cores}

        def tail_file(name: str, n: int = 80) -> str:
            try:
                p = Path(case_dir) / name
                if not p.exists():
                    return ""
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                return "\n".join(lines[-int(n) :])
            except Exception:
                return ""

        def run_step(step: str, command: str, timeout_s: int | None = None) -> int:
            to = int(timeout_s if timeout_s is not None else self.timeout_s)
            cmd = (
                f"bash -lc 'set -eo pipefail; . {self.foam_source} >/dev/null 2>&1 || true; cd {case_dir}; {command}'"
            )
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=to)
            details[f"{step}_stderr"] = (proc.stderr or "")[-800:]
            return int(proc.returncode)

        try:
            code = run_step("blockMesh", "blockMesh > log.blockMesh 2>&1")
            logs["blockMesh"] = tail_file("log.blockMesh")
            if code != 0:
                return RunResult(RunStatus.MESH_FAILED, time.perf_counter() - t0, logs, details)

            code = run_step("checkMesh", "checkMesh > log.checkMesh 2>&1")
            logs["checkMesh"] = tail_file("log.checkMesh", n=200)
            ok_mesh, mesh_diag = _parse_checkmesh(logs["checkMesh"])
            details["checkMesh"] = {"exit_code": code, "diag": mesh_diag}
            if code != 0 or not ok_mesh:
                return RunResult(RunStatus.CHECKMESH_FAILED, time.perf_counter() - t0, logs, details)

            code = run_step("decomposePar", "decomposePar -force > log.decomposePar 2>&1")
            logs["decomposePar"] = tail_file("log.decomposePar")
            if code != 0:
                return RunResult(RunStatus.DECOMP_FAILED, time.perf_counter() - t0, logs, details)

            solver_cmd = (
                "rm -f solver.pid; "
                f"launcher='mpirun -np {self.n_cores} foamRun -solver {self.solver} -parallel'; "
                "if command -v setsid >/dev/null 2>&1; then launcher=\"setsid $launcher\"; fi; "
                "($launcher > log.solver 2>&1 & echo $! > solver.pid); "
                "wait $(cat solver.pid)"
            )
            try:
                code = run_step("solver", solver_cmd, timeout_s=self.timeout_s)
            except subprocess.TimeoutExpired as e:
                details["timeout_exc"] = f"{type(e).__name__}: {e}"
                details["kill"] = _kill_local_solver_group(case_dir, timeout=self.timeout_s)
                logs["solver"] = tail_file("log.solver", n=200)
                return RunResult(RunStatus.SOLVER_TIMEOUT, time.perf_counter() - t0, logs, details)

            logs["solver"] = tail_file("log.solver", n=200)
            diverged = _parse_solver_failure(logs["solver"])
            if diverged is not None:
                details["diverged"] = diverged
                return RunResult(RunStatus.SOLVER_DIVERGED, time.perf_counter() - t0, logs, details)
            if code != 0:
                return RunResult(RunStatus.SOLVER_FAILED, time.perf_counter() - t0, logs, details)

            code = run_step("reconstructPar", "reconstructPar -latestTime > log.reconstructPar 2>&1")
            logs["reconstructPar"] = tail_file("log.reconstructPar")
            if code != 0:
                return RunResult(RunStatus.RECONSTRUCT_FAILED, time.perf_counter() - t0, logs, details)

            return RunResult(RunStatus.OK, time.perf_counter() - t0, logs, details)
        except Exception as e:
            details["exception"] = f"{type(e).__name__}: {e}"
            return RunResult(RunStatus.SSH_ERROR, time.perf_counter() - t0, logs, details)
