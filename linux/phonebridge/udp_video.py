"""UDP video transport (protocol/PROTOCOL.md section 1f).

Low-latency alternative to the TLS video connection of section 1b: one encoded H.264 access
unit is split into datagrams of <= MAX_DATAGRAM bytes, each sealed with AES-256-GCM under the
pairing session key. A lost packet costs one frame (and a keyframe request) instead of
stalling everything behind it the way TCP does. The phone falls back to the TCP connection if
the UDP probe isn't answered, so a firewall that drops UDP only costs latency.

Datagram: fp(8) | dir(1) | epoch(3) | counter(8) | AES-GCM(type(1) | body) + tag(16)
  - fp      pairing fingerprint as raw bytes (selects the key; not a secret)
  - nonce   = dir|epoch|counter (12 bytes). dir 0 = phone->PC, 1 = PC->phone, so the two
            directions never reuse a nonce; epoch is random per stream; counter increments.
  - AAD     = fp
Mirrors android/.../UdpVideo.kt — change both together.
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import time
from collections.abc import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .pairing import PairedDevice

log = logging.getLogger("phonebridge.udp_video")

MAX_DATAGRAM = 1200
HEADER_LEN = 20
TAG_LEN = 16
VIDEO_HEADER = struct.Struct(">IHHB")  # frame_id, frag_idx, frag_count, flags
MAX_FRAGMENT = MAX_DATAGRAM - HEADER_LEN - TAG_LEN - 1 - VIDEO_HEADER.size
FLAG_KEYFRAME = 1

T_VIDEO, T_PROBE, T_PROBE_ACK, T_KEYREQ, T_REPORT = range(5)
DIR_PHONE, DIR_PC = 0, 1

MAX_PENDING_FRAMES = 8
KEYREQ_MIN_INTERVAL = 0.5
REPORT_INTERVAL = 1.0
STREAM_IDLE = 3.0


class UdpChannel:
    """Seals/opens datagrams for one device and one direction of sending."""

    def __init__(self, session_key: bytes, fingerprint: str, send_dir: int) -> None:
        self._aead = AESGCM(session_key)
        self.fp = bytes.fromhex(fingerprint)
        self._dir = send_dir
        self._epoch = os.urandom(3)
        self._counter = 0

    def seal(self, ptype: int, body: bytes = b"") -> bytes:
        self._counter += 1
        meta = bytes([self._dir]) + self._epoch + struct.pack(">Q", self._counter)
        return self.fp + meta + self._aead.encrypt(meta, bytes([ptype]) + body, self.fp)

    def open(self, datagram: bytes) -> tuple[int, bytes, int, bytes, int] | None:
        """-> (dir, epoch, ptype, body, counter) or None if it doesn't authenticate."""
        if len(datagram) < HEADER_LEN + TAG_LEN + 1 or datagram[:8] != self.fp:
            return None
        meta = datagram[8:HEADER_LEN]
        try:
            plain = self._aead.decrypt(meta, datagram[HEADER_LEN:], self.fp)
        except InvalidTag:
            return None
        return meta[0], meta[1:4], plain[0], plain[1:], struct.unpack(">Q", meta[4:12])[0]


def fragment(frame_id: int, data: bytes, keyframe: bool) -> list[bytes]:
    """Bodies for T_VIDEO datagrams carrying one access unit."""
    count = max(1, -(-len(data) // MAX_FRAGMENT))
    flags = FLAG_KEYFRAME if keyframe else 0
    return [
        VIDEO_HEADER.pack(frame_id, i, count, flags) + data[i * MAX_FRAGMENT:(i + 1) * MAX_FRAGMENT]
        for i in range(count)
    ]


class Reassembler:
    """Collects fragments into frames, delivers them in order, and never feeds the decoder a
    delta frame whose reference was lost (it holds the last picture until a keyframe)."""

    def __init__(self, deliver: Callable[[bytes], None], request_keyframe: Callable[[], None]) -> None:
        self._deliver = deliver
        self._request_keyframe = request_keyframe
        self._pending: dict[int, dict] = {}
        self._last = -1
        self._need_key = True
        self._last_req = 0.0
        self.delivered = 0
        self.lost = 0

    def feed(self, body: bytes) -> None:
        if len(body) < VIDEO_HEADER.size:
            return
        frame_id, idx, count, flags = VIDEO_HEADER.unpack_from(body)
        if frame_id <= self._last or count == 0 or idx >= count:
            return
        frame = self._pending.setdefault(frame_id, {"count": count, "parts": {}, "flags": flags})
        if frame["count"] != count:
            return
        frame["parts"][idx] = body[VIDEO_HEADER.size:]
        if len(frame["parts"]) == count:
            self._complete(frame_id)
        elif len(self._pending) > MAX_PENDING_FRAMES:
            oldest = min(self._pending)
            del self._pending[oldest]
            self._mark_lost(1)

    def _complete(self, frame_id: int) -> None:
        frame = self._pending.pop(frame_id)
        # Anything older that never completed is gone for good.
        stale = [f for f in self._pending if f < frame_id]
        for f in stale:
            del self._pending[f]
        # Frame ids are consecutive, so every id between the last delivered one and this one
        # was either an incomplete frame or never arrived at all.
        skipped = frame_id - self._last - 1 if self._last >= 0 else 0
        if skipped > 0:
            self._mark_lost(skipped)
        self._last = frame_id
        key = bool(frame["flags"] & FLAG_KEYFRAME)
        if key:
            self._need_key = False
        elif self._need_key:
            self._request()
            return
        self.delivered += 1
        self._deliver(b"".join(frame["parts"][i] for i in range(frame["count"])))

    def _mark_lost(self, n: int) -> None:
        self.lost += n
        self._need_key = True
        self._request()

    def _request(self) -> None:
        now = time.monotonic()
        if now - self._last_req >= KEYREQ_MIN_INTERVAL:
            self._last_req = now
            self._request_keyframe()


REPLAY_WINDOW = 1024     # datagram counters remembered per stream epoch
MAX_OLD_EPOCHS = 256     # retired stream epochs remembered per session


class _Stream:
    def accept_counter(self, epoch: bytes, counter: int) -> bool:
        """Anti-replay for authentic datagrams. Within a stream (epoch) every counter is accepted once and counters far
        behind the newest are refused. A phone starts a new stream with a fresh random epoch; an epoch we have already
        retired can never come back, so a captured old datagram can't reset the stream or redirect replies."""
        if epoch != self._cur_epoch:
            if epoch in self._old_epochs:
                return False
            if self._cur_epoch is not None:
                self._old_epochs.add(self._cur_epoch)
                if len(self._old_epochs) > MAX_OLD_EPOCHS:
                    self._old_epochs.pop()
            self._cur_epoch, self._max_counter, self._recent = epoch, 0, set()
        if counter in self._recent or counter <= self._max_counter - REPLAY_WINDOW:
            return False
        self._recent.add(counter)
        if counter > self._max_counter:
            self._max_counter = counter
            if len(self._recent) > 2 * REPLAY_WINDOW:
                self._recent = {c for c in self._recent if c > counter - REPLAY_WINDOW}
        return True

    def __init__(self, device: PairedDevice, key: bytes) -> None:
        self._cur_epoch: bytes | None = None
        self._old_epochs: set[bytes] = set()
        self._max_counter = 0
        self._recent: set[int] = set()
        self.device = device
        self.key = key
        self.rx = UdpChannel(key, device.fingerprint, DIR_PC)  # opens phone datagrams
        self.tx = UdpChannel(key, device.fingerprint, DIR_PC)
        self.epoch: bytes | None = None
        self.reassembler: Reassembler | None = None
        self.addr: tuple | None = None
        self.last_rx = 0.0
        self.reported = (0, 0)


class UdpVideoEndpoint(asyncio.DatagramProtocol):
    """Receives video datagrams from paired phones and hands complete frames to `sink_for(fp)`."""

    def __init__(self, sink_for: Callable[[str], Callable[[bytes], None] | None],
                 on_stream: Callable[[str], None] | None = None,
                 key_for: Callable[[str], bytes | None] | None = None) -> None:
        """`key_for(fingerprint)` returns the UDP key of that phone's live control session (secure_session.Session.udp_key)
        or None when it isn't connected: UDP video only works inside an authenticated session."""
        self._key_for = key_for
        self._sink_for = sink_for
        self._on_stream = on_stream
        self._streams: dict[bytes, _Stream] = {}
        self._unknown: set[bytes] = set()
        self.transport: asyncio.DatagramTransport | None = None
        self._report_handle: asyncio.TimerHandle | None = None

    # -- asyncio plumbing
    def connection_made(self, transport) -> None:
        self.transport = transport
        self._report_handle = asyncio.get_running_loop().call_later(REPORT_INTERVAL, self._send_reports)

    def close(self) -> None:
        if self._report_handle:
            self._report_handle.cancel()
        if self.transport:
            self.transport.close()

    def _stream_for(self, fp: bytes) -> _Stream | None:
        key = self._key_for(fp.hex()) if self._key_for else None
        if key is None:      # no live authenticated session for this phone (a new session gets a new key)
            self._streams.pop(fp, None)
            return None
        stream = self._streams.get(fp)
        if stream is not None and stream.key != key:   # the phone reconnected: new session, new keys
            stream = None
        if stream is None:
            device = PairedDevice.load(fp.hex())
            if device is None:
                return None
            stream = self._streams[fp] = _Stream(device, key)
        return stream

    def datagram_received(self, data: bytes, addr) -> None:
        from . import peer_policy

        if len(data) < HEADER_LEN + TAG_LEN + 1 or not peer_policy.is_allowed(addr[0]):
            return
        stream = self._stream_for(data[:8])
        if stream is None:
            return
        opened = stream.rx.open(data)
        if opened is None or opened[0] != DIR_PHONE:
            return
        _dir, epoch, ptype, body, counter = opened
        if not stream.accept_counter(epoch, counter):
            return   # replayed or from a retired stream: ignore it, and above all don't let it move the reply address
        stream.addr = addr
        stream.last_rx = time.monotonic()
        if ptype == T_PROBE:
            self._send(stream, T_PROBE_ACK, body)
            return
        if ptype != T_VIDEO:
            return
        if epoch != stream.epoch:  # a new stream from the phone: frame ids restart
            stream.epoch = epoch
            if self._on_stream:
                self._on_stream(stream.device.fingerprint)
            stream.reassembler = Reassembler(
                lambda frame, fp=stream.device.fingerprint: self._deliver(fp, frame),
                lambda s=stream: self._send(s, T_KEYREQ),
            )
            stream.reported = (0, 0)
        if stream.reassembler is None:
            return
        stream.reassembler.feed(body)

    def _deliver(self, fingerprint: str, frame: bytes) -> None:
        sink = self._sink_for(fingerprint)
        if sink is not None:
            sink(frame)

    def _send(self, stream: _Stream, ptype: int, body: bytes = b"") -> None:
        if self.transport is not None and stream.addr is not None:
            self.transport.sendto(stream.tx.seal(ptype, body), stream.addr)

    def _send_reports(self) -> None:
        now = time.monotonic()
        for stream in self._streams.values():
            r = stream.reassembler
            if r is None or now - stream.last_rx > STREAM_IDLE:
                continue
            delivered, lost = r.delivered - stream.reported[0], r.lost - stream.reported[1]
            stream.reported = (r.delivered, r.lost)
            self._send(stream, T_REPORT, struct.pack(">II", delivered, lost))
        self._report_handle = asyncio.get_running_loop().call_later(REPORT_INTERVAL, self._send_reports)
