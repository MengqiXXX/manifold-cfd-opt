from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
import shutil


@dataclass
class SSHConfig:
    host: str
    user: str
    port: int = 22
    key_path: str | None = None
    strict_host_key: bool = False

    def target(self) -> str:
        return f"{self.user}@{self.host}"


def _is_transient_ssh_error(rc: int, err: str) -> bool:
    if rc in {255, 4294967295}:
        return True
    e = (err or "").lower()
    return any(
        s in e
        for s in [
            "kex_exchange_identification",
            "banner exchange",
            "error reading ssh protocol banner",
            "connection closed by",
            "connection reset by",
            "getsockname failed: not a socket",
            "read: unknown error",
        ]
    )


def _ssh_base_args(cfg: SSHConfig) -> list[str]:
    args = [
        "ssh",
        "-p",
        str(int(cfg.port)),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
    ]
    if os.getenv("VORTEX_SSH_DISABLE_CONTROL", "1").strip() not in {"1", "true", "True"}:
        base = Path(tempfile.gettempdir()) / "trae_sshcm_%h_%p_%r"
        args += [
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=300",
            "-o",
            f"ControlPath={str(base)}",
        ]
    if not cfg.strict_host_key:
        kh = Path(tempfile.gettempdir()) / "trae_known_hosts"
        args += ["-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={str(kh)}"]
    if cfg.key_path:
        kp = str(Path(cfg.key_path))
        if Path(kp).exists():
            args += ["-i", kp]
    return args


def _scp_base_args(cfg: SSHConfig) -> list[str]:
    args = [
        "scp",
        "-P",
        str(int(cfg.port)),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
    ]
    if os.getenv("VORTEX_SSH_DISABLE_CONTROL", "1").strip() not in {"1", "true", "True"}:
        base = Path(tempfile.gettempdir()) / "trae_sshcm_%h_%p_%r"
        args += [
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=300",
            "-o",
            f"ControlPath={str(base)}",
        ]
    if not cfg.strict_host_key:
        kh = Path(tempfile.gettempdir()) / "trae_known_hosts"
        args += ["-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={str(kh)}"]
    if cfg.key_path:
        kp = str(Path(cfg.key_path))
        if Path(kp).exists():
            args += ["-i", kp]
    return args


def ssh_exec(cfg: SSHConfig, cmd: str, timeout: int = 30) -> tuple[int, str, str]:
    last: tuple[int, str, str] = (255, "", "")
    for i in range(4):
        p = subprocess.run(
            _ssh_base_args(cfg) + [cfg.target(), cmd],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(timeout),
        )
        rc = int(p.returncode)
        out = p.stdout or ""
        err = p.stderr or ""
        last = (rc, out, err)
        if rc == 0:
            return last
        if not _is_transient_ssh_error(rc, err):
            return last
        time.sleep(1.5 * (i + 1))
    return last


def scp_put(cfg: SSHConfig, local_path: str | Path, remote_path: str) -> tuple[int, str, str]:
    last: tuple[int, str, str] = (255, "", "")
    for i in range(3):
        p = subprocess.run(
            _scp_base_args(cfg) + [str(local_path), f"{cfg.target()}:{remote_path}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        rc = int(p.returncode)
        out = p.stdout or ""
        err = p.stderr or ""
        last = (rc, out, err)
        if rc == 0:
            return last
        if not _is_transient_ssh_error(rc, err):
            return last
        time.sleep(1.5 * (i + 1))
    return last


def ssh_put_file(cfg: SSHConfig, local_path: str | Path, remote_path: str, timeout: int = 120) -> tuple[int, str, str]:
    local_path = Path(local_path)
    remote_cmd = "bash -lc " + repr(f"cat > {remote_path}")
    last: tuple[int, str, str] = (255, "", "")
    for i in range(3):
        p = subprocess.Popen(
            _ssh_base_args(cfg) + [cfg.target(), remote_cmd],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert p.stdin is not None
            with local_path.open("rb") as f:
                shutil.copyfileobj(f, p.stdin)
            p.stdin.close()
            out_b, err_b = p.communicate(timeout=int(timeout))
            rc = int(p.returncode or 0)
            out = out_b.decode(errors="replace")
            err = err_b.decode(errors="replace")
            last = (rc, out, err)
            if rc == 0:
                return last
            if not _is_transient_ssh_error(rc, err):
                return last
        except Exception as e:
            try:
                p.kill()
            except Exception:
                pass
            try:
                out_b, err_b = p.communicate(timeout=5)
            except Exception:
                out_b, err_b = b"", b""
            rc = int(p.returncode or 255)
            out = out_b.decode(errors="replace")
            err = (err_b.decode(errors="replace") + f"\n{type(e).__name__}: {e}").strip()
            last = (rc, out, err)
            if not _is_transient_ssh_error(rc, err):
                return last
        time.sleep(1.5 * (i + 1))
    return last
