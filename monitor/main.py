from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .providers.cfd_status import get_cfd_status
from .providers.heartbeat import HeartbeatState, heartbeat_loop, init_state
from .providers.qwen_diagnose import get_latest_report, run_diagnosis
from .providers.jobs import (
    get_job_detail,
    get_job_list,
    get_live_job_snapshot,
)
from .providers.metrics import get_metric_sample


def _root_dir() -> Path:
    env = os.getenv("VORTEX_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


ROOT = _root_dir()
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Manifold Monitor", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.state.heartbeat_state = HeartbeatState(False, 1.0, 80)
app.state.heartbeat_task = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "root": str(ROOT)})


@app.get("/api/jobs")
def jobs() -> JSONResponse:
    return JSONResponse({"items": get_job_list(ROOT)})


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str) -> JSONResponse:
    detail = get_job_detail(ROOT, job_id)
    if detail is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(detail)


@app.websocket("/ws/metrics")
async def ws_metrics(ws: WebSocket) -> None:
    await ws.accept()
    interval_s = float(ws.query_params.get("interval", "1"))
    interval_s = max(0.5, min(10.0, interval_s))
    try:
        while True:
            sample = await get_metric_sample()
            await ws.send_text(json.dumps(sample, ensure_ascii=False))
            await asyncio.sleep(interval_s)
    except WebSocketDisconnect:
        return


@app.websocket("/ws/job")
async def ws_job(ws: WebSocket) -> None:
    await ws.accept()
    interval_s = float(ws.query_params.get("interval", "2"))
    interval_s = max(0.5, min(10.0, interval_s))
    try:
        while True:
            snapshot = get_live_job_snapshot(ROOT)
            await ws.send_text(json.dumps(snapshot, ensure_ascii=False))
            await asyncio.sleep(interval_s)
    except WebSocketDisconnect:
        return


@app.get("/api/live")
def live_snapshot() -> JSONResponse:
    data: dict[str, Any] = {"metrics": None, "job": None}
    data["job"] = get_live_job_snapshot(ROOT)
    data["heartbeat"] = app.state.heartbeat_state.payload()
    return JSONResponse(data)


@app.get("/api/heartbeat")
def heartbeat() -> JSONResponse:
    return JSONResponse(app.state.heartbeat_state.payload())


@app.websocket("/ws/heartbeat")
async def ws_heartbeat(ws: WebSocket) -> None:
    await ws.accept()
    interval_s = float(ws.query_params.get("interval", "1"))
    interval_s = max(0.5, min(10.0, interval_s))
    try:
        while True:
            await ws.send_text(json.dumps(app.state.heartbeat_state.payload(), ensure_ascii=False))
            await asyncio.sleep(interval_s)
    except WebSocketDisconnect:
        return


@app.get("/api/cfd-status")
async def cfd_status_api() -> JSONResponse:
    status = await get_cfd_status()
    return JSONResponse(status.to_dict())


@app.post("/api/cfd-status/refresh")
async def cfd_status_refresh() -> JSONResponse:
    status = await get_cfd_status(force=True)
    return JSONResponse(status.to_dict())


@app.get("/api/qwen-diagnose")
def qwen_diagnose_get() -> JSONResponse:
    r = get_latest_report()
    if r is None:
        return JSONResponse({"ok": False, "qwen_analysis": "尚未运行诊断", "ts": None})
    return JSONResponse(r.to_dict())


@app.post("/api/qwen-diagnose")
async def qwen_diagnose_post(auto_launch: bool = True) -> JSONResponse:
    report = await run_diagnosis(auto_launch=auto_launch)
    return JSONResponse(report.to_dict())


@app.post("/api/admin/ssh-exec")
async def admin_ssh_exec(body: dict) -> JSONResponse:
    """管理员接口：通过共享 SSH 连接在服务器上执行命令。仅供内部管理使用。"""
    from .providers.ssh_pool import ssh_exec
    cmd = body.get("cmd", "")
    timeout = int(body.get("timeout", 20))
    if not cmd:
        return JSONResponse({"error": "cmd required"}, status_code=400)
    out = await asyncio.to_thread(ssh_exec, cmd, timeout)
    return JSONResponse({"output": out})


@app.on_event("startup")
async def _startup() -> None:
    state, client, model = init_state(ROOT)
    app.state.heartbeat_state = state
    if state.enabled and client and model:
        app.state.heartbeat_task = asyncio.create_task(heartbeat_loop(ROOT, state, client, model))
