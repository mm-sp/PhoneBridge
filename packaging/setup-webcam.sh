#!/usr/bin/env bash
# One-time setup for PhoneBridge's "phone as webcam": creates a virtual camera
# (/dev/video10, "PhoneBridge Camera") that browsers and video-call apps can select.
# Needs root: run with  sudo ./packaging/setup-webcam.sh  (or pkexec).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

echo "==> Installing v4l2loopback"
apt-get install -y v4l2loopback-dkms v4l2loopback-utils

echo "==> Configuring it to load at every boot"
echo "v4l2loopback" > /etc/modules-load.d/phonebridge-v4l2loopback.conf
cat > /etc/modprobe.d/phonebridge-v4l2loopback.conf <<'EOF'
# PhoneBridge virtual webcam (see PhoneBridge README, Camera tab)
options v4l2loopback video_nr=10 card_label="PhoneBridge Camera" exclusive_caps=1
EOF

echo "==> Loading it now"
modprobe -r v4l2loopback 2>/dev/null || true
modprobe v4l2loopback

if [ -e /dev/video10 ]; then
  echo "==> Done: /dev/video10 is \"$(cat /sys/class/video4linux/video10/name)\""
else
  echo "ERROR: /dev/video10 did not appear — check 'dmesg | tail'" >&2
  exit 1
fi
