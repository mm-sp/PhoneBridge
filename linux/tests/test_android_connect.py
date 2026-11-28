#!/usr/bin/env python3
"""How the Android app tries its routes to the PC (ConnectAttempt.kt, compiled on a plain JVM): an unreachable route
moves on to the next, but the PC's own "not paired" / "busy with another phone" answer reaches the service. Also checks
that the service connects through that helper, which partial file each incoming transfer writes (PartFiles.kt), which
screen-capture prompt still counts (ConsentPrompt.kt), and some privacy guards in the sources. Skips the JVM checks without the Kotlin toolchain.
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

        # Partial files of PC -> phone transfers (PartFiles.kt): no two running transfers ever write the same one.
        built = K.compile(tc, [ANDROID / "PartFiles.kt", HERE / "connect/PartsHarness.kt"], Path(tmp) / "parts")
        check("Android PartFiles.kt compiles on the JVM", built.ok)
        if not built.ok:
            print((built.stdout + built.stderr)[-1500:])
        else:
            ran = K.run(tc, Path(tmp) / "parts", "PartsHarnessKt")
            d = dict(line.split(" ", 1) for line in ran.stdout.splitlines() if " " in line)
            check("the parts harness ran", ran.ok)
            check("a transfer gets the resumable partial", d.get("FIRST") == ".pb-key1.part true null")
            check("the same file again meanwhile gets its own", d.get("TWIN") == ".pb-key1-t2.part false null")
            check("another file is unaffected", d.get("OTHER") == ".pb-key2.part true null")
            check("both may write theirs", d.get("BOTH_WRITE") == "yes,yes")
            check("the twin ending doesn't free the first one's partial",
                  d.get("TWIN_GONE") == ".pb-key1-t4.part false null")
            check("a retry after a reconnect takes the partial over and names the old transfer",
                  d.get("RETRY") == ".pb-key1.part true t1")
            check("…after which only the retry can write it", d.get("AFTER_TAKEOVER") == "no,yes")
            check("the old transfer ending doesn't free the retry's hold", d.get("OLD_RELEASE") == "true")
            check("a released partial can be resumed", d.get("FREE_AGAIN") == ".pb-key1.part true null")
            check("hostile ids stay inside the folder", d.get("HOSTILE") == "true .pb-k.part"
                  and d.get("HOSTILE_TWIN") == ".pb-k-xy.part")
            check("without a resume key the transfer id names the partial", d.get("NO_KEY") == ".pb-t7.part")
            check("a verified partial is set aside before saving, so a retry can't change it",
                  d.get("SEALED") == ".pb-k.part-a1.done first .pb-k.part")
            check("…and a transfer that was taken over can't set the retry's partial aside",
                  d.get("SEAL_TAKEN_OVER") == "no 14")
            check("saving never overwrites an earlier file",
                  d.get("SAVED") == "a.tar.gz=first|a.tar (1).gz=first|a.tar (2).gz=first"
                  and d.get("SAVED_PLAIN") == "notes=first|notes (1)=first")
            check("transfers finishing together get separate, whole files", d.get("TOGETHER") == "16 true")
            check("a failed copy leaves no file behind", d.get("FAILED_COPY") == "failed false")

        # Android's screen-capture prompt (ConsentPrompt.kt): only an answer nobody called off starts a capture.
        built = K.compile(tc, [ANDROID / "ConsentPrompt.kt", HERE / "connect/ConsentHarness.kt"], Path(tmp) / "consent")
        check("Android ConsentPrompt.kt compiles on the JVM", built.ok)
        if not built.ok:
            print((built.stdout + built.stderr)[-1500:])
        else:
            ran = K.run(tc, Path(tmp) / "consent", "ConsentHarnessKt")
            d = dict(line.split(" ", 1) for line in ran.stdout.splitlines() if " " in line)
            check("the consent harness ran", ran.ok)
            check("a second request doesn't stack a second prompt", d.get("OPEN") == "1 1 null")
            check("an answer counts once", d.get("ANSWER") == "true false")
            check("an older prompt's answer doesn't count", d.get("OLD") == "false true")
            check("a called-off prompt can't start a capture afterwards", d.get("CANCEL") == "true false false")
            check("…and the next request gets a new one", d.get("AFTER_CANCEL") == "2")
            check("a prompt left open too long may be replaced, and still counts until then",
                  d.get("TIMED_OUT") == "true false true")
            check("…unless it was called off", d.get("LATE_CANCEL") == "true false")
            check("an earlier prompt's timer leaves the new one alone", d.get("OLD_TIMER") == "false null")
            check("a replaced prompt's answer no longer counts", d.get("REPLACED") == "true 3 false")

    video = (ANDROID / "Video.kt").read_text(encoding="utf-8")

    def member(text: str, head: str) -> str:
        """The source of one 4-space indented function, empty if it isn't there."""
        start = text.find(f"    {head}")
        end = text.find("\n    }\n", start)
        return text[start:end] if start >= 0 and end > start else ""

    check("stopping the mirror calls off an open capture prompt", "consent.cancel()" in member(video, "fun stopMirror()"))
    result = member(video, "fun onProjectionResult(")
    check("a prompt's result is checked before any capture starts",
          "if (!consent.answer(gen))" in result and result.index("consent.answer(gen)") < result.index("startMirror("))
    start = member(video, "private fun startMirror(")
    check("a capture that fails to start leaves no encoder or projection running",
          "stream?.stop()" in start and "proj.stop()" in start)
    camera = member(video, "private fun openCamera()")
    check("a failing camera query is reported, not thrown on the main thread",
          0 <= camera.find("try {") < camera.find("cm.cameraIdList") < camera.find('refuse("Couldn\'t use'))

    # Late callbacks (an MMS result, the call log after a call) must not hit workers that shut down with the service.
    service = (ANDROID / "BridgeService.kt").read_text(encoding="utf-8")
    later = member(service, "fun later(")
    destroy = member(service, "override fun onDestroy()")
    check("work for a destroyed service is dropped instead of crashing the app",
          "RejectedExecutionException" in later and "later { send(type, payload) }" in member(service, "fun send(type: String"))
    check("destroying the service cancels its delayed callbacks", "main.removeCallbacksAndMessages(null)" in destroy)
    check("the call listener and notification relay submit work the same way",
          all("worker.execute" not in (ANDROID / name).read_text(encoding="utf-8")
              for name in ("Calls.kt", "NotificationRelay.kt")))

    relay = (ANDROID / "NotificationRelay.kt").read_text(encoding="utf-8")
    check("a notification reply that can't be delivered is reported to the PC",
          "getOrDefault(false)" in member(relay, "fun handleAction(") and 'send("error"' in member(relay, "fun handleAction(")
          and member(relay, "private fun reply(").count("return false") == 2)
    fetch = member(service, "private fun fetch(")
    check("a file or photo that can't be sent is reported with the request it belongs to, on its own link only",
          'fetch(c, JSONObject().put("source_path"' in service and 'fetch(c, JSONObject().put("photo_id"' in service
          and 'if (c === conn) send("error", about.put("message", it))' in fetch)
    media = (ANDROID / "Media.kt").read_text(encoding="utf-8")
    sends = media[media.find("    fun sendPhoto("):media.find("\nobject Files")] + member(media, "fun sendFile(")
    check("…and neither send gives up silently", "?: return" not in sends and "return\n" not in sends)

    # The phone's PC-control tabs (Android views, checked in the source): a refusal is shown, never left "Loading…".
    panels = (ANDROID / "PcPanels.kt").read_text(encoding="utf-8")
    sections = {m.group(1): m.group(0) for m in
                re.finditer(r"^class (\w+Panel)\(.*?(?=^class |\Z)", panels, re.S | re.M)}
    for name in ("DevicePanel", "FilesPanel", "ScreenPanel", "AppsPanel", "PortsPanel", "ServersPanel", "TerminalPanel"):
        check(f"{name} shows why the PC refused", "override fun onDenied(" in sections.get(name, ""))
    route = member((ANDROID / "MainActivity.kt").read_text(encoding="utf-8"), "private fun routePcMessage(")
    check("a refusal and an approval from the PC reach the open tab",
          "current?.onDenied(" in route and 'p.optBoolean("approved")' in route and "current?.onShow()" in route)

    receive = (ANDROID / "Media.kt").read_text(encoding="utf-8")
    receive = receive[receive.find("    fun receive(svc: BridgeService"):receive.find("\nobject Apps")]
    check("receiving writes the partial only while holding it, and releases it",
          receive.count("out.write(") == 1 and "parts.whileHeld(claim) { out.write(chunk) }" in receive
          and "parts.release(claim)" in receive and '".pb-' not in receive)
    check("receiving saves a set-aside copy under a reserved name",
          "parts.seal(claim)" in receive and "PartFiles.saveCopy(verified, destDir, name)" in receive
          and "part.copyTo(" not in receive)

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
    check("pairing anew ends the link to the previously paired PC",
          "later { dropIfRepaired() }" in service and "!= connectedTo) c.endSession(" in service)
    repaired = service.find("if (!stillPaired(pc))")
    check("…also one that was still being set up, and its address never lands in the new PC's record",
          0 <= repaired < service.find("connectedTo = pc.fingerprint")
          and "if (stillPaired(pc)) Store.updateLan(" in text)

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
