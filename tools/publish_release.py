#!/usr/bin/env python3
"""Maintainer tool: build, sign and stage a PhoneBridge release for any static web host.

  python3 tools/publish_release.py KEYFILE VERSION [--apk path/to/app-release.apk] [--out release/]

Builds the PC wheel from linux/, optionally includes the Android APK, signs a manifest with KEYFILE (an Ed25519 key made by
`sign_release.py keygen`), and writes everything to --out. Upload that directory's contents to an https location; users run
`python -m phonebridge.updates check https://your.host/phonebridge/` (after pinning your public key once).
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("keyfile"); ap.add_argument("version")
    ap.add_argument("--apk"); ap.add_argument("--out", default=str(ROOT / "release"))
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", tmp, str(ROOT / "linux")], check=True)
        wheels = list(Path(tmp).glob("phonebridge-*.whl"))
        if not wheels:
            print("could not build the wheel", file=sys.stderr); return 1
        artifacts = []
        for w in wheels:
            shutil.copy(w, out / w.name); artifacts.append(out / w.name)
    if a.apk:
        shutil.copy(a.apk, out / "phonebridge.apk"); artifacts.append(out / "phonebridge.apk")
    subprocess.run([sys.executable, str(ROOT / "tools/sign_release.py"), "sign", a.keyfile, a.version, *map(str, artifacts)], check=True)
    print(f"release {a.version} staged in {out}: " + ", ".join(p.name for p in artifacts) + ", manifest.json, manifest.sig")
    return 0


if __name__ == "__main__":
    sys.exit(main())
