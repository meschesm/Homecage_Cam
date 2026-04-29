from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from .auth import (
    login_response, logout_response, require_auth, require_auth_api,
    verify_password,
)
from .camera import discover_cameras
from .config import settings
from .recording import job_manager, run_job
from .session  import get_session, maybe_resume_session, start_session, stop_session as stop_session_fn
from .storage  import storage_manager
from .streaming import get_active_streams, is_streaming, stop_stream, stream_frames
from .utils    import fmt_bytes

app = FastAPI(title="homecagev3", docs_url=None, redoc_url=None)
_hostname = socket.gethostname()


@app.on_event("startup")
async def _startup():
    await maybe_resume_session()

_templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    return _templates.TemplateResponse("login.html", {"request": request, "next": next, "error": None})


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
):
    if (username == settings.admin_username
            and settings.admin_password_hash
            and verify_password(password, settings.admin_password_hash)):
        return login_response(username, next_url=next or "/")
    return _templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next, "error": "Invalid username or password"},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@app.get("/logout")
async def logout(_: str = Depends(require_auth)):
    return logout_response()


# ── Page routes ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(_: str = Depends(require_auth)):
    return RedirectResponse(url="/camera-test")


@app.get("/camera-test", response_class=HTMLResponse)
async def camera_test_page(request: Request, user: str = Depends(require_auth)):
    return _templates.TemplateResponse(
        "camera_test.html", {"request": request, "user": user, "hostname": _hostname}
    )


@app.get("/recording", response_class=HTMLResponse)
async def recording_page(request: Request, user: str = Depends(require_auth)):
    return _templates.TemplateResponse(
        "session.html", {"request": request, "user": user, "hostname": _hostname}
    )


# ── API routes ─────────────────────────────────────────────────────────────────

@app.get("/api/cameras")
async def api_cameras(_: str = Depends(require_auth_api)):
    cameras = discover_cameras()
    return [c.model_dump() for c in cameras]


@app.get("/api/camera-names")
async def api_camera_names_get(_: str = Depends(require_auth_api)):
    if not settings.camera_names_file.exists():
        return {}
    try:
        return json.loads(settings.camera_names_file.read_text())
    except Exception:
        return {}


@app.post("/api/camera-names")
async def api_camera_names_post(request: Request, _: str = Depends(require_auth_api)):
    body = await request.json()
    settings.camera_names_file.parent.mkdir(parents=True, exist_ok=True)
    settings.camera_names_file.write_text(json.dumps(body))
    return {"saved": True}


@app.get("/api/camera-settings")
async def api_camera_settings_get(_: str = Depends(require_auth_api)):
    if not settings.camera_settings_file.exists():
        return {}
    try:
        return json.loads(settings.camera_settings_file.read_text())
    except Exception:
        return {}


@app.post("/api/camera-settings")
async def api_camera_settings_post(request: Request, _: str = Depends(require_auth_api)):
    body = await request.json()
    settings.camera_settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings.camera_settings_file.write_text(json.dumps(body))
    return {"saved": True}


@app.get("/stream/{node_name}")
async def stream(node_name: str, hflip: bool = False, vflip: bool = False, _: str = Depends(require_auth_api)):
    """Live MJPEG stream for a camera node (e.g. video0, flir_18474893)."""
    if node_name.startswith("flir_"):
        node = node_name
    elif node_name.startswith("video"):
        node = f"/dev/{node_name}"
    else:
        return JSONResponse({"error": "Invalid node"}, status_code=400)
    try:
        return StreamingResponse(
            stream_frames(node, hflip=hflip, vflip=vflip),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)


@app.delete("/stream/{node_name}")
async def stream_stop(node_name: str, _: str = Depends(require_auth_api)):
    node = node_name if node_name.startswith("flir_") else f"/dev/{node_name}"
    await stop_stream(node)
    return {"stopped": node_name}


@app.get("/api/streams")
async def api_streams(_: str = Depends(require_auth_api)):
    """Return which nodes are currently streaming."""
    return {"streaming": get_active_streams()}


@app.post("/api/record")
async def api_record(
    request: Request,
    background_tasks: BackgroundTasks,
    _: str = Depends(require_auth_api),
):
    body = await request.json()

    # Validate required fields
    required = {"camera_name", "node", "input_fmt", "width", "height", "fps", "duration"}
    missing = required - body.keys()
    if missing:
        return JSONResponse({"error": f"Missing fields: {missing}"}, status_code=400)

    # Stop any active stream on this node before recording
    node = body["node"]
    if is_streaming(node):
        await stop_stream(node)
        await asyncio.sleep(0.5)  # give the device time to release

    params = {
        "node":      body["node"],
        "input_fmt": body["input_fmt"],
        "width":     int(body["width"]),
        "height":    int(body["height"]),
        "fps":       int(body["fps"]),
        "duration":  int(body["duration"]),
        "encoder":   body.get("encoder", "libx264"),
        "preset":    body.get("preset", "ultrafast"),
        "crf":       int(body.get("crf", 23)),
        "denoise":            bool(body.get("denoise", False)),
        "timestamp_overlay":  bool(body.get("timestamp_overlay", False)),
    }

    job = job_manager.create(
        camera_name=body["camera_name"],
        params=params,
        duration=params["duration"],
    )
    background_tasks.add_task(run_job, job)
    return {"job_id": job.id}


@app.get("/api/record/{job_id}")
async def api_record_status(job_id: str, _: str = Depends(require_auth_api)):
    job = job_manager.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return job.to_dict()


@app.get("/api/jobs")
async def api_jobs(_: str = Depends(require_auth_api)):
    return [j.to_dict() for j in job_manager.all()]


# ── Session routes ──────────────────────────────────────────────────────────────

@app.post("/api/session")
async def api_session_start(request: Request, _: str = Depends(require_auth_api)):
    body = await request.json()
    cameras      = body.get("cameras", [])
    duration     = body.get("duration")       # seconds or None
    session_name   = body.get("session_name", "session")
    scheduled_time = body.get("scheduled_time") or None   # "HH:MM" 24-hr or None
    if not cameras:
        return JSONResponse({"error": "No cameras provided"}, status_code=400)
    session = await start_session(cameras, duration, session_name, scheduled_time)
    return {"session_id": session.id}


@app.get("/api/session")
async def api_session_get(_: str = Depends(require_auth_api)):
    s = get_session()
    return s.to_dict() if s else None


@app.delete("/api/session")
async def api_session_stop(_: str = Depends(require_auth_api)):
    await stop_session_fn()
    return {"stopped": True}


@app.post("/api/shutdown")
async def api_shutdown(_: str = Depends(require_auth_api)):
    await stop_session_fn()   # graceful ffmpeg stop (≤15 s drain built in)

    async def _do_shutdown():
        await asyncio.sleep(2)   # allow HTTP response to reach the client
        subprocess.run(["sudo", "shutdown", "-h", "now"])

    asyncio.ensure_future(_do_shutdown())
    return {"message": "Shutting down..."}


# ── Storage management ─────────────────────────────────────────────────────────

@app.get("/api/storage")
async def api_storage_get(_: str = Depends(require_auth_api)):
    storage_manager.auto_select()
    return storage_manager.status()


@app.post("/api/storage")
async def api_storage_select(request: Request, _: str = Depends(require_auth_api)):
    body = await request.json()
    mount = body.get("mount", "")
    if not storage_manager.select(mount):
        return JSONResponse({"error": "Drive not mounted"}, status_code=400)
    return {"active": mount}


@app.post("/api/storage/eject")
async def api_storage_eject(_: str = Depends(require_auth_api)):
    s = get_session()
    if s and s.status in ("running", "scheduled"):
        return JSONResponse({"error": "Stop the active session before ejecting"}, status_code=409)
    try:
        await storage_manager.eject()
        return {"ejected": True}
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Recordings browser ─────────────────────────────────────────────────────────

@app.get("/api/recordings")
async def api_recordings(_: str = Depends(require_auth_api)):
    base = storage_manager.recordings_dir
    if not base.exists():
        return []
    result = []
    for folder in sorted(base.iterdir()):
        if not folder.is_dir():
            continue
        files = []
        # stat() once per file (sort key and data share the same stat call)
        file_stats = [(f, f.stat()) for f in folder.glob("*.mp4")]
        for f, st in sorted(file_stats, key=lambda t: t[1].st_mtime, reverse=True):
            files.append({
                "name":       f.name,
                "path":       f"{folder.name}/{f.name}",
                "size_bytes": st.st_size,
                "size_fmt":   fmt_bytes(st.st_size),
                "mtime":      datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
        if files:
            display = folder.name.replace("__", ": ").replace("_", " ").title()
            result.append({"folder": folder.name, "display_name": display, "files": files})
    return result


@app.get("/files/{path:path}")
async def serve_recording(
    path: str,
    dl: bool = False,
    _: str = Depends(require_auth_api),
):
    """Serve a recording file. ?dl=1 forces download; otherwise inline for browser playback."""
    rec_dir = storage_manager.recordings_dir
    target = (rec_dir / path).resolve()
    # Guard against path traversal
    if not str(target).startswith(str(rec_dir.resolve())):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(
        str(target),
        media_type="video/mp4",
        filename=target.name if dl else None,
    )


# ── Job log ─────────────────────────────────────────────────────────────────────

@app.get("/api/job-log")
async def api_job_log(_: str = Depends(require_auth_api)):
    log_path = storage_manager.test_recordings_dir / "job_log.jsonl"
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return list(reversed(entries))   # newest first


# ── Recording preview ───────────────────────────────────────────────────────────

@app.get("/api/preview/{node_name}")
async def api_preview(node_name: str, _: str = Depends(require_auth_api)):
    """Return the latest preview JPEG grabbed from the in-progress recording segment."""
    if not (node_name.startswith("video") or node_name.startswith("flir_")):
        return JSONResponse({"error": "Invalid node"}, status_code=400)
    path = Path(f"/tmp/hcv3_preview_{node_name}.jpg")
    if not path.exists():
        return JSONResponse({"error": "No preview available"}, status_code=404)
    return FileResponse(
        str(path), media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ── Disk space ──────────────────────────────────────────────────────────────────

@app.get("/api/disk")
async def api_disk(_: str = Depends(require_auth_api)):
    active = storage_manager.active
    if not active:
        return JSONResponse({"error": "No drive selected"}, status_code=503)
    try:
        u = shutil.disk_usage(active)
        return {"total": u.total, "used": u.used, "free": u.free}
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
