"""Session state machine. Illegal transitions are refused (and logged), never silently applied.

DISCONNECTED means "no mirroring session". The control link can be up while the machine is DISCONNECTED (idle);
that is tracked separately by the controller as `link_up`.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from enum import Enum
from typing import Callable

log = logging.getLogger("phonebridge.sync.state")


class State(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    AUTHENTICATING = "AUTHENTICATING"
    APP_LAUNCHING = "APP_LAUNCHING"
    MIRROR_STARTING = "MIRROR_STARTING"
    MIRROR_ACTIVE = "MIRROR_ACTIVE"
    DISPLAY_OFF = "DISPLAY_OFF"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


S = State
TRANSITIONS: dict[State, frozenset[State]] = {
    S.DISCONNECTED: frozenset({S.CONNECTING, S.AUTHENTICATING, S.APP_LAUNCHING, S.MIRROR_STARTING, S.ERROR}),
    S.CONNECTING: frozenset({S.AUTHENTICATING, S.DISCONNECTED, S.ERROR, S.RECONNECTING}),
    S.AUTHENTICATING: frozenset({S.APP_LAUNCHING, S.MIRROR_STARTING, S.DISCONNECTED, S.ERROR, S.RECONNECTING}),
    S.APP_LAUNCHING: frozenset({S.MIRROR_STARTING, S.STOPPING, S.RECONNECTING, S.DISCONNECTED, S.ERROR}),
    S.MIRROR_STARTING: frozenset({S.MIRROR_ACTIVE, S.STOPPING, S.RECONNECTING, S.DISCONNECTED, S.ERROR}),
    S.MIRROR_ACTIVE: frozenset({S.DISPLAY_OFF, S.STOPPING, S.RECONNECTING, S.DISCONNECTED, S.ERROR}),
    S.DISPLAY_OFF: frozenset({S.MIRROR_ACTIVE, S.STOPPING, S.RECONNECTING, S.DISCONNECTED, S.ERROR}),
    S.RECONNECTING: frozenset({S.CONNECTING, S.AUTHENTICATING, S.APP_LAUNCHING, S.MIRROR_STARTING, S.STOPPING,
                               S.DISCONNECTED, S.ERROR}),
    S.STOPPING: frozenset({S.DISCONNECTED, S.ERROR}),
    S.ERROR: frozenset({S.DISCONNECTED, S.CONNECTING, S.RECONNECTING, S.STOPPING}),
}

# States in which the phone's display may be off or about to be, i.e. where a display restore must be considered.
MIRRORING_STATES = frozenset({S.MIRROR_STARTING, S.MIRROR_ACTIVE, S.DISPLAY_OFF})

Listener = Callable[[State, State, str], None]


class SessionStateMachine:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.RLock()
        self._state = State.DISCONNECTED
        self._clock = clock
        self._since = clock()
        self._listeners: list[Listener] = []
        self.history: deque[tuple[float, State, State, str]] = deque(maxlen=64)
        self.last_error = ""

    @property
    def state(self) -> State:
        return self._state

    @property
    def seconds_in_state(self) -> float:
        return self._clock() - self._since

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def can(self, to: State) -> bool:
        return to in TRANSITIONS[self._state]

    def transition(self, to: State, reason: str = "") -> bool:
        """Moves to `to` if the table allows it. Returns False (state unchanged) otherwise. Same-state is a no-op True."""
        with self._lock:
            old = self._state
            if to == old:
                return True
            if to not in TRANSITIONS[old]:
                log.warning("refused illegal transition %s -> %s (%s)", old.value, to.value, reason)
                return False
            self._state = to
            self._since = self._clock()
            if to == State.ERROR:
                self.last_error = reason
            self.history.append((self._since, old, to, reason))
        log.info("session %s -> %s %s", old.value, to.value, f"({reason})" if reason else "")
        for fn in list(self._listeners):
            try:
                fn(old, to, reason)
            except Exception:  # noqa: BLE001 - a broken listener must not wedge the machine
                log.exception("state listener failed")
        return True

    def fail(self, reason: str) -> None:
        """Any state may fail; ERROR is always reachable (unless already there)."""
        with self._lock:
            if self._state == State.ERROR:
                self.last_error = reason
                return
            old = self._state
            self._state = State.ERROR
            self._since = self._clock()
            self.last_error = reason
            self.history.append((self._since, old, State.ERROR, reason))
        log.error("session %s -> ERROR (%s)", old.value, reason)
        for fn in list(self._listeners):
            try:
                fn(old, State.ERROR, reason)
            except Exception:  # noqa: BLE001
                log.exception("state listener failed")
