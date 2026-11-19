#!/usr/bin/env bash
# Decrypts a backup into a temporary directory (wiped afterwards) and checks that the certificate fingerprint matches.
#   tools/verify_keystore_backup.sh <backup.tar.gz.enc> <expected SHA-256 fingerprint, with or without colons>
set -euo pipefail
# Non-interactive use (tests/CI only): export PHONEBRIDGE_BACKUP_PASS=...; otherwise openssl prompts on the terminal.
PASSARG=(); [ -z "${PHONEBRIDGE_BACKUP_PASS:-}" ] || PASSARG=(-pass env:PHONEBRIDGE_BACKUP_PASS)
[ $# -eq 2 ] || { echo "usage: $0 <backup> <expected-sha256>" >&2; exit 2; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT; chmod 700 "$TMP"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 "${PASSARG[@]}" -in "$1" | tar -xz -C "$TMP"
KS="$(ls "$TMP"/*.jks)"; PW="$(grep '^storePassword=' "$TMP/keystore.properties" | cut -d= -f2-)"; AL="$(grep '^keyAlias=' "$TMP/keystore.properties" | cut -d= -f2-)"
GOT="$(keytool -list -v -keystore "$KS" -alias "$AL" -storepass "$PW" 2>/dev/null | sed -n 's/^ *SHA256: //p' | tr -d ':' | tr 'A-F' 'a-f')"
WANT="$(echo "$2" | tr -d ':' | tr 'A-F' 'a-f')"
[ "$GOT" = "$WANT" ] && echo "OK: backup is readable and the fingerprint matches" || { echo "MISMATCH (got $GOT)" >&2; exit 1; }
