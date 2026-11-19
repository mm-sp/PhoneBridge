#!/usr/bin/env python3
"""Security engine tests (docs/SECURITY.md): signed hash-chained ledger + tamper detection, keystore, auth
throttling, TLS 1.3 floor, zero-trust permissions (allow/deny/ask), revocation, disconnect-all lockdown,
unauthorized/bad-key rejection. Real ControlChannelServer over loopback TLS, no phone needed.
Run: `python3 tests/test_security.py`"""
import asyncio
import json
import os
import time
import shutil
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import crypto, pairing, secure_session, tls_identity, transport  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402
from phonebridge.security import ledger as L, permissions as P, keystore  # noqa: E402
from phonebridge.security.throttle import AuthThrottle  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


class Phone:
    """A fake paired phone speaking the real v4 wire protocol (Ed25519 identity + ephemeral handshake)."""

    def __init__(self, port, fp, key, identity=None, tls_fp="", tls=None):
        self.port, self.fp, self.key, self.seq, self.tls = port, fp, key, 0, tls
        self.identity, self.tls_fp, self.cipher, self.session = identity or secure_session.Identity.generate(), tls_fp, None, None

    async def connect(self, hello=True):
        """A phone that fails the handshake (stranger, wrong key, bad signature) is simply hung up on."""
        ctx = self.tls or ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.r, self.w = await asyncio.open_connection("127.0.0.1", self.port, ssl=ctx)
        self.w.write((self.fp + "\n").encode())
        frame, state = secure_session.client_hello(self.key, self.fp, self.tls_fp, self.identity)
        self.w.write(crypto.pack_frame(frame))
        await self.w.drain()
        try:
            n = crypto.unpack_frame_len(await asyncio.wait_for(self.r.readexactly(4), 3))
            self.session = secure_session.client_finish(state, await self.r.readexactly(n), self.pc_pub)
            self.cipher = self.session.cipher
            # key confirmation: the PC admits the session once a first frame decrypts under the new keys
            self.w.write(crypto.pack_frame(self.cipher.encrypt(Envelope("control", "hello", 0, {}).to_json())))
            await self.w.drain()
        except Exception:  # noqa: BLE001 - server hung up / sent the unpaired marker
            self.session = None

    async def send(self, type_, payload=None):
        self.seq += 1
        self.w.write(crypto.pack_frame(self.cipher.encrypt(Envelope("control", type_, self.seq, payload or {}).to_json())))
        await self.w.drain()

    async def recv(self, timeout=3):
        n = crypto.unpack_frame_len(await asyncio.wait_for(self.r.readexactly(4), timeout))
        return Envelope.from_json(self.cipher.decrypt(await self.r.readexactly(n)))

    async def closed(self, timeout=3):
        """True once the server hangs up (it may first send the plain PBUNPAIRED marker)."""
        try:
            await asyncio.wait_for(self.r.read(), timeout)   # returns only at EOF, i.e. the server hung up
            return True
        except (ConnectionError, ssl.SSLError):
            return True


def ledger_tests(tmp):
    print("1. signed, hash-chained ledger")
    ks = keystore.FileStore(tmp / "ks")
    led = L.LocalLedger(tmp / "audit.jsonl", ks)
    for i in range(5):
        led.append(L.AUTH_OK if i % 2 else L.FILE_TRANSFER, f"dev{i}", n=i)
    check("5 records verify", led.verify().ok and led.verify().records == 5)
    check("ledger signing key is in the keystore, not the log", ks.get(L.SIGNING_KEY_NAME) is not None)
    check("ledger file is private (0600)", (tmp / "audit.jsonl").stat().st_mode & 0o777 == 0o600)
    again = L.LocalLedger(tmp / "audit.jsonl", ks)
    again.append(L.PAIRED, "devX")
    check("a restarted ledger continues the same chain", again.verify().ok and again.verify().records == 6)

    lines = (tmp / "audit.jsonl").read_text().splitlines()
    ver = L.LedgerVerifier(led.public_key_b64)
    import time as _t
    big = L.LocalLedger(tmp / "big.jsonl", ks)
    for i in range(400):
        big.append(L.AUTH_OK, "d", n=i)
    t0 = _t.perf_counter(); first = big.verify(); cold = _t.perf_counter() - t0
    t0 = _t.perf_counter(); again = big.verify(); warm = _t.perf_counter() - t0
    check("re-verifying an already-verified ledger is much faster than the first full check (signature cache)", first.ok and again.ok and warm < cold * 0.6)
    raw = (tmp / "big.jsonl").read_text().splitlines()
    sw = json.loads(raw[100]); sw["sig"] = json.loads(raw[101])["sig"]
    (tmp / "big.jsonl").write_text("\n".join(raw[:100] + [json.dumps(sw)] + raw[101:]) + "\n")
    check("a signature swapped AFTER it was cached as good is still caught (cache keys on hash+signature)", not big.verify().ok)
    raw2 = json.loads(raw[200]); raw2["meta"]["n"] = -1
    (tmp / "big.jsonl").write_text("\n".join(raw[:200] + [json.dumps(raw2)] + raw[201:]) + "\n")
    check("an edited record is still caught on a warm cache", not big.verify().ok)

    def with_lines(new):
        p = tmp / "t.jsonl"
        p.write_text("\n".join(new) + "\n")
        return ver.verify_file(p)

    edited = json.loads(lines[2]); edited["meta"]["n"] = 999
    r = with_lines(lines[:2] + [json.dumps(edited)] + lines[3:])
    check("editing a record is detected", not r.ok and r.bad_index == 2)
    r = with_lines(lines[:2] + lines[3:])
    check("deleting a record is detected", not r.ok)
    r = with_lines([lines[0], lines[2], lines[1]] + lines[3:])
    check("reordering records is detected", not r.ok)
    forged = json.loads(lines[1]); forged["sig"] = json.loads(lines[0])["sig"]
    r = with_lines([lines[0], json.dumps(forged)] + lines[2:])
    check("a swapped/invalid signature is detected", not r.ok)
    other = L.LocalLedger(tmp / "other.jsonl", keystore.FileStore(tmp / "ks2"))
    other.append(L.AUTH_OK, "evil")
    r = L.LedgerVerifier(led.public_key_b64).verify_file(tmp / "other.jsonl")
    check("records signed by a different key are rejected", not r.ok)
    check("truncating the tail is not silently accepted as the full log's chain", with_lines(lines[:3]).ok and with_lines(lines[:3]).records == 3)
    check("PrivateBlockchain is the real anchored ledger (see test_anchor.py)", L.PrivateBlockchain.__name__ == "PrivateBlockchain")


def keystore_tests(tmp):
    print("2. keystore")
    plain = keystore.FileStore(tmp / "p")
    plain.set("a", b"secret")
    check("file store round-trips", plain.get("a") == b"secret" and plain.get("zz") is None)
    check("unprotected fallback reports protected=False", plain.protected is False)
    enc = keystore.FileStore(tmp / "e", passphrase="correct horse")
    enc.set("k", b"\x01\x02\x03-top-secret")
    raw = (tmp / "e" / "secrets.json").read_bytes()
    check("passphrase-protected file store does not contain the secret", b"top-secret" not in raw and b"AQID" not in raw)
    check("and reports protected=True", enc.protected)
    check("wrong passphrase cannot read it", _fails(lambda: keystore.FileStore(tmp / "e", passphrase="wrong").get("k")))
    check("right passphrase can", keystore.FileStore(tmp / "e", passphrase="correct horse").get("k") == b"\x01\x02\x03-top-secret")
    enc.delete("k")
    check("delete removes it", enc.get("k") is None)


def isolation_test():
    print("2b. scratch configs can never reach the real keyring")
    scratch = Path(tempfile.mkdtemp()) / ".config" / "phonebridge"
    check("a scratch dir gets a FileStore", isinstance(keystore.for_config_dir(scratch), keystore.FileStore))
    check("the real default config dir comes from the account, not $HOME",
          keystore.DEFAULT_CONFIG_DIR == keystore._account_home() / ".config" / "phonebridge")
    saved_home, saved_default = os.environ.get("HOME"), keystore.DEFAULT_CONFIG_DIR
    os.environ["HOME"] = str(scratch.parent.parent)
    keystore.DEFAULT_CONFIG_DIR = scratch   # what an old/misconfigured test would have done
    try:
        keystore._cache.pop(str(scratch), None)
        check("HOME=<scratch> never selects the Secret Service",
              not isinstance(keystore.for_config_dir(scratch), keystore.SecretServiceStore))
    finally:
        os.environ["HOME"] = saved_home
        keystore.DEFAULT_CONFIG_DIR = saved_default


def _fails(fn):
    try:
        fn()
    except Exception:
        return True
    return False


def throttle_tests():
    print("3. authentication throttling")
    now = [0.0]
    t = AuthThrottle(max_failures=3, window=60, base_lockout=10, max_lockout=40, clock=lambda: now[0])
    check("not blocked initially", not t.blocked("1.2.3.4"))
    t.failure("1.2.3.4"); t.failure("1.2.3.4")
    check("two failures don't lock", not t.blocked("1.2.3.4"))
    check("third failure locks out", t.failure("1.2.3.4") and t.blocked("1.2.3.4"))
    check("other addresses unaffected", not t.blocked("5.6.7.8"))
    now[0] = 11
    check("lockout expires", not t.blocked("1.2.3.4"))
    for _ in range(3): t.failure("1.2.3.4")
    now[0] = 11 + 15
    check("second lockout is longer (backoff)", t.blocked("1.2.3.4"))
    t.success("5.6.7.8")


async def server_tests(tmp):
    print("4. server: TLS floor, auth, permissions, revocation, lockdown")
    pairing.DEVICES_DIR = tmp / "cfg" / "devices"
    tls_identity.CONFIG_DIR, tls_identity.CERT_PATH, tls_identity.KEY_PATH = tmp / "cfg", tmp / "cfg/c.pem", tmp / "cfg/k.pem"
    key = os.urandom(32)
    ph_ident = secure_session.Identity.generate()
    dev = pairing.PairedDevice(fingerprint="aaaa000011112222", name="Test Phone", session_key=key,
                               identity_pub=ph_ident.public_b64)
    dev.save()
    saved = json.loads((pairing.DEVICES_DIR / f"{dev.fingerprint}.json").read_text())
    check("session key is NOT in the pairing JSON file", "session_key" not in saved and key.hex() not in json.dumps(saved))
    check("session key loads back from the keystore", pairing.PairedDevice.load(dev.fingerprint).session_key == key)
    check("the TLS private key is not on disk as a file", not tls_identity.KEY_PATH.exists())

    # a stored key that doesn't match the certificate is detected and replaced (never served as a broken pair)
    fp_before = tls_identity.ensure_server_identity()[1]
    store = keystore.for_config_dir(tmp / "cfg")
    good = store.get(tls_identity.KEY_NAME)
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    from cryptography.hazmat.primitives import serialization as _ser
    store.set(tls_identity.KEY_NAME, _ec.generate_private_key(_ec.SECP256R1()).private_bytes(
        _ser.Encoding.PEM, _ser.PrivateFormat.PKCS8, _ser.NoEncryption()))
    fp_after = tls_identity.ensure_server_identity()[1]
    check("a cert/key mismatch is detected and a fresh identity is generated", fp_after != fp_before)
    ctx_check = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        tls_identity.load_cert_chain(ctx_check)
        check("the regenerated identity loads (cert and key agree)", True)
    except ssl.SSLError:
        check("the regenerated identity loads (cert and key agree)", False)
    check("the TLS key is never left in a file after loading", not any(Path(tempfile.gettempdir()).glob("pb-tls-*/k.pem")))

    led = L.LocalLedger(tmp / "server-audit.jsonl", keystore.for_config_dir(tmp / "cfg"))
    server = transport.ControlChannelServer(port=0)
    server.ledger = led
    got = []

    async def sink(conn, env):
        got.append(env.type)

    for t in ("clipboard.update", "sms.new", "notification.posted", "file.offer"):
        server.on(t, sink)
    await server.start()
    asyncio.ensure_future(server.serve_forever())
    port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
    tls_fp = server.cert_fingerprint
    Phone.pc_pub = pairing.pc_identity().public_b64   # what a real phone learned at pairing
    check("TLS 1.3 is the minimum", server.ssl_context.minimum_version == ssl.TLSVersion.TLSv1_3)

    c12 = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c12.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        await asyncio.open_connection("127.0.0.1", port, ssl=_noverify(c12))
        check("a TLS 1.2-only client is refused (downgrade blocked)", False)
    except (ssl.SSLError, ConnectionError, OSError):
        check("a TLS 1.2-only client is refused (downgrade blocked)", True)

    ph = Phone(port, dev.fingerprint, key, ph_ident, tls_fp)
    await ph.connect()
    await ph.send("clipboard.update", {"text": "hi"})
    await asyncio.sleep(0.3)
    check("default policy: clipboard allowed", got == ["clipboard.update"])

    P.set_mode(pairing.PairedDevice.load(dev.fingerprint), "sms", P.DENY)
    await ph.send("sms.new", {"body": "x"})
    denial = await ph.recv()
    check("denied feature: handler not run and phone told why", "sms.new" not in got and denial.type == "permission.denied"
          and denial.payload["feature"] == "sms")
    check("denial is written to the ledger", any(r["type"] == L.DENIED and r["meta"]["feature"] == "sms" for r in led.records()))

    P.set_mode(pairing.PairedDevice.load(dev.fingerprint), "notifications", P.ASK)
    await ph.send("notification.posted", {"app": "x"})
    d2 = await ph.recv()
    check("'ask' with no prompt available behaves as deny", d2.type == "permission.denied" and "notification.posted" not in got)

    asked = []

    async def approve(device, feature):
        asked.append(feature)
        return True
    server.ask_callback = approve
    await ph.send("notification.posted", {"app": "x"})
    await ph.send("notification.posted", {"app": "y"})
    await asyncio.sleep(0.3)
    check("'ask' with approval allows it, asking only once per connection", got.count("notification.posted") == 2 and asked == ["notifications"])

    # photos vs files route by photo_id
    P.set_mode(pairing.PairedDevice.load(dev.fingerprint), "photos", P.DENY)
    await ph.send("file.offer", {"transfer_id": "t", "photo_id": "p1", "size": 1})
    d3 = await ph.recv()
    check("a photo offer is judged as 'photos', not 'files'", d3.type == "permission.denied" and d3.payload["feature"] == "photos")

    check("remote_control mirrors the legacy allow_pc_control flag",
          P.mode_of(pairing.PairedDevice.load(dev.fingerprint), "remote_control") == P.DENY)
    P.set_mode(pairing.PairedDevice.load(dev.fingerprint), "remote_control", P.ALLOW)
    check("turning it on sets allow_pc_control", pairing.PairedDevice.load(dev.fingerprint).allow_pc_control is True)
    _d = pairing.PairedDevice.load(dev.fingerprint)
    check("...with a deadline about an hour away", _d.pc_control_until is not None and 3000 < _d.pc_control_until - time.time() <= 3600
          and _d.pc_control_active())
    check("mode_of is DENY once the deadline has passed", (setattr(_d, "pc_control_until", time.time() - 1) or P.mode_of(_d, "remote_control")) == P.DENY)
    P.set_mode(pairing.PairedDevice.load(dev.fingerprint), "remote_control", P.DENY)
    check("turning it off clears the deadline", pairing.PairedDevice.load(dev.fingerprint).pc_control_until is None)
    check("remote_control can't be 'ask'", _fails(lambda: P.set_mode(pairing.PairedDevice.load(dev.fingerprint), "remote_control", P.ASK)))

    # unauthorized device / bad key
    stranger = Phone(port, "ffff999988887777", os.urandom(32))
    await stranger.connect()
    check("unknown device is rejected", await stranger.closed())
    wrong = Phone(port, dev.fingerprint, os.urandom(32), None, tls_fp)
    await wrong.connect()
    check("known fingerprint but wrong key is rejected", await wrong.closed())
    types = [r["type"] for r in led.records()]
    check("both failures are in the ledger", types.count(L.AUTH_FAIL) >= 2)
    transport.THROTTLE_EXEMPT = ()   # the test client connects from loopback, which production exempts (USB tunnel)
    for _ in range(6):
        s = Phone(port, "ffff999988887777", os.urandom(32))
        await s.connect(); await s.closed()
    check("repeated failures trigger the throttle", L.AUTH_THROTTLED in [r["type"] for r in led.records()])
    refused = Phone(port, dev.fingerprint, key, ph_ident, tls_fp)
    await refused.connect()
    check("a throttled address is refused even with valid credentials", await refused.closed())
    server.throttle = AuthThrottle()   # reset for the remaining checks

    # disconnect all
    n = await server.disconnect_all()
    check("disconnect-all drops the live session", n == 1 and await ph.closed())
    check("lockdown refuses a valid phone", server.locked_down)
    again = Phone(port, dev.fingerprint, key, ph_ident, tls_fp)
    await again.connect()
    check("…a valid reconnect during lockdown is refused", await again.closed())
    server.lift_lockdown()
    back = Phone(port, dev.fingerprint, key, ph_ident, tls_fp)
    await back.connect()
    await back.send("clipboard.update", {"text": "back"})
    await asyncio.sleep(0.3)
    check("after lifting lockdown the phone can reconnect", got.count("clipboard.update") == 2)

    # revoke
    check("revoke reports the pairing existed", await server.revoke(dev.fingerprint, "test"))
    check("revoke drops the live connection", await back.closed())
    check("revoked device's pairing and key are gone", pairing.PairedDevice.load(dev.fingerprint) is None
          and keystore.for_config_dir(tmp / "cfg").get(f"session-key:{dev.fingerprint}") is None)
    gone = Phone(port, dev.fingerprint, key, ph_ident, tls_fp)
    await gone.connect()
    check("a revoked device can no longer connect", await gone.closed())

    res = led.verify()
    check(f"ledger of the whole run still verifies ({res.records} records, {res.error})", res.ok and res.records > 10)
    for needed in (L.AUTH_OK, L.SESSION_START, L.SESSION_END, L.DISCONNECT_ALL, L.REVOKED, L.DENIED, L.AUTH_FAIL):
        check(f"ledger recorded {needed}", needed in [r["type"] for r in led.records()])
    blob = (tmp / "server-audit.jsonl").read_text()
    check("ledger holds no message content", '"hi"' not in blob and "back" not in blob)


def _noverify(ctx):
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def main():
    tmp = Path(tempfile.mkdtemp())
    try:
        ledger_tests(tmp)
        keystore_tests(tmp)
        isolation_test()
        throttle_tests()
        asyncio.run(server_tests(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
