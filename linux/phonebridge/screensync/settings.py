"""Persisted user settings for screen sync. A corrupt or missing file means defaults; writes are atomic."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger("phonebridge.sync.settings")


class SmartScreenMode(str, Enum):
    ENABLED = "enabled"        # turn the phone display off during successful mirroring (dim fallback if no ADB)
    DISABLED = "disabled"      # never touch the phone display
    AUTOMATIC = "automatic"    # only when a real power-off is available and verified; otherwise leave it on


@dataclass
class SyncSettings:
    smart_screen_mode: str = SmartScreenMode.AUTOMATIC.value
    auto_app_sync: bool = True
    auto_screen_off: bool = True
    auto_reconnect: bool = True
    adb_address: str = ""      # optional host:port of Wireless Debugging, adb-connected on demand

    def mode(self) -> SmartScreenMode:
        try:
            return SmartScreenMode(self.smart_screen_mode)
        except ValueError:
            return SmartScreenMode.AUTOMATIC


def _path(config_dir: Path | None = None) -> Path:
    from .. import pairing
    return (config_dir or pairing.DEVICES_DIR.parent) / "screensync.json"


def load(config_dir: Path | None = None) -> SyncSettings:
    s = SyncSettings()
    try:
        raw = json.loads(_path(config_dir).read_text())
        if isinstance(raw, dict):
            for k, v in raw.items():
                if hasattr(s, k) and type(getattr(s, k)) is type(v):
                    setattr(s, k, v)
    except (OSError, ValueError):
        pass
    if s.mode().value != s.smart_screen_mode:
        s.smart_screen_mode = SmartScreenMode.AUTOMATIC.value
    return s


def save(s: SyncSettings, config_dir: Path | None = None) -> None:
    p = _path(config_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".screensync.")
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(s), f, indent=1)
        os.replace(tmp, p)
    except OSError:
        log.exception("could not save screen sync settings")
