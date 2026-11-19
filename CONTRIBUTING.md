# Contributing

* Open an issue before large changes. Keep changes focused; keep the protocol spec (`protocol/PROTOCOL.md`) and both implementations (Python + Kotlin) in step.
* Run `tools/run_tests.sh` and `cd android && gradle :app:lintDebug` before sending a change. Add a test with every fix.
* Never include keys, passwords, keystores or personal data in commits, issues or logs.
* Security-sensitive areas (pairing, handshake, PC control, permission gates) need extra review; see `docs/SECURITY.md`.
* The project is licensed under Apache-2.0 (`LICENSE`). By submitting a contribution you agree it is licensed under the same terms.

## Reporting a bug
Open an issue with: PhoneBridge version (PC and phone), phone model + Android version, PC distribution + desktop, what you did, what you expected, what happened, and relevant log lines. Remove IP addresses, names, phone numbers and message content first. Do not attach files from `~/.config/phonebridge`.
