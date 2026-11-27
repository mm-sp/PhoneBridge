#!/usr/bin/env python3
"""The PC facts sent to the phone's Device tab: reading the current Wi-Fi network from `nmcli -t` output.
Run directly: `python3 tests/test_pc_info.py`
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import pc_info  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


def main() -> int:
    real_run = pc_info.subprocess.run
    seen = {}

    def wifi(output: str) -> dict:
        def fake(argv, **kw):
            seen.update(argv=argv, **kw)
            return types.SimpleNamespace(stdout=output, returncode=0)

        pc_info.subprocess.run = fake
        try:
            return pc_info._wifi()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 - a crash here disconnects the phone
            return {"raised": repr(exc)}
        finally:
            pc_info.subprocess.run = real_run

    check("the connected network is picked", wifi("no:Other:90\nyes:Home:72\n")
          == {"wifi_ssid": "Home", "wifi_signal_pct": 72})
    check("a network name containing ':' is read whole", wifi("yes:Cafe\\: guest:55\n")
          == {"wifi_ssid": "Cafe: guest", "wifi_signal_pct": 55})
    check("an escaped backslash is unescaped", wifi("yes:a\\\\b:40\n").get("wifi_ssid") == "a\\b")
    check("a missing signal value is 0", wifi("yes:Home:\n") == {"wifi_ssid": "Home", "wifi_signal_pct": 0})
    check("an unreadable signal value is 0", wifi("yes:Home:--\n").get("wifi_signal_pct") == 0)
    check("a truncated line is ignored", wifi("yes\nyes:Home\n") == {})
    check("no active network gives nothing", wifi("no:Other:90\n") == {})
    check("nmcli is asked for untranslated UTF-8 output",
          seen.get("env", {}).get("LC_ALL") == "C.UTF-8" and seen.get("errors") == "replace")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
