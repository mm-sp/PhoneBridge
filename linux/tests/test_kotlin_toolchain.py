#!/usr/bin/env python3
"""COMMIT-K0 (docs/SECURITY_REMEDIATION_PLAN.md 20.4): proves the shared Kotlin runner `tests/_kotlin.py` works.

test_kotlin_runner_compiles_sync_core (INTEROP): the REAL Android `SyncCore.kt` and its `sync/Harness.kt` are compiled by
the runner's JVM-PURE entry point `compile_pure` (the registry and Android-reference rules are enforced first) and run on
the JVM; the Kotlin side validates commands made by the Python side and the Python validator accepts
the commands the Kotlin side makes, so both languages' code is exercised together. The same file demonstrates the runner's
failure behaviour (compile error, missing source, exit-code propagation, temporary-directory cleanup) and, for the
JVM-COMPILE tier, that a file naming an `android.*` class compiles only with `android.jar` and runs its non-framework path.
Without the toolchain a normal run prints `SKIP:`; `PHONEBRIDGE_STRICT=1` turns that into a failure.
Run: `python3 tests/test_kotlin_toolchain.py`"""
import contextlib
import glob
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import _kotlin as K  # noqa: E402
from phonebridge.screensync import commands as C  # noqa: E402
import test_android_sync as S  # noqa: E402  (only its vector list; its main() is guarded)

PASS = FAIL = 0
STRICT = os.environ.get("PHONEBRIDGE_STRICT") == "1"


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


@contextlib.contextmanager
def private_tempdir():
    """Points tempfile at a private directory and ALWAYS restores the previous global state, even if the body raises."""
    old = tempfile.tempdir
    d = tempfile.TemporaryDirectory()
    tempfile.tempdir = d.name
    try:
        yield d.name
    finally:
        tempfile.tempdir = old
        d.cleanup()


class NoCompiler:
    """Counts every subprocess started through the runner (the Kotlin compiler and the JVM are only ever started that way)."""

    def __enter__(self):
        self.calls = []
        self.real = K.subprocess.run

        def counting(*a, **k):
            self.calls.append(a[0] if a else k.get("args"))
            return self.real(*a, **k)
        K.subprocess.run = counting
        return self

    def __exit__(self, *exc):
        K.subprocess.run = self.real


_COUNTER = [0]


def reject(tc, tmp, label, names, why=None, harness=(), **kw):
    """compile_pure must refuse: exit 97, the expected diagnostic, NO subprocess started and NO output directory created."""
    _COUNTER[0] += 1
    out = tmp / f"rej{_COUNTER[0]}"
    with NoCompiler() as nc:
        c = K.compile_pure(tc, names, out, harness=harness, **kw)
    good = c.returncode == K.RUNNER_ERROR and not nc.calls and not out.exists() and (why is None or why in c.stderr)
    check(f"rejected, compiler not invoked, no output directory: {label}", good)
    if not good:
        print("       ", c.returncode, c.stderr[:300], nc.calls)


def main() -> int:
    try:
        tc = K.find(need_bc=True, need_android=True)
    except K.ToolchainError as exc:
        if STRICT:
            check(f"the Kotlin toolchain is available ({exc})", False)
            print(f"\n{PASS} passed, {FAIL} failed")
            return 1
        print(f"SKIP: {exc}")
        return 0
    print("global temporary state is restored even when the body raises (L3)")
    original_tempdir = tempfile.tempdir
    raised, inside, held = False, False, None
    try:
        with private_tempdir() as held:
            inside = tempfile.tempdir == held
            raise RuntimeError("deliberate failure inside the body")
    except RuntimeError:
        raised = True
    check("an exception inside private_tempdir() propagates, tempfile.tempdir is restored and the private directory is removed",
          raised and inside and tempfile.tempdir == original_tempdir and not Path(held).exists())

    with private_tempdir() as private_root:                  # every K.Workdir of this run is created here, restored in finally
        K.Workdir.created.clear()
        print(f"toolchain: Kotlin {tc.kotlin_version} from {tc.kotlin_lib}; org.json {os.path.basename(tc.json_jar)}; "
              f"{os.path.basename(tc.bcprov_jar)}; {tc.android_jar}")

        print("test_kotlin_runner_compiles_sync_core (INTEROP)")
        now = 1_700_000_000_000
        with K.Workdir() as tmp:
            tmp = Path(tmp)
            out = tmp / "out"
            c = K.compile_pure(tc, ["SyncCore.kt"], out, harness=[HERE / "sync/Harness.kt"])
            check("compile_pure compiles the real SyncCore.kt with its harness (exit 0)", c.ok)
            if not c.ok:
                print(c.stdout, c.stderr)
            errors = [l for l in c.stderr.splitlines() if l.startswith("error:") or ": error:" in l]
            check("...with no compiler errors", not errors)
            print("       compiler warnings:", sum(1 for l in c.stderr.splitlines() if "warning:" in l))
            vec, made = tmp / "vec.json", tmp / "out.json"
            vec.write_text(json.dumps(S.vectors(now)))
            r = K.run(tc, out, "HarnessKt", [vec, now, made])
            lines = r.stdout.splitlines()
            oks = sum(1 for l in lines if l.startswith("  ok"))
            fails = sum(1 for l in lines if l.startswith(" FAIL"))
            check(f"the Kotlin harness exits 0 and reports its own checks ({oks} ok, {fails} FAIL)", r.returncode == 0 and fails == 0 and oks >= 40)
            # the authoritative labels of test_android_sync.vectors: Kotlin must have REJECTED each hostile Python-made vector
            for label, verdict in (("replayed id", "duplicate"), ("other session", "wrong_session"), ("stale", "stale"), ("bad id chars", "malformed")):
                want = f"  ok  python vector '{label}': expect {verdict}, got {verdict}"
                check(f"Kotlin rejected the hostile Python-made vector '{label}' as {verdict}", want in lines)
            v = C.CommandValidator("pc", clock_ms=lambda: now)
            items = json.loads(made.read_text()) if made.is_file() else []
            check("Python accepts every command the Kotlin code made (both languages exercised together)",
                  len(items) > 0 and all(v.validate(i["payload"], "k" * 16).ok for i in items))
        check("the runner's temporary directory was removed", not tmp.exists())

        print("runner failure behaviour and error codes")
        with K.Workdir() as tmp:
            tmp = Path(tmp)
            bad = tmp / "Bad.kt"
            bad.write_text("fun main() { val x: Int = \"not an int\" }\n")
            c = K.compile(tc, [bad], tmp / "o1")
            check("a compile error gives the compiler's own non-zero exit (not the runner's code) and a diagnostic",
                  c.returncode not in (0, K.RUNNER_ERROR) and "error:" in c.stderr)
            c = K.compile(tc, [tmp / "Missing.kt"], tmp / "o2")
            check("a missing source file is a RUNNER_ERROR (97), never skipped", c.returncode == K.RUNNER_ERROR and "not found" in c.stderr)
            c = K.compile(tc, [], tmp / "o3")
            check("an empty file list is a RUNNER_ERROR (97)", c.returncode == K.RUNNER_ERROR)
            ex = tmp / "Exit.kt"
            ex.write_text("fun main() { System.exit(3) }\n")
            K.compile(tc, [ex], tmp / "o4")
            r = K.run(tc, tmp / "o4", "ExitKt")
            check("the JVM exit status is propagated unchanged (3)", r.returncode == 3)
            r = K.run(tc, tmp / "o4", "NoSuchKt")
            check("running a missing main class fails (non-zero)", r.returncode != 0)
        check("temporary directories are removed even after failures", not tmp.exists())

        print("compile_pure enforces the JVM-PURE tier (every rejection is checked to start NO subprocess, i.e. the compiler never ran)")
        with K.Workdir() as tmp:
            tmp = Path(tmp)
            src, tdir, other = tmp / "src", tmp / "tests", tmp / "other"
            for d in (src, tdir, other):
                d.mkdir()
            (src / "GoodPure.kt").write_text("package dev.phonebridge\nfun goodPure(): Int = 7\n")
            (src / "UsesAndroid.kt").write_text("package dev.phonebridge\nimport android.os.Build\nfun usesAndroid(): Int = 1\n")
            (src / "NamesAndroid.kt").write_text('package dev.phonebridge\nfun namesAndroid(): String = android.os.Build.MODEL\n')
            (src / "SplitAndroid.kt").write_text("package dev.phonebridge\nfun splitAndroid(): String = android\n    .os.Build.MODEL\n")
            (src / "OpenComment.kt").write_text("package dev.phonebridge\n/* never closed\nfun openComment(): Int = 1\n")
            (src / "OpenRaw.kt").write_text('package dev.phonebridge\nval raw = """never closed\n')
            (src / "Unregistered.kt").write_text("package dev.phonebridge\nfun unregistered(): Int = 3\n")        # pure, NOT registered
            (other / "Elsewhere.kt").write_text("fun elsewhere(): Int = 1\n")
            reg = tmp / "registry.txt"
            reg.write_text("# test registry\nGoodPure.kt\nUsesAndroid.kt\nNamesAndroid.kt\nSplitAndroid.kt\nOpenComment.kt\nOpenRaw.kt\nMissingButRegistered.kt\n")
            kw = dict(registry=reg, src_dir=src, tests_dir=tdir)
            out = [0]

            def rejected(label, names, *, harness=(), why=None, **extra):
                out[0] += 1
                o = tmp / f"out{out[0]}"
                with NoCompiler() as nc:
                    c = K.compile_pure(tc, names, o, harness=harness, **{**kw, **extra})
                good = c.returncode == K.RUNNER_ERROR and not nc.calls and not o.exists() and (why is None or why in c.stderr)
                check(f"rejected, compiler not invoked: {label}", good)
                if not good:
                    print("       ", c.returncode, c.stderr[:300], nc.calls)

            # --- a valid registered file and a valid test-owned harness still work
            c = K.compile_pure(tc, ["GoodPure.kt"], tmp / "ok1", **kw)
            check("a registered pure Kotlin file compiles", c.ok)
            (tdir / "Harness.kt").write_text("import dev.phonebridge.goodPure\nfun main() { println(\"good=\" + goodPure()) }\n")
            c = K.compile_pure(tc, ["GoodPure.kt"], tmp / "ok2", harness=[tdir / "Harness.kt"], **kw)
            r2 = K.run(tc, tmp / "ok2", "HarnessKt") if c.ok else None
            check("a valid test-owned harness inside the tests directory compiles and runs through compile_pure", c.ok and r2.ok and r2.stdout.strip() == "good=7")
            # --- names
            rejected("an UNREGISTERED Kotlin file with no Android reference", ["Unregistered.kt"], why="Unregistered.kt")
            rejected("a REGISTERED file with an android import", ["UsesAndroid.kt"], why="references the Android framework")
            rejected("a REGISTERED file naming android.os.Build without an import", ["NamesAndroid.kt"], why="references the Android framework")
            rejected("a REGISTERED file with android split across lines", ["SplitAndroid.kt"], why="references the Android framework")
            rejected("a REGISTERED file with an unterminated block comment", ["OpenComment.kt"], why="unterminated block comment")
            rejected("a REGISTERED file with an unterminated raw string", ["OpenRaw.kt"], why="unterminated raw string")
            rejected("an empty list", [], why="no files")
            rejected("a registered but missing file", ["MissingButRegistered.kt"], why="does not exist")
            rejected("a path instead of a registry name", ["../src/GoodPure.kt"])
            rejected("a bare string instead of a list", "GoodPure.kt", why="names must be a list or tuple")
            # --- harness
            (tdir / "Bad.kt").write_text("import android.util.Log\nfun main() {}\n")
            (tdir / "OpenHarness.kt").write_text("/* never closed\nfun main() {}\n")
            (tdir / "Dir.kt").mkdir()
            rejected("a harness with an Android reference (inside the tests directory)", ["GoodPure.kt"], harness=[tdir / "Bad.kt"], why="references the Android framework")
            rejected("a harness with an unterminated comment", ["GoodPure.kt"], harness=[tdir / "OpenHarness.kt"], why="unterminated block comment")
            rejected("a missing harness file", ["GoodPure.kt"], harness=[tdir / "Nope.kt"], why="does not exist")
            rejected("a directory as harness", ["GoodPure.kt"], harness=[tdir / "Dir.kt"], why="not a file")
            rejected("an UNREGISTERED production Kotlin file (no Android reference) passed as harness", ["GoodPure.kt"], harness=[src / "Unregistered.kt"], why="production source directory")
            rejected("a REGISTERED production file passed as harness", ["GoodPure.kt"], harness=[src / "GoodPure.kt"], why="production source directory")
            (tdir / "link_to_production.kt").symlink_to(src / "Unregistered.kt")
            rejected("a symlink inside the tests directory pointing at an unregistered production file", ["GoodPure.kt"], harness=[tdir / "link_to_production.kt"], why="production source directory")
            (tdir / "link_elsewhere.kt").symlink_to(other / "Elsewhere.kt")
            rejected("a symlink inside the tests directory pointing outside it (not production)", ["GoodPure.kt"], harness=[tdir / "link_elsewhere.kt"], why="outside the tests directory")
            rejected("a path with .. that escapes the tests directory", ["GoodPure.kt"], harness=[tdir / ".." / "src" / "Unregistered.kt"], why="production source directory")
            (tdir / "notkotlin.txt").write_text("x")
            rejected("a harness that is not a .kt file", ["GoodPure.kt"], harness=[tdir / "notkotlin.txt"], why="not a .kt file")
            rejected("harness given as a bare string", ["GoodPure.kt"], harness="Harness.kt", why="harness must be a list or tuple")
            # --- registry
            for label, text in (("a parent-traversal entry ../Evil.kt", "../Evil.kt\n"), ("a slash entry", "sub/X.kt\n"), ("a backslash entry", "sub\\X.kt\n"),
                                ("a duplicate entry", "GoodPure.kt\nGoodPure.kt\n"), ("duplicate basenames (case-insensitive)", "GoodPure.kt\ngoodpure.kt\n")):
                bad = tmp / "badreg.txt"
                bad.write_text(text)
                rejected(f"an invalid registry: {label}", ["GoodPure.kt"], registry=bad, why="invalid registry")
            # --- the REAL registry and the REAL directories
            with NoCompiler() as nc:
                c = K.compile_pure(tc, ["Crypto.kt"], tmp / "real1")
            check("rejected, compiler not invoked: with the REAL registry, a real but unregistered file (Crypto.kt)",
                  c.returncode == K.RUNNER_ERROR and "Crypto.kt" in c.stderr and not nc.calls)
            with NoCompiler() as nc:
                c = K.compile_pure(tc, ["SyncCore.kt"], tmp / "real2", harness=[K.ANDROID_SRC / "SyncCore.kt"])
            check("rejected, compiler not invoked: with the REAL directories, the registered production SyncCore.kt passed as harness",
                  c.returncode == K.RUNNER_ERROR and "production source directory" in c.stderr and not nc.calls)

        print("compile_pure source-boundary hardening (M1, hard links, registered symlinks, L1, L2, M3)")
        with K.Workdir() as tmp:
            tmp = Path(tmp)
            src, tdir, other = tmp / "src", tmp / "tests", tmp / "other"
            for d in (src, tdir, other):
                d.mkdir()
            (src / "GoodPure.kt").write_text("package dev.phonebridge\nfun goodPure(): Int = 7\n")
            (src / "Unreg.kt").write_text("package dev.phonebridge\nfun unreg(): Int = 3\n")
            (src / "Real.kt").write_text("package dev.phonebridge\nfun real(): Int = 5\n")
            (src / "TickAndroid.kt").write_text("package dev.phonebridge\nfun t(): String = `android`.os.Build.MODEL\n")
            (src / "TickOpen.kt").write_text("package dev.phonebridge\nval `never closed\n")
            (other / "Elsewhere.kt").write_text("fun elsewhere(): Int = 1\n")
            (src / "Link1.kt").symlink_to(other / "Elsewhere.kt")
            (src / "Link2.kt").symlink_to(K.ANDROID_SRC / "SyncCore.kt")
            (src / "Link3.kt").symlink_to(src / "Real.kt")
            reg = tmp / "registry.txt"
            reg.write_text("GoodPure.kt\nReal.kt\nLink1.kt\nLink2.kt\nLink3.kt\nTickAndroid.kt\nTickOpen.kt\n")
            kw = dict(registry=reg, src_dir=src, tests_dir=tdir)
            prod = K.ANDROID_SRC / "SecureSession.kt"        # a real production file with no Android reference, NOT registered
            check("precondition: the real production file used below exists and has no Android reference",
                  prod.is_file() and K.android_references(prod.read_text(encoding="utf-8")) == [])

            print("  M1: the production directory is excluded whatever the caller passes")
            reject(tc, tmp, "src_dir=<tmp> and tests_dir=<Android source directory> with a production file as harness", ["GoodPure.kt"],
                   harness=[prod], why="overlaps the Android production source", registry=reg, src_dir=src, tests_dir=K.ANDROID_SRC)
            reject(tc, tmp, "tests_dir located INSIDE the Android source directory", ["GoodPure.kt"], harness=[tdir / "x.kt"],
                   why="overlaps the Android production source", registry=reg, src_dir=src, tests_dir=K.ANDROID_SRC / "sub")
            reject(tc, tmp, "tests_dir that CONTAINS the Android source directory (the repository root)", ["GoodPure.kt"], harness=[prod],
                   why="overlaps the Android production source", registry=reg, src_dir=src, tests_dir=HERE.parents[1])
            (tmp / "linktests").symlink_to(K.ANDROID_SRC)
            reject(tc, tmp, "tests_dir that is a symlink to the Android source directory", ["GoodPure.kt"], harness=[prod],
                   why="overlaps the Android production source", registry=reg, src_dir=src, tests_dir=tmp / "linktests")
            reject(tc, tmp, "tests_dir equal to the source directory in use", ["GoodPure.kt"], harness=[src / "Unreg.kt"],
                   why="overlaps the source directory in use", registry=reg, src_dir=src, tests_dir=src)
            reject(tc, tmp, "tests_dir that contains the source directory in use", ["GoodPure.kt"], harness=[src / "Unreg.kt"],
                   why="overlaps the source directory in use", registry=reg, src_dir=src, tests_dir=tmp)
            reject(tc, tmp, "a real production file as harness with a custom src_dir (the real directory is still excluded)", ["GoodPure.kt"], harness=[prod],
                   why="production source directory", **kw)

            print("  hard links")
            os.link(src / "Unreg.kt", tdir / "hard.kt")
            reject(tc, tmp, "a hard link in the tests directory to an unregistered production file", ["GoodPure.kt"], harness=[tdir / "hard.kt"],
                   why="hard link to a production source file", **kw)
            # (A hard link to the REAL production file is deliberately not created: even a temporary link would change the
            #  link count and ctime of a repository file. The (device, inode) rule is the same code path as above.)
            shutil.copy(src / "Unreg.kt", tdir / "copy.kt")
            c = K.compile_pure(tc, ["GoodPure.kt"], tmp / "copy_out", harness=[tdir / "copy.kt"], **kw)
            check("DOCUMENTED LIMITATION: a COPY of a production file placed in the tests directory is an ordinary test file and is accepted", c.ok)

            print("  registered files that are symlinks (the exact validated path is the one compiled)")
            reject(tc, tmp, "a registered file that is a symlink to a file outside the source directory", ["Link1.kt"], why="a symlink is not allowed", **kw)
            reject(tc, tmp, "a registered file that is a symlink into the REAL Android production source", ["Link2.kt"],
                   why="symlink into the Android production source", **kw)
            reject(tc, tmp, "a registered file that is a symlink to another file inside the source directory", ["Link3.kt"], why="a symlink is not allowed", **kw)
            seen = []
            real_compile = K.compile
            K.compile = lambda tc_, srcs, *a, **k: (seen.append([str(x) for x in srcs]), real_compile(tc_, srcs, *a, **k))[1]
            try:
                c = K.compile_pure(tc, ["Real.kt"], tmp / "exact", **kw)
            finally:
                K.compile = real_compile
            check("the file compiled is exactly the resolved, validated path", c.ok and seen == [[str((src / "Real.kt").resolve())]])

            print("  L1: invalid inputs return 97 (never an exception) and start nothing")
            reject(tc, tmp, "a missing registry file", ["GoodPure.kt"], why="registry cannot be read", registry=tmp / "nope.txt", src_dir=src, tests_dir=tdir)
            (tmp / "latin.txt").write_bytes(b"\xff\xfe\n")
            reject(tc, tmp, "a registry that is not valid UTF-8", ["GoodPure.kt"], why="registry cannot be read", registry=tmp / "latin.txt", src_dir=src, tests_dir=tdir)
            reject(tc, tmp, "a registry path that is a directory", ["GoodPure.kt"], why="registry cannot be read", registry=tmp, src_dir=src, tests_dir=tdir)
            reject(tc, tmp, "a harness path with an embedded NUL byte", ["GoodPure.kt"], harness=["a\0b.kt"], why="cannot be checked", **kw)
            reject(tc, tmp, "a harness entry that is None", ["GoodPure.kt"], harness=[None], why="a harness entry must be a str or pathlib.Path, not NoneType", **kw)
            reject(tc, tmp, "a registry name with an embedded NUL byte", ["Good\0Pure.kt"], why="is not listed", **kw)

            print("  L-A: arguments of the wrong type are refused with 97 (no exception, no subprocess, no output directory)")
            ok_reg = dict(registry=reg, src_dir=src, tests_dir=tdir)
            for label, extra, why in (
                    ("registry given as a str", dict(registry=str(reg)), "registry must be None or a pathlib.Path, not str"),
                    ("registry given as an int", dict(registry=5), "registry must be None or a pathlib.Path, not int"),
                    ("registry given as bytes", dict(registry=bytes(reg)), "registry must be None or a pathlib.Path, not bytes"),
                    ("tests_dir given as an int", dict(tests_dir=5), "tests_dir must be None or a pathlib.Path, not int"),
                    ("tests_dir given as a str", dict(tests_dir=str(tdir)), "tests_dir must be None or a pathlib.Path, not str"),
                    ("src_dir given as a str", dict(src_dir=str(src)), "src_dir must be None or a pathlib.Path, not str"),
                    ("src_dir given as a list", dict(src_dir=[src]), "src_dir must be None or a pathlib.Path, not list"),
                    ("src_dir given as bytes", dict(src_dir=b"/tmp"), "src_dir must be None or a pathlib.Path, not bytes"),
                    ("bc given as a str", dict(bc="yes"), "bc must be a bool, not str")):
                reject(tc, tmp, label, ["GoodPure.kt"], why=why, **{**ok_reg, **extra})
            for label, entry, tname in (("an int", 5, "int"), ("bytes", b"x.kt", "bytes"), ("a float", 1.5, "float"), ("a dict", {}, "dict"), ("a set", set(), "set")):
                reject(tc, tmp, f"a harness entry that is {label}", ["GoodPure.kt"], harness=[entry],
                       why=f"a harness entry must be a str or pathlib.Path, not {tname}", **kw)
            for label, bad_names, tname in (("an int", 5, "int"), ("a dict", {"GoodPure.kt": 1}, "dict"), ("a set", {"GoodPure.kt"}, "set"), ("a str", "GoodPure.kt", "str")):
                reject(tc, tmp, f"names given as {label}", bad_names, why=f"names must be a list or tuple, not {tname}", **kw)
            reject(tc, tmp, "harness given as a dict", ["GoodPure.kt"], harness={"a": 1}, why="harness must be a list or tuple, not dict", **kw)
            here = sorted(os.listdir("."))
            with NoCompiler() as nc:
                results = [K.compile_pure(tc, ["GoodPure.kt"], bad_out, **kw) for bad_out in (None, 5, b"out", ["x"])]
            check("out_dir of the wrong type (None, int, bytes, list) is refused with 97, no subprocess, nothing created in the working directory",
                  all(c.returncode == K.RUNNER_ERROR and "out_dir must be a str or pathlib.Path" in c.stderr for c in results) and not nc.calls and sorted(os.listdir(".")) == here)

            print("  M-A: the production source tree must be fully inspectable (fail closed)")
            as_root = os.geteuid() == 0
            if as_root:
                print("       note: running as root, so permission bits cannot make a directory unreadable; the permission-based cases are skipped "
                      "and the injected-error cases below carry the fail-closed evidence")
            insp = "production source cannot be fully inspected"
            (src / "subdir").mkdir()
            (src / "subdir" / "Hidden.kt").write_text("package dev.phonebridge\nfun hidden(): Int = 9\n")
            os.link(src / "subdir" / "Hidden.kt", tdir / "hard_hidden.kt")
            (tdir / "Plain.kt").write_text("import dev.phonebridge.goodPure\nfun main() { println(goodPure()) }\n")
            if not as_root:
                os.chmod(src / "subdir", 0)
                try:
                    reject(tc, tmp, "a hard link to a file inside an UNREADABLE production subdirectory (custom src_dir)", ["GoodPure.kt"],
                           harness=[tdir / "hard_hidden.kt"], why=insp, **kw)
                    reject(tc, tmp, "even a plain valid harness: the whole request is refused while the tree cannot be inspected", ["GoodPure.kt"],
                           harness=[tdir / "Plain.kt"], why=insp, **kw)
                    reject(tc, tmp, "the same with no harness at all", ["GoodPure.kt"], why=insp, **kw)
                finally:
                    os.chmod(src / "subdir", 0o755)
                c = K.compile_pure(tc, ["GoodPure.kt"], tmp / "restored", harness=[tdir / "Plain.kt"], **kw)
                check("after the permission is restored the normal valid harness compiles again", c.ok)
                reject(tc, tmp, "a hard link to the same production file is refused again once readable (inode check preserved)", ["GoodPure.kt"],
                       harness=[tdir / "hard_hidden.kt"], why="hard link to a production source file", **kw)
                # an unreadable production Kotlin FILE: its metadata is still readable, so its inode is still recognised
                (src / "NoRead.kt").write_text("package dev.phonebridge\nfun noRead(): Int = 1\n")
                os.link(src / "NoRead.kt", tdir / "hard_noread.kt")
                os.chmod(src / "NoRead.kt", 0)
                try:
                    reject(tc, tmp, "a hard link to an UNREADABLE production Kotlin file (still recognised by its inode)", ["GoodPure.kt"],
                           harness=[tdir / "hard_noread.kt"], why="hard link to a production source file", **kw)
                finally:
                    os.chmod(src / "NoRead.kt", 0o644)
            # --- deterministic injected faults (valid for every user, including root)
            real_walk, real_stat = os.walk, os.stat
            real_root = K.ANDROID_SRC.resolve()

            def walk_fails_for_real(top, onerror=None, **k):
                if Path(top).resolve() == real_root and onerror:
                    onerror(PermissionError(13, "Permission denied", str(top)))
                    return iter(())
                return real_walk(top, onerror=onerror, **k)
            os.walk = walk_fails_for_real
            try:
                reject(tc, tmp, "INJECTED: the REAL ANDROID_SRC cannot be listed, although src_dir is a readable custom directory", ["GoodPure.kt"],
                       harness=[tdir / "Plain.kt"], why=insp, **kw)
                with NoCompiler() as nc:
                    c = K.compile_pure(tc, ["SyncCore.kt"], tmp / "defaults_out", harness=[K.HERE / "sync/Harness.kt"])
                check("INJECTED: with the default directories the same fault also refuses the normal SyncCore.kt build (97, no subprocess, no output)",
                      c.returncode == K.RUNNER_ERROR and insp in c.stderr and not nc.calls and not (tmp / "defaults_out").exists())
            finally:
                os.walk = real_walk

            def stat_fails_for_hidden(path, *a, **k):
                if str(path).endswith("Hidden.kt") and "subdir" in str(path):
                    raise OSError(5, "Input/output error", str(path))
                return real_stat(path, *a, **k)
            os.stat = stat_fails_for_hidden
            try:
                reject(tc, tmp, "INJECTED: the metadata of one production .kt file cannot be read (I/O error)", ["GoodPure.kt"],
                       harness=[tdir / "Plain.kt"], why=insp, **kw)
            finally:
                os.stat = real_stat
            c = K.compile_pure(tc, ["GoodPure.kt"], tmp / "after_faults", harness=[tdir / "Plain.kt"], **kw)
            check("after the injected faults are removed the normal valid harness compiles (the patches were undone)", c.ok and os.walk is real_walk and os.stat is real_stat)
            print("  _source_inodes directly")
            good = tmp / "inodes"
            (good / "a" / "b").mkdir(parents=True)
            (good / "a" / "b" / "X.kt").write_text("x")
            (good / "Y.kt").write_text("y")
            (good / "notes.txt").write_text("n")
            (good / "Dangling.kt").symlink_to(good / "missing_target")
            keys = K._source_inodes(good)
            check("_source_inodes returns the inode of every regular .kt file (nested too) and ignores other files and dangling links", len(keys) == 2)
            try:
                K._source_inodes(tmp / "does_not_exist")
                check("_source_inodes raises InspectionError for a missing root", False)
            except K.InspectionError as exc:
                check("_source_inodes raises InspectionError for a missing root, naming the path", "cannot be fully inspected" in str(exc) and "does_not_exist" in str(exc))
            if not as_root:
                os.chmod(good / "a", 0)
                try:
                    K._source_inodes(good)
                    check("_source_inodes raises InspectionError for an unreadable subdirectory", False)
                except K.InspectionError as exc:
                    check("_source_inodes raises InspectionError for an unreadable subdirectory", "cannot be fully inspected" in str(exc))
                finally:
                    os.chmod(good / "a", 0o755)

            print("  L2 / M3: backtick identifiers")
            reject(tc, tmp, "a registered file with `android`.os.Build (backtick-quoted package)", ["TickAndroid.kt"], why="references the Android framework", **kw)
            reject(tc, tmp, "a registered file with an unterminated backtick identifier", ["TickOpen.kt"], why="unterminated backtick identifier", **kw)

        print("JVM-COMPILE capability (android.jar on the classpath; only a non-framework path runs)")
        with K.Workdir() as tmp:
            tmp = Path(tmp)
            src = tmp / "UsesAndroid.kt"
            src.write_text("import android.os.Build\nfun pureFn(): Int = 20 + 22\nfun main() { println(\"pure=\" + pureFn()) }\n")
            c = K.compile(tc, [src], tmp / "o1")
            check("without android.jar the compile fails SPECIFICALLY with \"unresolved reference 'android'\" (the compiler's own message)",
                  c.returncode not in (0, K.RUNNER_ERROR) and "unresolved reference 'android'" in c.stderr.lower())
            c = K.compile(tc, [src], tmp / "o2", android=True)
            check("with android.jar it compiles", c.ok)
            r = K.run(tc, tmp / "o2", "UsesAndroidKt", android=True)
            check("its non-framework function runs and prints 'pure=42'", r.ok and r.stdout.strip() == "pure=42")
            fw = tmp / "FrameworkCall.kt"
            fw.write_text("import android.os.Build\nfun main() { println(Build.getRadioVersion()) }\n")
            c = K.compile(tc, [fw], tmp / "o3", android=True)
            check("the framework-calling file COMPILES against android.jar", c.ok)
            r = K.run(tc, tmp / "o3", "FrameworkCallKt", android=True)
            check("running it fails and stderr contains the stub's 'Stub!' message (a framework call can never pass silently)",
                  c.ok and r.returncode != 0 and "Stub!" in r.stderr)

        print("classpath order (documented in _kotlin.py)")
        order = tc.classpath(bc=True, android=True).split(":")
        check("order is kotlin-stdlib, org.json, bcprov, android.jar", order == [tc.stdlib, tc.json_jar, tc.bcprov_jar, tc.android_jar])
        first = next((e for e in order if K._zip_has(e, "org/json/JSONObject.class")), None)
        check("the FIRST classpath entry that provides org.json.JSONObject is the real org.json jar", first == tc.json_jar)
        print("       android.jar also ships org/json stubs:", K._zip_has(tc.android_jar, "org/json/JSONObject.class"))
        with K.Workdir() as tmp:
            tmp = Path(tmp)
            src = tmp / "Where.kt"
            src.write_text("fun main() { println(org.json.JSONObject().javaClass.protectionDomain.codeSource.location.path) }\n")
            c = K.compile(tc, [src], tmp / "o")
            r = K.run(tc, tmp / "o", "WhereKt", bc=True, android=True)
            check("at run time with android.jar on the classpath, JSONObject is loaded from the real org.json jar, not the stub",
                  c.ok and r.ok and r.stdout.strip() == tc.json_jar)

        print("temporary-directory cleanup after this run (all K.Workdir directories were created under a private temp root)")
        created = list(K.Workdir.created)
        leftovers = glob.glob(os.path.join(private_root, "pb-kotlin-*"))
        check(f"{len(created)} pb-kotlin-* directories were created during the run, and none of them exists now",
              len(created) >= 5 and all(not Path(c).exists() for c in created) and leftovers == [])

    check("after the run tempfile.tempdir is back to its original value", tempfile.tempdir == original_tempdir)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
