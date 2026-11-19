# Signing and application identity

PhoneBridge's identity is three things that must never change for direct-distribution updates:

| | Value |
|---|---|
| Application ID | `dev.phonebridge` (do not change without a deliberate migration; users would have to reinstall) |
| Release certificate SHA-256 | `10:EE:C5:7A:52:F5:C2:01:CF:C6:71:49:6B:78:7D:48:49:9C:D8:DB:14:A2:12:2D:D6:32:2B:4E:62:DA:9B:F9` |
| Key | RSA 4096, SHA384withRSA, alias `phonebridge`, valid until 2051-11-13, subject `CN=PhoneBridge, O=PhoneBridge personal` |
| Keystore (local) | `android/keystore/phonebridge-release.jks`, credentials in `android/keystore.properties` (both git-ignored) |

Android only installs an update over an existing app when it is signed with the **same** certificate. If this key is
lost, every user must uninstall (losing their pairing) before they can install a differently signed build.

## Rules
* Never commit `*.jks`, `*.keystore`, `keystore.properties`, `release.key`, `.env`. `.gitignore` blocks them; git history was
  checked and contains none (only the property *names* appear in `build.gradle.kts`).
* Passwords come from `android/keystore.properties` or the environment variables `PHONEBRIDGE_KEYSTORE`,
  `PHONEBRIDGE_KEYSTORE_PASSWORD`, `PHONEBRIDGE_KEY_ALIAS`, `PHONEBRIDGE_KEY_PASSWORD`. Without them the release build prints a
  warning and falls back to the debug key; `tools/release.sh` refuses to run in that state.
* Do not generate a new key per release.

## Backups (required: at least two, on different physical devices)
```sh
tools/backup_keystore.sh android/keystore/phonebridge-release.jks android/keystore.properties /media/<usb-1>
tools/backup_keystore.sh android/keystore/phonebridge-release.jks android/keystore.properties /media/<usb-2>
tools/verify_keystore_backup.sh /media/<usb-1>/phonebridge-signing-*.tar.gz.enc 10:EE:C5:7A:...:9B:F9
```
You type the passphrase (AES-256, PBKDF2); it is not stored. Keep the passphrase separately from the backups (a password
manager plus a paper copy). Re-verify a backup after copying it and once a year.

## Recovery
Decrypt with `openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -in <backup> | tar -xz -C <new-dir>`, put the `.jks` and
`keystore.properties` back (fix `storeFile`), then run `tools/release.sh` and compare the printed certificate SHA-256 with
the value above.

## Moving the key off the development machine
Not done automatically. After two verified backups: move `android/keystore/` to encrypted external storage or
`~/.config/phonebridge-android-release/`, set `storeFile=` in `keystore.properties` (or `PHONEBRIDGE_KEYSTORE`) to the new path.

## Google Play (two different identities)
* **Upload key**: signs the `.aab` you upload. It may be this same key, but it is only used to authenticate uploads.
* **Play app-signing key**: held by Google; it signs the APKs users download from Play. Its certificate **differs** from the
  direct-APK certificate above, so a user cannot update a Play install with a sideloaded APK or the reverse; they must
  uninstall first. Decide which channel an install belongs to and document it for users.
