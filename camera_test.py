#!/usr/bin/env python3
"""
camera_test.py — Interactive UVC camera discovery and recording test tool.
Target: Raspberry Pi 5, Raspberry Pi OS Bookworm

Usage: python3 camera_test.py

Discovers all UVC cameras, enumerates their formats, lets you run a
parameterised test recording, measures CPU and file size, and logs
results to test_results.md on the USB drive.

Requirements: psutil  →  pip3 install psutil
"""

import os
import re
import sys
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    import psutil
except ImportError:
    sys.exit("psutil not found. Install with: pip3 install psutil")

# ── Paths ──────────────────────────────────────────────────────────────────────

OUTPUT_DIR  = Path("/media/ab-ivnc/hc2_data1/test_recordings")
RESULTS_MD  = OUTPUT_DIR / "test_results.md"

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class FormatMode:
    fmt: str          # e.g. MJPG, YUYV, H264
    width: int
    height: int
    fps: float

@dataclass
class Camera:
    name: str
    capture_node: str           # primary MJPEG/YUYV node
    h264_node: str | None       # passthrough H264 node if present
    usb_id: str                 # vendor:product
    modes: list[FormatMode] = field(default_factory=list)

# ── Discovery ──────────────────────────────────────────────────────────────────

def _usb_product(node: str) -> str | None:
    """Return the PRODUCT= field from sysfs for a /dev/videoN node, or None."""
    name = Path(node).name  # videoN
    uevent = Path(f"/sys/class/video4linux/{name}/device/uevent")
    try:
        for line in uevent.read_text().splitlines():
            if line.startswith("PRODUCT="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _driver(node: str) -> str | None:
    name = Path(node).name
    uevent = Path(f"/sys/class/video4linux/{name}/device/uevent")
    try:
        for line in uevent.read_text().splitlines():
            if line.startswith("DRIVER="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _parse_formats(node: str) -> list[FormatMode]:
    """Run v4l2-ctl --list-formats-ext and return a list of FormatModes."""
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--list-formats-ext", "--device", node],
            stderr=subprocess.DEVNULL, text=True
        )
    except subprocess.CalledProcessError:
        return []

    modes: list[FormatMode] = []
    current_fmt = None
    current_w = current_h = None

    for line in out.splitlines():
        # Format line: [0]: 'MJPG' (Motion-JPEG, compressed)
        m = re.search(r"'(\w+)'", line)
        if m and "Size:" not in line and "Interval:" not in line:
            current_fmt = m.group(1)
            current_w = current_h = None
            continue
        # Size line: Size: Discrete 1920x1080
        m = re.search(r"Size:\s+\S+\s+(\d+)x(\d+)", line)
        if m:
            current_w, current_h = int(m.group(1)), int(m.group(2))
            continue
        # Interval line: Interval: Discrete 0.033s (30.000 fps)
        m = re.search(r"\((\d+\.\d+)\s+fps\)", line)
        if m and current_fmt and current_w:
            modes.append(FormatMode(current_fmt, current_w, current_h, float(m.group(1))))

    return modes


def discover_cameras() -> list[Camera]:
    """
    Find all UVC cameras via sysfs. Groups nodes by USB product ID.
    Returns one Camera per physical device.
    """
    # All video nodes with uvcvideo driver
    uvc_nodes: dict[str, str] = {}  # node → usb product
    for node_path in sorted(Path("/dev").glob("video*"),
                            key=lambda p: int(p.name[5:])):
        node = str(node_path)
        if _driver(node) == "uvcvideo":
            product = _usb_product(node)
            if product:
                uvc_nodes[node] = product

    if not uvc_nodes:
        return []

    # Group by USB product → each product is one physical camera
    product_to_nodes: dict[str, list[str]] = {}
    for node, product in uvc_nodes.items():
        product_to_nodes.setdefault(product, []).append(node)

    cameras: list[Camera] = []
    for product, nodes in product_to_nodes.items():
        nodes = sorted(nodes, key=lambda p: int(Path(p).name[5:]))

        # Get camera name from v4l2-ctl for the first node
        name = Path(nodes[0]).name  # fallback
        try:
            out = subprocess.check_output(
                ["v4l2-ctl", "--device", nodes[0], "--info"],
                stderr=subprocess.DEVNULL, text=True
            )
            for line in out.splitlines():
                if "Card type" in line:
                    name = line.split(":", 1)[1].strip()
                    break
        except subprocess.CalledProcessError:
            pass

        # Determine capture node (MJPEG/YUYV) and H264 passthrough node
        capture_node = None
        h264_node = None
        for node in nodes:
            modes = _parse_formats(node)
            fmts = {m.fmt for m in modes}
            if "H264" in fmts and "MJPG" not in fmts and "YUYV" not in fmts:
                h264_node = node
            elif "MJPG" in fmts or "YUYV" in fmts:
                if capture_node is None:
                    capture_node = node

        if capture_node is None:
            continue  # no usable capture node

        # Parse formats for the capture node
        modes = _parse_formats(capture_node)
        # Also add H264 passthrough modes if node exists
        if h264_node:
            modes += _parse_formats(h264_node)

        usb_id = "/".join(product.split("/")[:2])  # vendor/product
        cameras.append(Camera(name, capture_node, h264_node, usb_id, modes))

    return cameras

# ── Display ────────────────────────────────────────────────────────────────────

def print_camera_summary(cameras: list[Camera]):
    print("\n" + "═" * 60)
    print("  DISCOVERED CAMERAS")
    print("═" * 60)
    for i, cam in enumerate(cameras):
        h264_flag = f"  ✓ H264 passthrough ({cam.h264_node})" if cam.h264_node else ""
        print(f"\n  [{i+1}] {cam.name}")
        print(f"       Node : {cam.capture_node}   USB: {cam.usb_id}{h264_flag}")
        print(f"       {'Format':<8}  {'Resolution':<14}  {'FPS'}")
        print(f"       {'-'*6}  {'-'*12}  {'-'*6}")
        for m in sorted(cam.modes, key=lambda x: (x.fmt, -x.width, -x.fps)):
            print(f"       {m.fmt:<8}  {m.width}x{m.height:<7}   {m.fps:.0f}")
    print()


def _pick(prompt: str, options: list, default: int = 0) -> int:
    """Show a numbered menu and return the chosen index."""
    for i, opt in enumerate(options):
        marker = " ←" if i == default else ""
        print(f"  [{i+1}] {opt}{marker}")
    while True:
        raw = input(f"{prompt} [default={default+1}]: ").strip()
        if raw == "":
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print("  Invalid choice, try again.")


def _ask(prompt: str, default: str) -> str:
    raw = input(f"{prompt} [default={default}]: ").strip()
    return raw if raw else default

# ── Build ffmpeg command ───────────────────────────────────────────────────────

@dataclass
class RecordParams:
    camera: Camera
    node: str           # actual capture node (may be h264_node for passthrough)
    input_fmt: str      # mjpeg / h264
    width: int
    height: int
    fps: int
    encoder: str        # libx264 / copy
    preset: str | None
    crf: str | None
    denoise: bool
    duration: int
    output_path: Path


def build_ffmpeg_cmd(p: RecordParams) -> list[str]:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-f", "v4l2",
        "-input_format", p.input_fmt,
        "-video_size", f"{p.width}x{p.height}",
        "-framerate", str(p.fps),
        "-thread_queue_size", "512",
        "-use_wallclock_as_timestamps", "1",
        "-i", p.node,
    ]
    if p.denoise and p.encoder != "copy":
        cmd += ["-vf", "hqdn3d=2:2:3:3"]
    if p.encoder == "copy":
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-preset", p.preset, "-crf", p.crf]
    cmd += ["-t", str(p.duration), str(p.output_path)]
    return cmd

# ── CPU monitor ────────────────────────────────────────────────────────────────

class CpuMonitor:
    """Samples CPU % of a set of PIDs every 0.5s in a background thread."""

    def __init__(self):
        self.samples: list[float] = []
        self._pids: list[int] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self, pids: list[int]):
        self._pids = pids
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()

    def _run(self):
        procs = []
        for pid in self._pids:
            try:
                procs.append(psutil.Process(pid))
                procs[-1].cpu_percent()  # first call initialises the counter
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

    @property
    def avg(self) -> float:
        return sum(self.samples) / len(self.samples) if self.samples else 0.0

    @property
    def peak(self) -> float:
        return max(self.samples) if self.samples else 0.0

# ── Record and measure ─────────────────────────────────────────────────────────

FRAME_ERR_RE = re.compile(
    r"(EOI missing|No JPEG data|Invalid data|Dropped frame|"
    r"Buffer underrun|DTS .* out of order)",
    re.IGNORECASE,
)


def run_recording(params: list[RecordParams]) -> list[dict]:
    """
    Run one or more recordings simultaneously.
    Returns a list of result dicts, one per camera.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    progress_files = [
        tempfile.NamedTemporaryFile(prefix="cam_progress_", suffix=".txt", delete=False)
        for _ in params
    ]
    for pf in progress_files:
        pf.close()

    # Build commands — inject -progress before output path
    processes = []
    stderr_threads = []
    frame_errors = [0] * len(params)
    stderr_lines: list[list[str]] = [[] for _ in params]

    def read_stderr(proc, idx):
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                stderr_lines[idx].append(line)
                if FRAME_ERR_RE.search(line):
                    frame_errors[idx] += 1

    print()
    for i, p in enumerate(params):
        cmd = build_ffmpeg_cmd(p)
        # Insert -progress before the output path (last element)
        cmd = cmd[:-1] + ["-progress", progress_files[i].name, cmd[-1]]
        print(f"  Starting: {p.camera.name}  →  {p.output_path.name}")
        print(f"  Command : {' '.join(cmd)}\n")
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
        processes.append(proc)
        t = threading.Thread(target=read_stderr, args=(proc, i), daemon=True)
        t.start()
        stderr_threads.append(t)

    # Start CPU monitor across all ffmpeg PIDs
    monitor = CpuMonitor()
    monitor.start([p.pid for p in processes])

    # Progress bar
    duration = max(p.duration for p in params)
    t_start = time.monotonic()
    print(f"  Recording", end="", flush=True)
    while any(p.poll() is None for p in processes):
        elapsed = time.monotonic() - t_start
        pct = min(elapsed / duration, 1.0)
        bar = "█" * int(pct * 30) + "░" * (30 - int(pct * 30))
        print(f"\r  Recording  [{bar}]  {elapsed:.0f}/{duration}s", end="", flush=True)
        time.sleep(0.5)
    print(f"\r  Recording  [{'█'*30}]  {duration}/{duration}s  ✓")

    for p in processes:
        p.wait()
    for t in stderr_threads:
        t.join()
    monitor.stop()

    # Parse final progress stats
    results = []
    for i, p in enumerate(params):
        prog: dict[str, str] = {}
        try:
            content = Path(progress_files[i].name).read_text()
            for line in content.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    prog[k.strip()] = v.strip()
        except OSError:
            pass
        finally:
            try:
                os.unlink(progress_files[i].name)
            except OSError:
                pass

        total_frames = int(prog.get("frame", 0))
        duration_us  = int(prog.get("out_time_us", 0))
        actual_fps   = (total_frames / (duration_us / 1e6)) if duration_us > 0 else 0.0
        file_size    = p.output_path.stat().st_size if p.output_path.exists() else 0

        results.append({
            "params":        p,
            "file_size":     file_size,
            "total_frames":  total_frames,
            "actual_fps":    actual_fps,
            "frame_errors":  frame_errors[i],
            "stderr_lines":  stderr_lines[i],
            "cpu_avg":       monitor.avg / len(params),
            "cpu_peak":      monitor.peak / len(params),
        })

    return results

# ── Report ─────────────────────────────────────────────────────────────────────

def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def print_results(results: list[dict]):
    print("\n" + "═" * 60)
    print("  RESULTS")
    print("═" * 60)
    for r in results:
        p: RecordParams = r["params"]
        size        = r["file_size"]
        duration    = p.duration
        per_hour    = int(size * 3600 / duration) if duration else 0
        per_14hr    = per_hour * 14

        print(f"\n  Camera     : {p.camera.name}  ({p.node})")
        print(f"  Parameters : {p.input_fmt.upper()} {p.width}x{p.height} @ {p.fps}fps"
              f"  encoder={p.encoder}"
              + (f"  preset={p.preset}  CRF={p.crf}" if p.encoder != "copy" else "")
              + (f"  denoise=hqdn3d" if p.denoise else ""))
        print(f"  ┌─────────────────────────────────────────┐")
        print(f"  │ File size          {fmt_bytes(size):<22}│")
        print(f"  │ Projected / hour   {fmt_bytes(per_hour):<22}│")
        print(f"  │ Projected / 14hr   {fmt_bytes(per_14hr):<22}│")
        print(f"  │ CPU avg            {r['cpu_avg']:.1f}%{'':<20}│")
        print(f"  │ CPU peak           {r['cpu_peak']:.1f}%{'':<20}│")
        print(f"  │ Frame errors       {r['frame_errors']:<22}│")
        print(f"  │ Requested FPS      {p.fps:<22}│")
        print(f"  │ Actual FPS         {r['actual_fps']:.2f}{'':<20}│")
        print(f"  └─────────────────────────────────────────┘")

        if r["stderr_lines"]:
            print(f"\n  ffmpeg warnings ({len(r['stderr_lines'])} lines):")
            for line in r["stderr_lines"][:10]:
                print(f"    {line}")
            if len(r["stderr_lines"]) > 10:
                print(f"    ... and {len(r['stderr_lines'])-10} more")
    print()

# ── Markdown logging ───────────────────────────────────────────────────────────

def append_results_md(results: list[dict]):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"\n### Test — {timestamp}\n"]

    for r in results:
        p: RecordParams = r["params"]
        size     = r["file_size"]
        duration = p.duration
        per_hour = int(size * 3600 / duration) if duration else 0
        per_14hr = per_hour * 14

        lines += [
            f"**Camera:** {p.camera.name} (`{p.node}`)\n",
            "",
            "| Parameter | Value |",
            "|-----------|-------|",
            f"| Input format | {p.input_fmt.upper()} |",
            f"| Resolution | {p.width}×{p.height} |",
            f"| Framerate | {p.fps} fps |",
            f"| Encoder | {p.encoder} |",
        ]
        if p.encoder != "copy":
            lines += [
                f"| Preset | {p.preset} |",
                f"| CRF | {p.crf} |",
                f"| Denoise | {'hqdn3d=2:2:3:3' if p.denoise else 'none'} |",
            ]
        lines += [
            f"| Duration | {p.duration}s |",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| File size | {fmt_bytes(size)} |",
            f"| Projected / hour | {fmt_bytes(per_hour)} |",
            f"| Projected / 14hr | {fmt_bytes(per_14hr)} |",
            f"| CPU avg | {r['cpu_avg']:.1f}% |",
            f"| CPU peak | {r['cpu_peak']:.1f}% |",
            f"| Frame errors | {r['frame_errors']} |",
            f"| Requested FPS | {p.fps} |",
            f"| Actual FPS | {r['actual_fps']:.2f} |",
            "",
        ]

    with open(RESULTS_MD, "a") as f:
        f.write("\n".join(lines) + "\n")

    print(f"  Results appended to: {RESULTS_MD}\n")

# ── Interactive setup ──────────────────────────────────────────────────────────

def prompt_test_params(cam: Camera) -> RecordParams:
    print(f"\n  Configuring test for: {cam.name}")
    print("  " + "─" * 40)

    # Input format
    avail_fmts = sorted({m.fmt for m in cam.modes})
    if cam.h264_node:
        avail_fmts = [f for f in avail_fmts if f != "H264"] + ["H264 (passthrough)"]
    fmt_idx = _pick("  Input format", avail_fmts)
    fmt_choice = avail_fmts[fmt_idx]
    passthrough = fmt_choice == "H264 (passthrough)"
    input_fmt = "h264" if passthrough else fmt_choice.lower()
    node = cam.h264_node if passthrough else cam.capture_node

    # Resolution
    if passthrough:
        res_modes = [m for m in cam.modes if m.fmt == "H264"]
    else:
        res_modes = [m for m in cam.modes if m.fmt == fmt_choice]
    unique_res = sorted({(m.width, m.height) for m in res_modes}, reverse=True)
    res_labels = [f"{w}x{h}" for w, h in unique_res]
    res_idx = _pick("  Resolution", res_labels)
    width, height = unique_res[res_idx]

    # Framerate
    fps_opts = sorted({m.fps for m in res_modes
                       if m.width == width and m.height == height}, reverse=True)
    fps_labels = [f"{f:.0f} fps" for f in fps_opts]
    # Default to 10fps if available, else first option
    default_fps_idx = next((i for i, f in enumerate(fps_opts) if f == 10.0), 0)
    fps_idx = _pick("  Framerate", fps_labels, default=default_fps_idx)
    fps = int(fps_opts[fps_idx])

    # Encoder
    if passthrough:
        encoder = "copy"
        preset = None
        crf = None
    else:
        enc_idx = _pick("  Encoder", ["libx264", "copy (H264 passthrough — only if H264 node)"], default=0)
        encoder = "copy" if enc_idx == 1 else "libx264"
        if encoder == "libx264":
            preset_opts = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"]
            preset_idx = _pick("  Preset", preset_opts, default=0)
            preset = preset_opts[preset_idx]
            crf = _ask("  CRF (18=high quality, 23=default, 28=smaller)", "23")
        else:
            preset = None
            crf = None

    # Denoise
    denoise = False
    if encoder != "copy":
        d = _ask("  Apply hqdn3d denoise filter? (y/n)", "n")
        denoise = d.lower().startswith("y")

    # Duration
    dur = int(_ask("  Duration (seconds)", "30"))

    # Output path
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^\w]", "_", cam.name.split(":")[0].strip())
    output_path = OUTPUT_DIR / f"{safe_name}_{ts}.mp4"

    return RecordParams(
        camera=cam, node=node,
        input_fmt=input_fmt, width=width, height=height, fps=fps,
        encoder=encoder, preset=preset, crf=crf,
        denoise=denoise, duration=dur, output_path=output_path,
    )

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 60)
    print("  CAMERA TEST TOOL  —  Raspberry Pi 5")
    print("═" * 60)
    print("  Discovering cameras...")

    cameras = discover_cameras()
    if not cameras:
        sys.exit("  No UVC cameras found. Check connections and driver.")

    print_camera_summary(cameras)

    # Camera selection
    cam_labels = [f"{c.name}  ({c.capture_node})" for c in cameras]
    cam_labels.append("Both cameras simultaneously")
    print("  Select camera(s) to test:")
    cam_idx = _pick("  Choice", cam_labels)

    if cam_idx < len(cameras):
        selected = [cameras[cam_idx]]
    else:
        selected = cameras  # both

    # Per-camera parameter prompts
    all_params: list[RecordParams] = []
    for cam in selected:
        all_params.append(prompt_test_params(cam))

    # Confirm
    print("\n  Ready to record. Press Enter to start or Ctrl+C to cancel.")
    try:
        input()
    except KeyboardInterrupt:
        print("\n  Cancelled.")
        return

    # Record
    results = run_recording(all_params)

    # Report
    print_results(results)

    # Log to markdown
    save = _ask("  Save results to test_results.md? (y/n)", "y")
    if save.lower().startswith("y"):
        append_results_md(results)


if __name__ == "__main__":
    main()
