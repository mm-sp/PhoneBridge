"""PC side of the Bluetooth RFCOMM pairing exchange — see protocol/PROTOCOL.md section 1.

Uses Python's stdlib `socket.AF_BLUETOOTH`/`BTPROTO_RFCOMM`, available on Linux with BlueZ;
no extra dependency. This only runs on a real Linux desktop with a Bluetooth adapter —
there is no Bluetooth socket access from inside this Termux/Android dev shell, so this
module is written and reviewed carefully but has not been exercised against real hardware.
"""
from __future__ import annotations

import json
import select
import socket
import threading
import time
from collections.abc import Callable

from . import crypto
from .pairing import PairedDevice, PairingError, PendingPairing

RFCOMM_CHANNEL = 4  # arbitrary fixed channel; advertised via SDP record using SERVICE_UUID
_ACCEPT_POLL_SECONDS = 0.5  # how often a blocked accept() re-checks its cancel event


def _send_line(sock: socket.socket, obj: dict) -> None:
    sock.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))


MAX_PAIRING_LINE = 8192     # a pairing message is a few hundred bytes
PAIRING_READ_DEADLINE = 30  # seconds for one whole message: a slow-drip peer can't hold the single pairing slot
# After the PC's user clicks Allow, the phone asks its own user to compare the code (the app waits 3 minutes) before it
# sends its identity key. Giving up earlier would leave the phone believing in a pairing this PC has thrown away.
PHONE_CONFIRM_DEADLINE = 190


class _NotConfirmed(PairingError):
    """The phone's side of the confirmation never came."""


def _await_phone(sock: socket.socket, cancel: threading.Event | None) -> None:
    """Waits for the phone to start answering, for as long as its user may be deciding."""
    deadline = time.monotonic() + PHONE_CONFIRM_DEADLINE
    while not select.select([sock], [], [], _ACCEPT_POLL_SECONDS)[0]:
        if cancel is not None and cancel.is_set():
            raise _NotConfirmed("pairing was closed on the PC before the phone confirmed it")
        if time.monotonic() > deadline:
            raise _NotConfirmed("the code was not confirmed on the phone in time")


def _recv_line(sock: socket.socket) -> dict:
    buf = bytearray()
    deadline = time.monotonic() + PAIRING_READ_DEADLINE
    while True:
        if time.monotonic() > deadline:
            raise PairingError("the phone took too long to send its message")
        chunk = sock.recv(1)
        if not chunk:
            raise PairingError("connection closed before expected message")
        if chunk == b"\n":
            break
        buf += chunk
        if len(buf) > MAX_PAIRING_LINE:
            raise PairingError("pairing message too long")
    try:
        return json.loads(buf.decode("utf-8"))
    except ValueError as exc:
        raise PairingError("malformed pairing message") from exc


def _send_encrypted_frame(sock: socket.socket, cipher: crypto.SessionCipher, obj: dict) -> None:
    plaintext = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    blob = cipher.encrypt(plaintext)
    sock.sendall(crypto.pack_frame(blob))


def _recv_encrypted_frame(sock: socket.socket, cipher: crypto.SessionCipher) -> dict:
    deadline = time.monotonic() + PAIRING_READ_DEADLINE

    def read_exact(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            if time.monotonic() > deadline:
                raise PairingError("the phone took too long to send its identity key")
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise PairingError("connection closed before the phone's identity key arrived")
            buf += chunk
        return buf

    n = crypto.unpack_frame_len(read_exact(crypto.FRAME_LEN_BYTES))
    return json.loads(cipher.decrypt(read_exact(n)))


class PairingCancelled(Exception):
    """Raised by accept_one_pairing when its `cancel` event is set before a phone connects."""


ConfirmFn = Callable[[str, str], bool]
"""confirm(phone_name, confirmation_code) -> True if the user allowed this phone on the PC."""


def run_pairing_handshake(
    conn: socket.socket, pending: PendingPairing, lan_ip: str, lan_port: int, cert_fingerprint: str,
    confirm: ConfirmFn, cancel: threading.Event | None = None,
) -> PairedDevice:
    """The PROTOCOL.md section 1 exchange over an already-connected stream socket —
    identical whether the phone reached us over Bluetooth RFCOMM or Wi-Fi TCP.

    Nothing is saved until the phone has proved it knows the PIN *and* the user has
    allowed it via `confirm` (which may block while a dialog is shown).

    A connection that stalls or is reset raises PairingError, like every other failed attempt: an OSError here
    would read as "this pairing route is broken" and stop the dialog from listening."""
    try:
        return _pairing_exchange(conn, pending, lan_ip, lan_port, cert_fingerprint, confirm, cancel)
    except OSError as exc:
        raise PairingError(f"the connection to the phone failed ({exc})") from exc


def _pairing_exchange(
    conn: socket.socket, pending: PendingPairing, lan_ip: str, lan_port: int, cert_fingerprint: str,
    confirm: ConfirmFn, cancel: threading.Event | None,
) -> PairedDevice:
    nonce = crypto.gen_nonce()
    # PC speaks first (the "offer") — see PROTOCOL.md section 1. The PIN is never sent.
    _send_line(conn, pending.offer_json(nonce))

    phone_hello = _recv_line(conn)
    phone_name = str(phone_hello.get("name", "Phone"))[:100]
    # SPAKE2: share exchange, then the phone proves it knows the PIN first (so a wrong guess gets nothing back but a refusal
    # and is counted), then the PC proves it back.
    try:
        session = pending.begin(phone_hello, nonce)
    except PairingError as exc:
        _send_line(conn, {"ok": False, "error": str(exc)})
        raise
    import base64

    _send_line(conn, {"spake": base64.b64encode(session.share).decode("ascii")})
    try:
        phone_pub = pending.verify_phone_confirmation(session, _recv_line(conn).get("confirm"))
    except PairingError as exc:
        _send_line(conn, {"ok": False, "error": str(exc)})
        raise
    _send_line(conn, {"confirm": pending.pc_confirmation(session)})

    if not confirm(phone_name, pending.confirmation_code(phone_pub)):
        _send_line(conn, {"ok": False, "error": "pairing was declined on the PC"})
        raise PairingError(f"you declined {phone_name}")

    device = pending.complete(session, phone_name, str(phone_hello.get("platform", "android")))
    # The phone answers with its Ed25519 identity public key (encrypted under the new pairing key). Without it
    # the pairing is unusable (v4 connections need a signature), so any failure from here on - including a connection
    # that drops before the phone has everything - aborts and forgets the pairing.
    try:
        _send_line(conn, {"ok": True, "name": pending.pc_name})
        cipher = crypto.SessionCipher(device.session_key, role="pc")
        _send_encrypted_frame(conn, cipher, pending.lan_info_message(lan_ip, lan_port, cert_fingerprint))
        _await_phone(conn, cancel)
        identity = _recv_encrypted_frame(conn, cipher)
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(base64.b64decode(str(identity["id_pub"])))   # raises unless a valid key
        device.identity_pub = str(identity["id_pub"])
    except Exception as exc:  # noqa: BLE001
        from .pairing import forget_device

        forget_device(device.fingerprint)
        if isinstance(exc, (OSError, _NotConfirmed)):
            raise   # a failed connection (reported as such by run_pairing_handshake) or no answer: not an outdated phone app
        raise PairingError(f"the phone didn't provide a valid identity key ({exc}); update PhoneBridge on the phone") from exc
    device.bt_addr = pending.bt_addr
    device.last_lan_ip = lan_ip
    device.last_lan_port = lan_port
    device.cert_fingerprint = cert_fingerprint
    device.save()
    return device


def accept_until_cancelled(server: socket.socket, cancel: threading.Event | None) -> socket.socket:
    """accept() that re-checks `cancel` every _ACCEPT_POLL_SECONDS (raises PairingCancelled)."""
    server.settimeout(_ACCEPT_POLL_SECONDS)
    while True:
        if cancel is not None and cancel.is_set():
            raise PairingCancelled()
        try:
            conn, _addr = server.accept()
        except socket.timeout:
            continue
        conn.settimeout(30)  # a phone that connects then stalls mustn't hang the dialog
        return conn


def accept_one_pairing(
    pending: PendingPairing,
    lan_ip: str,
    lan_port: int,
    cert_fingerprint: str,
    confirm: ConfirmFn,
    cancel: threading.Event | None = None,
) -> PairedDevice:
    """Blocks listening on RFCOMM channel `RFCOMM_CHANNEL` for exactly one pairing attempt.

    Call this right after rendering the QR code; it returns once a phone has completed
    the handshake (or raises PairingError on a bad/expired token). Setting `cancel` (e.g.
    when the pairing dialog is closed) stops listening and frees the channel, raising
    PairingCancelled — otherwise the socket would stay bound and the next attempt would
    fail with "Address already in use".
    """
    server = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    try:
        server.bind((pending.bt_addr, RFCOMM_CHANNEL))
        server.listen(1)
        conn = accept_until_cancelled(server, cancel)
        try:
            return run_pairing_handshake(conn, pending, lan_ip, lan_port, cert_fingerprint, confirm, cancel)
        finally:
            conn.close()
    finally:
        server.close()
