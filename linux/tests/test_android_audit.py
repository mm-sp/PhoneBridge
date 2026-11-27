#!/usr/bin/env python3
"""The Android security log (AuditChain) and permission mapping (PcAccess) run on a JVM; a separate Python verifier
re-checks the log's hashes and Ed25519 signatures, so the format is proven interoperable and tamper-evident.
Run: `python3 tests/test_android_audit.py` (skips without java/kotlinc/bcprov/org.json/android.jar)"""
import base64
import glob
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
HERE = Path(__file__).resolve().parent
ANDROID = HERE.parents[1] / "android/app/src/main/java/dev/phonebridge"
PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def python_verify(path: Path, pub: str):
    """Independent re-implementation of the log check (no Android code involved)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    prev, n = None, 0
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        if hashlib.sha256(rec["body"].encode()).hexdigest() != rec["hash"]:
            return False, n
        body = json.loads(rec["body"])
        if prev is not None and body["prev"] != prev:
            return False, n
        Ed25519PublicKey.from_public_bytes(base64.b64decode(body["pub"])).verify(base64.b64decode(rec["sig"]), rec["hash"].encode())
        prev, n = rec["hash"], n + 1
    return True, n


def main():
    java = subprocess.run(["which", "java"], capture_output=True, text=True).stdout.strip()
    libs = sorted(l for l in glob.glob(os.path.expanduser("~/.gradle/wrapper/dists/gradle-*/*/gradle-*/lib"))
                  if glob.glob(f"{l}/kotlin-stdlib-2*.jar"))
    bc = [j for j in sorted(glob.glob(os.path.expanduser("~/.gradle/caches/modules-2/files-2.1/org.bouncycastle/bcprov-jdk18on/*/*/bcprov-jdk18on-*.jar"))) if "sources" not in j]
    js = sorted(glob.glob(os.path.expanduser("~/.gradle/caches/modules-2/files-2.1/org.json/json/*/*/json-*.jar")))
    sdk = sorted(glob.glob(os.path.expanduser("~/Android/Sdk/platforms/android-*/android.jar")))
    if not (java and libs and bc and js and sdk):
        print("SKIP: need java, Gradle's Kotlin compiler, bcprov, org.json and an Android SDK android.jar")
        return 0
    lib = libs[0]
    jars = lambda *pats: [j for p in pats for j in sorted(glob.glob(f"{lib}/{p}"))]
    kcp = ":".join(jars("kotlin-*.jar", "trove4j*.jar", "annotations*.jar", "kotlinx-coroutines-core-jvm*.jar"))
    stdlib = jars("kotlin-stdlib-2*.jar")[0]
    run_cp = f"{stdlib}:{bc[-1]}:{js[-1]}"
    with tempfile.TemporaryDirectory() as tmp:
        srcs = [str(ANDROID / n) for n in ("PhoneAudit.kt", "PcAccess.kt", "SecureSession.kt", "Crypto.kt", "Privacy.kt")]
        # PhoneAudit/PcAccess reference Store and BridgeService; v4/Stubs.kt stands in for them (never called here).
        r = subprocess.run([java, "-cp", kcp, "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler", "-no-stdlib", "-no-reflect",
                            "-cp", f"{run_cp}:{sdk[-1]}", "-d", f"{tmp}/out", *srcs, str(HERE / "v4/Stubs.kt"),
                            str(HERE / "v4/AuditHarness.kt"), str(HERE / "v4/android/util/Base64.kt")], capture_output=True, text=True)
        if r.returncode:
            print("compile output (tail):", (r.stdout + r.stderr)[-1500:])
            check("Android audit/permission sources compile on the JVM", False)
            return 1
        check("Android PhoneAudit.kt / PcAccess.kt compile", True)
        work = Path(tmp) / "work"
        p = subprocess.run([java, "-cp", f"{tmp}/out:{run_cp}", "AuditHarnessKt", str(work)], capture_output=True, text=True, timeout=60)
        if p.returncode:
            print(p.stdout[-800:], p.stderr[-800:])
        out = [ln.split(" ", 1) for ln in p.stdout.splitlines() if " " in ln]
        d = {k: v for k, v in out if k != "FEATURE"}
        feats = dict(v.split(" ", 1) for k, v in out if k == "FEATURE")
        check("the harness ran", p.returncode == 0 and "CLEAN" in d)
        print("1. audit chain")
        check("a clean log verifies on Android (5 records)", d.get("CLEAN") == "true 5")
        ok, n = python_verify(work / "audit.log", d["PUB"])
        check("a SEPARATE Python verifier also accepts it (hashes and Ed25519 signatures)", ok and n == 5)
        check("editing a record is detected", d.get("TAMPER_EDIT") == "false")
        check("deleting a record is detected", d.get("TAMPER_DELETE") == "false")
        check("reordering records is detected", d.get("TAMPER_REORDER") == "false")
        check("a swapped signature is detected", d.get("TAMPER_SIG") == "false")
        print("2a. privacy: what counts as a sensitive notification")
        sens = {int(k): v for k, v in (ln.split(" ")[1:3] for ln in p.stdout.splitlines() if ln.startswith("SENSITIVE"))}
        labels = ["verification code message", "Google-style verification code", "OTP for a transfer", "password reset mail",
                  "authenticator app", "password manager app"]
        for i, label in enumerate(labels):
            check(f"sensitive: {label}", sens.get(i) == "true")
        for i, label in zip(range(6, 10), ["'Lunch at 1?'", "an address containing digits", "an order number", "plain chat text"]):
            check(f"not sensitive: {label}", sens.get(i) == "false")
        print("2. permission mapping")
        expect = {"file.offer": "files", "files.get": "files", "photos.list": "photos", "sms.send": "sms", "sms.list_threads": "sms",
                  "call.dial": "calls", "contacts.list_request": "calls", "mirror.start": "screen", "webcam.start": "screen",
                  "mirror.input": "remote_control", "hotspot.request": "remote_control", "lock": "remote_control",
                  "apps.launch": "apps", "clipboard.update": "clipboard", "notifications.sync": "notifications"}
        expect.update({"sms.send": "sms_send", "sms.send_mms": "sms_send", "call.dial": "call_dial", "call.action": "calls",
                       "sms.get_thread": "sms"})
        for t, f in expect.items():
            check(f"{t} is gated as '{f}'", feats.get(t) == f)
        for t in ("ping", "device.info_request", "relay.config", "link.disconnect", "rekey", "identity.rotate", "session.end"):
            check(f"{t} (housekeeping) needs no permission", feats.get(t) == "null")
        for t in ("mirror.stop", "webcam.stop"):
            check(f"{t} is never blocked (it only takes access away)", feats.get(t) == "null")
        check("webcam.switch is gated as 'screen'", feats.get("webcam.switch") == "screen")
        print("3. what 'Ask' remembers for one connection")
        check("a yes is remembered", d.get("ANSWER_YES") == "true")
        check("a no is remembered", d.get("ANSWER_NO") == "false")
        check("no answer is not remembered", d.get("ANSWER_NONE") == "null")
        check("sending messages and placing calls ask every time", d.get("ANSWER_EVERY_TIME") == "null null")
        check("a new connection starts with nothing remembered", d.get("ANSWER_OTHER_SESSION") == "null null")
        print("4. requests that arrive while a prompt is open, and what a refusal says")
        check("reads the PC sends in bursts can wait for the answer", d.get("CAN_WAIT") == "true")
        check("sends, calls, input and other actions never wait", d.get("MUST_NOT_WAIT") == "true")
        check("without an open prompt nothing is held", d.get("WAIT_NO_PROMPT") == "false false")
        check("an open prompt holds requests, up to its limit", d.get("WAIT_HELD") == "true true false")
        check("…only for its own connection and permission", d.get("WAIT_OTHER_KEY") == "false")
        check("the answer releases them in order", d.get("WAIT_RELEASED") == "a,b")
        check("…and nothing is held afterwards", d.get("WAIT_CLOSED") == "false 0")
        check("a refused offer is named by its transfer id, not its file name",
              d.get("DENIAL_OFFER") == "feature,transfer_id,type t1")
        check("a refusal never repeats a number or message text", d.get("DENIAL_SMS") == "feature,type")
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
