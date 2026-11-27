"""File transfer v2 — see protocol/PROTOCOL.md section 1c.

Every transfer gets its own TLS connection (like the video connection), so a big file never
blocks the control channel. The phone always opens that connection (the PC is the server):

    phone -> PC   "<fp>:file:<transfer_id>:<offset>\\n", then one frame  b"PBFILE:<transfer_id>"
                  (proves the phone holds the session key before anything is streamed)
    sender        AES-GCM frames of up to CHUNK bytes, starting at <offset>, then one trailer
                  frame  b"SHA256:<hex of the WHOLE file>"
    receiver      one frame  b"OK"  or  b"BAD"  (hash verified / not)

Control-channel messages: file.offer {transfer_id, filename, size, mime, resume_key, data_conn}
from the sender; file.accept {transfer_id, offset} / file.reject {transfer_id, reason} from the
receiver. A retried transfer of the same file (same resume_key) continues from the partial
file left by the interrupted attempt.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .netutil import spawn
from . import crypto
from .transport import DeviceConnection

log = logging.getLogger("phonebridge.file_transfer")

CHUNK_SIZE = 256 * 1024
OFFER_TIMEOUT = 120  # seconds the receiver has to answer / connect
STALL_TIMEOUT = 120  # an incoming transfer that delivers nothing for this long is over
AUTH_PREFIX = b"PBFILE:"
HASH_PREFIX = b"SHA256:"


class TransferError(Exception):
    pass


@dataclass
class _Outbound:
    path: Path
    size: int
    device_fp: str
    on_progress: object = None
    cancelled: bool = False
    writer: object = None   # the data connection, so a cancel can close it
    started: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    done: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


@dataclass
class _Inbound:
    payload: dict
    device_fp: str
    dest_dir: Path
    part_path: Path
    offset: int
    on_progress: object = None
    cancelled: bool = False
    writer: object = None
    session_id: str | None = None   # the control session the offer arrived on
    done: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


_OUT: dict[str, _Outbound] = {}
_IN: dict[str, _Inbound] = {}


def _open_part(path: Path):
    """Opens (creating if needed) a partial-download file readable by this user only (0600): half-received data must
    not be visible to other local users or to apps that can read the download folder."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    return os.fdopen(fd, "r+b")


STALE_PARTIAL_SECONDS = 7 * 24 * 3600


def purge_stale_partials(dest_dir: Path, max_age: float = STALE_PARTIAL_SECONDS) -> int:
    """Deletes abandoned partial downloads (.pb-*.part) older than `max_age`, so interrupted transfers don't linger."""
    import time

    n = 0
    for p in dest_dir.glob(".pb-*.part"):
        try:
            if time.time() - p.stat().st_mtime > max_age:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


async def cancel_transfer(conn, tid: str, notify: bool = True) -> bool:
    """Cancels a running transfer in either direction: stops the stream, closes the data connection, deletes the partial
    download and (if `notify`) tells the other side. Returns False if there was no such transfer."""
    out, inb = _OUT.get(tid), _IN.get(tid)
    target = out or inb
    if target is None or target.device_fp != conn.fingerprint:
        return False
    target.cancelled = True
    if target.writer is not None:
        try:
            target.writer.close()
        except Exception:  # noqa: BLE001
            pass
    if inb is not None:
        inb.part_path.unlink(missing_ok=True)
        if not inb.done.done():
            inb.done.set_exception(TransferError("cancelled"))
    if out is not None and not out.done.done():
        out.done.set_exception(TransferError("cancelled"))
        if not out.started.done():   # not accepted yet: send_file is waiting on this and must end now, not at the timeout
            out.done.exception()     # nobody will await `done`, so mark its error as seen
            out.started.set_exception(TransferError("cancelled"))
    if not out and not inb:
        return False
    if notify:
        try:
            await conn.send("control", "file.cancel", {"transfer_id": tid})
        except Exception:  # noqa: BLE001
            pass
    return True


def _audit(conn, direction: str, size: int, ok: bool, error: str = "") -> None:
    """Ledger entry for a transfer: direction, size and outcome only — never the file name or contents."""
    server = getattr(conn, "server", None)
    if server is not None:
        from .security import ledger

        server.audit(ledger.FILE_TRANSFER, conn.fingerprint, direction=direction, bytes=size, ok=ok,
                     **({"error": error[:80]} if error else {}))


def resume_key(path: Path) -> str:
    st = path.stat()
    return hashlib.sha1(f"{path.name}|{st.st_size}|{st.st_mtime_ns}".encode(), usedforsecurity=False).hexdigest()[:24]


def _safe_key(key: str) -> str:
    return "".join(ch for ch in key if ch.isalnum())[:40]


def _claim_part(dest_dir: Path, key: str, tid: str, session_id: str | None) -> tuple[Path, bool]:
    """(partial file, may a later attempt resume it) for a new incoming transfer. Two transfers must never write one
    partial: each checks only the bytes it received, so the mix would be saved as a good file."""
    part = dest_dir / f".pb-{key}.part"
    for other in list(_IN.values()):
        if other.part_path != part or other.done.done():
            continue
        if other.session_id == session_id:   # the same file twice at once: the second gets a partial of its own
            return dest_dir / f".pb-{key}-{_safe_key(tid)}.part", False
        # Left over from a link that has since been replaced: this is the retry, so end the old one and resume.
        other.cancelled = True
        if other.writer is not None:
            other.writer.close()
        other.done.set_exception(TransferError("the connection to the phone was lost"))
    return part, True


async def _read_frame(reader: asyncio.StreamReader, cipher: crypto.SessionCipher) -> bytes:
    n = crypto.unpack_frame_len(await reader.readexactly(crypto.FRAME_LEN_BYTES))
    return cipher.decrypt(await reader.readexactly(n))


async def _write_frame(writer: asyncio.StreamWriter, cipher: crypto.SessionCipher, data: bytes) -> None:
    writer.write(crypto.pack_frame(cipher.encrypt(data)))
    await writer.drain()


# ----------------------------------------------------------------------- sending (PC -> phone)

async def send_file(conn: DeviceConnection, path: Path, on_progress=None, extra_offer_fields: dict | None = None,
                    on_offer=None) -> None:
    """Offers `path` to the phone and returns once the phone has stored it and the hash matched.
    Raises TransferError if the phone rejects it or never connects for the data."""
    size = path.stat().st_size
    transfer_id = uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    out = _Outbound(path=path, size=size, device_fp=conn.fingerprint, on_progress=on_progress)
    _OUT[transfer_id] = out
    offer = {"transfer_id": transfer_id, "filename": path.name, "size": size, "mime": mime,
             "resume_key": resume_key(path), "data_conn": True}
    offer.update(extra_offer_fields or {})
    try:
        await conn.send("control", "file.offer", offer)
        if on_offer:
            on_offer(transfer_id)   # lets a UI offer a Cancel button for this transfer
        try:
            await asyncio.wait_for(asyncio.shield(out.started), OFFER_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise TransferError("the phone didn't accept the file in time") from exc
        await out.done
        _audit(conn, "out", size, True)
    except Exception as exc:
        _audit(conn, "out", size, False, str(exc))
        raise
    finally:
        _OUT.pop(transfer_id, None)


async def _stream_outbound(out: _Outbound, offset: int, reader, writer, cipher) -> None:
    if offset < 0 or offset > out.size:
        offset = 0
    digest = hashlib.sha256()
    loop = asyncio.get_running_loop()
    with out.path.open("rb") as f:
        remaining = offset
        while remaining:  # the hash covers the whole file, including the part the phone already has
            block = await loop.run_in_executor(None, f.read, min(1 << 20, remaining))
            if not block:
                raise TransferError("file shrank while sending")
            digest.update(block)
            remaining -= len(block)
        sent = offset
        while True:
            if out.cancelled:
                raise TransferError("cancelled")
            chunk = await loop.run_in_executor(None, f.read, CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            await _write_frame(writer, cipher, chunk)
            sent += len(chunk)
            if out.on_progress:
                out.on_progress(sent, out.size)
    if sent != out.size:
        raise TransferError("file changed while sending")
    await _write_frame(writer, cipher, HASH_PREFIX + digest.hexdigest().encode())
    verdict = await asyncio.wait_for(_read_frame(reader, cipher), 120)
    if verdict != b"OK":
        raise TransferError("the phone says the file arrived damaged")


# ---------------------------------------------------------------- receiving (phone -> PC)

def _unique_dest(dest_dir: Path, name: str) -> Path:
    safe = Path(name).name or "file"  # strip any path components the sender included
    dest = dest_dir / safe
    n = 1
    while dest.exists():  # never overwrite an earlier download with the same name
        dest = dest_dir / f"{Path(safe).stem} ({n}){Path(safe).suffix}"
        n += 1
    return dest


async def accept_offer(conn: DeviceConnection, envelope, dest_dir: Path, on_progress=None, on_done=None,
                       on_failed=None) -> None:
    """Handler body for an incoming file.offer: checks space, picks the resume offset, answers
    file.accept/file.reject and then (in a background task) waits for the data connection.
    Ends in exactly one of on_done(path, size, payload) and on_failed(payload, reason)."""
    p = envelope.payload
    tid = str(p.get("transfer_id", ""))
    size = int(p.get("size", 0))

    def failed(reason: str) -> None:
        _fail_fetch(p, reason)
        if on_failed:
            on_failed(p, reason)

    if not p.get("data_conn") or not tid or size < 0:
        failed("unsupported transfer; update PhoneBridge")
        await conn.send("control", "file.reject", {"transfer_id": tid, "reason": "unsupported transfer; update PhoneBridge"})
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    purge_stale_partials(dest_dir)
    key = _safe_key(str(p.get("resume_key", ""))) or _safe_key(tid)
    session_id = getattr(conn, "session_id", None)
    part, resumable = _claim_part(dest_dir, key, tid, session_id)
    offset = part.stat().st_size if part.exists() else 0
    if offset >= size:  # stale/complete leftovers: start over
        part.unlink(missing_ok=True)
        offset = 0
    if shutil.disk_usage(dest_dir).free < (size - offset) + (64 << 20):
        failed("not enough free space on the PC")
        await conn.send("control", "file.reject", {"transfer_id": tid, "reason": "not enough free space on the PC"})
        return
    inb = _Inbound(payload=p, device_fp=conn.fingerprint, dest_dir=dest_dir, part_path=part, offset=offset,
                   on_progress=on_progress, session_id=session_id)
    _IN[tid] = inb
    await conn.send("control", "file.accept", {"transfer_id": tid, "offset": offset})

    async def finish() -> None:
        try:
            # The limit is for the data connection to show up. A running transfer may take as long as it keeps
            # delivering (STALL_TIMEOUT in _stream_inbound ends one that doesn't).
            await asyncio.wait({inb.done}, timeout=OFFER_TIMEOUT)
            if not inb.done.done() and inb.writer is None:
                raise TransferError("the phone didn't send the file")
            dest = await inb.done
            _audit(conn, "in", size, True)
            if on_done:
                on_done(dest, size, p)
        except Exception as exc:  # noqa: BLE001
            _audit(conn, "in", size, False, str(exc))
            log.warning("incoming transfer %s failed: %s", p.get("filename"), exc)
            if not resumable:
                part.unlink(missing_ok=True)   # no later attempt would look for it
            failed(str(exc) or "transfer failed")
        finally:
            _IN.pop(tid, None)

    spawn(finish())


async def _read_incoming(reader: asyncio.StreamReader, cipher: crypto.SessionCipher) -> bytes:
    """The next frame of an incoming file, with failures worded for the person waiting for it."""
    try:
        return await asyncio.wait_for(_read_frame(reader, cipher), STALL_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise TransferError("the phone stopped sending") from exc
    except (asyncio.IncompleteReadError, ConnectionError) as exc:
        raise TransferError("the connection to the phone was lost") from exc


async def _stream_inbound(inb: _Inbound, reader, writer, cipher) -> None:
    size = int(inb.payload["size"])
    digest = hashlib.sha256()
    loop = asyncio.get_running_loop()
    inb.dest_dir.mkdir(parents=True, exist_ok=True)
    with _open_part(inb.part_path) as f:
        f.truncate(inb.offset)
        f.seek(0)
        remaining = inb.offset
        while remaining:
            block = await loop.run_in_executor(None, f.read, min(1 << 20, remaining))
            digest.update(block)
            remaining -= len(block)
        f.seek(inb.offset)
        received = inb.offset
        while received < size:
            chunk = await _read_incoming(reader, cipher)
            if inb.cancelled:
                raise TransferError("cancelled")
            if received + len(chunk) > size:
                raise TransferError("sender sent more bytes than it offered")
            digest.update(chunk)
            await loop.run_in_executor(None, f.write, chunk)
            received += len(chunk)
            if inb.on_progress:
                inb.on_progress(received, size)
        trailer = await _read_incoming(reader, cipher)
    ok = trailer == HASH_PREFIX + digest.hexdigest().encode()
    await _write_frame(writer, cipher, b"OK" if ok else b"BAD")
    if not ok:
        inb.part_path.unlink(missing_ok=True)  # corrupt: don't resume from garbage
        raise TransferError("checksum mismatch")
    dest = _unique_dest(inb.dest_dir, str(inb.payload.get("filename", "file")))
    os.replace(inb.part_path, dest)
    log.info("received %s (%d bytes, sha256 ok)", dest.name, size)
    inb.done.set_result(dest)


# ------------------------------------------------------------------ server-side plumbing

async def handle_data_connection(device, tid: str, offset: int, reader, writer, cipher) -> None:
    """Called by the control server for an ident of the form "<fp>:file:<tid>:<offset>"; `cipher` is the key the
    control session derives for this transfer (secure_session.Session.data_cipher)."""
    out = _OUT.get(tid)
    inb = _IN.get(tid)
    try:
        auth = await asyncio.wait_for(_read_frame(reader, cipher), 15)
        if auth != AUTH_PREFIX + tid.encode():
            raise TransferError("bad data-connection handshake")
        if out is not None and out.device_fp == device.fingerprint and not out.started.done():
            out.started.set_result(True)
            out.writer = writer
            try:
                await _stream_outbound(out, offset, reader, writer, cipher)
                if not out.done.done():
                    out.done.set_result(True)
            except Exception as exc:  # noqa: BLE001
                if not out.done.done():   # a cancel may already have settled it
                    out.done.set_exception(exc if isinstance(exc, TransferError) else TransferError(str(exc) or repr(exc)))
        elif inb is not None and inb.device_fp == device.fingerprint and not inb.done.done():
            inb.writer = writer
            try:
                await _stream_inbound(inb, reader, writer, cipher)
            except Exception as exc:  # noqa: BLE001
                if not inb.done.done():
                    inb.done.set_exception(exc if isinstance(exc, TransferError) else TransferError(str(exc) or repr(exc)))
        else:
            raise TransferError("unknown transfer")
    except Exception as exc:  # noqa: BLE001
        log.info("file data connection for %s ended: %s", tid, exc)
    finally:
        writer.close()


def on_reject(_conn, envelope) -> None:
    """The receiver turned an offered file down (file.reject)."""
    tid = str(envelope.payload.get("transfer_id", ""))
    out = _OUT.get(tid)
    if out is not None and not out.started.done():
        out.started.set_exception(TransferError(str(envelope.payload.get("reason") or "the phone rejected the file")))


def on_denied(conn, envelope) -> None:
    """The phone's permission manager refused a request (permission.denied); a refused file.offer gets no file.reject.
    Older phone apps don't say which offer: then it is only ended if it is the single one waiting."""
    p = envelope.payload
    if p.get("type") != "file.offer":
        return
    waiting = {tid: out for tid, out in _OUT.items() if out.device_fp == conn.fingerprint and not out.started.done()}
    tid = str(p.get("transfer_id", ""))
    if not tid and len(waiting) == 1:
        tid = next(iter(waiting))
    out = waiting.get(tid)
    if out is not None:
        out.started.set_exception(TransferError(
            "the phone didn't allow it: check the “Files” permission in PhoneBridge on the phone"))


# Files the phone-side file-manager mount asked for (files.get -> source_path): they are saved in the
# mount's cache instead of Downloads, and the waiting reader is released when they arrive.
_FETCHES: dict[str, tuple[Path, "asyncio.Future"]] = {}


def expect_fetch(source_path: str, cache_dir: Path) -> "asyncio.Future":
    """Call on the server's event loop before sending files.get; the future resolves to the saved Path."""
    fut = asyncio.get_running_loop().create_future()
    _FETCHES[source_path] = (cache_dir, fut)
    return fut


def cancel_fetch(source_path: str) -> None:
    _FETCHES.pop(source_path, None)


def _fail_fetch(payload: dict, reason: str) -> None:
    pending = _FETCHES.pop(str(payload.get("source_path", "")), None)
    if pending is not None and not pending[1].done():
        pending[1].set_exception(TransferError(reason))


def register(server, dest_dir_for, on_progress_for=None, on_done=None, on_failed=None) -> None:
    """Wires file.offer / file.reject / file.cancel / permission.denied into a ControlChannelServer.
    dest_dir_for(payload) -> Path; on_progress_for(payload) -> callable|None; on_done(path, size, payload);
    on_failed(payload, reason) for an incoming file that won't arrive."""

    def done(path, size, payload) -> None:
        pending = _FETCHES.pop(str(payload.get("source_path", "")), None)
        if pending is not None:
            if not pending[1].done():
                pending[1].set_result(path)
            return
        if on_done:
            on_done(path, size, payload)

    async def offer(conn, envelope) -> None:
        payload = envelope.payload
        payload["_fingerprint"] = conn.fingerprint   # so a UI can address a Cancel to the right phone
        routed = _FETCHES.get(str(payload.get("source_path", "")))
        await accept_offer(conn, envelope, routed[0] if routed else dest_dir_for(payload),
                           on_progress=None if routed else (on_progress_for(payload) if on_progress_for else None),
                           on_done=done, on_failed=None if routed else on_failed)

    async def reject(conn, envelope) -> None:
        on_reject(conn, envelope)

    async def cancel(conn, envelope) -> None:
        await cancel_transfer(conn, str(envelope.payload.get("transfer_id", "")), notify=False)

    async def denied(conn, envelope) -> None:
        on_denied(conn, envelope)

    server.on("file.cancel", cancel)
    server.on("file.offer", offer)
    server.on("file.reject", reject)
    server.on("permission.denied", denied)
