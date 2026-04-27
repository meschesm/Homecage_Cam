"""
Runtime USB drive management.
Discovers drives mounted under /media/ab-ivnc/, supports select and eject.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

_MEDIA_BASE = Path("/media/ab-ivnc")


class StorageManager:
    def __init__(self):
        self._active: Path | None = None

    # ── Discovery ──────────────────────────────────────────────────────────────

    def discover(self) -> list[dict]:
        """Return info for every currently mounted drive under _MEDIA_BASE."""
        if not _MEDIA_BASE.exists():
            return []
        drives = []
        for p in sorted(_MEDIA_BASE.iterdir()):
            if p.is_dir() and os.path.ismount(p):
                try:
                    u = shutil.disk_usage(p)
                    drives.append({
                        "mount": str(p),
                        "label": p.name,
                        "total": u.total,
                        "used":  u.used,
                        "free":  u.free,
                    })
                except OSError:
                    pass
        return drives

    def _validate_active(self):
        if self._active and not os.path.ismount(self._active):
            self._active = None

    def auto_select(self):
        """Auto-select if exactly one drive is mounted; clear stale selection."""
        self._validate_active()
        if self._active is None:
            drives = self.discover()
            if len(drives) == 1:
                self._active = Path(drives[0]["mount"])

    def select(self, mount: str) -> bool:
        p = Path(mount)
        if not p.is_dir() or not os.path.ismount(p):
            return False
        self._active = p
        return True

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def active(self) -> Path | None:
        self._validate_active()
        return self._active

    @property
    def recordings_dir(self) -> Path:
        root = self.active
        return (root or _MEDIA_BASE / "hc2_data") / "recordings"

    @property
    def test_recordings_dir(self) -> Path:
        root = self.active
        return (root or _MEDIA_BASE / "hc2_data") / "test_recordings"

    # ── Actions ────────────────────────────────────────────────────────────────

    async def eject(self) -> None:
        """Unmount the active drive. Raises RuntimeError on failure."""
        self._validate_active()
        if self._active is None:
            return
        target = str(self._active)
        proc = await asyncio.create_subprocess_exec(
            "sudo", "umount", target,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip() or "umount failed")
        self._active = None

    def status(self) -> dict:
        self._validate_active()
        return {
            "active": str(self._active) if self._active else None,
            "drives": self.discover(),
        }


storage_manager = StorageManager()
storage_manager.auto_select()
