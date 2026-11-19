#!/usr/bin/env bash
# Makes an ENCRYPTED backup of the release keystore + its properties. You type the passphrase; it is never stored or printed.
#   tools/backup_keystore.sh <keystore.jks> <keystore.properties> <destination-dir>
# Run it once per backup medium (at least two, on different devices), then run tools/verify_keystore_backup.sh.
set -euo pipefail
# Non-interactive use (tests/CI only): export PHONEBRIDGE_BACKUP_PASS=...; otherwise openssl prompts on the terminal.
PASSARG=(); [ -z "${PHONEBRIDGE_BACKUP_PASS:-}" ] || PASSARG=(-pass env:PHONEBRIDGE_BACKUP_PASS)
[ $# -eq 3 ] || { echo "usage: $0 <keystore.jks> <keystore.properties> <destination-dir>" >&2; exit 2; }
KS="$(realpath "$1")"; PROPS="$(realpath "$2")"; DEST="$(realpath "$3")"   # absolute: tar -C is relative to the previous -C
[ -f "$KS" ] && [ -f "$PROPS" ] && [ -d "$DEST" ] || { echo "missing input or destination" >&2; exit 2; }
OUT="$DEST/phonebridge-signing-$(date +%Y%m%d-%H%M%S).tar.gz.enc"
[ ! -e "$OUT" ] || { echo "refusing to overwrite $OUT" >&2; exit 1; }
tar -cz -C "$(dirname "$KS")" "$(basename "$KS")" -C "$(dirname "$PROPS")" "$(basename "$PROPS")" \
  | openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt "${PASSARG[@]}" -out "$OUT"   # prompts for a passphrase on the terminal
chmod 600 "$OUT"
[ "$(stat -c %s "$OUT")" -gt 1000 ] || { echo "backup suspiciously small, deleting it" >&2; rm -f "$OUT"; exit 1; }
sha256sum "$OUT"
echo "Backup written. Verify it with: tools/verify_keystore_backup.sh $OUT <expected-sha256-fingerprint>"
