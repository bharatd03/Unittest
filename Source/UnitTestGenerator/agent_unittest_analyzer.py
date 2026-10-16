"""Agent1 - agent_unittest_analyzer: discovery, expected-behaviour evidence, planning.

Agent1 is the analysis and planning authority [GEN-03]. It never writes test files [A1-27];
it produces a typed plan (UTPlan) for Agent2. Everything that can be extracted reliably is
extracted deterministically [A1-25u, ARCH-6]: functions, layout, existing tests, stubs needed,
evidence E1-E6, coverage gaps and the MC/DC condition vectors still missing [A1-18b].

Dependency analysis [A1-DA1 .. A1-DA14] uses the compiler's front end (clang -Xclang -ast-dump=json)
with the SUT's flags from Agent3's build description: exact function inventory, parameter types
for test inputs, the headers the SUT includes and every external symbol it uses, each decided
stub, real or system. Only the stub-or-real decision may ask the LLM [A1-DA8], and requirements written in
prose may be linked to functions by the brain, checked here [A1-PL13].

Coverage planning [A1-PL1 .. A1-PL14]: Agent1 determines the facts (gaps, path conditions, stubs, evidence,
what earlier tests reached); from the second plan on the brain suggests the scenarios per function, and
Agent1 checks every one deterministically before Agent2 receives it. Without a brain, or when nothing
passes the check, the rules plan the function [A1-PL8].

Tools (MCP, each a contract): discover, dependencies, plan, collect_evidence.
Run as an MCP server:  python agent_unittest_analyzer.py serve [--config ut_config.json]
"""
from __future__ import annotations

import ast
import asyncio
import itertools
import json
import os
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

import ut_contracts as uc
from ut_contracts import ba

LOG = ba.logging.getLogger("ut.agent1")
C_EXT = (".c",)
CPP_EXT = (".cpp", ".cc", ".cxx", ".c++")
HEADER_EXT = (".h", ".hpp", ".hh", ".hxx")
KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "do", "else", "case", "catch", "new", "delete",
            "static_cast", "reinterpret_cast", "const_cast", "dynamic_cast", "defined", "alignof", "decltype"}
FRAMEWORKS = [("ut_harness", "ut_harness.h"), ("googletest", "gtest/gtest.h"), ("unity", "unity.h"),
              ("cmocka", "cmocka.h"), ("catch2", "catch"), ("doctest", "doctest"), ("parasoft", "cpptest"),
              ("cppunit", "cppunit")]


class AnalyzerConfig(BaseModel):
    max_attempts_per_gap: int = 2
    max_scenarios_per_function: int = 12
    max_callers: int = 5
    tools_manifest: str = ""                 # "" = tools/toolchain.json [TP-10]
    real_dependency_depth: int = 2           # A1-DA10, ORC-7
    max_definition_units: int = 40           # translation units parsed to find one definition [A1-DA5]
    requirement_files: list[str] = []        # glob patterns, relative to the repository [A1-25x]
    brain_planning: bool = True              # A1-PL1
    max_brain_calls_per_function: int = 2    # re-ask included [A1-PL9, ORC-7]
    max_brain_scenarios: int = 8
    max_lookups_per_function: int = 6        # A1-PL6, ORC-7
    # Expected answer per brain scenario, in tokens: sets the answer limit and window [ARCH-11]
    tokens_per_scenario: int = 320
    link_requirements: bool = True           # A1-PL13
    max_requirements_to_link: int = 40
    min_link_probability: float = 0.6
    propose_declarations: bool = True        # A1-PL14


# ============================================================================ C/C++ source helpers
def strip_comments(text: str) -> str:
    """Removes comments and string/char literal contents, keeping every newline and offset."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
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
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(c + " " * max(j - i - 2, 0) + (c if j - i >= 2 else ""))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def find_functions(source: str, rel: str) -> list[uc.FunctionInfo]:
    """Function definitions in a C/C++ file, including class methods and constructors."""
    code = strip_comments(source)
    functions, stack, header_start = [], [], 0
    i = 0
    while i < len(code):
        ch = code[i]
        if ch in ";}":
            if ch == "}" and stack:
                kind, name, start, sig = stack.pop()
                if kind == "function" and not any(k == "function" for k, *_ in stack):
                    body = code[start:i]
                    if re.search(r"\)\s*:", sig):                    # constructor initializer list
                        body = sig[_close_paren(sig) + 1:] + body
                    calls = sorted({m.group(1) for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", body)
                                    if m.group(1) not in KEYWORDS} - {name.split("::")[-1]})
                    functions.append(dict(name=name, sig=sig, start=start, end=i, calls=calls))
            header_start = i + 1
        elif ch == "{":
            header = code[header_start:i].strip()
            if any(k == "function" for k, *_ in stack):
                stack.append(("block", "", i, ""))
            elif re.match(r"^(namespace\b|extern\s+\"|class\b|struct\b|union\b|enum\b|template\s*<.*>\s*(class|struct)\b)",
                          header) and "(" not in header.split("{")[0]:
                stack.append(("scope", "", i, ""))
                if header.startswith(("class", "struct")):
                    pass
            else:
                name = function_name(header)
                stack.append(("function", name, i, header) if name else ("block", "", i, ""))
            header_start = i + 1
        elif ch == ":" and i + 1 < len(code) and code[i + 1] == ":":
            i += 1
        elif ch in "#" and (i == 0 or code[i - 1] == "\n"):
            j = code.find("\n", i)
            while j > 0 and code[j - 1] == "\\":
                j = code.find("\n", j + 1)
            i = len(code) if j < 0 else j
            header_start = i
            continue
        i += 1
    out = []
    for f in functions:
        header = re.sub(r"\s+", " ", f["sig"]).strip()
        if re.search(r"\)\s*:", header):                       # constructor initializer list
            header = header[: _close_paren(header) + 1]
        start_line = line_of(code, f["start"] - len(f["sig"])) if f["sig"] else line_of(code, f["start"])
        start_line = _first_code_line(code, start_line, line_of(code, f["start"]))
        qualified = f["name"]
        out.append(uc.FunctionInfo(name=qualified.split("::")[-1] if "::" not in qualified else qualified,
                                   qualified_name=qualified, signature=header, file=rel, line_start=start_line,
                                   line_end=line_of(code, f["end"]),
                                   is_static=header.startswith("static") or " static " in f" {header} ",
                                   calls=f["calls"], member="::" in qualified))
    return out


def _first_code_line(code: str, first: int, last: int) -> int:
    lines = code.splitlines()
    for number in range(first, last + 1):
        if 0 < number <= len(lines) and lines[number - 1].strip():
            return number
    return last


def _close_paren(text: str) -> int:
    depth = 0
    for index, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return index
    return len(text) - 1


def function_name(header: str) -> str:
    header = re.sub(r"\s+", " ", header).strip()
    if not header or "(" not in header or header.startswith(("=", "return")):
        return ""
    head = header[: header.index("(")].strip()
    m = re.search(r"((?:[A-Za-z_]\w*\s*::\s*)*~?[A-Za-z_]\w*)$", head)
    if not m:
        return ""
    name = re.sub(r"\s+", "", m.group(1))
    if name.split("::")[-1] in KEYWORDS or name in ("operator",):
        return ""
    if head == m.group(1) and "::" not in name:
        return ""                                  # a call or macro, no return type
    return name


def find_declarations(text: str) -> dict[str, str]:
    """Function declarations (prototypes) in a header: name -> declaration text."""
    code = strip_comments(text)
    decls = {}
    for m in re.finditer(r"(?:^|[;}\n])\s*((?:extern\s+)?[A-Za-z_][\w\s\*&:<>,]*?\b([A-Za-z_]\w*)\s*\(([^;{}()]*)\)\s*(?:const\s*)?);",
                         code):
        decl, name = re.sub(r"\s+", " ", m.group(1)).strip(), m.group(2)
        if name in KEYWORDS or decl.startswith(("return", "typedef", "#")) or "=" in decl.split("(")[0]:
            continue
        decls.setdefault(name, decl)
    return decls


def parse_declaration(decl: str) -> tuple[str, list[list[str]]]:
    decl = re.sub(r"^extern\s+", "", decl.strip().rstrip(";"))
    head, params = decl[: decl.index("(")], decl[decl.index("(") + 1: decl.rindex(")")]
    name = re.search(r"([A-Za-z_]\w*)\s*$", head).group(1)
    return_type = head[: head.rindex(name)].strip()
    out = []
    if params.strip() not in ("", "void"):
        for index, p in enumerate(_split_top(params)):
            p = p.strip()
            m = re.search(r"([A-Za-z_]\w*)\s*(\[[^\]]*\])?$", p)
            if m and m.group(1) not in ("int", "char", "long", "short", "float", "double", "unsigned", "signed",
                                         "bool", "void", "size_t") and p != m.group(1):
                ptype = (p[: m.start(1)] + (m.group(2) and "*" or "")).strip()
                out.append([ptype, m.group(1)])
            else:
                out.append([p, f"arg{index}"])
    return return_type, out


def _split_top(text: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for ch in text:
        if ch in "(<[":
            depth += 1
        elif ch in ")>]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return parts


# ============================================================================ decisions and MC/DC [A1-18b]
def decision_text(source_line: str, column: int = 0) -> str:
    """The boolean expression of an if/while/return at this line. With a (1-based) column, the
    decision that starts at or before that column is chosen, so two decisions on one line are
    told apart."""
    code = strip_comments(source_line)
    found = list(re.finditer(r"\b(if|while)\s*\(", code))
    m = found[0] if found else None
    if column > 0 and len(found) > 1:
        before = [f for f in found if f.start() < column]
        m = before[-1] if before else found[0]
    if m:
        start = m.end() - 1
        end = _close_paren(code[start:]) + start
        return source_line[start + 1:end].strip()
    m = re.search(r"\breturn\s+(.*);", code)
    if m and ("&&" in m.group(1) or "||" in m.group(1)):
        return source_line[m.start(1): m.end(1)].strip()
    return ""


def split_conditions(expr: str) -> list[str]:
    """Leaf conditions of a && / || expression, left to right (LLVM numbers them C1..Cn)."""
    leaves: list[str] = []

    def walk(text: str) -> None:
        text = text.strip()
        while text.startswith("(") and _close_paren(text) == len(text) - 1:
            text = text[1:-1].strip()
        for op in ("||", "&&"):
            parts = _split_on(text, op)
            if len(parts) > 1:
                for part in parts:
                    walk(part)
                return
        if text.startswith("!") and text[1:].strip().startswith("(") and \
                _close_paren(text[1:].strip()) == len(text[1:].strip()) - 1 and \
                ("&&" in text or "||" in text):
            walk(text[1:].strip()[1:-1])
            return
        leaves.append(text)

    walk(expr)
    return leaves


def _split_on(text: str, op: str) -> list[str]:
    parts, depth, current, i = [], 0, "", 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and text.startswith(op, i):
            parts.append(current)
            current = ""
            i += len(op)
            continue
        current += ch
        i += 1
    parts.append(current)
    return [p for p in parts if p.strip()]


def to_python(expr: str, leaves: list[str]) -> str:
    """Boolean skeleton of the expression with leaves replaced by c[i]."""
    out, i = "", 0
    order = sorted(range(len(leaves)), key=lambda k: -len(leaves[k]))
    work = expr
    placeholders = {}
    for k in order:
        token = f"@{k}@"
        idx = work.find(leaves[k])
        if idx >= 0:
            work = work[:idx] + token + work[idx + len(leaves[k]):]
            placeholders[token] = f"c[{k}]"
    work = work.replace("&&", " and ").replace("||", " or ")
    work = re.sub(r"!(?!=)", " not ", work)
    for token, py in placeholders.items():
        work = work.replace(token, py)
    return work


def mcdc_pairs(expr: str) -> tuple[list[str], dict[int, tuple[str, str]], list[int]]:
    """Unique-cause pairs per condition: vectors differing only in that condition with
    different outcomes. Returns (leaves, {i: (vector_a, vector_b)}, infeasible indices).
    Coupled leaves (identical or negated text) are kept consistent [A3-121a]."""
    leaves = split_conditions(expr)
    n = len(leaves)
    if n < 2 or n > 10:
        return leaves, {}, []
    py = to_python(expr, leaves)
    # Only our own tokens may remain (c[i], and/or/not, parentheses): never evaluate source text.
    if not re.fullmatch(r"(?:\s|c\[\d+\]|\(|\)|\band\b|\bor\b|\bnot\b)*", py):
        return leaves, {}, []
    try:
        fn = eval("lambda c: bool(" + py + ")", {"__builtins__": {}, "bool": bool})
        fn(tuple(False for _ in leaves))
    except (SyntaxError, TypeError, IndexError, NameError):
        return leaves, {}, []
    norm = [re.sub(r"\s+", "", leaf) for leaf in leaves]
    coupled = []
    # A leaf with a call, ++/-- or an assignment can change between evaluations: never coupled.
    impure = re.compile(r"[A-Za-z_]\w*\s*\(|\+\+|--|(?<![=!<>])=(?!=)")
    pure = [not impure.search(leaf) for leaf in leaves]
    for a, b in itertools.combinations(range(n), 2):
        if not (pure[a] and pure[b]):
            continue
        if norm[a] == norm[b]:
            coupled.append((a, b, True))
        elif norm[a] == "!" + norm[b] or norm[b] == "!" + norm[a] or norm[a] == f"!({norm[b]})" or norm[b] == f"!({norm[a]})":
            coupled.append((a, b, False))

    def feasible(vec):
        return all((vec[a] == vec[b]) == same for a, b, same in coupled)

    vectors = [v for v in itertools.product([False, True], repeat=n) if feasible(v)]
    outcome = {v: fn(v) for v in vectors}
    pairs, infeasible = {}, []
    for i in range(n):
        found = None
        for v in vectors:
            w = tuple(not x if k == i else x for k, x in enumerate(v))
            if w in outcome and outcome[v] != outcome[w]:
                found = (v, w)
                break
        if found:
            pairs[i] = tuple(_vec_text(leaves, v, outcome[v]) for v in found)
        else:
            infeasible.append(i)
    return leaves, pairs, infeasible


def _vec_text(leaves: list[str], vec: tuple[bool, ...], result: bool) -> str:
    return ", ".join(f"({leaf}) is {'true' if v else 'false'}" for leaf, v in zip(leaves, vec)) + \
        f" -> decision {'true' if result else 'false'}"


def enclosing_conditions(source: str, fn: uc.FunctionInfo, line: int) -> list[str]:
    """Reaching conditions: the if/else chain that must hold to reach ``line``."""
    lines = source.splitlines()
    chain, stack = [], []
    for number in range(fn.line_start, min(line, fn.line_end) + 1):
        text = strip_comments(lines[number - 1]) if number - 1 < len(lines) else ""
        m = re.search(r"\b(if|while)\s*\((.*)\)\s*\{", text)
        if m and number < line:
            stack.append((text.count("{") - text.count("}"), f"({m.group(2).strip()}) is true", number))
        elif re.search(r"\}\s*else\s*\{", text) and stack:
            depth, cond, start = stack.pop()
            stack.append((1, cond.replace("is true", "is false"), number))
        else:
            delta = text.count("{") - text.count("}")
            if stack and delta < 0:
                depth, cond, start = stack[-1]
                depth += delta
                if depth <= 0:
                    stack.pop()
                else:
                    stack[-1] = (depth, cond, start)
            elif stack and delta > 0:
                depth, cond, start = stack[-1]
                stack[-1] = (depth + delta, cond, start)
    chain = [cond for _, cond, _ in stack]
    return chain


# ============================================================================ Clang front end [A1-DA1 .. A1-DA14]
NONDETERMINISTIC = {"time", "clock", "rand", "random", "rand_r", "drand48", "lrand48", "gettimeofday", "clock_gettime",
                    "getenv", "fopen", "fread", "fwrite", "fgets", "fgetc", "fscanf", "read", "write", "open", "socket",
                    "recv", "send", "sleep", "usleep", "nanosleep", "localtime", "gmtime", "getpid"}
INTEGRAL_T = re.compile(r"^(?:(?:un)?signed\s+)?(?:char|short|int|long|long long|long int|unsigned|size_t|ssize_t|"
                        r"ptrdiff_t|u?int(?:8|16|32|64)_t|u?int_(?:least|fast)(?:8|16|32|64)_t|u?intptr_t|wchar_t|"
                        r"char(?:8|16|32)_t)(?:\s+int)?$")
FLOAT_T = re.compile(r"^(?:float|double|long double)$")
DEP_QUESTION = {
    "decision": ba.Question(type="choice", instructions=(
        "A unit test of the SUT links this external dependency. Decide whether the test program uses the REAL "
        "definition from the repository or a generated STUB. Use only the evidence."), criteria={
        "stub": "It accesses hardware, registers, files, time, random numbers, communication or OS services; keeps "
                "state a test cannot reset; brings in many further dependencies; or SUT decisions depend on its "
                "result and the test must control that result to reach coverage.",
        "real": "It is a small, deterministic computation without side effects (conversion, calculation, utility), "
                "so the real behaviour makes the test more meaningful and needs no further stubs."})}
SYSTEM_QUESTION = {
    "decision": ba.Question(type="choice", instructions=(
        "The SUT calls a non-deterministic standard-library or operating-system service. Decide whether unit tests "
        "link the system implementation or replace it with a controllable stub."), criteria={
        "stub": "SUT decisions depend on its result, so tests must control it to be repeatable and reach coverage.",
        "system": "Its result does not influence SUT decisions, so the system implementation is fine."})}


def _plain_type(t: str) -> str:
    t = re.sub(r"\b(const|volatile|struct|union|enum|class|restrict|__restrict)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


class AstIndex:
    """Clang's JSON AST, read economically: only top-level declarations from repository files are
    parsed; for anything else the location comes from a text index. (One system namespace can be
    100 MB of JSON.) Clang prints a location's file only when it changes, so the file in effect at
    any point is the last "file" printed before it, ignoring "includedFrom" [A1-DA1]."""

    def __init__(self, text: str, cwd: Path, repo: Path):
        import bisect
        self._bisect = bisect
        self.text, self.cwd, self.repo = text, cwd, repo.resolve()
        self.files: list[tuple[int, str]] = []
        for m in re.finditer(r'"file": "((?:[^"\\]|\\.)*)"', text):
            if re.search(r'"includedFrom": \{\s*$', text[max(0, m.start() - 40): m.start()]):
                continue
            self.files.append((m.start(), self._resolve(json.loads('"' + m.group(1) + '"'))))
        self.file_offsets = [o for o, _ in self.files]
        # A declaration is printed before any reference to it ("referencedDecl" repeats id and kind).
        self.ids: dict[str, int] = {}
        for m in re.finditer(r'"id": "(0x[0-9a-f]+)",\s*"kind": "\w+"', text):
            self.ids.setdefault(m.group(1), m.start())
        starts = [m.start() + 1 for m in re.finditer(r"\n    \{\n", text)]
        self.chunks = []
        for s in starts:
            e = text.find("\n    }", s)
            self.chunks.append((s, e + 6 if e > 0 else len(text)))
        self.chunk_starts = [s for s, _ in self.chunks]
        self.nodes: dict[str, dict] = {}            # id -> node, for parsed chunks
        self.qualified: dict[str, str] = {}         # id -> qualified name
        self.parent: dict[str, str] = {}            # id -> enclosing record/namespace id
        self.parsed: set[int] = set()

    def _resolve(self, path: str) -> str:
        p = Path(path)
        p = p if p.is_absolute() else self.cwd / p
        try:
            return str(p.resolve())
        except OSError:
            return str(p)

    def file_at(self, offset: int) -> str:
        i = self._bisect.bisect_right(self.file_offsets, offset) - 1
        return self.files[i][1] if i >= 0 else ""

    def node_file(self, offset: int) -> str:
        """File in effect for the node starting at offset (its loc may itself name a new file)."""
        end = self.text.find('"range"', offset, offset + 4000)
        return self.file_at(end if end > 0 else offset + 200)

    def rel(self, path: str) -> str:
        try:
            return str(Path(path).relative_to(self.repo)).replace("\\", "/")
        except ValueError:
            return ""

    def chunk_index(self, offset: int) -> int:
        return self._bisect.bisect_right(self.chunk_starts, offset) - 1

    def parse_chunks(self, keep) -> list[dict]:
        """Parses the top-level declarations whose file satisfies keep(path)."""
        out = []
        for i, (s, e) in enumerate(self.chunks):
            path = self.node_file(s)
            if i in self.parsed or not keep(path):
                continue
            try:
                node = json.loads(self.text[s:e])
            except ValueError:
                continue
            self.parsed.add(i)
            node["_file"] = path
            self._index(node, "", "")
            out.append(node)
        return out

    def _index(self, node: dict, scope: str, parent: str) -> None:
        nid, kind, name = node.get("id"), node.get("kind", ""), node.get("name", "")
        qualified = f"{scope}::{name}" if scope and name else name
        if nid:
            self.nodes[nid] = node
            if parent:
                self.parent[nid] = parent
            if name:
                self.qualified[nid] = qualified
        inner_scope, inner_parent = scope, parent
        if kind in ("NamespaceDecl", "CXXRecordDecl", "RecordDecl", "ClassTemplateDecl") and name:
            inner_scope, inner_parent = qualified, nid
        elif kind == "EnumDecl" and node.get("scopedEnumTag") and name:
            inner_scope = qualified
        for child in node.get("inner", []) or []:
            if isinstance(child, dict):
                self._index(child, inner_scope, inner_parent)

    def qualified_name(self, node: dict) -> str:
        nid = node.get("id", "")
        name = self.qualified.get(nid) or node.get("name", "")
        owner = node.get("parentDeclContextId")
        if owner and owner in self.qualified and "::" not in name:
            name = f"{self.qualified[owner]}::{node.get('name', '')}"
        return name

    def decl_location(self, decl_id: str) -> tuple[str, int]:
        offset = self.ids.get(decl_id)
        return (self.node_file(offset), offset) if offset is not None else ("", -1)


def _offset(loc: dict) -> int | None:
    if "offset" in loc:
        return loc["offset"]
    for key in ("expansionLoc", "spellingLoc"):
        if key in loc and "offset" in loc[key]:
            return loc[key]["offset"]
    return None


def _end_offset(loc: dict) -> int | None:
    base = loc if "offset" in loc else loc.get("expansionLoc") or loc.get("spellingLoc") or {}
    return base["offset"] + base.get("tokLen", 0) if "offset" in base else None


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for child in node.get("inner", []) or []:
            yield from _walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk(child)


class FrontEnd:
    """One parse of a translation unit with its flags: functions, references, headers, types."""

    def __init__(self, tools: uc.Tools, repo: Path, rel_file: str, flags: list[str], language: str):
        self.tools, self.repo, self.rel_file, self.language = tools, repo.resolve(), rel_file, language
        self.path = str((self.repo / rel_file).resolve())
        self.flags = flags
        self.headers: list[str] = []
        self.missing: list[str] = []
        self.errors: list[str] = []
        self.ast: AstIndex | None = None

    def parse(self, timeout: float = 300) -> bool:
        cpp = self.language == "cpp"
        compiler = self.tools.cxx if cpp else self.tools.cc
        cmd = [compiler, "-fsyntax-only", "-Xclang", "-ast-dump=json", "-H", "-w",
               *(self.tools.cxx_flags if cpp else []), *uc.expand_flags(self.flags, self.repo), self.path]
        if not any(f.startswith("-std=") for f in self.flags):
            cmd.insert(1, "-std=c++17" if cpp else "-std=c11")
        try:
            proc = subprocess.run(cmd, cwd=str(self.repo), capture_output=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, env={**os.environ, **self.tools.env()})
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.errors.append(f"front end did not run: {exc}")
            return False
        err = proc.stderr.decode("utf-8", "replace")
        for line in err.splitlines():
            m = re.match(r"^\.+ (.+)$", line)
            if m:
                rel = self._rel(m.group(1).strip())
                if rel and rel not in self.headers:
                    self.headers.append(rel)
                continue
            m = re.search(r"'([^']+)' file not found", line)
            if m:
                self.missing.append(m.group(1))
            elif ": error:" in line:
                self.errors.append(line.strip()[:300])
        text = proc.stdout.decode("utf-8", "replace")
        if not text.strip().startswith("{"):
            self.errors.append("no syntax tree: " + err.strip()[-300:])
            return False
        self.ast = AstIndex(text, self.repo, self.repo)
        repo_str = str(self.repo)
        self.ast.parse_chunks(lambda p: p.startswith(repo_str))
        return not self.missing

    def _rel(self, path: str) -> str:
        p = Path(path)
        p = p if p.is_absolute() else self.repo / p
        try:
            return str(p.resolve().relative_to(self.repo)).replace("\\", "/")
        except (ValueError, OSError):
            return ""

    def source_bytes(self, path: str) -> bytes:
        try:
            return Path(path).read_bytes()
        except OSError:
            return b""

    # ------------------------------------------------------------ definitions in the main file
    def definitions(self) -> list[tuple[dict, str]]:
        """(node, qualified name) of every function with a body and every global variable defined
        in this translation unit's own file."""
        out = []
        for nid, node in self.ast.nodes.items():
            kind = node.get("kind", "")
            if node.get("isImplicit"):
                continue
            if kind in ("FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl"):
                if not any(c.get("kind") == "CompoundStmt" for c in node.get("inner", []) or []):
                    continue
            elif kind == "VarDecl":
                if node.get("storageClass") == "extern" and not node.get("init") or nid in self._locals():
                    continue
            else:
                continue
            offset = self.ast.ids.get(nid)
            if offset is None or self.ast.node_file(offset) != self.path:
                continue
            out.append((node, self.ast.qualified_name(node)))
        return out

    def _locals(self) -> set[str]:
        if not hasattr(self, "_local_ids"):
            ids = set()
            for node in self.ast.nodes.values():
                if node.get("kind") in ("FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl"):
                    for sub in _walk(node.get("inner", [])):
                        if sub.get("kind") in ("VarDecl", "ParmVarDecl", "BindingDecl") and sub.get("id"):
                            ids.add(sub["id"])
            self._local_ids = ids
        return self._local_ids

    # ------------------------------------------------------------ references out of a function body
    def references(self, node: dict) -> list[dict]:
        refs, seen = [], set()
        body = [c for c in node.get("inner", []) or [] if c.get("kind") in ("CompoundStmt", "CXXCtorInitializer")]
        for sub in _walk(body):
            kind = sub.get("kind")
            if kind == "DeclRefExpr":
                ref = sub.get("referencedDecl") or {}
                if ref.get("kind") in ("FunctionDecl", "CXXMethodDecl", "VarDecl", "CXXConstructorDecl") and \
                        ref.get("id") not in seen and ref.get("id") not in self._locals():
                    seen.add(ref.get("id"))
                    refs.append({"id": ref.get("id"), "kind": ref["kind"], "name": ref.get("name", ""),
                                 "type": (ref.get("type") or {}).get("qualType", "")})
            elif kind == "MemberExpr":
                rid = sub.get("referencedMemberDecl")
                target = self.ast.nodes.get(rid, {})
                if rid and rid not in seen and target.get("kind") in ("CXXMethodDecl", "CXXConstructorDecl") or \
                        (rid and rid not in seen and not target and "bound member" in (sub.get("type") or {}).get("qualType", "")):
                    seen.add(rid)
                    base = next(iter(sub.get("inner") or []), {})
                    owner = (base.get("type") or {}).get("desugaredQualType") or (base.get("type") or {}).get("qualType", "")
                    refs.append({"id": rid, "kind": target.get("kind", "CXXMethodDecl"), "name": sub.get("name", ""),
                                 "type": (target.get("type") or {}).get("qualType", ""), "owner": owner})
            elif kind == "CXXConstructExpr":
                cls = _plain_type((sub.get("type") or {}).get("qualType", ""))
                ctor = (sub.get("ctorType") or {}).get("qualType", "")
                key = f"ctor:{cls}:{ctor}"
                if cls and key not in seen:
                    seen.add(key)
                    refs.append({"id": self._find_ctor(cls, ctor), "kind": "CXXConstructorDecl",
                                 "name": cls.split("::")[-1], "type": ctor, "class": cls})
        return refs

    def _find_ctor(self, cls: str, ctor_type: str) -> str:
        for nid, node in self.ast.nodes.items():
            if node.get("kind") == "CXXConstructorDecl" and not node.get("isImplicit") and \
                    (node.get("type") or {}).get("qualType") == ctor_type and \
                    self.ast.qualified.get(self.ast.parent.get(nid, ""), "") == cls:
                return nid
        return ""

    # ------------------------------------------------------------ types for test inputs
    def enums(self) -> list[uc.EnumInfo]:
        out = []
        typedefs = {}                                   # unnamed enum id -> typedef name
        for nid, node in self.ast.nodes.items():
            if node.get("kind") == "TypedefDecl" and node.get("name"):
                for x in _walk(node.get("inner", [])):
                    if isinstance(x.get("decl"), dict) and x["decl"].get("id"):
                        typedefs.setdefault(x["decl"]["id"], self.ast.qualified.get(nid, node["name"]))
        for nid, node in self.ast.nodes.items():
            if node.get("kind") != "EnumDecl" or not (node.get("name") or nid in typedefs):
                continue
            qualified = self.ast.qualified.get(nid, node.get("name", "")) or typedefs[nid]
            scope = qualified.rsplit("::", 1)[0] + "::" if "::" in qualified else ""
            prefix = qualified + "::" if node.get("scopedEnumTag") else scope
            if nid in typedefs and not node.get("name"):
                prefix = typedefs[nid].rsplit("::", 1)[0] + "::" if "::" in typedefs[nid] else ""
            values, nxt = {}, 0
            for c in node.get("inner", []) or []:
                if c.get("kind") != "EnumConstantDecl":
                    continue
                value = next((int(x["value"]) for x in _walk(c.get("inner", [])) if "value" in x and
                              re.fullmatch(r"-?\d+", str(x["value"]))), nxt)
                values[prefix + c["name"]] = value
                nxt = value + 1
            out.append(uc.EnumInfo(name=qualified, values=values))
        return out

    def _records(self) -> dict[str, dict]:
        if hasattr(self, "_record_map"):
            return self._record_map
        records: dict[str, dict] = {}
        for nid, node in self.ast.nodes.items():
            if node.get("kind") in ("RecordDecl", "CXXRecordDecl") and node.get("completeDefinition"):
                if node.get("name"):
                    records[self.ast.qualified.get(nid, node["name"])] = node
                    records.setdefault(node["name"], node)
                records[nid] = node
        for nid, node in self.ast.nodes.items():
            if node.get("kind") == "TypedefDecl" and node.get("name"):
                ref = next((x["decl"]["id"] for x in _walk(node.get("inner", [])) if isinstance(x.get("decl"), dict)
                            and x["decl"].get("id") in records), None)
                if ref:
                    records[self.ast.qualified.get(nid, node["name"])] = records[ref]
                    records.setdefault(node["name"], records[ref])
        self._record_map = records
        return records

    def _fields(self, record: dict, depth: int = 0) -> list[uc.FieldInfo] | None:
        if record.get("kind") == "CXXRecordDecl" and any(c.get("kind") in ("CXXMethodDecl", "CXXConstructorDecl")
                                                         and not c.get("isImplicit") for c in record.get("inner", []) or []):
            return None                                      # a class with behaviour: not a plain aggregate
        out = []
        for c in record.get("inner", []) or []:
            if c.get("kind") != "FieldDecl":
                continue
            t = (c.get("type") or {}).get("desugaredQualType") or (c.get("type") or {}).get("qualType", "")
            info = uc.FieldInfo(name=c.get("name", ""), type=(c.get("type") or {}).get("qualType", t))
            base = _plain_type(t)
            m = re.match(r"^(.*?)\s*\[(\d+)\]$", base)
            if m:
                info.kind, info.array_len = "array", int(m.group(2))
            elif base in ("bool", "_Bool"):
                info.kind = "bool"
            elif INTEGRAL_T.match(base):
                info.kind = "int"
            elif FLOAT_T.match(base):
                info.kind = "float"
            elif "enum " in t:
                info.kind = "enum"
            elif base in self._records() and depth < 3:
                sub = self._fields(self._records()[base], depth + 1)
                if sub is None:
                    return None
                info.kind, info.fields = "struct", sub
            else:
                info.kind = "other"
            if not c.get("name"):
                return None                                  # anonymous members: no aggregate initializer
            out.append(info)
        return out

    def param_info(self, parm: dict, enums: dict[str, uc.EnumInfo], body: str) -> uc.ParamInfo:
        qt = (parm.get("type") or {}).get("qualType", "")
        dt = (parm.get("type") or {}).get("desugaredQualType") or qt
        name = parm.get("name", "")
        info = uc.ParamInfo(name=name, type=qt)
        t = dt.strip()
        if name and re.search(r"(\b" + re.escape(name) + r"\s*[!=]=\s*(NULL|nullptr|0)\b|\b(NULL|nullptr)\s*[!=]=\s*"
                              + re.escape(name) + r"\b|!\s*" + re.escape(name) + r"\b|if\s*\(\s*" + re.escape(name)
                              + r"\s*\))", body):
            info.nullable = True
        m = re.match(r"^(.*?)\s*\(\*\)\s*\((.*)\)$", t)
        if m:
            info.kind, info.callback_return = "callback", m.group(1).strip()
            info.callback_params = [p.strip() for p in m.group(2).split(",") if p.strip() and p.strip() != "void"]
            return info
        by_ref = t.endswith("&") and not t.endswith("&&")
        pointer = t.endswith("*") or by_ref
        base_dt = t.rstrip("*&").strip() if pointer else t
        base = _plain_type(base_dt)
        info.base = _plain_type(qt.rstrip("*&").strip()) if pointer else _plain_type(qt)
        info.by_ref = by_ref
        enum = enums.get(base) or enums.get(_plain_type(qt.rstrip("*&")))
        if pointer and base in ("char", "signed char", "unsigned char") and not by_ref:
            info.kind = "cstring" if base_dt.startswith("const") or "const char" in qt else "buffer"
        elif pointer and base == "void":
            info.kind = "other"
        elif pointer:
            if base in ("bool", "_Bool") or INTEGRAL_T.match(base) or FLOAT_T.match(base) or enum:
                info.kind = "ptr_scalar"
            elif base in self._records():
                fields = self._fields(self._records()[base])
                info.kind, info.fields = ("ptr_struct", fields) if fields is not None else ("object", [])
            else:
                info.kind = "other"
        elif base in ("bool", "_Bool"):
            info.kind = "bool"
        elif enum:
            info.kind, info.enum_values = "enum", list(enum.values)
        elif INTEGRAL_T.match(base):
            info.kind = "int"
        elif FLOAT_T.match(base):
            info.kind = "float"
        elif base in self._records():
            fields = self._fields(self._records()[base])
            info.kind, info.fields = ("struct", fields) if fields is not None else ("object", [])
        else:
            info.kind = "object" if self.language == "cpp" else "other"
        return info


# ============================================================================ infeasibility proofs [A3-121a, A1-37]
INF = float("inf")
CMP_LEAF = re.compile(r"^\s*(?P<l>[^<>=!]+?)\s*(?P<op>==|!=|<=|>=|<|>)\s*(?P<r>[^<>=!]+?)\s*$")
IDENT = re.compile(r"^[A-Za-z_][\w]*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*|\s*\[\s*\d+\s*\])*$")


def parse_bool(expr: str):
    """Tree of a && / || / ! expression whose leaves are numbered left to right, the way LLVM
    numbers MC/DC conditions (same order as split_conditions)."""
    leaves: list[str] = []

    def walk(text: str):
        text = text.strip()
        while text.startswith("(") and _close_paren(text) == len(text) - 1:
            text = text[1:-1].strip()
        for op, name in (("||", "or"), ("&&", "and")):
            parts = _split_on(text, op)
            if len(parts) > 1:
                return (name, [walk(p) for p in parts])
        if text.startswith("!") and not text.startswith("!=") and text[1:].strip().startswith("(") and \
                _close_paren(text[1:].strip()) == len(text[1:].strip()) - 1 and ("&&" in text or "||" in text):
            return ("not", walk(text[1:].strip()[1:-1]))
        leaves.append(text)
        return ("leaf", len(leaves) - 1)
    return walk(expr), leaves


def evaluate(tree, values: tuple[bool, ...]) -> tuple[bool, dict[int, bool]]:
    """Short-circuit evaluation: (outcome, {leaf: value} of the leaves actually evaluated)."""
    kind = tree[0]
    if kind == "leaf":
        return values[tree[1]], {tree[1]: values[tree[1]]}
    if kind == "not":
        v, seen = evaluate(tree[1], values)
        return not v, seen
    seen: dict[int, bool] = {}
    for child in tree[1]:
        v, s = evaluate(child, values)
        seen.update(s)
        if kind == "and" and not v:
            return False, seen
        if kind == "or" and v:
            return True, seen
    return kind == "and", seen


def _number(text: str, constants: dict[str, int]) -> int | None:
    text = text.strip().strip("()").strip()
    if text in constants:
        return constants[text]
    if text in ("true", "false"):
        return 1 if text == "true" else 0
    m = re.fullmatch(r"(-?)\s*(0[xX][0-9a-fA-F]+|\d+)[uUlL]*", text)
    if m:
        return int(m.group(1) + m.group(2), 0)
    return None


def preprocessor_macros(tools: uc.Tools, repo: Path, rel_file: str, flags: list[str],
                        language: str) -> dict[str, int] | None:
    """A1-37a: the integer macros the preprocessor defines for the SUT file with one variant's flags
    (clang -dM -E). Analysis only: nothing is compiled, linked or run (A1-37d). None: it did not run."""
    cpp = language == "cpp"
    cmd = [tools.cxx if cpp else tools.cc, "-dM", "-E", "-w", "-x", "c++" if cpp else "c",
           *(tools.cxx_flags if cpp else []), *uc.expand_flags(flags, repo), str(repo / rel_file)]
    try:
        proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
                              env={**os.environ, **tools.env()})
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    values: dict[str, int] = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        m = re.match(r"#define\s+([A-Za-z_]\w*)\s+(.+?)\s*$", line)
        if m:
            value = _number(m.group(2), {})
            if value is not None:
                values[m.group(1)] = value
    return values


def variant_dependent(variants: list[tuple[uc.BuildVariant, dict[str, int]]]) -> set[str]:
    """A1-37b: the macros whose value differs between the build variants (or that only some define)."""
    if len(variants) < 2:
        return set()
    names = set().union(*(values for _, values in variants))
    return {n for n in names if len({values.get(n) for _, values in variants}) > 1}


def _ivs(op: str, c: int) -> list[tuple[float, float]]:
    return {"==": [(c, c)], "!=": [(-INF, c - 1), (c + 1, INF)], "<": [(-INF, c - 1)], "<=": [(-INF, c)],
            ">": [(c + 1, INF)], ">=": [(c, INF)]}[op]


NEGATE = {"==": "!=", "!=": "==", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}
FLIP = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "==": "==", "!=": "!="}


def _intersect(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    for lo1, hi1 in a:
        for lo2, hi2 in b:
            lo, hi = max(lo1, lo2), min(hi1, hi2)
            if lo <= hi:
                out.append((lo, hi))
    return out


def leaf_constraint(leaf: str, constants: dict[str, int]) -> tuple[str, str, int] | None:
    """(variable, op, constant) meaning "leaf is true", or None when the leaf is outside the
    fragment the proof handles (calls, arithmetic, two variables)."""
    text = leaf.strip()
    while text.startswith("(") and _close_paren(text) == len(text) - 1:
        text = text[1:-1].strip()
    if re.search(r"[A-Za-z_]\w*\s*\(|\+\+|--|(?<![=!<>])=(?!=)", text):
        return None
    neg = False
    while text.startswith("!") and not text.startswith("!="):
        neg, text = not neg, text[1:].strip()
        while text.startswith("(") and _close_paren(text) == len(text) - 1:
            text = text[1:-1].strip()
    m = CMP_LEAF.match(text)
    if m:
        left, op, right = m.group("l").strip(), m.group("op"), m.group("r").strip()
        lv, rv = _number(left, constants), _number(right, constants)
        if lv is None and rv is not None and IDENT.match(left):
            var, c = left, rv
        elif rv is None and lv is not None and IDENT.match(right):
            var, c, op = right, lv, FLIP[op]
        elif lv is not None and rv is not None:
            truth = {"==": lv == rv, "!=": lv != rv, "<": lv < rv, "<=": lv <= rv, ">": lv > rv, ">=": lv >= rv}[op]
            var, op, c = "<constant>", "==" if truth else "!=", 1
        else:
            return None
    else:
        value = _number(text, constants)
        if value is not None:
            var, op, c = "<constant>", "==" if value else "!=", 1
        elif IDENT.match(text):
            var, op, c = text, "!=", 0
        else:
            return None
    if neg:
        op = NEGATE[op]
    return re.sub(r"\s+", "", var), op, c


def domains_of(fn: uc.FunctionInfo) -> dict[str, list[tuple[float, float]]]:
    out = {"<constant>": [(1, 1)]}
    for p in fn.params:
        t = re.sub(r"\b(const|volatile)\b", "", p.type).strip()
        if p.kind == "bool":
            out[p.name] = [(0, 1)]
        elif p.kind == "int":
            bits = {"char": 8, "short": 16, "int": 32, "long": 64, "long long": 64, "size_t": 64}
            width = next((int(m) for m in re.findall(r"int(8|16|32|64)_t", t)), None) or \
                next((b for k, b in sorted(bits.items(), key=lambda kv: -len(kv[0])) if re.search(r"\b" + k + r"\b", t)), 32)
            unsigned = "unsigned" in t or t.startswith("uint") or t == "size_t"
            out[p.name] = [(0, 2 ** width - 1)] if unsigned else [(-(2 ** (width - 1)), 2 ** (width - 1) - 1)]
    return out


def satisfiable(assign: dict[int, bool], constraints: list[tuple[str, str, int] | None],
                domains: dict[str, list[tuple[float, float]]]) -> tuple[bool, str]:
    per_var: dict[str, list[tuple[float, float]]] = {}
    why: dict[str, list[str]] = {}
    for leaf, value in assign.items():
        var, op, c = constraints[leaf]
        op = op if value else NEGATE[op]
        current = per_var.get(var, domains.get(var, [(-INF, INF)]))
        per_var[var] = _intersect(current, _ivs(op, c))
        why.setdefault(var, []).append(f"{var} {op} {c}")
        if not per_var[var]:
            if var == "<constant>":
                return False, "a condition on program constants (macros, enumerators) never has that value"
            return False, " and ".join(why[var]) + (f" (type range of {var})" if var in domains else "")
    return True, ""


def path_domains(path: list[str], before: str, constants: dict[str, int],
                 domains: dict[str, list[tuple[float, float]]],
                 params: set[str] | None = None) -> tuple[dict[str, list[tuple[float, float]]], list[str]]:
    """A1-37: the value ranges the conditions on the path to a gap leave for each variable. Only a single comparison
    of a scalar parameter with a constant counts, and only when the code before the gap never changes the parameter
    (a global or a member could change in a call)."""
    domains = {k: list(v) for k, v in domains.items()}
    used = []
    for cond in path:
        m = re.fullmatch(r"\((.*)\) is (true|false)", cond.strip())
        if not m:
            continue
        _, leaves = parse_bool(m.group(1))
        if len(leaves) != 1:
            continue
        c = leaf_constraint(leaves[0], constants)
        if c is None or c[0] == "<constant>":
            continue
        var, op, value = c
        if params is not None and var not in params:
            continue
        changed = re.compile(r"(?<![\w.>])" + re.escape(var) + r"\s*(=(?!=)|\+\+|--|[-+*/%&|^]=|<<=|>>=)|"
                             r"(\+\+|--)\s*" + re.escape(var) + r"\b|&\s*" + re.escape(var) + r"\b")
        if changed.search(strip_comments(before)):
            continue
        op = op if m.group(2) == "true" else NEGATE[op]
        domains[var] = _intersect(domains.get(var, [(-INF, INF)]), _ivs(op, value))
        used.append(f"{var} {op} {value}")
    return domains, used


def prove_infeasible(expr: str, metric: str, index: int | None, outcome: str, fn: uc.FunctionInfo,
                     constants: dict[str, int], path: list[str] | None = None, before: str = "") -> str:
    """A deterministic proof that the gap cannot be covered, or "" [A3-120, A3-121a]. The proof
    covers conditions that compare a variable (or member) with constants of the program; every
    combination of condition values with short-circuit evaluation is checked."""
    tree, leaves = parse_bool(expr)
    if not leaves or len(leaves) > 12:
        return ""
    constraints = [leaf_constraint(leaf, constants) for leaf in leaves]
    if any(c is None for c in constraints):
        return ""
    domains, on_path = path_domains(path or [], before, constants, domains_of(fn),
                                    {p.name for p in fn.params if p.kind in ("int", "bool", "enum", "float")})
    if any(not iv for iv in domains.values()):
        return "the conditions on the path to it contradict each other: " + " and ".join(on_path)
    vectors: dict[tuple, tuple[bool, dict[int, bool]]] = {}
    reasons = []
    for values in itertools.product([False, True], repeat=len(leaves)):
        result, seen = evaluate(tree, values)
        key = tuple(sorted(seen.items()))
        if key in vectors:
            continue
        ok, why = satisfiable(seen, constraints, domains)
        if ok:
            vectors[key] = (result, seen)
        elif why and why not in reasons:
            reasons.append(why)
    feasible = list(vectors.values())
    if on_path:
        reasons = [re.sub(r" \(type range of [^)]*\)", "", r) + " cannot hold where the path to it requires "
                   + " and ".join(on_path) for r in reasons]
    if metric == "DC":
        want = outcome == "T"
        if any(r == want for r, _ in feasible):
            return ""
        return f"no executable combination makes ({expr}) {'true' if want else 'false'}: " + "; ".join(reasons[:3])
    if metric == "BC" and index is not None:
        want = outcome == "T"
        if any(seen.get(index) == want for _, seen in feasible):
            return ""
        return f"condition ({leaves[index]}) is never {'true' if want else 'false'} when evaluated: " + \
            "; ".join(reasons[:3])
    if metric == "MCDC" and index is not None:
        for (r1, a), (r2, b) in itertools.combinations(feasible, 2):
            if r1 == r2 or index not in a or index not in b or a[index] == b[index]:
                continue
            if all(a[k] == b[k] for k in set(a) & set(b) if k != index):
                return ""
        return f"no pair of executable condition vectors shows ({leaves[index]}) independently affecting " \
               f"({expr}): " + "; ".join(reasons[:3])
    return ""


def block_lines(lines: list[str], line: int, branch: str) -> tuple[int, int, int] | None:
    """(first line, last line, first column on the first line) of the then- (T) or else-block (F)
    of the if-statement at ``line`` (1-based)."""
    text = "\n".join(lines[line - 1:])
    m = re.search(r"\bif\s*\(", text)
    if not m:
        return None
    close = _close_paren(text[m.end() - 1:]) + m.end() - 1
    rest = text[close + 1:]
    brace = re.match(r"\s*\{", rest)
    if not brace:
        if branch != "T":
            return None
        stmt_end = text.find(";", close)                    # a single statement without braces
        return (line + text.count("\n", 0, close), line + text.count("\n", 0, stmt_end),
                close - text.rfind("\n", 0, close) + 1) if stmt_end > 0 else None
    start = close + 1 + brace.end() - 1
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    else:
        return None
    if branch == "T":
        return line + text.count("\n", 0, start), line + text.count("\n", 0, end), 0
    m2 = re.match(r"\s*else\s*\{", text[end + 1:])
    if not m2:
        return None
    s2 = end + 1 + m2.end() - 1
    depth = 0
    for i in range(s2, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return line + text.count("\n", 0, s2), line + text.count("\n", 0, i), 0
    return None


# ============================================================================ requirement documents [A1-25x]
REQ_ID = re.compile(r"\b([A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)*[-_]\d+[a-z]?)\b")


def read_requirements(root: Path, files: list[str]) -> list[tuple[str, str, str, int]]:
    """(requirement id, text, file, line) from ReqIF (.reqif, .reqifz) and Markdown/text documents."""
    out = []
    for rel in files:
        path = root / rel
        try:
            if path.suffix.lower() in (".reqif", ".reqifz"):
                out += _reqif(path, rel)
            else:
                out += _markdown_requirements(path.read_text(encoding="utf-8", errors="replace"), rel)
        except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
            LOG.warning("requirement document %s not readable: %s", rel, exc)
    return out


def _reqif(path: Path, rel: str) -> list[tuple[str, str, str, int]]:
    if path.suffix.lower() == ".reqifz":
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".reqif"))
            data = z.read(name)
    else:
        data = path.read_bytes()
    root = ET.fromstring(data)
    local = lambda tag: tag.rsplit("}", 1)[-1]
    names = {}
    for el in root.iter():
        if local(el.tag).startswith("ATTRIBUTE-DEFINITION-"):
            names[el.get("IDENTIFIER")] = (el.get("LONG-NAME") or "").lower()
    out = []
    for obj in root.iter():
        if local(obj.tag) != "SPEC-OBJECT":
            continue
        rid, texts = obj.get("IDENTIFIER", ""), []
        for value in obj.iter():
            kind = local(value.tag)
            if not kind.startswith("ATTRIBUTE-VALUE-"):
                continue
            ref = next((d.text for d in value.iter() if local(d.tag).startswith("ATTRIBUTE-DEFINITION-") and d.text), "")
            label = names.get(ref, "")
            text = value.get("THE-VALUE") or " ".join(t.strip() for t in value.itertext() if t.strip() and t.strip() != ref)
            if label in ("reqif.foreignid", "id", "identifier", "req id", "requirement id") and text:
                rid = text.strip()
            elif text:
                texts.append(text.strip())
        if texts:
            out.append((rid, " ".join(texts)[:1500], rel, 0))
    return out


def _markdown_requirements(text: str, rel: str) -> list[tuple[str, str, str, int]]:
    out = []
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        m = re.match(r"^\s*(?:[#*\-|>]+\s*)*\**(?P<id>" + REQ_ID.pattern[3:-3] + r")\**\s*[:|\-\u2013]\s*"
                     r"(?P<body>.+?)\s*\|?\s*$", line)
        if m:
            body = m.group("body")
            for extra in lines[number: number + 5]:          # continuation lines of the same item
                if not extra.strip() or REQ_ID.match(extra.strip().lstrip("#*-|> ")) or extra.lstrip().startswith("#"):
                    break
                body += " " + extra.strip()
            out.append((m.group("id"), body[:1500], rel, number))
    return out


def stub_spec(dep: uc.Dependency) -> uc.StubSpec | None:
    """The stub of a dependency decided stub, from the front end's exact types [A2-21g, A2-21j]."""
    header = dep.header
    if dep.kind == "variable":
        name = dep.qualified_name or dep.name
        m = re.match(r"^(.*?)\s*(\[.*\])$", dep.type)
        var_type = f"{m.group(1)} {name}{m.group(2)}" if m else f"{dep.type} {name}"
        return uc.StubSpec(function=dep.name, declaration=dep.declaration, return_type="", header=header,
                           kind="variable", qualified_name=dep.qualified_name, var_type=var_type.strip(),
                           is_const=dep.is_const, initializer="{0}" if dep.is_const else "")
    if dep.return_type or dep.params or dep.kind == "constructor":
        return_type, params = dep.return_type, [list(p) for p in dep.params]
    else:
        try:
            return_type, params = parse_declaration(dep.declaration or f"int {dep.name}()")
        except (ValueError, AttributeError):
            return None
    return_type = re.sub(r"\b(static|virtual|inline|extern|explicit)\b", "", return_type).strip()
    return uc.StubSpec(function=dep.name, declaration=dep.declaration, return_type=return_type, params=params,
                       header=header, kind=dep.kind, qualified_name=dep.qualified_name, method_suffix=dep.method_suffix)


# ============================================================================ brain planning [A1-PL1 .. A1-PL14]
class PlannedValue(BaseModel):
    name: str = Field(description="parameter or variable name")
    value: str = Field(description="a C/C++ value or expression")


class PlannedStub(BaseModel):
    stub: str = Field(description="function name of a stub listed under STUBS")
    returns: list[str] = Field(description="return value per call, in call order; the last one stays")


class PlannedCheck(BaseModel):
    expr: str = Field(description="checked after the call: an output variable from setup or ut_stub_<name>_calls")
    value: str


class PlannedQuote(BaseModel):
    evidence_id: str
    text: str = Field(description="exact words copied from that evidence item")


class PlannedScenario(BaseModel):
    """What the brain returns per scenario [A1-PL3]; Agent1 checks it [A1-PL4]."""
    gap_ids: list[str] = Field(description="ids from OPEN GAPS that this scenario closes")
    name: str = Field(description="test name test_<what>_<behaviour>")
    purpose: str = Field(description="one line: what the test checks")
    args: list[PlannedValue] = Field(description="one entry per parameter, by parameter name")
    setup: list[str] = Field(description="C statements before the call: declare and fill local variables only")
    stubs: list[PlannedStub]
    variables: list[PlannedValue] = Field(description="stub variables (STUB VARIABLES) to set before the call")
    expected_return: str = Field(description="the return value the EVIDENCE states; empty when it states none")
    expected_after: list[PlannedCheck]
    evidence_ids: list[str]
    quotes: list[PlannedQuote]
    classification: Literal["confirmed_gap", "characterization_gap", "conflicting_evidence", "insufficient_evidence"]
    conflict: str = Field(description="conflicting_evidence only: which evidence ids contradict each other, how")
    information_sought: str = Field(description="insufficient_evidence only: what to look up, a few words")


class PlannedScenarios(BaseModel):
    scenarios: list[PlannedScenario]


class LinkAnswer(BaseModel):
    requirement_id: str
    functions: list[str] = Field(description="names of FUNCTIONS whose behaviour the requirement states")
    quote: str = Field(description="the exact sentence of the requirement that states it")
    probability: float = Field(description="0..1: how sure the link is")


class LinkAnswers(BaseModel):
    links: list[LinkAnswer]


class DeclarationAnswer(BaseModel):
    declaration: str = Field(description="one C/C++ declaration of the symbol, ending with ;")


# COV-5: a brain may not declare a gap unreachable, untestable, excluded or justified. Such an answer has
# no effect; only the agents' own deterministic analysis may prove that an item cannot be reached (A1-37).
WAY_OUT = re.compile(r"\b(unreachabl\w*|infeasib\w*|untestabl\w*|dead code|(?:impossible|not possible) to (?:reach|cover|test)"
                     r"|cannot (?:be )?(?:reached|covered|tested|reach|cover|test)|can't (?:be )?(?:reached|covered|tested)"
                     r"|not (?:worth|necessary) (?:to )?(?:test|cover)\w*|exclud\w* from (?:coverage|testing)|exclusion"
                     r"|justif\w*|waive\w*|waiver)\b", re.I)
PLAN_TASK = (
    "Plan unit-test scenarios for the function {fn} that close the OPEN GAPS (and, for EVIDENCE:<function>, check "
    "the behaviour the EVIDENCE states). Return JSON {{\"scenarios\": [...]}} with at most {limit} scenarios.\n"
    "Rules:\n"
    "- args: one entry per parameter, by its name. Values: integer, floating or character literals, enumerators, "
    "macros from the source, NULL only where PARAMETERS says it may be NULL, or a variable declared in setup "
    "(pass &var for a pointer).\n"
    "- setup: C statements that declare and fill local variables only; call only functions from HEADERS.\n"
    "- stubs: return values of the STUBS, one per call in call order; variables: STUB VARIABLES to set.\n"
    "- Every OPEN GAP is to be reached by a test: choose inputs, state and stub values that reach it.\n"
    "- Use the PATH CONDITIONS to choose values that reach each gap. For a gap listed under TRIED, choose "
    "different inputs, state or stub values than the ones tried.\n"
    "- classification: confirmed_gap only when an EVIDENCE item (not E8) states the expected result; then give "
    "expected_return / expected_after, its evidence_ids (the ids as written in EVIDENCE, for example "
    "EV-clamp_percent-1; never the classes E1-E8) and quotes copied word for word from it. "
    "characterization_gap when no evidence states the result: leave expected_return empty, the result is observed. "
    "conflicting_evidence when evidence items contradict each other (say which in conflict). "
    "insufficient_evidence when you need more evidence (say what in information_sought).\n"
    "- Never guess an expected value.")
LINK_TASK = (
    "Some REQUIREMENTS below do not name a function. For each one, decide which FUNCTIONS (if any) implement the "
    "behaviour it states. Return JSON {\"links\": [{\"requirement_id\", \"functions\", \"quote\", \"probability\"}]}: "
    "quote is the exact sentence of the requirement you rely on, copied word for word; leave out requirements that "
    "describe none of the FUNCTIONS. Do not invent ids or names.")
DECLARATION_TASK = (
    "The C/C++ symbol {name} is used by the code below, but no declaration of it exists in the repository. Propose "
    "one declaration that fits every use (return type and parameter types). Return JSON {{\"declaration\": "
    "\"...;\"}}. Use only standard C/C++ types or types that appear in the code.")
LIMITS = {"INT_MAX": 2 ** 31 - 1, "INT_MIN": -2 ** 31, "UINT_MAX": 2 ** 32 - 1, "CHAR_MAX": 127, "CHAR_MIN": -128,
          "SCHAR_MAX": 127, "SCHAR_MIN": -128, "UCHAR_MAX": 255, "SHRT_MAX": 32767, "SHRT_MIN": -32768,
          "USHRT_MAX": 65535, "LONG_MAX": 2 ** 63 - 1, "LONG_MIN": -2 ** 63, "ULONG_MAX": 2 ** 64 - 1,
          "LLONG_MAX": 2 ** 63 - 1, "LLONG_MIN": -2 ** 63, "ULLONG_MAX": 2 ** 64 - 1, "INT8_MAX": 127, "INT8_MIN": -128,
          "UINT8_MAX": 255, "INT16_MAX": 32767, "INT16_MIN": -32768, "UINT16_MAX": 65535, "INT32_MAX": 2 ** 31 - 1,
          "INT32_MIN": -2 ** 31, "UINT32_MAX": 2 ** 32 - 1, "INT64_MAX": 2 ** 63 - 1, "INT64_MIN": -2 ** 63,
          "UINT64_MAX": 2 ** 64 - 1, "SIZE_MAX": 2 ** 64 - 1, "true": 1, "false": 0}
RANGES = [(r"bool|_Bool", 0, 1), (r"uint8_t|unsigned char|u?int_least8_t|uint_fast8_t", 0, 255),
          (r"int8_t|signed char", -128, 127), (r"char", -128, 255),
          (r"uint16_t|unsigned short( int)?", 0, 65535), (r"int16_t|short( int)?|signed short", -32768, 32767),
          (r"uint32_t|unsigned( int)?", 0, 2 ** 32 - 1), (r"int32_t|int|signed( int)?", -2 ** 31, 2 ** 31 - 1),
          (r"uint64_t|unsigned long( long)?( int)?|size_t|uintptr_t", 0, 2 ** 64 - 1),
          (r"int64_t|long( long)?( int)?|signed long( long)?|ssize_t|ptrdiff_t|intptr_t", -2 ** 63, 2 ** 63 - 1)]
NULLS = {"NULL", "nullptr", "0", "(void*)0", "(void *)0", "((void*)0)", "((void *)0)"}
PLAN_FORBIDDEN = re.compile(r"#\s*include|\bmain\s*\(|\bsystem\s*\(|\bpopen\s*\(|\bexec\w*\s*\(|\bfopen\s*\(|"
                            r"\bremove\s*\(|\bunlink\s*\(|\bwhile\b|\bfor\s*\(|\bgoto\b|__asm|\basm\b|\breturn\b")
PLAN_SAFE_CALLS = {"sizeof", "strlen", "strcmp", "memset", "memcpy", "strcpy", "strncpy", "snprintf", "static_cast",
                   "reinterpret_cast", "const_cast"}
LOCAL_DECL = re.compile(r"^\s*(?:(?:const|static|struct|enum|union|unsigned|signed|volatile)\s+)*[A-Za-z_][\w:<>]*"
                        r"(?:\s+[A-Za-z_]\w*)?\s*[*&\s]*\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])*\s*(?:=|;|\{|\()")


def norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[/*]+", " ", text)).strip().lower()


def eval_int(expr: str, constants: dict[str, int]) -> int | None:
    """Integer value of a C constant expression made of literals, enumerators and macros (no calls)."""
    e = expr.strip()
    if not e or len(e) > 200:
        return None
    e = re.sub(r"\(\s*(?:(?:const|unsigned|signed|enum)\s+)*[A-Za-z_]\w*(?:\s+(?:int|long|char|short))*\s*\)\s*"
               r"(?=[\w(+\-~'])", "", e)                                   # casts
    e = re.sub(r"'\\?(.)'", lambda m: str(ord(m.group(1))), e)              # character literals
    e = re.sub(r"\b(0[xX][0-9a-fA-F]+|\d+)[uUlL]+\b", r"\1", e)
    e = re.sub(r"\b0([0-7]+)\b", lambda m: str(int(m.group(1), 8)), e)
    e = e.replace("&&", " and ").replace("||", " or ").replace("!", " not ").replace(" not =", "!=")
    try:
        tree = ast.parse(e, mode="eval")
    except SyntaxError:
        return None

    def walk(node) -> int:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, bool)):
            return int(node.value)
        if isinstance(node, ast.Name):
            name = node.id.split("::")[-1]
            if name in constants:
                return constants[name]
            raise ValueError(name)
        if isinstance(node, ast.Attribute):                                 # Mode::X parsed oddly: not supported
            raise ValueError("attribute")
        if isinstance(node, ast.UnaryOp):
            v = walk(node.operand)
            return {ast.USub: -v, ast.UAdd: v, ast.Invert: ~v, ast.Not: int(not v)}[type(node.op)]
        if isinstance(node, ast.BinOp):
            a, b = walk(node.left), walk(node.right)
            op = type(node.op)
            if op in (ast.Div, ast.FloorDiv):
                if b == 0:
                    raise ValueError("division by zero")
                return int(a / b)
            return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b, ast.Mod: a % b if b else 0, ast.LShift: a << b,
                    ast.RShift: a >> b, ast.BitOr: a | b, ast.BitAnd: a & b, ast.BitXor: a ^ b}[op]
        raise ValueError(type(node).__name__)
    try:
        return walk(tree)
    except (ValueError, KeyError, TypeError, OverflowError):
        return None


def int_range(ctype: str) -> tuple[int, int] | None:
    t = _plain_type(re.sub(r"\b(const|volatile|std::)\b", " ", ctype)).strip()
    t = t.replace("std::", "")
    for pattern, lo, hi in RANGES:
        if re.fullmatch(pattern, t):
            return lo, hi
    return None


def string_literal(value: str) -> bool:
    return bool(re.fullmatch(r'(?:L|u8|u|U)?"(?:[^"\\]|\\.)*"', value.strip()))


def setup_locals(setup: list[str]) -> set[str]:
    names = set()
    for stmt in setup:
        for part in stmt.split(";"):
            m = LOCAL_DECL.match(part + ";")
            if m and m.group(1) not in KEYWORDS:
                names.add(m.group(1))
    return names


def text_params(fn: uc.FunctionInfo) -> list[uc.ParamInfo]:
    """Parameters from the signature text when the front end could not parse the SUT [A1-DA14]."""
    if fn.params or "(" not in fn.signature:
        return list(fn.params)
    try:
        _, params = parse_declaration(fn.signature.split("{")[0].strip() + ";")
    except (ValueError, AttributeError):
        return []
    out = []
    for ptype, name in params:
        plain = _plain_type(ptype)
        kind = "int" if INTEGRAL_T.match(plain) else "float" if FLOAT_T.match(plain) else \
            "cstring" if re.fullmatch(r"(const\s+)?char\s*\*", ptype.strip()) else \
            "ptr_scalar" if ptype.strip().endswith("*") else "other"
        out.append(uc.ParamInfo(name=name, type=ptype, kind=kind))
    return out


def return_type_of(fn: uc.FunctionInfo) -> str:
    """The return type: from the front end, else from the signature text [A1-DA14]."""
    if fn.return_type or "(" not in fn.signature:
        return fn.return_type
    try:
        rt, _ = parse_declaration(fn.signature.split("{")[0].strip() + ";")
    except (ValueError, AttributeError):
        return ""
    return re.sub(r"\b(static|inline|extern|virtual|explicit)\b", "", rt).strip()


def items_in(items: list[str], first: int, last: int) -> list[str]:
    """Covered-item ids (LC:12, BC:12:5:T, MCDC:12:5:1) on the lines of one function."""
    out = []
    for item in items:
        parts = item.split(":")
        if len(parts) > 1 and parts[1].isdigit() and first <= int(parts[1]) <= last:
            out.append(item)
    return out


# ============================================================================ the agent
class AnalyzerAgent(ba.AgentCore):
    name = "agent_unittest_analyzer"
    application = "UT"                                            # VIEW-2
    view_role, view_description, view_support = uc.VIEW_AGENTS["agent_unittest_analyzer"]

    def view_summary(self, model):
        return uc.view_summary(self.view_label, model) or super().view_summary(model)

    def __init__(self, settings: ba.Settings | None = None, *, config: AnalyzerConfig | None = None,
                 router: ba.BrainRouter | None = None):
        self.cfg = config or AnalyzerConfig()
        self._link_cache: dict[str, list[uc.RequirementLink]] = {}
        self._variant_cache: dict[str, list[tuple[uc.BuildVariant, dict[str, int]]]] = {}   # A1-37a, A1-37e
        super().__init__(settings, router=router)

    def tools(self) -> list[ba.ContractTool]:
        def discover_pre(payload: uc.DiscoverRequest, request):
            root = Path(payload.repo_root)
            if not root.is_dir():
                raise ba.ContractRejected("repository root does not exist")
            if not (root / payload.sut_file).is_file():
                raise ba.ContractRejected("SUT file does not exist")
            if ".." in Path(payload.sut_file).parts:
                raise ba.ContractRejected("SUT path must stay inside the repository")

        async def discover(payload, request, scratch):
            return self.discover(payload)

        def plan_pre(payload: uc.PlanRequest, request):
            uc.check_signature(payload.layout, request)
            sut = Path(payload.layout.repo_root) / payload.layout.sut_file
            if uc.file_sha256(sut) != payload.layout.sut_sha256 and payload.last is None:
                raise ba.ContractRejected("SUT changed since discovery; run discover again [ORC-10]")

        async def plan(payload, request, scratch):
            return await self.plan(payload)

        def evidence_pre(payload: uc.EvidenceRequest, request):
            uc.check_signature(payload.layout, request)
            if not any(payload.function in (f.name, f.qualified_name) for f in payload.layout.functions):
                raise ba.ContractRejected(f"{payload.function} is not a function of the SUT [A1-24]")

        async def evidence(payload: uc.EvidenceRequest, request, scratch):
            return self.collect_evidence(payload.layout, payload.function)

        def deps_pre(payload: uc.DependencyRequest, request):
            uc.check_signature(payload.layout, request)
            sut = Path(payload.layout.repo_root) / payload.layout.sut_file
            if not sut.is_file() or uc.file_sha256(sut) != payload.layout.sut_sha256:
                raise ba.ContractRejected("SUT changed since discovery; run discover again [ORC-10]")

        async def deps(payload: uc.DependencyRequest, request, scratch):
            return await self.dependencies(payload)

        def deps_post(payload: uc.DependencyRequest, layout: uc.LayoutProfile) -> None:
            if layout.sut_sha256 != payload.layout.sut_sha256:
                raise ba.AgentFault("CONTRACT_VIOLATION", "the dependency plan is bound to another SUT version")
            if layout.dependencies and layout.sut_file in layout.dependencies.real_sources:
                raise ba.AgentFault("CONTRACT_VIOLATION", "the SUT cannot be its own real dependency [A1-DA9]")

        return [ba.ContractTool("discover", uc.DiscoverRequest, uc.LayoutProfile, discover, discover_pre,
                                read_only=True, description="Layout, functions, tests and stubs; no LLM [A1-31a]."),
                ba.ContractTool("dependencies", uc.DependencyRequest, uc.LayoutProfile, deps, deps_pre, deps_post,
                                description="Front-end analysis of the SUT with its recorded flags; every external "
                                            "dependency decided stub, real or system [A1-DA1..A1-DA14]."),
                ba.ContractTool("plan", uc.PlanRequest, uc.UTPlan, plan, plan_pre, self._plan_post,
                                description="Setup-repair or coverage plan for Agent2; scenarios suggested by the "
                                            "brain and checked here [A1-22, A1-26, A1-PL1..A1-PL14]."),
                ba.ContractTool("collect_evidence", uc.EvidenceRequest, uc.EvidenceResult, evidence, evidence_pre,
                                read_only=True, description="Expected-behaviour evidence for one function [A1-25j].")]

    @staticmethod
    def _plan_post(payload: uc.PlanRequest, plan: uc.UTPlan) -> None:
        names = {f.name for f in payload.layout.functions} | {f.qualified_name for f in payload.layout.functions}
        for s in plan.scenarios:
            if s.function not in names:
                raise ba.AgentFault("CONTRACT_VIOLATION", f"scenario for unknown function {s.function} [A1-24]")
            if s.made_by != "rule" and not s.checked:
                raise ba.AgentFault("CONTRACT_VIOLATION", f"{s.scenario_id} did not pass Agent1's check [A1-PL10]")

    # ---------------------------------------------------------------- discovery [A1-1..17]
    def discover(self, req: uc.DiscoverRequest) -> uc.LayoutProfile:
        root = Path(req.repo_root).resolve()
        sut_rel = req.sut_file.replace("\\", "/")
        sut = root / sut_rel
        language = "cpp" if sut.suffix.lower() in CPP_EXT else "c"
        source = sut.read_text(encoding="utf-8", errors="replace")
        component = self._component_root(root, sut)
        files = [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts and "ut_work" not in p.parts]
        rel = lambda p: str(p.relative_to(root)).replace("\\", "/")
        headers_all = [p for p in files if p.suffix.lower() in HEADER_EXT]
        included = re.findall(r'#\s*include\s*"([^"]+)"', source)
        include_dirs, headers = [], []
        for inc in included:
            for h in headers_all:
                if rel(h) == inc or rel(h).endswith("/" + inc):        # "ctrl.h" is not "brake_ctrl.h"
                    directory = rel(h)[: -len(inc)].rstrip("/") or "."
                    if directory not in include_dirs:
                        include_dirs.append(directory)
                    headers.append(rel(h))
                    break
        for candidate in ("include", "inc"):
            if (component / candidate).is_dir() and rel(component / candidate) not in include_dirs:
                include_dirs.append(rel(component / candidate))
        test_dirs = sorted({rel(p.parent) for p in files if p.suffix.lower() in C_EXT + CPP_EXT and
                            re.search(r"(^|/)(tests?|unittests?|ut)(/|$)", rel(p.parent), re.I)})
        stub_dirs = sorted({rel(p.parent) for p in files if re.search(r"(^|/)(stubs?|mocks?|fakes?)(/|$)",
                                                                      rel(p.parent), re.I)})
        # The delivered harness (ut_harness.c/.h) is infrastructure, and stubs are not tests.
        test_files = [rel(p) for p in files if rel(p.parent) in test_dirs and p.suffix.lower() in C_EXT + CPP_EXT
                      and not p.name.startswith("ut_harness.") and rel(p.parent) not in stub_dirs]
        stub_files = [rel(p) for p in files if rel(p.parent) in stub_dirs]
        read_only = [d for d in stub_dirs if re.search(r"generated|auto", d, re.I)]
        functions = find_functions(source, sut_rel)
        names = {f.name for f in functions}
        existing, framework = [], "ut_harness"
        executable = []
        for tf in test_files:
            text = (root / tf).read_text(encoding="utf-8", errors="replace")
            if "generated by agent_unittest_generator" in text[:600]:
                # This pipeline's own files from an earlier delivery: never existing tests, never E5
                # evidence (that would turn observed behaviour into expected behaviour) [A1-25d].
                continue
            fw = next((name for name, marker in FRAMEWORKS if marker in text.lower() or marker in text), "")
            for m in re.finditer(r"UT_TEST\(\s*(\w+)\s*\)|TEST(?:_F)?\(\s*(\w+)\s*,\s*(\w+)\s*\)|"
                                 r"void\s+(test_\w+)\s*\(\s*void\s*\)", text):
                name = next(g for g in m.groups() if g)
                body = text[m.end(): m.end() + 2000]
                targets = sorted(n for n in names if re.search(r"\b" + re.escape(n) + r"\s*\(", body))
                existing.append(uc.TestCaseInfo(name=name, file=tf, line=line_of(text, m.start()),
                                                enabled="DISABLED_" not in name, targets=targets, framework=fw))
            # Built with this SUT: the project's own tests of it - never files this pipeline generated
            # (stubs, access files, suites of other SUT files delivered by earlier runs) [ORC-23].
            if fw == "ut_harness" and any(sut_name in case.targets for case in existing if case.file == tf
                                          for sut_name in names):
                executable.append(tf)
        default_test_dir = req.test_dir or (test_dirs[0] if test_dirs else rel(component / "tests") if component != root
                                            else "tests")
        default_stub_dir = req.stub_dir or next((d for d in stub_dirs if d not in read_only),
                                                default_test_dir.rstrip("/") + "/stubs")
        suite = f"{default_test_dir.rstrip('/')}/test_{sut.stem}.{'cpp' if language == 'cpp' else 'c'}"
        build = self._build_system(root, component)
        requirement_files = self._requirement_files(root, files)
        blocked, reason = False, None
        if not functions:
            blocked, reason = True, "no function definitions found in the SUT"
        if default_stub_dir in read_only:
            blocked, reason = True, "only read-only/generated stub locations exist [A1-17]"
        return uc.LayoutProfile(
            repo_root=str(root), sut_file=sut_rel, language=language, component_root=rel(component) if component != root
            else ".", include_dirs=include_dirs, headers=headers, build_system=build, test_dirs=test_dirs,
            test_files=test_files, stub_dirs=stub_dirs, stub_files=stub_files, writable_test_dir=default_test_dir,
            writable_stub_dir=default_stub_dir, suite_file=suite, framework="ut_harness", existing_tests=existing,
            executable_tests=[t for t in executable if t != suite], functions=functions,
            sut_sha256=uc.file_sha256(sut), blocked=blocked, blocked_reason=reason,
            requirement_files=requirement_files)

    @staticmethod
    def _build_system(root: Path, component: Path) -> str:
        for base in (component, root):
            names = {p.name for p in base.iterdir()} if base.is_dir() else set()
            for system, markers in (("cmake", {"CMakeLists.txt"}), ("meson", {"meson.build"}),
                                    ("bazel", {"MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel"}),
                                    ("ninja", {"build.ninja"}), ("make", {"Makefile", "makefile", "GNUmakefile"})):
                if names & markers:
                    return system
            if any(n.endswith((".sln", ".vcxproj")) for n in names):
                return "msbuild"
        return "none"

    def _requirement_files(self, root: Path, files: list[Path]) -> list[str]:
        """ReqIF and Markdown requirement documents: configured patterns, else found by name [A1-25x]."""
        found = []
        for pattern in self.cfg.requirement_files:
            found += [p for p in root.glob(pattern) if p.is_file()]
        if not self.cfg.requirement_files:
            found = [p for p in files if p.suffix.lower() in (".reqif", ".reqifz") or (
                p.suffix.lower() in (".md", ".txt") and re.search(r"req|spec", p.stem, re.I))]
        rel = lambda p: str(p.relative_to(root)).replace("\\", "/")
        return sorted({rel(p) for p in found if "ut_output" not in p.relative_to(root).parts})[:20]

    @staticmethod
    def _component_root(root: Path, sut: Path) -> Path:
        for parent in sut.parents:
            if any((parent / marker).exists() for marker in ("CMakeLists.txt", "Makefile", "meson.build")):
                return parent
            if parent == root:
                break
        return sut.parent.parent if sut.parent != root else root

    # ---------------------------------------------------------------- evidence [A1-25a..25j]
    def collect_evidence(self, layout: uc.LayoutProfile, function: str) -> uc.EvidenceResult:
        root = Path(layout.repo_root)
        fn = next((f for f in layout.functions if function in (f.name, f.qualified_name)), None)
        if fn is None:
            return uc.EvidenceResult(function=function, exhausted=True)
        short = fn.name.split("::")[-1]
        refs: list[uc.EvidenceRef] = []

        def add(cls: str, source: str, line: int, text: str, linked_by: str = "") -> None:
            text = text.strip()
            if text and not any(r.text == text and r.source == source for r in refs):
                refs.append(uc.EvidenceRef(evidence_id=f"EV-{short}-{len(refs) + 1}", evidence_class=cls,
                                           source=source, line=line, text=text[:600], linked_by=linked_by))

        sut_text = (root / layout.sut_file).read_text(encoding="utf-8", errors="replace")
        for header in [layout.sut_file] + layout.headers:
            path = root / header
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"(/\*(?:[^*]|\*(?!/))*\*/|(?://[^\n]*\n)+)\s*(?:[\w \t\*&:<>,]*\n?)??"
                                 r"[\w \t\*&:<>,]*?\b" + re.escape(short) + r"\s*\(", text):
                comment = m.group(1)
                line = line_of(text, m.start())
                for tag in re.findall(r"\b([A-Z]{2,}(?:-[A-Z0-9]+)*-\d+)\b", comment):
                    add("E1", header, line, f"{tag}: {re.sub(r'[/*]+', ' ', comment).strip()}")
                cleaned = re.sub(r"^\s*(/\*\*?|\*/|\*|//)", "", comment, flags=re.M).replace("*/", "")
                add("E2", header, line, re.sub(r"\s+", " ", cleaned))
        tags = {r.text.split(":", 1)[0] for r in refs if r.evidence_class == "E1"}
        for rid, text, source, line in read_requirements(root, layout.requirement_files):
            if rid in tags or re.search(r"\b" + re.escape(short) + r"\b", text):
                add("E1", source, line, f"{rid}: {text}")             # requirement document [A1-25x]
        body = "\n".join(sut_text.splitlines()[fn.line_start - 1: fn.line_end])
        for m in re.finditer(r"\b(assert|static_assert)\s*\((.*?)\)\s*;", body):
            add("E3", layout.sut_file, fn.line_start + body[: m.start()].count("\n"), m.group(0))
        macros = {}
        for header in layout.headers:
            text = (root / header).read_text(encoding="utf-8", errors="replace") if (root / header).is_file() else ""
            for m in re.finditer(r"#[ \t]*define[ \t]+([A-Z_][A-Z0-9_]*)[ \t]+([^\n]+)", text):
                macros[m.group(1)] = (header, line_of(text, m.start()), m.group(0))
        for name, (header, line, text) in macros.items():
            if re.search(r"\b" + name + r"\b", body):
                add("E4", header, line, text)
        for case in layout.existing_tests:
            if short in case.targets:
                path = root / case.file
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                for number in range(case.line, min(case.line + 30, len(lines))):
                    if re.search(r"(EXPECT|ASSERT|TEST_ASSERT|assert)\w*\s*\(", lines[number]) and short in lines[number]:
                        add("E5", case.file, number + 1, lines[number])
        callers = 0
        for path in Path(layout.repo_root).rglob("*"):
            if callers >= self.cfg.max_callers or not path.is_file() or path.suffix.lower() not in C_EXT + CPP_EXT:
                continue
            rel = str(path.relative_to(root)).replace("\\", "/")
            if rel == layout.sut_file or rel in layout.test_files:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"[^\n]*\b" + re.escape(short) + r"\s*\([^\n]*", text):
                add("E6", rel, line_of(text, m.start()), m.group(0))
                callers += 1
                break
        for link in layout.requirement_links:                      # last, so the other ids stay stable [A1-PL13]
            if link.function == fn.name:
                add("E1", link.source, link.line, f"{link.requirement_id}: {link.quote}",
                    f"brain p={link.probability:.2f}")
        return uc.EvidenceResult(function=fn.name, evidence=refs, exhausted=True)


    # ---------------------------------------------------------------- dependency analysis [A1-DA1 .. A1-DA14]
    async def dependencies(self, req: uc.DependencyRequest) -> uc.LayoutProfile:
        layout = req.layout.model_copy(deep=True)
        build = req.build
        layout.build, layout.compile_flags, layout.build_flags_source = build, list(build.sut_flags), build.source
        repo = Path(layout.repo_root).resolve()
        previous = {d.name: d for d in (req.layout.dependencies.entries if req.layout.dependencies else [])}
        try:
            tools = uc.Tools.load(self.cfg.tools_manifest or None)
        except ba.AgentFault as exc:
            layout = self._text_dependencies(layout, f"front end unavailable: {exc.message}")
            layout.requirement_links = await self._link_requirements(layout)
            return layout
        sut = FrontEnd(tools, repo, layout.sut_file, build.sut_flags, layout.language)
        ok = await asyncio.to_thread(sut.parse)
        if sut.ast is None:
            layout = self._text_dependencies(layout, "the front end could not parse the SUT: " +
                                             "; ".join(sut.errors)[:300], sut.missing)
            layout.requirement_links = await self._link_requirements(layout)
            return layout
        functions, nodes = self.frontend_functions(sut, layout)
        if functions:
            layout.functions = functions                                          # A1-DA2
            layout.blocked, layout.blocked_reason = False, None
        layout.enums = sut.enums()
        plan = uc.DependencyPlan(headers=sut.headers)
        if sut.missing:                                                           # A1-DA13
            searched = [f for f in uc.expand_flags(build.sut_flags, repo) if f.startswith(("-I", "-isystem"))]
            plan.missing_headers = [f"{h} (searched: {', '.join(searched) or 'no include paths'})" for h in sut.missing]
        if not ok and sut.errors:
            plan.notes.append("front-end errors: " + "; ".join(sut.errors[:3]))
        sut_names = {f.qualified_name for f in functions} | {f.name for f in functions}
        layout.sut_state = []
        for node, qualified in sut.definitions():          # the SUT's own globals are part of the SUT [A1-DA15]
            if node.get("kind") != "VarDecl":
                continue
            sut_names |= {qualified, node.get("name", "")}
            qt = (node.get("type") or {}).get("qualType", "")
            const = (qt.startswith("const ") and "*" not in qt) or bool(re.search(r"\*\s*const\s*$", qt)) or \
                node.get("constexpr")
            plain = qualified.replace("(anonymous namespace)::", "")
            if not const and node.get("storageClass") != "extern" and "::" not in plain and node.get("name"):
                layout.sut_state.append(uc.StateVar(name=node["name"], type=qt))
        sut_text = (repo / layout.sut_file).read_text(encoding="utf-8", errors="replace")
        units: dict[str, FrontEnd | None] = {layout.sut_file: sut}
        queue = [(ref, 0, "") for node in nodes.values() for ref in sut.references(node)]
        decided: dict[str, uc.Dependency] = {}
        real_units: dict[str, str] = {}                                           # file -> the dependency that brought it
        while queue:
            ref, depth, via = queue.pop(0)
            dep = self._describe_dependency(sut if depth == 0 else units.get(via) or sut, ref, repo, sut_text, depth)
            if dep is None or dep.qualified_name in sut_names or dep.name in sut_names or \
                    (dep.qualified_name or dep.name) in decided:
                continue
            key = dep.qualified_name or dep.name
            old = previous.get(dep.name)
            dep.attempts = old.attempts if old else 0
            if not dep.system:
                dep.existing_stub = self._existing_stub(layout, dep)
                if not dep.existing_stub:
                    found = await asyncio.to_thread(self._find_definition, dep, layout, build, tools, units)
                    if found:
                        dep.definition = found
            await self._decide(dep, layout, units, real_units, sut_text)
            revision = self._revision(dep, req.last, old)                          # A1-DA12
            if revision:
                dep.decision, dep.decided_by, dep.reason = "stub", "revision", revision
                dep.attempts += 1
            decided[key] = dep
            if dep.decision == "real":
                if dep.definition not in real_units:
                    real_units[dep.definition] = key
                    unit = units.get(dep.definition)
                    for node, _ in (unit.definitions() if unit else []):
                        if node.get("kind") != "VarDecl":
                            queue += [(r, depth + 1, dep.definition) for r in unit.references(node)]
        # A real translation unit brings every definition in it: those dependencies are real too.
        for dep in decided.values():
            if dep.decision == "stub" and dep.definition in real_units and dep.decided_by != "revision":
                dep.decision, dep.decided_by = "real", "rule"
                dep.reason = f"defined in {dep.definition}, which is linked for {real_units[dep.definition]} [A1-DA10]"
        plan.entries = sorted(decided.values(), key=lambda d: (d.depth, d.name))
        plan.real_sources = sorted({d.definition for d in plan.entries if d.decision == "real" and d.definition})
        # declarations proposed earlier for symbols without one stay in the plan [A1-PL14]
        plan.entries += [d for d in previous.values() if d.name not in {e.name for e in plan.entries}
                         and (d.decided_by.startswith("brain declaration") or "[A1-PL14]" in d.reason)]
        layout.dependencies = plan
        layout.requirement_links = await self._link_requirements(layout)                # A1-PL13
        return layout

    def frontend_functions(self, fe: FrontEnd, layout: uc.LayoutProfile) -> tuple[list[uc.FunctionInfo], dict[str, dict]]:
        src = fe.source_bytes(fe.path)
        enums = {}
        for e in fe.enums():
            enums[e.name] = e
            enums.setdefault(e.name.split("::")[-1], e)
        out, nodes = [], {}
        for node, qualified in fe.definitions():
            if node.get("kind") == "VarDecl" or not qualified:
                continue
            begin, end = _offset(node["range"]["begin"]), _end_offset(node["range"]["end"])
            body = next(c for c in node["inner"] if c.get("kind") == "CompoundStmt")
            body_begin = _offset(body["range"]["begin"])
            if begin is None or end is None or body_begin is None:
                continue
            header = re.sub(r"\s+", " ", src[begin:body_begin].decode("utf-8", "replace")).strip()
            if re.search(r"\)\s*:", header):                       # constructor initializer list
                header = header[: _close_paren(header) + 1]
            body_text = src[body_begin:end].decode("utf-8", "replace")
            qt = (node.get("type") or {}).get("qualType", "")
            ret = qt[: qt.index("(")].strip() if "(" in qt and node["kind"] not in ("CXXConstructorDecl",
                                                                                     "CXXDestructorDecl") else ""
            params = [fe.param_info(p, enums, body_text) for p in node.get("inner", []) if p.get("kind") == "ParmVarDecl"]
            calls = sorted({r["name"] for r in fe.references(node) if r["kind"] != "VarDecl"})
            name = qualified if "::" in qualified else node.get("name", qualified)
            nodes[node["id"]] = node
            out.append(uc.FunctionInfo(
                name=name, qualified_name=qualified, signature=header, file=layout.sut_file,
                line_start=src.count(b"\n", 0, begin) + 1, line_end=src.count(b"\n", 0, end) + 1,
                is_static=node.get("storageClass") == "static" or "(anonymous namespace)" in qualified,
                calls=calls, return_type=ret, params=params, from_frontend=True,
                member=node["kind"] in ("CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl")))
        out.sort(key=lambda f: f.line_start)
        return out, nodes

    def _describe_dependency(self, fe: FrontEnd, ref: dict, repo: Path, sut_text: str, depth: int) -> uc.Dependency | None:
        kind = {"FunctionDecl": "function", "CXXMethodDecl": "method", "CXXConstructorDecl": "constructor",
                "VarDecl": "variable"}.get(ref["kind"])
        name = ref.get("name", "")
        if not kind or not name or name.startswith("__builtin") or name.startswith("operator"):
            return None
        node = fe.ast.nodes.get(ref.get("id", ""), {})
        path, offset = fe.ast.decl_location(ref.get("id", "")) if ref.get("id") else ("", -1)
        if kind == "constructor" and not node:
            return None                                   # implicit or system class: nothing to stub
        qualified = fe.ast.qualified_name(node) if node else name
        if kind == "constructor" and ref.get("class"):
            qualified = f"{ref['class']}::{ref['class'].split('::')[-1]}"
        rel = fe.ast.rel(path) if path else ""
        # Members of standard-library templates (std::array<int, 5>::back) are instantiated where they are used,
        # so their location can point into the repository: the namespace decides [A1-DA7].
        std = r"^(?:const\s+|volatile\s+)*(::)?(std|__gnu_cxx|__cxx11|__1)::"
        system = not rel or bool(re.match(std, qualified or "")) or bool(re.match(std, ref.get("owner", "")))
        if system:
            rel = ""
        dep = uc.Dependency(name=name, qualified_name=qualified if qualified != name else "", kind=kind,
                            type=ref.get("type", ""), header=rel, system=system, depth=depth)
        if node:
            src = fe.source_bytes(path)
            b, e = _offset(node.get("range", {}).get("begin", {})), _end_offset(node.get("range", {}).get("end", {}))
            if b is not None and e is not None and e > b:
                dep.declaration = re.sub(r"\s+", " ", src[b:e].decode("utf-8", "replace")).strip()
                dep.line = src.count(b"\n", 0, b) + 1
            qt = (node.get("type") or {}).get("qualType", "")
            if kind == "variable":
                dep.is_const = qt.startswith("const ") or bool(re.match(r"^\w[\w\s:]*\bconst\b", qt))
            else:
                if kind != "constructor" and "(" in qt:
                    dep.return_type = qt[: qt.index("(")].strip()
                m = re.search(r"\)\s*((?:const|volatile|noexcept|&|&&|\s)+)$", qt)
                dep.method_suffix = (" " + m.group(1).strip()) if m and kind == "method" else ""
                dep.params = [[(p.get("type") or {}).get("qualType", ""), p.get("name") or f"arg{i}"]
                              for i, p in enumerate(c for c in node.get("inner", []) if c.get("kind") == "ParmVarDecl")]
        pattern = re.compile(r"\b" + re.escape(name) + r"\b")
        decision_line = re.compile(r"\b(if|while|switch|for)\b|\?|&&|\|\||[<>]=?|[!=]=|\breturn\b")
        dep.used_in_decision = any(pattern.search(line) and decision_line.search(line) for line in sut_text.splitlines())
        return dep

    @staticmethod
    def _existing_stub(layout: uc.LayoutProfile, dep: uc.Dependency) -> str:
        """A stub file of the repository that already defines the dependency [A1-DA5, A1-10]."""
        root = Path(layout.repo_root)
        if dep.kind == "variable":
            pattern = re.compile(r"^[^;(){}]*\b" + re.escape(dep.name) + r"\b\s*(\[[^\]]*\])?\s*(=[^;]*)?;", re.M)
        else:
            pattern = re.compile(r"\b" + re.escape(dep.name) + r"\s*\([^;{}]*\)\s*(const\s*)?\{")
        for stub in layout.stub_files:
            path = root / stub
            if path.suffix.lower() in C_EXT + CPP_EXT and path.is_file():
                raw = path.read_text(encoding="utf-8", errors="replace")
                if "generated by agent_unittest_generator" in raw[:600]:
                    continue                    # this pipeline's own stub of an earlier delivery: regenerated
                text = strip_comments(raw)
                if pattern.search(text) and "extern" not in (pattern.search(text).group(0)):
                    return stub
        return ""

    def _find_definition(self, dep: uc.Dependency, layout: uc.LayoutProfile, build: uc.BuildDescription,
                         tools: uc.Tools, units: dict[str, Any]) -> str:
        """The translation unit of the repository that defines the dependency [A1-DA5]."""
        root = Path(layout.repo_root).resolve()
        skip = set(layout.test_files) | set(layout.stub_files) | {layout.sut_file}
        entries = {e.file: e for e in build.entries if e.file not in skip}
        if build.estimated or not entries:
            for path in root.rglob("*"):
                rel = str(path.relative_to(root)).replace("\\", "/")
                if path.suffix.lower() in C_EXT + CPP_EXT and rel not in skip and not any(
                        part in ("ut_work", "ut_output", ".git", "build") for part in path.relative_to(root).parts):
                    entries.setdefault(rel, uc.CompileEntry(file=rel, flags=build.sut_flags,
                                                            language="cpp" if path.suffix.lower() in CPP_EXT else "c"))
        word = re.compile(r"\b" + re.escape(dep.name) + r"\b")
        checked = 0
        for rel, entry in sorted(entries.items()):
            path = root / rel
            try:
                if not word.search(path.read_text(encoding="utf-8", errors="replace")):
                    continue
            except OSError:
                continue
            checked += 1
            if checked > self.cfg.max_definition_units:
                break
            if rel not in units:
                unit = FrontEnd(tools, root, rel, entry.flags, entry.language)
                unit.parse()
                units[rel] = unit if unit.ast is not None else None
            unit = units[rel]
            if unit is None:
                continue
            for node, qualified in unit.definitions():
                if qualified == (dep.qualified_name or dep.name) or (not dep.qualified_name and node.get("name") == dep.name):
                    if (dep.kind == "variable") == (node.get("kind") == "VarDecl"):
                        return rel
        return ""

    async def _decide(self, dep: uc.Dependency, layout: uc.LayoutProfile, units: dict[str, Any],
                      real_units: dict[str, str], sut_text: str) -> None:
        """Rules first [A1-DA7]; the LLM only for a dependency with a definition in the repository
        [A1-DA8]; its answer is checked [A1-DA9]."""
        if dep.system:
            dep.decision, dep.reason = "system", "standard library, compiler runtime or SDK [A1-DA7]"
            if dep.name in NONDETERMINISTIC and dep.used_in_decision:
                answer = await self._ask(SYSTEM_QUESTION, dep, layout, units, sut_text)
                if answer and answer[0] == "stub":
                    dep.decision, dep.decided_by = "stub", f"llm p={answer[1]:.2f}"
                    dep.reason = "non-deterministic system service whose result controls SUT decisions [A1-DA8]"
            return
        if dep.existing_stub:
            dep.decision, dep.reason = "stub", f"the existing stub {dep.existing_stub} is reused [A1-DA7]"
            return
        if not dep.definition:
            dep.decision, dep.reason = "stub", "no definition in the repository [A1-DA7]"
            return
        if dep.definition in real_units:
            dep.decision, dep.reason = "real", f"{dep.definition} is already linked for {real_units[dep.definition]}"
            return
        if dep.depth >= self.cfg.real_dependency_depth:
            dep.decision, dep.reason = "stub", f"beyond the configured depth of {self.cfg.real_dependency_depth} [A1-DA10]"
            return
        if dep.definition == layout.sut_file:
            dep.decision, dep.reason = "stub", "defined in the SUT itself [A1-DA9]"
            return
        answer = await self._ask(DEP_QUESTION, dep, layout, units, sut_text)
        if answer is None:
            dep.decision, dep.decided_by = "stub", "fallback"
            dep.reason = "no LLM available: every repository dependency is stubbed [A1-DA9]"
            return
        choice, p = answer
        further = self._further(dep, units)
        if choice == "real" and further is None:
            dep.decision, dep.decided_by = "stub", f"llm p={p:.2f}, refused"
            dep.reason = "real refused: its further dependencies cannot be analyzed [A1-DA9]"
            return
        dep.decision, dep.decided_by = choice, f"llm p={p:.2f}"
        dep.reason = ("small deterministic computation: the real behaviour is used" if choice == "real" else
                      "controls SUT decisions or has side effects: stubbed") + " [A1-DA8]"

    @staticmethod
    def _further(dep: uc.Dependency, units: dict[str, Any]) -> list[str] | None:
        unit = units.get(dep.definition)
        if unit is None:
            return None
        names = set()
        own = {q for _, q in unit.definitions()}
        for node, _ in unit.definitions():
            if node.get("kind") != "VarDecl":
                names |= {r["name"] for r in unit.references(node)}
        return sorted(n for n in names if n not in own and n != dep.name)

    async def _ask(self, question: dict, dep: uc.Dependency, layout: uc.LayoutProfile, units: dict[str, Any],
                   sut_text: str) -> tuple[str, float] | None:
        unit = units.get(dep.definition) if dep.definition else None
        definition = ""
        if unit is not None:
            for node, qualified in unit.definitions():
                if qualified == (dep.qualified_name or dep.name):
                    src = unit.source_bytes(unit.path)
                    b, e = _offset(node["range"]["begin"]), _end_offset(node["range"]["end"])
                    definition = src[b:e].decode("utf-8", "replace")[:3000] if b is not None and e else ""
        usage = [f"{n}: {line.strip()}" for n, line in enumerate(sut_text.splitlines(), 1)
                 if re.search(r"\b" + re.escape(dep.name) + r"\b", line)][:12]
        state = json.dumps({"dependency": dep.qualified_name or dep.name, "kind": dep.kind,
                            "declaration": dep.declaration, "declared_in": dep.header or "system",
                            "definition_file": dep.definition, "definition": definition,
                            "further_dependencies": self._further(dep, units) if dep.definition else [],
                            "sut_usage": usage, "sut_decisions_depend_on_it": dep.used_in_decision})
        try:
            answer = (await self.decide([uc.part("dependency", state)], question, step="A1.dependency",
                                        project=layout.repo_root,
                                        need=ba.OutputNeed(items=1, tokens_per_item=60, overhead=250)))["decision"]
        except ba.AgentFault as exc:
            LOG.info("dependency decision by the brain unavailable (%s)", exc.code)
            return None
        return answer["choice"], float(answer["probabilities"][answer["choice"]])

    @staticmethod
    def _revision(dep: uc.Dependency, last: uc.VerificationResult | None, old: uc.Dependency | None) -> str:
        """A1-DA12: a decision shown wrong by Agent3's results is revised (counted)."""
        if old is not None and old.decided_by == "revision" and old.decision == "stub":
            return old.reason                                         # keep earlier revisions
        if last is None or dep.decision != "real":
            return ""
        for f in last.failures:
            text = " ".join(f.evidence) + " " + f.diagnostic
            if f.classification == "build_configuration_defect" and (
                    f"real dependency {dep.name}" in text or (dep.definition and dep.definition in text) or
                    f.symbol == dep.name):
                return f"the real definition failed to build or link ({f.diagnostic[:120]}): stubbed [A1-DA12]"
            if f.kind in ("crash", "timeout") and dep.name in (f.affected_function, f.symbol):
                return "the real definition crashed or hung a test: stubbed [A1-DA12]"
        return ""

    def _text_dependencies(self, layout: uc.LayoutProfile, why: str, missing: list[str] | None = None) -> uc.LayoutProfile:
        """A1-DA14: text patterns only when the front end cannot parse the SUT; marked estimated."""
        plan = uc.DependencyPlan(estimated=True, notes=[why], missing_headers=list(missing or []))
        for spec in self._text_stubs(layout, None):
            plan.entries.append(uc.Dependency(name=spec.function, declaration=spec.declaration, header=spec.header,
                                              decision="stub", decided_by="rule", reason="text patterns only [A1-DA14]",
                                              return_type=spec.return_type, params=spec.params))
        layout.dependencies = plan
        return layout

    # ---------------------------------------------------------------- brain planning [A1-PL1 .. A1-PL12]
    def _constants_for_values(self, layout: uc.LayoutProfile) -> dict[str, int]:
        """Values the checks accept by name: integer macros, enumerators and the <limits.h> names."""
        constants = dict(LIMITS)
        root = Path(layout.repo_root)
        for rel in list(dict.fromkeys(layout.headers + [layout.sut_file] +
                                      (layout.dependencies.headers if layout.dependencies else []))):
            path = root / rel
            if not path.is_file():
                continue
            text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
            for m in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+(.+?)[ \t]*$", text, re.M):
                value = eval_int(m.group(2), constants)
                if value is not None:
                    constants.setdefault(m.group(1), value)
        for e in layout.enums:
            for k, v in e.values.items():
                constants[k.split("::")[-1]] = v
        return constants

    def _known_names(self, layout: uc.LayoutProfile, stubs: list[uc.StubSpec]) -> set[str]:
        """Identifiers a planned statement may use: SUT functions, everything the headers declare,
        the stubs' control variables and generated callbacks."""
        root = Path(layout.repo_root)
        names = set(PLAN_SAFE_CALLS) | set(LIMITS) | {"NULL", "nullptr", "true", "false"}
        for f in layout.functions:
            names.add(f.name.split("::")[-1])
            names |= set(f.qualified_name.split("::"))
        for rel in list(dict.fromkeys(layout.headers + (layout.dependencies.headers if layout.dependencies else []))):
            path = root / rel
            if path.is_file():
                names |= set(re.findall(r"\b([A-Za-z_]\w*)\b", strip_comments(path.read_text(encoding="utf-8",
                                                                                             errors="replace"))))
        for s in stubs:
            base = f"ut_stub_{uc.stub_base(s)}"
            names |= {s.function, s.qualified_name or s.function, f"{base}_return", f"{base}_calls",
                      f"{base}_returns", f"{base}_returns_len"}
            names |= {f"{base}_{p[1]}" for p in s.params if p[1]}
        for f in layout.functions:
            for i, p in enumerate(f.params):
                if p.kind == "callback":
                    names.add(f"ut_cb_{re.sub(r'[^A-Za-z0-9_]', '_', f.name.replace('(anonymous namespace)::', ''))}_{i}")
        for e in layout.enums:
            names |= {k.split("::")[-1] for k in e.values} | {e.name.split("::")[-1]}
        return names

    @staticmethod
    def _calls_known(text: str, known: set[str]) -> list[str]:
        calls = {m.group(1) for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", text)} - KEYWORDS - {"if", "switch"}
        return sorted(c for c in calls if c not in known and not re.fullmatch(
            r"(?:unsigned|signed|int|long|short|char|float|double|bool|size_t|u?int\d+_t)", c))

    def _value_problem(self, p: uc.ParamInfo, value: str, fn: uc.FunctionInfo, constants: dict[str, int],
                       local: set[str], known: set[str], layout: uc.LayoutProfile) -> str:
        """A1-PL4 e): does the value fit the parameter's type?"""
        v = value.strip()
        if not v:
            return f"{p.name}: no value"
        t = re.sub(r"\b(const|volatile)\b", "", p.type).strip()
        base_ident = re.sub(r"^[&*\s]+", "", v).split("[")[0].split(".")[0].split("->")[0].strip()
        if p.kind in ("int", "enum", "bool"):
            if p.kind == "enum" and v.split("::")[-1] in {x.split("::")[-1] for x in p.enum_values}:
                return ""
            n = eval_int(v, constants)
            if n is None:
                return f"{p.name}: {v!r} is not an integer value (literal, enumerator or macro)"
            if p.kind == "bool" and n not in (0, 1):
                return f"{p.name}: {v!r} is not a boolean"
            if p.kind == "enum":
                values = next((set(e.values.values()) for e in layout.enums
                               if e.name.split("::")[-1] == (p.base or t).split("::")[-1].replace("enum ", "")), set())
                if values and n not in values:
                    return f"{p.name}: {v} is not an enumerator of {p.type}"
            rng = int_range(t)
            if rng and not rng[0] <= n <= rng[1]:
                return f"{p.name}: {v} is outside the range of {p.type} ({rng[0]}..{rng[1]})"
            return ""
        if p.kind == "float":
            if eval_int(v, constants) is not None or re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?[fFlL]?", v):
                return ""
            return f"{p.name}: {v!r} is not a floating-point value"
        if v in NULLS:
            if p.kind in ("ptr_scalar", "ptr_struct", "cstring", "buffer", "callback", "other") and \
                    (p.nullable or p.kind == "other" and "*" not in t):
                return ""
            return f"{p.name}: NULL is not allowed - {fn.name} does not check {p.name} against NULL"
        if p.kind == "cstring" and string_literal(v):
            return ""
        if p.kind == "callback" and (base_ident in known or base_ident in local):
            return ""
        if p.kind == "struct" and v.startswith("{") and v.count("{") == v.count("}"):
            return ""
        if base_ident in local:
            return ""
        if p.kind in ("ptr_scalar", "ptr_struct", "cstring", "buffer", "struct", "callback"):
            return f"{p.name}: {v!r} - pass a variable declared in setup" + (" or NULL" if p.nullable else "")
        unknown = [w for w in re.findall(r"\b([A-Za-z_]\w*)\b", v) if w not in known and w not in local and
                   w not in KEYWORDS and w not in constants]
        return f"{p.name}: unknown names in {v!r}: {', '.join(unknown[:4])}" if unknown else ""

    def _check_planned(self, planned: "PlannedScenario", fn: uc.FunctionInfo, open_ids: set[str],
                       infeasible: set[str], evidence: dict[str, uc.EvidenceRef], stubs: list[uc.StubSpec], constants: dict[str, int],
                       known: set[str], layout: uc.LayoutProfile) -> tuple[uc.Scenario | None, list[str]]:
        """A1-PL4: every check, deterministically, before Agent2 receives the scenario."""
        reasons: list[str] = []
        claim = WAY_OUT.search(" ".join([planned.name, planned.purpose, planned.information_sought, planned.conflict]))
        if claim:                                                                          # COV-5
            return None, [f"claims the gap cannot or need not be covered ('{claim.group(0)}'); ignored [COV-5]"]
        gaps = [g for g in planned.gap_ids if g in open_ids]                               # a)
        if not gaps:
            reasons.append("names no open gap of this function: " + ", ".join(planned.gap_ids[:4]))
        unknown_ev = [e for e in planned.evidence_ids if e not in evidence]                # b)
        if unknown_ev:
            reasons.append("unknown evidence ids: " + ", ".join(unknown_ev[:4]))
        for q in planned.quotes:
            ev = evidence.get(q.evidence_id)
            if ev is None or not norm_text(q.text) or norm_text(q.text) not in norm_text(ev.text):
                reasons.append(f"the quote of {q.evidence_id} does not occur word for word in that evidence")
        local = setup_locals(planned.setup)
        if len(planned.setup) > 15:
            reasons.append("setup has more than 15 statements")
        for stmt in planned.setup:                                                         # grounding
            if PLAN_FORBIDDEN.search(stmt):
                reasons.append(f"setup statement not allowed: {stmt[:60]}")
            elif stmt.count("(") != stmt.count(")") or stmt.count("{") != stmt.count("}"):
                reasons.append(f"setup statement unbalanced: {stmt[:60]}")
            else:
                missing = self._calls_known(stmt, known | local)
                if missing:
                    reasons.append(f"setup calls functions that do not exist here: {', '.join(missing[:4])}")
        by_name = {p.name: p for p in text_params(fn)}
        given = {a.name: a.value for a in planned.args}
        if not fn.member:
            if set(given) != set(by_name):
                reasons.append(f"args must name every parameter exactly once: {', '.join(by_name) or 'none'}")
            for name, value in given.items():                                              # e)
                if name in by_name:
                    problem = self._value_problem(by_name[name], value, fn, constants, local, known, layout)
                    if problem:
                        reasons.append(problem)
        stub_specs = {}
        for s in stubs:
            stub_specs[s.function] = s
            stub_specs[s.qualified_name or s.function] = s
        for sv in planned.stubs:                                                           # d)
            spec = stub_specs.get(sv.stub)
            if spec is None or spec.kind == "variable":
                reasons.append(f"{sv.stub} is not a stubbed function of the dependency plan")
                continue
            ret = re.sub(r"\b(const|volatile|static|inline|extern|virtual)\b", "", spec.return_type).strip()
            if not ret or ret == "void" or spec.kind == "constructor":
                reasons.append(f"{sv.stub} returns nothing that a test can set")
                continue
            if not sv.returns or len(sv.returns) > 16:
                reasons.append(f"{sv.stub}: give 1 to 16 return values")
                continue
            scalar = int_range(ret) is not None or ret.endswith("*") or \
                any(e.name.split("::")[-1] == ret.replace("enum ", "").split("::")[-1] for e in layout.enums) or \
                re.fullmatch(r"(float|double|long double)", ret)
            if len(sv.returns) > 1 and not scalar:
                reasons.append(f"{sv.stub}: a sequence of return values needs a scalar return type, not {ret}")
            pseudo = uc.ParamInfo(name=f"{sv.stub}()", type=ret, kind="float" if re.fullmatch(
                r"(float|double|long double)", ret) else "int" if scalar and not ret.endswith("*") else "other",
                nullable=True)
            for value in sv.returns:
                problem = self._value_problem(pseudo, value, fn, constants, local, known, layout)
                if problem:
                    reasons.append(problem)
        variables = {s.function: s for s in stubs if s.kind == "variable"}
        variables.update({s.qualified_name: s for s in stubs if s.kind == "variable" and s.qualified_name})
        for var in planned.variables:
            spec = variables.get(var.name)
            if spec is None:
                reasons.append(f"{var.name} is not a stubbed variable")
            elif spec.is_const:
                reasons.append(f"{var.name} is const: a test cannot set it [A2-21j]")
        proven = [g for g in planned.gap_ids if g in infeasible]                           # f)
        if proven:
            reasons.append("targets gaps proven infeasible [A1-37]: " + ", ".join(proven[:3]))
        if not re.fullmatch(r"test_[A-Za-z0-9]+(?:_[A-Za-z0-9]+){1,}", planned.name or ""):
            planned.name = ""                                     # Agent2 names it [A2-21q]
        if reasons:
            return None, reasons
        cited = [evidence[e] for e in planned.evidence_ids if e in evidence]
        strong = [e for e in cited if e.evidence_class != "E8"]
        normative = planned.classification == "confirmed_gap" and bool(strong) and \
            bool(planned.expected_return.strip() or planned.expected_after) and bool(planned.quotes)   # c)
        notes = []
        if planned.classification == "confirmed_gap" and not normative:
            notes.append("downgraded to characterization: no quoted E1-E7 evidence or no expected value [A1-PL4 c]")
        if normative:
            full = return_type_of(fn)
            ret = re.sub(r"\b(const|volatile|static|inline)\b", "", full).strip()
            if planned.expected_return.strip():
                if not ret or ret == "void":
                    return None, [f"{fn.name} returns nothing; expected_return must be empty"]
                kind = "float" if re.fullmatch(r"(float|double|long double)", ret) else \
                    "cstring" if re.fullmatch(r"(const\s+)?char\s*\*", full.strip()) else \
                    "other" if ret.endswith("*") else "int"
                problem = self._value_problem(uc.ParamInfo(name="return value", type=ret, kind=kind, nullable=True),
                                              planned.expected_return, fn, constants, local, known, layout)
                if problem:
                    return None, [problem]
            for check in planned.expected_after:
                names = set(re.findall(r"\b([A-Za-z_]\w*)\b", check.expr)) - KEYWORDS
                if not names or any(n not in local and n not in known for n in names):
                    return None, [f"expected_after: {check.expr!r} is not a variable of the setup or a stub control"]
                if eval_int(check.value, constants) is None and not re.fullmatch(
                        r"[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?[fF]?", check.value.strip()) and not \
                        string_literal(check.value):
                    return None, [f"expected_after: {check.value!r} is not a value"]
        linked = bool(strong) and all(e.linked_by for e in strong)       # rests only on brain links
        scenario = uc.Scenario(
            scenario_id="", function=fn.name, gap_ids=gaps, description=planned.purpose[:300] or "planned by the brain",
            purpose=planned.purpose[:200], test_name=planned.name, args=[uc.NamedValue(name=a.name, value=a.value)
                                                                         for a in planned.args],
            setup=list(planned.setup), stub_values=[uc.StubValues(stub=s.stub, returns=list(s.returns))
                                                    for s in planned.stubs],
            variables=[uc.NamedValue(name=v.name, value=v.value) for v in planned.variables],
            expected_return=planned.expected_return.strip() if normative else "",
            expected_after=[uc.ExpectedCheck(expr=c.expr, value=c.value) for c in planned.expected_after] if normative
            else [], quotes=[uc.Quote(evidence_id=q.evidence_id, text=q.text) for q in planned.quotes],
            evidence=cited if normative else [], strongest_class=min((e.evidence_class for e in strong), default="E8")
            if normative else "E8", characterization=not normative, checked=True,
            classification="confirmed_gap" if normative else (
                planned.classification if planned.classification != "confirmed_gap" else "characterization_gap"),
            expected="as stated by the evidence" if normative else "observe it", linked=linked and normative)
        return scenario, notes

    def _facts(self, fn: uc.FunctionInfo, gaps: list[uc.GapItem], open_ids: set[str], layout: uc.LayoutProfile,
               req: uc.PlanRequest, sut_text: str, stubs: list[uc.StubSpec], evidence: list[uc.EvidenceRef]) -> list[ba.ContextPart]:
        """A1-PL2 (facts first) and A1-PL12 (what was tried)."""
        lines = sut_text.splitlines()
        root = Path(layout.repo_root)
        source = "\n".join(f"{n:4d}| {lines[n - 1]}" for n in range(fn.line_start, min(fn.line_end, len(lines)) + 1))
        params = []
        for p in text_params(fn):
            extra = []
            if p.enum_values:
                extra.append("enumerators " + ", ".join(p.enum_values[:20]))
            if p.fields:
                extra.append("fields " + ", ".join(f"{f.name}: {f.type}" for f in p.fields[:12]))
            if p.nullable:
                extra.append("may be NULL")
            params.append(f"- {p.name}: {p.type} ({p.kind}{'; ' + '; '.join(extra) if extra else ''})")
        gap_rows = []
        for g in gaps:
            code = lines[g.line - 1].strip() if 0 < g.line <= len(lines) else ""
            path = enclosing_conditions(sut_text, fn, g.line) if g.line else []
            tried = req.attempts.get(g.gap_id, 0)
            gap_rows.append(f"- {g.gap_id} [{g.metric}] line {g.line}: `{code}` {g.detail[:160]}"
                            + (f" | needs: {'; '.join(g.required[:4])}" if g.required else "")
                            + (f" | PATH CONDITIONS: {' && '.join(path[:6])}" if path else "")
                            + (f" | TRIED {tried} time(s) without reaching it" if tried else ""))
        if any(g.startswith("EVIDENCE:") for g in open_ids):
            gap_rows.append(f"- EVIDENCE:{fn.name}: check the behaviour the EVIDENCE states (results, error values, "
                            "limits) with asserted expected values")
        called = [s for s in stubs if s.kind != "variable" and (s.function in fn.calls or
                                                                  (s.qualified_name or s.function) in fn.calls)]
        stub_rows = [f"- {s.function}: {s.declaration or s.return_type + ' ' + s.function + '(...)'}" for s in called]
        var_rows = [f"- {s.function}: {s.var_type}" for s in stubs if s.kind == "variable" and not s.is_const]
        real = [d.name for d in (layout.dependencies.entries if layout.dependencies else []) if d.decision != "stub"]
        ev_rows = [f"- {e.evidence_id} {e.evidence_class}{' (linked by the brain)' if e.linked_by else ''} "
                   f"{e.source}:{e.line}: {e.text[:400]}" for e in evidence]
        # A1-PL7/A1-PL12: the earlier tests of this function and what each reached
        tried_rows = []
        suite = uc.test_sources(req.existing_suite) if req.existing_suite else {}
        status = {t.name: t.status for t in (req.last.tests if req.last else [])}
        short = fn.name.split("::")[-1]
        for name, src in suite.items():
            if not re.search(r"\b" + re.escape(short) + r"\s*\(", src):
                continue
            reached = items_in(req.test_items.get(name, []), fn.line_start, fn.line_end)
            body = "\n".join(src.splitlines()[2:12])
            tried_rows.append(f"- {name} ({status.get(name, 'not run')}; reached: {', '.join(reached[:12]) or 'unknown'})"
                              f"\n{body}")
            if len(tried_rows) >= 6:
                break
        style = []
        for case in layout.existing_tests[:2]:
            path = root / case.file
            if path.is_file():
                rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
                style.append("\n".join(rows[max(case.line - 1, 0): case.line + 12]))
        facts = [
            f"LANGUAGE: {'C++' if layout.language == 'cpp' else 'C'}",
            f"FUNCTION {fn.qualified_name} ({layout.sut_file}){' - static, called through a wrapper' if fn.is_static else ''}"
            f"{' - class member' if fn.member else ''}:\n{source}",
            "PARAMETERS:\n" + ("\n".join(params) or "- none"),
            f"RETURNS: {return_type_of(fn) or 'void'}",
            "OPEN GAPS:\n" + "\n".join(gap_rows),
            "STUBS (controllable return values):\n" + ("\n".join(stub_rows) or "- none"),
            "STUB VARIABLES:\n" + ("\n".join(var_rows) or "- none"),
            "REAL OR SYSTEM DEPENDENCIES (not controllable): " + (", ".join(real[:20]) or "none"),
        ]
        # ARCH-17: facts stay whole; evidence, earlier tests and the style give way in that order
        # (reverse) when the call does not fit the brain's window.
        return [uc.part("facts", "\n\n".join(facts)),
                uc.part("evidence", "EVIDENCE:\n" + ("\n".join(ev_rows) or
                                                     "- none: every scenario is characterization_gap"), uc.P_EVIDENCE),
                uc.part("tried", "TRIED (earlier tests of this function and the items each reached):\n"
                        + ("\n".join(tried_rows) or "- none"), uc.P_TRIED),
                uc.part("style", "EXISTING TEST STYLE:\n" + ("\n---\n".join(style) or f"- {layout.framework}"),
                        uc.P_EXAMPLES)]

    def _lookup(self, fn: uc.FunctionInfo, layout: uc.LayoutProfile, sought: list[str],
                evidence: list[uc.EvidenceRef], lookups: list[str], budget: int) -> list[uc.EvidenceRef]:
        """A1-PL6, A1-25p..25w: bounded look-ups in approved sources only, every step recorded."""
        if not sought or budget <= 0:
            return []
        root = Path(layout.repo_root)
        short = fn.name.split("::")[-1]
        self.knowledge.clear()
        kinds = {}
        for rel in [layout.sut_file] + layout.headers:
            if (root / rel).is_file():
                self.knowledge.add_text(rel, (root / rel).read_text(encoding="utf-8", errors="replace"),
                                        kind="E2" if rel != layout.sut_file else "E3", lines_per_chunk=8)
        for rid, text, source, line in read_requirements(root, layout.requirement_files):
            self.knowledge.chunks.append({"source": source, "kind": "E1", "line": line, "text": f"{rid}: {text}",
                                          "tokens": set(ba.tokens(f"{rid} {text}"))})
        for rel in layout.test_files[:20]:
            if (root / rel).is_file():
                self.knowledge.add_text(rel, (root / rel).read_text(encoding="utf-8", errors="replace"),
                                        kind="E5", lines_per_chunk=8)
        callers = 0
        for path in root.rglob("*"):
            if callers >= self.cfg.max_callers or not path.is_file() or path.suffix.lower() not in C_EXT + CPP_EXT:
                continue
            rel = str(path.relative_to(root)).replace("\\", "/")
            if rel == layout.sut_file or rel in layout.test_files or "ut_output" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"\b" + re.escape(short) + r"\s*\(", text):
                self.knowledge.add_text(rel, text, kind="E6", lines_per_chunk=8)
                callers += 1
        known = {(e.source, e.line) for e in evidence}
        found = []
        for query in sought[:budget]:
            hits = self.knowledge.search(f"{short} {query}", k=2)
            got = []
            for hit in hits:
                if (hit["source"], hit["line"]) in known:
                    continue
                known.add((hit["source"], hit["line"]))
                ref = uc.EvidenceRef(evidence_id=f"EV-{short}-L{len(found) + 1}", evidence_class=hit["kind"],
                                     source=hit["source"], line=hit["line"], text=hit["text"][:600], linked_by="lookup")
                found.append(ref)
                got.append(ref)
            lookups.append(f"{fn.name}: sought {query[:80]!r}; inspected SUT, headers, requirements, tests, callers; "
                           + (f"obtained {', '.join(f'{r.evidence_id} {r.source}:{r.line}' for r in got)}"
                              if got else "nothing new") + " [A1-25s]")
        return found

    async def _brain_scenarios(self, fn: uc.FunctionInfo, gaps: list[uc.GapItem], open_ids: set[str],
                               infeasible: set[str], layout: uc.LayoutProfile, req: uc.PlanRequest, sut_text: str,
                               stubs: list[uc.StubSpec], evidence: list[uc.EvidenceRef], plan_notes: dict[str, list[str]]
                               ) -> list[uc.Scenario]:
        """A1-PL1..A1-PL9: the brain suggests, Agent1 checks; at most the configured number of calls."""
        constants = self._constants_for_values(layout)
        known = self._known_names(layout, stubs)
        evidence = list(evidence)
        accepted: list[uc.Scenario] = []
        extra, calls, brain = "", 0, ""
        dropped: list[str] = []
        if not open_ids:
            return []                                                        # nothing to plan [ARCH-16]
        while calls < self.cfg.max_brain_calls_per_function:
            calls += 1
            context = self._facts(fn, gaps, open_ids, layout, req, sut_text, stubs, evidence)
            if extra:
                context.insert(1, uc.part("re-ask", extra.strip()))
            covered = {g for s in accepted for g in s.gap_ids}
            need = ba.OutputNeed(items=min(self.cfg.max_brain_scenarios, max(len(open_ids - covered), 1)),
                                 tokens_per_item=self.cfg.tokens_per_scenario, overhead=100)
            # as many scenarios as the answer limit was planned for, not the configured maximum [ARCH-11a]
            task = PLAN_TASK.format(fn=fn.qualified_name, limit=need.items)
            try:
                avoid = set(req.avoid_brains)                                      # COV-6: the next brain
                allowed = [b for b in self.allowed_brains if b not in avoid] if avoid else None
                answer = await self.generate(task, context, PlannedScenarios, need=need, step="A1.plan",
                                             project=layout.repo_root, allowed=allowed)
            except ba.AgentFault as exc:
                plan_notes["planning"].append(f"{fn.name}: rules - brain not available ({exc.code}) [A1-PL8]"
                                              if not accepted else f"{fn.name}: re-ask failed ({exc.code})")
                break
            used = self.brain_of_call()                             # this call's brain, not another's [ARCH-18]
            brain = f"brain:{used.get('brain', '')}/{used.get('model_id', '')}"
            ev_map = {e.evidence_id: e for e in evidence}
            problems, sought = [], []
            for k, planned in enumerate(answer.scenarios[: self.cfg.max_brain_scenarios], 1):
                scenario, reasons = self._check_planned(planned, fn, open_ids, infeasible, ev_map, stubs, constants,
                                                        known, layout)
                if scenario is None:
                    problems.append(f"- scenario {k} ({planned.name or planned.purpose[:40]}): " + "; ".join(reasons))
                    continue
                scenario.made_by = brain
                scenario.required_conditions = [c for g in gaps if g.gap_id in scenario.gap_ids
                                                for c in enclosing_conditions(sut_text, fn, g.line)][:8]
                same = scenario.model_dump(include={"gap_ids", "args", "setup", "stub_values", "variables",
                                                    "expected_return", "expected_after"})
                if any(s.model_dump(include=set(same)) == same for s in accepted):
                    continue                                                  # the re-ask repeated it
                accepted.append(scenario)
                if reasons:
                    plan_notes["planning"].append(f"{fn.name}: {'; '.join(reasons)}")
                if planned.classification == "conflicting_evidence":            # A1-PL5
                    ids = [e for e in re.findall(r"EV-[\w:-]+", planned.conflict) if e in ev_map]
                    if ids:
                        plan_notes["conflicts"].append(
                            f"{fn.name}: " + " vs ".join(f"{e} ({ev_map[e].evidence_class} {ev_map[e].source}:"
                                                         f"{ev_map[e].line})" for e in ids) + f": {planned.conflict[:200]}")
                if planned.classification == "insufficient_evidence" and planned.information_sought.strip():
                    sought.append(planned.information_sought.strip())
            dropped += problems
            budget = self.cfg.max_lookups_per_function - sum(1 for line in plan_notes["lookups"]
                                                             if line.startswith(fn.name + ":"))
            found = self._lookup(fn, layout, sought, evidence, plan_notes["lookups"], budget)
            evidence += found
            covered = {g for s in accepted for g in s.gap_ids}
            remaining = sorted(open_ids - covered)
            if not (problems or found) or not remaining or calls >= self.cfg.max_brain_calls_per_function:
                break
            extra = ("\n\nYOUR PREVIOUS ANSWER WAS CHECKED. Scenarios already accepted cover: "
                     + (", ".join(sorted(covered)) or "nothing") + ". Plan only these gaps now: " + ", ".join(remaining)
                     + ("\nProblems found (fix them):\n" + "\n".join(problems) if problems else "")
                     + ("\nNew evidence from the look-up:\n" + "\n".join(
                         f"- {e.evidence_id} {e.evidence_class} (linked by look-up) {e.source}:{e.line}: {e.text[:300]}"
                         for e in found) if found else ""))
        for index, scenario in enumerate(accepted, 1):
            scenario.scenario_id = f"S-brain-{fn.name.replace('::', '_')}-{req.iteration}-{index}"
        if brain and not accepted and not dropped:
            plan_notes["planning"].append(f"{fn.name}: rules - {brain.split(':', 1)[1]} suggested no scenario [A1-PL8]")
        elif brain and not accepted:
            plan_notes["planning"].append(
                f"{fn.name}: rules - no scenario of {brain.split(':', 1)[1]} passed the check"
                + (f" ({' | '.join(d[2:120] for d in dropped[:2])})" if dropped else "") + " [A1-PL8]")
        elif brain:
            plan_notes["planning"].append(
                f"{fn.name}: {len(accepted)} scenario(s) by {brain.split(':', 1)[1]} "
                f"({sum(not s.characterization for s in accepted)} asserting stated behaviour), {calls} call(s)"
                + (f"; dropped by the check: {' | '.join(d[2:120] for d in dropped[:3])}" if dropped else "")
                + " [A1-PL4]")
        return accepted

    async def _link_requirements(self, layout: uc.LayoutProfile) -> list[uc.RequirementLink]:
        """A1-PL13: requirements written in prose, linked by the brain and checked here."""
        if not self.cfg.link_requirements or not layout.requirement_files:
            return []
        root = Path(layout.repo_root)
        reqs = read_requirements(root, layout.requirement_files)
        shorts = {f.name.split("::")[-1]: f for f in layout.functions}
        tags = set()
        for rel in [layout.sut_file] + layout.headers:
            if (root / rel).is_file():
                tags |= set(re.findall(r"\b([A-Z]{2,}(?:-[A-Z0-9]+)*-\d+)\b",
                                       (root / rel).read_text(encoding="utf-8", errors="replace")))
        unlinked = [r for r in reqs if r[0] not in tags and not any(
            re.search(r"\b" + re.escape(n) + r"\b", r[1]) for n in shorts)][: self.cfg.max_requirements_to_link]
        if not unlinked or not shorts:
            return []
        key = ba.contract_digest([[list(r) for r in unlinked], sorted(shorts)])
        if key in self._link_cache:
            return self._link_cache[key]
        functions = []
        lines = (root / layout.sut_file).read_text(encoding="utf-8", errors="replace").splitlines()
        for f in layout.functions:
            comment = []
            n = f.line_start - 2
            while n >= 0 and len(comment) < 4 and lines[n].strip().startswith(("/*", "*", "//")):
                comment.insert(0, lines[n].strip())
                n -= 1
            functions.append(f"- {f.signature.strip().rstrip('{').strip()}  {' '.join(comment)[:200]}")
        context = [uc.part("functions", "FUNCTIONS:\n" + "\n".join(functions)),
                   uc.part("requirements", "REQUIREMENTS:\n" + "\n".join(
                       f"- {rid}: {text[:400]}" for rid, text, _, _ in unlinked))]
        try:
            answer = await self.generate(LINK_TASK, context, LinkAnswers, step="A1.links", project=layout.repo_root,
                                         need=ba.OutputNeed(items=len(unlinked), tokens_per_item=120, overhead=50))
        except ba.AgentFault as exc:
            LOG.info("requirement links by the brain unavailable (%s)", exc.code)
            return []
        by_id = {rid: (text, source, line) for rid, text, source, line in unlinked}
        links = []
        for a in answer.links:
            item = by_id.get(a.requirement_id)
            if item is None or not norm_text(a.quote) or norm_text(a.quote) not in norm_text(item[0]):
                continue
            if not 0.0 <= a.probability <= 1.0 or a.probability < self.cfg.min_link_probability:
                continue
            for name in a.functions:
                fn = shorts.get(name.split("::")[-1])
                if fn is not None:
                    links.append(uc.RequirementLink(requirement_id=a.requirement_id, function=fn.name, source=item[1],
                                                    line=item[2], quote=a.quote[:400], probability=round(a.probability, 2)))
        self._link_cache[key] = links
        return links

    async def _missing_declarations(self, layout: uc.LayoutProfile, symbols: list[str]) -> tuple[
            list[uc.Dependency], list[str]]:
        """A1-PL14: a declaration from the dependency plan or the headers; the brain only when none exists."""
        root = Path(layout.repo_root)
        known = {d.name: d for d in (layout.dependencies.entries if layout.dependencies else [])}
        header_decls: dict[str, tuple[str, str]] = {}
        for rel in self._all_headers(root):
            for name, decl in find_declarations((root / rel).read_text(encoding="utf-8", errors="replace")).items():
                header_decls.setdefault(name, (decl, rel))
        sut_lines = (root / layout.sut_file).read_text(encoding="utf-8", errors="replace").splitlines()
        out, open_items = [], []
        for sym in symbols:
            dep = known.get(sym)
            if dep is not None and (dep.declaration or dep.return_type or dep.kind == "variable"):
                out.append(dep.model_copy(update={"decision": "stub", "decided_by": "rule", "reason":
                           f"declared in {dep.header or 'a header outside the repository'} but not linked: stubbed "
                           "[A1-PL14]"}))
                continue
            if sym in header_decls:
                decl, rel = header_decls[sym]
                rt, params = parse_declaration(decl)
                out.append(uc.Dependency(name=sym, declaration=decl, header=rel, decision="stub", decided_by="rule",
                                         reason="declared in a header, not linked: stubbed [A1-PL14]",
                                         return_type=rt, params=params))
                continue
            usage = [f"{n}: {line.strip()}" for n, line in enumerate(sut_lines, 1)
                     if re.search(r"\b" + re.escape(sym) + r"\b", line)][:12]
            if not self.cfg.propose_declarations or not usage:
                open_items.append(f"no declaration found for {sym}; cannot stub it")
                continue
            try:
                answer = await self.generate(DECLARATION_TASK.format(name=sym),
                                             [uc.part("uses", "USES:\n" + "\n".join(usage))], DeclarationAnswer,
                                             step="A1.declaration", project=layout.repo_root,
                                             need=ba.OutputNeed(items=1, tokens_per_item=80, overhead=50))
                decl = re.sub(r"\s+", " ", answer.declaration.strip()).rstrip(";").strip()
                if re.match(r"^(static|inline|#|typedef)", decl) or "{" in decl:
                    raise ValueError("not a plain declaration")
                if "(" in decl:
                    if function_name(decl) != sym:
                        raise ValueError("not a declaration of " + sym)
                    rt, params = parse_declaration(decl + ";")
                    fields = {"return_type": rt, "params": params}
                else:
                    m = re.fullmatch(r"(?:extern\s+)?(.+?)\s*\b" + re.escape(sym) + r"\s*((?:\[[^\]]*\])*)", decl)
                    if not m or not m.group(1).strip():
                        raise ValueError("not a declaration of " + sym)
                    fields = {"kind": "variable", "type": m.group(1).strip() + (m.group(2) or ""),
                              "is_const": bool(re.search(r"\bconst\b", m.group(1)))}
            except (ba.AgentFault, ValueError, AttributeError) as exc:
                open_items.append(f"no declaration found for {sym}; the brain's proposal was not usable "
                                  f"({getattr(exc, 'code', '') or str(exc)[:80]})")
                continue
            who = f"brain declaration ({self.last_brain.get('brain', '')})"
            out.append(uc.Dependency(name=sym, declaration=decl + ";", decision="stub", decided_by=who,
                                     reason="no declaration in the repository: proposed by the brain from its uses "
                                            "and confirmed only by Agent3's build [A1-PL14]", **fields))
        return out, open_items

    # ---------------------------------------------------------------- planning
    async def plan(self, req: uc.PlanRequest) -> uc.UTPlan:
        layout, last = req.layout, req.last
        root = Path(layout.repo_root)
        plan_id = f"plan-{req.iteration}-{uuid.uuid4().hex[:6]}"
        suite_exists = (root / layout.suite_file).is_file() or bool(req.existing_suite)
        base = dict(plan_id=plan_id, layout=layout, create_suite=not suite_exists,
                    files_to_create=[] if suite_exists else [layout.suite_file],
                    files_to_edit=[layout.suite_file] if suite_exists else [])
        if layout.blocked:
            return uc.UTPlan(**base, stage="blocked", reason=layout.blocked_reason or "layout blocked")
        stubs = self._stubs(layout, last)
        disputed = set(req.disputed_evidence)
        notes: dict[str, list[str]] = {"planning": [], "conflicts": [], "lookups": []}
        # The brain plans once the deterministic tests have been measured [A1-PL1].
        brain_ok = self.cfg.brain_planning and last is not None and last.stage == "verify" and last.coverage is not None
        if last is not None and last.decision == "complete":
            pending = self._evidence_scenarios(layout, req, disputed)
            if pending and brain_ok:                                       # A1-25y with the brain [A1-PL1]
                pending = await self._with_brain(pending, {}, set(), layout, req, stubs, disputed, notes)
            if pending:                                    # A1-25y: behaviour, not only coverage
                return uc.UTPlan(**base, stage="coverage", stubs=stubs, scenarios=pending,
                                 keep_tests=[t.name for t in last.tests if t.status == "passed"],
                                 reason="coverage complete; the documented behaviour is still to be checked [A1-25y]",
                                 **notes)
            if not self._open_gaps(last):
                return uc.UTPlan(**base, stage="done", stubs=stubs, reason="all required metrics complete",
                                 keep_tests=[t.name for t in last.tests if t.status == "passed"])
            # COV-6: a target below 100 % is met, but items remain: keep working on them.
        # Stage A: build/setup failures first [A1-22]
        if last is not None and (not last.link_ok or last.decision == "setup_blocked"):
            repairs, open_items = [], []
            stubbed = {s.function for s in stubs}
            for f in last.failures:
                if f.kind in ("compile", "link") and f.symbol in stubbed and f.classification == "stub_defect":
                    continue                       # a planned stub resolves it [A1-22b]
                owner = "agent4" if f.route == "agent4" else "agent2"
                if f.route == "stop":
                    open_items.append(f"environment: {f.diagnostic}")
                    continue
                repairs.append(uc.RepairItem(item_id=f.failure_id, owner=owner, target_file=f.file or layout.suite_file,
                                             issue=f.classification, diagnostic=f.diagnostic, symbol=f.symbol,
                                             line=f.line, classification=f.classification,
                                             attempts=req.attempts.get(f.failure_id, 0)))
            missing_decls = [s for s in last.undefined_symbols if s not in stubbed and
                             s not in {f.name for f in layout.functions} and
                             s not in {f.qualified_name for f in layout.functions}]
            proposed, decl_items = await self._missing_declarations(layout, missing_decls) if missing_decls else ([], [])
            for dep in proposed:                                                # A1-PL14
                spec = stub_spec(dep)
                if spec is not None and spec.function not in stubbed:
                    stubs.append(spec)
                    stubbed.add(spec.function)
                    notes["planning"].append(f"stub for {dep.name}: {dep.decided_by} - {dep.declaration}")
            open_items += decl_items
            if layout.dependencies:
                open_items += [f"missing header {h} [A1-DA13]" for h in layout.dependencies.missing_headers]
            return uc.UTPlan(**base, stage="setup", stubs=stubs, repairs=repairs, open_items=open_items,
                             proposed_dependencies=proposed, planning=notes["planning"],
                             reason="setup or build issues must be fixed before coverage planning [A1-22a]")
        # Stage B: coverage plan
        sut_text = (root / layout.sut_file).read_text(encoding="utf-8", errors="replace")
        gaps = self._gaps(layout, last, sut_text)
        repairs, drop = [], []
        scenarios, open_items = [], []
        new_disputes: set[str] = set()
        for f in (last.failures if last else []):
            if f.route != "agent1":
                continue
            if f.kind in ("test", "crash", "timeout", "regression"):
                drop.append(f.affected_test) if f.affected_test else None
            evidence = self.collect_evidence(layout, f.affected_function).evidence if f.affected_function else []
            action, tries = "rewrite", req.attempts.get(f.failure_id, 0)
            if f.classification == "requirement_mismatch":                  # A1-PL13, A3-CL11
                keys = self._linked_keys(f, req, evidence)
                new_disputes |= keys
                action = "drop"
                linked = sorted({e.text.split(":", 1)[0] for e in evidence if uc.evidence_key(e) in keys})
                open_items.append(f"{f.affected_test}: the implementation of {f.affected_function or 'the function'} "
                                  f"disagrees with {', '.join(linked) or 'evidence'} linked by the brain - review the "
                                  "link or the code [A1-PL13, A3-CL11]")
            elif f.affected_test and tries >= self.cfg.max_attempts_per_gap:
                action = "drop"
                open_items.append(f"{f.affected_test}: still failing after {tries} rewrites; dropped [A2-21p, A1-36]")
            repairs.append(uc.RepairItem(item_id=f.failure_id, owner="agent2", target_file=layout.suite_file,
                                         issue=f.classification, diagnostic=f.diagnostic, symbol=f.symbol, line=f.line,
                                         affected_test=f.affected_test, classification=f.classification,
                                         attempts=tries, action=action))
            if f.classification == "undetermined" and not evidence:
                repairs[-1].diagnostic += " | no further expected-behaviour evidence found [A1-25j]"
        disputed |= new_disputes
        by_fuzz: set[str] = set()
        for k, inp in enumerate(req.fuzz.inputs if req.fuzz else []):           # A1-18c
            fn = next((f for f in layout.functions if inp.function in (f.name, f.qualified_name)), None)
            if fn is None:
                continue
            by_fuzz |= set(inp.covers)
            scenarios.append(uc.Scenario(
                scenario_id=f"S-fuzz-{fn.name.replace('::', '_')}-{k + 1}", function=fn.name, gap_ids=inp.covers,
                description=f"Inputs found by coverage-guided search reach {', '.join(inp.covers[:4])}",
                inputs=[inp], expected="observe it", characterization=True))
        real = {d.name: d for d in (layout.dependencies.entries if layout.dependencies else []) if d.decision == "real"}
        lines = sut_text.splitlines()
        active_by_fn: dict[str, list[uc.GapItem]] = {}
        for fn in layout.functions:
            fn_gaps = [g for g in gaps if g.function in (fn.name, fn.qualified_name) and g.gap_id not in by_fuzz
                       and "infeasible" not in g.detail and "unreachable_code" not in g.detail]
            if not fn_gaps:
                continue
            evidence = [e for e in self.collect_evidence(layout, fn.name).evidence if uc.evidence_key(e) not in disputed]
            strongest = min((r.evidence_class for r in evidence), default="E8")
            active, given_up = [], []
            for g in fn_gaps:
                g.attempts = req.attempts.get(g.gap_id, 0)
                (given_up if g.attempts >= 2 * self.cfg.max_attempts_per_gap else active).append(g)
            for g in given_up:
                open_items.append(f"{g.gap_id}: still uncovered after {g.attempts} attempts [A1-36]")
            for g in fn_gaps:
                text = lines[g.line - 1] if 0 < g.line <= len(lines) else ""
                for name in [n for n in real if re.search(r"\b" + re.escape(n) + r"\s*\(", text)]:
                    if g.attempts >= self.cfg.max_attempts_per_gap:
                        open_items.append(f"{g.gap_id}: needs control of the real dependency {name} [A1-DA12]")
            active_by_fn[fn.name] = active
            for group in self._group(active)[: self.cfg.max_scenarios_per_function]:
                lead = group[0]
                reach = enclosing_conditions(sut_text, fn, lead.line)
                hints = [f"set ut_stub_{uc.stub_base(s)}_return to control {s.qualified_name or s.function}()"
                         for s in stubs if s.function in fn.calls and s.kind in ("function", "method")
                         and s.return_type not in ("", "void")]
                hints += [f"set {s.qualified_name or s.function} before the call" for s in stubs
                          if s.kind == "variable" and not s.is_const]
                retry = lead.attempts >= self.cfg.max_attempts_per_gap
                required = list(dict.fromkeys(r for g in group for r in g.required))
                description = self._describe(fn, group, sut_text)
                if retry:
                    description += " Earlier attempts did not reach it: change the stub return values or inputs."
                scenarios.append(uc.Scenario(
                    scenario_id=f"S-{fn.name.replace('::', '_')}-{lead.line}-{lead.metric}", function=fn.name,
                    gap_ids=[g.gap_id for g in group], description=description,
                    required_conditions=reach + required, stub_hints=hints,
                    expected="derive from the evidence when it states the result, otherwise observe it",
                    evidence=evidence, strongest_class=strongest, characterization=strongest == "E8"))
        planned = {sc.function for sc in scenarios}
        if last is not None and last.coverage is not None:
            scenarios += [sc for sc in self._evidence_scenarios(layout, req, disputed) if sc.function not in planned]
        if brain_ok:                                                              # A1-PL1
            infeasible = {g.gap_id for g in gaps if "infeasible" in g.detail or "unreachable_code" in g.detail}
            scenarios = await self._with_brain(scenarios, active_by_fn, infeasible, layout, req, stubs, disputed, notes)
        keep = [t.name for t in (last.tests if last else []) if t.status == "passed" and t.name not in drop]
        return uc.UTPlan(**base, stage="coverage", stubs=stubs, scenarios=scenarios, repairs=repairs, gaps=gaps,
                         keep_tests=keep, drop_tests=sorted(set(drop)), open_items=open_items,
                         reason=last.reason if last else "no measurement yet: estimated obligations [A1-18a]",
                         disputed_evidence=sorted(new_disputes), **notes)

    @staticmethod
    def _open_gaps(last: uc.VerificationResult) -> bool:
        return last.coverage is not None and any(fc.gaps for fc in last.coverage.functions)

    async def _with_brain(self, scenarios: list[uc.Scenario], active_by_fn: dict[str, list[uc.GapItem]],
                          infeasible: set[str], layout: uc.LayoutProfile, req: uc.PlanRequest, stubs: list[uc.StubSpec],
                          disputed: set[str], notes: dict[str, list[str]]) -> list[uc.Scenario]:
        """Per planned function: the brain's checked scenarios replace the rule scenarios; the
        coverage-guided search inputs stay; without a usable answer the rules stay [A1-PL1, A1-PL8]."""
        sut_text = (Path(layout.repo_root) / layout.sut_file).read_text(encoding="utf-8", errors="replace")
        out = list(scenarios)
        work = []
        for name in list(dict.fromkeys(s.function for s in scenarios if not s.inputs)):
            fn = next((f for f in layout.functions if f.name == name), None)
            if fn is None:
                continue
            gaps = active_by_fn.get(name, [])
            open_ids = {g.gap_id for g in gaps}
            if any(s.function == name and s.scenario_id.startswith("S-evidence-") for s in scenarios):
                open_ids.add(f"EVIDENCE:{name}")
            if not open_ids:
                continue
            evidence = [e for e in self.collect_evidence(layout, name).evidence if uc.evidence_key(e) not in disputed]
            work.append((name, fn, gaps, open_ids, evidence))

        # ARCH-18: the functions are planned at the same time, each with notes of its own; the
        # results are combined below in the order of the functions, never in the order they arrive.
        def job(fn: uc.FunctionInfo, gaps: list[uc.GapItem], open_ids: set[str], evidence: list[uc.EvidenceRef]):
            async def run() -> tuple[list[uc.Scenario], dict[str, list[str]], dict[str, int]]:
                own = {key: [line for line in lines if key == "lookups" and line.startswith(fn.name + ":")]
                       for key, lines in notes.items()}
                before = {key: len(lines) for key, lines in own.items()}
                found = await self._brain_scenarios(fn, gaps, open_ids, infeasible, layout, req, sut_text, stubs,
                                                    evidence, own)
                return found, own, before
            return run
        results = await self.in_parallel([job(fn, gaps, open_ids, evidence) for _, fn, gaps, open_ids, evidence in work])
        for (name, *_), result in zip(work, results):
            if isinstance(result, BaseException):
                raise result
            brain, own, before = result
            for key, lines in own.items():
                notes.setdefault(key, []).extend(lines[before[key]:])
            if brain:
                out = [s for s in out if s.function != name or s.inputs] + brain
            elif not any(line.startswith(f"{name}:") for line in notes["planning"]):
                notes["planning"].append(f"{name}: rules - no scenario of the brain passed the check [A1-PL8]")
        return out

    @staticmethod
    def _linked_keys(f: uc.Failure, req: uc.PlanRequest, evidence: list[uc.EvidenceRef]) -> set[str]:
        """The brain-linked evidence a failing test rests on: named in its comment, else all of its function's."""
        source = uc.test_sources(req.existing_suite).get(f.affected_test, "") if req.existing_suite else ""
        keys = set(re.findall(r"@([^\s,\]]+:\d+)", source))
        linked = {uc.evidence_key(e) for e in evidence if e.linked_by}
        named = keys & linked if keys else set()
        return named or linked or keys

    def _evidence_scenarios(self, layout: uc.LayoutProfile, req: uc.PlanRequest,
                            disputed: set[str] | None = None) -> list[uc.Scenario]:
        """A1-25y: a function whose expected behaviour is stated (E1 requirement, E2 interface contract,
        E3 executable contract) gets tests that check that behaviour, even when characterization tests
        already reach full coverage; otherwise a defect against the documented behaviour stays hidden.
        Planned until such a test exists or the attempts are used up."""
        out = []
        suite = uc.test_sources(req.existing_suite)
        for fn in layout.functions:
            short = fn.name.split("::")[-1]
            key = f"EVIDENCE:{fn.name}"
            if req.attempts.get(key, 0) >= self.cfg.max_attempts_per_gap:
                continue
            checked = any(not src.startswith("/* evidence: E8") and re.search(r"\b" + re.escape(short) + r"\s*\(", src)
                          for src in suite.values())
            if checked:
                continue
            all_evidence = [e for e in self.collect_evidence(layout, fn.name).evidence
                            if uc.evidence_key(e) not in (disputed or set())]
            evidence = [e for e in all_evidence if e.evidence_class in ("E1", "E2", "E3")]
            if not evidence:
                continue
            out.append(uc.Scenario(
                scenario_id=f"S-evidence-{fn.name.replace('::', '_')}", function=fn.name, gap_ids=[key],
                description=f"Check the behaviour of {fn.qualified_name} that the evidence states (results, error "
                            "values, limits); assert the stated values, do not observe them.",
                expected="exactly as stated by the evidence", evidence=all_evidence,
                strongest_class=min(e.evidence_class for e in evidence), characterization=False))
        return out

    def _stubs(self, layout: uc.LayoutProfile, last: uc.VerificationResult | None) -> list[uc.StubSpec]:
        """The dependencies decided stub, with their exact declarations [A1-DA6, A2-21g]. Without a
        dependency plan (front end unavailable) text patterns are used [A1-DA14]."""
        plan = layout.dependencies
        if plan is None or (plan.estimated and not plan.entries):
            return self._text_stubs(layout, last)
        out = [stub_spec(d) for d in plan.entries if d.decision == "stub" and not d.existing_stub]
        out = [s for s in out if s is not None]
        planned = {d.name for d in plan.entries} | {d.qualified_name for d in plan.entries if d.qualified_name}
        unplanned = [s for s in (last.undefined_symbols if last else []) if s not in planned]
        if unplanned:                                     # A3-PR4: an analysis gap; added here
            out += [s for s in self._text_stubs(layout, last) if s.function in unplanned]
        return out

    def _text_stubs(self, layout: uc.LayoutProfile, last: uc.VerificationResult | None) -> list[uc.StubSpec]:
        """Functions the SUT calls but does not define, found by text patterns [A1-DA14]."""
        root = Path(layout.repo_root)
        defined = {f.name.split("::")[-1] for f in layout.functions}
        called = set()
        for f in layout.functions:
            called.update(f.calls)
        wanted = (called - defined) | set(last.undefined_symbols if last else [])
        decls: dict[str, tuple[str, str]] = {}
        for header in layout.headers + [h for h in self._all_headers(root)]:
            path = root / header
            if path.is_file():
                for name, decl in find_declarations(path.read_text(encoding="utf-8", errors="replace")).items():
                    decls.setdefault(name, (decl, header))
        sut_decls = find_declarations((root / layout.sut_file).read_text(encoding="utf-8", errors="replace"))
        out = []
        for name in sorted(wanted):
            if name in decls:
                decl, header = decls[name]
            elif name in sut_decls:
                decl, header = sut_decls[name], ""
            else:
                continue                      # standard library or macro: not stubbed
            if header and not self._project_header(root, header):
                continue
            try:
                return_type, params = parse_declaration(decl)
            except (ValueError, AttributeError):
                continue
            out.append(uc.StubSpec(function=name, declaration=decl, return_type=return_type, params=params,
                                   header=header))
        return out

    @staticmethod
    def _all_headers(root: Path) -> list[str]:
        return [str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*")
                if p.suffix.lower() in HEADER_EXT and ".git" not in p.parts and "ut_work" not in p.parts]

    @staticmethod
    def _project_header(root: Path, header: str) -> bool:
        return (root / header).is_file()

    def _gaps(self, layout: uc.LayoutProfile, last: uc.VerificationResult | None, sut_text: str) -> list[uc.GapItem]:
        measured = last is not None and last.coverage is not None and last.coverage.measured
        if measured:
            gaps = [g for fc in last.coverage.functions for g in fc.gaps]   # measured replaces estimated [A3-143]
        else:
            gaps = self._estimated_gaps(layout, sut_text)
        lines = sut_text.splitlines()
        constants = self._constants(layout) if measured else {}
        variants = self._variant_macros(layout) if measured else []                  # A1-37a
        differing = variant_dependent(variants)
        functions = {f.name: f for f in layout.functions}
        unreachable: list[tuple[int, int, str]] = []
        block_variants: dict[tuple[int, int, int], list[uc.VariantProof]] = {}
        for g in gaps:
            expr = decision_text(lines[g.line - 1], g.column) if 0 < g.line <= len(lines) else ""
            if g.metric in ("MCDC", "DC") and expr:
                leaves, pairs, infeasible = mcdc_pairs(expr)
                if g.metric == "MCDC" and g.condition_index is not None:
                    if g.condition_index in infeasible:
                        g.detail += " | infeasible_condition_combination: coupled conditions [A3-121a]"
                    elif g.condition_index in pairs:
                        g.required = list(pairs[g.condition_index])
                elif g.metric == "DC":
                    g.required = ["decision true", "decision false"]
            if g.metric == "BC" and expr and g.detail:
                g.required = [g.detail.replace("never", "must be")]
            fn = functions.get(g.function)
            if not measured or not expr or fn is None or "infeasible" in g.detail:
                continue
            # A deterministic proof that the remaining item cannot be covered [A3-120, A3-121a], with the
            # conditions on the path to it [A1-37].
            path = enclosing_conditions(sut_text, fn, g.line)
            before = "\n".join(lines[fn.line_start - 1: g.line - 1])
            use = constants
            used = differing & set(re.findall(r"[A-Za-z_]\w*", " ".join([expr, *path])))
            if used:                    # A1-37b: the item depends on a macro that differs between variants
                g.variant_proofs, use = self._variant_proofs(g, expr, fn, constants, variants, differing, used,
                                                             path, before, lines)
            proof = ""
            if g.metric == "MCDC":
                proof = prove_infeasible(expr, "MCDC", g.condition_index, "", fn, use, path, before)
                if proof:
                    g.detail += f" | infeasible_condition_combination: {proof} [A3-121a]"
            elif g.metric in ("DC", "BC"):
                seen = {o.rsplit("=", 1)[-1] for o in g.observed}
                missing = [o for o in ("T", "F") if o not in seen] if g.metric == "DC" else [g.gap_id.rsplit(":", 1)[-1]]
                index = self._leaf_at(lines[g.line - 1], expr, g.column) if g.metric == "BC" else None
                if g.metric == "BC" and index is None:
                    continue
                proofs = [prove_infeasible(expr, g.metric, index, o, fn, use, path, before) for o in missing]
                if missing and all(proofs):
                    g.detail += f" | infeasible_outcome: {'; '.join(proofs)} [A3-121a]"
                    if g.metric == "DC" or len(split_conditions(expr)) == 1:
                        for o in missing:
                            block = block_lines(lines, g.line, o)
                            if block:
                                unreachable.append((*block, f"line {g.line}: {proofs[0]}"))
                                if g.variant_proofs:                          # A1-37c: the lines inherit them
                                    block_variants[tuple(block)] = g.variant_proofs
        for g in gaps:
            if g.metric in ("LC", "SC") and "unreachable_code" not in g.detail:
                hit = next(((a, b, col, w) for a, b, col, w in unreachable if a <= g.line <= b and
                            (g.line != a or col == 0 or g.column >= col) and
                            (g.metric == "SC" or a != b or col == 0)), None)
                why = hit[3] if hit else ""
                if why:
                    g.detail += f" | unreachable_code: the branch is never taken ({why}) [A3-116, A3-121a]"
                    g.variant_proofs = list(block_variants.get(hit[:3], g.variant_proofs))
        return gaps

    def _variant_macros(self, layout: uc.LayoutProfile) -> list[tuple[uc.BuildVariant, dict[str, int]]]:
        """A1-37a, A1-37e: the macro values of every build variant, computed again whenever the build
        description or the SUT changes. Empty for a build with one variant, so nothing changes there."""
        build = layout.build
        if build is None or len(build.variants) < 2:
            return []
        key = layout.sut_sha256 + build.model_dump_json(include={"variants"})
        if key not in self._variant_cache:
            try:
                tools = uc.Tools.load(self.cfg.tools_manifest or None)
            except ba.AgentFault:
                return []
            repo = Path(layout.repo_root).resolve()
            found = [(v, preprocessor_macros(tools, repo, layout.sut_file, v.flags, layout.language))
                     for v in build.variants]
            self._variant_cache[key] = [] if any(values is None for _, values in found) else found
        return self._variant_cache[key]

    def _variant_proofs(self, g: uc.GapItem, expr: str, fn: uc.FunctionInfo, constants: dict[str, int],
                        variants: list[tuple[uc.BuildVariant, dict[str, int]]], differing: set[str], used: set[str],
                        path: list[str], before: str, lines: list[str]
                        ) -> tuple[list[uc.VariantProof], dict[str, int]]:
        """A1-37b: the proof of A1-37 for every variant, with that variant's macro values. Returns the
        results and the constants of the tested variant, which alone decides (A1-37c)."""
        results, tested = [], constants
        for variant, values in variants:
            own = {k: v for k, v in constants.items() if k not in differing}
            own.update({k: values[k] for k in differing if k in values})
            results.append(uc.VariantProof(variant=variant.name, tested=variant.tested, proof=self._proof_of(
                g, expr, fn, own, path, before, lines), macros={k: values[k] for k in sorted(used) if k in values}))
            if variant.tested:
                tested = own
        return results, tested

    def _proof_of(self, g: uc.GapItem, expr: str, fn: uc.FunctionInfo, constants: dict[str, int],
                  path: list[str], before: str, lines: list[str]) -> str:
        """The proof that _gaps() computes for one gap, with the given constants ("" = no proof)."""
        if g.metric == "MCDC":
            return prove_infeasible(expr, "MCDC", g.condition_index, "", fn, constants, path, before)
        if g.metric not in ("DC", "BC"):
            return ""
        seen = {o.rsplit("=", 1)[-1] for o in g.observed}
        missing = [o for o in ("T", "F") if o not in seen] if g.metric == "DC" else [g.gap_id.rsplit(":", 1)[-1]]
        index = self._leaf_at(lines[g.line - 1], expr, g.column) if g.metric == "BC" else None
        if g.metric == "BC" and index is None:
            return ""
        proofs = [prove_infeasible(expr, g.metric, index, o, fn, constants, path, before) for o in missing]
        return "; ".join(proofs) if missing and all(proofs) else ""

    @staticmethod
    def _leaf_at(line: str, expr: str, column: int) -> int | None:
        """Index of the condition that starts at ``column`` (1-based) of the source line."""
        start = line.find(expr)
        pos = start
        for index, leaf in enumerate(split_conditions(expr)):
            pos = line.find(leaf.lstrip("!( "), pos)
            if pos < 0:
                return None
            if abs(pos + 1 - column) <= 2 or (index == 0 and len(split_conditions(expr)) == 1):
                return index
            pos += len(leaf.lstrip("!( "))
        return None

    def _constants(self, layout: uc.LayoutProfile) -> dict[str, int]:
        """Integer macros (defined exactly once, not overridden by the build's -D flags) and
        enumerators: the constants a proof may use [A3-120]."""
        root = Path(layout.repo_root)
        files = list(dict.fromkeys(layout.headers + (layout.dependencies.headers if layout.dependencies else []) +
                                   [layout.sut_file]))
        values: dict[str, list[int]] = {}
        for rel in files:
            path = root / rel
            if not path.is_file():
                continue
            text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
            for m in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+(.+?)[ \t]*$", text, re.M):
                value = _number(m.group(2), {})
                values.setdefault(m.group(1), []).append(value if value is not None else 10 ** 30)
        flagged = {re.split(r"[=]", f[2:])[0] for f in layout.compile_flags if f.startswith(("-D", "-U"))}
        out = {k: v[0] for k, v in values.items() if len(v) == 1 and v[0] != 10 ** 30 and k not in flagged}
        for e in layout.enums:
            for k, v in e.values.items():
                out[k] = v
                out.setdefault(k.split("::")[-1], v)
        return out

    @staticmethod
    def _estimated_gaps(layout: uc.LayoutProfile, sut_text: str) -> list[uc.GapItem]:
        """A1-18a: obligations from the source when nothing is measured yet."""
        gaps, lines = [], sut_text.splitlines()
        for fn in layout.functions:
            gaps.append(uc.GapItem(gap_id=f"LC:{fn.name}:{fn.line_start}", function=fn.name, metric="LC",
                                   line=fn.line_start, detail="function never executed", estimated=True))
            for number in range(fn.line_start, fn.line_end + 1):
                expr = decision_text(lines[number - 1]) if number - 1 < len(lines) else ""
                if not expr:
                    continue
                for outcome in ("true", "false"):
                    gaps.append(uc.GapItem(gap_id=f"DC:{fn.name}:{number}:{outcome}", function=fn.name, metric="DC",
                                           line=number, detail=f"decision ({expr}) must be {outcome}", estimated=True,
                                           required=[f"({expr}) is {outcome}"]))
                leaves, pairs, _ = mcdc_pairs(expr)
                for index, pair in pairs.items():
                    gaps.append(uc.GapItem(gap_id=f"MCDC:{fn.name}:{number}:C{index + 1}", function=fn.name,
                                           metric="MCDC", line=number, condition_index=index, estimated=True,
                                           detail=f"condition ({leaves[index]}) must independently affect the decision",
                                           required=list(pair)))
        return gaps

    @staticmethod
    def _group(gaps: list[uc.GapItem]) -> list[list[uc.GapItem]]:
        """One scenario per decision/line, outer (earlier) lines first [plan section 3.3]."""
        groups: dict[tuple[int, str], list[uc.GapItem]] = {}
        for g in sorted(gaps, key=lambda g: (g.line, g.metric)):
            key = (g.line, "LC" if g.metric in ("LC", "SC") else "DEC")
            groups.setdefault(key, []).append(g)
        return list(groups.values())

    @staticmethod
    def _describe(fn: uc.FunctionInfo, group: list[uc.GapItem], sut_text: str) -> str:
        lines = sut_text.splitlines()
        lead = group[0]
        code = lines[lead.line - 1].strip() if 0 < lead.line <= len(lines) else ""
        kinds = sorted({g.metric for g in group})
        what = "; ".join(sorted({g.detail for g in group if g.detail}))[:500]
        return f"Cover {', '.join(kinds)} at line {lead.line} of {fn.name}: `{code}`. {what}"


def build_agent(settings: ba.Settings, config_path: str | None = None) -> AnalyzerAgent:
    section = {}
    if config_path:
        section = json.loads(Path(config_path).read_text(encoding="utf-8")).get("analyzer", {})
        section.setdefault("tools_manifest", str(uc.manifest_path(config_path)))
    agent = AnalyzerAgent(settings, config=AnalyzerConfig(**section))
    agent.packs = uc.knowledge_packs(config_path)                   # KP-3
    return agent


if __name__ == "__main__":
    _config = next((sys.argv[i + 1] for i, a in enumerate(sys.argv[:-1]) if a == "--config"), None)
    raise SystemExit(ba.agent_main(lambda s: build_agent(s, _config), "agent_unittest_analyzer (Agent1)"))
