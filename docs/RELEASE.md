# Release procedure

1. Update `versionName` and **increment `versionCode`** in `android/app/build.gradle.kts`; set the same version in `linux/pyproject.toml`.
2. Add a section to `RELEASE_NOTES.md`.
3. `tools/run_tests.sh` (all PC tests, strict) and `cd android && gradle :app:lintDebug`.
4. `tools/release.sh` -> `dist/<version>/`: signed APK, AAB, debug APK, `mapping-<v>.txt`, `SHA256SUMS`, `SIGNING_REPORT.txt`.
5. Confirm in `SIGNING_REPORT.txt`: package `dev.phonebridge`, new versionCode, certificate SHA-256 equals the value in `docs/SIGNING.md`.
6. **Smoke-test the minified release APK on a phone** (pairing, a file transfer, mirroring, SMS, notification, Security tab). R8 is on, and nothing automated exercises the shrunk build.
7. Upgrade test: install the previous release, then `adb install -r` the new one; pairing must survive.
8. Tag `v<version>` and publish `dist/<version>/*` (APK, SHA256SUMS) to the GitHub release; keep `mapping-<v>.txt` privately for de-obfuscating crash traces.
9. Google Play: only after the blockers in `docs/PLAY_STORE.md` are resolved; upload the `.aab` as a draft/internal-test release first.

Never publish, push or submit without the maintainer's explicit approval.
