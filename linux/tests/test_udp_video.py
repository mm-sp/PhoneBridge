#!/usr/bin/env python3
"""UDP video transport (protocol/PROTOCOL.md section 1f): sealing, fragmentation, loss handling
and the real UDP endpoint wired into ControlChannelServer. Run: `python3 tests/test_udp_video.py`"""
import asyncio
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from phonebridge import pairing, transport, udp_video as uv  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


def make_reassembler():
    out, keyreqs = [], []
    r = uv.Reassembler(out.append, lambda: keyreqs.append(1))
    return r, out, keyreqs


def feed_frame(r, fid, data, key, drop=()):
    for i, body in enumerate(uv.fragment(fid, data, key)):
        if i not in drop:
            r.feed(body)


async def main_async() -> int:
    key, fp = os.urandom(32), "deadbeefcafef00d"

    print("1. sealing")
    phone = uv.UdpChannel(key, fp, uv.DIR_PHONE)
    pc = uv.UdpChannel(key, fp, uv.DIR_PC)
    dg = phone.seal(uv.T_VIDEO, b"hello")
    got = pc.open(dg)
    check("round trip", got is not None and got[2] == uv.T_VIDEO and got[3] == b"hello" and got[0] == uv.DIR_PHONE)
    check("tampered datagram rejected", pc.open(dg[:-1] + bytes([dg[-1] ^ 1])) is None)
    check("wrong key rejected", uv.UdpChannel(os.urandom(32), fp, uv.DIR_PC).open(dg) is None)
    check("nonces differ per packet", phone.seal(uv.T_VIDEO, b"x")[8:20] != phone.seal(uv.T_VIDEO, b"x")[8:20])
    check("datagram fits MTU budget", len(phone.seal(uv.T_VIDEO, b"a" * (uv.MAX_FRAGMENT + uv.VIDEO_HEADER.size))) <= uv.MAX_DATAGRAM)

    print("2. fragmentation / reassembly")
    r, out, req = make_reassembler()
    big = os.urandom(10_000)
    bodies = uv.fragment(0, big, True)
    check("large frame is split", len(bodies) > 5)
    for b in reversed(bodies):  # out of order
        r.feed(b)
    check("out-of-order fragments reassemble", out == [big])
    for b in bodies:  # duplicates / replays
        r.feed(b)
    check("replayed fragments are ignored", out == [big])

    print("3. loss handling")
    r, out, req = make_reassembler()
    feed_frame(r, 0, b"K0" * 3000, True)
    feed_frame(r, 1, b"P1" * 100, False)
    check("clean stream delivers every frame", len(out) == 2 and not req)
    feed_frame(r, 2, b"P2" * 3000, False, drop=(1,))   # fragment lost
    feed_frame(r, 3, b"P3" * 100, False)
    check("frame after a loss is NOT delivered (reference missing)", len(out) == 2)
    check("loss triggers a keyframe request", len(req) == 1 and r.lost == 1)
    feed_frame(r, 4, b"K4" * 100, True)
    check("next keyframe resumes delivery", len(out) == 3 and out[-1] == b"K4" * 100)
    feed_frame(r, 5, b"P5", False)
    check("delta frames flow again after the keyframe", len(out) == 4)
    feed_frame(r, 3, b"late", False)
    check("late frame is dropped", len(out) == 4)

    r, out, req = make_reassembler()
    feed_frame(r, 0, b"P-before-key", False)
    check("deltas before the first keyframe are held back", out == [])

    print("4. real endpoint through ControlChannelServer")
    tmp = Path(tempfile.mkdtemp()) / "devices"
    pairing.DEVICES_DIR = tmp
    sys.path.insert(0, str(Path(__file__).resolve().parent)); from _isolate import isolate_tls; isolate_tls(pairing.DEVICES_DIR)
    from _phone import new_identity, phone_connect
    ident = new_identity()
    pairing.PairedDevice(fingerprint=fp, name="Pixel 9", session_key=key, identity_pub=ident.public_b64).save()
    server = transport.ControlChannelServer(port=0)
    await server.start()
    asyncio.ensure_future(server.serve_forever())
    port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
    # UDP video only works inside an authenticated control session: its key comes from that session.
    import ssl
    tctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tctx.check_hostname, tctx.verify_mode = False, ssl.CERT_NONE
    creader, cwriter = await asyncio.open_connection("127.0.0.1", port, ssl=tctx)
    session = await phone_connect(creader, cwriter, fp, key, ident, server.cert_fingerprint)
    stray = uv.UdpChannel(key, fp, uv.DIR_PHONE)   # the OLD long-term-key scheme must no longer be accepted
    key = session.udp_key
    phone = uv.UdpChannel(key, fp, uv.DIR_PHONE)
    frames = []
    server.set_video_sink(fp, frames.append)

    loop = asyncio.get_running_loop()
    inbox = []

    class Phone(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            inbox.append(data)

    tr, _ = await loop.create_datagram_endpoint(Phone, remote_addr=("127.0.0.1", port))
    tr.sendto(stray.seal(uv.T_PROBE, b"old-key"))
    await asyncio.sleep(0.2)
    check("datagrams sealed with the long-term pairing key are rejected (no session-derived key)", not inbox)
    tr.sendto(phone.seal(uv.T_PROBE, b"ping123"))
    await asyncio.sleep(0.2)
    opened = [pc_open for d in inbox if (pc_open := phone_rx(key, fp).open(d))]
    check("probe is answered with an authenticated ack", any(o[2] == uv.T_PROBE_ACK and o[3] == b"ping123" for o in opened))

    f0, f1 = os.urandom(5000), os.urandom(900)
    for fid, data, k in ((0, f0, True), (1, f1, False)):
        for body in uv.fragment(fid, data, k):
            tr.sendto(phone.seal(uv.T_VIDEO, body))
    await asyncio.sleep(0.3)
    check("frames arrive at the registered sink over real UDP", frames == [f0, f1])
    check("server records that this phone's video arrives over UDP", server.video_transport.get(fp) == "udp")

    tr.sendto(os.urandom(100))                                   # junk
    tr.sendto(uv.UdpChannel(os.urandom(32), fp, uv.DIR_PHONE).seal(uv.T_VIDEO, b"x" * 20))  # wrong key
    await asyncio.sleep(0.2)
    check("junk and wrong-key datagrams are ignored", frames == [f0, f1])

    inbox.clear()
    for body in uv.fragment(2, os.urandom(4000), False)[:2]:      # frame 2 incomplete
        tr.sendto(phone.seal(uv.T_VIDEO, body))
    for body in uv.fragment(3, b"p", False):
        tr.sendto(phone.seal(uv.T_VIDEO, body))
    await asyncio.sleep(1.4)                                      # also covers one report interval
    opened = [o for d in inbox if (o := phone_rx(key, fp).open(d))]
    types = [o[2] for o in opened]
    check("PC asks for a keyframe after a lost frame", uv.T_KEYREQ in types)
    reports = [struct.unpack(">II", o[3]) for o in opened if o[2] == uv.T_REPORT]
    check("PC reports delivered/lost counts", any(lost >= 1 for _d, lost in reports))

    print("5. replay protection (audit finding)")
    ep = server._udp_video  # noqa: SLF001
    stream = ep._streams[bytes.fromhex(fp)]  # noqa: SLF001
    frames.clear()
    d_old = phone.seal(uv.T_VIDEO, uv.fragment(40, b"OLD-SCREEN-FRAME", True)[0])
    tr.sendto(d_old); await asyncio.sleep(0.2)
    n_first = len(frames)
    tr.sendto(d_old); await asyncio.sleep(0.2)
    check("a replayed datagram is ignored (frame delivered once)", n_first == 1 and len(frames) == 1)
    phone2 = uv.UdpChannel(key, fp, uv.DIR_PHONE)             # a NEW stream: fresh random epoch, counters restart
    tr.sendto(phone2.seal(uv.T_VIDEO, uv.fragment(0, b"NEW-STREAM", True)[0])); await asyncio.sleep(0.2)
    check("a genuinely new stream (new epoch) is accepted", frames[-1] == b"NEW-STREAM")
    n_now = len(frames)
    tr.sendto(d_old); await asyncio.sleep(0.2)
    check("replaying a datagram from the RETIRED stream can't reset the stream or inject its old frame", len(frames) == n_now)
    addr_before = stream.addr
    spoof_tr, _ = await loop.create_datagram_endpoint(Phone, remote_addr=("127.0.0.1", port))
    spoof_tr.sendto(d_old); await asyncio.sleep(0.2)
    check("…nor can it move the address the PC sends replies to", stream.addr == addr_before)
    st = uv._Stream(pairing.PairedDevice.load(fp), key)  # noqa: SLF001
    check("window: a fresh stream accepts its first counters", st.accept_counter(b"e1\0", 5) and st.accept_counter(b"e1\0", 6))
    check("window: out-of-order arrival inside the window is accepted", st.accept_counter(b"e1\0", 4))
    check("window: a duplicate counter is refused", not st.accept_counter(b"e1\0", 6) and not st.accept_counter(b"e1\0", 4))
    check("window: a jump ahead is accepted and old counters beyond the window are refused",
          st.accept_counter(b"e1\0", 6 + uv.REPLAY_WINDOW + 50) and not st.accept_counter(b"e1\0", 5))
    check("window: a new epoch is accepted once, and the retired epoch can never return",
          st.accept_counter(b"e2\0", 1) and not st.accept_counter(b"e1\0", 10 ** 6) and st.accept_counter(b"e2\0", 2))
    spoof_tr.close()

    tr.close()
    server._udp_video.close()  # noqa: SLF001
    shutil.rmtree(tmp.parent, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def phone_rx(key, fp):
    return uv.UdpChannel(key, fp, uv.DIR_PHONE)  # opens PC->phone datagrams (dir is in the header)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
