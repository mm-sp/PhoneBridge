#!/usr/bin/env python3
"""How the Android app tries its routes to the PC (ConnectAttempt.kt, compiled on a plain JVM): an unreachable route
moves on to the next, but the PC's own "not paired" / "busy with another phone" answer reaches the service. Also checks
that the service connects through that helper. Skips without the Kotlin toolchain.
Run: python3 tests/test_android_connect.py"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _kotlin as K  # noqa: E402

ANDROID = K.ANDROID_SRC
PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def main():
    try:
        tc = K.find()
    except K.ToolchainError as exc:
        print(f"SKIP: {exc}")
        return 0
    with K.Workdir() as tmp:
        built = K.compile(tc, [ANDROID / "ConnectAttempt.kt", HERE / "connect/Harness.kt"], Path(tmp) / "out")
        check("Android ConnectAttempt.kt compiles on the JVM", built.ok)
        if not built.ok:
            print((built.stdout + built.stderr)[-1500:])
        else:
            ran = K.run(tc, Path(tmp) / "out", "HarnessKt")
            d = dict(line.split(" ", 1) for line in ran.stdout.splitlines() if " " in line)
            check("the harness ran", ran.ok)
            check("a route that works is used", d.get("SUCCESS") == "returned link")
            check("an unreachable route is just a failed route", d.get("UNREACHABLE") == "returned null")
            check("…and so is a failed handshake", d.get("BAD_HANDSHAKE") == "returned null")
            check("ordinary failures are reported to the caller", d.get("REPORTED") == "refused,handshake")
            check("'not paired' is passed on", d.get("UNPAIRED") == "unpaired")
            check("'busy' is passed on with the other phone's name", d.get("BUSY") == "busy Other Phone")
            check("after 'not paired' no further route is tried", d.get("ROUTES_UNPAIRED") == "usb,wifi UnpairedException")
            check("after 'busy' no further route is tried", d.get("ROUTES_BUSY") == "usb,wifi BusyException")

    # The service itself needs Android to run, so check its source: every connection attempt goes through the helper.
    service = (ANDROID / "BridgeService.kt").read_text(encoding="utf-8")
    body = re.search(r"private fun tryOpen\(.*?\n    }\n", service, re.S)
    check("tryOpen exists", body is not None)
    text = body.group(0) if body else ""
    check("every Connection.open in tryOpen goes through tryRoute (no catch-all can swallow the PC's answer)",
          text.count("Connection.open(") >= 4 and text.count("tryRoute") == text.count("Connection.open(") and "catch" not in text)
    loop = re.search(r"val c = try \{\s*tryOpen\(pc\).*?if \(c == null\)", service, re.S)
    check("the connection loop handles both answers from the connection attempt",
          loop is not None and "catch (e: BusyException)" in loop.group(0) and "catch (e: UnpairedException)" in loop.group(0))
    def kotlin_function(file: str, head: str) -> str:
        """The source of one function (4-space indented member), empty if it isn't there."""
        text = (ANDROID / file).read_text(encoding="utf-8")
        start = text.find(f"    {head}")
        end = text.find("\n    }\n", start)
        return text[start:end] if start >= 0 and end > start else ""

    # Privacy switches that guard code paths needing Android: check the guard comes before the data leaves.
    for file, head, guard in (
        ("InputService.kt", "private fun sendToPc(", "Clipboard.allowed("),
        ("InputService.kt", "fun copySelection()", "Clipboard.allowed("),
        ("NotificationRelay.kt", "override fun onNotificationRemoved(", "shareable("),
        ("NotificationRelay.kt", "private fun outgoing(", "shareable("),
    ):
        text = kotlin_function(file, head)
        send = text.find(".send(")
        check(f"{file} {head.split('(')[0].split()[-1]}: the privacy setting is checked before anything is sent",
              guard in text and (send < 0 or text.index(guard) < send))
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
