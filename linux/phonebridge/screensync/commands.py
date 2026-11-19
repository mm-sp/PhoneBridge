"""Wire format, validation and replay protection for the sync commands.

Transport: control-channel envelopes of type `sync.command` (and `sync.ack` for the reply). The channel itself is
already pinned-TLS + v4 handshake + AES-GCM + per-connection seq replay window, so these commands are only ever
accepted from the authenticated paired peer. This layer adds what the transport cannot know:

  * an allow-list of commands per direction and strict per-command argument schemas;
  * `command_id` de-duplication (a replayed or double-delivered command is acknowledged, never executed twice);
  * a timestamp freshness window (stale commands are refused);
  * `session_id` binding (a command for another/old session is refused);
  * an `origin` ("user" | "auto") so a launch caused by a command can never cause another launch.
"""
from __future__ import annotations

import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

TYPE_COMMAND = "sync.command"
TYPE_ACK = "sync.ack"

MAX_SKEW_MS = 5 * 60 * 1000          # peers' clocks are not synchronised; this only stops very old recordings
REPLAY_CACHE_SIZE = 2048
REPLAY_TTL_S = 15 * 60
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class Cmd(str, Enum):
    OPEN_PHONE_BRIDGE = "OPEN_PHONE_BRIDGE"                  # PC -> phone: bring the Android app up
    OPEN_PHONE_BRIDGE_VIEWER = "OPEN_PHONE_BRIDGE_VIEWER"    # phone -> PC: open the viewer window
    START_MIRRORING = "START_MIRRORING"
    STOP_MIRRORING = "STOP_MIRRORING"
    DISPLAY_OFF = "DISPLAY_OFF"
    DISPLAY_ON = "DISPLAY_ON"
    MIRROR_READY = "MIRROR_READY"                            # PC -> phone: frames are arriving
    CONNECTION_STATUS = "CONNECTION_STATUS"                  # either way: state + latency probe


class Origin(str, Enum):
    USER = "user"    # a person explicitly opened the app
    AUTO = "auto"    # started by a command, boot, reconnect, ... never propagates to the other side


# Who may receive what. Receiving a command outside its set is refused even from an authenticated peer.
PC_ACCEPTS = frozenset({Cmd.OPEN_PHONE_BRIDGE_VIEWER, Cmd.START_MIRRORING, Cmd.STOP_MIRRORING, Cmd.DISPLAY_OFF,
                        Cmd.DISPLAY_ON, Cmd.CONNECTION_STATUS})
PHONE_ACCEPTS = frozenset({Cmd.OPEN_PHONE_BRIDGE, Cmd.START_MIRRORING, Cmd.STOP_MIRRORING, Cmd.DISPLAY_OFF,
                           Cmd.DISPLAY_ON, Cmd.MIRROR_READY, Cmd.CONNECTION_STATUS})
# Commands that legitimately start a session, so they cannot be bound to the current one.
SESSION_STARTERS = frozenset({Cmd.OPEN_PHONE_BRIDGE, Cmd.OPEN_PHONE_BRIDGE_VIEWER, Cmd.START_MIRRORING})
# Commands that have to come from a person's explicit action to cause a launch.
LAUNCH_COMMANDS = frozenset({Cmd.OPEN_PHONE_BRIDGE, Cmd.OPEN_PHONE_BRIDGE_VIEWER})

DISPLAY_MODES = ("adb", "dim", "")        # who/how performs the change; "" = receiver decides
STATUS_FIELDS = {"state": str, "display": str, "link": str, "app_open": bool, "mirroring": bool,
                 "probe_ts": int, "echo_ts": int, "fps": (int, float), "width": int, "height": int,
                 "rotation": int, "note": str, "smart": str, "dim_ok": bool,
                 "probe_content": bool}


class Reject(str, Enum):
    MALFORMED = "malformed"
    UNKNOWN_COMMAND = "unknown_command"
    NOT_ALLOWED = "not_allowed_here"
    BAD_ARGS = "bad_args"
    STALE = "stale"
    DUPLICATE = "duplicate"
    WRONG_SESSION = "wrong_session"


@dataclass(frozen=True)
class Command:
    cmd: Cmd
    session_id: str
    command_id: str
    ts: int
    origin: Origin = Origin.AUTO
    args: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {"cmd": self.cmd.value, "session_id": self.session_id, "command_id": self.command_id, "ts": self.ts,
                "origin": self.origin.value, "args": self.args}


def new_id() -> str:
    return uuid.uuid4().hex


def make(cmd: Cmd, session_id: str, origin: Origin = Origin.AUTO, args: dict | None = None,
         now_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> Command:
    return Command(cmd, session_id, new_id(), now_ms(), origin, dict(args or {}))


@dataclass
class Verdict:
    ok: bool
    command: Command | None = None
    reason: Reject | None = None
    detail: str = ""


def _check_args(cmd: Cmd, args: dict) -> str | None:
    """Returns an error string, or None when args are valid for this command."""
    if not isinstance(args, dict) or len(args) > 16:
        return "args must be a small object"
    if cmd in (Cmd.DISPLAY_OFF, Cmd.DISPLAY_ON):
        if args.get("mode", "") not in DISPLAY_MODES:
            return "mode must be adb|dim"
        if "performed" in args and not isinstance(args["performed"], bool):
            return "performed must be boolean"
        extra = set(args) - {"mode", "performed", "reason"}
    elif cmd == Cmd.CONNECTION_STATUS:
        for k, v in args.items():
            t = STATUS_FIELDS.get(k)
            if t is None:
                return f"unknown status field {k!r}"
            if not isinstance(v, t) or (isinstance(v, bool) and t is not bool):
                return f"bad type for {k!r}"
            if isinstance(v, str) and len(v) > 200:
                return f"{k!r} too long"
        extra = set()
    elif cmd in (Cmd.START_MIRRORING, Cmd.STOP_MIRRORING, Cmd.MIRROR_READY,
                 Cmd.OPEN_PHONE_BRIDGE, Cmd.OPEN_PHONE_BRIDGE_VIEWER):
        for k, v in args.items():
            if k not in ("reason", "fps", "width", "height", "app"):
                return f"unknown arg {k!r}"
            if k in ("reason", "app") and (not isinstance(v, str) or len(v) > 200):
                return f"bad {k!r}"
            if k in ("fps", "width", "height") and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 100000):
                return f"bad {k!r}"
        extra = set()
    else:  # pragma: no cover
        return "unhandled command"
    if extra:
        return f"unknown arg {sorted(extra)[0]!r}"
    return None


class CommandValidator:
    """One per connection/peer. `direction` says which side we are ('pc' or 'phone')."""

    def __init__(self, local: str, clock_ms: Callable[[], int] | None = None, mono: Callable[[], float] = time.monotonic) -> None:
        if local not in ("pc", "phone"):
            raise ValueError("local must be 'pc' or 'phone'")
        self._accepts = PC_ACCEPTS if local == "pc" else PHONE_ACCEPTS
        self._now_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._mono = mono
        self._seen: OrderedDict[str, float] = OrderedDict()

    def _remember(self, command_id: str) -> None:
        now = self._mono()
        self._seen[command_id] = now
        while self._seen and (len(self._seen) > REPLAY_CACHE_SIZE or now - next(iter(self._seen.values())) > REPLAY_TTL_S):
            self._seen.popitem(last=False)

    def seen(self, command_id: str) -> bool:
        t = self._seen.get(command_id)
        return t is not None and self._mono() - t <= REPLAY_TTL_S

    def validate(self, payload: Any, current_session: str | None) -> Verdict:
        """Parses and checks one inbound sync.command payload. On success the command_id is recorded, so a second
        delivery gets Reject.DUPLICATE. Never raises on hostile input."""
        if not isinstance(payload, dict):
            return Verdict(False, reason=Reject.MALFORMED, detail="payload is not an object")
        try:
            cmd_name = payload.get("cmd")
            session_id, command_id, ts = payload.get("session_id"), payload.get("command_id"), payload.get("ts")
            origin_raw = payload.get("origin", "auto")
            args = payload.get("args", {})
            if not isinstance(cmd_name, str):
                return Verdict(False, reason=Reject.MALFORMED, detail="cmd missing")
            try:
                cmd = Cmd(cmd_name)
            except ValueError:
                return Verdict(False, reason=Reject.UNKNOWN_COMMAND, detail=cmd_name[:40])
            if not (isinstance(session_id, str) and _ID_RE.match(session_id)
                    and isinstance(command_id, str) and _ID_RE.match(command_id)):
                return Verdict(False, reason=Reject.MALFORMED, detail="bad session_id/command_id")
            if isinstance(ts, bool) or not isinstance(ts, int):
                return Verdict(False, reason=Reject.MALFORMED, detail="ts must be integer ms")
            try:
                origin = Origin(origin_raw)
            except ValueError:
                return Verdict(False, reason=Reject.MALFORMED, detail="bad origin")
            if cmd not in self._accepts:
                return Verdict(False, reason=Reject.NOT_ALLOWED, detail=cmd.value)
            bad = _check_args(cmd, args)
            if bad:
                return Verdict(False, reason=Reject.BAD_ARGS, detail=bad)
            if abs(self._now_ms() - ts) > MAX_SKEW_MS:
                return Verdict(False, reason=Reject.STALE, detail="timestamp outside freshness window")
            if self.seen(command_id):
                return Verdict(False, reason=Reject.DUPLICATE, detail=command_id)
            if cmd not in SESSION_STARTERS and cmd != Cmd.CONNECTION_STATUS and session_id != current_session:
                return Verdict(False, reason=Reject.WRONG_SESSION, detail="no such active session")
            self._remember(command_id)
            return Verdict(True, Command(cmd, session_id, command_id, ts, origin, args))
        except Exception as exc:  # noqa: BLE001 - anything odd is a rejection, not a crash
            return Verdict(False, reason=Reject.MALFORMED, detail=type(exc).__name__)


def ack_payload(command_id: str, ok: bool, result: str = "", reason: str = "") -> dict[str, Any]:
    return {"command_id": command_id, "ok": ok, "result": result[:60], "reason": reason[:120]}
