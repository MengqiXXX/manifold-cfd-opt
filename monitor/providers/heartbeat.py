from __future__ import annotations

import asyncio
import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .cfd_status import get_cfd_status
from .jobs import get_live_job_snapshot
from .metrics import get_metric_sample


def _iso(ts: dt.datetime) -> str:
    return ts.replace(microsecond=0).isoformat() + "Z"


def _config_path(root: Path) -> Path:
    env = os.getenv("VORTEX_CONFIG")
    if env:
        p = Path(env).expanduser()
        return p if p.is_absolute() else (root / p)
    return root / "config.yaml"


def _load_cfg(root: Path) -> dict[str, Any]:
    p = _config_path(root)
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _brief_metrics(metrics: dict[str, Any]) -> str:
    cpu = (metrics.get("cpu") or {}).get("usagePct", 0)
    mem = metrics.get("memory") or {}
    mem_used = mem.get("usedBytes", 0)
    mem_total = mem.get("totalBytes", 0)
    mem_pct = (mem_used / mem_total * 100) if mem_total else 0
    gpus = metrics.get("gpus") or []
    if not gpus:
        gpu_part = "GPU:无"
    else:
        parts = []
        for g in gpus:
            idx = g.get("index")
            u = int(g.get("usagePct", 0) or 0)
            mu = float(g.get("memUsedBytes", 0) or 0)
            mt = float(g.get("memTotalBytes", 0) or 0)
            mp = int((mu / mt * 100) if mt else 0)
            parts.append(f"GPU{idx}:{u}%/{mp}%mem")
        gpu_part = " | ".join(parts)
    return f"CPU:{int(cpu)}% MEM:{int(mem_pct)}% {gpu_part}"


def _brief_job(job: dict[str, Any] | None) -> str:
    if not job:
        return "JOB:无"
    status = job.get("status") or "unknown"
    it = job.get("iteration")
    ev = job.get("evaluated")
    bud = job.get("budget")
    phase = job.get("current_phase") or ""
    parts = [f"JOB:{status}"]
    if it is not None:
        parts.append(f"iter={it}")
    if ev is not None and bud is not None:
        parts.append(f"eval={ev}/{bud}")
    if phase:
        parts.append(f"phase={phase}")
    return " ".join(parts)

def _brief_cfd(cfd: dict[str, Any] | None) -> str:
    if not cfd:
        return "CFD:未知"
    n = cfd.get("n_running")
    try:
        n_i = int(n) if n is not None else 0
    except Exception:
        n_i = 0
    return f"CFD:运行中{n_i}个case" if n_i > 0 else "CFD:无运行中case"


@dataclass
class HeartbeatState:
    enabled: bool
    interval_s: float
    max_tokens: int
    ts: str | None = None
    summary: str | None = None
    last_error: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "ok": bool(self.summary),
            "enabled": self.enabled,
            "interval_s": self.interval_s,
            "ts": self.ts,
            "summary": self.summary,
            "error": self.last_error,
        }


def init_state(root: Path) -> tuple[HeartbeatState, Any | None, str | None]:
    cfg = _load_cfg(root)
    enabled = bool(cfg.get("llm_heartbeat_enabled", False))
    interval_s = float(cfg.get("llm_heartbeat_interval_s", 1.0))
    interval_s = max(0.5, min(10.0, interval_s))
    max_tokens = int(cfg.get("llm_heartbeat_max_tokens", 80))
    max_tokens = max(16, min(256, max_tokens))

    base_url = cfg.get("llm_base_url")
    model = cfg.get("llm_model")
    api_key = cfg.get("llm_api_key", "dummy")

    if not (enabled and base_url and model):
        return HeartbeatState(False, interval_s, max_tokens), None, None

    try:
        from openai import OpenAI
    except Exception:
        return HeartbeatState(False, interval_s, max_tokens, last_error="openai_not_installed"), None, None

    import httpx
    # 直连服务器，绕过本地代理（如 Clash/v2ray 监听 127.0.0.1:7890）
    http_client = httpx.Client(
        transport=httpx.HTTPTransport(proxy=None),
        trust_env=False,  # 忽略 http_proxy / HTTP_PROXY 环境变量
    )
    client = OpenAI(base_url=base_url, api_key=api_key, http_client=http_client)
    return HeartbeatState(True, interval_s, max_tokens), client, str(model)


async def heartbeat_loop(root: Path, state: HeartbeatState, client: Any, model: str) -> None:
    while True:
        t0 = dt.datetime.utcnow()
        try:
            metrics = await get_metric_sample()
            job = get_live_job_snapshot(root)
            cfd = (await get_cfd_status()).to_dict()
            prompt = (
                "你是仿真优化巡检机器人。每秒输出一条中文巡检摘要(<=80字)，"
                "包含：是否在跑仿真、GPU是否有推理负载、当前阶段/进度。"
                "若发现异常(发散/超时/无有效点)给出一句下一步排查建议。"
                f"\n{_brief_metrics(metrics)}\n{_brief_job(job)}\n{_brief_cfd(cfd)}"
            )

            def _call() -> str:
                r = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=state.max_tokens,
                )
                return (r.choices[0].message.content or "").strip()

            text = await asyncio.to_thread(_call)
            state.ts = _iso(t0)
            state.summary = text
            state.last_error = None
        except Exception as e:
            state.ts = _iso(t0)
            state.last_error = str(e)
        elapsed = (dt.datetime.utcnow() - t0).total_seconds()
        await asyncio.sleep(max(0.0, state.interval_s - elapsed))
