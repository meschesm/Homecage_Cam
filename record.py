#!/usr/bin/env python3
"""
Multi-camera recorder for Raspberry Pi 5.
Records from multiple UVC USB cameras simultaneously via ffmpeg subprocesses.

Usage:
    python3 record.py                          # record until Ctrl+C
    python3 record.py --duration 300           # record for 5 minutes
    python3 record.py --output /path/to/dir    # custom output directory
"""

import argparse
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

CAMERAS = [
    {
        "name": "arducam",
        "device": "/dev/video0",
        "input_format": "mjpeg",
        "resolution": "1920x1080",
        "framerate": 10,
    },
    {
        "name": "hd_usb",
        "device": "/dev/video5",
        "input_format": "mjpeg",
        "resolution": "1920x1080",
        "framerate": 10,
    },
]

OUTPUT_DIR         = Path("/media/ab-ivnc/hc2_data1/recordings")
SEGMENT_SECONDS    = 3600   # Split into 1-hour files
CRF                = "23"   # libx264 quality (lower = better quality, larger files)
RESTART_DELAY      = 5      # Seconds before restarting a failed camera
MAX_RESTARTS       = 10     # Give up after this many consecutive failures
STATS_INTERVAL     = 60     # Seconds between progress log entries
DISK_WARN_GB       = 10     # Warn when free space drops below this


# ── Helpers ────────────────────────────────────────────────────────────────────

def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1024**3


# ── Camera worker ──────────────────────────────────────────────────────────────

class CameraRecorder:
    def __init__(self, config: dict, output_dir: Path):
        self.cfg        = config
        self.name       = config["name"]
        self.output_dir = output_dir / self.name
        self.log        = logging.getLogger(self.name)
        self.process    = None
        self.restarts   = 0
        self._stop      = threading.Event()
        self.thread     = threading.Thread(target=self._run, name=self.name, daemon=True)

        # Stats (written by progress-reader thread, read by stats-logger thread)
        self._stats: dict = {}
        self._stats_lock  = threading.Lock()
        self._progress_path: Path | None = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        if self.process and self.process.poll() is None:
            self.log.info("Sending SIGTERM to ffmpeg (pid=%d)", self.process.pid)
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.log.warning("ffmpeg did not exit cleanly — killing")
                self.process.kill()

    def join(self):
        self.thread.join()

    # ── ffmpeg command builder ─────────────────────────────────────────────────

    def _ffmpeg_cmd(self, progress_path: str) -> list[str]:
        out_pattern = str(self.output_dir / "%Y%m%d_%H%M%S.mp4")
        return [
            "ffmpeg",
            "-loglevel",   "warning",   # errors/warnings to stderr
            # Input
            "-f",           "v4l2",
            "-input_format", self.cfg["input_format"],
            "-video_size",  self.cfg["resolution"],
            "-framerate",   str(self.cfg["framerate"]),
            "-thread_queue_size", "512",
            "-use_wallclock_as_timestamps", "1",
            "-i",           self.cfg["device"],
            # Denoise before encoding — removes IR sensor noise without losing behavioral detail
            "-vf",          "hqdn3d=2:2:3:3",
            # Encode
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", CRF,
            # Structured progress output
            "-progress",    progress_path,
            # Segmented output
            "-f",               "segment",
            "-segment_time",        str(SEGMENT_SECONDS),
            "-segment_atclocktime", "1",
            "-reset_timestamps",    "1",
            "-strftime",            "1",
            out_pattern,
        ]

    # ── Progress reader ────────────────────────────────────────────────────────

    def _read_progress(self, path: str):
        """Tail ffmpeg's -progress file and keep self._stats up to date."""
        try:
            with open(path) as f:
                buf: dict = {}
                for line in f:
                    if self._stop.is_set():
                        break
                    line = line.strip()
                    if "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    buf[key.strip()] = val.strip()
                    if key.strip() == "progress":  # end of one stats block
                        with self._stats_lock:
                            self._stats.update(buf)
                        buf = {}
        except Exception:
            pass  # process died, file closed — normal on shutdown

    # ── Stderr / warning parser ────────────────────────────────────────────────

    # Patterns that indicate frame-level problems worth counting
    _FRAME_WARN_RE = re.compile(
        r"(EOI missing|No JPEG data|Invalid data|Decoder overrun|"
        r"Dropped frame|Buffer underrun|DTS .* out of order)",
        re.IGNORECASE,
    )

    def _read_stderr(self, stderr):
        """Parse ffmpeg stderr, log warnings, and count frame errors."""
        frame_errors = 0
        for line in stderr:
            line = line.rstrip()
            if not line:
                continue
            if self._FRAME_WARN_RE.search(line):
                frame_errors += 1
                self.log.warning("frame issue #%d: %s", frame_errors, line)
            else:
                self.log.warning("ffmpeg: %s", line)
        if frame_errors:
            self.log.warning("Total frame-level issues this run: %d", frame_errors)

    # ── Periodic stats logger ──────────────────────────────────────────────────

    def _log_stats_periodically(self):
        while not self._stop.wait(STATS_INTERVAL):
            with self._stats_lock:
                s = dict(self._stats)
            if not s:
                continue
            frame       = s.get("frame", "?")
            fps         = s.get("fps", "?")
            drop        = s.get("drop_frames", "0")
            dup         = s.get("dup_frames", "0")
            total_size  = s.get("total_size")
            out_time    = s.get("out_time", "?")
            speed       = s.get("speed", "?")
            size_str    = fmt_bytes(int(total_size)) if total_size else "?"

            # Flag dropped frames prominently
            drop_int = int(drop) if drop.isdigit() else 0
            drop_str = f"{drop} ⚠" if drop_int > 0 else drop

            self.log.info(
                "frame=%-6s fps=%-5s drop=%-4s dup=%-4s size=%-8s time=%s speed=%s",
                frame, fps, drop_str, dup, size_str, out_time, speed,
            )

            # Check disk space
            try:
                gb = free_gb(self.output_dir)
                if gb < DISK_WARN_GB:
                    self.log.warning("Low disk space: %.1f GB remaining", gb)
            except OSError:
                pass

    # ── Main run loop ──────────────────────────────────────────────────────────

    def _run(self):
        stats_thread = threading.Thread(
            target=self._log_stats_periodically, name=f"{self.name}-stats", daemon=True
        )
        stats_thread.start()

        while not self._stop.is_set():
            self.log.info(
                "Starting | device=%s format=%s res=%s fps=%s encoder=libx264 ultrafast",
                self.cfg["device"], self.cfg["input_format"],
                self.cfg["resolution"], self.cfg["framerate"],
            )

            # Temp file for -progress output (ffmpeg writes, we tail-read)
            with tempfile.NamedTemporaryFile(
                prefix=f"{self.name}_progress_", suffix=".txt", delete=False
            ) as pf:
                progress_path = pf.name

            cmd = self._ffmpeg_cmd(progress_path)
            self.log.debug("Command: %s", " ".join(cmd))

            t_start = time.monotonic()
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                # Read progress file in a side thread
                prog_thread = threading.Thread(
                    target=self._read_progress, args=(progress_path,), daemon=True
                )
                prog_thread.start()

                # Read stderr in this thread (blocks until ffmpeg exits)
                self._read_stderr(self.process.stderr)
                self.process.wait()
                prog_thread.join(timeout=2)
            except Exception as exc:
                self.log.error("Failed to launch ffmpeg: %s", exc)
            finally:
                try:
                    os.unlink(progress_path)
                except OSError:
                    pass

            if self._stop.is_set():
                break

            elapsed = time.monotonic() - t_start
            rc = self.process.returncode if self.process else -1
            self.log.warning("ffmpeg exited after %.1fs (rc=%d)", elapsed, rc)

            self.restarts += 1
            if self.restarts >= MAX_RESTARTS:
                self.log.error("Reached max restarts (%d) — giving up", MAX_RESTARTS)
                break

            self.log.info(
                "Restarting in %ds... (%d/%d)", RESTART_DELAY, self.restarts, MAX_RESTARTS
            )
            self._stop.wait(RESTART_DELAY)

        self.log.info("Recorder stopped.")


# ── Disk space monitor ─────────────────────────────────────────────────────────

def _disk_monitor(output_dir: Path, stop: threading.Event):
    log = logging.getLogger("disk")
    while not stop.wait(300):  # check every 5 min
        try:
            usage = shutil.disk_usage(output_dir)
            free  = usage.free  / 1024**3
            total = usage.total / 1024**3
            used  = usage.used  / 1024**3
            log.info("Disk: %.1f GB used / %.1f GB total (%.1f GB free)", used, total, free)
            if free < DISK_WARN_GB:
                log.warning("LOW DISK SPACE: %.1f GB remaining — consider stopping soon", free)
        except OSError as e:
            log.error("Disk check failed: %s", e)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Multi-camera recorder")
    parser.add_argument("--output",   type=Path, default=OUTPUT_DIR,
                        help="Output directory (default: %(default)s)")
    parser.add_argument("--duration", type=int,  default=None,
                        help="Recording duration in seconds (default: run until Ctrl+C)")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)-14s] %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(args.output / "recorder.log"),
        ],
    )
    log = logging.getLogger("main")

    # ── Startup summary ───────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("Multi-camera recorder starting")
    log.info("Output dir  : %s", args.output)
    log.info("Cameras     : %d", len(CAMERAS))
    for cam in CAMERAS:
        log.info("  %-12s  %s  %s @ %sfps  [%s]",
                 cam["name"], cam["device"], cam["resolution"],
                 cam["framerate"], cam["input_format"])
    log.info("Segment size: %d min", SEGMENT_SECONDS // 60)
    log.info("Encoder     : libx264 ultrafast CRF=%s", CRF)
    if args.duration:
        log.info("Duration    : %d s", args.duration)
    else:
        log.info("Duration    : unlimited (Ctrl+C to stop)")
    try:
        gb = free_gb(args.output)
        log.info("Disk free   : %.1f GB", gb)
    except OSError:
        pass
    log.info("=" * 60)

    recorders = [CameraRecorder(cfg, args.output) for cfg in CAMERAS]
    stop_event = threading.Event()

    # Disk monitor
    disk_thread = threading.Thread(
        target=_disk_monitor, args=(args.output, stop_event), daemon=True, name="disk"
    )
    disk_thread.start()

    def shutdown(sig, _frame):
        log.info("Signal %d received — shutting down", sig)
        stop_event.set()
        for r in recorders:
            r.stop()

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    for r in recorders:
        r.start()
        log.info("Started recorder: %s", r.name)

    if args.duration:
        stop_event.wait(args.duration)
        if not stop_event.is_set():
            log.info("Duration reached — stopping")
            stop_event.set()
            for r in recorders:
                r.stop()

    for r in recorders:
        r.join()

    # ── Shutdown summary ──────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("Recording complete")
    for r in recorders:
        cam_dir = args.output / r.name
        files = sorted(cam_dir.glob("*.mp4")) if cam_dir.exists() else []
        total = sum(f.stat().st_size for f in files)
        log.info("  %-12s  %d file(s)  %s  restarts=%d",
                 r.name, len(files), fmt_bytes(total), r.restarts)
    try:
        gb = free_gb(args.output)
        log.info("Disk remaining: %.1f GB", gb)
    except OSError:
        pass
    log.info("=" * 60)


if __name__ == "__main__":
    main()
