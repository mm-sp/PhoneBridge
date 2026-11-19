# Changelog

## 1.4.0 - 2026-11 (unreleased; no tag/publication yet)
- Pairing v4 (SPAKE2), device identities, forward secrecy, key rotation, signed hash-chained audit ledger, permission manager, security dashboards, USB link, signed-update verification.
- Screen sync with PC-driven display-off and a restore watchdog.
- "Allow PC control" now expires (default 60 min, `PHONEBRIDGE_PC_CONTROL_MINUTES`); running shells are closed on expiry; shell no longer inherits `PHONEBRIDGE_*` variables.
- Android: portrait layout and TalkBack support (not yet tested on a device); release build with R8 + resource shrinking and log stripping; cleartext traffic disabled.
- Release signing moved to a permanent key stored outside the repository.
- Tooling: `tools/run_tests.sh` (strict), `tools/release.sh`, keystore backup/verify scripts, GitHub Actions workflow (unrun).
- Earlier history: see `RELEASE_NOTES.md` (1.0 - 1.3).
