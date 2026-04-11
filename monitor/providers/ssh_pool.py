"""
Shared SSH exec for all monitor providers.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from infra.ssh import SSHConfig, ssh_exec as _ssh_exec

_SSH_HOST = os.getenv("VORTEX_SSH_HOST", "192.168.110.10")
_SSH_PORT = int(os.getenv("VORTEX_SSH_PORT", "22"))
_SSH_USER = os.getenv("VORTEX_SSH_USER", "liumq")
_SSH_KEY = os.getenv("VORTEX_SSH_KEY", "C:/Users/LMQ/.ssh/id_ed25519")
_STRICT = os.getenv("VORTEX_SSH_STRICT_HOSTKEY", "0").strip() in {"1", "true", "True"}

_last_error: Optional[str] = None
_lock = threading.Lock()


def _cfg() -> SSHConfig:
    return SSHConfig(
        host=_SSH_HOST,
        user=_SSH_USER,
        port=_SSH_PORT,
        key_path=_SSH_KEY,
        strict_host_key=_STRICT,
    )


def ssh_exec(cmd: str, timeout: int = 15) -> str:
    global _last_error
    with _lock:
        try:
            code, out, err = _ssh_exec(_cfg(), cmd, timeout=int(timeout))
            _last_error = None
            return (out + err)
        except Exception as e:
            _last_error = f"{type(e).__name__}: {e}"
            return ""


def is_connected() -> bool:
    with _lock:
        try:
            code, out, _ = _ssh_exec(_cfg(), "echo ok", timeout=8)
            return code == 0 and "ok" in (out or "")
        except Exception:
            return False


def last_error() -> str | None:
    return _last_error
