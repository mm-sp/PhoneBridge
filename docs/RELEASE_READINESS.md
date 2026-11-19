# Release readiness report - 1.4.0 (2026-11-19)

## Verified (commands actually run)
| Check | Result |
|---|---|
| PC test suite, strict, 39 files | 792 passed, 0 failed, 0 skipped (earlier run + after PC-control expiry change) |
| Kotlin harnesses (audit 43, interop 18, sync 9, MMS 12) | passed |
| `gradle :app:assembleDebug :app:lintDebug` | succeeds; no accessibility lint findings |
| `gradle :app:assembleRelease :app:bundleRelease` with R8 + resource shrinking | succeeds; APK 4.27 MB -> 2.68 MB |
| `apksigner verify` (release APK) | verifies, v2 scheme, one signer, SHA-256 `10eec57a...da9bf9` = documented identity |
| `aapt2 dump badging` | `dev.phonebridge`, versionCode 14, versionName 1.4.0, targetSdk 29, no native libraries |
| AAB certificate (`keytool -printcert -jarfile`) | same SHA-256 |
| Dependency scan (`tools/audit_dependencies.py`, OSV) | 7 packages, no known vulnerabilities (names/versions were sent to api.osv.dev) |
| Git history secret scan | no keystore/password/key material ever committed |
| Keystore backup scripts | round trip + wrong-passphrase test on a *throwaway* key only |

## NOT done / not verifiable here
* **No device was attached.** Not run: install, upgrade-over-existing, runtime permissions, foreground-service behaviour, Android 13/14/15/16 testing, ARM32/ARM64 execution (the APK has no native code, so it is ABI-neutral), performance/memory testing, TalkBack and portrait checks.
* **The R8-shrunk release APK has never been executed.** Keep rules cover BouncyCastle, zxing and the services, but reflection/optimiser issues only show at runtime. Smoke-test before distributing (`docs/RELEASE.md` step 6).
* **bundletool** is not installed as a standalone tool; the AAB was built by AGP and its signer checked, but not validated with `bundletool validate`.
* **No encrypted keystore backups were made** (they need your passphrase and two physical devices). The key exists only at `android/keystore/` on this machine.
* **Android has no unit/instrumented tests.** GitHub Actions workflow is written but has never run (no remote).
* Not changed: targetSdk 29, applicationId, permissions (see `docs/PLAY_STORE.md`).

## Fixed during this effort
Debug-signed release -> real release key; no VCS -> git; PC-control now expires and closes live shells; shell env no longer leaks `PHONEBRIDGE_*`; per-message disk read; failing/skipped tests; portrait + TalkBack; R8 + log stripping + cleartext disabled; docs corrected.

## Remaining / known
* Play blockers (target API, SMS/call-log policy, accessibility use) - `docs/PLAY_STORE.md`.
* Manifest: `BootReceiver` is `exported=true` (listens for BOOT_COMPLETED / MY_PACKAGE_REPLACED) - left unchanged because the change could not be tested on a device; `NotificationRelay` and `InputService` are exported by necessity and protected by system-only bind permissions.
* Debug-signed APKs and the release APK use different keys: installing one over the other needs an uninstall (loses pairing). The release key has never been installed on your phone yet.
* No LICENSE chosen; no remote configured; security contact and privacy contact placeholders need filling.
* Firmware/OS reality: Play Protect warnings are possible for an accessibility + SMS + screen-capture sideloaded app. Nothing was done to avoid or test them and none were observed (no device).

## Verdict
**Direct APK / GitHub: ready after a device smoke test of the R8 build and your approval of the steps below. Google Play: not ready.**
