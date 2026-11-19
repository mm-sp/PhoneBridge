"""User-adjustable privacy settings (clipboard expiry, history retention), kept in ~/.config/phonebridge/privacy.json."""
from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {
    "clipboard_expiry_s": 120,        # remove text received from the phone from the PC clipboard after this many seconds (0 = keep)
    "clipboard_history_hours": 24,    # forget clipboard history entries older than this (0 = keep forever)
}

# Clipboard formats that password managers (KeePassXC, Bitwarden, …) attach to what they copy: never forward or store it.
SENSITIVE_CLIPBOARD_MIMES = ("x-kde-passwordManagerHint",)


def _path() -> Path:
    from . import pairing

    return pairing.DEVICES_DIR.parent / "privacy.json"


def get(name: str):
    try:
        value = json.loads(_path().read_text()).get(name)
    except (OSError, ValueError):
        value = None
    return DEFAULTS[name] if not isinstance(value, int) or value < 0 else value


def set_value(name: str, value: int) -> None:
    if name not in DEFAULTS or not isinstance(value, int) or value < 0:
        raise ValueError(f"bad privacy setting {name!r}={value!r}")
    path = _path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data[name] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(0o600)
