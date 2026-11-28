#!/usr/bin/env python3
"""Optional relay (protocol/PROTOCOL.md section 1g): a fake phone reaches the PC only through a real relay
server — relay handshake, TLS upgrade, pairing-key auth, per-connection slot replenishment, and refusal of
junk. Run: `python3 tests/test_relay.py`"""
import asyncio
import os
import shutil
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import crypto, pairing, relay_client, relay_server, transport  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


async def phone_via_relay(port, room, tls=True):
    """-> (reader, writer, first_line) after the relay handshake and TLS upgrade."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    transport_, protocol = await loop.create_connection(lambda: asyncio.StreamReaderProtocol(reader), "127.0.0.1", port)
    # Write on the raw transport: a throwaway StreamWriter would close the connection when garbage-collected.
    transport_.write(f"PBRELAY phone {room}\n".encode())
    line = (await asyncio.wait_for(reader.readline(), 10)).strip()
    if line != b"OK" or not tls:
        return reader, asyncio.StreamWriter(transport_, protocol, reader, loop), line
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    tls_transport = await loop.start_tls(transport_, protocol, ctx, server_hostname="pc")
    return reader, asyncio.StreamWriter(tls_transport, protocol, reader, loop), line


async def read_envelope(reader, cipher):
    n = crypto.unpack_frame_len(await reader.readexactly(4))
    return Envelope.from_json(cipher.decrypt(await reader.readexactly(n)))


async def main_async() -> int:
    tmp = Path(tempfile.mkdtemp())
    pairing.DEVICES_DIR = tmp / "devices"
    sys.path.insert(0, str(Path(__file__).resolve().parent)); from _isolate import isolate_tls; isolate_tls()
    key = os.urandom(32)
    fp = "0123456789abcdef"
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _phone import new_identity, phone_connect
    ident = new_identity()
    pairing.PairedDevice(fingerprint=fp, name="Pixel", session_key=key, identity_pub=ident.public_b64).save()
    room = crypto.relay_room(key)

    relay = relay_server.RelayServer()
    relay_port = await relay.start("127.0.0.1", 0)
    os.environ["PHONEBRIDGE_RELAY"] = f"127.0.0.1:{relay_port}"
    server = transport.ControlChannelServer(port=0)
    await server.start()
    asyncio.ensure_future(server.serve_forever())
    seen = []

    async def on_info(conn, env):
        seen.append(env.payload)

    server.on("device.info", on_info)
    await asyncio.sleep(0.6)                                    # slots park at the relay
    print("1. room name")
    check("room is 32 hex chars and differs per key", len(room) == 32 and room != crypto.relay_room(os.urandom(32)))

    print("2. a phone with no PC waiting")
    _r, w, line = await phone_via_relay(relay_port, crypto.relay_room(os.urandom(32)))
    check("unknown room gets NOPC", line == b"NOPC")
    w.close()
    r2 = asyncio.StreamReader()
    t, p = await asyncio.get_running_loop().create_connection(lambda: asyncio.StreamReaderProtocol(r2), "127.0.0.1", relay_port)
    t.write(b"GET / HTTP/1.1\r\n\r\n")
    check("junk first line is dropped", await asyncio.wait_for(r2.read(10), 5) == b"")

    print("3. control connection through the relay")
    reader, writer, line = await phone_via_relay(relay_port, room)
    check("relay answers OK once the PC is ready", line == b"OK")
    session = await phone_connect(reader, writer, fp, key, ident, server.cert_fingerprint)
    cipher = session.cipher
    writer.write(crypto.pack_frame(cipher.encrypt(Envelope("control", "device.info", 1, {"battery_pct": 77}).to_json())))
    await writer.drain()
    await asyncio.sleep(0.4)
    check("PC authenticates the phone over the relayed TLS connection", server.is_connected(fp))
    check("a message sent through the relay is dispatched", seen == [{"battery_pct": 77}])
    check("the relay's address is not remembered as the phone's LAN address",
          pairing.PairedDevice.load(fp).last_lan_ip is None)
    env = await asyncio.wait_for(read_envelope(reader, cipher), 5)
    check("PC tells the phone which relay to use", env.type == "relay.config" and env.payload["port"] == relay_port)

    print("4. more connections while the first is open (file transfer / video do this)")
    frames = []
    server.set_video_sink(fp, frames.append)
    vreader, vwriter, line2 = await phone_via_relay(relay_port, room)
    check("a second relay connection is served (replacement slot was parked)", line2 == b"OK")
    vwriter.write((fp + ":video\n").encode())
    vwriter.write(crypto.pack_frame(session.data_cipher("video").encrypt(b"frame-through-relay")))
    await vwriter.drain()
    await asyncio.sleep(0.4)
    check("video frames arrive over a relayed connection", frames == [b"frame-through-relay"])
    check("control connection is still up", server.is_connected(fp))

    print("5. closing")
    writer.close()
    vwriter.close()
    await asyncio.sleep(0.4)
    check("PC notices the phone leaving", not server.is_connected(fp))
    _r3, w3, line3 = await phone_via_relay(relay_port, room, tls=False)
    check("PC re-parks slots, so the phone can come back", line3 == b"OK")
    w3.close()

    print("6. turning the relay off while running")
    await server.configure_relay(None, 0)
    await asyncio.sleep(0.5)
    _r4, w4, line4 = await phone_via_relay(relay_port, room, tls=False)
    check("parked slots are withdrawn, the relay answers NOPC", line4 == b"NOPC")
    w4.close()
    check("server has no relay client any more", server.relay is None)

    print("7. a connection that fails after a phone took the slot is not parked twice")
    open_now: set = set()
    accepted = 0

    async def broken_relay(r, w):
        nonlocal accepted
        accepted += 1
        first = accepted <= 4          # the first few "phones" send junk instead of a TLS hello
        open_now.add(w)
        try:
            await r.readline()
            if first:
                w.write(b"GO\n")
                await r.readline()     # READY
                w.write(b"this is not TLS\r\n" * 4)
                await w.drain()
                w.close()
            await r.read()
        except ConnectionError:
            pass
        finally:
            open_now.discard(w)
            w.close()

    fake = await asyncio.start_server(broken_relay, "127.0.0.1", 0)
    relay_client.RETRY_SECONDS = 0.05
    client = relay_client.RelayClient(server, "127.0.0.1", fake.sockets[0].getsockname()[1])
    client.start()
    await asyncio.sleep(2.0)
    check("the failures happened", accepted >= 5)
    check("exactly the configured number of connections stays parked",
          len(open_now) == relay_client.SLOTS_PER_DEVICE and client._idle[room] == relay_client.SLOTS_PER_DEVICE)  # noqa: SLF001
    client.stop()
    await asyncio.sleep(0.3)
    fake.close()

    print("8. the relay forgets a parked connection the PC closed")
    relay_server.REAP_SECONDS = 0.1
    other = relay_server.RelayServer()
    other_port = await other.start("127.0.0.1", 0)
    gone_room, kept_room = "a" * 32, "b" * 32
    parked = []
    for name in (gone_room, kept_room):
        r, w = await asyncio.open_connection("127.0.0.1", other_port)
        w.write(f"PBRELAY pc {name}\n".encode())
        await w.drain()
        parked.append((r, w))
    await asyncio.sleep(0.4)
    check("both rooms are parked", set(other._rooms) == {gone_room, kept_room})  # noqa: SLF001
    parked[0][1].close()
    await asyncio.sleep(0.6)
    check("the closed one is removed without any phone asking", set(other._rooms) == {kept_room})  # noqa: SLF001
    _r5, w5, line5 = await phone_via_relay(other_port, gone_room, tls=False)
    check("…and a phone asking for it is told there is no PC", line5 == b"NOPC")
    w5.close()
    phone5 = asyncio.ensure_future(phone_via_relay(other_port, kept_room, tls=False))
    go = await asyncio.wait_for(parked[1][0].readline(), 5)
    parked[1][1].write(b"READY\n")
    _r6, w6, line6 = await phone5
    await asyncio.sleep(0.4)                       # several reap periods while the connection is in use
    w6.write(b"still-here")
    check("a connection in use is left alone", go == b"GO\n" and line6 == b"OK"
          and await asyncio.wait_for(parked[1][0].readexactly(10), 5) == b"still-here")
    w6.close()
    parked[1][1].close()
    other.server.close()

    print("9. relay addresses that are not usable")

    def parsed(text):
        try:
            return relay_client.parse_address(text)
        except ValueError:
            return None

    check("host and host:port are understood", parsed(" relay.example.com ") == ("relay.example.com", 28200)
          and parsed("relay.example.com:4000") == ("relay.example.com", 4000) and parsed("10.0.0.2:") == ("10.0.0.2", 28200))
    bad = (":28200", "host:0", "host:65536", "host:99999999", "host:-1", "host:abc", "host:²", "https://relay.example.com",
           "two words", "")
    check("anything else is refused", [t for t in bad if parsed(t) is not None] == [])
    for text in ("host:abc", "host:70000", ":1"):
        os.environ["PHONEBRIDGE_RELAY"] = text
        check(f"PHONEBRIDGE_RELAY={text!r} leaves the relay off instead of failing", relay_client.load_config() is None)
    del os.environ["PHONEBRIDGE_RELAY"]
    relay_client.CONFIG_PATH = tmp / "relay.json"
    for stored in ('[1]', '"x"', '{"host": "h", "port": "abc"}', '{"host": "h", "port": 70000}', "{nonsense"):
        relay_client.CONFIG_PATH.write_text(stored)
        check(f"stored setting {stored} leaves the relay off", relay_client.load_config() is None)
    relay_client.save_config("relay.example.com", 4000)
    check("a saved setting is read back", relay_client.load_config() == ("relay.example.com", 4000))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
