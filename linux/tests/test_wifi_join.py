#!/usr/bin/env python3
"""Joining the phone's hotspot must never put the Wi-Fi password on a command line (visible in `ps` to every local user).
Logic with fakes, plus a real NetworkManager round trip with a throwaway profile (skipped without NetworkManager/nmcli).
Run: python3 tests/test_wifi_join.py"""
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import wifi_join as W  # noqa: E402

PASS = FAIL = 0
SECRET = "pb-Secret-Pa55 word!"


def check(label, cond, extra=""):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label} {extra}")


print("A. logic")
s = W.connection_settings("Hotspot-1", SECRET)
check("WPA-PSK profile with the ssid as bytes", s["802-11-wireless-security"]["key-mgmt"] == "wpa-psk"
      and s["802-11-wireless"]["ssid"] == b"Hotspot-1" and s["802-11-wireless-security"]["psk"] == SECRET)
check("does not auto-connect later on its own", s["connection"]["autoconnect"] is False)

argvs = []
def run_ok(args, timeout):
    argvs.append(list(args))
    return 0, ""
added = []
ok, msg = W.join("Hotspot-1", SECRET, run=run_ok, add=lambda st: added.append(st) or "/p/1")
check("join succeeds when the profile is created and activated", ok and "Hotspot-1" in msg, f"({msg})")
check("the password is NOT in any command-line argument", not any(SECRET in a or SECRET in " ".join(av) for av in argvs for a in av))
check("the password reached NetworkManager through the D-Bus call instead", added and added[0]["802-11-wireless-security"]["psk"] == SECRET)
check("an older profile of the same name is replaced first", argvs[0][:4] == ["nmcli", "connection", "delete", "id"])
check("activation uses `connection up id <name>`", argvs[-1] == ["nmcli", "connection", "up", "id", W.PROFILE_NAME])

def add_fails(st): raise RuntimeError("org.freedesktop.NetworkManager is not running")
ok, msg = W.join("Hotspot-1", SECRET, run=run_ok, add=add_fails)
check("D-Bus failure is reported, not raised, and does not leak the secret", not ok and SECRET not in msg, f"({msg})")

def run_up_fails(args, timeout):
    return (4, "Error: Connection activation failed: No network with SSID 'Hotspot-1' found.") if "up" in args else (0, "")
ok, msg = W.join("Hotspot-1", SECRET, run=run_up_fails, add=lambda st: "/p/1")
check("activation failure gives a readable reason", not ok and "No network" in msg, f"({msg})")
check("empty ssid is refused", W.join("", SECRET, run=run_ok, add=lambda st: "/p")[0] is False)

print("B. real NetworkManager (throwaway profile)")
NAME = "pbtest-delete-me"
if not shutil.which("nmcli") or subprocess.run(["nmcli", "-t", "general", "status"], capture_output=True).returncode != 0:
    print("  SKIP: no NetworkManager here")
else:
    subprocess.run(["nmcli", "connection", "delete", "id", NAME], capture_output=True)
    try:
        path = W.add_profile(W.connection_settings("pbtest-nonexistent-ssid", SECRET, NAME))
        check("profile created over D-Bus", path.startswith("/org/freedesktop/NetworkManager/Settings/"), f"({path})")
        psk = subprocess.run(["nmcli", "-s", "-g", "802-11-wireless-security.psk", "connection", "show", "id", NAME],
                             capture_output=True, text=True).stdout.strip()
        check("the stored password is exactly what was sent", psk == SECRET)
        ssid = subprocess.run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", "id", NAME], capture_output=True, text=True).stdout.strip()
        check("the stored network name is right", ssid == "pbtest-nonexistent-ssid", f"({ssid})")
    except Exception as exc:  # noqa: BLE001
        check("profile created over D-Bus", False, f"({exc})")
    finally:
        subprocess.run(["nmcli", "connection", "delete", "id", NAME], capture_output=True)
    gone = subprocess.run(["nmcli", "-g", "NAME", "connection", "show"], capture_output=True, text=True).stdout
    check("the throwaway profile was cleaned up", NAME not in gone.splitlines())

    seen = []
    def spy_run(args, timeout):
        seen.append(list(args))
        return W._run(args, timeout)
    ok, msg = W.join("pbtest-nonexistent-ssid", SECRET, run=spy_run, name=NAME)
    subprocess.run(["nmcli", "connection", "delete", "id", NAME], capture_output=True)
    check("a real join to a network that does not exist fails cleanly", ok is False and bool(msg), f"({msg})")
    check("and the password never appeared in any nmcli argument", not any(SECRET in a for av in seen for a in av))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
