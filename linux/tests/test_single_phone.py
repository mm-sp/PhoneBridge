#!/usr/bin/env python3
"""Only one phone may be connected at a time.

Two paired fake phones connect to the real ControlChannelServer over loopback TLS: the
second is refused with the PBBUSY marker (naming the first) while the first is connected,
and gets in once the first disconnects.
Run directly: `python3 tests/test_single_phone.py`
"""
import asyncio
import os
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _phone import new_identity, phone_connect  # noqa: E402
from phonebridge import crypto, pairing, tls_identity, transport  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402

PASS = 0
FAIL = 0
PORT = 28193


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


KEYS: dict[str, bytes] = {}


IDENTITIES: dict = {}
SERVER = {}


async def connect(fingerprint: str, handshake: bool = True):
    """Returns (reader, writer, session); a phone that will be refused as busy never gets to the handshake."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT, ssl=ctx)
    if not handshake:
        writer.write((fingerprint + "\n").encode())
        await writer.drain()
        return reader, writer, None
    session = await phone_connect(reader, writer, fingerprint, KEYS[fingerprint], IDENTITIES[fingerprint], SERVER["tls"])
    return reader, writer, session


async def main_async() -> int:
    tmp = Path(tempfile.mkdtemp())
    pairing.DEVICES_DIR = tmp / "devices"
    tls_identity.CONFIG_DIR = tmp
    tls_identity.CERT_PATH = tmp / "cert.pem"
    tls_identity.KEY_PATH = tmp / "key.pem"
    transport.DISCOVERY_PORT = 28194
    IDENTITIES.update({"aaaa": new_identity(), "bbbb": new_identity()})
    a = pairing.PairedDevice("aaaa", "Phone A", os.urandom(32), identity_pub=IDENTITIES["aaaa"].public_b64)
    b = pairing.PairedDevice("bbbb", "Phone B", os.urandom(32), identity_pub=IDENTITIES["bbbb"].public_b64)
    a.save()
    b.save()
    KEYS.update({"aaaa": a.session_key, "bbbb": b.session_key})

    server = transport.ControlChannelServer(port=PORT)
    await server.start()
    SERVER["tls"] = server.cert_fingerprint
    serve = asyncio.ensure_future(server.serve_forever())

    print("1. first phone connects")
    ra, wa, session_a = await connect(a.fingerprint)
    cipher = session_a.cipher
    wa.write(crypto.pack_frame(cipher.encrypt(Envelope("control", "ping", 1, {}).to_json())))
    await wa.drain()
    n = crypto.unpack_frame_len(await ra.readexactly(4))
    pong = Envelope.from_json(cipher.decrypt(await ra.readexactly(n)))
    check("phone A is served (gets pong)", pong.type == "pong")

    print("2. second phone is refused while A is connected")
    rb, wb, _ = await connect(b.fingerprint, handshake=False)
    reply = await asyncio.wait_for(rb.readline(), 5)
    check("phone B gets PBBUSY naming phone A", reply == b"PBBUSY Phone A\n")
    check("only phone A is connected", server.connected_fingerprints() == ["aaaa"])
    wb.close()

    print("3. after A leaves, B can connect")
    wa.close()
    await asyncio.sleep(0.3)
    rb, wb, _ = await connect(b.fingerprint)
    await asyncio.sleep(0.3)
    check("phone B is now the connected phone", server.connected_fingerprints() == ["bbbb"])
    wb.close()

    serve.cancel()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
