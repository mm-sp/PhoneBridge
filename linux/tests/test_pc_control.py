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
    PcControl(server, asyncio.get_running_loop(), clipboard_text=lambda: "")
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

    print("6. terminal: run a command in a real shell")
    seq[0] += 1
    writer.write(crypto.pack_frame(cipher.encrypt(Envelope("control", "pc.term_open", seq[0],
                                                           {"cols": 80, "rows": 24}).to_json())))
    seq[0] += 1
    writer.write(crypto.pack_frame(cipher.encrypt(Envelope("control", "pc.term_input", seq[0],
                                                           {"data": "echo pb-$((40+2))\r"}).to_json())))
    await writer.drain()
    output = ""
    try:
        while "pb-42" not in output:
            env = await recv()
            if env.type == "pc.term_output":
                output += env.payload["data"]
    except asyncio.TimeoutError:
        pass
    check("shell ran the command and sent its output", "pb-42" in output)
    seq[0] += 1
    writer.write(crypto.pack_frame(cipher.encrypt(Envelope("control", "pc.term_close", seq[0], {}).to_json())))
    await writer.drain()
    exit_env = await recv()
    check("closing ends the shell", exit_env.type == "pc.term_exit")

    print("7. a running shell is closed by itself when the time limit runs out")
    seq[0] += 1
    writer.write(crypto.pack_frame(cipher.encrypt(Envelope("control", "pc.term_open", seq[0],
                                                           {"cols": 80, "rows": 24, "new": True}).to_json())))
    await writer.drain()
    await asyncio.sleep(0.5)
    phone.pc_control_until = time.time() - 1
    phone.save()
    ended = False
    try:
        for _ in range(60):
            env = await asyncio.wait_for(recv(), 12)
            if env.type == "pc.term_exit":
                ended = True
                break
    except asyncio.TimeoutError:
        pass
    check("idle shell was closed within ~5 s of the deadline, with no further message from the phone", ended)

    writer.close()
    serve.cancel()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
