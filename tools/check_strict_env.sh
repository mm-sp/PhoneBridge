#!/usr/bin/env bash
# COMMIT-K0 pre-flight for the strict local test run (docs/SECURITY_REMEDIATION_PLAN.md, 20.1).
# Checks every item the Kotlin-backed gates need and exits NON-ZERO if any is missing or unusable, so a missing
# toolchain can never look like a pass. Read-only: it prints paths and versions but never environment values and
# installs or downloads nothing. It creates no files itself; the Python helper it calls (linux/tests/_kotlin.py) makes one
# temporary `pb-kotlin-*` directory under $TMPDIR (default /tmp) for the compiler proof and removes it before returning.
#
# HOSTILE-PATH POLICY (what is and is not claimed). The script itself runs NO external helper found through PATH: it uses
# only bash built-ins (no head, cut, grep, dirname, cat ...). The only programs it starts are `python3` and `java`, each
# by the ABSOLUTE path that `find_tool` found, and `xvfb-run` is only located, never run. `find_tool` searches only
# absolute PATH entries (empty, ".", relative and `~` entries are skipped, so nothing in the current directory is found),
# with pathname expansion switched off (`set -f`), so a PATH entry containing `*` or `?` is a literal directory name,
# exactly as in linux/tests/_kotlin.py. Python is started with `-I`, so a planted gi.py or venv.py in the current
# directory is not imported. Not claimed: what `java` or `python3` themselves execute once started.
#   tools/check_strict_env.sh
set -u
set -f                                   # no pathname expansion anywhere (PATH entries with * or ? stay literal)
_self="${BASH_SOURCE[0]}"
case "$_self" in */*) _dir="${_self%/*}" ;; *) _dir="." ;; esac
FAILS=0
# Tool lookup policy (the same as linux/tests/_kotlin.py): only ABSOLUTE PATH entries are searched. Empty, "." and relative
# entries would resolve in the current directory, so a program planted there is never found and never run.
find_tool() {
  local d IFS=':'
  for d in $PATH; do
    case "$d" in
      /*) if [ -f "$d/$1" ] && [ -x "$d/$1" ]; then printf '%s\n' "$d/$1"; return 0; fi ;;
    esac
  done
  return 1
}
ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; FAILS=$((FAILS + 1)); }

if ! ROOT="$(CDPATH= cd -- "$_dir/.." && pwd)"; then  # built-ins only: no dirname
  bad "could not resolve the PhoneBridge checkout root"
  exit 1
fi

echo "PhoneBridge strict-environment pre-flight"

# bash and python3
ok "bash ${BASH_VERSION}"
PY="$(find_tool python3 || true)"
if [ -n "$PY" ]; then
  if PYV="$("$PY" -I -c 'import sys; sys.exit(1) if sys.version_info < (3, 10) else print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)" &&
     [[ "$PYV" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    ok "python3 ${PYV} at ${PY}"
  else
    bad "python3 at ${PY} failed the version check (Python 3.10 or newer required)"
  fi
else
  bad "python3 not on PATH (absolute entries only)"
fi

# java: found through an absolute PATH entry only, must RUN (the exit status of `java -version` itself, not of a pipeline),
# and must print a recognisable version line
JAVA_BIN="$(find_tool java || true)"
if [ -z "$JAVA_BIN" ]; then
  bad "java not on PATH (absolute entries only)"
else
  JV="$("$JAVA_BIN" -version 2>&1)"; JRC=$?
  JVERSION=""
  version_pattern='^((openjdk|java) version "[0-9][A-Za-z0-9._+-]*")([[:space:]]|$)'
  while IFS= read -r line; do
    if [[ "$line" =~ $version_pattern ]]; then
      JVERSION="${BASH_REMATCH[1]}"
      break
    fi
  done <<< "$JV"
  if [ "$JRC" -eq 0 ] && [ -n "$JVERSION" ]; then
    ok "java at ${JAVA_BIN}: ${JVERSION}"
  else
    bad "java at ${JAVA_BIN} failed the version check (exit ${JRC})"
  fi
fi

# xvfb-run (GUI tests)
XVFB="$(find_tool xvfb-run || true)"
if [ -n "$XVFB" ]; then ok "xvfb-run at ${XVFB}"; else bad "xvfb-run not on PATH (absolute entries only)"; fi

# GTK 4, libadwaita, GStreamer typelibs, venv support
if [ -n "$PY" ] && "$PY" -I - >/dev/null 2>&1 <<'PY'
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Adw, Gst
PY
then ok "GTK 4, libadwaita and GStreamer typelibs import"; else bad "GTK 4 / libadwaita / GStreamer typelibs are not importable by python3"; fi
if [ -n "$PY" ] && "$PY" -I -c "import venv, ensurepip" >/dev/null 2>&1; then ok "python3 venv and ensurepip available (tools/run_tests.sh builds its venv)"; else bad "python3 venv/ensurepip unavailable"; fi

# Kotlin toolchain, org.json, bcprov, android.jar, and proof that the compiler executes (shared with the test runner)
if [ -n "$PY" ] && ! "$PY" -I "$ROOT/linux/tests/_kotlin.py" check; then
  bad "Kotlin toolchain check failed"
fi

if [ "$FAILS" -ne 0 ]; then
  echo "strict environment: INCOMPLETE ($FAILS problem(s)); a strict run cannot be trusted until they are fixed"
  exit 1
fi
echo "strict environment: complete"
