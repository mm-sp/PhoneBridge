# GitHub publication checklist

Nothing has been created, pushed or published. The clean local history **has been applied and verified** (section B, section I). A later documentation-only correction pass (2026-11-19) fixed outdated license, key-location, test-count and feature-status statements, added `*.pem`, `*.key` and `*.bundle` to `.gitignore`, and generalised backup/passphrase locations so the public docs do not reveal them. Status: PASS / FAIL / WARNING / NOT TESTED / TODO.

## A. Decisions recorded
| Item | Value | Status |
|---|---|---|
| Repository | `https://github.com/mm-sp/PhoneBridge` (public) | URL returns 404 for the public API (no such public repository); **ownership/access of `mm-sp` could not be verified** (no `gh` CLI installed, no authenticated session) |
| GitHub account | `mm-sp`, user id `62277083` (public API), created 2020-03-17, 0 public repositories, no public display name | PASS (account exists) |
| Description | "Securely connect and control your Android phone and Linux PC with wireless and USB connectivity." | decided |
| Topics (proposed) | `android`, `linux`, `kotlin`, `python`, `gtk4`, `libadwaita`, `phone-link`, `screen-mirroring`, `file-transfer`, `privacy`, `end-to-end-encryption`, `self-hosted` | proposed |
| License | Apache-2.0, `Copyright 2026 Saiprakash Manepalli` (`LICENSE` = canonical text, sha256 `cfc7749b...523d30`; copyright line and dependency licenses in `NOTICE`) | PASS - `NOTICE` checked against package metadata and Maven POMs (corrected Pillow to MIT-CMU; added PyGObject, fusepy, zxing core, Kotlin stdlib) |
| Security contact | GitHub private vulnerability reporting (`SECURITY.md`); no personal e-mail published | PASS (must be enabled in repo settings after creation) |
| Noreply commit identity | `62277083+mm-sp@users.noreply.github.com` (GitHub's documented `<id>+<username>@users.noreply.github.com` format, id from the public API) | PASS - not guessed |
| Screenshots | placeholders only in README | TODO (needs real captures) |

## B. Clean-history cleanup (APPLIED locally; not pushed)
Why: all 8 commits carry the personal e-mail in author/committer metadata; `/home/<user>/...` paths exist in the old audit report in 6 early commits; and one intermediate commit (the first version of this checklist) quoted the e-mail address in file contents. The phone serial never appears in any commit's contents. The current tree contains none of these.
1. Keep the current repository as a backup (`cp -a PhoneBridge-linux-src PhoneBridge-linux-src.pre-publish-backup`, or `git bundle create ../phonebridge-pre-publish.bundle --all`).
2. Rebuild, in a **new** directory, one commit per existing commit, same order, same messages (including the `Co-Authored-By` trailers), same dates (note: dates carry the `+0530` timezone), with author = committer = `mm-sp <62277083+mm-sp@users.noreply.github.com>`, and personal data scrubbed from file contents: the home-directory prefix is replaced by the placeholder `/home/<user>`, the personal e-mail address by `<redacted-email>`, the phone's adb serial by `<phone-serial>`. The rebuild script is a throw-away helper kept outside the repository.
3. Verify the new repository: no personal e-mail anywhere (`git log --format=%ae%n%ce`, content scan), no personal path, no signing-file types, contents of final tree identical to the current tree apart from the path scrub (`diff -r`).
4. After your approval: replace the old `.git` with the new one (the backup from step 1 stays), then `git remote add origin ...` and push (section H).
Irreversible part: only step 4 (replacing `.git`); it is reversible while the backup from step 1 exists, and it is local until you push.
**Dry-run result:** see section I.

## C. Security audit (re-checked after the latest changes)
| Check | Status |
|---|---|
| Tracked files contain no `.jks/.keystore/.p12/.pem/.key/.env/.apk/.aab/.enc/.log/.db/.tar/.zip` | PASS |
| Same file types never present in any commit | PASS |
| Private-key blocks / cloud, GitHub, Slack, Google, OpenAI-style tokens / `password=`-style literals, current tree and all history | PASS - regex scan; **limitation:** gitleaks/trufflehog not installed, no entropy scanning |
| Signing key, `keystore.properties`, backup passphrase | PASS - not in the repository; the key and properties live in `~/.config/phonebridge-android-release/`; the passphrase is stored outside the repository |
| `build.gradle.kts` contains only property names, never values | PASS |
| Personal data in working tree (paths, e-mail, phone serial) | PASS |
| Personal data in history | PASS - cleaned by step B (completed locally): 0 hits for the personal e-mail, home-directory paths or phone serial in any of the 12 commits' contents or metadata |
| `.gitignore` (keys, properties, env, builds, IDE, logs, backups, passphrases) | PASS |
| CI workflow | WARNING: read-only permissions, only `actions/*` and `gradle/actions` (official), secrets only in the tag-triggered `release` job with a protected environment; **never executed**. Actions are pinned to major versions, not commit SHAs |
| Third-party asset/icon provenance | NOT TESTED |

## D. Tests and builds (latest run)
| Item | Status |
|---|---|
| PC tests, strict, 41 test files | PASS - 832 checks, 0 failed, 0 skipped |
| Android lint | PASS - 0 errors, 38 warnings |
| Debug APK, release APK, AAB | PASS |
| apksigner / AAB signer / fingerprint `10:EE:C5:7A:...:DA:9B:F9` | PASS (unchanged) |
| Application ID, version | PASS - `dev.phonebridge`, 1.4.0 (14), minSdk 26, targetSdk 29, no native libraries |
| OSV dependency scan | PASS - 7 packages, no known vulnerabilities |
| **R8 release APK on a real device** | **PARTIALLY TESTED**: one phone (Mi A3, Android 17), USB tunnel, 3 of 18 checks fully passed, 8 partly, 7 not tested; Wi-Fi, PC-to-phone control, SMS/calls/notifications/clipboard, TalkBack not tested (`docs/DEVICE_SMOKE_TEST.md`) |
| Approval dialog (`app.py`), CI workflow, bandit/shellcheck, Android 13-16, TalkBack | NOT TESTED |

## E. Signing backup status
Only **one** encrypted off-machine backup exists (on a separate device). A second, on different hardware, is still required. Keep any plaintext copy of the backup passphrase until it is stored safely elsewhere (password manager + paper) and a restore has been tested, then delete the plaintext copy.

## F. Remaining security issues
Play Protect may block the sideloaded APK (see `docs/TROUBLESHOOTING.md`); R8 build only partially tested on one device (`docs/DEVICE_SMOKE_TEST.md`); targetSdk 29; `BootReceiver` exported; PC-control approval dialog untested; single key backup; no independent human audit of SPAKE2/handshake; CI untested.

## G. Files published / excluded
Published: all git-tracked files (224 at the time of writing). Excluded: `dist/`, build outputs, keystores/properties/passphrases, venvs, logs, IDE files, `*.tar.gz`. Release artifacts (APK/AAB) are **not** attached anywhere until you approve.

## H. Exact publication commands (run only after final approval)
```sh
cd ~/projects/phonebridge/PhoneBridge-linux-src
# 1. create the empty public repository in the browser: https://github.com/new  (name PhoneBridge, Public, NO readme/license/gitignore)
git remote add origin https://github.com/mm-sp/PhoneBridge.git
git branch -M main
git push -u origin main                    # no force; fails safely if the remote is not empty
git tag -a v1.4.0 -m "PhoneBridge 1.4.0"   # only after the R8 smoke test passed
git push origin v1.4.0
# 2. repository settings: description + topics (section A); Security -> enable private vulnerability reporting;
#    branch protection on main; create environment "release" with required reviewers
# 3. release page: upload dist/1.4.0/phonebridge-1.4.0.apk + SHA256SUMS (not the debug APK, not the mapping file)
```
Authenticate with a GitHub token or SSH key yourself; never paste a token into a chat or a file in the repository.

## I. Cleanup result (2026-11-19)
Applied: the repository's `.git` was replaced by a rebuilt history. 11 commits (the original 10 plus the NOTICE correction), all authored and committed by `mm-sp <62277083+mm-sp@users.noreply.github.com>`, same order, messages and dates (dates carry the +0530 timezone). Verified: no personal e-mail, home-directory path or phone serial in any commit's contents or metadata; no signing-file types in any commit; every tracked file byte-identical to the pre-cleanup working tree; `git fsck --strict` clean; LICENSE sha256 `cfc7749b...523d30`; tests 813/0 (at that time); APK/AAB rebuilt; release certificate unchanged.
Private rollback material (contains the old identity - **never publish or upload these**), in the parent folder of the repository:
* `phonebridge-pre-publish-20261119.bundle` - git bundle of the original 10 commits (verified: clones to the same HEAD).
* `phonebridge-git-old-pre-publish/` - the complete old `.git` directory (all 11 old commits including the NOTICE fix).
Restore the old history: `cd PhoneBridge-linux-src && mv .git ../phonebridge-git-new && mv ../phonebridge-git-old-pre-publish .git`, or `git clone phonebridge-pre-publish-20261119.bundle restored`.
