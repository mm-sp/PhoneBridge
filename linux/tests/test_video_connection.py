#!/usr/bin/env python3
"""End-to-end check for phase 6's dual-connection design (protocol/PROTOCOL.md section 1b):
a second TLS connection identified by "<fingerprint>:video" must be routed to a registered
video sink, completely separately from the normal control connection's JSON dispatch —
and the control connection must keep working normally at the same time (the whole point
of splitting them). Run directly: `python3 tests/test_video_connection.py`
"""
import asyncio
import os
import shutil
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import crypto, pairing, transport  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402
from phonebridge.security import permissions  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


async def open_tls(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await asyncio.open_connection("127.0.0.1", port, ssl=ctx)


async def main_async() -> int:
    tmp_devices_dir = Path(tempfile.mkdtemp()) / "devices"
    pairing.DEVICES_DIR = tmp_devices_dir
    sys.path.insert(0, str(Path(__file__).resolve().parent)); from _isolate import isolate_tls; isolate_tls()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _phone import new_identity, phone_connect
    ident = new_identity()
    session_key = os.urandom(32)
    device = pairing.PairedDevice(fingerprint="deadbeefcafef00d", name="Pixel 9", session_key=session_key,
                                  identity_pub=ident.public_b64)
    device.save()

    transport.VIDEO_POLICY_SECONDS = 0.05   # so a permission change is picked up within the test's short waits
    server = transport.ControlChannelServer(port=0)
    await server.start()
    real_port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
    serve_task = asyncio.ensure_future(server.serve_forever())

    received_control: list[Envelope] = []

    async def on_device_info(conn, envelope) -> None:
        received_control.append(envelope)

    server.on("device.info", on_device_info)

    print("1. open the control connection and the video connection from the same fake phone")
    control_reader, control_writer = await open_tls(real_port)
    session = await phone_connect(control_reader, control_writer, device.fingerprint, session_key, ident,
                                  server.cert_fingerprint)

    video_reader, video_writer = await open_tls(real_port)
    video_writer.write((device.fingerprint + ":video\n").encode("utf-8"))
    await video_writer.drain()
    await asyncio.sleep(0.2)

    check("control connection is tracked normally", server.is_connected(device.fingerprint))
    check("video connection is NOT added as a second control connection", len(server._connections) == 1)  # noqa: SLF001

    print("2. register a video sink and stream fake H.264-shaped frames over the video connection")
    received_frames: list[bytes] = []
    server.set_video_sink(device.fingerprint, lambda frame: received_frames.append(frame))

    cipher = session.data_cipher("video")   # the video connection's key comes from the authenticated control session
    fake_frames = [os.urandom(200), os.urandom(50), os.urandom(500)]
    for frame in fake_frames:
        blob = cipher.encrypt(frame)
        video_writer.write(crypto.pack_frame(blob))
    await video_writer.drain()
    await asyncio.sleep(0.2)

    check("all video frames reached the registered sink, in order", received_frames == fake_frames)

    print("3. the control connection keeps dispatching normally WHILE the video stream is live")
    conn = server.get_connection(device.fingerprint)
    await conn.send("control", "device.info", {"name": "Pixel 9", "battery_pct": 42})
    # loop the real server-side dispatch by sending a message FROM the fake phone too
    envelope = Envelope(ch="control", type="device.info", seq=1, payload={"battery_pct": 99})
    blob = session.cipher.encrypt(envelope.to_json())
    control_writer.write(crypto.pack_frame(blob))
    await control_writer.drain()
    await asyncio.sleep(0.2)

    check("control messages still dispatch correctly alongside the live video stream",
          len(received_control) == 1 and received_control[0].payload["battery_pct"] == 99)

    print("3b. switching the screen permission off stops a stream that is already running")

    async def send_frame(data: bytes) -> bool:
        before = len(received_frames)
        video_writer.write(crypto.pack_frame(cipher.encrypt(data)))
        await video_writer.drain()
        await asyncio.sleep(0.2)
        return len(received_frames) > before

    permissions.set_mode(pairing.PairedDevice.load(device.fingerprint), "screen", permissions.DENY)
    await asyncio.sleep(0.1)
    check("with screen=deny no further frame reaches the viewer", not await send_frame(b"denied"))
    permissions.set_mode(pairing.PairedDevice.load(device.fingerprint), "screen", permissions.ASK)
    await asyncio.sleep(0.1)
    check("with screen=ask and no approval for this session, nothing either", not await send_frame(b"unasked"))
    server.get_connection(device.fingerprint).granted.add("screen")
    check("…once approved for this session, frames flow", await send_frame(b"approved"))
    server.get_connection(device.fingerprint).granted.clear()
    permissions.set_mode(pairing.PairedDevice.load(device.fingerprint), "screen", permissions.ALLOW)
    await asyncio.sleep(0.1)
    check("allowing it again resumes the same video connection", await send_frame(b"allowed"))
    del received_frames[3:]

    print("4. clearing the sink stops delivery without breaking the connection")
    server.set_video_sink(device.fingerprint, None)
    blob = cipher.encrypt(os.urandom(30))
    video_writer.write(crypto.pack_frame(blob))
    await video_writer.drain()
    await asyncio.sleep(0.2)
    check("frame count unchanged after clearing the sink (frame was dropped, not crashed)", len(received_frames) == 3)

    control_writer.close()
    video_writer.close()
    serve_task.cancel()
    shutil.rmtree(tmp_devices_dir.parent, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
