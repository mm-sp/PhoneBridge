"""Keeps a test's TLS identity (cert + key) out of the user's real ~/.config/phonebridge and keyring."""
from pathlib import Path


def isolate_tls(cfg: Path | None = None) -> Path:
    from phonebridge import pairing, tls_identity

    cfg = Path(cfg) if cfg else pairing.DEVICES_DIR.parent
    cfg.mkdir(parents=True, exist_ok=True)
    tls_identity.CONFIG_DIR, tls_identity.CERT_PATH, tls_identity.KEY_PATH = cfg, cfg / "tls-cert.pem", cfg / "tls-key.pem"
    return cfg
