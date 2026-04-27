# USB Camera Findings & Linux Access Guide

## Hardware Summary

Two physical USB cameras are connected to the Raspberry Pi 5 (`cam2` / `100.70.127.43`):

| # | Name | USB ID | Primary Node |
|---|------|--------|-------------|
| 1 | Arducam Webcam Vitade AF | `0c45:6366` | `/dev/video0` |
| 2 | HD USB Camera | `32e4:9230` | `/dev/video5` |

---

## Camera 1 — Arducam Webcam Vitade AF

**USB ID:** `0c45:6366` (Microdia)
**Driver:** `uvcvideo`
**V4L2 nodes:** `/dev/video0` (MJPEG/YUYV), `/dev/video2` (H264), `/dev/video1`, `/dev/video3` (metadata)

> `/dev/video2` is **not** a separate camera. The Arducam exposes both MJPEG and H264 interfaces as separate V4L2 nodes — same physical sensor, different encodings.

### Supported Formats

| Format | Resolution | FPS |
|--------|-----------|-----|
| MJPEG | 1920×1080 | 30 |
| MJPEG | 1280×720 | 30 |
| MJPEG | 640×480 | 30 |
| MJPEG | 320×240 | 30 |
| YUYV | 640×480 | 30 |
| YUYV | 320×240 | 30 |
| H264 | 1920×1080 | 30 |
| H264 | 1280×720 | 30 |
| H264 | 640×480 | 30 |
| H264 | 640×360 | 30 |

---

## Camera 2 — HD USB Camera

**USB ID:** `32e4:9230`
**Driver:** `uvcvideo`
**V4L2 nodes:** `/dev/video5` (primary), `/dev/video6` (metadata)

### Supported Formats

| Format | Resolution | FPS |
|--------|-----------|-----|
| MJPEG | 1920×1080 | 30 |
| MJPEG | 1280×720 | 60 |
| MJPEG | 1280×1024 | 30 |
| MJPEG | 1024×768 | 30 |
| MJPEG | 800×600 | 60 |
| MJPEG | 640×480 | 120 |
| MJPEG | 320×240 | 120 |
| YUYV | 1920×1080 | 6 |
| YUYV | 1280×720 | 9 |
| YUYV | 1280×1024 | 6 |
| YUYV | 1024×768 | 6 |
| YUYV | 800×600 | 20 |
| YUYV | 640×480 | 30 |
| YUYV | 320×240 | 30 |

---

## Linux Tools Guide

### 1. List all connected cameras

```bash
v4l2-ctl --list-devices
```

This groups V4L2 nodes by physical device. Each USB camera entry shows its human-readable name, USB bus path, and all associated `/dev/videoN` nodes. Use the **lowest-numbered node** for capture; higher nodes are typically metadata or alternate encodings.

### 2. Identify a camera's USB ID and driver

```bash
# Show USB vendor:product ID and driver for a node
cat /sys/class/video4linux/video0/device/uevent

# Alternatively, list all USB devices with names
lsusb
```

To check if two nodes belong to the same physical camera, compare their `PRODUCT=` field:

```bash
cat /sys/class/video4linux/video0/device/uevent
cat /sys/class/video4linux/video2/device/uevent
```

Identical `PRODUCT=` values mean the same physical device.

### 3. List supported formats, resolutions, and frame rates

```bash
v4l2-ctl --list-formats-ext --device /dev/video0
v4l2-ctl --list-formats-ext --device /dev/video5
```

### 4. Check which process is using a camera

```bash
fuser /dev/video0
lsof /dev/video0
```

### 5. Capture a single test frame

**MJPEG camera (Arducam or HD USB):**
```bash
ffmpeg -f v4l2 -input_format mjpeg -video_size 1920x1080 \
  -i /dev/video0 -frames:v 1 -update 1 /tmp/test.jpg -y
```

**H264 camera (Arducam alternate node):**
```bash
ffmpeg -f v4l2 -input_format h264 -video_size 1920x1080 \
  -i /dev/video2 -frames:v 1 -update 1 /tmp/test.jpg -y
```

### 6. Copy frames from cam2 to local machine

```bash
scp cam2:/tmp/test.jpg ~/Documents/homecagev3/
```

### 7. Stream live video (preview)

```bash
ffplay -f v4l2 -input_format mjpeg -video_size 1280x720 /dev/video0
```

---

## Node Map Reference

```
/dev/video0  — Arducam, MJPEG/YUYV capture      ← use this
/dev/video1  — Arducam, metadata
/dev/video2  — Arducam, H264 capture (same sensor)
/dev/video3  — Arducam, metadata
/dev/video5  — HD USB Camera, MJPEG/YUYV capture ← use this
/dev/video6  — HD USB Camera, metadata
/dev/video19 — rpi-hevc-dec (platform decoder, not a camera)
/dev/video20–35 — pispbe (Pi ISP backend, not cameras)
```
