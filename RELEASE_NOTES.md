# PhoneBridge release notes

## 1.4.0

### New
- Pairing v4 (SPAKE2), device identities, forward secrecy, key rotation, signed audit ledger, permission manager and
  Security dashboards on both sides; USB link; signed-update verification (`docs/SECURITY.md`).
- Screen sync: the PC can power the phone's panel off while mirroring, with a detached watchdog that restores it
  (`docs/SCREEN_SYNC.md`). Screen-rotation sync is **not** implemented.

### Changed
- **Phone-to-PC control needs approval on the PC and is limited to one hour.** A phone that asks while control is off raises a prompt on the PC (Deny / Allow 15 minutes / Allow 1 hour); a red banner shows who has control and offers "Revoke now"; revoking closes running shells immediately and expiry within seconds. `PHONEBRIDGE_PC_CONTROL_MINUTES` can only lower the default. Existing "on" settings count as off until you tick the box again.
- **The Android main screen is landscape-only, by design** (`sensorLandscape`: both landscape directions, chosen by the sensor; Android documents that the sensor is used even when auto-rotate is off, which is the only case checked on the Mi A3). The tab column, terminal and mirror views are laid out for a wide screen. A portrait layout (tabs in a strip along the bottom) is still in the code but is **dormant and untested**: it should be reachable only in unusual window shapes such as split-screen or freeform windows (an assumption), and none of that has been verified. The QR scanner keeps its own setting (`fullSensor`) so a portrait phone can still scan. A manifest test (`linux/tests/test_android_manifest.py`) guards both values.
- **Accessibility (TalkBack):** tabs announce "selected", the header is a heading and the connection chip is announced when it changes, screen/app images and symbol keys (arrows, ⌫, ⏎, Shift…) have spoken names, the on-screen and terminal keys can be activated by double-tap, buttons are at least 48 dp tall and tabs grow with large font sizes.
- Release APK is signed with the permanent release key, read from `~/.config/phonebridge-android-release/keystore.properties` or `PHONEBRIDGE_*` environment variables (falls back to the debug key with a warning if neither exists).
- The phone-controlled PC shell no longer inherits `PHONEBRIDGE_*` environment variables.
- "Allow PC control" is checked at most once per second per phone instead of on every message.
- `qrcode` is imported lazily; `fusepy` is the `fuse` extra; `tools/run_tests.sh` runs everything, skips = failures.

### Known limitations
- Verified on one phone only; MMS carrier delivery, real access-point join and WAN relay are unproven.
- No independent human security review.

## 1.3.0

### New

- The iPhone app was removed from this repository (it was never compiled or run). The PC still accepts a
  `platform: ios` pairing hello (`protocol/PROTOCOL.md` §4g).
- The PC now remembers whether a paired phone is Android or iPhone and, for an iPhone, shows only the Device and
  Files tabs. Older phone apps keep working unchanged. (`protocol/PROTOCOL.md` §4g)

- **Optional self-hosted relay** for using the phone away from the PC's network. Run
  `python3 -m phonebridge.relay_server` on a machine both can reach, enter its address in the PC app (Device tab) and
  the phone connects through it when the PC isn't reachable directly. No accounts and nothing stored on the relay,
  which only forwards bytes that stay end-to-end encrypted. Off by default. UDP video falls back to TCP through it.
  (`protocol/PROTOCOL.md` §1g; `linux/tests/test_relay.py` runs a fake phone through a real relay.)
- The mirror and webcam status now say whether video is arriving over UDP or TCP.
- Picture messages can go to several recipients (separate numbers with commas in the "To" box).

### Changed

- Version 1.3.0 on the PC package, and the Android app (`versionCode` 13).

## 1.2.0

**Upgrade the PC app and the phone app together.** Video falls back to the old transport if only one side is
upgraded, but picture messages and single-app streaming need both sides.

### New

- **UDP video.** Screen mirroring and the webcam now send video as encrypted UDP datagrams, so a lost packet costs
  one frame instead of stalling the stream. After a loss the PC keeps the last picture and asks the phone for a
  keyframe. The phone lowers the bitrate when the PC reports loss and raises it again after a clean stretch. If
  the PC doesn't answer the phone's UDP probe (for example a firewall drops UDP), the TCP video connection is
  used as before. This is not WebRTC: there is no ICE, DTLS or SRTP, and no retransmission or error correction.
  (`protocol/PROTOCOL.md` §1f)
- **Sending MMS.** Attach pictures in the Messages tab (paperclip button) and send them as an MMS. The phone
  shrinks pictures so the message stays under 300 KB, builds the message and hands it to Android's MMS service.
  The bubble shows sending / failed with the reason. (§4e)
  - Messages sent this way don't appear in the phone's own Messages app (Android only lets the default SMS app
    write there). The PC keeps them in the open conversation until you close it.
  - Needs mobile data. Sending to one person from the PC is supported; group MMS is not tested.
- **Per-app streaming.** In the Apps tab, choose "Stream only the app" to show a single app on the PC. It runs on a
  private virtual display, so the phone's own screen is left alone and no screen-capture consent is needed.
  (§4f)
  - Needs "Display over other apps" on the phone. Controlling the app from the PC needs Android 12L or newer;
    older versions are view-only.
  - Back / Home / Recents aren't available in this mode. Some apps (those that can't be resized, or that block
    screen capture) may show black or refuse to start. Experimental.

### Changed

- The Apps tab "Open with screen mirror" checkbox is now a three-way dropdown: Open on phone / Mirror the phone /
  Stream only the app.
- Version numbers were out of step: the Android app said 1.0.0 and the PC package 0.9.0 while the README described
  v1.1. Both are now 1.2.0 (Android `versionCode` 12).

### Known limitations

- Nothing in this release has been tested on a physical phone. Pairing, call audio, camera, UDP video, MMS and app
  streaming have only been exercised through unit/loopback tests and compile checks.
- The Kotlin UDP sender and the Python receiver have not been run against each other; their byte layouts were
  checked by reading. The MMS message encoder is checked against a spec-based decoder, not a real carrier.
- Not built: real WebRTC (needs a WebRTC library on both ends; the UDP transport covers the low-latency goal), a hosted relay service (only the self-hosted relay exists), iPhone Photos/notification features (iOS limits, plus no way to compile Swift here).

### Tests

`for t in linux/tests/test_*.py; do python3 $t; done` — the FUSE test needs `fusepy` and `/dev/fuse`; the two UI
tests need a display (`xvfb-run -a`); `test_mms_pdu.py` compiles the Kotlin encoder with the compiler bundled in
the Gradle install and skips if it can't find one.

## 1.1 (November 2026)

File transfer v2 (one TLS data connection per file, sha256 verification, resume), first-frame authentication of
control connections, a per-phone "Allow PC control" switch (off by default), mDNS + BLE-wake + HMAC-tagged LAN
discovery, mirroring bitrate adaptation, image clipboard, MMS viewing, local-only hotspot, call audio on the PC over
Bluetooth HFP, and a FUSE mount of the phone's files. Upgrade both apps together: a v1.0 phone and a v1.1 PC cannot
exchange files.

## 1.0

Android app rebuilt to match `protocol/PROTOCOL.md`: device status, notification sync with actions and reply, full SMS
history, call history, contacts and dialing, photo browsing, file browser, screen mirroring with mouse and keyboard
control, phone camera as a webcam, clipboard history and app launching.
