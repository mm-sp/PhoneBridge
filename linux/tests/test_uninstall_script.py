#!/usr/bin/env python3
"""packaging/uninstall.sh removes what the installer made and nothing else, also when PHONEBRIDGE_HOME is a folder that
holds other files. Runs the real script against a temporary home. Run: python3 tests/test_uninstall_script.py"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "packaging/uninstall.sh"
PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def put(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    return path


def uninstall(home: Path, **extra) -> int:
    env = {k: v for k, v in os.environ.items() if k not in ("PHONEBRIDGE_HOME", "XDG_DATA_HOME", "XDG_BIN_HOME")}
    env.update(HOME=str(home), **{k: str(v) for k, v in extra.items()})
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60).returncode


def installed(home: Path, app_home: Path, data: Path) -> list[Path]:
    return [put(app_home / "venv/bin/phonebridge"), put(home / ".local/bin/phonebridge"),
            put(data / "applications/dev.phonebridge.Linux.desktop"),
            put(data / "icons/hicolor/scalable/apps/dev.phonebridge.Linux.svg")]


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    print("1. default locations")
    home = tmp / "a"
    data = home / ".local/share"
    made = installed(home, data / "phonebridge", data) + [put(data / "phonebridge/clipboard_history.json")]
    keep = [put(data / "other-app/file"), put(home / ".config/phonebridge/devices/x.json")]
    check("the script succeeds", uninstall(home) == 0)
    check("everything installed is gone, including the app's data folder",
          not any(p.exists() for p in made) and not (data / "phonebridge").exists())
    check("other files and the pairing records are kept", all(p.exists() for p in keep))

    print("2. PHONEBRIDGE_HOME is a folder with other things in it")
    home = tmp / "b"
    data = home / ".local/share"
    shared = home / "apps"
    made = installed(home, shared, data)
    keep = [put(shared / "my-notes.txt"), put(shared / "another-tool/run")]
    check("the script succeeds", uninstall(home, PHONEBRIDGE_HOME=shared) == 0)
    check("PhoneBridge's own files are gone", not any(p.exists() for p in made) and not (shared / "venv").exists())
    check("the user's other files in that folder are kept", all(p.exists() for p in keep))

    print("3. an empty custom folder, and a custom data folder")
    home = tmp / "c"
    data, custom = home / "xdg-data", home / "pb"
    made = installed(home, custom, data)
    check("the script succeeds", uninstall(home, PHONEBRIDGE_HOME=custom, XDG_DATA_HOME=data) == 0)
    check("the emptied folder is removed", not custom.exists() and not any(p.exists() for p in made))
    home = tmp / "d"
    data = home / "xdg-data"
    made = installed(home, home / ".local/share/phonebridge", data)     # install.sh ignores XDG_DATA_HOME for the venv
    check("the script succeeds", uninstall(home, XDG_DATA_HOME=data) == 0)
    check("the virtualenv is found where the installer put it", not any(p.exists() for p in made))
    check("running it again is harmless", uninstall(home, XDG_DATA_HOME=data) == 0)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
