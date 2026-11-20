# Release readiness report - 1.4.0

Last updated 2026-11-19 (documentation correction pass). Everything in "Verified" was run on this machine; the rest is explicitly not done.

## Verified (commands actually run)
| Check | Result |
|---|---|
| PC test suite (`tools/run_tests.sh`, strict), 41 test files | 832 checks passed, 0 failed, 0 skipped |
| Kotlin harnesses (audit 43, interop 18, sync 9, MMS 12) | passed (part of the 832) |
| `gradle :app:lintDebug :app:lintRelease` | 0 errors, 38 warnings |
| `gradle :app:assembleDebug :app:assembleRelease :app:bundleRelease` (R8 + resource shrinking) | succeeds; release APK about 2.7 MB |
| `apksigner verify` (release APK) | verifies, v2 scheme, one signer, certificate SHA-256 `10eec57a...da9bf9` = documented identity |
| `aapt2 dump badging` | `dev.phonebridge`, versionCode 14, versionName 1.4.0, minSdk 26, targetSdk 29, no native libraries |
| AAB certificate (`keytool -printcert -jarfile`) | same SHA-256 |
| `sha256sum -c dist/1.4.0/SHA256SUMS` | all four files OK |
| Dependency scan (`tools/audit_dependencies.py`, OSV) | 7 packages, no known vulnerabilities (names/versions were sent to api.osv.dev) |
| Secret / personal-data scan of the working tree and all 12 commits | no keys, tokens, passwords, personal e-mail, home paths or phone serials (pattern scan; gitleaks/trufflehog not installed) |
| Keystore backup scripts | round trip and wrong-passphrase rejection; one real backup made and verified by decrypting a copy pulled back from the device |

## NOT done / not verifiable here
* **The R8-shrunk release APK has never been executed on a device.** Keep rules cover BouncyCastle, zxing and the services, but reflection/optimiser problems only show at run time. Follow `docs/DEVICE_SMOKE_TEST.md` before distributing.
* No install, upgrade-over-existing, runtime-permission, foreground-service, Android 13-16, ARM execution, performance, split-screen or TalkBack testing was done in this release cycle (the main screen is landscape-only by design).
* **bundletool** is not installed as a standalone tool; the AAB was built by AGP and its signer checked, but not validated with `bundletool validate`.
* The PC-approval dialog (`app.py`) and the GitHub Actions workflow have never been run; bandit and shellcheck are not installed.
* **Signing-key backups:** only **one** encrypted off-machine backup exists. A second, on different hardware, is still required (`docs/SIGNING.md`).
* **Android has no unit or instrumented tests.**
* Not changed: targetSdk 29, application ID, permission set (see `docs/PLAY_STORE.md`).

## Fixed during this effort
Debug-signed release -> permanent release key stored outside the repository; no version control -> git with a cleaned local history; phone-to-PC control needs approval on the PC, lasts at most one hour and can be revoked at once; shell environment no longer leaks `PHONEBRIDGE_*`; per-message disk read; failing/skipped tests; main screen locked to landscape on purpose, with a manifest regression test; TalkBack support (not device-tested); R8 + log stripping + cleartext disabled; documentation corrected.

## Remaining / known
* Play Store blockers (target API, SMS/call-log policy, accessibility use) - `docs/PLAY_STORE.md`.
* `BootReceiver` is `exported=true` (BOOT_COMPLETED / MY_PACKAGE_REPLACED); left unchanged because it could not be tested on a device. `NotificationRelay` and `InputService` are exported by necessity and protected by system-only bind permissions.
* Debug-signed and release-signed APKs cannot be installed over each other (uninstall first; pairing is lost).
* Licensing: Apache-2.0 (`LICENSE`, `NOTICE`), copyright 2026 Saiprakash Manepalli. The repository is not published yet: no remote is configured and nothing has been pushed. Security reports are meant to use GitHub private vulnerability reporting, which must be enabled in the repository settings after it is created (`SECURITY.md`).
* Google Play Protect may warn about or block a sideloaded app that combines accessibility, SMS and screen capture. Nothing was done to avoid it, and it was not tested on the target OnePlus device.

## Verdict
**Direct APK / GitHub source release: ready after the device smoke test of the R8 build and your approval to publish. Google Play: not ready.**
