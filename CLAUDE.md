# homecagev3 — Project Context for Claude

## Branch Strategy

| Branch | Device | Purpose |
|--------|--------|---------|
| `dev` | cam1 (`100.81.80.110`) | Development and testing |
| `main` | cam2 (`100.70.127.43`) | Production |

**Workflow:** develop and test on `dev` → cam1, then merge to `main` and deploy to cam2.

```bash
# Merge dev into main and deploy to cam2:
git checkout main && git merge dev && git push
# then deploy files to cam2 and restart service
```

## What This Project Is
Overnight behavioral video recording system for rodent home cages. Records from multiple USB cameras simultaneously on a Raspberry Pi 5, storing footage to an external USB drive.

## Hardware

**cam1** — Raspberry Pi 5
- IP: `100.81.80.110` (Tailscale)
- SSH alias: `cam1` (key auth, user `ab-ivnc`)
- OS: Raspberry Pi OS (Debian 12)
- App: `http://100.81.80.110` (login: ab-ivnc)

**cam2** — Raspberry Pi 5 Model B Rev 1.1
- IP: `100.70.127.43` (Tailscale)
- SSH alias: `cam2` (key auth, user `ab-ivnc`)
- OS: Raspberry Pi OS (Debian 12), kernel 6.12.62+rpt-rpi-2712
- App: `http://100.70.127.43` (login: admin)

**Cameras** (2+ physical USB cameras, all UVC/uvcvideo driver):
| Name | USB ID | Capture Node |
|------|--------|-------------|
| Arducam Webcam Vitade AF | `0c45:6366` | `/dev/video0` |
| HD USB Camera | `32e4:9230` | `/dev/video5` |

Note: `/dev/video2` is the Arducam's H264 interface (same sensor, not a separate camera).

**Storage:** Runtime-managed by `StorageManager`. Discovers all drives mounted under `/media/ab-ivnc/`, auto-selects if exactly one is present. Default drive: 931.5 GB exFAT at `/media/ab-ivnc/hc2_data`.

## Web Application (primary interface)

The main interface is a FastAPI web app deployed on cam2 as a systemd service behind nginx on the Tailscale IP.

**Source:** `webapp/app/`

| File | Purpose |
|------|---------|
| `webapp/app/main.py` | FastAPI routes — auth, cameras, streaming, recording, sessions, file serving |
| `webapp/app/session.py` | Multi-camera overnight session management (parallel ffmpeg subprocesses, 30-min segments, auto-stitch, auto-resume) |
| `webapp/app/recording.py` | Single-camera test recording jobs with CPU/temp monitoring and progress parsing |
| `webapp/app/streaming.py` | MJPEG live preview via multipart/x-mixed-replace |
| `webapp/app/camera.py` | V4L2 camera discovery via `v4l2-ctl` |
| `webapp/app/auth.py` | JWT auth, bcrypt passwords, HttpOnly cookie |
| `webapp/app/config.py` | Pydantic settings — paths, JWT secret, admin credentials |
| `webapp/app/storage.py` | Runtime USB drive management — discover, select, eject |
| `webapp/app/utils.py` | Shared utilities: `fmt_bytes()`, `FMT_MAP` (imported by session.py, recording.py, main.py) |
| `webapp/app/templates/base.html` | Shared layout, CSS variables, nav (Recording + Camera Test links, Power Down button) — v1.22 |
| `webapp/app/templates/session.html` | Overnight recording UI — camera config with encoder/preset/fps, session start/stop, recordings browser |
| `webapp/app/templates/camera_test.html` | Per-camera test UI — live preview, test config panel, batch format testing, job history |
| `webapp/app/templates/login.html` | Login page |

**Deploy files (run from project root):**
```bash
# Deploy to both cam1 and cam2:
for CAM in cam1 cam2; do
  scp webapp/app/main.py $CAM:~/homecagev3/app/main.py
  scp webapp/app/session.py $CAM:~/homecagev3/app/session.py
  scp webapp/app/recording.py $CAM:~/homecagev3/app/recording.py
  scp webapp/app/storage.py $CAM:~/homecagev3/app/storage.py
  scp webapp/app/utils.py $CAM:~/homecagev3/app/utils.py
  scp webapp/app/templates/session.html $CAM:~/homecagev3/app/templates/session.html
  scp webapp/app/templates/camera_test.html $CAM:~/homecagev3/app/templates/camera_test.html
  scp webapp/app/templates/base.html $CAM:~/homecagev3/app/templates/base.html
  ssh $CAM 'sudo systemctl restart homecagev3'
done
```

**Note:** cam2 app files live at `~/homecagev3/app/`; cam1 at `~/homecagev3/webapp/app/` (repo layout). See deploy paths above.

**Access:**
- cam1: `http://100.81.80.110/` (requires Tailscale)
- cam2: `http://100.70.127.43/` (requires Tailscale)

## Recording Architecture

### Session recording (`session.py`)

Overnight recording uses **10-minute segments** to limit data loss from power interruptions.

- `start_session(camera_configs, duration, session_name, scheduled_time)` — starts parallel ffmpeg subprocesses
- Segment files: `{recordings_dir}/{safe_sess}/_segs_{stem}/{stem}_%03d.mp4`
- Final output: `{recordings_dir}/{safe_sess}/{session_name}_{camera_tag}_{timestamp}.mp4`
- `SEGMENT_DURATION = 600` (10 minutes per segment)
- ffmpeg muxer: `-f segment -segment_time 600 -segment_format mp4 -reset_timestamps 1 -segment_list segments.txt`
- On completion (normal or SIGINT): `_stitch_segments()` runs `ffmpeg -f concat -safe 0 -c copy` → single final file → deletes `_segs_*` directory
- If stitching fails: segments preserved on disk; `stream.status = "error"` with path in `stream.error`
- Graceful stop: SIGINT → 15s drain for ffmpeg → 120s wait for stitching → mark stopped
- Stream status lifecycle: `pending → running → stitching → done | error`
- `asyncio.ensure_future(_watch_session(session))` for fire-and-forget parallel execution

**Session config persistence (auto-resume after power loss):**
- Before recording starts, full config is written to `/home/ab-ivnc/homecagev3/pending_session.json`
- Cleared on normal completion, stop, or error
- On app startup, `maybe_resume_session()` reads this file; if ≥60s remain of a timed session, auto-resumes with remaining duration in the same session folder
- Called via `@app.on_event("startup")` in `main.py`

### Test recording (`recording.py`)
- Single camera, timed jobs with CPU/temp monitoring and per-frame progress via `-progress` pipe
- Output: `{test_recordings_dir}/{camera_safe_name}_{w}x{h}_{fmt}_{enc_tag}_{job_id}.mp4`
- 5-second title card overlay showing recording parameters
- JSONL log at `{test_recordings_dir}/job_log.jsonl`

### ffmpeg command conventions
- **Format map:** `mjpg` → `mjpeg`, `yuyv` → `yuyv422` (defined in `utils.FMT_MAP`, shared across modules)
- **FPS enforcement:** UVC cameras ignore `-framerate`; always apply `fps=N` output filter
- **Filter chain order:** `fps=N` → `hqdn3d=2:2:3:3` (if denoise) → `drawtext=...` (if timestamp_overlay)
- **Timestamp overlay:** `drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='%{localtime\:%D %T}':x=w-tw-10:y=h-th-10:fontsize=36:fontcolor=white@0.9:box=1:boxcolor=black@0.4:boxborderw=4`
- **H264 copy encoder:** use `-loglevel error` + `-fflags +genpts` to suppress unavoidable UVC timestamp warnings
- **Thread queue / wallclock:** always pass `-thread_queue_size 512 -use_wallclock_as_timestamps 1` for V4L2 input

### Current recording settings (overnight)
- 1920×1080 @ 10fps, libx264 ultrafast CRF 23, hqdn3d denoise ON, timestamp overlay ON
- Lit-room storage rates: Arducam ~5.2 GB/hr, HD USB ~1.9 GB/hr
- IR/dark conditions expected 3–5× smaller

## Storage Paths (on cam2)

| Path | Purpose |
|------|---------|
| `/media/ab-ivnc/{drive}/recordings/` | Overnight session recordings (subfolders by session name) |
| `/media/ab-ivnc/{drive}/test_recordings/` | Camera test recordings and job_log.jsonl |
| `/home/ab-ivnc/homecagev3/camera_names.json` | User-assigned camera display names (persisted server-side) |
| `/home/ab-ivnc/homecagev3/camera_settings.json` | Per-camera resolution + FPS settings (persisted server-side) |
| `/home/ab-ivnc/homecagev3/pending_session.json` | Active session config for power-bump auto-resume; absent when no session is pending |

Paths are resolved at runtime by `StorageManager`. The paths in `config.py` are fallbacks only.

## API Routes (main.py)

| Route | Purpose |
|-------|---------|
| `GET /api/cameras` | Discover UVC cameras |
| `GET /api/camera-names` | Load user-assigned camera names |
| `POST /api/camera-names` | Save user-assigned camera names |
| `GET /api/camera-settings` | Load per-camera resolution + FPS settings |
| `POST /api/camera-settings` | Save per-camera resolution + FPS settings |
| `GET /stream/{node_name}` | MJPEG live preview |
| `DELETE /stream/{node_name}` | Stop live preview |
| `GET /api/streams` | List currently streaming nodes |
| `POST /api/record` | Start single test recording job |
| `GET /api/record/{job_id}` | Poll test job status |
| `GET /api/jobs` | List all test jobs |
| `POST /api/session` | Start overnight session (body: cameras, duration, session_name, scheduled_time) |
| `GET /api/session` | Get current session status |
| `DELETE /api/session` | Stop current session |
| `GET /api/storage` | List drives and active selection |
| `POST /api/storage` | Select active drive |
| `POST /api/storage/eject` | Eject active drive (requires no active session) |
| `GET /api/recordings` | List recording files grouped by session folder |
| `GET /files/{path}` | Serve recording file (`?dl=1` for download) |
| `GET /api/job-log` | Return job_log.jsonl entries, newest first |
| `GET /api/disk` | Disk usage for active drive |
| `POST /api/shutdown` | Gracefully stop session + stitch + power down Pi |

## UI Features

### Recording page (`session.html`)
- **Drive panel:** dropdown of mounted drives, Eject button, free space bar; polls every 5s
- **Per-camera config:**
  - Enable/disable checkbox
  - Resolution dropdown (MJPG modes, highest-first; persisted to `camera_settings.json`)
  - FPS dropdown (5/10/15/20/24/30; persisted to `camera_settings.json`)
  - Camera name input (persisted to `camera_names.json`)
  - Encoder dropdown: libx264 or copy/H264 passthrough (copy only shown if camera has H264 node)
  - Preset dropdown: ultrafast → medium (hidden when encoder = copy)
  - CRF input (hidden when encoder = copy)
  - hqdn3d denoise checkbox (hidden when encoder = copy)
  - Timestamp overlay checkbox (hidden when encoder = copy)
- Session settings: name, duration, storage estimate, optional scheduled start time
- Session monitor: stream cards with elapsed, file size, rate/hr, STITCHING badge during concat
- Recordings browser: grouped by session folder, inline playback or download

### Camera Test page (`camera_test.html`)
- Camera discovery with supported format table
- Live preview button (MJPEG stream, 640×480 @ 10fps)
- **Configure Test** button: inserts a config panel directly under the selected camera card
  - Format, resolution, FPS dropdowns (from camera's actual reported modes)
  - Encoder (libx264 / copy), preset, CRF, denoise, duration
  - Start Recording button
- **Run All Tests:** batch-tests every format/resolution combo; results table with file size, per-hour rate, CPU, temperature, actual FPS, frame errors
- Scheduled batch: runs at a user-specified local time
- Job history table

## Known Hardware Constraints

**`h264_v4l2m2m` is unavailable on this Pi 5.** Use `libx264` instead. See `recording_experiment_findings.md` for full explanation.

**HD USB Camera** occasionally emits corrupt MJPEG frames (`EOI missing`, `No JPEG data`) — ffmpeg recovers automatically, not a critical issue.

**HD USB Camera 320×240** — produces empty files in batch tests; cause unclear, not a priority.

**H264 UVC passthrough warnings** (`Timestamps are unset`, `Non-monotonous DTS`) — inherent to UVC H264 streams, suppressed with `-loglevel error` for copy encoder.

## Known Frontend Constraints

**iOS/iPadOS WebKit** silently truncates nested template literals (backtick inside `${}` inside backtick). Always use string concatenation instead of nested template literals in JS. Confirmed on Firefox for iPad.

## Sudoers (one-time setup on cam2)

```bash
ssh cam2 'echo "ab-ivnc ALL=(ALL) NOPASSWD: /usr/sbin/shutdown, /usr/bin/umount" | sudo tee /etc/sudoers.d/homecagev3 && sudo chmod 0440 /etc/sudoers.d/homecagev3'
```

## Legacy Scripts (superseded by web app)

| File | Purpose |
|------|---------|
| `record.py` | Original CLI recording script — use webapp instead |
| `camera_test.py` | Original CLI camera test tool — use webapp instead |
| `mount_cam2.sh` | Mount cam2 filesystem via sshfs to `~/mnt/cam2` |
| `camera_guide.md` | Linux camera enumeration reference |
| `recording_experiment_findings.md` | Findings from camera evaluation and recording experiments |

## Mount cam2 Filesystem Locally
```bash
# Requires: macFUSE + sshfs-mac (brew install --cask macfuse && brew install gromgit/fuse/sshfs-mac)
cd ~/Documents/homecagev3
./mount_cam2.sh           # mount at ~/mnt/cam2
./mount_cam2.sh unmount   # unmount
```
