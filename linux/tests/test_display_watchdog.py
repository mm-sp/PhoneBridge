#!/usr/bin/env python3
"""The display watchdog: if the PC app dies with the phone panel powered off, a separate process restores it.
Logic with fakes, plus a real end-to-end run (real process killed with SIGKILL, stub adb). Run: python3 tests/test_display_watchdog.py"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge.screensync import display_watchdog as W  # noqa: E402
from phonebridge.screensync.adb_bridge import Adb  # noqa: E402
from phonebridge.screensync.display_power import DisplayPowerManager, Pending  # noqa: E402
from phonebridge.screensync.settings import SmartScreenMode  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label} {extra}")


tmp = Path(tempfile.mkdtemp())


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


def fake_adb(log, display_on=True, reset_rc=0):
    def runner(args, timeout):
        log.append(args)
        if "power-reset" in args:
            return reset_rc, ""
        if "display" in args and "dumpsys" in args:
            return 0, "  mState=ON\n" if display_on else "  mState=OFF\n"
        if "power" in args and "dumpsys" in args:
            return 0, "mWakefulness=Awake"
        return 0, ""
    return Adb(runner=runner, adb_path="adb")


def write_marker(path, pid, serial="SER1", ts=100.0):
    path.write_text(json.dumps({"strategy": "adb", "serial": serial, "pid": pid, "ts": ts}))


print("A. logic")
m = tmp / "a.json"
clk = FakeClock()
check("no marker -> nothing to do", W.watch(m, fake_adb([]), alive=lambda p: True, sleep=clk.sleep, clock=clk) == "cleared")

write_marker(m, 4242)
calls = {"n": 0}
def alive_then_cleared(pid):
    calls["n"] += 1
    if calls["n"] == 3:
        m.unlink()
    return True
log = []
check("owner alive, then the app restores normally (marker cleared) -> watchdog leaves quietly",
      W.watch(m, fake_adb(log), alive=alive_then_cleared, sleep=clk.sleep, clock=clk) == "cleared" and not any("power-reset" in a for a in log))

write_marker(m, 4242, ts=1.0)
n = {"c": 0}
def alive_then_replaced(pid):
    n["c"] += 1
    if n["c"] == 2:
        write_marker(m, 4242, ts=2.0)   # a newer off-cycle
    return True
log = []
check("marker replaced by a newer off-cycle -> this watchdog stands down (the new one has its own)",
      W.watch(m, fake_adb(log), alive=alive_then_replaced, sleep=clk.sleep, clock=clk) == "replaced" and not any("power-reset" in a for a in log))

write_marker(m, 4242)
log = []
r = W.watch(m, fake_adb(log), alive=lambda p: False, sleep=clk.sleep, clock=clk)
check("owner dead -> power-reset sent to the right device and marker removed", r == "restored" and not m.exists()
      and any("power-reset" in a and "SER1" in a for a in log), f"({r})")

write_marker(m, 4242)
log = []
c3 = FakeClock()
r = W.watch(m, fake_adb(log, display_on=False, reset_rc=1), alive=lambda p: False, sleep=c3.sleep, clock=c3)
check("adb never works -> gives up after retrying, marker kept so the next app start still recovers", r == "failed" and m.exists(), f"({r})")

write_marker(m, 4242, serial=None)
check("marker without a serial cannot be acted on", W.watch(m, fake_adb([]), alive=lambda p: False, sleep=clk.sleep, clock=clk) == "failed")

write_marker(m, 4242)
c2 = FakeClock()
def dead_after(pid, state={"n": 0}):
    state["n"] += 1
    return state["n"] < 3
def adb_reset_marker_swapped():
    log = []
    runner_adb = fake_adb(log)
    orig = runner_adb.shell
    def shell(serial, *a, **k):
        if "power-reset" in a:
            write_marker(m, 4242, ts=555.0)   # a newer owner wrote its marker while we were resetting
        return orig(serial, *a, **k)
    runner_adb.shell = shell
    return runner_adb
r = W.watch(m, adb_reset_marker_swapped(), alive=dead_after, sleep=c2.sleep, clock=c2)
check("never deletes a marker that a newer owner wrote meanwhile", r == "restored" and m.exists() and json.loads(m.read_text())["ts"] == 555.0)

print("B. pid_alive")
check("this process is alive", W.pid_alive(os.getpid()))
check("a pid that does not exist is not alive", not W.pid_alive(2 ** 22 + 12345))
z = subprocess.Popen(["sleep", "30"])
check("a running child is alive", W.pid_alive(z.pid))
os.kill(z.pid, signal.SIGKILL)
time.sleep(0.3)   # killed but NOT reaped -> a zombie
check("a killed-but-unreaped child (zombie) counts as dead", not W.pid_alive(z.pid))
z.wait()

print("C. DisplayPowerManager starts the watchdog only for a real (adb) screen-off")
class Strat:
    def __init__(self, name, real, ok=True):
        self.name, self.real_off, self.ok, self.serial = name, real, ok, "S"
    def probe(self):
        from phonebridge.screensync.display_power import StepResult
        return StepResult(True, "x")
    def off(self):
        from phonebridge.screensync.display_power import StepResult
        return StepResult(self.ok, "x")
    def restore(self):
        from phonebridge.screensync.display_power import StepResult
        return StepResult(True, "x")

def mgr(strat, watchdog):
    return DisplayPowerManager(strategies=[strat], mode=lambda: SmartScreenMode.ENABLED, auto_off=lambda: True,
                               pending=Pending(tmp / "p.json"), watchdog=watchdog)
started = []
mgr(Strat("adb", True), lambda: started.append(1)).request_off()
check("real screen-off -> watchdog started once", started == [1])
started.clear()
mgr(Strat("dim", False), lambda: started.append(1)).request_off()
check("dim overlay (not a real off) -> no watchdog", started == [])
started.clear()
mgr(Strat("adb", True, ok=False), lambda: started.append(1)).request_off()
check("failed screen-off -> no watchdog", started == [])
def boom(): raise RuntimeError("cannot spawn")
check("a watchdog that fails to start does not break the screen-off", mgr(Strat("adb", True), boom).request_off() is True)
(tmp / "p.json").unlink(missing_ok=True)

print("D. end to end: a real process is SIGKILLed while 'the panel is off'")
adb_log = tmp / "adb.log"
stub = tmp / "adb"
stub.write_text(f"""#!/bin/bash
echo "$@" >> {adb_log}
case "$*" in
  *"dumpsys display"*) echo "  mState=ON" ;;
  *"dumpsys power"*) echo "mWakefulness=Awake" ;;
esac
exit 0
""")
stub.chmod(0o755)
owner = subprocess.Popen(["sleep", "60"])
marker = tmp / "e2e.json"
write_marker(marker, owner.pid, serial="E2ESER", ts=time.time())
os.environ.pop("PHONEBRIDGE_NO_WATCHDOG", None)
check("spawn() starts a detached watchdog", W.spawn(marker, str(stub)))
time.sleep(3)
check("while the app is alive the watchdog does nothing", not adb_log.exists() and marker.exists())
os.kill(owner.pid, signal.SIGKILL)       # the app is killed hard; the zombie is reaped later on purpose
t0 = time.time()
while marker.exists() and time.time() - t0 < 15:
    time.sleep(0.3)
owner.wait()
log_text = adb_log.read_text() if adb_log.exists() else ""
check("after the kill the watchdog restored the panel by itself", "power-reset 0" in log_text and "-s E2ESER" in log_text, f"({time.time() - t0:.1f}s)")
check("and removed the marker", not marker.exists())

check("PHONEBRIDGE_NO_WATCHDOG=1 disables it", (os.environ.__setitem__("PHONEBRIDGE_NO_WATCHDOG", "1"), W.spawn(marker, str(stub)))[1] is False)
os.environ.pop("PHONEBRIDGE_NO_WATCHDOG", None)
check("no adb path -> no watchdog", W.spawn(marker, None) is False)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
