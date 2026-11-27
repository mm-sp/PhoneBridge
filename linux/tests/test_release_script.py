#!/usr/bin/env python3
"""tools/release.sh must stop when the build fails instead of packaging what an earlier build left behind. A copy of the
script runs in a temporary tree with stand-ins for Gradle and the Android tools: nothing is built or signed, and the
real repository is not touched. Run: python3 tests/test_release_script.py"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "tools/release.sh"
PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def tool(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def release(tmp: Path, gradle_body: str):
    """A fresh tree holding the artifacts of an "earlier build", released with a Gradle stand-in."""
    root = tmp / "tree"
    shutil.rmtree(root, ignore_errors=True)
    (root / "tools").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "tools/release.sh")
    (root / "android/app").mkdir(parents=True)
    (root / "android/app/build.gradle.kts").write_text('    versionName = "9.9.9"\n')
    for name in ("apk/release/app-release.apk", "bundle/release/app-release.aab", "apk/debug/app-debug.apk",
                 "mapping/release/mapping.txt"):
        stale = root / "android/app/build/outputs" / name
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("left over from an earlier build")
    tool(tmp / "bin/gradle", gradle_body)
    tool(tmp / "bin/keytool", 'echo "Owner: test"; echo "SHA256: 00"')
    tool(tmp / "sdk/build-tools/1.0.0/apksigner", 'echo "Verifies"')
    tool(tmp / "sdk/build-tools/1.0.0/aapt2", 'echo "package: test"')
    env = dict(os.environ, GRADLE=str(tmp / "bin/gradle"), ANDROID_HOME=str(tmp / "sdk"), HOME=str(tmp / "home"),
               PHONEBRIDGE_KEYSTORE="unused-by-the-stand-ins", PATH=f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}")
    run = subprocess.run(["bash", str(root / "tools/release.sh")], capture_output=True, text=True, env=env, timeout=60)
    return run, root / "dist"


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    run, dist = release(tmp, 'echo "w: a warning"; echo "FAILURE: Build failed" >&2; exit 1')
    check("a failed build makes the release fail", run.returncode != 0)
    check("…shows why", "Build failed" in run.stdout)
    check("…and packages nothing", not dist.exists())
    run, dist = release(tmp, "exit 3")
    check("a build that fails silently also stops it", run.returncode != 0 and not dist.exists())
    for what, body in (("with only filtered warnings", 'echo "w: a warning"'), ("with no output at all", "true")):
        run, dist = release(tmp, body)
        made = sorted(p.name for p in (dist / "9.9.9").glob("*")) if dist.exists() else []
        check(f"a successful build {what} is packaged and checksummed",
              run.returncode == 0 and "phonebridge-9.9.9.apk" in made and "SHA256SUMS" in made)
        if run.returncode:
            print(run.stdout[-500:], run.stderr[-500:])
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
