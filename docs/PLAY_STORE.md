# Google Play preparation

**Status: NOT ready to submit. Do not submit.** Checked against Google's published requirements on 2026-11-19 (web search snippets, not the full policy text - re-read the live pages in Play Console before acting).

## Blockers (each needs a decision)
| # | Blocker | Why | Options |
|---|---|---|---|
| 1 | **Target API**: app targets 29; Play requires **API 36** for new apps and updates since 2026-08-31 | Play will reject the upload | Migrate to targetSdk 36 (scoped storage for the Files tab, package visibility for Apps, foreground-service types, accessibility/overlay/BT changes). Large, needs on-device regression testing of every tab. |
| 2 | **SMS / Call Log permissions** (`READ_SMS`, `SEND_SMS`, `RECEIVE_SMS`, `READ_CALL_LOG`) | Play only allows these for apps that are the default SMS/Phone/Assistant handler (narrow exceptions). PhoneBridge is not a default handler. Remote control of the phone is not an exception. | A Play flavor without SMS/Calls features, or make PhoneBridge a real default SMS app (large). Direct APK keeps the full feature set. |
| 3 | **AccessibilityService used for remote control** | Policy requires prominent disclosure, in-app consent, a Play Console declaration, and scrutiny of non-accessibility use | Keep for direct APK; a Play flavor would need a documented, justified use or removal of PC->phone input. |
| 4 | `SYSTEM_ALERT_WINDOW`, `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`, `MediaProjection` foreground service, `ACCESS_FINE_LOCATION` | Each needs a declaration/justification in Play Console | See table below. |
| 5 | Store assets, privacy policy URL, support email, content rating, Data safety form | Not produced / need your identity | Draft policy in `docs/PRIVACY_POLICY.md`; screenshots need a phone. |

Recommendation: ship **direct APK + GitHub** now; treat Play as a separate project with a reduced-permission flavor.

## Signing for Play
Upload key vs Play app-signing key: see `docs/SIGNING.md`. Enrol in Play App Signing; the first upload registers the upload key. Play-delivered installs carry Google's certificate and cannot be updated by the direct APK (and vice versa).

## Permission declaration worksheet
| Permission | Used for | Play-sensitive? |
|---|---|---|
| READ/SEND/RECEIVE_SMS | Messages tab (read threads, send from PC, live incoming) | Restricted - blocker 2 |
| READ_CALL_LOG, CALL_PHONE, ANSWER_PHONE_CALLS, READ_PHONE_STATE | Calls tab | Restricted (call log) |
| READ_CONTACTS | caller names | Sensitive |
| READ/WRITE_EXTERNAL_STORAGE (legacy, `requestLegacyExternalStorage`) | Files/Photos tabs | Not available on target 36 |
| CAMERA | webcam, QR pairing | Sensitive |
| BLUETOOTH_*, NEARBY_WIFI_DEVICES, ACCESS_*_LOCATION | pairing, BLE wake, Wi-Fi name | Location needs justification (only to read SSID) |
| BIND_ACCESSIBILITY_SERVICE (InputService) | PC mouse/keyboard on the phone | Restricted - blocker 3 |
| BIND_NOTIFICATION_LISTENER_SERVICE | notification mirroring | Needs disclosure |
| SYSTEM_ALERT_WINDOW | per-app streaming / dim overlay | Needs justification |
| FOREGROUND_SERVICE (dataSync, mediaProjection, camera) | connection, mirroring, webcam | Declare FGS types |
| RECEIVE_BOOT_COMPLETED, WAKE_LOCK, REQUEST_IGNORE_BATTERY_OPTIMIZATIONS | reconnect | Battery-optimisation request is restricted |

## Data safety checklist (draft answers; verify)
* Data collected by the developer: **none** (no developer servers). Data is transmitted off-device only to the user's own PC; declare it as "shared with the user's own device", encrypted in transit: **yes**.
* Users can request deletion: uninstalling removes app data; no server copy exists.
* Data types the app touches locally: messages, call log, contacts, photos/files, device IDs/status, app list, clipboard, screen content.

## Store listing to prepare
Title, short (80) and full (4000) description, category (Tools), icon 512x512, feature graphic 1024x500, 2+ phone screenshots (need a device), content rating questionnaire, privacy-policy URL (host `docs/PRIVACY_POLICY.md`), support email, testing instructions for reviewers (they need a Linux PC; provide steps and a demo video), release notes from `RELEASE_NOTES.md`.
