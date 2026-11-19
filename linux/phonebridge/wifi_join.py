"""Join a Wi-Fi network (the phone's hotspot) without putting its password on a command line.

`nmcli dev wifi connect SSID password PW` shows PW in `ps` for every local user. Instead the profile is created over
NetworkManager's D-Bus API (the secret travels in the message, never in argv) and activated with
`nmcli connection up id <name>`, whose arguments carry no secret.

Assumes WPA/WPA2-PSK, which is what an Android hotspot uses. The profile keeps the fixed name PROFILE_NAME and is replaced on
every join, so it never piles up.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Callable

log = logging.getLogger("phonebridge.wifi")

PROFILE_NAME = "PhoneBridge hotspot"
Runner = Callable[[list[str], float], tuple[int, str]]


def _run(args: list[str], timeout: float) -> tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 255, str(exc)


def connection_settings(ssid: str, password: str, name: str = PROFILE_NAME) -> dict:
    """The NetworkManager settings dict (plain Python values; converted to GVariants by add_profile)."""
    return {
        "connection": {"id": name, "type": "802-11-wireless", "autoconnect": False},
        "802-11-wireless": {"ssid": ssid.encode("utf-8"), "mode": "infrastructure"},
        "802-11-wireless-security": {"key-mgmt": "wpa-psk", "psk": password},
        "ipv4": {"method": "auto"},
        "ipv6": {"method": "auto"},
    }


def add_profile(settings: dict) -> str:
    """Creates the profile over the system D-Bus and returns its object path. Raises on failure."""
    from gi.repository import Gio, GLib

    def variant(value):
        if isinstance(value, bool):
            return GLib.Variant("b", value)
        if isinstance(value, bytes):
            return GLib.Variant("ay", value)
        return GLib.Variant("s", str(value))

    wire = {section: {k: variant(v) for k, v in values.items()} for section, values in settings.items()}
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    reply = bus.call_sync(
        "org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings",
        "org.freedesktop.NetworkManager.Settings", "AddConnection",
        GLib.Variant("(a{sa{sv}})", (wire,)), GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 10000, None)
    return reply.unpack()[0]


def join(ssid: str, password: str, *, run: Runner = _run, add: Callable[[dict], str] = add_profile,
         name: str = PROFILE_NAME) -> tuple[bool, str]:
    """Returns (ok, message for the user). Never raises."""
    if not ssid:
        return False, "The phone did not report a network name"
    run(["nmcli", "connection", "delete", "id", name], 10)   # replace an older profile; fine if there is none
    try:
        add(connection_settings(ssid, password, name))
    except Exception as exc:  # noqa: BLE001 - D-Bus errors, no NetworkManager, polkit refusal
        log.warning("could not create the Wi-Fi profile: %s", exc)
        return False, f"Couldn't set up the Wi-Fi connection: {exc}"
    rc, out = run(["nmcli", "connection", "up", "id", name], 90)
    if rc != 0:
        log.warning("nmcli connection up failed: %s", out[:200])
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        reason = next((ln for ln in lines if ln.lower().startswith("error")), lines[0] if lines else "unknown error")
        return False, f"Couldn't join {ssid}: {reason[:140]}"
    return True, f"Joined {ssid}"
