#!/usr/bin/env python3
"""COMMIT-K0 (docs/SECURITY_REMEDIATION_PLAN.md 20.1, 20.4): the strict-environment pre-flight, the JVM-PURE registry and
the runner's selection rules.

* test_strict_local_environment_present: `tools/check_strict_env.sh` passes here, FAILS (non-zero, naming the missing
  items) when the toolchain is absent or `java` is broken, prints no environment values, never runs a `java` or `python3`
  found through the current directory or a relative PATH entry (shell and Python agree), and, in the successful run,
  creates a `pb-kotlin-*` temporary directory (seen while it runs) and removes it.
* test_pure_kotlin_files_import_no_android: `SyncCore.kt` is registered, every registered file exists and references no
  `android.*` / `androidx.*` class; the lexical scanner has its own regression suite (see `_kotlin._blank_noncode` for the
  rules it supports and its limits).
* runner selection rules (no toolchain needed): android.jar choice, jar versions, Gradle lib rule, PATH handling, keyed
  diagnostics, distinct runner error code, registry validation, scanner errors.
Without the toolchain a normal run prints `SKIP:` for the machine-specific check (as the other Kotlin-backed tests);
`PHONEBRIDGE_STRICT=1` makes a missing toolchain a FAILURE. Run: `python3 tests/test_strict_environment.py`"""
import glob
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _kotlin as K  # noqa: E402

SCRIPT = HERE.parents[1] / "tools/check_strict_env.sh"
STRICT = os.environ.get("PHONEBRIDGE_STRICT") == "1"
PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def run_script(env_extra=None, tmpdir=None, cwd=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    if tmpdir:
        env["TMPDIR"] = tmpdir
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=600, cwd=cwd)


def run_script_watching(env_extra, tmpdir):
    """Runs the script while polling `tmpdir` every 10 ms; returns (completed process, whether a pb-kotlin-* directory was seen)."""
    import time
    env = dict(os.environ)
    env.update(env_extra)
    env["TMPDIR"] = tmpdir
    proc = subprocess.Popen(["bash", str(SCRIPT)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    seen = False
    while proc.poll() is None:
        seen = seen or bool(pb_dirs(tmpdir))
        time.sleep(0.01)
    out, err = proc.communicate()
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err), seen


def pb_dirs(d):
    return glob.glob(os.path.join(d, "pb-kotlin-*"))


def fake_tool(directory, name, body):
    p = Path(directory) / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def fake_java(directory, body):
    return fake_tool(directory, "java", body)


# Items of the pre-flight that exist only where the Kotlin/Gradle/Android toolchain is installed (the existing CI job installs
# neither). Everything else (bash, python3, java, xvfb-run, the GTK typelibs, venv support) is REQUIRED in every mode.
OPTIONAL_TOOLCHAIN_ITEMS = ("Gradle Kotlin compiler", "org.json", "bcprov", "android.jar", "the Kotlin compiler executes")
_BASELINE = {}


def fail_lines(out: str) -> list:
    return sorted(l.strip() for l in out.splitlines() if l.lstrip().startswith("FAIL "))


def baseline():
    """The pre-flight on this machine with the REAL, unmodified PATH: (returncode, output, FAIL lines, complete?). Computed once."""
    if not _BASELINE:
        with tempfile.TemporaryDirectory() as t:
            b = run_script({}, t)
        out = b.stdout + b.stderr
        _BASELINE.update(rc=b.returncode, out=out, fails=fail_lines(out), complete=b.returncode == 0 and "strict environment: complete" in out)
    return _BASELINE


def check_preflight_unchanged_by_hostile_path(label, r, out):
    """The hostile-PATH run must reach EXACTLY the verdict of the baseline run, so a planted program can neither break nor
    mask anything.
    * baseline complete (full toolchain): the hostile run must also complete with no FAIL line (as before);
    * baseline incomplete: STRICT mode fails (a strict run needs the full toolchain); non-strict mode accepts only toolchain
      items (Gradle/Kotlin, org.json, bcprov, android.jar) as missing; a missing REQUIRED base tool (java, python3,
      xvfb-run, typelibs, venv) is a failure in every mode, and the hostile run must have the same exit status and the same
      FAIL lines as the baseline: any additional or missing failure is reported."""
    b = baseline()
    if b["complete"]:
        check(f"{label}: the pre-flight still completes normally with the real tools", r.returncode == 0 and "strict environment: complete" in out and not fail_lines(out))
        return
    if STRICT:
        check(f"{label}: STRICT mode requires a complete pre-flight on this machine ({len(b['fails'])} item(s) missing)", False)
        return
    required_missing = [l for l in b["fails"] if not any(k in l for k in OPTIONAL_TOOLCHAIN_ITEMS)]
    check(f"{label}: non-strict, incomplete toolchain: only toolchain items are missing, no required base tool", not required_missing)
    check(f"{label}: non-strict, incomplete toolchain: the hostile-PATH run has the SAME exit status and the SAME FAIL lines as the baseline run",
          r.returncode == b["rc"] and fail_lines(out) == b["fails"] and "INCOMPLETE" in out)


def part_environment():
    print("test_strict_local_environment_present")
    check("tools/check_strict_env.sh exists and is executable", SCRIPT.is_file() and os.access(SCRIPT, os.X_OK))
    with tempfile.TemporaryDirectory() as empty_home, tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        neg = run_script({"HOME": empty_home, "PB_CANARY_SECRET": "canary-value-123"}, tmp1)
        out = neg.stdout + neg.stderr
        check("pre-flight exits non-zero when the Kotlin toolchain is absent", neg.returncode != 0)
        check("...and reports FAIL for the Kotlin compiler, org.json, bcprov and android.jar",
              all(any(k in l and "FAIL" in l for l in out.splitlines()) for k in ("Gradle Kotlin compiler", "org.json", "bcprov", "android.jar")))
        check("...and says the environment is incomplete", "INCOMPLETE" in out)
        check("pre-flight never prints environment values", "canary-value-123" not in out)
        pos, seen = run_script_watching({"PB_CANARY_SECRET": "canary-value-123"}, tmp2)
        out = pos.stdout + pos.stderr
        if pos.returncode != 0 and not STRICT:
            print("SKIP: strict environment incomplete on this machine (run with PHONEBRIDGE_STRICT=1 to make this a failure)")
            print(out)
        else:
            check("pre-flight passes on this machine (exit 0, 'complete', no FAIL line)",
                  pos.returncode == 0 and "strict environment: complete" in out and "FAIL" not in out)
            check("pre-flight proved the compiler executes (not only that files exist)", "the Kotlin compiler executes: compiled and ran a program" in out)
            check("pre-flight prints no environment values on success", "canary-value-123" not in out)
            check("successful run: a pb-kotlin-* directory WAS created in the given TMPDIR while the compiler proof ran", seen)
            check("successful run: no pb-kotlin-* directory remains afterwards (other temporary files are not claimed)", pb_dirs(tmp2) == [])
            if pos.returncode != 0:
                print(out)

    print("java check (deliberately broken java executables placed first on PATH)")
    cases = [("exit 3 but prints a version line", 'echo "openjdk version \\"17.0.1\\"" >&2; exit 3', False),
             ("exit 0 but prints no version", 'echo "hello"; exit 0', False),
             ("exit 0 and prints nothing", "exit 0", False),
             ("exit 0 and prints a version line", 'echo "openjdk version \\"17.0.1\\"" >&2; exit 0', True)]
    for label, body, good in cases:
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as t:
            fake_java(d, body)
            r = run_script({"PATH": d + os.pathsep + os.environ["PATH"], "PB_CANARY_SECRET": "canary-value-123"}, t)
            out = r.stdout + r.stderr
            line = [l for l in out.splitlines() if f"java at {d}/java" in l]
            if good:
                check(f"shell java check accepts: {label}", bool(line) and line[0].lstrip().startswith("ok"))
            else:
                check(f"shell java check FAILS: {label}", bool(line) and line[0].lstrip().startswith("FAIL") and r.returncode != 0)
            check(f"   ...no environment values printed ({label})", "canary-value-123" not in out)
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as t:
        fake_java(d, 'echo "openjdk version \\"17.0.1\\"" >&2; exit 3')
        ok, detail = K.java_version(str(Path(d) / "java"))
        check("python java_version() also rejects a java that exits non-zero", ok is False and "exit 3" in detail)
    print("relative PATH entries: fake java and python3 in the current directory are never executed (shell and Python agree)")
    for label, lead in (("empty first entry", ""), ("'.' entry", "."), ("relative directory entry", "bin")):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as t:
            marker_j, marker_p = Path(d) / "RAN_JAVA", Path(d) / "RAN_PYTHON"
            fake_java(d, f'touch "{marker_j}"; echo "openjdk version \\"17.0.1\\"" >&2; exit 0')
            fake_tool(d, "python3", f'touch "{marker_p}"; exit 0')
            (Path(d) / "bin").mkdir()
            fake_java(Path(d) / "bin", f'touch "{marker_j}"; echo "openjdk version \\"17.0.1\\"" >&2; exit 0')
            fake_tool(Path(d) / "bin", "python3", f'touch "{marker_p}"; exit 0')
            env = {"PATH": lead + os.pathsep + os.environ["PATH"]}
            r = run_script(env, t, cwd=d)
            out = r.stdout + r.stderr
            py = subprocess.run([sys.executable, str(HERE / "_kotlin.py"), "check"], capture_output=True, text=True, cwd=d,
                                env={**os.environ, **env, "TMPDIR": t}, timeout=600)
            shell_java = [l for l in out.splitlines() if l.lstrip().startswith("ok   java at")]
            py_java = [l for l in py.stdout.splitlines() if "java on PATH" in l]
            real = K._java_on_path()
            check(f"{label}: neither the shell pre-flight nor the Python check executed the fake java or the fake python3",
                  not marker_j.exists() and not marker_p.exists())
            check(f"{label}: shell and Python both used the same real java ({real}) and both report it OK",
                  bool(shell_java) and bool(py_java) and real in shell_java[0] and real in py_java[0] and py_java[0].lstrip().startswith("ok"))
            check_preflight_unchanged_by_hostile_path(f"{label} (relative PATH)", r, out)
            check(f"{label} (relative PATH): the Python check agrees with the baseline verdict",
                  (py.returncode == 0) == baseline()["complete"])


def part_hostile_path():
    print("hostile PATH: planted helper programs and modules are never executed or imported; a glob PATH entry stays literal")
    helpers = ["head", "cut", "grep", "dirname", "cat", "sed", "awk", "tr", "basename", "printf", "env", "sleep"]
    for label, lead in (("empty first entry", ""), ("'.' entry", "."), ("relative directory entry", "bin")):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as t:
            dd = Path(d)
            (dd / "bin").mkdir()
            for where in (dd, dd / "bin"):
                for name in helpers:
                    fake_tool(where, name, f'touch "{d}/RAN_{name}"; exec /usr/bin/{name} "$@"')
                fake_java(where, f'touch "{d}/RAN_java"; echo "openjdk version \\"17.0.1\\"" >&2; exit 0')
                fake_tool(where, "python3", f'touch "{d}/RAN_python3"; exit 0')
                fake_tool(where, "xvfb-run", f'touch "{d}/RAN_xvfb-run"; exit 0')
            for mod in ("gi", "venv", "ensurepip", "re", "zipfile"):
                (dd / f"{mod}.py").write_text(f'open("{d}/IMPORTED_{mod}", "w").close()\n')
            env = {"PATH": lead + os.pathsep + os.environ["PATH"]}
            r = run_script(env, t, cwd=d)
            out = r.stdout + r.stderr
            ran = sorted(x for x in os.listdir(d) if x.startswith(("RAN_", "IMPORTED_")))
            check(f"{label}: no planted helper (head, cut, grep, dirname, cat, ...), java, python3 or xvfb-run was executed and no planted module imported", ran == [])
            check_preflight_unchanged_by_hostile_path(f"{label}", r, out)
    # the script itself must not name a PATH-resolved external helper
    code = [l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#")]
    import re as _re
    check("the script's code uses no external helper command by name (head, cut, grep, dirname, cat, sed, awk, tr, basename)",
          not any(_re.search(r"(^|[|;&(`$]\s*)(head|cut|grep|dirname|cat|sed|awk|tr|basename)\s", l) for l in code))
    print("glob PATH entry")
    with tempfile.TemporaryDirectory() as g, tempfile.TemporaryDirectory() as t:
        gg = Path(g)
        for sub in ("a", "b"):
            (gg / sub).mkdir()
        fake_java(gg / "b", f'touch "{g}/RAN_GLOBJAVA"; echo "openjdk version \\"17.0.1\\"" >&2; exit 0')
        env = {"PATH": g + "/*" + os.pathsep + g + "/?" + os.pathsep + os.environ["PATH"]}
        r = run_script(env, t)
        py = subprocess.run([sys.executable, "-I", str(HERE / "_kotlin.py"), "check"], capture_output=True, text=True,
                            env={**os.environ, **env, "TMPDIR": t}, timeout=600)
        real = K._java_on_path()
        sj = [l for l in r.stdout.splitlines() if l.lstrip().startswith("ok   java at")]
        pj = [l for l in py.stdout.splitlines() if "java on PATH" in l]
        check("a PATH entry containing * or ? is literal: the planted java in a matching subdirectory was never executed", not (gg / "RAN_GLOBJAVA").exists())
        check(f"shell and Python select the same real java ({real}) and both report it OK",
              bool(sj) and bool(pj) and real in sj[0] and real in pj[0] and pj[0].lstrip().startswith("ok"))
        check_preflight_unchanged_by_hostile_path("glob PATH entries", r, r.stdout + r.stderr)
        old = os.environ["PATH"]
        try:
            os.environ["PATH"] = env["PATH"]
            check("in-process: K._java_on_path() also ignores the glob entries", K._java_on_path() == real and not (gg / "RAN_GLOBJAVA").exists())
        finally:
            os.environ["PATH"] = old


def part_registry_and_detector():
    print("test_pure_kotlin_files_import_no_android")
    files = K.pure_files()
    check("SyncCore.kt is registered (membership, not position)", "SyncCore.kt" in [f.name for f in files])
    check("every registered file exists under the Android source directory", all(f.is_file() for f in files))
    for f in files:
        refs = K.android_references(f.read_text(encoding="utf-8")) if f.is_file() else ["missing"]
        check(f"{f.name} references no android.* class", refs == [])
        if refs:
            print("       ", refs[:3])

    print("android-reference scanner: must be flagged")
    flagged = {
        "android.R.string.ok (uppercase segment)": "val s = android.R.string.ok\n",
        "android.Manifest.permission.CAMERA": "val p = android.Manifest.permission.CAMERA\n",
        "a URL string, then android.os.Build.MODEL on the same line": 'val u = "http://example.com/x"; val b = android.os.Build.MODEL\n',
        "import android.util.Log": "import android.util.Log\n",
        "import androidx.core.content.FileProvider": "import androidx.core.content.FileProvider\n",
        "import android.* (wildcard)": "import android.*\n",
        "import with alias": "import android.util.Log as L\n",
        "lowercase segments android.os.Build": "val v = android.os.Build.VERSION.SDK_INT\n",
        "androidx with an uppercase segment": "val c = androidx.Compose.Foo()\n",
        "code after a nested block comment": "/* a /* b */ still comment */ val x = android.os.Build.MODEL\n",
        "code after an escaped-quote string": 'val s = "a\\"b" + android.os.Build.MODEL\n',
        "code after a char literal of a quote": "val q = '\"'; val b = android.os.Build.MODEL\n",
        "code after an escaped char literal": "val q = '\\''; val b = android.os.Build.MODEL\n",
        "reference inside a ${} template": 'val s = "model=${android.os.Build.MODEL}"\n',
        "reference inside a template inside a raw string": 'val s = """x ${android.os.Build.MODEL} y"""\n',
        "code after a raw string": 'val s = """a "quoted" // not a comment"""; val b = android.os.Build.MODEL\n',
        "backtick-quoted package: `android`.os.Build.MODEL": "val b = `android`.os.Build.MODEL\n",
        "backtick-quoted import: import `android`.os.Build": "import `android`.os.Build\n",
        "backtick-quoted androidx": "import `androidx`.core.content.FileProvider\n",
        "reference on a line after a multi-line raw string": 'val s = """\nline // /* \n"""\nval b = android.os.Build.MODEL\n',
        "reference after a // inside a string on an earlier statement": 'val a = "//"; val b = android.os.Build.MODEL\n',
    }
    for label, src in flagged.items():
        check(f"flagged: {label}", K.android_references(src) != [])
    r = K.android_references('val s = """\nline // /* \n"""\nval b = android.os.Build.MODEL\nval c = 1\n')
    check("the reported line is the original source line (line numbers survive blanking)", r == ["val b = android.os.Build.MODEL"])

    print("android-reference scanner: multi-line references, line numbers, unterminated input")
    ML = [("name, newline, then .os.Build", "val b = android\n    .os.Build.MODEL\n"),
          ("name., newline, then os.Build", "val b = android.\n    os.Build\n"),
          ("androidx split over lines", "val b = androidx\n  .core.X\n"),
          ("a comment between the name and the dot", "val b = android /* c */\n // d\n .os.Build\n"),
          ("CRLF endings with a split name", "// c\r\nval b = android\r\n    .os.Build\r\n")]
    for label, src in ML:
        check(f"flagged across line breaks: {label}", K.android_references(src) != [])
    check("a multi-line hit is reported on the line where the name starts",
          K.android_reference_lines("fun f() {}\nval b = android\n    .os.Build.MODEL\n") == [(2, "val b = android")])
    ff = "/* a\x0c b */\n// c \x0b d\nval b = android.os.Build.MODEL\n"
    check("a form feed / vertical tab inside comments does not shift the reported line (still line 3, correct text)",
          K.android_reference_lines(ff) == [(3, "val b = android.os.Build.MODEL")])
    check("U+2028 and a lone CR inside a comment do not shift the reported line either",
          K.android_reference_lines("/* x\u2028y\rz */\nval b = android.os.Build.MODEL\n") == [(2, "val b = android.os.Build.MODEL")])
    check("blanked and original text have identical \\n line boundaries", all(
          K._blank_noncode(t).count("\n") == t.count("\n") and len(K._blank_noncode(t)) == len(t) for t in (ff, "a\r\nb\r\n", '"""a\nb"""\n')))
    for label, src in (("an unterminated block comment", "val a = 1\n/* never closed\nval b = android.os.Build.MODEL\n"),
                       ("an unterminated nested block comment", "/* a /* b */ still open\nval b = 1\n"),
                       ("an unterminated raw string", 'val a = 1\nval s = """never closed\nval b = android.os.Build.MODEL\n'),
                       ("an unterminated ${ template", 'val s = "x ${f(\nval b = 1\n'),
                       ("an unterminated backtick identifier at end of file", "val a = 1\nval `never closed"),
                       ("a backtick identifier broken by a line break", "val `broken\nname` = android.os.Build.MODEL\n")):
        try:
            K.android_references(src)
            check(f"{label} raises ScannerError", False)
        except K.ScannerError as exc:
            check(f"{label} raises ScannerError ({exc})", True)

    print("android-reference scanner: must NOT be flagged")
    clean = {
        "line comment": "// android.os.Build is not used\nfun f() {}\n",
        "block comment": "/* import android.util.Log */\nfun f() {}\n",
        "nested block comment with fake imports": "/* a /* import android.util.Log */ import android.os.Build */\nfun f() {}\n",
        "string literal": 'val s = "android.os.Build"\n',
        "string with a URL and android": 'val s = "http://example.com/android.os.Build"\n',
        "string with an escaped quote before the mention": 'val s = "say \\"hi\\" import android.util.Log"\n',
        "raw string with an import and comment markers": 'val s = """import android.util.Log // android.os.Build /* x */"""\n',
        "multi-line raw string": 'val s = """\nimport android.util.Log\nandroid.os.Build.MODEL\n"""\nfun f() {}\n',
        "char literal": "val c = 'a'\n",
        "package dev.phonebridge": "package dev.phonebridge\nimport org.json.JSONObject\n",
        "qualified name that merely contains 'android'": "val x = dev.phonebridge.android.Foo()\n",
        "identifier that starts with android": "val androidVersion = 1\n",
        "capitalised word Android alone": "val s = Android\n",
        "plain mention in a doc comment": "/** Android.os is the platform */\nfun f() {}\n",
        "backtick identifier": "val `android.os.Build` = 1\n",
        "backtick identifier that merely contains android": "val `android os` = 1\nfun `androidVersion`() {}\n",
        "backtick identifier, different case": "val `Android`.x = 1\n",
        "backtick function name with an apostrophe": "fun `it's fine`() {}\n",
        "backtick identifier unrelated to android": "val `class` = 1\n",
    }
    for label, src in clean.items():
        hits = K.android_references(src)
        check(f"not flagged: {label}", hits == [])
        if hits:
            print("       ", hits)


def part_selection_rules():
    print("runner selection rules (no toolchain needed)")
    base = "/h/Android/Sdk/platforms"
    cands = [f"{base}/android-34/android.jar", f"{base}/android-36/android.jar", f"{base}/android-9/android.jar",
             f"{base}/android-VanillaIceCream/android.jar", f"{base}/android-36-ext5/android.jar", f"{base}/android-37-preview/android.jar"]
    check("android.jar: highest NUMERIC level (36) is chosen, not a preview or extension directory",
          K.select_android_jar(cands) == f"{base}/android-36/android.jar")
    check("android.jar: numeric, not lexical (android-10 beats android-9)",
          K.select_android_jar([f"{base}/android-9/android.jar", f"{base}/android-10/android.jar"]) == f"{base}/android-10/android.jar")
    check("android.jar: only preview directories give no selection", K.select_android_jar([f"{base}/android-VanillaIceCream/android.jar"]) is None)
    jj = ["/c/org.json/json/20231013/a/json-20231013.jar", "/c/org.json/json/20240303/b/json-20240303.jar", "/c/org.json/json/20240303/b/json-20240303-sources.jar"]
    check("org.json: highest version, sources jar ignored", K.select_jar(jj, r"json-(\d+)\.jar") == jj[1])
    bb = [f"/c/bc/{v}/h/bcprov-jdk18on-{v}.jar" for v in ("1.9", "1.10", "1.78.1")] + ["/c/bc/1.99/h/bcprov-jdk18on-1.99-sources.jar"]
    check("bcprov: numeric version order (1.78.1 beats 1.10 beats 1.9), sources jar ignored",
          K.select_jar(bb, r"bcprov-jdk18on-([\d.]+)\.jar") == bb[2])
    libs = ["/g/gradle-8.14.7-all/x/gradle-8.14.7/lib", "/g/gradle-8.14-all/y/gradle-8.14/lib", "/g/gradle-8.9-bin/z/gradle-8.9/lib"]
    check("Gradle Kotlin lib: the lexically FIRST path (the rule of the existing tests, libs[0])", K.select_kotlin_lib(libs) == sorted(libs)[0] == libs[1])
    # the existing tests' own expressions, copied verbatim, must agree with the runner on this machine
    existing_libs = sorted(l for l in glob.glob(os.path.expanduser("~/.gradle/wrapper/dists/gradle-*/*/gradle-*/lib"))
                           if glob.glob(f"{l}/kotlin-stdlib-2*.jar"))
    if existing_libs:
        check("runner and the existing tests select the same Kotlin lib on this machine", K._locate()["lib"] == existing_libs[0])
        ex_json = [j for j in sorted(glob.glob(os.path.expanduser("~/.gradle/caches/modules-2/files-2.1/org.json/json/*/*/json-*.jar"))) if "sources" not in j]
        ex_bc = [j for j in sorted(glob.glob(os.path.expanduser("~/.gradle/caches/modules-2/files-2.1/org.bouncycastle/bcprov-jdk18on/*/*/bcprov-jdk18on-*.jar"))) if "sources" not in j]
        ex_sdk = sorted(glob.glob(os.path.expanduser("~/Android/Sdk/platforms/android-*/android.jar")))
        L = K._locate()
        check("runner and the existing tests select the same org.json, bcprov and android.jar on this machine",
              (not ex_json or L["json"] == ex_json[-1]) and (not ex_bc or L["bc"] == ex_bc[-1]) and (not ex_sdk or L["android"] == ex_sdk[-1]))
    # PATH handling
    with tempfile.TemporaryDirectory() as d:
        fake_java(d, "exit 0")
        old, cwd = os.environ.get("PATH", ""), os.getcwd()
        try:
            os.chdir(d)
            os.environ["PATH"] = os.pathsep.join(["", ".", "relative/dir", old])
            found = K._java_on_path()
            check("PATH: empty, '.' and relative entries are ignored (a java in the current directory is never used)",
                  found is None or os.path.isabs(found) and not found.startswith(d))
            os.environ["PATH"] = os.pathsep.join(["", d, old])
            check("PATH: an absolute entry is used", K._java_on_path() == os.path.join(d, "java"))
        finally:
            os.chdir(cwd)
            os.environ["PATH"] = old
    print("registry validation")
    with tempfile.TemporaryDirectory() as t:
        def names_of(text):
            f = Path(t) / "r.txt"
            f.write_text(text)
            try:
                return K.registry_names(f)
            except K.RegistryError as exc:
                return str(exc)
        check("valid entries, comments and blank lines: returned in file order", names_of("# c\nB.kt\n\nA.kt  # note\nC.kt\n") == ["B.kt", "A.kt", "C.kt"])
        for label, text in (("parent traversal ../Evil.kt", "../Evil.kt\n"), ("embedded ..", "a..b.kt\n"), ("a lone ..", "..\n"),
                            ("a slash path sub/X.kt", "sub/X.kt\n"), ("an absolute path", "/etc/X.kt\n"), ("a backslash path", "sub\\X.kt\n"),
                            ("a duplicate entry", "A.kt\nB.kt\nA.kt\n"), ("duplicate basenames differing only by case", "SyncCore.kt\nsynccore.kt\n"),
                            ("a non-.kt entry", "Evil.sh\n")):
            res = names_of(text)
            check(f"registry rejects {label}", isinstance(res, str) and ("line" in res))
    check("the real registry validates and lists SyncCore.kt", "SyncCore.kt" in K.registry_names())
    d = K.diagnose()
    check("diagnose() is keyed by item name (java, kotlin, json, bcprov, android), not by position",
          isinstance(d, dict) and set(d) == {"java", "kotlin", "json", "bcprov", "android"})
    check("the runner's own error code (97) is not a Kotlin compiler exit code (0, 1, 2, 3) and differs from the timeout code 124",
          K.RUNNER_ERROR == 97 and K.RUNNER_ERROR not in (0, 1, 2, 3, K.TIMEOUT))


def main() -> int:
    part_environment()
    part_hostile_path()
    part_registry_and_detector()
    part_selection_rules()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
