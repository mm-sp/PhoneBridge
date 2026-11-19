# GitHub publication checklist

Nothing has been pushed, published or created on GitHub. Status key: PASS / FAIL / WARNING / NOT TESTED / TODO.

## A. Proposed repository settings (for your approval)
| Item | Proposal |
|---|---|
| Name | `phonebridge` |
| Visibility | Public |
| Description | "Account-free Android <-> Linux bridge: notifications, SMS, files, clipboard, screen mirroring and webcam over an end-to-end encrypted direct connection." |
| Topics | `android`, `linux`, `kotlin`, `python`, `gtk4`, `libadwaita`, `phone-link`, `screen-mirroring`, `file-transfer`, `privacy`, `end-to-end-encryption`, `self-hosted` |
| Default branch | `main` |
| Remote | none configured; **I need the repository URL from you** |

## B. Blocking decisions
| # | Item | Status | Action |
|---|---|---|---|
| 1 | **License** | TODO - blocked on you | No `LICENSE` exists. Without one, nobody may legally use, copy or modify the code even though it is public; contributions are legally unclear. **MIT**: very short, permissive, no patent clause. **Apache-2.0**: permissive, explicit patent grant and patent-retaliation clause, requires NOTICE/attribution of changes, more boilerplate. For a security-sensitive project that others may build on, Apache-2.0 is the safer default; MIT if you want maximum simplicity. (GPL-3.0 is the choice if you want derivatives to stay open.) Tell me which one and the copyright holder name to put in it. |
| 2 | **Git author identity** | WARNING | All 6 commits are authored `sai <<redacted-email>>`; publishing the history publishes that real e-mail address. Options: (a) accept it; (b) before the first push, recreate history with a GitHub `noreply` address (rewrites the local commit ids, needs your approval; there is no remote yet so nothing public is affected); (c) publish a single squashed first commit. Also add `git config user.email <noreply>` for future commits. |
| 3 | Security contact | TODO | `SECURITY.md` and `docs/PRIVACY_POLICY.md` contain placeholders; enable GitHub private vulnerability reporting or give an address. |
| 4 | Screenshots | TODO | README says none yet; add real ones (needs a phone). |

## C. Security audit
| Check | Status | Evidence |
|---|---|---|
| Tracked files (213) contain no `.jks/.keystore/.p12/.pem/.key/.env/.apk/.aab/.enc/.log/.db/.tar/.zip` | PASS | `git ls-files` filter |
| Same file types never present in any commit (all reachable history, 6 commits) | PASS | `git log --all --name-only` filter |
| Private-key blocks, cloud/GitHub/Slack/Google/OpenAI-style tokens, `password=`-style literals in current tree and all history | PASS | regex scan with `git grep` over every revision. **Limitation:** gitleaks/trufflehog are not installed; this is a pattern scan, not an entropy-based scanner |
| Signing key / keystore.properties / passphrases | PASS | never committed; live in `~/.config/phonebridge-android-release/` and `~/phonebridge-backup-passphrase.txt` (outside the repo). `build.gradle.kts` only contains property *names* |
| Release certificate fingerprint in docs | PASS (public information) | |
| Personal data in tracked files (working tree) | PASS after fixes | the two absolute home-directory paths in the audit report were replaced (`docs/AUDIT_REPORT.md`); phone model (Mi A3) is mentioned as test hardware. Serial number and e-mail are not in the tree |
| Personal data in history (older commit contents) | WARNING | verified: the home-directory path `/home/<username>/projects/phonebridge/...` (2 lines) exists in `PROJECT_AUDIT_REPORT.md` in **all 6 existing commits** (the file was later moved and cleaned in the working tree). The phone serial number and the e-mail address do **not** appear in any file content in history (the e-mail is only in commit author metadata). Impact is low (a username and directory layout), but it is permanent once pushed. Decide with item B2 whether to recreate history |
| `.gitignore` covers keys, properties, env, builds, IDE, logs, backups | PASS | `.gitignore` reviewed |
| Staged/untracked secrets | PASS | `git status` clean of such files |
| APK/AAB/dist files tracked | PASS (none) | `dist/` is ignored |
| Backup tarballs `ios-removed-backup.tar.gz`, `phonebridge-src-backup-*.tar.gz` | PASS | they live outside this repository directory |
| Signing key exposure in history (the key migration commit `1bf6fe6`) | PASS | that commit changed scripts/docs only |
| Third-party code or assets with unclear licenses | NOT TESTED | no audit of icon/asset provenance (`android/app/src/main/res/drawable`, `linux/data/icons`) |

## D. Re-run immediately before pushing (they were clean except as noted in C)
```sh
for c in $(git rev-list --all); do git grep -lI '<phone-serial>' $c; done          # phone serial: none expected
for c in $(git rev-list --all); do git grep -cI '/home/<your-username>' $c; done   # known: old audit report
for c in $(git rev-list --all); do git grep -lI '<your-email>' $c; done      # none expected
```

## E. Build and test (this audit)
| Item | Status |
|---|---|
| PC tests, strict, 39 files | PASS - 792 checks, 0 failed |
| Android lint | PASS - 0 errors, 39 warnings |
| Debug APK, release APK, AAB build | PASS |
| APK signature (apksigner), AAB signer, fingerprint unchanged | PASS - `10:EE:C5:7A:...:DA:9B:F9` |
| Application ID / version | PASS - `dev.phonebridge`, 1.4.0 (14), minSdk 26, targetSdk 29, no native libs |
| Dependency vulnerability scan (OSV) | PASS - 7 packages (sent names/versions to osv.dev) |
| **Real-device smoke test of the R8 release APK** | **NOT TESTED** |
| Upgrade over an existing install, Android 13-16 matrix, TalkBack, portrait | NOT TESTED |
| GitHub Actions workflow (`ci.yml`) | NOT TESTED - YAML parses; never executed; assumes the hosted runner provides Android SDK platform 35 and Gradle 8.14.7 via `gradle/actions/setup-gradle` |
| Static analysis (bandit, shellcheck, ruff) | NOT TESTED - not installed |

## F. Files published / excluded
Published: everything tracked by git (213+ files: `linux/`, `android/`, `protocol/`, `docs/`, `packaging/`, `tools/`, `.github/`, top-level docs). Excluded by `.gitignore`: `dist/`, build outputs, `.gradle`, `local.properties`, keystores/properties/passphrases, venvs, caches, logs, IDE files, `*.tar.gz`.
Not in the repository directory at all: `PhoneBridge-Research/` and the two tarballs (parent folder). Decide separately whether to publish `PhoneBridge-Research/`.

## G. Release artifacts
Do not attach artifacts until you approve. When approved: `dist/1.4.0/phonebridge-1.4.0.apk`, `phonebridge-1.4.0.aab` (for Play only), `SHA256SUMS`. Do not attach the debug APK. Keep `mapping-1.4.0.txt` private (de-obfuscation).
Before attaching: smoke-test the APK on a phone (E), and run `sha256sum -c dist/1.4.0/SHA256SUMS`.

## H. Steps once everything above is approved
```sh
git remote add origin <your-repository-url>      # I will show you this before running it
git push -u origin main                          # only after your explicit approval
git tag -a v1.4.0 -m "PhoneBridge 1.4.0" && git push origin v1.4.0
```
Then: enable private vulnerability reporting, branch protection on `main`, protect the `release` environment, add repository secrets only if you want CI-signed releases (`PHONEBRIDGE_KEYSTORE_B64`, `..._PASSWORD`, `..._KEY_ALIAS`, `..._KEY_PASSWORD`), create the GitHub release with the checksums.
