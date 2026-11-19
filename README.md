# PhoneBridge

An account-free, cloud-free bridge between an **Android phone** and a **Linux desktop**: notifications, SMS/MMS, calls, photos, files, clipboard, screen mirroring with control, phone-as-webcam, and (opt-in) controlling the PC from the phone. Everything travels directly between your two devices, end-to-end encrypted.

> **Status: 1.4.0, pre-release.** Not yet published. No license has been chosen yet (see below). Verified on one phone (Xiaomi Mi A3, Android 17) and one PC (Ubuntu 24.04). Not available on Google Play (see `docs/PLAY_STORE.md`).

## Features
Pairing by QR/PIN (SPAKE2) - notifications mirror/reply - SMS and MMS - calls - photo browser - file transfer with resume and SHA-256 - clipboard sync (text, images) - screen mirroring with mouse/keyboard (TCP or encrypted UDP) - smart display-off "screen sync" - phone camera as a webcam (v4l2loopback) - app launcher - phone-controls-PC tabs (files, terminal, ports, services, Docker, screen) with a time-limited switch - USB link and optional self-hosted relay - Security dashboards with a signed audit log and per-feature permissions.
Feature-by-feature status (including what is experimental or unverified): `docs/PROJECT_REPORT.md`.

## Screenshots
_No screenshots have been added yet._ Place real screenshots in `docs/screenshots/` and link them here.

## Quick start
```sh
./packaging/install.sh && phonebridge        # Linux app
# Android: install the APK from the release page (see docs/INSTALLATION.md), then pair via QR or PIN
```
Build from source: `docs/BUILD_GUIDE.md`. Run all tests: `tools/run_tests.sh`.

## Documentation
| | |
|---|---|
| `docs/INSTALLATION.md`, `docs/BUILD_GUIDE.md`, `docs/TROUBLESHOOTING.md` | using and building |
| `docs/ARCHITECTURE.md`, `protocol/PROTOCOL.md` | design and wire protocol |
| `docs/SECURITY.md`, `SECURITY.md` | security model / reporting a vulnerability |
| `docs/PROJECT_REPORT.md`, `docs/AUDIT_REPORT.md` | status and audit results |
| `docs/SIGNING.md`, `docs/RELEASE.md`, `docs/PLAY_STORE.md` | signing, releasing, Google Play |
| `CHANGELOG.md`, `RELEASE_NOTES.md`, `docs/DEVELOPMENT_HISTORY.md` | history |

## Security in one paragraph
TLS 1.3 with a pinned certificate, an Ed25519-signed ephemeral key exchange, AES-256-GCM, SPAKE2 PIN pairing, per-feature permissions on both devices, a signed hash-chained audit log. Phone-controls-PC is off by default and expires after an hour. This has had automated tests, fuzzing and an AI-assisted review, **not** an independent human audit. Limits are listed in `docs/SECURITY.md`.

## Contributing / bugs
`CONTRIBUTING.md`. Report security problems privately (`SECURITY.md`), never in a public issue.

## License
**None chosen yet.** Until a `LICENSE` file is added, all rights are reserved by the author and others have no permission to use, modify or redistribute the code.
