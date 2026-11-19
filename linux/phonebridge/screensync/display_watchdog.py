"""Safety net for a dark phone: restores the panel if the PC app dies while it is powered off.

The panel is switched off with adb (`cmd display power-off 0`). Only adb can undo that, and the phone app cannot, so a PC app
that is killed (crash, kill -9, power loss of the window manager...) would leave the phone dark until the next start of the PC
app found the restore marker (display_power.Pending). This tiny separate process closes that gap: it is started right after
the panel goes off, in its own session (so it outlives a hard kill of the app), and it

  * exits quietly as soon as the marker is gone or replaced (the normal restore path cleared it);
  * if the owner process is dead while the marker is still there, runs `power-reset` itself, wakes the phone, verifies the
    panel and removes the marker.

Run as:  python -m phonebridge.screensync.display_watchdog <marker.json> <adb-path>
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .adb_bridge import Adb

log = logging.getLogger("phonebridge.sync.watchdog")

POLL_S = 1.0
RETRY_FOR_S = 45.0     # adb may be briefly unreachable (phone re-enumerating USB); keep trying this long
IDLE_LIMIT_S = 6 * 3600  # a marker nobody ever clears must not keep a process alive forever


def pid_alive(pid: int) -> bool:
    """False for a missing pid and for a zombie (killed but not yet reaped by its parent)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[:1] != ["Z"]


def read_marker(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def restore_panel(adb: Adb, serial: str) -> bool:
    rc, _out = adb.shell(serial, "cmd", "display", "power-reset", "0")
    if adb.display_on(serial) is False or adb.interactive(serial) is False:
        adb.shell(serial, "input", "keyevent", "KEYCODE_WAKEUP")
    return rc == 0 and adb.display_on(serial) is not False


def watch(marker: Path, adb: Adb, *, alive: Callable[[int], bool] = pid_alive, sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic) -> str:
    """Returns why it stopped: "cleared" | "replaced" | "restored" | "failed" | "idle". Pure enough to unit-test."""
    first = read_marker(marker)
    if first is None or "pid" not in first:
        return "cleared"
    owner, stamp, serial = int(first["pid"]), first.get("ts"), first.get("serial")
    started = clock()
    while True:
        cur = read_marker(marker)
        if cur is None:
            return "cleared"                      # the app restored the panel itself
        if cur.get("ts") != stamp or cur.get("pid") != owner:
            return "replaced"                     # a newer off-cycle owns the marker (and has its own watchdog)
        if not alive(owner):
            break
        if clock() - started > IDLE_LIMIT_S:
            return "idle"
        sleep(POLL_S)
    if not serial:
        return "failed"
    log.warning("PC app (pid %s) died with the phone display off; restoring it", owner)
    deadline = clock() + RETRY_FOR_S
    while clock() < deadline:
        if restore_panel(adb, serial):
            cur = read_marker(marker)
            if cur is not None and cur.get("ts") == stamp:   # never delete a marker a newer owner just wrote
                try:
                    marker.unlink()
                except OSError:
                    pass
            return "restored"
        sleep(2.0)
    return "failed"                                # marker stays: the next PC app start still recovers it


def spawn(marker: Path, adb_path: str | None) -> bool:
    """Starts the watchdog detached from this process. Never raises: it is a safety net, not a requirement."""
    if not adb_path or os.environ.get("PHONEBRIDGE_NO_WATCHDOG") == "1":
        return False
    try:
        subprocess.Popen([sys.executable, "-m", "phonebridge.screensync.display_watchdog", str(marker), adb_path],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
        return True
    except OSError:
        log.exception("could not start the display watchdog")
        return False


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    result = watch(Path(argv[1]), Adb(adb_path=argv[2]))
    log.info("display watchdog finished: %s", result)
    return 0 if result in ("cleared", "replaced", "restored", "idle") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
