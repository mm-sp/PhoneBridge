#!/usr/bin/env python3
"""FUSE phone mount: real FUSE mount in a temp dir, a real control server, and a fake phone that answers
files.list / files.get like the Android app (data connection + sha256). Skips if fusepy or /dev/fuse
are unavailable. Run with a Python that has fusepy: `python tests/test_phone_fs.py`"""
import asyncio
import faulthandler
import hashlib
import os
import ssl
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import fuse  # noqa: F401
except ImportError:
    print("SKIP: fusepy not installed")
    raise SystemExit(0)
if not os.path.exists("/dev/fuse"):
    print("SKIP: no /dev/fuse")
    raise SystemExit(0)

from phonebridge import crypto, file_transfer, pairing, phone_fs, transport  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402

faulthandler.dump_traceback_later(40, exit=True)  # a hang prints every thread's stack instead of blocking
PASS = FAIL = 0
TREE = {"/": [("DCIM", True, 0), ("notes.txt", False, 11)], "/DCIM": [("a.jpg", False, 300000)]}
FILES = {"/notes.txt": b"hello phone", "/DCIM/a.jpg": os.urandom(300000)}


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


async def fake_phone(port, fp, key, stop, ident, tls_fp):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _phone import phone_connect
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    session = await phone_connect(r, w, fp, key, ident, tls_fp)
    cipher = session.cipher
    seq = [0]

    async def send(t, p):
        seq[0] += 1
        w.write(crypto.pack_frame(cipher.encrypt(Envelope("control", t, seq[0], p).to_json())))
        await w.drain()

    async def stream(path, tid):
        data = FILES[path]
        dr, dw = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
        dw.write(f"{fp}:file:{tid}:0\n".encode())
        dcipher = session.data_cipher("file|" + tid)
        dw.write(crypto.pack_frame(dcipher.encrypt(b"PBFILE:" + tid.encode())))
        for i in range(0, len(data), 65536):
            dw.write(crypto.pack_frame(dcipher.encrypt(data[i:i + 65536])))
        dw.write(crypto.pack_frame(dcipher.encrypt(b"SHA256:" + hashlib.sha256(data).hexdigest().encode())))
        await dw.drain()
        n = crypto.unpack_frame_len(await dr.readexactly(4))
        verdict = dcipher.decrypt(await dr.readexactly(n))
        dw.close()
        return verdict

    awaiting: dict = {}
    tasks: list = []
    while not stop.is_set():
        try:
            n = crypto.unpack_frame_len(await asyncio.wait_for(r.readexactly(4), 0.3))
        except asyncio.TimeoutError:
            continue
        env = Envelope.from_json(cipher.decrypt(await r.readexactly(n)))
        if env.type == "files.list":
            path = env.payload["path"]
            entries = [{"name": n_, "is_dir": d, "size": s, "mtime": 1700000000000} for n_, d, s in TREE[path]]
            await send("files.list_result", {"path": path, "entries": entries})
        elif env.type == "files.get":
            path = env.payload["path"]
            tid = "t" + str(abs(hash(path)))[:10]
            await send("file.offer", {"transfer_id": tid, "filename": Path(path).name, "size": len(FILES[path]),
                                      "mime": "x/y", "resume_key": "rk" + tid, "data_conn": True, "source_path": path})
            awaiting[tid] = path
        elif env.type == "file.accept":
            tid = env.payload["transfer_id"]
            tasks.append(asyncio.ensure_future(stream(awaiting.pop(tid), tid)))  # keep a reference


async def main_async():
    tmp = Path(tempfile.mkdtemp())
    pairing.DEVICES_DIR = tmp / "devices"
    sys.path.insert(0, str(Path(__file__).resolve().parent)); from _isolate import isolate_tls; isolate_tls()
    phone_fs.CACHE_ROOT = tmp / "cache"
    key = os.urandom(32)
    from _phone import new_identity
    ident = new_identity()
    pairing.PairedDevice("cafe", "Pixel", key, identity_pub=ident.public_b64).save()
    transport.DISCOVERY_PORT = 28199
    from phonebridge import mdns
    mdns.advertise = lambda *a, **k: False
    server = transport.ControlChannelServer(port=0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
    asyncio.ensure_future(server.serve_forever())
    file_transfer.register(server, lambda _p: tmp / "downloads", None, None)
    stop = asyncio.Event()
    asyncio.ensure_future(fake_phone(port, "cafe", key, stop, ident, server.cert_fingerprint))
    await asyncio.sleep(0.4)

    loop = asyncio.get_running_loop()
    mount = phone_fs.Mount(server, loop, "cafe", "Pixel", tmp / "mnt")
    err = await loop.run_in_executor(None, mount.mount)
    check("mounted", err is None and os.path.ismount(tmp / "mnt"))
    run = lambda fn, *a: loop.run_in_executor(None, fn, *a)  # noqa: E731

    check("root lists DCIM and notes.txt", sorted(await run(os.listdir, tmp / "mnt")) == ["DCIM", "notes.txt"])
    st = await run(os.stat, tmp / "mnt" / "notes.txt")
    check("file size comes from the listing (no download yet)", st.st_size == 11)
    check("sub-folder listing", await run(os.listdir, tmp / "mnt" / "DCIM") == ["a.jpg"])
    data = await run(lambda: (tmp / "mnt" / "notes.txt").read_bytes())
    check("reading downloads the file and returns its bytes", data == FILES["/notes.txt"])
    big = await run(lambda: (tmp / "mnt" / "DCIM" / "a.jpg").read_bytes())
    check("multi-chunk file arrives intact (sha256-verified transfer)", big == FILES["/DCIM/a.jpg"])
    check("second read is served from the cache", await run(lambda: (tmp / "mnt" / "notes.txt").read_bytes()) == data)
    try:
        await run(lambda: (tmp / "mnt" / "new.txt").write_text("x"))
        ro = False
    except OSError:
        ro = True
    check("mount is read-only", ro)
    check("nothing leaked into Downloads", not (tmp / "downloads").exists() or not any((tmp / "downloads").iterdir()))

    await run(mount.unmount)
    check("unmounted", not os.path.ismount(tmp / "mnt"))
    stop.set()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
