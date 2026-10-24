"""ut_setup - checks and installs the tools the unit-test pipeline needs [TP-1..TP-16].

Called by unittest_generator.bat (Windows) and unittest_generator.sh (Linux) before every
command. Only the Python standard library is used, so it runs before anything else.

    python ut_setup.py ensure [--repo REPO] [--branch B]   check; install what is missing; write the manifest
    python ut_setup.py check  [--repo REPO]   check only, change nothing

REPO is a folder or a git repository link; for a link only the file list of the branch is read [TP-12a].
    python ut_setup.py ensure --force         re-check every tool (ignores the cached check)

What it guarantees: a Clang/LLVM toolchain (18 or newer) that compiles, links and runs C and C++
with MC/DC coverage, merges and exports the coverage, builds libFuzzer targets and runs the
static analyzer; CMake, Ninja and Git; and, for the given repository, the tools of its build
system (Make, Meson, Bazel through Bazelisk; MSBuild is detected, never installed).

Where tools come from [TP-3, TP-4]:
  1. a tool that is already installed and passes the functional check is used as it is
     (no second, overlapping installation);
  2. otherwise the version pinned in ut_config.json ("setup") is downloaded from the
     project's official release page, its SHA-256 is verified, and it is unpacked into
     <this folder>/tools/<tool>. Nothing outside the tools folder is changed, except Linux
     system headers, which only the distribution's package manager can install [TP-7].

The result is tools/toolchain.json [TP-10]. The pipeline reads tool locations only from that
file; it does not search the system and has no fallback compiler.

Exit codes: 0 ready; 1 a tool is missing or failed its check; 2 the operating system blocked a
tool (for example Windows Smart App Control) [TP-9]; 3 an action by you is needed (for example
Linux headers that need administrator rights).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST_SCHEMA = 1
IS_WINDOWS = os.name == "nt"
EXE = ".exe" if IS_WINDOWS else ""
OS_KEY = "windows" if IS_WINDOWS else "linux"
PY_PACKAGES = ("pydantic", "langgraph", "fastmcp", "uvicorn", "anthropic")                  # TP-13
# Windows: the operating system refused to start a program (application control / Smart App Control).
BLOCKED_WINERRORS = {225, 1260, 4551, 4556}
BLOCKED_TEXT = re.compile(r"application control|smart app control|blocked by (your )?(organization|policy)|"
                          r"system integrity policy|ERROR_ACCESS_DISABLED_BY_POLICY", re.I)


class Blocked(Exception):
    """The operating system refused to run a tool [TP-9]."""


class NeedsUser(Exception):
    """Something only the user can do (administrator rights, a licence decision) [TP-7]."""


class SetupFailed(Exception):
    pass


# ============================================================================ small helpers
def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str] | str, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout: float = 300) -> tuple[int, str]:
    """A string is passed to Windows unchanged (needed for cmd /s /c quoting)."""
    name = cmd if isinstance(cmd, str) else cmd[0]
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                              env={**os.environ, **(env or {})})
    except FileNotFoundError:
        return -127, f"not found: {name}"
    except subprocess.TimeoutExpired:
        return -9, f"timed out after {timeout:.0f} s: {name}"
    except OSError as exc:
        if getattr(exc, "winerror", None) in BLOCKED_WINERRORS or BLOCKED_TEXT.search(str(exc)):
            raise Blocked(f"{name}: {exc}") from exc
        return -126, f"{name}: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and BLOCKED_TEXT.search(out):
        raise Blocked(f"{name}: {out.strip()[:300]}")
    return proc.returncode, out


def version_of(text: str) -> str:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return ".".join(g for g in m.groups() if g is not None) if m else ""


def vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def file_fingerprint(paths: list[str]) -> str:
    h = hashlib.sha256()
    for p in paths:
        try:
            st = Path(p).stat()
            h.update(f"{p}|{st.st_size}|{st.st_mtime_ns}\n".encode())
        except OSError:
            h.update(f"{p}|missing\n".encode())
    return h.hexdigest()


def which_in(directory: Path, name: str) -> str:
    p = directory / (name + EXE)
    return str(p) if p.is_file() else ""


# ============================================================================ download and unpack [TP-4]
def download(url: str, sha256: str, dest: Path) -> Path:
    if not url.startswith("https://"):
        raise SetupFailed(f"refusing a non-HTTPS download: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and sha256 and hashlib.sha256(dest.read_bytes()).hexdigest() == sha256:
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    log(f"  downloading {url}")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "ut_setup"})
    start, last = time.time(), 0.0
    with urllib.request.urlopen(req, timeout=120) as resp, part.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if time.time() - last > 5:
                last = time.time()
                pct = f" {100 * done // total}%" if total else ""
                log(f"    {done >> 20} MB{pct}")
    digest = h.hexdigest()
    if sha256 and digest != sha256:
        part.unlink(missing_ok=True)
        raise SetupFailed(f"SHA-256 mismatch for {url}: expected {sha256}, got {digest}. The file was deleted; "
                          "nothing was installed [TP-4].")
    part.replace(dest)
    log(f"  verified SHA-256 {digest[:16]}... ({dest.stat().st_size >> 20} MB, {time.time() - start:.0f} s)")
    return dest


def _inside(base: Path, rel: str) -> bool:
    target = (base / rel).resolve()
    return target == base.resolve() or base.resolve() in target.parents


def unpack(archive: Path, dest: Path, strip: str = "", keep=None) -> None:
    """Unpack into dest (which must not exist), dropping the top folder ``strip``. Every member path
    is checked to stay inside dest; ``keep(rel)`` limits what is unpacked [TP-4]."""
    tmp = dest.with_name(dest.name + ".unpacking")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    def rel_of(name: str) -> str | None:
        name = name.replace("\\", "/")
        if strip:
            if name.rstrip("/") == strip:
                return None
            if not name.startswith(strip + "/"):
                return None
            name = name[len(strip) + 1:]
        if not name or (keep and not keep(name)):
            return None
        if name.startswith("/") or ".." in name.split("/") or re.match(r"^[A-Za-z]:", name):
            raise SetupFailed(f"unsafe path in {archive.name}: {name}")
        return name

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                rel = rel_of(info.filename)
                if rel is None:
                    continue
                if not _inside(tmp, rel):
                    raise SetupFailed(f"unsafe path in {archive.name}: {info.filename}")
                target = tmp / rel
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                mode = (info.external_attr >> 16) & 0o777
                if mode and not IS_WINDOWS:
                    target.chmod(mode | 0o600)
    else:
        with tarfile.open(archive) as t:
            for member in t:
                rel = rel_of(member.name)
                if rel is None:
                    continue
                member.name = rel
                if member.issym():
                    target = os.path.normpath(os.path.join(os.path.dirname(rel), member.linkname))
                    if os.path.isabs(member.linkname) or target.startswith(".."):
                        raise SetupFailed(f"unsafe link in {archive.name}: {rel} -> {member.linkname}")
                elif member.islnk():
                    link = rel_of(member.linkname)
                    if link is None:
                        continue
                    member.linkname = link
                elif not (member.isfile() or member.isdir()):
                    continue                                       # no devices, fifos
                if hasattr(tarfile, "data_filter"):
                    t.extract(member, tmp, filter="data")      # Python 3.12+: also refuses unsafe members
                else:
                    t.extract(member, tmp)
    shutil.rmtree(dest, ignore_errors=True)
    tmp.replace(dest)


# ============================================================================ the functional check [TP-8]
CHECK_C = r"""#include <stdio.h>
int decide(int a, int b, int c) { if ((a && b) || c) return 1; return 0; }
int main(void) { int r = decide(1, 1, 0) + decide(0, 0, 1) + decide(1, 0, 0) + decide(0, 1, 0);
                 printf("UT_CHECK %d\n", r); return r == 2 ? 0 : 1; }
"""
CHECK_CPP = r"""#include <string>
#include <vector>
#include <cstdio>
class Acc { public: explicit Acc(int s) : s_(s) {} int add(int v) { if (v > 0 && v < 100) s_ += v; return s_; } private: int s_; };
int main() { std::vector<std::string> v{"a", "b"}; Acc a(1); a.add(5); a.add(500);
             std::printf("UT_CHECK %d %zu\n", a.add(0), v.size()); return a.add(0) == 6 ? 0 : 1; }
"""
CHECK_FUZZ = r"""#include <stdint.h>
#include <stddef.h>
static int target(int x) { if (x == 0x5a) return 1; return 0; }
int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) { if (size >= 1) (void)target(data[0]); return 0; }
"""
CHECK_ANALYZE = r"""#include <stddef.h>
int deref(int *p) { if (p == NULL) { return *p; } return 0; }
"""


def check_toolchain(tc: dict, work: Path) -> dict:
    """Compile, link and run C and C++ with MC/DC coverage, merge and export it, build a libFuzzer
    target and run the static analyzer. Returns the features; raises SetupFailed on what is required."""
    env = tc.get("env") or {}
    if tc.get("path"):
        env = {**env, "PATH": os.pathsep.join(tc["path"] + [env.get("PATH", os.environ.get("PATH", ""))])}
    work.mkdir(parents=True, exist_ok=True)
    (work / "t.c").write_text(CHECK_C)
    (work / "t.cpp").write_text(CHECK_CPP)
    (work / "f.c").write_text(CHECK_FUZZ)
    (work / "a.c").write_text(CHECK_ANALYZE)
    cov = ["-fprofile-instr-generate", "-fcoverage-mapping", "-fcoverage-mcdc"]
    link = tc.get("link_flags", [])
    code, out = run([tc["cc"], *cov, "t.c", "-o", "t" + EXE, *link], work, env)
    if code != 0:
        raise SetupFailed("C compile/link with MC/DC coverage failed: " + out.strip()[-600:])
    code, out = run([str(work / ("t" + EXE))], work, {**env, "LLVM_PROFILE_FILE": str(work / "t.profraw")}, 60)
    if code != 0 or "UT_CHECK 2" not in out:
        raise SetupFailed("the compiled C check program did not run correctly: " + out.strip()[-300:])
    code, out = run([tc["profdata"], "merge", "-sparse", "t.profraw", "-o", "t.profdata"], work, env)
    if code != 0:
        raise SetupFailed("llvm-profdata merge failed: " + out.strip()[-300:])
    code, out = run([tc["cov"], "export", "-format=text", "t" + EXE, "-instr-profile=t.profdata"], work, env)
    if code != 0:
        raise SetupFailed("llvm-cov export failed: " + out.strip()[-300:])
    data = json.loads(out[out.index("{"):]) if "{" in out else {}
    mcdc = any(fn.get("mcdc_records") for d in data.get("data", []) for fn in d.get("functions", []))
    if not mcdc:
        raise SetupFailed("the coverage export contains no MC/DC records (Clang 18 or newer is required)")
    code, out = run([tc["cxx"], *tc.get("cxx_flags", []), *cov, "t.cpp", "-o", "tpp" + EXE, *link,
                     *tc.get("cxx_link_flags", [])], work, env)
    if code != 0:
        raise SetupFailed("C++ compile/link failed: " + out.strip()[-600:])
    code, out = run([str(work / ("tpp" + EXE))], work, {**env, "LLVM_PROFILE_FILE": str(work / "tpp.profraw")}, 60)
    if code != 0 or "UT_CHECK 6 2" not in out:
        raise SetupFailed("the compiled C++ check program did not run correctly: " + out.strip()[-300:])
    features = {"mcdc": True, "cpp": True, "fuzzer": False, "analyzer": False}
    code, out = run([tc["cc"], "-g", "-fsanitize=fuzzer", "f.c", "-o", "f" + EXE, *link], work, env)
    if code == 0:
        code, out = run([str(work / ("f" + EXE)), "-runs=200", "-seed=1"], work, env, 120)
        features["fuzzer"] = code == 0 and "Done" in out
    if not features["fuzzer"]:
        features["fuzzer_problem"] = out.strip()[-300:]
    code, out = run([tc["cc"], "--analyze", "-Xclang", "-analyzer-output=text", "a.c", "-o", "a.plist"], work, env)
    features["analyzer"] = "null pointer" in out.lower()
    if not features["analyzer"]:
        features["analyzer_problem"] = out.strip()[-300:]
    return features


# ============================================================================ platform facts [NC-10]
PROBE_HEAD = r"""#include <stdio.h>
#include <stdlib.h>
#ifdef _WIN32
#include <windows.h>
#define UT_QUIET() (SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX), \
                    _set_abort_behavior(0, _WRITE_ABORT_MSG | _CALL_REPORTFAULT))
#else
#define UT_QUIET() ((void)0)
#endif
"""
PROBES_C = {
    "normal": "int main(void) { UT_QUIET(); puts(\"UT_PROBE normal\"); return 0; }",
    "crash": "int main(void) { volatile int *p = 0; UT_QUIET(); *p = 1; return 0; }",
    "abort": "int main(void) { UT_QUIET(); abort(); }",
    "stack_overflow": "static int deep(volatile int n) { volatile char b[4096]; b[0] = (char)n; "
                      "return deep(n + 1) + b[0]; }\nint main(void) { UT_QUIET(); return deep(0); }",
    # the harness's own end by its watchdog: the marker, then exit(4) [A3-45d]
    "watchdog": "int main(void) { UT_QUIET(); printf(\"UT_WATCHDOG|probe\\n\"); fflush(stdout); exit(4); }",
}
PROBE_CPP = "int main() { UT_QUIET(); throw 1; }"


def measure_platform(tc: dict, work: Path) -> dict:
    """NC-10: how this platform and toolchain behave, measured with small probe programs: the exit status of a normal
    end, a crash, an abort, an uncaught C++ exception, a stack overflow and the watchdog's end; the register page unit;
    whether the file system tells upper from lower case; how the compiler prints a path; the program suffix and the
    line endings. Never fails the setup: what cannot be measured keeps the host's value."""
    import mmap
    env = tc.get("env") or {}
    if tc.get("path"):
        env = {**env, "PATH": os.pathsep.join(tc["path"] + [env.get("PATH", os.environ.get("PATH", ""))])}
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)       # no crash dialog for the probes
        except (AttributeError, OSError):
            pass
    work.mkdir(parents=True, exist_ok=True)
    facts: dict = {"os": OS_KEY if sys.platform != "darwin" else "darwin", "exe_suffix": EXE,
                   "line_ending": "\r\n" if IS_WINDOWS else "\n", "outcomes": {},
                   "page_bytes": int(getattr(mmap, "ALLOCATIONGRANULARITY", 4096) or 4096),
                   "watchdog_marker": "UT_WATCHDOG", "watchdog_exit": 4}
    for kind, body in [*PROBES_C.items(), ("exception", PROBE_CPP)]:
        cpp = kind == "exception"
        src = work / f"probe_{kind}.{'cpp' if cpp else 'c'}"
        src.write_text(PROBE_HEAD + body + "\n", encoding="utf-8")
        exe = work / f"probe_{kind}{EXE}"
        compiler = tc["cxx"] if cpp else tc["cc"]
        flags = [*tc.get("cxx_flags", [])] if cpp else []
        link = [*tc.get("link_flags", []), *(tc.get("cxx_link_flags", []) if cpp else [])]
        code, out = run([compiler, *flags, "-O0", src.name, "-o", exe.name, *link], work, env, 120)
        if code != 0:
            continue
        code, out = run([str(exe)], work, env, 20)
        if code == -9:
            continue
        if kind == "watchdog":
            facts["watchdog_exit"] = code
            if "UT_WATCHDOG|probe" not in out:
                facts["watchdog_marker"] = ""
            continue
        facts["outcomes"].setdefault(kind, [])
        if code not in facts["outcomes"][kind]:
            facts["outcomes"][kind].append(code)
    probe = work / "CaseProbe.txt"
    probe.write_text("x", encoding="utf-8")
    facts["case_sensitive"] = not (work / "caseprobe.txt").exists()
    (work / "sub").mkdir(exist_ok=True)
    (work / "sub" / "err.c").write_text("int f(void) { return undeclared_name; }\n", encoding="utf-8")
    code, out = run([tc["cc"], "-fsyntax-only", "sub/err.c"], work, env, 60)
    printed = re.search(r"^(.*?err\.c):\d+", out, re.M)
    facts["path_separator"] = "\\" if printed and "\\" in printed.group(1) else "/"
    facts["paths_absolute"] = bool(printed and re.match(r"^([A-Za-z]:)?[\\/]", printed.group(1)))
    facts["measured"] = _dt.date.today().isoformat()
    return facts


def missing_headers(message: str) -> list[str]:
    return re.findall(r"'([\w./+-]+)' file not found", message)


# ============================================================================ setup
class Setup:
    def __init__(self, config_path: Path, tools_dir: Path | None = None, force: bool = False,
                 check_only: bool = False, pip: bool = True):
        raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        self.cfg = raw.get("setup", {})
        self.pipeline = raw.get("pipeline", {})
        self.pins = self.cfg.get("downloads", {}).get(OS_KEY, {})
        tools = tools_dir or Path(self.cfg.get("tools_dir", "tools"))
        self.tools = tools if tools.is_absolute() else (config_path.resolve().parent / tools)
        self.manifest_path = self.tools / "toolchain.json"
        self.force, self.check_only, self.pip = force, check_only, pip
        self.min_clang = int(self.cfg.get("min_clang", 18))
        self.prefer_existing = bool(self.cfg.get("prefer_existing", True))
        self.old = self._read_manifest()
        self.manifest = {"schema": MANIFEST_SCHEMA, "os": OS_KEY, "machine": platform.machine(),
                         "written": "", "tools_dir": str(self.tools), "toolchain": {}, "tools": {},
                         "notes": []}
        self._vs_env: dict[str, str] | None = None

    def _read_manifest(self) -> dict:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return data if data.get("schema") == MANIFEST_SCHEMA and data.get("os") == OS_KEY else {}
        except (OSError, ValueError):
            return {}

    # ------------------------------------------------------------ the whole check
    def ensure(self, repo: Path | str | None, branch: str = "") -> int:
        self.tools.mkdir(parents=True, exist_ok=True)
        try:
            self.toolchain()
            self.cmake()
            self.ninja()
            self.git()
            if isinstance(repo, str):
                self.link_tools(repo, branch)
            elif repo is not None:
                self.project_tools(repo)
            self.python_packages()
            self.brains()
        except Blocked as exc:
            log("")
            log(f"BLOCKED BY WINDOWS: {exc}")
            log("Windows refused to start a downloaded tool (Smart App Control or an application-control "
                "policy). Setup stops here and does not work around the block [TP-9]. Options:")
            log("  1. Install Visual Studio Build Tools 2022 with 'Desktop development with C++' and the "
                "component 'C++ Clang Compiler for Windows' (Microsoft-signed); run setup again - it is "
                "found and used.")
            log("  2. Allow the tools folder in your organisation's application-control policy, or switch "
                "Smart App Control off (Windows Security > App & browser control).")
            return 2
        except NeedsUser as exc:
            log("")
            log(f"ACTION NEEDED: {exc}")
            return 3
        except SetupFailed as exc:
            log("")
            log(f"SETUP FAILED: {exc}")
            return 1
        if not self.check_only:
            self.manifest["written"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            self.manifest_path.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
        log(f"Tools ready ({self.manifest_path}).")
        return 0

    # ------------------------------------------------------------ compiler toolchain
    def toolchain(self) -> None:
        old = self.old.get("toolchain") or {}
        if old and not self.force and old.get("fingerprint") == self._fingerprint(old):
            self.manifest["toolchain"] = old
            log(f"Compiler    : {old['kind']} {old['version']} ({old['origin']}), checked "
                f"{old.get('checked', '')[:10]}")
            self.platform(old, self.old.get("platform"))
            return
        candidates = self._installed_toolchains() + (self._existing_toolchains() if self.prefer_existing else [])
        problems: list[str] = []
        if any(self._try(tc, problems) for tc in candidates):
            return
        headers = [h for p in problems for h in missing_headers(p)]
        if headers and not IS_WINDOWS and not self.check_only:
            self._linux_headers(headers)                 # C library headers come from the distribution [TP-7]
            problems.clear()
            if any(self._try(tc, problems) for tc in candidates):
                return
        if self.check_only:
            raise SetupFailed("no usable compiler toolchain: " + "; ".join(problems)[:800])
        tc = self._install_toolchain()
        if not self._try(tc, problems):
            raise SetupFailed("the installed toolchain failed its check: " + problems[-1][:800])

    def platform(self, tc: dict, old: dict | None) -> None:
        """NC-10: the platform facts, measured again whenever the toolchain is checked again (TP-11)."""
        if old and old.get("measured"):
            self.manifest["platform"] = old
            return
        with tempfile.TemporaryDirectory(prefix="ut_probe_", ignore_cleanup_errors=True) as tmp:
            try:
                facts = measure_platform(tc, Path(tmp))
            except (Blocked, OSError, KeyError) as exc:
                self.manifest["notes"].append(f"platform facts not measured: {exc}"[:200])
                return
        self.manifest["platform"] = facts
        log(f"Platform    : {facts['os']}, file names {'with' if facts['case_sensitive'] else 'without'} letter case, "
            f"register page {facts['page_bytes']} bytes, crash exit {facts['outcomes'].get('crash', ['?'])}")

    def _fingerprint(self, tc: dict) -> str:
        return file_fingerprint([tc.get("cc", ""), tc.get("cxx", ""), tc.get("profdata", ""), tc.get("cov", "")])

    def _try(self, tc: dict, problems: list[str]) -> bool:
        code, out = run([tc["cc"], "--version"], env=tc.get("env"))
        version = version_of(out.split("version", 1)[1]) if "version" in out else ""
        if code != 0 or vtuple(version) < (self.min_clang,):
            problems.append(f"{tc['cc']}: version {version or '?'} (need {self.min_clang}+)")
            return False
        code, target = run([tc["cc"], "-dumpmachine"], env=tc.get("env"))
        with tempfile.TemporaryDirectory(prefix="ut_check_", ignore_cleanup_errors=True) as tmp:
            try:
                features = check_toolchain(tc, Path(tmp))
            except SetupFailed as exc:
                problems.append(f"{tc['cc']}: {exc}")
                if not IS_WINDOWS and str(exc).startswith("C++") and missing_headers(str(exc)) and \
                        tc.get("libcxx_dir") and not tc.get("cxx_flags"):
                    # No libstdc++ headers on this system: the LLVM release brings its own libc++.
                    alt = {**tc, "cxx_flags": ["-stdlib=libc++"],
                           "cxx_link_flags": ["-stdlib=libc++", "-Wl,-rpath," + tc["libcxx_dir"]]}
                    return self._try(alt, problems)
                return False
        tc.update(version=version, target=target.strip(), checked=_dt.date.today().isoformat(), **features)
        tc["fingerprint"] = self._fingerprint(tc)
        self.manifest["toolchain"] = tc
        self.platform(tc, None)                                                             # NC-10
        extras = ", ".join(k for k in ("fuzzer", "analyzer") if features.get(k)) or "no fuzzer/analyzer"
        log(f"Compiler    : {tc['kind']} {version} ({tc['origin']}) - C, C++, MC/DC, {extras}")
        for k in ("fuzzer", "analyzer"):
            if not features.get(k):
                self.manifest["notes"].append(f"{k} not available with this toolchain: "
                                              f"{features.get(k + '_problem', '')[:200]}")
        return True

    def _installed_toolchains(self) -> list[dict]:
        out = []
        for name in ("llvm-mingw", "llvm"):
            bindir = self.tools / name / "bin"
            cc = which_in(bindir, "clang")
            if cc:
                out.append(self._chain(name, bindir, "installed"))
        return out

    def _chain(self, kind: str, bindir: Path, origin: str, env: dict | None = None,
               suffix: str = "") -> dict:
        tc = {"kind": kind, "origin": origin, "cc": which_in(bindir, "clang" + suffix) or str(bindir / ("clang" + EXE)),
              "cxx": which_in(bindir, "clang++" + suffix) or str(bindir / ("clang++" + EXE)),
              "profdata": which_in(bindir, "llvm-profdata" + suffix) or shutil.which("llvm-profdata" + suffix) or "",
              "cov": which_in(bindir, "llvm-cov" + suffix) or shutil.which("llvm-cov" + suffix) or "",
              "path": [str(bindir)], "env": env or {}, "link_flags": [], "cxx_flags": [], "cxx_link_flags": []}
        if kind == "llvm-mingw":
            tc["link_flags"] = ["-static"]           # test programs need no DLLs from the tools folder
        libcxx = bindir.parent / "lib" / "x86_64-unknown-linux-gnu"
        if (libcxx / "libc++.so").exists() or (libcxx / "libc++.a").exists():
            tc["libcxx_dir"] = str(libcxx)
        return tc

    def _existing_toolchains(self) -> list[dict]:
        out = []
        if IS_WINDOWS:
            dirs = []
            found = shutil.which("clang")
            if found:
                dirs.append((Path(found).parent, "existing (PATH)"))
            pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LLVM" / "bin"
            dirs.append((pf, "existing (LLVM)"))
            vs = self._vs_path()
            if vs:
                dirs.append((vs / "VC" / "Tools" / "Llvm" / "x64" / "bin", "existing (Visual Studio)"))
            for bindir, origin in dirs:
                if not which_in(bindir, "clang"):
                    continue
                code, target = run([str(bindir / "clang.exe"), "-dumpmachine"])
                env = self._vs_environment() if "msvc" in target else {}
                if "msvc" in target and not env:
                    continue                             # an MSVC-target Clang needs Visual Studio's libraries
                out.append(self._chain("llvm", bindir, origin, env))
        else:
            seen = set()
            for suffix in [""] + [f"-{v}" for v in range(30, self.min_clang - 1, -1)]:
                found = shutil.which("clang" + suffix)
                if not found:
                    continue
                real = Path(found).resolve()
                if real in seen:
                    continue
                seen.add(real)
                bindir = real.parent
                tc = self._chain("llvm", bindir, "existing", suffix="" if which_in(bindir, "clang") else suffix)
                tc["cc"], tc["cxx"] = found, shutil.which("clang++" + suffix) or str(bindir / "clang++")
                if not tc["profdata"]:
                    tc["profdata"] = shutil.which("llvm-profdata" + suffix) or ""
                if not tc["cov"]:
                    tc["cov"] = shutil.which("llvm-cov" + suffix) or ""
                if tc["profdata"] and tc["cov"]:
                    out.append(tc)
        return out

    def _install_toolchain(self) -> dict:
        name = "llvm-mingw" if IS_WINDOWS else "llvm"
        pin = self._pin(name)
        dest = self.tools / name
        if IS_WINDOWS:
            skip = ("aarch64-w64-mingw32/", "armv7-w64-mingw32/", "i686-w64-mingw32/", "python/")
            keep = lambda rel: not rel.startswith(skip)
        else:
            wanted_bins = {"clang", "clang++", "clang-cpp", f"clang-{pin['version'].split('.')[0]}", "lld", "ld.lld",
                           "llvm-profdata", "llvm-cov", "llvm-symbolizer", "llvm-ar", "llvm-ranlib", "llvm-nm",
                           "llvm-objdump"}
            keep = lambda rel: (rel.startswith("bin/") and rel[4:] in wanted_bins) or rel.startswith(
                ("lib/clang/", "lib/x86_64-unknown-linux-gnu/", "include/c++/", "include/x86_64-unknown-linux-gnu/")) \
                or rel in ("LICENSE.TXT",)
        self._install(name, pin, dest, keep)
        return self._chain(name, dest / "bin", "installed")

    # ------------------------------------------------------------ build tools
    def cmake(self) -> None:
        self._simple_tool("cmake", "cmake", ["--version"], "3.20", bin_sub="bin")

    def ninja(self) -> None:
        self._simple_tool("ninja", "ninja", ["--version"], "1.10", bin_sub="")

    def git(self) -> None:
        if IS_WINDOWS:
            self._simple_tool("git", "git", ["--version"], "2.30", bin_sub="cmd")
            return
        found = shutil.which("git")
        if found:
            code, out = run([found, "--version"])
            self.manifest["tools"]["git"] = {"path": found, "version": version_of(out), "origin": "existing"}
            log(f"git         : {version_of(out)} (existing)")
            return
        self._linux_packages(["git"], "git")

    def _simple_tool(self, name: str, exe: str, version_args: list[str], minimum: str, bin_sub: str) -> dict:
        old = (self.old.get("tools") or {}).get(name)
        if old and not self.force and Path(old["path"]).is_file() and old.get("fingerprint") == \
                file_fingerprint([old["path"]]):
            self.manifest["tools"][name] = old
            log(f"{name:12}: {old['version']} ({old['origin']})")
            return old
        bindir = self.tools / name / bin_sub if bin_sub else self.tools / name
        candidates = [(which_in(bindir, exe), "installed")]
        if self.prefer_existing:
            candidates.append((shutil.which(exe) or "", "existing"))
        for path, origin in candidates:
            if not path:
                continue
            code, out = run([path, *version_args])
            if code == 0 and vtuple(version_of(out)) >= vtuple(minimum):
                entry = {"path": path, "version": version_of(out), "origin": origin,
                         "fingerprint": file_fingerprint([path])}
                self.manifest["tools"][name] = entry
                log(f"{name:12}: {entry['version']} ({origin})")
                return entry
        if self.check_only:
            raise SetupFailed(f"{name} {minimum}+ is not available")
        pin = self._pin(name)
        if pin.get("archive") == "exe":
            dest = self.tools / name
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / (exe + EXE)
            download(pin["url"], pin["sha256"], target)
            if not IS_WINDOWS:
                target.chmod(0o755)
            (dest / ".ut-installed.json").write_text(json.dumps(pin, indent=1))
        else:
            self._install(name, pin, self.tools / name)
        path = which_in(bindir, exe)
        if not path:
            raise SetupFailed(f"{name}: {exe}{EXE} not found after unpacking")
        code, out = run([path, *version_args])
        if code != 0:
            raise SetupFailed(f"{name} does not run: {out.strip()[-300:]}")
        entry = {"path": path, "version": version_of(out), "origin": "installed", "fingerprint": file_fingerprint([path])}
        self.manifest["tools"][name] = entry
        log(f"{name:12}: {entry['version']} (installed)")
        return entry

    def _pin(self, name: str) -> dict:
        pin = self.pins.get(name)
        if not pin or not pin.get("url") or not pin.get("sha256"):
            raise SetupFailed(f"no pinned download for {name} on {OS_KEY} in ut_config.json (setup.downloads)")
        return pin

    def _install(self, name: str, pin: dict, dest: Path, keep=None) -> None:
        log(f"Installing {name} {pin.get('version', '')} into {dest}")
        archive = download(pin["url"], pin["sha256"], self.tools / "_downloads" / Path(pin["url"]).name)
        unpack(archive, dest, pin.get("strip", ""), keep)
        (dest / ".ut-installed.json").write_text(json.dumps(pin, indent=1))
        if not self.cfg.get("keep_downloads", False):
            archive.unlink(missing_ok=True)

    # ------------------------------------------------------------ per-project tools [TP-5]
    SKIP_DIRS = {".git", "node_modules", ".venv", "ut_output", "ut_work", "build", "out", "bazel-out"}

    @staticmethod
    def systems_of(names: set[str]) -> set[str]:
        found = set()
        if "CMakeLists.txt" in names:
            found.add("cmake")
        if names & {"Makefile", "makefile", "GNUmakefile"}:
            found.add("make")
        if "meson.build" in names:
            found.add("meson")
        if names & {"WORKSPACE", "WORKSPACE.bazel", "MODULE.bazel", "BUILD.bazel"}:
            found.add("bazel")
        if any(n.endswith((".sln", ".vcxproj")) for n in names):
            found.add("msbuild")
        return found

    @classmethod
    def build_systems(cls, repo: Path) -> list[str]:
        found = set()
        for root, dirs, files in os.walk(repo):
            depth = len(Path(root).relative_to(repo).parts)
            dirs[:] = [d for d in dirs if d not in cls.SKIP_DIRS and not d.startswith("bazel-") and depth < 4]
            found |= cls.systems_of(set(files))
        return sorted(found)

    @classmethod
    def build_systems_of_paths(cls, paths: list[str]) -> list[str]:
        """The same rules for the file list of a commit [TP-12a]."""
        by_dir: dict[str, set[str]] = {}
        for path in paths:
            parts = path.split("/")
            if len(parts) > 5 or any(p in cls.SKIP_DIRS or p.startswith("bazel-") for p in parts[:-1]):
                continue
            by_dir.setdefault("/".join(parts[:-1]), set()).add(parts[-1])
        found = set()
        for names in by_dir.values():
            found |= cls.systems_of(names)
        return sorted(found)

    def link_tools(self, url: str, branch: str = "") -> None:
        """TP-12a: only the file tree of one commit is fetched, into a temporary folder."""
        git = ((self.manifest.get("tools") or {}).get("git") or {}).get("path", "")
        pipeline = self.pipeline
        env = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        token = os.environ.get(pipeline.get("git_token_env", ""), "") if pipeline.get("git_token_env") else ""
        if token:                                       # passed through the environment only [ORC-21, TP-14]
            import base64
            user = pipeline.get("git_token_user", "x-access-token")
            env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0":
                        "Authorization: Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()})
        shown = re.sub(r"(\w+://)[^/@\s]+@", r"\1***@", url)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "tree.git"
            cmd = [git, "clone", "--bare", "--quiet", "--depth", "1", "--filter=blob:none",
                   *(["--branch", branch] if branch else []), url, str(target)]
            code, out = run(cmd, env=env, timeout=1800)
            if code != 0:
                message = out.strip()[-300:].replace(token, "***") if token else out.strip()[-300:]
                log(f"Repository  : not readable ({re.sub(r'(\w+://)[^/@\s]+@', r'\1***@', message)}); its build "
                    "tools are not checked")
                self.manifest.setdefault("notes", []).append(f"the repository {shown} could not be read by setup")
                return
            code, listing = run([git, "--git-dir", str(target), "ls-tree", "-r", "--name-only", "HEAD"], env=env)
            paths = listing.splitlines() if code == 0 else []
            systems = self.build_systems_of_paths(paths)
            log(f"Build files : {', '.join(systems) or 'none found'} ({shown}{' ' + branch if branch else ''})")
            self.manifest["project"] = {"repo": shown, "build_systems": systems}
            work = Path(tmp) / "files"
            work.mkdir()
            if "bazel" in systems:                     # the repository's Bazel version, if it names one
                code, version = run([git, "--git-dir", str(target), "show", "HEAD:.bazelversion"], env=env)
                if code == 0 and version.strip():
                    (work / ".bazelversion").write_text(version.strip() + "\n", encoding="utf-8")
            self._systems_tools(systems, work)

    def project_tools(self, repo: Path) -> None:
        systems = self.build_systems(repo)
        log(f"Build files : {', '.join(systems) or 'none found'} ({repo})")
        self.manifest["project"] = {"repo": str(repo), "build_systems": systems}
        self._systems_tools(systems, repo)

    def _systems_tools(self, systems: list[str], repo: Path) -> None:
        if "make" in systems:
            self.make()
        if "meson" in systems:
            self.meson()
        if "bazel" in systems:
            self.bazel(repo)
        if "msbuild" in systems:
            self.msbuild()

    def make(self) -> None:
        tc = self.manifest["toolchain"]
        if IS_WINDOWS:
            bundled = which_in(Path(tc["path"][0]), "mingw32-make") if tc.get("path") else ""
            path = bundled or shutil.which("make") or shutil.which("mingw32-make") or \
                which_in(self.tools / "make", "mingw32-make")
            if not path and not self.check_only:
                # Only llvm-mingw's make is unpacked: a single program that needs no DLLs.
                self._install("make", self._pin("llvm-mingw"), self.tools / "make",
                              keep=lambda rel: rel == "bin/mingw32-make.exe")
                path = which_in(self.tools / "make" / "bin", "mingw32-make")
        else:
            path = shutil.which("make") or ""
            if not path:
                self._linux_packages(["make"], "make")
                path = shutil.which("make") or ""
        if not path:
            raise SetupFailed("make is not available")
        code, out = run([path, "--version"])
        self.manifest["tools"]["make"] = {"path": path, "version": version_of(out), "origin": "existing"
                                          if self.tools not in Path(path).parents else "installed"}
        log(f"make        : {version_of(out)}")

    def meson(self) -> None:
        found = shutil.which("meson")
        if found and self.prefer_existing:
            code, out = run([found, "--version"])
            if code == 0 and vtuple(version_of(out)) >= (1, 0):
                self.manifest["tools"]["meson"] = {"path": found, "args": [], "version": version_of(out),
                                                   "origin": "existing"}
                log(f"meson       : {version_of(out)} (existing)")
                return
        script = self.tools / "meson" / "meson.py"
        if not script.is_file():
            if self.check_only:
                raise SetupFailed("meson is not available")
            self._install("meson", self._pin("meson"), self.tools / "meson")
        code, out = run([sys.executable, str(script), "--version"])
        if code != 0:
            raise SetupFailed("meson does not run: " + out.strip()[-300:])
        self.manifest["tools"]["meson"] = {"path": sys.executable, "args": [str(script)], "version": version_of(out),
                                           "origin": "installed"}
        log(f"meson       : {version_of(out)} (installed)")

    def bazel(self, repo: Path) -> None:
        found = (shutil.which("bazelisk") or shutil.which("bazel") or "") if self.prefer_existing else ""
        origin = "existing"
        if not found:
            found, origin = which_in(self.tools / "bazel", "bazelisk"), "installed"
        if not found:
            if self.check_only:
                raise SetupFailed("bazel (bazelisk) is not available")
            pin = self._pin("bazel")
            target = self.tools / "bazel" / ("bazelisk" + EXE)
            download(pin["url"], pin["sha256"], target)
            if not IS_WINDOWS:
                target.chmod(0o755)
            found = str(target)
        cache = self.tools / "bazel" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        env = {"BAZELISK_HOME": str(cache)}
        if not self.check_only:                          # downloads the Bazel version of the repository now
            code, out = run([found, "version"], cwd=repo, env=env, timeout=900)
            if code != 0:
                self.manifest["notes"].append("bazel version failed in the repository: " + out.strip()[-300:])
        self.manifest["tools"]["bazel"] = {"path": found, "version": "bazelisk", "origin": origin, "env": env}
        log(f"bazel       : {found} ({origin})")

    def msbuild(self) -> None:
        """Visual Studio is never installed by setup (administrator rights, licence) [TP-6]."""
        vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
            "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        path = ""
        if IS_WINDOWS and vswhere.is_file():
            code, out = run([str(vswhere), "-latest", "-products", "*", "-find", r"MSBuild\**\Bin\MSBuild.exe"])
            path = out.strip().splitlines()[0] if code == 0 and out.strip() else ""
        if path:
            self.manifest["tools"]["msbuild"] = {"path": path, "version": "", "origin": "existing"}
            log(f"msbuild     : {path}")
        else:
            self.manifest["notes"].append("Visual Studio projects found but MSBuild is not installed: their compile "
                                          "settings are read from the project files (A3-BD5)")
            log("msbuild     : not installed - settings are read from the .vcxproj files")

    # ------------------------------------------------------------ Windows: Visual Studio environment
    def _vs_path(self) -> Path | None:
        vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
            "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if not vswhere.is_file():
            return None
        code, out = run([str(vswhere), "-latest", "-products", "*", "-requires",
                         "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"])
        return Path(out.strip().splitlines()[0]) if code == 0 and out.strip() else None

    def _vs_environment(self) -> dict[str, str]:
        if self._vs_env is not None:
            return self._vs_env
        self._vs_env = {}
        vs = self._vs_path()
        if vs is None:
            return self._vs_env
        devcmd = vs / "Common7" / "Tools" / "VsDevCmd.bat"
        code, out = run(f'cmd /s /c ""{devcmd}" -arch=x64 -host_arch=x64 -no_logo && set"')
        if code != 0:
            return self._vs_env
        for line in out.splitlines():
            key, sep, value = line.partition("=")
            if sep and key.upper() in ("INCLUDE", "LIB", "LIBPATH", "PATH"):
                self._vs_env[key.upper()] = value
        return self._vs_env

    # ------------------------------------------------------------ Linux packages [TP-7]
    def _linux_packages(self, packages: list[str], what: str) -> None:
        if shutil.which("apt-get"):
            cmd = ["apt-get", "install", "-y", *packages]
        elif shutil.which("dnf"):
            cmd = ["dnf", "install", "-y", *packages]
        elif shutil.which("zypper"):
            cmd = ["zypper", "--non-interactive", "install", *packages]
        else:
            raise NeedsUser(f"install {what} with your distribution's package manager")
        if os.geteuid() == 0:
            code, out = run(cmd, timeout=1800)
        elif shutil.which("sudo"):
            code, out = run(["sudo", "-n", *cmd], timeout=1800)       # never asks for a password
        else:
            code, out = 1, ""
        if code != 0:
            raise NeedsUser(f"{what} must be installed by an administrator. Run:  sudo {' '.join(cmd)}  "
                            "and start setup again.")

    def _linux_headers(self, headers: list[str]) -> None:
        cxx = any(h in ("iostream", "string", "vector", "cstdio") for h in headers)
        if shutil.which("apt-get"):
            packages = ["libc6-dev"] + (["libstdc++-dev"] if cxx else [])
            code, out = run(["apt-cache", "show", "libstdc++-dev"]) if cxx else (0, "")
            if cxx and code != 0:
                packages = ["libc6-dev", "g++"]
        elif shutil.which("dnf"):
            packages = ["glibc-devel"] + (["libstdc++-devel"] if cxx else [])
        else:
            packages = ["the C library headers"]
        self._linux_packages(packages, "the C" + ("/C++" if cxx else "") + " library headers "
                             f"({', '.join(headers[:3])})")

    # ------------------------------------------------------------ Python and brains (checked, not installed)
    def python_packages(self) -> None:
        import importlib.util
        missing = [p for p in PY_PACKAGES if importlib.util.find_spec(p) is None]
        if missing and self.pip and not self.check_only:
            log(f"Installing Python packages from PyPI: {', '.join(missing)}")
            code, out = run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing],
                            timeout=1800)
            if code != 0:
                raise SetupFailed("pip install failed: " + out.strip()[-400:])
            missing = [p for p in PY_PACKAGES if importlib.util.find_spec(p) is None]
        if missing:
            raise SetupFailed(f"Python packages missing in {sys.executable}: {', '.join(missing)}")
        log(f"Python      : {sys.version.split()[0]} with {', '.join(PY_PACKAGES)}")

    def brains(self) -> None:
        """TP-13, ARCH-1a: reported, never installed. 'ut_pipeline.py check' shows which Claude
        models the key can access."""
        key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
        log("Claude      : " + ("ANTHROPIC_API_KEY is set; the models it can access are used first"
                                if key else "ANTHROPIC_API_KEY is not set: no Claude model"))
        remote = bool(os.environ.get("QWEN_REMOTE_API_KEY", "").strip())
        log("Remote Qwen : " + ("QWEN_REMOTE_API_KEY is set; the remote server is tried before Ollama"
                                if remote else "QWEN_REMOTE_API_KEY is not set: the remote server is skipped"))
        model = os.environ.get("BASEAGENT_QWEN_MODEL", "qwen3:8b")
        ollama = shutil.which("ollama")
        if not ollama:
            self.manifest["notes"].append("Ollama not found: no Qwen fallback brain")
            log("Ollama      : not found (no Qwen fallback; " + ("the Claude models answer)" if key else
                                                                 "LLM steps are skipped)"))
            return
        code, out = run([ollama, "list"], timeout=30)
        state = "model present" if code == 0 and model.split(":")[0] in out else \
            ("not running" if code != 0 else f"model {model} missing: ollama pull {model}")
        log(f"Ollama      : {state}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check and install the unit-test generator's tools.")
    parser.add_argument("command", choices=["ensure", "check"], nargs="?", default="ensure")
    parser.add_argument("--repo", help="also the build tools this repository (folder or link) needs")
    parser.add_argument("--branch", default="", help="for a link: the branch to be tested (default: the default branch)")
    parser.add_argument("--config", type=Path, default=HERE / "ut_config.json")
    parser.add_argument("--tools", type=Path, help="tools folder (default: setup.tools_dir of the config)")
    parser.add_argument("--force", action="store_true", help="check every tool again")
    parser.add_argument("--no-pip", action="store_true", help="do not install missing Python packages")
    args = parser.parse_args(argv)
    link = bool(args.repo) and bool(re.match(r"^(?:[a-z][a-z0-9+.-]*://|[\w.-]+@[\w.-]+:)", args.repo, re.I)) \
        and not Path(args.repo).is_dir()
    if args.repo and not link and not Path(args.repo).is_dir():
        log(f"Repository not found: {args.repo}")
        return 1
    setup = Setup(args.config, args.tools, force=args.force, check_only=args.command == "check", pip=not args.no_pip)
    repo = args.repo if link else (Path(args.repo).resolve() if args.repo else None)
    return setup.ensure(repo, args.branch)


if __name__ == "__main__":
    raise SystemExit(main())
