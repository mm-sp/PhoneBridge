"""Call audio on the PC through Bluetooth HFP (the PC acts as the phone's hands-free unit).

Android already sends call audio to a connected Bluetooth headset, so no phone-side code is
needed: while a call is active we make sure the phone is Bluetooth-connected to this PC and
switch the phone's PipeWire/PulseAudio card to the headset (HFP) profile — that gives the call
a speaker and microphone on the PC — and switch back to the previous profile (normally A2DP)
when the call ends. The phone must be Bluetooth-paired with this PC ("Phone calls" allowed
for it in the phone's Bluetooth settings); this module cannot pair it for you.

Everything here shells out to bluetoothctl / pactl and blocks: call it from a worker thread.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time

log = logging.getLogger("phonebridge.call_audio")

_HFP_PROFILES = ("headset-head-unit", "headset-head-unit-msbc", "headset-head-unit-cvsd")
_previous_profile: dict[str, str] = {}


def _run(*cmd: str, timeout: float = 8.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("%s failed: %s", cmd[0], exc)
        return ""


def find_phone(phone_name: str) -> str | None:
    """Bluetooth address of the paired device whose name matches the phone's name."""
    want = phone_name.strip().lower()
    for line in _run("bluetoothctl", "devices", "Paired").splitlines():
        m = re.match(r"Device\s+([0-9A-F:]{17})\s+(.*)", line.strip(), re.I)
        if m and m.group(2).strip().lower() == want:
            return m.group(1).upper()
    return None


def _card_name(addr: str) -> str:
    return "bluez_card." + addr.replace(":", "_")


def _card_profiles(card: str) -> tuple[str | None, list[str]]:
    """(active profile, available profile names) of a PulseAudio/PipeWire card."""
    out = _run("pactl", "list", "cards")
    block = ""
    for chunk in re.split(r"\n(?=Card #)", out):
        if f"Name: {card}" in chunk:
            block = chunk
            break
    if not block:
        return None, []
    active = re.search(r"Active Profile:\s*(\S+)", block)
    profiles = re.findall(r"^\s+(\S+):.*\(sinks: \d+, sources: \d+", block, re.M)
    return (active.group(1) if active else None), profiles


def status(phone_name: str) -> dict:
    """What a settings screen needs: is the phone paired/connected, does it offer HFP?"""
    addr = find_phone(phone_name)
    if addr is None:
        return {"ok": False, "reason": "The phone isn't Bluetooth-paired with this PC. Pair it in "
                                      "Settings → Bluetooth (and allow \"Phone calls\" on the phone)."}
    active, profiles = _card_profiles(_card_name(addr))
    if active is None:
        return {"ok": False, "addr": addr, "reason": "Paired but not connected over Bluetooth yet."}
    if not any(p in _HFP_PROFILES for p in profiles):
        return {"ok": False, "addr": addr, "reason": "This PC's audio stack has no headset (HFP) profile for the "
                                                     "phone. Install/enable PipeWire's Bluetooth HFP support "
                                                     "(wireplumber with bluez5 hfp_hf)."}
    return {"ok": True, "addr": addr, "profile": active}


def route_to_pc(phone_name: str) -> tuple[bool, str]:
    """Connects the phone's Bluetooth audio and switches it to the headset profile."""
    addr = find_phone(phone_name)
    if addr is None:
        return False, status(phone_name)["reason"]
    card = _card_name(addr)
    active, profiles = _card_profiles(card)
    if active is None:
        _run("bluetoothctl", "connect", addr, timeout=15)
        for _ in range(16):  # the card appears a moment after the link comes up
            active, profiles = _card_profiles(card)
            if active is not None:
                break
            time.sleep(0.5)
    if active is None:
        return False, "Couldn't connect the phone over Bluetooth."
    target = next((p for p in _HFP_PROFILES if p in profiles), None)
    if target is None:
        return False, status(phone_name).get("reason", "No HFP profile available.")
    if active not in _HFP_PROFILES:
        _previous_profile[card] = active
    _run("pactl", "set-card-profile", card, target)
    return True, "Call audio is on this PC"


def restore(phone_name: str) -> None:
    """Puts the phone's Bluetooth card back on the profile it had before the call."""
    addr = find_phone(phone_name)
    if addr is None:
        return
    card = _card_name(addr)
    previous = _previous_profile.pop(card, None)
    if previous:
        _run("pactl", "set-card-profile", card, previous)
