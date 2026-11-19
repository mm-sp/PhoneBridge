# Building

## Android
Requirements: JDK 17, Android SDK (platform 35, build-tools 34+), Gradle 8.14.x (no wrapper is checked in yet), `android/local.properties` with `sdk.dir=`.
```sh
cd android
gradle :app:assembleDebug        # debug APK, debug key
gradle :app:lintDebug
gradle :app:assembleRelease      # needs ~/.config/phonebridge-android-release/keystore.properties or PHONEBRIDGE_* env vars; R8 + resource shrinking on
gradle :app:bundleRelease        # .aab
```
Or everything with checks and checksums: `tools/release.sh` (output in `dist/<version>/`).

## Linux app
```sh
tools/run_tests.sh               # venv + requirements + all tests (skips count as failures)
./packaging/install.sh           # user-local install
```
System packages: GTK 4, libadwaita, python3-gobject, GStreamer, (optional) v4l2loopback, adb, xvfb for GUI tests.
Python dependencies are declared in `linux/pyproject.toml` (extras: `fuse`, `test`).
