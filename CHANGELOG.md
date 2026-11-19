# Changelog

## 1.4.0 - 2026-11 (unreleased; no tag/publication yet)
- Pairing v4 (SPAKE2), device identities, forward secrecy, key rotation, signed hash-chained audit ledger, permission manager, security dashboards, USB link, signed-update verification.
- Screen sync with PC-driven display-off and a restore watchdog.
- Phone-to-PC control now needs approval on the PC: a phone's request raises a prompt (Deny / 15 min / 1 hour); maximum one hour; a banner shows who has control with "Revoke now"; revoke/expiry close running shells; refusal/revoke starts a 2-minute cooldown; all steps are audit-logged. Shell no longer inherits `PHONEBRIDGE_*` variables.
- Android: documented the `Clipboard.overlay` lint warning as a false positive (application-context view, always cleared).
- Android: portrait layout and TalkBack support (not yet tested on a device); release build with R8 + resource shrinking and log stripping; cleartext traffic disabled.
- Release signing moved to a permanent key stored outside the repository.
- Tooling: `tools/run_tests.sh` (strict), `tools/release.sh`, keystore backup/verify scripts, GitHub Actions workflow (unrun).
- Licensed under Apache-2.0 (`LICENSE`, `NOTICE`).
- Earlier history: see `RELEASE_NOTES.md` (1.0 - 1.3).
