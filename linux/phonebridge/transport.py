"""The persistent Wi-Fi/TLS control channel — see protocol/PROTOCOL.md sections 2-4.

Each connected phone gets its own asyncio connection, keyed by its pairing fingerprint.
Every frame (regardless of transport) is [4-byte length][AES-GCM ciphertext] where the
plaintext is a control.py Envelope's JSON — the TLS layer only protects against passive
eavesdropping/MITM on the wire; the AES-GCM layer (keyed by the per-device session_key
from pairing) is what actually authenticates "this is the phone I paired with".
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import os
import time
import uuid

from . import crypto, peer_policy, tls_identity
from .security import ledger as sec_ledger, permissions as perms
from .security.throttle import AuthThrottle
from . import pairing as pairing_mod
from .pairing import PairedDevice, pc_identity
from .protocol import Envelope, SeqCounter

log = logging.getLogger("phonebridge.transport")

DEFAULT_PORT = 28100
# The phone pings every 30 s; if nothing at all arrives for this long the link is dead
# (e.g. the PC or phone moved to another network and TCP never got a reset).
IDLE_TIMEOUT = 100
FIRST_FRAME_TIMEOUT = 15
ASK_TIMEOUT = 60          # an unanswered "ask" prompt counts as "no"
ASK_RETRY_SECONDS = 120   # ...and the same feature is not asked again on that connection for this long
# Nothing is read (so no ping is answered) while a prompt is open, and the phone drops a link that is silent for 90 s:
# an unsolicited pong this often keeps it up, also when prompts for several features follow one another.
ASK_KEEPALIVE_SECONDS = 20
VIDEO_POLICY_SECONDS = 1.0   # how long a "screen" permission answer is reused for incoming video frames
MAX_IDENT_LINE = 200
THROTTLE_EXEMPT = ("127.0.0.1", "::1")   # the USB tunnel arrives from loopback; see ControlChannelServer._fail
UNPAIRED_MARKER = b"PBUNPAIRED\n"
# Only one phone may be connected at a time; others get this (+ the connected phone's name).
BUSY_MARKER = b"PBBUSY "

Handler = Callable[["DeviceConnection", Envelope], Awaitable[None]]


def secure_session_fp(public_b64: str) -> str:
    from . import secure_session

    return secure_session.fingerprint_of(public_b64)


class DeviceConnection:
    def __init__(self, fingerprint: str, device: PairedDevice, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.fingerprint = fingerprint
        self.device = device
        self._reader = reader
        self._writer = writer
        self._cipher = crypto.SessionCipher(device.session_key, role="pc")
        self._seq = SeqCounter()
        # Held across a whole file transfer (file_transfer.send_file) so no other message
        # can be written between a file.offer and its raw chunks.
        self.write_lock = asyncio.Lock()
        self._seen_seq: set[int] = set()
        self._max_seq = 0
        self.server: "ControlChannelServer | None" = None
        self.session = None   # secure_session.Session once the v4 handshake has run
        self.session_id = uuid.uuid4().hex   # names this connection in the audit ledger
        self.granted: set[str] = set()       # features the user approved ("ask") for this connection only
        # "ask" feature -> monotonic time before which it is not asked again (infinite after the user said no)
        self.ask_not_before: dict[str, float] = {}
        self.link = "lan"                    # "usb" | "lan" | "relay": how the phone reached the PC (see handle_stream)

    SEQ_WINDOW = 256

    def _check_replay(self, envelope: Envelope) -> None:
        """Rejects an envelope whose seq was already seen (or is far older than the newest). Peers number
        frames from 1 but may interleave a few across threads, so reordering inside a window is allowed."""
        n = envelope.seq
        if not isinstance(n, int) or n in self._seen_seq or n <= self._max_seq - self.SEQ_WINDOW:
            raise ValueError(f"replayed or invalid sequence number {n!r}")
        self._seen_seq.add(n)
        if n > self._max_seq:
            self._max_seq = n
            self._seen_seq = {x for x in self._seen_seq if x > n - self.SEQ_WINDOW}

    async def send(self, ch: str, type_: str, payload: dict[str, Any]) -> None:
        async with self.write_lock:
            await self.send_unlocked(ch, type_, payload)

    async def send_unlocked(self, ch: str, type_: str, payload: dict[str, Any]) -> None:
        """Caller must hold write_lock."""
        if self.session is not None:
            self.session.frames_sent += 1
        envelope = Envelope(ch=ch, type=type_, seq=self._seq.next(), payload=payload)
        blob = self._cipher.encrypt(envelope.to_json())
        self._writer.write(crypto.pack_frame(blob))
        await self._writer.drain()

    async def send_raw(self, data: bytes) -> None:
        """Used only for file/video chunk bytes — the receiver must already know (from a
        preceding file.offer/mirror.start's size/framing) that raw frames, not JSON
        envelopes, come next; see file_transfer.py. Caller must hold write_lock."""
        blob = self._cipher.encrypt(data)
        self._writer.write(crypto.pack_frame(blob))
        await self._writer.drain()

    async def recv_raw(self) -> bytes:
        header = await self._reader.readexactly(crypto.FRAME_LEN_BYTES)
        n = crypto.unpack_frame_len(header)
        blob = await self._reader.readexactly(n)
        return self._cipher.decrypt(blob)

    async def _recv(self) -> Envelope | None:
        envelope = Envelope.from_json(await self.recv_raw())
        self._check_replay(envelope)
        return envelope

    async def handshake(self, identity, tls_cert_fp: str) -> None:
        """v4 handshake (secure_session.py): reads the phone's signed hello, answers, and from then on this connection
        uses fresh per-direction keys. Raises HandshakeError / ConnectionError on any failure."""
        from . import secure_session

        frame = await self.recv_raw_plain()
        # The PC signs with the identity this phone trusts (the previous one until it acknowledges a rotation); the
        # phone may sign with its current or, mid-rotation, its previous identity key.
        self.identity_used = pairing_mod.pc_identity_for(self.device)
        reply, session = secure_session.server_hello(
            self.device.session_key, self.fingerprint, tls_cert_fp,
            pairing_mod.acceptable_phone_keys(self.device), self.identity_used, frame)
        self._writer.write(crypto.pack_frame(reply))
        await self._writer.drain()
        self.session = session
        self._cipher = session.cipher
        self.session_id = session.session_id

    async def after_handshake(self) -> None:
        """Identity housekeeping right after a successful handshake (see PROTOCOL.md 1h, rotation)."""
        device = PairedDevice.load(self.fingerprint)
        if device is None:
            return
        if device.identity_pub_prev and self.session.peer_identity == device.identity_pub:
            # the phone signed in with its new key: the old one is retired
            device = PairedDevice.update(self.fingerprint, identity_pub_prev=None) or device
        cur = pc_identity()
        if device.pc_pub_acked is None:   # pairing made before rotation existed: the phone got the current key at pairing
            device = PairedDevice.update(self.fingerprint, pc_pub_acked=cur.public_b64) or device
        if device.pc_pub_acked != cur.public_b64:  # this phone hasn't heard about the PC's new identity yet
            await self.send_pc_rotation(self.identity_used, cur.public_b64)

    async def send_pc_rotation(self, signer, new_pub: str) -> None:
        from . import secure_session

        await self.send("control", "identity.rotate", {
            "new_pub": new_pub, "sig": secure_session.sign_rotation(signer, self.fingerprint, new_pub)})

    async def recv_raw_plain(self) -> bytes:
        header = await self._reader.readexactly(crypto.FRAME_LEN_BYTES)
        return await self._reader.readexactly(crypto.unpack_frame_len(header))

    def close(self) -> None:
        self._writer.close()

    # ---- authenticated session termination (PROTOCOL.md 1i)
    ended_by: str | None = None   # "peer" (it sent session.end), "local" (we did) or None = the link just died

    async def end_session(self, reason: str = "closed") -> None:
        """Says goodbye with an authenticated message (so a forged TCP reset / truncation can be told apart from a real
        close), then closes and drops the session keys."""
        self.ended_by = "local"
        try:
            await asyncio.wait_for(self.send("control", "session.end", {"reason": reason}), 1.5)
        except Exception:  # noqa: BLE001 - the peer is already gone
            pass
        self.close()
        self.session = None   # drop the key material (Python can't zeroise immutable bytes; the references go away)

    # ---- session-key rotation (PROTOCOL.md section 1h): PC initiates, phone acknowledges, in-order on one stream
    _rekeying = False

    async def maybe_rekey(self) -> None:
        if self.session is None or self._rekeying or not self.session.needs_rekey():
            return
        self._rekeying = True
        async with self.write_lock:
            eph = self.session.start_rekey()
            await self.send_unlocked("control", "rekey", {"eph": eph, "gen": self.session.generation + 1})

    async def on_rekey_ack(self, payload: dict) -> None:
        """Phone answered: switch our receive key, announce it under the old send key, then switch sending."""
        async with self.write_lock:
            self.session.finish_rekey(str(payload["eph"]))
            await self.send_unlocked("control", "rekey.done", {})
            self.session.commit_send()
        self._rekeying = False
        if self.server is not None:
            self.server.audit(sec_ledger.REKEY, self.fingerprint, session=self.session_id, generation=self.session.generation)


VideoSink = Callable[[bytes], None]
ConnectionListener = Callable[["DeviceConnection", bool], None]

DISCOVERY_PORT = 28101


class _DiscoveryResponder(asyncio.DatagramProtocol):
    """Answers a paired phone's LAN broadcast so it can find this PC after the PC's IP
    changes (new DHCP lease) — replies only for fingerprints we have a pairing for."""

    # Per-source rate limit and a cache of valid tags: a LAN host can send these packets for free, and answering each one
    # must not cost file reads and keyring (D-Bus) lookups on the event loop.
    RATE_WINDOW, RATE_MAX, TAG_CACHE_SECONDS = 10.0, 8, 30.0

    def __init__(self, port: int) -> None:
        self._port = port
        self.transport: asyncio.DatagramTransport | None = None
        self._hits: dict[str, list[float]] = {}
        self._tags: tuple[float, int, set[str]] = (0.0, -1, set())

    def connection_made(self, transport) -> None:
        self.transport = transport

    def _rate_ok(self, ip: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(ip, []) if now - t < self.RATE_WINDOW]
        if len(hits) >= self.RATE_MAX:
            self._hits[ip] = hits
            return False
        hits.append(now)
        self._hits[ip] = hits
        if len(self._hits) > 1024:   # bounded memory under a spoofed-source flood
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self.RATE_WINDOW}
        return True

    def _valid_tags(self) -> set[str]:
        """The acceptable tags of every paired phone for the current time bucket (+-1), recomputed at most every 30 s."""
        now = time.monotonic()
        bucket = int(time.time() // crypto.DISCOVERY_BUCKET_SECONDS)
        cached_at, cached_bucket, tags = self._tags
        if now - cached_at < self.TAG_CACHE_SECONDS and cached_bucket == bucket:
            return tags
        tags = {crypto.discovery_tag(d.session_key, b) for d in PairedDevice.list_all() for b in (bucket - 1, bucket, bucket + 1)}
        self._tags = (now, bucket, tags)
        return tags

    def _known_phone(self, msg: dict) -> bool:
        """The broadcast carries a rotating HMAC tag (never the pairing fingerprint); answer
        only if it verifies against one of our paired phones' session keys."""
        tag = msg.get("tag")
        return isinstance(tag, str) and any(crypto.proof_matches(t, tag) for t in self._valid_tags())

    def datagram_received(self, data: bytes, addr) -> None:
        if not peer_policy.is_allowed(addr[0]) or not self._rate_ok(addr[0]):
            return   # not from a LAN/USB address, or too chatty
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if msg.get("phonebridge") == "pair_discover":
            # A new phone with only the PIN looking for a PC whose Pair dialog is open.
            from . import wifi_pairing

            active = wifi_pairing.active_pairing
            if active is None:
                return
            reply = json.dumps({"phonebridge": "pairing_here", **active}).encode()
            if self.transport is None:
                return
            self.transport.sendto(reply, addr)
            return
        if msg.get("phonebridge") != "discover" or not self._known_phone(msg):
            return
        reply = json.dumps({"phonebridge": "here", "port": self._port}).encode()
        if self.transport is None:
            return
        self.transport.sendto(reply, addr)


class ControlChannelServer:
    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self._handlers: dict[str, list[Handler]] = {}
        self._connections: dict[str, DeviceConnection] = {}
        self._video_sinks: dict[str, VideoSink] = {}
        self._video_policy: dict[str, tuple[float, str]] = {}   # fingerprint -> (valid until, "screen" mode)
        self._connection_listeners: list[ConnectionListener] = []
        self._server: asyncio.AbstractServer | None = None
        self._discovery: asyncio.DatagramTransport | None = None
        self._udp_video = None
        self.ssl_context: ssl.SSLContext | None = None
        self.relay = None   # relay_client.RelayClient when a relay is configured
        # fingerprint -> "udp" | "tcp": how that phone's video is arriving (shown in the mirror/webcam status).
        self.video_transport: dict[str, str] = {}
        self.cert_fingerprint: str = ""
        # Security engine (all optional: the server works without any of it, e.g. in unit tests).
        self.ledger: sec_ledger.SecurityLedger | None = None
        self.throttle = AuthThrottle()
        # async (device, feature) -> bool, set by the GUI to raise an "allow this?" prompt for "ask" permissions.
        self.ask_callback = None
        self.usb_serials: list[str] = []   # phones with an active USB tunnel (usb_link.py), shown in the dashboard
        self.locked_down = False   # "disconnect all": no new sessions until lift_lockdown()
        self._handlers.setdefault("identity.rotate", []).append(self._on_phone_rotate)
        self._handlers.setdefault("identity.rotated", []).append(self._on_pc_rotation_ack)

    # ------------------------------------------------------------------ security engine

    def audit(self, event: str, device: str | None = None, **meta) -> None:
        """Append to the signed ledger; never raises (a ledger problem must not take the connection down)."""
        if self.ledger is None:
            return
        try:
            self.ledger.append(event, device, **meta)
        except Exception:  # noqa: BLE001
            log.exception("audit ledger write failed")

    async def revoke(self, fingerprint: str, reason: str = "user") -> bool:
        """Forget a phone: delete its pairing + session key and drop any live connection."""
        from . import pairing

        existed = pairing.forget_device(fingerprint)
        conn = self._connections.pop(fingerprint, None)
        if conn is not None:
            await conn.end_session("revoked")
            self._notify_connection(conn, False)
        self.audit(sec_ledger.REVOKED, fingerprint, reason=reason, was_connected=conn is not None)
        return existed

    async def disconnect_all(self, lockdown: bool = True) -> int:
        """Emergency stop: close every session. With lockdown (default) no phone can open a new one until
        lift_lockdown() — pairings are kept, so this is reversible; use revoke() to remove a phone for good."""
        conns = list(self._connections.values())
        self.locked_down = lockdown
        self._connections.clear()
        await asyncio.gather(*(c.end_session("disconnect_all") for c in conns))
        for conn in conns:
            self._notify_connection(conn, False)
        self.audit(sec_ledger.DISCONNECT_ALL, None, sessions=len(conns), lockdown=lockdown)
        return len(conns)

    def lift_lockdown(self) -> None:
        if self.locked_down:
            self.locked_down = False
            self.audit(sec_ledger.PERMISSION, None, change="lockdown_lifted")

    async def _authorize(self, conn: "DeviceConnection", envelope: Envelope) -> bool:
        """Zero-trust check: every phone->PC message is mapped to a feature and checked against the device's
        current policy (re-read each time, so a change in the UI applies immediately)."""
        feature = perms.feature_for(envelope.type, envelope.payload)
        if feature is None or feature == "remote_control":   # remote control is enforced (and audited) by PcControl
            return True
        from .pairing import PairedDevice

        device = PairedDevice.load(conn.fingerprint)
        mode = perms.mode_of(device, feature) if device else perms.DENY
        if mode == perms.ASK and feature not in conn.granted and self.ask_callback is not None \
                and time.monotonic() >= conn.ask_not_before.get(feature, 0.0):
            try:
                if await asyncio.wait_for(self._ask_keeping_link(conn, device, feature), ASK_TIMEOUT):
                    conn.granted.add(feature)
                else:
                    conn.ask_not_before[feature] = float("inf")   # "no" lasts for this connection, like "yes"
            except Exception:  # noqa: BLE001 - timeout or UI failure counts as "no"
                # Nobody answered: don't stack a new prompt (and stall the link again) for every following message.
                conn.ask_not_before[feature] = time.monotonic() + ASK_RETRY_SECONDS
        if mode == perms.ALLOW or (mode == perms.ASK and feature in conn.granted):
            return True
        self.audit(sec_ledger.DENIED, conn.fingerprint, feature=feature, message=envelope.type, mode=mode)
        denial = {"feature": feature, "type": envelope.type}
        if envelope.type == "file.offer" and "transfer_id" in envelope.payload:
            denial["transfer_id"] = str(envelope.payload["transfer_id"])   # so the phone can end that upload at once
        await conn.send("control", "permission.denied", denial)
        return False

    async def _ask_keeping_link(self, conn: "DeviceConnection", device: PairedDevice, feature: str) -> bool:
        """ask_callback's answer, telling the phone meanwhile that the link is alive (ASK_KEEPALIVE_SECONDS).
        Cancelling this cancels the prompt."""
        answer = asyncio.ensure_future(self.ask_callback(device, feature))
        try:
            while not (await asyncio.wait({answer}, timeout=ASK_KEEPALIVE_SECONDS))[0]:
                try:
                    await conn.send("control", "pong", {})
                except ConnectionError:
                    pass   # the read loop notices a dead link once the prompt is over
            return answer.result()
        finally:
            answer.cancel()

    def _video_sink(self, fingerprint: str) -> VideoSink | None:
        """Where an incoming video frame goes, over TCP or UDP: the registered viewer, and only while the "screen"
        permission still allows this phone's live session, so switching it off also stops a running stream. The stored
        policy is re-read at most every VIDEO_POLICY_SECONDS (frames arrive far more often than that)."""
        sink = self._video_sinks.get(fingerprint)
        owner = self._connections.get(fingerprint)
        if sink is None or owner is None or self.locked_down:
            return None
        now = time.monotonic()
        expires, mode = self._video_policy.get(fingerprint, (0.0, perms.DENY))
        if now >= expires:
            device = PairedDevice.load(fingerprint)
            mode = perms.mode_of(device, "screen") if device else perms.DENY
            self._video_policy[fingerprint] = (now + VIDEO_POLICY_SECONDS, mode)
        allowed = mode == perms.ALLOW or (mode == perms.ASK and "screen" in owner.granted)
        return sink if allowed else None

    def _udp_key_for(self, fingerprint: str) -> bytes | None:
        conn = self._connections.get(fingerprint)
        return conn.session.udp_key if conn is not None and conn.session is not None else None

    async def rotate_pc_identity(self) -> int:
        """Gives this PC a new identity key; connected phones are told right away (signed by the old key), others on
        their next connection. Returns how many phones were notified now."""
        old, new = pairing_mod.rotate_pc_identity()
        self.audit(sec_ledger.IDENTITY_ROTATED, None, who="pc", new_fp=secure_session_fp(new.public_b64))
        n = 0
        for conn in list(self._connections.values()):
            try:
                await conn.send_pc_rotation(old, new.public_b64)
                n += 1
            except ConnectionError:
                pass
        pairing_mod.finish_rotation_if_done()
        return n

    async def _on_phone_rotate(self, conn: "DeviceConnection", env: Envelope) -> None:
        """The phone replaces its identity key; the old key signed for the new one."""
        from . import secure_session

        device = PairedDevice.load(conn.fingerprint)
        new, sig = str(env.payload.get("new_pub", "")), str(env.payload.get("sig", ""))
        if device is None or not device.identity_pub or not secure_session.verify_rotation(device.identity_pub, conn.fingerprint, new, sig):
            self.audit(sec_ledger.SECURITY_ERROR, conn.fingerprint, error="bad_identity_rotation")
            return
        PairedDevice.update(conn.fingerprint, identity_pub_prev=device.identity_pub, identity_pub=new, identity_prev_at=time.time())
        self.audit(sec_ledger.IDENTITY_ROTATED, conn.fingerprint, who="phone", new_fp=secure_session.fingerprint_of(new))
        await conn.send("control", "identity.rotated", {"new_pub": new})

    async def _on_pc_rotation_ack(self, conn: "DeviceConnection", env: Envelope) -> None:
        device = PairedDevice.load(conn.fingerprint)
        new = str(env.payload.get("new_pub", ""))
        if device is None or new != pc_identity().public_b64:
            return
        PairedDevice.update(conn.fingerprint, pc_pub_acked=new)
        pairing_mod.finish_rotation_if_done()

    def _fail(self, ip: str) -> bool:
        """Records a failed authentication from `ip`; True if that locks the address out. The USB tunnel arrives from
        127.0.0.1, which any local user can reach: it is not throttled (a lockout there would only let a local user cut off
        the USB phone, and guessing the pairing key is not feasible anyway)."""
        if ip in THROTTLE_EXEMPT:
            return False
        return self.throttle.failure(ip)

    def on(self, type_: str, handler: Handler) -> None:
        self._handlers.setdefault(type_, []).append(handler)

    def on_connection_change(self, listener: ConnectionListener) -> None:
        """listener(conn, connected) runs on the net thread when a phone (dis)connects."""
        self._connection_listeners.append(listener)

    def _notify_connection(self, conn: "DeviceConnection", connected: bool) -> None:
        for listener in self._connection_listeners:
            try:
                listener(conn, connected)
            except Exception:  # noqa: BLE001 - a UI callback must never kill the connection
                log.exception("connection listener failed")

    async def start(self) -> None:
        cert_path, fp = tls_identity.ensure_server_identity()
        self.cert_fingerprint = fp
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # TLS 1.3 only. Android 8-9 can't speak it; PHONEBRIDGE_ALLOW_TLS12=1 re-enables 1.2 for such a phone
        # (the AES-GCM layer inside still protects the data, but the outer pinning is then 1.2-grade).
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2 if os.environ.get("PHONEBRIDGE_ALLOW_TLS12") == "1" else ssl.TLSVersion.TLSv1_3
        tls_identity.load_cert_chain(ctx)
        self.ssl_context = ctx
        self._server = await asyncio.start_server(self._handle_client, "0.0.0.0", self.port, ssl=ctx)  # nosec B104 - LAN service; peer_policy drops non-LAN sources
        log.info("control channel listening on :%d (cert fingerprint %s)", self.port, fp)
        try:
            loop = asyncio.get_running_loop()
            self._discovery, _ = await loop.create_datagram_endpoint(
                lambda: _DiscoveryResponder(self.port), local_addr=("0.0.0.0", DISCOVERY_PORT), allow_broadcast=True  # nosec B104
            )
        except OSError as exc:  # discovery is a convenience; the direct address still works
            log.warning("LAN discovery responder unavailable: %s", exc)
        # UDP video (PROTOCOL.md 1f) shares the control port number; the phone falls back to
        # the TCP video connection if this isn't reachable.
        try:
            from . import udp_video

            actual_port = self._server.sockets[0].getsockname()[1]
            self._udp_video = udp_video.UdpVideoEndpoint(
                self._video_sink, on_stream=lambda fp: self.video_transport.__setitem__(fp, "udp"),
                key_for=self._udp_key_for)
            await loop.create_datagram_endpoint(lambda: self._udp_video, local_addr=("0.0.0.0", actual_port))  # nosec B104 - filtered by session key + peer policy
        except OSError as exc:
            log.warning("UDP video unavailable (TCP video still works): %s", exc)
        from . import mdns, relay_client

        await loop.run_in_executor(None, mdns.advertise, self.port, fp)
        cfg = relay_client.load_config()
        if cfg is not None:
            self.relay = relay_client.RelayClient(self, *cfg)
            self.relay.start()
            log.info("relay enabled: %s:%d", *cfg)

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("serve_forever() before start()")
        async with self._server:
            await self._server.serve_forever()

    async def send(self, fingerprint: str, ch: str, type_: str, payload: dict[str, Any]) -> bool:
        conn = self._connections.get(fingerprint)
        if conn is None:
            return False
        await conn.send(ch, type_, payload)
        return True

    async def send_all(self, ch: str, type_: str, payload: dict[str, Any]) -> None:
        for conn in list(self._connections.values()):
            await conn.send(ch, type_, payload)

    async def configure_relay(self, host: str | None, port: int) -> None:
        """Turn the relay on (or off with host=None) while running, and tell connected phones."""
        from . import relay_client

        if self.relay is not None:
            self.relay.stop()
            self.relay = None
        if host:
            self.relay = relay_client.RelayClient(self, host, port)
            self.relay.start()
        await self.send_all("control", "relay.config", {"host": host or "", "port": port})

    def is_connected(self, fingerprint: str) -> bool:
        return fingerprint in self._connections

    def get_connection(self, fingerprint: str) -> DeviceConnection | None:
        return self._connections.get(fingerprint)

    def connected_fingerprints(self) -> list[str]:
        return list(self._connections.keys())

    def set_video_sink(self, fingerprint: str, sink: VideoSink | None) -> None:
        """Called by the mirror/webcam UI when it opens or closes — see PROTOCOL.md section 1b.
        `None` clears it (e.g. when the mirror window is closed)."""
        if sink is None:
            self._video_sinks.pop(fingerprint, None)
        else:
            self._video_sinks[fingerprint] = sink

    _offnet_logged: dict[str, float] = {}

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if peer and not peer_policy.is_allowed(peer[0]):
            # Not a LAN/USB address: drop it before reading a byte. Logged at most once per address per 10 minutes so
            # internet scanners can't fill the ledger.
            now = time.monotonic()
            if now - self._offnet_logged.get(peer[0], -1e9) > 600:
                self._offnet_logged[peer[0]] = now
                self.audit(sec_ledger.AUTH_FAIL, None, reason="off_network", ip=peer[0])
            writer.close()
            return
        await self.handle_stream(reader, writer, relayed=False)

    async def handle_stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, relayed: bool) -> None:
        """One accepted connection, direct (relayed=False) or arriving through the relay (relayed=True)."""
        try:
            # The first line names the connection ("<fp>", "<fp>:file:…", "<fp>:video"): short, ASCII, and it must arrive
            # promptly — a client that stalls, sends an endless line or sends junk is simply dropped.
            line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=FIRST_FRAME_TIMEOUT)
            if len(line) > MAX_IDENT_LINE:
                raise ValueError("ident line too long")
            identity = line.decode("ascii").strip()
            if not pairing_mod.valid_fingerprint(identity.split(":", 1)[0]):
                raise ValueError("not a device fingerprint")   # e.g. "../x": never reaches any file lookup or the ledger
        except Exception:  # noqa: BLE001 - timeout, overrun, not ASCII, not a fingerprint, reset: all the same answer
            writer.close()
            return
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        if (ip not in THROTTLE_EXEMPT and self.throttle.blocked(ip)) or self.locked_down:
            writer.close()   # throttled address, or "disconnect all" lockdown: don't even look at the request
            return

        if ":file:" in identity:
            # "<fp>:file:<transfer_id>:<offset>" — one data connection per file (file_transfer.py)
            from . import file_transfer

            fp, _, rest = identity.partition(":file:")
            tid, _, off = rest.partition(":")
            device = PairedDevice.load(fp)
            if device is None or not off.isdigit():
                writer.close()
                return
            owner = self._connections.get(fp)
            if owner is None or owner.session is None:   # data connections only exist alongside a live, authenticated session
                writer.close()
                return
            await file_transfer.handle_data_connection(device, tid, int(off), reader, writer,
                                                       owner.session.data_cipher("file|" + tid))
            return

        if identity.endswith(":video"):
            await self._handle_video_connection(identity[: -len(":video")], reader, writer)
            return

        fingerprint = identity
        device = PairedDevice.load(fingerprint)
        if device is None:
            log.warning("rejecting connection from unknown fingerprint %s", fingerprint)
            self.audit(sec_ledger.AUTH_FAIL, fingerprint, reason="unknown_device", ip=ip)
            if self._fail(ip):
                self.audit(sec_ledger.AUTH_THROTTLED, None, ip=ip)
            # Plain marker (we have no key to encrypt with): tells the phone this PC no
            # longer has its pairing, so it can ask the user to pair again instead of
            # retrying forever.
            writer.write(UNPAIRED_MARKER)
            try:
                await writer.drain()
            except ConnectionError:
                pass
            writer.close()
            return

        if await self._refuse_if_busy(device, writer):
            return

        conn = DeviceConnection(fingerprint, device, reader, writer)
        # How this phone reached us, shown on the Device page: the USB tunnel arrives from loopback, a relayed connection
        # comes through the relay server (so the phone may be on another network, even mobile data), the rest is the LAN.
        conn.link = "relay" if relayed else ("usb" if ip in THROTTLE_EXEMPT else "lan")
        conn.server = self
        # The fingerprint line is public (it is broadcast-discoverable), so don't replace an
        # existing connection, remember the address or tell the UI anything until the peer
        # has completed the v4 handshake: it must know the pairing key AND sign with the phone's Ed25519 key.
        try:
            await asyncio.wait_for(conn.handshake(pc_identity(), self.cert_fingerprint), timeout=FIRST_FRAME_TIMEOUT)
            # Key confirmation: a recorded hello can be replayed to get a reply, but only the real phone holds the
            # ephemeral key behind it. So the session counts (and replaces an existing one) only after its first frame
            # decrypts under the new keys.
            first = await asyncio.wait_for(conn._recv(), timeout=FIRST_FRAME_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - bad key, junk, timeout, oversize frame, bad signature, old version
            log.warning("rejecting %s: handshake failed (%s)", fingerprint, exc)
            self.audit(sec_ledger.AUTH_FAIL, fingerprint, reason="handshake_failed", ip=ip)
            if self._fail(ip):
                self.audit(sec_ledger.AUTH_THROTTLED, None, ip=ip)
            writer.close()
            return
        # The handshake can take seconds: a "disconnect all" or a revoke that happened meanwhile must win.
        device = PairedDevice.load(fingerprint)
        if self.locked_down or device is None:
            self.audit(sec_ledger.DENIED, fingerprint, feature="session", message="lockdown_or_revoked_during_handshake")
            writer.close()
            return
        # Another phone may have completed its handshake while this one was in progress.
        if await self._refuse_if_busy(device, writer):
            return
        old = self._connections.get(fingerprint)
        if old is not None:  # phone reconnected before we noticed the old socket died
            old.close()
        self._connections[fingerprint] = conn
        log.info("device connected: %s (%s)%s", device.name, fingerprint, " via relay" if relayed else "")
        self.throttle.success(ip)
        self.audit(sec_ledger.AUTH_OK, fingerprint, ip=None if relayed else ip, relayed=relayed)
        self.audit(sec_ledger.SESSION_START, fingerprint, session=conn.session_id)
        # The session is registered now: whatever fails from here on must reach the cleanup below, or the dead entry
        # would keep every other phone out as "busy".
        try:
            if not relayed:   # through a relay the peer address is the relay's, not the phone's
                self._remember_address(device, writer)
            await conn.after_handshake()
            self._notify_connection(conn, True)
            if self.relay is not None:   # tell the phone where to reach us when it is away from this network
                await conn.send("control", "relay.config", {"host": self.relay.host, "port": self.relay.port})
            envelope = first
            while True:
                if envelope is None:
                    break
                if envelope.type == "ping":
                    await conn.send("control", "pong", {})
                elif envelope.type == "session.end":
                    conn.ended_by = "peer"
                    break
                elif envelope.type == "rekey.ack":
                    await conn.on_rekey_ack(envelope.payload)
                elif await self._authorize(conn, envelope):
                    for handler in self._handlers.get(envelope.type, []):
                        await handler(conn, envelope)
                await conn.maybe_rekey()
                envelope = await asyncio.wait_for(conn._recv(), timeout=IDLE_TIMEOUT)
        except (asyncio.IncompleteReadError, ConnectionError, Exception) as exc:  # noqa: BLE001
            log.info("device disconnected: %s (%s)", device.name, exc)
        finally:
            # Only drop the entry if it's still ours — a reconnect may have replaced it.
            if self._connections.get(fingerprint) is conn:
                del self._connections[fingerprint]
                self._notify_connection(conn, False)
            # Always record the end, including sessions revoke()/disconnect_all() already removed from the table.
            self.audit(sec_ledger.SESSION_END, fingerprint, session=conn.session_id,
                       how=conn.ended_by or "abrupt", clean=conn.ended_by is not None)
            conn.close()
            conn.session = None

    async def _refuse_if_busy(self, device: PairedDevice, writer: asyncio.StreamWriter) -> bool:
        """One phone at a time: while a different one is connected, answers with the busy marker and closes."""
        busy_with = next((c for fp, c in self._connections.items() if fp != device.fingerprint), None)
        if busy_with is None:
            return False
        log.info("refusing %s: already connected to %s", device.name, busy_with.device.name)
        writer.write(BUSY_MARKER + busy_with.device.name.encode("utf-8", "replace")[:100] + b"\n")
        try:
            await writer.drain()
        except ConnectionError:
            pass
        writer.close()
        return True

    @staticmethod
    def _remember_address(device: PairedDevice, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if peer and device.last_lan_ip != peer[0]:
            PairedDevice.update(device.fingerprint, last_lan_ip=peer[0])

    async def _handle_video_connection(self, fingerprint: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """See PROTOCOL.md section 1b — a second connection, raw frames only, no JSON
        envelopes ever flow here. Routed to whatever's currently registered as the sink
        (the mirror window or the webcam viewer) for this device, if anything is."""
        device = PairedDevice.load(fingerprint)
        if device is None:
            log.warning("rejecting video connection from unknown fingerprint %s", fingerprint)
            writer.close()
            return
        owner = self._connections.get(fingerprint)
        mode = perms.mode_of(device, "screen")
        # "ask" must be enforced here too: the feature has to have been approved for this very session.
        if self.locked_down or mode == perms.DENY or (mode == perms.ASK and (owner is None or "screen" not in owner.granted)):
            self.audit(sec_ledger.DENIED, fingerprint, feature="screen", message="video connection")
            writer.close()
            return
        if owner is None or owner.session is None:
            writer.close()
            return
        cipher = owner.session.data_cipher("video")
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        log.info("video connection opened: %s (%s)", device.name, fingerprint)
        first = True
        try:
            while True:
                header = await reader.readexactly(crypto.FRAME_LEN_BYTES)
                n = crypto.unpack_frame_len(header)
                blob = await reader.readexactly(n)
                try:
                    frame = cipher.decrypt(blob)
                except Exception:  # noqa: BLE001 - not under this session's video key: count it against the address
                    if self._fail(ip):
                        self.audit(sec_ledger.AUTH_THROTTLED, None, ip=ip)
                    raise
                if first:   # state changes only once the peer has proven it holds the session key
                    first = False
                    self.video_transport[fingerprint] = "tcp"
                sink = self._video_sink(fingerprint)
                if sink is not None:
                    sink(frame)
        except (asyncio.IncompleteReadError, ConnectionError, Exception) as exc:  # noqa: BLE001
            log.info("video connection closed: %s (%s)", device.name, exc)
        finally:
            writer.close()
