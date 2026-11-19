# Installation

## Linux app
Requirements: Python 3.10+, GTK 4 and libadwaita with the PyGObject bindings (Debian/Ubuntu: `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`), GStreamer plugins for H.264 decoding, `adb` (optional: USB link and screen sync), `v4l2loopback` (optional: webcam), Avahi (optional: mDNS).
```sh
git clone https://github.com/mm-sp/PhoneBridge.git && cd PhoneBridge
./packaging/install.sh          # user-local venv, launcher, desktop entry; no root
phonebridge                     # or start it from the app menu
./packaging/uninstall.sh        # removes what install.sh created
```
Webcam: `packaging/setup-webcam.sh` (needs root to load the kernel module). Running from a checkout without installing: `cd linux && python3 -m phonebridge.app` (needs `pip install -r requirements.txt`).

## Android app
1. Download `phonebridge-<version>.apk` and `SHA256SUMS` from the release page and check `sha256sum -c SHA256SUMS`.
2. Allow your file manager/browser to "Install unknown apps" (Settings -> Apps -> Special app access).
3. Open the APK. Google Play Protect may warn about an app from an unknown developer that uses accessibility, SMS and screen capture. Use "More details" -> "Install anyway" only if you obtained the APK from the official release and the checksum matches. Do not disable Play Protect. Alternative: `adb install phonebridge-<version>.apk`.
4. Installing a build signed with a different key (for example a debug build) over an existing install is refused by Android; uninstall first (this removes the pairing).
5. Grant the runtime permissions the app asks for, then in Android settings enable: Accessibility -> PhoneBridge remote control (for control from the PC), Notification access -> PhoneBridge, "Display over other apps" (per-app streaming), and exempt the app from battery optimisation if you want it to stay connected.

## First pairing
Open PhoneBridge on the PC ("Pair a device"), then in the Android app scan the QR code or enter the 6-digit PIN. Confirm that the confirmation codes match on both screens. The code is valid for 60 seconds.

## Updating
Install the new APK over the old one (same signing key required). Upgrade the PC app and the phone app together: older protocol versions are not compatible.
