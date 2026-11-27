"""Read-only file-manager view of the phone's storage (the equivalent of Phone Link's Explorer
integration): a FUSE mount at ~/PhoneBridge/<phone>/ that any file manager or app can browse.

Folder listings come from the phone on demand (`files.list`, cached for a short while); a file is
fetched through the normal file-transfer machinery (`files.get`, sha256-verified, data connection)
the first time something opens it and then served from a cache under ~/.cache/phonebridge/mount/.
Needs the optional `fusepy` package and libfuse (`fusermount3`).
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("phonebridge.phone_fs")

CACHE_ROOT = Path.home() / ".cache" / "phonebridge" / "mount"
LIST_TTL = 20.0
LIST_TIMEOUT = 20.0
FETCH_TIMEOUT = 900.0


class ServerBackend:
    """Asks a connected phone for listings/files through the control server."""

    def __init__(self, server, loop: asyncio.AbstractEventLoop, fingerprint: str) -> None:
        self._server, self._loop, self._fp = server, loop, fingerprint
        self._waiting: dict[str, asyncio.Future] = {}
        server.on("files.list_result", self._on_list_result)

    async def _on_list_result(self, conn, envelope) -> None:
        if conn.fingerprint != self._fp:
            return
        fut = self._waiting.pop(str(envelope.payload.get("path", "/")), None)
        if fut is not None and not fut.done():
            fut.set_result(envelope.payload)

    def list(self, path: str) -> dict:
        async def go() -> dict:
            # File managers ask about one folder from several threads at once: they all share one request and its answer.
            fut = self._waiting.get(path)
            ask = fut is None or fut.done()
            if ask:
                fut = self._waiting[path] = self._loop.create_future()
            try:
                if ask and not await self._server.send(self._fp, "control", "files.list", {"path": path}):
                    fut.set_exception(OSError(errno.ENOTCONN, "phone not connected"))
                return await asyncio.wait_for(asyncio.shield(fut), LIST_TIMEOUT)
            except BaseException:
                if self._waiting.get(path) is fut:
                    del self._waiting[path]   # give up on this request: the next caller asks again
                raise

        return self._wait(go(), LIST_TIMEOUT + 5)

    def _wait(self, coro, timeout: float):
        """Runs `coro` on the server's loop from a file-system thread. Every failure becomes an OSError with an errno
        (FUSE needs one; a timeout is an OSError without it)."""
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, OSError) and exc.errno:
                raise
            raise OSError(errno.EIO, str(exc) or type(exc).__name__) from exc

    def fetch(self, path: str, cache_dir: Path) -> Path:
        from . import file_transfer

        async def go() -> Path:
            fut = file_transfer.expect_fetch(path, cache_dir)
            try:
                if not await self._server.send(self._fp, "control", "files.get", {"path": path}):
                    raise OSError(errno.ENOTCONN, "phone not connected")
                return await asyncio.wait_for(fut, FETCH_TIMEOUT)
            finally:
                file_transfer.cancel_fetch(path)

        return self._wait(go(), FETCH_TIMEOUT + 5)


def make_operations(backend, fingerprint: str):
    from fuse import FuseOSError, Operations

    class PhoneFS(Operations):
        def __init__(self) -> None:
            self._lists: dict[str, tuple[float, dict]] = {}
            self._lock = threading.Lock()
            self._fetch_locks: dict[str, threading.Lock] = {}

        def _listing(self, path: str) -> dict:
            now = time.monotonic()
            with self._lock:
                hit = self._lists.get(path)
                if hit and now - hit[0] < LIST_TTL:
                    return hit[1]
            listing = backend.list(path)
            if listing.get("error"):
                raise FuseOSError(errno.EACCES)
            with self._lock:
                self._lists[path] = (now, listing)
            return listing

        def _entry(self, path: str) -> dict | None:
            parent, name = os.path.split(path.rstrip("/"))
            for e in self._listing(parent or "/")["entries"]:
                if e["name"] == name:
                    return e
            return None

        def getattr(self, path, fh=None):
            if path == "/":
                return {"st_mode": stat.S_IFDIR | 0o555, "st_nlink": 2, "st_mtime": time.time()}
            e = self._entry(path)
            if e is None:
                raise FuseOSError(errno.ENOENT)
            mtime = (e.get("mtime") or 0) / 1000
            if e["is_dir"]:
                return {"st_mode": stat.S_IFDIR | 0o555, "st_nlink": 2, "st_mtime": mtime, "st_atime": mtime, "st_ctime": mtime}
            return {"st_mode": stat.S_IFREG | 0o444, "st_nlink": 1, "st_size": int(e.get("size") or 0),
                    "st_mtime": mtime, "st_atime": mtime, "st_ctime": mtime}

        def readdir(self, path, fh):
            return [".", ".."] + [e["name"] for e in self._listing(path)["entries"]]

        def _cached(self, path: str) -> Path:
            e = self._entry(path)
            if e is None or e["is_dir"]:
                raise FuseOSError(errno.ENOENT)
            key = hashlib.sha1(f"{path}|{e.get('size')}|{e.get('mtime')}".encode(), usedforsecurity=False).hexdigest()[:20]
            cache_dir = CACHE_ROOT / fingerprint / key
            target = cache_dir / Path(path).name
            with self._lock:
                lock = self._fetch_locks.setdefault(key, threading.Lock())
            with lock:  # one download per file even if several readers ask at once
                if not target.exists():
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    got = backend.fetch(path, cache_dir)
                    if got != target:
                        os.replace(got, target)
            return target

        def open(self, path, flags):
            if flags & (os.O_WRONLY | os.O_RDWR):
                raise FuseOSError(errno.EROFS)
            return os.open(self._cached(path), os.O_RDONLY)

        def read(self, path, size, offset, fh):
            return os.pread(fh, size, offset)

        def release(self, path, fh):
            os.close(fh)
            return 0

        def statfs(self, path):
            return {"f_bsize": 4096, "f_blocks": 0, "f_bavail": 0, "f_bfree": 0, "f_files": 0}

        def _readonly(self, *_a, **_k):
            raise FuseOSError(errno.EROFS)

        create = mkdir = rmdir = unlink = rename = write = truncate = chmod = chown = symlink = _readonly

    return PhoneFS()


class Mount:
    """One mounted phone. mount() returns an error string or None; unmount() is idempotent."""

    def __init__(self, server, loop, fingerprint: str, phone_name: str, mountpoint: Path | None = None) -> None:
        # No leading dots: a phone named "." or ".." must not move the mount point onto ~/PhoneBridge or the home folder.
        safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in phone_name).strip(" .") or "phone"
        self.path = mountpoint or (Path.home() / "PhoneBridge" / safe)
        self._server, self._loop, self._fp = server, loop, fingerprint
        self._thread: threading.Thread | None = None

    @property
    def mounted(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def mount(self) -> str | None:
        try:
            from fuse import FUSE
        except ImportError:
            return "Install the optional 'fusepy' package (pip install fusepy) to mount the phone as a folder"
        except OSError as exc:   # fusepy is installed but the libfuse system library is not ("Unable to find libfuse")
            return f"FUSE is not available on this system ({exc}). Install libfuse2 (or fuse3) to mount the phone as a folder"
        if self.mounted:
            return None
        self.path.mkdir(parents=True, exist_ok=True)
        ops = make_operations(ServerBackend(self._server, self._loop, self._fp), self._fp)

        def run() -> None:
            try:
                FUSE(ops, str(self.path), foreground=True, ro=True, nothreads=False, allow_other=False,
                     fsname="phonebridge", subtype="phonebridge")
            except Exception as exc:  # noqa: BLE001
                log.warning("mount ended: %s", exc)

        self._thread = threading.Thread(target=run, name="pb-fuse", daemon=True)
        self._thread.start()
        for _ in range(40):  # wait until the kernel shows the mount
            if os.path.ismount(self.path):
                return None
            if not self._thread.is_alive():
                return "Couldn't mount (is fuse3 installed and /dev/fuse accessible?)"
            time.sleep(0.1)
        return None

    def unmount(self) -> None:
        subprocess.run(["fusermount3", "-u", "-z", str(self.path)], capture_output=True)
        if self._thread:
            self._thread.join(3)
        self._thread = None
