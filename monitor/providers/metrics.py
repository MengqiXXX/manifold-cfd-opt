from __future__ import annotations

import asyncio
import datetime as dt
import math
import re
import shutil
import subprocess
from typing import Any

import psutil

from .ssh_pool import ssh_exec, is_connected, _SSH_HOST

_REMOTE_SCRIPT = r"""
echo '===GPU==='
nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu \
  --format=csv,noheader,nounits 2>/dev/null || true
echo '===STAT1==='
grep '^cpu ' /proc/stat
sleep 0.3
echo '===STAT2==='
grep '^cpu ' /proc/stat
echo '===MEM==='
grep -E '^(MemTotal|MemAvailable|MemFree):' /proc/meminfo
echo '===TEMP==='
sensors 2>/dev/null | grep -E '^Tctl[[:space:]]|^Tdie[[:space:]]' | head -3 || \
  cat /sys/class/hwmon/hwmon0/temp1_input 2>/dev/null | awk '{printf "Tctl:  +%.1f\n", $1/1000}' || true
"""


def _now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _section(raw: str, key: str) -> str:
    lines = raw.splitlines()
    start_tag = f"==={key}==="
    end_re = __import__("re").compile(r"^===\w+===$")
    capturing = False
    out: list[str] = []
    for line in lines:
        if line.strip() == start_tag:
            capturing = True
            continue
        if capturing:
            if end_re.match(line.strip()):
                break
            out.append(line)
    return "\n".join(out)


def _parse_gpus(raw: str) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            idx       = int(parts[0])
            name      = parts[1]
            util      = float(re.sub(r"[^0-9.]+", "", parts[2]) or "nan")
            mem_used  = float(re.sub(r"[^0-9.]+", "", parts[3]) or "nan")
            mem_total = float(re.sub(r"[^0-9.]+", "", parts[4]) or "nan")
            temp_c    = float(re.sub(r"[^0-9.]+", "", parts[5]) or "nan") if len(parts) > 5 else float("nan")
            if not math.isfinite(util):      util      = 0.0
            if not math.isfinite(mem_used):  mem_used  = 0.0
            if not math.isfinite(mem_total): mem_total = 0.0
            entry: dict[str, Any] = {
                "index":         idx,
                "name":          name,
                "usagePct":      util,
                "memUsedBytes":  int(mem_used  * 1024 * 1024),
                "memTotalBytes": int(mem_total * 1024 * 1024),
            }
            if math.isfinite(temp_c):
                entry["tempC"] = round(temp_c, 1)
            gpus.append(entry)
        except Exception:
            continue
    return gpus


def _parse_cpu(stat_text: str) -> tuple[int, int]:
    for line in stat_text.splitlines():
        if line.startswith("cpu "):
            vals = list(map(int, line.split()[1:]))
            idle  = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
            return idle, total
    return 0, 1


def _parse_memory(mem_text: str) -> dict[str, int]:
    kv: dict[str, int] = {}
    for line in mem_text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            kv[m.group(1)] = int(m.group(2)) * 1024
    total     = kv.get("MemTotal", 0)
    available = kv.get("MemAvailable", kv.get("MemFree", 0))
    return {"usedBytes": total - available, "totalBytes": total}


def _parse_cpu_temp(temp_text: str) -> float | None:
    for line in temp_text.splitlines():
        m = re.search(r"[+-]?([\d.]+)\s*°?C?$", line.strip())
        if not m:
            m = re.search(r"[+-]?([\d.]+)", line.strip())
        if m:
            try:
                v = float(m.group(1))
                if 20 < v < 150:
                    return round(v, 1)
            except ValueError:
                pass
    return None


def _local_metrics() -> dict[str, Any]:
    cpu_pct = float(psutil.cpu_percent(interval=None))
    mem     = psutil.virtual_memory()
    gpus: list[dict[str, Any]] = []
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            r = subprocess.run(
                [smi, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
            )
            gpus = _parse_gpus(r.stdout or "")
        except Exception:
            gpus = []
    return {
        "cpu":    {"usagePct": cpu_pct},
        "memory": {"usedBytes": int(mem.used), "totalBytes": int(mem.total)},
        "gpus":   gpus,
    }


async def get_metric_sample() -> dict[str, Any]:
    def _collect() -> dict[str, Any]:
        raw = ssh_exec(_REMOTE_SCRIPT, timeout=12)
        if not raw.strip():
            local = _local_metrics()
            local["_source"] = "local_fallback"
            return local

        gpus     = _parse_gpus(_section(raw, "GPU"))
        stat1    = _section(raw, "STAT1")
        stat2    = _section(raw, "STAT2")
        mem      = _parse_memory(_section(raw, "MEM"))
        cpu_temp = _parse_cpu_temp(_section(raw, "TEMP"))

        idle1, total1 = _parse_cpu(stat1)
        idle2, total2 = _parse_cpu(stat2)
        dt_total = total2 - total1
        dt_idle  = idle2  - idle1
        cpu_pct  = round((1.0 - dt_idle / dt_total) * 100.0, 1) if dt_total > 0 else 0.0

        if not gpus and cpu_pct == 0.0 and mem["totalBytes"] == 0:
            local = _local_metrics()
            local["_source"] = "local_fallback"
            return local

        cpu_info: dict[str, Any] = {"usagePct": cpu_pct}
        if cpu_temp is not None:
            cpu_info["tempC"] = cpu_temp

        return {
            "_source": f"remote:{_SSH_HOST}",
            "cpu":    cpu_info,
            "memory": mem,
            "gpus":   gpus,
        }

    result = await asyncio.to_thread(_collect)
    result["ts"] = _now_iso()
    return result
