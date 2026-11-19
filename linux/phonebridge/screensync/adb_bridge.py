"""Thin wrapper around `adb`, injectable for tests. Everything here is a supported, non-root adb shell operation
on a device where the user enabled USB debugging / Wireless Debugging and authorised this PC."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("phonebridge.sync.adb")

Runner = Callable[[list[str], float], tuple[int, str]]
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]{1,80}$")
COMPONENT_RE = re.compile(r"^[A-Za-z0-9._]+/[A-Za-z0-9._$]+$")
ADDR_RE = re.compile(r"^[A-Za-z0-9._\-]+:\d{2,5}$")


def default_runner(args: list[str], timeout: float) -> tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        return 255, str(exc)


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str   # "device" | "unauthorized" | "offline" ...


class Adb:
    def __init__(self, runner: Runner = default_runner, adb_path: str | None = None, timeout: float = 8.0) -> None:
        self._run = runner
        self._adb = adb_path or shutil.which("adb")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return self._adb is not None

    def run(self, *args: str, serial: str | None = None, timeout: float | None = None) -> tuple[int, str]:
        if not self._adb:
            return 127, "adb not installed"
        if serial is not None and not SERIAL_RE.match(serial):
            return 2, "bad serial"
        cmd = [self._adb] + (["-s", serial] if serial else []) + list(args)
        return self._run(cmd, timeout or self.timeout)

    def shell(self, serial: str, *args: str, timeout: float | None = None) -> tuple[int, str]:
        return self.run("shell", *args, serial=serial, timeout=timeout)

    def devices(self) -> list[AdbDevice]:
        rc, out = self.run("devices")
        if rc != 0:
            return []
        found = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and SERIAL_RE.match(parts[0]):
                found.append(AdbDevice(parts[0], parts[1]))
        return found

    def connect(self, address: str) -> bool:
        """`adb connect host:port` for Wireless Debugging (pairing with the 6-digit code is done by the user once)."""
        if not ADDR_RE.match(address):
            return False
        rc, out = self.run("connect", address, timeout=10)
        return rc == 0 and ("connected" in out) and "failed" not in out and "cannot" not in out

    def pick_serial(self, hints: list[str]) -> str | None:
        """Chooses the adb device that is *this* phone. `hints` are the phone's LAN IP and/or known serials. With no
        usable hint we only auto-pick when exactly one authorised device is attached — never guess among several."""
        ready = [d.serial for d in self.devices() if d.state == "device"]
        for h in hints:
            if not h:
                continue
            for s in ready:
                if s == h or s.startswith(h + ":"):
                    return s
        for h in hints:
            if h and re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
                for s in ready:
                    rc, out = self.shell(s, "ip", "-o", "-4", "addr", "show")
                    if rc == 0 and re.search(rf"inet {re.escape(h)}/", out):
                        return s
        return ready[0] if len(ready) == 1 else None

    # --- queries -----------------------------------------------------------------------------------------------
    def sdk(self, serial: str) -> int | None:
        rc, out = self.shell(serial, "getprop", "ro.build.version.sdk")
        return int(out.strip()) if rc == 0 and out.strip().isdigit() else None

    def has_display_power_commands(self, serial: str) -> bool:
        """True if `cmd display` offers both power-off and power-reset (AOSP 14+ / LineageOS 21+ ... ; verified by probe,
        not by version number, because vendors differ)."""
        rc, out = self.shell(serial, "cmd", "display", "help")
        # Not `rc == 0`: on the Mi A3 (Android 17) `cmd display help` prints the help and exits 255.
        return "power-off" in out and "power-reset" in out

    def display_on(self, serial: str) -> bool | None:
        """Actual panel state from DisplayManager (mState=ON/OFF/DOZE...). None if it cannot be read."""
        rc, out = self.shell(serial, "dumpsys", "display")
        if rc != 0:
            return None
        # The dump has several "mState=" lines (e.g. AUTO_BRIGHTNESS_DISABLED); only a real display state counts.
        m = re.search(r"^\s*mState=(ON_SUSPEND|ON|OFF|DOZE_SUSPEND|DOZE|VR|UNKNOWN)\b", out, re.M)
        if not m:
            return None
        return m.group(1).upper() in ("ON", "ON_SUSPEND")

    def interactive(self, serial: str) -> bool | None:
        rc, out = self.shell(serial, "dumpsys", "power")
        m = re.search(r"mWakefulness=(\w+)", out) if rc == 0 else None
        return None if not m else m.group(1) == "Awake"

    def start_activity(self, serial: str, component: str, *extras: str) -> bool:
        if not COMPONENT_RE.match(component):
            return False
        rc, out = self.shell(serial, "am", "start", "-n", component, *extras)
        return rc == 0 and "Error" not in out
