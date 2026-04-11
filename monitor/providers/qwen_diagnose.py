"""
Qwen 智能诊断 provider。

通过 SSH 采集服务器上 OpenFOAM 的运行状态，发送给 Qwen 分析：
- 当前是否有 OF 进程运行
- 如有案例目录，读取日志、错误信息
- Qwen 输出：问题原因 + Debug 计划 + 可选启动指令

如果判断可以启动 OF，在用户允许时自动在服务器后台拉起。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

try:
    import paramiko
    _OK = True
except ImportError:
    _OK = False

_SSH_HOST = os.getenv("VORTEX_SSH_HOST", "192.168.110.10")
_SSH_PORT  = int(os.getenv("VORTEX_SSH_PORT", "22"))
_SSH_USER  = os.getenv("VORTEX_SSH_USER", "liumq")
_SSH_KEY   = os.getenv("VORTEX_SSH_KEY",  "C:/Users/LMQ/.ssh/id_ed25519")

from .ssh_pool import ssh_exec, is_connected

_LLM_BASE_URL = os.getenv("VORTEX_LLM_BASE_URL", "http://192.168.110.10:8001/v1")
_LLM_MODEL    = os.getenv("VORTEX_LLM_MODEL",    "qwen2.5-72b")
_LLM_API_KEY  = os.getenv("VORTEX_LLM_API_KEY",  "dummy")
_CASES_BASE   = os.getenv("VORTEX_REMOTE_CASES_BASE", "~/manifold_cases")


# ── 状态结构 ─────────────────────────────────────────────────────────────────

@dataclass
class DiagReport:
    ts: str = ""
    ok: bool = False
    of_running: bool = False
    foam_procs: list[str] = field(default_factory=list)
    foam_cwds: list[str] = field(default_factory=list)
    case_dirs: list[str] = field(default_factory=list)
    log_tail: str = ""
    error_snippet: str = ""
    of_version: str = ""
    qwen_analysis: str = ""   # 原因分析
    qwen_plan: str = ""       # Debug 计划
    qwen_action: str = ""     # 建议的下一步命令
    actions_taken: list[str] = field(default_factory=list)
    ssh_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── 全局缓存 ─────────────────────────────────────────────────────────────────

_latest: DiagReport | None = None
_lock = asyncio.Lock()


# ── SSH helpers — use shared pool for reads; own connection for launch ──────

def _exec_shared(cmd: str, timeout: int = 20) -> str:
    """Use the shared persistent connection for read-only queries."""
    return ssh_exec(cmd, timeout=timeout)


def _get_own_ssh() -> "paramiko.SSHClient | None":
    """Open a dedicated connection for long-running launch commands."""
    if not _OK:
        return None
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            hostname=_SSH_HOST, port=_SSH_PORT, username=_SSH_USER,
            key_filename=_SSH_KEY if Path(_SSH_KEY).exists() else None,
            timeout=12, banner_timeout=20,
        )
        return c
    except Exception:
        return None


def _exec_own(ssh: "paramiko.SSHClient", cmd: str, timeout: int = 30) -> str:
    try:
        _, o, e = ssh.exec_command(cmd, timeout=timeout)
        out = o.read().decode("utf-8", errors="replace")
        err = e.read().decode("utf-8", errors="replace")
        return (out + err).strip()
    except Exception:
        return ""


# ── 诊断采集脚本 ─────────────────────────────────────────────────────────────

_DIAG_SCRIPT = r"""
echo '===PROCS==='
ps aux | grep -E 'rhoSimpleFoam|foamRun|simpleFoam|rhoPimpleFoam|buoyantSimpleFoam|mpirun.*Foam' \
  | grep -v grep || echo '(无 OF 进程)'

echo '===CWDS==='
for pid in $(ps aux | grep -E 'rhoSimpleFoam|foamRun|simpleFoam|rhoPimpleFoam|buoyantSimpleFoam' \
    | grep -v grep | awk '{print $2}'); do
  readlink /proc/$pid/cwd 2>/dev/null
done | sort -u

echo '===OF_VERSION==='
bash -lc 'source /opt/openfoam13/etc/bashrc 2>/dev/null && foamVersion 2>/dev/null' || \
  bash -lc 'source /opt/openfoam*/etc/bashrc 2>/dev/null && foamVersion 2>/dev/null' || \
  echo 'unknown'

echo '===CASE_DIRS==='
find {{CASES_BASE}} -maxdepth 3 -type d -name system -printf '%h\n' 2>/dev/null | head -20 || echo '(无 case 目录)'

echo '===LATEST_LOG==='
latest=$(find {{CASES_BASE}} -maxdepth 5 -name 'log.*' -printf '%T@ %p\n' 2>/dev/null \
  | sort -rn | head -1 | awk '{print $2}')
if [ -n "$latest" ]; then
  echo "LOG_FILE:$latest"
  tail -60 "$latest"
else
  echo '(无求解日志)'
fi

echo '===BLOCKMESH_LOG==='
latest_bm=$(find {{CASES_BASE}} -maxdepth 5 -name 'log.blockMesh' -printf '%T@ %p\n' 2>/dev/null \
  | sort -rn | head -1 | awk '{print $2}')
[ -n "$latest_bm" ] && tail -30 "$latest_bm" || echo '(无 blockMesh 日志)'

echo '===DECOMPOSE_LOG==='
latest_dc=$(find {{CASES_BASE}} -maxdepth 5 -name 'log.decomposePar' -printf '%T@ %p\n' 2>/dev/null \
  | sort -rn | head -1 | awk '{print $2}')
[ -n "$latest_dc" ] && tail -20 "$latest_dc" || echo '(无 decomposePar 日志)'

echo '===GPU_LOAD==='
nvidia-smi --query-gpu=index,utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>/dev/null || true

echo '===DISK==='
df -h {{CASES_BASE}} 2>/dev/null | tail -1
"""


def _parse_section(raw: str, tag: str) -> str:
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
            out.append(line)
    return "\n".join(out).strip()


# ── Qwen 调用 ────────────────────────────────────────────────────────────────

def _call_qwen(state_summary: str) -> tuple[str, str, str]:
    """返回 (analysis, plan, action_cmd)"""
    try:
        import httpx
        from openai import OpenAI
        http_client = httpx.Client(
            transport=httpx.HTTPTransport(proxy=None),
            trust_env=False,
        )
        client = OpenAI(
            base_url=_LLM_BASE_URL,
            api_key=_LLM_API_KEY,
            http_client=http_client,
        )
        prompt = f"""你是一个 OpenFOAM CFD 仿真专家和 Linux 运维工程师。
以下是服务器当前状态信息（通过 SSH 采集）：

{state_summary}

请完成以下三个任务，用中文回答，格式严格如下：

【原因分析】
（分析 OpenFOAM 为何没有运行，或正在运行时的健康状态。如有错误，指出具体原因。100字以内）

【Debug 计划】
（分步骤列出排查和修复方案，每步一行，用数字编号。如果可以直接启动，说明启动步骤）

【建议命令】
（给出下一步最重要的一条 bash 命令，用于启动或修复 OpenFOAM。只写命令本身，不要解释，不要换行）
"""
        resp = client.chat.completions.create(
            model=_LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.2,
        )
        content = (resp.choices[0].message.content or "").strip()

        def _extract(tag: str) -> str:
            m = re.search(rf"【{tag}】\s*(.*?)(?=【|\Z)", content, re.DOTALL)
            return m.group(1).strip() if m else ""

        analysis = _extract("原因分析")
        plan      = _extract("Debug 计划")
        action    = _extract("建议命令")
        return analysis, plan, action
    except Exception as e:
        return f"Qwen 调用失败: {e}", "", ""


# ── 启动 OpenFOAM ─────────────────────────────────────────────────────────────

def _read_case_params(case_dir: str) -> dict:
    """从案例文件中读取几何参数，返回 dict {R, L, r_c, n_cores}。"""
    import math
    params = {"R": 0.0125, "L": 0.25, "r_c": 0.00375, "n_cores": 16}
    # 尝试从 blockMeshDict 顶点中提取（如果文件存在但内容有误）
    bmd = _exec_shared(f"cat '{case_dir}/system/blockMeshDict' 2>/dev/null | head -60")
    coords = re.findall(r'\(\s*(-?[\d.e+\-]+)\s+(-?[\d.e+\-]+)\s+(-?[\d.e+\-]+)\s*\)', bmd)
    if coords:
        xs = [abs(float(c[0])) for c in coords if float(c[0]) != 0]
        zs = [abs(float(c[2])) for c in coords if float(c[2]) != 0]
        if xs: params["R"] = max(xs)
        if zs: params["L"] = max(zs)
    # 从 decomposeParDict 读取核数
    decomp = _exec_shared(f"cat '{case_dir}/system/decomposeParDict' 2>/dev/null")
    m = re.search(r'numberOfSubdomains\s+(\d+)\s*;', decomp)
    if m: params["n_cores"] = int(m.group(1))
    return params


def _make_blockMeshDict(R: float, L: float, r_c: float,
                        n_r: int = 8, n_c: int = 6, n_z: int = 50) -> str:
    """生成正确的 3D O-grid 圆柱体 blockMeshDict。
    5 个块：1 个中心块 + 4 个外部弧形块。
    arc 边确保壁面为真圆柱。
    r_c 用于标记冷端半径（不影响网格，仅用于注释）。
    """
    import math
    a   = R * 0.4          # 内方块半宽
    ow  = R / math.sqrt(2) # 外角点 (±R/√2, ±R/√2)

    def v(x, y, z):
        return f"( {x:.8f} {y:.8f} {z:.8f} )"

    # 16 vertices: z=0 → 0-7, z=L → 8-15
    verts = []
    for z in [0.0, L]:
        verts += [
            v(-a, -a, z),   # SW inner
            v( a, -a, z),   # SE inner
            v( a,  a, z),   # NE inner
            v(-a,  a, z),   # NW inner
            v(-ow, -ow, z), # SW wall
            v( ow, -ow, z), # SE wall
            v( ow,  ow, z), # NE wall
            v(-ow,  ow, z), # NW wall
        ]

    verts_str = "\n".join(f"    {v_}  // {i}" for i, v_ in enumerate(verts))

    # Arc mid-points (on cylinder surface at cardinal points)
    def arc_z(z):
        return "\n".join([
            f"    arc  4  5 ( 0.0  {-R:.8f} {z:.8f} )",  # SW→SE  bottom arc
            f"    arc  5  6 ( {R:.8f}  0.0  {z:.8f} )",  # SE→NE  right arc
            f"    arc  6  7 ( 0.0  {R:.8f}  {z:.8f} )",  # NE→NW  top arc
            f"    arc  7  4 ( {-R:.8f} 0.0  {z:.8f} )",  # NW→SW  left arc
        ])

    arcs_z0 = arc_z(0.0)
    arcs_zL_raw = [
        f"    arc 12 13 ( 0.0  {-R:.8f} {L:.8f} )",
        f"    arc 13 14 ( {R:.8f}  0.0  {L:.8f} )",
        f"    arc 14 15 ( 0.0  {R:.8f}  {L:.8f} )",
        f"    arc 15 12 ( {-R:.8f} 0.0  {L:.8f} )",
    ]
    arcs_zL = "\n".join(arcs_zL_raw)

    nc, nz, nr = n_c, n_z, n_r

    content = f"""\
/*--------------------------------*- C++ -*----------------------------------*\\
  3D Vortex Tube blockMeshDict — O-grid cylinder
  D={2*R:.4f}m  L={L:.4f}m  r_c={r_c:.5f}m
  5 blocks: 1 center + 4 outer arcs
\\*---------------------------------------------------------------------------*/
FoamFile
{{
    format      ascii;
    class       dictionary;
    object      blockMeshDict;
}}

scale   1;

vertices
(
{verts_str}
);

blocks
(
    // Centre block
    hex ( 0  1  2  3  8  9 10 11) ({nc} {nc} {nz}) simpleGrading (1 1 1)
    // Bottom outer block
    hex ( 4  5  1  0 12 13  9  8) ({nr} {nc} {nz}) simpleGrading (1 1 1)
    // Right outer block
    hex ( 5  6  2  1 13 14 10  9) ({nr} {nc} {nz}) simpleGrading (1 1 1)
    // Top outer block
    hex ( 6  7  3  2 14 15 11 10) ({nr} {nc} {nz}) simpleGrading (1 1 1)
    // Left outer block
    hex ( 7  4  0  3 15 12  8 11) ({nr} {nc} {nz}) simpleGrading (1 1 1)
);

edges
(
{arcs_z0}
{arcs_zL}
);

boundary
(
    coldEnd
    {{
        type    patch;
        faces
        (
            ( 0  1  2  3)
            ( 4  5  1  0)
            ( 5  6  2  1)
            ( 6  7  3  2)
            ( 7  4  0  3)
        );
    }}
    hotEnd
    {{
        type    patch;
        faces
        (
            ( 8  9 10 11)
            (12 13  9  8)
            (13 14 10  9)
            (14 15 11 10)
            (15 12  8 11)
        );
    }}
    wall
    {{
        type    wall;
        faces
        (
            ( 4  5 13 12)
            ( 5  6 14 13)
            ( 6  7 15 14)
            ( 7  4 12 15)
        );
    }}
    inlet
    {{
        type    patch;
        faces   ();
    }}
);
"""
    return content


def _try_launch_of(case_dir: str) -> list[str]:
    """尝试在服务器上启动 OpenFOAM，全部通过共享 SSH 连接完成。"""
    import time
    actions: list[str] = []
    foam_source = "/opt/openfoam13/etc/bashrc"

    # ── 如果 blockMeshDict 缺失，生成一个正确的 O-grid 版本 ──────────────
    has_bmd = _exec_shared(f"[ -f '{case_dir}/system/blockMeshDict' ] && echo yes || echo no").strip()
    if has_bmd != "yes":
        params = _read_case_params(case_dir)
        bmd_content = _make_blockMeshDict(
            R=params["R"], L=params["L"], r_c=params["r_c"])
        # 用 base64 写入文件（避免 heredoc 在 exec_command 中的兼容性问题）
        import base64
        b64 = base64.b64encode(bmd_content.encode("utf-8")).decode("ascii")
        write_cmd = f"echo '{b64}' | base64 -d > '{case_dir}/system/blockMeshDict' && echo WRITE_OK"
        out = _exec_shared(write_cmd, timeout=15)
        if "WRITE_OK" in out:
            actions.append(f"已生成 blockMeshDict (O-grid 5块, R={params['R']:.4f}m L={params['L']:.4f}m)")
        else:
            actions.append(f"blockMeshDict 写入失败: {out[:200]}")
            return actions

    # blockMesh
    has_mesh = _exec_shared(f"[ -f '{case_dir}/constant/polyMesh/points' ] && echo yes || echo no")
    if has_mesh.strip() != "yes":
        out = _exec_shared(
            f"bash -lc 'source {foam_source} && cd {case_dir} && blockMesh > log.blockMesh 2>&1 && echo EXIT:0 || echo EXIT:1'",
            timeout=90)
        if "EXIT:0" in out:
            actions.append("blockMesh 成功完成")
        else:
            tail = _exec_shared(f"tail -20 '{case_dir}/log.blockMesh' 2>/dev/null")
            actions.append(f"blockMesh 失败:\n{tail[:500]}")
            return actions
    else:
        actions.append("网格已存在，跳过 blockMesh")

    # decomposePar
    has_proc0 = _exec_shared(f"[ -d '{case_dir}/processor0' ] && echo yes || echo no")
    decomp_txt = _exec_shared(f"cat '{case_dir}/system/decomposeParDict' 2>/dev/null")
    m_cores = re.search(r'numberOfSubdomains\s+(\d+)\s*;', decomp_txt)
    n_cores = int(m_cores.group(1)) if m_cores else 16

    if has_proc0.strip() != "yes":
        out = _exec_shared(
            f"bash -lc 'source {foam_source} && cd {case_dir} && decomposePar > log.decomposePar 2>&1 && echo EXIT:0 || echo EXIT:1'",
            timeout=90)
        if "EXIT:0" in out:
            actions.append(f"decomposePar 完成（{n_cores} 核）")
        else:
            tail = _exec_shared(f"tail -15 '{case_dir}/log.decomposePar' 2>/dev/null")
            actions.append(f"decomposePar 失败:\n{tail[:400]}")
            return actions
    else:
        actions.append(f"processor0 已存在，跳过 decomposePar（{n_cores} 核）")

    # 读取求解器名称
    ctrl_txt = _exec_shared(f"cat '{case_dir}/system/controlDict' 2>/dev/null")
    m_solver = re.search(r'application\s+(\S+?)\s*;', ctrl_txt)
    solver = m_solver.group(1) if m_solver else "rhoSimpleFoam"

    # nohup 后台启动 — 立即返回，不阻塞连接
    launch_cmd = (
        f"bash -lc 'source {foam_source} && cd {case_dir} && "
        f"nohup mpirun -np {n_cores} {solver} -parallel > log.{solver} 2>&1 </dev/null & "
        f"disown; echo LAUNCHED:$!'"
    )
    out = _exec_shared(launch_cmd, timeout=20)
    pid_m = re.search(r"LAUNCHED:(\d+)", out)
    if pid_m:
        actions.append(f"已启动 {solver}（PID {pid_m.group(1)}，{n_cores} 核并行）")
    else:
        actions.append(f"已发送启动命令: mpirun -np {n_cores} {solver} -parallel")

    # 等 5 秒确认进程存在
    time.sleep(5)
    proc_check = _exec_shared(f"ps aux | grep '{solver}' | grep -v grep | wc -l")
    try:
        cnt = int(proc_check.strip())
        if cnt > 0:
            actions.append(f"✓ 确认：检测到 {cnt} 个 {solver} 进程正在运行")
        else:
            # 看看日志头
            log_head = _exec_shared(f"head -20 '{case_dir}/log.{solver}' 2>/dev/null")
            actions.append(f"⚠ 进程未检测到，日志:\n{log_head[:300]}")
    except ValueError:
        pass

    return actions


# ── 主诊断函数 ────────────────────────────────────────────────────────────────

def _diagnose(auto_launch: bool = True) -> DiagReport:
    now = dt.datetime.utcnow()
    ts  = now.replace(microsecond=0).isoformat() + "Z"
    report = DiagReport(ts=ts)

    # ── 采集状态：使用共享连接 ────────────────────────────────────────────
    raw = _exec_shared(_DIAG_SCRIPT.replace("{{CASES_BASE}}", _CASES_BASE), timeout=30)
    if not raw.strip():
        report.ssh_error = f"SSH 连接失败 ({_SSH_HOST})"
        report.qwen_analysis = "无法连接服务器，SSH 连接失败。请检查网络和 SSH 密钥配置。"
        return report

    try:

        procs_raw  = _parse_section(raw, "PROCS")
        cwds_raw   = _parse_section(raw, "CWDS")
        of_ver     = _parse_section(raw, "OF_VERSION")
        case_dirs  = [l.strip().rstrip("/") for l in _parse_section(raw, "CASE_DIRS").splitlines() if l.strip() and l.strip() != "(无 case 目录)"]
        log_tail   = _parse_section(raw, "LATEST_LOG")
        bm_log     = _parse_section(raw, "BLOCKMESH_LOG")
        dc_log     = _parse_section(raw, "DECOMPOSE_LOG")
        gpu_load   = _parse_section(raw, "GPU_LOAD")
        disk_info  = _parse_section(raw, "DISK")

        foam_procs = [l.strip() for l in procs_raw.splitlines() if l.strip() and "无 OF 进程" not in l]
        foam_cwds = [l.strip().rstrip("/") for l in cwds_raw.splitlines() if l.strip()]
        of_running = (len(foam_procs) > 0) or (len(foam_cwds) > 0)

        # 提取日志中的错误片段
        error_keywords = ["FOAM FATAL", "Fatal error", "Floating point", "segfault", "Error", "failed", "SIGSEGV"]
        error_lines = [l for l in log_tail.splitlines() if any(k.lower() in l.lower() for k in error_keywords)]
        error_snippet = "\n".join(error_lines[-10:]) if error_lines else ""

        report.of_running    = of_running
        report.foam_procs    = foam_procs
        report.foam_cwds     = foam_cwds
        report.case_dirs     = case_dirs
        report.log_tail      = log_tail[:3000]
        report.error_snippet = error_snippet
        report.of_version    = of_ver.strip()

        # ── 构建给 Qwen 的状态摘要 ────────────────────────────────────────
        state_summary = f"""
=== OpenFOAM 进程状态 ===
{'有进程运行' if of_running else '没有 OpenFOAM 进程在运行'}
{procs_raw[:800] if of_running else ''}

=== OF 版本 ===
{of_ver}

=== 案例目录 ===
{chr(10).join(case_dirs) if case_dirs else '无案例目录'}

=== 最新求解器日志（末尾）===
{log_tail[-1500:] if log_tail else '无日志'}

=== blockMesh 日志 ===
{bm_log[-500:] if bm_log else '无'}

=== decomposePar 日志 ===
{dc_log[-500:] if dc_log else '无'}

=== GPU 负载 ===
{gpu_load}

=== 磁盘 ===
{disk_info}
"""

        # ── 调用 Qwen 分析 ────────────────────────────────────────────────
        analysis, plan, action = _call_qwen(state_summary)
        report.qwen_analysis = analysis
        report.qwen_plan     = plan
        report.qwen_action   = action

        # ── 自动启动 OF（若未运行且有可用案例）─────────────────────────────
        if auto_launch and not of_running:
            if case_dirs:
                launch_dir = case_dirs[0]
            else:
                launch_dir = None

            if launch_dir:
                actions = _try_launch_of(launch_dir)
                report.actions_taken = actions
            else:
                report.actions_taken = ["未找到可启动的 OpenFOAM 案例目录"]

        report.ok = True

    except Exception as e:
        report.ssh_error = str(e)

    return report


# ── 公共 API ─────────────────────────────────────────────────────────────────

async def run_diagnosis(auto_launch: bool = True) -> DiagReport:
    global _latest
    async with _lock:
        _latest = await asyncio.to_thread(_diagnose, auto_launch)
    return _latest


def get_latest_report() -> DiagReport | None:
    return _latest
