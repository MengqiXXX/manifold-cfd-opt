"""
Shared single persistent SSH connection for all monitor providers.

All providers import `ssh_exec()` from here — only ONE TCP connection
to the server is ever open, eliminating sshd MaxStartups errors.
Thread-safe via threading.Lock (paramiko channels are multiplexed over
the single transport, so concurrent exec_command calls are fine).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

try:
    import paramiko
    _OK = True
except ImportError:
    _OK = False

_SSH_HOST = os.getenv("VORTEX_SSH_HOST", "192.168.110.10")
_SSH_PORT  = int(os.getenv("VORTEX_SSH_PORT", "22"))
_SSH_USER  = os.getenv("VORTEX_SSH_USER", "liumq")
_SSH_KEY   = os.getenv("VORTEX_SSH_KEY",  "C:/Users/LMQ/.ssh/id_ed25519")

_client: Optional["paramiko.SSHClient"] = None
_lock = threading.Lock()


def _connect() -> "paramiko.SSHClient":
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        hostname=_SSH_HOST, port=_SSH_PORT, username=_SSH_USER,
        key_filename=_SSH_KEY if Path(_SSH_KEY).exists() else None,
        timeout=12, banner_timeout=20,
        # Keep-alive to prevent idle disconnect
        disabled_algorithms={"pubkeys": []},
    )
    t = c.get_transport()
    if t:
        t.set_keepalive(30)
    return c


def _get() -> "paramiko.SSHClient | None":
    global _client
    if not _OK:
        return None
    try:
        if _client is not None:
            t = _client.get_transport()
            if t and t.is_active():
                return _client
    except Exception:
        pass
    try:
        _client = _connect()
        return _client
    except Exception:
        _client = None
        return None


def ssh_exec(cmd: str, timeout: int = 15) -> str:
    """Run cmd on the remote server; return stdout+stderr or '' on error.
    Thread-safe: uses a single shared connection with per-call channels.
    """
    with _lock:
        ssh = _get()
        if ssh is None:
            return ""
        try:
            _, o, e = ssh.exec_command(cmd, timeout=timeout)
            out = o.read().decode("utf-8", errors="replace")
            err = e.read().decode("utf-8", errors="replace")
            return (out + err)
        except Exception:
            # Mark connection as dead
            global _client
            try:
                _client.close()
            except Exception:
                pass
            _client = None
            return ""


def is_connected() -> bool:
    with _lock:
        try:
            if _client is None:
                return False
            t = _client.get_transport()
            return bool(t and t.is_active())
        except Exception:
            return False
