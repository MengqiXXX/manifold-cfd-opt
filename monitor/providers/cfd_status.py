"""
SSH 采集服务器上正在运行的 OpenFOAM 案例信息（10 分钟缓存）。
使用共享 SSH 连接池 (ssh_pool.py)，不独立建立新连接。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .ssh_pool import ssh_exec

REFRESH_INTERVAL = 600


@dataclass
class CaseGeom:
    case_id: str
    D_mm: float | None = None
    L_mm: float | None = None
    L_D: float | None = None
    r_c: float | None = None
    n_cells: int | None = None
    current_step: int | None = None


@dataclass
class CFDStatus:
    ts: str = ""
    next_refresh_ts: str = ""
    ok: bool = False
    error: str | None = None
    solver: str = "unknown"
    turb_model: str = "unknown"
    cores_per_case: int = 0
    of_version: str = ""
    cases_base: str = ""
    cases: list[CaseGeom] = field(default_factory=list)
    n_running: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cases"] = [asdict(c) for c in self.cases]
        return d


_cache: CFDStatus | None = None
_cache_time: float = 0.0
_lock = asyncio.Lock()

_DISCOVER_SCRIPT = r"""
SOLVER_RE='rhoSimpleFoam|foamRun|simpleFoam|rhoPimpleFoam|buoyantSimpleFoam|buoyantPimpleFoam|pisoFoam|pimpleFoam'
echo '===CWDS==='
for pid in $(ps aux | grep -E "$SOLVER_RE" | grep -v grep | awk '{print $2}'); do
  readlink /proc/$pid/cwd 2>/dev/null
done | sort -u

echo '===RECENT_LOGS==='
find ~/vortex_opt/of_cases -maxdepth 2 -name 'log.*Foam' -mmin -30 2>/dev/null \
    | sed 's|/log\.[^/]*$||' | sort -u

echo '===HAS_PROC0==='
for d in ~/vortex_opt/of_cases/*/; do
    d="${d%/}"
    [ -d "$d/processor0" ] && echo "$d"
done 2>/dev/null
echo '===END==='
"""


def _section(raw: str, tag: str) -> list[str]:
    lines = raw.splitlines()
    start = f"==={tag}==="
    end_re = re.compile(r"^===\w+=")
    capturing = False
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s == start:
            capturing = True
            continue
        if capturing:
            if end_re.match(s) and s != start:
                break
            if s:
                out.append(s)
    return out


def _pick_running_dirs(raw: str) -> list[str]:
    cwds   = _section(raw, "CWDS")
    recent = _section(raw, "RECENT_LOGS")
    proc0  = _section(raw, "HAS_PROC0")
    candidates = cwds or recent or proc0
    seen: set[str] = set()
    result: list[str] = []
    for d in candidates:
        if d and d not in seen:
            seen.add(d)
            result.append(d)
    return result


def _case_info_script(dirs: list[str]) -> str:
    parts = ['echo "===CASES_START==="']
    for d in dirs:
        parts.append(f"""
echo "===CASE_BEGIN==="
echo "CASE_DIR:{d}"
echo "===controlDict==="
cat '{d}/system/controlDict' 2>/dev/null | head -40
echo "===turbulenceProperties==="
cat '{d}/constant/turbulenceProperties' 2>/dev/null | head -30
echo "===decomposeParDict==="
cat '{d}/system/decomposeParDict' 2>/dev/null | head -20
echo "===blockMeshDict==="
cat '{d}/system/blockMeshDict' 2>/dev/null | head -100
echo "===owner==="
awk 'NR<=30' '{d}/constant/polyMesh/owner' 2>/dev/null || true
echo "===time_dirs==="
ls '{d}' 2>/dev/null | grep -E '^[0-9]+$' | sort -n | tail -1
echo "===CASE_END==="
""")
    parts.append('echo "===CASES_END==="')
    return "\n".join(parts)


def _section_lines(raw: str, start_tag: str) -> list[str]:
    lines = raw.splitlines()
    capturing = False
    end_re = re.compile(r"^===\w+=")
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s == start_tag:
            capturing = True
            continue
        if capturing:
            if end_re.match(s) and s != start_tag:
                break
            out.append(line)
    return out


def _parse_case_block(block: str) -> CaseGeom:
    def sec(name: str) -> str:
        return "\n".join(_section_lines(block, f"==={name}==="))

    m_dir = re.search(r"CASE_DIR:(.+)", block)
    cid = Path(m_dir.group(1).strip()).name if m_dir else "unknown"
    geom = CaseGeom(case_id=cid)

    bmd   = sec("blockMeshDict")
    owner = sec("owner")
    tdir  = sec("time_dirs").strip()

    coords = re.findall(r'\(\s*(-?[\d.e+\-]+)\s+(-?[\d.e+\-]+)\s+(-?[\d.e+\-]+)\s*\)', bmd)
    if coords:
        xs = [abs(float(c[0])) for c in coords]
        zs = [abs(float(c[2])) for c in coords]
        R = max(xs) if xs else None
        L = max(zs) if zs else None
        if R and R > 1e-9:
            geom.D_mm = round(R * 2 * 1000, 1)
        if L and L > 1e-9:
            geom.L_mm = round(L * 1000, 1)
        if geom.D_mm and geom.L_mm:
            geom.L_D = round(geom.L_mm / geom.D_mm, 1)

    m_D  = re.search(r'[Dd]\s*=\s*([\d.]+)\s*m', bmd)
    m_L  = re.search(r'[Ll]\s*=\s*([\d.]+)\s*m', bmd)
    m_rc = re.search(r'r_c\s*=\s*([\d.]+)', bmd)
    if m_D:
        geom.D_mm = round(float(m_D.group(1)) * 1000, 1)
    if m_L:
        geom.L_mm = round(float(m_L.group(1)) * 1000, 1)
        if geom.D_mm:
            geom.L_D = round(geom.L_mm / geom.D_mm, 1)
    if m_rc:
        geom.r_c = float(m_rc.group(1))

    m_nc = re.search(r'nCells\s*[:\s]+(\d+)', owner)
    if not m_nc:
        m_nc = re.search(r'(\d{4,})', owner)
    if m_nc:
        geom.n_cells = int(m_nc.group(1))

    if tdir:
        try:
            geom.current_step = int(float(tdir))
        except ValueError:
            pass

    return geom


def _collect() -> CFDStatus:
    now = dt.datetime.utcnow()
    ts      = now.replace(microsecond=0).isoformat() + "Z"
    next_ts = (now + dt.timedelta(seconds=REFRESH_INTERVAL)).replace(microsecond=0).isoformat() + "Z"
    base    = CFDStatus(ts=ts, next_refresh_ts=next_ts, cases_base="~/vortex_opt/of_cases")

    discover_out = ssh_exec(_DISCOVER_SCRIPT, timeout=20)
    if not discover_out:
        base.error = "SSH 连接失败"
        return base

    running_dirs = _pick_running_dirs(discover_out)
    base.n_running = len(running_dirs)

    if not running_dirs:
        base.ok = True
        return base

    info_script = _case_info_script(running_dirs)
    info_out    = ssh_exec(info_script, timeout=30)

    case_blocks = re.split(r"===CASE_BEGIN===", info_out)
    cases: list[CaseGeom] = []
    for block in case_blocks:
        if "CASE_DIR:" not in block:
            continue
        cases.append(_parse_case_block(block))
    base.cases = cases

    if len(case_blocks) > 1:
        first = case_blocks[1]

        def fsec(name: str) -> str:
            return "\n".join(_section_lines(first, f"==={name}==="))

        ctrl = fsec("controlDict")
        m_solver = re.search(r'application\s+(\S+?)\s*;', ctrl)
        base.solver = m_solver.group(1) if m_solver else "rhoSimpleFoam"

        turb = fsec("turbulenceProperties")
        m_turb = re.search(r'RASModel\s+(\S+?)\s*;', turb)
        if not m_turb:
            m_turb = re.search(r'LESModel\s+(\S+?)\s*;', turb)
        base.turb_model = m_turb.group(1) if m_turb else "unknown"

        decomp = fsec("decomposeParDict")
        m_cores = re.search(r'numberOfSubdomains\s+(\d+)\s*;', decomp)
        base.cores_per_case = int(m_cores.group(1)) if m_cores else 0

    base.of_version = "OpenFOAM 13"
    base.ok = True
    return base


async def get_cfd_status(force: bool = False) -> CFDStatus:
    global _cache, _cache_time
    import time
    async with _lock:
        if force or _cache is None or (time.monotonic() - _cache_time) >= REFRESH_INTERVAL:
            _cache = await asyncio.to_thread(_collect)
            _cache_time = time.monotonic()
    return _cache
