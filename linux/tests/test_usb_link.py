#!/usr/bin/env python3
"""USB link helper against a fake `adb`. Run: `python3 tests/test_usb_link.py`"""
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phonebridge import usb_link  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def main():
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "calls.log"
    fake = tmp / "adb"
    fake.write_text(f"""#!/bin/sh
echo "$@" >> {log}
case "$1" in
  devices) printf 'List of devices attached\\nAAA111\\tdevice\\nBBB222\\tunauthorized\\nCCC333\\tdevice\\nDDD444\\toffline\\n' ;;
  -s) if [ "$2" = "CCC333" ] && [ "$3" = "reverse" ] && [ "$4" != "--remove" ]; then echo "error: closed" >&2; exit 1; fi ;;
esac
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    check("only authorised phones count (not unauthorized/offline)", usb_link.devices(str(fake)) == ["AAA111", "CCC333"])
    ok = usb_link.ensure_reverse(28100, str(fake))
    check("the tunnel is set up for each phone adb accepts", ok == ["AAA111"])
    calls = log.read_text()
    check("it forwards the control port to itself over USB", "-s AAA111 reverse tcp:28100 tcp:28100" in calls)
    check("a phone adb refuses is skipped, not fatal", "-s CCC333 reverse tcp:28100 tcp:28100" in calls and "CCC333" not in ok)
    usb_link.remove_reverse(28100, str(fake))
    check("the tunnel can be removed", "reverse --remove tcp:28100" in log.read_text())
    os.environ["PATH"] = str(tmp / "nothing")
    check("without adb the USB link is simply unavailable", usb_link.adb_path() is None and usb_link.devices() == [] and usb_link.ensure_reverse(28100) == [])
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
