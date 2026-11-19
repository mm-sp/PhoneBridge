# PhoneBridge - Project Report

Version 1.4.0 (Android `versionCode` 14). Report date 2026-11-19. Statuses come from reading the source and from commands run during this audit; where something was not run it says **NOT TESTED**. Details of the earlier full audit are in `docs/AUDIT_REPORT.md`.

## 1. Executive summary
PhoneBridge links one Android phone to one Linux PC without any account or cloud: notifications, SMS/MMS, calls, photos, files, clipboard, screen mirroring with control, phone-as-webcam, and the reverse direction (phone controls the PC). Roughly 12.8k lines of Python (PC) and 7.9k lines of Kotlin (Android). The PC side has 792 automated checks that all pass in this audit. The Android side has no in-framework tests; it is covered by five JVM harnesses, lint and one hardware device (Xiaomi Mi A3). The project is suitable for direct-APK/GitHub distribution after a device smoke test of the R8 build; it is **not** ready for Google Play (section 33).

## 2. Introduction
A "Phone Link for Linux": all trust is local (pairing records on each device), all traffic is end-to-end encrypted and goes directly between the two devices (LAN, Bluetooth for pairing, USB tunnel) or through an optional relay you host.

## 3. Problem statement
Phone Link and similar tools need Microsoft/Google accounts or cloud services and mostly target Windows. Linux desktop users lack an account-free, auditable alternative that includes mirroring, SMS and file transfer.

## 4. Objectives
Account-free pairing; strong local security (PAKE pairing, forward secrecy, signed audit ledger, per-feature permissions); feature parity with Phone Link where Android allows; sideload-friendly distribution.

## 5. Target users
A technically capable individual with an Android phone (Android 8+) and a GTK/Wayland or X11 Linux desktop who accepts sideloading. Not aimed at non-technical users or managed devices.

## 6-7. Features and implementation status
| Feature | Status | Where | Notes |
|---|---|---|---|
| QR/PIN pairing (SPAKE2) | Implemented | `pairing.py`, `spake2.py`; `Pairing.kt`, `Spake2.kt` | 51 tests; in-house P-256 SPAKE2, no independent crypto audit |
| Encrypted session (TLS 1.3 + AES-GCM, rekey, rotation) | Implemented | `secure_session.py`, `transport.py`; `Connection.kt`, `SecureSession.kt` | 66 + 18 interop tests |
| Discovery (mDNS, tagged UDP, BLE wake) | Implemented | `mdns.py`, `netutil.py`, `ble_wake.py` | BLE not hardware-checked in this audit |
| USB link (`adb reverse`) | Implemented | `usb_link.py` | needs adb + USB debugging |
| Self-hosted relay | Partially | `relay_server.py`, `relay_client.py` | loopback-tested only; no WAN run |
| Notifications | Implemented | `ui/notifications_page.py`; `NotificationRelay.kt` | privacy filters |
| SMS read/send | Implemented | `ui/messages_page.py`; `Sms.kt` | not the default SMS app |
| MMS | Partially | `Mms.kt`, `MmsPdu.kt` | never sent via a real carrier |
| Calls (history, dial, answer, mute) | Partially | `ui/calls_page.py`; `Calls.kt` | decline is best-effort on Android |
| Call audio on PC (Bluetooth HFP) | Experimental | `call_audio.py` | no real-call test |
| Photos, file transfer (both ways, resume, sha256) | Implemented | `file_transfer.py`; `Media.kt` | |
| FUSE mount of phone files | Partially | `phone_fs.py` | passes in strict run (fusepy installed in the test venv) |
| Clipboard sync (text + image) | Implemented | `clipboard_watcher.py`; `Clipboard.kt` | phone->PC only while the app is focused (Android 10+) |
| Screen mirroring + control | Implemented | `ui/mirror_page.py`, `udp_video.py`; `Video.kt`, `InputService.kt` | needs unlocked phone + consent tap each time |
| Per-app streaming | Experimental | `ui/apps_page.py`; `Video.kt` | many apps blank/refuse |
| Screen sync / smart display-off | Partially | `screensync/*`; `SyncCore.kt`, `ScreenSync.kt`, `DisplayPower.kt` | rotation sync not implemented; some paths not hardware-verified |
| Webcam (v4l2loopback) | Partially | `ui/webcam_page.py`; `Video.kt` | needs kernel module; no automated test of the sink |
| Phone->PC control (shell, files, input, screen, Docker, apps) | Implemented, time-limited | `pc_control.py`, `terminal.py`, `pc_remote.py`; `PcPanels.kt` | default off, auto-expires |
| Security dashboards, signed ledger, permission manager | Implemented | `security/*`, `ui/security_page.py`; `SecurityPanel.kt`, `PcAccess.kt` | |
| Signed-update verification | Partially | `updates.py`, `tools/sign_release.py` | verify only, no update server |
| Portrait + TalkBack on Android | Implemented in code | `MainActivity.kt`, `InAppKeyboard.kt` | **NOT TESTED on a device** |
| iPhone support | Not implemented | residual `platform: ios` handling | app removed |
| Localisation | Not implemented | | English only |

## 8-11. Architecture, communication, data flow
See `docs/ARCHITECTURE.md` for diagrams (system, pairing, mirroring, file transfer). Summary: asyncio TLS server on the PC (`ControlChannelServer`), Android foreground `BridgeService` as client; JSON envelopes `{ch,type,seq,payload}` inside AES-GCM frames; separate connections for files and video; optional UDP for video.

## 12-16. Workflows
Pairing, mirroring and file transfer: `docs/ARCHITECTURE.md`. Remote control: `pc.*` messages gated by permission, expiry and per-feature checks in `pc_control.py`; the phone separately gates what the PC may request (`PcAccess.kt`, ask-every-time for sending SMS and placing calls).

## 17-18. Technology and dependencies
PC: Python >= 3.10, PyGObject >= 3.46 (GTK4/libadwaita), `cryptography` >= 46, `qrcode` >= 7.4, `pillow` >= 13, optional `fusepy`; system GStreamer, adb, v4l2loopback, NetworkManager, BlueZ, Avahi. Android: Kotlin 2.0.21, AGP 8.11.1, Gradle 8.14.7, compileSdk 35, minSdk 26, targetSdk 29, BouncyCastle 1.87, zxing-android-embedded 4.3.0, androidx.core 1.13.1. OSV scan (run in this audit): 7 packages, no known vulnerabilities (the PC packages were read from the installed app venv, not the test venv).

## 19. Directory structure
`linux/phonebridge` (app), `linux/tests` (39 test files + Kotlin harnesses), `linux/tools` (hardware harness), `android/app/src/main/java/dev/phonebridge` (32 Kotlin files), `protocol/` (spec + schema), `docs/`, `packaging/`, `tools/` (release, backup, audit scripts), `.github/workflows/ci.yml`.

## 20-22. Build, install, configure
`docs/BUILD_GUIDE.md`, `docs/INSTALLATION.md`. Configuration is through the app UI; environment variables: `PHONEBRIDGE_ALLOW_PUBLIC`, `PHONEBRIDGE_ALLOW_TLS12`, `PHONEBRIDGE_PASSPHRASE` (fallback key file), `PHONEBRIDGE_PC_CONTROL_MINUTES`, `PHONEBRIDGE_ORIGIN`.

## 23. Permissions and security
Android requests SMS, call log, contacts, phone, storage (legacy), camera, Bluetooth/BLE, location (SSID only), overlay, battery-optimisation exemption, boot, plus Accessibility and Notification-listener services. Security model, tests and limits: `docs/SECURITY.md`. Key properties: TLS 1.3 only, pinned cert, Ed25519-signed ephemeral handshake, AES-256-GCM with replay window, peer-network policy, auth throttling, signed hash-chained audit ledger, PC-control time limit.

## 24. Signing and release architecture
Application ID `dev.phonebridge`. Release certificate SHA-256 `10:EE:C5:7A:52:F5:C2:01:CF:C6:71:49:6B:78:7D:48:49:9C:D8:DB:14:A2:12:2D:D6:32:2B:4E:62:DA:9B:F9` (RSA 4096). Keystore lives outside the repo (`docs/SIGNING.md`); one encrypted backup exists on a phone, a second is still required. Release builds use R8 shrinking + obfuscation; Play-Store signing differs from direct-APK signing (`docs/SIGNING.md`).

## 25. Testing - actual results (this audit, 2026-11-19)
| Check | Result |
|---|---|
| PC test suite (`tools/run_tests.sh`, strict, scratch HOME) | **PASS** - 792 checks, 0 failed, 0 skipped (includes Kotlin harnesses audit 43, interop 18, sync 9, MMS 12) |
| Android lint (debug + release) | **PASS with warnings** - 0 errors, 39 warnings (22 `SetTextI18n`, 4 `CommitPrefEdits` = false positives, 2 `StaticFieldLeak`, 1 `CustomX509TrustManager` = intentional pinning, others minor) |
| Debug + release APK, AAB build | **PASS** |
| `apksigner` on release APK | **PASS**, certificate SHA-256 as above, v2 scheme |
| AAB certificate | **PASS**, same fingerprint |
| OSV dependency scan | **PASS**, no known vulnerabilities (7 packages) |
| Secret/history scan | **PASS** (own regex scan; gitleaks/trufflehog not installed) |
| Real-device smoke test of the R8 release APK | **NOT TESTED** |
| Install/upgrade, Android 13-16, ARM execution, performance, TalkBack | **NOT TESTED** |
| GitHub Actions workflow | **NOT TESTED** (never run; no remote) |
| bandit / shellcheck / ruff | **NOT TESTED** (not installed) |
No Android unit or instrumented tests exist; no coverage tool is configured, so coverage is unknown.

## 26. Performance
Measured figures in `docs/SECURITY.md` ("Measured cost") are from the author's earlier loopback benchmark (`tools/bench.py`) and were **not re-run** here. Known considerations: software H.264 decode on the PC, Python-side SPAKE2, JPEG screen streaming for the phone's "view the PC" feature. No memory or startup profiling was done.

## 27. Compatibility
Android: minSdk 26 (8.0); Android 8-9 cannot speak TLS 1.3 and are refused unless `PHONEBRIDGE_ALLOW_TLS12=1`. The APK contains no native libraries (ABI-independent). Verified on one device (Mi A3, Android 17). Linux: GTK 4 + libadwaita desktops; tested only on Ubuntu 24.04.4 LTS (kernel 6.8). Other distributions: **NOT TESTED**.

## 28-29. Known issues and limitations
See `docs/AUDIT_REPORT.md` section 15 and its remediation appendix. Open items: Play Store blockers (`docs/PLAY_STORE.md`), targetSdk 29, `BootReceiver` exported, `StaticFieldLeak` overlay in `Clipboard.kt:15,104` (low), 22 hard-coded UI strings, no Android automated tests, screen-rotation sync missing, MMS/relay/Wi-Fi-join unproven in the real world, no independent security review, single signing-key backup.

## 30. Roadmap
Smoke-test and ship the R8 build; second keystore backup; Android unit tests and CI; targetSdk 36 migration with scoped storage (prerequisite for Play); reduced-permission Play flavor (no SMS/call log); rotation sync; packaging (.deb/Flatpak); independent crypto review.

## 31-32. Repository and release process
Layout in section 19. Release procedure: `docs/RELEASE.md`; checklist before going public: `docs/GITHUB_PUBLICATION_CHECKLIST.md`.

## 33. Google Play readiness
**Not ready.** Blockers: target API 29 vs Play's API 36 requirement; SMS/Call-Log permissions restricted to default handlers; accessibility-service remote control needs declaration and justification. Details and permission worksheet: `docs/PLAY_STORE.md`.

## 34. Troubleshooting
`docs/TROUBLESHOOTING.md`.

## 35. Contributing
`CONTRIBUTING.md`. No license is chosen yet, so contributions cannot currently be accepted under defined terms.

## 36. Conclusion
A feature-rich, security-minded project with a strong automated test base on the PC side and honest documentation of its limits. Remaining gaps are mostly release engineering and on-device validation rather than missing core functionality.
