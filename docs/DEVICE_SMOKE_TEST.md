# Real-device smoke test for the R8 release APK

**Status: PARTIALLY PERFORMED.** One session on one real phone (Xiaomi Mi A3, Android 17, LineageOS) on 2026-11-19, using the R8-shrunk **release** APK, over a **USB tunnel (not Wi-Fi)**. It covered pairing, the encrypted session, screen mirroring, screen sync, device information, PC-home browsing, one file transfer and the PC-control approval workflow. It did **not** cover the whole app: see "Not tested". This is not full Android feature coverage, not a release sign-off, and says nothing about Google Play readiness (see `docs/PLAY_STORE.md`, which lists open blockers).

Legend used below: **PASS** = observed working in this session. **PARTIAL** = part of the check was observed. **NOT TESTED** = not run. **OBSERVATION** = something noticed that is not fixed.

The release build is shrunk and obfuscated by R8 (`android/app/proguard-rules.pro`). R8 problems appear only at runtime, so every feature that uses reflection, BouncyCastle, zxing, or system services should be exercised at least once; the results below show which ones were.

## Test record
| | |
|---|---|
| Date | 2026-11-19 |
| Device | Xiaomi Mi A3 (arm64), Android 17 (API 37), LineageOS; PhoneBridge was not installed beforehand, so the first install was fresh (nothing pre-existing was uninstalled or overwritten). The test install itself was then **updated in place twice with `adb install -r`** (same signing key, app data kept): see "APK builds used during the session" |
| APK tested | R8 release build, `versionName 1.4.0` / `versionCode 14`, signed with the permanent release key (certificate SHA-256 `10eec57a...da9bf9`), file SHA-256 `db1d3a12945b749f00485db8c48a4b7e972bb4cd7860bf3f6ed0815ab50457e6` |
| Build identity | The final build (the one used for every result below except the first launch and an initial partial tab walk) was built from commit `18d75c9` **plus one then-uncommitted change**: `android/app/src/main/AndroidManifest.xml`, MainActivity `screenOrientation="sensorLandscape"` (the committed value at that time was `fullSensor`). That change is now the intended official setting (see "Orientation"). It is **not** the APK in `dist/1.4.0/` (SHA-256 `6df5376a...`, built earlier with `fullSensor`; the `dist` files are local and were never published) |
| PC side | PhoneBridge PC app run from the source checkout at the same commit, in a private virtual display, with the real keyring/config directory |
| Connection | USB tunnel (`adb reverse`, loopback address, encrypted). The Wi-Fi LAN path was **not** tested |
| Method | Driven through `adb` (taps, screenshots, log capture), not by hand: Android's own prompts (screen-share consent, permission dialogs, the phone-side approval notification) were answered by automated taps. Some runtime permissions were granted with `adb` (camera, notifications, storage, "display over other apps"); others were left ungranted (including location and SMS). So the permission flow is not representative of a normal first-run experience |

## APK builds used during the session
| Order | Build | SHA-256 | Installed how | Used for |
|---|---|---|---|---|
| 1 | `dist/1.4.0/phonebridge-1.4.0.apk` (`fullSensor`) | `6df5376a7cd621f9ec9759ec02aa0ba2cf6eb9a5d213596f50f9d51d763f0055` | `adb install` (fresh) | first launch (portrait layout) and an initial partial walk of the tabs. That walk's crash detector gave false alarms and was redone on build 3; its tab results are not used |
| 2 | temporary build with `screenOrientation="portrait"` | begins `d38e4a0061c0202d` (not retained) | `adb install -r` (in place) | only an orientation-lock check requested at that moment |
| 3 | **final build**, `sensorLandscape` | `db1d3a12945b749f00485db8c48a4b7e972bb4cd7860bf3f6ed0815ab50457e6` | `adb install -r` (in place) | everything else: the full tab walk, QR scanner, PIN dialog, pairing, mirroring, screen sync, file transfer, PC-control approval, Security tab |

The final build's file was verified byte-identical to the copy pulled back from the phone. A copy of it is kept outside the repository by the maintainer.

### Rebuild from the final source (NOT device-tested)
After the orientation setting was made official, the release APK was rebuilt from the final source. Its manifest differs from commit `18d75c9` in exactly one line (the MainActivity orientation) and contains no added comment. **This rebuilt APK has not been installed or tested on a device**; the statement "tested on the phone" applies only to the file `db1d3a12...57e6`.

| | |
|---|---|
| SHA-256 / size | `c17cb047f534025b4b0c195144d361cb81aa6026f18e5a4a070628db59a1f006`, 2,676,345 bytes (the same size as the tested APK) |
| Version / package | `1.4.0` / versionCode 14, minSdk 26, targetSdk 29, `dev.phonebridge`; signed with the permanent release key (certificate SHA-256 `10eec57a...da9bf9`), v2 signature verifies; not debuggable |
| Manifest orientation | MainActivity `sensorLandscape` (value 6), QR scanner `fullSensor` (value 10) with its `tools:replace` |
| ZIP entries vs the tested APK | **All 57 entries are identical**: same names and order, and identical content, sizes, CRCs, timestamps and compression. This includes the compiled `AndroidManifest.xml`, identical even in its source-line markers |
| v2 signature vs the tested APK | identical |
| Whole-file SHA-256 vs the tested APK | **differs.** 1,727 bytes differ, all inside the APK Signing Block, none in the ZIP entries, central directory or end record. Of the Signing Block's three entries, the v2 signature and the padding are identical; only the entry with ID `0x504b4453` (believed to be AGP's dependency-information block) differs. That entry changes on every build: two scratch builds of identical source produced different file hashes that differed only in it |

So the rebuilt APK's code, resources, manifest and signature equal those of the tested APK; only that one per-build signing-block entry differs. Making the file hash itself reproducible would need a Gradle build-configuration change, which was deliberately not made.

Maintenance note: an earlier variant of the final source carried an explanatory comment in `AndroidManifest.xml`. It produced identical code and resources but shifted the source-line metadata of the compiled manifest (every line marker +5, even when the comment was moved to the end of the file), so the comment was removed; the orientation decision is documented here and guarded by `linux/tests/test_android_manifest.py` instead.

## Results (the 18 checks)
| # | Check | Result | Notes |
|---|---|---|---|
| 1 | Install, open the app | **PASS** | no crash on launch; all nine tabs opened on the final (landscape) build. The first launch and an initial partial walk on the earlier `dist` build also showed no crash |
| 2 | Grant runtime permissions | PARTIAL | prompts appeared and the app continued, but most were answered by automation, not a normal user flow (see Method) |
| 3 | Pair by QR | PARTIAL | the QR scanner (zxing under R8) opened with a live camera preview; **no QR code was scanned** and no pairing was done this way |
| 4 | Pair by PIN | PARTIAL | **PASS** for a correct PIN: SPAKE2 (BouncyCastle) completed in the minified APK, confirmation codes matched on both devices, pairing succeeded. A wrong PIN was **not** tested |
| 5 | Reconnect after closing/reopening | NOT TESTED | |
| 6 | Device tab | PARTIAL | the PC's Device page showed the phone's battery, storage, memory and Wi-Fi (network name hidden because location was denied). The phone's own Device tab (PC info) was seen only while PC control was refused (see OBSERVATION 1) |
| 7 | File transfer | PARTIAL | PC to phone: PASS (200,000 bytes, identical SHA-256 on both sides). Phone to PC: NOT TESTED |
| 8 | Notifications | NOT TESTED | |
| 9 | SMS | NOT TESTED | |
| 10 | Clipboard | NOT TESTED | |
| 11 | Screen mirroring | PARTIAL | the live phone screen appeared on the PC and the PC's mirror counter reached 160 frames, after accepting the consent dialog: PASS (decoding was not independently verified beyond seeing the picture). Mouse/keyboard control from the PC: NOT TESTED (the Accessibility service was not enabled) |
| 12 | Security tab | PARTIAL | identity fingerprints and "audit log verified" (12 signed, hash-chained records) shown: PASS. The permission and emergency buttons were not exercised |
| 13 | PC-control approval | **PASS** | request raises a prompt on the PC; Deny keeps it off; Allow 15 minutes works; banner shows with "Revoke now"; revoke takes effect immediately (details below) |
| 14 | Hotspot / Wi-Fi name | NOT TESTED | location was denied; the PC displayed the expected "allow Location" hint |
| 15 | Rotate screen during each tab | PARTIAL | on the final build the screen stayed 1560x720 (landscape) on the Home screen with the phone's rotation forced to each of the four directions; not done per tab. Split-screen was not tried |
| 16 | TalkBack | NOT TESTED | |
| 17 | Kill and relaunch while connected | NOT TESTED | |
| 18 | `adb logcat` during the session | **PASS** | no fatal exception, no crash attributed to the app, no ANR, no `ClassNotFoundException`/`NoSuchMethodException`/`NoClassDefFoundError` in the app's process |

Tally: 3 PASS, 8 PARTIAL, 7 NOT TESTED (of 18).

## What was observed working (PASS)
1. SPAKE2 PIN pairing with BouncyCastle in the minified release APK.
2. Matching confirmation codes on phone and PC; pairing completed.
3. TLS 1.3 with the pinned PC certificate.
4. AES-256-GCM encrypted session with per-direction keys and forward secrecy (as displayed by the phone's Security tab).
5. Identity exchange; both identity fingerprints displayed and pinned.
6. Signed audit log on the phone: 12 verified, hash-chained records.
7. Phone-side approval of remote control (notification with Allow/Deny).
8. Live screen mirroring: the live phone screen was shown on the PC and the PC mirror counter reached 160 frames (decoding not independently verified beyond that).
9. Screen sync: the phone's display was powered off while mirroring and restored when mirroring stopped; no pending-restore marker was left behind.
10. Device information on the PC: battery, storage, memory, Wi-Fi.
11. Browsing the PC's home folder from the phone after approval.
12. File transfer PC to phone: 200,000 bytes, identical SHA-256.
13. Zero application crashes during the session.
14. PC-control approval workflow: a phone request raised the prompt (Deny / Allow 15 minutes / Allow 1 hour).
15. Denial: the phone reported "not approved"; an immediate re-request raised no new prompt (two-minute cooldown); the prompt returned after the cooldown.
16. Fifteen-minute approval: banner on the PC ("can control this PC, switches off in 15 min") with "Revoke now"; the phone reported the approval.
17. Immediate revocation: banner gone, control off, new requests refused without a prompt.
18. Signed audit ledger on the PC recorded pairing/authentication/session start, the request, the refusal, the second request, the 15-minute approval, the file transfer and the revocation.

## Not tested
* **PC-to-phone control** (mouse/keyboard on the phone): Android's Accessibility service was not enabled; it must be enabled by hand in the phone's Accessibility settings.
* SMS, calls, contacts, **notification relay** and **clipboard**.
* **Terminal tab** live operation (only its empty, unconnected state was seen).
* Per-app streaming, phone-as-webcam, the relay.
* **Wi-Fi LAN connection** (the whole session used the USB tunnel).
* Also not run: QR pairing to completion, wrong-PIN refusal, reconnect after relaunch, phone-to-PC file transfer, TalkBack, Android versions other than 17, other devices.

## Observations (open; NOT fixed)
1. **Device tab** on the phone stays on "Loading PC info..." when PC control is refused (the Files tab does explain the refusal).
2. **Files tab** keeps showing an old file list after a refusal or revocation; the explanatory line can scroll out of view.
3. **Mirroring diagnostics** do not clearly identify missing permissions: mirroring stayed at "starting, 0 frames" until notifications, "display over other apps" and the remote-control approval were in place, and the PC reports only "no screen frames arrived". Which of these was strictly required was not isolated.
4. **Possible race** (suspected, not confirmed as a defect): accepting a still-pending screen-capture prompt after the PC has cancelled the session may start capture and open a video connection that nothing uses until the next session.
5. **ISSUE-14:** connecting starts mirroring automatically, even with `PHONEBRIDGE_ORIGIN=auto`, which raises a capture prompt on the phone.
6. **Approved phone access can expose files in the PC's home directory** (non-hidden files can be listed and downloaded while control is approved). This is by design, but users should not leave sensitive files in their home folder while approving control.

Minor extras noticed in the same session: Android logged two `requestLayout() improperly called ... during layout` warnings on the Terminal tab (no visible effect); on the phone's Security tab the "DISCONNECT NOW" and "Rotate identity key" buttons are slightly misaligned; the phone's `user_rotation` setting changed during the session although PhoneBridge contains no code that writes it (cause not established; restored to its original value afterwards).

## Orientation (official behaviour)
* The main screen (`MainActivity`) is **landscape-only, by design**: `sensorLandscape`, i.e. both landscape directions, chosen by the sensor. Android documents that this holds even when the user's auto-rotate is off; only that case was exercised on the Mi A3 (auto-rotate off, rotation forced through the phone's settings), and turning the phone by hand between the two landscape directions was not tried. This is the application's original design.
* The QR scanner keeps `fullSensor` so a portrait phone can scan.
* A portrait layout (tab strip along the bottom, portrait-aware terminal sizing) remains in the code but is **dormant and untested**; it should be reachable only in unusual window shapes (for example split-screen or freeform windows; an assumption), and **none of that is verified**.
* Screen mirroring starts with the capture size of the display's current rotation, so it starts landscape-shaped when PhoneBridge is in front (rotation reported to the PC is fixed at 0).
* `linux/tests/test_android_manifest.py` fails if either orientation value changes.

## What this does and does not show
It shows that the minified release build starts, pairs, encrypts, mirrors, transfers a file and enforces the PC-control approval workflow on this phone, with no crash and no R8 class-loading error in the exercised paths. It does **not** show that every feature works, that other devices or Android versions behave the same, that Wi-Fi works, or that the project is ready for Google Play or for general distribution.

## Rules that protect your data (for future runs)
* **Do not uninstall an existing PhoneBridge and do not clear its data.** Android refuses to install a differently signed APK over an existing one (a debug-signed install versus the release key), so test the release APK on a **second phone**, or on a phone with no PhoneBridge pairing you care about. A fresh install of the release APK has no signature conflict.
* If you only have one phone with an existing install: skip the test rather than uninstalling, or build a variant with a different application ID (needs a code change; ask first).
* Use `adb install` for the first install. `adb install -r` over a debug-signed install fails with a signature mismatch, which is Android protecting you; over a release-signed install it updates in place and keeps data.
* Do not enable the Accessibility service programmatically; Android requires the user to do it in Settings.

## Before a run
- [ ] `sha256sum -c dist/<version>/SHA256SUMS` passes; `apksigner verify --print-certs` shows `10:EE:C5:7A:...:DA:9B:F9` (`10eec57a...da9bf9`). If the APK under test is not the `dist` one, record its own SHA-256 and the commit plus any uncommitted changes.
- [ ] PC app running from the commit under test; for Wi-Fi testing, same network and firewall allowing TCP 28100 / UDP 28101.

## If something crashes
Capture `adb logcat -d | grep -E "FATAL EXCEPTION|Process: dev.phonebridge|ClassNotFound|NoSuchMethod"`. With R8 obfuscation, de-obfuscate with `dist/<version>/mapping-<version>.txt` (keep it private). The usual fix is a `-keep` rule in `android/app/proguard-rules.pro`.

## Result record
Date: 2026-11-19  Device/Android: Xiaomi Mi A3 / Android 17  Connection: USB tunnel  APK sha256: `db1d3a12945b749f00485db8c48a4b7e972bb4cd7860bf3f6ed0815ab50457e6`  Passed: 3/18 fully, 8 partially, 7 not tested  Notes: no crash in the exercised paths; open observations listed above; Wi-Fi, PC-to-phone control, SMS/calls/notifications/clipboard, Terminal, webcam, relay and TalkBack untested.
