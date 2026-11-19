"""USB link: reaches the PC over a cable instead of the network, using adb's reverse port forwarding.

`adb reverse tcp:28100 tcp:28100` makes the phone's 127.0.0.1:28100 reach this PC's control port, through the USB cable
only — nothing is exposed on any network. It needs adb on the PC and USB debugging enabled and authorised on the phone
(a developer feature), so it is optional; without it the phone simply uses Wi-Fi. The connection itself is the same
pinned-TLS + v4 authenticated handshake as over Wi-Fi: USB changes the route, not the trust.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger("phonebridge.usb")


def adb_path() -> str | None:
    return shutil.which("adb")


def devices(adb: str | None = None) -> list[str]:
    """Serials of phones adb can talk to (state 'device': USB debugging authorised)."""
    adb = adb or adb_path()
    if not adb:
        return []
    try:
        out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def ensure_reverse(port: int, adb: str | None = None) -> list[str]:
    """Sets up (or refreshes) the reverse tunnel for every attached phone; returns the serials that now have it."""
    adb = adb or adb_path()
    ok = []
    for serial in devices(adb):
        try:
            r = subprocess.run([adb, "-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}"], capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("adb reverse failed for %s: %s", serial, exc)
            continue
        if r.returncode == 0:
            ok.append(serial)
        else:
            log.info("adb reverse refused for %s: %s", serial, r.stderr.strip())
    return ok


def remove_reverse(port: int, adb: str | None = None) -> None:
    adb = adb or adb_path()
    for serial in devices(adb):
        subprocess.run([adb, "-s", serial, "reverse", "--remove", f"tcp:{port}"], capture_output=True, timeout=8)
