"""Authentication throttling: after repeated failures from one address, refuse it for a growing cool-down."""
from __future__ import annotations

import time


class AuthThrottle:
    def __init__(self, max_failures: int = 5, window: float = 300.0, base_lockout: float = 30.0, max_lockout: float = 900.0,
                 clock=time.monotonic) -> None:
        self._max, self._window, self._base, self._cap, self._clock = max_failures, window, base_lockout, max_lockout, clock
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}

    def blocked(self, key: str) -> bool:
        return self._clock() < self._locked_until.get(key, 0.0)

    def failure(self, key: str) -> bool:
        """Record a failed attempt; returns True if this failure locks the address out."""
        now = self._clock()
        recent = [t for t in self._fails.get(key, []) if now - t < self._window] + [now]
        self._fails[key] = recent
        if len(recent) >= self._max:
            strikes = self._strikes[key] = self._strikes.get(key, 0) + 1
            self._locked_until[key] = now + min(self._cap, self._base * 2 ** (strikes - 1))
            self._fails[key] = []
            return True
        return False

    def success(self, key: str) -> None:
        self._fails.pop(key, None)
        self._strikes.pop(key, None)
