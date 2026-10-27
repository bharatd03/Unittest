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
HARNESS_VERSION = "ut_harness 1.3"
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
    overload: int = 0                    # C++: 0 the first declaration of its name, 1.. the other overloads [A1-DA4]


class StubControl(Contract):
    """A1-DA7a: one control of a project stub - a function a test calls or a global it sets."""
    name: str
    declaration: str                     # "void sim_set_echo_us(uint32_t us)" / "extern int sim_level"
    return_type: str = "void"
    params: list[list[str]] = Field(default_factory=list)       # [[type, name], ...]
    kind: Literal["function", "variable"] = "function"
    type: str = ""                       # a variable's type


class ProjectStub(Contract):
    """A1-DA7a: a stub file of the repository that is reused, with the controls a test can drive it by."""
    file: str
    header: str = ""                     # the header that declares its controls ("" = none)
    controls: list[StubControl] = Field(default_factory=list)
    reset: str = ""                      # a control without parameters that resets it, called before each test
    serves: list[str] = Field(default_factory=list)            # the dependencies it defines for the SUT


class DependencyPlan(Contract):
    entries: list[Dependency] = Field(default_factory=list)
    project_stubs: list[ProjectStub] = Field(default_factory=list)   # reused stubs and their controls [A1-DA7a]
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


_SETTABLE = re.compile(r"(?:(?:unsigned|signed)\s+)?(?:char|short|int|long|long long|long int|short int)|unsigned|signed|"
                       r"bool|_Bool|size_t|ptrdiff_t|u?intptr_t|u?int(?:_least|_fast)?\d+_t")


def state_setter_name(var: StateVar) -> str:
    """A2-21z: the setter of a SUT state variable - one name for Agent1's offer, Agent2's file and its checks."""
    return "ut_state_set_" + safe_name(var.name)


def state_getter_name(var: StateVar) -> str:
    """A2-21z: the getter of a SUT state variable, next to its setter."""
    return "ut_state_get_" + safe_name(var.name)


def settable_state(layout: "LayoutProfile") -> list[StateVar]:
    """A2-21z: the SUT state variables that get a setter - every scalar or enumeration variable, whatever its
    linkage. Agent1 offers exactly these to the brain and Agent2 writes exactly these."""
    enums = {e.name.split("::")[-1] for e in layout.enums}
    out = []
    for v in layout.sut_state:
        t = re.sub(r"\b(volatile|const|static|struct|enum)\b", " ", v.type)
        t = re.sub(r"\bstd::", "", re.sub(r"\s+", " ", t)).strip()
        if not t or any(c in t for c in "[*(&<"):
            continue
        base = t.split("::")[-1]
        resolved = layout.typedefs.get(base, "") or layout.typedefs.get(t, "")
        if resolved and (re.search(r"\b(struct|union|class)\b", resolved) or any(c in resolved for c in "[*(")):
            continue
        if _SETTABLE.fullmatch(t) or base in enums or (re.fullmatch(r"[A-Za-z_]\w*_t", base) and not
                                                        re.search(r"\b(struct|union)\b", v.type)):
            out.append(v)
    return out


HIDDEN_MARK = "\0hidden:"          # an entry of a set of allowed names: a SUT variable only the SUT file sees
PIPELINE_HELPER = re.compile(r"^ut_(?:state_set|state_get|stub|fake)_\w+$")
_STRING_LITERAL = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')


def hidden_state(layout: "LayoutProfile", headers: str) -> set[str]:
    """A2-21ai: SUT state variables a test cannot name - static, in an unnamed namespace, or declared in no header.
    Only a declaration as a variable (extern, or an inline variable in C++) makes one visible: the same name as a
    field, a parameter or a variable of another namespace does not."""
    out = set()
    for v in layout.sut_state:
        short = v.name.split("::")[-1]
        declared = re.search(r"\b(?:extern|inline)\b[^;(){}]*?(?<![\w.>])" + re.escape(short) +
                             r"\s*(?:\[[^\]]*\]\s*)*(?:=[^;]*)?;", headers)
        if not declared:
            out.add(v.name)
    return out


def _namespace_of(name: str) -> str:
    return name.replace("(anonymous namespace)::", "").rpartition("::")[0]


def _closest_first(names: list[str], function: str) -> list[str]:
    """Variables of one name in several namespaces: those of the function's own namespace (or one enclosing it)
    first, the deepest first [A2-21ai]."""
    own = _namespace_of(function)

    def rank(n: str) -> tuple[int, int]:
        ns = _namespace_of(n)
        inside = own == ns or own.startswith(ns + "::") or not ns
        return (0 if inside else 1, -len(ns))
    return sorted(names, key=rank)


def name_problem(code: str, calls: set[str], allowed: set[str], function: str = "") -> str:
    """A2-21ai: a guard - helpers of the pipeline that are not written for this SUT, or a SUT variable that only the
    SUT file sees, used by its name. ``code`` without comments and strings; ``allowed`` may carry HIDDEN_MARK
    entries; ``function`` (the function under test) puts the variables of its namespace first."""
    setters = sorted(a for a in allowed if a.startswith("ut_state_set_"))
    missing = sorted(c for c in calls if c not in allowed and PIPELINE_HELPER.match(c))
    if missing:
        return (f"uses helpers the pipeline does not write for this SUT: {', '.join(missing[:4])}; the state setters "
                f"that exist: {', '.join(setters[:8]) or 'none'} [A2-21ai]")
    hidden: dict[str, list[str]] = {}
    for entry in sorted(a for a in allowed if a.startswith(HIDDEN_MARK)):
        name = entry[len(HIDDEN_MARK):]
        hidden.setdefault(name.split("::")[-1], []).append(name)
    for short, names in sorted(hidden.items()):
        use = re.search(r"(?<![\w.>])((?:\w+::)*)" + re.escape(short) + r"\b(?!\s*::)", code)
        declared = re.search(r"\b[A-Za-z_][\w:<>]*[\s*&]+" + re.escape(short) + r"\s*(?:[=;\[(,{)]|$)", code)
        if use and not declared:
            written = use.group(1).rstrip(":")
            named = [n for n in names if written and (n.endswith("::" + written + "::" + short) or
                                                     n.replace("(anonymous namespace)::", "") == written + "::" + short)]
            ways = []
            for n in _closest_first(named or names, function):
                setter, getter = state_setter_name(StateVar(name=n)), state_getter_name(StateVar(name=n))
                if setter in allowed:
                    ways.append(f"{setter}(value) sets and {getter}() reads {n.replace('(anonymous namespace)::', '')}")
            how = "; ".join(ways[:4]) if ways else "observe it through the SUT's functions"
            return f"uses {short}, which only the SUT file sees: {how} [A2-21ai]"
    return ""


def _uses(text: str, short: str) -> bool:
    return re.search(r"(?<![\w.>])(?:\w+::)*" + re.escape(short) + r"\b(?!\s*\()", text) is not None


_DECISION = re.compile(r"\b(?:if|while|switch|case)\b|\?")


def function_state(fn: "FunctionInfo", layout: "LayoutProfile", lines: list[str], limit: int = 40
                   ) -> tuple[list[StateVar], list[StateVar]]:
    """A2-21aj: the settable SUT state (A2-21z) the function and the SUT functions it calls (two levels) use - those
    its own decisions read first - as (listed, the rest beyond ``limit``). A name that several namespaces use is
    resolved by the namespace written in the code, else by the function's own namespace."""
    by_short: dict[str, list[StateVar]] = {}
    for v in settable_state(layout):
        by_short.setdefault(v.name.split("::")[-1], []).append(v)
    if not by_short:
        return [], []

    def body(f: "FunctionInfo") -> str:
        return "\n".join(lines[f.line_start - 1: f.line_end]) if 0 < f.line_start <= len(lines) else ""
    own_file = [g for g in layout.functions if g.file == fn.file]

    def callees(f: "FunctionInfo") -> list["FunctionInfo"]:
        """The SUT functions f calls, read from its text with the namespace written there; an unqualified name
        several namespaces define is the one closest to f's own namespace."""
        text = body(f).partition("{")[2]                                 # the body, not the function's own name
        out = []
        for short in sorted({c.split("::")[-1] for c in f.calls} | {g.name.split("::")[-1] for g in own_file}):
            same = [g for g in own_file if g is not f and g.name.split("::")[-1] == short]
            if not same:
                continue
            for m in re.finditer(r"(?<![\w:.>])((?:\w+::)*)" + re.escape(short) + r"\s*\(", text):
                written = m.group(1).rstrip(":")
                pick = [g for g in same if written and (g.qualified_name.replace("(anonymous namespace)::", "")
                                                        .endswith(written + "::" + short)
                                                        or g.qualified_name == written + "::" + short)]
                if not pick and not written:
                    first = _closest_first([g.qualified_name for g in same], f.qualified_name)[0]
                    pick = [g for g in same if g.qualified_name == first]
                out += [g for g in pick if g not in out]
        return out
    reach, frontier = [fn], [fn]
    for _ in range(2):
        nxt = []
        for f in frontier:
            for g in callees(f):
                if g not in reach:
                    reach.append(g)
                    nxt.append(g)
        frontier = nxt
    own = body(fn)
    decisions = "\n".join(line for line in own.splitlines() if _DECISION.search(line))
    found: list[tuple[int, int, StateVar]] = []
    for k, f in enumerate(reach):
        text = own if f is fn else body(f)
        for short, candidates in by_short.items():
            if not _uses(text, short):
                continue
            written = {m.group(1).rstrip(":") for m in re.finditer(
                r"(?<![\w.>])((?:\w+::)+)" + re.escape(short) + r"\b", text)}
            chosen = [v for v in candidates if any(v.name.replace("(anonymous namespace)::", "").endswith(
                w + "::" + short) for w in written)]
            if not chosen:
                ranked = _closest_first([v.name for v in candidates], f.qualified_name)
                chosen = [v for v in candidates if v.name == ranked[0]] if len(candidates) > 1 and _namespace_of(
                    f.qualified_name) else candidates
            for v in chosen:
                rank = 0 if f is fn and _uses(decisions, short) else 1 if f is fn else 2
                found.append((rank, k, v))
    seen: set[str] = set()
    ordered = []
    for _, _, v in sorted(found, key=lambda x: (x[0], x[1])):
        if v.name not in seen:
            seen.add(v.name)
            ordered.append(v)
    return ordered[:limit], ordered[limit:]


def namespace_names(layout: "LayoutProfile", headers: str) -> dict[str, str]:
    """A2-21ai: C++ functions and types of the SUT declared in a namespace, by their name without it - only names
    with exactly one such candidate and no global one of the same name."""
    if layout.language != "cpp":
        return {}
    found: dict[str, set[str]] = {}
    plain: set[str] = set()

    def add(qualified: str) -> None:
        qualified = qualified.replace("(anonymous namespace)::", "").strip(":")
        if "::" in qualified:
            found.setdefault(qualified.rsplit("::", 1)[1], set()).add(qualified)
        elif qualified:
            plain.add(qualified)
    for f in layout.functions:
        if f.member:
            add(f.qualified_name.rsplit("::", 1)[0])           # the class
        elif not f.qualified_name.startswith("operator"):
            add(f.qualified_name)
    for d in (layout.dependencies.entries if layout.dependencies else []):
        if d.kind == "function" and d.qualified_name:
            add(d.qualified_name)
    for e in layout.enums:
        add(e.name)
    for i in layout.interfaces:
        add(i.name)
    stack: list[str | None] = []
    for m in re.finditer(r"\bnamespace\s+([\w:]+)?\s*\{|\b(?:struct|class|union|enum(?:\s+class|\s+struct)?)\s+"
                         r"([A-Za-z_]\w*)\s*(?=[{:;])|\{|\}", headers):
        if m.group(0).startswith("namespace"):
            stack.append(m.group(1) or "")
        elif m.group(2):
            spaces = [s for s in stack if s]
            if len(spaces) == len([s for s in stack if s is not None]):            # not inside a class body
                add("::".join(spaces + [m.group(2)]))
        elif m.group(0) == "{":
            stack.append(None)
        elif stack:
            stack.pop()
    return {short: next(iter(q)) for short, q in found.items() if len(q) == 1 and short not in plain}


def qualify_names(body: str, names: dict[str, str]) -> tuple[str, list[str]]:
    """A2-21ai: writes C++ SUT names that are used without their namespace with it (a form fix, ARCH-20). Never
    inside strings, after ::, . or ->, or when the body opens the namespace with a using-directive or declares
    the name itself. Returns the body and the names fixed."""
    if not names or re.search(r"\busing\s+namespace\b", body):
        return body, []
    fixed: list[str] = []
    parts, last = [], 0
    for m in _STRING_LITERAL.finditer(body):
        parts.append((body[last:m.start()], True))
        parts.append((m.group(0), False))
        last = m.end()
    parts.append((body[last:], True))
    code = "".join(p for p, is_code in parts if is_code)
    wanted = {s: q for s, q in names.items() if re.search(r"(?<![\w:.>])" + re.escape(s) + r"\b(?!\s*::)", code)
              and not re.search(r"\b[A-Za-z_][\w:<>]*[\s*&]+" + re.escape(s) + r"\s*[=;\[,)]", code)}
    if not wanted:
        return body, []
    pattern = re.compile(r"(?<![\w:.>])(" + "|".join(map(re.escape, sorted(wanted, key=len, reverse=True))) +
                         r")\b(?!\s*::)")
    out = []
    for text, is_code in parts:
        if is_code:
            text = pattern.sub(lambda m: wanted[m.group(1)], text)
        out.append(text)
    fixed = sorted(wanted)
    return "".join(out), fixed


def state_resettable(var: StateVar, cpp: bool) -> bool:
    """A2-21s: C state is restored byte for byte; in C++ only scalars, enumerations, pointers and arrays of them."""
    if not cpp:
        return True
    t = re.sub(r"\b(volatile|const|unsigned|signed|struct|enum)\b", " ", var.type)
    t = re.sub(r"\bstd::", "", re.sub(r"\[[^\]]*\]", "", t)).strip()      # std::uint32_t is a scalar too
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
DEFAULT_ITERATIONS = 10                 # the safety ceiling of LA-7 (ORC-7)


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


class DefectCandidate(Contract):
    """A1-37f: a decision whose proven infeasibility the operator explains - a finding for the developer."""
    candidate_id: str = ""               # D1, D2 ... per file
    line: int
    function: str = ""
    decision: str                        # the decision as written
    proof: str                           # the proof of A1-37
    change: str                          # the operator change that removes the proof
    evidence: str = ""                   # the comment or requirement that names the bounds
    items: list[str] = Field(default_factory=list)              # the items it blocks


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
    defect: DefectCandidate | None = None      # the proof is explained by a wrong operator [A1-37f]


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
    unknown_outcomes: list[str] = Field(default_factory=list)      # ends the platform facts do not describe [NC-12]
    measurement_notes: list[str] = Field(default_factory=list)     # profiles left out of the merge [A3-60a]


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
    overload: int = 0                # C++: the other overloads of a name get their own controls [A1-DA4]


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
    calls_before: list[str] = Field(default_factory=list)   # earlier calls that build up the state [BP-2 call_sequence]
    kind: str = "scenario"             # the answer kind it was asked for [BP-2, BP-3]
    trial: list[str] = Field(default_factory=list)          # refused only by predictions: the tool decides [NC-7]
    function_line: int = 0             # first line of the function: tells overloads apart (0 = the first) [NC-4]


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
    kinds_available: list[str] = Field(default_factory=list)   # answer kinds an available brain may give [BP-3, BP-9]
    rules_only: bool = False          # the rule round: no brain writes a test; the scenarios are facts [A1-PL1]
    deferred: list[str] = Field(default_factory=list)          # functions left for a later brain round [A1-PL16]


class FuzzResult(Contract):
    inputs: list[FuzzInput] = Field(default_factory=list)
    executions: int = 0
    functions: list[str] = Field(default_factory=list)      # fuzzed
    unsupported: list[str] = Field(default_factory=list)    # "fn: reason"
    crashes: list[str] = Field(default_factory=list)        # inputs that crashed the SUT (reported, never asserted)
    reason: str = ""
    engine: str = ""                                        # "libFuzzer" or "ut_search" [A3-FZ11]


# ============================================================================ learning between attempts [LA-1 .. LA-10]
RootCause = Literal["test_did_not_compile", "test_setup", "wrong_inputs", "needs_call_sequence",
                    "needs_dependency_control", "wrong_expectation", "unreachable", "needs_requirement",
                    "needs_source_change", "pipeline_limitation", "stub_not_driven", "undetermined"]
FinalStatus = Literal["covered", "proven_unreachable", "needs_requirement", "needs_source_change",
                      "pipeline_limitation", "undetermined"]
CAUSE_TEXT = {
    "test_did_not_compile": "the test did not compile",
    "test_setup": "the test could not build what the call needs (an object, an argument, a stub or variable that "
                  "does not exist)",
    "wrong_inputs": "the test ran, but its values do not meet the path conditions of the item",
    "needs_call_sequence": "the item needs state that only several calls in a row build up",
    "needs_dependency_control": "the item needs control of a real dependency's result",
    "wrong_expectation": "the test contradicts its own scenario or evidence",
    "unreachable": "a deterministic proof shows that no test can reach it",
    "needs_requirement": "the expected behaviour is not stated",
    "needs_source_change": "the source prevents the item",
    "pipeline_limitation": "the pipeline cannot yet build the test the item needs",
    "stub_not_driven": "no test set the stub the item depends on, so it always returned its default [LA-3a]",
    "undetermined": "no cause could be decided from the facts"}
FIX_FORM = {                                                       # LA-4: the form a fix takes
    "test_did_not_compile": "write a test that compiles: use only the names in HEADERS, STUB CONTROL and SETTERS",
    "test_setup": "build the object or argument as shown under OBJECT, and use only controls that exist",
    "wrong_inputs": "choose other values that meet the PATH CONDITIONS of the item",
    "needs_call_sequence": "call the function several times in a row (the earlier calls in setup) to build up its state",
    "needs_dependency_control": "set what the real dependency reads (a global or register), or ask for a stub",
    "wrong_expectation": "assert only what the evidence states for exactly these inputs, or observe the result",
    "stub_not_driven": "set the stub's return value or call its control (STUB CONTROL, PROJECT STUB CONTROLS) before "
                       "the call"}
OWNER = {"covered": "", "proven_unreachable": "developer: the source", "needs_requirement": "developer: requirement "
         "documents", "needs_source_change": "developer: a source change through review", "pipeline_limitation":
         "the pipeline's developers", "undetermined": "developer: with the evidence"}


class JournalEntry(Contract):
    """LA-2: one deviation of one attempt - what was tried, what the tools said and why it missed."""
    entry_id: str
    function: str
    items: list[str] = Field(default_factory=list)
    iteration: int = 0
    attempt: int = 0
    strategy: str = ""                 # brain scenario, brain test, rule test, rewrite
    brain: str = ""                    # name/model
    tried: str = ""                    # the test (name and body) or scenario, in short
    result: str = ""                   # the tool's message: compiler, assertion, crash, items reached
    cause: RootCause = "undetermined"
    decided_by: Literal["rule", "brain"] = "rule"
    next_action: str = ""
    status: Literal["open", "closed"] = "open"
    final_status: FinalStatus | Literal[""] = ""


class ItemStatus(Contract):
    """LA-6: the final status of an item not covered, with its reason and owner."""
    item_id: str
    function: str = ""
    status: FinalStatus
    reason: str = ""
    owner: str = ""


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
    refused: dict[str, list[str]] = Field(default_factory=dict)  # function -> scenarios refused earlier [A1-PL8]
    crashed_calls: list[str] = Field(default_factory=list)       # rule-based test inputs that crashed [A2-21f]
    inputs_only: bool = False          # only the search's inputs as scenarios, no brain [ORC-5a, A1-18c]
    journal: dict[str, str] = Field(default_factory=dict)        # function -> journal excerpt [LA-4]
    tried_scenarios: list[str] = Field(default_factory=list)     # signatures of earlier scenarios [A1-PL4 g, LA-4]
    other_model_items: list[str] = Field(default_factory=list)   # items whose third attempt needs another model [LA-5]
    final_items: list[str] = Field(default_factory=list)         # items with a final status: not planned [LA-6]
    lessons: list[str] = Field(default_factory=list)             # verified lessons of the knowledge base [LA-8]
    item_kinds: dict[str, str] = Field(default_factory=dict)     # item -> answer kind of its next attempt [BP-3]
    iterations_left: int = 0                                      # after this one: the brain's rounds [A1-PL16]
    no_brain_declarations: list[str] = Field(default_factory=list)   # the brain's declaration did not build [A1-PL14]
    examples: dict[str, str] = Field(default_factory=dict)       # function -> planning examples of the KB [KB-25]


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
    crashed_calls: list[str] = Field(default_factory=list)   # rule-based inputs that crashed or hung [A2-21f]
    # tests refused only by predictions: name -> the refusal; the tool decides in a trial [NC-6, NC-7]
    trial: dict[str, str] = Field(default_factory=dict)
    trial_targets: dict[str, list[str]] = Field(default_factory=dict)   # name -> the items it targets
    made: dict[str, str] = Field(default_factory=dict)        # test -> "brain/model kind" that wrote it [BP-7]
    recipes: dict[str, str] = Field(default_factory=dict)     # test -> building block (Recipe JSON) to store [NC-2]
    needs_kind: dict[str, str] = Field(default_factory=dict)  # item -> answer kind no available brain may give [BP-9]


EXAMPLE_KINDS = ("test_example", "plan_example")                  # KB-23
LEARN_KINDS = Literal["positive", "gap_closure", "negative", "lesson", "test_example", "plan_example"]


class KBExample(Contract):
    example_id: str
    kind: LEARN_KINDS
    language: str
    framework: str
    category: str
    snippet: str
    reason: str = ""
    uses: int = 1
    pattern: str = ""                  # LA-8: the construct a lesson applies to
    step: str = ""                     # KB-23: the brain step an example is for (A1.plan, A2.tests)
    function: str = ""
    signature: str = ""
    context: str = ""                  # what it reached, with the code of the items


class KBGuidance(Contract):
    positive: list[KBExample] = Field(default_factory=list)
    negative: list[KBExample] = Field(default_factory=list)
    lessons: list[KBExample] = Field(default_factory=list)      # verified lessons [LA-8, KB-22]
    examples: dict[str, list[KBExample]] = Field(default_factory=dict)   # "<step>|<function>" -> examples [KB-24]


def example_key(step: str, function: str) -> str:
    """KB-24: the key of a function's examples in KBGuidance.examples."""
    return f"{step}|{function}"


class GenerationRequest(Contract):
    plan: UTPlan
    guidance: KBGuidance = Field(default_factory=KBGuidance)
    existing_suite: str = ""
    journal: dict[str, str] = Field(default_factory=dict)        # function -> journal excerpt [LA-4]
    item_kinds: dict[str, str] = Field(default_factory=dict)     # item -> answer kind of its next attempt [BP-3]


class FinalizeRequest(Contract):
    bundle: FileActionBundle
    observed: dict[str, str]
    test_items: dict[str, list[str]] = Field(default_factory=dict)
    crashed: dict[str, str] = Field(default_factory=dict)   # tests that crashed or hung in the observe run [A2-21f]


# ============================================================================ Agent3 requests
class VerifyRequest(Contract):
    layout: LayoutProfile
    stage: Literal["probe", "build_check", "verify", "observe"]
    measure_tests: list[str] | None = None    # observe: the tests to measure alone; None = all [A3-OBS5, LA-10]
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
class ExampleQuery(Contract):
    """KB-24: one function the brain plans or writes tests for - what its examples are found by."""
    step: Literal["A1.plan", "A2.tests"]
    function: str
    text: str                          # its signature, the code and path conditions of its open items, causes
    patterns: list[str] = Field(default_factory=list)            # its constructs [LA-8]


class GuidanceRequest(Contract):
    language: str
    framework: str
    categories: list[str] = Field(default_factory=list)
    k: int = Field(default=4, ge=1, le=12)
    patterns: list[str] = Field(default_factory=list)           # constructs of the SUT, for lessons [LA-8]
    functions: list[ExampleQuery] = Field(default_factory=list)  # examples per function [KB-24]
    examples_per_function: int = Field(default=2, ge=0, le=6)


class LearningEvent(Contract):
    kind: Literal["positive", "gap_closure", "negative", "lesson", "test_example", "plan_example"]
    language: str
    framework: str
    category: str
    snippet: str
    reason: str = ""
    pattern: str = ""                                           # LA-8
    verification: dict[str, Any] = Field(default_factory=dict)
    step: str = ""                                              # KB-23: A1.plan or A2.tests
    function: str = ""
    signature: str = ""
    context: str = ""                                           # the items it reached, with their code
    project: str = ""


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


class PipelineFinding(Contract):
    """NC-5: what the pipeline itself could not do - a limitation, a false refusal of a check or an unknown outcome -
    with what is needed to fix it once for every project."""
    kind: Literal["pipeline_limitation", "false_refusal", "unknown_outcome"]
    signature: str                      # the normalized construct, check or message (counted across runs)
    subject: str = ""                   # the construct signature, the check or the outcome
    item: str = ""                      # the item or test concerned
    kinds_tried: list[str] = Field(default_factory=list)   # answer kinds tried [BP-2]
    brains: list[str] = Field(default_factory=list)
    message: str = ""                   # the tool's last message
    platform: str = ""
    sut_file: str = ""
    count: int = 1                      # occurrences across runs (filled from the findings file)


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
    cache_use: list[str] = Field(default_factory=list)         # what each cache saved in this run [CA-1]
    brain_answers: list[str] = Field(default_factory=list)     # per brain step: accepted, fixed, refused [KB-25]
    defect_candidates: list[DefectCandidate] = Field(default_factory=list)   # [A1-37f]
    open_summary: str = ""                     # open items per final status and root cause [LA-3a]
    knowledge_packs: list[str] = Field(default_factory=list)   # name, version, SHA-256 [KP-7]
    escalations: list[str] = Field(default_factory=list)       # rules that needed the brain, with outcome [ARCH-22]
    item_status: list[ItemStatus] = Field(default_factory=list)    # final status per item not covered [LA-6]
    journal: str = ""                                              # the failure journal of the file [LA-2]
    lessons: list[str] = Field(default_factory=list)               # lessons applied or learned [LA-8, KB-22]
    decisions: list[str] = Field(default_factory=list)             # questions of the decision point [LA-9]
    profiles: list[str] = Field(default_factory=list)              # "brain: profile sha256 kinds" [BP-7]
    kinds_used: dict[str, int] = Field(default_factory=dict)       # answer kind -> accepted pieces [BP-7]
    recipes: list[str] = Field(default_factory=list)               # recipes used, stored and proposed [NC-2, NC-3]
    findings: list["PipelineFinding"] = Field(default_factory=list)   # this run's pipeline findings [NC-5]
    findings_overall: list[str] = Field(default_factory=list)      # "count x signature" across runs [NC-5]
    trials: list[str] = Field(default_factory=list)                # what the trials of NC-7 decided


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
                   "A1.header", "A2.value", "A3.repair", "A3.host",     # rule failures [ARCH-21]
                   "A2.setup", "A2.stub", "A2.sequence", "A3.outcome"]  # building blocks, unknown outcomes [NC-1, NC-12]
# Priorities of the context parts of a brain call (ARCH-17): 0 is never shortened; the highest
# number is shortened or left out first when a call does not fit the brain's window.
P_FACTS, P_EVIDENCE, P_PACKS, P_TRIED, P_EXAMPLES = 0, 1, 2, 3, 4
APP_DIR = Path(__file__).resolve().parent


def part(name: str, text: str, priority: int = P_FACTS, min_chars: int = 0) -> ba.ContextPart:
    return ba.ContextPart(name=name, text=text, priority=priority, min_chars=min_chars)


# ============================================================================ caches of a run [CA-1 .. CA-7]
CACHE_DEFAULTS = {"enabled": True, "dir": "ut_cache/run_cache", "max_mb": 4096, "brain_answers": True,
                  "test_coverage": True, "verified_tests": True, "objects": True, "build_capture": True,
                  "failure_journal": True, "failure_analysis": True, "stage_a": True, "code_analysis": True,
                  "mutations": True, "search_corpus": True}           # CA-8 .. CA-13
CACHE_VERSION = "1"            # the form of what the pipeline stores; a change makes every earlier entry a miss


def cache_settings(config_path: str | Path | None) -> dict[str, Any]:
    """CA-1: the "cache" section of ut_config.json over the defaults; the folder absolute (relative to the
    configuration's folder)."""
    section: dict[str, Any] = {}
    if config_path and Path(config_path).is_file():
        import json as _json
        with __import__("contextlib").suppress(ValueError, OSError):
            section = _json.loads(Path(config_path).read_text(encoding="utf-8")).get("cache", {}) or {}
    out = {**CACHE_DEFAULTS, **{k: v for k, v in section.items() if not k.startswith("_")}}
    base = Path(config_path).resolve().parent if config_path and Path(config_path).is_file() else APP_DIR
    folder = Path(str(out["dir"]))
    out["dir"] = str(folder if folder.is_absolute() else base / folder)
    return out


def cache_on(config_path: str | Path | None, name: str) -> bool:
    """CA-1: a cache is used when the caches are on, this one is on, and UT_CACHE=off is not set."""
    settings = cache_settings(config_path)
    return bool(settings.get("enabled")) and bool(settings.get(name, True)) and not ba.cache_switched_off()


def answer_cache(config_path: str | Path | None) -> "ba.AnswerCache | None":
    """CA-2: the brain-answer cache of this PC, or None when it is off."""
    if not cache_on(config_path, "brain_answers"):
        return None
    return ba.AnswerCache(Path(cache_settings(config_path)["dir"]) / "brain_answers.sqlite3")


def cache_log(kind: str, hit: bool) -> None:
    """CA-1: one use of a cache, for the report's "What the caches saved" (UT_CACHE_LOG of the run)."""
    log = os.environ.get("UT_CACHE_LOG", "")
    if log:
        with __import__("contextlib").suppress(OSError):
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(f'{{"cache": "{kind}", "hit": {str(hit).lower()}}}\n')


class RunCache:
    """CA-1: a content-addressed store of one kind (JSON values or files) in the cache folder. A missing or damaged
    entry is a miss; a write goes through a temporary file and a rename, so a reader in another process never sees
    half of it. Hits and misses are counted, and appended to UT_CACHE_LOG when a run names one."""
    def __init__(self, root: str | Path, kind: str):
        self.root = Path(root) / kind
        self.kind = kind
        self.hits = self.misses = 0

    @staticmethod
    def key(*parts: Any) -> str:
        text = "\x1f".join(str(p) for p in (CACHE_VERSION, *parts))
        return hashlib.sha256(re.sub(r"\b\d{8}-\d{6}-[0-9a-f]{6,8}\b", "<run>", text).encode("utf-8",
                                                                                         "replace")).hexdigest()

    def _path(self, key: str, suffix: str) -> Path:
        return self.root / key[:2] / f"{key}{suffix}"

    def _count(self, hit: bool) -> None:
        if hit:
            self.hits += 1
        else:
            self.misses += 1
        cache_log(self.kind, hit)

    def get_json(self, key: str, count: bool = True) -> Any | None:
        if ba.cache_switched_off():
            return None
        import json as _json
        try:
            value = _json.loads(self._path(key, ".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if count:
                self._count(False)
            return None
        if count:
            self._count(True)
        return value

    def put_json(self, key: str, value: Any) -> None:
        if ba.cache_switched_off():
            return
        import json as _json
        self._write(self._path(key, ".json"), _json.dumps(value).encode("utf-8"))

    def get_file(self, key: str, target: Path, suffix: str = ".bin", count: bool = True) -> bool:
        if ba.cache_switched_off():
            return False
        try:
            data = self._path(key, suffix).read_bytes()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError:
            if count:
                self._count(False)
            return False
        if count:
            self._count(True)
        return True

    def get_bytes(self, key: str, suffix: str = ".bin", count: bool = True) -> bytes | None:
        if ba.cache_switched_off():
            return None
        try:
            data = self._path(key, suffix).read_bytes()
        except OSError:
            data = None
        if count:
            self._count(data is not None)
        return data

    def put_bytes(self, key: str, data: bytes, suffix: str = ".bin") -> None:
        if not ba.cache_switched_off():
            self._write(self._path(key, suffix), data)

    def put_file(self, key: str, source: Path, suffix: str = ".bin") -> None:
        if ba.cache_switched_off():
            return
        with __import__("contextlib").suppress(OSError):
            self._write(self._path(key, suffix), source.read_bytes())

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        with __import__("contextlib").suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(tmp, path)
            except OSError:
                with __import__("contextlib").suppress(OSError):
                    os.unlink(tmp)


ROOT_FORMS = ("{ut-root-json}", "{ut-root}", "{ut-root/}")


def _root_forms(root: str) -> list[str]:
    import json as _json
    return [_json.dumps(root)[1:-1], root, root.replace("\\", "/")]


def unrooted(text: str, root: str | Path) -> str:
    """CA-10, CA-11: a text to store, with a folder of the run (as JSON text, as written, with forward slashes - the
    same on Linux) replaced by marks that from_root() puts back for another run's folder."""
    root = str(root)
    if len(root) < 2:
        return text
    for form, mark in zip(_root_forms(root), ROOT_FORMS):
        text = text.replace(form, mark)
    return text


def rooted(text: str, root: str | Path) -> str:
    """CA-10, CA-11: a stored text with the marks of unrooted() replaced by this run's folder."""
    for form, mark in zip(_root_forms(str(root)), ROOT_FORMS):
        text = text.replace(mark, form)
    return text


def names_a_run(text: str, *roots: str | Path) -> bool:
    """CA-10, CA-11: a text that still names a run's folder (a run id, or one of the given folders) is not stored -
    in another run that path would point at the earlier run's files."""
    if re.search(r"\b\d{8}-\d{6}-[0-9a-f]{6,8}\b", text):
        return True
    return any(form in text for root in roots if len(str(root)) > 3
               for form in _root_forms(str(Path(root).resolve())) + _root_forms(str(root)))


_CODE_STOP = {"if", "else", "for", "while", "do", "return", "void", "const", "static", "inline", "volatile",
              "struct", "enum", "union", "class", "the", "a", "an", "of", "and", "or", "to", "in", "is", "ut", "test",
              "expect", "eq", "int", "line", "true", "false", "null", "nullptr", "observe", "e8", "evidence", "x", "n"}


def project_controls(fn: "FunctionInfo", layout: "LayoutProfile") -> list[tuple["ProjectStub", "StubControl"]]:
    """A2-21ak: the controls of the project stubs that the function and the SUT functions it calls (two levels)
    depend on."""
    plan = layout.dependencies
    if plan is None or not plan.project_stubs:
        return []
    names = {f.name: f for f in layout.functions} | {f.qualified_name: f for f in layout.functions}
    reach, todo = set(fn.calls), [(c, 1) for c in fn.calls]
    while todo:
        name, depth = todo.pop()
        callee = names.get(name)
        if callee is not None and depth < 3:
            for c in callee.calls:
                if c not in reach:
                    reach.add(c)
                    todo.append((c, depth + 1))
    return [(p, c) for p in plan.project_stubs if reach & set(p.serves) for c in p.controls]


def item_parts(item: str) -> tuple[str, str, int, str]:
    """(metric, function, line, rest) of a coverage item - a gap as BC:ns::f:28:13:F or LC:f:12, an item a test
    reached as BC:28:13:F or LC:12 (no function); line 0 when it has none (EVIDENCE:f)."""
    m = re.match(r"^([A-Z]+):(?:(.*?[A-Za-z_~].*?):)?(\d+)((?::[^:]*)*)$", item)
    if m:
        return m.group(1), m.group(2) or "", int(m.group(3)), m.group(4)
    head, _, rest = item.partition(":")
    return head, rest, 0, ""


def item_key(item: str) -> str:
    """A gap and the item a test reached, in one form: metric:line:rest."""
    metric, _, line, rest = item_parts(item)
    return f"{metric}:{line}{rest}" if line else item


def code_words(text: str) -> list[str]:
    """KB-24: the words of a text for the example search - code names also by their parts (bcm_set_bits: bcm,
    set, bits; setBits: set, bits), lower case, without the words every text has."""
    out: list[str] = []
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+", text):
        parts = [p for chunk in word.split("_") for p in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", chunk) if p]
        for w in [word.lower(), *(p.lower() for p in parts)]:
            if len(w) > 1 and w not in _CODE_STOP and not w.isdigit():
                out.append(w)
    return list(dict.fromkeys(out))


def neutral_failure(text: str, *folders: str | Path) -> str:
    """CA-9: a failure as the tools reported it, without what differs between two runs of the same failure - the
    run's folders (also as JSON text), addresses and measured durations."""
    import json as _json
    for folder in folders:
        name = str(folder or "")
        if len(name) < 4:
            continue
        for form in sorted({name, name.replace("\\", "/"), _json.dumps(name)[1:-1]}, key=len, reverse=True):
            text = text.replace(form, "<run>")
    text = re.sub(r"\b0x[0-9a-fA-F]{6,16}\b", "0x<addr>", text)
    return re.sub(r"\b\d+(?:\.\d+)?\s?ms\b|\b\d+\.\d+\s?s\b", "<time>", text)


async def brains_of(agent: Any) -> str:
    """CA-9: the brains an agent may ask (name/model): part of the key of a stored brain decision."""
    try:
        router = await agent._brains()
        return ",".join(sorted(f"{router.name_of(b)}/{getattr(b, 'model_id', '')}" for b in router.brains))
    except Exception:                                   # no brain: nothing stored or found under it
        return ""


def run_cache(config_path: str | Path | None, name: str) -> RunCache | None:
    """CA-1: the store of one cache, or None when it is off."""
    if not cache_on(config_path, name):
        return None
    return RunCache(cache_settings(config_path)["dir"], name)


def prune_cache(config_path: str | Path | None) -> int:
    """CA-1: the oldest entries removed until the cache folder is under its size limit; returns the bytes removed.
    Never raises: a file another process holds is left."""
    settings = cache_settings(config_path)
    root = Path(settings["dir"])
    limit = int(float(settings.get("max_mb", 4096)) * 1024 * 1024)
    try:
        files = [(p.stat().st_mtime, p.stat().st_size, p) for p in root.rglob("*") if p.is_file()
                 and p.suffix != ".sqlite3"]           # the answer store stays; its rows are small
    except OSError:
        return 0
    total, removed = sum(s for _, s, _ in files), 0
    for _, size, path in sorted(files):
        if total - removed <= limit:
            break
        with __import__("contextlib").suppress(OSError):
            path.unlink()
            removed += size
    return removed


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
    base = (spec.qualified_name or spec.function).replace("::", "_").replace("~", "dtor_")
    return f"{base}_{spec.overload + 1}" if getattr(spec, "overload", 0) else base          # A1-DA4: overloads apart


def gap_item(g: "GapItem") -> str:
    """The covered-item id that closes a gap (the id format of Agent3's coverage report)."""
    if g.metric == "LC":
        return f"LC:{g.line}"
    if g.metric in ("SC", "DC"):
        return f"{g.metric}:{g.line}:{g.column}"
    if g.metric == "BC":
        return f"BC:{g.line}:{g.column}:{g.gap_id.rsplit(':', 1)[-1]}"
    return f"MCDC:{g.line}:{g.column}:{(g.condition_index or 0) + 1}"


# ============================================================================ learning between attempts [LA-1 .. LA-10]
_QUOTE_NOISE = re.compile(r"(?:^|\s)@\w+|[/*]+|^\s*[-#>]+")


def norm_quote(text: str) -> str:
    """A1-PL4 b), COV-12: a quote and its evidence compared without white space, comment markers (//, ///, /*, *)
    and a leading @tag, in either; the words themselves must stay the same."""
    return re.sub(r"\s+", " ", _QUOTE_NOISE.sub(" ", text or "")).strip().lower()


def scenario_signature(sc: Any) -> str:
    """A1-PL4 g), LA-4: what makes two scenarios the same attempt - the function, its targets, the inputs and the
    set-up, with white space normalised."""
    norm = lambda v: re.sub(r"\s+", " ", str(v)).strip()
    parts = [sc.function, ",".join(sorted(sc.gap_ids)),
             ";".join(sorted(f"{a.name}={norm(a.value)}" for a in sc.args)),
             ";".join(norm(x) for x in sc.setup),
             ";".join(f"{v.stub}:{','.join(norm(r) for r in v.returns)}" for v in getattr(sc, "stub_values", [])),
             ";".join(sorted(f"{v.name}={norm(v.value)}" for v in sc.variables))]
    before = getattr(sc, "calls_before", [])
    if before:                                       # a call sequence is another attempt [BP-2]
        parts.append(";".join(norm(x) for x in before))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def normalized_body(text: str) -> str:
    """A2-21p: a test body without comments and white space differences, to recognise an unchanged rewrite."""
    text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text or "", flags=re.S)
    return re.sub(r"\s+", "", text)


_HEADER_CACHE: dict[tuple, str] = {}


def headers_text(layout: "LayoutProfile") -> str:
    """The text of the SUT's headers (and of the SUT when it is a header) [A2-21ae]."""
    root = Path(layout.repo_root)
    rels = list(dict.fromkeys(layout.headers + ([layout.sut_file] if layout.header_sut else [])))
    key = (layout.repo_root, tuple(rels), layout.sut_sha256)
    if key not in _HEADER_CACHE:
        texts = []
        for rel in rels:
            path = root / rel
            if path.is_file():
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
        _HEADER_CACHE[key] = "\n".join(texts)
    return _HEADER_CACHE[key]


def class_of(fn: "FunctionInfo") -> str:
    """The class of a member function, qualified ("" for a free function)."""
    q = fn.qualified_name.replace("(anonymous namespace)::", "")
    return q.rsplit("::", 1)[0] if fn.member and "::" in q else ""


def internal_member(fn: "FunctionInfo", layout: "LayoutProfile") -> bool:
    """A2-21ae: a member of a class that no header declares (an unnamed namespace, a class of the SUT file itself):
    a test cannot name it."""
    cls = class_of(fn)
    if not cls:
        return False
    short = cls.split("::")[-1]
    return "(anonymous namespace)" in fn.qualified_name or not re.search(
        r"\b(?:class|struct)\s+(?:\w+\s+)*" + re.escape(short) + r"\b", headers_text(layout))


def static_member(fn: "FunctionInfo", layout: "LayoutProfile") -> bool:
    """A member function declared static in its class (called through the class, not an object)."""
    if not fn.member:
        return False
    short = fn.qualified_name.split("::")[-1]
    return bool(re.match(r"^\s*static\b", fn.signature)) or bool(re.search(
        r"\bstatic\b[^;{}()]*\b" + re.escape(short) + r"\s*\(", headers_text(layout)))


def callers_of(fn: "FunctionInfo", layout: "LayoutProfile") -> list["FunctionInfo"]:
    """The SUT's functions that call this one (by name), callable ones first."""
    short = fn.qualified_name.split("::")[-1]
    out = [f for f in layout.functions if f is not fn and f.qualified_name != fn.qualified_name and any(
        c == fn.name or c == fn.qualified_name or c.split("::")[-1] == short for c in f.calls)]
    return sorted(out, key=lambda f: internal_member(f, layout))


def keeps_state(fn: "FunctionInfo", layout: "LayoutProfile") -> str:
    """A1-PL2: why a behaviour of this function may need several calls in a row ("" when it keeps no state)."""
    statics = [v.name.split("::")[-1] for v in layout.local_statics
               if v.name.startswith((fn.qualified_name + "::", fn.name + "::"))]
    if statics:
        return "keeps state between calls in its static variable(s) " + ", ".join(statics[:4])
    if fn.member and not static_member(fn, layout):
        return "a member function: the object keeps state between calls"
    return ""


def fake_name_of(interface: str) -> str:
    return "ut_fake_" + re.sub(r"\W", "_", interface.split("::")[-1])


def object_recipe(fn: "FunctionInfo", layout: "LayoutProfile") -> tuple[list[str], str] | None:
    """A2-21ae: how a test builds the object a member function is called on - every argument a named object
    declared before (never a temporary bound to a reference) - and the expression to call it with. None for a
    free function, a static member or a member no test can name."""
    if not fn.member or static_member(fn, layout) or internal_member(fn, layout):
        return None
    cls = class_of(fn)
    short = cls.split("::")[-1]
    if fn.qualified_name.split("::")[-1] in (short, "~" + short):
        return None
    ctors = [f for f in layout.functions if f.member and f.qualified_name.replace("(anonymous namespace)::", "")
             .endswith(f"{short}::{short}")]
    ctor = min(ctors, key=lambda f: len(f.params)) if ctors else None
    lines, args = [], []
    for i, p in enumerate(ctor.params if ctor else []):
        base = re.sub(r"\b(const|volatile|struct|class)\b|[&*]", " ", p.base or p.type).strip()
        iface = next((f for f in layout.interfaces if f.name.split("::")[-1] == base.split("::")[-1]), None)
        if iface is not None or p.kind == "interface":
            lines.append(f"{fake_name_of(iface.name if iface else base)} ut_dep{i};")
            args.append(f"ut_dep{i}" if p.by_ref or "&" in p.type else f"&ut_dep{i}")
        elif p.by_ref or "&" in p.type:
            lines.append(f"{base or 'int'} ut_dep{i}{{}};")
            args.append(f"ut_dep{i}")
        elif "*" in p.type:
            lines.append(f"{base or 'int'} ut_dep{i}{{}};")
            args.append(f"&ut_dep{i}")
        else:
            args.append("{}")
    lines.append(f"{cls} ut_obj{('(' + ', '.join(args) + ')') if args else ''};")
    return lines, f"ut_obj.{fn.qualified_name.split('::')[-1]}"


def call_form(fn: "FunctionInfo", layout: "LayoutProfile") -> str:
    """A2-21ae: one line for the brain on how a test calls this function."""
    if internal_member(fn, layout):
        callers = [f.qualified_name for f in callers_of(fn, layout) if not internal_member(f, layout)]
        return (f"{fn.qualified_name} cannot be named by a test (its class is declared in no header): reach it through "
                f"{', '.join(callers[:3]) or 'the functions that call it'}.")
    if static_member(fn, layout):
        return f"a static member function: call it as {class_of(fn)}::{fn.qualified_name.split('::')[-1]}(...)."
    recipe = object_recipe(fn, layout)
    if recipe is not None:
        lines, call = recipe
        return ("a member function: declare the object and every constructor argument as a named variable first "
                f"(never a temporary), exactly: {' '.join(lines)} then call {call}(...). Set a fake's controls "
                "(<method>_return) on ut_depN before the call.")
    return ""


def reset_state(layout: "LayoutProfile") -> list["StateVar"]:
    """A2-21s: the state every test starts from - the SUT's own state and the mutable global variables of real
    dependencies linked into the test program (a test that sets STATUS_REG must not change the next test)."""
    state = list(layout.sut_state)
    names = {v.name for v in state}
    plan = layout.dependencies
    for d in (plan.entries if plan else []):
        name = d.qualified_name or d.name
        if d.kind == "variable" and d.decision == "real" and not d.is_const and not d.system and d.definition \
                and d.definition in plan.real_sources and name not in names:
            state.append(StateVar(name=name, type=d.type))
            names.add(name)
    return state


def patterns_of(fn: "FunctionInfo", layout: "LayoutProfile") -> list[str]:
    """LA-8: the constructs of a function a lesson can apply to, decided from the facts (no LLM)."""
    out = [f"language {layout.language}"]
    if fn.member:
        out.append("static member function" if static_member(fn, layout) else "member function")
        recipe = object_recipe(fn, layout)
        if recipe and any("ut_fake_" in line for line in recipe[0]):
            out.append("constructor takes an injected interface")
        if recipe and any(re.search(r"\but_dep\d+\{\};", line) and "ut_fake_" not in line for line in recipe[0]):
            out.append("constructor takes a reference or pointer")
    if internal_member(fn, layout):
        out.append("class no header declares")
    if keeps_state(fn, layout) and not fn.member:
        out.append("function keeps state between calls")
    if fn.is_static:
        out.append("static function")
    plan = layout.dependencies
    real = {d.name for d in (plan.entries if plan else []) if d.decision == "real"}
    if real & set(fn.calls):
        out.append("calls a real dependency")
    if any(p.kind == "callback" for p in fn.params):
        out.append("callback parameter")
    if any(p.kind in ("ptr_struct", "struct") for p in fn.params):
        out.append("structure parameter")
    return out


# ============================================================================ brain profiles [BP-1 .. BP-10]
# BP-2: the kinds of answer a brain may give, from narrow to wide (repair and decision are not on the ladder)
ANSWER_KINDS = ("values", "scenario", "call_sequence", "setup", "stub", "test", "suite_part", "repair", "decision",
                "file_edit")     # file_edit: a free edit of generated files after the offered repairs failed [BP-11]
KIND_LADDER = ("values", "scenario", "call_sequence", "setup", "stub", "test", "suite_part")
MINIMUM_KINDS = ("values", "scenario", "test", "repair", "decision", "file_edit")   # Qwen3 8B's before BP; BP-11
# BP-3: a cause on the test-building side, and the narrowest kind that contains the missing piece
CAUSE_KIND = {"needs_call_sequence": "call_sequence", "test_setup": "setup", "needs_dependency_control": "stub",
              "pipeline_limitation": "test"}
KIND_STEP = {"values": "A2.value", "scenario": "A1.plan", "call_sequence": "A1.plan", "setup": "A2.setup",
             "stub": "A2.stub", "test": "A2.tests", "suite_part": "A2.tests", "repair": "A3.repair",
             "decision": "", "file_edit": "A3.repair"}


class BrainProfile(Contract):
    """BP-1: what one brain may write and how much context it is given. The engine settings of ARCH-11 to ARCH-14
    (window, answer limit, characters per token, effort) stay in the brain's section of ut_config.json and belong to
    the profile; ``window_tokens`` here only overrides them for the limits of BP-5."""
    name: str
    kinds: list[str] = Field(default_factory=lambda: list(MINIMUM_KINDS))
    journal_tokens: int = 1500             # journal excerpt per function [LA-4, BP-5]
    long_function_lines: int = 150         # above it only the paths to the open items; 0: only when it does not fit
    window_tokens: int = 16384             # the window the limits of BP-5 are computed for
    scenarios_per_call: int = 4            # ARCH-11a
    cost_input_per_mtok: float = 0.0       # for the report only
    cost_output_per_mtok: float = 0.0

    def allows(self, kind: str) -> bool:
        return kind in self.kinds

    def digest(self) -> str:
        return sha256_bytes(self.model_dump_json().encode())[:16]

    def fits_whole(self, function_chars: int, facts_chars: int = 0) -> bool:
        """BP-5: does the whole function with its facts fit this profile's window (cautiously, 3 chars per token,
        half the window for the facts)?"""
        return (function_chars + facts_chars) / 3.0 <= self.window_tokens / 2

    def short_function(self, lines: int, function_chars: int, facts_chars: int = 0) -> bool:
        """LA-10, BP-5: is the function sent whole? The line limit applies only to a profile that sets one; a profile
        without one (0) sends it whole whenever it fits the window."""
        if self.long_function_lines > 0:
            return lines <= self.long_function_lines
        return self.fits_whole(function_chars, facts_chars)


DEFAULT_PROFILES: dict[str, BrainProfile] = {
    "minimum": BrainProfile(name="minimum"),
    "full": BrainProfile(name="full", kinds=list(ANSWER_KINDS), journal_tokens=8000, long_function_lines=0,
                         window_tokens=1_000_000, scenarios_per_call=4)}
DEFAULT_BRAIN_PROFILES = {"opus": "full", "fable": "full", "sonnet": "full", "haiku": "full",
                          "qwen_remote": "minimum", "qwen": "minimum"}


class ProfileBook(Contract):
    """BP-1, BP-6: the profiles, which brain has which, and a brain order per step. Configuration only."""
    profiles: dict[str, BrainProfile] = Field(default_factory=lambda: dict(DEFAULT_PROFILES))
    brains: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_BRAIN_PROFILES))
    steps: dict[str, list[str]] = Field(default_factory=dict)
    calibration_share: float = 0.9          # BP-8

    def of(self, brain: str) -> BrainProfile:
        """The profile of a brain; a brain without one gets "minimum" (BP-1)."""
        name = self.brains.get(brain, "minimum")
        return self.profiles.get(name) or self.profiles.get("minimum") or DEFAULT_PROFILES["minimum"]

    def allows(self, brain: str, kind: str) -> bool:
        return self.of(brain).allows(kind)

    def order(self, names: list[str], step: str = "") -> list[str]:
        """BP-6: the brains of a step's own order first, then the others in their usual order."""
        own = [n for n in self.steps.get(step, []) if n in names]
        return own + [n for n in names if n not in own]

    def record(self, brains: list[str]) -> list[str]:
        """BP-7: "brain: profile (sha256) kinds" for the result and the ledger."""
        out = []
        for b in brains:
            p = self.of(b)
            out.append(f"{b}: {p.name} ({p.digest()}) - {', '.join(p.kinds)}")
        return out


def brain_profiles(config_path: str | Path | None) -> ProfileBook:
    """BP-1: the "profiles" section of ut_config.json:
    {"brains": {"sonnet": "full", ...}, "definitions": {"minimum": {...}, "full": {...}}, "steps": {"A2.value": [...]},
     "calibration_share": 0.9}. Missing parts keep their defaults; an unknown answer kind in a profile is ignored."""
    section: dict[str, Any] = {}
    if config_path and Path(config_path).is_file():
        import json as _json
        section = _json.loads(Path(config_path).read_text(encoding="utf-8")).get("profiles", {}) or {}
    profiles = dict(DEFAULT_PROFILES)
    for name, raw in (section.get("definitions") or {}).items():
        data = {**(profiles[name].model_dump() if name in profiles else {}), **dict(raw or {}), "name": name}
        data["kinds"] = [k for k in data.get("kinds", MINIMUM_KINDS) if k in ANSWER_KINDS]
        profiles[name] = BrainProfile(**data)
    brains = {**DEFAULT_BRAIN_PROFILES, **(section.get("brains") or {})}
    return ProfileBook(profiles=profiles, brains=brains, steps=dict(section.get("steps") or {}),
                       calibration_share=float(section.get("calibration_share", 0.9)))


async def brains_for(agent: ba.AgentCore, kind: str, step: str = "", avoid: set[str] | frozenset = frozenset()
                     ) -> list[str]:
    """BP-3, BP-6: the available brains, in the step's order, whose profile allows ``kind``. Empty: no brain may give
    this kind now (BP-9) - the caller does not ask any brain for it."""
    book: ProfileBook = getattr(agent, "profiles", None) or ProfileBook()
    try:
        router = await agent._brains()
    except ba.AgentFault:
        return []
    names = [n for n in router.available if n in agent.allowed_brains or n in ("injected", "demo")]
    return [n for n in book.order(names, step) if n not in avoid and book.allows(n, kind)]


async def answering_profile(agent: ba.AgentCore, step: str = "") -> BrainProfile:
    """BP-5: the profile of the brain that would answer next (the first available brain of the step's order)."""
    book: ProfileBook = getattr(agent, "profiles", None) or ProfileBook()
    try:
        router = await agent._brains()
    except ba.AgentFault:
        return book.profiles.get("minimum") or DEFAULT_PROFILES["minimum"]
    names = [n for n in router.available if n in agent.allowed_brains or n in ("injected", "demo")]
    first = next(iter(book.order(names, step)), "")
    return book.of(first)


# BP-3: the brain steps that give one of the answer kinds; the other steps are not restricted by a profile
STEP_KIND = {"A1.plan": "scenario", "A2.value": "values", "A2.sequence": "call_sequence", "A2.setup": "setup",
             "A2.stub": "stub", "A2.tests": "test", "A3.repair": "repair", "A3.host": "repair",
             "A1.dependency": "decision", "A3.root_cause": "decision", "A4.review": "decision",
             "A3.outcome": "decision"}


def apply_profiles(agent: ba.AgentCore, config_path: str | Path | None) -> None:
    """BP-1, BP-3, BP-6: the agent asks a brain for a step only when the brain's profile allows the step's answer
    kind, and in the step's own brain order when the configuration gives one."""
    book = brain_profiles(config_path)
    agent.profiles = book

    def gate(step: str, names: list[str]) -> list[str] | None:
        kind = STEP_KIND.get(step)
        if not kind:
            return None
        return [n for n in book.order(list(names), step) if book.allows(n, kind)]
    agent.brains_for_step = gate
    agent.step_order = lambda step: book.steps.get(step) or None


def next_kind(current: str, cause: str, allowed: list[str] | tuple[str, ...]) -> str:
    """BP-3: the kind of the next attempt. A cause on the test-building side climbs the ladder to the next wider kind
    that contains the missing piece and is allowed; any other cause keeps the kind. "" when the piece needs a kind
    no available brain may give (BP-9)."""
    if cause not in CAUSE_KIND:
        return current if current in allowed else next((k for k in KIND_LADDER if k in allowed), "")
    wanted = CAUSE_KIND[cause]
    start = max(KIND_LADDER.index(wanted), KIND_LADDER.index(current) + 1 if current in KIND_LADDER else 0)
    return next((k for k in KIND_LADDER[start:] if k in allowed), "")


# ============================================================================ checks: guard or prediction [NC-6]
# NC-6: every check that refuses an answer before the tool declares its kind here. A guard protects the trust of the
# result and is never overruled; a prediction only predicts what the tool would reject, and never removes an item by
# itself: an item refused only by predictions is built alone, and the tool decides (NC-7). A refusal text that
# matches no prediction is a guard - an unknown check never lets an item through. A stub, stub variable or return
# sequence the dependency plan does not have is a guard of the plan: no test can be assembled with it.
PREDICTION_CHECKS = {
    "calls functions that do not exist here": "unknown name",              # A2-20, A1-PL4 grounding
    "unbalanced": "unbalanced braces or parentheses",
    "is set twice in a row": "value set twice",                           # A2-21t
    "is not an integer value": "value form",                              # A1-PL4 e
    "is not a floating-point value": "value form",
    "pass a variable declared in setup": "value form",
    "unknown names in": "unknown name",
    "is not a variable of the setup or a stub control": "unknown name",
    "is not a value": "value form",
    "earlier call is not a call of the SUT": "call sequence form",       # BP-2 call_sequence
    "argument(s), this call has": "macro arity",                          # A2-21ah (a)
}


def check_kind(reason: str) -> Literal["guard", "prediction"]:
    """NC-6: the kind of the check that gave this refusal."""
    return "prediction" if any(text in reason for text in PREDICTION_CHECKS) else "guard"


def prediction_name(reason: str) -> str:
    return next((name for text, name in PREDICTION_CHECKS.items() if text in reason), "guard")


def only_predictions(reasons: list[str]) -> bool:
    """NC-7: refused, and only by predictions - the item goes to a trial instead of being dropped."""
    return bool(reasons) and all(check_kind(r) == "prediction" for r in reasons)


# ============================================================================ recipes [NC-1 .. NC-3]
RECIPE_RULES = {"setup": "NC-2 setup", "stub": "NC-2 stub", "call_sequence": "NC-2 call_sequence"}


class Recipe(Contract):
    """NC-2: a building block the tool accepted, as a template. Parameters are written @@name@@ in the lines (braces
    are C code, so they are not used for parameters): the class, the method and the constructor's parameter types."""
    kind: Literal["setup", "stub", "call_sequence"]
    signature: str                      # the construct, from the front end
    lines: list[str] = Field(default_factory=list)
    call: str = ""                      # the call target, e.g. "ut_obj.@@method@@"
    made_by: str = ""                   # brain/model, or "library" for the application's recipes
    uses: int = 0

    def instantiate(self, params: dict[str, str]) -> tuple[list[str], str]:
        def fill(text: str) -> str:
            return re.sub(r"@@(\w+)@@", lambda m: params.get(m.group(1), m.group(0)), text)
        return [fill(line) for line in self.lines], fill(self.call)

    def complete(self, params: dict[str, str]) -> bool:
        """Every parameter of the template is known for this construct."""
        used = set(re.findall(r"@@(\w+)@@", "\n".join(self.lines) + self.call))
        return used <= set(params)


def templated(lines: list[str], call: str, params: dict[str, str]) -> tuple[list[str], str]:
    """NC-2: a block written for one class turned into a template: every parameter value (longest first, whole words
    only) becomes @@name@@, so the block applies to another class of the same construct."""
    order = sorted(((k, v) for k, v in params.items() if v), key=lambda kv: -len(kv[1]))

    def tpl(text: str) -> str:
        for k, v in order:
            text = re.sub(r"(?<![\w:])" + re.escape(v) + r"(?![\w])", f"@@{k}@@", text)
        return text
    return [tpl(line) for line in lines], tpl(call)


def construct_of(fn: "FunctionInfo", layout: "LayoutProfile") -> tuple[str, dict[str, str]]:
    """NC-2: the construct signature of the object a member function is called on, from the front end's facts, and
    the parameters of a recipe for it. Free functions have no object construct ("", {})."""
    if not fn.member or static_member(fn, layout):
        return "", {}
    cls = class_of(fn)
    short = cls.split("::")[-1]
    ctors = [f for f in layout.functions if f.member and f.qualified_name.replace("(anonymous namespace)::", "")
             .endswith(f"{short}::{short}")]
    ctor = min(ctors, key=lambda f: len(f.params)) if ctors else None
    params = {"cls": cls, "method": fn.qualified_name.split("::")[-1]}
    shapes = []
    for i, p in enumerate(ctor.params if ctor else []):
        base = re.sub(r"\b(const|volatile|struct|class)\b|[&*]", " ", p.base or p.type).strip()
        iface = next((f for f in layout.interfaces if f.name.split("::")[-1] == base.split("::")[-1]), None)
        how = "ref" if p.by_ref or "&" in p.type else "ptr" if "*" in p.type else "value"
        what = "interface" if iface is not None or p.kind == "interface" else p.kind or "other"
        shapes.append(f"{how} {what}")
        if base:
            params[f"t{i}"] = base
            if iface is not None:
                params[f"fake{i}"] = fake_name_of(iface.name)
    traits = [f"ctor({', '.join(shapes)})" if ctor else "no constructor declared"]
    if internal_member(fn, layout):
        traits.append("internal linkage")
    if "<" in cls or any(t.name.split("::")[-1] == short for t in getattr(layout, "templates", []) or []):
        traits.append("template")
    return "object: " + "; ".join(traits), params


def application_recipes(folder: Path | None = None) -> list[Recipe]:
    """NC-3: the application's recipe library (recipes/*.json next to the agents): reviewed data, never written by a
    run. A file that is not a valid recipe is skipped."""
    import json as _json
    folder = folder or APP_DIR / "recipes"
    out: list[Recipe] = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            raw = _json.loads(path.read_text(encoding="utf-8"))
            for item in raw if isinstance(raw, list) else [raw]:
                out.append(Recipe(**{**item, "made_by": item.get("made_by") or "library"}))
        except (OSError, ValueError, TypeError):
            continue
    return out


# ============================================================================ platform facts [NC-10, NC-11]
class PlatformFacts(Contract):
    """NC-10: how the platform and the toolchain behave, measured by the setup with small probe programs and written
    to the platform section of the tool manifest. ``measured`` is empty when only the host defaults are known."""
    os: str = ""                                   # windows | linux | darwin
    case_sensitive: bool = True                    # does the file system tell A.c from a.c
    page_bytes: int = 4096                         # register pages in host memory [A3-HW1]
    exe_suffix: str = ""
    line_ending: str = "\n"
    path_separator: str = "/"                      # how the compiler prints a path given with forward slashes
    paths_absolute: bool = False                   # does llvm-cov print absolute source paths
    outcomes: dict[str, list[int]] = Field(default_factory=dict)   # normal, crash, abort, exception, stack_overflow
    watchdog_marker: str = "UT_WATCHDOG"           # the harness's own marker of a watchdog end [A3-45d]
    watchdog_exit: int | None = 4
    measured: str = ""                             # date of the measurement

    def outcome_of(self, code: int) -> str:
        """The measured kind of an end with this exit status, or ""."""
        return next((kind for kind, codes in self.outcomes.items() if code in codes), "")


def host_facts() -> PlatformFacts:
    """The facts a host shows without a measurement (used until the setup has measured them)."""
    import mmap as _mmap
    windows = os.name == "nt"
    return PlatformFacts(os="windows" if windows else ("darwin" if sys.platform == "darwin" else "linux"),
                         case_sensitive=not windows and sys.platform != "darwin",
                         page_bytes=int(getattr(_mmap, "ALLOCATIONGRANULARITY", 4096) or 4096),
                         exe_suffix=".exe" if windows else "", line_ending="\r\n" if windows else "\n",
                         path_separator="\\" if windows else "/")


def platform_facts(manifest: "Tools | dict[str, Any] | None") -> PlatformFacts:
    """NC-11: the platform facts of the manifest; the host defaults when the manifest has none."""
    data = manifest.data if isinstance(manifest, Tools) else (manifest or {})
    raw = data.get("platform") if isinstance(data, dict) else None
    if not raw:
        return host_facts()
    try:
        return PlatformFacts(**raw)
    except (TypeError, ValueError):
        return host_facts()


def path_key(path: str, facts: PlatformFacts | None = None, fold: bool | None = None) -> str:
    """NC-11: a path as an identity: separators unified, '.' parts removed, a drive letter in one case, and letter case
    folded where the file system does not distinguish it (``fold`` overrides the facts)."""
    facts = facts or host_facts()
    text = str(path).strip().strip('"').replace("\\", "/")
    while "//" in text[1:]:
        text = text[0] + text[1:].replace("//", "/")
    text = "/".join(p for p in text.split("/") if p != ".")
    if re.match(r"^[A-Za-z]:/", text):
        text = text[0].lower() + text[1:]
    return text.lower() if (fold if fold is not None else not facts.case_sensitive) else text


def windows_path(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", path)) or "\\" in path


def same_file(a: str, b: str, facts: PlatformFacts | None = None) -> bool:
    """NC-11, A3-CL14: do two paths name the same file - whatever form a tool printed them in (absolute or relative,
    either separator, any letter case where the file system ignores it)? A relative path matches an absolute one that
    ends with it at a folder boundary. Letter case is folded when the measured file system ignores it, when a path is
    a Windows path, and when nothing was measured: the paths compared are the few files of one bundle."""
    fold = facts is None or not facts.case_sensitive or windows_path(a) or windows_path(b)
    ka, kb = path_key(a, facts, fold), path_key(b, facts, fold)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    shorter, longer = sorted((ka, kb), key=len)
    return not re.match(r"^([a-z]:)?/", shorter) and longer.endswith("/" + shorter)


def program_outcome(code: int, text: str, facts: PlatformFacts | None = None) -> str:
    """NC-11, NC-12: how a test program ended, from its exit status and output and the measured facts: normal,
    watchdog, timeout, crash, abort, exception, stack_overflow - or unknown when the facts do not describe it."""
    facts = facts or host_facts()
    if facts.watchdog_marker and facts.watchdog_marker + "|" in text:
        return "watchdog"
    if code == -9 and "UT_TIMEOUT" in text:
        return "timeout"
    if code == 0:
        return "normal"
    measured = facts.outcome_of(code)
    if measured:
        return measured
    if code < 0:                                   # a signal on Linux and macOS
        return {-11: "crash", -6: "abort", -7: "crash", -8: "crash", -4: "crash", -5: "crash"}.get(code, "unknown")
    if code > 0xFFFF:                              # Windows NTSTATUS
        return {0xC0000005: "crash", 0xC00000FD: "stack_overflow", 0xC0000094: "crash", 0xC0000409: "abort",
                0xC000001D: "crash", 0xE06D7363: "exception"}.get(code, "unknown")
    return "unknown"


# ============================================================================ pipeline findings [NC-5]
def finding_signature(kind: str, subject: str, message: str = "") -> str:
    """NC-5: the same finding in another run or file - normalized as in ARCH-23."""
    return ba.RuleFailure(rule=kind, failure="", subject=subject, message=message).signature()


def findings_path(config_path: str | Path | None, output_dir: str = "ut_output") -> Path:
    """NC-5: the application's findings file, ut_output/pipeline_findings.jsonl next to ut_config.json."""
    base = Path(config_path).resolve().parent if config_path and Path(config_path).is_file() else APP_DIR
    out = Path(output_dir)
    return (out if out.is_absolute() else base / out) / "pipeline_findings.jsonl"


def record_findings(path: Path, findings: list["PipelineFinding"]) -> list[str]:
    """NC-5: appends this run's findings (never credentials) and returns every signature with its count across runs,
    most frequent first: "3x pipeline_limitation: <subject> - <message>"."""
    import json as _json
    counts: dict[str, int] = {}
    shown: dict[str, str] = {}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if findings:
            text = "".join(_json.dumps(f.model_dump(mode="json"), ensure_ascii=False) + "\n" for f in findings)
            with open(path, "a", encoding="utf-8") as handle:               # one write: whole lines [ORC-23 (d)]
                handle.write(text)
        for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
            try:
                raw = _json.loads(line)
            except ValueError:
                continue
            sig = raw.get("signature", "")
            counts[sig] = counts.get(sig, 0) + 1
            shown[sig] = f"{raw.get('kind', '')}: {raw.get('subject', '')} - {raw.get('message', '')}"[:300]
    except OSError:
        return []
    for f in findings:
        f.count = counts.get(f.signature, 1)
    return [f"{n}x {shown[s]}" for s, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


# ============================================================================ names of functions [NC-4]
OPERATOR_WORDS = {"+": "plus", "-": "minus", "*": "times", "/": "div", "%": "mod", "==": "eq", "!=": "ne", "<": "lt",
                  ">": "gt", "<=": "le", ">=": "ge", "<=>": "cmp", "=": "assign", "+=": "plus_assign",
                  "-=": "minus_assign", "*=": "times_assign", "/=": "div_assign", "%=": "mod_assign", "&=": "and_assign",
                  "|=": "or_assign", "^=": "xor_assign", "<<=": "shl_assign", ">>=": "shr_assign", "[]": "index",
                  "()": "call", "<<": "shl", ">>": "shr", "&&": "and", "||": "or", "!": "not", "&": "bitand",
                  "|": "bitor", "^": "xor", "~": "compl", "++": "inc", "--": "dec", "->": "arrow", "->*": "arrow_star",
                  ",": "comma"}


def safe_name(name: str) -> str:
    """A function name as part of an identifier (test names, scenario ids): namespaces joined with _, an operator
    named in words (operator+ -> operator_plus), every other character that is not allowed replaced by _."""
    name = name.replace("(anonymous namespace)::", "")

    def word(m: "re.Match[str]") -> str:
        symbol = re.sub(r"\s+", "", m.group(1))
        return "operator_" + OPERATOR_WORDS.get(symbol, re.sub(r"\W+", "_", symbol).strip("_") or "op")
    name = re.sub(r"\boperator\s*(\(\s*\)|\[\s*\]|[^\w\s:]+|\s+[A-Za-z_][\w:<>*&\s]*)", word, name)
    return re.sub(r"\W", "_", name.replace("::", "_"))


def overloads(fn: "FunctionInfo", layout: "LayoutProfile") -> list["FunctionInfo"]:
    """Every function of the SUT with the same name, in the order of the file (one for a function that has none)."""
    key = fn.qualified_name or fn.name
    return sorted((f for f in layout.functions if (f.qualified_name or f.name) == key), key=lambda f: f.line_start)


def function_stem(fn: "FunctionInfo", layout: "LayoutProfile") -> str:
    """The function's part of a test name or scenario id: unique also for overloads (scale, scale_2)."""
    same = overloads(fn, layout)
    stem = safe_name(fn.name)
    index = next((k for k, f in enumerate(same) if f.line_start == fn.line_start), 0)
    return stem if len(same) < 2 or index == 0 else f"{stem}_{index + 1}"


def function_of(name: str, layout: "LayoutProfile", line: int = 0) -> "FunctionInfo | None":
    """The function a scenario, gap or input names; for overloads the one that starts at, or contains, ``line``."""
    same = [f for f in layout.functions if name in (f.name, f.qualified_name)]
    if line and len(same) > 1:
        return next((f for f in same if f.line_start == line), None) or \
            next((f for f in same if f.line_start <= line <= f.line_end), same[0])
    return same[0] if same else None


CONTROL_WORDS = r"(?:if|while|for|switch|return|else|do|case|sizeof|throw|new|delete)"


def definition_match(text: str, name: str) -> "re.Match[str] | None":
    """The definition of the function ``name`` in a source text, never a call: before the name only a declaration's
    words (type, storage class, attributes, qualifiers, Class::), never "(" of a call or a control statement; the
    return type may stand on the line before (BSD style). Its match ends at the opening brace."""
    short = re.escape(name.split("::")[-1])
    owner = r"(?:[A-Za-z_]\w*(?:<[^<>;(){}]*>)?::)*"
    pattern = (r"^(?!\s*" + CONTROL_WORDS + r"\b)[\w \t*&:<>,~\[\]]*?(?:\n[\w \t*&]*?)?" + owner + r"~?\b" + short +
               r"\s*\((?:[^;{}()]|\([^;{}()]*\))*\)\s*(?:const\b\s*)?(?:noexcept\b\s*)?(?:override\b\s*)?"
               r"(?:->\s*[\w:<>*& ]+)?(?::[^;{]*)?\{")
    for m in re.finditer(pattern, text, re.M):
        head = text[m.start(): m.end()]
        if re.search(r"(?:^|[^\w])" + CONTROL_WORDS + r"\s*\(", head.split(name.split("::")[-1])[0]):
            continue
        return m
    return None


def dependency_code(fn: "FunctionInfo", layout: "LayoutProfile", max_functions: int = 4, max_lines: int = 30) -> str:
    """A1-PL2, NC-1: the code of the real dependencies the function calls, from their files in the repository, so a
    brain can choose inputs that pass through them (for example hw_read(x) returning STATUS_REG + x). Short functions
    only, whole; nothing from system headers."""
    plan = layout.dependencies
    root = Path(layout.repo_root)
    out: list[str] = []
    for d in (plan.entries if plan else []):
        if len(out) >= max_functions:
            break
        if d.decision != "real" or d.system or d.kind not in ("function", "method") or not d.definition:
            continue
        if d.name not in fn.calls and (d.qualified_name or "") not in fn.calls:
            continue
        path = root / d.definition
        if not path.is_file():
            continue
        text = read_source(path)
        m = definition_match(text, d.name)
        if m is None:
            continue
        depth, k = 0, m.end() - 1
        while k < len(text):
            depth += {"{": 1, "}": -1}.get(text[k], 0)
            if depth == 0:
                break
            k += 1
        body = text[m.start(): k + 1]
        if body.count("\n") + 1 > max_lines:
            continue
        out.append(f"// {d.definition}\n{body}")
    return "\n\n".join(out)


DECLARATION = re.compile(r"^\s*(?:(?:const|static|volatile|constexpr|struct|class|enum)\s+)*"
                         r"([A-Za-z_][\w:]*(?:\s*<[^;(){}]*>)?)[\s*&]+([A-Za-z_]\w*)\s*(?:[;,({=\[]|$)")
NOT_TYPES = {"return", "else", "case", "goto", "throw", "new", "delete", "sizeof", "typedef", "using", "do", "if",
             "while", "for", "switch"}


def declared_names(statement: str) -> set[str]:
    """The variables or objects a statement declares ("TorqueMap ut_obj;", "int a = 1, *b;" gives a and b)."""
    out = set()
    for part in re.split(r";", statement):
        m = DECLARATION.match(part)
        if not m or m.group(1).split("<")[0].strip() in NOT_TYPES:
            continue
        out.add(m.group(2))
        depth, start, rest = 0, 0, part[m.end(2):]
        pieces = []
        for i, ch in enumerate(rest):
            depth += ch in "([{<"
            depth -= ch in ")]}>"
            if ch == "," and depth == 0:
                pieces.append(rest[start:i])
                start = i + 1
        pieces.append(rest[start:])
        for piece in pieces[1:]:
            more = re.match(r"\s*[*&\s]*([A-Za-z_]\w*)\s*(?:[=({\[]|$)", piece)
            if more:
                out.add(more.group(1))
    return out


def without_redeclarations(statements: list[str], declared: set[str]) -> tuple[list[str], list[str]]:
    """ARCH-20 (form is forgiven), A2-21ae: the statements of a set-up without those that declare again an object the
    building block already declares (a brain often writes "TorqueMap ut_obj;" although the object exists). Returns the
    statements kept and those dropped. A line with several statements keeps its other statements."""
    kept, dropped = [], []
    for stmt in statements:
        parts = [x for x in stmt.split(";")]
        keep = [x for x in parts if not (declared_names(x + ";") & declared)]
        if len(keep) != len(parts):
            dropped.append(stmt.strip())
            rest = ";".join(keep).strip()
            if rest.strip(" ;"):
                kept.append(rest if rest.endswith((";", "}")) else rest + ";")
        else:
            kept.append(stmt)
    return kept, dropped


# ============================================================================ generated files per SUT file [A2-21af]
STUB_HEADER = re.compile(r"(?:^|[/\\])ut_stubs(?:_\w+)?\.h$")
SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".c++")


def unit_name(layout: "LayoutProfile") -> str:
    """A2-21af: the unit name of the SUT file, read from its suite file (test_<unit>.c)."""
    stem = Path(layout.suite_file or layout.sut_file).stem
    return safe_name(stem[5:] if stem.startswith("test_") and len(stem) > 5 else stem) or "sut"


def unit_name_of(sut_file: str, repository_files: list[str]) -> str:
    """A2-21af: the SUT file's name without its extension, or its path when another source file of the repository
    has the same name (src/uart.c, hal/uart.c -> src_uart, hal_uart)."""
    sut = Path(sut_file.replace("\\", "/"))
    same = [f for f in repository_files if Path(f.replace("\\", "/")).stem == sut.stem
            and Path(f.replace("\\", "/")).suffix.lower() in SOURCE_SUFFIXES
            and f.replace("\\", "/") != sut.as_posix()]
    if not same:
        return re.sub(r"\W", "_", sut.stem) or "sut"
    return re.sub(r"\W", "_", sut.with_suffix("").as_posix()).strip("_") or "sut"


def stub_names(layout: "LayoutProfile") -> tuple[str, str]:
    """A2-21af: (stub source path, stub header file name) of the SUT file."""
    ext = "cpp" if layout.language == "cpp" else "c"
    unit = unit_name(layout)
    return f"{layout.writable_stub_dir.rstrip('/')}/ut_stubs_{unit}.{ext}", f"ut_stubs_{unit}.h"


# ============================================================================ shared files across parallel runs
class LockBusy(Exception):
    """file_lock: another run held the lock longer than the caller wanted to wait."""


class file_lock:
    """ORC-23 (d): one holder at a time across processes (several SUT files run in parallel, each with its own agent
    processes). The lock is a file created exclusively; its holder refreshes it, so the lock of a run that ended
    without releasing it is taken over after ``stale_s``. Raises LockBusy after ``wait_s``."""

    def __init__(self, path: str | Path, wait_s: float = 3600.0, stale_s: float = 120.0, poll_s: float = 0.5):
        self.path, self.wait_s, self.stale_s, self.poll_s = Path(path), wait_s, stale_s, poll_s
        self._stop = None
        self._beat = None

    def __enter__(self) -> "file_lock":
        import threading
        import time as _time
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = _time.monotonic() + self.wait_s
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {_time.time():.0f}\n".encode())
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if _time.time() - self.path.stat().st_mtime > self.stale_s:
                        self.path.unlink()
                        continue
                except OSError:
                    continue
                if _time.monotonic() > deadline:
                    raise LockBusy(f"{self.path.name} is held by another run") from None
                _time.sleep(self.poll_s)
        self._stop = threading.Event()

        def beat() -> None:
            while not self._stop.wait(max(1.0, self.stale_s / 4)):
                try:
                    os.utime(self.path)
                except OSError:
                    pass
        self._beat = threading.Thread(target=beat, daemon=True)
        self._beat.start()
        return self

    def __exit__(self, *_: Any) -> None:
        if self._stop is not None:
            self._stop.set()
            self._beat.join(timeout=5)
        try:
            self.path.unlink()
        except OSError:
            pass


def dependency_key(dep: "Dependency") -> str:
    """A1-DA4: a dependency's identity - its qualified name, and for a function its parameter types, so that the
    overloads of a name (hw::write(uint8_t), hw::write(uint16_t)) are each decided and stubbed."""
    name = dep.qualified_name or dep.name
    if dep.kind == "variable":
        return name
    types = ",".join(" ".join(p[0].split()) for p in dep.params)
    return f"{name}({types})"


def number_overloads(entries: list["Dependency"]) -> None:
    """A1-DA4: the overloads of one name get 0, 1, 2 ... in the order of their declarations."""
    groups: dict[str, list["Dependency"]] = {}
    for d in entries:
        if d.kind != "variable":
            groups.setdefault(d.qualified_name or d.name, []).append(d)
    for group in groups.values():
        for i, d in enumerate(sorted(group, key=lambda d: (d.header, d.line, dependency_key(d)))):
            d.overload = i if len(group) > 1 else 0


# ============================================================================ standard headers [A1-7]
STANDARD_HEADERS = {f"{n}.h" for n in (
    "assert complex ctype errno fenv float inttypes iso646 limits locale math setjmp signal stdalign stdarg stdatomic "
    "stdbit stdbool stdckdint stddef stdint stdio stdlib stdnoreturn string tgmath threads time uchar wchar wctype "
    "unistd fcntl pthread sys/types sys/stat sys/time sys/mman windows intrin malloc memory strings").split()}


# ============================================================================ target register windows [A3-HW1]
def _macro_int(expr: str, macros: dict[str, str], depth: int = 0) -> int | None:
    """An integer constant expression of literals and macros (0x40000000UL + 0x1000, (1u << 4) | BASE)."""
    if depth > 8:
        return None
    expr = re.sub(r"\(\s*(?:const\s+)?(?:volatile\s+)?(?:unsigned\s+)?[A-Za-z_]\w*\s*\)", " ", expr)   # (uint32_t)
    def name(m: "re.Match[str]") -> str:
        value = _macro_int(macros[m.group(0)], macros, depth + 1) if m.group(0) in macros else None
        return str(value) if value is not None else m.group(0)
    expr = re.sub(r"\b(0[xX][0-9a-fA-F]+|\d+)[uUlL]+\b", r"\1", expr)
    expr = re.sub(r"\b[A-Za-z_]\w*\b", name, expr)
    if not re.fullmatch(r"[\s0-9a-fA-FxX()+\-*|&<>~^]+", expr) or re.search(r"[a-wyzA-WYZ]", re.sub(r"0[xX][0-9a-fA-F]+", "", expr)):
        return None
    try:
        value = eval(expr, {"__builtins__": {}}, {})                                          # noqa: S307
    except Exception:                                                                        # noqa: BLE001
        return None
    return value if isinstance(value, int) else None


def register_windows(layout: "LayoutProfile", defaults: list[list[int]], files: list[Path] | None = None,
                     span: int = 0x100000, limit: int = 16) -> tuple[list[list[int]], list[str]]:
    """A3-HW1: the host memory windows the harness maps for target registers: the configured ones and a window
    around every constant address the SUT and its headers turn into a pointer ((volatile uint32_t *)0xF0001000,
    #define PERIPH_BASE 0x40000000UL used in such a cast). Addresses the host cannot map (below 64 KiB) are
    returned as notes. Returns (windows, notes)."""
    root = Path(layout.repo_root)
    paths = files if files is not None else [root / layout.sut_file, *[root / h for h in layout.headers],
                                             *[root / h for h in (layout.dependencies.headers if layout.dependencies else [])]]
    texts = []
    for p in dict.fromkeys(paths):
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = re.sub(r"\\[ \t]*\r?\n", " ", re.sub(r"/\*.*?\*/", " ", text, flags=re.S))
        texts.append(re.sub(r"//[^\n]*", "", text))
    macros: dict[str, str] = {}
    for text in texts:
        for m in re.finditer(r"(?m)^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+(.+)$", text):
            macros.setdefault(m.group(1), m.group(2).strip())
    cast = re.compile(r"\(\s*(?:const\s+)?(?:volatile\s+)?[A-Za-z_][\w\s]*?(?:volatile\s*)?\*\s*(?:volatile\s*)?\)\s*"
                      r"(\([^;()\n]*(?:\([^;()\n]*\)[^;()\n]*)*\)|[A-Za-z_]\w*|0[xX][0-9a-fA-F]+[uUlL]*|\d+[uUlL]*)")
    addresses = set()
    for text in texts:
        for m in cast.finditer(text):
            value = _macro_int(m.group(1), macros)
            if value is not None and value > 0:
                addresses.add(value)
    for name, body in macros.items():                       # a base address the casts reach through macros
        if re.search(r"(?:BASE|ADDR|ADDRESS)\b", name.upper()):
            value = _macro_int(body, macros)
            if value is not None and value >= 0x10000:
                addresses.add(value)
    windows = [list(w[:2]) for w in defaults]
    notes = []
    for a in sorted(addresses):
        if a < 0x10000:
            notes.append(f"register address 0x{a:X} is below 64 KiB: the host cannot map it [A3-HW1]")
            continue
        if any(lo <= a < hi for lo, hi in windows):
            continue
        lo = a & ~(span - 1)
        windows.append([lo, lo + span])
    merged: list[list[int]] = []
    for lo, hi in sorted(windows):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged[:max(limit, len(defaults))], notes[:8]


# ============================================================================ the harness API [A2-21ah]
# The header every generated test includes. Agent3 delivers and builds it; Agent2 and the checks read the names and
# argument counts of its macros from this text, so they always match the harness that is built.
HARNESS_H = r"""/* ut_harness.h - generated by agent_unittest_compilation (ut_harness 1.3).
   A minimal unit-test harness; no external test framework is needed. */
#ifndef UT_HARNESS_H
#define UT_HARNESS_H
#include <stdio.h>
#include <string.h>
#include <math.h>
#include <stdint.h>
#ifdef __cplusplus
#include <exception>
#include <typeinfo>
extern "C" {
#endif
typedef void (*ut_fn)(void);
typedef struct { const char *name; ut_fn fn; } ut_test_t;
void ut_fail(const char *file, int line, const char *what, const char *expected, const char *actual);
void ut_observe(const char *id, const char *value);
void ut_stub_call(void);            /* every stub call counts against the test's call budget [A3-45b] */
void ut_expect_no_return(void);     /* the next call is expected never to return [A2-21y] */
const char *ut_type_name(const char *mangled);   /* readable C++ type name [A3-45e] */
/* The result of target assembly replaced on the host (0 unless the test sets it) [A3-HW3]. */
extern unsigned long long ut_asm_value;
unsigned long long ut_asm_result(void);
/* Hardware that sets (or clears) register bits after a while [A2-21ab], while the code under test waits. */
void ut_later(volatile void *addr, unsigned long long bits, int size, int ms, int set);
void ut_before_each(void);          /* defined in the generated stub file */
#ifdef __cplusplus
}
#define UT_SUITE_BEGIN extern "C" const ut_test_t ut_tests[] = {
#define UT_SUITE_END {0, 0} }; extern "C" const int ut_test_count = (int)(sizeof(ut_tests) / sizeof(ut_tests[0])) - 1;
#else
#define UT_SUITE_BEGIN const ut_test_t ut_tests[] = {
#define UT_SUITE_END {0, 0} }; const int ut_test_count = (int)(sizeof(ut_tests) / sizeof(ut_tests[0])) - 1;
#endif
#define UT_CASE(name) { #name, name },
#define UT_TEST(name) static void name(void)

/* A target register in host memory [A3-HW1]: the page is mapped at the first access. */
#define UT_REG32(addr) (*(volatile uint32_t *)(uintptr_t)(addr))
#define UT_REG16(addr) (*(volatile uint16_t *)(uintptr_t)(addr))
#define UT_REG8(addr) (*(volatile uint8_t *)(uintptr_t)(addr))
#define UT_REG_SET_LATER(reg, bits, ms) ut_later((volatile void *)&(reg), (unsigned long long)(bits), \
    (int)sizeof(reg), (ms), 1)
#define UT_REG_CLEAR_LATER(reg, bits, ms) ut_later((volatile void *)&(reg), (unsigned long long)(bits), \
    (int)sizeof(reg), (ms), 0)
/* A call that must not return (an endless main loop): ended by the harness, which is the expected result [A2-21y]. */
#define UT_EXPECT_NO_RETURN(stmt) do { ut_expect_no_return(); stmt; \
    ut_fail(__FILE__, __LINE__, "UT_EXPECT_NO_RETURN(" #stmt ")", "no return", "returned"); } while (0)
#ifdef __cplusplus
/* Exceptions [A3-45e]; a build without exceptions (-fno-exceptions) or RTTI (-fno-rtti) compiles too [A2-21ag] */
#if defined(__cpp_exceptions) || defined(__EXCEPTIONS) || defined(_CPPUNWIND)
#define UT_HAS_EXCEPTIONS 1
#else
#define UT_HAS_EXCEPTIONS 0
#endif
#if defined(__cpp_rtti) || defined(__GXX_RTTI) || defined(_CPPRTTI)
#define UT_EXCEPTION_NAME(e) ut_type_name(typeid(e).name())
#else
#define UT_EXCEPTION_NAME(e) "std::exception"
#endif
#if UT_HAS_EXCEPTIONS
#define UT_EXPECT_THROW(stmt, type) do { bool ut_thrown_ = false; try { stmt; } catch (const type &) { ut_thrown_ = true; } \
    catch (...) { ut_thrown_ = true; ut_fail(__FILE__, __LINE__, "UT_EXPECT_THROW(" #stmt ", " #type ")", #type, "another exception"); } \
    if (!ut_thrown_) ut_fail(__FILE__, __LINE__, "UT_EXPECT_THROW(" #stmt ", " #type ")", #type, "no exception"); } while (0)
#define UT_EXPECT_ANY_THROW(stmt) do { bool ut_thrown_ = false; try { stmt; } catch (...) { ut_thrown_ = true; } \
    if (!ut_thrown_) ut_fail(__FILE__, __LINE__, "UT_EXPECT_ANY_THROW(" #stmt ")", "an exception", "no exception"); } while (0)
#define UT_EXPECT_NO_THROW(stmt) do { try { stmt; } catch (const std::exception &ut_e_) { \
    ut_fail(__FILE__, __LINE__, "UT_EXPECT_NO_THROW(" #stmt ")", "no exception", UT_EXCEPTION_NAME(ut_e_)); } \
    catch (...) { ut_fail(__FILE__, __LINE__, "UT_EXPECT_NO_THROW(" #stmt ")", "no exception", "unknown"); } } while (0)
#define UT_OBSERVE_THROW(id, stmt) do { try { stmt; ut_observe(id, "none"); } \
    catch (const std::exception &ut_e_) { ut_observe(id, UT_EXCEPTION_NAME(ut_e_)); } \
    catch (...) { ut_observe(id, "unknown"); } } while (0)
#define UT_CALL_ANY(stmt) do { try { stmt; } catch (...) {} } while (0)
#else
#define UT_EXPECT_THROW(stmt, type) ut_fail(__FILE__, __LINE__, "UT_EXPECT_THROW(" #stmt ", " #type ")", #type, \
    "exceptions are disabled in this build")
#define UT_EXPECT_ANY_THROW(stmt) ut_fail(__FILE__, __LINE__, "UT_EXPECT_ANY_THROW(" #stmt ")", "an exception", \
    "exceptions are disabled in this build")
#define UT_EXPECT_NO_THROW(stmt) do { stmt; } while (0)
#define UT_OBSERVE_THROW(id, stmt) do { stmt; ut_observe(id, "none"); } while (0)
#define UT_CALL_ANY(stmt) do { stmt; } while (0)
#endif
#endif

#define UT_EXPECT_TRUE(c) do { if (!(c)) ut_fail(__FILE__, __LINE__, "UT_EXPECT_TRUE(" #c ")", "true", "false"); } while (0)
#define UT_EXPECT_FALSE(c) do { if (c) ut_fail(__FILE__, __LINE__, "UT_EXPECT_FALSE(" #c ")", "false", "true"); } while (0)
#define UT_EXPECT_EQ_INT(actual, expected) do { long long ut_a_ = (long long)(actual); long long ut_e_ = (long long)(expected); \
    if (ut_a_ != ut_e_) { char ut_eb_[32], ut_ab_[32]; snprintf(ut_eb_, sizeof ut_eb_, "%lld", ut_e_); snprintf(ut_ab_, sizeof ut_ab_, "%lld", ut_a_); \
    ut_fail(__FILE__, __LINE__, "UT_EXPECT_EQ_INT(" #actual ", " #expected ")", ut_eb_, ut_ab_); } } while (0)
#define UT_EXPECT_NE_INT(actual, other) do { long long ut_a_ = (long long)(actual); long long ut_o_ = (long long)(other); \
    if (ut_a_ == ut_o_) { char ut_ab_[32]; snprintf(ut_ab_, sizeof ut_ab_, "%lld", ut_a_); \
    ut_fail(__FILE__, __LINE__, "UT_EXPECT_NE_INT(" #actual ", " #other ")", "different", ut_ab_); } } while (0)
#define UT_EXPECT_EQ_DBL(actual, expected, tol) do { double ut_a_ = (double)(actual); double ut_e_ = (double)(expected); \
    if (fabs(ut_a_ - ut_e_) > (double)(tol)) { char ut_eb_[48], ut_ab_[48]; snprintf(ut_eb_, sizeof ut_eb_, "%.17g", ut_e_); snprintf(ut_ab_, sizeof ut_ab_, "%.17g", ut_a_); \
    ut_fail(__FILE__, __LINE__, "UT_EXPECT_EQ_DBL(" #actual ", " #expected ")", ut_eb_, ut_ab_); } } while (0)
#define UT_EXPECT_EQ_STR(actual, expected) do { const char *ut_a_ = (actual); const char *ut_e_ = (expected); \
    if ((ut_a_ == 0) != (ut_e_ == 0) || (ut_a_ && strcmp(ut_a_, ut_e_) != 0)) \
    ut_fail(__FILE__, __LINE__, "UT_EXPECT_EQ_STR(" #actual ", " #expected ")", ut_e_ ? ut_e_ : "(null)", ut_a_ ? ut_a_ : "(null)"); } while (0)
#define UT_EXPECT_NULL(p) do { if ((p) != 0) ut_fail(__FILE__, __LINE__, "UT_EXPECT_NULL(" #p ")", "null", "non-null"); } while (0)
#define UT_EXPECT_NOT_NULL(p) do { if ((p) == 0) ut_fail(__FILE__, __LINE__, "UT_EXPECT_NOT_NULL(" #p ")", "non-null", "null"); } while (0)

/* Observation points: record what the current implementation does [A3-OBS1]. */
#define UT_OBSERVE_INT(id, expr) do { char ut_ob_[32]; snprintf(ut_ob_, sizeof ut_ob_, "%lld", (long long)(expr)); ut_observe(id, ut_ob_); } while (0)
#define UT_OBSERVE_DBL(id, expr) do { char ut_ob_[48]; snprintf(ut_ob_, sizeof ut_ob_, "%.17g", (double)(expr)); ut_observe(id, ut_ob_); } while (0)
#define UT_OBSERVE_STR(id, expr) do { const char *ut_os_ = (expr); ut_observe(id, ut_os_ ? ut_os_ : "(null)"); } while (0)
#define UT_OBSERVE_BOOL(id, expr) do { ut_observe(id, (expr) ? "1" : "0"); } while (0)

/* Familiar names, so generated code compiles on the first attempt more often. */
#define EXPECT_TRUE UT_EXPECT_TRUE
#define EXPECT_FALSE UT_EXPECT_FALSE
#define EXPECT_EQ UT_EXPECT_EQ_INT
#define EXPECT_NE UT_EXPECT_NE_INT
#define EXPECT_STREQ UT_EXPECT_EQ_STR
#define EXPECT_NEAR UT_EXPECT_EQ_DBL
#endif
"""


def harness_api(header: str = HARNESS_H) -> dict[str, int]:
    """A2-21ah: every macro and function the harness header declares -> its number of parameters (-1: any)."""
    out: dict[str, int] = {}
    text = re.sub(r"\\\s*\n", " ", header)
    for m in re.finditer(r"#\s*define\s+([A-Za-z_]\w*)\(([^)]*)\)", text):
        params = [p.strip() for p in m.group(2).split(",") if p.strip()]
        out.setdefault(m.group(1), -1 if "..." in params else len(params))
    for m in re.finditer(r"#\s*define\s+([A-Za-z_]\w*)(?![\w(])", text):
        out.setdefault(m.group(1), -1)
    for m in re.finditer(r"\b(?:void|int|const\s+char\s*\*|unsigned\s+long\s+long)\s+\**(ut_\w+)\s*\(([^)]*)\)\s*;", text):
        params = [p for p in m.group(2).split(",") if p.strip() and p.strip() != "void"]
        out.setdefault(m.group(1), len(params))
    for m in re.finditer(r"\bextern\s+[\w\s]+?\b(ut_\w+)\s*;", text):
        out.setdefault(m.group(1), -1)
    return out


def macro_arity_problem(body: str, api: dict[str, int] | None = None) -> str:
    """A2-21ah (a): a harness macro called with another number of arguments than its definition takes."""
    api = harness_api() if api is None else api
    for m in re.finditer(r"\b(UT_[A-Z0-9_]+)\s*\(", body):
        want = api.get(m.group(1))
        if want is None or want < 0:
            continue
        depth, k, args, cur = 1, m.end(), [], ""
        while k < len(body) and depth:
            ch = body[k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            if depth == 0:
                break
            if ch == "," and depth == 1:
                args.append(cur)
                cur = ""
            else:
                cur += ch
            k += 1
        args.append(cur)
        got = 0 if len(args) == 1 and not args[0].strip() else len(args)    # a blanked string is an argument too
        if got != want:
            return (f"{m.group(1)} takes {want} argument(s), this call has {got}: "
                    f"{body[m.start(): k + 1][:80]} [A2-21ah]")
    return ""


def void_values_as_statements(body: str, void_names: set[str]) -> tuple[str, list[str]]:
    """A2-21ah (b): UT_OBSERVE_*("id", call) or UT_EXPECT_*(call, ...) around a call of a function that returns void
    becomes the call as a statement of its own (the form is forgiven, ARCH-20). Returns the body and what changed."""
    if not void_names:
        return body, []
    names = "|".join(map(re.escape, sorted(void_names, key=len, reverse=True)))
    call = r"((?:[A-Za-z_][\w]*\s*(?:\.|->|::)\s*)*(?:" + names + r")\s*\((?:[^()]|\([^()]*\))*\))"
    patterns = [re.compile(r'\bUT_OBSERVE_[A-Z]+\(\s*"[^"]*"\s*,\s*\(?\s*' + call + r'\s*\)?\s*\)\s*;'),
                re.compile(r"\bUT_EXPECT_(?:TRUE|FALSE)\(\s*\(?\s*" + call + r"\s*\)?\s*\)\s*;"),
                re.compile(r"\bUT_EXPECT_[A-Z_]+\(\s*\(?\s*" + call + r"\s*\)?\s*(?:\?[^,;]*)?,[^;]*\)\s*;")]
    changed: list[str] = []
    for pattern in patterns:
        def fix(m: "re.Match[str]") -> str:
            changed.append(m.group(1))
            return m.group(1) + ";"
        body = pattern.sub(fix, body)
    return body, changed
