"""Agent2 - agent_unittest_generator: test suites, test cases, stubs and test-side fixes.

Agent2 turns Agent1's plan into file actions [A2-1, A2-25]. It never builds or runs anything
and never claims verification [A2-32, A2-33]; Agent3 does that.

How it generates, cheapest first:
1. Stubs are generated deterministically from their declarations [A2-21g], with a sequence of
   return values per call for scalar return types.
2. Deterministic characterization tests (no LLM): each testable free function is called with
   boundary values taken from its own decisions and macros; results are observed, not guessed.
3. Scenarios the brain planned and Agent1 checked are assembled directly into tests when every value
   is given [A2-21m].
4. The LLM writes the rest, checked against a schema and against the symbols that really exist
   [A2-20, A2-21]: tests that assert stated behaviour [A2-21n], tests for values the deterministic
   generator cannot build [A2-21o], and rewrites of failing tests from Agent3's diagnostic [A2-21p].
   Every such test gets a readable name and a purpose comment [A2-21q].
Values the evidence does not state are recorded with UT_OBSERVE_* first and turned into
characterization assertions by ``finalize`` after Agent3 observed them [A2-21f].

Tools (MCP, each a contract): generate, finalize.
Run as an MCP server:  python agent_unittest_generator.py serve [--config ut_config.json]
"""
from __future__ import annotations

import functools
import itertools
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

import ut_contracts as uc
from ut_contracts import ba

HARNESS_MACROS = {"UT_EXPECT_TRUE", "UT_EXPECT_FALSE", "UT_EXPECT_EQ_INT", "UT_EXPECT_NE_INT", "UT_EXPECT_EQ_DBL",
                  "UT_EXPECT_EQ_STR", "UT_EXPECT_NULL", "UT_EXPECT_NOT_NULL", "UT_OBSERVE_INT", "UT_OBSERVE_DBL",
                  "UT_OBSERVE_STR", "UT_OBSERVE_BOOL", "EXPECT_TRUE", "EXPECT_FALSE", "EXPECT_EQ", "EXPECT_NE",
                  "EXPECT_STREQ", "EXPECT_NEAR",
                  # ut_harness 1.2: exceptions, endless loops, target registers [A3-45e, A2-21y, A3-HW1]
                  "UT_EXPECT_THROW", "UT_EXPECT_NO_THROW", "UT_OBSERVE_THROW", "UT_EXPECT_NO_RETURN",
                  "UT_REG32", "UT_REG16", "UT_REG8"}
SAFE_CALLS = {"sizeof", "strlen", "strcmp", "strncmp", "memset", "memcpy", "memcmp", "strcpy", "strncpy",
              "snprintf", "abs", "labs", "fabs", "static_cast", "const_cast", "reinterpret_cast", "int", "long",
              "unsigned", "char", "double", "float", "bool", "short"}
FORBIDDEN = re.compile(r"#\s*include|\bmain\s*\(|\bsystem\s*\(|\bpopen\s*\(|\bexec\w*\s*\(|\bfopen\s*\(|\bremove\s*\(|"
                       r"\bunlink\s*\(|\bwhile\s*\(\s*(1|true)\s*\)|\bfor\s*\(\s*;\s*;\s*\)|\bUT_TEST\s*\(|__asm|\basm\b")
INTEGRAL = re.compile(r"^(const\s+)?(unsigned\s+|signed\s+)?(int|long|long long|short|char|(?:std::)?size_t|"
                      r"(?:std::)?u?int(?:_least|_fast)?\d+_t|(?:std::)?u?intptr_t|(?:std::)?ptrdiff_t|"
                      r"bool|_Bool|unsigned|long int|unsigned long|unsigned int)$")
FLOATING = re.compile(r"^(const\s+)?(float|double|long double)$")


class GeneratorConfig(BaseModel):
    mode: str = "deterministic_then_llm"        # deterministic_then_llm | llm | deterministic
    max_auto_tests_per_function: int = 24
    max_llm_tests_per_function: int = 8
    # Expected answer per test, in tokens: sets the answer limit and window [ARCH-11]
    tokens_per_test: int = 380
    rule_attempts: int = 2                       # brain attempts per rule failure [ARCH-21, ORC-7]
    max_brain_values: int = 12                   # parameters per request whose value the brain builds [A2-21ac]


class ValueRecipe(BaseModel):
    """A2-21ac: how a test builds a valid value the rules cannot build."""
    setup: list[str] = Field(description="C/C++ statements that create the value; declared variables start with "
                                         "ut_r; no assertion, no loop that waits", max_length=12)
    argument: str = Field(description="the expression passed for the parameter, using the variables of setup")


VALUE_TASK = (
    "A deterministic test generator must call the function below but cannot build a valid value for one of its "
    "parameters. Write the setup statements that create a valid, fully initialized value and the argument "
    "expression to pass. Use only the declared types, functions and macros shown (for example the SUT's own init "
    "function, or a local object a pointer can point to). Name every variable you declare ut_r<something>. "
    "Return JSON {{\"setup\": [statements], \"argument\": expression}}. The parameter is number {index} "
    "({name}, type {type}).")
VALUE_MARK = "/* value of {function}:{param} built by the brain [A2-21ac] */"

# ============================================================================ LLM output contract
class GeneratedTest(BaseModel):
    name: str = Field(description="test_<what>_<behaviour>, a C identifier")
    scenario_ids: list[str] = Field(description="the scenario ids this test covers")
    purpose: str = Field(default="", description="one line: what the test checks")
    evidence_ids: list[str] = Field(default_factory=list, description="EVIDENCE ids its expected values come from")
    body: str = Field(description="C/C++ statements of the test body only, no function header")
    evidence_class: str = Field(description="E1..E7 when an expected value comes from that evidence, else E8")


class GeneratedTests(BaseModel):
    tests: list[GeneratedTest]
    shared: str = Field(default="", description=(
        "only when the task asks for SHARED SET-UP: static helper functions and variables these tests use, every "
        "name starting with ut_; otherwise empty"))


# ============================================================================ building blocks [NC-1, BP-2]
class SetupBlock(BaseModel):
    """NC-1, BP-2 setup: how a test builds the object a member function is called on."""
    lines: list[str] = Field(description="C++ statements that declare the object and every argument of its "
                                         "constructor as a named variable before it (never a temporary); every "
                                         "variable you declare starts with ut_", max_length=16)
    call: str = Field(description="the object and the method to call, for example ut_obj.update")


class FakeClass(BaseModel):
    """NC-1, BP-2 stub: a fake of an injected interface the rules cannot fake."""
    code: str = Field(description="the complete C++ class definition, nothing else")


SETUP_TASK = (
    "Build the object for a unit test: the test calls the member function {method} of the class {cls}, and the rules "
    "cannot build the object it is called on. Write the statements that declare the object and, before it, every argument of its "
    "constructor as a named variable (never a temporary bound to a reference); use a fake class ut_fake_<Interface> "
    "where one is shown for an interface. Use only the declarations shown. Name every variable you declare ut_<name>. "
    "Return JSON {{\"lines\": [statements], \"call\": \"<object>.{method}\"}}.")
FAKE_TASK = (
    "Write a fake of the C++ interface {iface} for unit tests: the class {fake} derives publicly from {iface} and "
    "overrides every pure virtual method. For each method keep a call counter <method>_calls and return a member "
    "<method>_return of the result type (for a reference result: a member of the referenced type, returned by "
    "reference). Members are public and initialised with {{}}. Return JSON {{\"code\": \"class {fake} : public "
    "{iface} {{ ... }};\"}} - the class definition only, no #include, no other code.")
BLOCK_MARK = "/* building block {key} [NC-2] */"
SHARED_BLOCK = re.compile(r"/\* ut_shared (\S+) \[BP-2 suite_part\] \*/.*?/\* ut_shared end \*/", re.S)
SHARED_BEGIN = "/* ut_shared {function} [BP-2 suite_part] */"
SHARED_END = "/* ut_shared end */"


# ============================================================================ source helpers
def stub_names(spec: uc.StubSpec) -> dict[str, str]:
    base = f"ut_stub_{uc.stub_base(spec)}"
    return {"return": f"{base}_return", "calls": f"{base}_calls", "returns": f"{base}_returns",
            "returns_len": f"{base}_returns_len", "step": f"{base}_step"}


SOURCE_FILE = re.compile(r"\.(c|cc|cpp|cxx|c\+\+)$", re.I)


def _int_value(token: str, macros: dict[str, str], depth: int = 0) -> int | None:
    """An integer literal or an integer macro (followed through other macros), else None."""
    token = token.strip().strip("()").strip()
    m = re.fullmatch(r"(0[xX][0-9a-fA-F]+|\d+)[uUlL]*", token)
    if m:
        return int(m.group(1), 0)
    if depth < 4 and token in macros:
        return _int_value(re.sub(r"/\*.*?\*/|//.*", "", macros[token]), macros, depth + 1)
    return None


def _macro_files(layout: uc.LayoutProfile) -> list[Path]:
    """The SUT file, its headers and the headers they include (A2-21x, A2-21z, A2-21ab)."""
    root = Path(layout.repo_root)
    files = [root / layout.sut_file, *[root / h for h in layout.headers]]
    return list(dict.fromkeys([f for f in files if f.is_file()] + _sut_includes(layout)))


def _define_lines(path: Path) -> list[str]:
    """The #define lines of a file, continuation lines joined and comments removed."""
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return []
    return list(_define_lines_of(str(path), stamp))


@functools.lru_cache(maxsize=512)
def _define_lines_of(path: str, _stamp: int) -> tuple[str, ...]:
    text = re.sub(r"\\[ \t]*\r?\n", " ", Path(path).read_text(encoding="utf-8", errors="replace"))
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return tuple(re.sub(r"//.*$", "", line).strip() for line in text.splitlines() if re.match(r"\s*#\s*define\b", line))


def header_macros(layout: uc.LayoutProfile) -> dict[str, str]:
    """#define NAME value of the SUT file, its headers and the headers they include (continuation lines joined),
    and -DNAME=value of its compile flags, which come first as the compiler sees them."""
    macros: dict[str, str] = {}
    for flag in layout.compile_flags:
        m = re.match(r"-D([A-Za-z_]\w*)(?:=(.*))?$", flag)
        if m:
            macros.setdefault(m.group(1), m.group(2) if m.group(2) is not None else "1")
    for path in _macro_files(layout):
        for line in _define_lines(path):
            m = re.match(r"#\s*define[ \t]+([A-Za-z_]\w*)[ \t]+(.+)$", line)
            if m and m.group(2).strip():
                macros.setdefault(m.group(1), m.group(2).strip())
    return macros


def function_macros(layout: uc.LayoutProfile | None) -> dict[str, tuple[list[str], str]]:
    """Function-like macros of the SUT file and its headers: NAME -> (parameters, body)."""
    out: dict[str, tuple[list[str], str]] = {}
    if layout is None:
        return out
    for path in _macro_files(layout):
        for line in _define_lines(path):
            m = re.match(r"#\s*define[ \t]+([A-Za-z_]\w*)\(([^)]*)\)[ \t]*(.+)$", line)
            if m:
                out.setdefault(m.group(1), ([p.strip() for p in m.group(2).split(",")], m.group(3).strip()))
    return out


def expand_macros(text: str, macros: dict[str, tuple[list[str], str]]) -> str:
    """One level of function-like macro calls in an expression, as the preprocessor would expand them
    (READ_BIT(USART1->SR, TXE) -> ((USART1->SR) & (TXE)))."""
    if not macros:
        return text
    out, k = "", 0
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", text):
        if m.start() < k or m.group(1) not in macros:
            continue
        depth, j = 1, m.end()
        while j < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        args, depth, cur = [], 0, ""
        for c in text[m.end(): j - 1]:                     # an expression's commas: "->" is not a bracket
            depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(c, 0)
            if c == "," and depth == 0:
                args.append(cur.strip())
                cur = ""
            else:
                cur += c
        args.append(cur.strip())
        params, body = macros[m.group(1)]
        if len(args) != len([p for p in params if p]):
            continue
        for p, a in zip(params, args):
            body = re.sub(r"\b" + re.escape(p) + r"\b", a.replace("\\", "\\\\"), body)
        # a simple operand needs no parentheses of its own: ((REG) & (BIT)) reads as (REG & BIT)
        body = re.sub(r"(?<![\w)\]])\(\s*([A-Za-z_][\w:]*(?:\s*(?:->|\.)\s*\w+|\s*\[\s*\w+\s*\])*|"
                      r"0[xX][0-9a-fA-F]+[uUlL]*|\d+[uUlL]*)\s*\)", r"\1", body)
        out += text[k: m.start()] + "(" + body + ")"
        k = j
    return out + text[k:]


def clock_steps(stubs: list[uc.StubSpec], sut_text: str, macros: dict[str, str],
                layout: uc.LayoutProfile | None = None) -> dict[str, int]:
    """A2-21x: stubs the SUT uses like a clock - a call result subtracted from or by another value, as in
    hal_micros() - t0 - and the step each advances by: the largest constant the elapsed time is compared
    with on those lines, divided by 8 (at least 1)."""
    out = {}
    for spec in stubs:
        name = spec.function.split("::")[-1]
        storage = re.sub(r"^const\s+", "", re.sub(r"\b(extern|static|inline|virtual)\b", "", spec.return_type).strip())
        if layout is not None:
            storage = resolve_typedefs(storage, layout) or storage                                 # A1-DA21
        if spec.kind not in ("function", "method") or not INTEGRAL.match(storage):
            continue
        call = rf"\b{re.escape(name)}\s*\([^()]*\)"
        lines = [line for line in sut_text.splitlines()
                 if re.search(call + r"\s*-(?!-|>|=)", line) or re.search(r"(?<![-+])-\s*" + call, line)]
        if not lines:
            continue
        limits = [_int_value(m.group(1), macros) for line in lines
                  for m in re.finditer(r"(?:[<>]=?)\s*\(?\s*([A-Za-z_]\w*|0[xX][0-9a-fA-F]+|\d+)[uUlL]*", line)]
        limits = [v for v in limits if v and v > 0]
        out[spec.function] = max(1, max(limits) // 8) if limits else 1
    return out


def sequence_type(storage: str, layout: uc.LayoutProfile) -> bool:
    """A stub of this return type can return one value per call [A2-21g]: scalars, enums, pointers."""
    s = re.sub(r"\b(enum|const|volatile)\b", "", storage).strip()
    s = resolve_typedefs(s, layout) or s                                                      # A1-DA21
    return bool(INTEGRAL.match(s) or FLOATING.match(s) or s.endswith("*") or
                any(e.name.split("::")[-1] == s.split("::")[-1] for e in layout.enums))


def _value_type(ptype: str) -> str | None:
    """The type of a variable that records an argument: top-level const and volatile dropped, so it can be
    assigned (char const * const -> char const *, const int -> int) [A2-21ad]."""
    t = ptype.replace("&&", "").strip()
    if t.endswith("&"):
        t = t[:-1].strip()
    if "[" in t or t in ("void", "...") or "(*)" in t:
        return None
    if "*" in t:
        head, tail = t.rsplit("*", 1)
        tail = re.sub(r"\b(const|volatile|restrict|__restrict|__restrict__)\b", "", tail).strip()
        return f"{head.rstrip()} *" + (f" {tail}" if tail else "")
    return re.sub(r"\s+", " ", re.sub(r"\b(const|volatile)\b", " ", t)).strip() or None


def _declarator(ptype: str, name: str) -> str:
    """'int (*)(int)' + 'cb' -> 'int (*cb)(int)'; 'int[4]' + 'a' -> 'int a[4]'; else 'type name'."""
    if "(*)" in ptype:
        return ptype.replace("(*)", f"(*{name})", 1)
    m = re.match(r"^(.*?)\s*(\[.*\])$", ptype)
    if m:
        return f"{m.group(1)} {name}{m.group(2)}"
    return f"{ptype} {name}"


def include_name(header: str, layout: uc.LayoutProfile) -> str:
    """How the stub file includes a header: relative to the longest include path that contains it."""
    header = header.replace("\\", "/")
    dirs = [uc.repo_relative(f[2:]) for f in layout.compile_flags if f.startswith("-I" + uc.REPO_TOKEN)]
    dirs += [f[2:] for f in layout.compile_flags if f.startswith("-I") and not f.startswith("-I{")]
    dirs += layout.include_dirs
    best = ""
    for d in dirs:
        d = d.strip("/")
        if d in ("", ".", uc.REPO_TOKEN):
            continue
        if header.startswith(d + "/") and len(d) > len(best):
            best = d
    return header[len(best) + 1:] if best else (header if layout.compile_flags else Path(header).name)


def render_stubs(plan: uc.UTPlan) -> tuple[str, str, str]:
    """(stub source path, stub source, stub header) for the plan's stubs [A2-21g, A2-21j]: functions,
    C++ methods and constructors with a controllable return value, captured arguments and a call
    counter; global variables defined under their own name and exact type."""
    layout = plan.layout
    cpp = layout.language == "cpp"
    ext = "cpp" if cpp else "c"
    stub_dir = layout.writable_stub_dir.rstrip("/")
    # A2-21w: only header files; a declaration placed in a source file is never reached by #include
    headers = sorted({include_name(s.header, layout) for s in plan.stubs if s.header and not SOURCE_FILE.search(s.header)})
    root = Path(layout.repo_root)
    sut_path = root / layout.sut_file
    sut_text = sut_path.read_text(encoding="utf-8", errors="replace") if sut_path.is_file() else ""
    steps = clock_steps(plan.stubs, sut_text, header_macros(layout), layout)                  # A2-21x
    decl_lines, def_lines, reset = [], [], []

    def clear(var: str, vtype: str) -> None:
        if cpp:
            reset.append(f"    {var} = {{}};" if not re.search(r"\[|\(\*\)", vtype) else
                         f"    memset((void *)&{var}, 0, sizeof {var});")
        else:
            reset.append(f"    memset((void *)&{var}, 0, sizeof {var});")

    rendered: set[tuple] = set()
    for spec in plan.stubs:
        target = spec.qualified_name or spec.function
        # one definition per symbol: a second entry of the same function (a declaration proposed again for a symbol
        # the plan already stubs) would define it twice and its controls twice [A1-PL14, A2-21g]
        key = (spec.kind, uc.stub_base(spec), tuple(re.sub(r"\s+", "", p[0]) for p in spec.params))
        if key in rendered or (spec.kind != "variable" and (spec.kind, uc.stub_base(spec)) in {k[:2] for k in rendered}):
            continue
        rendered.add(key)
        # A2-21ad: a C++ symbol of a namespace is stubbed inside that namespace, so the types of its declaration
        # resolve as they do there; its control variables are made visible outside with using-declarations
        ns = spec.namespace if cpp and spec.namespace and target.startswith(spec.namespace + "::") else ""
        d0, f0 = len(decl_lines), len(def_lines)
        if ns:
            target = target[len(ns) + 2:]
        if spec.kind == "variable":
            definition = spec.var_type or f"int {target}"
            if spec.is_const:
                def_lines.append(f"/* const: its value is fixed for the whole test program [A2-21j] */\n"
                                 f"{definition} = {spec.initializer or '{0}'};")
            else:
                def_lines.append(f"{definition};")
                full = spec.qualified_name or spec.function
                if not re.search(r"\b(class|std::)", definition):
                    reset.append(f"    memset((void *)&{full}, 0, sizeof {full});")
            if ns:                                  # defined inside its namespace, as it is declared [A2-21ad]
                def_lines[f0:] = [f"namespace {ns} {{", *def_lines[f0:], "}"]
            continue
        names = stub_names(spec)
        ret = re.sub(r"\b(extern|static|inline|virtual|explicit)\b", "", spec.return_type).strip()
        storage = re.sub(r"^const\s+", "", ret.rstrip("&").strip())
        seq = spec.kind != "constructor" and bool(ret) and ret != "void" and sequence_type(storage, layout)
        if spec.kind != "constructor" and ret and ret != "void":
            decl_lines.append(f"extern {storage} {names['return']};")
            def_lines.append(f"{storage} {names['return']};")
            clear(names["return"], storage)
        if seq:                                     # one value per call, then the single value [A2-21g]
            decl_lines += [f"extern {storage} {names['returns']}[UT_STUB_SEQUENCE];", f"extern int {names['returns_len']};"]
            def_lines += [f"{storage} {names['returns']}[UT_STUB_SEQUENCE];", f"int {names['returns_len']};"]
            reset += [f"    memset((void *){names['returns']}, 0, sizeof {names['returns']});",
                      f"    {names['returns_len']} = 0;"]
        decl_lines.append(f"extern int {names['calls']};")
        def_lines.append(f"int {names['calls']};")
        reset.append(f"    {names['calls']} = 0;")
        step = steps.get(spec.function) if spec.kind != "constructor" and ret and ret != "void" else None
        if step is not None:                        # a clock advances by default [A2-21x]
            decl_lines.append(f"extern {storage} {names['step']};   /* clock: advance per call (0 holds it) */")
            def_lines.append(f"{storage} {names['step']};")
            reset.append(f"    {names['step']} = {step};")
        params, stores = [], []
        for index, (ptype, pname) in enumerate(spec.params):
            if ptype.strip() == "...":                         # variadic: declared as it is, not captured [A2-21g]
                params.append("...")
                continue
            pname = pname or f"arg{index}"
            params.append(_declarator(ptype, pname))
            vt = _value_type(ptype)
            if vt:
                var = f"ut_stub_{uc.stub_base(spec)}_{pname}"
                decl_lines.append(f"extern {vt} {var};")
                def_lines.append(f"{vt} {var};")
                stores.append(f"    {var} = {pname};")
                clear(var, vt)
            else:
                stores.append(f"    (void){pname};")
            if uc.stub_output(ptype):         # structured: what the stub writes into an output parameter
                out = f"ut_stub_{uc.stub_base(spec)}_{pname}_out"
                # the type it points to, named where the stub is declared: a test names it through this alias, so
                # a type of the stub's namespace resolves as in the declaration (drv::can::Frame written Frame)
                decl_lines += [f"typedef {uc.stub_pointee(ptype)} {out}_t;",
                               f"extern unsigned char {out}[UT_STUB_OUT];", f"extern int {out}_len;"]
                def_lines += [f"unsigned char {out}[UT_STUB_OUT];", f"int {out}_len;"]
                reset.append(f"    {out}_len = 0;")
                address = f"&{pname}" if ptype.strip().endswith("&") else pname
                # never more than the object the caller passed: its size parameter, or the referenced object
                size = next((n for k, (pt, n) in enumerate(spec.params) if k > index and n and
                             re.fullmatch(r"(?i)(size|len|length|count|n|bytes|max\w*|\w*_(?:size|len))", n) and
                             _value_type(pt) and "*" not in pt), "")
                elem = uc.stub_pointee(ptype)
                limit = (f"(size_t){out}_len" if not size else
                         f"((size_t){out}_len < (size_t){size} * {1 if elem in ('unsigned char', 'char', 'uint8_t') else f'sizeof({elem})'} "
                         f"? (size_t){out}_len : (size_t){size} * {1 if elem in ('unsigned char', 'char', 'uint8_t') else f'sizeof({elem})'})")
                if ptype.strip().endswith("&"):
                    limit = f"((size_t){out}_len < sizeof({pname}) ? (size_t){out}_len : sizeof({pname}))"
                nonnull = "" if ptype.strip().endswith("&") else f" && {pname} != 0"
                stores.append(f"    if ({out}_len > 0 && {out}_len <= UT_STUB_OUT{nonnull}) "
                              f"memcpy((void *){address}, {out}, {limit});")
        arglist = ", ".join(params) if params else ("void" if not cpp else "")
        if spec.kind == "constructor":
            signature = f"{target}({arglist})"
        else:
            signature = f"{ret} {target}({arglist}){spec.method_suffix}"
        # the counter stops at INT_MAX instead of overflowing; every call counts against the test's budget
        body = [f"    if ({names['calls']} < INT_MAX) ++{names['calls']};", "    ut_stub_call();", *stores]   # A2-21g, A3-45b
        if seq:
            body.append(f"    if ({names['calls']} >= 1 && {names['calls']} <= {names['returns_len']} && "
                        f"{names['returns_len']} <= UT_STUB_SEQUENCE) return {names['returns']}[{names['calls']} - 1];")
        if step is not None:
            body.append(f"    return ({storage})({names['return']} + ({storage}){names['step']} * ({storage})({names['calls']} - 1));")
        elif spec.kind != "constructor" and ret and ret != "void":
            body.append(f"    return {names['return']};")
        def_lines.append(signature + "\n{\n" + "\n".join(body) + "\n}\n")
        if ns:
            names_here = [m.group(1) for line in decl_lines[d0:]
                          for m in [re.search(r"\b(ut_stub_\w+)\s*(?:\[[^\]]*\])?\s*;", line)] if m]
            decl_lines[d0:] = [f"namespace {ns} {{", *decl_lines[d0:], "}", *[f"using {ns}::{n};" for n in names_here]]
            def_lines[f0:] = [f"namespace {ns} {{", *def_lines[f0:], "}"]
    if uc.reset_state(layout):               # A2-21s: the SUT's state as before the first test
        decl_lines.append(('extern "C" ' if cpp else "") + "void ut_sut_reset(void);")
        reset.insert(0, "    ut_sut_reset();")
    project_headers = []                     # A2-21ak: the controls of the reused project stubs
    for ps in (layout.dependencies.project_stubs if layout.dependencies else []):
        if ps.header:
            project_headers.append(Path(ps.header).name)          # its folder is an include path of the build
        else:
            decl_lines += [(c.declaration if c.kind == "variable" else ('extern "C" ' if cpp and ps.file.endswith(".c")
                            else "") + c.declaration) + ";" for c in ps.controls]
        if ps.reset:
            reset.append(f"    {ps.reset}();   /* the project stub as before the first test [A2-21ak] */")
    guard = f"UT_STUBS_{uc.unit_name(layout).upper()}_H"                                  # A2-21af
    fake_headers, fakes = render_fakes(layout)                                                 # A2-21aa
    if fakes:
        headers = sorted(set(headers) | set(fake_headers))
    # A2-21ad: the SUT's own prologue first, so headers come in the order (and with the defines) the SUT
    # compiles with; then the declaring headers the prologue does not reach
    prologue = uc.sut_prologue(sut_text, layout.functions, layout.header_sut)
    if prologue:
        reached = {include_name(h, layout) for h in (layout.dependencies.headers if layout.dependencies else [])}
        headers = [h for h in headers if h not in reached]
    stub_path, stub_h = uc.stub_names(layout)                                              # A2-21af
    header = [f"/* {stub_h} - generated by agent_unittest_generator. Control variables of the stubs. */",
              f"#ifndef {guard}", f"#define {guard}", *prologue, *[f'#include "{h}"' for h in headers],
              *[f'#include "{h}"   /* controls of the project stub [A2-21ak] */' for h in project_headers],
              *(["#include <limits.h>", '#include "ut_harness.h"'] if fakes else []),
              "#define UT_STUB_SEQUENCE 16", "#define UT_STUB_OUT 256", *decl_lines, *fakes,
              f"#endif /* {guard} */", ""]
    source = [f"/* {Path(stub_path).name} - generated by agent_unittest_generator from the declarations [A2-21g]. */",
              "#include <string.h>", "#include <limits.h>", '#include "ut_harness.h"', f'#include "{stub_h}"', "",
              *def_lines,
              ('extern "C" ' if cpp else "") + "void ut_before_each(void)", "{", *reset, "}", ""]
    return stub_path, "\n".join(source), "\n".join(header)


# ============================================================================ embedded targets
def fake_name(iface: uc.InterfaceInfo) -> str:
    return "ut_fake_" + _ident(iface.name.split("::")[-1])


def _fake_bases(iface: uc.InterfaceInfo) -> list[str]:
    """Control names of a fake's methods; overloads get the index of the method as a suffix."""
    names = [m.name for m in iface.methods]
    return [_ident(n) if names.count(n) == 1 else f"{_ident(n)}_{k}" for k, n in enumerate(names)]


def fake_supported(iface: uc.InterfaceInfo, layout: uc.LayoutProfile) -> bool:
    """A fake can return a value of every method: a reference result refers to the fake's own member, so only a
    reference to an interface (an abstract class the fake cannot hold) is left out."""
    abstract = {i.name.split("::")[-1] for i in layout.interfaces}
    return bool(iface.methods) and not any("&" in m.return_type and re.sub(
        r"\b(const|volatile|virtual)\b|&", "", m.return_type).strip().split("::")[-1] in abstract for m in iface.methods)


def render_fakes(layout: uc.LayoutProfile) -> tuple[list[str], list[str]]:
    """A2-21aa: (headers, class definitions) of a fake class per injected interface. Every pure virtual
    method is overridden with the controls of a stub: <method>_return, <method>_returns[] and
    <method>_returns_len, <method>_calls and the captured arguments <method>_<parameter>."""
    if layout.language != "cpp":
        return [], []
    headers, rows = [], []
    for iface in layout.interfaces:
        if not fake_supported(iface, layout):
            continue
        if iface.header and not SOURCE_FILE.search(iface.header):
            headers.append(include_name(iface.header, layout))
        rows += [f"/* fake of the injected interface {iface.name} [A2-21aa] */",
                 f"class {fake_name(iface)} : public {iface.name} {{", "public:"]
        for m, base in zip(iface.methods, _fake_bases(iface)):
            ret = re.sub(r"\b(virtual|inline)\b", "", m.return_type).strip() or "void"
            storage = re.sub(r"^const\s+", "", ret.replace("&", "")).strip()   # a reference: to the member
            seq = ret != "void" and sequence_type(storage, layout)
            if ret != "void":
                rows.append(f"    mutable {storage} {base}_return{{}};")
            if seq:
                rows += [f"    mutable {storage} {base}_returns[UT_STUB_SEQUENCE]{{}};",
                         f"    mutable int {base}_returns_len = 0;"]
            rows.append(f"    mutable int {base}_calls = 0;")
            params, stores = [], []
            for k, (ptype, pname) in enumerate(m.params):
                if ptype.strip() == "...":
                    params.append("...")
                    continue
                pname = pname or f"arg{k}"
                params.append(_declarator(ptype, pname))
                vt = _value_type(ptype)
                if vt and not re.search(r"\b(class|struct)\b", vt):
                    rows.append(f"    mutable {vt} {base}_{pname}{{}};")
                    stores.append(f"{base}_{pname} = {pname};")
                else:
                    stores.append(f"(void){pname};")
            suffix = re.sub(r"\b(override|final)\b|=\s*0", "", m.suffix).rstrip()
            body = [f"if ({base}_calls < INT_MAX) ++{base}_calls;", "ut_stub_call();", *stores]
            if seq:
                body.append(f"if ({base}_calls <= {base}_returns_len && {base}_returns_len <= UT_STUB_SEQUENCE) "
                            f"return {base}_returns[{base}_calls - 1];")
            if ret != "void":
                body.append(f"return {base}_return;")
            rows.append(f"    {ret} {m.name}({', '.join(params)}){suffix} override {{ {' '.join(body)} }}")
        rows += ["};", ""]
    return headers, rows


def _fake_options(p: uc.ParamInfo, i: int, layout: uc.LayoutProfile, values: list[int], tag: str = ""):
    """Choices for an interface parameter: a fake object with the return values of its methods [A2-21aa]."""
    iface = next((f for f in layout.interfaces if f.name == p.base or f.name.split("::")[-1] == p.base.split("::")[-1]), None)
    if iface is None or not fake_supported(iface, layout):
        return None
    var = f"ut_{tag}f{i}"
    small = sorted(set(values), key=lambda v: (abs(v), v))[:6]
    dims, bases = [], []
    for m, base in zip(iface.methods, _fake_bases(iface)):
        r = re.sub(r"\b(const|volatile|virtual)\b", "", m.return_type).strip()
        if r in ("bool", "_Bool"):
            dims.append([0, 1]); bases.append(base)
        elif INTEGRAL.match(r) or any(r.split("::")[-1] == e.name.split("::")[-1] for e in layout.enums):
            dims.append(small if INTEGRAL.match(r) else [0]); bases.append(base)
        elif FLOATING.match(r):
            dims.append([float(v) for v in small]); bases.append(base)
    combos = combine(dims, max(6, max((len(d) for d in dims), default=1))) if dims else [()]
    arg = var if p.by_ref else f"&{var}"
    out = []
    for combo in combos:
        setup = [f"{fake_name(iface)} {var};"]
        for base, v in zip(bases, combo):
            r = next(m.return_type for m, b in zip(iface.methods, _fake_bases(iface)) if b == base)
            value = ("true" if v else "false") if re.sub(r"\bconst\b", "", r).strip() == "bool" else _c_literal(v)
            if not INTEGRAL.match(re.sub(r"\b(const|volatile)\b", "", r).strip()) and not FLOATING.match(
                    re.sub(r"\b(const|volatile)\b", "", r).strip()) and value == "0":
                plain = re.sub(r"^const\s+", "", r).strip()
                value = f"static_cast<{plain}>(0)"
            setup.append(f"{var}.{base}_return = {value};")
        out.append((setup, arg, []))
    return out


TARGET_WIDTH = {"long", "unsigned long", "long int", "unsigned long int", "signed long", "long unsigned int"}


def width_values(p: uc.ParamInfo) -> list[int]:
    """A3-HW4: a parameter whose type has 32 bits on an ILP32 target and 64 on the host also gets the
    boundaries of the target width."""
    t = re.sub(r"\b(const|volatile)\b", "", p.canonical or p.type).strip()
    if t not in TARGET_WIDTH:
        return []
    return [0x7FFFFFFF, 0x80000000, 0xFFFFFFFF] if "unsigned" in t else [0x7FFFFFFF, -0x80000000]


def _conditions(body: str) -> list[str]:
    """The conditions of if, while, for, switch and ?: in a function body."""
    out = []
    for m in re.finditer(r"\b(if|while|switch)\s*\(", body):
        depth, k = 1, m.end()
        while k < len(body) and depth:
            depth += {"(": 1, ")": -1}.get(body[k], 0)
            k += 1
        out.append(body[m.end(): k - 1])
    out += re.findall(r"([^;{}?]+)\?[^:;]+:", body)
    return out


ENDLESS = re.compile(r"\bfor\s*\(\s*;\s*;\s*\)|\bwhile\s*\(\s*(?:1[uUlL]*|true|TRUE)\s*\)")


def endless_loop(body: str) -> bool:
    """A2-21y: an endless loop (for (;;), while (1), while (true)) that nothing in its body leaves."""
    return uc.endless_loop_end(body) is not None


def _locals(body: str) -> set[str]:
    return set(re.findall(r"\b[A-Za-z_][\w:]*[\s*&]+([A-Za-z_]\w*)\s*(?:=|;|\[)", body))


REG_TEST = re.compile(r"\(\s*([A-Za-z_][\w:]*(?:\s*\(\s*\))?(?:\s*(?:->|\.)\s*\w+|\s*\[\s*\w+\s*\])*)\s*&\s*"
                      r"(\(\s*[A-Za-z_][\w:]*\s*\)|[A-Za-z_][\w:]*|0[xX][0-9a-fA-F]+[uUlL]*|\d+[uUlL]*|"
                      r"\(\s*1[uUlL]*\s*<<\s*\w+\s*\))\s*\)")


def register_polls(fn: uc.FunctionInfo, body: str, layout: uc.LayoutProfile | None = None) -> list[tuple[str, str]]:
    """A2-21ab: bits of a target register the function tests in a decision, as (register, mask): a
    register is a macro, a constant address dereferenced or a member reached through a pointer or a
    function call, never a parameter or a local variable of the function."""
    own = {p.name for p in fn.params} | _locals(body)
    out = []
    macros = function_macros(layout)
    for cond in _conditions(body):
        cond = expand_macros(cond, macros)
        for m in REG_TEST.finditer("(" + cond + ")"):
            reg, mask = re.sub(r"\s+", "", m.group(1)), m.group(2).strip()
            first = re.match(r"[A-Za-z_][\w:]*", reg).group(0)
            if first.split("::")[-1] in own or first in own:
                continue
            if not ("->" in reg or "()" in reg or re.fullmatch(r"[A-Z][A-Z0-9_]*", first)):
                continue
            if re.fullmatch(r"[A-Za-z_][\w:]*\(.*\)", reg):   # the value a call returns (__get_PRIMASK()): none to set
                continue
            if (reg, mask) not in out:
                out.append((reg, mask))
    return out


def wait_polarity(cond: str, mask: str) -> bool | None:
    """A2-21ab: True when a loop with this condition waits while the masked bit is clear, False while it is set,
    None when the condition does not decide it: the condition is evaluated with the bit set and with it clear
    (0u == (SR & TXE), !(SR & RDY), (SR & RDY) != RDY, SR & BUSY ...)."""
    expr = cond
    hits = 0
    for m in list(REG_TEST.finditer("(" + cond + ")"))[::-1]:
        if re.sub(r"\s+", "", m.group(2)) != re.sub(r"\s+", "", mask):
            continue
        start, end = m.start() - 1, m.end() - 1            # positions in cond (the added "(" shifts them)
        expr = expr[:max(start, 0)] + " __X__ " + expr[end:]
        hits += 1
    if not hits:
        return None
    value = 1
    literal = re.fullmatch(r"\(?\s*(0[xX][0-9a-fA-F]+|\d+)[uUlL]*\s*\)?", mask.strip())
    if literal:
        value = int(literal.group(1), 0) or 1
    expr = expr.replace(mask.strip(), " __M__ ")
    expr = re.sub(r"\b(0[xX][0-9a-fA-F]+|\d+)[uUlL]+\b", r"\1", expr)
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"!(?!=)", " not ", expr)
    words = set(re.findall(r"[A-Za-z_]\w*", expr)) - {"__X__", "__M__", "and", "or", "not"}
    if words or not re.fullmatch(r"[\s\w()<>=!+\-*/%&|^~]+", expr):
        return None
    try:
        when_set = bool(eval(expr, {"__builtins__": {}}, {"__X__": value, "__M__": value}))   # noqa: S307
        when_clear = bool(eval(expr, {"__builtins__": {}}, {"__X__": 0, "__M__": value}))     # noqa: S307
    except Exception:                                                                        # noqa: BLE001
        return None
    if when_clear and not when_set:
        return True
    if when_set and not when_clear:
        return False
    return None


def register_waits(fn: uc.FunctionInfo, body: str, layout: uc.LayoutProfile | None = None
                   ) -> list[tuple[str, str, bool]]:
    """A2-21ab: register bits a loop waits for without any other way out (while ((REG & MASK) == 0) {} with no
    parameter, local variable, break, return or goto), as (register, mask, waits_while_clear). The test lets
    the "hardware" change the bit after a while (UT_REG_SET_LATER, UT_REG_CLEAR_LATER), so the call returns
    and its coverage is real."""
    own = {p.name for p in fn.params} | _locals(body)
    out = []
    for m in re.finditer(r"\bwhile\s*\(", body):
        depth, k = 1, m.end()
        while k < len(body) and depth:
            depth += {"(": 1, ")": -1}.get(body[k], 0)
            k += 1
        cond = expand_macros(body[m.end(): k - 1], function_macros(layout))
        rest = body[k:].lstrip()
        if rest.startswith("{"):
            depth, j = 0, 0
            for j, c in enumerate(rest):
                depth += {"{": 1, "}": -1}.get(c, 0)
                if depth == 0:
                    break
            loop = rest[: j + 1]
        else:
            loop = rest.split(";", 1)[0]
        if re.search(r"\b(break|return|goto|longjmp|exit)\b", loop):
            continue
        idents = set(re.findall(r"(?<![\w.>:])([A-Za-z_]\w*)", re.sub(r"/\*.*?\*/", "", cond)))
        if idents & own:
            continue
        polls = [p for p in register_polls(fn, "if (" + cond + ")") if p[0].split("->")[0] not in own]
        for reg, mask in polls:
            clear = wait_polarity(cond, mask)              # decided by the condition itself [A2-21ab]
            if clear is not None and (reg, mask, clear) not in out:
                out.append((reg, mask, clear))
    return out


def wait_setup(waits: list[tuple[str, str, bool]], preset: set[tuple[str, str]], cls: bool = False) -> list[str]:
    """A2-21ab: the register change a waiting function needs to return: the bit it waits for set (or cleared)
    by the "hardware" 20 ms after the call starts."""
    rows = []
    for reg, mask, clear in waits:
        if clear and (reg, mask) not in preset:
            rows.append(f"UT_REG_SET_LATER({reg}, {mask}, 20);   /* the hardware sets the bit after 20 ms [A2-21ab] */")
        elif not clear and (reg, mask) in preset:
            rows.append(f"UT_REG_CLEAR_LATER({reg}, {mask}, 20);   /* the hardware clears the bit after 20 ms [A2-21ab] */")
    return rows


def _sut_includes(layout: uc.LayoutProfile) -> list[Path]:
    """Headers the SUT includes with quotes, found in its directory or the include directories, and the
    headers those include (two levels)."""
    root = Path(layout.repo_root)
    dirs = [root / Path(layout.sut_file).parent] + [root / d for d in layout.include_dirs] + \
        [Path(uc.expand_flags([f], root)[0][2:]) for f in layout.compile_flags if f.startswith("-I")]
    found: list[Path] = []
    todo = [(root / layout.sut_file, 0)]
    while todo:
        path, depth = todo.pop(0)
        if not path.is_file() or depth > 2:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for quote, inc in re.findall(r'#\s*include\s*([<"])([^>"]+)[>"]', text):
            if quote == "<" and ("." not in inc or inc in uc.STANDARD_HEADERS):
                continue                                   # a system header; <drivers/uart.h> of the project counts
            for d in ([path.parent] if quote == '"' else []) + dirs:
                c = d / inc
                if c.is_file():
                    if c not in found:
                        found.append(c)
                        todo.append((c, depth + 1))
                    break
    return found


ASM_OUT = re.compile(r"\b(?:__asm__|__asm|asm|__ASM)\b\s*(?:(?:volatile|__volatile__|__volatile|inline)\s*)*\("
                     r"[^;]*?:\s*\"[=+]")


@functools.lru_cache(maxsize=64)
def _asm_functions(paths: tuple[str, ...]) -> frozenset[str]:
    """Functions and macros whose body has target assembly with an output operand (CMSIS __get_PRIMASK())."""
    names = set()
    for name in paths:
        try:
            text = Path(name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in ASM_OUT.finditer(text):
            before = text[: m.start()]
            line_start = before.rfind("\n") + 1
            d = re.match(r"\s*#\s*define\s+(\w+)", text[line_start: m.start()])
            if d:
                names.add(d.group(1))
                continue
            f = list(re.finditer(r"\b(\w+)\s*\([^;{}()]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{", before))
            if f:
                names.add(f[-1].group(1))
    return frozenset(names)


def uses_target_asm(fn: uc.FunctionInfo, body: str, layout: uc.LayoutProfile) -> bool:
    """A3-HW3: the function's result depends on target assembly that the host build replaces; its tests vary
    the replaced result (ut_asm_value)."""
    if ASM_OUT.search(body):
        return True
    paths = tuple(str(p) for p in _sut_includes(layout))
    return any(re.search(r"\b" + re.escape(n) + r"\s*\(", body) for n in _asm_functions(paths))


def register_headers(plan: uc.UTPlan) -> list[str]:
    """A2-21ab: the headers that define the registers and masks the tests set, as the suite includes them."""
    layout = plan.layout
    root = Path(layout.repo_root)
    sut = root / layout.sut_file
    if not sut.is_file():
        return []
    lines = sut.read_text(encoding="utf-8", errors="replace").splitlines()
    names = set()
    for fn in layout.functions:
        for reg, mask in register_polls(fn, "\n".join(lines[fn.line_start - 1: fn.line_end]), plan.layout):
            names.add(re.match(r"[A-Za-z_][\w:]*", reg).group(0).split("::")[-1])
    if not names:
        return []
    out = []
    for h in _sut_includes(layout):
        text = h.read_text(encoding="utf-8", errors="replace")
        if any(re.search(r"(#\s*define\s+" + re.escape(n) + r"\b|\b" + re.escape(n) + r"\s*\()", text) for n in names):
            rel = os.path.relpath(h, root).replace("\\", "/")
            if not rel.startswith(".."):
                out.append(include_name(rel, layout))
    return out


def decision_state(fn: uc.FunctionInfo, body: str, layout: uc.LayoutProfile, macros: dict[str, str]) -> list[tuple[uc.StateVar, list[int]]]:
    """A2-21z: scalar SUT state the function reads in a decision, with 0, 1 and the constants it is compared with."""
    out = []
    conds = _conditions(body)
    settable = {v.name for v in uc.settable_state(layout)}             # only variables that get a setter [A2-21z]
    for var in layout.sut_state:
        if var.name not in settable:
            continue
        short = var.name.split("::")[-1]
        t = re.sub(r"\b(volatile|const|static)\b", "", var.type).strip()
        if "[" in t or "*" in t or not (INTEGRAL.match(t) or re.fullmatch(r"[A-Za-z_][\w:]*_t", t) or
                                        any(t.split("::")[-1] == e.name.split("::")[-1] for e in layout.enums)):
            continue
        used = [c for c in conds if re.search(r"(?<![\w.>])" + re.escape(short) + r"\b", c)]
        if not used:
            continue
        values = {0, 1}
        for c in used:
            for m in re.finditer(r"\b" + re.escape(short) + r"\s*(?:==|!=|<=|>=|<|>)\s*\(?\s*([A-Za-z_]\w*|-?(?:0[xX][0-9a-fA-F]+|\d+))", c):
                v = _int_value(m.group(1), macros)
                if v is not None:
                    values |= {v - 1, v, v + 1}
        out.append((var, sorted(values)))
    return out


def state_setter(var: uc.StateVar) -> str:
    return uc.state_setter_name(var)


def setter_vars(plan: uc.UTPlan) -> list[uc.StateVar]:
    """A2-21z: every scalar or enumeration SUT state variable gets a setter in the sut_access file - the list Agent1
    offers the brain, so a setter the brain was told of always exists."""
    layout = plan.layout
    if not layout.sut_state or not (Path(layout.repo_root) / layout.sut_file).is_file():
        return []
    return uc.settable_state(layout)


def setter_source(var: uc.StateVar, cpp: bool = False) -> str:
    """A2-21z: in C++ the variable's own type (decltype): a type its namespace declares need not be visible here."""
    t = f"decltype({var.name})" if cpp else re.sub(r"\s+", " ", re.sub(r"\b(volatile|const|static)\b", "", var.type)).strip()
    return f"void {state_setter(var)}(long long v) {{ {var.name} = ({t})v; }}"


def state_part(fn: uc.FunctionInfo, layout: uc.LayoutProfile, lines: list[str], limit: int = 40) -> str:
    """A2-21aj: the setters and getters of the SUT state this function and the functions it calls use."""
    listed, rest = uc.function_state(fn, layout, lines, limit)
    if not listed:
        return ""
    rows = [f"void {uc.state_setter_name(v)}(long long v);  long long {uc.state_getter_name(v)}(void);  "
            f"/* {v.type} {v.name.replace('(anonymous namespace)::', '')} */" for v in listed]
    if rest:
        rows.append("/* also used: " + ", ".join(v.name.replace("(anonymous namespace)::", "") for v in rest[:40])
                    + " - the same two helpers exist for each */")
    return ("SUT STATE (variables only the SUT file sees: set one before the call with its setter, read it after the "
            "call with its getter; never name the variable itself) [A2-21z, A2-21aj]:\n" + "\n".join(rows))


def element_setter_source(entry: tuple[str, uc.StateVar, bool, str, str], cpp: bool = False) -> str:
    """Structured scenario: the setter of one element or field of SUT state - an array, a structure or an array of
    structures - with the index checked, so that a wrong index sets nothing instead of writing past the object."""
    name, var, indexed, path, kind = entry
    target = f"{var.name}{'[i]' if indexed else ''}{'.' + path if path else ''}"
    vtype = "double" if kind == "float" else "long long"
    cast = f"static_cast<std::remove_reference<decltype({target})>::type>(v)" if cpp else f"(__typeof__({target}))v"
    guard = f"if (i < 0 || i >= {var.array_len}) return; " if indexed else ""
    params = (f"long long i, {vtype} v" if indexed else f"{vtype} v")
    return f"void {name}({params}) {{ {guard}{target} = {cast}; }}"


def element_setter_prototype(entry: tuple[str, uc.StateVar, bool, str, str]) -> str:
    name, _, indexed, _, kind = entry
    vtype = "double" if kind == "float" else "long long"
    return f"void {name}({'long long i, ' if indexed else ''}{vtype} v);"


def getter_source(var: uc.StateVar, cpp: bool = False) -> str:
    """A2-21z: the variable's value for a test that observes or asserts it."""
    return f"long long {uc.state_getter_name(var)}({'' if cpp else 'void'}) {{ return (long long)({var.name}); }}"


def repeat_count(fn: uc.FunctionInfo, body: str, layout: uc.LayoutProfile, macros: dict[str, str]) -> int:
    """A2-21z: how often a stateful function is called in one test (1 for a function without state): one more
    than the largest small constant (2..8) it compares with, else 4."""
    stateful = any(v.name.startswith(fn.qualified_name + "::") for v in layout.local_statics) or \
        bool(decision_state(fn, body, layout, macros))
    if not stateful:
        return 1
    small = [v for c in _conditions(body) for m in re.finditer(r"(?:==|!=|<=|>=|<|>)\s*\(?\s*([A-Za-z_]\w*|\d+)[uUlL]*", c)
             for v in [_int_value(m.group(1), macros)] if v is not None and 2 <= v <= 8]
    return max(small) + 1 if small else 4


def throws(body: str, layout: uc.LayoutProfile) -> bool:
    return layout.language == "cpp" and bool(re.search(r"\bthrow\b", body))


def _guard_throw(line: str, oid: str, can_throw: bool) -> str:
    """A3-45e: a call of a C++ function that throws is observed with UT_OBSERVE_THROW; A2-21f turns it
    into UT_EXPECT_THROW or a plain call."""
    if not can_throw:
        return line
    return f'UT_OBSERVE_THROW("{oid}_x", {line.rstrip().rstrip(";")});'


# ============================================================================ header SUT [A1-DA19]
# A1-DA19: the class templates of a header SUT are one fact of ut_contracts, shared with Agent1.
SCALAR_ARG = uc.SCALAR_ARG
_namespace_at = uc.namespace_at
_repository_text = uc.repository_text
_declared_types = uc.declared_types
_usable_arg = uc.usable_template_arg
_top_level_commas = uc.top_level_commas
_instantiate_param = uc.instantiate_param


def class_templates(plan: uc.UTPlan) -> dict[str, tuple[str, str]]:
    """A1-DA19: class templates the header SUT defines, short name -> (qualified name, template arguments)."""
    return uc.class_templates(plan.layout)


def _template_args(plan: uc.UTPlan, cls: str) -> dict[str, str]:
    """Template parameter name -> argument for a class template of the header SUT."""
    return uc.template_args(plan.layout, cls)


def function_templates(plan: uc.UTPlan) -> list[str]:
    """A1-DA19: explicit instantiations of the function templates the header SUT defines at namespace scope, with
    the arguments the repository uses (name<int>(...)) or int for a type and 4 for a value; only for a plain
    signature (no auto, decltype, requires, enable_if or parameter pack), so the access file always compiles."""
    layout = plan.layout
    sut = Path(layout.repo_root) / layout.sut_file
    if not layout.header_sut or layout.language != "cpp" or not sut.is_file():
        return []
    code = re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: " " * len(m.group(0)), sut.read_text(encoding="utf-8",
                  errors="replace"), flags=re.S)
    usage = _repository_text(str(Path(layout.repo_root)), str(sut))
    own_types = _declared_types(code)
    rows = []
    pattern = (r"\btemplate\s*<([^<>]*(?:<[^<>]*>[^<>]*)*)>\s*((?:(?:static|inline|constexpr)\s+)*)"
               r"([A-Za-z_][\w:<>,\s*&]*?)\s+([A-Za-z_]\w*)\s*\(([^(){};]*)\)\s*(?:noexcept\s*)?\{")
    for m in re.finditer(pattern, code):
        params, storage, ret, name, args_text = m.groups()
        if re.search(r"\b(?:auto|decltype|requires|enable_if\w*)\b|\.\.\.", m.group(0)) or "static" in storage:
            continue
        stack = []
        for b in re.finditer(r"\bnamespace\s+[\w:]*\s*\{|\{|\}", code[: m.start()]):
            if b.group(0) == "}":
                if stack:
                    stack.pop()
            else:
                stack.append(b.group(0).startswith("namespace"))
        if not all(stack):
            continue                                     # a member template: instantiated with its class
        names, defaults = [], []
        for prm in _top_level_commas(params):
            prm = prm.strip()
            if not prm:
                continue
            d = re.search(r"=\s*(.+)$", prm)
            names.append(re.sub(r"=.*$", "", prm).strip().split()[-1])
            if re.match(r"^(typename|class)\b", prm):
                defaults.append(d.group(1).strip() if d else "int")
            else:
                defaults.append(d.group(1).strip() if d else "4")
        chosen = defaults
        for use in re.finditer(r"\b" + re.escape(name) + r"\s*<\s*([^<>;{}()]+?)\s*>\s*\(", usage):
            found = [a.strip() for a in use.group(1).split(",")]
            if len(found) == len(names) and all(_usable_arg(a, own_types) for a in found):
                chosen = found
                break

        def put(text: str) -> str:
            for n, a in zip(names, chosen):
                text = re.sub(r"\b" + re.escape(n) + r"\b", a, text)
            return text
        plist = ", ".join(re.sub(r"\s*=\s*[^,]+$", "", p.strip()) for p in _top_level_commas(args_text) if p.strip())
        ns = _namespace_at(code, m.start())
        qualified = f"{ns}::{name}" if ns else name
        rows.append(f"template {put(ret.strip())} {qualified}<{', '.join(chosen)}>({put(plist)});")
    return rows


def template_instances(plan: uc.UTPlan) -> list[str]:
    """A1-DA19: explicit instantiations in the sut_access file, so the template's code is compiled and measured."""
    rows = [f"template class {q}<{args}>;" for q, args in class_templates(plan).values()]
    rows += function_templates(plan)
    return (["/* A1-DA19: the templates of the header SUT, instantiated for the tests. */", "#include <cstdint>",
             "#include <cstddef>", *rows, ""] if rows else [])


def static_functions(layout: uc.LayoutProfile) -> list[uc.FunctionInfo]:
    """A2-21k: functions outside any class with internal linkage that are not templates - the ones a wrapper reaches."""
    lines = _sut_lines(layout)
    return [f for f in layout.functions if f.is_static and f.from_frontend and not f.member
            and not _is_template(f, lines)]


def _sut_lines(layout: uc.LayoutProfile) -> list[str]:
    try:
        return uc.read_source(Path(layout.repo_root) / layout.sut_file).splitlines()
    except OSError:
        return []


def _is_template(fn: uc.FunctionInfo, lines: list[str]) -> bool:
    """A2-21k: the function is a template (its declaration, or the line before it, starts with template <...>)."""
    if re.match(r"\s*template\s*<", fn.signature):
        return True
    for k in (fn.line_start - 2, fn.line_start - 1):
        if 0 <= k < len(lines) and re.match(r"\s*template\s*<", lines[k]) and not lines[k].rstrip().endswith(";"):
            if k == fn.line_start - 2 or not re.search(r"\(", lines[k].split(">", 1)[-1]):
                return True
    return False


def wrapper_name(fn: uc.FunctionInfo) -> str:
    return "ut_static_" + re.sub(r"\W", "_", fn.name.replace("(anonymous namespace)::", ""))


def wrapper_signature(fn: uc.FunctionInfo) -> str:
    params = ", ".join(_declarator(p.type, p.name or f"a{i}") for i, p in enumerate(fn.params))
    return f"{fn.return_type or 'void'} {wrapper_name(fn)}({params or ('void' if fn.file.endswith('.c') else '')})"


def render_sut_access(plan: uc.UTPlan) -> tuple[str, str] | None:
    """A file in the test directory that includes the SUT, so its static functions can be called
    through wrappers; Agent3 then compiles and instruments it instead of the SUT [A2-21k]."""
    layout = plan.layout
    statics = static_functions(layout)
    setters = setter_vars(plan)                                                                # A2-21z
    elements = uc.element_setters(layout) if (Path(layout.repo_root) / layout.sut_file).is_file() else []
    if not statics and not uc.reset_state(layout) and not layout.header_sut and not elements:
        return None
    ext = "cpp" if layout.language == "cpp" else "c"
    test_dir = layout.writable_test_dir.rstrip("/")
    rel = os.path.relpath(layout.sut_file, test_dir).replace("\\", "/")
    access_name = f"ut_sut_access_{uc.unit_name(layout)}.{ext}"                             # A2-21af
    rows = [f"/* {access_name} - generated by agent_unittest_generator. It includes the SUT so that its static",
            "   functions can be tested; build it instead of the SUT itself, never together with it [A2-21k]. */",
            f'#include "{rel}"', ""]
    for fn in statics:
        callee = fn.qualified_name.replace("(anonymous namespace)::", "")
        args = ", ".join(p.name or f"a{i}" for i, p in enumerate(fn.params))
        call = f"{callee}({args})"
        rows += [wrapper_signature(fn), "{", f"    {'return ' if (fn.return_type or 'void') != 'void' else ''}{call};", "}", ""]
    if uc.reset_state(layout):                                                               # A2-21s
        rows.append(uc.sut_reset_source(uc.reset_state(layout), layout.language == "cpp"))
    if setters:                                                       # A2-21z: SUT state set by the tests
        rows += ["/* A2-21z: the tests set and read SUT state that only this file sees. */",
                 *[setter_source(v, layout.language == "cpp") for v in setters],
                 *[getter_source(v, layout.language == "cpp") for v in setters], ""]
    if elements:                                        # structured scenarios: elements and fields of SUT state
        rows += ["/* elements and fields of SUT state, set by structured scenarios */",
                 *(["#include <type_traits>"] if layout.language == "cpp" else []),
                 *[element_setter_source(e, layout.language == "cpp") for e in elements], ""]
    if layout.header_sut:                                                                    # A1-DA19
        rows += template_instances(plan)
    return f"{test_dir}/{access_name}", "\n".join(rows)


SUITE_TEST = re.compile(r"(?:/\*\s*evidence:\s*(E\d)(\s+linked)?\s*\*/\s*\n)?UT_TEST\(\s*(\w+)\s*\)\s*\{", re.M)


def parse_suite(text: str) -> dict[str, dict[str, str]]:
    """Tests of an existing generated suite: name -> {"source", "evidence"} (verbatim)."""
    tests = {}
    masked = uc.c_masked(text)                  # a brace in a string or a comment is not code
    for m in SUITE_TEST.finditer(text):
        depth, i = 0, m.end() - 1
        while i < len(text):
            if masked[i] == "{":
                depth += 1
            elif masked[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        tests[m.group(3)] = {"source": text[m.start(): i + 1], "evidence": m.group(1) or "E8",
                             "linked": bool(m.group(2))}
    return tests


def test_at_line(text: str, line: int) -> str:
    offset = sum(len(row) + 1 for row in text.split("\n")[: max(line - 1, 0)])
    for name, item in parse_suite(text).items():
        start = text.find(item["source"])
        if start <= offset <= start + len(item["source"]):
            return name
    return ""


def assemble_suite(plan: uc.UTPlan, tests: list[tuple[str, str, str]], stub_header: str | None,
                   shared: list[str] | None = None) -> str:
    """tests: (name, full test source incl. evidence comment, evidence class); ``shared``: the shared set-up of suite
    parts (BP-2), placed before the tests."""
    layout = plan.layout
    sut_headers = [include_name(h, layout) for h in layout.headers]
    declared = set()
    root = Path(layout.repo_root)
    for h in layout.headers:
        if (root / h).is_file():
            declared |= set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", (root / h).read_text(encoding="utf-8", errors="replace")))
    prototypes = [f.signature.rstrip("{ ") + ";" for f in layout.functions
                  if not f.is_static and "::" not in f.qualified_name and f.name not in declared and f.signature]
    # A2-21ad: a function of a namespace that no header declares is declared inside its namespace
    prototypes += [f"namespace {f.qualified_name.rsplit('::', 1)[0]} {{ {f.signature.rstrip('{ ')}; }}"
                   for f in layout.functions if layout.language == "cpp" and not f.is_static and "::" in f.qualified_name
                   and "(anonymous" not in f.qualified_name and not _is_member(f, layout)
                   and f.name.split("::")[-1] not in declared and f.signature and not layout.header_sut]
    lines = [f"/* {Path(layout.suite_file).name} - generated by agent_unittest_generator for {layout.sut_file}.",
             "   Every test states the strongest evidence class behind its assertions [A2-21d]. */",
             '#include "ut_harness.h"']
    root_text = uc.read_source(Path(layout.repo_root) / layout.sut_file) if (Path(layout.repo_root) / layout.sut_file).is_file() else ""
    lines += uc.sut_prologue(root_text, layout.functions, layout.header_sut)                    # A2-21ad
    if stub_header:
        lines.append(f'#include "{uc.stub_names(layout)[1]}"')                              # A2-21af
    if layout.header_sut and include_name(layout.sut_file, layout) not in sut_headers:          # A1-DA19
        sut_headers.append(include_name(layout.sut_file, layout))
    if uc.sut_prologue(root_text, layout.functions, layout.header_sut):
        sut_headers = [h for h in sut_headers if layout.header_sut and h == include_name(layout.sut_file, layout)]
    lines += [f'#include "{h}"' for h in sut_headers if not uc.STUB_HEADER.search(h)]
    instances = class_templates(plan)
    functions = function_templates(plan)
    if instances or functions:      # the tests use the instrumented instantiation of the sut_access file [A1-DA19]
        lines += ["#include <cstdint>"] + [f"extern template class {q}<{a}>;" for q, a in instances.values()]
        lines += ["extern " + row for row in functions]
    if render_sut_access(plan) is not None:                     # A2-21k
        prototypes += [wrapper_signature(f) + ";" for f in static_functions(layout) if not uses_private_type(f, layout)]
        prototypes += [f"void {state_setter(v)}(long long v);" for v in setter_vars(plan)]    # A2-21z
        prototypes += [f"long long {uc.state_getter_name(v)}(void);" for v in setter_vars(plan)]
        prototypes += [element_setter_prototype(e) for e in uc.element_setters(layout)]       # structured
    if layout.language == "cpp":                     # structured: accessors of the object members the tests set
        used = set(re.findall(r"\b(ut_tag_\w+)\b", "\n".join(src for _, src, _ in tests) + "\n".join(shared or [])))
        prototypes += uc.member_access_source([m for m in layout.class_members if uc.member_tag(m) in used], layout)
    lines += [f'#include "{h}"' for h in register_headers(plan) if h not in sut_headers]      # A2-21ab
    helpers = callback_helpers(layout)
    extra = ["#include <stddef.h>"] + (["#include <stdbool.h>"] if layout.language == "c" and any(
        p.kind == "bool" for f in layout.functions for p in f.params) else [])
    if layout.language == "cpp":
        extra.append("#include <stdexcept>")                  # exception types of UT_EXPECT_THROW [A3-45e]
    lines += ["#include <string.h>", *extra, ""] + prototypes + ([""] if prototypes else []) + \
        helpers + ([""] if helpers else [])
    for block in shared or []:                                                          # BP-2 suite_part
        lines += [block, ""]
    for _, source, _ in tests:
        lines += [source, ""]
    lines.append("UT_SUITE_BEGIN")
    lines += [f"    UT_CASE({name})" for name, _, _ in tests]
    lines += ["UT_SUITE_END", ""]
    return "\n".join(lines)


def render_test(name: str, body: str, evidence: str, linked: bool = False, purpose: str = "") -> str:
    """One test with its evidence class [A2-21d]; ``purpose`` becomes the first line of the body [A2-21q]."""
    rows = body.strip("\n").splitlines()
    first = next((r.strip() for r in rows if r.strip()), "")
    if purpose and not first.startswith(("/*", "//")):
        rows.insert(0, "/* " + purpose.replace("*/", "* /").strip() + " */")
    body = "\n".join("    " + row.rstrip() if row.strip() else "" for row in rows)
    return f"/* evidence: {evidence}{' linked' if linked and evidence != 'E8' else ''} */\nUT_TEST({name})\n{{\n{body}\n}}"


# ============================================================================ deterministic tests
INT_LITERAL = r"(-?(?:0[xX][0-9a-fA-F]+|\d+))[uUlL]*"


def int_literal(text: str) -> int | None:
    """'10', '-3', '0x1u', '(40)', '2UL' -> the integer; None for anything else."""
    m = re.fullmatch(r"\(?\s*" + INT_LITERAL + r"\s*\)?", text.strip())
    return int(m.group(1), 0) if m else None


def literal_values(text: str, macros: dict[str, str]) -> list[int]:
    values = set()
    for m in re.finditer(r"(?<![\w.])" + INT_LITERAL + r"(?![\w.])", text):
        v = int(m.group(1), 0)
        values |= {v - 1, v, v + 1}
    for name, value in macros.items():
        if re.search(r"\b" + name + r"\b", text):
            v = int_literal(value)
            if v is not None:
                values |= {v - 1, v, v + 1}
    if re.search(r"[(<>=,!]\s*-\s*[A-Za-z_(]", text):        # a comparison with a negated limit: x < -max_
        values |= {-v for v in values if v > 0}
    return sorted(values | {0, 1, -1})


def field_values(field: str, body: str, macros: dict[str, str]) -> list[int]:
    """A2-21r: the values a decision on a struct field compares it with (and their neighbours), and the bit
    masks it is tested with."""
    def value(token: str) -> int | None:
        return int_literal(macros.get(token, token)) if token else None
    values = set()
    ref = r"(?:->|\.)\s*" + re.escape(field) + r"\b"
    operand = r"([A-Za-z_]\w*|-?(?:0[xX][0-9a-fA-F]+|\d+))[uUlL]*"
    for m in re.finditer(ref + r"\s*(<=|>=|<|>|==|!=)\s*" + operand, body):
        v = value(m.group(2))
        if v is not None:
            values |= {v - 1, v, v + 1}
    for m in re.finditer(operand + r"\s*(<=|>=|<|>|==|!=)\s*[\w\]\[]+" + ref, body):
        v = value(m.group(1))
        if v is not None:
            values |= {v - 1, v, v + 1}
    masks = [v for m in re.finditer(ref + r"\s*&\s*" + operand, body) if (v := value(m.group(1))) is not None]
    values |= set(masks)
    if len(masks) > 1:
        values.add(functools.reduce(lambda a, b: a | b, masks))
    return sorted(values)


def pairwise(dimensions: list[list[Any]], cap: int) -> list[tuple[Any, ...]]:
    """Greedy all-pairs selection (deterministic), every value at least once, at most ``cap``."""
    if not dimensions:
        return [()]
    cap = max(cap, max(len(d) for d in dimensions))          # every value at least once
    if len(dimensions) == 1:
        return [(v,) for v in dimensions[0]][:cap]
    uncovered = {(i, a, j, b) for i, j in itertools.combinations(range(len(dimensions)), 2)
                 for a in range(len(dimensions[i])) for b in range(len(dimensions[j]))}
    unseen = {(i, a) for i in range(len(dimensions)) for a in range(len(dimensions[i]))}
    chosen = []
    candidates = list(itertools.islice(itertools.product(*[range(len(d)) for d in dimensions]), 20000))
    while (uncovered or unseen) and len(chosen) < cap:
        best, gain = None, -1
        for c in candidates:
            # every single value first (weight), then the most new pairs
            g = 100 * sum((i, c[i]) in unseen for i in range(len(c))) + \
                sum((i, c[i], j, c[j]) in uncovered for i, j in itertools.combinations(range(len(c)), 2))
            if g > gain:
                best, gain = c, g
        if gain <= 0:
            break
        chosen.append(best)
        unseen -= {(i, best[i]) for i in range(len(best))}
        uncovered -= {(i, best[i], j, best[j]) for i, j in itertools.combinations(range(len(best)), 2)}
    return [tuple(dimensions[k][idx] for k, idx in enumerate(c)) for c in chosen]


def _magnitude(choice: Any) -> int:
    """How far a choice is from a neutral value: the sum of the integers it contains (0 is neutral)."""
    text = choice if isinstance(choice, str) else repr(choice)
    return sum(abs(int(n, 0)) for n in re.findall(r"(?<![\w.])(-?(?:0[xX][0-9a-fA-F]+|\d+))", text))


def _spread(choices: list[Any]) -> list[int]:
    """The order in which one dimension's values are tried: the literals themselves (the middle of a v-1, v, v+1
    triple) and the return sequences first, then the values just above, then the values just below - so that
    a limit on the number of tests still reaches every boundary of the function."""
    ints = {c for c in choices if isinstance(c, int) and not isinstance(c, bool)}
    def rank(k: int) -> tuple:
        c = choices[k]
        if not isinstance(c, int) or isinstance(c, bool):
            return (0, k)
        if c - 1 in ints and c + 1 in ints:
            return (0, abs(c), k)
        if c - 1 in ints:
            return (1, abs(c), k)
        return (2, abs(c), k)
    return sorted(range(len(choices)), key=rank)


def _bases(dimensions: list[list[Any]], neutral: list[Any] | None = None) -> list[list[int]]:
    """The neutral base (the smallest values) and, per two-valued dimension (a flag), the base with it flipped."""
    keys = neutral or dimensions
    base = [min(range(len(d)), key=lambda k: (_magnitude(keys[i][k]), k)) for i, d in enumerate(dimensions)]
    return [base] + [[1 - b if j == i else b for j, b in enumerate(base)]
                     for i, d in enumerate(dimensions) if len(d) == 2]


def with_sequences(dimensions: list[list[Any]], neutral: list[Any] | None = None, picks: int = 3) -> list[tuple]:
    """A2-21r: a stub return sequence matters together with the inputs that make the function call the stub
    several times; each sequence is combined with the first values (in _spread order) of every other dimension,
    from every base."""
    out = []
    if not dimensions or any(not d for d in dimensions):
        return out
    for base in _bases(dimensions, neutral):
        for s, dim in enumerate(dimensions):
            for k, choice in enumerate(dim):
                if not isinstance(choice, tuple):
                    continue
                for p, other in enumerate(dimensions):
                    if p == s:
                        continue
                    for idx in [i for i in _spread(other) if i != base[p]][:picks]:
                        c = list(base)
                        c[s], c[p] = k, idx
                        out.append(tuple(dimensions[j][i] for j, i in enumerate(c)))
    return out


def one_at_a_time(dimensions: list[list[Any]], neutral: list[Any] | None = None) -> list[tuple[Any, ...]]:
    """A2-21r: a neutral base combination, then every value of each dimension with the others neutral - so that
    a value is not hidden by an unlucky partner (an error flag set together with the value under test). A
    two-valued dimension (a flag) gives a second base with the flag flipped. The order is breadth first (the
    k-th value of every dimension before the k+1-th), so a limit on the number keeps every dimension varied."""
    if not dimensions or any(not d for d in dimensions):
        return []
    bases = _bases(dimensions, neutral)
    orders = [_spread(d) for d in dimensions]
    runs = []
    for b in bases:
        run = [tuple(b)]
        for step in range(max(len(d) for d in dimensions)):
            for i, d in enumerate(dimensions):
                if step < len(d) and orders[i][step] != b[i]:
                    c = list(b)
                    c[i] = orders[i][step]
                    run.append(tuple(c))
        runs.append(run)
    order = [c for group in itertools.zip_longest(*runs) for c in group if c is not None]
    return [tuple(dimensions[j][idx] for j, idx in enumerate(c)) for c in order]


def combine(dimensions: list[list[Any]], cap: int, neutral: list[Any] | None = None) -> list[tuple[Any, ...]]:
    """One value at a time from a neutral base (at most the longest dimension plus ``cap``), the stub sequences
    with the inputs, then pairs; no duplicates (redundant ones are removed after the observation run, A2-21i)."""
    seen, out = set(), []
    sequences = with_sequences(dimensions, neutral)
    first = max([cap] + [len(d) for d in dimensions]) + cap
    limit = first + len(sequences) + cap
    for combo in one_at_a_time(dimensions, neutral)[:first] + sequences + pairwise(dimensions, cap):
        key = repr(combo)
        if key not in seen:
            seen.add(key)
            out.append(combo)
    return out[:limit]


def text_auto_tests(plan: uc.UTPlan, cap: int, existing: set[str]) -> list[tuple[str, str, str]]:
    """Without front-end types [A1-DA14]: free functions with scalar parameters only [A2-21h]."""
    layout = plan.layout
    root = Path(layout.repo_root)
    sut_lines = (root / layout.sut_file).read_text(encoding="utf-8", errors="replace").splitlines()
    macros = {}
    for h in layout.headers:
        if (root / h).is_file():
            for m in re.finditer(r"#[ \t]*define[ \t]+([A-Z_][A-Z0-9_]*)[ \t]+([^\n]+)",
                                 (root / h).read_text(encoding="utf-8", errors="replace")):
                macros[m.group(1)] = m.group(2)
    stubs = {s.function: s for s in plan.stubs}
    wanted = {s.function for s in plan.scenarios}
    out = []
    for fn in layout.functions:
        if fn.name not in wanted or fn.is_static or "::" in fn.qualified_name:
            continue
        m = re.match(r"^(.*?)\b" + re.escape(fn.name) + r"\s*\((.*)\)\s*(const)?$", fn.signature)
        if not m:
            continue
        ret = m.group(1).replace("static", "").replace("inline", "").replace("extern", "").strip()
        params = [p.strip() for p in m.group(2).split(",") if p.strip() and p.strip() != "void"]
        body = "\n".join(sut_lines[fn.line_start - 1: fn.line_end])
        values = literal_values(body, macros)
        dims, ok = [], True
        for p in params:
            ptype = re.sub(r"\s*\b[A-Za-z_]\w*$", "", p).strip() or p
            if INTEGRAL.match(ptype):
                dims.append([1 if v else 0 for v in (0, 1)] if "bool" in ptype else values)
            elif FLOATING.match(ptype):
                dims.append([float(v) for v in values] + [0.5])
            else:
                ok = False
        observe = "UT_OBSERVE_INT" if INTEGRAL.match(ret) else "UT_OBSERVE_DBL" if FLOATING.match(ret) else \
            ("" if ret == "void" else None)
        if not ok or observe is None:
            continue
        stub_dims = []
        controlled = [stubs[c] for c in fn.calls if c in stubs and stubs[c].return_type not in ("void", "")
                      and (INTEGRAL.match(stubs[c].return_type) or FLOATING.match(stubs[c].return_type))]
        for s in controlled:
            stub_dims.append(values)
        combos = pairwise(dims + stub_dims, cap)
        for index, combo in enumerate(combos, 1):
            name = f"test_{uc.function_stem(fn, plan.layout)}_auto_{index}"
            if name in existing:
                continue
            args = ", ".join(_c_literal(v) for v in combo[: len(dims)])
            lines = [f"{stub_names(s)['return']} = {_c_literal(v)};" for s, v in zip(controlled, combo[len(dims):])]
            call = f"{fn.name}({args})"
            lines.append(f'{observe}("{name}_r", {call});' if observe else f"{call};")
            out.append((name, render_test(name, "\n".join(lines), "E8"), "E8"))
    return out


def resolve_typedefs(t: str, layout: uc.LayoutProfile) -> str:
    """A1-DA21: a project typedef resolved to the type it names (kBase_t -> int32_t), through chains of them."""
    r = re.sub(r"\s+", " ", re.sub(r"\b(const|volatile|static|inline|extern|virtual)\b", "", t or "")).strip()
    for _ in range(8):
        nxt = layout.typedefs.get(r) or layout.typedefs.get(r.split("::")[-1]) if r else None
        if not nxt or nxt == r:
            break
        r = re.sub(r"\s+", " ", re.sub(r"\b(const|volatile)\b", "", nxt)).strip()
    return r


def _observer(ret: str, layout: uc.LayoutProfile) -> str | None:
    """The observation macro for a return type; "" for void, None when nothing can be observed."""
    r = re.sub(r"\b(const|volatile|static|inline|extern|virtual)\b", "", ret or "void").strip()
    resolved = resolve_typedefs(r, layout)
    if resolved and resolved != r and _observer(resolved, layout.model_copy(update={"typedefs": {}})) is not None:
        return _observer(resolved, layout.model_copy(update={"typedefs": {}}))
    enums = {e.name for e in layout.enums} | {e.name.split("::")[-1] for e in layout.enums}
    if r == "void":
        return ""
    if r in ("bool", "_Bool"):
        return "UT_OBSERVE_BOOL"
    if INTEGRAL.match(r) or r in enums or r.replace("enum ", "") in enums:
        return "UT_OBSERVE_INT"
    if FLOATING.match(r):
        return "UT_OBSERVE_DBL"
    if re.fullmatch(r"(const\s+)?char\s*\*", ret.strip()):
        return "UT_OBSERVE_STR"
    if r.endswith("*"):
        return "UT_OBSERVE_PTR"
    return None


def uses_private_type(fn: uc.FunctionInfo, layout: uc.LayoutProfile) -> bool:
    """A2-21ad: the function's signature names a type only the SUT file declares; no test file can call it."""
    if not layout.private_types:
        return False
    words = set(re.findall(r"\b[A-Za-z_]\w*\b", " ".join([fn.return_type, *(p.type for p in fn.params)])))
    return bool(words & set(layout.private_types))


def callee_of(fn: uc.FunctionInfo) -> str:
    return wrapper_name(fn) if fn.is_static and fn.from_frontend else fn.qualified_name


def callback_helpers(layout: uc.LayoutProfile) -> list[str]:
    """Callback functions tests can pass for function-pointer parameters, with a controllable result."""
    rows = []
    for fn in layout.functions:
        for i, p in enumerate(fn.params):
            if p.kind != "callback":
                continue
            base = f"ut_cb_{_ident(fn.name)}_{i}"
            ret = re.sub(r"^const\s+", "", p.callback_return or "void").strip()
            params = ", ".join(f"{t} a{k}" for k, t in enumerate(p.callback_params)) or ("void" if layout.language == "c" else "")
            body = [f"(void)a{k};" for k in range(len(p.callback_params))] + [f"++{base}_calls;"]
            if ret != "void":
                rows.append(f"static {ret} {base}_return;")
                body.append(f"return {base}_return;")
            rows.append(f"static int {base}_calls;")
            rows.append(f"static {ret} {base}({params}) {{ {' '.join(body)} }}")
    return rows


def _options(fn: uc.FunctionInfo, i: int, p: uc.ParamInfo, values: list[int], layout: uc.LayoutProfile,
             body: str = "", macros: dict[str, str] | None = None, tag: str = ""):
    """(setup lines, argument, observe-after lines) choices for one parameter, or None. ``tag`` keeps the
    local variables of a constructor's arguments apart from those of the method's."""
    opts = _param_options(fn, i, p, values, layout, body, macros)
    if opts is None or not tag:
        return opts
    rename = re.compile(r"\but_(v|b|f)" + str(i) + r"(?![0-9A-Za-z])")      # ut_v0 and ut_v0_dep0
    return [([rename.sub(lambda m: f"ut_{tag}{m.group(1)}{i}", x) for x in s_], rename.sub(lambda m: f"ut_{tag}{m.group(1)}{i}", a),
             [rename.sub(lambda m: f"ut_{tag}{m.group(1)}{i}", x) for x in o]) for s_, a, o in opts]


POINTEE_ELEMENTS = 256        # A2-21h: what a pointer to a scalar points to - an index of one byte stays inside
VOID_BUFFER_BYTES = 64        # A2-21h: what an untyped pointer points to


def buffer_length(fn: uc.FunctionInfo, p: uc.ParamInfo, body: str) -> int | None:
    """A2-21h: an integer parameter that bounds the elements read or written through a pointer parameter (a loop
    over buf[i] while i < len, a memcpy of len bytes): the size of what the test's pointer points to, else None."""
    ptrs = [q for q in fn.params if q.kind in ("ptr_scalar", "ptr_void", "buffer") and q.name and not q.by_ref]
    if not ptrs or not p.name:
        return None
    name = re.escape(p.name)
    bounds = re.search(r"\bfor\s*\([^;]*;[^;]*\b" + name + r"\b|\bwhile\s*\([^)]*\b" + name + r"\b|"
                       r"\bmem(?:cpy|set|cmp|move)\s*\([^;]*\b" + name + r"\b", body)
    if not bounds:
        return None
    used = [q for q in ptrs if re.search(r"\b" + re.escape(q.name) + r"\s*(?:\[|\+\+|\+=|\+\s*\w)|"
                                         r"\*\s*" + re.escape(q.name) + r"\s*\+\+|\bmem(?:cpy|set|cmp|move)\s*\([^;]*\b"
                                         + re.escape(q.name) + r"\b", body)]
    if not used:
        return None
    return min(VOID_BUFFER_BYTES if q.kind == "ptr_void" else POINTEE_ELEMENTS for q in used)


def _param_options(fn: uc.FunctionInfo, i: int, p: uc.ParamInfo, values: list[int], layout: uc.LayoutProfile,
                   body: str = "", macros: dict[str, str] | None = None):
    cpp = layout.language == "cpp"
    null = "nullptr" if cpp else "NULL"
    small = sorted(set(values), key=lambda v: (abs(v), v))[:5]
    t = re.sub(r"\b(const|volatile)\b", "", p.type).strip()
    if p.kind == "int":
        cast = "" if t in ("int", "long", "long long") else f"({t})"
        wide = [v for v in width_values(p) if v not in values]                             # A3-HW4
        size = buffer_length(fn, p, body)              # a length of the test's buffer stays inside it [A2-21h]
        chosen = values + wide if size is None else sorted({v for v in values + [0, 1, size] if 0 <= v <= size})
        return [([], f"{cast}{v}" if v >= 0 or not cast else f"{cast}({v})", []) for v in chosen]
    if p.kind == "ptr_void":                                          # A2-21h: a zeroed buffer of 64 bytes
        return [([f"unsigned char ut_b{i}[{VOID_BUFFER_BYTES}] = {{0}};"], f"ut_b{i}", [])] + \
            ([([], null, [])] if p.nullable else [])
    if p.kind == "interface":                                                                  # A2-21aa
        opts = _fake_options(p, i, layout, values)
        return (opts + ([([], null, [])] if p.nullable and not p.by_ref else [])) if opts else None
    if p.kind == "bool":
        return [([], ("true" if v else "false") if cpp else str(v), []) for v in (0, 1)]
    if p.kind == "enum":
        return [([], v, []) for v in p.enum_values] or None
    if p.kind == "float":
        return [([], repr(float(v)), []) for v in values[:8]] + [([], "0.5", [])]
    if p.kind == "ptr_scalar":
        base = p.base or "int"
        cast = "" if base in ("int", "long", "long long", "double", "float") else f"({base})"
        observe = "UT_OBSERVE_INT" if not FLOATING.match(base) else "UT_OBSERVE_DBL"
        if p.by_ref:
            opts = [([f"{base} ut_v{i} = {cast}{v};"], f"ut_v{i}",
                     [] if "const" in p.type else [f'{observe}("@_o{i}", ut_v{i});']) for v in small]
        else:              # A2-21h: a pointer may be read or written beyond its first element (a buffer, a bit field)
            opts = [([f"{base} ut_v{i}[{POINTEE_ELEMENTS}] = {{{cast}{v}}};"], f"ut_v{i}",
                     [] if "const" in p.type.split("*")[0] else [f'{observe}("@_o{i}", ut_v{i}[0]);']) for v in small]
        return opts + ([([], null, [])] if p.nullable and not p.by_ref else [])
    # the parameter's own character type: C++ converts no string literal or char array to unsigned char *
    plain_char = re.fullmatch(r"(const\s+)?char\s*\*", p.type.strip())
    if p.kind == "cstring":
        cast = "" if plain_char or not p.type.strip().endswith("*") else f"({p.type.strip()})"
        return [([], cast + s, []) for s in ('""', '"a"', '"ABCDEFGHIJKLMNOPQRSTUVWXYZ"')] + \
            ([([], null, [])] if p.nullable else [])
    if p.kind == "buffer":
        element = "char" if plain_char or not p.type.strip().endswith("*") else p.type.strip()[:-1].strip()
        return [([f"{element} ut_b{i}[256] = {{0}};"], f"ut_b{i}", [])]
    if p.kind in ("struct", "ptr_struct") and p.fields:
        own = {f.name: field_values(f.name, body, macros or {}) for f in p.fields}           # A2-21r
        # A2-21ad: a pointer field points to a zeroed local object of its pointee (a handle's Instance)
        targets = [uc.pointee_object(f.base, f"ut_p{i}_{k}", cpp) if f.kind == "pointer" else None
                   for k, f in enumerate(p.fields)]
        # ... and NULL as well when the function compares that field with NULL
        dims = [sorted(set(small) | set(own[f.name])) if f.kind in ("int", "enum") else [0, 1] if f.kind == "bool"
                else [float(v) for v in small] if f.kind == "float" else
                [targets[k][1]] + (["0"] if uc.null_checked(f.name, body) else []) if f.kind == "pointer"
                else [0] if f.kind == "callback" else ["{}" if cpp else "{0}"] for k, f in enumerate(p.fields)]
        # C++: any other field is value-initialized with {} - a scoped enumeration or a class takes no {0} [A2-21ad]
        base = p.base or t.rstrip("*& ").strip()
        # C++ forbids narrowing in a brace initializer: integer fields get their own type, and an enumeration field
        # (a scoped one takes no integer at all) the type of the field itself, whatever namespace declared it [A2-21ad]
        casts = [f"({f.type})" if cpp and f.kind == "int" and f.type else
                 f"static_cast<decltype({base}::{f.name})>" if cpp and f.kind == "enum" else "" for f in p.fields]
        inits = ["{" + ", ".join(casts[k] + (f"({_c_literal(v)})" if casts[k] else _c_literal(v))
                                 for k, v in enumerate(combo)) + "}"
                 for combo in combine(dims, max(6, max(len(d) for d in dims)))]
        ref = p.kind == "struct" or p.by_ref
        pre = [x[0] for x in targets if x is not None]
        opts = [([*pre, f"{base} ut_v{i} = {init};"], f"ut_v{i}" if ref else f"&ut_v{i}", []) for init in inits]
        return opts + ([([], null, [])] if p.kind == "ptr_struct" and p.nullable and not p.by_ref else [])
    if p.kind == "callback":
        base = f"ut_cb_{_ident(fn.name)}_{i}"
        if (p.callback_return or "void").strip() == "void":
            return [([], base, [])] + ([([], null, [])] if p.nullable else [])
        return [([f"{base}_return = {v};"], base, []) for v in (0, 1, -1)] + ([([], null, [])] if p.nullable else [])
    cls = uc.sut_class(p.base or p.type, layout) if p.kind == "object" else ""
    if cls:                              # an object of a class of the SUT: built by its constructor [A2-21ae]
        lines, var = uc.class_recipe(cls, layout, f"ut_v{i}",
                                     declared=re.sub(r"\b(const|volatile)\b|[&*]", "", p.base or p.type).strip())
        pointer = "*" in p.type and not p.by_ref
        return [(lines, f"&{var}" if pointer else var, [])] + ([([], null, [])] if pointer and p.nullable else [])
    return None


def auto_tests(plan: uc.UTPlan, cap: int, existing: set[str],
               brain_values: dict[tuple[str, int], tuple[list[str], str]] | None = None) -> list[tuple[str, str, str]]:
    """Characterization tests without an LLM [A2-21h]: every parameter kind the front end reports
    (scalars, enums, pointers to scalars, strings, buffers, plain structs, callbacks) gets values from
    the function's own literals and macros, combined pairwise with the controlled stub values;
    static functions are called through their wrappers [A2-21k]. Results are observed (E8)."""
    layout = plan.layout
    if not any(f.from_frontend for f in layout.functions):
        return text_auto_tests(plan, cap, existing)
    root = Path(layout.repo_root)
    sut_lines = (root / layout.sut_file).read_text(encoding="utf-8", errors="replace").splitlines()
    macros = {}
    for h in layout.headers:
        if (root / h).is_file():
            for m in re.finditer(r"#[ \t]*define[ \t]+([A-Z_][A-Z0-9_]*)[ \t]+([^\n]+)",
                                 (root / h).read_text(encoding="utf-8", errors="replace")):
                macros[m.group(1)] = m.group(2)
    stubs = {s.function: s for s in plan.stubs if s.kind in ("function", "method")}
    wanted = {s.function for s in plan.scenarios if not s.inputs}
    out = []
    for fn in layout.functions:
        if fn.name not in wanted or (fn.qualified_name.count("::") and _is_member(fn, layout)) \
                or uses_private_type(fn, layout):                                       # A2-21ad
            continue
        observe = _observer(fn.return_type, layout)
        body = "\n".join(sut_lines[fn.line_start - 1: fn.line_end])
        helpers = "\n".join("\n".join(sut_lines[g.line_start - 1: g.line_end]) for g in layout.functions
                            if g.is_static and g.name in fn.calls and g is not fn)                 # A2-21r
        values = literal_values(body + "\n" + helpers, macros)
        options = [_options(fn, i, p, values, layout, body, macros) for i, p in enumerate(fn.params)]
        for i, p in enumerate(fn.params):                       # A2-21ac: a value the rules cannot build
            recipe = (brain_values or {}).get((fn.qualified_name, i))
            if options[i] is None and recipe is not None:
                null = "nullptr" if layout.language == "cpp" else "NULL"
                options[i] = [(list(recipe[0]), recipe[1], [])] + ([([], null, [])] if p.nullable else [])
        endless = endless_loop(body)                                                       # A2-21y
        if any(o is None for o in options) or (observe is None and not endless):
            continue
        polls = register_polls(fn, body + "\n" + helpers, plan.layout)                      # A2-21ab
        waits = register_waits(fn, body, plan.layout)
        state = decision_state(fn, body, layout, header_macros(layout))                    # A2-21z
        repeats = 1 if endless or waits else repeat_count(fn, body, layout, header_macros(layout))
        can_throw = throws(body + "\n" + helpers, layout)                                  # A3-45e
        asm = uses_target_asm(fn, body + "\n" + helpers, layout)                           # A3-HW3
        # the function's own stubs and those of the static helpers it calls [A2-21r]
        called = list(dict.fromkeys([*fn.calls, *(c for g in layout.functions if g.is_static and g.name in fn.calls
                                                   and g is not fn for c in g.calls)]))
        controlled = [stubs[c] for c in called if c in stubs and stubs[c].return_type not in ("void", "")
                      and (INTEGRAL.match(resolve_typedefs(stubs[c].return_type, layout))
                           or FLOATING.match(resolve_typedefs(stubs[c].return_type, layout)))]
        dims = [list(range(len(o))) for o in options] + [_stub_choices(s, body + "\n" + helpers, values, layout)
                                                         for s in controlled]
        dims += [[0, 1] for _ in polls] + [vals for _, vals in state] + ([[0, 1]] if asm else [])
        drivers = project_drivers(fn, layout, sut_lines, macros)                                  # A2-21ak
        dims += [vals for _, vals in drivers]
        neutral = [[" ".join(o[k][0]) + " " + o[k][1] for k in range(len(o))] for o in options] + dims[len(options):]
        n_stub = len(options) + len(controlled)
        n_asm = n_stub + len(polls) + len(state)
        for index, combo in enumerate(combine(dims, cap, neutral), 1):
            name = f"test_{uc.function_stem(fn, layout)}_auto_{index}"              # overloads apart [NC-4]
            if name in existing:
                continue
            setup, args, after = [], [], []
            for k, choice in enumerate(combo[: len(options)]):
                s, a, o = options[k][choice]
                setup += s
                args.append(a)
                after += [line.replace("@", name) for line in o]
            for s, v in zip(controlled, combo[len(options): n_stub]):
                names = stub_names(s)
                if isinstance(v, tuple):                          # a return sequence [A2-21g, A2-21r]
                    setup += [f"{names['returns']}[{k}] = {_c_literal(x)};" for k, x in enumerate(v)]
                    setup += [f"{names['returns_len']} = {len(v)};", f"{names['return']} = {_c_literal(v[-1])};"]
                else:
                    setup.append(f"{names['return']} = {_c_literal(v)};")
            setup += [f"{reg} |= {mask};   /* the register bit the function waits for [A2-21ab] */"
                      for (reg, mask), v in zip(polls, combo[n_stub: n_stub + len(polls)]) if v]
            setup += [f"{state_setter(var)}({v});" for (var, _), v in
                      zip(state, combo[n_stub + len(polls): n_stub + len(polls) + len(state)])]
            if asm and combo[n_asm]:
                setup.append(f"ut_asm_value = {combo[n_asm]};   /* result of the replaced target assembly [A3-HW3] */")
            for (control, _), v in zip(drivers, combo[n_asm + (1 if asm else 0):]):          # A2-21ak
                if v is not None:
                    setup.append(f"{control.name}({_c_literal(v)});" if control.kind == "function" else
                                 f"{control.name} = {_c_literal(v)};")
            call = f"{callee_of(fn)}({', '.join(args)})"
            preset = {p for p, v in zip(polls, combo[n_stub: n_stub + len(polls)]) if v}
            setup += wait_setup(waits, preset)                    # A2-21ab
            if endless:                                           # the call ends by its budget [A2-21y]
                lines = setup + [f"UT_EXPECT_NO_RETURN({call});"]
            else:
                calls = []
                for k in range(1, repeats + 1):                    # a call sequence of a stateful function [A2-21z]
                    oid = f"{name}_r" if repeats == 1 else f"{name}_r{k}"
                    calls.append(_guard_throw(_observe_call(observe, oid, _enum_value(fn.return_type, layout, call)),
                                              oid, can_throw))
                lines = setup + calls + after
            out.append((name, render_test(name, "\n".join(lines), "E8"), "E8"))
    return out


def project_stub_text(fn: uc.FunctionInfo, layout: uc.LayoutProfile) -> str:
    """A2-21ak: the controls of the project stubs this function depends on, for the brain."""
    rows: dict[str, list[str]] = {}
    for ps, control in uc.project_controls(fn, layout):
        rows.setdefault(f"{ps.file}" + (f" (header {ps.header})" if ps.header else ""), []).append(
            f"  {control.declaration};")
    if not rows:
        return ""
    return ("PROJECT STUB CONTROLS (the project's own stubs of the hardware this code calls; call these in the set-up "
            "to set what the stubbed functions return - without them they return their defaults, so many branches "
            "stay unreachable) [A2-21ak]:\n" + "\n".join(f"- {k}:\n" + "\n".join(v) for k, v in rows.items()))


def project_drivers(fn: uc.FunctionInfo, layout: uc.LayoutProfile, sut_lines: list[str],
                    macros: dict[str, str]) -> list[tuple[uc.StubControl, list[Any]]]:
    """A2-21ak: the controls of the project stubs the function depends on that take one integral or boolean value
    (a writable integral global too), each with "not called" and the literals and macros of the function and the SUT
    functions it calls (two levels); a boolean with false and true."""
    out = []
    reach = [fn] + [g for g in layout.functions if g is not fn and (g.name in fn.calls or g.qualified_name in fn.calls
                                                                    or any(g.name in h.calls for h in layout.functions
                                                                           if h.name in fn.calls))]
    text = "\n".join("\n".join(sut_lines[g.line_start - 1: g.line_end]) for g in reach)
    values = literal_values(text, macros)
    for _, control in uc.project_controls(fn, layout):
        ctype = control.type if control.kind == "variable" else (control.params[0][0] if len(control.params) == 1 else "")
        plain = resolve_typedefs(re.sub(r"\b(const|volatile)\b", "", ctype).strip(), layout) if ctype else ""
        if control.kind == "function" and (len(control.params) != 1 or control.return_type.strip() not in ("void", "")):
            continue
        if re.fullmatch(r"(_Bool|bool)", plain):
            out.append((control, [None, 0, 1]))
        elif plain and INTEGRAL.match(plain):
            usable = [v for v in values if v >= 0] if "unsigned" in plain or re.match(r"uint|size_t", plain) else values
            if len(usable) > 10:                                     # evenly over the range, the ends kept
                step = (len(usable) - 1) / 9
                usable = sorted({usable[round(k * step)] for k in range(10)})
            out.append((control, [None] + usable))
    return out


def _stub_choices(spec: uc.StubSpec, body: str, values: list[int], layout: uc.LayoutProfile) -> list[Any]:
    """Single return values; for a stub the function calls more than once also sequences in which the calls
    return different values (A2-21r)."""
    choices: list[Any] = list(values)
    calls = len(re.findall(r"\b" + re.escape(spec.function.split("::")[-1]) + r"\s*\(", body))
    storage = re.sub(r"^const\s+", "", re.sub(r"\b(extern|static|inline|virtual)\b", "", spec.return_type).strip())
    if calls > 1 and INTEGRAL.match(resolve_typedefs(storage, layout) or storage) and sequence_type(storage, layout):
        positive = [v for v in values if v > 0][-6:]
        choices += [(0,) * (calls - 1) + (v,) for v in positive] + [(v,) + (0,) * (calls - 1) for v in positive[-2:]]
    return choices


def _enum_value(ret: str, layout: uc.LayoutProfile, expr: str) -> str:
    """A C++ scoped enumeration has no implicit conversion: it is observed as its integer value."""
    r = re.sub(r"\b(const|volatile|static|inline|extern|virtual|enum|class)\b", "", ret or "").strip()
    if layout.language == "cpp" and r and any(r.split("::")[-1] == e.name.split("::")[-1] for e in layout.enums):
        return f"static_cast<int>({expr})"
    return expr


def _observe_call(observe: str, oid: str, call: str) -> str:
    if not observe:
        return f"{call};"
    if observe == "UT_OBSERVE_PTR":
        return f'UT_OBSERVE_BOOL("{oid}", ({call}) != 0);'
    return f'{observe}("{oid}", {call});'


def _is_member(fn: uc.FunctionInfo, layout: uc.LayoutProfile) -> bool:
    return fn.member


def _ident(name: str) -> str:
    return uc.safe_name(name)                                   # operators in words [NC-4]


# A test name has at most 63 characters: the length of an internal identifier that C99 and later
# guarantee to be significant [A2-21q].
NAME_LIMIT = 63


def readable_test_name(raw: str, function: str, taken: set[str]) -> str:
    """A2-21q: the name repaired deterministically - never a reason to discard a test. Characters not
    allowed in an identifier become underscores, test_ and missing words come from the function's name,
    a long name is cut after its last whole word, a name in use gets _2, _3 ..."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", raw or "") if w]
    if words and words[0].lower() == "test":
        words = words[1:]
    fn_words = [w for w in re.split(r"[^A-Za-z0-9]+", _ident(function)) if w] or ["function"]
    if len(words) < 2:                                  # test_<what>_<behaviour>: at least three words
        words = fn_words + (words or ["checks", "behaviour"])
        words = words if len(words) >= 2 else words + ["case"]

    def cut(limit: int) -> str:
        name = "test"
        for w in words:
            if len(name) + 1 + len(w) > limit:
                break
            name += "_" + w
        if name.count("_") < 2:                         # one very long word: cut inside it
            name = ("test_" + "_".join(words))[:limit].rstrip("_")
        return name

    name, k = cut(NAME_LIMIT), 2
    while name in taken:
        suffix = f"_{k}"
        name, k = cut(NAME_LIMIT - len(suffix)) + suffix, k + 1
    return name


# ---------------------------------------------------------------- expected values of LLM tests [A2-21u]
EXPECT_CALL = re.compile(r"\b(UT_EXPECT_(?:EQ_INT|NE_INT|EQ_DBL|EQ_STR|TRUE|FALSE))\s*\(")
NUMBER = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?[uUlLfF]*$")
COMPARE = re.compile(r"(==|!=|<=|>=|(?<![-<>])<(?![<=])|(?<![-<>])>(?![>=]))")


def _call_args(text: str, open_at: int) -> tuple[list[str], int]:
    """The top-level arguments of the call whose '(' is at ``open_at``, and the index after its ')'."""
    depth, args, start, i, quote = 0, [], open_at + 1, open_at, ""
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                args.append(text[start:i].strip())
                return args, i + 1
        elif c == "," and depth == 1:
            args.append(text[start:i].strip())
            start = i + 1
        i += 1
    return [], len(text)


def _value(expr: str) -> float | None:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        expr = expr[1:-1].strip()
    if not NUMBER.match(expr):
        return None
    try:
        return float(expr.rstrip("uUlLfF") if not re.search(r"[eE]", expr) else expr.rstrip("uUlLfF"))
    except ValueError:
        return None


def _stated(expected: str, texts: list[str], bool_ok: bool) -> bool:
    """A2-21u: a number occurs in a cited evidence text; a name occurs there as a word; true/false also
    for a bool function citing E1-E3 evidence of its own."""
    expected = expected.strip()
    while expected.startswith("(") and expected.endswith(")"):
        expected = expected[1:-1].strip()
    number = _value(expected)
    if number is not None:
        found = {float(m) for t in texts for m in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", t)}
        return number in found
    if expected in ("true", "false", "1", "0") and bool_ok:
        return True
    if re.fullmatch(r"[A-Za-z_][\w:]*", expected):
        word = expected.split("::")[-1]
        return any(re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", t) for t in texts)
    if expected.startswith('"') and expected.endswith('"'):
        return any(expected[1:-1] in t for t in texts)
    return False


def ground_expectations(body: str, test: str, texts: list[str], bool_ok: bool) -> tuple[str, int]:
    """A2-21u: every expected value the LLM wrote must be stated by the evidence the test cites; one
    that is not is observed instead of asserted (A2-21f), a string is left out. Returns the body and
    the number of assertions changed."""
    calls = []
    for m in EXPECT_CALL.finditer(body):
        args, end = _call_args(body, m.end() - 1)
        if args:
            calls.append((m.start(), end, m.group(1), args))
    used = len(re.findall(r"UT_OBSERVE_\w+\(", body))
    changed = 0
    for start, end, macro, args in reversed(calls):
        observed = ""
        if macro in ("UT_EXPECT_EQ_INT", "UT_EXPECT_NE_INT", "UT_EXPECT_EQ_DBL") and len(args) >= 2:
            if _stated(args[1], texts, bool_ok):
                continue
            observed = f"{'UT_OBSERVE_DBL' if macro == 'UT_EXPECT_EQ_DBL' else 'UT_OBSERVE_INT'}(\"{{id}}\", {args[0]})"
        elif macro in ("UT_EXPECT_TRUE", "UT_EXPECT_FALSE") and args:
            cond = args[0]
            parts = COMPARE.split(cond, maxsplit=1)
            if len(parts) == 3:
                left, right = parts[0].strip(), parts[2].strip()
                literal, other = (right, left) if _value(right) is not None or not _value(left) else (left, right)
                if _stated(literal, texts, bool_ok):
                    continue
                kind = "UT_OBSERVE_DBL" if (_value(literal) or 0) % 1 else "UT_OBSERVE_INT"
                observed = f"{kind}(\"{{id}}\", {other})"
            else:
                if _stated("true" if macro == "UT_EXPECT_TRUE" else "false", texts, bool_ok):
                    continue
                observed = f"UT_OBSERVE_INT(\"{{id}}\", ({cond}) ? 1 : 0)"
        elif macro == "UT_EXPECT_EQ_STR" and len(args) >= 2:
            if _stated(args[1], texts, bool_ok):
                continue
        else:
            continue
        used += 1
        changed += 1
        tail = body[end:]
        tail = re.sub(r"^\s*;[ \t]*(/\*\s*EV-[^*]*\*/)?", ";", tail, count=1)
        replacement = observed.replace("{id}", f"{test}_g{used}") if observed else \
            "/* expected string not stated by the evidence: not asserted [A2-21u] */ (void)0"
        body = body[:start] + replacement + tail
    return body, changed


def observed_instead(name: str, source: str, repair: uc.RepairItem) -> str | None:
    """A2-21p: Agent3 decided that a failing asserted test, not the source, is wrong (or Agent4 found no source
    change, A4-23j). The run's value is the fact: the test keeps its inputs and observes what it asserted; the observe
    run and finalize (A2-21f) turn each observation back into an assertion with the run's value - the reference form
    of the oracle check (A3-85b), so the change is accepted only when the test detects every mutant the reference
    detects, and it is reported as a change of expectation. The values now come from the run: E8. A crash or a timeout
    means its inputs miss: dropped (None), its items are planned again with the reason. No brain rewrites the test."""
    if repair.failure_kind in ("crash", "timeout"):
        return None
    masked = uc.c_masked(source)
    head = re.search(r"UT_TEST\(\s*\w+\s*\)\s*\{", masked)
    end = masked.rfind("}")
    if not head or end <= head.end():
        return None
    body = source[head.end(): end]
    body = re.sub(r"^\s*/\*.*?\*/\s*\n", "", body, count=1, flags=re.S)          # its old purpose line
    body, _ = ground_expectations(body, name, [], False)       # nothing is stated any more: every value observed
    count = len(re.findall(r"UT_OBSERVE_\w+\(", body))
    for macro in ("UT_EXPECT_THROW", "UT_EXPECT_ANY_THROW", "UT_EXPECT_NULL", "UT_EXPECT_NOT_NULL"):
        while True:
            m = re.search(r"\b" + macro + r"\s*\(", body)
            if not m:
                break
            args, after = _call_args(body, m.end() - 1)
            if not args:
                break
            count += 1
            oid = f"{name}_g{count}"
            text = (f'UT_OBSERVE_THROW("{oid}", {args[0]})' if "THROW" in macro else
                    f'UT_OBSERVE_BOOL("{oid}", ({args[0]}) != 0)')
            body = body[: m.start()] + text + body[after:]
    if not re.search(r"\bUT_OBSERVE_\w+\(", body) or re.search(r"\bUT_EXPECT_", body):
        return None                                  # nothing left to observe, or a check it cannot observe
    why = (repair.diagnostic or repair.issue)[:140]
    return render_test(name, body.strip("\n"), "E8", False,
                       f"values from the run - it disagreed with the expected ones ({why}) and the test was judged "
                       "wrong [A2-21p]")


def return_of(fn: uc.FunctionInfo) -> str:
    """The return type: from the front end, else from the signature text [A1-DA14]."""
    if fn.return_type:
        return fn.return_type
    head = fn.signature.split("(")[0].strip()
    m = re.match(r"^(.*?)\b[A-Za-z_][\w:]*$", head)
    return re.sub(r"\b(static|inline|extern|virtual|explicit)\b", "", m.group(1)).strip() if m else ""


def _assert_return(fn: uc.FunctionInfo, call: str, expected: str) -> str:
    full = return_of(fn)
    ret = re.sub(r"\b(const|volatile|static|inline|extern|virtual)\b", "", full or "void").strip()
    if FLOATING.match(ret):
        return f"UT_EXPECT_EQ_DBL({call}, {expected}, 1e-6);"
    if re.fullmatch(r"(const\s+)?char\s*\*", (full or "").strip()):
        return f"UT_EXPECT_EQ_STR({call}, {expected});"
    if ret.endswith("*"):
        return f"UT_EXPECT_TRUE(({call}) == ({expected}));"
    return f"UT_EXPECT_EQ_INT({call}, {expected});"


def _assert_value(expr: str, value: str) -> str:
    v = value.strip()
    if re.fullmatch(r'(?:L|u8|u|U)?"(?:[^"\\]|\\.)*"', v):
        return f"UT_EXPECT_EQ_STR({expr}, {v});"
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?[fF]?", v):
        return f"UT_EXPECT_EQ_DBL({expr}, {v}, 1e-6);"
    return f"UT_EXPECT_EQ_INT({expr}, {v});"


def param_names(fn: uc.FunctionInfo) -> list[str] | None:
    """Parameter names in order: from the front end, else from the signature text [A1-DA14]."""
    if fn.params:
        return [p.name for p in fn.params]
    m = re.search(r"\((.*)\)", fn.signature.split("{")[0], re.S)
    if m is None:
        return None
    inner = m.group(1).strip()
    if inner in ("", "void"):
        return []
    names = []
    for part in re.split(r",(?![^()]*\))", inner):
        found = re.findall(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*$", part.strip())
        if not found:
            return None
        names.append(found[-1])
    return names


def scenario_comment(sc: uc.Scenario) -> str:
    """First line of a test [A2-21q]: scenario id, purpose and the evidence its assertions rest on [A2-21a]."""
    cited = [e.evidence_id + (f" @{uc.evidence_key(e)}" if e.linked_by else "") for e in sc.evidence] \
        if not sc.characterization else []
    return f"{sc.scenario_id}: {sc.purpose or sc.description[:120]}" + (f" [{', '.join(cited)}]" if cited else "")


def assemble_scenario(sc: uc.Scenario, plan: uc.UTPlan, name: str,
                      block: tuple[list[str], str] | None = None) -> str | None:
    """A2-21m: a checked brain scenario whose values are all given becomes a test without an LLM; None
    when it needs code the scenario does not contain (an object, a missing argument). ``block``: the object's
    building block from a recipe or the brain, in place of the rule's (NC-1, NC-2)."""
    layout = plan.layout
    fn = uc.function_of(sc.function, layout, sc.function_line)                          # overloads [NC-4]
    if fn is None or (fn.is_static and not fn.from_frontend):
        return None
    ctor = fn.member and fn.qualified_name.split("::")[-1] == uc.class_of(fn).split("::")[-1]   # a constructor
    recipe = block or (uc.object_recipe(fn, layout, sc.object_args) if fn.member and not ctor else None)  # A2-21ae
    if fn.member and recipe is None and not ctor and not uc.static_member(fn, layout):
        return None
    given = {a.name: a.value for a in sc.args}
    names = param_names(fn)
    if names is None or set(given) != set(names):
        return None
    stubs: dict[str, list[uc.StubSpec]] = {}
    for s in plan.stubs:                             # a name of several overloads sets every one of them [A1-DA4]
        for key in dict.fromkeys((s.function, s.qualified_name or s.function)):
            stubs.setdefault(key, []).append(s)
    lines = list(recipe[0]) if recipe else []                   # the object and its named arguments first [A2-21ae]
    for sv in sc.stub_values:
        if not stubs.get(sv.stub) or not sv.returns:
            return None
        for spec in stubs[sv.stub]:
            controls = stub_names(spec)
            if len(sv.returns) == 1:
                lines.append(f"{controls['return']} = {sv.returns[0]};")
                continue
            storage = re.sub(r"^const\s+", "", re.sub(r"\b(extern|static|inline|virtual)\b", "",
                                                       spec.return_type).strip().rstrip("&").strip())
            if not sequence_type(storage, layout) or len(sv.returns) > 16:
                return None
            lines += [f"{controls['returns']}[{i}] = {v};" for i, v in enumerate(sv.returns)]
            lines += [f"{controls['returns_len']} = {len(sv.returns)};", f"{controls['return']} = {sv.returns[-1]};"]
    lines += [f"{v.name} = {v.value};" for v in sc.variables]
    setup = [s.strip() if s.strip().endswith((";", "}")) else s.strip() + ";" for s in sc.setup if s.strip()]
    if recipe:                                   # the object is declared once, by its building block [A2-21ae]
        setup, _ = uc.without_redeclarations(setup, set().union(*(uc.declared_names(x) for x in recipe[0])))
    lines += setup
    sut = Path(layout.repo_root) / layout.sut_file
    body = "\n".join(sut.read_text(encoding="utf-8", errors="replace").splitlines()[fn.line_start - 1: fn.line_end]) \
        if sut.is_file() else ""
    waits = register_waits(fn, body, plan.layout)            # the brain's test ends a register wait too [A2-21ab]
    if waits:
        given_setup = " ".join(sc.setup)
        preset = {(reg, mask) for reg, mask, _ in waits
                  if re.search(re.escape(reg) + r"\s*(\|=|=)[^;]*\b" + re.escape(mask) + r"\b", given_setup)}
        lines += wait_setup(waits, preset)
    lines += list(sc.calls_before)                    # the earlier calls that build up the state [BP-2]
    if ctor:            # the object is what the constructor makes: its stated exception, or the object itself
        made = f"{uc.instantiated_class(uc.class_of(fn), layout)} ut_obj({', '.join(given[n] for n in names)})"
        wrapped = f"([&] {{ {made}; }}())"            # one macro argument, even with a template's commas
        if not sc.characterization and sc.expected_exception:
            lines.append(f"UT_EXPECT_THROW({wrapped}, {sc.expected_exception});")
        elif sc.characterization and throws(body, layout):                                   # A3-45e
            lines.append(f'UT_OBSERVE_THROW("{name}_x", {wrapped});')
        else:
            lines.append(f"{made};")
            if not sc.characterization:
                lines += [_assert_value(c.expr, c.value) for c in sc.expected_after]
        return "\n".join(lines)
    target = recipe[1] if recipe else (f"{uc.class_of(fn)}::{fn.qualified_name.split('::')[-1]}" if fn.member
                                       else callee_of(fn))
    call = f"{target}({', '.join(given[n] for n in names)})"
    full = return_of(fn)
    ret = re.sub(r"\b(const|volatile|static|inline|extern|virtual)\b", "", full or "void").strip()
    can_throw = sc.characterization and throws(body, layout)   # an observed exception, not a failure [A3-45e]
    if not sc.characterization and sc.expected_exception:                 # the stated exception [structured]
        lines.append(f"UT_EXPECT_THROW({call}, {sc.expected_exception});")
    elif not sc.characterization and sc.expected_return:
        lines.append(_assert_return(fn, call, sc.expected_return))
    elif ret == "void" or not ret:
        lines.append(_guard_throw(f"{call};", name, can_throw))
    else:
        observe = _observer(full, layout)
        lines.append(_guard_throw(_observe_call(observe, f"{name}_r", _enum_value(full, layout, call)), f"{name}_r",
                                  can_throw) if observe is not None else _guard_throw(f"{call};", name, can_throw))
    if not sc.characterization:
        lines += [_assert_value(c.expr, c.value) for c in sc.expected_after]
    return "\n".join(lines)


def setup_names(statements: list[str]) -> set[str]:
    """Local variables a test body declares (function pointers among them may be called), and objects it
    constructs - "MotorCtrl ctrl(enc);" declares ctrl, it does not call a function ctrl [A2-20]."""
    names = set()
    for stmt in statements:
        names |= set(re.findall(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)\s*\(", stmt))
        for m in DECLARED_OBJECT.finditer(code_only(stmt)):
            if m.group(1) not in NOT_A_TYPE:
                names.add(m.group(2))
    return names


# "Type name(args)" or "Type name{args}" at the start of a statement: an object, not a call [A2-20]
DECLARED_OBJECT = re.compile(r"(?:^|[;{}])\s*(?:(?:const|static|volatile|constexpr)\s+)*"
                             r"([A-Za-z_][\w:]*(?:<[^;(){}]*>)?)\s*[&*]*\s+([A-Za-z_]\w*)\s*[({]", re.M)
NOT_A_TYPE = {"return", "else", "case", "goto", "throw", "new", "delete", "sizeof", "typedef", "using", "do",
              "co_return", "co_yield", "co_await", "not", "and", "or"}
CHECKS = re.compile(r"\bUT_(?:EXPECT|OBSERVE|ASSERT)\w*\s*\(")


def code_only(text: str) -> str:
    """The text without comments, string and character literals: braces in a comment are not code [A2-24]. The
    lexer of ut_contracts, so every agent reads C text the same way."""
    return uc.c_masked(text)


def _has_auto(function: str, auto_names: list[str], layout: uc.LayoutProfile) -> bool:
    """Did the deterministic generator write a test for this function in this bundle [A2-21o]?"""
    fn = next((f for f in layout.functions if function in (f.name, f.qualified_name)), None)
    if fn is None:
        return False
    prefixes = [f"test_{uc.function_stem(f, layout)}_auto_" for f in uc.overloads(fn, layout)] + \
        [f"test_{fn.name.replace('::', '_')}_auto_", f"test_{_ident(fn.name)}_auto_"]
    if "::" in fn.qualified_name:
        cls, method = fn.qualified_name.rsplit("::", 1)
        prefixes.append(f"test_{cls.split('::')[-1]}_{method}_auto_")
    return any(n.startswith(tuple(prefixes)) for n in auto_names)


def fuzz_tests(plan: uc.UTPlan, existing: set[str]) -> list[tuple[str, str, str]]:
    """One characterization test per input found by coverage-guided search [A3-FZ4, A2-21l]."""
    layout = plan.layout
    out = []
    for scenario in plan.scenarios:
        for inp in scenario.inputs:
            fn = uc.function_of(inp.function, layout, scenario.function_line)
            if fn is None:
                continue
            k = 1
            while f"test_{uc.function_stem(fn, layout)}_fuzz_{k}" in existing:
                k += 1
            name = f"test_{uc.function_stem(fn, layout)}_fuzz_{k}"
            existing.add(name)
            observe = _observer(fn.return_type, layout)
            method = fn.member and not uc.static_member(fn, layout)      # the setup built ut_obj [A3-FZ2, A2-21ae]
            call = f"{'ut_obj.' + fn.qualified_name.split('::')[-1] if method else callee_of(fn)}({', '.join(inp.args)})"
            body = inp.setup + [_observe_call(observe or "", f"{name}_r", _enum_value(fn.return_type, layout, call))]
            body.append(f"/* reaches: {', '.join(inp.covers[:6])} */")
            out.append((name, render_test(name, "\n".join(body), "E8"), "E8"))
    return out


def _params(signature: str, name: str) -> tuple[str, list[str]] | None:
    m = re.match(r"^(.*?)\b" + re.escape(name) + r"\s*\((.*)\)\s*(const)?$", signature)
    if not m:
        return None
    ret = m.group(1).replace("static", "").replace("inline", "").replace("extern", "").replace("virtual", "").strip()
    params = [p.strip() for p in m.group(2).split(",") if p.strip() and p.strip() != "void"]
    return ret, params


def _scalar_dims(params: list[str], values: list[int]) -> list[list[Any]] | None:
    dims = []
    for p in params:
        ptype = re.sub(r"\s*\b[A-Za-z_]\w*$", "", p).strip() or p
        if INTEGRAL.match(ptype):
            dims.append([0, 1] if "bool" in ptype else values)
        elif FLOATING.match(ptype):
            dims.append([float(v) for v in values] + [0.5])
        else:
            return None
    return dims


def _member_options(fn: uc.FunctionInfo | None, values: list[int], layout: uc.LayoutProfile, body: str,
                    macros: dict[str, str], tag: str) -> list[list[tuple[list[str], str, list[str]]]] | None:
    """Choices per parameter of a constructor or method: the front end's kinds (A2-21h), fakes for injected
    interfaces (A2-21aa); without front-end types only scalars."""
    if fn is None:
        return []
    if fn.from_frontend:
        options = [_options(fn, i, p, values, layout, body, macros, tag) for i, p in enumerate(fn.params)]
        return None if any(o is None for o in options) else options
    sig = _params(fn.signature, fn.name)
    dims = _scalar_dims(sig[1], values) if sig else None
    if dims is None:
        return None
    return [[([], _c_literal(v), []) for v in d] for d in dims]


class _Choice:
    """A parameter choice that is not a plain literal (a fake, a buffer): ordered by the values it contains."""
    def __init__(self, k: int, text: str):
        self.k, self.text = k, text

    def __repr__(self) -> str:
        return self.text

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Choice) and other.k == self.k

    def __hash__(self) -> int:
        return hash(("choice", self.k))


def _literal_of(arg: str) -> int | float | None:
    text = arg.strip()
    if text in ("true", "false"):
        return 1 if text == "true" else 0
    text = re.sub(r"^\((?:const\s+)?[A-Za-z_][\w:\s]*\)\s*", "", text).strip()
    text = text[1:-1].strip() if text.startswith("(") and text.endswith(")") else text
    try:
        return int(text, 0)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None


def _option_tokens(options: list[tuple[list[str], str, list[str]]]) -> list[Any]:
    """The values of one parameter as the combination rules see them [A2-21h, A2-21r]: plain literals as
    numbers (so the neutral base and the boundary order work on values), anything else as a choice."""
    tokens = [(_literal_of(a) if not s_ and not o else None) for s_, a, o in options]
    if all(t is not None for t in tokens) and len(set(tokens)) == len(tokens):
        return tokens
    return [_Choice(k, " ".join(s_) + " " + a) for k, (s_, a, o) in enumerate(options)]


def _gentle(option: tuple[list[str], str, list[str]]) -> tuple[int, int]:
    """Order of preference for a value set up only to bring an object into a state: 1, 0, then small."""
    text = " ".join(option[0]) + " " + option[1]
    nums = [int(n, 0) for n in re.findall(r"(?<![\w.])-?(?:0[xX][0-9a-fA-F]+|\d+)(?![\w.])", text.split("=")[-1])]
    v = nums[-1] if nums else 0
    return ({1: 0, 0: 1}.get(v, 2), abs(v))


def _class_sizes(cls: str, headers: str) -> list[int]:
    """Small sizes in a class's member declarations (template arguments, array bounds): a sequence of calls
    one longer than such a size fills the member [A2-21z]."""
    m = re.search(r"\b(?:class|struct)\s+" + re.escape(cls.split("::")[-1].split("<")[0]) + r"\b[^;{]*\{", headers)
    if not m:
        return []
    depth, k = 1, m.end()
    while k < len(headers) and depth:
        depth += {"{": 1, "}": -1}.get(headers[k], 0)
        k += 1
    body = headers[m.end(): k]
    body = re.sub(r"\{[^{}]*\}", ";", body)          # inline method bodies out
    sizes = [int(x) for pair in re.findall(r"<[^<>;()]*?\b(\d+)[uUlL]*\s*>|\[\s*(\d+)[uUlL]*\s*\]", body)
             for x in pair if x]
    return [v for v in sizes if 2 <= v <= 16]


def auto_class_tests(plan: uc.UTPlan, cap: int, existing: set[str]) -> list[tuple[str, str, str]]:
    """C++: characterization tests for class methods [A2-21h]. Each test constructs an object - with fakes for
    injected interfaces (A2-21aa) - and calls the method repeatedly (stateful behaviour, A2-21z), with the
    stubbed dependencies set before construction and before the calls. A method that throws is observed with
    UT_OBSERVE_THROW (A3-45e); one with an endless loop is called with UT_EXPECT_NO_RETURN (A2-21y); a register
    bit it waits for is set before the call (A2-21ab)."""
    layout = plan.layout
    root = Path(layout.repo_root)
    sut_lines = (root / layout.sut_file).read_text(encoding="utf-8", errors="replace").splitlines()
    macros = header_macros(layout)
    stubs = {s.function: s for s in plan.stubs}
    wanted = {s.function for s in plan.scenarios}
    by_class: dict[str, list[uc.FunctionInfo]] = {}
    for fn in layout.functions:
        if "::" in fn.qualified_name and (fn.member or not fn.from_frontend):
            by_class.setdefault(fn.qualified_name.rsplit("::", 1)[0], []).append(fn)
    out = []
    headers = "\n".join(h.read_text(encoding="utf-8", errors="replace") for h in _sut_includes(layout))
    if layout.header_sut:
        headers += "\n" + "\n".join(sut_lines)
    for cls, members in by_class.items():
        short = cls.split("::")[-1]
        if not re.search(r"\b(class|struct)\s+(\w+\s+)*" + re.escape(short) + r"\b", headers):
            continue                       # a class of the SUT file itself: no test can name it
        ctor = next((f for f in members if f.qualified_name.endswith(f"{short}::{short}")), None)
        ctor_body = "\n".join(sut_lines[ctor.line_start - 1: ctor.line_end]) if ctor else ""
        declared = None if ctor else uc.constructor_of(cls, layout)          # declared in a header, inline too
        mapping = _template_args(plan, cls)                                                    # A1-DA19
        if mapping:
            members = [f.model_copy(update={"params": [_instantiate_param(p, mapping) for p in f.params]})
                       for f in members]
            ctor = next((f for f in members if f.qualified_name.endswith(f"{short}::{short}")), None)
            cls = uc.instantiated_class(cls, layout)                                       # A1-DA19
        ctor = ctor or declared
        for fn in members:
            if fn is ctor or fn.name not in wanted or fn.qualified_name.split("::")[-1].startswith("~"):
                continue
            sig = _params(fn.signature, fn.name)
            ret = sig[0] if sig else ""
            ret = fn.return_type or ret
            body = "\n".join(sut_lines[fn.line_start - 1: fn.line_end])
            values = literal_values(body + "\n" + ctor_body, macros)
            ctor_opts = _member_options(ctor, values, layout, ctor_body, macros, "c")
            opts = _member_options(fn, values, layout, body, macros, "")
            if ctor_opts and ctor:          # a fake's values matter only to methods that call the interface [A2-21aa]
                for k, p in enumerate(ctor.params):
                    iface = next((f for f in layout.interfaces if p.kind == "interface" and
                                  f.name.split("::")[-1] == p.base.split("::")[-1]), None)
                    if iface and not any(re.search(r"(\.|->)\s*" + re.escape(m.name) + r"\s*\(", body)
                                         for m in iface.methods):
                        ctor_opts[k] = ctor_opts[k][:1]
            observe = _observer(ret, layout)
            endless = endless_loop(body)
            if ctor_opts is None or opts is None or (observe is None and not endless):
                continue
            static = bool(re.match(r"^\s*static\b", fn.signature)) or ("static" in ret.split())
            calls = set(fn.calls) | set(ctor.calls if ctor else [])
            controlled = [stubs[c] for c in sorted(calls) if c in stubs and
                          (INTEGRAL.match(resolve_typedefs(stubs[c].return_type, layout))
                           or FLOATING.match(resolve_typedefs(stubs[c].return_type, layout)))]
            # value at construction; at the calls a single value or one value per call [A2-21g, A2-21r]
            steps = [v for v in values if v > 1][-6:]
            at_calls = [list(values) + ([(0, v, 2 * v) for v in steps] if sequence_type(
                re.sub(r"^const\s+", "", st.return_type.strip()), layout) else []) for st in controlled]
            built = [st for st in controlled if ctor and st.function in ctor.calls]
            at_ctor = [values for _ in built]
            polls = register_polls(fn, body, plan.layout)                                      # A2-21ab
            waits = register_waits(fn, body, plan.layout)
            repeats = 1 if endless else max(4, repeat_count(fn, body, layout, macros))
            sizes = [int(v) for k, v in mapping.items() if re.fullmatch(r"\d+", v) and re.search(r"\b" + k + r"\b", body)]
            sizes += _class_sizes(cls, headers)       # and the sizes of the class's members [A2-21z]
            if sizes and not endless:                   # a template size the method compares with [A1-DA19, A2-21z]
                repeats = min(17, max(repeats, max(sizes) + 1))
            if waits:                                   # the first call takes the register bit [A2-21ab]
                repeats = 1
            can_throw = throws(body, layout)
            # the object as the class's other methods leave it: each called once before [A2-21z]
            others = []
            read = {f for c in _conditions(body) for f in re.findall(r"\b([A-Za-z_]\w*)\b", c)}
            if not static and read:
                for j, other in enumerate(g for g in members if g is not fn and g is not ctor and not
                                          g.qualified_name.split("::")[-1].startswith("~") and
                                          not re.search(r"\)\s*const\s*$", g.signature)):
                    o_body = "\n".join(sut_lines[other.line_start - 1: other.line_end])
                    if endless_loop(o_body) or register_waits(other, o_body, plan.layout):
                        continue
                    # only a method that changes what this one decides on, by a computed value (not a reset)
                    changed = {m.group(1) for m in re.finditer(
                        r"\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?:\+\+|--|[-+*/|&^]=|=(?!=)\s*(?!-?\s*(?:\d|true\b|"
                        r"false\b|nullptr\b|NULL\b|[A-Z]\w*::\w+\s*;)))", o_body)}
                    changed |= set(re.findall(r"(?:\+\+|--)\s*([A-Za-z_]\w*)", o_body))
                    if not changed & read:
                        continue
                    o_opts = _member_options(other, values, layout, o_body, macros, f"p{j}")
                    if o_opts is not None and all(o_opts):
                        others.append((other, o_opts))
            asm = uses_target_asm(fn, body, layout)                                            # A3-HW3
            dims = [_option_tokens(o) for o in ctor_opts + opts] + at_ctor + at_calls + [[0, 1] for _ in polls] + \
                ([[0, 1]] if asm else []) + ([[0, 1]] if others else [])
            n_ctor, n_opts = len(ctor_opts), len(opts)
            method = uc.function_stem(fn, layout).split(_ident(uc.class_of(fn)) + "_", 1)[-1] \
                if "::" in fn.qualified_name else uc.safe_name(fn.qualified_name.split("::")[-1])
            for index, combo in enumerate(combine(dims, cap), 1):
                name = f"test_{short}_{method}_auto_{index}"
                if name in existing:
                    continue
                setup, ctor_args, args, after = [], [], [], []
                for k, token in enumerate(combo[:n_ctor]):
                    s_, a, o = ctor_opts[k][dims[k].index(token)]
                    setup += s_
                    ctor_args.append(a)
                    after += [x.replace("@", name) for x in o]
                method_setup = []
                for k, token in enumerate(combo[n_ctor: n_ctor + n_opts]):
                    s_, a, o = opts[k][dims[n_ctor + k].index(token)]
                    method_setup += s_
                    args.append(a)
                    after += [x.replace("@", name) for x in o]
                stub_values = combo[n_ctor + n_opts: n_ctor + n_opts + len(built) + len(controlled)]
                before = [f"{stub_names(st)['return']} = {_c_literal(v)};" for st, v in zip(built, stub_values)]
                at = []
                for st, v in zip(controlled, stub_values[len(built):]):
                    names = stub_names(st)
                    if isinstance(v, tuple):
                        at += [f"{names['returns']}[{k}] = {_c_literal(x)};" for k, x in enumerate(v)]
                        at += [f"{names['returns_len']} = {len(v)};", f"{names['return']} = {_c_literal(v[-1])};"]
                    else:
                        at.append(f"{names['return']} = {_c_literal(v)};")
                at += [f"{reg} |= {mask};   /* the register bit the method waits for [A2-21ab] */"
                       for (reg, mask), v in zip(polls, combo[n_ctor + n_opts + len(built) + len(controlled):]) if v]
                if static:
                    lines = setup + before + method_setup + at
                    target = f"{cls}::{method}"
                else:
                    lines = setup + before + [f"{cls} ut_obj{'(' + ', '.join(ctor_args) + ')' if ctor_args else ''};"] + \
                        method_setup + at
                    target = f"ut_obj.{method}"
                call = f"{target}({', '.join(args)})"
                preset = {p for p, v in zip(polls, combo[n_ctor + n_opts + len(built) + len(controlled):]) if v}
                if asm and combo[len(dims) - 1 - (1 if others else 0)]:
                    lines.append(f"ut_asm_value = 1;   /* result of the replaced target assembly [A3-HW3] */")
                if others and combo[-1]:
                    for other, o_opts in others:
                        o_setup, o_args = [], []
                        for o in o_opts:
                            pick = min(o, key=_gentle)          # a small value: 1, then 0 (loop counts, sizes)
                            o_setup += pick[0]
                            o_args.append(pick[1])
                        lines += o_setup + [f"UT_CALL_ANY(ut_obj.{other.qualified_name.split('::')[-1]}({', '.join(o_args)}));"]
                lines += wait_setup(waits, preset)                         # A2-21ab
                if endless:                                                # A2-21y
                    lines.append(f"UT_EXPECT_NO_RETURN({call});")
                else:
                    for k in range(1, repeats + 1):       # a series of calls on one object [A2-21z]
                        oid = f"{name}_r{k}"
                        lines.append(_guard_throw(_observe_call(observe, oid, _enum_value(ret, layout, call)), oid, can_throw))
                    lines += after
                out.append((name, render_test(name, "\n".join(lines), "E8"), "E8"))
    return out


def _c_literal(v: Any) -> str:
    if isinstance(v, float):
        return repr(v)
    return str(v)


# ============================================================================ the agent
class GeneratorAgent(ba.AgentCore):
    name = "agent_unittest_generator"
    application = "UT"                                            # VIEW-2
    view_role, view_description, view_support = uc.VIEW_AGENTS["agent_unittest_generator"]

    def view_summary(self, model):
        return uc.view_summary(self.view_label, model) or super().view_summary(model)

    def __init__(self, settings: ba.Settings | None = None, *, config: GeneratorConfig | None = None,
                 router: ba.BrainRouter | None = None):
        self.cfg = config or GeneratorConfig()
        super().__init__(settings, router=router)
        self._recipes: dict[tuple[str, int], tuple[tuple[list[str], str], ba.RuleFailure, str]] = {}   # A2-21ac
        self._confirmed: set[tuple[str, int]] = set()
        self._pending_blocks: dict[str, tuple[str, str, str]] = {}   # mark -> (rule, key, recipe JSON) [NC-2]

    def tools(self) -> list[ba.ContractTool]:
        def pre(payload: uc.GenerationRequest, request):
            plan = payload.plan
            uc.check_signature(plan.layout, request)
            if plan.layout.blocked or plan.stage == "blocked":
                raise ba.ContractRejected("layout is blocked or ambiguous [A2-4]")
            if plan.stage == "done":
                raise ba.ContractRejected("coverage is already complete: nothing to generate [A2-5]")

        async def generate(payload: uc.GenerationRequest, request, scratch):
            return await self.generate_bundle(payload)

        def post(payload, bundle: uc.FileActionBundle):
            if bundle.claims_verification:
                raise ba.AgentFault("CONTRACT_VIOLATION", "Agent2 must not claim verification [A2-33]")

        def finalize_pre(payload: uc.FinalizeRequest, request):
            if payload.bundle.claims_verification:
                raise ba.ContractRejected("the bundle claims verification; only Agent3 verifies [A2-33]")
            if not payload.bundle.observations:
                raise ba.ContractRejected("the bundle has no observation probes to finalize [A2-21f]")

        async def finalize(payload: uc.FinalizeRequest, request, scratch):
            return finalize_bundle(payload.bundle, payload.observed, payload.test_items, payload.crashed,
                                   payload.ran, payload.build_message)

        return [ba.ContractTool("generate", uc.GenerationRequest, uc.FileActionBundle, generate, pre, post,
                                description="Stubs, test suite and test cases for Agent1's plan."),
                ba.ContractTool("finalize", uc.FinalizeRequest, uc.FileActionBundle, finalize, finalize_pre, post,
                                description="Turn observed values into characterization assertions [A2-21f].")]

    async def generate_bundle(self, req: uc.GenerationRequest) -> uc.FileActionBundle:
        plan = req.plan
        layout = plan.layout
        root = Path(layout.repo_root)
        suite_path = root / layout.suite_file
        existing_text = req.existing_suite or (suite_path.read_text(encoding="utf-8") if suite_path.is_file() else "")
        existing = parse_suite(existing_text)
        notes, changes = [], []
        self._confirm_blocks(existing_text, notes)                                           # NC-2, NC-3
        trial: dict[str, str] = {}                     # refused only by predictions: the tool decides [NC-7]
        trial_targets: dict[str, list[str]] = {}
        made: dict[str, str] = {}                      # BP-7
        recipes: dict[str, str] = {}                   # NC-2
        needs_kind: dict[str, str] = {}                # BP-9
        shared = {m.group(1): m.group(0) for m in SHARED_BLOCK.finditer(existing_text)}      # BP-2 suite_part
        # Failing tests named by Agent1, or found from a diagnostic's line in the suite.
        drop = set(plan.drop_tests)
        rewrite: dict[str, uc.RepairItem] = {}
        for r in plan.repairs:
            name = ""
            if r.target_file.endswith(Path(layout.suite_file).name) and r.line and existing_text:
                name = test_at_line(existing_text, r.line)
                if name:
                    drop.add(name)
            if r.affected_test:
                drop.add(r.affected_test)
                name = name or r.affected_test
            if name and r.action == "rewrite" and name in existing and existing[name]["evidence"] != "E8":
                rewrite[name] = r          # A2-21p: Agent3 decided the test is wrong, not the source - see below
        kept = [(n, t["source"], t["evidence"]) for n, t in existing.items() if n not in drop]
        linked = {n for n, t in existing.items() if n not in drop and t.get("linked")}
        stub_path, stub_source, stub_header = render_stubs(plan)
        actions = [uc.FileAction(path=stub_path, action="replace" if (root / stub_path).exists() else "create",
                                 role="stub", content=stub_source),
                   uc.FileAction(path=str(Path(stub_path).with_name(uc.stub_names(layout)[1])).replace("\\", "/"),
                                 action="create", role="helper", content=stub_header)]
        access = render_sut_access(plan)
        if access is not None:
            actions.append(uc.FileAction(path=access[0], action="replace" if (root / access[0]).exists() else "create",
                                         role="sut_access", content=access[1]))
        new_tests: list[tuple[str, str, str]] = []
        names = {n for n, _, _ in kept}
        for name, r in sorted(rewrite.items()):           # A2-21p: the decision, then a rule - never a rewrite
            replacement = observed_instead(name, existing[name]["source"], r)
            if replacement is None:
                notes.append(f"{name}: {r.failure_kind or 'failed'} - its inputs miss: dropped, its items are "
                             "planned again [A2-21p]")
                continue
            new_tests.append((name, replacement, "E8"))
            names.add(name)
            notes.append(f"{name}: the run disagrees with its expected values and the test, not the source, was "
                         "judged wrong: same inputs, its values taken from the run (reference form, A3-85b) [A2-21p]")
        if plan.stage == "coverage" and any(s.inputs for s in plan.scenarios):
            fuzz = fuzz_tests(plan, names)
            new_tests += fuzz
            if fuzz:
                notes.append(f"{len(fuzz)} tests from inputs found by coverage-guided search")
        if plan.stage == "coverage":
            auto: list[tuple[str, str, str]] = []
            if self.cfg.mode in ("deterministic_then_llm", "deterministic") and not any(
                    n.endswith(tuple(f"_auto_{i}" for i in range(1, 3))) for n in names) and \
                    not any(sc.inputs for sc in plan.scenarios):
                values = await self._brain_values(plan, self._allowed_symbols(plan, self._headers_text(layout),
                                                                              stub_header), existing_text, notes)
                auto = auto_tests(plan, self.cfg.max_auto_tests_per_function, names, values)
                if layout.language == "cpp":
                    auto += auto_class_tests(plan, self.cfg.max_auto_tests_per_function, names)
                if auto:
                    notes.append(f"{len(auto)} deterministic characterization tests")
            new_tests += auto
            names |= {n for n, _, _ in new_tests}
            allowed = self._allowed_symbols(plan, self._headers_text(layout), stub_header)
            llm_scenarios, assembled = [], 0
            # BP-2: a whole test is written as code only by a brain whose profile has the kind "test". For the others
            # (Qwen3 8B) every brain test comes from a structured scenario the generator builds; a scenario it cannot
            # build is not built, and the reason goes to the journal, so the next plan changes it [structured].
            writers = await uc.brains_for(self, "test", "A2.tests")

            def to_writer(sc: uc.Scenario, why: str) -> None:
                if writers or plan.rules_only:            # the rule round leaves them to the plan below [A1-PL1]
                    llm_scenarios.append(sc)
                    return
                notes.append(f"{sc.function}: not built ({why}): no brain of this run writes whole tests - the next "
                             "plan gets the reason [BP-2, structured]")
            for sc in plan.scenarios:
                if sc.inputs:
                    continue
                if sc.checked:                                                        # A2-21m
                    if sc.kind in ("test", "suite_part") and not sc.trial:    # BP-3: the assembled form missed
                        to_writer(sc, f"asked as {sc.kind}")
                        continue
                    name = self._test_name(sc, names)
                    fn = uc.function_of(sc.function, layout, sc.function_line)
                    block, block_by, pending = None, "", []
                    if fn is not None and fn.member and not uc.static_member(fn, layout) and \
                            not uc.internal_member(fn, layout):                                     # NC-1
                        block, block_by, pending, missing = await self._building_block(fn, plan, sc.kind, req, notes)
                        if missing and not await uc.brains_for(self, "test", "A2.tests"):
                            needs_kind.update({g: missing for g in sc.gap_ids})   # not even a whole test [BP-9]
                    body = assemble_scenario(sc, plan, name, block)
                    problem = self._body_problem(body, allowed | setup_names(sc.setup) | setup_names(
                        block[0] if block else []) | ({"ut_obj"} if fn is not None and fn.member else set())) \
                        if body is not None else "?"         # ut_obj(30): the object built with its values
                    predicted = bool(problem) and body is not None and uc.check_kind(problem) == "prediction"
                    if body is not None and (not problem or predicted):
                        evidence = sc.strongest_class if not sc.characterization else "E8"
                        if sc.made_by.startswith("brain:"):                                         # BP-7
                            made[name] = f"{sc.made_by.split(':', 1)[1]} {sc.kind}"
                        if block_by:
                            made[name] = (made.get(name, "") + f"; object: {block_by}").strip("; ")
                        by = f" - by {made[name]} [BP-7]" if name in made else ""
                        new_tests.append((name, render_test(name, body, evidence, sc.linked, scenario_comment(sc) + by),
                                          evidence))
                        names.add(name)
                        assembled += 1
                        if sc.linked and evidence != "E8":
                            linked.add(name)
                        if sc.trial or predicted:                                                 # NC-7
                            trial[name] = "; ".join(sc.trial + ([problem] if predicted else []))[:600]
                            trial_targets[name] = list(sc.gap_ids)
                        for mark, rule, key, raw in pending:                                       # NC-2
                            self._pending_blocks[mark] = (rule, key, raw)
                            recipes[name] = raw
                        continue
                    if sc.trial:                       # a trial scenario that cannot be assembled is not rewritten
                        notes.append(f"{name}: refused by predictions ({'; '.join(sc.trial)[:160]}) and not "
                                     "assembled; dropped [NC-7]")
                        continue
                    to_writer(sc, f"the generator could not build it: {problem}" if body is not None and problem
                              else "the generator could not build it: an argument or the object is missing")
                elif not sc.characterization:                                         # A2-21n
                    to_writer(sc, "no checked plan states the documented behaviour yet")
                elif self.cfg.mode == "llm" or not _has_auto(sc.function, [n for n, _, _ in auto], layout):
                    to_writer(sc, "no rule test and no checked plan")                 # A2-21o
            if assembled:
                notes.append(f"{assembled} tests assembled from checked scenarios - planned by the brain or solved by rule "
                             "[A2-21m, A1-PL15]")
            if plan.rules_only and llm_scenarios:            # the rule round: measured first, then the brain [A1-PL1]
                notes.append(f"{len(llm_scenarios)} scenario(s) of {len({sc.function for sc in llm_scenarios})} "
                             "function(s) left to the brain's plan after this round's measurement [A1-PL1]")
                llm_scenarios = []
            if self.cfg.mode in ("deterministic_then_llm", "llm") and llm_scenarios:
                llm_tests, llm_notes, llm_linked, llm_trial, llm_made, llm_shared = await self._llm_tests(
                    plan, req, names, llm_scenarios, allowed)
                new_tests += llm_tests
                notes += llm_notes
                linked |= llm_linked
                for name, why in llm_trial.items():                                              # NC-7
                    trial[name] = why
                    sc_ids = re.findall(r"\bS-[A-Za-z0-9_.\-]*[A-Za-z0-9_]", next((src for n, src, _ in llm_tests
                                                                                if n == name), ""))
                    trial_targets[name] = sorted({g for sc in plan.scenarios if sc.scenario_id in sc_ids
                                                  for g in sc.gap_ids})
                made.update(llm_made)
                shared.update(llm_shared)
        for name in sorted(drop & set(existing)):
            replacement = next((t for t in new_tests if t[0] == name), None)
            old = existing[name]
            if old["evidence"] != "E8" and replacement is not None and _assertions(old["source"]) != _assertions(replacement[1]):
                changes.append(uc.ExpectationChange(test=name, old="; ".join(_assertions(old["source"])),
                                                    new="; ".join(_assertions(replacement[1])),
                                                    reason="values from the run, the test judged wrong "
                                                    "[A2-21p]: " +
                                                    (rewrite[name].diagnostic[:160] if name in rewrite else
                                                     "repair requested by Agent1"), evidence=old["evidence"]))
            elif name in rewrite and replacement is None:
                pass                                         # dropped with its reason above [A2-21p]
        tests = kept + new_tests
        final = {n for n, _, _ in tests}
        kinds = {r.affected_test: r.failure_kind for r in plan.repairs if r.affected_test}
        dropped = {n: {"crash": "crashed", "timeout": "timeout"}.get(kinds.get(n, ""), "failing: the run disagreed "
                                                                                  "with its expected values")
                   for n in drop if n in existing and n not in final}
        suite = assemble_suite(plan, tests, stub_header, [shared[k] for k in sorted(shared)])
        actions.append(uc.FileAction(path=layout.suite_file, action="replace" if existing_text else "create",
                                     role="testsuite", content=suite,
                                     evidence_classes={n: e for n, _, e in tests},
                                     linked_tests=sorted(linked & {n for n, _, _ in tests})))
        bundle = uc.FileActionBundle(bundle_id=f"bundle-{uuid.uuid4().hex[:8]}", actions=actions,
                                     tests=[n for n, _, _ in tests], new_tests=[n for n, _, _ in new_tests],
                                     characterization_tests=[n for n, _, e in tests if e == "E8"],
                                     observations=re.findall(r'UT_OBSERVE_\w+\(\s*"([^"]+)"', suite),
                                     expectation_changes=changes, notes=notes,
                                     trial={n: w for n, w in trial.items() if n in {x for x, _, _ in tests}},
                                     trial_targets={n: g for n, g in trial_targets.items() if n in trial},
                                     made={n: m for n, m in made.items() if n in {x for x, _, _ in tests}},
                                     recipes=recipes, needs_kind=needs_kind, dropped=dropped)
        bundle.validation = self.validate(bundle, plan)
        return bundle

    # ---------------------------------------------------------------- building blocks [NC-1, NC-2, NC-3]
    def _confirm_blocks(self, existing_text: str, notes: list[str]) -> None:
        """NC-2: a block whose test is part of the suite Agent3 measured (a test that did not compile is left out of
        it, A3-CL12) was accepted by the compiler: it is stored as a project recipe."""
        for mark, (rule, key, raw) in list(self._pending_blocks.items()):
            if mark not in existing_text:
                continue
            del self._pending_blocks[mark]
            if self.resolutions is None:
                continue
            recipe = uc.Recipe.model_validate_json(raw)
            stored = next(iter(self.resolutions.get(self.name, rule, key)), "")
            if stored:
                with_uses = uc.Recipe.model_validate_json(stored)
                recipe.uses = with_uses.uses
            recipe.uses += 1
            self.resolutions.put(self.name, rule, key, recipe.model_dump_json())
            notes.append(f"recipe stored after the compiler accepted it: {recipe.kind} {recipe.signature} - used for "
                         f"{recipe.uses} item(s) [NC-2]")

    def _recipe(self, kind: str, signature: str, params: dict[str, str]) -> uc.Recipe | None:
        """NC-2, NC-3: a stored project recipe first, then the application's library."""
        key = uc.finding_signature(kind, signature)
        if self.resolutions is not None:
            for raw in self.resolutions.get(self.name, uc.RECIPE_RULES[kind], key):
                try:
                    recipe = uc.Recipe.model_validate_json(raw)
                except ValueError:
                    continue
                if recipe.complete(params):
                    return recipe
        library = getattr(self, "_library", None)
        if library is None:
            library = self._library = uc.application_recipes()
        return next((r for r in library if r.kind == kind and r.signature == signature and r.complete(params)), None)

    @staticmethod
    def _block_problem(lines: list[str], call: str = "", fake: bool = False) -> str:
        """BP-4: the guards of a building block - nothing forbidden, no assertion, placed in the test only. Names,
        types and syntax are not predicted here: the compiler decides (NC-8). A fake is a class: its methods may have
        any name, so only what no generated code may contain is refused."""
        text = "\n".join(lines)
        if re.search(r"#\s*include|\bmain\s*\(|\bsystem\s*\(|\bpopen\s*\(|__asm|\basm\b|\bUT_TEST\s*\(", text):
            return "a forbidden construct"
        if not fake and FORBIDDEN.search(text):
            return "a forbidden construct"
        if re.search(r"\bUT_(EXPECT|OBSERVE|ASSERT)", text):
            return "a building block must not assert or observe anything"
        if not fake and not call.strip():
            return "the call target is empty"
        return ""

    async def _building_block(self, fn: uc.FunctionInfo, plan: uc.UTPlan, kind: str, req: uc.GenerationRequest,
                              notes: list[str]) -> tuple[tuple[list[str], str] | None, str, list[tuple[str, str, str, str]],
                                                         str]:
        """NC-1: the object a member function is called on, as a building block: a stored recipe first (NC-2), then
        the rule (A2-21ae), then the brain within its profile (BP-2: stub for a fake the rules cannot write, setup for
        the object). Returns (block, made_by, pending recipes [(mark, rule, key, json)], kind no brain may give)."""
        layout = plan.layout
        signature, params = uc.construct_of(fn, layout)
        short_cls = uc.class_of(fn).split("::")[-1]
        if not signature or uc.internal_member(fn, layout) or \
                fn.qualified_name.split("::")[-1] in (short_cls, "~" + short_cls):    # a constructor needs no object
            return None, "", [], ""
        uc.resolution_project(self, layout)
        journal = req.journal.get(fn.qualified_name) or req.journal.get(fn.name, "")
        headers = self._headers_text(layout)
        made, pending, lines = "", [], []
        short = fn.qualified_name.split("::")[-1]
        cls = uc.class_of(fn)
        ctors = uc.constructors_of(cls, layout)
        ctor = uc.constructor_of(cls, layout)
        # stub: a fake of an injected interface the rules cannot fake (a reference result) [A2-21aa, BP-2]
        for p in (ctor.params if ctor else []):
            base = re.sub(r"\b(const|volatile|struct|class)\b|[&*]", " ", p.base or p.type).strip()
            iface = next((f for f in layout.interfaces if f.name.split("::")[-1] == base.split("::")[-1]), None)
            if iface is None or fake_supported(iface, layout):
                continue
            fake_sig = f"fake of the interface {iface.name}"
            recipe = self._recipe("stub", fake_sig, {})
            if recipe is not None:
                lines += recipe.lines
                made = made or f"recipe {recipe.made_by}"
                continue
            if not await uc.brains_for(self, "stub", "A2.stub"):
                notes.append(f"{fn.qualified_name}: the fake of {iface.name} needs the answer kind stub, which no available "
                             "brain's profile allows [BP-9]")
                return None, "", [], "stub"
            failure = ba.RuleFailure(rule="NC-1 stub", failure=f"no rule writes a fake of {iface.name} (a method "
                                     "returns a reference)", subject=fake_sig, message=journal[:1500])
            answer = await self.resolve(
                failure, task=FAKE_TASK.format(iface=iface.name, fake=fake_name(iface)),
                context=[uc.part("interface", f"INTERFACE {iface.name}:\n" + "\n".join(
                    f"{m.return_type} {m.name}({', '.join(f'{a} {b}' for a, b in m.params)}){m.suffix}"
                    for m in iface.methods)), uc.part("headers", f"HEADERS:\n{headers}", uc.P_EVIDENCE, 2000)],
                output_model=FakeClass, verify=lambda a: self._block_problem([a.code], fake=True), step="A2.stub",
                attempts=self.cfg.rule_attempts, project=layout.repo_root, remember=False,
                need=ba.OutputNeed(items=1, tokens_per_item=600, overhead=200))
            if answer is None:
                notes.append(f"{fn.qualified_name}: no fake of {iface.name} [NC-1]")
                return None, "", [], ""
            code = answer.code.strip()
            key = uc.finding_signature("stub", fake_sig)
            mark = BLOCK_MARK.format(key=key[:12])
            lines += [mark, code]
            made = f"{self.brain_of_call().get('brain', '')} stub"
            pending.append((mark, uc.RECIPE_RULES["stub"], key,
                            uc.Recipe(kind="stub", signature=fake_sig, lines=[code],
                                      made_by=made.split(" ")[0]).model_dump_json()))
        rule = uc.object_recipe(fn, layout)
        if rule is not None and kind != "setup":
            return ((lines + rule[0], rule[1]) if lines else None), made, pending, ""
        recipe = self._recipe("setup", signature, params)
        if recipe is not None and kind != "setup" or recipe is not None and not journal:
            body, call = recipe.instantiate(params)
            return (lines + body, call), f"recipe {recipe.made_by}", pending, ""
        if not await uc.brains_for(self, "setup", "A2.setup"):
            notes.append(f"{fn.qualified_name}: the object needs the answer kind setup, which no available brain's "
                         "profile allows [BP-9]")
            return ((lines + rule[0], rule[1]) if rule and lines else None), made, pending, \
                ("setup" if rule is None else "")                  # with a rule's object the rule is tried [BP-9]
        failure = ba.RuleFailure(rule="NC-1 setup", failure=f"no rule builds the object of {cls}"
                                 + (" that compiles" if rule is not None else ""), subject=signature,
                                 message=journal[:1500])
        context = [uc.part("construct", f"CONSTRUCT: {signature}\nCONSTRUCTORS: " + "; ".join(
                       c.signature for c in ctors[:4]) + f"\nFAKES: {', '.join(fake_name(i) for i in layout.interfaces)}"
                       + (f"\nTHE RULE BUILT (the compiler rejected it): {' '.join(rule[0])}" if rule else "")),
                   uc.part("headers", f"HEADERS:\n{headers}", uc.P_EVIDENCE, 2000)]
        answer = await self.resolve(failure, task=SETUP_TASK.format(method=short, cls=cls), context=context,
                                    output_model=SetupBlock, verify=lambda a: self._block_problem(a.lines, a.call),
                                    step="A2.setup", attempts=self.cfg.rule_attempts, project=layout.repo_root,
                                    remember=False, need=ba.OutputNeed(items=1, tokens_per_item=400, overhead=200))
        if answer is None:
            notes.append(f"{fn.qualified_name}: no object block from the brain [NC-1]")
            return None, made, pending, ""
        body, call = [x.strip() for x in answer.lines if x.strip()], answer.call.strip()
        if not call.endswith(short):
            call = f"{call.rstrip('.')}.{short}" if "." not in call else call
        key = uc.finding_signature("setup", signature)
        mark = BLOCK_MARK.format(key=key[:12])
        brain = self.brain_of_call().get("brain", "")
        tpl_lines, tpl_call = uc.templated(body, call, params)
        pending.append((mark, uc.RECIPE_RULES["setup"], key,
                        uc.Recipe(kind="setup", signature=signature, lines=tpl_lines, call=tpl_call,
                                  made_by=brain).model_dump_json()))
        return (lines + [mark] + body, call), f"{brain} setup", pending, ""

    @staticmethod
    def _shared_problem(text: str) -> str:
        """BP-4: shared set-up of a suite part defines only helpers of the test file (names ut_...), nothing of the SUT,
        and nothing forbidden."""
        if FORBIDDEN.search(text) or re.search(r"\bUT_(EXPECT|OBSERVE)", text):
            return "the shared set-up contains a forbidden construct or a check"
        code = code_only(text)
        names = re.findall(r"(?:^|[;}])\s*(?:static\s+)?(?:const\s+)?[A-Za-z_][\w:<>,\s*&]*?\b([A-Za-z_]\w*)\s*(?:\(|=|;|\[)",
                           code, re.M)
        bad = sorted({n for n in names if not n.startswith("ut_") and n not in ("return", "if", "for", "while")})
        if bad:
            return f"the shared set-up may define only helpers named ut_...: {', '.join(bad[:4])}"
        return ""

    @staticmethod
    def _headers_text(layout: uc.LayoutProfile) -> str:
        root = Path(layout.repo_root)
        return "\n".join(f"// {h}\n" + (root / h).read_text(encoding="utf-8", errors="replace")
                         for h in layout.headers if (root / h).is_file())

    @staticmethod
    def _test_name(sc: uc.Scenario, names: set[str]) -> str:
        base = sc.test_name if re.fullmatch(r"test_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+", sc.test_name or "") else \
            f"test_{_ident(sc.function)}_{'checks_stated_behaviour' if not sc.characterization else 'scenario'}"
        return readable_test_name(base, sc.function, names)                                  # A2-21q

    # ---------------------------------------------------------------- LLM tests
    async def _llm_tests(self, plan: uc.UTPlan, req: uc.GenerationRequest, names: set[str],
                         scenarios: list[uc.Scenario], allowed: set[str]
                         ) -> tuple[list, list[str], set[str], dict[str, str], dict[str, str], dict[str, str]]:
        """Whole tests a full-profile brain writes (A2-21n, A2-21o, BP-2); returns (tests, notes, linked, trial, made, shared): the tests
        refused only by predictions go to a trial (NC-7), ``made`` names the brain and kind of every test (BP-7),
        ``shared`` the checked shared set-up of a suite part (BP-2)."""
        layout = plan.layout
        root = Path(layout.repo_root)
        sut_lines = (root / layout.sut_file).read_text(encoding="utf-8", errors="replace").splitlines()
        headers = self._headers_text(layout)
        _, _, stub_header = render_stubs(plan)
        out, notes, linked = [], [], set()
        by_function: dict[tuple[str, int], list[uc.Scenario]] = {}
        for s in scenarios:
            fn_of = uc.function_of(s.function, layout, s.function_line)
            by_function.setdefault((s.function, fn_of.line_start if fn_of else 0), []).append(s)
        # ARCH-18: the functions' tests are asked for at the same time; every call sees the same names
        # and evidence it would see alone, and the answers are combined below in the order of the
        # functions, with names made unique there (A2-21q).
        done = [0]

        def job(key: tuple[str, int], fn_scenarios: list[uc.Scenario]):
            fn_name = key[0]

            async def run():
                try:
                    return await write()
                finally:                                                                       # VIEW-16
                    done[0] += 1
                    self.emit("stage", data={"stage": "A2.tests", "done": done[0], "of": len(work)},
                              summary=f"UT-Generator: brain tests for {done[0]} of {len(work)} functions "
                                      f"(last: {fn_name})")

            async def write():
                fn = uc.function_of(fn_name, layout, key[1])
                if fn is None or (fn.is_static and not fn.from_frontend):
                    return [], [f"{fn_name}: static or unknown, covered only through its callers"], fn, {}, "", ""
                if uc.internal_member(fn, layout):                                                # A2-21ae
                    return [], [f"{fn_name}: {uc.call_form(fn, layout)}"], fn, {}, "", ""
                call_as = f"Call it as {wrapper_name(fn)}(...): it is static [A2-21k]. " if fn.is_static else ""
                member = (uc.call_form(fn, layout) or "It is a class member: construct an object in the test (see "
                          "HEADERS) and call it on the object.") + " " if fn.member else ""                 # A2-21ae
                params = "; ".join(f"{p.name}: {p.type} ({p.kind}{', may be NULL' if p.nullable else ''})" for p in fn.params)
                source = "\n".join(f"{n:4d}| {sut_lines[n - 1]}" for n in range(fn.line_start, fn.line_end + 1))
                project_text = project_stub_text(fn, layout)                                             # A2-21ak
                examples = req.guidance.examples.get(uc.example_key("A2.tests", fn.qualified_name)) or \
                    req.guidance.examples.get(uc.example_key("A2.tests", fn.name), [])          # KB-25
                evidence: dict[str, uc.EvidenceRef] = {}
                for s in fn_scenarios:
                    for e in s.evidence:
                        evidence.setdefault(e.evidence_id, e)
                scenario_rows = []
                for s in fn_scenarios:
                    row = f"- {s.scenario_id} ({'asserts stated behaviour' if not s.characterization else 'characterization'}"\
                          f"): {s.purpose or s.description}"
                    if s.checked:
                        row += "".join([
                            f" | args: {', '.join(f'{a.name}={a.value}' for a in s.args)}" if s.args else "",
                            f" | setup: {' '.join(s.setup)}" if s.setup else "",
                            "".join(f" | stub {v.stub} returns {', '.join(v.returns)} per call" for v in s.stub_values),
                            "".join(f" | set {v.name} = {v.value}" for v in s.variables),
                            f" | expected return: {s.expected_return}" if s.expected_return else "",
                            "".join(f" | expect {c.expr} == {c.value}" for c in s.expected_after)])
                    else:
                        row += f" | conditions: {'; '.join(s.required_conditions) or '-'} | stubs: {'; '.join(s.stub_hints) or '-'}"
                    scenario_rows.append(row)
                state_text = state_part(fn, layout, sut_lines) if setter_vars(plan) else ""         # A2-21aj
                # ARCH-17: what the tests are built from stays whole; the headers keep at least their
                # beginning; KB examples and the list of names give way first.
                context = [
                    uc.part("facts", "\n".join([
                        f"LANGUAGE: {'C++' if layout.language == 'cpp' else 'C'}",
                        f"FUNCTION UNDER TEST ({layout.sut_file}):\n{source}",
                        f"PARAMETERS: {params or 'none'}. {call_as}{member}",
                        *([("VALUES ONLY THE SUT FILE DEFINES (a test cannot name them - write the value) [A1-PL2a]: "
                            + ", ".join(f"{n} = {v}" for n, v in internal_here))] if (internal_here := [
                                (n, v) for n, v in plan.internal_values.items()
                                if re.search(r"\b" + re.escape(n) + r"\b", source)][:30]) else [])])),
                    # A2-21v: only the controls that exist; without stubs no ut_stub_ pattern at all
                    uc.part("stubs", ("STUB CONTROL (only the names declared below exist; set them before calling; "
                                      "they are reset before every test; an array ..._returns[i] with ..._returns_len "
                                      "gives one return value per call; a clock stub with ..._step returns ..._return "
                                      "plus step per call, so timeouts expire - 0 holds the clock [A2-21x]; a class "
                                      "ut_fake_<Interface> implements an injected interface: declare one, set its "
                                      "<method>_return and pass it to the constructor [A2-21aa]; UT_REG32(address) "
                                      "sets a target register, UT_EXPECT_NO_RETURN(call) tests an endless loop, "
                                      "UT_EXPECT_THROW(call, type) an exception):\n"
                                      f"{stub_header}")
                            if "ut_stub_" in stub_header or "ut_fake_" in stub_header else
                            "STUB CONTROL: none. No function this code calls is replaced by a stub, so no ut_stub_ "
                            "name exists: do not use one. Call the function under test directly."),
                    *([uc.part("project stubs", project_text)] if project_text else []),               # A2-21ak
                    *([uc.part("state", state_text)] if state_text else []),
                    *([uc.part("journal", "JOURNAL (why earlier tests of this function missed; remove the cause) "
                               "[LA-4]:\n" + (req.journal.get(fn.qualified_name) or req.journal.get(fn.name, "")))]
                      if req.journal.get(fn.qualified_name) or req.journal.get(fn.name) else []),
                    *([uc.part("lessons", "LESSONS (verified fixes of the same causes) [LA-8]:\n" + "\n".join(
                        f"- {k.snippet[:400]}" for k in req.guidance.lessons[:4]), uc.P_EXAMPLES)]
                      if req.guidance.lessons else []),
                    *([uc.part("real dependency code", "CODE OF THE REAL DEPENDENCIES IT CALLS (they run in the "
                               "test):\n" + uc.dependency_code(fn, layout), uc.P_EVIDENCE, min_chars=600)]
                      if uc.dependency_code(fn, layout) else []),
                    uc.part("scenarios", "SCENARIOS:\n" + ("\n".join(scenario_rows) or "- none")),
                    uc.part("evidence", "EVIDENCE (expected behaviour):\n" + ("\n".join(
                        f"- {e.evidence_id} {e.evidence_class}{' (linked by the brain)' if e.linked_by else ''} "
                        f"{e.source}:{e.line}: {e.text}" for e in evidence.values()) or
                        "- none (all expected values must be observed)"), uc.P_EVIDENCE),
                    uc.part("headers", f"HEADERS:\n{headers}", uc.P_EVIDENCE, min_chars=2000),
                    uc.part("names", "EXISTING TEST NAMES (do not reuse): " + ", ".join(sorted(names)), uc.P_TRIED),
                    *([uc.part("kb", "KB GUIDANCE (patterns that failed before):\n" + "\n".join(
                        f"- avoid: {k.snippet[:200]} ({k.reason})" for k in req.guidance.negative[:3]), uc.P_EXAMPLES)]
                      if req.guidance.negative else []),
                    *([uc.part("examples", "EXAMPLES (verified tests of similar functions in other code: follow their "
                               "form; their names and values do not exist here and are not evidence) [KB-25]:\n"
                               + "\n".join(f"--- for {e.signature or e.function} ({e.context[:200]}):\n{e.snippet[:1500]}"
                                           for e in examples), uc.P_EXAMPLES)] if examples else []),
                ]
                need = ba.OutputNeed(items=min(self.cfg.max_llm_tests_per_function,
                                               max(len(fn_scenarios), 1)),
                                     tokens_per_item=self.cfg.tokens_per_test, overhead=100, asked=True)   # ARCH-20c
                task = (
                    f"Write at most {need.items} unit tests for {fn.qualified_name} that cover "              # ARCH-11a
                    "the SCENARIOS. Return JSON {\"tests\": [{\"name\", \"scenario_ids\", "
                    "\"purpose\", \"evidence_ids\", \"body\", \"evidence_class\"}]}. name: test_<what>_<behaviour> (at least "
                    f"three words, at most {NAME_LIMIT} characters). purpose: one line. body: statements only, no function "
                    "header, no #include. Use only: the function under test and other functions declared in HEADERS, the "
                    "ut_stub_* variables, and these macros: UT_EXPECT_EQ_INT(actual, expected), UT_EXPECT_TRUE(c), "
                    "UT_EXPECT_FALSE(c), UT_EXPECT_EQ_DBL(a, e, tol), UT_EXPECT_EQ_STR(a, e), UT_OBSERVE_INT(\"id\", expr), "
                    "UT_OBSERVE_DBL(\"id\", expr); for target code also UT_REG32(address) (a register in host "
                    "memory), ut_state_set_<variable>(value) and ut_state_get_<variable>() where listed, ut_asm_value = v (the result of replaced target "
                    "assembly) and, in C++, UT_EXPECT_THROW(statement, type) and UT_OBSERVE_THROW(\"id\", statement). "
                    "When the EVIDENCE states the expected result, assert it, put the "
                    "EVIDENCE ids (as written there, in the form EV-<function>-<n>; never the classes E1-E8) into evidence_ids, write /* EV-... */ after the assertion and set evidence_class to that "
                    "evidence's class (E1-E7). Otherwise record the result with UT_OBSERVE_INT(\"<test name>_<n>\", expr) "
                    "and set evidence_class to E8. Never guess an expected value. Every test contains at least one "
                    "UT_EXPECT_... or UT_OBSERVE_...; in C++ it uses only public members of a class (never a private "
                    "member), and an object is declared once - never again one the set-up already declares.")
                part = any(s.kind == "suite_part" for s in fn_scenarios)                           # BP-2, BP-3
                part_brains = await uc.brains_for(self, "suite_part", "A2.tests") if part else []
                if part and part_brains:
                    task += (" SHARED SET-UP: these tests may share static helper functions and variables in the field "
                             "shared (every name starting with ut_); a test calls them [BP-2].")
                dropped: list[str] = []
                tried: dict[str, str] = {}
                forms: list[str] = []
                check = self._grounding(allowed, names, function=fn.name, dropped=dropped, trial=tried, shared_ok=bool(part_brains), void_names=void_functions(layout),
                                        fixed=forms, qualify=uc.namespace_names(layout, self._headers_text(layout)))
                try:
                    result = await self.generate(task, context, GeneratedTests, need=need, step="A2.tests",
                                                 project=layout.repo_root, check=check,
                                                 allowed=part_brains or None)
                except ba.AgentFault as exc:
                    return [], [f"{fn.qualified_name}: no LLM tests ({exc.code}: {exc.message[:200]})"], fn, \
                        {}, "", ""
                used = self.brain_of_call()
                made_by = f"{used.get('brain', '')}/{used.get('model_id', '')} {'suite_part' if part_brains else 'test'}"
                shared_text = ""
                if part_brains and result.shared.strip():
                    problem = self._shared_problem(result.shared)
                    if problem:
                        dropped.append(f"shared set-up refused: {problem} [BP-4]")
                    else:
                        shared_text = (SHARED_BEGIN.format(function=_ident(fn.qualified_name)) + "\n"
                                       + result.shared.strip() + "\n" + SHARED_END)
                own_notes = [f"{fn.qualified_name}: {x}" for x in forms] + [f"{fn.qualified_name}: {d}" for d in dropped] + \
                    [f"{fn.qualified_name}: {n} refused only by predictions ({w[:120]}); built in a trial [NC-7]"
                     for n, w in tried.items()]
                produced = []
                for t in result.tests[: self.cfg.max_llm_tests_per_function]:
                    ids = [i for i in t.evidence_ids if i in evidence and evidence[i].evidence_class != "E8"]
                    if "UT_OBSERVE" in t.body:
                        cls, is_linked = "E8", False
                    elif ids:
                        cls = min(evidence[i].evidence_class for i in ids)
                        is_linked = all(evidence[i].linked_by for i in ids)
                    elif re.fullmatch(r"E[1-7]", t.evidence_class) and any(
                            e.evidence_class == t.evidence_class for e in evidence.values()):
                        cls = t.evidence_class
                        is_linked = all(e.linked_by for e in evidence.values() if e.evidence_class == cls)
                    else:
                        cls, is_linked = "E8", False
                    cited = [i + (f" @{uc.evidence_key(evidence[i])}" if evidence[i].linked_by else "") for i in ids]
                    # what the expected values may come from [A2-21u]
                    stated = [evidence[i] for i in ids] if ids else \
                        [e for e in evidence.values() if e.evidence_class == cls] if cls != "E8" else []
                    texts = [e.text for e in stated]
                    bool_ok = re.sub(r"\s+", "", fn.return_type or "") in ("bool", "_Bool") and \
                        any(e.evidence_class in ("E1", "E2", "E3") for e in stated)
                    purpose = f"{', '.join(t.scenario_ids) or (fn_scenarios[0].scenario_id if fn_scenarios else '')}: " \
                              f"{t.purpose or (fn_scenarios[0].purpose or fn_scenarios[0].description[:100] if fn_scenarios else '')}" \
                              + (f" [{', '.join(cited)}]" if cited else "")
                    produced.append((t, cls, is_linked, purpose, texts, bool_ok))
                return produced, own_notes, fn, tried, made_by, shared_text
            return run
        work = list(by_function.items())
        if len(work) > 1:                                                                          # VIEW-16
            self.emit("stage", data={"stage": "A2.tests", "done": 0, "of": len(work)},
                      summary=f"UT-Generator: the brain writes tests for {len(work)} functions")
        results = await self.in_parallel([job(fn_name, fn_scenarios) for fn_name, fn_scenarios in work])
        trial_out: dict[str, str] = {}
        made_out: dict[str, str] = {}
        shared_out: dict[str, str] = {}
        for result in results:
            if isinstance(result, BaseException):
                raise result
            produced, own_notes, fn, tried, made_by, shared_text = result
            notes.extend(own_notes)
            if shared_text and fn is not None:
                shared_out[_ident(fn.qualified_name)] = shared_text
            reserved = set(names)                     # a name already used is changed [A2-24, A2-21q]
            for t, cls, is_linked, purpose, texts, bool_ok in produced:
                name = readable_test_name(t.name, fn.name, reserved) if t.name in reserved else t.name
                reserved.add(name)
                body, unstated = ground_expectations(t.body, name, texts, bool_ok)                   # A2-21u
                if unstated:
                    notes.append(f"{fn.qualified_name}: {name}: {unstated} expected value(s) not stated by the cited "
                                 "evidence; observed instead of asserted [A2-21u]")
                    if "UT_OBSERVE" in body:
                        cls, is_linked = "E8", False
                out.append((name, render_test(name, body, cls, is_linked, f"{purpose} - by {made_by} [BP-7]"), cls))
                names.add(name)
                if is_linked:
                    linked.add(name)
                if t.name in tried:                                                          # NC-7
                    trial_out[name] = tried[t.name]
                made_out[name] = made_by                                                     # BP-7
        return out, notes, linked, trial_out, made_out, shared_out

    async def _brain_values(self, plan: uc.UTPlan, allowed: set[str], existing_text: str,
                            notes: list[str]) -> dict[tuple[str, int], tuple[list[str], str]]:
        """A2-21ac, ARCH-21: parameters whose value no rule can build are handed to the brain. The answer is checked
        here (declared symbols only, no assertion) and by Agent3's build; it is stored in the resolution memory
        (ARCH-23) only once tests using it are part of the verified suite, so only a value that compiled and ran
        is reused without the brain."""
        layout = plan.layout
        uc.resolution_project(self, layout)
        recipes = getattr(self, "_recipes", None)
        if recipes is None:
            recipes = self._recipes = {}
        for key, (recipe, failure, answer) in list(recipes.items()):           # confirmed by the verified suite
            mark = VALUE_MARK.format(function=key[0], param=key[1])
            if mark in existing_text and self.resolutions is not None and key not in self._confirmed:
                self.resolutions.put(self.name, failure.rule, failure.signature(), answer)
                self._confirmed.add(key)
        root = Path(layout.repo_root)
        sut_lines = uc.read_source(root / layout.sut_file).splitlines() if (root / layout.sut_file).is_file() else []
        wanted = {s.function for s in plan.scenarios if not s.inputs}
        headers = self._headers_text(layout)
        out: dict[tuple[str, int], tuple[list[str], str]] = {}
        asked = 0
        for fn in layout.functions:
            if fn.name not in wanted or (fn.qualified_name.count("::") and _is_member(fn, layout)):
                continue
            for i, p in enumerate(fn.params):
                if _options(fn, i, p, [0, 1], layout) is not None:
                    continue
                key = (fn.qualified_name, i)
                if key in recipes:
                    out[key] = recipes[key][0]
                    continue
                if asked >= self.cfg.max_brain_values:
                    continue
                asked += 1
                body = "\n".join(sut_lines[fn.line_start - 1: fn.line_end])
                type_name = re.sub(r"\b(const|volatile|struct|union|enum|class)\b|[*&\[\]]", " ", p.type).split()
                where = [m.start() for t in type_name[-1:] for m in re.finditer(r"\b" + re.escape(t) + r"\b", headers)][:3]
                defs = "\n...\n".join(headers[max(0, k - 800): k + 1200] for k in where)
                context = [uc.part("function", body[:6000]),
                           uc.part("parameter", f"{p.name}: {p.type} (after typedefs: {p.canonical or p.type})"),
                           uc.part("declarations of the type", defs[:8000] or "(not found in the headers)",
                                   priority=uc.P_EVIDENCE),
                           uc.part("functions of the SUT", "\n".join(f.signature for f in layout.functions)[:4000],
                                   priority=uc.P_EVIDENCE)]
                failure = ba.RuleFailure(rule="A2-21ac value", failure=f"no rule builds a value of {p.type} for "
                                         f"parameter {p.name} of {fn.name}", subject=p.canonical or p.type)

                def check(answer: ValueRecipe) -> str:
                    text = "\n".join(answer.setup) + "\n" + answer.argument + ";"
                    if not answer.argument.strip():
                        return "the argument expression is empty"
                    if re.search(r"\bUT_(EXPECT|OBSERVE)", text):
                        return "the setup must not assert or observe anything"
                    return self._body_problem(text, allowed | setup_names(answer.setup))

                answer = await self.resolve(failure, task=VALUE_TASK.format(index=i, name=p.name, type=p.type),
                                            context=context, output_model=ValueRecipe, verify=check, step="A2.value",
                                            attempts=self.cfg.rule_attempts, project=layout.repo_root, remember=False,
                                            need=ba.OutputNeed(items=1, tokens_per_item=300, overhead=200))
                if answer is None:
                    notes.append(f"{fn.name}: parameter {p.name} ({p.type}): no rule builds its value and the brain "
                                 "gave none that passed the checks; no rule-based tests for it [A2-21ac]")
                    continue
                recipe = ([VALUE_MARK.format(function=key[0], param=i), *answer.setup], answer.argument)
                recipes[key] = (recipe, failure, answer.model_dump_json())
                out[key] = recipe
        if out:
            notes.append(f"{len(out)} parameter value(s) built by the brain where no rule could [A2-21ac]")
        return out

    @staticmethod
    def _allowed_symbols(plan: uc.UTPlan, headers: str, stub_header: str) -> set[str]:
        allowed = set(HARNESS_MACROS) | SAFE_CALLS | set(uc.harness_api())                   # A2-21ah (c)
        for f in plan.layout.functions:
            allowed.add(f.name.split("::")[-1])
            allowed |= set(f.qualified_name.split("::"))
        allowed |= set(re.findall(r"\b([A-Za-z_]\w*)\b", headers))
        allowed |= set(re.findall(r"\b([A-Za-z_]\w*)\b", stub_header))
        allowed |= {wrapper_name(f) for f in static_functions(plan.layout)}
        allowed |= set(re.findall(r"\b(ut_cb_\w+)\b", "\n".join(callback_helpers(plan.layout))))
        allowed |= {state_setter(v) for v in setter_vars(plan)}                                   # A2-21z
        allowed |= {uc.state_getter_name(v) for v in setter_vars(plan)}
        allowed |= {e[0] for e in uc.element_setters(plan.layout)}                    # structured scenarios
        allowed |= {"ut_member", "memcpy"} | {uc.member_tag(m) for m in plan.layout.class_members}
        allowed |= {uc.HIDDEN_MARK + n for n in uc.hidden_state(plan.layout, headers)}             # A2-21ai
        allowed |= {c.name for ps in (plan.layout.dependencies.project_stubs if plan.layout.dependencies else [])
                    for c in ps.controls}                                                         # A2-21ak
        return allowed

    @staticmethod
    def _body_problem(body: str, allowed: set[str], function: str = "") -> str:
        """A2-20, A2-21: only symbols that exist here, no forbidden constructs, balanced."""
        if FORBIDDEN.search(body):
            return "forbidden construct in the body"
        calls = {m.group(1) for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", code_only(body))} - {
            "if", "for", "while", "switch", "return", "sizeof", "catch"}
        named = uc.name_problem(code_only(body), calls, allowed, function)           # a guard [A2-21ai, NC-6]
        if named:
            return named
        unknown = sorted(c for c in calls if c not in allowed)
        if unknown:
            return f"calls functions that do not exist here: {', '.join(unknown[:5])} [A2-20]"
        arity = uc.macro_arity_problem(code_only(body))                                           # A2-21ah (a)
        if arity:
            return arity
        form = uc.c_form_problem(body)                  # a fact: a test that is not well-formed C cannot compile
        if form:
            return form
        return ""

    @classmethod
    def _grounding(cls, allowed: set[str], existing_names: set[str], function: str = "",
                   dropped: list[str] | None = None, trial: dict[str, str] | None = None, shared_ok: bool = False,
                   void_names: set[str] | None = None, fixed: list[str] | None = None,
                   qualify: dict[str, str] | None = None):
        """The check of one LLM answer, test by test [A2-21t]: names are repaired (A2-21q), a test whose body
        fails a check is dropped and noted in ``dropped``; the brain is asked again only when none is left. With
        ``trial``, a test refused only by a prediction is kept and named there: the tool decides (NC-6, NC-7).
        ``shared_ok``: helpers of the shared set-up (ut_...) count as known names (BP-2)."""

        def check(parsed: GeneratedTests) -> None:
            seen: set[str] = set()
            kept: list[GeneratedTest] = []
            problems: list[str] = []
            for t in parsed.tests:
                name = readable_test_name(t.name, function, existing_names | seen)
                if name != t.name:
                    t = t.model_copy(update={"name": name})
                body, changed = uc.void_values_as_statements(t.body, void_names or set())        # A2-21ah (b)
                if changed:
                    t = t.model_copy(update={"body": body})
                    if fixed is not None:
                        fixed.append(f"{t.name}: form fixed: {', '.join(changed[:3])} returns nothing, so it is "
                                     "called as a statement [ARCH-20, A2-21ah]")
                body, named = uc.qualify_names(t.body, qualify or {})                           # A2-21ai
                if named:
                    t = t.model_copy(update={"body": body})
                    if fixed is not None:
                        fixed.append(f"{t.name}: form fixed: {', '.join(named[:3])} written with its namespace "
                                     "[ARCH-20, A2-21ai]")
                helpers = set(re.findall(r"\b(ut_\w+)\s*\(", parsed.shared)) if shared_ok else set()
                problem = cls._body_problem(t.body, allowed | setup_names([t.body]) | helpers, function)
                if problem and trial is not None and uc.check_kind(problem) == "prediction" and \
                        CHECKS.search(code_only(t.body)):                                    # NC-7
                    trial[t.name] = problem
                    problem = ""
                if not problem and not CHECKS.search(code_only(t.body)):
                    problem = ("checks nothing: no UT_EXPECT_... or UT_OBSERVE_... in the body - assert the expected "
                               "value or observe the result [A2-21t]")
                if problem:
                    problems.append(f"{t.name}: {problem}")
                    if trial is not None:
                        trial.pop(t.name, None)
                    continue
                seen.add(t.name)
                kept.append(t)
            if not kept:
                raise ba.AgentFault("MODEL_OUTPUT_INVALID", "no test passes the checks: " + "; ".join(problems[:3]))
            parsed.tests = kept
            if dropped is not None:
                dropped[:] = [f"dropped {p} [A2-21t]" for p in problems]
        return check

    # ---------------------------------------------------------------- structural validation [A2-28, A2-29]
    @staticmethod
    def validate(bundle: uc.FileActionBundle, plan: uc.UTPlan) -> list[str]:
        layout = plan.layout
        checks = []
        paths = [a.path for a in bundle.actions]
        if len(paths) != len(set(paths)):
            raise ba.AgentFault("CONTRACT_VIOLATION", "two actions write the same file [A2-22]")
        writable = (layout.writable_test_dir.rstrip("/") + "/", layout.writable_stub_dir.rstrip("/") + "/")
        for a in bundle.actions:
            if ".." in Path(a.path).parts or a.path.startswith("/") or not a.path.startswith(writable):
                raise ba.AgentFault("CONTRACT_VIOLATION", f"{a.path} is outside the writable test/stub directories")
            if not a.content.strip():
                raise ba.AgentFault("CONTRACT_VIOLATION", f"{a.path} is empty")
        checks.append("paths inside writable test/stub directories")
        suite = next(a for a in bundle.actions if a.role == "testsuite")
        names = re.findall(r"UT_TEST\(\s*(\w+)\s*\)", suite.content)
        cases = re.findall(r"UT_CASE\(\s*(\w+)\s*\)", suite.content)
        if len(names) != len(set(names)) or sorted(names) != sorted(cases):
            raise ba.AgentFault("CONTRACT_VIOLATION", "duplicate tests or registration mismatch [A2-24]")
        form = uc.c_form_problem(suite.content)  # every test passed the same check, so only a generator fault [A2-24]
        if form:
            raise ba.AgentFault("CONTRACT_VIOLATION", f"the suite is {form}")
        checks += ["single writer per file", "no duplicate tests", "every test registered", "balanced suite"]
        return checks




def _assertions(source: str) -> list[str]:
    return [m.group(0) for m in re.finditer(r"\b(?:UT_)?EXPECT_\w+\s*\([^;]*\)\s*;", source)]


def redundant_tests(candidates: list[str], test_items: dict[str, list[str]]) -> list[str]:
    """Greedy set cover: candidates (new characterization tests) that add no covered item or
    MC/DC vector beyond the other tests are redundant [A2-21i]. Tests that are not candidates are kept."""
    if not test_items or not candidates:
        return []
    covered = set()
    for name, items in test_items.items():
        if name not in candidates:
            covered |= set(items)
    keep, remaining = set(), [c for c in candidates if c in test_items]
    while remaining:
        best = max(remaining, key=lambda n: (len(set(test_items[n]) - covered), -remaining.index(n)))
        gain = set(test_items[best]) - covered
        if not gain:
            break
        keep.add(best)
        covered |= gain
        remaining.remove(best)
    return [c for c in candidates if c in test_items and c not in keep]


def drop_tests(bundle: uc.FileActionBundle, names: list[str]) -> uc.FileActionBundle:
    if not names:
        return bundle
    return uc.without_tests(bundle, names, f"{len(names)} redundant characterization test(s) removed: "
                                           "they added no coverage", why="redundant: no new coverage")


def finalize_bundle(bundle: uc.FileActionBundle, observed: dict[str, str],
                    test_items: dict[str, list[str]] | None = None,
                    crashed: dict[str, str] | None = None, ran: list[str] | None = None,
                    build_message: str = "") -> uc.FileActionBundle:
    """UT_OBSERVE_*("id", expr) -> a characterization assertion with the observed value [A2-21c].
    With per-test coverage from Agent3, new characterization tests that add nothing are dropped."""
    observed = dict(observed)
    repaired = {change.test for change in bundle.expectation_changes}  # REC-7: preserve the repair's witness
    candidates = [t for t in bundle.new_tests if t in set(bundle.characterization_tests) and t not in repaired]
    bundle = drop_tests(bundle, redundant_tests(candidates, test_items or {}))
    out = bundle.model_copy(deep=True)
    macro = {"INT": "UT_EXPECT_EQ_INT({e}, {v})", "DBL": "UT_EXPECT_EQ_DBL({e}, {v}, 1e-9)",
             "STR": "UT_EXPECT_EQ_STR({e}, {v})", "BOOL": "UT_EXPECT_EQ_INT(({e}) ? 1 : 0, {v})"}
    pattern = re.compile(r'UT_OBSERVE_(INT|DBL|STR|BOOL)\(\s*"([^"]+)"\s*,\s*(.*)\)\s*;')
    thrown = re.compile(r'UT_OBSERVE_THROW\(\s*"([^"]+)"\s*,\s*(.*)\)\s*;\s*$')
    missing = []
    for action in out.actions:
        if action.role != "testsuite":
            continue
        lines = []
        for line in action.content.split("\n"):
            t = thrown.search(line)
            if t:                                       # A3-45e: an exception or none, as observed [A2-21f]
                tid, stmt = t.groups()
                inner = pattern.search(stmt + ";")
                call = inner.group(3).strip() if inner else stmt.strip()
                kind = observed.get(tid)
                if kind is None or kind == "none":
                    line = line[: t.start()] + stmt.strip() + ";"
                    if kind is None:
                        missing.append(tid)
                elif re.fullmatch(r"std::[A-Za-z_][\w:]*", kind):
                    for oid in ([inner.group(2)] if inner else []):
                        observed.pop(oid, None)
                    lines.append(line[: t.start()] + f"UT_EXPECT_THROW({call}, {kind});")
                    continue
                else:                                   # a type the test cannot name: any exception
                    lines.append(line[: t.start()] + "UT_EXPECT_ANY_THROW(" + call + ");  /* observed: " +
                                 kind.replace("*/", "") + " */")
                    continue
            m = pattern.search(line)
            if not m:
                lines.append(line)
                continue
            kind, oid, expr = m.groups()
            if oid not in observed:
                missing.append(oid)
                # (void): a bare `ns::f(&x);` would be read by C++ as the declaration of a reference x [A2-21ad]
                lines.append(line[: m.start()] + f"(void)({expr});  /* not observed: test did not reach this point */")
                continue
            value = observed[oid]
            if kind == "STR":
                value = json.dumps(value)
            elif kind == "DBL" and value in ("inf", "-inf", "nan"):
                lines.append(line[: m.start()] + f"{expr};  /* observed {value}: not asserted */")
                continue
            lines.append(line[: m.start()] + macro[kind].format(e=expr.strip(), v=value) + ";")
        action.content = "\n".join(lines)
    out.observations = []
    if missing:
        out.notes.append(f"{len(missing)} observation(s) not reached: {', '.join(missing[:5])}")
    # A2-21f: a new characterization test none of whose observations was reached (the function crashed or did not
    # return) asserts nothing and would fail every verify: it is left out, and its call is reported
    suites = [a.content for a in out.actions if a.role == "testsuite"]
    sources = {name: src for text in suites for name, src in uc.test_sources(text).items()}
    unreached, not_run = [], []
    for name in set(out.new_tests) & set(out.characterization_tests):
        src = sources.get(name, "")
        ids = re.findall(r"/\* not observed: test did not reach this point \*/", src)
        if ran is not None and name not in set(ran) and name not in (crashed or {}):
            if ids and not re.search(r"\bUT_EXPECT", src):
                not_run.append(name)       # it did not run - the build failed: not a crash, no crashing input [A2-21f]
        elif (ids and not re.search(r"\bUT_EXPECT", src)) or name in (crashed or {}):
            unreached.append(name)         # a test that crashed in the observe run crashes the next build too
    if not_run:
        out = uc.without_tests(out, sorted(not_run), f"{len(not_run)} characterization test(s) left out: they did not "
                               "run because the observe build failed (test_did_not_compile, not a crash; their items "
                               "are planned again)" + (f": {build_message[:300]}" if build_message else "")
                               + " [A2-21f]", why="did not run: the observe build failed")
    if unreached:
        out.dropped.update({n: ("timeout" if "timeout" in (crashed or {}).get(n, "") else "crashed")
                            if n in (crashed or {}) else "not observed: no observation was reached" for n in unreached
                            if n in set(out.tests)})
        calls = [m.group(1).strip() for n in sorted(unreached)
                 for m in [re.search(r"\(void\)\((.*)\);\s*/\* not observed", sources[n]) or
                           re.search(r"UT_EXPECT_\w+\(\s*\(?((?:[\w:]+)\s*\(.*?\))", sources[n])] if m]
        out.crashed_calls = list(dict.fromkeys(out.crashed_calls + calls))[:40]           # facts for the brain [A2-21f]
        out = uc.without_tests(out, sorted(unreached), f"{len(unreached)} characterization test(s) left out: the "
                               "function crashed or did not return before any observation (input that crashes the SUT, "
                               f"characterization only), e.g. {'; '.join(calls[:3])} [A2-21f]"[:600])
    out.characterization_tests = sorted(set(out.characterization_tests))
    return out


def void_functions(layout: uc.LayoutProfile) -> set[str]:
    """A2-21ah (b): the names of the SUT's functions and methods that return void."""
    return {f.qualified_name.split("::")[-1] for f in layout.functions
            if re.sub(r"\b(static|inline|virtual|extern|constexpr)\b", "", f.return_type or "").strip() == "void"
            and not f.qualified_name.split("::")[-1].startswith("~")}


def build_agent(settings: ba.Settings, config_path: str | None = None) -> GeneratorAgent:
    section = {}
    if config_path:
        section = json.loads(Path(config_path).read_text(encoding="utf-8")).get("generator", {})
    agent = GeneratorAgent(settings, config=GeneratorConfig(**section))
    agent.packs = uc.knowledge_packs(config_path)                   # KP-3
    agent.resolutions = uc.resolution_memory(config_path)           # ARCH-23
    agent.answer_cache = uc.answer_cache(config_path)              # CA-2
    uc.apply_profiles(agent, config_path)                         # BP-1, BP-3, BP-6
    return agent


if __name__ == "__main__":
    _config = next((sys.argv[i + 1] for i, a in enumerate(sys.argv[:-1]) if a == "--config"), None)
    raise SystemExit(ba.agent_main(lambda s: build_agent(s, _config), "agent_unittest_generator (Agent2)"))
