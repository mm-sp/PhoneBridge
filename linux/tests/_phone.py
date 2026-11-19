"""Phone-side helpers for tests: a fake phone that speaks the real v4 protocol (Ed25519 identity + ephemeral handshake)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import crypto, secure_session  # noqa: E402


def new_identity() -> secure_session.Identity:
    return secure_session.Identity.generate()


async def phone_connect(reader, writer, fingerprint: str, psk: bytes, identity, tls_fp: str,
                        pc_public_b64: str | None = None, ident_line: str | None = None, confirm: bool = True):
    """Writes the ident line, runs the client side of the v4 handshake and returns the Session (cipher included).
    `pc_public_b64` defaults to the PC identity of the current test config (what the phone learned at pairing)."""
    from phonebridge import pairing

    pc_pub = pc_public_b64 or pairing.pc_identity().public_b64
    writer.write(((ident_line or fingerprint) + "\n").encode())
    frame, state = secure_session.client_hello(psk, fingerprint, tls_fp, identity)
    writer.write(crypto.pack_frame(frame))
    await writer.drain()
    n = crypto.unpack_frame_len(await reader.readexactly(crypto.FRAME_LEN_BYTES))
    session = secure_session.client_finish(state, await reader.readexactly(n), pc_pub)
    if confirm:
        # Key confirmation: the PC only accepts the session once a first frame decrypts under the new keys (the real
        # Android app's first frame is device.info). seq 0 so the test's own numbering can start at 1.
        from phonebridge.protocol import Envelope
        writer.write(crypto.pack_frame(session.cipher.encrypt(Envelope("control", "hello", 0, {}).to_json())))
        await writer.drain()
    return session
