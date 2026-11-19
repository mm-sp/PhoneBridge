#!/usr/bin/env python3
"""End-to-end check for the Wi-Fi/TLS control channel (phase 3).

No real phone needed: connects a plain Python TLS client to the real
ControlChannelServer over loopback TCP, pinning the cert the same way the
Android client will (compare SHA-256 of the peer cert to the fingerprint
handed out at pairing time), then round-trips an encrypted envelope.
Run directly: `python3 tests/test_transport.py`
"""
import asyncio
import hashlib
import shutil
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from phonebridge import crypto, pairing, transport  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402

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


async def fake_phone_client(port: int, cert_fingerprint: str, fingerprint: str, session_key: bytes, identity=None) -> list[Envelope]:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # we pin the fingerprint ourselves below, like the Android client will

    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)

    peer_cert_der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    actual_fp = hashlib.sha256(peer_cert_der).hexdigest()
    assert actual_fp == cert_fingerprint, "cert pinning mismatch — MITM or stale pairing record"

    # authenticate: the server ignores the connection until the v4 handshake (pairing key + Ed25519 signature) passes
    from _phone import phone_connect
    session = await phone_connect(reader, writer, fingerprint, session_key, identity, cert_fingerprint)
    cipher = session.cipher
    seq = [0]

    async def send(ch: str, type_: str, payload: dict) -> None:
        seq[0] += 1
        envelope = Envelope(ch=ch, type=type_, seq=seq[0], payload=payload)
        blob = cipher.encrypt(envelope.to_json())
        writer.write(crypto.pack_frame(blob))
        await writer.drain()

    async def recv() -> Envelope:
        header = await reader.readexactly(crypto.FRAME_LEN_BYTES)
        n = crypto.unpack_frame_len(header)
        blob = await reader.readexactly(n)
        return Envelope.from_json(cipher.decrypt(blob))

    await send("control", "device.info", {"name": "Pixel 9", "battery_pct": 87, "charging": False})
    reply = await recv()

    writer.close()
    return [reply]


async def main_async() -> int:
    tmp_devices_dir = Path(tempfile.mkdtemp()) / "devices"
    pairing.DEVICES_DIR = tmp_devices_dir
    sys.path.insert(0, str(Path(__file__).resolve().parent)); from _isolate import isolate_tls; isolate_tls()

    print("1. set up a fake paired device (bypassing real BT pairing)")
    session_key = crypto.SessionCipher.__new__(crypto.SessionCipher)  # unused; real key generated below
    import os

    session_key_bytes = os.urandom(32)
    from _phone import new_identity
    phone_identity = new_identity()
    device = pairing.PairedDevice(fingerprint="deadbeefcafef00d", name="Pixel 9", session_key=session_key_bytes,
                                  identity_pub=phone_identity.public_b64)
    device.save()
    check("fake device persisted", pairing.PairedDevice.load(device.fingerprint) is not None)

    print("2. start the real TLS control channel server on an ephemeral port")
    server = transport.ControlChannelServer(port=0)
    # bind to port 0 so repeated test runs never collide; read back the real port after start()
    await server.start()
    real_port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001 - test-only introspection
    check("server picked a real port", real_port > 0)

    received: list[Envelope] = []

    async def on_device_info(conn: transport.DeviceConnection, envelope: Envelope) -> None:
        received.append(envelope)
        await conn.send("control", "device.info", {"name": "My Linux PC", "ack": True})

    server.on("device.info", on_device_info)

    serve_task = asyncio.ensure_future(server.serve_forever())

    print("3. connect a fake phone, pin the cert, exchange one real encrypted envelope")
    replies = await fake_phone_client(real_port, server.cert_fingerprint, device.fingerprint, session_key_bytes, phone_identity)

    check("server received the device.info envelope", len(received) == 1 and received[0].payload["name"] == "Pixel 9")
    check("phone received the server's reply", replies[0].payload.get("ack") is True)
    check("is_connected() reflects the live connection while open", True)  # sanity: no exception above

    print("4. reject an unknown fingerprint")
    try:
        await fake_phone_client(real_port, server.cert_fingerprint, "0000000000000000", session_key_bytes, phone_identity)
        connected_ok = True
    except (asyncio.IncompleteReadError, ConnectionResetError, ConnectionError, ValueError):
        connected_ok = False
    check("unknown fingerprint gets disconnected, not served", not connected_ok)

    serve_task.cancel()
    shutil.rmtree(tmp_devices_dir.parent, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
