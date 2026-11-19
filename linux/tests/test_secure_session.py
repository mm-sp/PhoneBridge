#!/usr/bin/env python3
"""Protocol v4: Ed25519 identities, per-connection ephemeral handshake (forward secrecy), key rotation, data-key
derivation, downgrade/impersonation rejection. Real ControlChannelServer over loopback TLS.
Run: `python3 tests/test_secure_session.py`"""
import asyncio
import os
import shutil
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolate import isolate_tls  # noqa: E402
from phonebridge import crypto, pairing, secure_session as S, transport  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402
from phonebridge.security import keystore, ledger as L  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


def unit_tests():
    print("1. handshake unit properties")
    psk = os.urandom(32)
    pc, ph = S.Identity.generate(), S.Identity.generate()
    fp, tls = "abcd1234abcd1234", "AB" * 32

    def run(psk_c=psk, psk_s=psk, ph_id=ph, ph_pub=None, tls_c=tls, tls_s=tls, pc_pub=None):
        blob, st = S.client_hello(psk_c, fp, tls_c, ph_id)
        reply, ps = S.server_hello(psk_s, fp, tls_s, ph_pub or ph.public_b64, pc, blob)
        return ps, S.client_finish(st, reply, pc_pub or pc.public_b64)

    ps, cs = run()
    check("both sides derive the same session id and exporter", ps.session_id == cs.session_id and ps.exporter == cs.exporter)
    check("PC->phone and phone->PC frames decrypt (separate per-direction keys)",
          cs.cipher.decrypt(ps.cipher.encrypt(b"a")) == b"a" and ps.cipher.decrypt(cs.cipher.encrypt(b"b")) == b"b")
    ps2, cs2 = run()
    check("every connection gets fresh, unrelated keys (ephemeral X25519)", ps2.exporter != ps.exporter)
    blob = ps.cipher.encrypt(b"recorded traffic")
    check("recorded traffic can't be opened with the long-term pairing key alone (forward secrecy)",
          raises(lambda: crypto.SessionCipher(psk, role="phone").decrypt(blob)))
    # A full-compromise attacker: knows the pairing key, both Ed25519 private keys and recorded the whole handshake.
    # He can open both hello frames (they're sealed with a key derived from the pairing key) and read the two
    # ephemeral PUBLIC keys, but the session key needs one ephemeral PRIVATE key, which no one stored.
    rec_blob, rec_state = S.client_hello(psk, fp, tls, ph)
    rec_reply, rec_ps = S.server_hello(psk, fp, tls, ph.public_b64, pc, rec_blob)
    opened_c, opened_s = S._open_hs(psk, "client", rec_blob), S._open_hs(psk, "server", rec_reply)
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    guess = X25519PrivateKey.generate().exchange(S.X25519PublicKey.from_public_bytes(S._unb64(opened_s["eph"])))
    forged = S.Session("phone", S._master(guess, psk, S._server_msg(opened_c, fp, tls, opened_s["eph"], opened_s["n"])), "x")
    check("…nor by an attacker with the pairing key + both identity keys + the recorded handshake (ephemerals are gone)",
          raises(lambda: forged.cipher.decrypt(rec_ps.cipher.encrypt(b"secret"))))
    check("a frame can't be reflected back to its sender",
          raises(lambda: ps.cipher.decrypt(ps.cipher.encrypt(b"x"))))
    check("each data key is bound to its label",
          raises(lambda: ps.data_cipher("file|a").decrypt(cs.data_cipher("file|b").encrypt(b"x")))
          and ps.data_cipher("file|a").decrypt(cs.data_cipher("file|a").encrypt(b"x")) == b"x")
    check("data keys of another session don't work", raises(lambda: ps.data_cipher("video").decrypt(cs2.data_cipher("video").encrypt(b"x"))))
    check("UDP key is session specific", ps.udp_key == cs.udp_key != ps2.udp_key)

    check("wrong pairing key is rejected", raises(lambda: run(psk_c=os.urandom(32)), S.HandshakeError))
    attacker = S.Identity.generate()
    check("someone holding the pairing key but not the phone's Ed25519 key can't pose as the phone",
          raises(lambda: run(ph_id=attacker), S.HandshakeError))
    check("the phone refuses a PC that signs with a different identity (PC impersonation)",
          raises(lambda: run(pc_pub=S.Identity.generate().public_b64), S.HandshakeError))
    check("signatures are bound to the TLS certificate (relaying onto another cert fails)",
          raises(lambda: run(tls_c="CD" * 32), S.HandshakeError))
    check("a pairing without an identity key is refused", raises(lambda: S.server_hello(
        psk, fp, tls, None, pc, S.client_hello(psk, fp, tls, ph)[0]), S.HandshakeError))
    blob, st = S.client_hello(psk, fp, tls, ph)
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    check("a tampered hello is rejected", raises(lambda: S.server_hello(psk, fp, tls, ph.public_b64, pc, tampered), S.HandshakeError))
    reply, _ps = S.server_hello(psk, fp, tls, ph.public_b64, pc, blob)
    check("a server hello replayed into another handshake is rejected",
          raises(lambda: S.client_finish(S.client_hello(psk, fp, tls, ph)[1], reply, pc.public_b64), S.HandshakeError))

    print("2. key rotation (unit)")
    ps, cs = run()
    gen0, exp0 = ps.generation, ps.exporter
    eph = ps.start_rekey(); ack = cs.respond_rekey(eph); old_ack = cs.cipher.encrypt(b"ack"); cs.commit_send()
    assert ps.cipher.decrypt(old_ack) == b"ack"
    ps.finish_rekey(ack); done = ps.cipher.encrypt(b"done"); ps.commit_send()
    assert cs.cipher.decrypt(done) == b"done"; cs.commit_recv()
    check("after a rekey both directions work with the new keys",
          ps.cipher.decrypt(cs.cipher.encrypt(b"n1")) == b"n1" and cs.cipher.decrypt(ps.cipher.encrypt(b"n2")) == b"n2")
    check("generation advanced; exporter (data/udp keys) unchanged for the session", ps.generation == gen0 + 1 == cs.generation and ps.exporter == exp0)
    check("frames from before the rekey no longer open", raises(lambda: ps.cipher.decrypt(old_ack)))
    check("an unsolicited rekey acknowledgement is refused", raises(lambda: run()[0].finish_rekey(ack), S.HandshakeError))


class Phone:
    def __init__(self, port, fp, key, ident, tls_fp, pc_pub):
        self.port, self.fp, self.key, self.ident, self.tls_fp, self.pc_pub, self.seq = port, fp, key, ident, tls_fp, pc_pub, 0

    async def connect(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        self.r, self.w = await asyncio.open_connection("127.0.0.1", self.port, ssl=ctx)
        from _phone import phone_connect
        self.session = await phone_connect(self.r, self.w, self.fp, self.key, self.ident, self.tls_fp, self.pc_pub)

    async def send(self, type_, payload=None):
        self.seq += 1
        self.w.write(crypto.pack_frame(self.session.cipher.encrypt(Envelope("control", type_, self.seq, payload or {}).to_json())))
        await self.w.drain()

    async def recv(self, t=3):
        n = crypto.unpack_frame_len(await asyncio.wait_for(self.r.readexactly(4), t))
        return Envelope.from_json(self.session.cipher.decrypt(await self.r.readexactly(n)))


async def server_tests(tmp):
    print("3. against the real server")
    pairing.DEVICES_DIR = tmp / "cfg" / "devices"
    isolate_tls()
    key, fp = os.urandom(32), "bbbb000011112222"
    ph_id = S.Identity.generate()
    pairing.PairedDevice(fingerprint=fp, name="V4 Phone", session_key=key, identity_pub=ph_id.public_b64).save()
    server = transport.ControlChannelServer(port=0)
    server.ledger = L.LocalLedger(tmp / "audit.jsonl", keystore.for_config_dir(tmp / "cfg"))
    await server.start()
    asyncio.ensure_future(server.serve_forever())
    port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
    pc_pub = pairing.pc_identity().public_b64

    ph = Phone(port, fp, key, ph_id, server.cert_fingerprint, pc_pub)
    await ph.connect()
    await ph.send("ping")
    check("an authenticated v4 session works", (await ph.recv()).type == "pong")
    conn = server.get_connection(fp)
    check("the server's session id matches the phone's", conn.session_id == ph.session.session_id)

    # old (v3) style: first frame sealed with the long-term key, no handshake
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    w.write((fp + "\n").encode())
    w.write(crypto.pack_frame(crypto.SessionCipher(key).encrypt(Envelope("control", "hello", 0, {}).to_json())))
    await w.drain()
    reply = await asyncio.wait_for(r.read(), 3)
    check("downgrade: a v3-style first frame gets no reply and the connection is closed", reply == b"")
    await asyncio.sleep(0.2)
    check("…and was not admitted as a second session", server.get_connection(fp) is conn)

    # attacker with the pairing key and a TLS connection but his own Ed25519 key
    evil = Phone(port, fp, key, S.Identity.generate(), server.cert_fingerprint, pc_pub)
    refused = False
    try:
        await evil.connect()
    except Exception:  # noqa: BLE001
        refused = True
    check("a stolen pairing key without the phone's identity key is useless", refused)
    await asyncio.sleep(0.2)
    check("…the real session is untouched and the attempt is in the ledger",
          server.get_connection(fp) is conn and any(x["type"] == L.AUTH_FAIL and x["meta"].get("reason") == "handshake_failed"
                                                    for x in server.ledger.records()))

    print("4. rotation through the real server")
    S.REKEY_FRAMES = 3            # the PC rekeys after it has sent 3 frames
    for _ in range(4):
        await ph.send("ping")
    got = []
    for _ in range(6):
        try:
            env = await ph.recv(1.5)
        except asyncio.TimeoutError:
            break
        got.append(env.type)
        if env.type == "rekey":
            eph = ph.session.respond_rekey(env.payload["eph"])
            ph.seq += 1
            ack = ph.session.cipher.encrypt(Envelope("control", "rekey.ack", ph.seq, {"eph": eph}).to_json())
            ph.w.write(crypto.pack_frame(ack)); await ph.w.drain()
            ph.session.commit_send()
        elif env.type == "rekey.done":
            ph.session.commit_recv()
            break
    check("the PC initiated a rekey and finished it", "rekey" in got and "rekey.done" in got)
    await ph.send("ping")
    check("traffic keeps flowing on the new keys", (await ph.recv()).type == "pong")
    check("PC and phone are on generation 1", conn.session.generation == 1 and ph.session.generation == 1)
    check("the rekey is in the ledger", any(x["type"] == L.REKEY for x in server.ledger.records()))
    S.REKEY_FRAMES = 1_000_000

    print("5. data connections need a live session")
    await server.disconnect_all(lockdown=False)
    await asyncio.sleep(0.2)
    r2, w2 = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    w2.write(f"{fp}:video\n".encode())
    w2.write(crypto.pack_frame(S.Session("phone", b"\1" * 32, "x").data_cipher("video").encrypt(b"x")))
    await w2.drain()
    check("a video connection without an authenticated control session is dropped", await asyncio.wait_for(r2.read(), 3) == b"")
    check("the ledger still verifies", server.ledger.verify().ok)

    print("6b. authenticated session termination")
    ph = Phone(port, fp, key, ph_id, server.cert_fingerprint, pc_pub)
    await ph.connect(); await ph.send("ping"); await ph.recv()
    await ph.send("session.end", {"reason": "user"})
    await asyncio.sleep(0.3)
    ends = [x for x in server.ledger.records() if x["type"] == L.SESSION_END]
    check("a phone that says goodbye is recorded as a clean end", ends[-1]["meta"]["how"] == "peer" and ends[-1]["meta"]["clean"])
    ph = Phone(port, fp, key, ph_id, server.cert_fingerprint, pc_pub)
    await ph.connect(); await ph.send("ping"); await ph.recv()
    ph.w.close()
    await asyncio.sleep(0.4)
    ends = [x for x in server.ledger.records() if x["type"] == L.SESSION_END]
    check("a link that just dies (reset/truncation) is recorded as abrupt", ends[-1]["meta"]["how"] == "abrupt" and not ends[-1]["meta"]["clean"])
    ph = Phone(port, fp, key, ph_id, server.cert_fingerprint, pc_pub)
    await ph.connect(); await ph.send("ping"); await ph.recv()
    await server.disconnect_all(lockdown=False)
    bye = await ph.recv()
    check("the PC announces why it is closing (authenticated, not just a TCP close)",
          bye.type == "session.end" and bye.payload["reason"] == "disconnect_all")
    check("and its session keys are dropped", server.get_connection(fp) is None)


async def rotation_tests(tmp):
    print("6. identity rotation")
    pairing.DEVICES_DIR = tmp / "rot" / "devices"
    isolate_tls(tmp / "rot")
    key, fp = os.urandom(32), "cccc000011112222"
    old_id = S.Identity.generate()
    pc0 = pairing.pc_identity()
    pairing.PairedDevice(fingerprint=fp, name="Rot Phone", session_key=key, identity_pub=old_id.public_b64,
                         pc_pub_acked=pc0.public_b64).save()
    server = transport.ControlChannelServer(port=0)
    server.ledger = L.LocalLedger(tmp / "rot-audit.jsonl", keystore.for_config_dir(tmp / "rot"))
    await server.start()
    asyncio.ensure_future(server.serve_forever())
    port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
    known_pc = {"pub": pc0.public_b64}

    def phone(ident):
        return Phone(port, fp, key, ident, server.cert_fingerprint, known_pc["pub"])

    print("   phone rotates its identity key")
    ph = phone(old_id)
    await ph.connect()
    new_id = S.Identity.generate()
    await ph.send("identity.rotate", {"new_pub": new_id.public_b64,
                                      "sig": S.sign_rotation(old_id, fp, new_id.public_b64)})
    env = await ph.recv()
    check("the PC accepted a rotation signed by the old key", env.type == "identity.rotated")
    dev = pairing.PairedDevice.load(fp)
    check("the new key is current and the old one is kept for the grace period",
          dev.identity_pub == new_id.public_b64 and dev.identity_pub_prev == old_id.public_b64)
    ph.w.close()
    await asyncio.sleep(0.2)
    ph2 = phone(old_id)
    await ph2.connect()
    await ph2.send("ping")
    check("during the grace period the old key still signs in (a lost ack can't lock the phone out)", (await ph2.recv()).type == "pong")
    ph2.w.close(); await asyncio.sleep(0.2)
    ph3 = phone(new_id)
    await ph3.connect()
    await ph3.send("ping")
    check("the new key signs in", (await ph3.recv()).type == "pong")
    check("…which retires the old key", pairing.PairedDevice.load(fp).identity_pub_prev is None)
    ph3.w.close(); await asyncio.sleep(0.2)
    stale = phone(old_id)
    refused = False
    try:
        await stale.connect()
    except Exception:  # noqa: BLE001
        refused = True
    check("the retired key no longer works", refused)
    ph4 = phone(new_id)
    await ph4.connect()
    evil = S.Identity.generate()
    await ph4.send("identity.rotate", {"new_pub": evil.public_b64, "sig": S.sign_rotation(evil, fp, evil.public_b64)})
    await asyncio.sleep(0.3)
    check("a rotation not signed by the current key is ignored and audited",
          pairing.PairedDevice.load(fp).identity_pub == new_id.public_b64
          and any(x["type"] == L.SECURITY_ERROR and x["meta"].get("error") == "bad_identity_rotation" for x in server.ledger.records()))
    check("accepted rotations are in the ledger", any(x["type"] == L.IDENTITY_ROTATED and x["meta"]["who"] == "phone" for x in server.ledger.records()))

    print("   the PC rotates its identity key while the phone is connected")
    notified = await server.rotate_pc_identity()
    env = await ph4.recv()
    new_pc = pairing.pc_identity().public_b64
    check("the connected phone is told immediately", notified == 1 and env.type == "identity.rotate" and env.payload["new_pub"] == new_pc)
    check("the notice is signed by the OLD PC key the phone knows",
          S.verify_rotation(known_pc["pub"], fp, env.payload["new_pub"], env.payload["sig"]))
    check("a forged notice (new key signing for itself) would not verify",
          not S.verify_rotation(env.payload["new_pub"], fp, "AAAA", env.payload["sig"]))
    await ph4.send("identity.rotated", {"new_pub": env.payload["new_pub"]})
    await asyncio.sleep(0.3)
    check("after the phone acknowledges, the PC records it and deletes the old private key",
          pairing.PairedDevice.load(fp).pc_pub_acked == new_pc and pairing.pc_identity_prev() is None)
    known_pc["pub"] = new_pc
    ph4.w.close(); await asyncio.sleep(0.2)

    print("   the PC rotates while the phone is OFFLINE")
    old_pc = known_pc["pub"]
    await server.rotate_pc_identity()
    check("the old PC key is kept for the phone that hasn't acknowledged", pairing.pc_identity_prev() is not None)
    check("the PC signs this phone's handshake with the key the phone knows",
          pairing.pc_identity_for(pairing.PairedDevice.load(fp)).public_b64 == old_pc)
    late = Phone(port, fp, key, new_id, server.cert_fingerprint, old_pc)   # phone still holds the old PC key
    await late.connect()
    notice = await late.recv()
    check("the returning phone authenticates the PC with the old key, then learns the new one",
          notice.type == "identity.rotate" and S.verify_rotation(old_pc, fp, notice.payload["new_pub"], notice.payload["sig"]))
    await late.send("identity.rotated", {"new_pub": notice.payload["new_pub"]})
    await asyncio.sleep(0.3)
    check("now fully rotated: acknowledged and the old key is gone",
          pairing.PairedDevice.load(fp).pc_pub_acked == pairing.pc_identity().public_b64 and pairing.pc_identity_prev() is None)
    check("the ledger still verifies", server.ledger.verify().ok)
    late.w.close()


async def network_tests(tmp):
    print("7. network interruption, replay and secure reconnection")
    pairing.DEVICES_DIR = tmp / "net" / "devices"
    isolate_tls(tmp / "net")
    key, fp = os.urandom(32), "dddd000011112222"
    ident = S.Identity.generate()
    pairing.PairedDevice(fingerprint=fp, name="Net Phone", session_key=key, identity_pub=ident.public_b64).save()
    server = transport.ControlChannelServer(port=0)
    server.ledger = L.LocalLedger(tmp / "net-audit.jsonl", keystore.for_config_dir(tmp / "net"))
    await server.start()
    asyncio.ensure_future(server.serve_forever())
    port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
    pc_pub = pairing.pc_identity().public_b64
    transport.FIRST_FRAME_TIMEOUT = 1.0

    def phone():
        return Phone(port, fp, key, ident, server.cert_fingerprint, pc_pub)

    ph = phone()
    await ph.connect(); await ph.send("ping"); await ph.recv()
    first_session, first_exporter = ph.session.session_id, ph.session.exporter
    ph.w.transport.abort()                          # the network dies: no goodbye, no FIN handshake
    await asyncio.sleep(0.4)
    check("an abrupt drop is detected and the session removed", server.get_connection(fp) is None)
    ends = [x for x in server.ledger.records() if x["type"] == L.SESSION_END]
    check("…and recorded as abrupt (not clean)", ends[-1]["meta"]["how"] == "abrupt")
    ph2 = phone()
    await ph2.connect(); await ph2.send("ping")
    check("the phone reconnects immediately with a fresh authenticated handshake", (await ph2.recv()).type == "pong")
    check("the new session has completely new keys", ph2.session.session_id != first_session and ph2.session.exporter != first_exporter)
    check("keys from the dropped session can't open the new session's data connections",
          _raises_any(lambda: ph2.session.data_cipher("video").decrypt(S.Session("phone", b"\x07" * 32, "old").data_cipher("video").encrypt(b"x"))))

    # replay: a recorded hello must not displace the live session
    rec_frame, rec_state = S.client_hello(key, fp, server.cert_fingerprint, ident)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    live = server.get_connection(fp)
    ra, wa = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    wa.write((fp + "\n").encode()); wa.write(crypto.pack_frame(rec_frame)); await wa.drain()
    n = crypto.unpack_frame_len(await ra.readexactly(4))
    S.client_finish(rec_state, await ra.readexactly(n), pc_pub)   # the original phone (holding rec_state) can finish...
    # ...but the ATTACKER who only recorded the bytes replays them on another connection:
    rb, wb = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    wb.write((fp + "\n").encode()); wb.write(crypto.pack_frame(rec_frame)); await wb.drain()
    n = crypto.unpack_frame_len(await rb.readexactly(4))
    await rb.readexactly(n)                          # the PC answers (it can't know it is a replay yet)
    await asyncio.sleep(0.3)
    check("a replayed hello does not replace the live session (no key confirmation yet)", server.get_connection(fp) is live)
    check("…the attacker cannot prove key possession, so the PC hangs up", await asyncio.wait_for(rb.read(), 3) == b"")
    check("…and records it as a failed authentication",
          any(x["type"] == L.AUTH_FAIL and x["meta"].get("reason") == "handshake_failed" for x in server.ledger.records()))
    check("the real phone's session survived all of that", server.get_connection(fp) is live)
    wa.close(); wb.close()

    # a client that handshakes but never confirms (slow/half-open)
    server.throttle = transport.AuthThrottle()
    rc, wc = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    frame, st = S.client_hello(key, fp, server.cert_fingerprint, ident)
    wc.write((fp + "\n").encode()); wc.write(crypto.pack_frame(frame)); await wc.drain()
    check("a client that stalls after the handshake is cut off", await asyncio.wait_for(rc.read(), 4) is not None)
    check("…without disturbing the live session", server.get_connection(fp) is live)

    # concurrent updates of a device record never lose a change
    import threading
    feats = list(__import__("phonebridge.security.permissions", fromlist=["x"]).FEATURES)
    feats.remove("remote_control")
    from phonebridge.security import permissions as P
    def toggle(f):
        P.set_mode(pairing.PairedDevice.load(fp), f, P.DENY)
    threads = [threading.Thread(target=toggle, args=(f,)) for f in feats]
    [t.start() for t in threads]; [t.join() for t in threads]
    check("simultaneous permission changes all survive (no lost updates)",
          all(pairing.PairedDevice.load(fp).permissions.get(f) == "deny" for f in feats))
    check("the ledger of all this verifies", server.ledger.verify().ok)
    transport.FIRST_FRAME_TIMEOUT = 15


def _raises_any(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


def main():
    tmp = Path(tempfile.mkdtemp())
    try:
        unit_tests()
        asyncio.run(server_tests(tmp))
        asyncio.run(rotation_tests(tmp))
        asyncio.run(network_tests(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
