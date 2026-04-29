"""
Test recording job management.
Each job runs ffmpeg as an async subprocess and samples CPU with psutil.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import psutil

from .config import settings
from .flir import FLIR_HEIGHT, FLIR_WIDTH, FlirCapture, is_flir, serial_from_node
from .storage import storage_manager
from .utils import FMT_MAP, fmt_bytes

FRAME_ERR_RE = re.compile(
    r"(EOI missing|No JPEG data|Invalid data|Dropped frame|Buffer underrun)",
    re.IGNORECASE,
)


# ── Job state ──────────────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE    = "done"
    ERROR   = "error"


@dataclass
class RecordResult:
    file_size_bytes: int
    per_hour_bytes: int
    per_14hr_bytes: int
    total_frames: int
    actual_fps: float
    requested_fps: int
    frame_errors: int
    cpu_avg: float
    cpu_peak: float
    temp_avg: float | None
    temp_peak: float | None
    output_path: str


@dataclass
class RecordJob:
    id: str
    camera_name: str
    params: dict[str, Any]
    duration: int
    status: JobStatus = JobStatus.PENDING
    elapsed: float = 0.0
    result: RecordResult | None = None
    error: str | None = None
    stderr_tail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "id":          self.id,
            "camera_name": self.camera_name,
            "params":      self.params,
            "duration":    self.duration,
            "status":      self.status,
            "elapsed":     round(self.elapsed, 1),
            "error":       self.error,
            "stderr_tail": self.stderr_tail[-10:],
            "result":      None,
        }
        if self.result:
            r = self.result
            d["result"] = {
                "file_size":     fmt_bytes(r.file_size_bytes),
                "per_hour":      fmt_bytes(r.per_hour_bytes),
                "per_14hr":      fmt_bytes(r.per_14hr_bytes),
                "total_frames":  r.total_frames,
                "actual_fps":    round(r.actual_fps, 2),
                "requested_fps": r.requested_fps,
                "frame_errors":  r.frame_errors,
                "cpu_avg":       round(r.cpu_avg, 1),
                "cpu_peak":      round(r.cpu_peak, 1),
                "temp_avg":      r.temp_avg,
                "temp_peak":     r.temp_peak,
                "output_path":   r.output_path,
            }
        return d


# ── Job manager ────────────────────────────────────────────────────────────────

class JobManager:
    def __init__(self):
        self._jobs: dict[str, RecordJob] = {}

    def create(self, camera_name: str, params: dict, duration: int) -> RecordJob:
        job = RecordJob(
            id=str(uuid.uuid4())[:8],
            camera_name=camera_name,
            params=params,
            duration=duration,
        )
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> RecordJob | None:
        return self._jobs.get(job_id)

    def all(self) -> list[RecordJob]:
        return sorted(self._jobs.values(), key=lambda j: j.id, reverse=True)


job_manager = JobManager()


# ── CPU monitor ────────────────────────────────────────────────────────────────

_TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")


def _read_temp_c() -> float | None:
    """Read SoC temperature in °C from the kernel thermal interface."""
    try:
        return int(_TEMP_PATH.read_text().strip()) / 1000.0
    except OSError:
        return None


class _CpuMonitor:
    def __init__(self):
        self.samples:      list[float] = []
        self.temp_samples: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._pids: list[int] = []

    def start(self, pids: list[int]):
        self._pids = pids
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)

    def _run(self):
        procs = []
        for pid in self._pids:
            try:
                p = psutil.Process(pid)
                p.cpu_percent()   # initialise counter
                procs.append(p)
            except psutil.NoSuchProcess:
                pass
        time.sleep(0.5)
        while not self._stop.wait(0.5):
            total = 0.0
            for p in procs:
                try:
                    total += p.cpu_percent()
                except psutil.NoSuchProcess:
                    pass
            if total > 0:
                self.samples.append(total)
            t = _read_temp_c()
            if t is not None:
                self.temp_samples.append(t)

    @property
    def avg(self) -> float:
        return sum(self.samples) / len(self.samples) if self.samples else 0.0

    @property
    def peak(self) -> float:
        return max(self.samples) if self.samples else 0.0

    @property
    def temp_avg(self) -> float | None:
        return round(sum(self.temp_samples) / len(self.temp_samples), 1) if self.temp_samples else None

    @property
    def temp_peak(self) -> float | None:
        return round(max(self.temp_samples), 1) if self.temp_samples else None


# ── ffmpeg command builder ─────────────────────────────────────────────────────

def _build_cmd(params: dict, progress_path: str, output_path: str) -> list[str]:
    input_fmt = FMT_MAP.get(params["input_fmt"].lower(), params["input_fmt"].lower())
    is_copy = params.get("encoder") == "copy"

    # H264 UVC streams have inherently missing/zero timestamps that the mp4
    # muxer warns about but corrects. Use loglevel=error for copy to avoid
    # filling the UI with unavoidable muxer warnings; real errors still show.
    loglevel = "error" if is_copy else "warning"
    cmd = ["ffmpeg", "-y", "-loglevel", loglevel, "-f", "v4l2",
           "-input_format", input_fmt,
           "-video_size",   f"{params['width']}x{params['height']}",
           "-thread_queue_size", "512",
           "-use_wallclock_as_timestamps", "1",
    ]
    if is_copy:
        cmd += ["-fflags", "+genpts"]
    else:
        cmd += ["-framerate", str(params["fps"])]
    cmd += ["-i", params["node"]]

    if not is_copy:
        filters = [f"fps={params['fps']}"]
        if params.get("hflip"):
            filters.append("hflip")
        if params.get("vflip"):
            filters.append("vflip")
        if params.get("denoise"):
            filters.append("hqdn3d=2:2:3:3")
        if params.get("timestamp_overlay"):
            filters.append(
                r"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
                r":text='%{localtime\:%D %T}'"
                r":x=w-tw-10:y=h-th-10:fontsize=36"
                r":fontcolor=white@0.9:box=1:boxcolor=black@0.4:boxborderw=4"
            )
        # 5-second title card showing recording parameters
        enc_label   = "copy" if is_copy else f"libx264 CRF{params.get('crf', 23)}"
        denoise_str = "  denoise" if params.get("denoise") else ""
        title_text  = (f"{params['width']}x{params['height']}  "
                       f"{params['input_fmt'].upper()}  "
                       f"{enc_label}  "
                       f"{params['fps']}fps{denoise_str}")
        filters.append(
            f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            f":text='{title_text}'"
            f":x=(w-tw)/2:y=(h-th)/2"
            f":fontsize=52:fontcolor=white@0.95:box=1:boxcolor=black@0.7:boxborderw=14"
            f":enable='lt(t,5)'"
        )
        cmd += ["-vf", ",".join(filters)]
    if is_copy:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", "libx264",
                "-preset", params.get("preset", "ultrafast"),
                "-crf",    str(params.get("crf", 23))]
    cmd += [
        "-t",        str(params["duration"]),
        "-progress", progress_path,
        output_path,
    ]
    return cmd


# ── ffmpeg command builder for FLIR (rawvideo pipe:0) ─────────────────────────

def _build_flir_cmd(params: dict, progress_path: str, output_path: str) -> list[str]:
    w   = int(params.get("width",  FLIR_WIDTH))
    h   = int(params.get("height", FLIR_HEIGHT))
    fps = int(params.get("fps", 10))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-f", "rawvideo",
        "-pix_fmt", "gray",
        "-color_range", "2",
        "-s", f"{w}x{h}",
        "-r", str(fps),
        "-thread_queue_size", "512",
        "-i", "pipe:0",
    ]
    filters = ["format=yuv420p", f"fps={fps}"]
    if params.get("hflip"):
        filters.append("hflip")
    if params.get("vflip"):
        filters.append("vflip")
    if params.get("denoise"):
        filters.append("hqdn3d=2:2:3:3")
    if params.get("timestamp_overlay"):
        filters.append(
            r"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            r":text='%{localtime\:%D %T}'"
            r":x=w-tw-10:y=h-th-10:fontsize=36"
            r":fontcolor=white@0.9:box=1:boxcolor=black@0.4:boxborderw=4"
        )
    enc_label  = f"libx264 CRF{params.get('crf', 23)}"
    title_text = (f"{w}x{h}  MONO8  {enc_label}  {fps}fps")
    filters.append(
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        f":text='{title_text}'"
        f":x=(w-tw)/2:y=(h-th)/2"
        f":fontsize=52:fontcolor=white@0.95:box=1:boxcolor=black@0.7:boxborderw=14"
        f":enable='lt(t,5)'"
    )
    cmd += ["-vf", ",".join(filters)]
    cmd += [
        "-c:v", "libx264",
        "-preset", params.get("preset", "ultrafast"),
        "-crf",    str(params.get("crf", 23)),
        "-t",        str(params["duration"]),
        "-progress", progress_path,
        output_path,
    ]
    return cmd


# ── Async recording task ───────────────────────────────────────────────────────

async def run_job(job: RecordJob):
    test_dir = storage_manager.test_recordings_dir
    test_dir.mkdir(parents=True, exist_ok=True)

    _is_flir = is_flir(job.params.get("node", ""))

    # Descriptive filename: Camera_WxH_FMT_encoder_jobid.mp4
    safe      = re.sub(r"[^\w]", "_", job.camera_name.split(":")[0].strip())
    w, h      = job.params.get("width", 0), job.params.get("height", 0)
    fmt       = "MONO8" if _is_flir else re.sub(r"[^\w]", "", job.params.get("input_fmt", "").upper())
    enc       = job.params.get("encoder", "libx264")
    enc_tag   = "copy" if enc == "copy" else f"libx264_crf{job.params.get('crf', 23)}"
    output_path = test_dir / f"{safe}_{w}x{h}_{fmt}_{enc_tag}_{job.id}.mp4"

    # Temp file for -progress output
    pf = tempfile.NamedTemporaryFile(
        prefix="hcv3_progress_", suffix=".txt", delete=False
    )
    pf.close()
    progress_path = pf.name

    flir_capture: FlirCapture | None = None
    if _is_flir:
        serial = serial_from_node(job.params["node"])
        flir_capture = FlirCapture(serial, w or FLIR_WIDTH, h or FLIR_HEIGHT,
                                   float(job.params.get("fps", 10)))
        flir_capture.start()
        loop = asyncio.get_event_loop()
        ready = await loop.run_in_executor(None, flir_capture.wait_ready, 8.0)
        if not ready:
            flir_capture.stop()
            job.status = JobStatus.ERROR
            job.error  = "FLIR camera did not initialise within 8 seconds"
            return
        # Update params with camera-reported actual resolution
        job.params["width"]  = flir_capture.actual_width
        job.params["height"] = flir_capture.actual_height
        w, h = flir_capture.actual_width, flir_capture.actual_height
        cmd = _build_flir_cmd(job.params, progress_path, str(output_path))
    else:
        cmd = _build_cmd(job.params, progress_path, str(output_path))

    job.status = JobStatus.RUNNING
    t_start = time.monotonic()
    frame_errors = 0
    proc = None
    monitor = _CpuMonitor()

    try:
        if _is_flir:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        monitor.start([proc.pid])

        async def _read_stderr():
            nonlocal frame_errors
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    job.stderr_tail.append(line)
                    if FRAME_ERR_RE.search(line):
                        frame_errors += 1

        async def _tick():
            while proc.returncode is None:
                job.elapsed = time.monotonic() - t_start
                await asyncio.sleep(0.5)

        async def _feed_stdin():
            assert flir_capture is not None
            loop2 = asyncio.get_event_loop()
            try:
                while proc.returncode is None:
                    frame = await loop2.run_in_executor(None, flir_capture.get, 2.0)
                    if frame is None:
                        break
                    if proc.stdin.is_closing():
                        break
                    proc.stdin.write(frame)
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
                except Exception:
                    pass

        if _is_flir:
            await asyncio.gather(_read_stderr(), _tick(), _feed_stdin())
        else:
            await asyncio.gather(_read_stderr(), _tick())
        await proc.wait()
        monitor.stop()

        # Parse progress file
        total_frames = 0
        duration_us  = 0
        try:
            for line in Path(progress_path).read_text().splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.strip()
                    if k == "frame":
                        total_frames = int(v)
                    elif k == "out_time_us":
                        duration_us = int(v)
        except OSError:
            pass

        actual_fps = (total_frames / (duration_us / 1e6)) if duration_us > 0 else 0.0
        file_size  = output_path.stat().st_size if output_path.exists() else 0
        dur        = job.params["duration"]
        per_hour   = int(file_size * 3600 / dur) if dur else 0

        job.result = RecordResult(
            file_size_bytes=file_size,
            per_hour_bytes=per_hour,
            per_14hr_bytes=per_hour * 14,
            total_frames=total_frames,
            actual_fps=actual_fps,
            requested_fps=job.params["fps"],
            frame_errors=frame_errors,
            cpu_avg=monitor.avg,
            cpu_peak=monitor.peak,
            temp_avg=monitor.temp_avg,
            temp_peak=monitor.temp_peak,
            output_path=str(output_path),
        )
        job.status = JobStatus.DONE

        # Persist job metadata to JSONL log
        try:
            log_entry = {
                "timestamp":      datetime.now().isoformat(timespec="seconds"),
                "job_id":         job.id,
                "camera":         job.camera_name,
                "file":           output_path.name,
                "width":          w,
                "height":         h,
                "input_fmt":      "MONO8" if _is_flir else job.params.get("input_fmt", ""),
                "encoder":        enc,
                "crf":            job.params.get("crf"),
                "fps":            job.params.get("fps"),
                "denoise":        job.params.get("denoise", False),
                "duration_s":     job.params.get("duration"),
                "file_size_bytes": file_size,
                "file_size":      fmt_bytes(file_size),
                "per_hour":       fmt_bytes(per_hour),
                "actual_fps":     round(actual_fps, 2),
                "frame_errors":   frame_errors,
                "cpu_avg":        round(monitor.avg, 1),
                "cpu_peak":       round(monitor.peak, 1),
                "temp_avg_c":     monitor.temp_avg,
                "temp_peak_c":    monitor.temp_peak,
            }
            log_path = storage_manager.test_recordings_dir / "job_log.jsonl"
            with open(log_path, "a") as lf:
                lf.write(json.dumps(log_entry) + "\n")
        except OSError:
            pass

    except Exception as exc:
        monitor.stop()
        if proc and proc.returncode is None:
            proc.kill()
        if flir_capture:
            flir_capture.stop()
        job.status = JobStatus.ERROR
        job.error = str(exc)

    finally:
        if flir_capture:
            flir_capture.stop()
        job.elapsed = time.monotonic() - t_start
        try:
            os.unlink(progress_path)
        except OSError:
            pass
