# PhoneBridge privacy policy (draft - review before publishing)

PhoneBridge connects your Android phone to your own Linux computer. **It has no accounts, no servers operated by the developer, no analytics and no advertising.** Nothing is sent to the developer or any third party.

**What the app can access on the phone (only when you grant the permission and your paired PC asks):** notifications, SMS/MMS and contacts, call log and calls, photos and files, clipboard, camera, screen content (screen sharing and the accessibility service), device status (battery, Wi-Fi name, storage), installed apps.
**Where it goes:** directly to your paired PC over an encrypted connection (TLS 1.3 + AES-256-GCM) on your network, USB, or through a relay you host yourself. Data is not stored by the developer.
**What is stored:** pairing keys on each device (Android Keystore / desktop keyring), a signed local audit log, and files you transfer. SMS and notification content is not written to the PC's disk unless you save it.
**Your controls:** per-feature permissions on the phone and PC, "Hide notification content", per-app blocking, clipboard expiry, revoking a device, "Disconnect all", uninstalling the app.
**Children:** not directed at children.
**Questions and reports:** this project publishes no e-mail address or website. Report privacy or security problems through GitHub's private vulnerability reporting on this repository (Security tab -> *Report a vulnerability*). That route works only after the maintainer has enabled private vulnerability reporting in the repository settings; do not rely on it before then. If the button is not available, open a public issue that asks for a private channel and contains **no** details about the problem.
