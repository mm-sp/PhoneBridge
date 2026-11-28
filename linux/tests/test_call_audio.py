#!/usr/bin/env python3
"""Call audio on the PC (call_audio.py) against fake bluetoothctl/pactl: success is only reported when the headset profile
was really switched, and the previous profile comes back. Run: python3 tests/test_call_audio.py"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import call_audio  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


class Tools:
    """What the two commands would answer; `refuse` makes set-card-profile fail."""

    def __init__(self) -> None:
        self.active, self.refuse, self.switched = "a2dp-sink", False, []

    def run(self, cmd, **_kw):
        out, code = "", 0
        if cmd[:3] == ("bluetoothctl", "devices", "Paired"):
            out = "Device AA:BB:CC:DD:EE:FF Pixel\n"
        elif cmd[:3] == ("pactl", "list", "cards"):
            out = ("Card #1\n\tName: bluez_card.AA_BB_CC_DD_EE_FF\n\tProfiles:\n"
                   "\t\ta2dp-sink: A2DP (sinks: 1, sources: 0, priority: 1)\n"
                   "\t\theadset-head-unit: HFP (sinks: 1, sources: 1, priority: 1)\n"
                   f"\tActive Profile: {self.active}\n")
        elif cmd[:2] == ("pactl", "set-card-profile"):
            if self.refuse:
                code = 1
            else:
                self.active = cmd[3]
                self.switched.append(cmd[3])
        return types.SimpleNamespace(returncode=code, stdout=out)


def main() -> int:
    tools = Tools()
    call_audio.subprocess = types.SimpleNamespace(run=tools.run, TimeoutExpired=TimeoutError)

    tools.refuse = True
    ok, message = call_audio.route_to_pc("Pixel")
    check("a refused profile switch is reported as a failure", not ok and "stays on the phone" in message)
    call_audio.restore("Pixel")
    check("…and leaves nothing to restore", tools.switched == [])

    tools.refuse = False
    ok, _message = call_audio.route_to_pc("pixel ")
    check("a switch that works moves to the headset profile", ok and tools.active == "headset-head-unit")
    tools.refuse = True
    call_audio.restore("Pixel")
    tools.refuse = False
    call_audio.restore("Pixel")
    check("a failed restore is tried again next time", tools.active == "a2dp-sink")
    before = list(tools.switched)
    call_audio.restore("Pixel")
    check("…and once restored nothing is switched again", tools.switched == before)

    ok, message = call_audio.route_to_pc("Other phone")
    check("an unpaired phone is explained", not ok and "isn't Bluetooth-paired" in message)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
