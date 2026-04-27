"""
UVC camera discovery via sysfs + v4l2-ctl.
Ported from camera_test.py — returns Pydantic models for the API.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from pydantic import BaseModel


class FormatMode(BaseModel):
    fmt: str        # MJPG | YUYV | H264
    width: int
    height: int
    fps: float


class CameraInfo(BaseModel):
    name: str
    capture_node: str
    h264_node: str | None
    usb_id: str
    modes: list[FormatMode]


# ── sysfs helpers ──────────────────────────────────────────────────────────────

def _read_uevent(node: str) -> dict[str, str]:
    name = Path(node).name
    uevent = Path(f"/sys/class/video4linux/{name}/device/uevent")
    result: dict[str, str] = {}
    try:
        for line in uevent.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    except OSError:
        pass
    return result


def _driver(node: str) -> str | None:
    return _read_uevent(node).get("DRIVER")


def _usb_product(node: str) -> str | None:
    return _read_uevent(node).get("PRODUCT")


# ── Format enumeration ─────────────────────────────────────────────────────────

def _parse_formats(node: str) -> list[FormatMode]:
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--list-formats-ext", "--device", node],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []

    modes: list[FormatMode] = []
    current_fmt: str | None = None
    current_w: int | None = None
    current_h: int | None = None

    for line in out.splitlines():
        # Format line: [0]: 'MJPG' (Motion-JPEG, compressed)
        m = re.search(r"'\s*(\w+)\s*'", line)
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
        if m and current_fmt and current_w is not None:
            modes.append(FormatMode(
                fmt=current_fmt,
                width=current_w,
                height=current_h,
                fps=float(m.group(1)),
            ))

    return modes


# ── Discovery ──────────────────────────────────────────────────────────────────

def discover_cameras() -> list[CameraInfo]:
    """
    Find all cameras: UVC via sysfs/v4l2-ctl, and FLIR via aravis.
    """
    from .flir import discover_flir_cameras
    # Collect all UVC nodes with their USB product string
    uvc: dict[str, str] = {}  # node → PRODUCT
    for node_path in sorted(Path("/dev").glob("video*"),
                            key=lambda p: int(p.name[5:])):
        node = str(node_path)
        if _driver(node) == "uvcvideo":
            product = _usb_product(node)
            if product:
                uvc[node] = product

    if not uvc:
        return []

    # Group by product — one physical camera per product
    by_product: dict[str, list[str]] = {}
    for node, product in uvc.items():
        by_product.setdefault(product, []).append(node)

    cameras: list[CameraInfo] = []

    for product, nodes in by_product.items():
        nodes = sorted(nodes, key=lambda p: int(Path(p).name[5:]))

        # Camera name from v4l2-ctl --info
        name = Path(nodes[0]).name
        try:
            out = subprocess.check_output(
                ["v4l2-ctl", "--device", nodes[0], "--info"],
                stderr=subprocess.DEVNULL, text=True, timeout=5,
            )
            for line in out.splitlines():
                if "Card type" in line:
                    name = line.split(":", 1)[1].strip()
                    break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

        # Classify each node: capture (MJPEG/YUYV) or H264 passthrough
        capture_node: str | None = None
        h264_node: str | None = None
        all_modes: list[FormatMode] = []

        for node in nodes:
            modes = _parse_formats(node)
            fmts = {m.fmt for m in modes}
            if "H264" in fmts and not (fmts & {"MJPG", "YUYV"}):
                h264_node = node
                all_modes.extend(modes)
            elif fmts & {"MJPG", "YUYV"}:
                if capture_node is None:
                    capture_node = node
                    all_modes.extend(modes)

        if capture_node is None:
            continue

        usb_id = "/".join(product.split("/")[:2])
        cameras.append(CameraInfo(
            name=name,
            capture_node=capture_node,
            h264_node=h264_node,
            usb_id=usb_id,
            modes=all_modes,
        ))

    cameras += discover_flir_cameras()
    return cameras
