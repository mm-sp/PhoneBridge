"""AppLaunchSyncManager: "open PhoneBridge here" opens it on the other device, exactly once, and never ping-pongs.

Rules (all enforced here, one place, so both launch directions behave identically):
  1. Only an explicit user launch (Origin.USER) is forwarded. A launch that was itself caused by a remote command
     is Origin.AUTO and is never forwarded back  -> no infinite loop.
  2. A remote OPEN command is executed only if its origin is USER; AUTO ones are acknowledged and ignored.
  3. If the peer's app is already open (per its CONNECTION_STATUS), nothing is launched.
  4. If the local app is already open, a remote OPEN is acknowledged as `already_open` and nothing is launched.
  5. Cooldown + circuit breaker bound the launch rate in both directions even if the other side misbehaves.
  6. Command de-duplication (command_id) is done by CommandValidator before anything gets here.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from enum import Enum
from typing import Callable

from .commands import Cmd, Command, LAUNCH_COMMANDS, Origin, make

log = logging.getLogger("phonebridge.sync.launch")

SEND_COOLDOWN_S = 5.0          # don't forward two launches closer together than this
BREAKER_MAX = 4                # ... nor more than this many per BREAKER_WINDOW_S
BREAKER_WINDOW_S = 60.0
RECV_MAX = 3                   # executed remote launches per RECV_WINDOW_S
RECV_WINDOW_S = 30.0
PEER_STATUS_TTL_S = 30.0       # what we last heard about the peer's app state is only trusted this long
ECHO_SUPPRESS_S = 15.0         # a local launch this soon after executing a remote one is its consequence, not the user


class Decision(str, Enum):
    SENT = "sent"
    SKIP_AUTO = "skip_auto_origin"
    SKIP_ECHO = "skip_remote_caused"
    SKIP_DISABLED = "skip_sync_disabled"
    SKIP_LINK_DOWN = "skip_link_down"
    SKIP_PEER_OPEN = "skip_peer_already_open"
    SKIP_COOLDOWN = "skip_cooldown"
    SKIP_BREAKER = "skip_breaker_open"


class AppLaunchSyncManager:
    def __init__(self, local: str, *, send: Callable[[Command], None], launch_local: Callable[[Origin], bool],
                 is_local_open: Callable[[], bool], sync_enabled: Callable[[], bool],
                 link_up: Callable[[], bool], clock: Callable[[], float] = time.monotonic) -> None:
        self.local = local
        self._send, self._launch_local = send, launch_local
        self._is_local_open, self._sync_enabled, self._link_up = is_local_open, sync_enabled, link_up
        self._clock = clock
        self._peer_open: bool | None = None
        self._peer_ts = 0.0
        self._sent: deque[float] = deque()
        self._executed: deque[float] = deque()
        self._echo_until = 0.0

    @property
    def _outbound(self) -> Cmd:
        return Cmd.OPEN_PHONE_BRIDGE if self.local == "pc" else Cmd.OPEN_PHONE_BRIDGE_VIEWER

    def note_peer_status(self, app_open: bool | None) -> None:
        self._peer_open = app_open
        self._peer_ts = self._clock()

    @property
    def peer_open(self) -> bool | None:
        """Whether the peer's app is open, as last reported; None (unknown) once that report is older than the TTL."""
        return self._peer_open if self._clock() - self._peer_ts <= PEER_STATUS_TTL_S else None

    @peer_open.setter
    def peer_open(self, value: bool | None) -> None:
        self.note_peer_status(value)

    def on_local_app_opened(self, origin: Origin, session_id: str, args: dict | None = None) -> Decision:
        """Call when the local app/viewer was opened. Forwards it to the peer only when it should be."""
        now = self._clock()
        if origin != Origin.USER:
            return self._skip(Decision.SKIP_AUTO)
        if now < self._echo_until:
            return self._skip(Decision.SKIP_ECHO)
        if not self._sync_enabled():
            return self._skip(Decision.SKIP_DISABLED)
        if not self._link_up():
            return self._skip(Decision.SKIP_LINK_DOWN)
        if self.peer_open:
            return self._skip(Decision.SKIP_PEER_OPEN)
        self._prune(self._sent, BREAKER_WINDOW_S, now)
        if self._sent and now - self._sent[-1] < SEND_COOLDOWN_S:
            return self._skip(Decision.SKIP_COOLDOWN)
        if len(self._sent) >= BREAKER_MAX:
            log.error("launch circuit breaker open: %d forwards in %.0fs; refusing more", len(self._sent), BREAKER_WINDOW_S)
            return self._skip(Decision.SKIP_BREAKER)
        self._sent.append(now)
        self._send(make(self._outbound, session_id, Origin.USER, args))
        return Decision.SENT

    def on_remote_command(self, cmd: Command) -> tuple[bool, str]:
        """Handles OPEN_PHONE_BRIDGE / OPEN_PHONE_BRIDGE_VIEWER from the peer. Returns (ok, result) for the ack."""
        if cmd.cmd not in LAUNCH_COMMANDS:
            return False, "not_a_launch_command"
        now = self._clock()
        if cmd.origin != Origin.USER:
            return True, "ignored_auto_origin"
        if not self._sync_enabled():
            return True, "sync_disabled"
        if self._is_local_open():
            self.note_peer_status(True)
            return True, "already_open"
        self._prune(self._executed, RECV_WINDOW_S, now)
        if len(self._executed) >= RECV_MAX:
            log.error("refusing remote launch: %d already executed in %.0fs", len(self._executed), RECV_WINDOW_S)
            return False, "rate_limited"
        self._executed.append(now)
        self._echo_until = now + ECHO_SUPPRESS_S
        try:
            launched = self._launch_local(Origin.AUTO)
        except Exception:  # noqa: BLE001
            log.exception("local launch raised")
            launched = False
        if not launched:
            self._echo_until = 0.0
        return launched, "launched" if launched else "launch_failed"

    @staticmethod
    def _prune(q: deque, window: float, now: float) -> None:
        while q and now - q[0] > window:
            q.popleft()

    @staticmethod
    def _skip(d: Decision) -> Decision:
        log.info("launch not forwarded: %s", d.value)
        return d
