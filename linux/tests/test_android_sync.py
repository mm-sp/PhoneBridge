#!/usr/bin/env python3
"""Compiles the Android SyncCore.kt on a plain JVM and (1) runs its unit checks, (2) has it validate commands made by the
Python side (valid and hostile), and (3) validates the commands it makes with the Python validator. Skips if the Kotlin
compiler / org.json jar are not in ~/.gradle (same requirement as test_android_interop.py).
Run: python3 tests/test_android_sync.py"""
import glob
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from phonebridge.screensync import commands as C  # noqa: E402

ANDROID = HERE.parents[1] / "android/app/src/main/java/dev/phonebridge"
PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def vectors(now):
    sid = "a" * 16
    def base(**kw):
        c = C.make(C.Cmd.STOP_MIRRORING, sid, C.Origin.USER, now_ms=lambda: now).to_payload()
        c.update(kw)
        return c
    def v(name, payload, expect, session=sid):
        return {"name": name, "payload": payload, "expect": expect, "session": session}
    cid = lambda n: f"{n:016d}"
    return [
        v("valid stop", base(command_id=cid(1)), "ok"),
        v("replayed id", base(command_id=cid(1)), "duplicate"),
        v("other session", base(command_id=cid(2), session_id="b" * 16), "wrong_session"),
        v("no active session", base(command_id=cid(3)), "wrong_session", session=None),
        v("open phone bridge (session starter)", base(command_id=cid(4), cmd="OPEN_PHONE_BRIDGE"), "ok", session=None),
        v("viewer cmd not accepted by phone", base(command_id=cid(5), cmd="OPEN_PHONE_BRIDGE_VIEWER"), "not_allowed_here"),
        v("unknown cmd", base(command_id=cid(6), cmd="WIPE"), "unknown_command"),
        v("stale", base(command_id=cid(7), ts=now - 10 * 60_000), "stale"),
        v("future", base(command_id=cid(8), ts=now + 10 * 60_000), "stale"),
        v("bad id chars", base(command_id="../../x../../x"), "malformed"),
        v("float ts", base(command_id=cid(9), ts=1.5), "malformed"),
        v("bool ts", base(command_id=cid(10), ts=True), "malformed"),
        v("bad origin", base(command_id=cid(11), origin="root"), "malformed"),
        v("display bad mode", base(command_id=cid(12), cmd="DISPLAY_OFF", args={"mode": "x"}), "bad_args"),
        v("display performed not bool", base(command_id=cid(13), cmd="DISPLAY_OFF", args={"performed": "yes"}), "bad_args"),
        v("status unknown field", base(command_id=cid(14), cmd="CONNECTION_STATUS", args={"evil": 1}), "bad_args"),
        v("status fps string", base(command_id=cid(15), cmd="CONNECTION_STATUS", args={"fps": "x"}), "bad_args"),
        v("status valid (any session)", base(command_id=cid(16), cmd="CONNECTION_STATUS", session_id="z" * 16,
                                             args={"state": "MIRROR_ACTIVE", "fps": 30, "app_open": True, "smart": "automatic", "dim_ok": True}), "ok"),
        v("MIRROR_READY valid", base(command_id=cid(17), cmd="MIRROR_READY", args={"fps": 29.5, "width": 720}), "ok"),
        v("args not object", base(command_id=cid(18), args=[1]), "bad_args"),
        v("missing cmd", {k: x for k, x in base(command_id=cid(19)).items() if k != "cmd"}, "malformed"),
    ]


def main():
    java = subprocess.run(["which", "java"], capture_output=True, text=True).stdout.strip()
    libs = sorted(l for l in glob.glob(os.path.expanduser("~/.gradle/wrapper/dists/gradle-*/*/gradle-*/lib"))
                  if glob.glob(f"{l}/kotlin-stdlib-2*.jar"))
    js = sorted(glob.glob(os.path.expanduser("~/.gradle/caches/modules-2/files-2.1/org.json/json/*/*/json-*.jar")))
    if not (java and libs and js):
        print("SKIP: need java, Gradle's Kotlin compiler and the org.json jar in ~/.gradle")
        return 0
    lib = libs[0]
    jars = lambda *pats: [j for p in pats for j in sorted(glob.glob(f"{lib}/{p}"))]
    kcp = ":".join(jars("kotlin-*.jar", "trove4j*.jar", "annotations*.jar", "kotlinx-coroutines-core-jvm*.jar"))
    stdlib = jars("kotlin-stdlib-2*.jar")[0]
    run_cp = f"{stdlib}:{[j for j in js if 'sources' not in j][-1]}"
    now = 1_700_000_000_000
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run([java, "-cp", kcp, "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler", "-no-stdlib", "-no-reflect", "-cp", run_cp,
                            "-d", f"{tmp}/out", str(ANDROID / "SyncCore.kt"), str(HERE / "sync/Harness.kt")], capture_output=True, text=True)
        if r.returncode:
            print(r.stdout, r.stderr)
            check("SyncCore.kt compiles on the JVM", False)
            print(f"\n{PASS} passed, {FAIL} failed")
            return 1
        check("Android SyncCore.kt compiles on the JVM", True)
        vec = Path(tmp) / "vec.json"
        out = Path(tmp) / "out.json"
        vec.write_text(json.dumps(vectors(now)))
        p = subprocess.run([java, "-cp", f"{tmp}/out:{run_cp}", "HarnessKt", str(vec), str(now), str(out)], capture_output=True, text=True, timeout=60)
        print(p.stdout.rstrip())
        if p.stderr.strip():
            print(p.stderr[-800:])
        kp = fk = 0
        for line in p.stdout.splitlines():
            if line.startswith("  ok"):
                kp += 1
            elif line.startswith(" FAIL"):
                fk += 1
        check("Kotlin unit + Python-vector checks all pass", p.returncode == 0 and fk == 0 and kp >= 40)
        # Kotlin -> Python
        v = C.CommandValidator("pc", clock_ms=lambda: now)
        made = json.loads(out.read_text())
        for item in made:
            verdict = v.validate(item["payload"], "k" * 16)
            check(f"Python accepts Kotlin-made {item['name']}", verdict.ok)
        check("Kotlin origin/cmd survive the round trip", any(i["payload"]["cmd"] == "OPEN_PHONE_BRIDGE_VIEWER" and i["payload"]["origin"] == "user" for i in made))
    print(f"\n{PASS} passed, {FAIL} failed (plus the Kotlin-side counts above)")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
