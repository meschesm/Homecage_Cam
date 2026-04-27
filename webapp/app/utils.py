"""Shared utilities used across multiple modules."""

# V4L2 fourcc → ffmpeg pixel format name
FMT_MAP: dict[str, str] = {"mjpg": "mjpeg", "yuyv": "yuyv422"}


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n} TB"
