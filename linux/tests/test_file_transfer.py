#!/usr/bin/env python3
"""End-to-end check for file transfer v2 (one data connection per file, sha256, resume,
reject): a real TLS server and a fake phone that speaks the same protocol as the Android
app. Run directly: `python3 tests/test_file_transfer.py`
"""
import asyncio
import hashlib
import os
import shutil
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import crypto, file_transfer, pairing, transport  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402

PASS = 0
FAIL = 0
CHUNK = file_transfer.CHUNK_SIZE


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


async def open_tls(port):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    return await asyncio.open_connection("127.0.0.1", port, ssl=ctx)


class FakePhone:
    def __init__(self, port, fp, key, identity, tls_fp):
        self.port, self.fp, self.key, self.identity, self.tls_fp = port, fp, key, identity, tls_fp
        self.seq = 0
        self.cipher = self.dcipher = None

    async def connect(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _phone import phone_connect
        self.r, self.w = await open_tls(self.port)
        self.session = await phone_connect(self.r, self.w, self.fp, self.key, self.identity, self.tls_fp)
        self.cipher = self.session.cipher

    async def send(self, type_, payload):
        self.seq += 1
        self.w.write(crypto.pack_frame(self.cipher.encrypt(Envelope("control", type_, self.seq, payload).to_json())))
        await self.w.drain()

    async def recv(self) -> Envelope:
        n = crypto.unpack_frame_len(await self.r.readexactly(4))
        return Envelope.from_json(self.cipher.decrypt(await self.r.readexactly(n)))

    async def data_connection(self, tid, offset):
        r, w = await open_tls(self.port)
        w.write(f"{self.fp}:file:{tid}:{offset}\n".encode())
        self.dcipher = self.session.data_cipher("file|" + tid)   # key derived from the authenticated control session
        w.write(crypto.pack_frame(self.dcipher.encrypt(b"PBFILE:" + tid.encode())))
        await w.drain()
        return r, w

    async def read_frame(self, r):
        n = crypto.unpack_frame_len(await r.readexactly(4))
        return self.dcipher.decrypt(await r.readexactly(n))

    def frame(self, data):
        return crypto.pack_frame(self.dcipher.encrypt(data))


async def phone_receives(phone: FakePhone, dest: Path, stop_after=None, reject=False):
    """What the Android app does for a PC->phone offer. Returns (offer, received_bytes, hash_ok)."""
    offer = await phone.recv()
    assert offer.type == "file.offer" and offer.payload["data_conn"]
    p = offer.payload
    if reject:
        await phone.send("file.reject", {"transfer_id": p["transfer_id"], "reason": "no space"})
        return offer, b"", None
    part = dest / f".pb-{p['resume_key']}.part"
    offset = part.stat().st_size if part.exists() else 0
    r, w = await phone.data_connection(p["transfer_id"], offset)
    got = part.read_bytes() if part.exists() else b""
    digest = hashlib.sha256(got)
    size = p["size"]
    frames = 0
    while len(got) < size:
        chunk = await phone.read_frame(r)
        got += chunk
        digest.update(chunk)
        with part.open("ab") as f:
            f.write(chunk)
        frames += 1
        if stop_after and frames >= stop_after:  # simulate the link dropping mid-transfer
            w.close()
            return offer, got, None
    trailer = await phone.read_frame(r)
    ok = trailer == b"SHA256:" + digest.hexdigest().encode()
    w.write(phone.frame(b"OK" if ok else b"BAD"))
    await w.drain()
    w.close()
    part.replace(dest / p["filename"])
    return offer, got, ok


async def main_async() -> int:
    tmp_devices_dir = Path(tempfile.mkdtemp()) / "devices"
    pairing.DEVICES_DIR = tmp_devices_dir
    sys.path.insert(0, str(Path(__file__).resolve().parent)); from _isolate import isolate_tls; isolate_tls()
    work = Path(tempfile.mkdtemp())
    session_key = os.urandom(32)
    from _phone import new_identity
    ident = new_identity()
    device = pairing.PairedDevice(fingerprint="deadbeefcafef00d", name="Pixel 9", session_key=session_key,
                                  identity_pub=ident.public_b64)
    device.save()

    print("1. server up, fake phone connected")
    server = transport.ControlChannelServer(port=0)
    transport.DISCOVERY_PORT = 28197
    await server.start()
    port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
    serve = asyncio.ensure_future(server.serve_forever())

    received_files: list = []
    dest_pc = work / "pc_inbox"
    file_transfer.register(server, lambda _p: dest_pc, None, lambda path, size, p: received_files.append(path))

    phone = FakePhone(port, device.fingerprint, session_key, ident, server.cert_fingerprint)
    await phone.connect()
    await asyncio.sleep(0.2)
    conn = server.get_connection(device.fingerprint)
    check("server sees the phone", conn is not None)

    print("2. PC -> phone, 2.5 chunks, hash verified")
    src = work / "photo.bin"
    data = os.urandom(int(CHUNK * 2.5))
    src.write_bytes(data)
    phone_dest = work / "phone_inbox"
    phone_dest.mkdir()
    progress = []
    send = asyncio.ensure_future(file_transfer.send_file(conn, src, on_progress=lambda s, t: progress.append(s)))
    _offer, got, ok = await asyncio.wait_for(phone_receives(phone, phone_dest), 10)
    await asyncio.wait_for(send, 10)
    check("phone got identical bytes", got == data)
    check("phone verified the sha256 trailer", ok is True)
    check("progress reached 100%", bool(progress) and progress[-1] == len(data))

    print("3. the control channel is NOT blocked while a transfer is in flight")
    big = work / "big.bin"
    big.write_bytes(os.urandom(CHUNK * 4))
    send = asyncio.ensure_future(file_transfer.send_file(conn, big))
    offer = await phone.recv()  # phone has the offer but hasn't connected the data link yet
    await conn.send("control", "clipboard.update", {"text": "hi"})
    other = await asyncio.wait_for(phone.recv(), 3)
    check("a control message gets through before the data link even opens", other.type == "clipboard.update")
    r, w = await phone.data_connection(offer.payload["transfer_id"], 0)
    got = b""
    while len(got) < big.stat().st_size:
        got += await phone.read_frame(r)
    await phone.read_frame(r)
    w.write(phone.frame(b"OK"))
    await w.drain()
    w.close()
    await asyncio.wait_for(send, 10)
    check("big transfer completes", got == big.read_bytes())

    print("4. resume after the link drops mid-file")
    resume_src = work / "movie.bin"
    resume_bytes = os.urandom(CHUNK * 5)
    resume_src.write_bytes(resume_bytes)
    send1 = asyncio.ensure_future(file_transfer.send_file(conn, resume_src))
    _o, partial, _ok = await asyncio.wait_for(phone_receives(phone, phone_dest, stop_after=2), 10)
    try:
        await asyncio.wait_for(send1, 10)
        first_failed = False
    except file_transfer.TransferError:
        first_failed = True
    check("interrupted transfer reports failure on the PC", first_failed)
    check("phone kept a partial of 2 chunks", len(partial) == CHUNK * 2)
    send2 = asyncio.ensure_future(file_transfer.send_file(conn, resume_src))
    _o, got, ok = await asyncio.wait_for(phone_receives(phone, phone_dest), 10)
    await asyncio.wait_for(send2, 10)
    check("second attempt resumed (whole file reassembled, hash ok)", ok is True and got == resume_bytes)

    print("5. phone rejects an offer")
    send = asyncio.ensure_future(file_transfer.send_file(conn, src))
    await phone_receives(phone, phone_dest, reject=True)
    try:
        await asyncio.wait_for(send, 5)
        rejected = False
    except file_transfer.TransferError as exc:
        rejected = "no space" in str(exc)
    check("send_file raises TransferError carrying the reason", rejected)

    print("6. phone -> PC upload with accept, resume offset and hash check")
    up = os.urandom(int(CHUNK * 1.5))
    tid = "t" * 32
    await phone.send("file.offer", {"transfer_id": tid, "filename": "up.bin", "size": len(up),
                                    "mime": "application/octet-stream", "resume_key": "abc123", "data_conn": True})
    acc = await asyncio.wait_for(phone.recv(), 5)
    check("PC answers file.accept with offset 0", acc.type == "file.accept" and acc.payload["offset"] == 0)
    r, w = await phone.data_connection(tid, 0)
    for i in range(0, len(up), CHUNK):
        w.write(phone.frame(up[i:i + CHUNK]))
    w.write(phone.frame(b"SHA256:" + hashlib.sha256(up).hexdigest().encode()))
    await w.drain()
    verdict = await asyncio.wait_for(phone.read_frame(r), 5)
    w.close()
    await asyncio.sleep(0.3)
    check("PC verified the upload (OK)", verdict == b"OK")
    check("upload saved intact", bool(received_files) and received_files[-1].read_bytes() == up)

    print("7. corrupted upload is refused and discarded")
    tid2 = "u" * 32
    await phone.send("file.offer", {"transfer_id": tid2, "filename": "bad.bin", "size": 10,
                                    "mime": "x/y", "resume_key": "zzz", "data_conn": True})
    await asyncio.wait_for(phone.recv(), 5)
    r, w = await phone.data_connection(tid2, 0)
    w.write(phone.frame(b"0123456789"))
    w.write(phone.frame(b"SHA256:" + b"0" * 64))
    await w.drain()
    verdict = await asyncio.wait_for(phone.read_frame(r), 5)
    w.close()
    await asyncio.sleep(0.3)
    check("PC answers BAD on a hash mismatch", verdict == b"BAD")
    check("no file was kept", not (dest_pc / "bad.bin").exists() and not (dest_pc / ".pb-zzz.part").exists())

    print("7b. cancelling a PC -> phone transfer")
    big2 = work / "cancel-me.bin"
    big2.write_bytes(os.urandom(CHUNK * 8))
    sendc = asyncio.ensure_future(file_transfer.send_file(conn, big2))
    offer_c = await phone.recv()
    tid_c = offer_c.payload["transfer_id"]
    rc, wc = await phone.data_connection(tid_c, 0)
    await phone.read_frame(rc)                       # the transfer is genuinely running
    check("the PC knows the transfer", tid_c in file_transfer._OUT)  # noqa: SLF001
    check("cancelling reports success", await file_transfer.cancel_transfer(conn, tid_c))
    try:
        await asyncio.wait_for(sendc, 5)
        cancelled = False
    except file_transfer.TransferError as exc:
        cancelled = "cancel" in str(exc)
    check("send_file raises TransferError('cancelled')", cancelled)
    note = await asyncio.wait_for(phone.recv(), 5)
    check("the phone is told (file.cancel)", note.type == "file.cancel" and note.payload["transfer_id"] == tid_c)
    check("the transfer is forgotten", tid_c not in file_transfer._OUT)  # noqa: SLF001
    check("cancelling an unknown transfer is a no-op", not await file_transfer.cancel_transfer(conn, "nope"))
    wc.close()

    print("7c. cancelling a phone -> PC upload deletes the partial; partials are private")
    tid_u = "v" * 32
    upc = os.urandom(CHUNK * 4)
    await phone.send("file.offer", {"transfer_id": tid_u, "filename": "cancelled.bin", "size": len(upc),
                                    "mime": "x/y", "resume_key": "cancelkey", "data_conn": True})
    await asyncio.wait_for(phone.recv(), 5)
    r, w = await phone.data_connection(tid_u, 0)
    w.write(phone.frame(upc[:CHUNK]))
    await w.drain()
    await asyncio.sleep(0.4)
    part = dest_pc / ".pb-cancelkey.part"
    check("a partial exists while the upload runs", part.exists())
    check("…and is readable by this user only (0600)", part.exists() and (part.stat().st_mode & 0o777) == 0o600)
    await phone.send("file.cancel", {"transfer_id": tid_u})
    await asyncio.sleep(0.4)
    check("the phone's cancel removes the partial", not part.exists())
    check("no file was kept", not (dest_pc / "cancelled.bin").exists())
    check("the transfer is forgotten", tid_u not in file_transfer._IN)  # noqa: SLF001
    w.close()

    print("7d. stale partials are purged; hostile filenames stay inside the folder")
    import time as _t
    old = dest_pc / ".pb-ancient.part"
    old.write_bytes(b"x")
    os.utime(old, (_t.time() - 8 * 86400,) * 2)
    fresh = dest_pc / ".pb-recent.part"
    fresh.write_bytes(b"x")
    check("only partials older than a week are purged", file_transfer.purge_stale_partials(dest_pc) == 1
          and not old.exists() and fresh.exists())
    for evil in ("../../escape.txt", "/etc/passwd", "..", ".", "a/../../b", "x\\..\\y"):
        dest = file_transfer._unique_dest(dest_pc, evil)  # noqa: SLF001
        check(f"filename {evil!r} stays inside the download folder", dest.parent == dest_pc and dest.name not in ("", ".", ".."))

    print("8. data connection without the key is dropped")
    r, w = await open_tls(port)
    w.write(f"{device.fingerprint}:file:{tid}:0\n".encode())
    w.write(crypto.pack_frame(crypto.SessionCipher(os.urandom(32)).encrypt(b"PBFILE:" + tid.encode())))
    await w.drain()
    check("server closes it", await asyncio.wait_for(r.read(), 5) == b"")

    serve.cancel()
    shutil.rmtree(tmp_devices_dir.parent, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
