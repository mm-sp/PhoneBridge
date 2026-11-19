"""Control-channel envelope (de)serialization — matches protocol/schema.json."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

VALID_CH = {"control", "file", "video"}


@dataclass
class Envelope:
    ch: str
    type: str
    seq: int
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> bytes:
        if self.ch not in VALID_CH:
            raise ValueError(f"invalid channel {self.ch!r}")
        return json.dumps(
            {"ch": self.ch, "type": self.type, "seq": self.seq, "payload": self.payload},
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def from_json(blob: bytes) -> "Envelope":
        obj = json.loads(blob.decode("utf-8"))
        return Envelope(ch=obj["ch"], type=obj["type"], seq=obj["seq"], payload=obj.get("payload", {}))


class SeqCounter:
    """Monotonic per-connection sequence numbers, one counter per direction."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n
