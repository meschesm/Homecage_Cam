"""
Live MJPEG streaming from UVC cameras.

Each camera node gets one ffmpeg process shared across the lifetime of a request.
Frames are parsed from stdout by detecting JPEG SOI/EOI markers and yielded
as multipart/x-mixed-replace chunks — natively supported by all browsers including
Safari on iPad.

Only one stream per node is allowed at a time (UVC devices can't be opened twice).
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator

log = logging.getLogger(__name__)

# node (e.g. "/dev/video0") → running ffmpeg process
_active: dict[str, asyncio.subprocess.Process] = {}

BOUNDARY = b"--frame"
_STREAM_RES  = "640x480"   # preview resolution — fast and low-CPU
_STREAM_FPS  = 10
_STREAM_Q    = 5           # JPEG quality for mjpeg output (1=best, 31=worst)


def is_streaming(node: str) -> bool:
    proc = _active.get(node)
    return proc is not None and proc.returncode is None


def get_active_streams() -> list[str]:
    return [n for n, p in _active.items() if p.returncode is None]


async def stop_stream(node: str):
    proc = _active.pop(node, None)
    if proc and proc.returncode is None:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            pass
    log.info("Stream stopped: %s", node)


async def stream_frames(node: str, input_fmt: str = "mjpeg", hflip: bool = False, vflip: bool = False) -> AsyncGenerator[bytes, None]:  # noqa: C901
    """
    Async generator — yields raw multipart chunks suitable for StreamingResponse.
    Handles both UVC (V4L2) and FLIR (aravis) nodes.
    """
    from .flir import is_flir, flir_stream_frames
    if is_flir(node):
        async for chunk in flir_stream_frames(node, hflip=hflip, vflip=vflip):
            yield chunk
        return

    if is_streaming(node):
        # Another request is already streaming this node — reject
        raise RuntimeError(f"{node} is already streaming")

    _DRAWTEXT = (
        r"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        r":text='%{localtime\:%D %T}'"
        r":x=w-tw-10:y=h-th-10:fontsize=24"
        r":fontcolor=white@0.9:box=1:boxcolor=black@0.4:boxborderw=3"
    )
    cmd = [
        "ffmpeg",
        "-loglevel",      "error",
        "-f",             "v4l2",
        "-input_format",  input_fmt,
        "-video_size",    _STREAM_RES,
        "-framerate",     str(_STREAM_FPS),
        "-i",             node,
        "-vf",            ",".join(filter(None, [
                              "hflip" if hflip else None,
                              "vflip" if vflip else None,
                              _DRAWTEXT,
                          ])),
        "-f",             "mjpeg",
        "-q:v",           str(_STREAM_Q),
        "pipe:1",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _active[node] = proc
    log.info("Stream started: %s (pid=%d)", node, proc.pid)

    buf = b""
    try:
        while proc.returncode is None:
            chunk = await asyncio.wait_for(proc.stdout.read(32768), timeout=5.0)
            if not chunk:
                break
            buf += chunk

            # Extract complete JPEG frames (SOI=FF D8 … EOI=FF D9)
            while True:
                start = buf.find(b"\xff\xd8")
                if start == -1:
                    buf = b""
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end == -1:
                    buf = buf[start:]   # keep partial frame
                    break
                frame = buf[start : end + 2]
                buf = buf[end + 2 :]
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
        log.warning("Stream error on %s: %s", node, exc)
    finally:
        await stop_stream(node)
