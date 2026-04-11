from __future__ import annotations

import math
import posixpath
import shlex
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class SSHExecutor(Protocol):
    def exec(self, cmd: str, timeout: int) -> tuple[int, str, str]: ...


def _ssh_exec(ssh: SSHExecutor, cmd: str, timeout: int) -> tuple[int, str, str]:
    return ssh.exec(cmd, timeout=int(timeout))


@dataclass
class ScalarReadResult:
    value: float | None
    used_fallback: bool
    logs: str
    status: str = "OK"
    diag: str | None = None
    source_path: str | None = None


class ScalarReadStatus(str, Enum):
    OK = "OK"
    MISSING = "MISSING"
    EMPTY = "EMPTY"
    NON_NUMERIC = "NON_NUMERIC"
    NAN_INF = "NAN_INF"
    POSTPROCESS_FAILED = "POSTPROCESS_FAILED"
    SSH_ERROR = "SSH_ERROR"


def _to_finite_float(text: str) -> tuple[float | None, str | None]:
    try:
        v = float(text.strip())
    except Exception:
        return None, "NON_NUMERIC"
    if not math.isfinite(v):
        return None, "NAN_INF"
    return v, None


def read_latest_surface_field_value(
    ssh: SSHExecutor,
    case_dir: str,
    func_name: str,
    timeout: int,
    foam_source: str,
) -> ScalarReadResult:
    read_script = (
        f"cd {case_dir} && "
        f"f=$(ls -1t postProcessing/{func_name}/*/*.dat "
        f"processor*/postProcessing/{func_name}/*/*.dat 2>/dev/null | head -n 1); "
        "if [ -z \"$f\" ]; then exit 2; fi; "
        "v=$(awk \"NF {last=\\$NF} END{if(last==\\\"\\\") exit 3; print last}\" \"$f\"); "
        "echo \"$f\"; echo \"$v\""
    )
    read_cmd = "bash -lc " + shlex.quote(read_script)

    last_logs = ""
    code = 255
    out = ""
    err = ""
    for attempt in range(4):
        try:
            code, out, err = _ssh_exec(ssh, read_cmd, timeout=timeout)
        except Exception as e:
            return ScalarReadResult(
                None,
                used_fallback=False,
                logs=f"{type(e).__name__}: {e}",
                status=ScalarReadStatus.SSH_ERROR.value,
                diag="SSH exec failed (read)",
            )

        last_logs = (out or "") + ("\n" + err if err else "")
        if code == 0:
            lines = (out or "").splitlines()
            src = lines[0].strip() if lines else None
            val_text = lines[1].strip() if len(lines) > 1 else ""
            v, err_code = _to_finite_float(val_text)
            if v is not None:
                return ScalarReadResult(v, used_fallback=False, logs="", status=ScalarReadStatus.OK.value, source_path=src)
            status = ScalarReadStatus.NON_NUMERIC.value if err_code == "NON_NUMERIC" else ScalarReadStatus.NAN_INF.value
            return ScalarReadResult(
                None,
                used_fallback=False,
                logs=last_logs.strip(),
                status=status,
                diag=err_code,
                source_path=src,
            )

        if code in {2, 3}:
            break

        e = (err or out or "").lower()
        if code == 255 or any(
            s in e
            for s in [
                "connection closed",
                "connection reset",
                "kex_exchange_identification",
                "error reading ssh protocol banner",
                "getsockname failed: not a socket",
                "read from remote host",
            ]
        ):
            time.sleep(1.5 * (attempt + 1))
            continue
        return ScalarReadResult(
            None,
            used_fallback=False,
            logs=last_logs.strip(),
            status=ScalarReadStatus.SSH_ERROR.value,
            diag=f"read_cmd exit_code={code}",
        )

    if code == 255:
        return ScalarReadResult(
            None,
            used_fallback=False,
            logs=last_logs.strip(),
            status=ScalarReadStatus.SSH_ERROR.value,
            diag="SSH transient failures",
        )

    if code == 3:
        return ScalarReadResult(
            None,
            used_fallback=False,
            logs=last_logs.strip(),
            status=ScalarReadStatus.EMPTY.value,
            diag="postProcessing dat exists but empty",
        )

    return ScalarReadResult(
        None,
        used_fallback=False,
        logs=last_logs.strip(),
        status=ScalarReadStatus.MISSING.value,
        diag="postProcessing missing",
    )
