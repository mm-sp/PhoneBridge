"""A shell on this PC for the phone app's Terminal tab (protocol/PROTOCOL.md section 4c).

The user's login shell runs in a pseudo-terminal; its output is batched and sent to the
phone, and the phone's keystrokes are written straight into it (so the shell's own
line editing, history and Tab completion work). TERM=dumb and a no-op pager keep tools to
plain text the phone can show; full-screen programs (vim, htop) aren't supported.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
from collections.abc import Awaitable, Callable

from .netutil import spawn

log = logging.getLogger("phonebridge.terminal")

SCROLLBACK = 64 * 1024  # bytes of recent output re-sent when the phone reopens the tab
FLUSH_DELAY = 0.03


class TerminalSession:
    def __init__(self, loop: asyncio.AbstractEventLoop, send: Callable[[str, dict], Awaitable[None]],
                 cols: int = 80, rows: int = 24) -> None:
        self._loop = loop
        self._send = send
        self._pending = bytearray()
        self._flush_handle: asyncio.TimerHandle | None = None
        self.scrollback = bytearray()
        self.alive = True

        shell = os.environ.get("SHELL") or "/bin/bash"
        env = dict(os.environ, TERM="dumb", PAGER="cat", GIT_PAGER="cat", SYSTEMD_PAGER="", MANPAGER="cat",
                   LESS="-FRX", COLUMNS=str(cols), LINES=str(rows))
        env.pop("PROMPT_COMMAND", None)
        # The phone's shell must not inherit PhoneBridge's own configuration/secrets (e.g. the keystore passphrase).
        for name in [k for k in env if k.startswith("PHONEBRIDGE_")]:
            del env[name]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child: become the shell, in the user's home folder
            try:
                os.chdir(os.path.expanduser("~"))
                os.execvpe(shell, [shell, "-i"], env)
            finally:
                os._exit(127)
        os.set_blocking(self.fd, False)
        self.resize(cols, rows)
        loop.add_reader(self.fd, self._on_readable)
        log.info("terminal started (pid %d, %s)", self.pid, shell)

    def _on_readable(self) -> None:
        try:
            data = os.read(self.fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            self._on_exit()
            return
        self._pending += data
        self.scrollback += data
        if len(self.scrollback) > SCROLLBACK:
            del self.scrollback[: len(self.scrollback) - SCROLLBACK]
        if self._flush_handle is None:
            self._flush_handle = self._loop.call_later(FLUSH_DELAY, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        if self._pending:
            text = self._pending.decode("utf-8", "replace")
            self._pending.clear()
            spawn(self._send("pc.term_output", {"data": text}))

    def _on_exit(self) -> None:
        if not self.alive:
            return
        self.alive = False
        self._loop.remove_reader(self.fd)
        self._flush()
        code = None
        try:
            _pid, status = os.waitpid(self.pid, os.WNOHANG)
            code = os.waitstatus_to_exitcode(status) if _pid else None
        except ChildProcessError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        spawn(self._send("pc.term_exit", {"code": code}))
        log.info("terminal exited (%s)", code)

    def write(self, data: str) -> None:
        if self.alive:
            try:
                os.write(self.fd, data.encode("utf-8"))
            except OSError:
                self._on_exit()

    def resize(self, cols: int, rows: int) -> None:
        if self.alive and cols > 0 and rows > 0:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def close(self) -> None:
        if self.alive:
            try:
                os.killpg(os.getpgid(self.pid), signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass
            self._on_exit()
