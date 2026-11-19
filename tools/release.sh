#!/usr/bin/env bash
# Builds, verifies and checksums a release. Reads signing credentials from the environment or android/keystore.properties.
# Output: dist/<version>/{phonebridge-<v>.apk,.aab,debug apk,SHA256SUMS,SIGNING_REPORT.txt}
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/android"
GRADLE="${GRADLE:-$(ls -d ~/.gradle/wrapper/dists/gradle-*-all/*/gradle-*/bin/gradle 2>/dev/null | tail -1)}"
[ -x "$GRADLE" ] || GRADLE=gradle
BT="$(ls -d "${ANDROID_HOME:-$HOME/Android/Sdk}"/build-tools/* | sort -V | tail -1)"
VER="$(sed -n 's/.*versionName = "\(.*\)".*/\1/p' app/build.gradle.kts)"
[ -f "${PHONEBRIDGE_SIGNING_PROPERTIES:-$HOME/.config/phonebridge-android-release/keystore.properties}" ] || [ -f keystore.properties ] || [ -n "${PHONEBRIDGE_KEYSTORE:-}" ] || { echo "No signing credentials: refusing to make an unsigned/debug-signed 'release'" >&2; exit 1; }
"$GRADLE" --offline :app:assembleDebug :app:assembleRelease :app:bundleRelease -q 2>&1 | grep -v '^w:' || true
OUT="$ROOT/dist/$VER"; mkdir -p "$OUT"
cp app/build/outputs/apk/release/app-release.apk "$OUT/phonebridge-$VER.apk"
cp app/build/outputs/bundle/release/app-release.aab "$OUT/phonebridge-$VER.aab"
cp app/build/outputs/apk/debug/app-debug.apk "$OUT/phonebridge-$VER-debug.apk"
cp app/build/outputs/mapping/release/mapping.txt "$OUT/mapping-$VER.txt"
{
  echo "== apksigner"; "$BT/apksigner" verify --verbose --print-certs "$OUT/phonebridge-$VER.apk" 2>&1 | grep -E "Verifies|scheme|SHA-256 digest|signers"
  echo "== identity"; "$BT/aapt2" dump badging "$OUT/phonebridge-$VER.apk" | grep -E "^package|targetSdkVersion|sdkVersion:"
  echo "== aab signer"; keytool -printcert -jarfile "$OUT/phonebridge-$VER.aab" 2>/dev/null | grep -E "Owner|SHA256"
} | tee "$OUT/SIGNING_REPORT.txt"
(cd "$OUT" && sha256sum phonebridge-* mapping-* > SHA256SUMS && cat SHA256SUMS)
echo "Release artifacts in $OUT (dist/ is git-ignored)."
