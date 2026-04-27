#!/bin/bash
# Mount cam2's filesystem at ~/mnt/cam2.
# Usage: ./mount_cam2.sh          — mount
#        ./mount_cam2.sh unmount  — unmount

REMOTE="cam2:/"
MOUNTPOINT="$HOME/mnt/cam2"

unmount() {
    if mount | grep -q "$MOUNTPOINT"; then
        diskutil unmount force "$MOUNTPOINT" 2>/dev/null || umount -f "$MOUNTPOINT" 2>/dev/null
        echo "Unmounted $MOUNTPOINT"
    else
        echo "Not mounted."
    fi
    exit 0
}

[[ "$1" == "unmount" ]] && unmount

# Already mounted and healthy
if mount | grep -q "$MOUNTPOINT" && ls "$MOUNTPOINT/home" &>/dev/null; then
    echo "Already mounted at $MOUNTPOINT"
    exit 0
fi

# Unmount stale mount if present
if mount | grep -q "$MOUNTPOINT"; then
    echo "Stale mount detected, cleaning up..."
    diskutil unmount force "$MOUNTPOINT" 2>/dev/null || umount -f "$MOUNTPOINT" 2>/dev/null
fi

mkdir -p "$MOUNTPOINT"
echo "Mounting $REMOTE -> $MOUNTPOINT"
sshfs "$REMOTE" "$MOUNTPOINT" \
    -o reconnect \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=3 \
    -o follow_symlinks \
    -o defer_permissions \
    -o volname=cam2

if mount | grep -q "$MOUNTPOINT"; then
    echo "Mounted at $MOUNTPOINT"
else
    echo "Mount failed. Is cam2 reachable? Try: ssh cam2 'echo ok'"
    exit 1
fi
