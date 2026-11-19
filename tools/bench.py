#!/usr/bin/env python3
"""Measures what the security layer costs: handshake latency, per-frame encryption throughput, an encrypted file transfer over
loopback with the real server, ledger appends and reconnect time. Numbers are for THIS machine, not guarantees.
Run: ~/.local/share/phonebridge/venv/bin/python tools/bench.py"""
import asyncio
import hashlib
import os
import ssl
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "linux"))
sys.path.insert(0, str(ROOT / "linux/tests"))

from _isolate import isolate_tls  # noqa: E402
from _phone import phone_connect  # noqa: E402
from phonebridge import crypto, file_transfer, pairing, secure_session as S, transport  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402
from phonebridge.security import keystore, ledger as L  # noqa: E402


def timed(fn, n):
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n


async def main():
    tmp = Path(tempfile.mkdtemp())
    pairing.DEVICES_DIR = tmp / "cfg" / "devices"
    isolate_tls()
    results = {}

    psk = os.urandom(32); pc, ph = S.Identity.generate(), S.Identity.generate()
    def handshake():
        blob, st = S.client_hello(psk, "abcd1234abcd1234", "AB" * 32, ph)
        reply, _ = S.server_hello(psk, "abcd1234abcd1234", "AB" * 32, ph.public_b64, pc, blob)
        S.client_finish(st, reply, pc.public_b64)
    results["handshake CPU (both sides, Ed25519 + X25519 + HKDF)"] = f"{timed(handshake, 200) * 1000:.2f} ms"

    blob, st = S.client_hello(psk, "abcd1234abcd1234", "AB" * 32, ph)
    reply, ps = S.server_hello(psk, "abcd1234abcd1234", "AB" * 32, ph.public_b64, pc, blob)
    cs = S.client_finish(st, reply, pc.public_b64)
    chunk = os.urandom(256 * 1024)
    n = 400
    t0 = time.perf_counter()
    for _ in range(n):
        cs.cipher.decrypt(ps.cipher.encrypt(chunk))
    secs = time.perf_counter() - t0
    results["AES-256-GCM encrypt+decrypt, 256 KiB frames"] = f"{n * len(chunk) / secs / 1e6:.0f} MB/s"
    small = os.urandom(200)
    results["small control frame seal+open"] = f"{timed(lambda: cs.cipher.decrypt(ps.cipher.encrypt(small)), 20000) * 1e6:.1f} µs"

    led = L.LocalLedger(tmp / "audit.jsonl", keystore.for_config_dir(tmp / "cfg"))
    results["signed ledger append"] = f"{timed(lambda: led.append(L.AUTH_OK, 'dev', n=1), 300) * 1000:.2f} ms"
    results["ledger verify (300 records)"] = f"{timed(led.verify, 5) * 1000:.1f} ms"

    key, fp = os.urandom(32), "eeee000011112222"
    pairing.PairedDevice(fingerprint=fp, name="Bench", session_key=key, identity_pub=ph.public_b64).save()
    server = transport.ControlChannelServer(port=0)
    server.ledger = led
    transport.DISCOVERY_PORT = 28188
    await server.start()
    asyncio.ensure_future(server.serve_forever())
    port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE

    times = []
    for _ in range(15):
        t0 = time.perf_counter()
        r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
        sess = await phone_connect(r, w, fp, key, ph, server.cert_fingerprint)
        w.write(crypto.pack_frame(sess.cipher.encrypt(Envelope("control", "ping", 1, {}).to_json()))); await w.drain()
        n_ = crypto.unpack_frame_len(await r.readexactly(4)); sess.cipher.decrypt(await r.readexactly(n_))
        times.append(time.perf_counter() - t0)
        w.close(); await asyncio.sleep(0.05)
    results["reconnect (TLS 1.3 + handshake + first round trip), median"] = f"{statistics.median(times) * 1000:.1f} ms"

    # encrypted file transfer, PC -> "phone", real server + data connection
    r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    sess = await phone_connect(r, w, fp, key, ph, server.cert_fingerprint)
    await asyncio.sleep(0.2)
    conn = server.get_connection(fp)
    size = 64 * 1024 * 1024
    src = tmp / "big.bin"
    with src.open("wb") as f:
        for _ in range(size // (4 << 20)):
            f.write(os.urandom(4 << 20))
    send = asyncio.ensure_future(file_transfer.send_file(conn, src))
    n_ = crypto.unpack_frame_len(await r.readexactly(4)); offer = Envelope.from_json(sess.cipher.decrypt(await r.readexactly(n_)))
    tid = offer.payload["transfer_id"]
    dr, dw = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    dc = sess.data_cipher("file|" + tid)
    dw.write(f"{fp}:file:{tid}:0\n".encode()); dw.write(crypto.pack_frame(dc.encrypt(b"PBFILE:" + tid.encode()))); await dw.drain()
    t0 = time.perf_counter(); got = 0; digest = hashlib.sha256()
    while got < size:
        n_ = crypto.unpack_frame_len(await dr.readexactly(4)); piece = dc.decrypt(await dr.readexactly(n_))
        got += len(piece); digest.update(piece)
    await dr.readexactly(4)
    secs = time.perf_counter() - t0
    dw.write(crypto.pack_frame(dc.encrypt(b"OK"))); await dw.drain()
    await asyncio.wait_for(send, 30)
    results["encrypted file transfer over loopback (TLS + AES-GCM + SHA-256), 64 MiB"] = f"{size / secs / 1e6:.0f} MB/s"
    w.close(); dw.close()

    width = max(len(k) for k in results)
    for k, v in results.items():
        print(f"{k:<{width}}  {v}")


if __name__ == "__main__":
    asyncio.run(main())
