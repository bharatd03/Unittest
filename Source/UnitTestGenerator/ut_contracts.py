"""Contracts (typed handoffs) between the unit-test agents.

Every handoff in the pipeline is one of these pydantic models, carried inside BaseAgent's
ContractRequest / ContractResponse envelope [ARCH-3, ARCH-4]. The receiving agent checks
its preconditions, the orchestrator checks postconditions, both sides validate the schema.

Requirement IDs refer to unittest_generator_requirments.txt.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# baseagent.py lives in the BaseAgent folder next to this one (or BASEAGENT_HOME).
_BASEAGENT_HOME = os.environ.get("BASEAGENT_HOME") or str(Path(__file__).resolve().parent.parent / "BaseAgent")
if _BASEAGENT_HOME not in sys.path:
    sys.path.insert(0, _BASEAGENT_HOME)

import baseagent as ba  # noqa: E402  (path set above)

CONTRACTS_VERSION = "1.0"
HARNESS_VERSION = "ut_harness 1.2"
METRICS = ("LC", "SC", "BC", "DC", "MCDC")
FAILURE_CATEGORIES = ("test_generation_defect", "stub_defect", "test_framework_defect",
                      "build_configuration_defect", "source_compilation_defect", "source_behavior_defect",
                      "requirement_mismatch", "environment_defect", "undetermined")
# A3-177: who handles each category. "agent3" = Agent3 repairs its own harness/build.
ROUTES = {"test_generation_defect": "agent1", "stub_defect": "agent1", "requirement_mismatch": "agent1",
          "undetermined": "agent1", "test_framework_defect": "agent3", "build_configuration_defect": "agent3",
          "source_compilation_defect": "agent4", "source_behavior_defect": "agent4",
          "environment_defect": "stop"}

Category = Literal["test_generation_defect", "stub_defect", "test_framework_defect", "build_configuration_defect",
                   "source_compilation_defect", "source_behavior_defect", "requirement_mismatch",
                   "environment_defect", "undetermined"]
Metric = Literal["LC", "SC", "BC", "DC", "MCDC"]
EvidenceClass = Literal["E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def files_signature(root: Path, files: list[str]) -> str:
    """One signature over several files (path + content), order-independent [ORC-10]."""
    h = hashlib.sha256()
    for rel in sorted(set(files)):
        p = root / rel
        h.update(rel.replace("\\", "/").encode() + b"\0")
        h.update(file_sha256(p).encode() if p.is_file() else b"missing")
        h.update(b"\n")
    return h.hexdigest()


# ============================================================================ discovery (Agent1)
class RequirementDocument(Contract):
    """A1-RQ1, A1-RQ3: a requirement document given for the run, copied unchanged into the run's output folder."""
    name: str                                      # "requirements/<file>": the source that evidence and tests cite
    path: str                                      # the copy in the run's output folder
    sha256: str
    origin: str = ""                               # the path the user gave


class DiscoverRequest(Contract):
    repo_root: str
    sut_file: str                                  # relative to repo_root
    project: str = ""                              # the tested repository: link or source folder [ARCH-23]
    test_dir: str | None = None                    # override where tests are written
    stub_dir: str | None = None
    requirement_documents: list[RequirementDocument] = Field(default_factory=list)   # given for the run [A1-RQ1]


ParamKind = Literal["int", "bool", "enum", "float", "cstring", "buffer", "ptr_scalar", "struct", "ptr_struct",
                    "callback", "object", "other", "ptr_void", "interface"]


class FieldInfo(Contract):
    name: str
    type: str
    kind: str = "other"                  # int | bool | enum | float | array | struct | pointer | callback | other
    array_len: int = 0
    base: str = ""                       # pointer: the pointee type, qualifiers removed [A1-DA21]
    fields: list["FieldInfo"] = Field(default_factory=list)      # nested struct


class ParamInfo(Contract):
    """A parameter as the compiler front end sees it [A1-DA2], with the kind of value a test can pass."""
    name: str
    type: str
    kind: ParamKind = "other"
    base: str = ""                       # pointee / element / record type
    fields: list[FieldInfo] = Field(default_factory=list)       # struct and ptr_struct
    enum_values: list[str] = Field(default_factory=list)       # enumerator names
    nullable: bool = False               # the function compares it with NULL, so NULL is a valid input
    by_ref: bool = False                 # C++ reference: pass the variable, not its address
    callback_return: str = ""            # callback: its return type
    callback_params: list[str] = Field(default_factory=list)
    canonical: str = ""                  # the type with typedefs resolved (u32_t -> unsigned long) [A3-HW4]


class FunctionInfo(Contract):
    name: str
    qualified_name: str
    signature: str
    file: str
    line_start: int
    line_end: int
    is_static: bool = False
    calls: list[str] = Field(default_factory=list)
    return_type: str = ""
    params: list[ParamInfo] = Field(default_factory=list)
    from_frontend: bool = False          # False: text parsing only (A1-DA14, estimated)
    member: bool = False                 # C++ class member (method, constructor, destructor)


class TestCaseInfo(Contract):
    name: str
    file: str
    line: int
    enabled: bool = True
    targets: list[str] = Field(default_factory=list)
    framework: str = ""


class CompileEntry(Contract):
    """One recorded translation unit of the build description [A3-BD1, A3-BD8]. Paths inside the
    working copy are written as {repo}/..., so every staged copy can use them."""
    file: str                                       # relative to the repository
    flags: list[str] = Field(default_factory=list)  # host flags: -D -U -I -isystem -include -std= ...
    dropped: list[str] = Field(default_factory=list)  # target-specific options left out [A3-BD7]
    language: Literal["c", "cpp"] = "c"
    output: str = ""                                # the object file the build writes; names a variant [A3-BD14]


class BuildVariant(Contract):
    """One way the build compiles the SUT file, when it compiles it more than once with different
    flags [A3-BD14]. Only the tested variant is built and measured; the others are analysed (A1-37a)."""
    name: str
    flags: list[str] = Field(default_factory=list)
    tested: bool = False


class BuildDescription(Contract):
    source: Literal["configured", "client_compile_commands", "cmake", "meson", "ninja", "make", "bazel",
                    "msbuild", "project_file", "compile_commands", "estimated"] = "estimated"
    command: str = ""
    duration_s: float = 0.0
    outcome: str = ""
    entries: list[CompileEntry] = Field(default_factory=list)
    sut_flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    estimated: bool = True               # True: flags were guessed, results are flagged [A3-BD5]
    suggested_flags: list[str] = Field(default_factory=list)   # "flag (file: text)" kept from the brain [A3-BD13]
    reused: bool = False                 # taken from the build-capture cache of this commit [ORC-22]
    variants: list[BuildVariant] = Field(default_factory=list)  # two or more ways the SUT is compiled [A3-BD14]
    host_adaptation: list[str] = Field(default_factory=list)   # host definitions by the brain, kept [A3-BD16]


class Dependency(Contract):
    """An external symbol the SUT uses and its decision [A1-DA4 .. A1-DA11]."""
    name: str
    qualified_name: str = ""
    kind: Literal["function", "method", "constructor", "variable"] = "function"
    declaration: str = ""                # exact declaration text from the header
    type: str = ""                       # the front end's type, e.g. "int (int)" or "const int[4]"
    header: str = ""                     # declaring header, relative to the repository ("" = system)
    line: int = 0
    definition: str = ""                 # file that defines it (relative), "" = none in the repository
    existing_stub: str = ""              # a stub file that already defines it
    system: bool = False
    decision: Literal["stub", "real", "system"] = "stub"
    decided_by: str = "rule"             # rule | llm p=0.91 | fallback
    reason: str = ""
    depth: int = 0                       # 0 = used by the SUT itself; >0 = brought in by a real dependency
    used_in_decision: bool = False
    is_const: bool = False
    attempts: int = 0
    return_type: str = ""                # from the front end, fully qualified
    params: list[list[str]] = Field(default_factory=list)   # [[type, name], ...]
    method_suffix: str = ""              # C++: " const" etc.
    namespace: str = ""                  # C++: the namespace that declares it, e.g. "QP" [A2-21ad]


class DependencyPlan(Contract):
    entries: list[Dependency] = Field(default_factory=list)
    headers: list[str] = Field(default_factory=list)          # every header the SUT includes [A1-DA3]
    missing_headers: list[str] = Field(default_factory=list)  # with the places searched [A1-DA13]
    real_sources: list[str] = Field(default_factory=list)     # files Agent3 compiles and links [A3-38a]
    estimated: bool = False                                   # text patterns only [A1-DA14]
    notes: list[str] = Field(default_factory=list)


class RequirementLink(Contract):
    """A requirement written in prose, linked to a function by the brain and checked by Agent1 [A1-PL13]."""
    requirement_id: str
    function: str
    source: str
    line: int = 0
    quote: str
    probability: float = Field(ge=0.0, le=1.0)


class EnumInfo(Contract):
    name: str
    values: dict[str, int] = Field(default_factory=dict)


class StateVar(Contract):
    """A1-DA15: a variable of the SUT file that the SUT can change."""
    name: str
    type: str = ""


def state_resettable(var: StateVar, cpp: bool) -> bool:
    """A2-21s: C state is restored byte for byte; in C++ only scalars, enumerations, pointers and arrays of them."""
    if not cpp:
        return True
    t = re.sub(r"\b(volatile|const|unsigned|signed|struct|enum)\b", " ", var.type)
    t = re.sub(r"\[[^\]]*\]", "", t).strip()
    return t.endswith("*") or bool(re.fullmatch(r"(bool|char|short|int|long|long long|float|double|long double|"
                                                 r"u?int\d+_t|size_t|[A-Za-z_]\w*_t|[A-Z]\w*)\s*", t))


def sut_reset_source(state: list[StateVar], cpp: bool) -> str:
    """A2-21s, A3-FZ8: ut_sut_reset() records the SUT state at its first call and restores it at every later
    one. Placed in a translation unit that includes the SUT (sut_access, fuzz target)."""
    kept = [v for v in state if state_resettable(v, cpp)]
    rows = ["/* A2-21s: every test starts from the SUT's initial state. */", "#include <string.h>"]
    rows += [f"static unsigned char ut_state_{i}[sizeof({v.name})];" for i, v in enumerate(kept)]
    rows += ["static int ut_state_saved;", ('extern "C" ' if cpp else "") + "void ut_sut_reset(void)", "{",
             "    if (!ut_state_saved) {"]
    rows += [f"        memcpy(ut_state_{i}, (const void *)&{v.name}, sizeof ut_state_{i});" for i, v in enumerate(kept)]
    rows += ["        ut_state_saved = 1;", "        return;", "    }"]
    rows += [f"    memcpy((void *)&{v.name}, ut_state_{i}, sizeof ut_state_{i});" for i, v in enumerate(kept)]
    rows += ["}", ""]
    return "\n".join(rows)


# ============================================================================ endless loops [A2-21y, A3-HW5]
ENDLESS_LOOP = re.compile(r"\bfor\s*\(\s*;\s*;\s*\)|\bwhile\s*\(\s*(?:1[uUlL]*|true|TRUE)\s*\)")
_LEAVES = re.compile(r"\b(return|goto|longjmp|siglongjmp|exit|_Exit|abort|throw)\b")


def _statement_end(text: str, k: int) -> int:
    """End of the statement that starts at or after index k: a balanced block or up to the next semicolon."""
    while k < len(text) and text[k] in " \t\r\n":
        k += 1
    if k < len(text) and text[k] == "{":
        depth = 0
        for j in range(k, len(text)):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            if depth == 0:
                return j + 1
        return len(text)
    j = text.find(";", k)
    return len(text) if j < 0 else j + 1


def _without_nested(loop: str) -> str:
    """The loop body without the bodies of nested switch statements and loops: a break there does not
    leave the outer loop."""
    out, k = [], 0
    for m in re.finditer(r"\b(switch|for|while|do)\b", loop):
        if m.start() < k:
            continue
        j = m.end()
        if m.group(1) != "do":
            while j < len(loop) and loop[j] in " \t\r\n":
                j += 1
            if j < len(loop) and loop[j] == "(":
                depth = 0
                for q in range(j, len(loop)):
                    depth += {"(": 1, ")": -1}.get(loop[q], 0)
                    if depth == 0:
                        j = q + 1
                        break
        end = _statement_end(loop, j)
        out.append(loop[k: m.start()])
        k = end
    out.append(loop[k:])
    return "".join(out)


def endless_loop_end(body: str) -> int | None:
    """A2-21y: the end (index in ``body``) of an endless loop - for (;;), while (1), while (true) - that nothing
    in its body leaves (no break of its own, no return, goto, longjmp, exit, abort or throw), or None."""
    code = re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: re.sub(r"[^\n]", " ", m.group(0)), body, flags=re.S)
    for m in ENDLESS_LOOP.finditer(code):
        end = _statement_end(code, m.end())
        loop = code[m.end(): end]
        if _LEAVES.search(loop) or re.search(r"\bbreak\b", _without_nested(loop.strip()[1:-1] if loop.strip().startswith("{") else loop)):
            continue
        return end
    return None


def unreachable_lines(sut_text: str, functions: list["FunctionInfo"]) -> set[int]:
    """A3-HW5: lines of code after an endless loop. No test can execute them; LLVM's coverage counts the
    region after a loop from the loop's entry count, so a test the harness ends inside the loop would show
    them as executed - they are reported as not covered instead."""
    lines = sut_text.splitlines()
    out: set[int] = set()
    for fn in functions:
        body = "\n".join(lines[fn.line_start - 1: fn.line_end])
        end = endless_loop_end(body)
        if end is None:
            continue
        first = fn.line_start + body[:end].count("\n") + 1
        for number in range(first, fn.line_end + 1):
            text = re.sub(r"/\*.*?\*/|//.*", "", lines[number - 1]).strip() if number - 1 < len(lines) else ""
            if text and text.strip("}; ") != "":
                out.add(number)
    return out


SCALAR_TYPE = re.compile(r"^(?:(?:unsigned|signed|short|long|int|char|float|double|bool|_Bool)\b\s*)+$|^(?:std::)?u?int\w*_t$|"
                         r"^(?:std::)?size_t$|^.*\*$")


def pointee_object(base: str, var: str, cpp: bool) -> tuple[str, str]:
    """(definition, address) of a zeroed local object that a pointer field of a struct argument points to - a
    handle's Instance gets a register block of its own [A2-21ad, A3-FZ2]."""
    base = (base or "").strip()
    if base in ("", "void"):
        return f"unsigned char {var}[64] = {{0}};", var
    if base in ("char", "signed char", "unsigned char"):
        return f"{base} {var}[256] = {{0}};", var
    if cpp:
        return f"{base} {var}{{}};", f"&{var}"
    return f"{base} {var} = {'0' if SCALAR_TYPE.match(base) else '{0}'};", f"&{var}"


def null_checked(field: str, body: str) -> bool:
    """The function compares a struct field with NULL (x->buf == NULL, !x->buf, NULL != x.buf) [A2-21ad]."""
    f = re.escape(field)
    return bool(re.search(r"(?:->|\.)\s*" + f + r"\s*[!=]=\s*(?:NULL|nullptr|0)\b|\b(?:NULL|nullptr)\s*[!=]=\s*[\w\[\]]+\s*"
                          r"(?:->|\.)\s*" + f + r"\b|!\s*\(?\s*[\w\[\]]+\s*(?:->|\.)\s*" + f + r"\b", body))


PROLOGUE_DIRECTIVE = re.compile(r"^#\s*(include|define|undef|if|ifdef|ifndef|elif|elifdef|elifndef|else|endif|pragma)\b(.*)$")


def without_comments(text: str) -> str:
    """Comments replaced by blanks (newlines kept); string and character literals kept as they are."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j]))
            i = j
        elif text[i] in "\"'":
            j = i + 1
            while j < n and text[j] != text[i] and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            out.append(text[i: j + 1])
            i = j + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def sut_prologue(sut_text: str, functions: list["FunctionInfo"], header_sut: bool = False) -> list[str]:
    """A2-21ad: the SUT's preprocessor prologue - its #include, #define, #undef and conditional directives
    before its first function definition, in their order, without includes of source files. Conditionals still
    open there are closed. A header SUT has none: its directives start with its own include guard."""
    if header_sut:
        return []
    first = min((f.line_start for f in functions if f.line_start > 0), default=0)
    rows = without_comments(sut_text).split("\n")
    rows = rows[: first - 1] if first > 0 else rows
    out, depth, i = [], 0, 0
    while i < len(rows):
        line = rows[i].strip()
        while line.endswith("\\") and i + 1 < len(rows):
            i += 1
            line = line[:-1].rstrip() + " " + rows[i].strip()
        i += 1
        m = PROLOGUE_DIRECTIVE.match(line)
        if not m:
            continue
        kind, rest = m.group(1), m.group(2)
        if kind == "include" and re.search(r"[\"<][^\">]+\.(?:c|cc|cpp|cxx|c\+\+)[\">]", rest, re.I):
            continue                                              # never a source file [A2-21w]
        if kind == "pragma" and re.match(r"\s*once\b", rest):
            continue
        if kind in ("if", "ifdef", "ifndef"):
            depth += 1
        elif kind == "endif":
            if depth == 0:
                continue
            depth -= 1
        out.append("#" + kind + rest)
    return out + ["#endif"] * depth


class InterfaceMethod(Contract):
    """A pure virtual method of an injected interface [A1-DA17]."""
    name: str
    return_type: str = "void"
    params: list[list[str]] = Field(default_factory=list)   # [[type, name], ...]
    suffix: str = ""                                         # " const", " noexcept" ...


class InterfaceInfo(Contract):
    """A1-DA17: a C++ class with pure virtual methods that the SUT receives by reference or pointer."""
    name: str                                                # qualified, e.g. "hw::IEncoder"
    header: str = ""
    methods: list[InterfaceMethod] = Field(default_factory=list)


class LayoutProfile(Contract):
    """A1-16: everything later agents need to know about the repository."""
    repo_root: str
    sut_file: str
    language: Literal["c", "cpp"]
    component_root: str
    include_dirs: list[str] = Field(default_factory=list)
    headers: list[str] = Field(default_factory=list)          # headers declaring the SUT's API
    extra_sources: list[str] = Field(default_factory=list)    # other production files the SUT needs
    defines: list[str] = Field(default_factory=list)
    build_system: str = "none"
    test_dirs: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    stub_dirs: list[str] = Field(default_factory=list)
    stub_files: list[str] = Field(default_factory=list)
    writable_test_dir: str
    writable_stub_dir: str
    suite_file: str                                           # where the generated suite lives
    framework: str = "ut_harness"                             # ut_harness | existing:<name>
    existing_tests: list[TestCaseInfo] = Field(default_factory=list)
    executable_tests: list[str] = Field(default_factory=list)  # existing test files Agent3 may build
    functions: list[FunctionInfo] = Field(default_factory=list)
    sut_sha256: str
    # Flags of the real build for the SUT (-D, -U, -I, -std), from the build description [A1-16a].
    compile_flags: list[str] = Field(default_factory=list)
    build_flags_source: str = ""                              # BuildDescription.source, "" = not captured yet
    build: BuildDescription | None = None
    dependencies: DependencyPlan | None = None
    enums: list[EnumInfo] = Field(default_factory=list)
    sut_state: list[StateVar] = Field(default_factory=list)      # A1-DA15, A2-21s
    local_statics: list[StateVar] = Field(default_factory=list)  # "function::variable" [A1-DA18, A3-45c]
    interfaces: list[InterfaceInfo] = Field(default_factory=list)   # injected interfaces [A1-DA17, A2-21aa]
    header_sut: bool = False                                     # the SUT is a header [A1-DA19]
    requirement_files: list[str] = Field(default_factory=list)  # ReqIF / Markdown requirement documents [A1-25x]
    requirement_links: list[RequirementLink] = Field(default_factory=list)   # linked by the brain [A1-PL13]
    project: str = ""                                            # the tested repository [ARCH-23]
    typedefs: dict[str, str] = Field(default_factory=dict)      # typedef -> the type it names [A1-DA21]
    private_types: list[str] = Field(default_factory=list)      # types only the SUT file declares [A2-21ad]
    given_requirements: list[RequirementDocument] = Field(default_factory=list)   # given for the run [A1-RQ1]
    requirement_notes: list[str] = Field(default_factory=list)  # not read, no ID, conflicts [A1-RQ2, RQ4, RQ6]
    blocked: bool = False
    blocked_reason: str | None = None


# ============================================================================ build, run, coverage (Agent3)
class Diagnostic(Contract):
    file: str = ""
    line: int = 0
    column: int = 0
    severity: str = "error"
    message: str
    category: str = "other"          # missing_header, undeclared_symbol, undefined_reference, ...
    symbol: str = ""
    role: str = ""                   # sut | test | stub | harness | build


class TestResult(Contract):
    name: str
    status: Literal["passed", "failed", "crashed", "skipped", "timeout"]
    file: str = ""
    line: int = 0
    message: str = ""
    expected: str = ""
    actual: str = ""


class MetricValue(Contract):
    covered: int = 0
    total: int = 0
    percent: float = 0.0
    status: Literal["complete", "incomplete", "not_applicable", "not_measured"] = "not_measured"


def metric_text(m: MetricValue) -> str:
    """COV-8, VIEW-5: "NA" when there is nothing to measure, never 0 % or 100 %."""
    if m.status == "not_applicable":
        return "NA"
    if m.status == "not_measured":
        return "not measured"
    return f"{m.percent:g} %"


def metric(covered: int, total: int, measured: bool = True) -> MetricValue:
    if not measured:
        return MetricValue(status="not_measured")
    if total == 0:
        return MetricValue(covered=0, total=0, percent=100.0, status="not_applicable")
    pct = round(100.0 * covered / total, 2)
    return MetricValue(covered=covered, total=total, percent=pct,
                       status="complete" if covered >= total else "incomplete")


# ============================================================================ coverage targets [COV-1, COV-2]
ASIL_LEVELS = ("QM", "A", "B", "C", "D")
# The lowest targets the requirements allow (COV-1); a configuration may only be stricter.
MIN_TARGETS = {level: {"LC": "100", "SC": "100", "BC": "100", "DC": "100",
                       "MCDC": "100" if level in ("C", "D") else ">95"} for level in ASIL_LEVELS}
DEFAULT_ITERATIONS = 5


def parse_target(text: str | float | int) -> tuple[float, bool]:
    """"100" -> (100.0, False) means at least 100; ">95" -> (95.0, True) means above 95."""
    raw = str(text).strip().replace("%", "").replace(" ", "")
    strict = raw.startswith(">")
    value = float(raw.lstrip(">=") or "100")
    if not 0 <= value <= 100:
        raise ValueError(f"coverage target out of range: {text}")
    return value, strict


def target_met(percent: float, target: str) -> bool:
    value, strict = parse_target(target)
    return percent > value if strict else percent >= value


def target_text(target: str) -> str:
    value, strict = parse_target(target)
    return ("above " if strict else "") + f"{value:g} %"


def _stricter_or_equal(a: str, b: str) -> bool:
    (va, sa), (vb, sb) = parse_target(a), parse_target(b)
    return va > vb or (va == vb and (sa or not sb))


def coverage_policy(sut_file: str, section: dict[str, Any] | None) -> tuple[str, str, dict[str, str], int]:
    """(ASIL level, where it came from, targets per metric, iteration limit) for one SUT file. Every file is
    ASIL D unless the configuration names another level for it (COV-2); targets never below COV-1."""
    import fnmatch
    section = section or {}
    level, source = str(section.get("default_level", "D")).upper(), "default"
    for pattern, named in (section.get("files") or {}).items():
        if fnmatch.fnmatch(sut_file.replace("\\", "/"), pattern):
            level, source = str(named).upper(), f"configuration: {pattern}"
            break
    if level not in ASIL_LEVELS:
        raise ba.AgentFault("CONFIGURATION_ERROR", f"Unknown ASIL level {level!r}; use one of {', '.join(ASIL_LEVELS)}.")
    targets = dict(MIN_TARGETS[level])
    for metric, value in ((section.get("levels") or {}).get(level) or {}).items():
        if metric in targets:
            parse_target(value)
            if not _stricter_or_equal(str(value), targets[metric]):
                raise ba.AgentFault("CONFIGURATION_ERROR", f"coverage.levels.{level}.{metric} = {value} is below the "
                                    f"lowest target the requirements allow ({targets[metric]}, COV-1).")
            targets[metric] = str(value)
    iterations = int((section.get("iterations") or {}).get(level, DEFAULT_ITERATIONS))
    if iterations < 1:
        raise ba.AgentFault("CONFIGURATION_ERROR", "coverage.iterations must be at least 1 (COV-9).")
    return level, source, targets, iterations


class Metrics(Contract):
    LC: MetricValue = Field(default_factory=MetricValue)
    SC: MetricValue = Field(default_factory=MetricValue)
    BC: MetricValue = Field(default_factory=MetricValue)
    DC: MetricValue = Field(default_factory=MetricValue)
    MCDC: MetricValue = Field(default_factory=MetricValue)


class GapItem(Contract):
    gap_id: str
    function: str
    metric: Metric
    line: int
    column: int = 0
    detail: str = ""
    condition_index: int | None = None         # MC/DC condition (0-based) or branch outcome
    observed: list[str] = Field(default_factory=list)   # condition vectors seen at this decision [A3-71a]
    required: list[str] = Field(default_factory=list)   # vectors still needed (filled by Agent1) [A1-18b]
    attempts: int = 0
    estimated: bool = False
    variant_proofs: list["VariantProof"] = Field(default_factory=list)   # per build variant [A1-37b, A1-37c]


class VariantProof(Contract):
    """The proof of A1-37 for one build variant of the SUT file [A1-37b]. Analysis only (A1-37d)."""
    variant: str
    tested: bool = False
    macros: dict[str, int] = Field(default_factory=dict)   # the variant-dependent macros the item depends on
    proof: str = ""                                       # empty: no proof in this variant


class FunctionCoverage(Contract):
    name: str
    line_start: int = 0
    line_end: int = 0
    executed: bool = False
    metrics: Metrics = Field(default_factory=Metrics)
    gaps: list[GapItem] = Field(default_factory=list)


class CoverageReport(Contract):
    toolchain: str
    definitions: str
    measured: bool = True
    file_metrics: Metrics = Field(default_factory=Metrics)
    functions: list[FunctionCoverage] = Field(default_factory=list)
    covered_items: list[str] = Field(default_factory=list)    # stable ids of covered items [A3-108a]


class Failure(Contract):
    failure_id: str
    kind: Literal["compile", "link", "test", "crash", "timeout", "regression", "environment"]
    classification: Category
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    deterministic: bool = True
    route: Literal["agent1", "agent3", "agent4", "stop"]
    file: str = ""
    line: int = 0
    symbol: str = ""
    diagnostic: str = ""
    affected_test: str = ""
    affected_function: str = ""
    evidence: list[str] = Field(default_factory=list)


class Finding(Contract):
    """A static-analyzer report in the SUT [A4-32a]."""
    file: str
    line: int
    function: str = ""
    checker: str = ""
    message: str


class VerificationResult(Contract):
    run_id: str
    stage: Literal["probe", "build_check", "verify", "observe"]
    sut_sha256: str
    suite_sha256: str
    compiler: str = ""
    compiler_version: str = ""
    harness_version: str = HARNESS_VERSION
    build_ok: bool = False
    link_ok: bool = False
    run_ok: bool = False
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    undefined_symbols: list[str] = Field(default_factory=list)
    tests: list[TestResult] = Field(default_factory=list)
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    coverage: CoverageReport | None = None
    failures: list[Failure] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    newly_covered: int = 0
    run_dir: str = ""
    manifest: str = ""
    observed: dict[str, str] = Field(default_factory=dict)
    mcdc_incomplete: list[str] = Field(default_factory=list)   # decisions the compiler did not instrument
    # observe stage, Clang/LLVM: covered items and executed MC/DC vectors per test (for minimization)
    test_items: dict[str, list[str]] = Field(default_factory=dict)
    analyzer_findings: list[Finding] = Field(default_factory=list)
    analyzer_run: bool = False
    build_source: str = ""               # where the SUT's flags came from [A3-BD10]
    decision: Literal["complete", "gaps_remain", "failures", "setup_blocked", "environment_error"]
    reason: str = ""
    verified: bool = False
    # generated tests that did not compile: left out of this run, which measured the others [A3-CL12]
    quarantined: list[Failure] = Field(default_factory=list)
    register_pages: list[str] = Field(default_factory=list)   # target register pages in host memory [A3-HW1]
    host_notes: list[str] = Field(default_factory=list)       # target keywords, target asm, data model [A3-HW2..HW4]
    repaired_files: dict[str, str] = Field(default_factory=dict)   # generated files repaired by the brain [A3-CL14]


# ============================================================================ planning (Agent1)
class EvidenceRef(Contract):
    evidence_id: str
    evidence_class: EvidenceClass
    source: str
    line: int = 0
    text: str
    # "" = found by a rule; otherwise linked to the function by the brain ("brain p=0.82") or by the
    # retrieval loop ("lookup"): usable for planning, never for a source fix [A1-PL13, A4-23i].
    linked_by: str = ""


def evidence_key(e: "EvidenceRef") -> str:
    """Stable identity of an evidence item across plans: where it is [A1-PL13]."""
    return f"{e.source}:{e.line}"


class StubSpec(Contract):
    function: str
    declaration: str                 # e.g. "int read_sensor(int id)"
    return_type: str
    params: list[list[str]] = Field(default_factory=list)   # [[type, name], ...]
    header: str = ""                 # header that declares it ("" = declared in the SUT)
    kind: Literal["function", "method", "constructor", "variable"] = "function"
    qualified_name: str = ""         # C++: ns::Class::method
    method_suffix: str = ""          # C++: " const", " noexcept"
    var_type: str = ""               # variable: the declaration without "extern", e.g. "const int calib[4]"
    is_const: bool = False
    initializer: str = ""            # const variable: its value for the whole test program [A2-21j]
    namespace: str = ""              # C++: the declaring namespace; its types resolve there [A2-21ad]


class FuzzInput(Contract):
    """Concrete inputs found by coverage-guided search that reach still uncovered items [A3-FZ4]."""
    function: str
    args: list[str] = Field(default_factory=list)         # one C/C++ expression per parameter
    setup: list[str] = Field(default_factory=list)        # statements before the call (locals, stub values)
    covers: list[str] = Field(default_factory=list)       # gap items this input reaches


class NamedValue(Contract):
    name: str
    value: str                         # a C/C++ expression


class StubValues(Contract):
    stub: str                          # function name of a stub of the plan
    returns: list[str] = Field(default_factory=list)   # one value per call, the last one stays [A2-21g]


class ExpectedCheck(Contract):
    expr: str                          # what to check after the call (an output variable, a stub's call count)
    value: str


class Quote(Contract):
    evidence_id: str
    text: str                          # exact text of that evidence item [A1-25h]


GapClass = Literal["confirmed_gap", "characterization_gap", "conflicting_evidence", "insufficient_evidence"]


class Scenario(Contract):
    scenario_id: str
    function: str
    gap_ids: list[str] = Field(default_factory=list)
    description: str
    required_conditions: list[str] = Field(default_factory=list)
    stub_hints: list[str] = Field(default_factory=list)
    expected: str = ""
    evidence: list[EvidenceRef] = Field(default_factory=list)
    strongest_class: EvidenceClass = "E8"
    characterization: bool = True     # A1-25g: only E8 evidence
    inputs: list[FuzzInput] = Field(default_factory=list)   # concrete inputs from the fuzzer [A1-18c]
    # A brain-made scenario, checked by Agent1 before Agent2 receives it [A1-PL3, A1-PL4, A1-PL10]
    made_by: str = "rule"              # rule | brain:<name>/<model>
    checked: bool = False              # passed every check of A1-PL4
    classification: GapClass | Literal[""] = ""
    purpose: str = ""
    test_name: str = ""                # test_<what>_<behaviour> [A2-21q]
    args: list[NamedValue] = Field(default_factory=list)
    setup: list[str] = Field(default_factory=list)
    stub_values: list[StubValues] = Field(default_factory=list)
    variables: list[NamedValue] = Field(default_factory=list)
    expected_return: str = ""          # asserted only for a normative scenario [A1-PL4 c]
    expected_after: list[ExpectedCheck] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    linked: bool = False               # rests on evidence linked by the brain [A1-PL13]


class RepairItem(Contract):
    item_id: str
    owner: Literal["agent2", "agent4"]
    target_file: str
    issue: str
    diagnostic: str = ""
    symbol: str = ""
    line: int = 0
    affected_test: str = ""
    classification: str = ""
    attempts: int = 0
    action: Literal["rewrite", "drop"] = "rewrite"     # A2-21p


class UTPlan(Contract):
    plan_id: str
    stage: Literal["setup", "coverage", "done", "blocked"]
    layout: LayoutProfile
    create_suite: bool = False
    stubs: list[StubSpec] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    repairs: list[RepairItem] = Field(default_factory=list)
    gaps: list[GapItem] = Field(default_factory=list)
    keep_tests: list[str] = Field(default_factory=list)       # verified tests to preserve [A3-97]
    drop_tests: list[str] = Field(default_factory=list)       # generated tests to rewrite
    open_items: list[str] = Field(default_factory=list)
    files_to_edit: list[str] = Field(default_factory=list)
    files_to_create: list[str] = Field(default_factory=list)
    reason: str = ""
    planning: list[str] = Field(default_factory=list)       # who planned what, per function [A1-PL8]
    conflicts: list[str] = Field(default_factory=list)      # conflicting evidence with sources and ranks [A1-PL5]
    lookups: list[str] = Field(default_factory=list)        # retrieval-loop steps [A1-25s, A1-PL6]
    disputed_evidence: list[str] = Field(default_factory=list)   # evidence keys not to plan again [A1-PL13]
    proposed_dependencies: list[Dependency] = Field(default_factory=list)   # declarations proposed [A1-PL14]


class FuzzResult(Contract):
    inputs: list[FuzzInput] = Field(default_factory=list)
    executions: int = 0
    functions: list[str] = Field(default_factory=list)      # fuzzed
    unsupported: list[str] = Field(default_factory=list)    # "fn: reason"
    crashes: list[str] = Field(default_factory=list)        # inputs that crashed the SUT (reported, never asserted)
    reason: str = ""
    engine: str = ""                                        # "libFuzzer" or "ut_search" [A3-FZ11]


class PlanRequest(Contract):
    layout: LayoutProfile
    last: VerificationResult | None = None
    iteration: int = 0
    attempts: dict[str, int] = Field(default_factory=dict)
    existing_suite: str = ""          # current generated suite source (for repairs)
    fuzz: FuzzResult | None = None    # inputs from Agent3's coverage-guided search [A1-18c]
    test_items: dict[str, list[str]] = Field(default_factory=dict)   # what each test reached [A1-PL7, A1-PL12]
    disputed_evidence: list[str] = Field(default_factory=list)       # A1-PL13
    avoid_brains: list[str] = Field(default_factory=list)            # COV-6: ask the next brain
    failed_tests: dict[str, str] = Field(default_factory=dict)   # test -> why it failed in this run [A1-PL8]


class DependencyRequest(Contract):
    """A1-DA1: analyze the SUT with its recorded flags and decide every external dependency."""
    layout: LayoutProfile
    build: BuildDescription
    last: VerificationResult | None = None      # revisions from Agent3's results [A1-DA12]
    attempts: dict[str, int] = Field(default_factory=dict)


class CaptureRequest(Contract):
    """A3-BD1: run the project's own build on the working copy and record the compiler calls."""
    layout: LayoutProfile
    command: str = ""                            # configured build command [A3-BD2]
    compile_commands: str = ""                   # a compilation database supplied by the client
    cache_key: str = ""                          # tested commit + build configuration [ORC-22]


class FlagRequest(Contract):
    """A3-BD11: ask the brain for missing flags of an estimated build description."""
    layout: LayoutProfile
    build: BuildDescription


class FuzzRequest(Contract):
    layout: LayoutProfile
    bundle: FileActionBundle | None = None
    functions: list[str] = Field(default_factory=list)
    gaps: list[GapItem] = Field(default_factory=list)
    covered: list[str] = Field(default_factory=list)       # items the suite already covers
    seconds: int = Field(default=60, ge=1, le=3600)


class EvidenceRequest(Contract):
    layout: LayoutProfile
    function: str
    question: str = ""


class RequirementStatusRequest(Contract):
    """A1-RQ5: the requirement status per function, from the delivered suite and the tests that passed."""
    layout: LayoutProfile
    suite: str = ""
    passed: list[str] = Field(default_factory=list)


class FunctionRequirements(Contract):
    function: str
    requirements: list[str] = Field(default_factory=list)  # "REQ-1 (rule|brain p=0.80) source:line: asserted by ..."
    stated: bool = False                                   # any stated expected behaviour (E1 to E3)


class RequirementStatus(Contract):
    functions: list[FunctionRequirements] = Field(default_factory=list)


class EvidenceResult(Contract):
    function: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    exhausted: bool = False


# ============================================================================ generation (Agent2)
class ExpectationChange(Contract):
    test: str
    old: str
    new: str
    reason: str
    evidence: str = ""


class FileAction(Contract):
    path: str                          # relative to repo_root
    action: Literal["create", "replace"]
    role: Literal["testsuite", "stub", "helper", "sut_access"]   # sut_access: includes the SUT so static functions are testable [A2-21k]
    content: str
    evidence_classes: dict[str, str] = Field(default_factory=dict)   # test name -> strongest class [A2-21d]
    linked_tests: list[str] = Field(default_factory=list)             # rest on brain-linked evidence [A3-CL11]


class FileActionBundle(Contract):
    bundle_id: str
    actions: list[FileAction] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    characterization_tests: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)   # pending UT_OBSERVE ids [A2-21f]
    expectation_changes: list[ExpectationChange] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)      # structural checks performed
    claims_verification: bool = False                       # must stay False [A2-33]
    new_tests: list[str] = Field(default_factory=list)       # added by this bundle (minimization candidates)
    notes: list[str] = Field(default_factory=list)


class KBExample(Contract):
    example_id: str
    kind: Literal["positive", "gap_closure", "negative"]
    language: str
    framework: str
    category: str
    snippet: str
    reason: str = ""
    uses: int = 1


class KBGuidance(Contract):
    positive: list[KBExample] = Field(default_factory=list)
    negative: list[KBExample] = Field(default_factory=list)


class GenerationRequest(Contract):
    plan: UTPlan
    guidance: KBGuidance = Field(default_factory=KBGuidance)
    existing_suite: str = ""


class FinalizeRequest(Contract):
    bundle: FileActionBundle
    observed: dict[str, str]
    test_items: dict[str, list[str]] = Field(default_factory=dict)


# ============================================================================ Agent3 requests
class VerifyRequest(Contract):
    layout: LayoutProfile
    stage: Literal["probe", "build_check", "verify", "observe"]
    targets: dict[str, str] = Field(default_factory=dict)     # COV-1 per metric ("100", ">95"); empty = 100
    bundle: FileActionBundle | None = None
    previous: VerificationResult | None = None
    source_overrides: dict[str, str] = Field(default_factory=dict)   # path -> content (Agent4 patches)
    static_analysis: bool = False      # also run the static analyzer on the SUT [A4-32a]


class HarnessRequest(Contract):
    """The test harness that the delivered tests include [A3-PR1]."""
    language: Literal["c", "cpp"] = "c"


class HarnessFiles(Contract):
    files: dict[str, str] = Field(default_factory=dict)      # file name -> content


class OracleRequest(Contract):
    layout: LayoutProfile
    test: str
    function: str
    original_suite: str
    changed_suite: str
    bundle: FileActionBundle


class OracleResult(Contract):
    test: str
    decision: Literal["accepted", "oracle_weakening", "oracle_strength_undetermined"]
    mutants: int = 0
    killed_by_reference: int = 0
    killed_by_changed: int = 0
    reason: str = ""
    operators: list[str] = Field(default_factory=list)


class FaultCheckRequest(Contract):
    """Does a source-change suggestion weaken the fault detection of the existing tests? [COV-14 .. COV-19]"""
    layout: LayoutProfile
    bundle: FileActionBundle | None = None
    suggestion_id: str
    original_source: str
    suggested_source: str
    tests: list[str] = Field(default_factory=list)       # the tests that pass on the trial copy [COV-15]
    functions: list[str] = Field(default_factory=list)   # functions the suggestion names, besides the changed ones


class FaultMutant(Contract):
    """One compared mutant [COV-16, COV-18]: the same operator at the same place on an unchanged line."""
    mutant_id: str                       # L<original line>:<operator>.<occurrence>
    label: str                           # as A3-85c records it, e.g. "L12:<-><="
    function: str
    original_line: int
    suggested_line: int
    detected_before: bool
    detected_after: bool | None = None   # None: not run, because it was not detected before [COV-19]


class FaultCheckResult(Contract):
    suggestion_id: str
    decision: Literal["kept", "weakened", "not_applicable", "not_checked"]
    functions: list[str] = Field(default_factory=list)
    compared: int = 0                    # mutants on unchanged lines [COV-16]
    detected_before: int = 0
    still_detected: int = 0
    changed_line_mutants: int = 0        # on added, removed or changed lines: counted only [COV-16]
    missed: list[str] = Field(default_factory=list)       # labels of mutants no longer detected
    mutants: list[FaultMutant] = Field(default_factory=list)
    operators: list[str] = Field(default_factory=list)
    max_mutants: int = 0
    time_limit_s: float = 0.0
    workers: int = 0
    elapsed_s: float = 0.0
    compiler: str = ""
    reason: str = ""

    def summary(self) -> str:
        """The line shown with a suggestion [COV-18]."""
        changed = (f"; {self.changed_line_mutants} mutant(s) on changed lines not compared"
                   if self.changed_line_mutants else "")
        if self.decision == "not_applicable":
            return "fault detection: NA - no unchanged line of the changed function(s) has a mutant" + changed
        if self.decision == "not_checked":
            return "fault detection could not be checked: " + self.reason
        if self.decision == "weakened":
            return (f"fault detection weakened: {len(self.missed)} of {self.detected_before} compared mutants are no "
                    f"longer detected ({', '.join(self.missed[:5])})" + changed)
        if not self.detected_before:
            return (f"fault detection: none of the {self.compared} compared mutants was detected before, so none "
                    f"can be lost" + changed)
        return f"fault detection: {self.still_detected} of {self.detected_before} compared mutants still detected" + \
            changed


# ============================================================================ Agent4
class CorrectionRequest(Contract):
    layout: LayoutProfile
    failure: Failure
    test_source: str = ""
    evidence: list[EvidenceRef] = Field(default_factory=list)


class SourcePatch(Contract):
    patch_id: str
    file: str
    function: str
    original_fragment: str
    corrected_fragment: str
    rationale: str
    failure_id: str
    status: Literal["candidate_source_fix", "verified_source_fix", "rejected"] = "candidate_source_fix"
    patched_content: str = ""
    author: str = ""                  # the brain that wrote it [A4-23h]
    changed_lines: int = 0            # size of the change [A4-23g]
    changed_chars: int = 0


class CandidateSet(Contract):
    """A4-23g: different candidate fixes for one failure."""
    patches: list[SourcePatch] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ReviewRequest(Contract):
    """A4-23h: a second brain reviews a verified candidate before it is published."""
    layout: LayoutProfile
    failure: Failure
    patch: SourcePatch
    verification: VerificationResult
    evidence: list[EvidenceRef] = Field(default_factory=list)


class ReviewResult(Contract):
    decision: Literal["agree", "block"]
    reason: str = ""
    reviewer: str = ""
    same_brain: bool = False


class PublishRequest(Contract):
    """Deliver a source fix that Agent3 has reverified [A4-23c, A4-33]."""
    layout: LayoutProfile
    patch: SourcePatch
    verification: VerificationResult          # Agent3's reverification of the patched source
    original_repo: str                        # the user's repository (the pipeline works on a copy)
    output_dir: str
    # Characterization (E8) tests recorded the old behaviour; they may fail after a fix and are
    # then regenerated [A1-25g, A4-52].
    characterization_tests: list[str] = Field(default_factory=list)
    branch_prefix: str = "ut-source-fix/"
    baseline_findings: list[Finding] = Field(default_factory=list)   # analyzer findings before the fix [A4-32a]
    evidence: list[EvidenceRef] = Field(default_factory=list)        # what the fix relies on [A4-23f]
    commit: bool = True               # False: repository input; delivered by the review pull request [ORC-25]


class PublishCheck(Contract):
    ok: bool
    reason: str = ""


class PublishResult(Contract):
    patch_id: str
    status: Literal["verified_source_fix", "rejected"]
    patch_file: str = ""
    branch: str = ""
    commit: str = ""
    reason: str = ""


# ============================================================================ KB
class GuidanceRequest(Contract):
    language: str
    framework: str
    categories: list[str] = Field(default_factory=list)
    k: int = Field(default=4, ge=1, le=12)


class LearningEvent(Contract):
    kind: Literal["positive", "gap_closure", "negative"]
    language: str
    framework: str
    category: str
    snippet: str
    reason: str = ""
    verification: dict[str, Any] = Field(default_factory=dict)


class LearnAck(Contract):
    stored: bool
    example_id: str = ""
    duplicate: bool = False
    reason: str = ""


# ============================================================================ pipeline
class PipelineRequest(Contract):
    repo_root: str
    sut_file: str
    test_dir: str | None = None
    stub_dir: str | None = None
    build_command: str = ""          # overrides the derived build command [A3-BD2]
    compile_commands: str = ""       # a compile_commands.json supplied by the client [A3-BD2]
    requirements: list[str] = Field(default_factory=list)   # requirement documents: files or folders [A1-RQ1]


class RepositoryRequest(Contract):
    """ORC-17: a git repository link (or a local folder for pilots and development), one or more
    SUT files relative to it - none: every production file (ORC-35) - and optionally a branch (default: the
    server's default branch)."""
    repository: str
    sut_files: list[str] = Field(default_factory=list)
    branch: str = ""
    test_dir: str | None = None
    stub_dir: str | None = None
    build_command: str = ""
    compile_commands: str = ""
    requirements: list[str] = Field(default_factory=list)   # requirement documents: files or folders [A1-RQ1]


class IterationRecord(Contract):
    iteration: int
    stage: str
    metrics: Metrics | None = None
    tests_added: int = 0
    passed: int = 0
    failed: int = 0
    newly_covered: int = 0
    remaining_gaps: int = 0
    decision: str = ""


def open_items_count(coverage: Any) -> int:
    """Items still open over the five metrics (total - covered), for comparing two measured runs [COV-13]."""
    if coverage is None:
        return 0
    count = 0
    for name in METRICS:
        value = getattr(coverage.file_metrics, name)
        if value.status in ("complete", "incomplete"):
            count += max(0, value.total - value.covered)
    return count


class ItemEvidence(Contract):
    """COV-10: why one coverage item was not reached, from measured data and the agents' own analysis."""
    item_id: str
    metric: Metric
    function: str
    line: int = 0
    detail: str = ""
    missing: list[str] = Field(default_factory=list)          # MC/DC: the condition vectors still needed
    path: list[str] = Field(default_factory=list)             # conditions on the way to the item
    strategies: list[str] = Field(default_factory=list)       # COV-6, in the order tried
    rejected: list[str] = Field(default_factory=list)         # brain suggestions refused, with the reason
    proof: str = ""                                           # A1-37, when the item cannot be reached
    variants: list[str] = Field(default_factory=list)         # the proof per build variant [A1-37c]


class SuggestRequest(Contract):
    """COV-12: after a stop below target, Agent4 suggests source changes for the items not reached."""
    layout: LayoutProfile
    items: list[ItemEvidence]
    evidence: list[EvidenceRef] = Field(default_factory=list)
    max_suggestions: int = 3


class CoverageSuggestion(Contract):
    suggestion_id: str
    file: str
    function: str
    item_ids: list[str] = Field(default_factory=list)
    cause: Literal["condition_defect", "dead_code", "required_defensive_code", "not_controllable"]
    original_fragment: str
    corrected_fragment: str
    patched_content: str = ""
    rationale: str = ""
    requirement_ids: list[str] = Field(default_factory=list)
    with_requirements: bool = False
    author: str = ""


class SuggestionSet(Contract):
    suggestions: list[CoverageSuggestion] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PipelineResult(Contract):
    final_state: Literal["verified_measured", "incomplete"]
    reason: str
    sut_file: str
    language: str = ""
    compiler: str = ""
    framework: str = HARNESS_VERSION
    iterations: list[IterationRecord] = Field(default_factory=list)
    metrics: Metrics | None = None
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    unfinished_tests: list[str] = Field(default_factory=list)   # "name: crashed/timeout - signal or limit" [A3-29d]
    register_pages: list[str] = Field(default_factory=list)     # target register pages in host memory [A3-HW1]
    host_notes: list[str] = Field(default_factory=list)         # target keywords, asm, data model [A3-HW2..HW4]
    files_generated: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    characterization_tests: int = 0
    oracle_checks: list[OracleResult] = Field(default_factory=list)
    source_fixes: list[SourcePatch] = Field(default_factory=list)
    open_items: list[str] = Field(default_factory=list)
    asil: str = "D"                                   # COV-2
    asil_source: str = "default"                      # "default" or "configuration: <pattern>"
    targets: dict[str, str] = Field(default_factory=dict)       # COV-1, e.g. {"MCDC": ">95"}
    target_check: list[str] = Field(default_factory=list)       # COV-8: metric, target, measured, met
    not_reached: list[ItemEvidence] = Field(default_factory=list)   # COV-10
    action_needed: list[str] = Field(default_factory=list)          # COV-11
    suggestions: list[str] = Field(default_factory=list)            # COV-12, COV-13 (checked on a trial copy)
    output_dir: str = ""
    ledger: str = ""
    build: str = ""                                   # source and command of the build description [A3-BD10]
    build_estimated: bool = False                     # flags were guessed: results apply to them only
    dependencies: list[str] = Field(default_factory=list)     # "name: decision (who) - reason" [A1-DA11]
    function_coverage: list[FunctionCoverage] = Field(default_factory=list)   # per function [A3-63]
    fuzzing: list[str] = Field(default_factory=list)  # what coverage-guided search did [A3-FZ7]
    repository: str = ""                              # link or folder [ORC-12]
    branch: str = ""
    commit: str = ""                                  # the tested commit [ORC-19]
    delivery: str = ""                                # how the result reached the repository [ORC-12]
    previous_review: str = ""                         # state of the last review pull request [ORC-26]
    planning: list[str] = Field(default_factory=list)          # who planned what [A1-PL8]
    conflicts: list[str] = Field(default_factory=list)         # A1-PL5
    lookups: list[str] = Field(default_factory=list)           # A1-25s
    requirement_links: list[str] = Field(default_factory=list)  # linked by the brain [A1-PL13]
    requirement_documents: list[str] = Field(default_factory=list)   # "name sha256 (given as ...)" [A1-RQ3]
    requirement_status: list[FunctionRequirements] = Field(default_factory=list)   # per function [A1-RQ5]
    suggested_flags: list[str] = Field(default_factory=list)   # A3-BD13
    fix_candidates: list[str] = Field(default_factory=list)    # candidates not chosen, with the reason [A4-23g]
    reviews: list[str] = Field(default_factory=list)           # second opinions [A4-23h]
    token_use: list[str] = Field(default_factory=list)         # brain calls per step [ARCH-15]
    knowledge_packs: list[str] = Field(default_factory=list)   # name, version, SHA-256 [KP-7]
    escalations: list[str] = Field(default_factory=list)       # rules that needed the brain, with outcome [ARCH-22]


class DeliveryRecord(Contract):
    """ORC-24 to ORC-31: what reached the repository, and how."""
    kind: Literal["direct_commit", "pull_request", "review_branch", "local", "written_to_folder", "none"]
    sut_files: list[str] = Field(default_factory=list)
    branch: str = ""
    commit: str = ""
    url: str = ""
    reason: str = ""
    skipped: list[str] = Field(default_factory=list)      # not overwritten [ORC-14]


class SelectRequest(Contract):
    """ORC-35: the production files of a working tree, selected by Agent1 without an LLM."""
    repo_root: str
    compile_commands: str = ""                     # the client's compile database: only its files [A3-BD2]


class FileSelection(Contract):
    selected: list[str] = Field(default_factory=list)       # relative to the repository, in path order
    left_out: list[str] = Field(default_factory=list)       # "path: reason"


class RepositoryResult(Contract):
    repository: str
    branch: str = ""
    commit: str = ""
    selection: FileSelection | None = None         # when the request named no SUT file [ORC-35]
    results: list[PipelineResult] = Field(default_factory=list)
    deliveries: list[DeliveryRecord] = Field(default_factory=list)
    output_dir: str = ""
    reason: str = ""


# ============================================================================ shared suite helpers
_TEST_HEAD = re.compile(r"(?:/\*\s*evidence:\s*E\d(?:\s+linked)?\s*\*/\s*\n)?UT_TEST\(\s*(\w+)\s*\)\s*\{", re.M)


def test_sources(suite: str) -> dict[str, str]:
    """name -> verbatim source of every UT_TEST in a generated suite (with its evidence comment)."""
    out = {}
    for m in _TEST_HEAD.finditer(suite):
        depth, i = 0, m.end() - 1
        while i < len(suite):
            if suite[i] == "{":
                depth += 1
            elif suite[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out[m.group(1)] = suite[m.start(): i + 1]
    return out


def test_at_line(suite: str, line: int) -> str:
    """The UT_TEST whose source holds ``line`` (1-based) of the suite, or ""."""
    if line <= 0:
        return ""
    offset = sum(len(row) + 1 for row in suite.split("\n")[: line - 1])
    for name, source in test_sources(suite).items():
        start = suite.find(source)
        if start <= offset <= start + len(source):
            return name
    return ""


def remove_tests(suite: str, names: list[str] | set[str]) -> str:
    """The suite without the named UT_TEST blocks and their UT_CASE registrations. Agent3 and the
    orchestrator use the same function, so the suite Agent3 measured is the suite delivered [A3-CL12]."""
    sources = test_sources(suite)
    for name in sorted(set(names)):
        if name in sources:
            source = sources[name]
            suite = suite.replace(source + "\n\n", "", 1) if source + "\n\n" in suite else suite.replace(source, "", 1)
        suite = re.sub(r"[ \t]*UT_CASE\(\s*" + re.escape(name) + r"\s*\)[ \t]*\r?\n?", "", suite)
    return suite


def without_tests(bundle: "FileActionBundle", names: list[str] | set[str], note: str = "") -> "FileActionBundle":
    """A copy of the bundle without the named tests [A3-CL12, A2-21i]."""
    gone = set(names)
    out = bundle.model_copy(deep=True)
    if not gone:
        return out
    for action in out.actions:
        if action.role == "testsuite":
            action.content = remove_tests(action.content, gone)
            for name in gone:
                action.evidence_classes.pop(name, None)
    out.tests = [t for t in out.tests if t not in gone]
    out.characterization_tests = [t for t in out.characterization_tests if t not in gone]
    out.new_tests = [t for t in out.new_tests if t not in gone]
    out.observations = [o for o in out.observations if not any(o == n or o.startswith(n + "_") for n in gone)]
    if note:
        out.notes.append(note)
    return out


def suite_of(bundle: "FileActionBundle | None") -> str:
    if bundle is None:
        return ""
    return next((a.content for a in bundle.actions if a.role == "testsuite"), "")


def check_signature(layout: "LayoutProfile", request: Any) -> None:
    """ORC-10: a request bound to another SUT version than its payload is refused."""
    expected = (getattr(request, "signatures", None) or {}).get("sut")
    if expected and expected != layout.sut_sha256:
        raise ba.ContractRejected("stale SUT signature: the request is bound to another SUT version [ORC-10]")


def check_suite_signature(suite: str, request: Any) -> None:
    """ORC-10: the test-suite signature must match the suite the payload carries."""
    expected = (getattr(request, "signatures", None) or {}).get("test_suite")
    if expected and expected != hashlib.sha256(suite.encode("utf-8")).hexdigest():
        raise ba.ContractRejected("stale test-suite signature [ORC-10]")


def real_source_error(definition: str, text: str) -> bool:
    """A1-DA12: a compiler error located in the file of a real definition (by path, or by its file name as the
    compiler prints it from the file's folder)."""
    name = re.escape(Path(definition).name)
    return bool(re.search(r"(?:^|[\s/\\(])" + name + r":\d+(?::\d+)?:\s*(?:fatal\s+)?error\b", text))


def safe_relpath(path: str) -> bool:
    """A repository-relative path that cannot leave the repository."""
    p = path.replace("\\", "/")
    return bool(p) and not p.startswith("/") and not re.match(r"^[A-Za-z]:", p) and ".." not in p.split("/")


# ============================================================================ source text, byte-exact
def source_encoding(path: Path) -> str:
    """UTF-8 when the file decodes as UTF-8, otherwise cp1252 (common for MSVC sources), else latin-1."""
    data = path.read_bytes() if path.is_file() else b""
    for encoding in ("utf-8", "cp1252"):
        try:
            data.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "latin-1"


def read_source(path: Path) -> str:
    return path.read_bytes().decode(source_encoding(path))


def encode_like(path: Path, text: str) -> bytes:
    """Bytes of ``text`` in the encoding the file at ``path`` uses (UTF-8 for a new file)."""
    return text.encode(source_encoding(path) if path.is_file() else "utf-8", errors="strict")


# ============================================================================ git: commit to a separate branch
def git_exe() -> str:
    """git from the tool manifest [TP-10]: the system is not searched."""
    try:
        command = Tools.load().command("git")
    except ba.AgentFault:
        command = None
    if not command:
        raise ba.AgentFault("ENVIRONMENT_ERROR", f"git is not in the tool manifest: {Tools.SETUP_HINT}.")
    return command[0]


def _git(repo: Path, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None) -> str:
    proc = subprocess.run([git_exe(), "-C", str(repo), *args], capture_output=True, input=stdin,
                          stdin=None if stdin is not None else subprocess.DEVNULL,
                          env={**os.environ, **(env or {})}, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip()[:200] or f"git {args[0]} failed")
    return proc.stdout.decode("utf-8", "replace").strip()


def git_branch_head(repo: Path, branch: str) -> str:
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    except RuntimeError:
        return ""


def git_show(repo: Path, ref: str, rel_file: str) -> bytes | None:
    """The bytes of ``rel_file`` at ``ref``; None when the file is not in that commit."""
    proc = subprocess.run([git_exe(), "-C", str(repo), "show", f"{ref}:{rel_file.replace(chr(92), '/')}"],
                          capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    return proc.stdout if proc.returncode == 0 else None


def git_dirty(repo: Path, rel_file: str) -> bool:
    """True when the working-tree file differs from HEAD (uncommitted edits)."""
    try:
        _git(repo, "diff", "--quiet", "HEAD", "--", rel_file)
        return False
    except RuntimeError:
        return True


def commit_files_to_branch(repo: Path, files: dict[str, bytes], branch: str, message: str,
                           parent: str = "HEAD") -> tuple[str, str]:
    """Commits ``files`` (path -> bytes) on ``branch`` from ``parent`` without touching the working
    tree, the index or the current branch: a temporary index file [A4-23c]. ("", "") when the
    folder is not a git repository."""
    try:
        git_exe()
    except ba.AgentFault:
        return "", ""
    if not (repo / ".git").exists():
        return "", ""
    base = _git(repo, "rev-parse", parent)
    branch = re.sub(r"[^A-Za-z0-9._/-]", "-", branch)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index"), "GIT_AUTHOR_NAME": "ut-pipeline",
               "GIT_AUTHOR_EMAIL": "ut-pipeline@localhost", "GIT_COMMITTER_NAME": "ut-pipeline",
               "GIT_COMMITTER_EMAIL": "ut-pipeline@localhost"}
        _git(repo, "read-tree", base, env=env)
        for rel_file, content in sorted(files.items()):
            rel_file = rel_file.replace("\\", "/")
            blob = _git(repo, "hash-object", "-w", "--stdin", f"--path={rel_file}", stdin=content, env=env)
            listed = _git(repo, "ls-files", "-s", "--", rel_file, env=env)
            mode = listed.split()[0] if listed else "100644"
            _git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel_file}", env=env)
        tree = _git(repo, "write-tree", env=env)
        commit = _git(repo, "commit-tree", tree, "-p", base, "-m", message, env=env)
        _git(repo, "update-ref", f"refs/heads/{branch}", commit, env=env)
    return branch, commit


# ============================================================================ tools [TP-10]
DEFAULT_MANIFEST = Path(__file__).resolve().parent / "tools" / "toolchain.json"


def manifest_path(config_path: str | Path | None, section: dict[str, Any] | None = None) -> Path:
    """Where the tool manifest is: UT_TOOLS_MANIFEST, else compilation.tools_manifest of the config
    (relative to the config file), else tools/toolchain.json next to this file."""
    if os.environ.get("UT_TOOLS_MANIFEST"):
        return Path(os.environ["UT_TOOLS_MANIFEST"])
    if config_path and Path(config_path).is_file():
        import json as _json
        raw = _json.loads(Path(config_path).read_text(encoding="utf-8"))
        rel = (section or raw.get("compilation", {})).get("tools_manifest") or raw.get("compilation", {}).get(
            "tools_manifest")
        if rel:
            p = Path(rel)
            return p if p.is_absolute() else (Path(config_path).resolve().parent / p)
    return DEFAULT_MANIFEST


# ============================================================================ brain context [ARCH-11..17, KP]
# The brain steps of this application (KP-2); a knowledge pack names the steps it is for.
KNOWLEDGE_STEPS = ["A1.dependency", "A1.plan", "A1.links", "A1.declaration", "A2.tests", "A3.root_cause",
                   "A3.flags", "A4.candidates", "A4.review", "A4.suggest",
                   "A1.header", "A2.value", "A3.repair", "A3.host"]     # rule failures [ARCH-21]
# Priorities of the context parts of a brain call (ARCH-17): 0 is never shortened; the highest
# number is shortened or left out first when a call does not fit the brain's window.
P_FACTS, P_EVIDENCE, P_PACKS, P_TRIED, P_EXAMPLES = 0, 1, 2, 3, 4
APP_DIR = Path(__file__).resolve().parent


def part(name: str, text: str, priority: int = P_FACTS, min_chars: int = 0) -> ba.ContextPart:
    return ba.ContextPart(name=name, text=text, priority=priority, min_chars=min_chars)


def resolution_memory(config_path: str | Path | None) -> ba.ResolutionMemory:
    """ARCH-23: the resolution memory, kb.resolutions_path of ut_config.json, by default next to the KB's database.
    The project is set per request (resolution_project)."""
    section: dict[str, Any] = {}
    if config_path and Path(config_path).is_file():
        import json as _json
        section = _json.loads(Path(config_path).read_text(encoding="utf-8")).get("kb", {}) or {}
    path = section.get("resolutions_path") or str(Path(section.get("db_path") or "ut_kb.sqlite3").parent
                                                  / "ut_resolutions.sqlite3")
    base = Path(config_path).resolve().parent if config_path and Path(config_path).is_file() else APP_DIR
    path = Path(path) if Path(path).is_absolute() else base / path              # like the KB's db_path
    return ba.ResolutionMemory(path)


def resolution_project(agent: ba.AgentCore, layout: "LayoutProfile") -> None:
    """The tested repository the agent's resolutions belong to (ARCH-23): never shared between repositories."""
    if agent.resolutions is not None:
        agent.resolutions.project = layout.project or str(Path(layout.repo_root).resolve())


def knowledge_packs(config_path: str | Path | None) -> ba.KnowledgePacks | None:
    """The knowledge packs of the "knowledge" section of ut_config.json (KP-1, KP-3):
    {"enabled": true, "dirs": ["knowledge"], "project_dir": ".ut/knowledge"}; relative folders are
    in the application's folder. Without a section the application's knowledge/ folder is used.
    UT_KNOWLEDGE=off switches the packs off (the benchmark without packs, KP-8)."""
    section: dict[str, Any] = {}
    base = APP_DIR
    if config_path and Path(config_path).is_file():
        import json as _json
        section = _json.loads(Path(config_path).read_text(encoding="utf-8")).get("knowledge", {}) or {}
    if os.environ.get("UT_KNOWLEDGE", "").lower() in ("0", "off", "false", "no"):
        return None                                    # the benchmark without packs [KP-8]
    if not section.get("enabled", True):
        return None
    dirs = [Path(d) if Path(d).is_absolute() else base / d for d in section.get("dirs", ["knowledge"])]
    return ba.KnowledgePacks(dirs, steps=KNOWLEDGE_STEPS, project_dir=section.get("project_dir", ".ut/knowledge"))


# ============================================================================ viewer [VIEW-1, VIEW-2, VIEW-6]
VIEW_AGENTS = {  # technical name -> (role, one line on what it does, support agent)
    "agent_unittest_analyzer": ("Analyzer", "plans the tests", False),
    "agent_unittest_generator": ("Generator", "writes tests and stubs", False),
    "agent_unittest_compilation": ("Compilation", "builds, runs, measures", False),
    "agent_sourcecode_corrector": ("Source Code Corrector", "fixes source defects, suggests changes", False),
    "ut_fix_knowledgebase": ("KB", "examples from earlier runs", True),
}


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def variant_text(proofs: list["VariantProof"]) -> list[str]:
    """A1-37c in plain words: the tested variant decides; another variant without a proof may reach the
    item, which this run neither built nor measured (A1-37d)."""
    def values(v: "VariantProof") -> str:
        return f" ({', '.join(f'{k} = {n}' for k, n in v.macros.items())})" if v.macros else ""
    rows = []
    for v in sorted(proofs, key=lambda v: not v.tested):
        if v.tested:
            rows.append(f"tested variant {v.variant}{values(v)}: " +
                        ("cannot be reached" if v.proof else "no proof"))
        elif v.proof:
            rows.append(f"variant {v.variant}{values(v)}: cannot be reached there either")
        else:
            rows.append(f"variant {v.variant}{values(v)}: no proof - may be reachable there; not built or "
                        "measured by this run")
    return rows


def view_summary(label: str, model: Any) -> str | None:
    """Plain-language summaries of the main contracts (counts and states only, never code) [VIEW-6]."""
    def pct(v: "VerificationResult") -> str:
        if v.coverage is None:
            return ""
        m = v.coverage.file_metrics
        return f", MC/DC {m.MCDC.percent:g} %, branch {m.BC.percent:g} %"
    if isinstance(model, LayoutProfile):
        return f"{label}: {len(model.functions)} functions in {model.sut_file}"
    if isinstance(model, BuildDescription):
        return f"{label}: build description from {model.source}" + (" (estimated)" if model.estimated else "")
    if isinstance(model, UTPlan):
        fns = len({s.function for s in model.scenarios})
        return (f"{label}: plan ({model.stage}) with {len(model.scenarios)} scenarios for {fns} functions"
                + (f", {len(model.repairs)} repairs" if model.repairs else ""))
    if isinstance(model, FileActionBundle):
        suites = [a.path.rsplit("/", 1)[-1] for a in model.actions if a.role == "testsuite"]
        others = [a.path.rsplit("/", 1)[-1] for a in model.actions if a.role != "testsuite"]
        return (f"{label}: {', '.join(suites) or 'test suite'} with {_n(len(model.tests), 'test')} "
                f"({len(model.new_tests)} new)" + (f", plus {', '.join(others)}" if others else ""))
    if isinstance(model, VerificationResult):
        if not model.build_ok or not model.link_ok:
            return f"{label}: {model.stage}: the tests do not build yet ({len(model.failures)} problems)"
        return f"{label}: {model.stage}: {model.passed} of {model.total} tests passed{pct(model)}"
    if isinstance(model, FuzzResult):
        return f"{label}: coverage-guided search found {len(model.inputs)} new inputs"
    if isinstance(model, SelectRequest):
        return f"{label}: select the production files to test"
    if isinstance(model, FileSelection):
        return f"{label}: {_n(len(model.selected), 'production file')} selected, {len(model.left_out)} left out"
    if isinstance(model, RequirementStatusRequest):
        return f"{label}: requirement status per function"
    if isinstance(model, RequirementStatus):
        asserted = sum(1 for f in model.functions for r in f.requirements if ": asserted by" in r)
        return (f"{label}: {asserted} requirement(s) asserted by passed tests; "
                f"{sum(1 for f in model.functions if not f.stated)} function(s) without stated behaviour")
    if isinstance(model, CandidateSet):
        return f"{label}: {len(model.patches)} candidate source fixes"
    if isinstance(model, SuggestionSet):
        return f"{label}: {len(model.suggestions)} source-change suggestions for the developer"
    if isinstance(model, ReviewResult):
        return f"{label}: second opinion: {model.decision}"
    if isinstance(model, PublishResult):
        return f"{label}: fix {model.status.replace('_', ' ')}"
    if isinstance(model, PlanRequest):
        return f"{label}: plan request, iteration {model.iteration}"
    if isinstance(model, VerifyRequest):
        return f"{label}: {model.stage} request" + (f" with {len(model.bundle.tests)} tests" if model.bundle else "")
    if isinstance(model, GenerationRequest):
        plan = model.plan
        if plan.stage == "setup":
            return f"{label}: set up the test suite, stubs and harness" + \
                (f", {_n(len(plan.repairs), 'repair')}" if plan.repairs else " (no tests yet)")
        if not plan.scenarios and not plan.repairs:
            return f"{label}: no new scenario in the plan: keep the suite as it is"
        fns = sorted({s.function for s in plan.scenarios})
        items = sum(len(s.gap_ids) for s in plan.scenarios)
        return (f"{label}: implement {_n(len(plan.scenarios), 'scenario')} for {', '.join(fns[:3])}"
                + (" ..." if len(fns) > 3 else "") + (f" ({_n(items, 'coverage item')})" if items else "")
                + (f", {_n(len(plan.repairs), 'repair')}" if plan.repairs else ""))
    if isinstance(model, SuggestRequest):
        return f"{label}: suggest changes for {len(model.items)} items not reached"
    # VIEW-8: every other contract of the tools, in plain words
    if isinstance(model, DiscoverRequest):
        return f"{label}: find the layout of {model.sut_file}"
    if isinstance(model, CaptureRequest):
        how = "the configured command" if model.command else "the supplied compile_commands.json" \
            if model.compile_commands else "the build files"
        return f"{label}: capture the build of {model.layout.sut_file} from {how}"
    if isinstance(model, DependencyRequest):
        return f"{label}: decide stub or real for the dependencies of {model.layout.sut_file}" + \
            (" again, with the last results" if model.last is not None else "")
    if isinstance(model, FlagRequest):
        return f"{label}: suggest the flags missing from the estimated build"
    if isinstance(model, FuzzRequest):
        return f"{label}: search inputs for {len(model.gaps)} gaps in {', '.join(model.functions[:3]) or '-'}" + \
            (" ..." if len(model.functions) > 3 else "") + f" ({model.seconds} s)"
    if isinstance(model, EvidenceRequest):
        return f"{label}: expected behaviour of {model.function}"
    if isinstance(model, EvidenceResult):
        classes = sorted({e.evidence_class for e in model.evidence})
        return f"{label}: {len(model.evidence)} evidence items for {model.function}" + \
            (f" ({', '.join(classes)})" if classes else "")
    if isinstance(model, HarnessRequest):
        return f"{label}: the test harness for {'C++' if model.language == 'cpp' else 'C'}"
    if isinstance(model, HarnessFiles):
        return f"{label}: {len(model.files)} harness files"
    if isinstance(model, OracleRequest):
        return f"{label}: is the changed test {model.test} as strong as before?"
    if isinstance(model, OracleResult):
        return f"{label}: {model.test}: {model.decision.replace('_', ' ')} ({model.mutants} mutants)"
    if isinstance(model, FaultCheckRequest):
        return f"{label}: does suggestion {model.suggestion_id} weaken what the {_n(len(model.tests), 'test')} detect?"
    if isinstance(model, FaultCheckResult):
        return f"{label}: {model.suggestion_id}: {model.summary()}"
    if isinstance(model, FinalizeRequest):
        return f"{label}: turn {_n(len(model.observed), 'observed value')} into assertions, drop tests that add nothing"
    if isinstance(model, CorrectionRequest):
        return (f"{label}: {model.failure.classification.replace('_', ' ')} in "
                f"{model.failure.affected_function or model.failure.file or '-'}, {len(model.evidence)} evidence items")
    if isinstance(model, SourcePatch):
        return f"{label}: {model.status.replace('_', ' ')} for {model.function} ({model.changed_lines} lines)"
    if isinstance(model, ReviewRequest):
        return f"{label}: second opinion on the fix of {model.patch.function}"
    if isinstance(model, PublishRequest):
        v = model.verification
        return f"{label}: publish fix {model.patch.patch_id} for {model.patch.function} (reverified: {v.passed} of {v.total} tests pass)"
    if isinstance(model, PublishCheck):
        return f"{label}: publication rules {'met' if model.ok else 'not met: ' + model.reason[:70]}"
    if isinstance(model, GuidanceRequest):
        return f"{label}: examples for {', '.join(model.categories[:3]) or model.language} ({model.framework})"
    if isinstance(model, KBGuidance):
        return f"{label}: {len(model.positive)} good and {len(model.negative)} bad examples"
    if isinstance(model, LearningEvent):
        return f"{label}: learn a {model.kind.replace('_', ' ')} example ({model.category})"
    if isinstance(model, LearnAck):
        return f"{label}: " + ("already known" if model.duplicate else "stored" if model.stored else
                               f"not stored ({model.reason[:60]})")
    return None


def plans_of(response: Any) -> list[dict[str, Any]]:
    """The token plans of a contract response (ARCH-15)."""
    meta = getattr(response, "meta", None)
    return list(getattr(meta, "token_plans", []) or [])


def token_summary(plans: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Per brain step: calls, brains, windows, tokens and what was shortened (ARCH-15); and the
    knowledge packs used with name, version and SHA-256 (KP-7). For the final result (ORC-12)."""
    from collections import Counter
    steps: dict[str, list[dict[str, Any]]] = {}
    for plan in plans:
        steps.setdefault(plan.get("step") or f"{plan.get('agent', '')}.{plan.get('tool', '')}", []).append(plan)
    lines = []
    for step, items in sorted(steps.items()):
        answered = [p for p in items if p.get("outcome") == "answered"]
        brains = Counter(p.get("brain", "") for p in answered)
        windows = sorted({p["window"] for p in answered if p.get("window")})
        cut = Counter(name for p in items for name in p.get("shortened", []))
        left = Counter(name for p in items for name in p.get("dropped", []))
        failed = Counter(p.get("outcome", "") for p in items if p.get("outcome") != "answered")
        line = (f"{step}: {len(answered)} of {len(items)} call(s) answered"
                + (f" ({', '.join(f'{b} {n}' for b, n in sorted(brains.items()))})" if brains else "")
                + (f"; windows {'/'.join(str(w) for w in windows)}" if windows else "")
                + f"; tokens estimated {sum(p.get('input_tokens', 0) for p in answered):,} in, "
                  f"limit {sum(p.get('output_tokens', 0) for p in answered):,} out; reported "
                  f"{sum(p.get('used_input_tokens', 0) for p in answered):,} in, "
                  f"{sum(p.get('used_output_tokens', 0) for p in answered):,} out")
        if cut:
            line += "; shortened: " + ", ".join(f"{n} x{k}" for n, k in sorted(cut.items()))
        if left:
            line += "; left out: " + ", ".join(f"{n} x{k}" for n, k in sorted(left.items()))
        if failed:
            line += "; not answered: " + ", ".join(f"{n} x{k}" for n, k in sorted(failed.items()))
        lines.append(line)
    packs: dict[tuple[str, str, str, str], Counter] = {}
    for plan in plans:
        for pack in plan.get("packs", []):
            key = (pack.get("name", ""), pack.get("version", ""), pack.get("sha256", ""), pack.get("source", ""))
            packs.setdefault(key, Counter())[pack.get("use", "whole")] += 1
    pack_lines = [f"{name} {version} ({source}) sha256:{sha[:16]}: " + ", ".join(
                  f"{n} call(s) {use}" for use, n in sorted(uses.items()))
                  for (name, version, sha, source), uses in sorted(packs.items())]
    return lines, pack_lines


class Tools:
    """Tool locations written by ut_setup.py [TP-10]. The pipeline uses only these: it does not
    search the system and has no fallback compiler [A3-33a]."""
    SETUP_HINT = "run 'unittest_generator.bat setup' (Windows) or './unittest_generator.sh setup' (Linux)"

    def __init__(self, data: dict[str, Any], path: Path):
        self.data, self.path = data, path
        self.tc: dict[str, Any] = data.get("toolchain") or {}

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Tools":
        import json as _json
        p = Path(path) if path else manifest_path(None)
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ba.AgentFault("ENVIRONMENT_ERROR", f"No tool manifest at {p}: {cls.SETUP_HINT}.") from exc
        tools = cls(data, p)
        missing = [k for k in ("cc", "cxx", "profdata", "cov") if not tools.tc.get(k) or not Path(tools.tc[k]).is_file()]
        if missing:
            raise ba.AgentFault("ENVIRONMENT_ERROR", f"The tool manifest {p} names missing tools "
                                f"({', '.join(missing)}): {cls.SETUP_HINT}.")
        return tools

    def __getattr__(self, name: str) -> Any:
        if name in ("cc", "cxx", "profdata", "cov", "version", "kind", "target"):
            return self.tc.get(name, "")
        if name in ("mcdc", "fuzzer", "analyzer"):
            return bool(self.tc.get(name))
        if name in ("link_flags", "cxx_flags", "cxx_link_flags"):
            return list(self.tc.get(name) or [])
        raise AttributeError(name)

    def env(self) -> dict[str, str]:
        """Environment for the compiler and the test programs (Visual Studio's INCLUDE/LIB when used)."""
        env = dict(self.tc.get("env") or {})
        base = env.get("PATH", os.environ.get("PATH", ""))
        if self.tc.get("path"):
            env["PATH"] = os.pathsep.join(list(self.tc["path"]) + [base])
        return env

    def command(self, name: str) -> list[str] | None:
        entry = (self.data.get("tools") or {}).get(name)
        if not entry or not entry.get("path"):
            return None
        return [entry["path"], *entry.get("args", [])]

    def tool_env(self, name: str) -> dict[str, str]:
        return dict(((self.data.get("tools") or {}).get(name) or {}).get("env") or {})


# ============================================================================ build flags [A3-BD7]
REPO_TOKEN = "{repo}"
KEEP_WITH_VALUE = ("-isystem", "-iquote", "-idirafter", "-imacros", "-include", "-I", "-D", "-U", "/FI", "/I", "/D", "/U")
PATH_OPTIONS = ("-isystem", "-iquote", "-idirafter", "-imacros", "-include", "-I", "/FI", "/I")
KEEP_FLAGS = re.compile(r"^(-std=.+|-fno-exceptions|-fno-rtti|-fsigned-char|-funsigned-char|-fshort-enums|"
                        r"-fshort-wchar|-fms-extensions|-fwrapv|-fno-common|-fpack-struct(=\d+)?|-fno-signed-char|"
                        r"-fno-unsigned-char)$")
TARGET_FLAGS = re.compile(r"^(-m.+|--target=.+|-target|--specs=.+|-specs=.+|-nostdlib|-nostdinc|-ffreestanding|"
                          r"--sysroot=.+|--sysroot|-isysroot|-B.*|-mcpu=.+|-march=.+|-mtune=.+|-fno-builtin.*|"
                          r"-fstack-usage|-flto.*|--cpu=.+|--arm_.*|/arch:.+)$")
SOURCE_EXT = (".c", ".cc", ".cpp", ".cxx", ".c++", ".cp")


def split_command(command: str) -> list[str]:
    import shlex
    return shlex.split(command.replace("\\", "/"))


def translate_flags(args: list[str], directory: str, map_path) -> tuple[list[str], list[str]]:
    """Host flags from one recorded compiler call: defines, include paths, forced includes, the
    language standard and the few options that change the meaning of code are kept; target
    options are dropped and returned for the build description [A3-BD7]. ``map_path(abs) -> str``
    writes a path inside the working copy as {repo}/..."""
    kept, dropped = [], []
    i, n = 1, len(args)                       # args[0] is the compiler
    driver = Path(str(args[0]).replace("\\", "/")).name.lower() if args else ""
    msvc = driver in ("cl", "cl.exe", "clang-cl", "clang-cl.exe") or "--driver-mode=cl" in args
    prefixes = [p for p in KEEP_WITH_VALUE if msvc or not p.startswith("/")]
    while i < n:
        arg = str(args[i]).replace("\\", "/")
        i += 1
        if arg in ("-target", "--sysroot", "-isysroot", "-arch") and i < n:
            dropped += [arg, str(args[i])]
            i += 1
            continue
        if arg in ("-o", "-MF", "-MT", "-MQ", "-x", "-Xclang", "-Xlinker", "-Xassembler", "-include-pch",
                   "--serialize-diagnostics") and i < n:
            i += 1
            continue
        option, value = None, None
        for prefix in prefixes:
            if arg == prefix:
                if i < n:
                    option, value = prefix, str(args[i]).replace("\\", "/")
                    i += 1
                break
            if arg.startswith(prefix) and len(arg) > len(prefix):
                option, value = prefix, arg[len(prefix):]
                break
        if option is None:
            m = re.match(r"^[-/]std[:=](.+)$", arg)
            if m:
                kept.append("-std=" + m.group(1).replace("c++latest", "c++20").replace("stdcpp", "c++"))
            elif KEEP_FLAGS.match(arg):
                kept.append(arg)
            elif arg in ("/GR-",):
                kept.append("-fno-rtti")
            elif arg == "/J":
                kept.append("-funsigned-char")
            elif arg == "-ansi":
                kept.append("-std=c89")
            elif TARGET_FLAGS.match(arg):
                dropped.append(arg)
            continue
        flag = option.replace("/FI", "-include").replace("/I", "-I").replace("/D", "-D").replace("/U", "-U")
        if option in PATH_OPTIONS:
            path = value if re.match(r"^(/|[A-Za-z]:)", value) else f"{directory.rstrip('/')}/{value}"
            value = map_path(os.path.normpath(path).replace("\\", "/"))
        if flag in ("-include", "-imacros", "-isystem", "-iquote", "-idirafter"):
            kept += [flag, value]
        else:
            kept.append(flag + value)
    return kept, dropped


def path_mapper(repo: Path, original_roots: list[str]):
    """Maps an absolute path inside the working copy or an original checkout to {repo}/..."""
    roots = [str(repo.resolve()).replace("\\", "/").rstrip("/")] + [r.replace("\\", "/").rstrip("/") for r in original_roots if r]

    def map_path(path: str) -> str:
        p = path.replace("\\", "/")
        for root in roots:
            if p.lower() == root.lower():
                return REPO_TOKEN
            if p.lower().startswith(root.lower() + "/"):
                return REPO_TOKEN + p[len(root):]
        return p
    return map_path


def expand_flags(flags: list[str], repo: Path) -> list[str]:
    root = str(repo.resolve())
    return [f.replace(REPO_TOKEN, root) for f in flags]


def repo_relative(path: str) -> str:
    """{repo}/src/a.c -> src/a.c; other paths unchanged."""
    return path[len(REPO_TOKEN) + 1:] if path.startswith(REPO_TOKEN + "/") else path


for _model in (FieldInfo, ParamInfo, FunctionInfo, FuzzRequest, FuzzResult, LayoutProfile, CaptureRequest,
               DependencyRequest, VerifyRequest, PlanRequest, FlagRequest):
    _model.model_rebuild()


def stub_base(spec: "StubSpec") -> str:
    """Name stem of a stub's control variables: ut_stub_<stem>_return / _calls / _<param> [A2-21g]."""
    return (spec.qualified_name or spec.function).replace("::", "_").replace("~", "dtor_")


def gap_item(g: "GapItem") -> str:
    """The covered-item id that closes a gap (the id format of Agent3's coverage report)."""
    if g.metric == "LC":
        return f"LC:{g.line}"
    if g.metric in ("SC", "DC"):
        return f"{g.metric}:{g.line}:{g.column}"
    if g.metric == "BC":
        return f"BC:{g.line}:{g.column}:{g.gap_id.rsplit(':', 1)[-1]}"
    return f"MCDC:{g.line}:{g.column}:{(g.condition_index or 0) + 1}"
