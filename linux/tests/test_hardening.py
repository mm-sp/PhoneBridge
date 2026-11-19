#!/usr/bin/env python3
"""Security hardening checks: replayed control frames are rejected, and PC file browsing/fetching
can't reach hidden paths (~/.ssh, ~/.config/phonebridge keys...) or escape ~ through symlinks.
Run: `python3 tests/test_hardening.py`"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def _fails_value(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


def raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


from phonebridge import crypto, pc_info  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402
from phonebridge.transport import DeviceConnection  # noqa: E402

conn = DeviceConnection.__new__(DeviceConnection)
conn._seen_seq, conn._max_seq = set(), 0
env = lambda n: Envelope("control", "ping", n, {})
conn._check_replay(env(1)); conn._check_replay(env(2))
check("fresh sequence numbers accepted", True)
check("exact replay rejected", raises(lambda: conn._check_replay(env(2))))
conn._check_replay(env(4)); conn._check_replay(env(3))
check("small reordering tolerated", True)
check("replay after reordering rejected", raises(lambda: conn._check_replay(env(3))))
conn._check_replay(env(1000))
check("very old seq rejected after the window moves", raises(lambda: conn._check_replay(env(5))))
check("non-integer seq rejected", raises(lambda: conn._check_replay(env("x"))))

key = os.urandom(32)
pc, phone = crypto.SessionCipher(key, role="pc"), crypto.SessionCipher(key)
def fails(fn):
    try:
        fn()
    except Exception:
        return True
    return False
check("phone opens what the PC sealed", phone.decrypt(pc.encrypt(b"hi")) == b"hi")
check("PC opens what the phone sealed", pc.decrypt(phone.encrypt(b"yo")) == b"yo")
check("PC frame reflected back at the PC is rejected", fails(lambda: pc.decrypt(pc.encrypt(b"x"))))
check("phone frame reflected back at the phone is rejected", fails(lambda: phone.decrypt(phone.encrypt(b"x"))))
check("a frame with no direction label (v2 format) is rejected",
      fails(lambda: phone.decrypt((n := os.urandom(12)) + crypto.AESGCM(key).encrypt(n, b"x", None))))

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp).resolve()
    (home / ".ssh").mkdir(); (home / ".ssh/id_rsa").write_text("secret")
    (home / "Docs").mkdir(); (home / "Docs/a.txt").write_text("hi")
    outside = Path(tempfile.mkdtemp()); (outside / "x.txt").write_text("out")
    (home / "link").symlink_to(outside)
    (home / "Docs/.hidden").write_text("h")
    pc_info.HOME = home
    check("normal file allowed", pc_info.resolve_home_file("Docs/a.txt") == home / "Docs/a.txt")
    check(".ssh key refused", pc_info.resolve_home_file(".ssh/id_rsa") is None)
    check("hidden file in visible dir refused", pc_info.resolve_home_file("Docs/.hidden") is None)
    check("../ escape refused", pc_info.resolve_home_file("../" + outside.name + "/x.txt") is None)
    check("symlink escape refused", pc_info.resolve_home_file("link/x.txt") is None)
    check("home itself is not a file", pc_info.resolve_home_file("") is None)
    check(".ssh listing refused", bool(pc_info.list_dir(".ssh").get("error")))
    check("symlink listing refused", bool(pc_info.list_dir("link").get("error")))
    check("normal listing works", [e["name"] for e in pc_info.list_dir("Docs")["entries"]] == ["a.txt"])

print("privacy settings")
import tempfile as _tf
from phonebridge import pairing as _pairing, privacy as _privacy
_old = _pairing.DEVICES_DIR
_pairing.DEVICES_DIR = Path(_tf.mkdtemp()) / "cfg" / "devices"
check("defaults: 2 min clipboard expiry, 24 h history", _privacy.get("clipboard_expiry_s") == 120 and _privacy.get("clipboard_history_hours") == 24)
_privacy.set_value("clipboard_expiry_s", 30)
check("a changed setting persists", _privacy.get("clipboard_expiry_s") == 30)
check("the settings file is private (0600)", ((_pairing.DEVICES_DIR.parent / "privacy.json").stat().st_mode & 0o777) == 0o600)
check("a bogus setting is refused", _fails_value(lambda: _privacy.set_value("nope", 1)) and _fails_value(lambda: _privacy.set_value("clipboard_expiry_s", -5)))
check("password-manager clipboard hint is on the never-forward list", "x-kde-passwordManagerHint" in _privacy.SENSITIVE_CLIPBOARD_MIMES)
from phonebridge import clipboard_watcher as _cw
check("the watcher process uses the same list", set(_cw.SENSITIVE_MIMES) == set(_privacy.SENSITIVE_CLIPBOARD_MIMES))
_pairing.DEVICES_DIR = _old

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
