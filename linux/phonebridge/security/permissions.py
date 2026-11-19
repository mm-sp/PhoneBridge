"""Per-device, per-feature permissions: allow / deny / ask.

These gate what a paired phone may push to or do on this PC. A device being paired never implies any of them
(zero-trust): every incoming message is mapped to a feature and checked against the device's policy before a
handler runs. Remote control (shell, PC files, input, screen view) is denied until the user turns it on — that
setting is the `allow_pc_control` flag plus a deadline (`pc_control_until`): it switches itself off after
PHONEBRIDGE_PC_CONTROL_MINUTES (default 60).

"ask" means: the PC raises a prompt (the GUI registers `ask_callback`) the first time a feature is used in a
connection; the answer lasts for that connection only. With no prompt available, "ask" behaves as "deny".
"""
from __future__ import annotations

ALLOW, DENY, ASK = "allow", "deny", "ask"
MODES = (ALLOW, DENY, ASK)

FEATURES = {
    "files": "File transfer",
    "photos": "Photo access",
    "videos": "Video access",
    "notifications": "Notification sync",
    "sms": "SMS / messages",
    "calls": "Call management",
    "clipboard": "Clipboard sync",
    "screen": "Screen mirroring and camera",
    "remote_control": "Remote control of this PC",
    "apps": "Application integration",
}
DEFAULTS = {f: ALLOW for f in FEATURES} | {"remote_control": DENY}

# Message types that need no permission (connection housekeeping and status the user must always see).
EXEMPT = {"ping", "pong", "device.info", "device.unpair", "error", "load.progress", "file.accept", "file.reject", "file.cancel", "identity.rotate", "identity.rotated", "session.end",
          "file.saved", "relay.config", "hotspot.state", "screen.state", "volume.state"}


def feature_for(msg_type: str, payload: dict | None = None) -> str | None:
    """The feature a phone->PC message belongs to, or None if it is exempt/unknown (unknown types have no
    handler registered, so they do nothing anyway)."""
    if msg_type in EXEMPT:
        return None
    if msg_type in ("file.offer", "file.chunk", "files.list_result"):
        return "photos" if (payload or {}).get("photo_id") else "files"
    if msg_type.startswith(("photo.", "photos.")):
        return "photos"
    if msg_type.startswith(("notification.", "notifications.")):
        return "notifications"
    if msg_type.startswith(("sms.", "mms.")):
        return "sms"
    if msg_type.startswith(("call.", "calls.", "contacts.")):
        return "calls"
    if msg_type == "clipboard.update":
        return "clipboard"
    if msg_type.startswith(("video.", "mirror.", "webcam.", "sync.")):   # sync.* = screen-sync session commands
        return "screen"
    if msg_type.startswith(("pc.", "pcfiles.")):
        return "remote_control"
    if msg_type.startswith("apps."):
        return "apps"
    return None


def mode_of(device, feature: str) -> str:
    if feature == "remote_control":
        return ALLOW if device.pc_control_active() else DENY
    return device.permissions.get(feature, DEFAULTS[feature])


# Called as hook(device, feature, mode) after every change, so the app can record it in the audit ledger.
on_change: list = []


def set_mode(device, feature: str, mode: str) -> None:
    if feature not in FEATURES:
        raise ValueError(f"unknown feature {feature!r}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    from ..pairing import PairedDevice

    if feature == "remote_control":
        if mode == ASK:
            raise ValueError("remote control is either on or off")
        import time

        from ..pairing import pc_control_ttl_seconds

        on = mode == ALLOW
        until = time.time() + pc_control_ttl_seconds() if on else None   # remote control always expires
        updated = PairedDevice.update(device.fingerprint, allow_pc_control=on, pc_control_until=until)
        device.allow_pc_control, device.pc_control_until = on, until
    else:
        updated = PairedDevice.mutate(device.fingerprint, lambda d: d.permissions.__setitem__(feature, mode))
        device.permissions[feature] = mode
    if updated is None:   # an unsaved/foreign object (tests, a record deleted meanwhile): fall back to saving it
        device.save()
    for hook in on_change:
        try:
            hook(device, feature, mode)
        except Exception:  # noqa: BLE001 - auditing must never block a permission change
            pass
