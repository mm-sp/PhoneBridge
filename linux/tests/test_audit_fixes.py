#!/usr/bin/env python3
"""Regression tests for the findings of the independent security review (see docs/SECURITY.md). Run: `python3 tests/test_audit_fixes.py`"""
import asyncio
import json
import os
import shutil
import ssl
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))

from _isolate import isolate_tls  # noqa: E402
from _phone import new_identity, phone_connect  # noqa: E402
from phonebridge import crypto, pairing, pc_info, secure_session as S, transport  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402
from phonebridge.security import keystore, ledger as L, permissions as P  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


async def main_async(tmp):
    pairing.DEVICES_DIR = tmp / "cfg" / "devices"
    isolate_tls(tmp / "cfg")
    transport.DISCOVERY_PORT = 28185
    key, fp, ident = os.urandom(32), "abc0000011112222", new_identity()
    pairing.PairedDevice(fingerprint=fp, name="Audit", session_key=key, identity_pub=ident.public_b64).save()
    # a non-device JSON file next to the real record (the traversal target in the review)
    (tmp / "cfg" / "devices" / "decoy.json").write_text('{"not": "a device"}')
    server = transport.ControlChannelServer(port=0)
    server.ledger = L.LocalLedger(tmp / "a.jsonl", keystore.for_config_dir(tmp / "cfg"))
    await server.start()
    asyncio.ensure_future(server.serve_forever())
    port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE

    print("1. path traversal through the identity line")
    for evil in (f"../devices/{fp}", f"./{fp}", "../devices/decoy", f"{fp}/../{fp}", "..", "a b", f"{fp}\x00"):
        check(f"valid_fingerprint refuses {evil!r}", not pairing.valid_fingerprint(evil))
    check("a real fingerprint is accepted", pairing.valid_fingerprint(fp))
    check("PairedDevice.load never builds a path from an unvalidated name", pairing.PairedDevice.load(f"../devices/{fp}") is None
          and pairing.PairedDevice.load("../devices/decoy") is None)
    r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    w.write(f"../devices/{fp}\n".encode()); await w.drain()
    check("a traversal ident line is dropped without a reply, an exception or a second 'device'",
          await asyncio.wait_for(r.read(), 3) == b"" and server.connected_fingerprints() == [])
    r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    w.write(b"../devices/decoy\n"); await w.drain()
    check("a path that names a non-device JSON file can't crash the handler", await asyncio.wait_for(r.read(), 3) == b"")
    check("none of this touched the audit ledger with attacker text", not (tmp / "a.jsonl").exists() or "../devices" not in (tmp / "a.jsonl").read_text())

    print("2. revoke/lockdown during the handshake wins")
    ctx2 = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx2.check_hostname, ctx2.verify_mode = False, ssl.CERT_NONE
    r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx2)
    w.write((fp + "\n").encode())
    frame, st = S.client_hello(key, fp, server.cert_fingerprint, ident)
    w.write(crypto.pack_frame(frame)); await w.drain()
    n = crypto.unpack_frame_len(await r.readexactly(4))
    session = S.client_finish(st, await r.readexactly(n), pairing.pc_identity().public_b64)
    await server.disconnect_all(lockdown=True)            # happens AFTER the handshake reply but BEFORE the first frame
    w.write(crypto.pack_frame(session.cipher.encrypt(Envelope("control", "hello", 0, {}).to_json()))); await w.drain()
    await asyncio.sleep(0.4)
    check("a session finishing its handshake during 'disconnect all' is not admitted", server.connected_fingerprints() == [])
    server.lift_lockdown()

    print("3. TCP video: 'ask' is enforced and state changes only after authentication")
    ph = await asyncio.open_connection("127.0.0.1", port, ssl=ctx2)
    sess = await phone_connect(ph[0], ph[1], fp, key, ident, server.cert_fingerprint)
    P.set_mode(pairing.PairedDevice.load(fp), "screen", P.ASK)
    r2, w2 = await asyncio.open_connection("127.0.0.1", port, ssl=ctx2)
    w2.write(f"{fp}:video\n".encode()); w2.write(crypto.pack_frame(sess.data_cipher("video").encrypt(b"frame"))); await w2.drain()
    check("a video connection is refused while screen=ask and not approved for this session", await asyncio.wait_for(r2.read(), 3) == b"")
    server.video_transport.pop(fp, None)
    P.set_mode(pairing.PairedDevice.load(fp), "screen", P.ALLOW)
    r3, w3 = await asyncio.open_connection("127.0.0.1", port, ssl=ctx2)
    w3.write(f"{fp}:video\n".encode()); await w3.drain()
    await asyncio.sleep(0.3)
    check("connecting to the video port without proving the key changes no state", fp not in server.video_transport)
    w3.write(crypto.pack_frame(sess.data_cipher("video").encrypt(b"frame"))); await w3.drain(); await asyncio.sleep(0.3)
    check("…the first authentic frame does", server.video_transport.get(fp) == "tcp")
    ph[1].close(); w3.close()

    print("4. loopback (USB tunnel) is not throttled")
    for _ in range(8):
        server._fail("127.0.0.1")
    check("repeated failures from 127.0.0.1 never lock out the USB phone", not server.throttle.blocked("127.0.0.1"))
    check("but a LAN address still is", any([server._fail("10.9.9.9") for _ in range(6)]) and server.throttle.blocked("10.9.9.9"))

    print("5. discovery: rate limit and no per-packet keyring/file work")
    disc = transport._DiscoveryResponder(28100)
    answered = []
    class T:
        def sendto(self, data, addr): answered.append(addr)
    disc.transport = T()
    tag = crypto.discovery_tag(key, int(time.time() // crypto.DISCOVERY_BUCKET_SECONDS))
    pkt = json.dumps({"phonebridge": "discover", "tag": tag}).encode()
    for _ in range(30):
        disc.datagram_received(pkt, ("127.0.0.1", 5555))
    check("a valid tag is answered, but only up to the per-source rate limit", 1 <= len(answered) <= disc.RATE_MAX)
    calls = []
    orig = pairing.PairedDevice.list_all
    pairing.PairedDevice.list_all = staticmethod(lambda: (calls.append(1), orig())[1])
    disc2 = transport._DiscoveryResponder(28100); disc2.transport = T()
    bad = json.dumps({"phonebridge": "discover", "tag": "x"}).encode()
    for i in range(40):
        disc2.datagram_received(bad, (f"10.0.0.{i}", 5555))
    pairing.PairedDevice.list_all = orig
    check("40 bogus packets cost at most one device listing (tags are cached)", len(calls) <= 1)

    print("6. identity-rotation grace period expires")
    dev = pairing.PairedDevice.load(fp)
    dev = pairing.PairedDevice.update(fp, identity_pub_prev="PREVKEY", identity_prev_at=time.time())
    check("a just-retired phone key is still acceptable", "PREVKEY" in pairing.acceptable_phone_keys(dev))
    dev = pairing.PairedDevice.update(fp, identity_prev_at=time.time() - pairing.IDENTITY_GRACE_SECONDS - 5)
    check("…and is refused after 7 days", "PREVKEY" not in pairing.acceptable_phone_keys(dev))

    print("7. secrets are never visible to others, even briefly")
    store = keystore.FileStore(tmp / "ks")
    store.set("k", b"v")
    check("the file keystore is written 0600", ((tmp / "ks" / "secrets.json").stat().st_mode & 0o777) == 0o600)
    check("pairing records are written 0600", ((tmp / "cfg" / "devices" / f"{fp}.json").stat().st_mode & 0o777) == 0o600)

    print("8. hostile container ids")
    for bad_id in ("--privileged", "-v", "a b", "x;rm", "", "../x", "x" * 200):
        check(f"docker_action refuses {bad_id[:20]!r}", pc_info.docker_action(bad_id, "start") == (False, "Unknown action"))


def main():
    tmp = Path(tempfile.mkdtemp())
    try:
        asyncio.run(main_async(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
