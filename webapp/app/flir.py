"""
FLIR Blackfly S camera support via aravis GObject Introspection bindings.
Camera nodes use the form 'flir_{serial}', e.g. 'flir_18474893'.

aravis is only required on machines where a FLIR camera is attached.
On machines without aravis installed, discover_flir_cameras() returns []
and all other functions are no-ops / raise clearly.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import AsyncGenerator

from .camera import CameraInfo, FormatMode

log = logging.getLogger(__name__)

# ── aravis availability ────────────────────────────────────────────────────────

try:
    import gi
    gi.require_version("Aravis", "0.8")
    from gi.repository import Aravis as _Aravis
    _ARAVIS = True
except Exception:
    _Aravis = None   # type: ignore
    _ARAVIS = False

# ── Constants ──────────────────────────────────────────────────────────────────

FLIR_WIDTH  = 1440
FLIR_HEIGHT = 1080

FLIR_MODES: list[FormatMode] = [
    FormatMode(fmt="MONO8", width=FLIR_WIDTH, height=FLIR_HEIGHT, fps=5.0),
    FormatMode(fmt="MONO8", width=FLIR_WIDTH, height=FLIR_HEIGHT, fps=10.0),
    FormatMode(fmt="MONO8", width=FLIR_WIDTH, height=FLIR_HEIGHT, fps=15.0),
    FormatMode(fmt="MONO8", width=FLIR_WIDTH, height=FLIR_HEIGHT, fps=30.0),
]


# ── Node helpers ───────────────────────────────────────────────────────────────

# Maps node string → aravis device ID (populated during discovery)
_arv_id_map: dict[str, str] = {}


def is_flir(node: str) -> bool:
    return node.startswith("flir_")


def serial_from_node(node: str) -> str:
    """Extract the serial from a 'flir_{serial}' node string."""
    return node[5:]


def _arv_id_for_node(node: str) -> str:
    """Return the aravis device ID for a node, falling back to None (first camera)."""
    return _arv_id_map.get(node)


# ── Discovery ──────────────────────────────────────────────────────────────────

def discover_flir_cameras() -> list[CameraInfo]:
    """Return a CameraInfo for every aravis-visible camera. Safe on non-FLIR hosts."""
    if not _ARAVIS:
        return []
    try:
        _Aravis.update_device_list()
        n = _Aravis.get_n_devices()
    except Exception as exc:
        log.debug("aravis device scan failed: %s", exc)
        return []

    cameras: list[CameraInfo] = []
    for i in range(n):
        try:
            arv_id = _Aravis.get_device_id(i)
            cam    = _Aravis.Camera.new(arv_id)
            vendor = cam.get_vendor_name() or "FLIR"
            model  = cam.get_model_name()  or "Camera"
            try:
                serial = cam.get_string_feature_value("DeviceSerialNumber")
            except Exception:
                serial = arv_id.replace(" ", "_")
            del cam
            node = f"flir_{serial}"
            _arv_id_map[node] = arv_id   # save so FlirCapture can connect correctly
            cameras.append(CameraInfo(
                name=f"{vendor} {model}",
                capture_node=f"flir_{serial}",
                h264_node=None,
                usb_id="1e10/4000",
                modes=FLIR_MODES,
            ))
        except Exception as exc:
            log.warning("Failed to enumerate FLIR device %d: %s", i, exc)

    return cameras


# ── Frame capture thread ───────────────────────────────────────────────────────

class FlirCapture:
    """
    Captures mono8 frames from an aravis camera in a daemon thread.
    Frames are placed into a bounded queue; get() blocks until a frame arrives
    or the timeout expires.
    """

    def __init__(self, serial: str, width: int, height: int, fps: float):
        self._serial = serial
        self._fps    = fps
        self._q: queue.Queue[bytes | None] = queue.Queue(maxsize=8)
        self._stop   = threading.Event()
        self._ready  = threading.Event()
        self._thread: threading.Thread | None = None
        self.actual_width  = width
        self.actual_height = height

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"flir-cap-{self._serial}"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)   # unblock any waiting get()
        if self._thread:
            self._thread.join(timeout=5)

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def get(self, timeout: float = 2.0) -> bytes | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def _run(self) -> None:
        cam = stream = None
        try:
            arv_id = _arv_id_map.get(f"flir_{self._serial}")
            cam = _Aravis.Camera.new(arv_id)
            # Reset binning to 1x1 for full resolution
            try:
                cam.set_binning(1, 1)
            except Exception:
                pass
            cam.set_frame_rate(self._fps)
            cam.set_pixel_format(_Aravis.PIXEL_FORMAT_MONO_8)
            # Query actual frame size (camera may have its own defaults)
            x, y, w, h = cam.get_region()
            self.actual_width  = w
            self.actual_height = h
            log.info("FLIR actual resolution: %dx%d", w, h)
            self._ready.set()
            stream  = cam.create_stream(None, None)
            payload = cam.get_payload()
            for _ in range(8):
                stream.push_buffer(_Aravis.Buffer.new_allocate(payload))
            cam.start_acquisition()

            while not self._stop.is_set():
                buf = stream.timeout_pop_buffer(500_000)   # 0.5 s in µs
                if buf is None:
                    continue
                if buf.get_status() == _Aravis.BufferStatus.SUCCESS:
                    try:
                        self._q.put_nowait(bytes(buf.get_data()))
                    except queue.Full:
                        pass   # drop frame if consumer is slow
                stream.push_buffer(buf)

        except Exception as exc:
            log.error("FLIR capture thread error (%s): %s", self._serial, exc)
        finally:
            if cam:
                try:
                    cam.stop_acquisition()
                except Exception:
                    pass
            self._q.put(None)   # signal end of stream to any waiting get()


# ── ffmpeg command builder for recording ──────────────────────────────────────

_PREV_DRAWTEXT = (
    r"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    r":text='%{localtime\:%D %T}'"
    r":x=10:y=10:fontsize=28:fontcolor=white@0.9:box=1:boxcolor=black@0.5:boxborderw=4"
)

_REC_DRAWTEXT = (
    r"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    r":text='%{localtime\:%D %T}'"
    r":x=w-tw-10:y=h-th-10:fontsize=36"
    r":fontcolor=white@0.9:box=1:boxcolor=black@0.4:boxborderw=4"
)


def build_flir_cmd(
    params: dict,
    segment_pattern: str,
    segment_list_path: str,
    segment_duration: int,
) -> list[str]:
    """
    Build the ffmpeg command for segmented FLIR recording.
    Input is rawvideo mono8 on stdin (pipe:0); output is segmented H264 MP4.
    A second output writes a preview JPEG every 10 s.
    """
    fps = int(params.get("fps", 10))
    w   = int(params.get("width", FLIR_WIDTH))
    h   = int(params.get("height", FLIR_HEIGHT))

    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-f", "rawvideo",
        "-pix_fmt", "gray",
        "-s", f"{w}x{h}",
        "-r", str(fps),
        "-thread_queue_size", "512",
        "-i", "pipe:0",
    ]

    rec_filters = [f"fps={fps}"]
    if params.get("denoise"):
        rec_filters.append("hqdn3d=2:2:3:3")
    if params.get("timestamp_overlay"):
        rec_filters.append(_REC_DRAWTEXT)

    filter_complex = (
        "[0:v]split=2[_m][_r];"
        "[_m]" + ",".join(rec_filters) + "[rec];"
        "[_r]fps=1/10," + _PREV_DRAWTEXT + "[prev]"
    )
    cmd += ["-filter_complex", filter_complex, "-map", "[rec]"]
    cmd += [
        "-c:v", "libx264",
        "-preset", params.get("preset", "ultrafast"),
        "-crf", str(params.get("crf", 23)),
    ]

    if params.get("duration"):
        cmd += ["-t", str(params["duration"])]

    cmd += [
        "-f", "segment",
        "-segment_time", str(segment_duration),
        "-segment_format", "mp4",
        "-reset_timestamps", "1",
        "-segment_list", segment_list_path,
        segment_pattern,
    ]

    preview_path = f"/tmp/hcv3_preview_{params['node']}.jpg"
    cmd += ["-map", "[prev]", "-update", "1", "-q:v", "5"]
    if params.get("duration"):
        cmd += ["-t", str(params["duration"])]
    cmd.append(preview_path)

    return cmd


# ── Streaming ──────────────────────────────────────────────────────────────────

_STREAM_DRAWTEXT = (
    r"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    r":text='%{localtime\:%D %T}'"
    r":x=w-tw-10:y=h-th-10:fontsize=24"
    r":fontcolor=white@0.9:box=1:boxcolor=black@0.4:boxborderw=3"
)

BOUNDARY = b"--frame"


async def flir_stream_frames(node: str) -> AsyncGenerator[bytes, None]:
    """
    Async generator of MJPEG multipart chunks for live preview.
    Captures at full resolution, scales to 640×480 for streaming.
    """
    from .streaming import _active   # shared active-stream registry

    if node in _active and (_active[node].returncode is None):
        raise RuntimeError(f"{node} is already streaming")

    capture = FlirCapture(serial_from_node(node), FLIR_WIDTH, FLIR_HEIGHT, 10.0)
    capture.start()

    # Wait for camera to connect and report its actual resolution
    loop = asyncio.get_event_loop()
    ready = await loop.run_in_executor(None, capture.wait_ready, 5.0)
    if not ready:
        capture.stop()
        raise RuntimeError("FLIR camera did not initialize within 5 seconds")

    w, h = capture.actual_width, capture.actual_height
    log.info("FLIR stream using %dx%d", w, h)

    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "gray",
        "-s", f"{w}x{h}", "-r", "10",
        "-i", "pipe:0",
        "-vf", f"scale=640:480,{_STREAM_DRAWTEXT}",
        "-f", "mjpeg", "-q:v", "5",
        "pipe:1",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _active[node] = proc
    log.info("FLIR stream started: %s (pid=%d)", node, proc.pid)

    stop_feed = asyncio.Event()

    async def _feeder():
        try:
            while not stop_feed.is_set() and proc.returncode is None:
                frame = await loop.run_in_executor(None, capture.get, 2.0)
                if frame is None:
                    break
                if proc.stdin.is_closing():
                    break
                proc.stdin.write(frame)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.debug("FLIR feeder done: %s", exc)
        finally:
            try:
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except Exception:
                pass

    feed_task = asyncio.ensure_future(_feeder())

    buf = b""
    try:
        while proc.returncode is None:
            chunk = await asyncio.wait_for(proc.stdout.read(32768), timeout=5.0)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                if start == -1:
                    buf = b""
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end == -1:
                    buf = buf[start:]
                    break
                frame = buf[start:end + 2]
                buf   = buf[end + 2:]
                header = (
                    BOUNDARY + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n"
                    b"\r\n"
                )
                yield header + frame + b"\r\n"

    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    except Exception as exc:
        log.warning("FLIR stream error on %s: %s", node, exc)
    finally:
        stop_feed.set()
        feed_task.cancel()
        capture.stop()
        _active.pop(node, None)
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
        log.info("FLIR stream stopped: %s", node)
