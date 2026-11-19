# PhoneBridge security report

**PhoneBridge is not "completely secure" or "unhackable".** This lists what exists and is tested, what does not, and the
assumptions it rests on. Test counts are real runs of `linux/tests/run_all.py`.

## Architecture

```
Android phone ── TLS 1.3 (pinned self-signed cert; or USB via adb reverse) ── Linux PC (ControlChannelServer)
  per connection: Ed25519-signed ephemeral X25519 handshake (forward secrecy) → per-direction AES-256-GCM keys
  file/video data connections and UDP video use keys derived from the live session; the control channel rekeys in-band
Security engine, PC (linux/phonebridge/security/): keystore · signed hash-chained ledger · permissions · throttle
Security engine, phone: Android-Keystore-wrapped keys · signed hash-chained log · permission manager · privacy settings
Dashboards: PC "Security" page, Android "Security" tab.   No cloud, no blockchain, works fully offline.
```

## What is implemented (and how it is tested)

| Spec area | Implementation | Tests |
|---|---|---|
| Device identity | Ed25519 identity per device; public keys exchanged inside the PIN-authenticated pairing channel; fingerprints shown as device IDs; **rotation** in both directions (old key vouches for new; offline phones are never locked out); revocation | test_secure_session (rotation), test_android_interop |
| Secure key storage | PC: desktop keyring (Secret Service), encrypted-file fallback (scrypt+AES-GCM with a passphrase) and the dashboard says when keys are *not* protected. Phone: private key and pairing key wrapped by an Android Keystore AES key (hardware-backed where supported) | test_security, test_android_audit (log), on-device |
| Pairing | **SPAKE2 PAKE** (P-256, RFC 9382 construction; protocol §1j): the PIN is never sent and recorded exchanges give no offline PIN test, wrong PINs are counted per live attempt, confirmation code on both screens, explicit approval, 3 wrong PINs burn the code, **code lives 60 s, is single-use**, dialog shows the countdown and swaps in a fresh code | test_pairing_handshake (31), test_pairing_window_ui |
| End-to-end encryption | TLS 1.3 only (downgrade refused) + AES-256-GCM, per-direction keys, direction-bound AAD, replay window, **forward secrecy** (ephemeral keys, tested against a full-compromise attacker), in-band **key rotation**, **key confirmation** (a replayed hello can't displace the real phone), authenticated **session termination** (abrupt closes are recorded) | test_secure_session (66), test_android_interop (18) |
| Mutual authentication | Both identities sign every handshake, bound to the PC's TLS certificate; the shared pairing key alone cannot impersonate either device | test_secure_session |
| Zero-trust permissions | PC: allow/deny/ask per feature per phone, checked on every message. **Phone: permission manager** for what the PC may request (files, photos, notifications, messages, calls, clipboard, screen, remote control, apps); sending a message or placing a call **asks every time** | test_security, test_android_audit |
| Secure file transfer | Chunked, SHA-256-verified, resumable; **cancellable** (both sides, partial deleted); partials private (PC 0600, phone app-private); stale partials purged after 7 days; path traversal blocked on both sides; hidden/home-escape paths refused | test_file_transfer (34), test_hardening, test_files_cancel_ui |
| Notifications / clipboard / SMS | Notifications: master switch, hide all text, auto-hide sensitive ones (codes, password/authenticator apps), per-app blocking. Clipboard: master switch, expiry of synced text (PC and phone), history retention, password-manager copies never forwarded. SMS: reading is permission-gated; sending asks each time; SMS are not written to the PC's disk | test_android_audit (sensitivity rules), test_clipboard_privacy_ui |
| Blockchain-ready ledger | `SecurityLedger` → `LocalLedger` (Ed25519-signed, hash-chained) + `LedgerVerifier`; **`PrivateBlockchain`** (optional, off by default): anchors the ledger head on a private permissioned chain *you* host (`anchor_server.py`, or a file on a second medium) so even a full rewrite or truncation of the log is detected; only hashes/counts/public keys leave the device; works offline | test_security (69), test_anchor (18) |
| Audit logging | PC: pairing, auth ok/fail/throttled, sessions (clean/abrupt), permission & privacy changes, denials, transfers (size/outcome only), rotation, rekey, revoke, disconnect-all. Phone: the same classes of events in its own signed chain, verified independently by a Python implementation | test_security, test_android_audit |
| Network | TLS 1.3, auth throttling with back-off, handshake/idle timeouts, secure reconnection (fresh keys every time), nothing requires a cloud. **USB** link via `adb reverse` (optional; needs adb + USB debugging) | test_secure_session §7, test_usb_link |
| Dashboards | PC page and Android tab: status, encryption, identities, ledger/log verification, permissions, privacy, active session, failed auths, recent events, revoke, **DISCONNECT ALL** | test_security_ui |
| Internet exposure | The server listens on all interfaces but **drops any connection (TCP, discovery, UDP video) whose source is not loopback/USB, private, link-local or on a network directly attached to the PC** before any protocol runs; `PHONEBRIDGE_ALLOW_PUBLIC=1` opts out. Hostile first lines (non-ASCII, endless, stalled) are dropped too | test_peer_policy (23), test_fuzz_quick |
| Fuzzing and static analysis | Parser fuzzing (handshake frames, envelopes, framing, UDP, pairing hello, manifests, paths, file offers) and a live-server fuzz (330 hostile connections, then a legitimate phone must still be served); Bandit (0 high/medium findings) and Android lint (security findings fixed: certificate-pinning trust manager, broadcast receiver); the fuzzers found and I fixed three real bugs | test_fuzz_quick, tests/fuzz_protocol.py |
| Advanced | Forward secrecy, rotation, pinning, downgrade protection, **signed updates** (release key generated and its public half pinned; `phonebridge.updates check <https/file URL>` fetches, verifies the signature against the pinned key, refuses downgrades/replays, downloads and hash-checks every artifact, installs nothing; `tools/publish_release.py` builds + signs a release; verified end to end on a real staged 1.4.0 release), **dependency scanning** (`tools/audit_dependencies.py` against OSV), log hygiene check (no keys/content in logs) | test_updates, test_log_hygiene |

## Measured cost (`tools/bench.py`, this laptop, loopback)

handshake 1.4 ms CPU · reconnect (TLS 1.3 + handshake + first round trip) 32 ms · small control frame 6.7 µs ·
AES-256-GCM 250 MB/s · 64 MiB encrypted file transfer 63 MB/s (faster than the Wi-Fi link) · ledger append 0.2 ms.
Re-verifying the ledger caches signature checks, so the dashboard stays fast as the log grows.

## Dependency scan (2026-11-13)

`python3 tools/audit_dependencies.py` → **no known vulnerabilities** for the installed app's venv (cryptography 50.0.2,
pillow 13.0.0, qrcode, PyGObject) and the Android libraries (BouncyCastle upgraded 1.79 → 1.87 because of five advisories;
zxing, androidx.core). The system Python's distro packages are older and are flagged by version, but the app does not use them.
Only package names and versions are ever sent, and only when you run the tool.

## Independent review (fresh-context reviewer, read-only)

A reviewer with no knowledge of how the code was written audited the handshake, transport, file/path handling, the Android
permission gate and the security modules. **Cryptographic core (handshake, key schedule, rekey, data keys): no flaw found.**
Findings in the surrounding code, and what was done:

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | PIN-only pairing: a LAN host answering the phone's discovery broadcast can crack the 6-digit PIN offline from the phone's proof and be paired silently | medium-high | **Fixed**: pairing v4 verifies the PIN with SPAKE2 (a PAKE), so there is nothing to crack offline: each guess needs one live exchange, counted and capped at 3 per code. The user still confirms the 6-digit code against the real PC's screen, and QR pairing additionally pins the PC's key. Tests: test_spake2 (incl. the real Kotlin code vs the Python), test_pairing_handshake. |
| 2 | UDP video: no replay window; a captured datagram could reset the stream, replay an old frame, and redirect replies | low-medium | **Fixed** (per-stream replay window, retired epochs can't return, reply address only from accepted datagrams). Phone-side receive path not hardened (low value). |
| 3 | Unauthenticated path traversal in the device lookup via the first line (`../devices/<fp>`) | low-medium | **Fixed** (fingerprints are lowercase hex only, validated before any file access; malformed records are skipped, not crashes) |
| 4 | UDP discovery flood did file/keyring work per packet on the event loop | low-medium | **Fixed** (per-source rate limit, cached tags) |
| 5 | Lockdown/revoke race during the handshake | low | **Fixed** (re-checked after key confirmation) |
| 6 | TCP video: state set before authentication, ledger/throttle flood, `ask` not enforced | low | **Fixed** |
| 7 | Pairing listener: no peer policy, no length cap, slow-drip hold | low | **Fixed** (peer policy, 8 KiB cap, 30 s total deadline) |
| 8 | Android MMS `requestId` path traversal | low | **Fixed** (letters/digits only) |
| 9 | Phone-chosen file names auto-opened on the PC | low | **Fixed** (only inert types open automatically; others need a click) |
| 10 | Rotated-away phone identity key honoured forever | low | **Fixed** (7-day grace period) |
| 11 | Smaller: secrets files briefly world-readable; USB tunnel throttle (loopback) lets a local user cut off the USB phone; huge `size` overflow; docker ids starting with `-` | low | **Fixed** (0600 from creation; loopback exempt from throttle; overflow-safe check; id validation). **Not changed:** `device.info_request` and `relay.config` skip the phone's permission gate (metadata only; TLS pinning still holds); `remove_stale_pairings` removes any pairing with the same phone name (needs the user to approve a new pairing first). |
| 12 | "Join phone hotspot from this PC" ran `nmcli … password <PW>`: the Wi-Fi password was visible in `ps` to every local user, and the process was never reaped | low | **Fixed** (`wifi_join.py`: profile created over NetworkManager's D-Bus API, activated with `nmcli connection up id`; `tests/test_wifi_join.py`, including a real NetworkManager round trip. Joining a real access point was **not** exercised) |
| 13 | A PC app killed while the phone panel was powered off left the phone dark until the app was started again | low | **Fixed** (detached watchdog, `screensync/display_watchdog.py`; hardware: panel back 1.4 s after `kill -9`; `tests/test_display_watchdog.py`) |

Regression tests for all of these: `tests/test_audit_fixes.py` (31), `test_udp_video.py` §5, `test_fuzz_quick.py`.

## Not implemented / limits (be aware)

* **SPAKE2 pairing (v4):** the PC and Android app speak it (checked against each other in `test_spake2`, with the real Kotlin
  compiled on the JVM) and **it was run on the real Mi A3 (2026-11-13)**: PIN pairing over the USB tunnel, PIN pairing over Wi-Fi with no tunnel (the phone found the PC by broadcast), a wrong PIN refused on the phone and counted once on the PC, both followed by an authenticated session. The PC's P-256 arithmetic is Python: secret-scalar
  multiplications use OpenSSL for the generator and otherwise a blinded, uniform-operation Montgomery ladder, which mitigates
  timing leaks but is not constant-time at the instruction level (a one-shot, 60 s, 3-attempt exchange; not a general-purpose EC library). The old HMAC proof helpers remain in `crypto.py` but are no longer used by pairing. The USB pairing route needs adb and USB debugging, like the USB link.
* **Release key:** the signing key was generated at `~/.config/phonebridge-release/release.key` (0600) and its public half is
  pinned in the app. **Move the private key somewhere offline** — it is the one thing that can make a malicious update look
  genuine. No update server exists; host `manifest.json`, `manifest.sig` and the artifacts on any https site. Android updates
  also rely on Android's same-signature rule (the debug build here is signed with a debug key).
* **AES-256-GCM, not XChaCha20-Poly1305:** the spec allowed either; XChaCha20 would need a hand-written HChaCha20 because the
  maintained library only offers the 12-byte-nonce variant, and the spec says no custom cryptography.
* **Review:** one fresh-context AI review plus fuzzing and static analysis found and fixed real bugs. That is not the same as
  a human security audit or a penetration test; nobody has done either.
* **Pairing key:** still a shared secret that gates the handshake and derives discovery tags/relay rooms. It can no longer
  forge a device or decrypt past traffic, but a stolen key plus an attacker who can reach the port can make the PC spend CPU
  on handshakes (rate-limited; and only LAN/USB sources are accepted).
* **Phone-side "ask":** shown as a notification; with notifications disabled for PhoneBridge the request times out as *deny*.
  On connect the PC's GUI asks for messages, calls and screen state, so a fresh connection can raise several prompts at once.
* **Local log trust anchor:** the plain local ledger detects edits, deletions and reordering; replacing a whole log with a new
  valid chain is only caught if you enable the optional anchoring (`anchor.json`).
* **Android 8–9** cannot speak TLS 1.3 and are refused unless `PHONEBRIDGE_ALLOW_TLS12=1`. Android lint also lists
  compatibility errors for calls that need newer Android versions (pre-existing, not security).
* **PC control** (shell, remote input, files, screen view) is as powerful as ever while enabled. Safeguards: off by default; it can only be switched on **on the PC** (Device page checkbox, or by approving the prompt a phone's request raises: *Deny / Allow 15 minutes / Allow 1 hour*, default Deny); hard maximum one hour (`PC_CONTROL_MAX_SECONDS`, not configurable beyond that); a red banner in the main window names the phone and the minutes left, with **Revoke now**; revoking or expiry closes a running shell/screen view immediately (revoke) or within ~5 s (expiry sweep); a refused or revoked phone cannot raise another prompt for 2 minutes; requests, approvals, refusals, revocations and expiries are written to the signed audit ledger (`permission.changed` records with `action=pc_control.*`). A stored "on" without a deadline counts as off. Not covered: malware already running as your user can edit the device file; the approval dialog itself (`app.py`) is not covered by an automated test.
* **Memory/malware:** anything running as you can read the unlocked keyring and process memory; the keystore protects
  against file theft and backups, not malware running as you. Python cannot zero key bytes.
* **Screen mirroring needs an unlocked phone** (Android shows its consent dialog behind the lock screen).

## Verified on the real phone (Xiaomi Mi A3, Android 17)

Pairing (v4: identities exchanged), authenticated session over the **USB tunnel** and over Wi-Fi, info, clipboard, a 3 MB push
(sha256 verified) and a pull; **screen mirroring over USB (TCP)**; the phone's permission manager (an "Ask" prompt appeared as
a notification and a Deny reached the PC as `permission.denied`); the Android Security tab (identity fingerprints, signed log
verified, permission buttons); **hidden notification content** (a verification-code and a password notification arrived as
"Content hidden", ordinary ones intact); **per-app blocking** (nothing from a blocked app reaches the PC); **clipboard
auto-clear** (the phone's own log recorded `clipboard.expired` for synced items); **cancelling a transfer** (the partial
download was 0600 in the app-private folder mid-transfer, deleted on cancel, nothing leaked to the shared folder).
**UDP video over Wi-Fi** (new protocol, replay window active): **173 frames in 8 s at 1280x576**, ~1,350 authenticated datagrams
received. (A still screen sends nothing: the encoder is configured for static content.) **Identity rotation, both directions, live:** the PC's key was rotated while the phone was connected (the phone verified the notice and acknowledged, the old key was deleted, and the phone reconnected authenticating the PC with its new key); and the phone's own key was rotated from its Security tab (`44c51f44` → `e23a3fcb`; the PC kept the old key for the grace period, the phone reconnected signing with the new one, the old key was then retired). The ledger verified after both.

**SPAKE2 pairing (v4), live:** pairing with the PIN over the USB tunnel and over Wi-Fi (no tunnel), the 6-digit codes matched on both screens, the new pairing connected and authenticated; a deliberately wrong PIN showed "Pairing failed: wrong PIN" on the phone and was counted once on the PC with nothing paired. Over USB the PC now hands back its real LAN address (found by this test). **USB link, live:** with the phone's Wi-Fi and mobile data switched off, the phone reconnected through `adb reverse` and the PC's audit log recorded `auth.ok` from `127.0.0.1` with a fresh session, while the Wi-Fi sessions before it came from the phone's LAN address.

## Incident note

While adding the keystore, three GUI tests that set `$HOME` to a scratch dir overwrote the real `tls-server-key` keyring
entry; the original key was already migrated out of its file, so the phone had to be re-paired. Fixed: the real config dir is
derived from the account database, TLS-using tests are isolated (`tests/_isolate.py`), and a cert/key consistency check
regenerates the identity instead of serving a broken pair. Later review found and fixed: lost updates when two threads
changed a device record (now atomic writes + locked updates), and a replay that could displace a live session (key confirmation).

## How to re-run everything

`python3 linux/tests/run_all.py` (use a venv with `fusepy` for test_phone_fs; GUI tests run under `xvfb-run`).
Latest audit run (2026-11-19, PC side): 652 checks passed across 39 files; the Kotlin-dependent tests and the FUSE test were skipped in that environment. Use `tools/run_tests.sh` (skips count as failures). Not covered by automation: the live phone (see below).
