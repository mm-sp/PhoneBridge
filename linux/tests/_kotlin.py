"""COMMIT-K0: the one shared routine that locates the Kotlin toolchain and compiles and runs Kotlin test files on a plain JVM.

Plan: docs/SECURITY_REMEDIATION_PLAN.md sections 20.1 and 20.4. This is the compile-and-run step of
`test_android_sync.py` (`-no-stdlib -no-reflect`, Gradle's bundled compiler) parameterised by a file list. It only
reads files from `~/.gradle` and `~/Android/Sdk`; it downloads and installs nothing; it never edits a source file.

TIERS (20.4) as this module supports them
* JVM-PURE     compile_pure(tc, names, out)       **the only entry point a JVM-PURE test may use**: it refuses any file that is
                                                   not listed in `pure_kotlin_files.txt` and any file with an Android reference
* JVM-COMPILE  compile(tc, files, out, android=True)  android.jar on the classpath; stand-ins supply the rest; a call that reaches
                                                   the framework stub throws `Stub!` and fails
* INTEROP      compile(...) + run(...) next to Python code in the calling test (e.g. `test_kotlin_toolchain.py`)
Nothing here decides a test's tier or marks a test executable: a test is executable only once it has been run.

CLASSPATH ORDER (compile and run): kotlin-stdlib, org.json, [bcprov], [android.jar], then caller extras. It matters:
`android.jar` contains stub classes for `org.json`; the real org.json jar must come first or the stubs would shadow it and
every JSON call would throw `Stub!`. `test_kotlin_toolchain.py` checks this order and that the first entry providing
`org.json.JSONObject` is the real jar.

SELECTION RULES (deterministic; reported by `check`)
* Gradle Kotlin libs: among `~/.gradle/wrapper/dists/gradle-*/*/gradle-*/lib` holding `kotlin-stdlib-2*.jar`, the
  lexically FIRST path, exactly the rule of `test_android_sync.py`, `test_android_interop.py` and `test_android_audit.py`
  (`libs[0]`), so the shared runner and the existing tests use the same compiler. A newer Gradle install can never
  silently change the compiler. The Kotlin version is read from the stdlib jar name and printed.
* org.json and bcprov: the highest numeric version; sources/javadoc jars are ignored.
* android.jar: the highest NUMERIC API level (`android-36`); preview or extension directories are ignored.

EXIT CODES: the compiler's and the JVM's own codes are returned unchanged (Kotlin compiler: 0 ok, 1 compilation error,
2 internal error, 3 script error). Errors raised by this module use RUNNER_ERROR (97), which is not an exit code of the
Kotlin compiler; a timeout is 124. A program run by `run()` can exit with any status it chooses (even `System.exit(97)`),
so a 97 from `run()` is the program's own; RUNNER_ERROR is only ever returned by `compile`/`compile_pure`.

CLI: `python3 tests/_kotlin.py check` prints every required item with its path (and the Kotlin version) and exits non-zero if
one is missing or the compiler cannot actually compile and run a program."""
from __future__ import annotations

import glob
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANDROID_SRC = HERE.parents[1] / "android/app/src/main/java/dev/phonebridge"
PURE_REGISTRY = HERE / "pure_kotlin_files.txt"
COMPILER_MAIN = "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler"
COMPILE_TIMEOUT = 300
RUN_TIMEOUT = 60
RUNNER_ERROR = 97          # an error of this module (bad input, violated tier rule); the Kotlin compiler never exits with 97
TIMEOUT = 124


class ToolchainError(Exception):
    """A required tool or jar is missing or unusable; the message names every problem."""


class ScannerError(ValueError):
    """The Kotlin source cannot be scanned reliably (unterminated block comment, raw string or template)."""


class InspectionError(Exception):
    """The production source tree cannot be fully inspected (unreadable directory, metadata error). Never ignored."""


class RegistryError(ValueError):
    """`pure_kotlin_files.txt` contains an unsafe, duplicate or colliding entry."""


# ------------------------------------------------------------------ selection rules

def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def _version_tuple(text: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", text))


def select_kotlin_lib(libs: list[str]) -> str | None:
    """The lexically first Gradle lib directory (the rule of the existing tests: `libs[0]`)."""
    return sorted(libs)[0] if libs else None


def select_jar(candidates: list[str], name_re: str) -> str | None:
    """The jar with the highest numeric version in its file name; sources and javadoc jars (which do not match) are ignored."""
    best, best_v = None, None
    for p in candidates:
        m = re.fullmatch(name_re, os.path.basename(p))
        if m and (best_v is None or _version_tuple(m.group(1)) > best_v):
            best, best_v = p, _version_tuple(m.group(1))
    return best


def select_android_jar(candidates: list[str]) -> str | None:
    """The android.jar of the highest NUMERIC platform directory (`android-36`); `android-VanillaIceCream`,
    `android-36-ext5` and similar are ignored."""
    best, best_level = None, -1
    for p in candidates:
        m = re.fullmatch(r"android-(\d+)", os.path.basename(os.path.dirname(p)))
        if m and int(m.group(1)) > best_level:
            best, best_level = p, int(m.group(1))
    return best


def _java_on_path() -> str | None:
    """The first executable `java` in an ABSOLUTE PATH entry. Empty and relative entries (which would resolve in the
    current directory) are ignored."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue
        c = os.path.join(d, "java")
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def java_version(java: str) -> tuple[bool, str]:
    """Runs `java -version`; ok only if it exits 0 and prints a recognisable version line."""
    try:
        r = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{java} does not run: {exc}"
    first = (r.stderr or r.stdout).strip().splitlines()[:1]
    line = first[0][:120] if first else ""
    if r.returncode != 0 or not re.search(r'(openjdk|java) version "\d', line):
        return False, f"{java} -version exit {r.returncode}: {line or '(no output)'}"
    return True, line


@dataclass(frozen=True)
class Toolchain:
    java: str
    kotlin_lib: str
    kcp: str            # classpath of the Kotlin compiler itself
    stdlib: str
    json_jar: str
    bcprov_jar: str | None
    android_jar: str | None

    @property
    def kotlin_version(self) -> str:
        m = re.search(r"kotlin-stdlib-([\d.]+)\.jar", os.path.basename(self.stdlib))
        return m.group(1) if m else "unknown"

    def classpath(self, *, bc: bool = False, android: bool = False, extra: tuple[str, ...] = ()) -> str:
        """stdlib, org.json, [bcprov], [android.jar], extras: the order is part of the contract (module docstring)."""
        parts = [self.stdlib, self.json_jar]
        if bc:
            if not self.bcprov_jar:
                raise ToolchainError("bcprov jar required but not found")
            parts.append(self.bcprov_jar)
        if android:
            if not self.android_jar:
                raise ToolchainError("android.jar required but not found")
            parts.append(self.android_jar)
        return ":".join(parts + list(extra))


def _zip_has(path: str | None, member: str) -> bool:
    if not path:
        return False
    try:
        with zipfile.ZipFile(path) as z:
            return member in z.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _locate() -> dict:
    libs = [l for l in glob.glob(_home(".gradle/wrapper/dists/gradle-*/*/gradle-*/lib"))
            if glob.glob(f"{l}/kotlin-stdlib-2*.jar")]
    lib = select_kotlin_lib(libs)
    jars = lambda *pats: [j for p in pats for j in sorted(glob.glob(f"{lib}/{p}"))] if lib else []
    caches = _home(".gradle/caches/modules-2/files-2.1")
    return {
        "java": _java_on_path(),
        "lib": lib,
        "kcp": ":".join(jars("kotlin-*.jar", "trove4j*.jar", "annotations*.jar", "kotlinx-coroutines-core-jvm*.jar")),
        "stdlib": (jars("kotlin-stdlib-2*.jar") or [None])[0],
        "json": select_jar(glob.glob(f"{caches}/org.json/json/*/*/json-*.jar"), r"json-(\d+)\.jar"),
        "bc": select_jar(glob.glob(f"{caches}/org.bouncycastle/bcprov-jdk18on/*/*/bcprov-jdk18on-*.jar"), r"bcprov-jdk18on-([\d.]+)\.jar"),
        "android": select_android_jar(glob.glob(_home("Android/Sdk/platforms/android-*/android.jar"))),
    }


@dataclass(frozen=True)
class Item:
    ok: bool
    name: str
    detail: str


def diagnose() -> dict[str, Item]:
    """Every required item, KEYED (java, kotlin, json, bcprov, android) so reordering cannot change what a caller selects.
    Looks at what is installed and runs `java -version`; `check_compiler` is the proof that the compiler executes."""
    L = _locate()
    out: dict[str, Item] = {}
    jname = "java on PATH (absolute entries only) and runs"
    if L["java"]:
        ok, detail = java_version(L["java"])
        out["java"] = Item(ok, jname, f"{L['java']}: {detail}" if ok else detail)
    else:
        out["java"] = Item(False, jname, "not found")
    kok = bool(L["lib"] and L["stdlib"] and "kotlin-compiler-embeddable" in L["kcp"])
    ver = re.search(r"kotlin-stdlib-([\d.]+)\.jar", os.path.basename(L["stdlib"] or ""))
    out["kotlin"] = Item(kok, "Gradle Kotlin compiler (kotlin-stdlib-2*.jar and compiler jars)",
                         f"{L['lib']} (Kotlin {ver.group(1) if ver else '?'})" if kok
                         else "no ~/.gradle/wrapper/dists/gradle-*/*/gradle-*/lib with kotlin-stdlib-2*.jar and the compiler jars")
    out["json"] = Item(_zip_has(L["json"], "org/json/JSONObject.class"), "org.json jar (readable, contains org/json/JSONObject)",
                       L["json"] or "not found under ~/.gradle/caches")
    out["bcprov"] = Item(_zip_has(L["bc"], "org/bouncycastle/jce/provider/BouncyCastleProvider.class"),
                         "bcprov-jdk18on jar (readable, contains BouncyCastleProvider)", L["bc"] or "not found under ~/.gradle/caches")
    out["android"] = Item(_zip_has(L["android"], "android/os/Build.class"), "Android SDK android.jar (readable, contains android/os/Build)",
                          L["android"] or "not found under ~/Android/Sdk/platforms (numeric android-NN only)")
    return out


def find(*, need_bc: bool = False, need_android: bool = False) -> Toolchain:
    """The toolchain, or ToolchainError naming everything missing. Never returns a partial toolchain silently."""
    d = diagnose()
    required = ["java", "kotlin", "json"] + (["bcprov"] if need_bc else []) + (["android"] if need_android else [])
    bad = [f"{d[k].name}: {d[k].detail}" for k in required if not d[k].ok]
    if bad:
        raise ToolchainError("; ".join(bad))
    L = _locate()
    return Toolchain(L["java"], L["lib"], L["kcp"], L["stdlib"], L["json"], L["bc"], L["android"])


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _runner_error(message: str) -> Result:
    return Result(RUNNER_ERROR, "", message)


def compile(tc: Toolchain, sources: list, out_dir, *, bc: bool = False, android: bool = False,
            extra_cp: tuple[str, ...] = ()) -> Result:
    """Compiles exactly `sources` (and nothing else) into `out_dir`. A missing source file or an empty list is an error
    (RUNNER_ERROR), never a skip. JVM-PURE tests must call `compile_pure` instead."""
    srcs = [str(s) for s in sources]
    if not srcs:
        return _runner_error("no source files given")
    missing = [s for s in srcs if not os.path.isfile(s)]
    if missing:
        return _runner_error(f"source file(s) not found: {missing}")
    cmd = [tc.java, "-cp", tc.kcp, COMPILER_MAIN, "-no-stdlib", "-no-reflect",
           "-cp", tc.classpath(bc=bc, android=android, extra=extra_cp), "-d", str(out_dir), *srcs]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=COMPILE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return Result(TIMEOUT, "", f"compiler timed out after {COMPILE_TIMEOUT} s")
    return Result(r.returncode, r.stdout, r.stderr)


def run(tc: Toolchain, out_dir, main_class: str, args: list | tuple = (), *, bc: bool = False, android: bool = False,
        extra_cp: tuple[str, ...] = (), timeout: int = RUN_TIMEOUT) -> Result:
    """Runs a compiled main class; the JVM's exit status is returned unchanged."""
    cmd = [tc.java, "-cp", f"{out_dir}:{tc.classpath(bc=bc, android=android, extra=extra_cp)}", main_class, *map(str, args)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(TIMEOUT, "", f"timed out after {timeout} s")
    return Result(r.returncode, r.stdout, r.stderr)


class Workdir(tempfile.TemporaryDirectory):
    """A temporary build directory (`pb-kotlin-*` under the system temp directory, which honours TMPDIR), outside the
    source tree, removed on exit even when the body raises. Only these directories are cleaned up by this module."""

    created: list[str] = []      # every directory this class made in this process (tests check that all are gone)

    def __init__(self):
        super().__init__(prefix="pb-kotlin-")
        Workdir.created.append(self.name)


# ------------------------------------------------------------------ the Android-reference scanner

def _blank_noncode(text: str) -> str:
    """Returns `text` with every comment and every string/character literal body replaced by spaces (newlines kept, so
    line numbers survive). Code inside string templates (`${ ... }`) is kept: it is code.

    Supported lexical rules: `//` line comments; `/* */` block comments, NESTED as Kotlin allows; `"..."` strings with
    backslash escapes; `\"\"\"...\"\"\"` raw strings (no escapes; the closing delimiter is the last three quotes of a run of
    quotes); `'x'`, `'\\''`, `'\\u0041'` character literals; `${ ... }` templates (nested, braces counted, strings
    inside templates handled recursively); backtick-quoted identifiers are blanked EXCEPT those whose whole token is
    exactly `android` or `androidx`, which stay visible (their backticks become spaces) because `android`.os.Build names the
    Android package; an unterminated backtick identifier (none is closed on its own line) raises ScannerError.
    An unterminated block comment, raw string, `${` template or backtick identifier raises ScannerError (they would otherwise hide the rest
    of the file). Lines are the original `\\n`-separated lines: other separators (form feed, U+2028, lone CR) are ordinary
    characters here, so blanked and original text always have identical line boundaries.
    Limitations: this is NOT a Kotlin parser. It does not resolve imports, aliases or local names (a local variable called
    `android` followed by a dot and a letter is reported: the scanner errs towards reporting), it does not understand
    multi-dollar string interpolation, and an unterminated ordinary string literal blanks only the rest of its line (the
    compiler rejects such a file anyway)."""
    out = list(text)
    n = len(text)

    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    def code(i: int, in_template: bool) -> int:
        depth = 0
        while i < n:
            c = text[i]
            two = text[i:i + 2]
            if two == "//":
                j = text.find("\n", i)
                j = n if j < 0 else j
                blank(i, j)
                i = j
            elif two == "/*":
                j, level = i + 2, 1
                while j < n and level:
                    if text.startswith("/*", j):
                        level, j = level + 1, j + 2
                    elif text.startswith("*/", j):
                        level, j = level - 1, j + 2
                    else:
                        j += 1
                if level:
                    raise ScannerError(f"unterminated block comment starting at line {text.count(chr(10), 0, i) + 1}")
                blank(i, j)
                i = j
            elif text.startswith('"""', i):
                i = raw_string(i)
            elif c == '"':
                i = string(i)
            elif c == "'":
                j = i + 1
                while j < n and text[j] != "'" and text[j] != "\n":
                    j += 2 if text[j] == "\\" else 1
                blank(i + 1, j)
                i = min(j + 1, n)
            elif c == "`":
                j = text.find("`", i + 1)
                nl = text.find("\n", i + 1)
                if j < 0 or 0 <= nl < j:        # a backtick identifier never contains a line break
                    raise ScannerError(f"unterminated backtick identifier at line {text.count(chr(10), 0, i) + 1}")
                if text[i + 1:j] in ("android", "androidx"):
                    blank(i, i + 1)              # keep the name itself visible to the matcher: `android`.os.Build IS the package
                    blank(j, j + 1)
                else:
                    blank(i, j + 1)
                i = j + 1
            elif in_template and c == "{":
                depth += 1
                i += 1
            elif in_template and c == "}":
                if depth == 0:
                    return i + 1
                depth -= 1
                i += 1
            else:
                i += 1
        if in_template:
            raise ScannerError("unterminated ${ template")
        return i

    def string(i: int) -> int:
        j = i + 1
        while j < n:
            c = text[j]
            if c == "\\":
                blank(j, j + 2)
                j += 2
            elif c == '"':
                return j + 1
            elif c == "\n":
                return j                 # unterminated line: stop at the newline
            elif text.startswith("${", j):
                j = code(j + 2, True)
            else:
                blank(j, j + 1)
                j += 1
        return j

    def raw_string(i: int) -> int:
        j = i + 3
        while j < n:
            if text.startswith('"""', j):
                k = j
                while k < n and text[k] == '"':
                    k += 1
                return k                 # the closing delimiter is the last three quotes of the run
            elif text.startswith("${", j):
                j = code(j + 2, True)
            else:
                blank(j, j + 1)
                j += 1
        raise ScannerError(f"unterminated raw string starting at line {text.count(chr(10), 0, i) + 1}")

    code(0, False)
    return "".join(out)


# `android.` or `androidx.` as the start of a qualified name (not preceded by a name character or a dot), followed by a
# letter, underscore or `*`, upper or lower case, so `android.R.string`, `android.Manifest`, `androidx.core.X` and
# `import android.*` are all found. It is matched against the WHOLE blanked text (`\s` includes newlines and the blanked
# comments), so `android` + newline + `.os.Build` and `android.` + newline + `os.Build` are found too.
_ANDROID_NAME = re.compile(r"(?<![\w.])(?:android|androidx)\s*\.\s*(?:[A-Za-z_]|\*)")


def android_reference_lines(text: str) -> list[tuple[int, str]]:
    """(1-based line number, original source line) for each line on which a reference STARTS. Raises ScannerError for
    an unterminated comment, raw string or template. Lines are `\\n`-separated (see `_blank_noncode`)."""
    blanked = _blank_noncode(text)
    assert len(blanked) == len(text) and blanked.count("\n") == text.count("\n")
    orig = text.split("\n")
    seen: dict[int, str] = {}
    for m in _ANDROID_NAME.finditer(blanked):
        no = blanked.count("\n", 0, m.start())
        seen.setdefault(no, orig[no].strip())
    return [(no + 1, line) for no, line in sorted(seen.items())]


def android_references(text: str) -> list[str]:
    """Source lines that import or name an `android.*` / `androidx.*` class in CODE (comments and string contents are
    ignored; code inside `${}` templates is not). May raise ScannerError. See `_blank_noncode` for the rules and limits."""
    return [line for _no, line in android_reference_lines(text)]


# ------------------------------------------------------------------ pure-file registry and compile_pure (20.4, JVM-PURE)

def registry_names(registry: Path | None = None) -> list[str]:
    """The validated entries of `pure_kotlin_files.txt`, in file order. Raises RegistryError for an entry that contains
    `/` or `\\`, contains `..`, is not a `*.kt` file name, or repeats an earlier entry (also compared case-insensitively,
    so two names that would collide on one file name are refused). Nothing is normalised: an unsafe entry is an error."""
    names, seen = [], {}
    for no, line in enumerate((registry or PURE_REGISTRY).read_text(encoding="utf-8").splitlines(), 1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if "/" in entry or "\\" in entry:
            raise RegistryError(f"line {no}: {entry!r} contains a path separator; entries are plain file names")
        if ".." in entry:
            raise RegistryError(f"line {no}: {entry!r} contains '..'")
        if not entry.endswith(".kt") or entry in (".kt",):
            raise RegistryError(f"line {no}: {entry!r} is not a .kt file name")
        key = entry.casefold()
        if key in seen:
            raise RegistryError(f"line {no}: {entry!r} duplicates the entry on line {seen[key]}")
        seen[key] = no
        names.append(entry)
    return names


def pure_files(registry: Path | None = None, src_dir: Path | None = None) -> list[Path]:
    """Files registered in tests/pure_kotlin_files.txt (names relative to the Android source directory), in file order."""
    return [(src_dir or ANDROID_SRC) / n for n in registry_names(registry)]


def _inode_key(p: Path):
    st = os.stat(p)
    return (st.st_dev, st.st_ino)


def _source_inodes(root: Path) -> set:
    """(device, inode) of every .kt file below `root`. A hard link shares its inode with the original and cannot cross a
    filesystem, so equality of (device, inode) is an exact test for "this file IS that production file".

    FAILS CLOSED: any directory that cannot be listed (permission, vanished, I/O error), a missing `root`, or any `.kt` entry
    whose metadata cannot be read raises InspectionError("production source cannot be fully inspected: ..."); nothing is
    skipped, because a skipped file could be the target of a hard link. (A file whose CONTENT cannot be read still has
    readable metadata, so it is still recognised by its inode.) Symbolic links to directories are not followed; a symlink
    named *.kt that does not lead to a regular file is not a source file and is ignored."""
    def fail(exc):
        raise InspectionError(f"production source cannot be fully inspected: {getattr(exc, 'filename', None) or root}: "
                              f"{type(exc).__name__}: {getattr(exc, 'strerror', None) or exc}") from exc

    keys = set()
    for dirpath, _dirs, files in os.walk(root, onerror=fail):
        for name in files:
            if not name.endswith(".kt"):
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)                     # follows a symlink, like the compiler would
            except FileNotFoundError:
                continue                               # a dangling symlink: not a source file
            except OSError as exc:
                fail(exc)
            if stat.S_ISREG(st.st_mode):
                keys.add((st.st_dev, st.st_ino))
    return keys


def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _boundary_problem(tests_dir: Path, src_dir: Path) -> str | None:
    """The tests directory must not overlap the REAL Android production source (whatever `src_dir` a caller passes) or the
    source directory in use. Both sides are resolved first, so symlinks cannot hide an overlap."""
    t = tests_dir.resolve()
    for label, root in (("the Android production source directory", ANDROID_SRC.resolve()), ("the source directory in use", src_dir.resolve())):
        if _inside(t, root) or _inside(root, t):
            return f"tests directory {t} overlaps {label} {root}"
    return None


def _harness_problem(h, tests_dir: Path, src_dir: Path, production_inodes: set) -> str | None:
    """Why a harness path is not acceptable, or None. The path is RESOLVED first (symlinks followed), so a link inside
    the tests directory that points at production code resolves outside it and is refused. The REAL Android production
    directory is always excluded, whatever `src_dir` is; a hard link to a production file is refused by (device, inode)."""
    try:
        p = Path(h).resolve(strict=False)
        if not p.is_file():
            return f"harness {p} does not exist or is not a file"
        if p.suffix != ".kt":
            return f"harness {p.name} is not a .kt file"
        for root in (ANDROID_SRC.resolve(), src_dir.resolve()):
            if _inside(p, root):
                return f"harness {p} is inside the Android production source directory {root}"
        if not _inside(p, tests_dir.resolve()):
            return f"harness {h} resolves to {p}, outside the tests directory {tests_dir.resolve()}"
        if _inode_key(p) in production_inodes:
            return f"harness {p} is a hard link to a production source file (same device and inode)"
    except (ValueError, OSError, RuntimeError, TypeError) as exc:
        return f"harness {h!r} cannot be checked ({type(exc).__name__}: {exc})"
    return None


def _registered_problem(n: str, src_dir: Path, real_inodes: set) -> tuple[Path | None, str | None]:
    """The exact resolved path of a registered file, or why it is refused. The file must resolve to `<src_dir>/<name>`
    itself: a symlink (the file or a directory above it) that leads anywhere else is refused, with a specific message
    when it leads into the real Android production source."""
    try:
        root = src_dir.resolve()
        raw = src_dir / n
        if not raw.exists():
            return None, f"registered file {n} does not exist"
        p = raw.resolve()
        if p != root / n:
            into_prod = _inside(p, ANDROID_SRC.resolve()) and root != ANDROID_SRC.resolve()
            return None, (f"registered file {n} is a symlink into the Android production source ({p})" if into_prod
                          else f"registered file {n} resolves to {p}, not to {root / n} (a symlink is not allowed)")
        if not p.is_file():
            return None, f"registered file {n} is not a regular file"
        if root != ANDROID_SRC.resolve() and _inode_key(p) in real_inodes:
            return None, f"registered file {n} is a hard link to a production source file"
    except (ValueError, OSError, RuntimeError) as exc:
        return None, f"registered file {n} cannot be checked ({type(exc).__name__}: {exc})"
    return p, None


def compile_pure(tc: Toolchain, names: list, out_dir, *, harness: list = (), bc: bool = False,
                 registry: Path | None = None, src_dir: Path | None = None, tests_dir: Path | None = None) -> Result:
    """THE entry point for a JVM-PURE test (future JVM-PURE tests must use it, not `compile`).

    `names` are registry names such as "SyncCore.kt". Everything is validated BEFORE the compiler runs, and the output
    directory is not created for a refused request; any problem returns RUNNER_ERROR (97) with all diagnostics in `stderr`.
    It refuses: an unreadable or invalid registry (missing file, bad encoding, `registry_names` rules); an empty list; a
    name that is not a registered plain file name; a registered file that does not exist or whose resolved path is not
    `<src_dir>/<name>` (symlinks); a `tests_dir` that overlaps the REAL Android production source directory or `src_dir`
    (the real directory is excluded whatever `src_dir` the caller passes); any `harness` that does not resolve (symlinks
    followed) to a `.kt` file inside `tests_dir` and outside the production directories, or that is a hard link to a
    production file (device and inode); a path that cannot be checked (embedded NUL, I/O error); any file, registered or
    harness, with an Android reference; any file that cannot be scanned (unterminated comment, raw string, template or
    backtick identifier). The files compiled are exactly the resolved paths that were validated and scanned.
    `harness` are test-owned files (for example `tests/sync/Harness.kt`) compiled with the pure files; they need not be
    registered. It also refuses, with exit 97 and BEFORE anything is read, arguments of the wrong type: `names` and
    `harness` must be lists or tuples (`names` of str, `harness` of str or pathlib.Path); `registry`, `src_dir` and
    `tests_dir` must be None or a pathlib.Path (not a str, int or bytes); `out_dir` a str or pathlib.Path; `bc` a bool.
    If the production source tree (the real ANDROID_SRC and `src_dir`) cannot be fully inspected, the request is refused:
    "production source cannot be fully inspected".
    Limitation: a hard link is detected only against production files that exist under the production directories at the
    time of the call; a file COPIED into the tests directory is an ordinary test file."""
    wrong = []
    for label, value in (("registry", registry), ("src_dir", src_dir), ("tests_dir", tests_dir)):
        if value is not None and not isinstance(value, Path):
            wrong.append(f"{label} must be None or a pathlib.Path, not {type(value).__name__}")
    if not isinstance(out_dir, (str, Path)):
        wrong.append(f"out_dir must be a str or pathlib.Path, not {type(out_dir).__name__}")
    if not isinstance(bc, bool):
        wrong.append(f"bc must be a bool, not {type(bc).__name__}")
    if not isinstance(names, (list, tuple)):
        wrong.append(f"names must be a list or tuple, not {type(names).__name__}")
    if not isinstance(harness, (list, tuple)):
        wrong.append(f"harness must be a list or tuple, not {type(harness).__name__}")
    else:
        for h in harness:
            if not isinstance(h, (str, Path)):
                wrong.append(f"a harness entry must be a str or pathlib.Path, not {type(h).__name__}")
    if wrong:
        return _runner_error("compile_pure refused: invalid arguments: " + "; ".join(wrong))
    src_dir = src_dir if src_dir is not None else ANDROID_SRC
    tests_dir = tests_dir if tests_dir is not None else HERE
    if not names:
        return _runner_error("compile_pure: no files given")
    try:
        registered = {n: src_dir / n for n in registry_names(registry)}
    except RegistryError as exc:
        return _runner_error(f"compile_pure refused: invalid registry: {exc}")
    except (ValueError, OSError, UnicodeDecodeError) as exc:      # missing file, bad encoding, NUL, I/O error
        return _runner_error(f"compile_pure refused: registry cannot be read ({type(exc).__name__}: {exc})")
    problems, srcs = [], []
    try:
        why = _boundary_problem(tests_dir, src_dir)
        real_inodes = _source_inodes(ANDROID_SRC)
        production_inodes = real_inodes | _source_inodes(src_dir)
    except InspectionError as exc:
        why, real_inodes, production_inodes = str(exc), set(), set()
    except (ValueError, OSError, RuntimeError) as exc:
        why, real_inodes, production_inodes = f"directories cannot be checked ({type(exc).__name__}: {exc})", set(), set()
    if why:
        problems.append(why)
    for n in names:
        if not isinstance(n, str) or n not in registered:
            problems.append(f"{n!r} is not listed in pure_kotlin_files.txt")
            continue
        path, bad = _registered_problem(n, src_dir, real_inodes)
        if bad:
            problems.append(bad)
        else:
            srcs.append(path)
    if not why:
        for h in harness:
            bad = _harness_problem(h, tests_dir, src_dir, production_inodes)
            if bad:
                problems.append(bad)
            else:
                srcs.append(Path(h).resolve())
    for p in srcs:
        try:
            refs = android_references(p.read_text(encoding="utf-8"))
        except ScannerError as exc:
            problems.append(f"{p.name} cannot be scanned: {exc}")
            continue
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            problems.append(f"{p.name} cannot be read: {exc}")
            continue
        if refs:
            problems.append(f"{p.name} references the Android framework: {refs[:3]}")
    if problems:
        return _runner_error("compile_pure refused: " + "; ".join(problems))
    return compile(tc, srcs, out_dir, bc=bc)


# ------------------------------------------------------------------ execution proof + CLI

HELLO = 'fun main() { println("k0-compiler-ok") }\n'


def check_compiler(tc: Toolchain) -> tuple[bool, str]:
    """Proves the compiler runs: compiles and executes a one-line program in a temporary directory (removed afterwards)."""
    with Workdir() as tmp:
        src = Path(tmp) / "Hello.kt"
        src.write_text(HELLO)
        c = compile(tc, [src], Path(tmp) / "out")
        if not c.ok:
            return False, f"compile failed (exit {c.returncode}): {(c.stderr or c.stdout).strip()[:400]}"
        r = run(tc, Path(tmp) / "out", "HelloKt")
        if not r.ok or "k0-compiler-ok" not in r.stdout:
            return False, f"run failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:400]}"
    return True, f"compiled and ran a program with Kotlin {tc.kotlin_version}"


def main(argv: list[str]) -> int:
    if argv[:1] != ["check"]:
        print("usage: python3 tests/_kotlin.py check", file=sys.stderr)
        return 2
    bad = 0
    for item in diagnose().values():
        print(f"  {'ok  ' if item.ok else 'FAIL'} {item.name}: {item.detail}")
        bad += not item.ok
    if not bad:
        tc = find(need_bc=True, need_android=True)
        ok, detail = check_compiler(tc)
        print(f"  {'ok  ' if ok else 'FAIL'} the Kotlin compiler executes: {detail}")
        bad += not ok
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
