# Real-device smoke test for the R8 release APK

**Status: NOT PERFORMED.** Nothing below has been run on a device. Record results in the table at the end; do not mark an item passed unless you saw it work.

The release build is shrunk and obfuscated by R8 (`android/app/proguard-rules.pro`). R8 problems appear only at runtime, so every feature that uses reflection, BouncyCastle, zxing, or system services must be exercised once.

## Rules that protect your data
* **Do not uninstall your current PhoneBridge and do not clear its data.** Android refuses to install a differently signed APK over an existing one (your current app is debug-signed; the release APK is signed with the permanent release key), so test the release APK on a **second phone**, or on a phone that has no PhoneBridge pairing you care about. A fresh install of the release APK has no signature conflict.
* If you only have one phone: skip this test rather than uninstalling. Alternatively build a variant with a different application ID for side-by-side testing (needs a code change; ask first).
* Install with `adb install` (or the file manager). Do not use `adb install -r` over a debug-signed install; it will fail with a signature mismatch, which is Android protecting you.

## Before
- [ ] `sha256sum -c dist/<version>/SHA256SUMS` passes; `apksigner verify --print-certs` shows `10:EE:C5:7A:...:DA:9B:F9` (`10eec57a...da9bf9`).
- [ ] PC app running from this commit (`phonebridge`), same Wi-Fi, firewall allows TCP 28100 / UDP 28101.

## Checks (tick only what you observed)
| # | Check | Expected | Result |
|---|---|---|---|
| 1 | Install, open the app | no crash on launch; tabs visible; portrait and landscape both work | |
| 2 | Grant runtime permissions | each prompt appears and the app continues | |
| 3 | Pair by QR | QR scans (zxing under R8), confirmation codes match, paired | |
| 4 | Pair by PIN (second pairing) | SPAKE2 completes (BouncyCastle under R8); wrong PIN refused | |
| 5 | Reconnect after closing/reopening | connects with no re-pair | |
| 6 | Device tab | battery, Wi-Fi, storage shown | |
| 7 | File PC -> phone and phone -> PC | transfer completes, sha256 OK, files appear | |
| 8 | Notifications (grant access) | mirror to PC; reply/dismiss works | |
| 9 | SMS read; send one from the PC (approve the phone prompt) | thread list and send work | |
| 10 | Clipboard both directions | text arrives; phone->PC while app focused | |
| 11 | Screen mirroring (accept the consent dialog) | video shows; PC clicks/typing control the phone (accessibility service enabled) | |
| 12 | Security tab | identity fingerprint, log verification, permission buttons work | |
| 13 | PC-control approval | request from the phone raises a prompt on the PC; Deny keeps it off; Allow works; banner shows; Revoke ends it | |
| 14 | Hotspot / Wi-Fi name (location permission) | no crash | |
| 15 | Rotate screen during each tab | no crash, panel not restarted | |
| 16 | TalkBack on | tabs announce "selected"; keyboard keys speakable and double-tap works | |
| 17 | Kill and relaunch the app while connected | service restarts, reconnects | |
| 18 | `adb logcat` during the above | no `AndroidRuntime` FATAL, no `ClassNotFoundException`/`NoSuchMethodException` | |

## If something crashes
Capture `adb logcat -d | grep -E "AndroidRuntime|FATAL|ClassNotFound|NoSuchMethod"`. With R8 obfuscation, de-obfuscate with `dist/<version>/mapping-<version>.txt` (keep it private). The usual fix is a `-keep` rule in `android/app/proguard-rules.pro`.

## Result record
Date: ____  Device/Android: ____  APK sha256: ____  Passed: __/18  Notes: ____
