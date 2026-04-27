# Multi-Camera Recording: Experiment Findings

**Date:** 2026-03-18 (updated 2026-04-03)
**Platform:** Raspberry Pi 5 Model B Rev 1.1 (BCM2712, kernel 6.12.62+rpt-rpi-2712)
**Storage:** 931.5 GB exFAT USB drive, managed at runtime by `StorageManager`

---

## Hardware

### Physical Cameras

Two USB cameras confirmed. A third apparent device (`/dev/video2`) turned out to be the Arducam's H264 interface — same physical sensor, not a separate camera.

| Name | USB ID | Primary Node | Driver |
|------|--------|-------------|--------|
| Arducam Webcam Vitade AF | `0c45:6366` | `/dev/video0` | uvcvideo |
| HD USB Camera | `32e4:9230` | `/dev/video5` | uvcvideo |

### V4L2 Node Map

Each physical camera exposes multiple V4L2 nodes. Only the first node per camera is used for capture:

```
/dev/video0  — Arducam, MJPEG/YUYV capture      ← capture node
/dev/video1  — Arducam, metadata
/dev/video2  — Arducam, H264 output (same sensor, hardware-encoded by camera)
/dev/video3  — Arducam, metadata
/dev/video5  — HD USB Camera, MJPEG/YUYV capture ← capture node
/dev/video6  — HD USB Camera, metadata
/dev/video19 — rpi-hevc-dec (HEVC decoder, not a camera or encoder)
/dev/video20–35 — pispbe (Pi ISP back end, for CSI/libcamera pipeline)
```

---

## Why `h264_v4l2m2m` Is Listed But Doesn't Work

`ffmpeg -encoders` showed `h264_v4l2m2m` as available. Running it failed with:

```
[h264_v4l2m2m] Could not find a valid device
[h264_v4l2m2m] can't configure encoder
```

### The cause

`h264_v4l2m2m` is compiled into this version of ffmpeg, but **it requires a V4L2 memory-to-memory (M2M) hardware encoder device** to exist at runtime — typically `/dev/video10` or `/dev/video11` on a Raspberry Pi 4.

**The Pi 5 does not expose one.** Here is why:

| Generation | SoC | GPU | H264 encoder via V4L2 M2M |
|------------|-----|-----|--------------------------|
| Pi 4 | BCM2711 | VideoCore VI | Yes — `/dev/video10`, `/dev/video11` |
| Pi 5 | BCM2712 | VideoCore VII | **No** |

The Pi 5 uses a fundamentally different architecture. Its I/O is handled by the **RP1 south bridge** chip, and the VideoCore VII GPU — while capable of hardware video encoding in principle — does **not** expose an H264 encoder through the V4L2 M2M interface in the current kernel (6.12.x). The RPi Foundation has not yet implemented this in the mainline or RPi kernel driver.

What the Pi 5 *does* expose:
- `rpi-hevc-dec` (`/dev/video19`) — a hardware **HEVC decoder** only
- `pispbe` (`/dev/video20–35`) — the Pi ISP back end, used by the `libcamera` / CSI camera pipeline, not relevant to USB cameras

### Bottom line

`h264_v4l2m2m` is a dead end on the Pi 5 with USB cameras under the current kernel. Software encoding with `libx264` is the correct path.

---

## Encoder: `libx264 ultrafast` with CRF

Since hardware encoding is unavailable, the recorder falls back to `libx264` with `-preset ultrafast -crf 23`.

### What CRF means for overnight recordings

`-crf 23` is **constant rate factor** (constant perceptual quality), not constant bitrate. This is the right choice for overnight behavioral recordings:

- A dark, still cage at 3am uses very few bits (small files)
- An active, brightly lit scene gets more bits automatically (better quality)
- No manual bitrate tuning needed per-camera

The `2M` bitrate setting in the config is ignored by `libx264` when `-crf` is set — it's a leftover from the hardware encoder path. It can be removed, or kept as an upper-bound cap with `-maxrate 2M -bufsize 4M`.

### Observed output sizes

#### 30fps (initial tests — too large for overnight use)

| Camera | 5-min recording | Projected per hour |
|--------|----------------|-------------------|
| Arducam (1920×1080@30fps) | ~1.3 GB | ~15.6 GB |
| HD USB Camera (1920×1080@30fps) | ~405 MB | ~4.9 GB |

**Overnight projection (8 hours, 2 cameras) @ 30fps: ~164 GB**

#### 10fps (final configuration)

| Camera | 30s recording | Projected per hour | Projected 8hr overnight |
|--------|--------------|-------------------|------------------------|
| Arducam (1920×1080@10fps) | 66 MB | ~528 MB | ~4.2 GB |
| HD USB Camera (1920×1080@10fps) | 17 MB | ~136 MB | ~1.1 GB |

**Overnight projection (8 hours, 2 cameras) @ 10fps: ~5.3 GB** — well within the 931 GB drive.

The ~4× size reduction from dropping 30fps → 10fps is expected: fewer frames means fewer keyframes and less inter-frame data for the encoder to process.

The large difference (~4×) between the two cameras at the same settings is due to scene complexity and MJPEG input quality differences — `libx264` CRF encodes each camera's feed on its own merits.

---

## Software Architecture

### Why ffmpeg subprocesses (not OpenCV or Python V4L2)

Each camera runs as its own `ffmpeg` child process. This approach is:

- **CPU-efficient:** Python's GIL is never a bottleneck; each process runs independently
- **Kernel-efficient:** ffmpeg reads directly from the V4L2 kernel buffer with minimal copies
- **Resilient:** one camera crashing doesn't affect the other
- **Proven:** ffmpeg's V4L2 input path is mature and handles UVC quirks (corrupt MJPEG frames, timestamp gaps) gracefully

### Input flags that matter

| Flag | Purpose |
|------|---------|
| `-input_format mjpeg` | Tell V4L2 to request MJPEG from the camera (encoding happens on-device, not on Pi CPU) |
| `-thread_queue_size 512` | Larger kernel read buffer — reduces risk of dropped frames during brief CPU spikes |
| `-use_wallclock_as_timestamps 1` | Replace V4L2 DTS (which can drift or reset) with wall-clock time — essential for overnight recordings |

### Output: 30-minute segments + auto-stitch

Recordings are split into 30-minute `.mp4` segments via ffmpeg's `-f segment` muxer. On completion (normal end or manual stop):

1. ffmpeg receives SIGINT and cleanly closes the current segment
2. `_stitch_segments()` runs `ffmpeg -f concat -safe 0 -c copy` to join all completed segments into a single final `.mp4`
3. The raw segment directory (`_segs_*`) is deleted

Benefits:
- A power loss loses at most 30 minutes of footage; all prior segments are intact
- The final file delivered to the user is a single contiguous recording (no manual concat needed)
- If stitching fails, raw segments are preserved and the error message includes their path

Segment duration is set by `SEGMENT_DURATION = 1800` in `session.py`.

### Power-bump auto-resume

Before any ffmpeg process starts, the full session configuration is written to `pending_session.json` on the Pi's SD card. On app startup the file is read; if a timed session still has ≥60 seconds remaining, it resumes automatically with the remaining duration, writing new segments into the same session folder.

### Robustness features

- **Segment-based recording:** power loss loses at most 30 minutes (one in-progress segment)
- **Auto-resume on reboot:** session config persisted to SD card; app restarts the session after a power bump
- **Frame error detection:** corrupt MJPEG frames (`EOI missing`, `No JPEG data`) are counted and shown in the test recording UI
- **Clean shutdown:** SIGINT gracefully terminates ffmpeg, triggering segment close and stitch before the Pi powers off

---

## Denoising: `hqdn3d` Filter

### Motivation

These recordings are made under IR illumination in a dark closet. USB cameras not optimised for IR produce noisy, grainy images at low light. `libx264` with CRF treats sensor noise as detail to preserve — wasting bits on information that is not behaviourally relevant.

The `hqdn3d=2:2:3:3` filter applies spatial and temporal blurring before encoding, suppressing noise without degrading actual content (mouse movement, posture).

### CPU cost

| Configuration | CPU per ffmpeg process | System idle |
|--------------|----------------------|-------------|
| libx264 only | ~94% (1 core) | ~50% |
| libx264 + hqdn3d | ~150% (1.5 cores) | ~23% |

Two cameras with hqdn3d consume ~3 of 4 cores. Sufficient headroom remains for the overnight run.

### File size impact (lit room — IR conditions will reduce further)

| Camera | Without hqdn3d | With hqdn3d | Reduction |
|--------|---------------|-------------|-----------|
| Arducam | 51 MB/60s | 36 MB/60s | 29% |
| HD USB | 13 MB/60s | 5.1 MB/60s | 61% |

Under noisy IR illumination, the reduction is expected to be substantially greater.

### Filter parameters

`hqdn3d=luma_spatial:chroma_spatial:luma_temporal:chroma_temporal`

Current values `2:2:3:3` are conservative. If CPU headroom becomes tight, reduce to `1:1:2:2`. Do not increase beyond `4:4:6:6` or motion blur becomes visible in fast movement.

---

## Known Issues and Observations

### HD USB Camera: intermittent corrupt MJPEG frames

During the 5-minute test, the HD USB Camera emitted occasional malformed MJPEG frames:

```
[mjpeg] EOI missing, emulating
[mjpeg] Found EOI before any SOF, ignoring
[mjpeg] No JPEG data found in image
```

These are **not dropped frames** — ffmpeg recovers and continues. They likely originate from the camera's USB transfer buffer being flushed mid-frame under load. The `thread_queue_size 512` flag helps absorb these. This camera is lower priority if you only need one reliable feed.

---

## Tools

The CLI scripts (`record.py`, `camera_test.py`) are superseded by the web app. All recording, testing, and monitoring is now done through the browser UI at `http://100.70.127.43/`.

### Web app — Camera Test page

Discovers all UVC cameras, enumerates formats, and runs parameterised test recordings. Reports file size, storage projections (per hour and per 14-hour session), CPU avg/peak, SoC temperature, frame errors, and actual vs requested FPS. Results are logged to `job_log.jsonl` on the USB drive and viewable in the job history table.

The **Run All Tests** button tests every format/resolution combination automatically and displays results in a sortable table.

### Web app — Recording page

Starts multi-camera overnight sessions with configurable resolution, FPS, encoder (libx264 or H264 passthrough), preset, CRF, denoise, and timestamp overlay per camera. Supports scheduled start times and produces segmented recordings with automatic stitching on completion.

---

## Notes on Overnight Storage (IR conditions)

Light-room rates measured in testing:

| Camera | Resolution | Rate (lit room) | Rate (IR/dark, estimated) |
|--------|-----------|----------------|--------------------------|
| Arducam | 1920×1080 | ~5.2 GB/hr | ~1.0–1.7 GB/hr |
| HD USB  | 1920×1080 | ~1.9 GB/hr | ~0.3–0.6 GB/hr |

IR conditions compress 3–5× better than lit-room conditions because the scene is dark, low-contrast, and low-noise (relative to the sensor baseline). The hqdn3d filter provides additional compression in dark/noisy conditions by suppressing sensor grain before encoding.

For a 14-hour overnight session with both cameras at 1920×1080 @ 10fps CRF23 under IR illumination, expect 15–25 GB total.

---

## Future

- **H264 hardware encoder on Pi 5** — watch for RPi kernel updates adding `v4l2-m2m` H264 encoder support via VideoCore VII. When available, switching to `h264_v4l2m2m` would eliminate the libx264 CPU cost (~80% reduction). As of kernel 6.12 this is not yet implemented.
