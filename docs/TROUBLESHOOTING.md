# Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "App blocked to protect your device" when installing | Google Play Protect. Use "More details" -> "Install anyway" or `adb install`. Do not disable Play Protect. Make sure the "install unknown apps" permission is granted to the installer app. |
| The Android app is always horizontal | By design: the main screen is landscape-only (both landscape directions). Portrait is not supported. The QR scanner screen does allow portrait. |
| "App not installed" over an existing install | Different signing key (debug vs release). Uninstall first; this removes the pairing. |
| Phone cannot find the PC | Both on the same network? Firewall must allow TCP 28100 and UDP 28101. Try the USB link (`adb`, USB debugging on). Android 8-9 need `PHONEBRIDGE_ALLOW_TLS12=1` on the PC. |
| Pairing fails: wrong PIN | The code lives 60 s and burns after 3 wrong tries; start a new pairing. |
| Screen mirroring never starts | The phone must be unlocked; Android asks for consent every time (tap "Start now"). |
| Remote control from the PC does nothing | Enable the PhoneBridge accessibility service on the phone. Note: `adb shell am force-stop dev.phonebridge` disables it. |
| Phone says PC control is off | "Allow PC control" on the PC's Device page is off or timed out (default 60 min); tick it again. |
| Phone->PC clipboard not syncing | Android 10+ only allows the focused app to read the clipboard; open PhoneBridge on the phone. |
| App crashes at start with `ValueError: Namespace Gst not available` | The GStreamer GObject bindings are missing: install `gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0` (and the plugin packages listed in `docs/INSTALLATION.md`). |
| Webcam not listed | Load `v4l2loopback` (see `packaging/setup-webcam.sh`). |
| Android notifications/permission prompts missing | Notifications for PhoneBridge are disabled: "ask" prompts then count as deny. |
| GUI tests fail with missing `qrcode` | Use `tools/run_tests.sh`, which builds a venv from `requirements.txt`. |
| Keys lost / keyring wiped | Re-pair the phone; see `docs/SECURITY.md` (incident note). |

Bug reports: see `CONTRIBUTING.md`. Never attach keys, logs with personal data, or `~/.config/phonebridge` contents.
