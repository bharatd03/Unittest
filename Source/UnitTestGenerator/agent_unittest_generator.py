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
                  "EXPECT_STREQ", "EXPECT_NEAR"}
SAFE_CALLS = {"sizeof", "strlen", "strcmp", "strncmp", "memset", "memcpy", "memcmp", "strcpy", "strncpy",
              "snprintf", "abs", "labs", "fabs", "static_cast", "const_cast", "reinterpret_cast", "int", "long",
              "unsigned", "char", "double", "float", "bool", "short"}
FORBIDDEN = re.compile(r"#\s*include|\bmain\s*\(|\bsystem\s*\(|\bpopen\s*\(|\bexec\w*\s*\(|\bfopen\s*\(|\bremove\s*\(|"
                       r"\bunlink\s*\(|\bwhile\s*\(\s*(1|true)\s*\)|\bfor\s*\(\s*;\s*;\s*\)|\bUT_TEST\s*\(|__asm|\basm\b")
INTEGRAL = re.compile(r"^(const\s+)?(unsigned\s+|signed\s+)?(int|long|long long|short|char|size_t|uint\d+_t|int\d+_t|"
                      r"bool|_Bool|unsigned|long int|unsigned long|unsigned int)$")
FLOATING = re.compile(r"^(const\s+)?(float|double|long double)$")


class GeneratorConfig(BaseModel):
    mode: str = "deterministic_then_llm"        # deterministic_then_llm | llm | deterministic
    max_auto_tests_per_function: int = 24
    max_llm_tests_per_function: int = 8
    # Expected answer per test, in tokens: sets the answer limit and window [ARCH-11]
    tokens_per_test: int = 380


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


# ============================================================================ source helpers
def stub_names(spec: uc.StubSpec) -> dict[str, str]:
    base = f"ut_stub_{uc.stub_base(spec)}"
    return {"return": f"{base}_return", "calls": f"{base}_calls", "returns": f"{base}_returns",
            "returns_len": f"{base}_returns_len"}


def sequence_type(storage: str, layout: uc.LayoutProfile) -> bool:
    """A stub of this return type can return one value per call [A2-21g]: scalars, enums, pointers."""
    s = re.sub(r"\b(enum|const|volatile)\b", "", storage).strip()
    return bool(INTEGRAL.match(s) or FLOATING.match(s) or s.endswith("*") or
                any(e.name.split("::")[-1] == s.split("::")[-1] for e in layout.enums))


def _value_type(ptype: str) -> str | None:
    t = ptype.replace("&&", "").strip()
    if t.endswith("&"):
        t = t[:-1].strip()
    t = re.sub(r"^const\s+", "", t)
    if "[" in t or t in ("void", "...") or "(*)" in t:
        return None
    return t


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
    headers = sorted({include_name(s.header, layout) for s in plan.stubs if s.header})
    decl_lines, def_lines, reset = [], [], []

    def clear(var: str, vtype: str) -> None:
        if cpp:
            reset.append(f"    {var} = {{}};" if not re.search(r"\[|\(\*\)", vtype) else
                         f"    memset((void *)&{var}, 0, sizeof {var});")
        else:
            reset.append(f"    memset((void *)&{var}, 0, sizeof {var});")

    for spec in plan.stubs:
        target = spec.qualified_name or spec.function
        if spec.kind == "variable":
            definition = spec.var_type or f"int {target}"
            if spec.is_const:
                def_lines.append(f"/* const: its value is fixed for the whole test program [A2-21j] */\n"
                                 f"{definition} = {spec.initializer or '{0}'};")
            else:
                def_lines.append(f"{definition};")
                if not re.search(r"\b(class|std::)", definition):
                    reset.append(f"    memset((void *)&{target}, 0, sizeof {target});")
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
        params, stores = [], []
        for index, (ptype, pname) in enumerate(spec.params):
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
        arglist = ", ".join(params) if params else ("void" if not cpp else "")
        if spec.kind == "constructor":
            signature = f"{target}({arglist})"
        else:
            signature = f"{ret} {target}({arglist}){spec.method_suffix}"
        body = [f"    ++{names['calls']};", *stores]
        if seq:
            body.append(f"    if ({names['calls']} <= {names['returns_len']}) return {names['returns']}[{names['calls']} - 1];")
        if spec.kind != "constructor" and ret and ret != "void":
            body.append(f"    return {names['return']};")
        def_lines.append(signature + "\n{\n" + "\n".join(body) + "\n}\n")
    if layout.sut_state:                     # A2-21s: the SUT's state as before the first test
        decl_lines.append(('extern "C" ' if cpp else "") + "void ut_sut_reset(void);")
        reset.insert(0, "    ut_sut_reset();")
    guard = "UT_STUBS_H"
    header = ["/* ut_stubs.h - generated by agent_unittest_generator. Control variables of the stubs. */",
              f"#ifndef {guard}", f"#define {guard}", *[f'#include "{h}"' for h in headers],
              "#define UT_STUB_SEQUENCE 16", *decl_lines,
              f"#endif /* {guard} */", ""]
    source = ["/* ut_stubs - generated by agent_unittest_generator from the declarations [A2-21g]. */",
              "#include <string.h>", '#include "ut_harness.h"', '#include "ut_stubs.h"', "", *def_lines,
              ('extern "C" ' if cpp else "") + "void ut_before_each(void)", "{", *reset, "}", ""]
    return f"{stub_dir}/ut_stubs.{ext}", "\n".join(source), "\n".join(header)


def static_functions(layout: uc.LayoutProfile) -> list[uc.FunctionInfo]:
    return [f for f in layout.functions if f.is_static and f.from_frontend]


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
    if not statics and not layout.sut_state:
        return None
    ext = "cpp" if layout.language == "cpp" else "c"
    test_dir = layout.writable_test_dir.rstrip("/")
    rel = os.path.relpath(layout.sut_file, test_dir).replace("\\", "/")
    rows = [f"/* ut_sut_access.{ext} - generated by agent_unittest_generator. It includes the SUT so that its static",
            "   functions can be tested; build it instead of the SUT itself, never together with it [A2-21k]. */",
            f'#include "{rel}"', ""]
    for fn in statics:
        callee = fn.qualified_name.replace("(anonymous namespace)::", "")
        args = ", ".join(p.name or f"a{i}" for i, p in enumerate(fn.params))
        call = f"{callee}({args})"
        rows += [wrapper_signature(fn), "{", f"    {'return ' if (fn.return_type or 'void') != 'void' else ''}{call};", "}", ""]
    if layout.sut_state:                                                                     # A2-21s
        rows.append(uc.sut_reset_source(layout.sut_state, layout.language == "cpp"))
    return f"{test_dir}/ut_sut_access.{ext}", "\n".join(rows)


SUITE_TEST = re.compile(r"(?:/\*\s*evidence:\s*(E\d)(\s+linked)?\s*\*/\s*\n)?UT_TEST\(\s*(\w+)\s*\)\s*\{", re.M)


def parse_suite(text: str) -> dict[str, dict[str, str]]:
    """Tests of an existing generated suite: name -> {"source", "evidence"} (verbatim)."""
    tests = {}
    for m in SUITE_TEST.finditer(text):
        depth, i = 0, m.end() - 1
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
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


def assemble_suite(plan: uc.UTPlan, tests: list[tuple[str, str, str]], stub_header: str | None) -> str:
    """tests: (name, full test source incl. evidence comment, evidence class)."""
    layout = plan.layout
    sut_headers = [include_name(h, layout) for h in layout.headers]
    declared = set()
    root = Path(layout.repo_root)
    for h in layout.headers:
        if (root / h).is_file():
            declared |= set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", (root / h).read_text(encoding="utf-8", errors="replace")))
    prototypes = [f.signature.rstrip("{ ") + ";" for f in layout.functions
                  if not f.is_static and "::" not in f.qualified_name and f.name not in declared and f.signature]
    lines = [f"/* {Path(layout.suite_file).name} - generated by agent_unittest_generator for {layout.sut_file}.",
             "   Every test states the strongest evidence class behind its assertions [A2-21d]. */",
             '#include "ut_harness.h"']
    if stub_header:
        lines.append('#include "ut_stubs.h"')
    lines += [f'#include "{h}"' for h in sut_headers if h != "ut_stubs.h"]
    if render_sut_access(plan) is not None:                     # A2-21k
        prototypes += [wrapper_signature(f) + ";" for f in static_functions(layout)]
    helpers = callback_helpers(layout)
    extra = ["#include <stddef.h>"] + (["#include <stdbool.h>"] if layout.language == "c" and any(
        p.kind == "bool" for f in layout.functions for p in f.params) else [])
    lines += ["#include <string.h>", *extra, ""] + prototypes + ([""] if prototypes else []) + \
        helpers + ([""] if helpers else [])
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
            name = f"test_{fn.name}_auto_{index}"
            if name in existing:
                continue
            args = ", ".join(_c_literal(v) for v in combo[: len(dims)])
            lines = [f"{stub_names(s)['return']} = {_c_literal(v)};" for s, v in zip(controlled, combo[len(dims):])]
            call = f"{fn.name}({args})"
            lines.append(f'{observe}("{name}_r", {call});' if observe else f"{call};")
            out.append((name, render_test(name, "\n".join(lines), "E8"), "E8"))
    return out


def _observer(ret: str, layout: uc.LayoutProfile) -> str | None:
    """The observation macro for a return type; "" for void, None when nothing can be observed."""
    r = re.sub(r"\b(const|volatile|static|inline|extern|virtual)\b", "", ret or "void").strip()
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
             body: str = "", macros: dict[str, str] | None = None):
    """(setup lines, argument, observe-after lines) choices for one parameter, or None."""
    cpp = layout.language == "cpp"
    null = "nullptr" if cpp else "NULL"
    small = sorted(set(values), key=lambda v: (abs(v), v))[:5]
    t = re.sub(r"\b(const|volatile)\b", "", p.type).strip()
    if p.kind == "int":
        cast = "" if t in ("int", "long", "long long") else f"({t})"
        return [([], f"{cast}{v}" if v >= 0 or not cast else f"{cast}({v})", []) for v in values]
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
        opts = [([f"{base} ut_v{i} = {cast}{v};"], f"ut_v{i}" if p.by_ref else f"&ut_v{i}",
                 [] if "const" in p.type.split("*")[0] or p.by_ref and "const" in p.type else
                 [f'{observe}("@_o{i}", ut_v{i});']) for v in small]
        return opts + ([([], null, [])] if p.nullable and not p.by_ref else [])
    if p.kind == "cstring":
        return [([], s, []) for s in ('""', '"a"', '"ABCDEFGHIJKLMNOPQRSTUVWXYZ"')] + \
            ([([], null, [])] if p.nullable else [])
    if p.kind == "buffer":
        return [([f"char ut_b{i}[256] = {{0}};"], f"ut_b{i}", [])]
    if p.kind in ("struct", "ptr_struct") and p.fields:
        own = {f.name: field_values(f.name, body, macros or {}) for f in p.fields}           # A2-21r
        dims = [sorted(set(small) | set(own[f.name])) if f.kind in ("int", "enum") else [0, 1] if f.kind == "bool"
                else [float(v) for v in small] if f.kind == "float" else ["{0}"] for f in p.fields]
        inits = ["{" + ", ".join(_c_literal(v) for v in combo) + "}"
                 for combo in combine(dims, max(6, max(len(d) for d in dims)))]
        base = p.base or t.rstrip("*& ").strip()
        ref = p.kind == "struct" or p.by_ref
        opts = [([f"{base} ut_v{i} = {init};"], f"ut_v{i}" if ref else f"&ut_v{i}", []) for init in inits]
        return opts + ([([], null, [])] if p.kind == "ptr_struct" and p.nullable and not p.by_ref else [])
    if p.kind == "callback":
        base = f"ut_cb_{_ident(fn.name)}_{i}"
        if (p.callback_return or "void").strip() == "void":
            return [([], base, [])] + ([([], null, [])] if p.nullable else [])
        return [([f"{base}_return = {v};"], base, []) for v in (0, 1, -1)] + ([([], null, [])] if p.nullable else [])
    return None


def auto_tests(plan: uc.UTPlan, cap: int, existing: set[str]) -> list[tuple[str, str, str]]:
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
        if fn.name not in wanted or (fn.qualified_name.count("::") and _is_member(fn, layout)):
            continue
        observe = _observer(fn.return_type, layout)
        body = "\n".join(sut_lines[fn.line_start - 1: fn.line_end])
        helpers = "\n".join("\n".join(sut_lines[g.line_start - 1: g.line_end]) for g in layout.functions
                            if g.is_static and g.name in fn.calls and g is not fn)                 # A2-21r
        values = literal_values(body + "\n" + helpers, macros)
        options = [_options(fn, i, p, values, layout, body, macros) for i, p in enumerate(fn.params)]
        if any(o is None for o in options) or observe is None:
            continue
        controlled = [stubs[c] for c in fn.calls if c in stubs and stubs[c].return_type not in ("void", "")
                      and (INTEGRAL.match(stubs[c].return_type) or FLOATING.match(stubs[c].return_type))]
        dims = [list(range(len(o))) for o in options] + [_stub_choices(s, body, values, layout) for s in controlled]
        neutral = [[" ".join(o[k][0]) + " " + o[k][1] for k in range(len(o))] for o in options] + dims[len(options):]
        for index, combo in enumerate(combine(dims, cap, neutral), 1):
            name = f"test_{fn.name.replace('::', '_')}_auto_{index}"
            if name in existing:
                continue
            setup, args, after = [], [], []
            for k, choice in enumerate(combo[: len(options)]):
                s, a, o = options[k][choice]
                setup += s
                args.append(a)
                after += [line.replace("@", name) for line in o]
            for s, v in zip(controlled, combo[len(options):]):
                names = stub_names(s)
                if isinstance(v, tuple):                          # a return sequence [A2-21g, A2-21r]
                    setup += [f"{names['returns']}[{k}] = {_c_literal(x)};" for k, x in enumerate(v)]
                    setup += [f"{names['returns_len']} = {len(v)};", f"{names['return']} = {_c_literal(v[-1])};"]
                else:
                    setup.append(f"{names['return']} = {_c_literal(v)};")
            call = f"{callee_of(fn)}({', '.join(args)})"
            lines = setup + [_observe_call(observe, f"{name}_r", _enum_value(fn.return_type, layout, call))] + after
            out.append((name, render_test(name, "\n".join(lines), "E8"), "E8"))
    return out


def _stub_choices(spec: uc.StubSpec, body: str, values: list[int], layout: uc.LayoutProfile) -> list[Any]:
    """Single return values; for a stub the function calls more than once also sequences in which the calls
    return different values (A2-21r)."""
    choices: list[Any] = list(values)
    calls = len(re.findall(r"\b" + re.escape(spec.function.split("::")[-1]) + r"\s*\(", body))
    storage = re.sub(r"^const\s+", "", re.sub(r"\b(extern|static|inline|virtual)\b", "", spec.return_type).strip())
    if calls > 1 and INTEGRAL.match(storage) and sequence_type(storage, layout):
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
    return re.sub(r"\W", "_", name.replace("(anonymous namespace)::", ""))


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


def assemble_scenario(sc: uc.Scenario, plan: uc.UTPlan, name: str) -> str | None:
    """A2-21m: a checked brain scenario whose values are all given becomes a test without an LLM; None
    when it needs code the scenario does not contain (an object, a missing argument)."""
    layout = plan.layout
    fn = next((f for f in layout.functions if sc.function in (f.name, f.qualified_name)), None)
    if fn is None or fn.member or (fn.is_static and not fn.from_frontend):
        return None
    given = {a.name: a.value for a in sc.args}
    names = param_names(fn)
    if names is None or set(given) != set(names):
        return None
    stubs: dict[str, uc.StubSpec] = {}
    for s in plan.stubs:
        stubs[s.function] = s
        stubs[s.qualified_name or s.function] = s
    lines = []
    for sv in sc.stub_values:
        spec = stubs.get(sv.stub)
        if spec is None or not sv.returns:
            return None
        controls = stub_names(spec)
        if len(sv.returns) == 1:
            lines.append(f"{controls['return']} = {sv.returns[0]};")
        else:
            storage = re.sub(r"^const\s+", "", re.sub(r"\b(extern|static|inline|virtual)\b", "",
                                                       spec.return_type).strip().rstrip("&").strip())
            if not sequence_type(storage, layout) or len(sv.returns) > 16:
                return None
            lines += [f"{controls['returns']}[{i}] = {v};" for i, v in enumerate(sv.returns)]
            lines += [f"{controls['returns_len']} = {len(sv.returns)};", f"{controls['return']} = {sv.returns[-1]};"]
    lines += [f"{v.name} = {v.value};" for v in sc.variables]
    lines += [s.strip() if s.strip().endswith((";", "}")) else s.strip() + ";" for s in sc.setup if s.strip()]
    call = f"{callee_of(fn)}({', '.join(given[n] for n in names)})"
    full = return_of(fn)
    ret = re.sub(r"\b(const|volatile|static|inline|extern|virtual)\b", "", full or "void").strip()
    if not sc.characterization and sc.expected_return:
        lines.append(_assert_return(fn, call, sc.expected_return))
    elif ret == "void" or not ret:
        lines.append(f"{call};")
    else:
        observe = _observer(full, layout)
        lines.append(_observe_call(observe, f"{name}_r", _enum_value(full, layout, call)) if observe is not None
                     else f"{call};")
    if not sc.characterization:
        lines += [_assert_value(c.expr, c.value) for c in sc.expected_after]
    return "\n".join(lines)


def setup_names(statements: list[str]) -> set[str]:
    """Local variables a test body declares (function pointers among them may be called)."""
    names = set()
    for stmt in statements:
        names |= set(re.findall(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)\s*\(", stmt))
    return names


def _has_auto(function: str, auto_names: list[str], layout: uc.LayoutProfile) -> bool:
    """Did the deterministic generator write a test for this function in this bundle [A2-21o]?"""
    fn = next((f for f in layout.functions if function in (f.name, f.qualified_name)), None)
    if fn is None:
        return False
    prefixes = [f"test_{fn.name.replace('::', '_')}_auto_", f"test_{_ident(fn.name)}_auto_"]
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
            fn = next((f for f in layout.functions if inp.function in (f.name, f.qualified_name)), None)
            if fn is None:
                continue
            k = 1
            while f"test_{fn.name.replace('::', '_')}_fuzz_{k}" in existing:
                k += 1
            name = f"test_{fn.name.replace('::', '_')}_fuzz_{k}"
            existing.add(name)
            observe = _observer(fn.return_type, layout)
            call = f"{callee_of(fn)}({', '.join(inp.args)})"
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


def auto_class_tests(plan: uc.UTPlan, cap: int, existing: set[str]) -> list[tuple[str, str, str]]:
    """C++: characterization tests for classes whose constructor and methods take scalars [A2-21h].
    Each test constructs an object, then calls the method four times (stateful behaviour),
    with the stubbed dependencies set before construction and before the calls."""
    layout = plan.layout
    root = Path(layout.repo_root)
    sut_lines = (root / layout.sut_file).read_text(encoding="utf-8", errors="replace").splitlines()
    stubs = {s.function: s for s in plan.stubs}
    wanted = {s.function for s in plan.scenarios}
    by_class: dict[str, list[uc.FunctionInfo]] = {}
    for fn in layout.functions:
        if "::" in fn.qualified_name:
            by_class.setdefault(fn.qualified_name.rsplit("::", 1)[0], []).append(fn)
    out = []
    for cls, members in by_class.items():
        short = cls.split("::")[-1]
        ctor = next((f for f in members if f.qualified_name.endswith(f"{short}::{short}")), None)
        ctor_sig = _params(ctor.signature, ctor.name) if ctor else ("", [])
        if ctor_sig is None:
            continue
        for fn in members:
            if fn is ctor or fn.name not in wanted or fn.qualified_name.split("::")[-1].startswith("~"):
                continue
            sig = _params(fn.signature, fn.name)
            if sig is None:
                continue
            ret, params = sig
            body = "\n".join(sut_lines[fn.line_start - 1: fn.line_end])
            ctor_body = "\n".join(sut_lines[ctor.line_start - 1: ctor.line_end]) if ctor else ""
            values = literal_values(body + "\n" + ctor_body, {})
            ctor_dims = _scalar_dims(ctor_sig[1], values)
            dims = _scalar_dims(params, values)
            observe = _observer(ret, layout)
            if ctor_dims is None or dims is None or observe is None:
                continue
            calls = set(fn.calls) | set(ctor.calls if ctor else [])
            controlled = [stubs[c] for c in sorted(calls) if c in stubs and
                          (INTEGRAL.match(stubs[c].return_type) or FLOATING.match(stubs[c].return_type))]
            # value at construction; at the three calls a single value or one value per call [A2-21g, A2-21r]
            steps = [v for v in values if v > 1][-6:]
            at_calls = [list(values) + ([(0, v, 2 * v) for v in steps] if sequence_type(
                re.sub(r"^const\s+", "", st.return_type.strip()), layout) else []) for st in controlled]
            at_ctor = [values for st in controlled if ctor and st.function in ctor.calls]
            stub_dims = at_ctor + at_calls
            combos = combine(ctor_dims + dims + stub_dims, cap)
            method = fn.qualified_name.split("::")[-1]
            for index, combo in enumerate(combos, 1):
                name = f"test_{short}_{method}_auto_{index}"
                if name in existing:
                    continue
                k = len(ctor_dims)
                ctor_args = ", ".join(_c_literal(v) for v in combo[:k])
                args = ", ".join(_c_literal(v) for v in combo[k: k + len(dims)])
                stub_values = combo[k + len(dims):]
                built = [st for st in controlled if ctor and st.function in ctor.calls]
                before = [f"{stub_names(st)['return']} = {_c_literal(v)};" for st, v in zip(built, stub_values)]
                after = []
                for st, v in zip(controlled, stub_values[len(built):]):
                    names = stub_names(st)
                    if isinstance(v, tuple):
                        after += [f"{names['returns']}[{k}] = {_c_literal(x)};" for k, x in enumerate(v)]
                        after += [f"{names['returns_len']} = {len(v)};", f"{names['return']} = {_c_literal(v[-1])};"]
                    else:
                        after.append(f"{names['return']} = {_c_literal(v)};")
                lines = before + [f"{cls} ut_obj{'(' + ctor_args + ')' if ctor_args else ''};"] + after
                for call in range(1, 5):                  # a series of three, then one more call
                    expr = _enum_value(ret, layout, f"ut_obj.{method}({args})")
                    lines.append(_observe_call(observe, f"{name}_r{call}", expr))
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
            return finalize_bundle(payload.bundle, payload.observed, payload.test_items)

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
                rewrite[name] = r                                                      # A2-21p
        kept = [(n, t["source"], t["evidence"]) for n, t in existing.items() if n not in drop]
        linked = {n for n, t in existing.items() if n not in drop and t.get("linked")}
        stub_path, stub_source, stub_header = render_stubs(plan)
        actions = [uc.FileAction(path=stub_path, action="replace" if (root / stub_path).exists() else "create",
                                 role="stub", content=stub_source),
                   uc.FileAction(path=str(Path(stub_path).with_name("ut_stubs.h")).replace("\\", "/"),
                                 action="create", role="helper", content=stub_header)]
        access = render_sut_access(plan)
        if access is not None:
            actions.append(uc.FileAction(path=access[0], action="replace" if (root / access[0]).exists() else "create",
                                         role="sut_access", content=access[1]))
        new_tests: list[tuple[str, str, str]] = []
        names = {n for n, _, _ in kept}
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
                auto = auto_tests(plan, self.cfg.max_auto_tests_per_function, names)
                if layout.language == "cpp":
                    auto += auto_class_tests(plan, self.cfg.max_auto_tests_per_function, names)
                if auto:
                    notes.append(f"{len(auto)} deterministic characterization tests")
            new_tests += auto
            names |= {n for n, _, _ in new_tests}
            allowed = self._allowed_symbols(plan, self._headers_text(layout), stub_header)
            llm_scenarios, assembled = [], 0
            for sc in plan.scenarios:
                if sc.inputs:
                    continue
                if sc.checked:                                                        # A2-21m
                    name = self._test_name(sc, names)
                    body = assemble_scenario(sc, plan, name)
                    problem = self._body_problem(body, allowed | setup_names(sc.setup)) if body is not None else "?"
                    if body is not None and not problem:
                        evidence = sc.strongest_class if not sc.characterization else "E8"
                        new_tests.append((name, render_test(name, body, evidence, sc.linked, scenario_comment(sc)),
                                          evidence))
                        names.add(name)
                        assembled += 1
                        if sc.linked and evidence != "E8":
                            linked.add(name)
                        continue
                    llm_scenarios.append(sc)
                elif not sc.characterization:                                         # A2-21n
                    llm_scenarios.append(sc)
                elif self.cfg.mode == "llm" or not _has_auto(sc.function, [n for n, _, _ in auto], layout):
                    llm_scenarios.append(sc)                                          # A2-21o
            if assembled:
                notes.append(f"{assembled} tests assembled from scenarios the brain planned and Agent1 checked [A2-21m]")
            if self.cfg.mode in ("deterministic_then_llm", "llm") and (llm_scenarios or rewrite):
                llm_tests, llm_notes, llm_linked = await self._llm_tests(plan, req, names, llm_scenarios, rewrite,
                                                                         existing, allowed)
                new_tests += llm_tests
                notes += llm_notes
                linked |= llm_linked
        for name in sorted(drop & set(existing)):
            replacement = next((t for t in new_tests if t[0] == name), None)
            old = existing[name]
            if old["evidence"] != "E8" and replacement is not None and _assertions(old["source"]) != _assertions(replacement[1]):
                changes.append(uc.ExpectationChange(test=name, old="; ".join(_assertions(old["source"])),
                                                    new="; ".join(_assertions(replacement[1])),
                                                    reason="rewritten from Agent3's diagnostic [A2-21p]: " +
                                                    (rewrite[name].diagnostic[:160] if name in rewrite else
                                                     "repair requested by Agent1"), evidence=old["evidence"]))
            elif name in rewrite and replacement is None:
                notes.append(f"{name}: not rewritten; dropped [A2-21p]")
        tests = kept + new_tests
        suite = assemble_suite(plan, tests, stub_header)
        actions.append(uc.FileAction(path=layout.suite_file, action="replace" if existing_text else "create",
                                     role="testsuite", content=suite,
                                     evidence_classes={n: e for n, _, e in tests},
                                     linked_tests=sorted(linked & {n for n, _, _ in tests})))
        bundle = uc.FileActionBundle(bundle_id=f"bundle-{uuid.uuid4().hex[:8]}", actions=actions,
                                     tests=[n for n, _, _ in tests], new_tests=[n for n, _, _ in new_tests],
                                     characterization_tests=[n for n, _, e in tests if e == "E8"],
                                     observations=re.findall(r'UT_OBSERVE_\w+\(\s*"([^"]+)"', suite),
                                     expectation_changes=changes, notes=notes)
        bundle.validation = self.validate(bundle, plan)
        return bundle

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
                         scenarios: list[uc.Scenario], rewrite: dict[str, uc.RepairItem],
                         existing: dict[str, dict[str, str]], allowed: set[str]) -> tuple[list, list[str], set[str]]:
        layout = plan.layout
        root = Path(layout.repo_root)
        sut_lines = (root / layout.sut_file).read_text(encoding="utf-8", errors="replace").splitlines()
        headers = self._headers_text(layout)
        _, _, stub_header = render_stubs(plan)
        out, notes, linked = [], [], set()
        by_function: dict[str, list[uc.Scenario]] = {}
        for s in scenarios:
            by_function.setdefault(s.function, []).append(s)
        rewrites_of: dict[str, list[str]] = {}
        for name in rewrite:                                       # the function a failing test calls [A2-21p]
            fn = next((f for f in layout.functions if re.search(
                r"\b" + re.escape(callee_of(f).split("::")[-1]) + r"\s*\(", existing[name]["source"])), None)
            if fn is not None:
                rewrites_of.setdefault(fn.name, []).append(name)
                by_function.setdefault(fn.name, [])
        # ARCH-18: the functions' tests are asked for at the same time; every call sees the same names
        # and evidence it would see alone, and the answers are combined below in the order of the
        # functions, with names made unique there (A2-21q).
        def job(fn_name: str, fn_scenarios: list[uc.Scenario]):
            async def run():
                fn = next((f for f in layout.functions if fn_name in (f.name, f.qualified_name)), None)
                if fn is None or (fn.is_static and not fn.from_frontend):
                    return [], [f"{fn_name}: static or unknown, covered only through its callers"], fn, []
                to_rewrite = rewrites_of.get(fn.name, [])
                call_as = f"Call it as {wrapper_name(fn)}(...): it is static [A2-21k]. " if fn.is_static else ""
                member = "It is a class member: construct an object in the test (see HEADERS) and call it on the " \
                    "object. " if fn.member else ""
                params = "; ".join(f"{p.name}: {p.type} ({p.kind}{', may be NULL' if p.nullable else ''})" for p in fn.params)
                source = "\n".join(f"{n:4d}| {sut_lines[n - 1]}" for n in range(fn.line_start, fn.line_end + 1))
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
                fixes = [f"- {name}: {rewrite[name].issue}: {rewrite[name].diagnostic[:300]}\n{existing[name]['source']}"
                         for name in to_rewrite]
                # ARCH-17: what the tests are built from stays whole; the headers keep at least their
                # beginning; KB examples and the list of names give way first.
                context = [
                    uc.part("facts", "\n".join([
                        f"LANGUAGE: {'C++' if layout.language == 'cpp' else 'C'}",
                        f"FUNCTION UNDER TEST ({layout.sut_file}):\n{source}",
                        f"PARAMETERS: {params or 'none'}. {call_as}{member}"])),
                    # A2-21v: only the controls that exist; without stubs no ut_stub_ pattern at all
                    uc.part("stubs", ("STUB CONTROL (only the names declared below exist; set them before calling; "
                                      "they are reset before every test; an array ..._returns[i] with ..._returns_len "
                                      f"gives one return value per call):\n{stub_header}")
                            if "ut_stub_" in stub_header else
                            "STUB CONTROL: none. No function this code calls is replaced by a stub, so no ut_stub_ "
                            "name exists: do not use one. Call the function under test directly."),
                    uc.part("scenarios", "SCENARIOS:\n" + ("\n".join(scenario_rows) or "- none")),
                    uc.part("rewrite", "TESTS TO REWRITE (keep the name, the scenario and the evidence; fix what the "
                                       "diagnostic shows):\n" + ("\n".join(fixes) if fixes else "- none")),
                    uc.part("evidence", "EVIDENCE (expected behaviour):\n" + ("\n".join(
                        f"- {e.evidence_id} {e.evidence_class}{' (linked by the brain)' if e.linked_by else ''} "
                        f"{e.source}:{e.line}: {e.text}" for e in evidence.values()) or
                        "- none (all expected values must be observed)"), uc.P_EVIDENCE),
                    uc.part("headers", f"HEADERS:\n{headers}", uc.P_EVIDENCE, min_chars=2000),
                    uc.part("names", "EXISTING TEST NAMES (do not reuse): " + ", ".join(sorted(names)), uc.P_TRIED),
                    uc.part("kb", "KB GUIDANCE:\n" + "\n".join(
                        [f"+ {k.snippet[:300]}" for k in req.guidance.positive[:3]] +
                        [f"- avoid: {k.snippet[:200]} ({k.reason})" for k in req.guidance.negative[:3]]), uc.P_EXAMPLES),
                ]
                need = ba.OutputNeed(items=min(self.cfg.max_llm_tests_per_function,
                                               max(len(fn_scenarios) + len(to_rewrite), 1)),
                                     tokens_per_item=self.cfg.tokens_per_test, overhead=100)
                task = (
                    f"Write at most {need.items} unit tests for {fn.qualified_name} that cover "              # ARCH-11a
                    "the SCENARIOS and rewrite the TESTS TO REWRITE. Return JSON {\"tests\": [{\"name\", \"scenario_ids\", "
                    "\"purpose\", \"evidence_ids\", \"body\", \"evidence_class\"}]}. name: test_<what>_<behaviour> (at least "
                    f"three words, at most {NAME_LIMIT} characters; a rewritten test keeps its name). purpose: one line. body: statements only, no function "
                    "header, no #include. Use only: the function under test and other functions declared in HEADERS, the "
                    "ut_stub_* variables, and these macros: UT_EXPECT_EQ_INT(actual, expected), UT_EXPECT_TRUE(c), "
                    "UT_EXPECT_FALSE(c), UT_EXPECT_EQ_DBL(a, e, tol), UT_EXPECT_EQ_STR(a, e), UT_OBSERVE_INT(\"id\", expr), "
                    "UT_OBSERVE_DBL(\"id\", expr). When the EVIDENCE states the expected result, assert it, put the "
                    "EVIDENCE ids (as written there, for example EV-clamp_percent-1; never the classes E1-E8) into evidence_ids, write /* EV-... */ after the assertion and set evidence_class to that "
                    "evidence's class (E1-E7). Otherwise record the result with UT_OBSERVE_INT(\"<test name>_<n>\", expr) "
                    "and set evidence_class to E8. Never guess an expected value.")
                dropped: list[str] = []
                check = self._grounding(allowed, names - set(to_rewrite), set(to_rewrite), fn.name, dropped)
                try:
                    result = await self.generate(task, context, GeneratedTests, need=need, step="A2.tests",
                                                 project=layout.repo_root, check=check)
                except ba.AgentFault as exc:
                    return [], [f"{fn.qualified_name}: no LLM tests ({exc.code}: {exc.message[:200]})"], fn, to_rewrite
                own_notes = [f"{fn.qualified_name}: {d}" for d in dropped]
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
                    purpose = f"{', '.join(t.scenario_ids) or (fn_scenarios[0].scenario_id if fn_scenarios else 'rewrite')}: " \
                              f"{t.purpose or (fn_scenarios[0].purpose or fn_scenarios[0].description[:100] if fn_scenarios else '')}" \
                              + (f" [{', '.join(cited)}]" if cited else "")
                    produced.append((t, cls, is_linked, purpose, texts, bool_ok))
                return produced, own_notes, fn, to_rewrite
            return run
        work = list(by_function.items())
        results = await self.in_parallel([job(fn_name, fn_scenarios) for fn_name, fn_scenarios in work])
        for result in results:
            if isinstance(result, BaseException):
                raise result
            produced, own_notes, fn, to_rewrite = result
            notes.extend(own_notes)
            for t, cls, is_linked, purpose, texts, bool_ok in produced:
                name = t.name if t.name in to_rewrite else \
                    readable_test_name(t.name, fn.name, names) if t.name in names else t.name
                # A2-21u; a rewritten test keeps its evidence and goes through the oracle-strength check (A2-21p)
                body, unstated = (t.body, 0) if t.name in to_rewrite else \
                    ground_expectations(t.body, name, texts, bool_ok)
                if unstated:
                    notes.append(f"{fn.qualified_name}: {name}: {unstated} expected value(s) not stated by the cited "
                                 "evidence; observed instead of asserted [A2-21u]")
                    if "UT_OBSERVE" in body:
                        cls, is_linked = "E8", False
                out.append((name, render_test(name, body, cls, is_linked, purpose), cls))
                names.add(name)
                if is_linked:
                    linked.add(name)
        return out, notes, linked

    @staticmethod
    def _allowed_symbols(plan: uc.UTPlan, headers: str, stub_header: str) -> set[str]:
        allowed = set(HARNESS_MACROS) | SAFE_CALLS
        for f in plan.layout.functions:
            allowed.add(f.name.split("::")[-1])
            allowed |= set(f.qualified_name.split("::"))
        allowed |= set(re.findall(r"\b([A-Za-z_]\w*)\b", headers))
        allowed |= set(re.findall(r"\b([A-Za-z_]\w*)\b", stub_header))
        allowed |= {wrapper_name(f) for f in static_functions(plan.layout)}
        allowed |= set(re.findall(r"\b(ut_cb_\w+)\b", "\n".join(callback_helpers(plan.layout))))
        return allowed

    @staticmethod
    def _body_problem(body: str, allowed: set[str]) -> str:
        """A2-20, A2-21: only symbols that exist here, no forbidden constructs, balanced."""
        if FORBIDDEN.search(body):
            return "forbidden construct in the body"
        calls = {m.group(1) for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", body)} - {"if", "for", "while", "switch", "return"}
        unknown = sorted(c for c in calls if c not in allowed)
        if unknown:
            return f"calls functions that do not exist here: {', '.join(unknown[:5])} [A2-20]"
        if body.count("{") != body.count("}") or body.count("(") != body.count(")"):
            return "unbalanced braces or parentheses"
        return ""

    @classmethod
    def _grounding(cls, allowed: set[str], existing_names: set[str], rewritten: set[str] | None = None,
                   function: str = "", dropped: list[str] | None = None):
        """The check of one LLM answer, test by test [A2-21t]: names are repaired (A2-21q), a test whose body
        fails a check is dropped and noted in ``dropped``; the brain is asked again only when none is left."""
        rewritten = rewritten or set()

        def check(parsed: GeneratedTests) -> None:
            seen: set[str] = set()
            kept: list[GeneratedTest] = []
            problems: list[str] = []
            for t in parsed.tests:
                if not (t.name in rewritten and t.name not in seen):
                    name = readable_test_name(t.name, function, existing_names | seen | rewritten)
                    if name != t.name:
                        t = t.model_copy(update={"name": name})
                problem = cls._body_problem(t.body, allowed | setup_names([t.body]))
                if problem:
                    problems.append(f"{t.name}: {problem}")
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
        if suite.content.count("{") != suite.content.count("}"):
            raise ba.AgentFault("CONTRACT_VIOLATION", "suite braces are unbalanced")
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
    out = bundle.model_copy(deep=True)
    for action in out.actions:
        if action.role != "testsuite":
            continue
        sources = uc.test_sources(action.content)
        for name in names:
            if name in sources:
                action.content = action.content.replace(sources[name] + "\n\n", "").replace(sources[name], "")
            action.content = re.sub(r"[ \t]*UT_CASE\(\s*" + re.escape(name) + r"\s*\)\n?", "", action.content)
            action.evidence_classes.pop(name, None)
    gone = set(names)
    out.tests = [t for t in out.tests if t not in gone]
    out.characterization_tests = [t for t in out.characterization_tests if t not in gone]
    out.new_tests = [t for t in out.new_tests if t not in gone]
    out.notes.append(f"{len(names)} redundant characterization test(s) removed: they added no coverage")
    return out


def finalize_bundle(bundle: uc.FileActionBundle, observed: dict[str, str],
                    test_items: dict[str, list[str]] | None = None) -> uc.FileActionBundle:
    """UT_OBSERVE_*("id", expr) -> a characterization assertion with the observed value [A2-21c].
    With per-test coverage from Agent3, new characterization tests that add nothing are dropped."""
    candidates = [t for t in bundle.new_tests if t in set(bundle.characterization_tests)]
    bundle = drop_tests(bundle, redundant_tests(candidates, test_items or {}))
    out = bundle.model_copy(deep=True)
    macro = {"INT": "UT_EXPECT_EQ_INT({e}, {v})", "DBL": "UT_EXPECT_EQ_DBL({e}, {v}, 1e-9)",
             "STR": "UT_EXPECT_EQ_STR({e}, {v})", "BOOL": "UT_EXPECT_EQ_INT(({e}) ? 1 : 0, {v})"}
    pattern = re.compile(r'UT_OBSERVE_(INT|DBL|STR|BOOL)\(\s*"([^"]+)"\s*,\s*(.*)\)\s*;')
    missing = []
    for action in out.actions:
        if action.role != "testsuite":
            continue
        lines = []
        for line in action.content.split("\n"):
            m = pattern.search(line)
            if not m:
                lines.append(line)
                continue
            kind, oid, expr = m.groups()
            if oid not in observed:
                missing.append(oid)
                lines.append(line[: m.start()] + f"{expr};  /* not observed: test did not reach this point */")
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
    out.characterization_tests = sorted(set(out.characterization_tests))
    return out


def build_agent(settings: ba.Settings, config_path: str | None = None) -> GeneratorAgent:
    section = {}
    if config_path:
        section = json.loads(Path(config_path).read_text(encoding="utf-8")).get("generator", {})
    agent = GeneratorAgent(settings, config=GeneratorConfig(**section))
    agent.packs = uc.knowledge_packs(config_path)                   # KP-3
    return agent


if __name__ == "__main__":
    _config = next((sys.argv[i + 1] for i, a in enumerate(sys.argv[:-1]) if a == "--config"), None)
    raise SystemExit(ba.agent_main(lambda s: build_agent(s, _config), "agent_unittest_generator (Agent2)"))
