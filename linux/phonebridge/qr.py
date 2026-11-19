"""Render a pairing payload as a QR code PNG, plus the human-readable PIN fallback."""
from __future__ import annotations

import json
from pathlib import Path



def render_qr(payload: dict, out_path: str | Path) -> Path:
    import qrcode  # lazy: the pairing window can still show the PIN if the package is missing

    img = qrcode.make(json.dumps(payload, separators=(",", ":")))
    out_path = Path(out_path)
    img.save(out_path)
    return out_path


def pin_text(payload: dict) -> str:
    """6-digit token shown under the QR for manual entry when scanning isn't possible."""
    return payload["token"]
