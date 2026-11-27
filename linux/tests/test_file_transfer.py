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
    failures: list = []
    file_transfer.register(server, lambda _p: dest_pc, None, lambda path, size, p: received_files.append(path),
                           lambda p, reason: failures.append((p.get("filename"), reason)))

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

    print("5b. the phone's permission manager refuses an offer (permission.denied, no file.reject)")

    async def outcome(task, wait=5.0):
        try:
            await asyncio.wait_for(asyncio.shield(task), wait)
            return "sent"
        except file_transfer.TransferError as exc:
            return str(exc)
        except asyncio.TimeoutError:
            return "waiting"

    async def offer_one():
        task = asyncio.ensure_future(file_transfer.send_file(conn, src))
        return task, (await asyncio.wait_for(phone.recv(), 5)).payload["transfer_id"]

    first, tid_1 = await offer_one()
    second, tid_2 = await offer_one()
    await phone.send("permission.denied", {"feature": "sms", "type": "sms.list_threads"})
    await phone.send("permission.denied", {"feature": "files", "type": "file.offer"})
    check("a refusal that names no offer ends none of two", await outcome(first, 0.5) == "waiting" and not second.done())
    await phone.send("permission.denied", {"feature": "files", "type": "file.offer", "transfer_id": tid_2})
    check("the named offer ends at once, saying which permission", "Files" in await outcome(second))
    check("…and only that one", not first.done())
    await phone.send("permission.denied", {"feature": "files", "type": "file.offer"})
    check("without an id the single waiting offer ends", "didn't allow" in await outcome(first))
    check("refused offers are forgotten", tid_1 not in file_transfer._OUT and tid_2 not in file_transfer._OUT)  # noqa: SLF001

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

    sendp = asyncio.ensure_future(file_transfer.send_file(conn, big2))
    tid_p = (await phone.recv()).payload["transfer_id"]       # offered, but the phone has not accepted it yet
    check("a transfer can be cancelled before the phone accepts it", await file_transfer.cancel_transfer(conn, tid_p))
    try:
        await asyncio.wait_for(sendp, 5)
        reason = "no error"
    except file_transfer.TransferError as exc:
        reason = str(exc)
    except asyncio.TimeoutError:
        reason = "still waiting for the phone"
    check("…and send_file ends at once with 'cancelled'", reason == "cancelled")
    note = await asyncio.wait_for(phone.recv(), 5)
    check("…the phone is told", note.type == "file.cancel" and note.payload["transfer_id"] == tid_p)
    rp, wp = await phone.data_connection(tid_p, 0)
    check("…a data connection arriving afterwards is given nothing", await asyncio.wait_for(rp.read(), 5) == b"")
    wp.close()

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

    print("7e. every incoming file that won't arrive is reported, once, with a reason")
    check("a damaged upload was reported", [r for n, r in failures if n == "bad.bin"] == ["checksum mismatch"])
    check("a cancelled upload was reported as cancelled", [r for n, r in failures if n == "cancelled.bin"] == ["cancelled"])
    check("a saved upload was not reported as failed", not [n for n, _r in failures if n == "up.bin"])

    async def offer_up(name: str, size: int) -> str:
        tid_x = hashlib.sha256(name.encode()).hexdigest()[:32]
        await phone.send("file.offer", {"transfer_id": tid_x, "filename": name, "size": size, "mime": "x/y",
                                        "resume_key": tid_x[:12], "data_conn": True})
        return tid_x

    async def reason_for(name: str, wait: float = 5.0) -> list:
        for _ in range(int(wait / 0.05)):
            if any(n == name for n, _r in failures):
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        return [r for n, r in failures if n == name]

    await offer_up("huge.bin", 1 << 62)
    answer = await asyncio.wait_for(phone.recv(), 5)
    check("an upload refused for lack of space is reported", answer.type == "file.reject"
          and await reason_for("huge.bin") == ["not enough free space on the PC"])

    tid_l = await offer_up("lost.bin", CHUNK * 4)
    await asyncio.wait_for(phone.recv(), 5)
    r, w = await phone.data_connection(tid_l, 0)
    w.write(phone.frame(os.urandom(CHUNK)))
    await w.drain()
    w.close()
    check("a dropped data connection is reported in plain words",
          await reason_for("lost.bin") == ["the connection to the phone was lost"])
    check("…and the partial stays for a resume", any(dest_pc.glob(".pb-*.part")))

    limits = file_transfer.OFFER_TIMEOUT, file_transfer.STALL_TIMEOUT
    try:
        file_transfer.OFFER_TIMEOUT, file_transfer.STALL_TIMEOUT = 0.6, 0.6
        tid_s = await offer_up("stall.bin", CHUNK * 4)
        await asyncio.wait_for(phone.recv(), 5)
        r, w = await phone.data_connection(tid_s, 0)
        w.write(phone.frame(os.urandom(CHUNK)))
        await w.drain()
        check("an upload that stops delivering ends", await reason_for("stall.bin") == ["the phone stopped sending"])
        check("…and its data connection is closed", await asyncio.wait_for(r.read(), 5) == b"")
        w.close()

        tid_n = await offer_up("never.bin", 100)
        await asyncio.wait_for(phone.recv(), 5)
        check("an offer the phone never follows up ends", await reason_for("never.bin") == ["the phone didn't send the file"])
        check("…and is forgotten", tid_n not in file_transfer._IN)  # noqa: SLF001

        slow = os.urandom(8 * 1000)
        tid_w = await offer_up("slow.bin", len(slow))
        await asyncio.wait_for(phone.recv(), 5)
        r, w = await phone.data_connection(tid_w, 0)
        for i in range(0, len(slow), 1000):   # 1.6 s in all: longer than either limit, never silent for that long
            await asyncio.sleep(0.2)
            w.write(phone.frame(slow[i:i + 1000]))
            await w.drain()
        w.write(phone.frame(b"SHA256:" + hashlib.sha256(slow).hexdigest().encode()))
        await w.drain()
        verdict = await asyncio.wait_for(phone.read_frame(r), 5)
        w.close()
        await asyncio.sleep(0.3)
        check("a slow but steady upload outlasts the limits and is saved", verdict == b"OK"
              and bool(received_files) and received_files[-1].name == "slow.bin" and not await reason_for("slow.bin", 0.1))
    finally:
        file_transfer.OFFER_TIMEOUT, file_transfer.STALL_TIMEOUT = limits

    print("7f. the same file sent twice at once never shares a partial")
    twice = os.urandom(CHUNK * 4)

    async def offer_same(tid_x: str, name: str) -> None:
        await phone.send("file.offer", {"transfer_id": tid_x, "filename": name, "size": len(twice), "mime": "x/y",
                                        "resume_key": "samekey", "data_conn": True})

    async def upload(r, w, from_byte: int, to_byte: int | None = None) -> None:
        for i in range(from_byte, len(twice) if to_byte is None else to_byte, CHUNK):
            w.write(phone.frame(twice[i:i + CHUNK]))
        if to_byte is None:
            w.write(phone.frame(b"SHA256:" + hashlib.sha256(twice).hexdigest().encode()))
        await w.drain()

    await offer_same("w" * 32, "twice.bin")
    first_answer = await asyncio.wait_for(phone.recv(), 5)
    r1, w1 = await phone.data_connection("w" * 32, first_answer.payload["offset"])
    cipher1 = phone.dcipher
    await upload(r1, w1, 0, CHUNK * 3)
    await asyncio.sleep(0.4)
    await offer_same("x" * 32, "twice.bin")
    second_answer = await asyncio.wait_for(phone.recv(), 5)
    check("the second transfer starts from nothing, in its own partial", second_answer.payload["offset"] == 0)
    r2, w2 = await phone.data_connection("x" * 32, 0)
    cipher2 = phone.dcipher
    await upload(r2, w2, 0, CHUNK)   # the second has written only its start when the first finishes
    await asyncio.sleep(0.4)
    phone.dcipher = cipher1
    await upload(r1, w1, CHUNK * 3)
    verdict_1 = await asyncio.wait_for(phone.read_frame(r1), 5)
    await asyncio.sleep(0.3)
    saved_1 = (dest_pc / "twice.bin").read_bytes()
    phone.dcipher = cipher2
    await upload(r2, w2, CHUNK)
    verdict_2 = await asyncio.wait_for(phone.read_frame(r2), 5)
    w1.close()
    w2.close()
    await asyncio.sleep(0.3)
    check("the first file is whole as soon as it is reported saved", verdict_1 == b"OK" and saved_1 == twice)
    check("…and so is the second, under its own name", verdict_2 == b"OK"
          and (dest_pc / "twice (1).bin").read_bytes() == twice)

    await offer_same("y" * 32, "again.bin")
    await asyncio.wait_for(phone.recv(), 5)
    r1, w1 = await phone.data_connection("y" * 32, 0)
    await upload(r1, w1, 0, CHUNK * 2)
    await asyncio.sleep(0.4)
    await offer_same("z" * 32, "again.bin")
    await asyncio.wait_for(phone.recv(), 5)
    r2, w2 = await phone.data_connection("z" * 32, 0)
    await upload(r2, w2, 0, CHUNK)
    await asyncio.sleep(0.4)
    extra = list(dest_pc.glob(".pb-samekey-*.part"))
    w2.close()
    await asyncio.sleep(0.4)
    check("a second transfer that fails leaves no partial nothing would resume", len(extra) == 1 and not extra[0].exists())
    check("…while the first keeps its own", (dest_pc / ".pb-samekey.part").stat().st_size == CHUNK * 2)

    # The link is replaced while the first still hangs: the retry takes over the partial and resumes it.
    await phone.w.drain()
    phone.w.close()
    await asyncio.sleep(0.3)
    await phone.connect()
    await asyncio.sleep(0.3)
    old_r = r1
    await offer_same("q" * 32, "again.bin")
    retry_answer = await asyncio.wait_for(phone.recv(), 5)
    check("a retry after a reconnect resumes where the stuck transfer got to", retry_answer.payload["offset"] == CHUNK * 2)
    check("…and the stuck one is ended", await asyncio.wait_for(old_r.read(), 5) == b""
          and "y" * 32 not in file_transfer._IN)  # noqa: SLF001
    r2, w2 = await phone.data_connection("q" * 32, CHUNK * 2)
    await upload(r2, w2, CHUNK * 2)
    verdict = await asyncio.wait_for(phone.read_frame(r2), 5)
    w2.close()
    w1.close()
    await asyncio.sleep(0.3)
    check("…then completes intact", verdict == b"OK" and (dest_pc / "again.bin").read_bytes() == twice)
    conn = server.get_connection(device.fingerprint)

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
