#!/usr/bin/env python3
"""Short fuzz run for the regular suite: parser fuzzing (tests/fuzz_protocol.py) plus garbage thrown at a LIVE server, after
which a legitimate phone must still connect and be served. For a long run: `python3 tests/fuzz_protocol.py --seconds 300`."""
import asyncio
import os
import random
import shutil
import ssl
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))

import fuzz_protocol as fz  # noqa: E402
from _isolate import isolate_tls  # noqa: E402
from _phone import new_identity, phone_connect  # noqa: E402
from phonebridge import crypto, pairing, transport  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


async def live(tmp: Path, seed: int):
    rng = random.Random(seed)
    pairing.DEVICES_DIR = tmp / "live" / "devices"
    isolate_tls(tmp / "live")
    transport.DISCOVERY_PORT = 28187
    transport.FIRST_FRAME_TIMEOUT = 1.0     # stalled junk connections are dropped quickly
    key, fp, ident = os.urandom(32), "feed000011112222", new_identity()
    pairing.PairedDevice(fingerprint=fp, name="Legit", session_key=key, identity_pub=ident.public_b64).save()
    server = transport.ControlChannelServer(port=0)
    await server.start()
    asyncio.ensure_future(server.serve_forever())
    port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE

    async def junk():
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port, ssl=ctx), 3)
        except Exception:  # noqa: BLE001
            return
        kind = rng.randint(0, 7)
        try:
            if kind == 0:   w.write(os.urandom(rng.randint(1, 3000)))
            elif kind == 1: w.write(b"A" * rng.randint(1, 70000))                                   # an endless "line"
            elif kind == 2: w.write(f"{fp}\\n".encode() + os.urandom(rng.randint(0, 400)))            # real fingerprint, junk frame
            elif kind == 3: w.write(f"{fp}\\n".encode() + struct.pack(">I", rng.choice([0, 1, 2**31, 2**32 - 1, 9 * 2**20])) + os.urandom(8))
            elif kind == 4: w.write(f"{fp}:file:{os.urandom(4).hex()}:{rng.choice(['x', '-1', '99999999999999999999', ''])}\\n".encode())
            elif kind == 5: w.write(f"{fp}:video\\n".encode() + os.urandom(rng.randint(0, 100)))
            elif kind == 6: w.write(f"{os.urandom(8).hex()}\\n".encode())                              # unknown fingerprint
            else:           w.write(f"{fp}\\n".encode())                                               # then silence (slowloris)
            await w.drain()
            await asyncio.wait_for(r.read(100), 0.3)
        except Exception:  # noqa: BLE001
            pass
        finally:
            w.close()

    t0 = time.monotonic()
    await asyncio.gather(*(junk() for _ in range(150)))
    for _ in range(3):
        await asyncio.gather(*(junk() for _ in range(60)))
    server.throttle = transport.AuthThrottle()          # the junk legitimately tripped the per-address lockout
    r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    sess = await phone_connect(r, w, fp, key, ident, server.cert_fingerprint)
    w.write(crypto.pack_frame(sess.cipher.encrypt(Envelope("control", "ping", 1, {}).to_json()))); await w.drain()
    n = crypto.unpack_frame_len(await asyncio.wait_for(r.readexactly(4), 5))
    pong = Envelope.from_json(sess.cipher.decrypt(await r.readexactly(n)))
    check(f"a legitimate phone is still served after 330 hostile connections ({time.monotonic() - t0:.1f}s)", pong.type == "pong")
    check("none of the junk was admitted as a session", server.connected_fingerprints() == [fp])
    check("the server holds no leftover half-open sessions", len(server._connections) == 1)  # noqa: SLF001
    w.close()


def main():
    seed = random.randrange(1 << 30)
    print(f"parser fuzzing, 14 s, seed {seed}")
    fz.run(14, seed)
    check("no parser crashed, hung or accepted garbage", not fz.FAILURES)
    for f in fz.FAILURES:
        print("  ", f.splitlines()[0])
    tmp = Path(tempfile.mkdtemp())
    try:
        asyncio.run(live(tmp, seed))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
