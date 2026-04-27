"""
Multi-camera overnight recording sessions.
One session at a time; each camera runs an independent ffmpeg subprocess.
Both streams start in parallel and are monitored until done or stopped.

Recordings are split into 10-minute segments; on completion (or manual stop)
the segments are concatenated with ffmpeg concat into a single final MP4 and
the raw segment files are deleted.  If stitching fails the segments are
preserved on disk and the stream is marked with an error describing their
location.

Session configuration is persisted to disk before recording begins so that
the recording can be automatically resumed after a power interruption.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import settings
from .storage import storage_manager
from .utils import FMT_MAP, fmt_bytes

SEGMENT_DURATION = 600    # 10 minutes per segment
_PENDING_FILE    = Path("/home/ab-ivnc/homecagev3/pending_session.json")


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class StreamStatus:
    camera_name:       str
    node:              str
    output_path:       str           # final stitched file path
    params:            dict[str, Any]
    status:            str   = "pending"   # pending|running|stitching|done|error
    elapsed:           float = 0.0
    file_size:         int   = 0
    error:             str | None    = None
    stderr_lines:      list[str]     = field(default_factory=list)
    proc:              Any           = field(default=None, repr=False, compare=False)
    segments_dir:      str | None    = None   # directory holding segment files
    segment_list_path: str | None    = None   # ffmpeg -segment_list output file

    def to_dict(self) -> dict:
        elapsed  = self.elapsed
        rate_bps = (self.file_size / elapsed) if elapsed > 5 else None
        return {
            "camera_name":     self.camera_name,
            "node":            self.node,
            "output_path":     self.output_path,
            "params":          self.params,
            "status":          self.status,
            "elapsed":         round(elapsed, 1),
            "elapsed_fmt":     _fmt_elapsed(elapsed),
            "file_size":       fmt_bytes(self.file_size),
            "file_size_bytes": self.file_size,
            "rate_per_hr":     fmt_bytes(int(rate_bps * 3600)) if rate_bps else None,
            "error":           self.error,
            "stderr_lines":    self.stderr_lines[-10:],
        }


@dataclass
class Session:
    id:            str
    started_at:    str
    duration:      int | None          # seconds; None = run until stopped
    streams:       list[StreamStatus]
    status:        str = "running"     # scheduled|running|stopping|stopped|done|error
    scheduled_for: str | None = None   # "HH:MM" wall-clock target
    countdown:     int = 0             # seconds remaining until scheduled start

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "started_at":    self.started_at,
            "duration":      self.duration,
            "status":        self.status,
            "scheduled_for": self.scheduled_for,
            "countdown":     self.countdown,
            "streams":       [s.to_dict() for s in self.streams],
        }


def _fmt_elapsed(secs: float) -> str:
    s = int(secs)
    h, rem = divmod(s, 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── Global state ───────────────────────────────────────────────────────────────

_current: Session | None = None


def get_session() -> Session | None:
    return _current


# ── Session config persistence (for auto-resume after power loss) ──────────────

def _save_pending(camera_configs: list[dict], duration: int | None,
                  session_name: str, scheduled_time: str | None) -> None:
    try:
        _PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PENDING_FILE.write_text(json.dumps({
            "camera_configs":  camera_configs,
            "duration":        duration,
            "session_name":    session_name,
            "scheduled_time":  scheduled_time,
            "started_at":      datetime.now().isoformat(timespec="seconds"),
        }))
    except OSError:
        pass


def _clear_pending() -> None:
    try:
        _PENDING_FILE.unlink()
    except FileNotFoundError:
        pass


# ── ffmpeg command builder ─────────────────────────────────────────────────────

def _build_cmd(params: dict, segment_pattern: str, segment_list_path: str) -> list[str]:
    """Build the ffmpeg command for segmented recording.

    For non-copy encoders a second output is added that writes a preview JPEG
    every 10 seconds in real-time (fps=1/10).  This avoids trying to seek into
    an in-progress MP4 file (which lacks a moov atom until ffmpeg closes it).
    """
    input_fmt = FMT_MAP.get(params["input_fmt"].lower(), params["input_fmt"].lower())
    is_copy   = params.get("encoder") == "copy"
    loglevel  = "error" if is_copy else "warning"

    cmd = [
        "ffmpeg", "-y", "-loglevel", loglevel,
        "-f", "v4l2",
        "-input_format",      input_fmt,
        "-video_size",        f"{params['width']}x{params['height']}",
        "-thread_queue_size", "512",
        "-use_wallclock_as_timestamps", "1",
    ]
    if is_copy:
        cmd += ["-fflags", "+genpts"]
    else:
        cmd += ["-framerate", str(params["fps"])]
    cmd += ["-i", params["node"]]

    if not is_copy:
        rec_filters = [f"fps={params['fps']}"]
        if params.get("denoise"):
            rec_filters.append("hqdn3d=2:2:3:3")
        if params.get("timestamp_overlay"):
            rec_filters.append(
                r"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
                r":text='%{localtime\:%D %T}'"
                r":x=w-tw-10:y=h-th-10:fontsize=36"
                r":fontcolor=white@0.9:box=1:boxcolor=black@0.4:boxborderw=4"
            )
        # Split: [rec] → full recording chain; [prev] → 1 frame per 10 s preview with timestamp
        prev_ts = (
            r"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            r":text='%{localtime\:%D %T}'"
            r":x=10:y=10:fontsize=28:fontcolor=white@0.9:box=1:boxcolor=black@0.5:boxborderw=4"
        )
        filter_complex = (
            "[0:v]split=2[_m][_r];"
            "[_m]" + ",".join(rec_filters) + "[rec];"
            "[_r]fps=1/10," + prev_ts + "[prev]"
        )
        cmd += ["-filter_complex", filter_complex, "-map", "[rec]"]

    if is_copy:
        cmd += ["-c:v", "copy"]
    else:
        cmd += [
            "-c:v",    "libx264",
            "-preset", params.get("preset", "ultrafast"),
            "-crf",    str(params.get("crf", 23)),
        ]

    # Duration limit on the recording output
    if params.get("duration"):
        cmd += ["-t", str(params["duration"])]

    # Segment muxer — splits output at SEGMENT_DURATION boundaries
    cmd += [
        "-f",              "segment",
        "-segment_time",   str(SEGMENT_DURATION),
        "-segment_format", "mp4",
        "-reset_timestamps", "1",
        "-segment_list",   segment_list_path,
        segment_pattern,
    ]

    # Preview JPEG output: overwrite the same file on every frame (fps=1/10)
    if not is_copy:
        preview_path = f"/tmp/hcv3_preview_{Path(params['node']).name}.jpg"
        cmd += ["-map", "[prev]", "-update", "1", "-q:v", "5"]
        if params.get("duration"):
            cmd += ["-t", str(params["duration"])]
        cmd.append(preview_path)

    return cmd


# ── Segment stitching ──────────────────────────────────────────────────────────

async def _stitch_segments(stream: StreamStatus) -> bool:
    """Concatenate all recorded segments into stream.output_path.

    Returns True on success.  On failure the segment directory is left intact
    so the operator can retrieve the raw segments.
    """
    if not stream.segment_list_path or not stream.segments_dir:
        return False

    list_path = Path(stream.segment_list_path)
    segs_dir  = Path(stream.segments_dir)
    if not list_path.exists():
        return False

    lines = [l.strip() for l in list_path.read_text().splitlines() if l.strip()]
    if not lines:
        return False

    # Build the concat input file with absolute paths
    concat_file = segs_dir / "concat.txt"
    valid_segs  = []
    for seg in lines:
        p = Path(seg) if Path(seg).is_absolute() else segs_dir / seg
        if p.exists() and p.stat().st_size > 0:
            valid_segs.append(p)

    if not valid_segs:
        return False

    with open(concat_file, "w") as f:
        for p in valid_segs:
            f.write(f"file '{p}'\n")

    Path(stream.output_path).parent.mkdir(parents=True, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        stream.output_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    if proc.returncode == 0 and Path(stream.output_path).exists():
        shutil.rmtree(segs_dir, ignore_errors=True)
        return True
    return False


# ── Stream runner ──────────────────────────────────────────────────────────────

async def _run_stream(stream: StreamStatus) -> None:
    # Place segment files in a hidden subfolder next to the final output
    final_path = Path(stream.output_path)
    segs_dir   = final_path.parent / ("_segs_" + final_path.stem)
    segs_dir.mkdir(parents=True, exist_ok=True)

    seg_pattern   = str(segs_dir / (final_path.stem + "_%03d.mp4"))
    seg_list_path = str(segs_dir / "segments.txt")

    stream.segments_dir      = str(segs_dir)
    stream.segment_list_path = seg_list_path

    cmd     = _build_cmd(stream.params, seg_pattern, seg_list_path)
    t_start = time.monotonic()
    stream.status = "running"

    is_copy      = stream.params.get("encoder") == "copy"
    preview_path = (
        Path(f"/tmp/hcv3_preview_{Path(stream.node).name}.jpg") if not is_copy else None
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        stream.proc = proc

        async def _read_stderr():
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line and "deprecated pixel format used" not in line:
                    stream.stderr_lines.append(line)

        async def _tick():
            while proc.returncode is None:
                stream.elapsed = time.monotonic() - t_start
                try:
                    stream.file_size = sum(
                        f.stat().st_size for f in segs_dir.glob("*.mp4")
                        if f.exists()
                    )
                except OSError:
                    pass
                await asyncio.sleep(1.0)

        await asyncio.gather(_read_stderr(), _tick())
        await proc.wait()

        stream.elapsed = time.monotonic() - t_start

        # returncode 0 = natural end, -2 = SIGINT (Linux), 255 = SIGINT (ffmpeg)
        if proc.returncode in (0, -2, 255):
            stream.status = "stitching"
            ok = await _stitch_segments(stream)
            if ok:
                stream.status    = "done"
                out = Path(stream.output_path)
                if out.exists():
                    stream.file_size = out.stat().st_size
            else:
                stream.status = "error"
                stream.error  = (
                    "Stitch failed — segments preserved at " +
                    (stream.segments_dir or "?")
                )
        else:
            stream.status = "error"
            stream.error  = f"ffmpeg exited {proc.returncode}"

    except Exception as exc:
        stream.status  = "error"
        stream.error   = str(exc)
        stream.elapsed = time.monotonic() - t_start
    finally:
        if preview_path:
            preview_path.unlink(missing_ok=True)


# ── Session lifecycle ──────────────────────────────────────────────────────────

async def start_session(
    camera_configs: list[dict],
    duration: int | None,
    session_name: str = "session",
    scheduled_time: str | None = None,   # "HH:MM" 24-hr local time
) -> Session:
    global _current

    now       = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    safe_sess = re.sub(r"[^\w\-]", "_", session_name.strip()).lower() or "session"
    streams   = []

    # Persist config so we can auto-resume after a power bump
    _save_pending(camera_configs, duration, session_name, scheduled_time)

    for cfg in camera_configs:
        tag = re.sub(r"[^\w\-]", "_", cfg.get("camera_tag", cfg["camera_name"]).strip()).lower()
        tag = re.sub(r"_+", "_", tag).strip("_") or "cam"
        filename    = f"{safe_sess}_{tag}_{timestamp}.mp4"
        output_path = str(storage_manager.recordings_dir / safe_sess / filename)
        params = {
            "node":              cfg["node"],
            "input_fmt":         cfg["input_fmt"],
            "width":             int(cfg["width"]),
            "height":            int(cfg["height"]),
            "fps":               int(cfg.get("fps", 10)),
            "encoder":           cfg.get("encoder", "libx264"),
            "preset":            cfg.get("preset", "ultrafast"),
            "crf":               int(cfg.get("crf", 23)),
            "denoise":           bool(cfg.get("denoise", True)),
            "timestamp_overlay": bool(cfg.get("timestamp_overlay", True)),
            "duration":          duration,
        }
        streams.append(StreamStatus(
            camera_name=cfg["camera_name"],
            node=cfg["node"],
            output_path=output_path,
            params=params,
        ))

    initial_status = "scheduled" if scheduled_time else "running"
    session = Session(
        id=str(uuid.uuid4())[:8],
        started_at=now.isoformat(timespec="seconds"),
        duration=duration,
        streams=streams,
        status=initial_status,
    )
    _current = session
    asyncio.ensure_future(_watch_session(session, scheduled_time))
    return session


async def _wait_until(session: Session, scheduled_time: str) -> bool:
    """Sleep until HH:MM today (or tomorrow if past). Returns False if cancelled."""
    h, m = map(int, scheduled_time.split(":"))
    target = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= datetime.now():
        target += timedelta(days=1)
    session.scheduled_for = target.strftime("%Y-%m-%d %H:%M")
    while True:
        remaining = int((target - datetime.now()).total_seconds())
        if remaining <= 0:
            break
        if session.status == "stopped":
            return False
        session.countdown = remaining
        await asyncio.sleep(1)
    session.countdown = 0
    return True


async def _watch_session(session: Session, scheduled_time: str | None = None) -> None:
    if scheduled_time:
        proceed = await _wait_until(session, scheduled_time)
        if not proceed:
            _clear_pending()
            return
    session.status = "running"
    t_watch_start = time.monotonic()
    await asyncio.gather(
        *[_run_stream(s) for s in session.streams],
        return_exceptions=True,
    )
    # Only clear the pending file if the session ran long enough to be considered
    # a real run (≥60 s).  A very short run most likely means an auto-resume
    # failed immediately (e.g. cameras not ready at boot) — preserve the file so
    # the next reboot can try again.
    if time.monotonic() - t_watch_start >= 60:
        _clear_pending()
    if session.status not in ("stopping", "stopped"):
        session.status = "done" if all(
            s.status == "done" for s in session.streams
        ) else "error"


async def stop_session() -> None:
    global _current
    if not _current or _current.status not in ("running", "scheduled"):
        return
    _current.status = "stopping"
    for stream in _current.streams:
        if stream.proc and stream.proc.returncode is None:
            try:
                stream.proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
    # Wait up to 15 s for ffmpeg to flush and close the current segment
    for _ in range(15):
        await asyncio.sleep(1)
        if all(s.status in ("done", "stitching", "error") for s in _current.streams):
            break
    # Wait up to 120 s for segment stitching to complete
    for _ in range(120):
        if all(s.status in ("done", "error") for s in _current.streams):
            break
        await asyncio.sleep(1)
    _clear_pending()
    _current.status = "stopped"


# ── Auto-resume on startup ─────────────────────────────────────────────────────

async def maybe_resume_session() -> None:
    """Called at app startup: restart a session interrupted by power loss.

    Reads pending_session.json written by start_session().  If a duration was
    set, only resumes if there is at least 60 seconds remaining.  The resumed
    session records new segments into the same session folder so all footage
    ends up together.

    A 15-second startup delay lets USB cameras and the storage drive finish
    initialising before ffmpeg is spawned.
    """
    if not _PENDING_FILE.exists():
        return
    try:
        data = json.loads(_PENDING_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        _clear_pending()
        return

    camera_configs = data.get("camera_configs", [])
    duration       = data.get("duration")
    session_name   = data.get("session_name", "session")
    started_at_str = data.get("started_at", "")

    if not camera_configs:
        _clear_pending()
        return

    remaining: int | None = None
    if duration is not None:
        try:
            started_at = datetime.fromisoformat(started_at_str)
            elapsed_s  = (datetime.now() - started_at).total_seconds()
            remaining  = int(duration - elapsed_s)
            if remaining < 60:
                _clear_pending()
                return
        except (ValueError, TypeError):
            _clear_pending()
            return

    # Wait for cameras and storage to finish initialising after boot.
    await asyncio.sleep(15)

    try:
        await start_session(camera_configs, remaining, session_name, scheduled_time=None)
    except Exception as exc:
        # Log to stderr (visible in journalctl) but don't delete the pending
        # file — the next reboot will try again.
        import sys
        print(f"[homecagev3] auto-resume failed: {exc}", file=sys.stderr, flush=True)
