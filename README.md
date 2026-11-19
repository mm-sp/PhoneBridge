# PhoneBridge

A login-free, account-free phone↔Linux-PC bridge in the spirit of Microsoft's
Phone Link — notifications, SMS, calls, photos, clipboard, file transfer,
screen mirroring, and phone-as-webcam, all over a direct Bluetooth/Wi-Fi
connection between your own two devices. No Microsoft/Google account is ever
involved; pairing trust lives only in local files on each device.

```
linux/      GTK4 + libadwaita desktop app (Python)
android/    Android companion app (Kotlin)
protocol/   Shared wire protocol spec + JSON schema — read this first
```

See `protocol/PROTOCOL.md` for the pairing handshake and message catalogue.

## Status

**v1.4.0 (current):** PC package and Android app both report 1.4.0. Adds SPAKE2 pairing (v4), device identities, forward
secrecy, the security engine/dashboards, bidirectional screen sync with PC-driven display-off, and signed-update
verification (see `docs/SECURITY.md`, `docs/SCREEN_SYNC.md`, `RELEASE_NOTES.md`).

**What is verified:** the PC side by `tools/run_tests.sh` (about 650 automated checks, run under `xvfb-run`); the
phone side only on one device (Xiaomi Mi A3, Android 17) - see "Verified on the real phone" in `docs/SECURITY.md`.
Not verified anywhere: sending MMS through a real carrier, joining a real access point, the relay across the internet,
screen-rotation sync (not implemented). The older "not yet visually run / unverified" notes in the phase history
below are historical.

**v1.3:** an optional self-hosted **relay** for use away from home.

**v1.2:** on top of v1.1 — **UDP video** (encrypted datagrams, loss-driven bitrate and keyframe requests, TCP fallback —
`protocol/PROTOCOL.md` §1f; not WebRTC), **sending MMS** from the PC (§4e) and **per-app streaming** (one app on a private
virtual display, §4f — experimental). Upgrade both apps together. An optional **self-hosted relay** (`python3 -m phonebridge.relay_server`, §1g) lets the phone reach the PC from another network. Details and known
limitations in `RELEASE_NOTES.md`.

**v1.1:** per-file TLS data connections with sha256 + resume (`protocol/PROTOCOL.md` §1c), authenticated control
connections, per-phone "Allow PC control" (off by default), mDNS + BLE-wake + HMAC-tagged LAN discovery, wakelock
only during transfers/streams (30 s ping), mirroring bitrate adaptation, image clipboard, MMS viewing, local-only
hotspot, call audio on the PC over Bluetooth HFP. **Upgrade both the PC app and the phone app together** — a v1.0
phone and a v1.1 PC cannot exchange files. Caller names on calls already work (contact lookup).
Tests: `for t in linux/tests/test_*.py; do python3 $t; done` (needs `pip install qrcode` only for the app itself).

**v1.0 (Android app rebuilt, `android/`):** the companion app was rewritten to match
`protocol/PROTOCOL.md` (see section 4b for the added messages): full device status
(battery, signal, Wi-Fi, storage, RAM), active-notification sync with actions/reply,
full SMS history with contact names, call history + contacts + dialing from the PC,
paged photo browsing, phone file browser with transfers both ways, screen mirroring with
mouse/keyboard control, front/back camera as webcam, clipboard history, and app
launching. The Linux side renders video through an appsink (no `gtk4paintablesink`
needed) and watches the clipboard through an XWayland helper so sync works while the
window isn't focused. Build with `gradle :app:assembleDebug` in `android/` and install
`app/build/outputs/apk/debug/app-debug.apk`.

Earlier phase notes:

Phases 1-8 of 9 done, phase 9 (polish + packaging) in progress:

- **Protocol + pairing** — X25519 + HKDF + AES-GCM handshake over Bluetooth
  RFCOMM, offer-first wire order, cert-fingerprint pinning for the Wi-Fi leg.
  Verified end-to-end by `linux/tests/test_pairing_handshake.py` (13/13).
- **Wi-Fi/TLS control channel** — persistent encrypted connection, self-signed
  PC identity cert, per-device session keys, async dispatch by message type.
  Verified end-to-end over real loopback TLS by `linux/tests/test_transport.py`
  (6/6) — this is a real client/server exchange, not a mock.
- **Linux app**: GTK4/libadwaita sidebar shell (all 10 sections), pairing
  dialog, live device battery status, notifications list (with dismiss →
  phone), clipboard sync (both directions, echo-safe). **Not yet visually
  run** — written from a Termux/Android dev shell with no display server;
  run it on your real Linux desktop to confirm layout.
- **Android app**: pairing (QR scan), a foreground `BridgeService` holding
  the live PC connection, `NotificationRelay` (mirrors + dismiss), battery
  reporting, clipboard relay. **Builds into a real, correctly-signed debug
  APK**, verified by Gradle in this session — but not yet installed/run on a
  physical phone, so Bluetooth/camera/notification-listener code paths are
  unexercised at runtime until you sideload and grant the relevant one-time
  OS permissions (Camera, Notification access, Bluetooth, battery).
- Known limitation carried over from the protocol spec: phone→PC clipboard
  sync only works reliably while the PhoneBridge app is in the foreground on
  the phone (Android 10+ background clipboard-read restriction — not a bug).
- **File transfer** (PC→phone, drag-and-drop): real multi-chunk transfer over
  the same TLS channel, verified end-to-end by `linux/tests/test_file_transfer.py`
  (6/6) including a byte-for-byte SHA-256 check of the received file. Android
  side saves into the phone's Downloads via MediaStore (API 29+) or legacy
  external storage (API ≤28). Auto-accepted for now — no reject-prompt UX yet.
- **SMS** (not the default-SMS-app role — stock Messages app keeps working
  normally alongside it): thread list + conversation view + send, incoming
  texts pushed live. Linux: two-pane Messages page.
- **Calls**: ringing/active/ended state mirrored to a Calls page with
  Answer/Decline/Mute. Answer and mute are solid (standard permissions);
  **decline is best-effort** — Android generally restricts programmatic call
  rejection to the default dialer app, so it may simply no-op depending on
  the phone's Android version (documented in `telephony/CallRelay.kt`, not a
  bug to chase). Caller name lookup isn't implemented yet (number only).
- **Photos**: camera-roll browser (thumbnail grid, fetched on demand) +
  auto-push of brand-new photos as they're taken. Reuses the same generic
  file-transfer mechanism as phase 3 (now also receiving phone→PC, not just
  sending), tagged with `photo_id` so the PC routes finished downloads to
  `~/Pictures/PhoneBridge` instead of generic `~/Downloads/PhoneBridge`.
  Auto-sync baselines to "now" on first run — it won't flood-push your
  entire existing camera roll, only photos taken after pairing.
- **Screen mirroring + input passthrough**: a dedicated second TLS connection
  carries the continuous H.264 stream (needed a real transport-design fix —
  see "Architecture note" below), verified end-to-end with fake frames over
  real concurrent sockets by `linux/tests/test_video_connection.py` (5/5).
  Android: MediaProjection capture → MediaCodec H.264 encode, downscaled to
  ≤1280px wide for bandwidth. Android **cannot** start capture without the
  user seeing and approving the OS's own consent dialog on the phone, every
  time — `mirror.start` from the PC only triggers a notification prompting
  that; it can't bypass it, by design. Input: an AccessibilityService
  (one-time toggle in Settings > Accessibility) handles tap/swipe/back/home/
  recents; text injection is best-effort (only works into a focused,
  accessible editable field — no general "type anywhere" API exists without
  also being the active keyboard). Linux: GStreamer
  (`appsrc ! h264parse ! avdec_h264 ! videoconvert ! gtk4paintablesink`)
  decode/render + mouse-drag-to-tap/swipe. **Entirely unverified end-to-end**
  — no camera/display/GStreamer available in this dev shell, and MediaProjection
  fundamentally cannot be exercised without a real phone screen and a human
  tapping the consent dialog. Compiles cleanly via Gradle; that's the extent
  of what could be checked here.
- **Phone-as-webcam**: reuses the exact same dedicated video connection as
  screen mirroring (Camera2 + MediaCodec H.264 instead of MediaProjection).
  Notably does *not* need a per-session OS consent dialog like mirroring
  does — just the one-time CAMERA permission (already requested during QR
  pairing) — so `webcam.start` can begin streaming immediately. Linux feeds
  the decoded stream into a `v4l2loopback` virtual device so any app (a
  browser, video-call software) can pick "PhoneBridge Camera" as a webcam;
  needs `v4l2loopback-dkms` installed and `modprobe`'d on your PC first
  (instructions on the Camera page). Equally unverified as phase 6, for the
  same reasons (no camera/GStreamer/v4l2loopback here) — compiles cleanly.
- **Apps**: remote launcher — browse installed apps (icon grid) and launch
  one on the phone from the PC. "App mirroring" is explicitly an
  approximation here, documented as such rather than pretended otherwise:
  there's no public API for isolated per-app window capture on Android, so
  seeing the launched app requires separately starting full-screen mirroring.
- **Phase 9 (polish + packaging)**: GTK stylesheet applied app-wide (`linux/phonebridge/ui/style.css`),
  app icon + `.desktop` entry (`linux/data/`), `pyproject.toml` for a proper
  `pip install` of the Linux app, and a signed **release** Android APK (see
  "Building the Android app" below — the debug keystore from earlier phases
  is replaced by a dedicated release key in `android/keystore/`, kept out of
  git via `.gitignore`/`keystore.properties`). `packaging/install.sh` /
  `uninstall.sh` install the Linux app for the current user (venv + launcher
  script + desktop entry + icon into standard XDG locations) — no `.deb` or
  Flatpak packaging yet, since neither toolchain is available in the Termux
  shell this was built from.

### Architecture note (phase 6)

The control connection (JSON messages) can't also carry screen-mirror/webcam
frames: those are a continuous, unbounded raw byte stream with no JSON
wrapper, so a receiver can't tell "is the next thing on the wire a JSON
message or a video frame?" if arbitrary control traffic can interleave with
it (file transfer avoided this by being bounded and handled synchronously
within one handler call — video has no such bound). Fixed by giving
video its own second connection, opened on demand, identified by
`"<fingerprint>:video"` instead of just the fingerprint — see
`protocol/PROTOCOL.md` section 1b. This is reused as-is for phase 7's webcam.

See the plan this was built from for the full phase breakdown.

## Running the tests

```sh
tools/run_tests.sh            # venv + requirements + every test; skipped tests count as failures
PHONEBRIDGE_STRICT=0 tools/run_tests.sh   # allow skips
```
Kotlin interop tests need Java and Gradle's Kotlin compiler under `~/.gradle`; the FUSE test needs `fusepy` and
`/dev/fuse`; GUI tests need `xvfb-run`. A single file: `cd linux && python3 tests/test_security.py`.

## Running the Linux app

Needs GTK4 + libadwaita system libraries (`gtk4`, `libadwaita`,
`python3-gobject` — package names vary by distro) in addition to
`requirements.txt`.

```sh
cd linux
python3 -m phonebridge.app
```

## Installing the Linux app

For a one-time setup that adds PhoneBridge to your app menu instead of
running it from a checkout:

```sh
./packaging/install.sh      # venv + launcher + desktop entry + icon, user-local
./packaging/uninstall.sh    # removes everything the above created
```

## Building the Android app

```sh
cd android
gradle :app:assembleDebug     # unsigned-equivalent debug build, debug key
gradle :app:assembleRelease   # signed with android/keystore/phonebridge-release.jks via android/keystore.properties
```

The release keystore and its credentials (`android/keystore.properties`) are
generated locally and `.gitignore`d. **Back up `android/keystore/` somewhere safe: installing a release build over a
debug-signed install (or vice versa) needs an uninstall, which also drops the pairing.** If `keystore.properties` is
missing the build prints a warning and falls back to the debug key — regenerate them if you clone this repo
elsewhere; nothing under `android/keystore/` is meant to be shared or reused
for a real public release.

On a real PC, open `android/` in Android Studio — it's a normal Gradle project
and Android Studio will generate the `gradlew` wrapper for you automatically.
**If building from Termux** (like this was developed), there's one
extra step: AGP's bundled `aapt2` is a standard glibc binary that can't execute
in Termux's non-FHS environment. Point it at Termux's own native `aapt2`
package instead, in your user-global (not project) Gradle properties so it
never affects a real-machine build:

```sh
pkg install aapt2
mkdir -p ~/.gradle
echo "android.aapt2FromMavenOverride=$PREFIX/bin/aapt2" >> ~/.gradle/gradle.properties
```

Install the debug build with `adb install app/build/outputs/apk/debug/app-debug.apk`,
or the signed release build with `adb install app/build/outputs/apk/release/app-release.apk`
(no Play Store, no Google account — straight sideload).
