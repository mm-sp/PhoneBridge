#!/usr/bin/env python3
"""The phone app's "control this PC" requests, answered by the real PcControl over the
real TLS control channel: PC info, ports/network, apps, home-folder listing and a file
download. (Mouse/keyboard/screen need the desktop's RemoteDesktop portal and a human
clicking "Allow", so they're not covered here.)
Run directly: `python3 tests/test_pc_control.py`
"""
import asyncio
import os
import ssl
import time
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import crypto, pairing, pc_info, tls_identity, transport  # noqa: E402
from phonebridge.pc_control import PcControl  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402

PASS = 0
FAIL = 0
PORT = 28195


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


async def main_async() -> int:
    tmp = Path(tempfile.mkdtemp())
    pairing.DEVICES_DIR = tmp / "devices"
    tls_identity.CONFIG_DIR, tls_identity.CERT_PATH, tls_identity.KEY_PATH = tmp, tmp / "c.pem", tmp / "k.pem"
    transport.DISCOVERY_PORT = 28196
    home = tmp / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "notes.txt").write_text("hello from the PC")
    pc_info.HOME = home
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _phone import new_identity, phone_connect
    ident = new_identity()
    phone = pairing.PairedDevice("cafe", "Test Phone", os.urandom(32), allow_pc_control=True, pc_control_until=time.time() + 3600, identity_pub=ident.public_b64)
    phone.save()

    server = transport.ControlChannelServer(port=PORT)
    await server.start()
    pc = PcControl(server, asyncio.get_running_loop(), clipboard_text=lambda: "")
    serve = asyncio.ensure_future(server.serve_forever())

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT, ssl=ctx)
    session = await phone_connect(reader, writer, "cafe", phone.session_key, ident, server.cert_fingerprint)
    cipher = session.cipher
    seq = [0]

    async def ask(type_: str, payload: dict | None = None) -> Envelope:
        seq[0] += 1
        writer.write(crypto.pack_frame(cipher.encrypt(Envelope("control", type_, seq[0], payload or {}).to_json())))
        await writer.drain()
        return await recv()

    async def recv_raw() -> bytes:
        n = crypto.unpack_frame_len(await reader.readexactly(4))
        return cipher.decrypt(await reader.readexactly(n))

    async def recv() -> Envelope:
        return Envelope.from_json(await asyncio.wait_for(recv_raw(), 20))

    print("0. PC control is refused while it is switched off for the phone")
    phone.allow_pc_control = False
    phone.save()
    denied = await ask("pc.info_request")
    check("denied with a pc.status message when allow_pc_control is off",
          denied.type == "pc.status" and denied.payload.get("denied") is True)
    phone.allow_pc_control = True
    phone.pc_control_until = None
    phone.save()
    denied = await ask("pc.info_request")
    check("a grant without a deadline (older versions) is treated as expired",
          denied.type == "pc.status" and denied.payload.get("denied") is True)
    phone.pc_control_until = time.time() + 3600
    phone.save()
    phone.pc_control_until = time.time() - 1
    phone.save()
    denied = await ask("pc.info_request")
    check("an expired grant is refused on the very next message",
          denied.type == "pc.status" and denied.payload.get("denied") is True)
    check("pc_control_active() is False after the deadline", not phone.pc_control_active())
    phone.pc_control_until = time.time() + 3600
    phone.save()

    print("1. device info")
    info = await ask("pc.info_request")
    check("pc.info has hostname, OS, RAM, disk", info.type == "pc.info" and
          all(info.payload.get(k) for k in ("hostname", "os", "ram_total", "disk_total")))

    print("2. ports + network")
    ports = await ask("pc.ports_request")
    check("pc.ports lists ports and interfaces", ports.type == "pc.ports" and "ports" in ports.payload
          and isinstance(ports.payload.get("interfaces"), list))
    check("PhoneBridge's own port 28195 is listed", any(p["port"] == PORT for p in ports.payload["ports"]))

    print("3. apps")
    apps = await ask("pc.apps_request")
    check("pc.apps returns a list", apps.type == "pc.apps" and isinstance(apps.payload.get("apps"), list))

    print("4. home-folder files")
    root = await ask("pcfiles.list", {"path": "/"})
    check("root lists Documents", [e["name"] for e in root.payload["entries"]] == ["Documents"])
    docs = await ask("pcfiles.list", {"path": "/Documents"})
    check("Documents lists notes.txt", docs.payload["entries"][0]["name"] == "notes.txt")
    outside = await ask("pcfiles.list", {"path": "/../../etc"})
    check("paths outside home are refused", bool(outside.payload.get("error")))

    print("5. download a file to the phone")
    offer = await ask("pcfiles.get", {"path": "/Documents/notes.txt"})
    check("file.offer for notes.txt", offer.type == "file.offer" and offer.payload["filename"] == "notes.txt")
    check("offer announces a separate data connection with a resume key",
          offer.payload.get("data_conn") is True and bool(offer.payload.get("resume_key")))

    print("7. the expiry sweep clears an expired grant, records it and ends a live screen view")
    import types
    from phonebridge import pc_control as pcmod

    events = []
    stops = []
    real_audit, real_idle = server.audit, pcmod.GLib.idle_add
    server.audit = lambda event, device=None, **meta: events.append((event, device, meta))
    pcmod.GLib.idle_add = lambda fn, *a: stops.append(fn)
    other = pairing.PairedDevice("beef", "Other Phone", os.urandom(32), allow_pc_control=True,
                                 pc_control_until=time.time() + 3600, identity_pub=ident.public_b64)
    other.save()
    phone.allow_pc_control, phone.pc_control_until = True, time.time() + 1
    phone.save()
    phone.pc_control_until = time.time() - 1
    phone.save()
    pc._stream_conn = types.SimpleNamespace(fingerprint="beef")      # a view of the phone whose grant is still valid
    pc._sweep_expired()
    other_untouched = not stops
    pc._stream_conn = types.SimpleNamespace(fingerprint="cafe")      # a view of the phone whose grant just expired
    pc._sweep_expired()
    stored = pairing.PairedDevice.load("cafe")
    expired_events = [e for e in events if e[0] == "permission.changed" and e[2].get("action") == "pc_control.expired"]
    parts = {"switch cleared": stored.allow_pc_control is False and stored.pc_control_until is None,
             "one expired record": len(expired_events) == 1 and expired_events[0][1] == "cafe",
             "gate cache no longer allows the phone": not pc._pc_control_allowed("cafe"),
             "screen view of the expired phone stopped once": stops == [pc._stop_screen],
             "other phone's view not stopped": other_untouched}
    check("sweep clears the expired grant, records pc_control.expired once and stops only that phone's screen view", all(parts.values()))
    for k, v in parts.items():
        if not v:
            print("       failed part:", k)
    pc._stream_conn = None
    server.audit, pcmod.GLib.idle_add = real_audit, real_idle
    (pairing.DEVICES_DIR / "beef.json").unlink()

    print("8. a phone cannot enable PC control by itself: only the answer on the PC does")
    from phonebridge.security import permissions as P

    async def next_status(timeout=10):
        while True:
            env = await asyncio.wait_for(recv(), timeout)
            if env.type == "pc.status":
                return env

    async def drain():
        try:
            while True:
                await asyncio.wait_for(recv(), 0.5)
        except asyncio.TimeoutError:
            pass

    await drain()
    phone.allow_pc_control, phone.pc_control_until = False, None
    phone.save()
    pc._gate_cache.clear()
    pc._approval_cooldown.clear()
    check("no approval callback: request stays refused, nothing is enabled",
          (await ask("pc.info_request")).payload.get("denied") is True and not pairing.PairedDevice.load("cafe").pc_control_active())

    prompts = []

    async def grant(minutes):
        async def cb(device):
            prompts.append(device.name)
            return minutes
        return cb

    pc.approval_callback = await grant(15)
    first = await ask("pc.info_request")
    check("while a prompt is open the phone is told to wait", first.payload.get("denied") is True and "approval" in first.payload["message"].lower())
    done = await next_status()
    check("approval on the PC enables control", done.payload.get("ok") is True and prompts == ["Test Phone"])
    left = pairing.PairedDevice.load("cafe").pc_control_until - time.time()
    check("granted for the chosen 15 minutes", 14 * 60 < left <= 15 * 60 + 5)
    check("requests now work", (await ask("pc.info_request")).type == "pc.info")

    P.set_mode(pairing.PairedDevice.load("cafe"), "remote_control", P.DENY)
    check("revoking is immediate (very next message refused)", (await ask("pc.info_request")).payload.get("denied") is True)
    await drain()
    check("a revoked phone cannot raise a new prompt straight away", prompts == ["Test Phone"] and not pairing.PairedDevice.load("cafe").pc_control_active())

    pc._approval_cooldown.clear()
    pc.approval_callback = await grant(9999)
    await ask("pc.info_request")
    await next_status()
    left = pairing.PairedDevice.load("cafe").pc_control_until - time.time()
    check("an absurd request is clamped to one hour", left <= 3600 + 5)
    P.set_mode(pairing.PairedDevice.load("cafe"), "remote_control", P.DENY)
    await drain()

    prompts.clear()
    pc._approval_cooldown.clear()
    pc.approval_callback = await grant(0)
    await ask("pc.info_request")
    refused = await next_status()
    check("refusal is reported and nothing is enabled", refused.payload.get("denied") is True and not pairing.PairedDevice.load("cafe").pc_control_active())
    await ask("pc.info_request")
    await drain()
    check("after a refusal the phone cannot spam prompts (cooldown)", prompts == ["Test Phone"])

    print("9. revoking records the event, starts the cooldown and ends a live screen view")
    pc._approval_cooldown.clear()
    P.set_mode(pairing.PairedDevice.load("cafe"), "remote_control", P.ALLOW)
    pc._gate_cache.clear()
    await drain()
    events, stops = [], []
    server.audit = lambda event, device=None, **meta: events.append((event, device, meta))
    pcmod.GLib.idle_add = lambda fn, *a: stops.append(fn)
    pc._stream_conn = types.SimpleNamespace(fingerprint="beef")      # another phone's view must survive this revoke
    P.set_mode(pairing.PairedDevice.load("cafe"), "remote_control", P.ALLOW)
    P.set_mode(pairing.PairedDevice.load("cafe"), "remote_control", P.DENY)
    await asyncio.sleep(0.3)
    other_untouched = not stops
    P.set_mode(pairing.PairedDevice.load("cafe"), "remote_control", P.ALLOW)
    pc._stream_conn = types.SimpleNamespace(fingerprint="cafe")
    events.clear()
    pc._approval_cooldown.clear()
    P.set_mode(pairing.PairedDevice.load("cafe"), "remote_control", P.DENY)
    await asyncio.sleep(0.3)
    revoked = [e for e in events if e[0] == "permission.changed" and e[2].get("action") == "pc_control.revoked"]
    parts = {"one revoked record": len(revoked) == 1 and revoked[0][1] == "cafe",
             "cooldown set": pc._approval_cooldown.get("cafe", 0) > time.monotonic() + 100,
             "gate cache dropped": "cafe" not in pc._gate_cache,
             "screen view stopped once": stops == [pc._stop_screen],
             "other phone's view not stopped": other_untouched}
    check("revoke records pc_control.revoked once, starts the cooldown and stops only that phone's screen view", all(parts.values()))
    for k, v in parts.items():
        if not v:
            print("       failed part:", k)
    pc._stream_conn = None
    server.audit, pcmod.GLib.idle_add = real_audit, real_idle

    writer.close()
    serve.cancel()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
