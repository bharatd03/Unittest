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
what earlier tests reached); from the first coverage plan on the brain plans the scenarios per function, and
Agent1 checks every one deterministically before Agent2 receives it. When nothing passes the check, the next
brain is asked; the rules never plan in the brain's place - without a usable plan the run stops [A1-PL8, ARCH-24].

Tools (MCP, each a contract): discover, dependencies, plan, collect_evidence.
Run as an MCP server:  python agent_unittest_analyzer.py serve [--config ut_config.json]
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import csv
import functools
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
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, create_model, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

import ut_contracts as uc
from ut_contracts import ba

LOG = ba.logging.getLogger("ut.agent1")
C_EXT = (".c",)
CPP_EXT = (".cpp", ".cc", ".cxx", ".c++")


def cpp_content(text: str) -> bool:
    """A1-DA19: C++ constructs in a file (comments and strings removed)."""
    code = re.sub(r"/\*.*?\*/|//[^\n]*|\"(?:[^\"\\\n]|\\.)*\"", " ", text, flags=re.S)
    return bool(re.search(r"\b(class|namespace|template\s*<|public\s*:|private\s*:|protected\s*:|constexpr|nullptr|"
                          r"typename|operator\s*\W|static_cast|using\s+\w+\s*=)\b|\w::\w|#\s*include\s*<c(stdint|stddef|"
                          r"stdio|string|stdlib|math)>|#\s*include\s*<(vector|string|array|memory|stdexcept|cstddef)>", code))
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
    max_brain_calls_per_function: int = 2    # re-ask included [A1-PL9, ORC-7]
    max_brain_scenarios: int = 4             # per call: shorter answers, faster on a small brain [ORC-7]
    brain_functions_per_round: int = 30      # functions planned with the brain per round, at least [A1-PL16, ORC-7]
    state_per_function: int = 40             # state variables listed per function [A2-21aj]
    long_function_lines: int = 150           # above: only the paths to the open items go to the brain [LA-10]
    max_lookups_per_function: int = 6        # A1-PL6, ORC-7
    # Expected answer per brain scenario, in tokens: sets the answer limit and window [ARCH-11]
    tokens_per_scenario: int = 320
    link_requirements: bool = True           # A1-PL13
    max_requirements_to_link: int = 40
    min_link_probability: float = 0.6
    propose_declarations: bool = True        # A1-PL14
    prefer_project_stubs: bool = False       # A1-DA7a: keep a project stub that offers no control [ORC-7]
    rule_attempts: int = 2                   # brain attempts per rule failure [ARCH-21, ORC-7]
    # CSV requirement documents: the column of the ID and of the text, first match by name [A1-RQ2]
    requirement_id_columns: list[str] = ["id", "requirement id", "req id", "req-id", "identifier", "key",
                                         "reqif.foreignid"]
    requirement_text_columns: list[str] = ["text", "requirement", "requirement text", "description",
                                           "reqif.text", "summary"]
    max_selected_files: int = 200            # production files selected when a run names none [ORC-35, ORC-7]
    targets_per_scenario: int = 3            # targets one scenario may name [A1-PL3a, ORC-7]
    split_calls: int = 3                     # smaller requests after a cut answer, per function and round [ARCH-19b]
    carry_families: bool = True              # scenarios carried over within a family of similar functions [A1-PL17]


class HeaderFolder(BaseModel):
    """A1-DA20: the folder, relative to the repository, to add as an include path for a missing header."""
    folder: str = Field(description="a folder of the repository, relative to its root ('.' for the root)")
    reason: str = Field(description="one sentence: why the project finds the header there")


HEADER_TASK = (
    "The C/C++ front end cannot find a header the file includes: {header}. Choose the folder of this repository "
    "that the project's build adds as an include path for it - from CANDIDATES when there are any (files with "
    "that name), otherwise from the folders listed. Return JSON {{\"folder\": \"...\", \"reason\": \"...\"}}.")
HEADER_SKIP = {".git", ".svn", ".hg", "node_modules", ".venv", "__pycache__", "ut_output", "ut_work", "ut_cache"}


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
        elif ch in "#" and (i == 0 or code[i - 1] == "\n" or not code[code.rfind("\n", 0, i) + 1: i].strip()):
            j = code.find("\n", i)
            while j > 0 and code[j - 1] == "\\":
                j = code.find("\n", j + 1)
            j = len(code) if j < 0 else j
            if re.match(r"#\s*(?:else|elif|elifdef|elifndef)\b", code[i:j]):
                # only the first branch of #if ... #else counts: both branches opening "{" would unbalance the
                # braces and lose every later function [A1-1]
                depth, k = 1, j
                for d in re.finditer(r"(?m)^[ \t]*#[ \t]*(if|ifdef|ifndef|endif)\b", code[j:]):
                    depth += 1 if d.group(1) != "endif" else -1
                    if depth == 0:
                        k = j + d.start()
                        break
                j = code.find("\n", k) if depth == 0 else len(code)
                j = len(code) if j < 0 else j
            i = j
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


OPERATOR_NAME = re.compile(r"((?:[A-Za-z_]\w*\s*::\s*)*operator\s*(?:\(\s*\)|\[\s*\]|(?:new|delete)(?:\s*\[\s*\])?|"
                           r"[^\w\s(]+|[A-Za-z_][\w:<>*&\s]*?))\s*\(")


def _strip_balanced(text: str, start: int) -> int:
    """The index after the parenthesis group that starts at ``start`` (text[start] == "(")."""
    depth, k = 0, start
    while k < len(text):
        depth += {"(": 1, ")": -1}.get(text[k], 0)
        k += 1
        if depth == 0:
            break
    return k


def declaration_head(header: str) -> str:
    """A function header without what does not name it: attributes (__attribute__((...)), __declspec(...),
    [[...]], alignas(...)) and leading macro calls that stand for the return type or storage class
    (AUTOSAR FUNC(void, COM_CODE), a project's API_EXPORT(int)), so that "<return type> name(" remains [A1-1]."""
    out = re.sub(r"\[\[.*?\]\]", " ", header)
    for word in ("__attribute__", "__declspec", "alignas", "_Alignas", "__pragma", "_Pragma"):
        while True:
            m = re.search(r"\b" + word + r"\s*\(", out)
            if not m:
                break
            out = out[: m.start()] + " " + out[_strip_balanced(out, m.end() - 1):]
    while True:                                   # MACRO(...) before the name: the macro is the return type
        m = re.match(r"\s*((?:[A-Za-z_]\w*\s+)*?)([A-Z_][A-Z0-9_]*)\s*\(", out)
        if not m:
            break
        end = _strip_balanced(out, m.end() - 1)
        if not re.match(r"\s*[A-Za-z_~][\w:~]*\s*\(", out[end:]):
            break
        out = (m.group(1) or "") + " ut_macro_type " + out[end:]
    return re.sub(r"\s+", " ", out).strip()


def function_name(header: str) -> str:
    header = declaration_head(re.sub(r"\s+", " ", header).strip())
    if header.startswith("typedef "):
        return ""
    if not header or "(" not in header or header.startswith(("=", "return")):
        return ""
    op = OPERATOR_NAME.search(header)                     # an operator is a function too [NC-4]
    if op and not re.match(r"^\s*(?:return|if|while)\b", header):
        name = re.sub(r"\s*(::)\s*", r"\1", op.group(1).strip())
        return re.sub(r"operator\s+(?=[^\w\s])", "operator", re.sub(r"\s+", " ", name))
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


def _declared_in(short: str, namespace: str, rel: str, root: Path) -> bool:
    """A1-PL14: the header declares ``short`` inside ``namespace`` (hal::pwm in namespace hal { ... })."""
    return bool(_declarations_in(short, namespace, rel, root))


def _declarations_in(short: str, namespace: str, rel: str, root: Path) -> list[str]:
    """A1-PL14: the declarations of ``short`` that a header makes inside ``namespace`` - also when the namespace is
    written nested (namespace hal { namespace adc { ... } }) or in one (namespace drv::can { ... }) - so that two
    functions of the same name in two namespaces (hal::adc::read, drv::can::read) each get their own."""
    try:
        text = strip_comments((root / rel).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    parts = [p for p in namespace.split("::") if p]
    out: list[str] = []

    def block(start: int) -> int:
        depth, k = 1, start
        while k < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[k], 0)
            k += 1
        return k

    def search(lo: int, hi: int, rest: list[str]) -> None:
        if not rest:
            body = text[lo:hi]
            nested = [(m.start(), block(lo + m.end()) - lo) for m in re.finditer(r"\bnamespace\s+[\w:]+\s*\{", body)]
            flat = list(body)
            for a, b in nested:                 # a nested namespace is another namespace: left out here
                flat[a:b] = " " * (b - a)
            flat = "".join(flat)
            out.extend(d for n, d in _all_declarations(flat) if n == short)
            return
        for k in range(len(rest), 0, -1):       # namespace a::b { or namespace a { namespace b {
            name = "::".join(rest[:k])
            for m in re.finditer(r"\bnamespace\s+(?:inline\s+)?" + re.escape(name).replace("::", r"\s*::\s*")
                                 + r"\s*\{", text[lo:hi]):
                start = lo + m.end()
                search(start, block(start), rest[k:])
    search(0, len(text), parts)
    return out


def _all_declarations(code: str) -> list[tuple[str, str]]:
    """Every function declaration of a text, overloads included: (name, declaration)."""
    out = []
    for m in re.finditer(r"(?:^|[;}{\n])\s*((?:extern\s+)?[A-Za-z_][\w\s\*&:<>,]*?\b([A-Za-z_]\w*)\s*"
                         r"\(((?:[^;{}()]|\([^;{}()]*\))*)\)\s*(?:const\s*)?"
                         r"(?:(?:noexcept|__attribute__\s*\(\((?:[^;{}()]|\([^;{}()]*\))*\)\))\s*)*);", code):
        decl, name = re.sub(r"\s+", " ", m.group(1)).strip(), m.group(2)
        if name in KEYWORDS or decl.startswith(("return", "typedef", "#", "using")) or "=" in decl.split("(")[0]:
            continue
        out.append((name, decl))
    return out


def _arity(params: str) -> int:
    """The number of parameters of a parameter list as the linker prints it ("unsigned char, unsigned short")."""
    params = params.strip()
    if params in ("", "void"):
        return 0
    depth, count = 0, 1
    for ch in params:
        depth += {"(": 1, "<": 1, ")": -1, ">": -1}.get(ch, 0)
        count += ch == "," and depth == 0
    return count


STD_TYPES = {"size_t", "ptrdiff_t", "intptr_t", "uintptr_t", "nullptr_t", "FILE", "va_list", "string", "string_view",
             "vector", "array", "map", "unordered_map", "function", "unique_ptr", "shared_ptr", "optional", "pair"}


def unknown_types(types: list[str], texts: list[str]) -> list[str]:
    """A1-PL14: the type names of a proposed declaration that neither the language, the standard library nor the
    repository declares - a guard against an invented type (hal::pwm_channel_t)."""
    code = "\n".join(texts)
    out = []
    for t in types:
        for m in re.finditer(r"((?:[A-Za-z_]\w*::)*)([A-Za-z_]\w*)", re.sub(r"\s*::\s*", "::", t)):
            ns, name = m.group(1), m.group(2)
            if ns.startswith("std::") or name in TYPE_WORDS or name in TYPE_QUALIFIERS or name in STD_TYPES or \
                    re.fullmatch(r"u?int(?:_least|_fast)?\d+_t|u?intmax_t", name) or name in out:
                continue
            if not re.search(r"\b(?:typedef\b[^;]*\b" + re.escape(name) + r"\s*;|using\s+" + re.escape(name) +
                             r"\s*=|(?:struct|class|union|enum(?:\s+class|\s+struct)?)\s+" + re.escape(name) +
                             r"\b|#\s*define\s+" + re.escape(name) + r"\b)", code):
                out.append(ns + name)
    return out


def find_declarations(text: str) -> dict[str, str]:
    """Function declarations (prototypes) in a header: name -> declaration text."""
    return dict(_declarations(text))


@functools.lru_cache(maxsize=4096)                 # the same headers are read in every plan [A1-31b]
def _declarations(text: str) -> tuple[tuple[str, str], ...]:
    code = strip_comments(text)
    decls = {}
    # A1-DA4: one level of parentheses inside the parameters (function pointers) and trailing attributes or
    # noexcept are part of a declaration
    for m in re.finditer(r"(?:^|[;}\n])\s*((?:extern\s+)?[A-Za-z_][\w\s\*&:<>,]*?\b([A-Za-z_]\w*)\s*"
                         r"\(((?:[^;{}()]|\([^;{}()]*\))*)\)\s*(?:const\s*)?"
                         r"(?:(?:noexcept|__attribute__\s*\(\((?:[^;{}()]|\([^;{}()]*\))*\)\))\s*)*);", code):
        decl, name = re.sub(r"\s+", " ", m.group(1)).strip(), m.group(2)
        if name in KEYWORDS or decl.startswith(("return", "typedef", "#")) or "=" in decl.split("(")[0]:
            continue
        decls.setdefault(name, decl)
    return tuple(decls.items())


TYPE_WORDS = {"int", "char", "long", "short", "float", "double", "unsigned", "signed", "bool", "_Bool", "void",
              "size_t", "wchar_t", "char16_t", "char32_t", "auto"}
TYPE_QUALIFIERS = {"const", "volatile", "struct", "union", "enum", "class", "register", "restrict", "__restrict",
                   "typename"}


def parse_declaration(decl: str) -> tuple[str, list[list[str]]]:
    decl = re.sub(r"^extern\s+", "", decl.strip().rstrip(";"))
    decl = re.sub(r"\s*(?:noexcept|__attribute__\s*\(\(.*\)\))\s*$", "", decl)           # trailing attributes
    head, params = decl[: decl.index("(")], decl[decl.index("(") + 1: decl.rindex(")")]
    # the name with its namespaces (hal::pwm): the qualifier is not part of the return type [A1-PL14]
    qualified = re.search(r"((?:[A-Za-z_]\w*\s*::\s*)*)([A-Za-z_]\w*)\s*$", head)
    return_type = head[: qualified.start()].strip()
    out = []
    if params.strip() not in ("", "void"):
        for index, p in enumerate(_split_top(params)):
            p = re.sub(r"\s*=\s*[^=].*$", "", p.strip())                  # a C++ default argument
            if p == "...":                                                       # variadic [A2-21g]
                out.append(["...", ""])
                continue
            fp = re.match(r"^(.*?)\(\s*\*\s*([A-Za-z_]\w*)?\s*\)\s*(\(.*\))$", p)
            if fp:                                                               # int (*cb)(int)
                out.append([f"{fp.group(1).strip()} (*){fp.group(3)}", fp.group(2) or f"arg{index}"])
                continue
            m = re.search(r"([A-Za-z_]\w*)\s*(\[[^\]]*\])?$", p)
            before = p[: m.start(1)] if m else ""
            # a name follows a complete type: a type word that is not only a qualifier, or * or &
            named = m is not None and m.group(1) not in TYPE_WORDS and not before.rstrip().endswith("::") and (
                re.search(r"[*&]", before) or set(re.findall(r"[A-Za-z_]\w*", before)) - TYPE_QUALIFIERS)
            if named:
                ptype = (before + (m.group(2) and "*" or "")).strip()
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


# ============================================================================ solved scenarios [A1-PL15]
LEAF_COMPARE = re.compile(r"^(?P<a>[^<>=!]+?)\s*(?P<op><=|>=|==|!=|<|>)\s*(?P<b>[^<>=!]+)$")
SOLVE_CMP = {"<": lambda x, y: x < y, ">": lambda x, y: x > y, "<=": lambda x, y: x <= y, ">=": lambda x, y: x >= y,
       "==": lambda x, y: x == y, "!=": lambda x, y: x != y}


def _unwrap(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and _close_paren(text) == len(text) - 1:
        text = text[1:-1].strip()
    return text


def _constant(text: str, constants: dict[str, int]) -> float | int | None:
    text = _unwrap(text)
    if text in ("true", "TRUE"):
        return 1
    if text in ("false", "FALSE"):
        return 0
    m = re.fullmatch(r"([+-]?\d+\.\d*|[+-]?\d*\.\d+)(?:[eE][+-]?\d+)?[fFlL]?", text)
    if m:
        return float(text.rstrip("fFlL"))
    return eval_int(text, constants)


def param_constraint(leaf: str, params: set[str], constants: dict[str, int]) -> tuple[str, str, Any, bool] | None:
    """A1-PL15: (parameter, comparison, constant, negated) of a condition, ("p", "bool", None, negated) for a boolean
    parameter, or None when the condition has another form."""
    text, neg = _unwrap(leaf), False
    while text.startswith("!") and not text.startswith("!="):
        neg, text = not neg, _unwrap(text[1:])
    if text in params:
        return text, "bool", None, neg
    m = LEAF_COMPARE.match(text)
    if not m:
        return None
    a, op, b = _unwrap(m.group("a")), m.group("op"), _unwrap(m.group("b"))
    if a in params and b not in params:
        value = _constant(b, constants)
        return (a, op, value, neg) if value is not None else None
    if b in params and a not in params:
        value = _constant(a, constants)
        return (b, FLIP[op], value, neg) if value is not None else None
    return None


def _holds(constraint: tuple[str, str, Any, bool], truth: bool, value: Any) -> bool:
    _, op, constant, neg = constraint
    result = bool(value) if op == "bool" else SOLVE_CMP[op](value, constant)
    return (result != neg) == truth


def solve_parameters(fn: uc.FunctionInfo, wanted: list[tuple[tuple[str, str, Any, bool], bool]]
                     ) -> list[uc.NamedValue] | None:
    """A1-PL15: values for every parameter of ``fn`` that make every (condition, truth) of ``wanted`` hold, from the
    constants and their neighbours; None when a parameter cannot be given a value this way."""
    out = []
    for p in fn.params:
        cs = [(c, truth) for c, truth in wanted if c[0] == p.name]
        typ = (p.canonical or p.type).replace("const", "").strip()
        if p.kind == "bool" or typ in ("bool", "_Bool"):
            cands: list[Any] = [False, True]
        elif p.kind in ("int", "enum") or INTEGRAL_T.match(typ):
            lits = [int(c[2]) for c, _ in cs if c[2] is not None]
            cands = sorted({0, *lits, *(v - 1 for v in lits), *(v + 1 for v in lits)}, key=lambda v: (abs(v), v))
            if re.search(r"\bunsigned\b|\buint|\bsize_t\b", typ):
                cands = [v for v in cands if v >= 0]
        elif p.kind == "float" or FLOAT_T.match(typ):
            lits = [float(c[2]) for c, _ in cs if c[2] is not None]
            cands = sorted({0.0, *lits, *(v - 0.5 for v in lits), *(v + 0.5 for v in lits)}, key=lambda v: (abs(v), v))
        else:
            return None
        value = next((v for v in cands if all(_holds(c, truth, v) for c, truth in cs)), None)
        if value is None:
            return None
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif p.kind == "enum":
            text = f"({p.type}){int(value)}"
        else:
            text = repr(value) if isinstance(value, float) else str(int(value))
        out.append(uc.NamedValue(name=p.name, value=text))
    return out


def mcdc_vectors(expr: str) -> tuple[list[str], Any, list[tuple[bool, ...]], dict[int, tuple[tuple, tuple]]]:
    """A1-PL15: the leaves, the decision as a function of their values, the possible vectors and the unique-cause
    pair (as vectors) of every condition - mcdc_pairs as values instead of text."""
    leaves = split_conditions(expr)
    if not 1 <= len(leaves) <= 10:
        return leaves, None, [], {}
    py = to_python(expr, leaves)
    if not re.fullmatch(r"(?:\s|c\[\d+\]|\(|\)|\band\b|\bor\b|\bnot\b)*", py):
        return leaves, None, [], {}
    try:
        decide = eval("lambda c: bool(" + py + ")", {"__builtins__": {}, "bool": bool})        # noqa: S307
        decide(tuple(False for _ in leaves))
    except (SyntaxError, TypeError, IndexError, NameError):
        return leaves, None, [], {}
    vectors = list(itertools.product([False, True], repeat=len(leaves)))
    pairs = {}
    for i in range(len(leaves)):
        for v in vectors:
            w = tuple(not x if k == i else x for k, x in enumerate(v))
            if decide(v) != decide(w):
                pairs[i] = (v, w)
                break
    return leaves, decide, vectors, pairs


def _match_brace(text: str, start: int) -> int:
    """Index of the brace closing the one at ``start`` (len(text) when it is not closed)."""
    depth = 0
    for k in range(start, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return k
    return len(text)


def guard_conditions(lines: list[str], fn: uc.FunctionInfo, line: int) -> list[tuple[str, bool]] | None:
    """A1-PL15: what must hold on the path to ``line``: the if/else chain around it (true or false, A1-37) and the
    guard clauses of the function's body before it (``if (c) return ...;`` - c false, or the call would have left).
    None when the path cannot be read this way (a loop, a switch or a goto before the line)."""
    out: list[tuple[str, bool]] = []
    for cond in enclosing_conditions("\n".join(lines), fn, line):
        m = re.fullmatch(r"\((.*)\) is (true|false)", cond)
        if m is None:
            return None
        out.append((m.group(1), m.group(2) == "true"))
    first = max(fn.line_start - 1, 0)
    body = "\n".join(strip_comments(x) for x in lines[first: min(line - 1, len(lines))])
    if re.search(r"\b(for|while|do|switch|goto)\b", body):
        return None                                   # the path depends on more than the conditions before it
    open_at = body.find("{")
    if open_at < 0:
        return out
    if body[open_at:].count("{") - body[open_at:].count("}") != 1 + len(out):
        return None                                   # a block around the line that is not an if/else of the chain
    for m in re.finditer(r"\bif\s*\(", body):
        if m.start() < open_at or body[open_at: m.start()].count("{") - body[open_at: m.start()].count("}") != 1:
            continue                                  # not a statement of the function's body itself
        paren = m.end() - 1
        close = paren + _close_paren(body[paren:])
        if close <= paren:
            return None
        cond, rest = body[paren + 1: close], body[close + 1:]
        lead = len(rest) - len(rest.lstrip())
        if rest.lstrip().startswith("{"):
            stop = close + 1 + lead + _match_brace(rest, lead)
        else:
            semi = rest.find(";")
            stop = close + 1 + semi if semi >= 0 else len(body)
        if stop >= len(body):
            continue                                  # the statement encloses the line: enclosing_conditions has it
        if re.match(r"\s*else\b", body[stop + 1:]):
            continue                                  # an if/else: either way may lead on
        if re.search(r"\b(return|throw|exit|abort)\b", body[close + 1: stop + 1]):
            out.append((cond, False))
    return out


def _changed(name: str, text: str) -> bool:
    """The parameter is assigned, incremented or has its address taken in ``text``."""
    n = re.escape(name)
    return bool(re.search(rf"(?<![\w.>])(?:\+\+|--)\s*{n}\b|(?<![\w.>]){n}\s*(?:\+\+|--|[-+*/%&|^]?=(?!=)|<<=|>>=)|"
                          rf"&\s*{n}\b(?!\s*\()", text))


def solver_params(fn: uc.FunctionInfo) -> list[uc.ParamInfo]:
    """The parameters as the front end saw them, else read from the signature (scalar kinds only) [A1-DA14]."""
    if fn.params or fn.from_frontend:
        return fn.params
    try:
        _, params = parse_declaration(re.sub(r"^\s*(?:static|inline|extern)\s+", "", fn.signature.split("{")[0]))
    except (ValueError, AttributeError):
        return []
    out = []
    for typ, name in params:
        bare = re.sub(r"\bstd::", "", re.sub(r"\b(?:const|volatile|register)\b", "", typ)).strip()
        kind = "bool" if bare in ("bool", "_Bool") else "int" if INTEGRAL_T.match(bare) else \
            "float" if FLOAT_T.match(bare) else "other"
        out.append(uc.ParamInfo(name=name, type=typ, kind=kind))
    return out


def solve_gap(g: uc.GapItem, fn: uc.FunctionInfo, lines: list[str], constants: dict[str, int]
              ) -> list[tuple[list[uc.NamedValue], str]]:
    """A1-PL15: argument sets that take the open item (branch outcome, decision outcome, MC/DC pair) when the
    decision and its path compare parameters with constants; [] otherwise. Every set is checked against every
    condition; each comes with the condition vector it makes."""
    fn = fn if fn.params else fn.model_copy(update={"params": solver_params(fn)})
    if not fn.params or not 0 < g.line <= len(lines) or "infeasible" in g.detail:
        return []
    params = {p.name for p in fn.params if p.name}
    expr = decision_text(lines[g.line - 1], g.column)
    if not expr:
        return []
    before = "\n".join(strip_comments(x) for x in lines[max(fn.line_start - 1, 0): g.line - 1])
    sig_end = before.find("{")
    if any(_changed(p, before[sig_end + 1:] if sig_end >= 0 else before) for p in params):
        return []                                     # a value set before the decision: not the argument any more
    leaves, decide, vectors, pairs = mcdc_vectors(expr)
    if decide is None:
        return []
    leaf_cs = [param_constraint(x, params, constants) for x in leaves]
    path = guard_conditions(lines, fn, g.line)
    if path is None or any(c is None for c in leaf_cs):
        return []
    # A path condition may itself combine conditions: every vector of it with the needed outcome is an alternative.
    path_alts: list[list[list[tuple]]] = []
    for cond, truth in path:
        p_leaves, p_decide, p_vectors, _ = mcdc_vectors(cond)
        cs = [param_constraint(x, params, constants) for x in p_leaves]
        if p_decide is None or any(c is None for c in cs):
            return []
        path_alts.append([list(zip(cs, v)) for v in p_vectors if p_decide(v) == truth])
    combos = [[]]
    for alts in path_alts:
        combos = [c + a for c in combos for a in alts][:64]
        if not combos:
            return []

    def flip(v: tuple, k: int) -> tuple:
        return tuple(not x if i == k else x for i, x in enumerate(v))

    targets: list[list[tuple]] = []                  # alternatives: each a list of vectors that must all be made
    if g.metric == "MCDC" and g.condition_index is not None and 0 <= g.condition_index < len(leaves):
        k = g.condition_index
        targets = [[v, flip(v, k)] for v in vectors if not v[k] and decide(v) != decide(flip(v, k))]
    elif g.metric == "BC":
        m = re.search(r":([TF])$", g.gap_id)
        line_text = lines[g.line - 1]
        at, cursor = None, line_text.find(expr)
        for k, leaf in enumerate(leaves):
            pos = line_text.find(leaf, max(cursor, 0))
            if pos >= 0 and pos < g.column <= pos + len(leaf) + 1:
                at = k
            cursor = pos + len(leaf) if pos >= 0 else cursor
        if m is None or at is None:
            return []
        truth = m.group(1) == "T"
        targets = [[v] for v in vectors if v[at] == truth and decide(v) != decide(flip(v, at))]   # evaluated
    elif g.metric == "DC":
        seen = {o.rsplit("=", 1)[-1] for o in g.observed}
        for want in (True, False):
            if ("T" if want else "F") not in seen:
                targets = [[v] for v in vectors if decide(v) == want]
                break
    targets = targets[:256]
    for target in targets:
        found = []
        for vec in target:
            for combo in combos:
                args = solve_parameters(fn, list(zip(leaf_cs, vec)) + combo)
                if args is not None:
                    found.append((args, _vec_text(leaves, vec, decide(vec))))
                    break
            else:
                break
        if len(found) == len(target):
            return found
    return []


def _vec_text(leaves: list[str], vec: tuple[bool, ...], result: bool) -> str:
    return ", ".join(f"({leaf}) is {'true' if v else 'false'}" for leaf, v in zip(leaves, vec)) + \
        f" -> decision {'true' if result else 'false'}"


def enclosing_conditions(source: str, fn: uc.FunctionInfo, line: int) -> list[str]:
    """Reaching conditions: the if/else chain that must hold to reach ``line`` [A1-37]. An else-if branch is reached
    only when every earlier condition of its chain is false and its own condition is true; an else branch when every
    condition of the chain is false. Each open block is [depth, its conditions, the raw conditions of its chain]."""
    lines = source.splitlines()
    stack: list[list[Any]] = []
    closed: list[Any] | None = None                    # the block that closed last: an else on the next line extends it
    for number in range(fn.line_start, min(line, fn.line_end) + 1):
        text = strip_comments(lines[number - 1]) if number - 1 < len(lines) else ""
        if not text.strip():
            continue
        elif_ = re.search(r"\belse\s+if\s*\((.*)\)\s*\{", text)
        else_ = None if elif_ else re.search(r"\belse\s*\{", text)
        if elif_ or else_:
            if text.lstrip().startswith("}") and stack:      # "} else ..." closes the previous branch here
                closed = stack.pop()
            chain = list(closed[2]) if closed else []
            conds = [f"({c}) is false" for c in chain]
            if elif_ and number < line:
                conds.append(f"({elif_.group(1).strip()}) is true")
            stack.append([1, conds, chain + ([elif_.group(1).strip()] if elif_ else [])])
            closed = None
            continue
        m = re.search(r"\b(if|while)\s*\((.*)\)\s*\{", text)
        if m and number < line:
            stack.append([max(1, text.count("{") - text.count("}")), [f"({m.group(2).strip()}) is true"],
                          [m.group(2).strip()] if m.group(1) == "if" else []])
            closed = None
            continue
        delta = text.count("{") - text.count("}")
        if stack and delta:
            stack[-1][0] += delta
            if stack[-1][0] <= 0:
                closed = stack.pop()
            continue
        if not text.strip().startswith("}"):
            closed = None                                    # a statement between: no else can follow
    return [c for block in stack for c in block[1]]


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


TRAILING_QUALIFIERS = re.compile(r"(?:\s*\b(?:const|volatile|restrict|__restrict|__restrict__)\b)+\s*$")


def pointer_core(t: str) -> str:
    """A1-DA21: the type without the qualifiers of the pointer itself ('List_t *const' -> 'List_t *')."""
    return TRAILING_QUALIFIERS.sub("", t.strip())


def _unqualified(t: str) -> str:
    """A type as a test declares a variable of it: qualifiers removed, struct/union/enum kept (C needs them)."""
    t = re.sub(r"\b_Atomic\s*\(([^()]*)\)", r"\1", t)
    t = re.sub(r"\b(const|volatile|restrict|__restrict|__restrict__|_Atomic)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def qualify_type(t: str, scoped: dict[str, str], namespace: str) -> str:
    """A2-21ad: a type as written inside a namespace ('const Evt *const' in namespace fw), with the names declared
    in a namespace qualified ('const fw::Evt *const'), so a test outside the namespace can use it."""
    if not t or not scoped or not namespace:
        return t
    def one(m: re.Match) -> str:
        name = m.group(0)
        start = m.start()
        if start >= 2 and t[start - 2: start] == "::":
            return name                                   # already qualified
        return scoped.get(name, name)
    return re.sub(r"\b[A-Za-z_]\w*\b", one, t)


def _plain_type(t: str) -> str:
    t = re.sub(r"\b_Atomic\s*\(([^()]*)\)", r"\1", t)                   # A1-DA21: _Atomic(unsigned long)
    t = re.sub(r"\b(const|volatile|struct|union|enum|class|restrict|__restrict|_Atomic)\b", " ", t)
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
            j = m.start()                                 # any indentation: includedFrom can be deeply nested
            while j > 0 and text[j - 1] in " \t\r\n":
                j -= 1
            if text.endswith('"includedFrom": {', 0, j):
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
        self.internal: set[str] = set()             # ids declared inside an unnamed namespace (internal linkage)
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
        if kind == "CXXRecordDecl" and name and scope.rsplit("::", 1)[-1] == name and \
                self.nodes.get(parent, {}).get("kind") == "ClassTemplateDecl":
            qualified = scope                     # the record of a class template is the template itself
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
        unnamed = kind == "NamespaceDecl" and not name          # internal linkage, also inside a named namespace
        for child in node.get("inner", []) or []:
            if isinstance(child, dict):
                if (unnamed or nid in self.internal) and child.get("id"):
                    self.internal.add(child["id"])
                self._index(child, inner_scope, inner_parent)

    def qualified_name(self, node: dict) -> str:
        nid = node.get("id", "")
        name = self.qualified.get(nid) or node.get("name", "")
        owner = node.get("parentDeclContextId")
        # The semantic owner wins over the place of the definition: `namespace QP { void *QMPool::get() {} }` is
        # QP::QMPool::get, not QP::get (a member defined outside its class, inside a namespace) [A1-DA21]
        if owner and owner in self.qualified and node.get("name"):
            name = f"{self.qualified[owner]}::{node['name']}"
        return name

    def namespace_of(self, node: dict) -> str:
        """The named namespaces around a declaration, outermost first ("QP::detail") [A2-21ad]."""
        names, nid = [], self.parent.get(node.get("id", ""))
        while nid:
            parent = self.nodes.get(nid, {})
            if parent.get("kind") == "NamespaceDecl":
                if not parent.get("name"):
                    return ""                                   # an anonymous namespace: not nameable
                names.append(parent["name"])
            nid = self.parent.get(nid)
        return "::".join(reversed(names))

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
    cache: "uc.RunCache | None" = None               # CA-11: set by build_agent

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
        if Path(self.path).suffix.lower() in HEADER_EXT + (".inl", ".ipp", ".tpp"):
            cmd[-1:-1] = ["-x", "c++" if cpp else "c"]          # a header SUT parsed as a source [A1-DA19]
        key = self._cache_key(cmd)
        found = self._from_cache(key) if key else None                                       # CA-11
        if found is not None:
            text, err = found
        else:
            try:
                proc = subprocess.run(cmd, cwd=str(self.repo), capture_output=True, timeout=timeout,
                                      stdin=subprocess.DEVNULL, env={**os.environ, **self.tools.env()})
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.errors.append(f"front end did not run: {exc}")
                return False
            err, text = proc.stderr.decode("utf-8", "replace"), proc.stdout.decode("utf-8", "replace")
            if key and text.lstrip().startswith("{") and "file not found" not in err:   # a missing header may come
                with contextlib.suppress(Exception):                     # a cache never fails a run [CA-1]
                    self._to_cache(key, text, err)
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
        if not text.strip().startswith("{"):
            self.errors.append("no syntax tree: " + err.strip()[-300:])
            return False
        self.ast = AstIndex(text, self.repo, self.repo)
        repo_str = str(self.repo)
        self.ast.parse_chunks(lambda p: p.startswith(repo_str))
        return not self.missing

    # ------------------------------------------------------------ CA-11: a parse of unchanged files
    def _cache_key(self, cmd: list[str]) -> str:
        if self.cache is None or ba.cache_switched_off():
            return ""
        try:
            source = uc.sha256_bytes(Path(self.path).read_bytes())
        except OSError:
            return ""
        return self.cache.key("code_analysis", json.dumps(self.tools.tc, sort_keys=True, default=str),
                              uc.unrooted("\x1f".join(cmd), str(self.repo)), source)

    def _headers_read(self, err: str) -> list[Path]:
        out = []
        for line in err.splitlines():
            m = re.match(r"^\.+ (.+)$", line)
            if m:
                p = Path(m.group(1).strip())
                out.append(p if p.is_absolute() else self.repo / p)
        return out

    def _from_cache(self, key: str) -> tuple[str, str] | None:
        """The stored output while every header it read is unchanged; it must name the parsed file."""
        import zlib
        manifest = self.cache.get_json(key, count=False)
        hit = None
        if isinstance(manifest, dict) and isinstance(manifest.get("deps"), dict) and manifest.get("output"):
            fresh = True
            for name, digest in manifest["deps"].items():
                try:
                    if uc.sha256_bytes(Path(uc.rooted(name, str(self.repo))).read_bytes()) != digest:
                        fresh = False
                        break
                except OSError:
                    fresh = False
                    break
            data = self.cache.get_bytes(str(manifest["output"]), ".zz", count=False) if fresh else None
            if data:
                with contextlib.suppress(Exception):
                    text, err = uc.rooted(zlib.decompress(data).decode("utf-8"), str(self.repo)).split("\x00", 1)
                    if json.dumps(self.path)[1:-1] in text:
                        hit = (text, err)
        self.cache._count(hit is not None)
        return hit

    def _to_cache(self, key: str, text: str, err: str) -> None:
        import zlib
        deps = {}
        for path in self._headers_read(err):
            deps[uc.unrooted(str(path), str(self.repo))] = uc.sha256_bytes(path.read_bytes())   # unreadable: no entry
        stored = uc.unrooted(text + "\x00" + err, str(self.repo))
        if uc.names_a_run(stored[:20_000_000]) or json.dumps(self.path)[1:-1] not in text:
            return
        output = self.cache.key("code_analysis-output", key, str(sorted(deps.items())))
        self.cache.put_bytes(output, zlib.compress(stored.encode("utf-8"), 1), ".zz")
        self.cache.put_json(key, {"deps": deps, "output": output})

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

    def private_types(self) -> list[str]:
        """A2-21ad: names of the types (struct, union, enum, typedef) the SUT file itself declares: a test in another
        file cannot name them, so no rule-based test or wrapper prototype may use them."""
        out = []
        for nid, node in self.ast.nodes.items():
            if node.get("kind") not in ("RecordDecl", "CXXRecordDecl", "EnumDecl", "TypedefDecl", "TypeAliasDecl") \
                    or not node.get("name") or node.get("isImplicit"):
                continue
            offset = self.ast.ids.get(nid)
            if offset is not None and self.ast.node_file(offset) == self.path:
                out.append(node["name"])
        return sorted(set(out))

    def local_statics(self) -> list[tuple[str, str, str]]:
        """A1-DA18: (function, variable, type) of every static variable defined inside a function of this
        file, C and C++ (a static object inside a function included)."""
        out = []
        for node, qualified in self.definitions():
            if node.get("kind") == "VarDecl":
                continue
            for sub in _walk([c for c in node.get("inner", []) or [] if c.get("kind") == "CompoundStmt"]):
                if sub.get("kind") == "VarDecl" and sub.get("storageClass") == "static" and sub.get("name"):
                    out.append((qualified or node.get("name", ""), sub["name"], (sub.get("type") or {}).get("qualType", "")))
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

    def callee_name(self, ref: dict) -> str:
        """The qualified name of a function a reference names, as the dependency plan names it (a using-declaration
        or a namespace the call omits resolved by the compiler: read(3) after using hal::adc::read is
        hal::adc::read); "" when it is not known."""
        if ref.get("kind") == "CXXConstructorDecl" and ref.get("class"):
            return f"{ref['class']}::{ref['class'].split('::')[-1]}"
        node = self.ast.nodes.get(ref.get("id", ""), {})
        name = self.ast.qualified_name(node) if node else ""
        return name if name and name != ref.get("name") else ""

    def _find_ctor(self, cls: str, ctor_type: str) -> str:
        for nid, node in self.ast.nodes.items():
            if node.get("kind") == "CXXConstructorDecl" and not node.get("isImplicit") and \
                    (node.get("type") or {}).get("qualType") == ctor_type and \
                    self.ast.qualified.get(self.ast.parent.get(nid, ""), "") == cls:
                return nid
        return ""

    # ------------------------------------------------------------ types for test inputs
    def _abstract(self) -> set[str]:
        """Names (short and qualified) of the classes with pure virtual methods."""
        if not hasattr(self, "_abstract_names"):
            names = set()
            for i in self.interfaces():
                names |= {i.name, i.name.split("::")[-1]}
            self._abstract_names = names
        return self._abstract_names

    def interfaces(self) -> list[uc.InterfaceInfo]:
        """A1-DA17: classes of the repository with pure virtual methods (injected interfaces)."""
        out = []
        for nid, node in self.ast.nodes.items():
            if node.get("kind") != "CXXRecordDecl" or not node.get("completeDefinition"):
                continue
            pure = [c for c in node.get("inner", []) or [] if c.get("kind") == "CXXMethodDecl" and c.get("pure")]
            if not pure:
                continue
            methods = []
            for m in pure:
                qt = (m.get("type") or {}).get("qualType", "")
                ret = qt[: qt.index("(")].strip() if "(" in qt else "void"
                suffix = re.search(r"\)\s*((?:const|volatile|noexcept|&|&&|\s)*)$", qt)
                methods.append(uc.InterfaceMethod(
                    name=m.get("name", ""), return_type=ret,
                    params=[[(p.get("type") or {}).get("qualType", ""), p.get("name") or f"arg{i}"]
                            for i, p in enumerate(c for c in m.get("inner", []) or [] if c.get("kind") == "ParmVarDecl")],
                    suffix=(" " + suffix.group(1).strip()) if suffix and suffix.group(1).strip() else ""))
            path, _ = self.ast.decl_location(nid)
            out.append(uc.InterfaceInfo(name=self.ast.qualified.get(nid, node.get("name", "")),
                                        header=self.ast.rel(path) if path else "", methods=methods))
        return out

    def typedefs(self) -> dict[str, str]:
        """A1-DA21: every typedef and type alias of the repository's headers -> the type it names (one level,
        as the front end prints it), by short and by qualified name; a test resolves chains of them."""
        if getattr(self, "_typedef_map", None) is not None:
            return self._typedef_map
        out: dict[str, str] = {}
        self._typedef_map = out
        for nid, node in self.ast.nodes.items():
            if node.get("kind") in ("TypedefDecl", "TypeAliasDecl") and node.get("name") and not node.get("isImplicit"):
                t = node.get("type") or {}
                target = t.get("desugaredQualType") or t.get("qualType", "")
                if target and target != node["name"]:
                    out.setdefault(node["name"], target)
                    out.setdefault(self.ast.qualified.get(nid, node["name"]), target)
        return out

    def scoped_types(self) -> dict[str, str]:
        """A2-21ad: short name -> qualified name of the records, enums and type aliases declared in a C++ namespace
        (only names that are unique), so tests outside the namespace can name them."""
        seen: dict[str, set[str]] = {}
        for nid, node in self.ast.nodes.items():
            if node.get("kind") in ("RecordDecl", "CXXRecordDecl", "EnumDecl", "TypedefDecl", "TypeAliasDecl",
                                    "ClassTemplateDecl") and node.get("name") and not node.get("isImplicit"):
                qualified = self.ast.qualified.get(nid, node["name"])
                if "::" in qualified and "(anonymous" not in qualified:
                    seen.setdefault(node["name"], set()).add(qualified)
        return {name: next(iter(q)) for name, q in seen.items() if len(q) == 1}

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
        out, unnamed = [], None
        for c in record.get("inner", []) or []:
            if c.get("kind") in ("RecordDecl", "CXXRecordDecl") and not c.get("name") and c.get("completeDefinition"):
                unnamed = c                          # struct { ... } bits; - the record of the field that follows
                continue
            if c.get("kind") != "FieldDecl":
                continue
            t = (c.get("type") or {}).get("desugaredQualType") or (c.get("type") or {}).get("qualType", "")
            if unnamed is not None and re.search(r"\((?:unnamed|anonymous)\b", t) and depth < 3:
                sub = self._fields(unnamed, depth + 1)
                unnamed = None
                if sub is not None and c.get("name"):
                    out.append(uc.FieldInfo(name=c["name"], type=re.sub(r"\s*\(.*$", "", t) or "struct",
                                            kind="struct", fields=sub))
                    continue
            info = uc.FieldInfo(name=c.get("name", ""), type=(c.get("type") or {}).get("qualType", t))
            base = self._through_typedefs(_plain_type(t), {})                          # A1-DA21
            m = re.match(r"^(.*?)\s*\[(\d+)\]$", base)
            if m:
                info.kind, info.array_len = "array", int(m.group(2))
            elif "(*)" in t:
                info.kind = "callback"                       # a function pointer field: NULL by default [A1-DA21]
            elif pointer_core(t).endswith("*"):
                qt_field = (c.get("type") or {}).get("qualType", t)
                info.kind, info.base = "pointer", _unqualified(pointer_core(qt_field)[:-1])    # A1-DA21
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

    def _through_typedefs(self, base: str, enums: dict[str, uc.EnumInfo]) -> str:
        """A1-DA21: a base type the rules do not know, resolved through the repository's typedefs until it is a
        builtin, an integer typedef of the standard library, a known record or an enum (typedef struct pb_istream_s
        pb_istream_t names the record pb_istream_s)."""
        seen = set()
        while base and base not in seen and not (base in ("bool", "_Bool", "void") or INTEGRAL_T.match(base)
                                                 or FLOAT_T.match(base) or base in self._records() or base in enums):
            seen.add(base)
            nxt = self.typedefs().get(base)
            if not nxt:
                break
            base = _plain_type(pointer_core(nxt)) if not pointer_core(nxt).endswith("*") else base
            if base in seen:
                break
        return base

    def param_info(self, parm: dict, enums: dict[str, uc.EnumInfo], body: str) -> uc.ParamInfo:
        qt = (parm.get("type") or {}).get("qualType", "")
        dt = (parm.get("type") or {}).get("desugaredQualType") or qt
        name = parm.get("name", "")
        info = uc.ParamInfo(name=name, type=qt, canonical=dt)
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
        core, qcore = pointer_core(t), pointer_core(qt)                       # A1-DA21: T * const is a pointer
        pointer = core.endswith("*") or by_ref
        base_dt = core.rstrip("*&").strip() if pointer else t
        base = self._through_typedefs(_plain_type(base_dt), enums)                    # A1-DA21: pb_byte_t -> uint8_t
        if base.startswith("std::") and (INTEGRAL_T.match(base[5:]) or FLOAT_T.match(base[5:])):
            base = base[5:]                       # std::uint8_t is uint8_t: the same builtin, named in namespace std
        info.base = _unqualified(qcore.rstrip("*&").strip()) if pointer else _unqualified(qt)
        info.by_ref = by_ref
        enum = enums.get(base) or enums.get(_plain_type(qcore.rstrip("*&")))
        if pointer and base in ("char", "signed char", "unsigned char") and not by_ref:
            info.kind = "cstring" if base_dt.startswith("const") or "const char" in qt else "buffer"
        elif pointer and base == "void" and not by_ref:
            info.kind = "ptr_void"                                                  # A2-21h: a zeroed buffer
        elif pointer and base in self._abstract():
            info.kind = "interface"                                                 # A1-DA17: a fake is passed
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


RANGE_WORDS = re.compile(r"\b(range|ranges|limit|limits|bound|bounds|inside|within|between|valid|allowed|min|max)\b",
                         re.I)


def defect_candidate(expr: str, metric: str, index: int | None, outcome: str, fn: uc.FunctionInfo,
                     constants: dict[str, int], path: list[str], before: str, proof: str, lines: list[str],
                     line: int) -> uc.DefectCandidate | None:
    """A1-37f: the proof no longer holds when the operators are swapped (|| for &&, && for ||) - then the operator is
    the likely defect. The comment at the decision is its evidence when it names the bounds or a range."""
    if "||" not in expr and "&&" not in expr:
        return None
    mutant = re.sub(r"\|\||&&", lambda m: "&&" if m.group(0) == "||" else "||", expr)
    if prove_infeasible(mutant, metric, index, outcome, fn, constants, path, before) or \
            any(prove_infeasible(mutant, "DC", None, o, fn, constants, path, before) for o in ("T", "F")):
        return None                         # the swap does not explain it, or makes the decision constant: redundant
    names = set(re.findall(r"\b([A-Za-z_]\w*)\b", expr))
    evidence = ""
    for k in (line - 1, line - 2, line - 3, line):             # the comment above it, or at its end
        if 0 < k <= len(lines):
            m = re.search(r"(//.*|/\*.*?\*/|/\*.*$|^\s*\*.*$)", lines[k - 1])
            if m and (RANGE_WORDS.search(m.group(1)) or names & set(re.findall(r"\b([A-Za-z_]\w*)\b", m.group(1)))):
                evidence = f"line {k}: {m.group(1).strip()[:160]}"
                break
    return uc.DefectCandidate(line=line, function=fn.qualified_name, decision=expr.strip(), proof=proof[:400],
                              change=f"{expr.strip()} -> {mutant.strip()}", evidence=evidence)


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
        # no else: when the then-block always returns, the rest of the enclosing block is the false branch [A1-37]
        then = strip_comments(text[start + 1:end]).strip()
        last = then.rsplit(";", 2)[-2] if then.count(";") >= 1 else ""
        if not re.match(r"\s*(?:\}\s*)*return\b", last.split("{")[-1].split("}")[-1]):
            return None
        depth = 0
        for i in range(end + 1, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth < 0:
                    first = line + text.count("\n", 0, end) + 1
                    stop = line + text.count("\n", 0, i) - (0 if text[text.rfind("\n", 0, i) + 1:i].strip() else 1)
                    return (first, stop, 0) if stop >= first else None
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


# ============================================================================ production files [ORC-35]
TEST_PARTS = {"test", "tests", "testing", "unittest", "unittests", "unit_tests", "unit-tests", "ut", "utest", "gtest",
              "googletest", "googlemock", "gmock", "unity", "cmocka", "cpputest", "catch2", "doctest", "mock", "mocks",
              "fake", "fakes", "stub", "stubs", "testdata", "test_data"}
OTHER_PARTS = {"example", "examples", "sample", "samples", "demo", "demos", "doc", "docs", "documentation"}
THIRD_PARTY_PARTS = {"third_party", "thirdparty", "third-party", "3rdparty", "vendor", "vendors", "external",
                     "externals", "extern", "deps", "_deps"}
TEST_NAME = re.compile(r"(?:^|[_\-.])(?:test|tests|unittest|mock|mocks|stub|stubs|fake|fakes)(?:$|[_\-.])|"
                       r"[a-z0-9](?:Test|Tests|Mock|Stub|Fake)$", re.I)
LICENSE_NAME = re.compile(r"^(licen[cs]e|copying)(\b|[._-])", re.I)
DEFINITION = re.compile(r"(?m)^[ \t]*(?:[A-Za-z_][\w:<>,~]*[ \t*&]+)+[A-Za-z_~][\w:~]*[ \t]*\([^;{}]*\)[ \t\n]*"
                        r"(?:const[ \t\n]*)?(?:noexcept[ \t\n]*)?(?:->[^;{]+)?\{")


def _build_part(part: str) -> bool:
    low = part.lower()
    return low in ("out", "bin", "obj", "build") or low.startswith(("build-", "build_", "cmake-build"))


def select_production_files(root: Path, compile_commands: str = "", limit: int = 200) -> uc.FileSelection:
    """ORC-35: the production files of a working tree - C and C++ sources that are not tests, the pipeline's own
    files, examples or documentation, build output, third-party or generated code, and define a function."""
    submodules = set()
    gitmodules = root / ".gitmodules"
    if gitmodules.is_file():
        submodules = {m.strip().strip("/").replace("\\", "/") for m in
                      re.findall(r"^\s*path\s*=\s*(.+)$", gitmodules.read_text(encoding="utf-8", errors="replace"), re.M)}
    in_database: set[str] | None = None
    if compile_commands:
        try:
            entries = json.loads(Path(compile_commands).read_text(encoding="utf-8"))
            in_database = set()
            for e in entries:
                path = Path(e.get("file", ""))
                path = path if path.is_absolute() else Path(e.get("directory", "")) / path
                with contextlib.suppress(ValueError, OSError):
                    in_database.add(str(path.resolve().relative_to(root.resolve())).replace("\\", "/"))
        except (OSError, ValueError, TypeError, AttributeError):
            in_database = None
    licensed: dict[Path, bool] = {}

    def has_license(folder: Path) -> bool:
        if folder not in licensed:
            licensed[folder] = any(LICENSE_NAME.match(p.name) for p in folder.iterdir() if p.is_file())
        return licensed[folder]

    selected, left_out = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in HEADER_SKIP and not d.startswith("."))
        folder = Path(dirpath)
        rel_dir = folder.relative_to(root)
        for name in sorted(filenames):
            if Path(name).suffix.lower() not in C_EXT + CPP_EXT:
                continue
            rel = (rel_dir / name).as_posix()
            parts = [p.lower() for p in rel_dir.parts]
            reason = ""
            if any(rel == m or rel.startswith(m + "/") for m in submodules):
                reason = "third-party code (git submodule)"
            elif any(p in THIRD_PARTY_PARTS for p in parts):
                reason = "third-party code (folder name)"
            elif any(has_license(root.joinpath(*rel_dir.parts[: k + 1])) for k in range(len(rel_dir.parts))):
                reason = "third-party code (a folder with its own license file)"
            elif any(p in TEST_PARTS for p in parts) or TEST_NAME.search(Path(name).stem):
                reason = "test code"
            elif any(p in OTHER_PARTS for p in parts):
                reason = "example or documentation"
            elif any(_build_part(p) for p in rel_dir.parts):
                reason = "build output"
            elif in_database is not None and rel not in in_database:
                reason = "not compiled by the build (compile database)"
            if not reason:
                try:
                    text = (folder / name).read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    text, reason = "", f"not readable ({exc.__class__.__name__})"
                head = "\n".join(text.splitlines()[:30]).lower()
                if not reason and "generated by agent_unittest_generator" in head:
                    reason = "the pipeline's own file"
                elif not reason and re.search(r"auto-?generated|generated (by|from|with)", head) and \
                        re.search(r"do not (edit|modify)|don't (edit|modify)", head):
                    reason = "generated code"
                elif not reason and not DEFINITION.search(strip_comments(text)) and not find_functions(text, rel):
                    reason = "no function definition"
            if reason:
                left_out.append(f"{rel}: {reason}")
            elif len(selected) >= limit:
                left_out.append(f"{rel}: over the limit of {limit} files (ORC-7)")
            else:
                selected.append(rel)
    return uc.FileSelection(selected=selected, left_out=left_out)


# ============================================================================ requirement documents [A1-25x]
REQ_ID = re.compile(r"\b([A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)*[-_]\d+[a-z]?)\b")


REQUIREMENT_FORMATS = (".reqif", ".reqifz", ".md", ".markdown", ".txt", ".csv")       # A1-RQ2
DOCUMENT_ERRORS = (OSError, ValueError, KeyError, StopIteration, UnicodeError, csv.Error, zipfile.BadZipFile,
                   ET.ParseError)
Columns = tuple[tuple[str, ...], tuple[str, ...]]
DEFAULT_COLUMNS: Columns = (tuple(AnalyzerConfig().requirement_id_columns), tuple(AnalyzerConfig().requirement_text_columns))


def read_document(path: Path, label: str, columns: Columns = DEFAULT_COLUMNS) -> list[tuple[str, str, str, int]]:
    """(requirement id, text, label, line) of one document: ReqIF, CSV, else Markdown or plain text [A1-RQ2]."""
    suffix = path.suffix.lower()
    if suffix in (".reqif", ".reqifz"):
        return _reqif(path, label)
    if suffix == ".csv":
        return _csv_requirements(path.read_text(encoding="utf-8-sig", errors="replace"), label, columns)
    return _markdown_requirements(path.read_text(encoding="utf-8", errors="replace"), label)


def read_requirements(root: Path, files: list[str], given: list[uc.RequirementDocument] | None = None,
                      columns: Columns = DEFAULT_COLUMNS) -> list[tuple[str, str, str, int]]:
    """(requirement id, text, file, line) from the documents given for the run (A1-RQ1) and those of the
    repository (A1-25x). An ID a given document states is taken from there only [A1-RQ4]."""
    out = []
    for doc in given or []:
        try:
            out += read_document(Path(doc.path), doc.name, columns)
        except DOCUMENT_ERRORS as exc:
            LOG.warning("requirement document %s not readable: %s", doc.name, exc)
    stated = {r[0] for r in out}
    for rel in files:
        try:
            out += [r for r in read_document(root / rel, rel, columns) if r[0] not in stated]
        except DOCUMENT_ERRORS as exc:
            LOG.warning("requirement document %s not readable: %s", rel, exc)
    return out


def _csv_requirements(text: str, rel: str, columns: Columns) -> list[tuple[str, str, str, int]]:
    """A1-RQ2: CSV with an ID column and a text column (names configurable); the line is the row's line."""
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = csv.reader(text.splitlines(), dialect)
    header = [h.strip().lower() for h in next(rows, [])]
    id_col = next((header.index(c) for c in columns[0] if c in header), None)
    text_col = next((header.index(c) for c in columns[1] if c in header), None)
    if id_col is None or text_col is None:
        raise ValueError(f"no ID column ({', '.join(columns[0])}) or no text column ({', '.join(columns[1])}) "
                         f"in the first row")
    out = []
    for number, row in enumerate(rows, 2):
        if len(row) > max(id_col, text_col) and row[id_col].strip() and row[text_col].strip():
            out.append((row[id_col].strip(), re.sub(r"\s+", " ", row[text_col]).strip()[:1500], rel, number))
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
                           namespace=dep.namespace,
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
                       header=header, kind=dep.kind, qualified_name=dep.qualified_name, method_suffix=dep.method_suffix,
                       namespace=dep.namespace, overload=dep.overload)


# ============================================================================ brain planning [A1-PL1 .. A1-PL14]
# ============================================================================ targets and families [A1-PL3a, A1-PL17]
class Target(BaseModel):
    """A1-PL3a: the open items of one function reached by the same path, named by a short id the brain uses."""
    tid: str
    items: list[str]
    function: str = ""
    first: int = 0
    last: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    conditions: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)
    needs: list[str] = Field(default_factory=list)


def _outcome(gap_id: str) -> str:
    tail = gap_id.rsplit(":", 1)[-1]
    return tail if tail in ("T", "F") else ""


def make_targets(fn: uc.FunctionInfo, gaps: list[uc.GapItem], sut_text: str,
                 layout: uc.LayoutProfile | None = None) -> list[Target]:
    """A1-PL3a: items reached by the same path form one target - the lines and statements of one block, the branch
    outcome that enters it, and the decision and MC/DC items of its condition. A documented-behaviour check
    (EVIDENCE:<function>) is a target of its own. Targets in the order of their first line: T1, T2, ..."""
    lines = sut_text.splitlines()
    groups: dict[tuple, list[uc.GapItem]] = {}
    special: list[Target] = []
    for g in gaps:
        if g.gap_id.startswith("EVIDENCE:"):
            special.append(Target(tid=g.gap_id, items=[g.gap_id], function=fn.name))
            continue
        owner = fn
        if g.line and not fn.line_start <= g.line <= fn.line_end and layout is not None:
            owner = next((c for c in layout.functions if c.line_start <= g.line <= c.line_end), fn)
        line = g.line
        if g.metric == "BC" and _outcome(g.gap_id) and line:              # the outcome enters its block
            block = block_lines(lines, line, _outcome(g.gap_id))
            if block:                                 # its first statement: the brace's own line holds none
                line = block[0] + 1 if block[1] > block[0] and block[0] in (line, line + 1) else block[0]
        path = tuple(enclosing_conditions(sut_text, owner, line)) if line else ()
        groups.setdefault((owner.name,) + path, []).append(g)
    out: list[Target] = []
    for key, items in sorted(groups.items(), key=lambda kv: (min((g.line or 0) for g in kv[1]), kv[0])):
        counts: dict[str, int] = {}
        for g in items:
            counts[g.metric] = counts.get(g.metric, 0) + 1
        numbers = [g.line for g in items if g.line]
        details = [f"{g.metric} line {g.line}: {g.detail[:90]}" for g in items if g.metric not in ("LC", "SC")][:3]
        needs = next((list(g.required[:3]) for g in items if g.required), [])
        out.append(Target(tid=f"T{len(out) + 1}", items=[g.gap_id for g in items], function=key[0],
                          first=min(numbers, default=0), last=max(numbers, default=0), counts=counts,
                          conditions=list(key[1:7]), details=details, needs=needs))
    return out + special


def expand_targets(planned: "PlannedScenario", by_id: dict[str, Target]) -> list[str]:
    """A1-PL3a: the targets a scenario names, expanded to their items (gap_ids); a target written with its row text
    ("T3: lines ...") is read as its id, and an item id given directly stays. Returns the format fixes."""
    fixes, tids, ids = [], [], []
    for value in list(planned.targets) + list(planned.gap_ids):
        m = re.match(r"\s*\[?(T\d+|EVIDENCE:\S+?)\]?(?=$|[\s:,|(\-])", str(value), re.I)
        tid = (m.group(1).upper() if m and m.group(1)[0] in "tT" else m.group(1)) if m else ""
        if tid in by_id:
            if tid != str(value).strip():
                fixes.append(f"targets: {str(value)[:60]!r} -> {tid!r}")
            if tid not in tids:
                tids.append(tid)
            ids += [i for i in by_id[tid].items if i not in ids]
        elif str(value) not in ids:
            ids.append(str(value))
    planned.targets, planned.gap_ids = tids, ids
    return fixes


_SHAPE_WORDS = KEYWORDS | {"int", "char", "long", "short", "float", "double", "unsigned", "signed", "bool", "_Bool",
                           "void", "size_t", "const", "volatile", "struct", "union", "enum", "class", "static",
                           "inline", "extern", "typedef", "break", "continue", "default", "goto", "true", "false",
                           "NULL", "nullptr", "this", "register", "auto"}
_STD_TYPE = re.compile(r"u?int\d+_t|u?int_(?:least|fast)\d+_t|u?intptr_t|ssize_t|ptrdiff_t|wchar_t")


def function_shapes(layout: uc.LayoutProfile, sut_text: str) -> dict[str, tuple[tuple, list[str]]]:
    """A1-PL17: per function, its shape - the body with every identifier (its name, its state, its constants, the
    functions it calls) replaced by a placeholder in order of first use, and its parameter types - and the
    identifiers in that order."""
    lines = sut_text.splitlines()
    out: dict[str, tuple[tuple, list[str]]] = {}
    for f in layout.functions:
        body = strip_comments("\n".join(lines[f.line_start - 1: f.line_end]))
        names: list[str] = []
        shape: list[str] = []
        for tok in re.findall(r"[A-Za-z_]\w*|0[xX][0-9A-Fa-f]+[uUlL]*|\d+(?:\.\d+)?[uUlLfF]*|\S", body):
            if re.match(r"[A-Za-z_]", tok) and tok not in _SHAPE_WORDS and not _STD_TYPE.fullmatch(tok):
                if tok not in names:
                    names.append(tok)
                shape.append(f"@{names.index(tok)}")
            else:
                shape.append(tok)
        types = tuple(" ".join(p.type.split()) for p in f.params)
        out[f.name] = ((types, tuple(shape)), names)
    return out


def function_families(shapes: dict[str, tuple[tuple, list[str]]]) -> dict[str, tuple]:
    """A1-PL17: function -> its family's shape, for functions that share their shape with another function."""
    members: dict[tuple, list[str]] = {}
    for name, (key, _) in shapes.items():
        members.setdefault(key, []).append(name)
    return {name: key for key, names in members.items() if len(names) > 1 for name in names}


def carry_text(text: str, mapping: dict[str, str]) -> str:
    """A1-PL17: a scenario's text with one member's names replaced by another's - also inside the pipeline's helper
    names (ut_state_set_<name>, ut_stub_<name>_return)."""
    def one(m: re.Match) -> str:
        word = m.group(0)
        if word in mapping:
            return mapping[word]
        for prefix in ("ut_state_set_", "ut_state_get_", "ut_stub_"):
            if word.startswith(prefix):
                rest = word[len(prefix):]
                for suffix in ("_returns", "_return", "_step", "_calls", ""):
                    if rest.endswith(suffix) and rest[:len(rest) - len(suffix)] in mapping:
                        return prefix + mapping[rest[:len(rest) - len(suffix)]] + suffix
        return word
    return re.sub(r"[A-Za-z_]\w*", one, text)


# ARCH-20c: short, closed answer fields - the brain's schema carries the limits the checks enforce anyway, so a
# constrained decoder stops a runaway answer ("0uLL, 0uLL, ...") where it starts; the checks stay the judge.
VALUE = {"maxLength": 120}              # one C expression
ITEM = {"maxLength": 60}                # one element, field value or return value


def _most(n: int) -> dict[str, int]:
    return {"maxItems": n}


class PlannedValue(BaseModel):
    name: str = Field(description="parameter or variable name")
    value: str = Field(description="a C/C++ value or expression", json_schema_extra=VALUE)


class PlannedField(BaseModel):
    field: str = Field(description="a field name from the lists, for example status or pos.x")
    value: str = Field(description="its value: a literal, an enumerator or a macro", json_schema_extra=ITEM)


class PlannedArgValue(PlannedValue):
    """Structured scenario: a parameter's value - or, for a buffer, an array, a pointer or a structure, only what it
    contains; the generator declares the object, fills it and passes it in the right form."""
    value: str = Field(default="", description="the value of a plain parameter; empty for a buffer or a structure",
                       json_schema_extra=VALUE)
    elements: list[Annotated[str, Field(json_schema_extra=ITEM)]] = Field(default_factory=list, description=(
        "a buffer, array or pointer parameter: its element values in order (the test declares the buffer and passes "
        "it); empty otherwise"), json_schema_extra=_most(64))
    fields: list[PlannedField] = Field(default_factory=list, description=(
        "a structure parameter or a pointer to one: its field values (the test declares the structure and passes "
        "it); empty otherwise"), json_schema_extra=_most(32))


class PlannedState(BaseModel):
    """Structured scenario: SUT state set before the call - a variable, or one element or field of it."""
    variable: str = Field(description="a variable from SUT STATE")
    index: int = Field(default=-1, description="an array variable: the element index; -1 otherwise")
    field: str = Field(default="", description="a structure variable: the field from SUT STATE; empty otherwise")
    value: str = Field(description="the value to set", json_schema_extra=VALUE)


class PlannedCall(BaseModel):
    """Structured scenario: a call made before the call under test, to build up state."""
    function: str = Field(description="a function from CALLABLE FUNCTIONS")
    args: list[PlannedArgValue] = Field(default_factory=list, json_schema_extra=_most(8), description=(
        "one entry per parameter, by parameter name, in the form of the scenario's args"))

    @field_validator("args", mode="before")
    @classmethod
    def _plain_values(cls, value: Any) -> Any:
        """Argument values given in the plain form (a list of values, in order) are read as unnamed entries."""
        if isinstance(value, list):
            return [{"name": "", "value": str(v)} if isinstance(v, (str, int, float)) else
                    v.model_dump() if isinstance(v, BaseModel) else v for v in value]
        return value


class PlannedWrite(BaseModel):
    """Structured scenario: what a stub writes into a pointer or reference parameter when it is called."""
    stub: str = Field(description="a stub from STUB OUTPUTS")
    param: str = Field(description="its output parameter from STUB OUTPUTS")
    elements: list[Annotated[str, Field(json_schema_extra=ITEM)]] = Field(
        default_factory=list, description="element values written, in order (a buffer)", json_schema_extra=_most(64))
    fields: list[PlannedField] = Field(default_factory=list, description="field values written (a structure)",
                                       json_schema_extra=_most(32))


class PlannedRegister(BaseModel):
    address: str = Field(description="a register address from REGISTERS, for example 0x40021000")
    value: str = Field(description="the 32-bit value it holds", json_schema_extra=ITEM)


class PlannedMember(BaseModel):
    """Structured scenario (C++): a data member of the object set before the call, private ones included."""
    member: str = Field(description="a member from OBJECT MEMBERS")
    value: str = Field(default="", description="its value; empty for a container", json_schema_extra=VALUE)
    elements: list[Annotated[str, Field(json_schema_extra=ITEM)]] = Field(
        default_factory=list, description="a container member: its elements in order", json_schema_extra=_most(64))


class PlannedStub(BaseModel):
    stub: str = Field(description="function name of a stub listed under STUBS")
    returns: list[Annotated[str, Field(json_schema_extra=ITEM)]] = Field(
        description="return value per call, in call order; the last one stays", json_schema_extra=_most(16))


class PlannedCheck(BaseModel):
    expr: str = Field(description="checked after the call: an output variable from setup or ut_stub_<name>_calls",
                      json_schema_extra=VALUE)
    value: str = Field(json_schema_extra=ITEM)


class PlannedQuote(BaseModel):
    evidence_id: str
    text: str = Field(description="exact words copied from that evidence item", json_schema_extra={"maxLength": 300})

    @model_validator(mode="before")
    @classmethod
    def _from_text(cls, value: Any) -> Any:
        """ARCH-20b, A1-PL3: a quote written as one text ("EV-f-1: the words") is its evidence ID and its words."""
        if isinstance(value, str):
            m = re.match(r'\s*\[?(EV-\S+?|E\d)\]?(?:\s*[:=\u2013-]\s+|\s+)["\']?(.*?)["\']?\s*$', value, re.S)
            return {"evidence_id": m.group(1), "text": m.group(2)} if m and m.group(2) else \
                {"evidence_id": "", "text": value}
        return value


class PlannedScenario(BaseModel):
    """What the brain returns per scenario [A1-PL3]; Agent1 checks it [A1-PL4]. The brain names TARGETS (A1-PL3a,
    ARCH-20d); Agent1 expands them to the open items (gap_ids) before its checks."""
    targets: list[str] = Field(default_factory=list, description="ids from TARGETS this scenario reaches, one to three")
    gap_ids: list[str] = Field(default_factory=list, description="ids from OPEN GAPS that this scenario closes")
    name: str = Field(description="test name test_<what>_<behaviour>", json_schema_extra={"maxLength": 63})
    purpose: str = Field(description="one line: what the test checks", json_schema_extra={"maxLength": 160})
    args: list[PlannedArgValue] = Field(description="one entry per parameter, by parameter name")
    # the brain says what, the generator writes how (structured scenario): free set-up code is no longer asked for;
    # it is kept for answers in the older form (cached answers, carried scenarios)
    setup: SkipJsonSchema[list[str]] = Field(default_factory=list)
    state: list[PlannedState] = Field(default_factory=list, description="SUT state to set before the call")
    calls: list[PlannedCall] = Field(default_factory=list, description=(
        "earlier calls that build up state, in order; only when a target needs a call sequence"))
    writes: list[PlannedWrite] = Field(default_factory=list, description="what stubs write into output parameters")
    registers: list[PlannedRegister] = Field(default_factory=list, description="register values before the call")
    members: list[PlannedMember] = Field(default_factory=list, description="C++: object members before the call")
    stubs: list[PlannedStub] = Field(default_factory=list, description="return values of the stubs it calls")
    variables: list[PlannedValue] = Field(description="stub variables (STUB VARIABLES) to set before the call")
    expected_return: str = Field(description="the return value the EVIDENCE states; empty when it states none",
                                 json_schema_extra=ITEM)
    expected_exception: SkipJsonSchema[str] = Field(default="")       # offered only when the function throws
    expected_after: list[PlannedCheck] = Field(json_schema_extra=_most(8))
    evidence_ids: list[str]
    quotes: list[PlannedQuote]
    classification: Literal["confirmed_gap", "characterization_gap", "conflicting_evidence", "insufficient_evidence"]
    conflict: str = Field(description="conflicting_evidence only, else empty: which evidence ids contradict each "
                          "other, how", json_schema_extra={"maxLength": 300})                      # ARCH-20c
    information_sought: str = Field(description="insufficient_evidence only, else empty: what to look up, a few words",
                                    json_schema_extra={"maxLength": 160})
    calls_before: SkipJsonSchema[list[str]] = Field(default_factory=list)      # the older form of calls
    object_args: SkipJsonSchema[list[str]] = Field(default_factory=list)       # from calls: the object's constructor

    @field_validator("args", mode="before")
    @classmethod
    def _plain_args(cls, value: Any) -> Any:
        """Arguments given in the plain form (name and value only) are read in the structured form."""
        if isinstance(value, list):
            return [v.model_dump() if isinstance(v, BaseModel) else v for v in value]
        return value


class PlannedScenarios(BaseModel):
    scenarios: list[PlannedScenario]


SCALAR_VALUE = {"maxLength": 32}       # a number, enumerator, macro or short expression: never a list or a sentence


def value_schema(p: uc.ParamInfo) -> dict[str, Any]:
    """The schema of the value of one target, from its type (a fact): an enumeration or a boolean takes one of its
    names, a number at most SCALAR_VALUE characters; a structure, buffer, pointer or injected interface takes no
    value - its contents go into fields or elements, the generator declares it (NULL when it may be NULL). The
    check after the answer stays the judge; the schema only keeps a model from writing what can never pass."""
    if p.kind == "enum" and p.enum_values:                    # every qualification: Run, Mode::Run, app::Mode::Run
        return {"enum": sorted({"::".join(v.split("::")[k:]) for v in p.enum_values for k in range(v.count("::") + 1)})}
    if p.kind == "bool":
        return {"enum": ["true", "false", "0", "1"]}
    if p.kind in ("int", "float") or (p.kind == "ptr_scalar" and p.by_ref):
        return dict(SCALAR_VALUE)
    if p.kind in ("struct", "ptr_struct", "buffer", "ptr_scalar", "ptr_void", "interface"):
        return {"enum": ["", "NULL", "nullptr"] if p.nullable and not p.by_ref else [""]}
    return dict(VALUE)


def _listed(values: Any) -> Any:
    """ARCH-20a: a text field whose values are known: the brain's schema lists them; the agent's check stays the
    judge (the value is still a str when it is parsed). An empty list adds nothing."""
    values = sorted({str(v) for v in values if str(v).strip()})
    return Annotated[str, Field(json_schema_extra={"enum": values})] if values else str


def planned_model(open_ids: Any, evidence_ids: Any, params: Any, stubs: Any, variables: Any,
                  target_ids: Any = None, per_scenario: int = 3, state: Any = (), fields: Any = (),
                  callables: Any = (), outputs: Any = (), registers: Any = (), members: Any = (),
                  exceptions: Any = (), member_kinds: dict[str, uc.ParamInfo] | None = None,
                  call_params: dict[str, list[uc.ParamInfo]] | None = None, objects: Any = (),
                  stub_returns: dict[str, uc.ParamInfo] | None = None,
                  returns: uc.ParamInfo | None = None) -> type[BaseModel]:
    """ARCH-20a, A1-PL3: the answer of one planning call with the lists of this function - its targets (A1-PL3a; its
    open items when it has none), its evidence, its parameters, the stubs whose return value a test can set and the
    stub variables. With targets the brain never writes the items out (ARCH-20d)."""
    field = create_model("PlannedFieldChoice", __base__=PlannedField,
                         field=(_listed(fields), Field(description="a field from the lists")))
    params = list(params)
    names = [p.name if isinstance(p, uc.ParamInfo) else p for p in params]

    objects = set(objects or ())

    def arg_of(k: int, p: uc.ParamInfo | None, ps: list[str] | None = None, prefix: str = "PlannedArg") \
            -> type[BaseModel]:
        """One parameter's entry: its name, and only the slot its type takes (a fact) - a value for a scalar, elements
        for a buffer or pointer, fields for a structure, nothing for an object the generator builds (a class of the
        SUT); None: any parameter name, every slot. ``ps``, ``prefix``: the parameters and the model name of a
        call made before - its arguments take the same form."""
        if p is None:
            return create_model(prefix, __base__=PlannedArgValue,
                                name=(_listed(ps if ps is not None else names),
                                      Field(description="a parameter name from PARAMETERS")),
                                fields=(list[field], Field(default_factory=list, json_schema_extra=_most(32),
                                                           description="a structure parameter: its fields")))
        record = p.kind in ("struct", "ptr_struct") or (p.by_ref and bool(p.fields))
        listed = p.kind in ("buffer", "ptr_scalar", "ptr_void", "cstring") and not p.by_ref
        built = p.kind == "object" and p.name in objects
        hidden = SkipJsonSchema
        return create_model(
            f"{prefix}{k}", __base__=PlannedArgValue,
            name=(_listed([p.name]), Field(description="a parameter name from PARAMETERS")),
            value=(Annotated[str, Field(json_schema_extra={"enum": [""]} if built else value_schema(p))],
                   Field(default="", description="its value" + (
                       ": empty - the generator builds the object with its class's constructor" if built else
                       ": empty - give its fields or elements" if record or (listed and p.kind != "cstring") else ""))),
            elements=((list[Annotated[str, Field(json_schema_extra=ITEM)]], Field(
                default_factory=list, json_schema_extra=_most(64),
                description="the values it points to, in order (the test declares the buffer)"))
                if listed else (hidden[list[str]], Field(default_factory=list))),
            fields=((list[field], Field(default_factory=list, description="its fields", json_schema_extra=_most(32)))
                    if record else (hidden[list[PlannedField]], Field(default_factory=list))))
    typed_params = [p for p in params if isinstance(p, uc.ParamInfo)]
    arg_models = [arg_of(k, p) for k, p in enumerate(typed_params)] or [arg_of(0, None)]
    arg = arg_models[0] if len(arg_models) == 1 else Union[tuple(arg_models)]
    call_params = call_params or {}

    def call_of(k: int, name: str) -> type[BaseModel]:
        """One function a scenario may call before: one argument entry per parameter, in the form of the scenario's
        args - its name and only the slot its type takes."""
        ps = call_params[name]
        made = "::" in name and name.split("::")[-1] == name.split("::")[-2]   # a constructor: a value not given is {}
        models = [arg_of(j, q, prefix=f"PlannedCall{k}Arg") for j, q in enumerate(ps)] or \
            [arg_of(0, None, [], prefix=f"PlannedCall{k}Arg")]
        item = models[0] if len(models) == 1 else Union[tuple(models)]
        return create_model(f"PlannedCall{k}", __base__=PlannedCall,
                            function=(_listed([name]), Field(description="a function from CALLABLE FUNCTIONS")),
                            args=(list[item], Field(
                                default_factory=list, description="one entry per parameter, by parameter name",
                                json_schema_extra={"minItems": 0 if made else len(ps), "maxItems": len(ps)})))
    call_ms = [call_of(k, c) for k, c in enumerate(sorted(callables)) if c in call_params]
    state_m = create_model("PlannedStateChoice", __base__=PlannedState,
                           variable=(_listed(state), Field(description="a variable from SUT STATE")))
    call_m = call_ms[0] if len(call_ms) == 1 else Union[tuple(call_ms)] if call_ms and len(call_ms) == len(
        set(callables)) else create_model("PlannedCallChoice", __base__=PlannedCall,
                                          function=(_listed(callables), Field(description="a function from CALLABLE "
                                                                                          "FUNCTIONS")))
    write_m = create_model("PlannedWriteChoice", __base__=PlannedWrite,
                           stub=(_listed({o.split(".")[0] for o in outputs}), Field(description="a stub of STUB OUTPUTS")),
                           param=(_listed({o.split(".", 1)[1] for o in outputs if "." in o}),
                                  Field(description="its output parameter")),
                           fields=(list[field], Field(default_factory=list, json_schema_extra=_most(32))))
    reg_m = create_model("PlannedRegisterChoice", __base__=PlannedRegister,
                         address=(_listed(registers), Field(description="a register from REGISTERS")))
    member_kinds = member_kinds or {}
    member_ms = [create_model(f"PlannedMember{k}", __base__=PlannedMember,
                              member=(_listed([m]), Field(description="a member from OBJECT MEMBERS")),
                              value=(Annotated[str, Field(json_schema_extra=value_schema(member_kinds[m]))],
                                     Field(default="", description="its value; empty for a container")))
                 for k, m in enumerate(members) if m in member_kinds]
    member_m = member_ms[0] if len(member_ms) == 1 else Union[tuple(member_ms)] if member_ms else create_model(
        "PlannedMemberChoice", __base__=PlannedMember,
        member=(_listed(members), Field(description="a member from OBJECT MEMBERS")))
    hidden = SkipJsonSchema                         # a structured field with nothing to offer stays out of the schema
    structured = dict(
        state=((list[state_m], Field(default_factory=list, description="SUT state to set before the call",
                                     json_schema_extra=_most(32)))
               if state else (hidden[list[PlannedState]], Field(default_factory=list))),
        calls=((list[call_m], Field(default_factory=list, description="earlier calls that build up state, in order",
                                    json_schema_extra=_most(12)))                    # the check refuses more than 12
               if callables else (hidden[list[PlannedCall]], Field(default_factory=list))),
        writes=((list[write_m], Field(default_factory=list, description="what stubs write into output parameters",
                                      json_schema_extra=_most(8)))
                if outputs else (hidden[list[PlannedWrite]], Field(default_factory=list))),
        registers=((list[reg_m], Field(default_factory=list, description="register values before the call",
                                       json_schema_extra=_most(16)))
                   if registers else (hidden[list[PlannedRegister]], Field(default_factory=list))),
        members=((list[member_m], Field(default_factory=list, description="object members before the call",
                                        json_schema_extra=_most(16)))
                 if members else (hidden[list[PlannedMember]], Field(default_factory=list))),
        **({"expected_return": (Annotated[str, Field(json_schema_extra={"enum": ["", *value_schema(returns)["enum"]]})],
                                Field(default="", description="the return value the EVIDENCE states; empty when it "
                                                              "states none"))}     # an enumeration or a boolean
           if returns is not None and returns.kind in ("enum", "bool") and "enum" in value_schema(returns) else {}),
        expected_exception=((_listed(["", *exceptions]), Field(default="", description=(
            "C++: the exception type from EXCEPTIONS the EVIDENCE says the call throws; empty when none")))
            if exceptions else (hidden[str], Field(default=""))))
    stubs, stub_returns = sorted(set(stubs)), stub_returns or {}
    stub_ms = [create_model(f"PlannedStub{k}", __base__=PlannedStub,          # its return values typed by its type
                            stub=(_listed([n]), Field(description="a stub listed under STUBS")),
                            returns=(list[Annotated[str, Field(json_schema_extra=value_schema(stub_returns[n]))]], Field(
                                description="return value per call, in call order; the last one stays",
                                json_schema_extra=_most(16))))
               for k, n in enumerate(stubs) if n in stub_returns]
    stub = stub_ms[0] if len(stub_ms) == 1 else Union[tuple(stub_ms)] if stub_ms and len(stub_ms) == len(stubs) \
        else create_model("PlannedStubChoice", __base__=PlannedStub,
                          stub=(_listed(stubs), Field(description="function name of a stub listed under STUBS")))
    var = create_model("PlannedVariable", __base__=PlannedValue,
                       name=(_listed(variables), Field(description="a stub variable from STUB VARIABLES")))
    quote = create_model("PlannedQuoteChoice", __base__=PlannedQuote, evidence_id=(_listed(evidence_ids), ...))
    if target_ids:
        listed = dict(targets=(list[_listed(target_ids)], Field(
                          default_factory=list, description="ids from TARGETS this scenario reaches, one to three",
                          json_schema_extra={"maxItems": max(1, per_scenario)})),                    # ARCH-20c
                      gap_ids=(SkipJsonSchema[list[str]], Field(default_factory=list)))
    else:
        listed = dict(targets=(SkipJsonSchema[list[str]], Field(default_factory=list)),
                      gap_ids=(list[_listed(open_ids)], Field(description="ids from OPEN GAPS that this scenario closes")))
    scenario = create_model(
        "PlannedScenarioChoice", __base__=PlannedScenario, **listed, **structured,
        args=(list[arg], Field(description="one entry per parameter, by parameter name",   # a fact: its parameters
                               json_schema_extra={"minItems": len(names), "maxItems": len(names)})),
        stubs=((list[stub], Field(default_factory=list, json_schema_extra=_most(len(stubs))))
               if stubs else (SkipJsonSchema[list[PlannedStub]], Field(default_factory=list))),   # it calls none
        variables=(list[var], Field(description="stub variables to set before the call",
                                    json_schema_extra=_most(max(1, len(list(variables)))))),
        evidence_ids=(list[_listed(evidence_ids)], Field(json_schema_extra=_most(4))),
        quotes=(list[quote], Field(json_schema_extra=_most(4))))
    return create_model("PlannedScenariosChoice", __base__=PlannedScenarios, scenarios=(list[scenario], ...))


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
    "Plan unit-test scenarios for the function {fn} that reach the TARGETS (and, for EVIDENCE:<function>, check "
    "the behaviour the EVIDENCE states). Return JSON {{\"scenarios\": [...]}} with at most {limit} scenarios.\n"
    "Rules:\n"
    "- You say WHAT the test sets; the generator writes the code: never a declaration, an address (&) or a cast.\n"
    "- args: one entry per parameter, by its name, with its value (a literal, an enumerator or a macro of the "
    "facts; NULL only where PARAMETERS says it may be NULL).\n"
    "{forms}"
    "- stubs: return values of the STUBS, one per call in call order; variables: STUB VARIABLES to set.\n"
    "- targets: the ids of the TARGETS a scenario reaches (one to three, for example [\"T2\"]); never write out "
    "line numbers or item ids. Every TARGET is to be reached by a test: choose inputs, state and stub values that "
    "reach it.\n"
    "- Use the PATH CONDITIONS to choose values that reach each target. For a target marked TRIED, choose "
    "different inputs, state or stub values than the ones tried.\n"
    "- classification: confirmed_gap only when an EVIDENCE item (not E8) states the expected result; then give "
    "expected_return / expected_after, its evidence_ids (the ids as written in EVIDENCE, in the form "
    "EV-<function>-<n>; never the classes E1-E8) and quotes copied word for word from it. "
    "characterization_gap when no evidence states the result: leave expected_return empty, the result is observed. "
    "conflicting_evidence when evidence items contradict each other (say which in conflict). "
    "insufficient_evidence when you need more evidence (say what in information_sought).\n"
    "- Keep every text short: purpose one line; information_sought and conflict stay \"\" unless the classification "
    "is insufficient_evidence or conflicting_evidence. Write no explanation outside the fields.\n"
    "- Never guess an expected value.\n"
    "Example of one scenario in this form (for a function int f(int a, int b) with a target T1):\n"
    '{{"targets": ["T1"], "name": "test_f_negative_b", "purpose": "b below 0 returns the error", '
    '"args": [{{"name": "a", "value": "1"}}, {{"name": "b", "value": "-1"}}], "state": [], "stubs": [], '
    '"variables": [], "expected_return": "", "expected_after": [], "evidence_ids": [], "quotes": [], '
    '"classification": "characterization_gap", "conflict": "", "information_sought": ""}}\n'
    "A confirmed_gap carries the evidence it rests on, for example: \"classification\": \"confirmed_gap\", "
    '"expected_return": "-1", "evidence_ids": ["EV-f-1"], "quotes": [{{"evidence_id": "EV-f-1", "text": "returns -1 '
    'when b is negative"}}] - every quote an object with the evidence id and the words copied from it.\n'
    "Values are C code exactly as in the example (\"1\", \"-1\", a macro or enumerator name): never a ':' or '=' "
    "before a value, never the parameter name, a file path or a placeholder.")
CONST_DEF = re.compile(r"\b(?:constexpr|const)\s+((?:(?:static|inline|unsigned|signed|long|short)\s+)*[A-Za-z_][\w:]*)"
                       r"\s+([A-Za-z_]\w*)\s*(?:=\s*([^;{}=]+?)|\{\s*([^;{}]+?)\s*\})\s*;")


def integral_constants(text: str) -> list[tuple[str, str]]:
    """(name, expression) of the integer constants a file defines with const or constexpr - facts of the source,
    like its integer macros (A1-PL2a): a test outside the file writes the value."""
    out = []
    for m in CONST_DEF.finditer(text):
        ctype = re.sub(r"\b(static|inline)\b", "", m.group(1)).strip()
        if INTEGRAL_T.match(re.sub(r"\bstd::", "", ctype)) or ctype in ("bool",):
            out.append((m.group(2), (m.group(3) or m.group(4) or "").strip()))
    return out


def target_param(name: str, ctype: str, layout: uc.LayoutProfile) -> uc.ParamInfo:
    """The kind of value a target of this C/C++ type takes (a member, a state variable, a field, an element), as a
    parameter of that type: through the repository's typedefs to a builtin, an enumeration or "other"."""
    t = _plain_type(pointer_core(ctype or ""))
    seen: set[str] = set()
    while t and t not in seen:
        seen.add(t)
        bare = re.sub(r"\bstd::", "", t)
        if bare in ("bool", "_Bool"):
            return uc.ParamInfo(name=name, type=bare, kind="bool")
        enum = next((e for e in layout.enums if e.name == t or e.name.split("::")[-1] == t.split("::")[-1]), None)
        if enum is not None:
            return uc.ParamInfo(name=name, type=enum.name, kind="enum", base=enum.name, enum_values=list(enum.values))
        if INTEGRAL_T.match(bare):
            return uc.ParamInfo(name=name, type=bare, kind="int")
        if FLOAT_T.match(bare):
            return uc.ParamInfo(name=name, type=bare, kind="float")
        nxt = layout.typedefs.get(t) or layout.typedefs.get(bare)
        if not nxt:
            break
        t = _plain_type(pointer_core(nxt))
    return uc.ParamInfo(name=name, type=ctype or "", kind="other")


def array_element(ctype: str) -> str:
    """The element type of an array type (uint8_t[4] -> uint8_t, std::array<int, 3> -> int); else the type."""
    m = re.match(r"^\s*(?:const\s+)?std::(?:array|vector)\s*<\s*([^,>]+)", ctype or "")
    return m.group(1).strip() if m else re.sub(r"\s*\[[^\]]*\]\s*$", "", ctype or "").strip()


def as_list(value: str) -> list[str]:
    """A list a brain wrote into one value ("[0, 0, 1]" or "{0, 0, 1}") as its items; a single value as one item."""
    v = fix_value(value)
    if len(v) >= 2 and v[0] + v[-1] in ("[]", "{}"):
        return [x.strip() for x in uc.top_level_commas(v[1:-1]) if x.strip()]
    return [v] if v else []


FIELD_PART = re.compile(r"([A-Za-z_]\w*)\s*((?:\[\s*\d+\s*\])*)")


def field_at(fields: list[uc.FieldInfo], path: str) -> uc.FieldInfo | None:
    """The field a path (pos.x, samples[1], pts[0].x) names in a structure's fields, or None. An element of an array
    field (an index within its length) is named with the element's type."""
    parts = path.split(".") if path else []
    for k, part in enumerate(parts):
        m = FIELD_PART.fullmatch(part.strip())
        found = next((f for f in fields if f.name == m.group(1)), None) if m else None
        if found is None:
            return None
        if m.group(2):
            index = int(re.search(r"\d+", m.group(2)).group(0))
            if found.kind != "array" or (found.array_len and index >= found.array_len):
                return None
            found = found.model_copy(update={"type": array_element(found.type), "kind": "other", "array_len": 0})
        if k == len(parts) - 1:
            return found
        fields = found.fields
    return None


def record_fields(type_name: str, layout: uc.LayoutProfile) -> list[uc.FieldInfo]:
    """The fields of a structure type, from a parameter or state variable of that type (the front end's)."""
    short = re.sub(r"\b(const|volatile|struct|union|class)\b", "", type_name).strip().split("::")[-1]
    for f in layout.functions:
        for p in f.params:
            if p.fields and re.sub(r"\b(const|volatile|struct|union|class)\b|[*&]", "", p.base or p.type).strip() \
                    .split("::")[-1] == short:
                return list(p.fields)
    return next((list(v.fields) for v in layout.sut_state if v.fields and re.sub(
        r"\b(const|volatile|struct|union|class)\b|\[.*\]", "", v.type).strip().split("::")[-1] == short), [])


def thrown_types(code: str) -> list[str]:
    """The exception types a throw expression in this code names (throw std::overflow_error("...")): a fact of the
    source, the only types an expected exception may name."""
    masked = uc.c_masked(code)
    return sorted(set(re.findall(r"\bthrow\s+((?:::)?[A-Za-z_][\w:]*)\s*[({]", masked)))


def plan_forms(fn: uc.FunctionInfo, lists: dict[str, Any]) -> str:
    """The rules of the structured fields this function's answer has - only those (a small window keeps the
    facts): buffers and structures among its parameters, its SUT state, earlier calls, stub outputs, registers,
    object members."""
    kinds = {p.kind for p in text_params(fn)} | {"struct" for p in text_params(fn) if p.by_ref and p.fields}
    rows = []
    if kinds & {"buffer", "ptr_scalar", "ptr_void", "cstring"}:
        rows.append('- A buffer, array or pointer parameter: elements, the values it points to, for example '
                    '{"name": "data", "elements": ["0x01", "0x02"]}.\n')
    if kinds & {"struct", "ptr_struct"}:
        rows.append('- A structure parameter or a pointer to one: fields, for example {"name": "cfg", "fields": '
                    '[{"field": "limit", "value": "5"}]}.\n')
    if lists.get("callables") and not lists.get("state"):
        rows.append("- calls: earlier calls of CALLABLE FUNCTIONS, only when a target needs state that calls build "
                    "up; their args take the form of args, one entry per parameter by name.\n")
    if lists.get("state"):
        rows.append('- state: SUT STATE to set before the call - the variable, for an array its index (else -1), for '
                    'a structure its field (else ""), and the value, for example {"variable": "tbl", "index": 1, '
                    '"field": "count", "value": "3"}.\n')
        if lists.get("callables"):
            rows.append("- calls: earlier calls of CALLABLE FUNCTIONS, only when a target needs state that calls "
                        "build up; their args take the form of args, one entry per parameter by name.\n")
    if lists.get("outputs"):
        rows.append("- writes: what a stub of STUB OUTPUTS writes into its output parameter (elements or fields).\n")
    if lists.get("registers"):
        rows.append("- registers: the value a register of REGISTERS holds before the call.\n")
    if lists.get("members"):
        rows.append("- members: OBJECT MEMBERS to set before the call (elements for a container).\n")
    if lists.get("exceptions"):
        rows.append("- expected_exception: when the EVIDENCE says the call throws, its type from EXCEPTIONS (then "
                    "expected_return stays empty).\n")
    return "".join(rows).replace("{", "{{").replace("}", "}}")


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
                   "reinterpret_cast", "const_cast",
                   "UT_REG32", "UT_REG_SET_LATER", "UT_REG_CLEAR_LATER"}      # harness macros [A3-HW1, A2-21ab, A1-PL4 d]
# A declaration: type words, then a separator (blank, *, &), then the name - the separator keeps a name such as
# "list" in "List_t list;" from being split into a second type word "lis" and a name "t".
LOCAL_DECL = re.compile(r"^\s*(?:(?:const|static|struct|enum|union|unsigned|signed|volatile)\s+)*[A-Za-z_][\w:<>]*"
                        r"(?:\s+[A-Za-z_]\w*)?(?:\s*[*&]+\s*|\s+)([A-Za-z_]\w*)\s*(?:\[[^\]]*\])*\s*(?:=|;|\{|\()")


def block_end(lines: list[str], line: int) -> int:
    """The last line of the block a decision at ``line`` (1-based) opens; the line itself when it opens none."""
    depth, opened = 0, False
    for n in range(line - 1, min(len(lines), line + 400)):
        code = re.sub(r"/\*.*?\*/|//.*$|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", "", lines[n])
        for ch in code:
            if ch == "{":
                depth, opened = depth + 1, True
            elif ch == "}":
                depth -= 1
                if opened and depth <= 0:
                    return n + 1
        if not opened and n > line and code.strip().endswith(";"):
            return n + 1                                     # a single statement without braces
    return line


def norm_text(text: str) -> str:
    return uc.norm_quote(text)                             # white space, comment markers and @tags [A1-PL4 b]


# ARCH-20, A1-PL4: form is forgiven, content is not. Harmless format noise of a brain answer is removed before the
# unchanged strict checks; every fix is recorded with the value before and after.
VALUE_PREFIX = re.compile(r"^\s*(?:=|:(?!:))\s*")           # a stray ':' or '=' (never the '::' of a C++ name)


def fix_value(value: str, name: str = "", keep_quotes: bool = False) -> str:
    """A value as a brain may write it, without format noise: comments, a copied 'name:' or 'name =', a stray ':'
    or '=', a trailing ';' or ',', backticks, or double quotes around a number or name (unless a string is meant)."""
    v = str(value).strip()
    v = re.sub(r"/\*.*?\*/", " ", v)
    v = re.sub(r"//.*$", "", v).strip()
    if name:
        v = re.sub(r"^\s*" + re.escape(name) + r"\s*(?:=|:(?!:))\s*", "", v)
    v = VALUE_PREFIX.sub("", v)
    v = uc.without_json_tail(v.rstrip(";,").strip())          # "0x42'}], ": the rest of the JSON is not the value
    if len(v) >= 2 and v[0] == v[-1] == "`":
        v = v[1:-1].strip()
    if not keep_quotes and len(v) >= 2 and v[0] == v[-1] == '"' and re.fullmatch(r"[-+!~]?[\w.:]+", v[1:-1].strip()):
        v = v[1:-1].strip()
    return v


def fix_gap_id(gap_id: str, open_ids: set[str], function: str, evidence_ids: set[str]) -> str:
    """A gap id as a brain may write it: a copied OPEN GAPS line is read as the id it starts with; an evidence id of
    the function is read as EVIDENCE:<function> when that is the function's only open item."""
    g = str(gap_id).strip().strip("`").lstrip("- ").strip()
    if g in open_ids:
        return g
    starts = [o for o in open_ids if g.startswith(o) and (len(g) == len(o) or g[len(o)] in " :|[(-")]
    if starts:
        return max(starts, key=len)
    only = f"EVIDENCE:{function}"
    if open_ids == {only} and (g in evidence_ids or g.startswith(f"EV-{function}-") or g == function):
        return only
    return g


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
    e = e.replace("&&", " and ").replace("||", " or ").replace("!", " not ").replace(" not =", "!=").strip()
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


SAFE_MEMBER = re.compile(r"(?:bool|_Bool|char|float|double|long double|(?:(?:un)?signed\s+)?(?:char|short|int|long|long long)"
                         r"(?:\s+int)?|unsigned|(?:std::)?u?int(?:8|16|32|64)_t|(?:std::)?size_t)")


def class_members(sut: "FrontEnd", classes: list[str], layout: uc.LayoutProfile) -> list[uc.MemberInfo]:
    """The data members of the SUT's classes - private ones included - whose type a test can spell: a scalar, an
    enumeration of the SUT, or a std::array / std::vector of scalars (structured scenarios set them)."""
    enums = {e.name for e in layout.enums} | {e.name.split("::")[-1] for e in layout.enums}
    records = sut._records()
    out: list[uc.MemberInfo] = []
    for cls in classes:
        rec = records.get(cls) or records.get(cls.split("::")[-1])
        if rec is None:
            continue
        for c in rec.get("inner", []) or []:
            if c.get("kind") not in ("FieldDecl", "VarDecl") or not c.get("name") or c.get("isImplicit"):
                continue
            ty = c.get("type") or {}
            t = re.sub(r"\b(const|volatile)\b", "", ty.get("desugaredQualType") or ty.get("qualType", "")).strip()
            if (ty.get("qualType", "").startswith("const ") or "&" in t or "*" in t
                    or (c.get("kind") == "VarDecl" and c.get("storageClass") != "static")):
                continue
            plain = re.sub(r"\s+", " ", t).replace("enum ", "").replace("class ", "").strip()
            container = re.fullmatch(r"std::(?:__\w+::)?(array|vector)<\s*(.+?)\s*(?:,\s*(\d+)\s*)?>", plain)
            elem = container.group(2) if container else plain
            if not (SAFE_MEMBER.fullmatch(elem) or elem in enums):
                continue
            if container:
                plain = f"std::{container.group(1)}<{elem}" + (f", {container.group(3)}>" if container.group(3) else ">")
            out.append(uc.MemberInfo(class_name=cls, name=c["name"], type=plain,
                                     static=c.get("kind") == "VarDecl"))
    return out[:200]


def class_constructors(sut: "FrontEnd", classes: list[str]) -> list[uc.FunctionInfo]:
    """The public constructors the SUT's classes declare (inline in a header too), copy and move constructors left
    out: a test builds the object with one of them [A2-21ae]."""
    enums = {}
    for e in sut.enums():
        enums[e.name] = e
        enums.setdefault(e.name.split("::")[-1], e)
    records = sut._records()
    out: list[uc.FunctionInfo] = []
    for cls in classes:
        rec = records.get(cls) or records.get(cls.split("::")[-1])
        if rec is None:
            continue
        short = cls.split("::")[-1]
        public = rec.get("tagUsed") != "class"
        for c in rec.get("inner", []) or []:
            if c.get("kind") == "AccessSpecDecl":
                public = c.get("access") == "public"
            if c.get("kind") != "CXXConstructorDecl" or c.get("isImplicit") or c.get("explicitlyDeleted") or not public:
                continue
            params = [sut.param_info(p, enums, "") for p in c.get("inner", []) or [] if p.get("kind") == "ParmVarDecl"]
            if len(params) == 1 and re.sub(r"\b(const|volatile|class|struct)\b|[&*\s]", "", params[0].type) \
                    .split("::")[-1] == short:
                continue                                             # a copy or move constructor
            out.append(uc.FunctionInfo(name=f"{cls}::{short}", qualified_name=f"{cls}::{short}", member=True,
                                       signature=f"{short}({', '.join(p.type for p in params)})", file="",
                                       line_start=0, line_end=0, params=params, from_frontend=True))
    return out[:100]


stub_output = uc.stub_output
stub_pointee = uc.stub_pointee


def refusal_causes(reasons: list[str]) -> list[str]:
    """ORC-12b: the cause of each refusal of a planned scenario, for the refusal rate per cause."""
    out = []
    for r in reasons:
        low = r.lower()
        cause = ("vocabulary" if re.search(r"unknown names|do not exist here|not a stubbed|is not a variable|not exist",
                                           low) else
                 "pointer form" if re.search(r"pass a variable|declare a buffer|declare `|null is not allowed", low) else
                 "value range" if "outside the range" in low else
                 "enumerator" if "not an enumerator" in low else
                 "value form" if re.search(r"not an integer|not a boolean|not a floating|no value", low) else
                 "arguments" if "args must name" in low else
                 "set-up" if low.startswith("setup") or "setup " in low or "earlier call" in low else
                 "stubs" if re.search(r"return values|returns nothing|const: a test", low) else
                 "targets" if re.search(r"open gap|proven infeasible|names no", low) else
                 "way out" if "cov-5" in low else "other")
        if cause not in out:
            out.append(cause)
    return out or ["other"]


def brief(sc: uc.Scenario, by_tid: dict[str, "Target"] | None = None) -> str:
    """A1-PL18: a tried scenario in short - its targets, arguments, set-up and stub values."""
    tids = [tid for tid, tg in (by_tid or {}).items() if set(tg.items) & set(sc.gap_ids)]
    parts = [("targets " + ", ".join(tids[:3])) if tids else ("items " + ", ".join(sc.gap_ids[:3])),
             "args " + ", ".join(f"{a.name}={a.value}" for a in sc.args)]
    if sc.setup:
        parts.append("setup " + " ".join(sc.setup)[:160])
    if sc.calls_before:
        parts.append("calls before " + " ".join(sc.calls_before)[:120])
    if sc.stub_values:
        parts.append("stubs " + "; ".join(f"{v.stub} -> {', '.join(v.returns)}" for v in sc.stub_values)[:160])
    return " | ".join(parts)[:400]


def pointee(p: uc.ParamInfo) -> str:
    """The type a pointer parameter points to, without const and volatile; "" when it cannot be named."""
    base = p.base or re.sub(r"\*\s*$", "", p.type.strip())
    base = re.sub(r"\b(const|volatile|restrict|__restrict)\b", " ", base).replace("&", "")
    base = re.sub(r"\s+", " ", base).strip()
    if p.kind == "ptr_void" or base in ("void", ""):
        return "uint8_t" if p.kind in ("ptr_void", "buffer") else ""
    return base if "*" not in base and "(" not in base and "[" not in base else ""


def pointer_form(p: uc.ParamInfo) -> str:
    """A1-PL2, structured scenario: what the brain gives for a pointer, structure or reference parameter - its
    contents; the generator declares the object and passes it in the right form (never written by the brain)."""
    base = pointee(p) or "T"
    if p.kind in ("struct", "ptr_struct") or (p.by_ref and p.fields):
        return f"give its fields (a {base}); the generator declares it and passes it"
    if p.by_ref:
        return f"give its value (a {base}); the generator declares it and passes it"
    if p.kind == "cstring":
        return "give a string literal as its value, or its characters as elements"
    if p.kind == "callback":
        return "give a function that exists here (a callback helper ut_cb_...) as its value"
    return f"give its elements (the {base} values it points to, in order); the generator declares the buffer and passes it"


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
        self.last_fixes: list[str] = []                              # format fixes of the last check [ARCH-20]
        self._variant_cache: dict[str, list[tuple[uc.BuildVariant, dict[str, int]]]] = {}   # A1-37a, A1-37e
        self._sources: dict[str, dict[str, tuple[tuple[int, int], str]]] = {}                # A1-31b
        self._families: dict[str, dict[tuple, dict[str, Any]]] = {}          # SUT file -> family -> its plan [A1-PL17]
        self._tried_text: dict[str, dict[str, str]] = {}       # function -> signature -> the scenario in short [A1-PL18]
        self._asked: dict[str, tuple[str, int]] = {}           # function -> (facts of its last call, accepted) [A1-PL18]
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
            out = await self.plan(payload)
            out.kinds_available = [k for k in uc.ANSWER_KINDS if await uc.brains_for(self, k)]   # BP-3, BP-9
            return out

        def evidence_pre(payload: uc.EvidenceRequest, request):
            uc.check_signature(payload.layout, request)
            if not any(payload.function in (f.name, f.qualified_name) for f in payload.layout.functions):
                raise ba.ContractRejected(f"{payload.function} is not a function of the SUT [A1-24]")

        async def evidence(payload: uc.EvidenceRequest, request, scratch):
            return self.collect_evidence(payload.layout, payload.function)

        def select_pre(payload: uc.SelectRequest, request):
            if not Path(payload.repo_root).is_dir():
                raise ba.ContractRejected("repository root does not exist")

        async def select(payload: uc.SelectRequest, request, scratch):
            return select_production_files(Path(payload.repo_root), payload.compile_commands,
                                           self.cfg.max_selected_files)

        def status_pre(payload: uc.RequirementStatusRequest, request):
            uc.check_signature(payload.layout, request)

        async def status(payload: uc.RequirementStatusRequest, request, scratch):
            return self.requirement_status(payload)

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
                                read_only=True, description="Expected-behaviour evidence for one function [A1-25j]."),
                ba.ContractTool("select_files", uc.SelectRequest, uc.FileSelection, select, select_pre, read_only=True,
                                description="The production files of a working tree when a run names none; no "
                                            "LLM [ORC-35]."),
                ba.ContractTool("requirement_status", uc.RequirementStatusRequest, uc.RequirementStatus, status,
                                status_pre, read_only=True,
                                description="Per function: the linked requirements, whether a passed test asserts "
                                            "each, and whether any expected behaviour is stated [A1-RQ5].")]

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
        language = "cpp" if sut.suffix.lower() in CPP_EXT + (".hpp", ".hh", ".hxx", ".ipp", ".tpp") else "c"
        source = sut.read_text(encoding="utf-8", errors="replace")
        header_sut = sut.suffix.lower() in HEADER_EXT + (".inl", ".ipp", ".tpp")              # A1-DA19
        if header_sut and cpp_content(source):
            language = "cpp"                                     # a .h or .inl by its content, a .hpp by its name
        component = self._component_root(root, sut)
        files = [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts and "ut_work" not in p.parts]
        rel = lambda p: str(p.relative_to(root)).replace("\\", "/")
        headers_all = [p for p in files if p.suffix.lower() in HEADER_EXT]
        # "..." and <...> includes that resolve inside the repository (a <...> one found nowhere there is a system
        # header); of several files with that name, the one the compiler would most likely find: next to the SUT,
        # then inside its component, then the nearest to the SUT [A1-7]
        included = [inc for quote, inc in re.findall(r'#\s*include\s*([<"])([^>"]+)[>"]', source)
                    if quote == '"' or ("." in inc and inc not in uc.STANDARD_HEADERS)]
        include_dirs, headers = [], []

        def nearness(h: Path, inc: str) -> tuple[int, int, int, str]:
            beside = (sut.parent / inc).resolve() == h.resolve()
            inside = h.resolve().is_relative_to(component.resolve())
            shared = len(Path(os.path.commonpath([str(h.parent.resolve()), str(sut.parent.resolve())])).parts)
            return (0 if beside else 1, 0 if inside else 1, -shared, rel(h))
        for inc in included:
            inc = inc.replace("\\", "/")
            matches = [h for h in headers_all if rel(h) == inc or rel(h).endswith("/" + inc)]   # "ctrl.h" is not "brake_ctrl.h"
            if not matches:
                continue
            h = min(matches, key=lambda h: nearness(h, inc))
            directory = rel(h)[: -len(inc)].rstrip("/") or "."
            if directory not in include_dirs:
                include_dirs.append(directory)
            headers.append(rel(h))
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
        unit = uc.unit_name_of(sut_rel, [rel(p) for p in files if p.suffix.lower() in uc.SOURCE_SUFFIXES])  # A2-21af
        suite = f"{default_test_dir.rstrip('/')}/test_{unit}.{'cpp' if language == 'cpp' else 'c'}"
        build = self._build_system(root, component)
        requirement_files = self._requirement_files(root, files)
        given, requirement_notes = self._given_requirements(req.requirement_documents, root, requirement_files)
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
            requirement_files=requirement_files, header_sut=header_sut, project=req.project,
            given_requirements=given, requirement_notes=requirement_notes)

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

    @property
    def _columns(self) -> Columns:
        return tuple(c.lower() for c in self.cfg.requirement_id_columns), \
            tuple(c.lower() for c in self.cfg.requirement_text_columns)

    def _given_requirements(self, docs: list[uc.RequirementDocument], root: Path, repo_files: list[str]
                            ) -> tuple[list[uc.RequirementDocument], list[str]]:
        """A1-RQ2, A1-RQ4, A1-RQ6: the documents given for the run that can be read and state requirement IDs;
        the others and every ID stated differently in the repository become notes for the final result."""
        given, notes, stated = [], [], {}
        for doc in docs:
            if Path(doc.name).suffix.lower() not in REQUIREMENT_FORMATS:
                notes.append(f"{doc.name} (given as {doc.origin or doc.name}) is not read: only ReqIF (.reqif, "
                             ".reqifz), Markdown, plain text or CSV - please give an export in one of them [A1-RQ2]")
                continue
            try:
                entries = read_document(Path(doc.path), doc.name, self._columns)
            except DOCUMENT_ERRORS as exc:
                notes.append(f"{doc.name} (given as {doc.origin or doc.name}) cannot be read: {exc}"[:400]
                             + "; the run continues without it [A1-RQ6]")
                continue
            if not entries:
                notes.append(f"{doc.name} (given as {doc.origin or doc.name}) contains no requirement ID; the run "
                             "continues without it - IDs are never invented [A1-RQ6]")
                continue
            given.append(doc)
            for rid, text, source, line in entries:
                stated.setdefault(rid, (text, source, line))
        for rel in repo_files:
            try:
                entries = read_document(root / rel, rel, self._columns)
            except DOCUMENT_ERRORS:
                continue
            for rid, text, source, line in entries:
                if rid in stated and " ".join(text.split()) != " ".join(stated[rid][0].split()):
                    notes.append(f"conflict: {rid} is stated differently in {stated[rid][1]}:{stated[rid][2]} (given "
                                 f"for the run, used) and {source}:{line} [A1-RQ4, A1-25e]")
        return given, notes

    def _requirements(self, layout: uc.LayoutProfile) -> list[tuple[str, str, str, int]]:
        return read_requirements(Path(layout.repo_root), layout.requirement_files, layout.given_requirements,
                                 self._columns)

    @staticmethod
    def _component_root(root: Path, sut: Path) -> Path:
        """A1-3: the nearest folder with a build file of any common build system; without one, the folder above a
        source folder (src, source, lib ...) or else the SUT's own folder."""
        markers = ("CMakeLists.txt", "Makefile", "makefile", "GNUmakefile", "meson.build", "SConstruct", "SConscript",
                   "BUILD", "BUILD.bazel", "configure.ac", "library.json", "platformio.ini", "Kbuild", "build.ninja",
                   "premake5.lua", "xmake.lua", "conanfile.py")
        patterns = ("*.vcxproj", "*.ewp", "*.uvprojx", ".cproject", "*.mk")
        for parent in sut.parents:
            if any((parent / marker).exists() for marker in markers) or \
                    any(next(parent.glob(pattern), None) is not None for pattern in patterns):
                return parent
            if parent == root:
                break
        if sut.parent == root:
            return root
        if sut.parent.name.lower() in ("src", "source", "sources", "lib", "libs", "core", "impl", "private", "c", "cpp",
                                       "include", "inc", "includes", "headers", "public", "api"):
            return sut.parent.parent
        return sut.parent

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
        for rid, text, source, line in self._requirements(layout):
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
        call = re.compile(r"\b" + re.escape(short) + r"\s*\(")
        skip = {layout.sut_file, *layout.test_files}
        for rel, text in self._source_texts(root):                       # read once per run [A1-31b]
            if callers >= self.cfg.max_callers:
                break
            if rel in skip or short not in text:
                continue
            m = call.search(text)
            if m:
                start = text.rfind("\n", 0, m.start()) + 1
                end = text.find("\n", m.end())
                add("E6", rel, line_of(text, start), text[start: end if end >= 0 else len(text)])
                callers += 1
        for link in layout.requirement_links:                      # last, so the other ids stay stable [A1-PL13]
            if link.function == fn.name:
                add("E1", link.source, link.line, f"{link.requirement_id}: {link.quote}",
                    f"brain p={link.probability:.2f}")
        return uc.EvidenceResult(function=fn.name, evidence=refs, exhausted=True)


    def requirement_status(self, req: uc.RequirementStatusRequest) -> uc.RequirementStatus:
        """A1-RQ5: a requirement counts as asserted when a test that passed cites its evidence item and asserts
        something, and is not a characterization test (E8)."""
        blocks = {}
        for m in re.finditer(r"/\*\s*evidence:\s*(E\d)[^*]*\*/\s*UT_TEST\((\w+)\)\s*\{(.*?)\n\}", req.suite, re.S):
            blocks[m.group(2)] = (m.group(1), m.group(3))
        passed = set(req.passed)
        out = []
        for fn in req.layout.functions:
            evidence = self.collect_evidence(req.layout, fn.name).evidence
            lines, seen = [], set()
            for e in evidence:
                if e.evidence_class != "E1":
                    continue
                rid = e.text.split(":", 1)[0].strip()
                if (rid, e.source) in seen:
                    continue
                seen.add((rid, e.source))
                cite = re.compile(r"\b" + re.escape(e.evidence_id) + r"\b")
                by = sorted(name for name, (cls, body) in blocks.items() if name in passed and cls != "E8"
                            and cite.search(body) and re.search(r"\bUT_(EXPECT|ASSERT)\w*\s*\(", body))
                lines.append(f"{rid} ({e.linked_by or 'rule'}) {uc.evidence_key(e)}: "
                             + (f"asserted by {', '.join(by[:5])}" if by else "not asserted by a test"))
            out.append(uc.FunctionRequirements(function=fn.qualified_name or fn.name, requirements=lines,
                                               stated=any(e.evidence_class in ("E1", "E2", "E3") for e in evidence)))
        return uc.RequirementStatus(functions=out)

    def _source_texts(self, root: Path) -> list[tuple[str, str]]:
        """The C and C++ sources of the working copy, read once and again only when a file changed [A1-31b]."""
        cache = self._sources.setdefault(str(root), {})
        out = []
        for path in root.rglob("*"):
            if path.suffix.lower() not in C_EXT + CPP_EXT or not path.is_file():
                continue
            rel = str(path.relative_to(root)).replace("\\", "/")
            try:
                st = path.stat()
            except OSError:
                continue
            stamp = (st.st_mtime_ns, st.st_size)
            if rel not in cache or cache[rel][0] != stamp:
                cache[rel] = (stamp, path.read_text(encoding="utf-8", errors="replace"))
            out.append((rel, cache[rel][1]))
        return out

    # ---------------------------------------------------------------- dependency analysis [A1-DA1 .. A1-DA14]
    async def _find_headers(self, layout: uc.LayoutProfile, build: uc.BuildDescription, repo: Path,
                            tools: uc.Tools, sut: "FrontEnd") -> tuple["FrontEnd", uc.BuildDescription]:
        """A1-DA20: a header the front end does not find is searched by name in the working copy; one candidate is
        taken, several (or none) go to the brain; every folder is kept only when the parse then finds the header
        and has no more errors. The build description itself stays as captured; the folder is recorded."""
        uc.resolution_project(self, layout)
        files = [p for p in repo.rglob("*") if p.is_file() and p.suffix.lower() in HEADER_EXT + (".inc", ".def")
                 and not any(part in HEADER_SKIP for part in p.relative_to(repo).parts)]
        flags = list(build.sut_flags)
        notes: list[str] = []
        for header in list(dict.fromkeys(sut.missing)):
            spelled = header.replace("\\", "/")
            candidates = sorted({str(p.relative_to(repo)).replace("\\", "/")[: -len(spelled)].rstrip("/") or "."
                                 for p in files if str(p.relative_to(repo)).replace("\\", "/").endswith("/" + spelled)
                                 or str(p.relative_to(repo)).replace("\\", "/") == spelled})
            best: dict[str, Any] = {}

            def parse_with(folder: str) -> str:
                path = (repo / folder).resolve()
                if not path.is_dir() or not path.is_relative_to(repo):
                    return f"{folder} is not a folder of the repository"
                trial = FrontEnd(tools, repo, layout.sut_file, flags + [f"-I{uc.REPO_TOKEN}/{folder}".rstrip("/.")
                                                                         if folder != "." else f"-I{uc.REPO_TOKEN}"],
                                 layout.language)
                trial.parse()
                if header in trial.missing:
                    return f"with -I{folder} the front end still does not find {header}"
                if len(trial.missing) + len(trial.errors) > len(sut.missing) + len(sut.errors):
                    return f"with -I{folder} the front end reports more errors: " + "; ".join(trial.errors[:3])
                best.update(folder=folder, front=trial)
                return ""

            if len(candidates) == 1 and not await asyncio.to_thread(parse_with, candidates[0]):
                notes.append(f"{header}: found in {candidates[0]} by its name [A1-DA20]")
            else:
                failure = ba.RuleFailure(rule="A1-DA20 header", failure=f"the header {header} is not on the include "
                                         "paths" + (f"; {len(candidates)} files have its name" if candidates else
                                                    " and no file of the repository has its name"), subject=header)
                folders = sorted({str(p.parent.relative_to(repo)).replace("\\", "/") for p in files})[:400]
                context = [uc.part("facts", f"FILE: {layout.sut_file}\nMISSING HEADER: {header}\nCANDIDATES:\n"
                                   + ("\n".join(candidates) or "- none") + "\nINCLUDE PATHS USED:\n"
                                   + (" ".join(f for f in flags if f.startswith("-I")) or "-")),
                           uc.part("folders", "FOLDERS WITH HEADERS:\n" + "\n".join(folders), uc.P_TRIED, min_chars=800)]
                answer = await self.resolve(
                    failure, task=HEADER_TASK.format(header=header), context=context, output_model=HeaderFolder,
                    verify=lambda a: asyncio.to_thread(parse_with, a.folder.strip().strip("/") or "."),
                    step="A1.header", attempts=self.cfg.rule_attempts, project=layout.repo_root,
                    need=ba.OutputNeed(items=1, tokens_per_item=60, overhead=60))
                if answer is None or "folder" not in best:
                    continue
                notes.append(f"{header}: folder {best['folder']} chosen by the brain, checked by the front end [A1-DA20]")
            folder = best["folder"]
            flags.append(f"-I{uc.REPO_TOKEN}/{folder}".rstrip("/.") if folder != "." else f"-I{uc.REPO_TOKEN}")
            sut = best["front"]
        if notes:
            build = build.model_copy(update={"sut_flags": flags, "notes": list(build.notes) + notes})
        return sut, build

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
        if sut.missing:                                                           # A1-DA20, ARCH-21
            sut, build = await self._find_headers(layout, build, repo, tools, sut)
            ok = not sut.missing
            layout.build, layout.compile_flags = build, list(build.sut_flags)
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
        layout.typedefs = sut.typedefs()                                          # A1-DA21
        layout.private_types = [] if layout.header_sut else sut.private_types()    # A2-21ad
        if layout.language == "cpp":                                               # A2-21ad
            scoped = sut.scoped_types()
            for fn in layout.functions:
                namespace = fn.qualified_name.rsplit("::", 1)[0] if "::" in fn.qualified_name else ""
                fn.return_type = qualify_type(fn.return_type, scoped, namespace)
                for p in fn.params:
                    p.type, p.base = qualify_type(p.type, scoped, namespace), qualify_type(p.base, scoped, namespace)
                    todo = list(p.fields)
                    while todo:                                   # the fields' types too (casts, pointees)
                        f = todo.pop()
                        f.type, f.base = qualify_type(f.type, scoped, namespace), qualify_type(f.base, scoped, namespace)
                        todo += f.fields
        plan = uc.DependencyPlan(headers=sut.headers)
        if sut.missing:                                                           # A1-DA13
            searched = [f for f in uc.expand_flags(build.sut_flags, repo) if f.startswith(("-I", "-isystem"))]
            plan.missing_headers = [f"{h} (searched: {', '.join(searched) or 'no include paths'})" for h in sut.missing]
        if not ok and sut.errors:
            plan.notes.append("front-end errors: " + "; ".join(sut.errors[:3]))
        sut_names = {f.qualified_name for f in functions} | {f.name for f in functions}
        layout.sut_state = []
        classes = {f.qualified_name.rsplit("::", 1)[0] for f in functions if f.member}
        for node, qualified in sut.definitions():          # the SUT's own globals are part of the SUT [A1-DA15]
            if node.get("kind") != "VarDecl":
                continue
            sut_names |= {qualified, node.get("name", "")}
            qt = (node.get("type") or {}).get("qualType", "")
            const = (qt.startswith("const ") and "*" not in qt) or bool(re.search(r"\*\s*const\s*$", qt)) or \
                node.get("constexpr")
            plain = qualified.replace("(anonymous namespace)::", "")
            # namespace variables count too; static data members of a class do not (private, reset by the class)
            scope = plain.rsplit("::", 1)[0] if "::" in plain else ""
            if not const and node.get("storageClass") != "extern" and node.get("name") and scope not in classes:
                layout.sut_state.append(self._state_var(sut, plain, node, qt))
        layout.local_statics = [uc.StateVar(name=f"{fn}::{var}", type=t) for fn, var, t in sut.local_statics()]   # A1-DA18
        if layout.language == "cpp":                    # data members a structured scenario may set
            with contextlib.suppress(Exception):
                layout.class_members = class_members(sut, sorted(classes), layout)
            with contextlib.suppress(Exception):
                layout.constructors = class_constructors(sut, sorted(classes))
        if layout.language == "cpp":                                                     # A1-DA17
            used = " ".join(" ".join(p.type for p in f.params) + " " + f.signature
                            for f in functions + layout.constructors)
            layout.interfaces = [i for i in sut.interfaces()
                                 if re.search(r"\b" + re.escape(i.name.split("::")[-1]) + r"\s*[&*]", used)]
        sut_text = (repo / layout.sut_file).read_text(encoding="utf-8", errors="replace")
        units: dict[str, FrontEnd | None] = {layout.sut_file: sut}
        queue = [(ref, 0, "") for node in nodes.values() for ref in sut.references(node)]
        decided: dict[str, uc.Dependency] = {}
        real_units: dict[str, str] = {}                                           # file -> the dependency that brought it
        while queue:
            ref, depth, via = queue.pop(0)
            dep = self._describe_dependency(sut if depth == 0 else units.get(via) or sut, ref, repo, sut_text, depth)
            if dep is None or dep.qualified_name in sut_names or dep.name in sut_names or \
                    uc.dependency_key(dep) in decided:
                continue
            key = uc.dependency_key(dep)                                        # overloads apart [A1-DA4]
            old = previous.get(dep.name)
            dep.attempts = old.attempts if old else 0
            if not dep.system and not dep.definition.lower().endswith(HEADER_EXT):
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
            if dep.decision == "real" and dep.definition and not dep.definition.lower().endswith(HEADER_EXT):
                if dep.definition not in real_units:                # a unit, never "no definition" [A1-DA10]
                    real_units[dep.definition] = key
                    unit = units.get(dep.definition)
                    for node, _ in (unit.definitions() if unit else []):
                        if node.get("kind") != "VarDecl":
                            queue += [(r, depth + 1, dep.definition) for r in unit.references(node)]
        # A real translation unit brings every definition in it: those dependencies are real too.
        for dep in decided.values():
            if dep.decision == "stub" and dep.definition and dep.definition in real_units and dep.decided_by != "revision":
                dep.decision, dep.decided_by = "real", "rule"
                dep.reason = f"defined in {dep.definition}, which is linked for {real_units[dep.definition]} [A1-DA10]"
        plan.entries = sorted(decided.values(), key=lambda d: (d.depth, d.name))
        uc.number_overloads(plan.entries)                                                    # A1-DA4
        plan.real_sources = sorted({d.definition for d in plan.entries if d.decision == "real" and d.definition
                                    and not d.definition.lower().endswith(HEADER_EXT)})            # A1-DA16
        # declarations proposed earlier for symbols without one stay in the plan [A1-PL14]
        plan.entries += [d for d in previous.values() if d.name not in {e.name for e in plan.entries}
                         and (d.decided_by.startswith("brain declaration") or "[A1-PL14]" in d.reason)]
        self._project_stubs(layout, plan)                                                       # A1-DA7a
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
            refs = [r for r in fe.references(node) if r["kind"] != "VarDecl"]
            calls = sorted({r["name"] for r in refs} | {q for r in refs for q in [fe.callee_name(r)] if q})
            name = qualified if "::" in qualified else node.get("name", qualified)
            nodes[node["id"]] = node
            out.append(uc.FunctionInfo(
                name=name, qualified_name=qualified, signature=header, file=layout.sut_file,
                line_start=src.count(b"\n", 0, begin) + 1, line_end=src.count(b"\n", 0, end) + 1,
                is_static=node.get("storageClass") == "static" or "(anonymous namespace)" in qualified or
                (node.get("kind") == "FunctionDecl" and node.get("id") in fe.ast.internal),   # unnamed namespace [A2-21k]
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
                            type=ref.get("type", ""), header=rel, system=system, depth=depth,
                            namespace=fe.ast.namespace_of(node) if node else "")
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
                dep.method_suffix = (" " + m.group(1).strip()) if m and kind == "method" else \
                    " noexcept" if m and "noexcept" in m.group(1) else ""        # a noexcept function [A2-21ad]
                dep.params = [[(p.get("type") or {}).get("qualType", ""), p.get("name") or f"arg{i}"]
                              for i, p in enumerate(c for c in node.get("inner", []) if c.get("kind") == "ParmVarDecl")]
                if node.get("variadic") or re.search(r"(?:,\s*|\()\.\.\.\)", qt):              # f(fmt, ...) [A2-21g]
                    dep.params.append(["...", ""])
        # A1-DA16: a definition in a header (inline function, member defined in its class, constexpr or inline
        # variable, template) is part of every translation unit that includes it: never a dependency to stub.
        if node and rel and rel.lower().endswith(HEADER_EXT) and (
                any(c.get("kind") == "CompoundStmt" for c in node.get("inner", []) or []) or
                (kind == "variable" and (node.get("constexpr") or node.get("inline") or node.get("init")))):
            dep.definition = rel
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

    # ---------------------------------------------------------------- A1-DA7a: controls of a project stub
    PROTOTYPE = re.compile(r"^[ \t]*(?!typedef\b|static\b|return\b|#)((?:[A-Za-z_][\w:<>]*[\s*&]+)+)([A-Za-z_]\w*)\s*"
                           r"\(([^;{}()]*)\)\s*(?:;|\{)", re.M)
    GLOBAL = re.compile(r"^[ \t]*extern\s+(?![^;]*\bconst\b)([^;()]*?)\b([A-Za-z_]\w*)\s*;", re.M)

    @staticmethod
    def _stub_header(root: Path, stub: str, layout: uc.LayoutProfile) -> str:
        """The header that declares a project stub's controls: the one of the same name next to it, else a header
        in a stub folder that the stub includes."""
        path = root / stub
        for ext in (".h", ".hpp", ".hh"):
            if path.with_suffix(ext).is_file():
                return str(path.with_suffix(ext).relative_to(root)).replace("\\", "/")
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        for name in re.findall(r'#\s*include\s*"([^"]+)"', text):
            for folder in [str(Path(stub).parent)] + list(layout.stub_dirs):
                candidate = root / folder / name
                if candidate.is_file():
                    return str(candidate.relative_to(root)).replace("\\", "/")
        return ""

    @classmethod
    def _controls_in(cls, text: str, served: set[str], production: set[str], definitions: bool) -> list[uc.StubControl]:
        out: dict[str, uc.StubControl] = {}
        for m in cls.PROTOTYPE.finditer(text):
            ret, name, args = " ".join(m.group(1).split()), m.group(2), m.group(3).strip()
            if name in served or name in production or name in KEYWORDS or name in ("main", "if", "while", "for"):
                continue
            if definitions != m.group(0).rstrip().endswith("{"):
                continue
            params = []
            for a in ([] if args in ("", "void") else (x.strip() for x in args.split(","))):
                pm = re.match(r"^(.*?[\s*&])([A-Za-z_]\w*)\s*(\[[^\]]*\])?$", a)
                params.append([" ".join(pm.group(1).split()), pm.group(2)] if pm and pm.group(1).strip()
                              and pm.group(2) not in ("int", "char", "long", "short", "unsigned", "float", "double")
                              else [a, ""])
            out.setdefault(name, uc.StubControl(name=name, declaration=f"{ret} {name}({args or 'void'})",
                                                return_type=ret, params=params))
        if not definitions:
            for m in cls.GLOBAL.finditer(text):
                name = m.group(2)
                if name not in served and name not in production:
                    out.setdefault(name, uc.StubControl(name=name, kind="variable", type=" ".join(m.group(1).split()),
                                                        declaration=f"extern {' '.join(m.group(1).split())} {name}"))
        return list(out.values())

    def _project_stubs(self, layout: uc.LayoutProfile, plan: uc.DependencyPlan) -> None:
        """A1-DA7a: the controls of every reused project stub; a stub without any control is not reused (unless
        the configuration keeps project stubs): its dependencies get generated stubs."""
        root = Path(layout.repo_root)
        by_file: dict[str, list[uc.Dependency]] = {}
        for d in plan.entries:
            if d.existing_stub and d.decision == "stub":
                by_file.setdefault(d.existing_stub, []).append(d)
        production = set()
        for rel in list(dict.fromkeys(layout.headers + plan.headers)):
            if (root / rel).is_file() and "/stub" not in "/" + rel.lower():
                production |= set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", strip_comments(
                    (root / rel).read_text(encoding="utf-8", errors="replace"))))
        stubs = []
        for file, deps in sorted(by_file.items()):
            served = {d.name for d in deps} | {d.qualified_name for d in deps if d.qualified_name}
            header = self._stub_header(root, file, layout)
            controls = []
            if header:
                controls = self._controls_in(strip_comments((root / header).read_text(encoding="utf-8", errors="replace")),
                                             served, production, definitions=False)
            if not controls and (root / file).is_file():
                controls = self._controls_in(strip_comments((root / file).read_text(encoding="utf-8", errors="replace")),
                                             served, production, definitions=True)
            if not controls and not self.cfg.prefer_project_stubs:
                for d in deps:
                    d.existing_stub = ""
                    d.reason = (f"the project stub {file} offers no control a test can set, so a generated stub with "
                                "controls is used instead [A1-DA7a]")
                plan.notes.append(f"{file}: no control a test can set - generated stubs replace it for "
                                  f"{', '.join(sorted(d.name for d in deps))} [A1-DA7a]")
                continue
            reset = next((c.name for c in controls if c.kind == "function" and not c.params and
                          re.search(r"reset|init|clear", c.name, re.I)), "")
            stubs.append(uc.ProjectStub(file=file, header=header, controls=controls, reset=reset,
                                        serves=sorted(served)))
            for d in deps:
                d.reason = (f"the existing stub {file} is reused [A1-DA7]; its controls: "
                            + (", ".join(c.name for c in controls[:6]) or "none") + " [A1-DA7a]")
        plan.project_stubs = stubs

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
        header = Path(dep.header).stem if dep.header else ""

        def likely(item: tuple[str, Any]) -> tuple[int, int, str]:
            """The file named like the declaring header first (hw.h -> hw.c), then the nearest to the header."""
            rel = item[0]
            near = len(os.path.commonprefix([rel, dep.header or layout.sut_file]))
            return (0 if header and Path(rel).stem == header else 1, -near, rel)
        for rel, entry in sorted(entries.items(), key=likely):
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
                    if (dep.kind == "variable") != (node.get("kind") == "VarDecl"):
                        continue
                    # internal linkage defines another symbol of the same name, never this one [A1-DA5]
                    if node.get("storageClass") == "static" or "(anonymous" in (qualified or ""):
                        continue
                    if dep.kind != "variable" and dep.params is not None:          # the same overload [A1-DA4]
                        params = [c for c in node.get("inner", []) or [] if c.get("kind") == "ParmVarDecl"]
                        wanted = [p for p in dep.params if p[0] != "..."]
                        if dep.params and len(params) != len(wanted):
                            continue
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
        owner = (dep.qualified_name or "").rsplit("::", 1)[0]
        if dep.kind == "method" and owner and any(i.name == owner or i.name.endswith("::" + owner)
                                                  for i in layout.interfaces):
            dep.decision = "real"                                                          # A1-DA17, A2-21aa
            dep.reason = f"pure virtual method of the injected interface {owner}: the test passes a fake [A2-21aa]"
            return
        if dep.definition.lower().endswith(HEADER_EXT):                                    # A1-DA16
            dep.decision = "real"
            dep.reason = f"defined in a header ({dep.definition}), so part of every translation unit [A1-DA16]"
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
            if dep.definition and uc.real_source_error(dep.definition, text):     # target code that is not for the host
                return (f"the file of the real definition does not compile here ({f.diagnostic[:120]}): stubbed "
                        "[A1-DA12]")
        if dep.definition and uc.real_source_error(dep.definition, last.reason):
            return f"the file of the real definition does not compile here ({last.reason[:120]}): stubbed [A1-DA12]"
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

    @staticmethod
    def _state_var(sut: "FrontEnd", name: str, node: dict, qt: str) -> uc.StateVar:
        """A SUT state variable with its array length and the fields of its structure, so that a structured scenario
        sets one element or field of it (s_faults[1].active) through a generated setter."""
        var = uc.StateVar(name=name, type=qt)
        with contextlib.suppress(Exception):
            m = re.search(r"\[(\d+)\]\s*$", qt)
            elem = re.sub(r"\s*\[\d+\]\s*$", "", qt) if m else qt
            var.array_len = int(m.group(1)) if m else 0
            base = _plain_type(elem)
            records = sut._records()
            rec = records.get(base) or records.get(sut._through_typedefs(base, {}))
            if rec is not None:
                var.fields = sut._fields(rec) or []
        return var

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
            for name, expr in integral_constants(text):               # C/C++ const and constexpr integers
                value = eval_int(expr, constants)
                if value is not None:
                    constants.setdefault(name, value)
        for e in layout.enums:
            for k, v in e.values.items():
                constants[k.split("::")[-1]] = v
        return constants

    def _internal_values(self, layout: uc.LayoutProfile) -> dict[str, str]:
        """A1-PL2a: the integer macros and enumerators that only the SUT file defines - the SUT sees them, a test
        file does not - with their values. A test writes the value; the facts show the value next to the name."""
        key = (layout.repo_root, layout.sut_file, layout.sut_sha256)
        cached = getattr(self, "_internal_cache", {}).get(key)
        if cached is not None:
            return cached
        root = Path(layout.repo_root)
        sut = root / layout.sut_file
        out: dict[str, str] = {}
        if sut.is_file() and not layout.header_sut:
            text = strip_comments(sut.read_text(encoding="utf-8", errors="replace"))
            visible = set(re.findall(r"\b([A-Za-z_]\w*)\b", "\n".join(self._dependency_headers_text(layout))))
            constants = self._constants_for_values(layout)
            names = re.findall(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+\S", text, re.M)
            names += [n for n, _ in integral_constants(text)]          # constexpr std::uint32_t kPumpMaxOnMs = ...
            names += [k.split("::")[-1] for e in layout.enums for k in e.values]
            for name in dict.fromkeys(names):
                if name in visible or name in LIMITS or name not in constants:
                    continue
                if not re.search(r"\b" + re.escape(name) + r"\b", text):
                    continue                                  # an enumerator of a header the suite does not include
                out[name] = str(constants[name])
        if not hasattr(self, "_internal_cache"):
            self._internal_cache = {}
        self._internal_cache[key] = out
        return out

    def _accumulated(self, fn: uc.FunctionInfo, layout: uc.LayoutProfile) -> dict[str, str]:
        """A1-PL2b: parameter -> the setter of the state variable the function adds it to (state += dt_ms, state =
        state + dt_ms), so that a threshold the parameter's range cannot reach is reached through the state."""
        path = Path(layout.repo_root) / layout.sut_file
        if not path.is_file():
            return {}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        body = strip_comments("\n".join(lines[fn.line_start - 1: fn.line_end]))
        alias = self._state_aliases(layout)
        out: dict[str, str] = {}
        for p in text_params(fn):
            if p.kind not in ("int", "float"):
                continue
            q = re.escape(p.name)
            for m in re.finditer(r"([A-Za-z_][\w.:>-]*)\s*(?:\+=\s*(?:\([^()]*\)\s*)?" + q + r"\b|=\s*\1\s*\+\s*"
                                 r"(?:\([^()]*\)\s*)?" + q + r"\b)", body):
                target = m.group(1).replace("->", ".").split(".")[-1] if "." in m.group(1) or "->" in m.group(1) \
                    else m.group(1)
                suffix = alias.get(m.group(1)) or alias.get(target)
                if suffix:
                    out[p.name] = next((v.name.replace("(anonymous namespace)::", "") for v in uc.settable_state(layout)
                                        if uc.state_setter_name(v) == f"ut_state_set_{suffix}"), target)
                    break
        return out

    def _state_hint(self, value: str, p: uc.ParamInfo, fn: uc.FunctionInfo, layout: uc.LayoutProfile) -> str:
        """A1-PL2b: an enumerator given for a parameter that belongs to the type of a SUT state variable - the
        setter of that variable, for the re-ask."""
        name = value.strip().split("::")[-1]
        enum = next((e for e in layout.enums if name in {k.split("::")[-1] for k in e.values}), None)
        if enum is None:
            return ""
        short = enum.name.split("::")[-1]
        holders = [v for v in uc.settable_state(layout)
                   if re.sub(r"\b(volatile|const|static|enum)\b", "", v.type).strip().split("::")[-1] == short]
        if not holders:
            return ""
        sets = " or ".join(f"state {v.name.replace('(anonymous namespace)::', '')} = {name}" for v in holders[:2])
        return (f" - {name} is a value of the SUT state {', '.join(v.name for v in holders[:2])} ({short}): set it "
                f"with {sets}, and give {p.name} one of " + ", ".join(p.enum_values[:8] or ["its own values"])
                + " [A1-PL2b]")

    def _state_settings(self, conditions: list[str], fn: uc.FunctionInfo, layout: uc.LayoutProfile) -> list[str]:
        """A1-PL2b: the setter calls that meet the path conditions on SUT state, e.g. s_mode == MODE_ON ->
        ut_state_set_s_mode(MODE_ON); a value only the SUT file defines is written as its value (A1-PL2a)."""
        alias = {n: s for n, s in self._state_aliases(layout).items() if n not in {p.name for p in text_params(fn)}}
        if not alias or not conditions:
            return []
        values = self._internal_values(layout)
        names = "|".join(re.escape(n) for n in sorted(alias, key=len, reverse=True))
        value = r"([-+]?\w[\w:]*)"
        out: list[str] = []
        for c in conditions:
            for m in re.finditer(r"(?<![\w.>])(" + names + r")\s*(==|!=|>=|<=|>|<)\s*" + value, c):
                out.append(self._setting(m.group(1), m.group(2), m.group(3), values))
            for m in re.finditer(value + r"\s*(==|!=|>=|<=|>|<)\s*(" + names + r")\b(?!\s*(?:\.|->|\[|\())", c):
                flip = {">": "<", "<": ">", ">=": "<=", "<=": ">="}.get(m.group(2), m.group(2))
                out.append(self._setting(m.group(3), flip, m.group(1), values))
            for m in re.finditer(r"(?:^|[(&|]\s*)(!?)\s*(" + names + r")\s*(?=$|[)&|])", c.strip()):
                out.append(f"{m.group(2)} = {0 if m.group(1) else 1}")
        return list(dict.fromkeys(out))[:4]

    @staticmethod
    def _setting(name: str, op: str, v: str, values: dict[str, str]) -> str:
        """A1-PL2b: the state value that meets one path condition, as a structured state entry (name = value)."""
        v = values.get(v, v)
        number = re.fullmatch(r"[-+]?\d+", v)
        if op in ("==", ">=", "<="):
            arg = v
        elif op == ">":
            arg = str(int(v) + 1) if number else f"{v} + 1"
        elif op == "<":
            arg = str(int(v) - 1) if number else f"{v} - 1"
        else:
            return f"{name} = a value other than {v}"
        return f"{name} = {arg}"

    def _known_names(self, layout: uc.LayoutProfile, stubs: list[uc.StubSpec]) -> set[str]:
        """Identifiers a planned statement may use: SUT functions, everything the headers declare,
        the stubs' control variables and generated callbacks."""
        root = Path(layout.repo_root)
        names = set(PLAN_SAFE_CALLS) | set(LIMITS) | {"NULL", "nullptr", "true", "false"}
        names |= set(re.findall(r"^#define\s+([A-Za-z_]\w*)", uc.HARNESS_H, re.M))       # every harness macro [A1-PL4]
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
        names |= {c.name for ps in (layout.dependencies.project_stubs if layout.dependencies else [])
                  for c in ps.controls}                                                              # A2-21ak
        names |= {uc.state_setter_name(v) for v in uc.settable_state(layout)}                        # A2-21z
        names |= {uc.state_getter_name(v) for v in uc.settable_state(layout)}
        names |= {e[0] for e in uc.element_setters(layout)}                       # structured: elements and fields
        names |= {"ut_member", "memcpy"} | {uc.member_tag(m) for m in layout.class_members}
        for s in stubs:                                                          # structured: stub outputs
            base = f"ut_stub_{uc.stub_base(s)}"
            names |= {f"{base}_{p[1]}_out" for p in s.params if p[1] and uc.stub_output(p[0])}
            names |= {f"{base}_{p[1]}_out_len" for p in s.params if p[1] and uc.stub_output(p[0])}
            names |= {f"{base}_{p[1]}_out_t" for p in s.params if p[1] and uc.stub_output(p[0])}
        names |= {"ut_static_" + re.sub(r"\W", "_", f.name.replace("(anonymous namespace)::", ""))
                  for f in layout.functions if f.is_static and f.from_frontend}
        return names

    @staticmethod
    def _plan_model(fn: uc.FunctionInfo, open_ids: set[str], evidence: list[uc.EvidenceRef],
                    stubs: list[uc.StubSpec], targets: list[str] | None = None, per_scenario: int = 3,
                    lists: dict[str, list[str]] | None = None) -> type[BaseModel]:
        """ARCH-20a: the lists of this function for the brain's schema; its targets instead of its items (A1-PL3a);
        ``lists``: the names of the structured scenario - state, fields, callable functions, stub outputs, registers
        and object members (the brain names them, the generator writes the code)."""
        lists = lists or {}
        settable = list(lists["stub_returns"]) if "stub_returns" in lists else [     # the stubs it calls
            n for s in stubs if s.kind not in ("variable", "constructor") and
            re.sub(r"\b(const|volatile|static|inline|extern|virtual)\b", "", s.return_type).strip()
            not in ("", "void") for n in {s.function, s.qualified_name or s.function}]
        variables = [n for s in stubs if s.kind == "variable" and not s.is_const
                     for n in {s.function, s.qualified_name or s.function}]
        lists = lists or {}
        return planned_model(open_ids, [e.evidence_id for e in evidence], text_params(fn),       # typed values
                             settable, variables, targets, per_scenario, lists.get("state", []),
                             lists.get("fields", []), lists.get("callables", []), lists.get("outputs", []),
                             lists.get("registers", []), lists.get("members", []), lists.get("exceptions", []),
                             lists.get("member_kinds"), lists.get("call_params"), lists.get("objects", ()),
                             lists.get("stub_returns"), lists.get("returns"))

    # ---------------------------------------------------------------- structured scenario: the brain says what
    def _structured_lists(self, fn: uc.FunctionInfo, layout: uc.LayoutProfile, stubs: list[uc.StubSpec],
                          sut_text: str) -> dict[str, Any]:
        """The names a structured scenario of ``fn`` may use, with what the facts show of each: the SUT state the
        function and its callees use (variables, and the elements and fields of arrays and structures), the fields
        of its structure parameters and of that state, the functions a test may call before it, the output
        parameters of the stubs it calls, the registers it reads and the members of its object (C++)."""
        lines = sut_text.splitlines()
        body = "\n".join(lines[fn.line_start - 1: fn.line_end])
        listed, rest = uc.function_state(fn, layout, lines, self.cfg.state_per_function)
        callee_text = body
        for g in layout.functions:                                   # one level of callees for arrays and structures
            if g is not fn and re.search(r"\b" + re.escape(g.name.split("::")[-1]) + r"\s*\(", body):
                callee_text += "\n" + "\n".join(lines[g.line_start - 1: g.line_end])
        state_rows, state_names, fields = [], [], set()
        for v in listed + rest:
            name = v.name.replace("(anonymous namespace)::", "")
            state_names.append(name)
            state_rows.append(f"- {name} ({v.type})")
        by_var: dict[str, list[tuple]] = {}
        for entry in uc.element_setters(layout):
            by_var.setdefault(entry[1].name, []).append(entry)
        for vname, entries in by_var.items():
            plain = vname.replace("(anonymous namespace)::", "")
            if not re.search(r"\b" + re.escape(plain.split("::")[-1]) + r"\b", callee_text):
                continue
            v = entries[0][1]
            state_names.append(plain)
            paths = [e[3] for e in entries if e[3]]
            fields |= set(paths)
            state_rows.append(f"- {plain} ({v.type})" + (f": index 0..{v.array_len - 1}" if v.array_len else "")
                              + (f"; fields {', '.join(paths[:16])}" if paths else ""))
        for p in text_params(fn):
            todo = [(f, f.name) for f in p.fields[:16]]
            while todo:
                f, path = todo.pop(0)
                fields.add(path)
                if f.kind == "array":                                   # an element of an array field, by index
                    fields |= {f"{path}[{i}]" for i in range(min(f.array_len or 0, 16))}
                if f.fields and path.count(".") < 2:                    # a nested structure or union
                    todo += [(g, f"{path}.{g.name}") for g in f.fields[:12]]
        callables, call_rows, call_params = [], [], {}
        cls = uc.class_of(fn)
        for g in layout.functions:
            if g.line_start == 0 or g.qualified_name.split("::")[-1].startswith("~"):
                continue
            if g.member and (not fn.member or uc.class_of(g) != cls or g.qualified_name.split("::")[-1] ==
                             cls.split("::")[-1]):
                continue
            name = g.qualified_name.replace("(anonymous namespace)::", "")
            same_object = fn.member and g.member and not uc.static_member(g, layout)    # it changes the object too
            if same_object or (state_names or uc.keeps_state(fn, layout)) and (g is fn or any(
                    re.search(r"\b" + re.escape(s.replace("(anonymous namespace)::", "").split("::")[-1]) + r"\b",
                              "\n".join(lines[g.line_start - 1: g.line_end])) for s in state_names)):
                callables.append(name)                             # calls build up state: only with state
                call_rows.append(f"- {name}({', '.join(p.type for p in text_params(g))})")
                call_params[name] = list(uc.instantiated(g, layout).params)
        made = uc.constructor_of(cls, layout) if fn.member and uc.object_recipe(fn, layout) else None
        if made is not None and any(p.kind in ("int", "enum", "bool", "float") for p in made.params):
            name = made.qualified_name.replace("(anonymous namespace)::", "")   # the object's values [A2-21ae]
            callables.append(name)
            call_params[name] = list(made.params)
            call_rows.append(f"- {name}({', '.join(p.type for p in made.params)}) - the constructor of the object: "
                             "name it first to build the object with these values (else each value is {})")
        outputs, output_rows = [], []
        called = uc.stubs_called(fn, layout, stubs)               # the stubs it calls, by the names that pass
        stub_returns = {}
        for n, s in called.items():
            ret = re.sub(r"\b(const|volatile|static|inline|extern|virtual)\b", "", s.return_type).strip()
            if s.kind != "constructor" and ret not in ("", "void"):
                stub_returns[n] = uc.ParamInfo(name=f"{n}()", type=ret, kind="other") if ret.endswith(("*", "&")) \
                    else target_param(f"{n}()", ret, layout)
        for s in {id(s): s for s in called.values()}.values():
            for ptype, pname in s.params:
                if pname and stub_output(ptype):
                    outputs.append(f"{s.qualified_name or s.function}.{pname}")
                    output_rows.append(f"- {s.qualified_name or s.function}.{pname} ({ptype})")
                    fields |= {f for f in self._type_fields(stub_pointee(ptype), layout)}
        registers, reg_rows = [], []
        constants = self._constants_for_values(layout)
        macros = {}
        for macro, expr in re.findall(r"#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+\(?\s*\*\s*\(\s*volatile[^)]*\*\s*\)\s*"
                                      r"([^\n]+?)\s*\)?[ \t]*$", "\n".join(self._dependency_headers_text(layout)) +
                                      "\n" + sut_text, re.M):
            value = eval_int(expr.strip().rstrip(")") if expr.count(")") > expr.count("(") else expr, constants)
            if value is not None and value > 0:                  # a literal address or base + offset by macros
                macros[macro] = f"0x{value:X}"
        for macro, address in macros.items():
            if re.search(r"\b" + re.escape(macro) + r"\b", callee_text):
                registers.append(address)
                reg_rows.append(f"- {address} ({macro})")
        exceptions = thrown_types(callee_text) if layout.language == "cpp" else []
        members, member_rows, member_kinds = [], [], {}
        made_here = fn.member and fn.qualified_name.split("::")[-1] == cls.split("::")[-1]
        if fn.member and not made_here:              # a constructor's object does not exist before the call
            for m in layout.class_members:
                if m.class_name.replace("(anonymous namespace)::", "") == cls:
                    members.append(m.name)
                    member_rows.append(f"- {m.name} ({m.type}{', static' if m.static else ''})")
                    container = bool(re.search(r"\bstd::(?:array|vector)\s*<", m.type))
                    member_kinds[m.name] = uc.ParamInfo(name=m.name, type=m.type, kind="buffer") if container \
                        else target_param(m.name, m.type, layout)
        return {"state": state_names, "fields": sorted(fields), "callables": callables[:40],
                "outputs": outputs, "registers": registers, "members": members, "exceptions": exceptions,
                "member_kinds": member_kinds, "call_params": call_params, "stub_returns": stub_returns,
                "returns": target_param("", re.sub(r"\b(const|volatile|static|inline)\b", "", return_type_of(fn)).strip(),
                                        layout),
                "objects": [p.name for p in text_params(fn) if p.kind == "object" and uc.sut_class(p.base or p.type,
                                                                                                   layout)],
                "rows": {"state": state_rows, "callables": call_rows[:40], "outputs": output_rows,
                         "registers": reg_rows, "members": member_rows,
                         "exceptions": [f"- {e}" for e in exceptions]}}

    @staticmethod
    def _type_fields(type_name: str, layout: uc.LayoutProfile) -> list[str]:
        """The field names of a structure the parameters of the SUT's functions use (from the front end)."""
        short = re.sub(r"\b(const|volatile|struct|union|class)\b", "", type_name).strip().split("::")[-1]
        for f in layout.functions:
            for p in f.params:
                if p.fields and (p.base or p.type).replace("*", "").replace("&", "").strip().split("::")[-1] \
                        .replace("const ", "").strip() == short:
                    return [x.name for x in p.fields]
        return []

    def _lower_structured(self, planned: "PlannedScenario", fn: uc.FunctionInfo, layout: uc.LayoutProfile,
                          stubs: list[uc.StubSpec], constants: dict[str, int] | None = None,
                          known: set[str] | None = None) -> tuple[list[str], list[str]]:
        """Structured scenario: the brain said what, this writes how - deterministically, the same code for every
        brain. Buffers and structures become declared objects passed in the right form, state becomes setter calls,
        earlier calls become calls of the function (through its wrapper or the object), stub outputs become the
        stub's output controls, registers UT_REG32 assignments and object members accessor assignments. Every value
        is checked and shaped by the type of what it sets (``typed``), the same check a plain argument gets. Returns
        (fixes, problems); a name that is not in the lists is a problem and the scenario is refused with it."""
        fixes, problems, decls, stmts = [], [], [], []
        cpp = layout.language == "cpp"
        params = {p.name: p for p in text_params(fn)}
        constants = constants if constants is not None else self._constants_for_values(layout)
        known = known if known is not None else self._known_names(layout, stubs)
        internal = self._internal_values(layout)
        scoped = uc.namespace_names(layout, uc.headers_text(layout)) if cpp else {}

        def typed(where: str, value: str, target: uc.ParamInfo) -> str | None:
            """One value for one target, by facts: the rest of the JSON cut off, a name only the SUT file sees
            replaced by its value (A1-PL2a), an enumerator or a namespaced name written in full (A2-21ai), then one
            C expression and the target type's check (range, form, known names). None refuses the entry."""
            v = uc.with_values(fix_value(value), internal)
            if target.kind == "enum":
                full = next((k for k in target.enum_values if k.split("::")[-1] == v.split("::")[-1]), None)
                v = full or v
            elif scoped:
                v = uc.qualify_names(v, scoped)[0]
            problem = uc.c_expression_problem(v, braces=target.kind not in ("int", "enum", "bool", "float"))
            problem = f"{where}: {v[:40]!r} is {problem}" if problem else self._value_problem(
                target.model_copy(update={"name": where}), v, fn, constants, set(), known, layout)
            if problem:
                problems.append(problem)
                return None
            return v

        def all_typed(where: str, values: list[str], target: uc.ParamInfo) -> list[str] | None:
            out = [typed(where, v, target) for v in values]
            return None if any(v is None for v in out) else out

        def fields_of(var: str, where: str, given: list["PlannedField"], fields: list[uc.FieldInfo]) -> list[str] | None:
            """Assignments of a structure's fields: a field must be one of its fields (when they are known); an array
            field takes a list, element by element."""
            rows, known_fields = [], {f.name for f in fields}
            for f in given[:32]:
                path = re.sub(r"\s+", "", f.field)
                info = field_at(fields, path)
                if known_fields and (re.sub(r"\[.*$", "", path.split(".")[0]) not in known_fields
                                     or ("[" in path and info is None)):
                    problems.append(f"{where}: {path} is not a field or an element of one (its fields: "
                                    f"{', '.join(sorted(known_fields)[:12])})")
                    return None
                if info is not None and info.kind == "array":
                    items = all_typed(f"{where}.{path}", as_list(f.value), target_param("", array_element(info.type),
                                                                                       layout))
                    if items is None:
                        return None
                    rows += [f"{var}.{path}[{i}] = {x};" for i, x in enumerate(items[: info.array_len or 256])]
                    continue
                v = typed(f"{where}.{path}", f.value, target_param("", info.type, layout) if info else
                          uc.ParamInfo(name="", type="", kind="other"))
                if v is None:
                    return None
                rows.append(f"{var}.{path} = {v};")
            return rows
        def lower_arg(a: "PlannedArgValue", p: uc.ParamInfo, var: str, where: str) -> bool:
            """One argument - of the call under test or of a call made before, the same form for both: the value
            its type takes, or the object the generator declares and fills (a buffer, a structure, a reference to a
            scalar, an injected interface's fake, an object of a class of the SUT) and passes in the right form.
            False when it is refused (the problem is recorded)."""
            if p.kind == "enum" and a.value:                                     # the enumerator in full [A2-21ai]
                full = next((k for k in p.enum_values if k.split("::")[-1] == fix_value(a.value).split("::")[-1]), None)
                a.value = full or a.value
            base = pointee(p) or re.sub(r"\b(const|volatile)\b|[&*]", "", p.base or p.type).strip()
            iface = next((i for i in layout.interfaces if i.name.split("::")[-1] == base.split("::")[-1]), None)
            if p.kind == "interface" or iface is not None:   # an injected interface: the generator's fake [A2-21aa]
                decls.append(f"{uc.fake_name_of(iface.name if iface else base)} {var};")
                a.value = var if p.by_ref or "*" not in p.type else f"&{var}"
                a.elements, a.fields = [], []
                fixes.append(f"{where}: an injected {base} - the generator's fake {var} [structured]")
                return True
            value = fix_value(a.value)
            cls = uc.sut_class(p.base or p.type, layout) if p.kind == "object" else ""
            if cls and not ("*" in p.type and not p.by_ref and value in NULLS):
                lines, obj = uc.class_recipe(cls, layout, var, declared=base)   # its constructor builds it [A2-21ae]
                decls.extend(lines)
                if value or a.elements or a.fields:
                    fixes.append(f"{where}: an object of {cls} takes no value - built by its constructor [structured]")
                a.value = obj if p.by_ref or "*" not in p.type else f"&{obj}"
                a.elements, a.fields = [], []
                fixes.append(f"{where}: an object of {cls} built by its constructor, passed as {a.value} [structured]")
                return True
            record = p.kind in ("ptr_struct", "struct")
            if record and not (a.elements or a.fields) and (not value or value not in NULLS and (  # no field given:
                    eval_int(value, constants) is not None or value.startswith("{"))):          # a zeroed structure
                init = value if value.startswith("{") and not uc.c_form_problem(value) else "{" + ("" if cpp else "0") + "}"
                decls.append(f"{base} {var} = {init};")    # a structure is its initializer; no field given: zero
                a.value = var if p.by_ref or "*" not in p.type else f"&{var}"
                fixes.append(f"{where}: a {base} declared {'with ' + init if init != value else 'zeroed'} "
                             f"and passed as {a.value} [structured]")
                return True
            if p.kind == "ptr_scalar" and p.by_ref:     # a reference to a scalar: a named variable with its value
                v = typed(where, value or (a.elements[0] if a.elements else ""), target_param(a.name, base, layout))
                if v is None:
                    return False
                decls.append(f"{base} {var} = {v};")
                a.value, a.elements, a.fields = var, [], []
                fixes.append(f"{where}: a {p.type} - {var} declared with {v} and passed [structured]")
                return True
            if not (a.elements or a.fields):
                return True
            scalar = p.kind in ("int", "enum", "bool", "float")
            if scalar and value and not uc.c_expression_problem(value, braces=False):
                fixes.append(f"{where}: a {p.type} takes its value; elements and fields left out [structured]")
                a.elements, a.fields = [], []          # the type is a fact: one value, so the list is form noise
                return True
            if a.elements and p.kind in ("buffer", "ptr_scalar", "ptr_void", "cstring") and not p.by_ref:
                given = all_typed(where, list(a.elements[:256]), target_param("", base, layout))
                if given is None:
                    return False
                decls.append(f"{base or 'unsigned char'} {var}[{max(len(given), 1)}] = {{{', '.join(given)}}};")
                a.value = var
            elif a.fields and record:
                rows = fields_of(var, where, a.fields, p.fields)
                if rows is None:
                    return False
                decls.extend([f"{base} {var} = {{{'' if cpp else '0'}}};", *rows])
                a.value = var if p.by_ref or "*" not in p.type else f"&{var}"
            else:
                problems.append(f"{where}: elements are for a buffer or pointer, fields for a structure parameter"
                                f" (its type is {p.type})")
                return False
            fixes.append(f"{where}: declared and filled by the generator, passed as {a.value} [structured]")
            a.elements, a.fields = [], []
            return True
        for a in planned.args:
            if a.name in params:
                lower_arg(a, params[a.name], f"ut_{re.sub(r'[^A-Za-z0-9_]', '_', a.name)}", f"args.{a.name}")
        alias = self._state_aliases(layout)
        scalars = {uc.state_setter_name(v)[len("ut_state_set_"):]: v for v in uc.settable_state(layout)}
        elements: dict[tuple[str, str], tuple[str, bool, uc.StateVar]] = {}
        for name, v, indexed, path, _ in uc.element_setters(layout):
            for key in {v.name, v.name.replace("(anonymous namespace)::", ""), v.name.split("::")[-1]}:
                elements.setdefault((key, path), (name, indexed, v))
        for s in planned.state:
            var = s.variable.strip()
            if var in alias:                         # a plain variable: an index or a field does not apply (its type)
                if s.index >= 0 or s.field.strip():
                    fixes.append(f"state {var}: a plain variable - index and field left out [structured]")
                target = target_param("", scalars[alias[var]].type, layout)
                v = typed(f"state {var}", s.value, target)
                if v is not None:            # the setter takes long long: a scoped enumerator converts only by a cast
                    stmts.append(f"ut_state_set_{alias[var]}({'(long long)' if cpp and target.kind == 'enum' else ''}{v});")
                continue
            field = s.field.strip()
            setter = elements.get((var, field)) or elements.get((var, field.split(".", 1)[-1] if field.startswith(
                var.split("::")[-1] + ".") else field))
            if setter is None:
                problems.append(f"state {var}{'[' + str(s.index) + ']' if s.index >= 0 else ''}"
                                f"{'.' + field if field else ''}: not a variable, element or field of SUT STATE")
                continue
            name, indexed, sv = setter
            if indexed and s.index < 0:
                problems.append(f"state {var}: an array - give its index")
                continue
            if indexed and sv.array_len and s.index >= sv.array_len:
                problems.append(f"state {var}[{s.index}]: the index is outside 0..{sv.array_len - 1}")
                continue
            info = field_at(sv.fields, field) if field else None
            ctype = info.type if info else array_element(sv.type)
            v = typed(f"state {var}{'.' + field if field else ''}", s.value, target_param("", ctype, layout))
            if v is not None:
                stmts.append(f"{name}({str(s.index) + ', ' if indexed else ''}{v});")
        made = uc.constructor_of(uc.class_of(fn), layout) if fn.member and uc.object_recipe(fn, layout) else None

        def by_param(c: "PlannedCall", ps: list[uc.ParamInfo]) -> list["PlannedArgValue"] | None:
            """The arguments of a call made before, one per parameter: by name when they are named, else in order."""
            named = [x for x in c.args if x.name]
            if named and len(named) == len(c.args):
                wrong = [x.name for x in named if x.name not in {p.name for p in ps}]
                if wrong or len({x.name for x in named}) != len(named):
                    problems.append(f"calls {c.function}: name each parameter once - its parameters: "
                                    f"{', '.join(p.name for p in ps) or 'none'}")
                    return None
                return [next((x for x in named if x.name == p.name), PlannedArgValue(name=p.name)) for p in ps]
            return [x.model_copy(update={"name": p.name}) for x, p in zip(c.args, ps)] + \
                [PlannedArgValue(name=p.name) for p in ps[len(c.args):]]
        for c in planned.calls:
            g = uc.function_of(c.function, layout) or next(
                (f for f in layout.functions if f.qualified_name.replace("(anonymous namespace)::", "") == c.function
                 or f.name.split("::")[-1] == c.function.split("::")[-1]), None)
            if made is not None and c.function.replace("(anonymous namespace)::", "").split("::")[-2:] == \
                    made.qualified_name.replace("(anonymous namespace)::", "").split("::")[-2:]:
                if len(c.args) > len(made.params) or planned.object_args:   # the object's constructor [A2-21ae]
                    problems.append(f"calls: the constructor {c.function} takes {len(made.params)} argument(s), "
                                    f"given {len(c.args)}" if not planned.object_args else
                                    f"calls: the constructor {c.function} is named twice")
                    continue
                given = by_param(c, made.params)                                   # a value not given: {} as listed
                if given is None:
                    continue
                values = [typed(f"calls {c.function}.{p.name}", x.value, p) if p.kind in ("int", "enum", "bool", "float")
                          and x.value.strip() else "" for x, p in zip(given, made.params)]
                if all(v is not None for v in values):
                    planned.object_args = values
                    fixes.append(f"calls: {c.function} builds the object [structured]")
                continue
            if g is None:
                problems.append(f"calls: {c.function} is not a function of the SUT")
                continue
            g = uc.instantiated(g, layout)
            if len(c.args) != len(g.params):
                problems.append(f"calls: {c.function} takes {len(g.params)} argument(s), {len(c.args)} given")
                continue
            if g.member:
                if not fn.member or uc.class_of(g) != uc.class_of(fn):
                    problems.append(f"calls: {c.function} is a member of another class")
                    continue
                callee = f"ut_obj.{g.qualified_name.split('::')[-1]}"
            elif g.is_static and g.from_frontend:
                callee = "ut_static_" + re.sub(r"\W", "_", g.name.replace("(anonymous namespace)::", ""))
            else:
                callee = g.qualified_name.replace("(anonymous namespace)::", "")
            given = by_param(c, list(g.params))
            if given is None:
                continue
            args = []
            for i, (x, p) in enumerate(zip(given, g.params)):           # the same form as the call under test's
                where = f"calls {c.function}.{p.name}"
                if not lower_arg(x, p, f"ut_c{len(planned.calls_before)}_{i}", where):
                    args.append(None)
                elif p.kind in ("int", "enum", "bool", "float"):
                    args.append(typed(where, x.value, p))
                else:                                              # any other: one C expression at least
                    v = fix_value(x.value)
                    problem = uc.c_expression_problem(v) if v else "no value"
                    if problem:
                        problems.append(f"{where}: {v[:40]!r} is {problem}" if v else f"{where}: no value")
                    args.append(None if problem else v)
            if any(x is None for x in args):
                continue
            planned.calls_before.append(f"{callee}({', '.join(args)});")
        by_stub = {s.function: s for s in stubs} | {s.qualified_name: s for s in stubs if s.qualified_name}
        for w in planned.writes:
            spec = next((s for s in stubs if (s.qualified_name or s.function) == w.stub and
                         any(pn == w.param for _, pn in s.params)), None) or by_stub.get(w.stub)
            ptype = next((pt for pt, pn in (spec.params if spec else []) if pn == w.param), None)
            if spec is None or ptype is None or not stub_output(ptype):
                problems.append(f"writes: {w.stub}.{w.param} is not an output parameter of STUB OUTPUTS")
                continue
            ctrl = f"ut_stub_{uc.stub_base(spec)}_{w.param}_out"
            base = stub_pointee(ptype)
            if w.elements:
                given = all_typed(f"writes {w.stub}.{w.param}", list(w.elements[:256]), target_param("", base, layout))
                if given is None:
                    continue
                stmts.append(f"{{ {ctrl}_t ut_w[] = {{{', '.join(given)}}}; memcpy({ctrl}, ut_w, sizeof ut_w); "
                             f"{ctrl}_len = (int)sizeof ut_w; }}")
            elif w.fields:
                rows = fields_of("ut_w", f"writes {w.stub}.{w.param}", w.fields, record_fields(base, layout))
                if rows is None:
                    continue
                stmts.append(f"{{ {ctrl}_t ut_w = {{{'' if cpp else '0'}}}; " + " ".join(rows)
                             + f" memcpy({ctrl}, &ut_w, sizeof ut_w); {ctrl}_len = (int)sizeof ut_w; }}")
        variables = {s.function: s for s in stubs if s.kind == "variable"} | {
            s.qualified_name: s for s in stubs if s.kind == "variable" and s.qualified_name}
        for v in planned.variables:                       # a stub variable: its full name and the value its type takes
            spec = variables.get(v.name)
            if spec is None:
                continue
            v.name = spec.qualified_name or spec.function
            value = typed(f"variables {v.name}", v.value, target_param("", re.sub(
                r"\b" + re.escape(spec.function.split("::")[-1]) + r"\b.*$", "", spec.var_type).strip(), layout))
            if value is not None:
                v.value = value
        word = uc.ParamInfo(name="", type="uint32_t", kind="int")              # a 32-bit register [A3-HW1]
        for r in planned.registers:
            given = all_typed("registers", [r.address, r.value], word)
            if given is not None:
                stmts.append(f"UT_REG32({given[0]}) = {given[1]};")
        cls = uc.class_of(fn)
        if planned.members and fn.member and fn.qualified_name.split("::")[-1] == cls.split("::")[-1]:
            problems.append("members: the constructor makes the object - there is no object to set before the call; "
                            "check its members after the call in expected_after")
            planned.members = []
        for m in planned.members:
            info = next((x for x in layout.class_members if x.name == m.member and
                         x.class_name.replace("(anonymous namespace)::", "") == cls), None)
            if info is None or not fn.member:
                problems.append(f"members: {m.member} is not a member of the object")
                continue
            ref = f"*ut_member({uc.member_tag(info)}{{}})" if info.static else f"ut_obj.*ut_member({uc.member_tag(info)}{{}})"
            container = re.search(r"\bstd::(?:array|vector)\s*<\s*([^,>]+)", info.type)
            items = list(m.elements[:256]) or (as_list(m.value) if container else [])
            if container:
                given = all_typed(f"members {m.member}", items, target_param("", container.group(1), layout))
                if given is not None:
                    stmts.append(f"{ref} = {{{', '.join(given)}}};")
                continue
            if items and not m.value:                  # a scalar member takes its value, never a list
                m.value = items[0] if len(items) == 1 else ""
                if not m.value:
                    problems.append(f"members {m.member}: a {info.type} takes one value, not a list")
                    continue
            v = typed(f"members {m.member}", m.value, target_param("", info.type, layout))
            if v is not None:
                stmts.append(f"({ref}) = {v};")
        expected = planned.expected_return.strip()
        ret = target_param("", re.sub(r"\b(const|volatile|static|inline)\b", "", return_type_of(fn)).strip(), layout)
        if expected and ret.kind == "enum":            # its enumerator in full, as an argument's [A2-21ai]
            full = next((k for k in ret.enum_values if k.split("::")[-1] == expected.split("::")[-1]), None)
            if full and full != expected:
                planned.expected_return = full
                fixes.append(f"expected_return: {expected!r} -> {full!r} (the enumerator in full) [structured]")
        if decls or stmts:
            planned.setup = decls + list(planned.setup) + stmts
            fixes.append(f"structured: {len(decls) + len(stmts)} statement(s) written by the generator")
        planned.state, planned.calls, planned.writes, planned.registers, planned.members = [], [], [], [], []
        return fixes, problems

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
        form = uc.c_expression_problem(v, braces=p.kind not in ("int", "enum", "bool", "float"))
        if form:
            return f"{p.name}: {v!r} is {form}"
        t = re.sub(r"\b(const|volatile)\b", "", p.type).strip()
        base_ident = re.sub(r"^[&*\s]+", "", v).split("[")[0].split(".")[0].split("->")[0].strip()
        if p.kind in ("int", "enum", "bool"):
            if p.kind == "enum" and v.split("::")[-1] in {x.split("::")[-1] for x in p.enum_values}:
                return ""
            n = eval_int(v, constants)
            if n is None and re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?[fF]?", v) and float(v.rstrip("fF")).is_integer():
                n = int(float(v.rstrip("fF")))                          # 1.0 for an int: valid C [A1-PL4]
            if n is None:
                return f"{p.name}: {v!r} is not an integer value (literal, enumerator or macro)"
            if p.kind == "bool" and n not in (0, 1):
                return f"{p.name}: {v!r} is not a boolean"
            if p.kind == "enum":
                values = next((set(e.values.values()) for e in layout.enums
                               if e.name.split("::")[-1] == (p.base or t).split("::")[-1].replace("enum ", "")), set())
                if values and n not in values:
                    return f"{p.name}: {v} is not an enumerator of {p.type}" + self._state_hint(v, p, fn, layout)
            rng = int_range(t)
            if rng and not rng[0] <= n <= rng[1]:
                setter = self._accumulated(fn, layout).get(p.name, "")
                return f"{p.name}: {v} is outside the range of {p.type} ({rng[0]}..{rng[1]})" + (
                    f" - {fn.name} adds {p.name} to the state {setter}: set {setter} in state near the threshold "
                    f"and give {p.name} a value within {rng[0]}..{rng[1]} [A1-PL2b]" if setter else "")
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
            return f"{p.name}: {v!r} - {pointer_form(p)}" + (" or NULL" if p.nullable else "")
        unknown = [w for w in re.findall(r"\b([A-Za-z_]\w*)\b", v) if w not in known and w not in local and
                   w not in KEYWORDS and w not in constants]
        return f"{p.name}: unknown names in {v!r}: {', '.join(unknown[:4])}" if unknown else ""

    @staticmethod
    def _state_aliases(layout: uc.LayoutProfile) -> dict[str, str]:
        """A1-PL4, A2-21z: every name a brain may give a SUT state variable -> the suffix of its setter and getter:
        the suffix itself, the variable's name, its name without the unnamed namespace and its short name (only
        when one variable has it)."""
        out: dict[str, str] = {}
        shorts: dict[str, list[str]] = {}
        for v in uc.settable_state(layout):
            suffix = uc.state_setter_name(v)[len("ut_state_set_"):]
            plain = v.name.replace("(anonymous namespace)::", "")
            for name in (suffix, v.name, plain):
                out.setdefault(name, suffix)
            shorts.setdefault(plain.split("::")[-1], []).append(suffix)
        for short, suffixes in shorts.items():
            if len(set(suffixes)) == 1:
                out.setdefault(short, suffixes[0])
        return out

    @staticmethod
    def _fold_fields(planned: "PlannedScenario", params: dict[str, uc.ParamInfo]) -> list[str]:
        """A1-PL4: fields of a structure parameter given as arguments of their own (p->f, p.f) - one variable of
        the structure in the set-up, its fields assigned there, the variable (or its address) as the argument."""
        fields: dict[str, list[tuple[str, str]]] = {}
        keep = []
        for a in planned.args:
            m = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*(?:->|\.)\s*([A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*)\s*", a.name)
            if m and m.group(1) in params and not any(x.name == m.group(1) for x in planned.args):
                fields.setdefault(m.group(1), []).append((re.sub(r"\s*(?:->|\.)\s*", ".", m.group(2)), a.value))
            else:
                keep.append(a)
        if not fields:
            return []
        out = []
        for name, values in fields.items():
            p = params[name]
            var = name if name in setup_locals(planned.setup) else f"{name}_v"
            if var != name:
                base = re.sub(r"\b(const|volatile)\b", "", p.base or p.type).replace("*", "").replace("&", "").strip()
                planned.setup.append(f"{base} {var} = {{0}};")
            planned.setup += [f"{var}.{field} = {value};" for field, value in values]
            pointer = p.kind == "ptr_struct" or "*" in p.type
            keep.append(type(planned.args[0])(name=name, value=("&" + var) if pointer else var))
            out.append(f"args: fields of {name} ({', '.join(f for f, _ in values)}) set in the set-up of {var}")
        planned.args = keep
        return out

    @staticmethod
    def _normalize_planned(planned: "PlannedScenario", fn: uc.FunctionInfo, open_ids: set[str],
                           evidence: dict[str, uc.EvidenceRef], layout: uc.LayoutProfile | None = None,
                           internal: dict[str, str] | None = None, known: set[str] | None = None) -> list[str]:
        """ARCH-20, A1-PL4: format noise removed from values and gap ids; returns "field: 'before' -> 'after'".
        ``internal``: the values of the names only the SUT file defines (A1-PL2a); ``known``: the names a test can
        use, so that an undeclared pointer argument is told from a global the headers declare (A1-PL4)."""
        fixes: list[str] = []
        if internal:                                     # the test vocabulary: a name only the SUT sees -> its value
            def vocab(field: str, value: str) -> str:
                new = uc.with_values(value, internal)
                if new != value:
                    fixes.append(f"{field}: {value[:60]!r} -> {new[:60]!r} (only the SUT file defines it) [A1-PL2a]")
                return new
            for a in planned.args:
                a.value = vocab(f"args.{a.name}", a.value)
            for v in planned.variables:
                v.value = vocab(f"variables.{v.name}", v.value)
            for st in planned.stubs:
                st.returns = [vocab(f"stubs.{st.stub}", r) for r in st.returns]
            planned.setup = [vocab("setup", s) for s in planned.setup]
            planned.calls_before = [vocab("calls_before", s) for s in planned.calls_before or []]
            planned.expected_return = vocab("expected_return", planned.expected_return)
            for c in planned.expected_after:
                c.value = vocab(f"expected_after.{c.expr}", c.value)
        if layout is not None:                       # a setter for a visible variable, an assignment of a hidden one
            state = {uc.state_setter_name(v)[len("ut_state_set_"):]: v.name for v in uc.settable_state(layout)}
            alias = AnalyzerAgent._state_aliases(layout)                                      # A1-PL4: by its names
            visible = uc.headers_text(layout)

            def helpers(text: str) -> str:
                """ut_state_set_/ut_state_get_ of a variable named by another of its names -> the real helper."""
                def one(m: "re.Match[str]") -> str:
                    real = alias.get(m.group(2))
                    return f"ut_state_{m.group(1)}_{real}" if real and m.group(2) not in state else m.group(0)
                return re.sub(r"\but_state_(set|get)_(\w+)\b", one, text)
            setup = []
            for stmt in planned.setup:
                new = helpers(stmt)
                m = re.fullmatch(r"\s*ut_state_set_(\w+)\s*\((.*)\)\s*;?\s*", new, re.S)
                if m and m.group(1) not in state and re.search(r"\b" + re.escape(m.group(1)) + r"\b", visible):
                    new = f"{m.group(1)} = {m.group(2).strip()};"
                m = re.fullmatch(r"\s*([A-Za-z_][\w:]*)\s*=(?!=)\s*([^;]+);?\s*", new)
                if m and alias.get(m.group(1)) and not re.search(r"\b" + re.escape(m.group(1)) + r"\b", visible):
                    new = f"ut_state_set_{alias[m.group(1)]}({m.group(2).strip()});"
                if new != stmt:
                    fixes.append(f"setup: {stmt.strip()[:60]!r} -> {new[:60]!r}")
                setup.append(new)
            planned.setup = setup
            calls = [helpers(c) for c in planned.calls_before or []]
            if calls != list(planned.calls_before or []):
                fixes.append("calls_before: state helpers named by the variable's other name")
                planned.calls_before = calls
            cls = uc.class_of(fn) if fn.member else ""
            members = {m.name: m for m in layout.class_members
                       if cls and m.class_name.replace("(anonymous namespace)::", "") == cls}
            for c in planned.expected_after:                  # a state variable read through its getter [A2-21z]
                new = helpers(c.expr)
                name = new.strip()
                if alias.get(name) and not re.search(r"\b" + re.escape(name) + r"\b", visible):
                    new = f"ut_state_get_{alias[name]}()"
                def accessor(m: "re.Match[str]") -> str:          # a member of the object, private ones too,
                    info = members.get(m.group(1))                   # read through the generated accessor
                    if info is None:
                        return m.group(0)
                    tag = f"{uc.member_tag(info)}{{}}"
                    return f"(*ut_member({tag}))" if info.static else f"(ut_obj.*ut_member({tag}))"
                if members:                       # count_, violations_ == 0, ut_obj.count_, this->count_
                    new = re.sub(r"(?:\but_obj\s*\.\s*|\bthis\s*->\s*|(?<![\w.>:]))([A-Za-z_]\w*)\b(?!\s*\()",
                                 accessor, new)
                if new != c.expr:
                    fixes.append(f"expected_after: {c.expr[:60]!r} -> {new[:60]!r}")
                    c.expr = new
        params = {p.name: p for p in text_params(fn)}
        fixes += AnalyzerAgent._fold_fields(planned, params)                                  # A1-PL4
        given = {a.name: a.value for a in planned.args}
        named = planned.expected_return.strip()
        if named in params and given.get(named, "").strip():        # "returns request": the value given for it
            planned.expected_return = given[named]
            fixes.append(f"expected_return: {named!r} -> {given[named]!r} (the value of the parameter)")
        literals = dict(re.findall(r"(?:^|;)\s*(?:(?:const|unsigned|signed|static)\s+)*[A-Za-z_][\w:]*\s+([A-Za-z_]\w*)\s*="
                                   r"\s*([+-]?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?)[uUlLfF]*)\s*(?=;)",
                                   ";".join(planned.setup)))
        for st in planned.stubs:                        # a stub value named by a set-up variable: its literal
            values = [literals.get(r.strip(), r) for r in st.returns]
            if values != st.returns:
                fixes.append(f"stubs.{st.stub}: {st.returns!r} -> {values!r} (the literals of the set-up)")
                st.returns = values
        strings = {p.name for p in text_params(fn) if p.kind == "cstring"}

        def fixed(field: str, value: str, name: str = "", keep: bool = False) -> str:
            new = fix_value(value, name, keep)
            if new != value and new:
                fixes.append(f"{field}: {value!r} -> {new!r}")
                return new
            return value
        for a in planned.args:
            a.value = fixed(f"args.{a.name}", a.value, a.name, a.name in strings)
        fixes += AnalyzerAgent._pointer_forms(planned, params, known)                           # A1-PL4
        for v in planned.variables:
            v.value = fixed(f"variables.{v.name}", v.value, v.name)
        for st in planned.stubs:
            st.returns = [fixed(f"stubs.{st.stub}", r) for r in st.returns]
        if planned.expected_return.strip():
            planned.expected_return = fixed("expected_return", planned.expected_return, "",
                                            bool(re.search(r"char\s*\*", return_type_of(fn) or "")))
        for c in planned.expected_after:
            c.value = fixed(f"expected_after.{c.expr}", c.value)
        ids = []
        for g in planned.gap_ids:
            new = fix_gap_id(g, open_ids, fn.name, set(evidence))
            if new != g:
                fixes.append(f"gap_ids: {g[:60]!r} -> {new!r}")
            if new not in ids:
                ids.append(new)
        planned.gap_ids = ids
        return fixes

    @staticmethod
    def _pointer_forms(planned: "PlannedScenario", params: dict[str, uc.ParamInfo],
                       known: set[str] | None = None) -> list[str]:
        """A1-PL4: the address of an array declared in the set-up passed for a pointer is the array (&buf -> buf);
        a name the set-up does not declare (ut_v0, copied from another test) is declared at the start of the set-up -
        a zeroed buffer of the pointee type, a zeroed structure by its address, or a scalar assigned in the set-up."""
        fixes: list[str] = []
        arrays = set()
        for stmt in planned.setup:
            for part in stmt.split(";"):
                m = re.match(r"\s*(?:(?:const|static|volatile|unsigned|signed|struct|union|enum)\s+)*[A-Za-z_][\w:]*"
                             r"(?:\s+(?:int|long|short|char))*\s+([A-Za-z_]\w*)\s*\[", part)
                if m:
                    arrays.add(m.group(1))
        declared = setup_locals(planned.setup) | arrays
        decls: list[str] = []
        for a in planned.args:
            p = params.get(a.name)
            if p is None or p.by_ref or p.kind not in ("ptr_scalar", "buffer", "ptr_struct", "ptr_void"):
                continue
            v = a.value.strip()
            m = re.fullmatch(r"&\s*\(?\s*([A-Za-z_]\w*)\s*\)?(?:\s*\[\s*0\s*\])?", v)
            if m and m.group(1) in arrays and not v.endswith("]"):
                a.value = m.group(1)
                fixes.append(f"args.{a.name}: {v!r} -> {a.value!r} (an array is passed by its name) [A1-PL4]")
                continue
            m = re.fullmatch(r"(&?)\s*([A-Za-z_]\w*)", v)
            if not m or v in NULLS or m.group(2) in declared or m.group(2) in (known or set()) or \
                    m.group(2) in KEYWORDS or m.group(2) in params:
                continue
            name, base = m.group(2), pointee(p)
            if not base:
                continue
            scalar = any(re.match(r"\s*" + re.escape(name) + r"\s*=(?!=)", s) for s in planned.setup)
            if p.kind == "ptr_struct":
                decl, value = f"{base} {name} = {{0}};", "&" + name
            elif scalar:
                decl, value = f"{base} {name} = 0;", "&" + name
            else:
                decl, value = f"{base} {name}[16] = {{0}};", name
            decls.append(decl)
            declared.add(name)
            if value != v:
                a.value = value
            fixes.append(f"args.{a.name}: {v!r} was not declared - '{decl}' added to the set-up, passed as "
                         f"{value!r} [A1-PL4]")
        if decls:
            planned.setup = decls + list(planned.setup)
        return fixes

    def _check_planned(self, planned: "PlannedScenario", fn: uc.FunctionInfo, open_ids: set[str],
                       infeasible: set[str], evidence: dict[str, uc.EvidenceRef], stubs: list[uc.StubSpec], constants: dict[str, int],
                       known: set[str], layout: uc.LayoutProfile) -> tuple[uc.Scenario | None, list[str]]:
        """A1-PL4: every check, deterministically, before Agent2 receives the scenario. The format fixes of ARCH-20
        are applied first and recorded in ``self.last_fixes``."""
        fn = uc.instantiated(fn, layout)          # a method of a class template: its parameters as tests call them
        lowered, structured_problems = self._lower_structured(planned, fn, layout, stubs, constants,
                                                              known)                    # the brain said what
        self.last_fixes = lowered + self._normalize_planned(planned, fn, open_ids, evidence, layout,
                                                            self._internal_values(layout), known)  # A1-PL2a, A1-PL4
        reasons: list[str] = list(structured_problems)
        claim = WAY_OUT.search(" ".join([planned.name, planned.purpose, planned.information_sought, planned.conflict]))
        if claim:                                                                          # COV-5
            return None, [f"claims the gap cannot or need not be covered ('{claim.group(0)}'); ignored [COV-5]"]
        gaps = [g for g in planned.gap_ids if g in open_ids]                               # a)
        if not gaps:
            reasons.append("names no open gap of this function: " + ", ".join(planned.gap_ids[:4]))
        unknown_ev = [e for e in planned.evidence_ids if e not in evidence]                # b)
        dropped_quotes = [q for q in planned.quotes if evidence.get(q.evidence_id) is None or not norm_text(q.text)
                          or norm_text(q.text) not in norm_text(evidence[q.evidence_id].text)]
        if unknown_ev or dropped_quotes:         # what rests on them is not asserted; the inputs stay [A1-PL4]
            planned.evidence_ids = [e for e in planned.evidence_ids if e in evidence]
            planned.quotes = [q for q in planned.quotes if q not in dropped_quotes]
            self.last_fixes.append("evidence not usable, left out - the result is observed unless other quoted "
                                   "evidence states it: " + "; ".join(
                                       [f"unknown id {e}" for e in unknown_ev[:3]] +
                                       [f"the quote of {q.evidence_id or '?'} does not occur word for word"
                                        for q in dropped_quotes[:3]]) + " [A1-PL4]")
        recipe = uc.object_recipe(fn, layout)                         # the object of a member function [A2-21ae]
        if recipe is not None:               # the object exists already: a second declaration is form, not content
            declared = set().union(*(uc.declared_names(x) for x in recipe[0]))
            planned.setup, gone = uc.without_redeclarations(planned.setup, declared)
            if gone:
                self.last_fixes.append(f"setup: {'; '.join(gone)[:160]!r} declared the object again - dropped, the "
                                       "test declares it once [ARCH-20, A2-21ae]")
        local = setup_locals(planned.setup)
        if recipe is not None:
            local |= setup_locals(recipe[0]) | {"ut_obj"} | {f"ut_dep{i}" for i in range(8)}
        if len(planned.setup) > 40:
            reasons.append("setup has more than 40 statements")
        sut_names = {f.name.split("::")[-1] for f in layout.functions} | {
            "ut_static_" + re.sub(r"\W", "_", f.name.replace("(anonymous namespace)::", ""))
            for f in layout.functions if f.is_static and f.from_frontend}                   # structured calls
        for stmt in planned.calls_before[:12]:                                     # BP-2 call_sequence
            if PLAN_FORBIDDEN.search(stmt):
                reasons.append(f"setup statement not allowed: {stmt[:60]}")
            elif not any(re.search(r"\b" + re.escape(n) + r"\s*\(", stmt) for n in sut_names):
                reasons.append(f"an earlier call is not a call of the SUT: {stmt[:60]} [BP-2]")
            else:
                missing = self._calls_known(stmt, known | local)
                named = uc.name_problem("", set(missing), known)                         # a guard [A2-21ai]
                if named:
                    reasons.append(f"setup {named}")
                elif missing:
                    reasons.append(f"setup calls functions that do not exist here: {', '.join(missing[:4])}")
        if len(planned.calls_before) > 12:
            reasons.append("more than 12 earlier calls")
        for stmt in planned.setup:                                                         # grounding
            if PLAN_FORBIDDEN.search(stmt):
                reasons.append(f"setup statement not allowed: {stmt[:60]}")
            elif uc.c_form_problem(stmt):
                reasons.append(f"setup statement {uc.c_form_problem(stmt)}: {stmt[:60]}")
            else:
                missing = self._calls_known(stmt, known | local)
                named = uc.name_problem("", set(missing), known)                         # a guard [A2-21ai]
                if named:
                    reasons.append(f"setup {named}")
                elif missing:
                    reasons.append(f"setup calls functions that do not exist here: {', '.join(missing[:4])}")
        by_name = {p.name: p for p in text_params(fn)}
        given = {a.name: a.value for a in planned.args}
        if set(given) != set(by_name):                    # a method's parameters are facts as much as a function's
            reasons.append(f"args must name every parameter exactly once: {', '.join(by_name) or 'none'}")
        for name, value in given.items():                                                  # e)
            if name in by_name:
                problem = self._value_problem(by_name[name], value, fn, constants, local, known, layout)
                if problem:
                    reasons.append(problem)
        stub_specs = uc.stubs_called(fn, layout, stubs)                 # the stubs it calls, by the names that pass
        void = [sv.stub for sv in planned.stubs if sv.stub in stub_specs and stub_specs[sv.stub].kind != "variable" and (
            stub_specs[sv.stub].kind == "constructor" or re.sub(r"\b(const|volatile|static|inline|extern|virtual)\b", "",
                                                                 stub_specs[sv.stub].return_type).strip() in ("", "void"))]
        if void:                       # nothing to set: the entry carries no information - form, not content [ARCH-20]
            planned.stubs = [sv for sv in planned.stubs if sv.stub not in void]
            self.last_fixes.append(f"stubs: {', '.join(void)} return nothing - their entries dropped [A1-PL4]")
        for sv in planned.stubs:                                                           # d)
            spec = stub_specs.get(sv.stub)
            if spec is None:
                reasons.append(f"{sv.stub} is not a stub this function calls (STUBS: "
                               f"{', '.join(sorted({s.qualified_name or s.function for s in stub_specs.values()}))[:200] or 'none'})")
                continue
            ret = re.sub(r"\b(const|volatile|static|inline|extern|virtual)\b", "", spec.return_type).strip()
            if not ret or ret == "void" or spec.kind == "constructor":
                reasons.append(f"{sv.stub} returns nothing that a test can set")
                continue
            if not sv.returns or len(sv.returns) > 16:
                reasons.append(f"{sv.stub}: give 1 to 16 return values")
                continue
            ret = layout.typedefs.get(ret, ret) if int_range(ret) is None else ret          # TickType_t [A1-PL4]
            ret = re.sub(r"\b(const|volatile)\b", "", ret).strip()
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
        if reasons and not uc.only_predictions(reasons):         # a guard refused it [NC-6]
            return None, reasons
        trial = list(reasons)                                     # only predictions: the tool decides [NC-7]
        cited = [evidence[e] for e in planned.evidence_ids if e in evidence]
        strong = [e for e in cited if e.evidence_class != "E8"]
        notes = []
        exception = planned.expected_exception.strip()
        if exception:
            sut_lines = uc.read_source(Path(layout.repo_root) / layout.sut_file).splitlines()
            code = "\n".join(sut_lines[fn.line_start - 1: fn.line_end] + [
                "\n".join(sut_lines[g.line_start - 1: g.line_end]) for g in layout.functions if g.name in fn.calls])
            thrown = thrown_types(code)
            if exception not in thrown and exception.split("::")[-1] not in {t.split("::")[-1] for t in thrown}:
                notes.append(f"expected_exception {exception!r} left out: {fn.name} throws "
                             f"{', '.join(thrown) or 'nothing'} [A1-PL4]")
                exception = ""
            elif planned.expected_return.strip():
                notes.append("expected_return left out: a call that throws returns nothing [A1-PL4]")
                planned.expected_return = ""
        normative = planned.classification == "confirmed_gap" and bool(strong) and \
            bool(planned.expected_return.strip() or planned.expected_after or exception) and bool(planned.quotes)  # c)
        if planned.classification == "confirmed_gap" and not normative:
            notes.append("downgraded to characterization: no quoted E1-E7 evidence or no expected value [A1-PL4 c]")
        if normative:
            problem = self._expectation_problem(planned, fn, constants, local, known, layout)
            if problem:              # the inputs stay, the result is observed instead of asserted [A1-PL4]
                normative = False
                notes.append(f"expectation not usable ({problem}): kept as a characterization scenario, its result "
                             "is observed [A1-PL4]")
        linked = bool(strong) and all(e.linked_by for e in strong)       # rests only on brain links
        scenario = uc.Scenario(
            scenario_id="", function=fn.name, gap_ids=gaps, description=planned.purpose[:300] or "planned by the brain",
            purpose=planned.purpose[:200], test_name=planned.name, args=[uc.NamedValue(name=a.name, value=a.value)
                                                                         for a in planned.args],
            setup=list(planned.setup), stub_values=[uc.StubValues(stub=s.stub, returns=list(s.returns))
                                                    for s in planned.stubs],
            variables=[uc.NamedValue(name=v.name, value=v.value) for v in planned.variables],
            expected_return=planned.expected_return.strip() if normative else "",
            expected_exception=exception if normative else "",
            expected_after=[uc.ExpectedCheck(expr=c.expr, value=c.value) for c in planned.expected_after] if normative
            else [], quotes=[uc.Quote(evidence_id=q.evidence_id, text=q.text) for q in planned.quotes],
            evidence=cited if normative else [], strongest_class=min((e.evidence_class for e in strong), default="E8")
            if normative else "E8", characterization=not normative, checked=True,
            classification="confirmed_gap" if normative else (
                planned.classification if planned.classification != "confirmed_gap" else "characterization_gap"),
            expected="as stated by the evidence" if normative else "observe it", linked=linked and normative,
            calls_before=[s.strip() if s.strip().endswith((";", "}")) else s.strip() + ";"
                          for s in planned.calls_before if s.strip()],                         # BP-2
            object_args=list(planned.object_args), trial=trial)                                # NC-7
        return scenario, notes

    def _expectation_problem(self, planned: "PlannedScenario", fn: uc.FunctionInfo, constants: dict[str, int],
                             local: set[str], known: set[str], layout: uc.LayoutProfile) -> str:
        """A1-PL4 c): why the expected return value or an expected check cannot be asserted; "" when they can."""
        full = return_type_of(fn)
        ret = re.sub(r"\b(const|volatile|static|inline)\b", "", full).strip()
        enumerators = {k.split("::")[-1] for e in layout.enums for k in e.values}
        if planned.expected_return.strip():
            if not ret or ret == "void":
                return f"{fn.name} returns nothing; expected_return must be empty"
            kind = "float" if re.fullmatch(r"(float|double|long double)", ret) else \
                "cstring" if re.fullmatch(r"(const\s+)?char\s*\*", full.strip()) else \
                "other" if ret.endswith("*") else "int"
            if not (kind == "int" and planned.expected_return.strip().split("::")[-1] in enumerators):
                problem = self._value_problem(uc.ParamInfo(name="return value", type=ret, kind=kind, nullable=True),
                                              planned.expected_return, fn, constants, local, known, layout)
                if problem:
                    return problem
        for check in planned.expected_after:
            names = set(re.findall(r"\b([A-Za-z_]\w*)\b", check.expr)) - KEYWORDS
            if not names or any(n not in local and n not in known for n in names):
                return f"expected_after: {check.expr!r} is not a variable of the setup or a stub control"
            value = check.value.strip()
            if eval_int(value, constants) is None and not re.fullmatch(
                    r"[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?[fF]?", value) and not string_literal(value) \
                    and value.split("::")[-1] not in enumerators:                  # MonitorState::Ok is a value
                return f"expected_after: {check.value!r} is not a value"
        return ""

    @staticmethod
    def _expected_form(fn: uc.FunctionInfo, open_ids: set[str]) -> str:
        """ARCH-20: the re-ask shows the form each field is expected in, not only what was wrong."""
        params = text_params(fn)
        args = ", ".join('{"name": "%s", "value": "1"}' % p.name for p in params[:3]) or "(no parameters)"
        return ("Expected form: every value exactly as C code, for example " + args + " - a number, an enumerator or "
                "a macro; a buffer or pointer parameter as elements, a structure as fields; SUT state as state "
                "entries; never a declaration, an address (&), a ':' or '=' before a value, the parameter name, a "
                "file path or a placeholder. targets: only the ids of TARGETS, for example \"T1\" - never line "
                "numbers or item ids.")

    def _suggested_values(self, fn: uc.FunctionInfo, sut_text: str, stubs: list[uc.StubSpec],
                          constants: dict[str, int] | None = None,
                          internal: dict[str, str] | None = None) -> dict[str, list[str]]:
        """A1-PL2: per parameter and stub, the values the function itself decides on (literals, enumerators and
        macros compared with it, and the values next to them); 0 and 1 for a value used as true or false."""
        body = strip_comments("\n".join(sut_text.splitlines()[fn.line_start - 1: fn.line_end]))
        head, _, code = body.partition("{")
        names = {p.name: [p.name] for p in text_params(fn)}
        for p in text_params(fn):                                        # fields of a struct or pointer parameter
            used = {m.group(2) for m in re.finditer(r"\b" + re.escape(p.name) + r"\s*(->|\.)\s*([A-Za-z_]\w*)", code)}
            for field in sorted({f.name for f in p.fields[:16]} | used):
                names[f"{p.name}->{field}" if "*" in p.type or "->" in code else f"{p.name}.{field}"] = [
                    re.escape(p.name) + r"\s*(?:->|\.)\s*" + re.escape(field)]
        for st in stubs:
            if st.kind == "variable" or not st.return_type or st.return_type.strip() in ("void", ""):
                continue
            short = st.function.split("::")[-1]
            holders = re.findall(r"\b([A-Za-z_]\w*)\s*=\s*(?:\([^()]*\)\s*)?" + re.escape(short) + r"\s*\(", code)
            names[f"{st.function}()"] = holders + [short + r"\s*\([^()]*\)"]
        out: dict[str, list[str]] = {}
        op = r"(==|!=|<=|>=|<|>)"
        value = r"([-+]?(?:0[xX][0-9a-fA-F]+|\d+)[uUlL]*|'[^']'|[A-Za-z_]\w*(?:::\w+)*)"
        for key, patterns in names.items():
            found: list[str] = []
            for pat in patterns:
                ident = pat if ("(" in pat or "\\" in pat) else re.escape(pat)
                for m in re.finditer(r"\b" + ident + r"\s*" + op + r"\s*" + value, code):
                    found.append(m.group(2) + ("|edge" if m.group(1) in "<><=>=" else ""))
                for m in re.finditer(value + r"\s*" + op + r"\s*" + (r"\b" if not ident.startswith("\\") else "")
                                     + ident + r"\b", code):
                    found.append(m.group(1) + ("|edge" if m.group(2) in "<><=>=" else ""))
                for m in re.finditer(r"\b" + ident + r"\s*&\s*" + value, code):
                    found += ["0", m.group(1)]
                truth = [m for m in re.finditer(r"(?:\|\||&&|!|\(|\?)\s*!?\s*" + ident + r"\s*(?:\|\||&&|\)|\?)", code)
                         if not re.search(r"\b(switch|sizeof|return)\s*$", code[:m.start()])]
                if truth or re.search(r"(?:\|\||&&)\s*!?\s*" + ident + r"\b", code):
                    found += ["0", "1"]                                  # used as true or false
            values: list[str] = []
            for f in found:
                v, edge = f.split("|")[0], f.endswith("|edge")
                if v in names or v in KEYWORDS or (re.fullmatch(r"[A-Za-z_][\w:]*", v) and constants is not None
                                                     and v.split("::")[-1] not in constants):
                    continue                                   # only literals, enumerators and macros
                v = (internal or {}).get(v.split("::")[-1], v)       # in the test vocabulary [A1-PL2a]
                more = [v]
                number = re.fullmatch(r"([-+]?\d+)[uUlL]*", v)
                if edge and number:
                    n = int(number.group(1))
                    more = [str(n - 1), str(n), str(n + 1)]
                for x in more:
                    if x not in values:
                        values.append(x)
            if values:
                out[key] = values[:8]
        return out

    def _facts(self, fn: uc.FunctionInfo, gaps: list[uc.GapItem], open_ids: set[str], layout: uc.LayoutProfile,
               req: uc.PlanRequest, sut_text: str, stubs: list[uc.StubSpec], evidence: list[uc.EvidenceRef],
               profile: uc.BrainProfile | None = None, sequence: set[str] | frozenset = frozenset(),
               targets: list[Target] | None = None, structured: dict[str, Any] | None = None) -> list[ba.ContextPart]:
        """A1-PL2 (facts first) and A1-PL12 (what was tried). ``profile``: the answering brain's limits (BP-5);
        ``sequence``: gaps whose next attempt is a call sequence (BP-2, BP-3); ``targets``: the open items grouped
        by their path (A1-PL3a) - the brain names these, never the items (ARCH-20d)."""
        lines = sut_text.splitlines()
        root = Path(layout.repo_root)
        source = self._source_for_brain(fn, lines, [g.line for g in gaps], profile)              # LA-10, BP-5
        params = []
        internal = self._internal_values(layout)                                               # A1-PL2a
        suggested = self._suggested_values(fn, sut_text, stubs, self._constants_for_values(layout), internal)
        accumulated = self._accumulated(fn, layout)                                            # A1-PL2b
        for p in text_params(fn):
            extra = []
            if p.enum_values:
                extra.append("enumerators " + ", ".join(p.enum_values[:20]))
            if p.fields:
                extra.append("fields " + ", ".join(f"{f.name} ({f.type})" for f in p.fields[:12]))
            if p.nullable:
                extra.append("may be NULL")
            if p.kind in ("ptr_scalar", "ptr_struct", "buffer", "struct", "ptr_void") or p.by_ref:
                extra.append(pointer_form(p))                                                   # A1-PL2
            if p.name in accumulated:
                extra.append(f"added to the SUT state {accumulated[p.name]} on every call: a total beyond its range "
                             f"is set in state")                                                # A1-PL2b
            # "name (type)", never "name: type" - a smaller brain copies that form into the value [ARCH-20]
            params.append(f"- {p.name} (type {p.type}, {p.kind}{'; ' + '; '.join(extra) if extra else ''})"
                          + (f" - suggested values: {', '.join(suggested[p.name])}" if p.name in suggested else ""))
            params += [f"  - field {key} - suggested values: {', '.join(values)}" for key, values in suggested.items()
                       if key.startswith((p.name + "->", p.name + "."))]
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
        if targets is not None:                                                                # A1-PL3a, ARCH-20d
            gap_rows = []
            for tg in targets:
                if tg.tid.startswith("EVIDENCE:"):
                    if tg.tid in open_ids:
                        gap_rows.append(f"- {tg.tid}: check the behaviour the EVIDENCE states (results, error values, "
                                        "limits) with asserted expected values")
                    continue
                if not set(tg.items) & open_ids:
                    continue
                first = lines[tg.first - 1].strip() if 0 < tg.first <= len(lines) else ""
                last = lines[tg.last - 1].strip() if tg.last > tg.first and tg.last <= len(lines) else ""
                tried = max((req.attempts.get(i, 0) for i in tg.items), default=0)
                where = (f"line {tg.first}" if tg.first == tg.last else f"lines {tg.first}-{tg.last}") + \
                    (f" of {tg.function}" if tg.function != fn.name else "")
                gap_rows.append(f"- {tg.tid} [{', '.join(f'{m} {n}' for m, n in tg.counts.items())}] {where}: `{first}`"
                                + (f" .. `{last}`" if last else "")
                                + (f" | needs: {'; '.join(tg.needs)}" if tg.needs else "")
                                + (f" | PATH CONDITIONS: {' && '.join(tg.conditions)}" if tg.conditions else "")
                                + (f" | STATE TO SET: {'; '.join(sets)}" if (sets := self._state_settings(
                                    tg.conditions, fn, layout)) else "")                        # A1-PL2b
                                + (f" | open: {'; '.join(tg.details)}" if tg.details else "")
                                + (f" | TRIED {tried} time(s) without reaching it" if tried else ""))
        called = list({id(s): s for s in uc.stubs_called(fn, layout, stubs).values()}.values())
        stub_rows = [f"- {s.qualified_name or s.function} - {s.declaration or s.return_type + ' ' + s.function + '(...)'}"
                     + (f" - suggested return values: {', '.join(suggested[s.function + '()'])}"
                        if s.function + "()" in suggested else "") for s in called]
        var_rows = [f"- {s.function}: {s.var_type}" for s in stubs if s.kind == "variable" and not s.is_const]
        controls = uc.project_controls(fn, layout)                                          # A2-21ak
        project = ("PROJECT STUB CONTROLS (the project's own stubs of the hardware this code calls: call them in setup "
                   "to set what those functions return; without them they return their defaults):\n"
                   + "\n".join(f"- {c.declaration};  ({ps.file})" for ps, c in controls)) if controls else ""
        real = [d.name for d in (layout.dependencies.entries if layout.dependencies else []) if d.decision != "stub"]
        # A1-PL2: what a test can set besides the stubs - globals the headers declare, the SUT's state setters
        stubbed_vars = {s.function for s in stubs if s.kind == "variable"}
        globals_ = sorted({m.group(2) for m in re.finditer(
            r"^\s*extern\s+(?!.*\bconst\b)([^;()]*?)\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*;",
            "\n".join(self._dependency_headers_text(layout)), re.M)} - stubbed_vars)
        listed, rest = uc.function_state(fn, layout, sut_text.splitlines(), self.cfg.state_per_function)  # A2-21aj
        setters = [f"{uc.state_setter_name(v)}(value) sets and {uc.state_getter_name(v)}() reads "
                   f"{v.name.replace('(anonymous namespace)::', '')} ({v.type})" for v in listed]
        if rest:
            setters.append("also used (the same two helpers exist for each): " + ", ".join(
                v.name.replace("(anonymous namespace)::", "") for v in rest[:40]))
        outside = sorted({g.line for g in gaps if g.line and not fn.line_start <= g.line <= fn.line_end})
        callees = [f for f in layout.functions if f is not fn and any(f.line_start <= n <= f.line_end for n in outside)]
        ev_rows = [f"- {e.evidence_id} {e.evidence_class}{' (linked by the brain)' if e.linked_by else ''} "
                   f"{e.source}:{e.line}: {e.text[:400]}" for e in evidence]
        # A1-PL7/A1-PL12: the earlier tests of this function and what each reached
        tried_rows = []
        suite = uc.test_sources(req.existing_suite) if req.existing_suite else {}
        status = {t.name: t.status for t in (req.last.tests if req.last else [])}
        short = fn.name.split("::")[-1]
        # REC-12: a growing suite must not hide the latest failed/state-building attempt behind its first tests.
        recent = list(reversed(list(suite.items())))
        recent.sort(key=lambda pair: pair[0] not in req.failed_tests and status.get(pair[0]) not in
                    ("failed", "crashed", "timeout"))
        targets_left = {uc.gap_item(g) for g in gaps}
        for name, src in recent:
            if not re.search(r"\b" + re.escape(short) + r"\s*\(", src):
                continue
            reached = items_in(req.test_items.get(name, []), fn.line_start, fn.line_end)
            measured = name in req.test_items
            reached_text = ', '.join(reached[:12]) or ('none' if measured else 'unknown: not measured per test')
            missed = sorted(targets_left - set(req.test_items.get(name, []))) if measured else []
            tried_rows.append(f"- {name} ({status.get(name, 'not run')}; reached: {reached_text})"
                              + (f"; targets not reached: {', '.join(missed[:12])}" if missed else "")
                              + f"\n{src[:1600]}")
            if len(tried_rows) >= 6:
                break
        built = set(req.tried_scenarios)
        done = [text for sig, text in self._tried_text.get(fn.name, {}).items() if sig in built][-8:]
        if done:                                                                                  # A1-PL18
            tried_rows.append("- TRIED - do not repeat (scenarios of the brain already built and measured; give other "
                              "values, set-up or stub values):\n" + "\n".join(f"  - {x}" for x in done))
        for reason in req.refused.get(fn.name, [])[-6:]:                     # earlier refusals [A1-PL2, A1-PL8]
            tried_rows.append(f"- REFUSED earlier: {reason[:300]}")
        for call in [c for c in req.crashed_calls if re.match(r"\s*(?:\(void\)\s*\(?\s*)?" + re.escape(short) + r"\s*\(", c)][:6]:
            tried_rows.append(f"- CRASHED or did not end: {call[:200]} (a rule-based test; do not plan it again) [A2-21f]")
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
            *([f"OBJECT: {uc.call_form(fn, layout)}"] if uc.call_form(fn, layout) else []),            # A2-21ae
            *([f"STATE: {fn.qualified_name} {uc.keeps_state(fn, layout)}: a behaviour may need several calls in a row "
               "- put the earlier calls in setup"] if uc.keeps_state(fn, layout) else []),      # A1-PL2, A1-PL3
            *[f"REACHED THROUGH THIS FUNCTION: {c.qualified_name} (no test can call it) holds open gaps; reach them "
              f"by calling {fn.qualified_name}:\n{self._source_for_brain(c, lines, outside, profile)}"
              for c in callees[:2]],
            *([("VALUES ONLY THE SUT FILE DEFINES (a test cannot name them - write the value): " + ", ".join(
                f"{n} = {v}" for n, v in shown_values))] if (shown_values := [
                    (n, v) for n, v in internal.items() if re.search(r"\b" + re.escape(n) + r"\b", source)][:30])
              else []),                                                                     # A1-PL2a
            "PARAMETERS:\n" + ("\n".join(params) or "- none"),
            f"RETURNS: {return_type_of(fn) or 'void'}",
            ("TARGETS (name these by their id; each groups open items reached by the same path):\n" if targets is not None
             else "OPEN GAPS:\n") + "\n".join(gap_rows),
            "STUBS (controllable return values):\n" + ("\n".join(stub_rows) or "- none"),
            "STUB VARIABLES:\n" + ("\n".join(var_rows) or "- none"),
            *([project] if project else []),                                                  # A2-21ak
            "REAL OR SYSTEM DEPENDENCIES (their real code runs; they have no ut_stub_ control - control them through "
            "what they read, such as the GLOBALS below): " + (", ".join(real[:20]) or "none"),
            "GLOBALS a test can set directly: " + (", ".join(globals_[:20]) or "none"),
            *(["SUT STATE (set with state: the variable, for an array its index, for a structure its field, and the "
               "value; read a variable after the call with its getter ut_state_get_<name>()):\n"
               + "\n".join(structured["rows"]["state"])] if structured and structured["rows"]["state"] else
              ["SUT STATE (only the SUT file sees these variables: never name one, use its setter and getter) "
               "[A2-21z, A2-21aj]:\n" + ("\n".join(f"- {x}" for x in setters) or "- none")]),
            *(["CALLABLE FUNCTIONS (calls: earlier calls that build up state; their args in the form of args):\n"
               + "\n".join(structured["rows"]["callables"])] if structured and structured["rows"]["callables"] else []),
            *(["STUB OUTPUTS (writes: what the stub writes into this parameter when it is called - elements or "
               "fields):\n" + "\n".join(structured["rows"]["outputs"])]
              if structured and structured["rows"]["outputs"] else []),
            *(["REGISTERS (registers: the value the register holds before the call):\n"
               + "\n".join(structured["rows"]["registers"])] if structured and structured["rows"]["registers"] else []),
            *(["OBJECT MEMBERS (members: the value of a member of the object before the call, private ones too; "
               "elements for a container):\n" + "\n".join(structured["rows"]["members"])]
              if structured and structured["rows"]["members"] else []),
            *(["EXCEPTIONS the function throws (expected_exception: the one the EVIDENCE states for this case):\n"
               + "\n".join(structured["rows"]["exceptions"])]
              if structured and structured["rows"].get("exceptions") else []),
        ]
        # ARCH-17: facts stay whole; evidence, earlier tests and the style give way in that order
        # (reverse) when the call does not fit the brain's window.
        journal = req.journal.get(fn.qualified_name) or req.journal.get(fn.name, "")
        if profile is not None and journal:
            journal = journal[: profile.journal_tokens * 3]                                    # BP-5, LA-4
        if sequence:                                                                         # BP-2, BP-3
            facts.append(f"CALL SEQUENCE: the gaps {', '.join(sorted(sequence)[:6])} need state that only earlier calls "
                         "build up (see JOURNAL). For them give calls: the earlier calls of CALLABLE FUNCTIONS, in "
                         "order, each with its args in the form of args; then the call of args.")
        dep_code = uc.dependency_code(fn, layout)                               # what the real code computes
        examples = req.examples.get(fn.qualified_name) or req.examples.get(fn.name, "")          # KB-25
        return [uc.part("facts", "\n\n".join(facts)),
                *([uc.part("real dependency code", "CODE OF THE REAL DEPENDENCIES IT CALLS (they run in the test; "
                           "choose inputs and globals so that their results meet the PATH CONDITIONS):\n" + dep_code,
                           uc.P_EVIDENCE, min_chars=600)] if dep_code else []),
                *([uc.part("journal", "JOURNAL (why earlier attempts missed; plan so that the cause is removed) "
                           "[LA-4]:\n" + journal)] if journal else []),
                *([uc.part("lessons", "LESSONS (verified fixes of the same causes) [LA-8]:\n"
                           + "\n".join(f"- {x}" for x in req.lessons[:4]), uc.P_EXAMPLES)] if req.lessons else []),
                uc.part("evidence", "EVIDENCE:\n" + ("\n".join(ev_rows) or
                                                     "- none: every scenario is characterization_gap"), uc.P_EVIDENCE),
                uc.part("tried", "TRIED (earlier tests of this function and the items each reached):\n"
                        + ("\n".join(tried_rows) or "- none")
                        + "".join(f"\n- {name}: FAILED in this run ({why}) - do not plan it again unchanged"
                                  for name, why in sorted(req.failed_tests.items())
                                  if re.search(r"(?:^|_)" + re.escape(fn.name.split("::")[-1]) + r"(?:_|$)", name)),
                        uc.P_TRIED),
                uc.part("style", "EXISTING TEST STYLE:\n" + ("\n---\n".join(style) or f"- {layout.framework}"),
                        uc.P_EXAMPLES),
                *([uc.part("examples", "EXAMPLES (scenarios that reached similar items in verified tests of other code: "
                           "follow how their inputs meet the PATH CONDITIONS; their names and values do not exist "
                           "here and are not evidence) [KB-25]:\n" + examples, uc.P_EXAMPLES)] if examples else [])]

    def _source_for_brain(self, fn: uc.FunctionInfo, lines: list[str], focus: list[int],
                          profile: uc.BrainProfile | None = None) -> str:
        """The function's source with line numbers; for a long function only the lines around its open items,
        with the function's first lines (LA-10). What is long follows the answering brain's profile: a line limit,
        or - in a profile without one - only a function that does not fit its window (BP-5)."""
        end = min(fn.line_end, len(lines))
        rows = range(fn.line_start, end + 1)
        count = end - fn.line_start + 1
        if profile is None:
            long = count > self.cfg.long_function_lines
        else:
            long = not profile.short_function(count, sum(len(lines[n - 1]) + 7 for n in rows))
        if long and focus:
            keep = set(range(fn.line_start, fn.line_start + 3))
            for n in focus:
                keep |= set(range(max(fn.line_start, n - 6), min(end, n + 3) + 1))
                for k in range(n - 1, fn.line_start, -1):          # the decisions that enclose the item
                    if re.search(r"\b(if|else|for|while|switch|case)\b", lines[k - 1]) and block_end(lines, k) >= n:
                        keep.add(k)
            out, last = [], 0
            for n in sorted(k for k in keep if fn.line_start <= k <= end):
                if last and n > last + 1:
                    out.append("   ...")
                out.append(f"{n:4d}| {lines[n - 1]}")
                last = n
            return "\n".join(out)
        return "\n".join(f"{n:4d}| {lines[n - 1]}" for n in rows)

    def _dependency_headers_text(self, layout: uc.LayoutProfile) -> list[str]:
        root = Path(layout.repo_root)
        out = []
        for rel in list(dict.fromkeys(layout.headers + (layout.dependencies.headers if layout.dependencies else []))):
            path = root / rel
            if path.is_file():
                out.append(strip_comments(path.read_text(encoding="utf-8", errors="replace")))
        return out

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
        for rid, text, source, line in self._requirements(layout):
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
                               stubs: list[uc.StubSpec], evidence: list[uc.EvidenceRef], plan_notes: dict[str, list[str]],
                               kept: list[tuple[uc.Scenario, dict]] | None = None) -> list[uc.Scenario]:
        """A1-PL1..A1-PL9: the brain suggests, Agent1 checks; at most the configured number of calls. The brain names
        targets (A1-PL3a); a cut answer is split up, never asked again unchanged (ARCH-19b). ``kept`` receives each
        accepted scenario with the answer it came from, for the family of the function (A1-PL17)."""
        constants = self._constants_for_values(layout)
        known = self._known_names(layout, stubs)
        evidence = list(evidence)
        accepted: list[uc.Scenario] = []
        pending_trials: list[uc.Scenario] = []           # refused only by predictions [NC-7]
        extra, calls, brain = "", 0, ""
        dropped: list[str] = []
        failures: list[str] = []
        tried: set[str] = set()
        if not open_ids:
            return []                                                        # nothing to plan [ARCH-16]
        limit = self.cfg.max_brain_calls_per_function
        available, family = await self._available_brains()
        third = open_ids & set(req.other_model_items)          # REC-3: prefer another model, retain one-model recovery
        if third:
            used = {family.get(b, b) for b in req.avoid_brains}
            others = [b for b in available if family.get(b, b) not in used]
            if not others:
                plan_notes["planning"].append(f"{fn.name}: no other model available; bounded recovery with the "
                                              "same model and prior diagnostics [REC-3]")
        tried_before = set(req.tried_scenarios)                # A1-PL4 g, LA-4
        execution_context = req.execution_context or uc.execution_context(layout)
        targets = make_targets(fn, gaps, sut_text, layout) + [Target(tid=g, items=[g], function=fn.name)
                                                              for g in sorted(open_ids) if g.startswith("EVIDENCE:")]
        by_tid = {tg.tid: tg for tg in targets}                                                # A1-PL3a

        def left_targets(covered: set[str]) -> list[Target]:
            return [tg for tg in targets if set(tg.items) & open_ids and not set(tg.items) & covered]
        split_size, splits, focus = 0, 0, []                  # ARCH-19b: smaller requests after a cut answer
        lists = self._structured_lists(fn, layout, stubs, sut_text)       # the names of the structured scenario
        profile = await uc.answering_profile(self, "A1.plan")                                  # BP-5
        book: uc.ProfileBook = getattr(self, "profiles", None) or uc.ProfileBook()
        sequence = {g for g in open_ids if req.item_kinds.get(g) == "call_sequence"}           # BP-2, BP-3
        seq_brains = await uc.brains_for(self, "call_sequence", "A1.plan") if sequence else []
        if sequence and not seq_brains:
            plan_notes["planning"].append(f"{fn.name}: {', '.join(sorted(sequence)[:4])} need a call sequence, which no "
                                          "available brain's profile allows; planned as a scenario [BP-3, BP-9]")
            sequence = set()

        def next_brains() -> list[str] | None:
            """COV-6: the next available brain of another model in the brain order; none left - the same brains
            again with the facts of what was tried. A1-PL8: a model whose scenarios all failed the check is not
            asked again - neither on another server (the remote Qwen and the one in Ollama are one model) [ARCH-19]."""
            if not (req.avoid_brains or tried or third):
                return None
            failed = {family.get(b, b) for b in tried}
            avoid = {family.get(b, b) for b in req.avoid_brains}
            pool = [b for b in available if family.get(b, b) not in failed]
            # REC-3: prefer a different family, but the sole model may use the bounded recovery attempt.
            return [b for b in pool if family.get(b, b) not in avoid] or pool
        digest = ""
        while calls < limit:
            calls += 1
            covered = {g for s in accepted for g in s.gap_ids}
            left = left_targets(covered)
            shown = [tg for tg in left if tg.tid in focus] if focus else left              # ARCH-19b: one at a time
            context = self._facts(fn, gaps, open_ids, layout, req, sut_text, stubs, evidence, profile, sequence,
                                  targets=shown, structured=lists)
            if calls == 1:                       # A1-PL18: the same facts give the same answer - not asked again
                # REC-4: diagnostic evidence is part of the question; iteration numbers and call IDs are not.
                digest = uc.sha256_bytes(json.dumps([
                    layout.sut_sha256, fn.qualified_name, fn.line_start, sorted(open_ids), sorted(req.avoid_brains),
                    sorted(f"{e.evidence_id} {e.text}" for e in evidence),
                    {k: v for k, v in lists.items() if k not in ("rows", "member_kinds")},
                    execution_context, req.failed_tests, req.journal, req.refused, req.lessons,
                    sorted(req.tried_scenarios), req.test_items,
                    [t.model_dump(mode="json", include={"name", "status", "message", "expected", "actual"})
                     for t in req.last.tests] if req.last else []],
                    sort_keys=True, default=str).encode("utf-8"))
                earlier = self._asked.get(fn.qualified_name)
                checks_only = all(g.startswith("EVIDENCE:") for g in open_ids)
                if earlier is not None and earlier[0] == digest and (earlier[1] == 0 or checks_only):
                    # a requirement check (A1-25y) is asked once per state of its facts: its answer was built and
                    # judged; asked again unchanged, the brain repeats it
                    plan_notes["planning"].append(f"{fn.name}: facts unchanged since its last brain call, which gave "
                                                  + ("no accepted scenario" if earlier[1] == 0 else
                                                     "its requirement check") + " - not asked again [A1-PL18]")
                    return []
            if extra:
                context.insert(1, uc.part("re-ask", extra.strip()))
            need = ba.OutputNeed(items=min(self.cfg.max_brain_scenarios, max(profile.scenarios_per_call, 1),
                                           max(len(shown), 1), split_size or self.cfg.max_brain_scenarios),
                                 tokens_per_item=self.cfg.tokens_per_scenario, overhead=100,   # ARCH-11a, BP-5
                                 asked=True)                                                  # ARCH-20c
            # as many scenarios as the answer limit was planned for, not the configured maximum [ARCH-11a]
            task = PLAN_TASK.format(fn=fn.qualified_name, limit=need.items,
                                    forms=plan_forms(fn, lists).replace("{{", "{").replace("}}", "}"))
            try:
                allowed = next_brains()                                            # COV-6, A1-PL8: the next brain
                if allowed is not None and not allowed:
                    raise ba.AgentFault("NO_BRAIN_AVAILABLE", "every allowed brain was tried")
                if sequence:                                   # BP-3: only a brain whose profile allows it [BP-2]
                    allowed = [b for b in (allowed if allowed is not None else seq_brains) if b in seq_brains] or allowed
                answer = await self.generate(
                    task, context, self._plan_model(fn, open_ids, evidence, stubs, [tg.tid for tg in shown],
                                                    self.cfg.targets_per_scenario, lists),
                    need=need, step="A1.plan", project=layout.repo_root, allowed=allowed,
                    vary=calls > 1)                                                         # ARCH-20, ARCH-20a
            except ba.AgentFault as exc:
                failures.append(f"{exc.code}: {exc.message}"[:300])
                if exc.code in ba.NO_BRAIN_CODES and not accepted and not brain:
                    raise ba.AgentFault("NO_BRAIN_AVAILABLE", f"{fn.qualified_name}: no brain answered the planning "
                                        f"({exc.code}: {exc.message}) [ARCH-24, A1-PL8]")
                if getattr(exc, "truncated", False) and splits < self.cfg.split_calls and shown:     # ARCH-19b
                    splits, limit = splits + 1, limit + 1
                    if need.items > 1:
                        split_size, focus = max(1, need.items // 2), []
                    else:                                       # one target per request, the next one each time
                        done_alone = set(focus)
                        nxt = next((tg.tid for tg in left if tg.tid not in done_alone and tg.tid != (shown[0].tid
                                    if len(shown) == 1 else "")), None)
                        if nxt is None:
                            break
                        split_size, focus = 1, [nxt]
                    plan_notes["planning"].append(
                        f"{fn.name}: the answer ended ({getattr(exc, 'ended', '') or 'cut'}) - asked again for "
                        f"{split_size} scenario(s){' for ' + focus[0] if focus else ''} [ARCH-19b]")
                    continue
                if not accepted:
                    plan_notes["planning"].append(f"{fn.name}: no usable brain answer ({exc.code})")
                break
            used = self.brain_of_call()                             # this call's brain, not another's [ARCH-18]
            brain = f"brain:{used.get('brain', '')}/{used.get('model_id', '')}"
            ev_map = {e.evidence_id: e for e in evidence}
            if not book.allows(used.get("brain", ""), "call_sequence"):      # BP-3: never taken from a brain whose
                for planned in answer.scenarios:                            # profile does not allow the kind
                    planned.calls_before, planned.calls = [], []
            problems, sought, before, repeats = [], [], len(accepted), 0
            for k, planned in enumerate(answer.scenarios[: self.cfg.max_brain_scenarios], 1):
                aimed = [str(t) for t in (planned.targets or planned.gap_ids)] or sorted(open_ids)
                purpose = "requirement check" if aimed and all(t.startswith("EVIDENCE:") for t in aimed) \
                    else "coverage"                                   # the rates of the two apart [ORC-12b]
                # REC-2: preserve the logical name during repair; compare executed semantic inputs below.
                tfix = expand_targets(planned, by_tid)                                            # A1-PL3a
                answered = planned.model_dump(mode="json")                                         # A1-PL17
                scenario, reasons = self._check_planned(planned, fn, open_ids, infeasible, ev_map, stubs, constants,
                                                        known, layout)
                fixes = tfix + list(self.last_fixes)
                if fixes:                                                   # form forgiven, recorded [ARCH-20]
                    self.log_answer("A1.plan", "fixed", function=fn.name, scenario=k, fixes=fixes)
                    plan_notes["planning"].append(f"{fn.name}: format fixed in scenario {k}: {'; '.join(fixes[:4])}"
                                                  [:400] + " [ARCH-20]")
                if scenario is None:
                    self.log_answer("A1.plan", "refused", function=fn.name, scenario=k, reasons=reasons)
                    self.log_answer("A1.plan", "scenario", function=fn.name, scenario=k, purpose=purpose, outcome="refused",
                                    causes=refusal_causes(reasons))                                 # ORC-12b
                    problems.append(f"- scenario {k} ({planned.name or planned.purpose[:40]}): " + "; ".join(reasons))
                    continue
                if scenario.trial:          # refused only by predictions: the brain may correct it in the re-ask;
                    self.log_answer("A1.plan", "refused", function=fn.name, scenario=k,   # otherwise the tool decides
                                    reasons=scenario.trial, then="trial if the re-ask does not cover its items")
                    self.log_answer("A1.plan", "scenario", function=fn.name, scenario=k, purpose=purpose, outcome="trial",
                                    causes=refusal_causes(scenario.trial))                         # ORC-12b
                    problems.append(f"- scenario {k} ({planned.name or planned.purpose[:40]}): "
                                    + "; ".join(scenario.trial))
                    sig = uc.scenario_signature(scenario, execution_context)
                    if sig not in tried_before and all(sig != uc.scenario_signature(x, execution_context)
                                                       for x in pending_trials):
                        scenario.made_by = brain
                        pending_trials.append(scenario)                                         # NC-7
                    continue
                kinds = [req.item_kinds.get(g, "") for g in scenario.gap_ids]
                wider = [k2 for k2 in uc.KIND_LADDER if k2 in kinds and k2 in ("setup", "stub", "test", "suite_part")]
                scenario.kind = wider[-1] if wider else ("call_sequence" if scenario.calls_before else "scenario")
                if uc.scenario_signature(scenario, execution_context) in tried_before:  # REC-2
                    self.log_answer("A1.plan", "refused", function=fn.name, scenario=k,
                                    reasons=["the same scenario was tried in an earlier iteration"])
                    self.log_answer("A1.plan", "scenario", function=fn.name, scenario=k, purpose=purpose, outcome="refused",
                                    causes=["repeat"])                                              # ORC-12b
                    problems.append(f"- scenario {k} ({planned.name or planned.purpose[:40]}): the same inputs and "
                                    "set-up were tried in an earlier iteration and did not reach the items (see "
                                    "JOURNAL): plan different values, set-up or a sequence of calls")
                    repeats += 1
                    continue
                scenario.made_by = brain
                scenario.required_conditions = [c for g in gaps if g.gap_id in scenario.gap_ids
                                                for c in enclosing_conditions(sut_text, fn, g.line)][:8]
                same = scenario.model_dump(include={"gap_ids", "args", "setup", "stub_values", "variables",
                                                    "expected_return", "expected_after"})
                if any(s.model_dump(include=set(same)) == same for s in accepted):
                    continue                                                  # the re-ask repeated it
                self.log_answer("A1.plan", "scenario", function=fn.name, scenario=k, purpose=purpose, outcome="accepted")   # ORC-12b
                self._tried_text.setdefault(fn.name, {})[uc.scenario_signature(scenario, execution_context)] = brief(scenario,
                                                                                                    by_tid)  # A1-PL18
                accepted.append(scenario)
                if kept is not None:                                                            # A1-PL17
                    kept.append((scenario, answered))
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
            remaining = [tg.tid for tg in left_targets(covered)]
            done_targets = [tg.tid for tg in targets if set(tg.items) & covered]
            salvaged = bool(used.get("salvaged"))
            if salvaged and remaining and splits < self.cfg.split_calls:          # ARCH-19b: the rest, smaller
                splits, limit = splits + 1, limit + 1
                split_size, focus = max(1, need.items // 2), []
                plan_notes["planning"].append(f"{fn.name}: the answer ended ({used.get('ended') or 'cut'}); its "
                                              f"complete scenarios are kept and {', '.join(remaining[:6])} asked for "
                                              f"again, {split_size} scenario(s) at a time [ARCH-19b]")
            elif split_size and remaining and not problems and len(accepted) > before and splits < self.cfg.split_calls:
                splits, limit = splits + 1, limit + 1          # ARCH-19b: the rest of the split work, as small
                focus = [remaining[0]] if focus else []
            elif not accepted and calls >= limit and not tried:     # A1-PL8: once more with the next brain
                tried.add(used.get("brain", ""))
                if not next_brains():
                    break                                         # no other brain is available
                limit += self.cfg.max_brain_calls_per_function
            elif not (problems or found) and accepted or not remaining or calls >= limit:
                break
            elif problems and repeats == len(problems) and not found and len(accepted) == before:
                plan_notes["planning"].append(f"{fn.name}: the answer repeated what was tried - not asked again with "
                                              "the same facts [A1-PL18]")
                break
            extra = ("\n\nYOUR PREVIOUS ANSWER WAS CHECKED. Scenarios already accepted reach the targets: "
                     + (", ".join(done_targets) or "none") + ". Plan only these targets now: " + ", ".join(remaining)
                     + ("\nProblems found (fix them):\n" + "\n".join(problems) + "\n" + self._expected_form(fn, open_ids)
                        if problems else "")
                     + ("\nNew evidence from the look-up:\n" + "\n".join(
                         f"- {e.evidence_id} {e.evidence_class} (linked by look-up) {e.source}:{e.line}: {e.text[:300]}"
                         for e in found) if found else ""))
        if digest:
            self._asked[fn.qualified_name] = (digest, len(accepted))                               # A1-PL18
        for scenario in pending_trials:          # NC-7: no checked scenario took its items - the tool decides
            if set(scenario.gap_ids) - {g for s in accepted for g in s.gap_ids}:
                accepted.append(scenario)
                plan_notes["planning"].append(f"{fn.name}: a scenario refused only by predictions "
                                              f"({'; '.join(scenario.trial)[:200]}) is built in a trial [NC-6, NC-7]")
        for index, scenario in enumerate(accepted, 1):
            scenario.scenario_id = f"S-brain-{uc.function_stem(fn, layout)}-{req.iteration}-{index}"
            scenario.function_line = fn.line_start                                               # overloads [NC-4]
        if not accepted and all(g.startswith("EVIDENCE:") for g in open_ids):
            # only a documented-behaviour check was asked: it stays open and is planned again with the reasons
            # (a brain whose profile writes whole tests gets it in Agent2) [A1-PL1, A2-21n, BP-2]
            plan_notes["planning"].append(f"{fn.name}: the documented behaviour is planned again: no scenario of "
                                          f"the brain passed the check [A1-PL1]")
            return []
        if not accepted:                                     # no rule plan in the brain's place [A1-PL8, ARCH-24]
            why = (" | ".join(d[2:160] for d in dropped[:3]) if dropped else
                   "; ".join(failures[:2]) if failures else "the brain suggested no scenario")
            raise ba.AgentFault("BRAIN_PLAN_FAILED", f"{fn.qualified_name}: no scenario of "
                                f"{', '.join(sorted(tried | {brain.split(':', 1)[-1].split('/')[0]} - {''})) or 'any brain'} "
                                f"passed the check after {calls} call(s): {why} [A1-PL8]"[:900])
        if brain:
            plan_notes["planning"].append(
                f"{fn.name}: {len(accepted)} scenario(s) by {brain.split(':', 1)[1]} "
                f"({sum(not s.characterization for s in accepted)} asserting stated behaviour), {calls} call(s)"
                + (f"; dropped by the check: {' | '.join(d[2:120] for d in dropped[:3])}" if dropped else "")
                + " [A1-PL4]")
        return accepted

    async def _link_requirements(self, layout: uc.LayoutProfile) -> list[uc.RequirementLink]:
        """A1-PL13: requirements written in prose, linked by the brain and checked here."""
        if not self.cfg.link_requirements or not (layout.requirement_files or layout.given_requirements):
            return []
        root = Path(layout.repo_root)
        reqs = self._requirements(layout)
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

    async def _missing_declarations(self, layout: uc.LayoutProfile, symbols: list[str],
                                    no_brain: set[str] | frozenset = frozenset()) -> tuple[
            list[uc.Dependency], list[str]]:
        """A1-PL14: a declaration from the dependency plan or the headers; the brain only when none exists."""
        root = Path(layout.repo_root)
        entries = layout.dependencies.entries if layout.dependencies else []
        known = {d.name: d for d in entries} | {d.qualified_name: d for d in entries if d.qualified_name}
        header_decls: dict[str, tuple[str, str]] = {}
        header_texts = []
        for rel in self._all_headers(root):
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
            header_texts.append(text)
            for name, decl in find_declarations(text).items():
                header_decls.setdefault(name, (decl, rel))
        sut_lines = (root / layout.sut_file).read_text(encoding="utf-8", errors="replace").splitlines()
        out, open_items = [], []
        for sym in symbols:
            dep = known.get(sym) or known.get(sym.split("(")[0].strip())
            if dep is not None and (dep.declaration or dep.return_type or dep.kind == "variable"):
                out.append(dep.model_copy(update={"decision": "stub", "decided_by": "rule", "reason":
                           f"declared in {dep.header or 'a header outside the repository'} but not linked: stubbed "
                           "[A1-PL14]"}))
                continue
            short = sym.split("(")[0].strip().split("::")[-1]
            plain = sym.split("(")[0].strip()
            found = None
            if "::" in plain:                 # the declaration inside its own namespace, of the right overload
                wanted = _arity(sym[sym.index("(") + 1: sym.rindex(")")]) if "(" in sym and ")" in sym else None
                for rel in self._all_headers(root):
                    decls = _declarations_in(short, plain.rsplit("::", 1)[0], rel, root)
                    pick = [d for d in decls if wanted is None or _arity(d[d.index("(") + 1: d.rindex(")")]) == wanted]
                    if pick or decls:
                        found = ((pick or decls)[0], rel)
                        break
            elif plain in header_decls:
                found = header_decls[plain]
            if found is not None:
                decl, rel = found
                rt, params = parse_declaration(decl)
                out.append(uc.Dependency(name=short, qualified_name=plain if short != plain else "", declaration=decl,
                                         header=rel, decision="stub", decided_by="rule",
                                         reason="declared in a header, not linked: stubbed [A1-PL14]",
                                         return_type=rt, params=params))
                continue
            usage = [f"{n}: {line.strip()}" for n, line in enumerate(sut_lines, 1)
                     if re.search(r"\b" + re.escape(sym) + r"\b", line)][:12]
            if not self.cfg.propose_declarations or not usage:
                open_items.append(f"no declaration found for {sym}; cannot stub it")
                continue
            if sym in no_brain or short in no_brain:                                 # ARCH-21a: not asked again
                open_items.append(f"no declaration found for {sym}; the brain's proposal did not build - a declaration "
                                  "in the repository is needed [A1-PL14]")
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
                    if function_name(decl) not in (sym, short):
                        raise ValueError("not a declaration of " + sym)
                    rt, params = parse_declaration(decl + ";")
                    unknown = unknown_types([rt] + [p[0] for p in params], header_texts + ["\n".join(sut_lines)])
                    if unknown:                                     # a guard: the brain invented a type [A1-PL14]
                        raise ValueError("uses types the repository does not declare: " + ", ".join(unknown[:4]))
                    fields = {"return_type": rt, "params": params}
                    if short != sym:
                        fields["qualified_name"] = sym
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
            out.append(uc.Dependency(name=short if "(" in decl else sym, declaration=decl + ";", decision="stub",
                                     decided_by=who,
                                     reason="no declaration in the repository: proposed by the brain from its uses "
                                            "and confirmed only by Agent3's build [A1-PL14]", **fields))
        return out, open_items

    # ---------------------------------------------------------------- planning
    async def plan(self, req: uc.PlanRequest) -> uc.UTPlan:
        layout, last = req.layout, req.last
        root = Path(layout.repo_root)
        plan_id = f"plan-{req.iteration}-{uuid.uuid4().hex[:6]}"
        suite_exists = (root / layout.suite_file).is_file() or bool(req.existing_suite)
        base = dict(plan_id=plan_id, iteration=req.iteration, layout=layout, create_suite=not suite_exists,
                    files_to_create=[] if suite_exists else [layout.suite_file],
                    files_to_edit=[layout.suite_file] if suite_exists else [])
        with contextlib.suppress(Exception):           # the test vocabulary for Agent2 too [A1-PL2a]
            base["internal_values"] = self._internal_values(layout)
        if layout.blocked:
            return uc.UTPlan(**base, stage="blocked", reason=layout.blocked_reason or "layout blocked")
        stubs = self._stubs(layout, last)
        disputed = set(req.disputed_evidence)
        notes: dict[str, list[str]] = {"planning": [], "conflicts": [], "lookups": []}
        if req.retry_scenarios:                                              # REC-11, REC-12
            source = uc.read_source(root / layout.sut_file)
            scenarios, gaps = [], []
            groups: dict[tuple[str, int], list[uc.Scenario]] = {}
            for previous in req.retry_scenarios:
                groups.setdefault((previous.function, previous.function_line), []).append(previous)
            available_gaps = [g for f in last.coverage.functions for g in f.gaps] if last and last.coverage else []
            for (name, line), previous in groups.items():
                fn = uc.function_of(name, layout, line)
                if fn is None:
                    raise ba.AgentFault("INVALID_REQUEST", f"retry function is not in the layout: {name}:{line}")
                wanted = {g for sc in previous for g in sc.gap_ids}
                active = [g for g in available_gaps if g.gap_id in wanted]
                evidence = [e for e in self.collect_evidence(layout, name).evidence
                            if uc.evidence_key(e) not in disputed]
                repaired = await self._brain_scenarios(fn, active, wanted, set(), layout, req, source,
                                                      stubs, evidence, notes)
                for sc in repaired:
                    old = max(previous, key=lambda p: len(set(p.gap_ids) & set(sc.gap_ids)))
                    sc.scenario_id += "-repair-" + uuid.uuid4().hex[:8]
                    if old.test_name:
                        sc.test_name = old.test_name
                scenarios.extend(repaired)
                gaps.extend(active)
            return uc.UTPlan(**base, stage="coverage", stubs=stubs, scenarios=scenarios, gaps=gaps,
                             reason="scoped replanning from compiler feedback [REC-11]", **notes)
        if req.inputs_only:                      # the search's inputs only, no brain [ORC-5a, A1-18c]
            return uc.UTPlan(**base, stage="coverage", stubs=stubs, scenarios=self._input_scenarios(layout, req),
                             reason="inputs found by the coverage-guided search [A1-18c]")
        # The first coverage iteration combines the cheap tests and search with brain planning. Later plans receive
        # their measured coverage and journaled failures [A1-PL1].
        brain_ok = last is not None
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
            stubbed = {s.function for s in stubs} | {s.qualified_name for s in stubs if s.qualified_name}   # A1-PL14
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
            proposed, decl_items = await self._missing_declarations(
                layout, missing_decls, set(req.no_brain_declarations)) if missing_decls else ([], [])
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
                                         attempts=tries, action=action, failure_kind=f.kind))
            if f.classification == "undetermined" and not evidence:
                repairs[-1].diagnostic += " | no further expected-behaviour evidence found [A1-25j]"
        disputed |= new_disputes
        found = self._input_scenarios(layout, req)                                # A1-18c
        by_fuzz: set[str] = {g for sc in found for g in sc.gap_ids}
        scenarios += found
        real = {d.name: d for d in (layout.dependencies.entries if layout.dependencies else []) if d.decision == "real"}
        lines = sut_text.splitlines()
        active_by_fn: dict[str, list[uc.GapItem]] = {}
        final = set(req.final_items)
        through = {}                          # A2-21ae: a member no test can name is planned through its caller
        for fn in layout.functions:
            if uc.internal_member(fn, layout):
                caller = next((c for c in uc.callers_of(fn, layout) if not uc.internal_member(c, layout)), None)
                if caller is not None:
                    through[fn.qualified_name] = caller
        planned_fns = [f for f in layout.functions if f.qualified_name not in through]
        solver: dict[str, Any] = {}                    # A1-PL15: the constants, read once and only when needed
        for fn in planned_fns:
            own = {fn.name, fn.qualified_name} | {q for q, c in through.items() if c is fn} | {
                f.name for f in layout.functions if f.qualified_name in through and through[f.qualified_name] is fn}
            fn_gaps = [g for g in gaps if g.function in own and g.gap_id not in by_fuzz and g.gap_id not in final
                       and "infeasible" not in g.detail and "unreachable_code" not in g.detail]
            if len(uc.overloads(fn, layout)) > 1:            # an overload: only the items inside its own lines [NC-4]
                fn_gaps = [g for g in fn_gaps if not g.line or fn.line_start <= g.line <= fn.line_end]
            if not fn_gaps:
                continue
            evidence = [e for e in self.collect_evidence(layout, fn.name).evidence if uc.evidence_key(e) not in disputed]
            strongest = min((r.evidence_class for r in evidence), default="E8")
            active, given_up = [], []
            third = set(req.other_model_items)                  # LA-5: a third attempt only by another model
            for g in fn_gaps:
                g.attempts = req.attempts.get(g.gap_id, 0)
                limit = self.cfg.max_attempts_per_gap + (1 if g.gap_id in third else 0)
                (given_up if g.attempts >= limit else active).append(g)
            for g in given_up:
                open_items.append(f"{g.gap_id}: still uncovered after {g.attempts} attempts [A1-36, LA-5]")
            for g in fn_gaps:
                text = lines[g.line - 1] if 0 < g.line <= len(lines) else ""
                for name in [n for n in real if re.search(r"\b" + re.escape(n) + r"\s*\(", text)]:
                    if g.attempts >= self.cfg.max_attempts_per_gap:
                        open_items.append(f"{g.gap_id}: needs control of the real dependency {name} [A1-DA12]")
            solved = self._solved(fn, active, lines, layout, last, solver)                # A1-PL15
            scenarios += solved
            given = {g for sc in solved for g in sc.gap_ids}
            active = [g for g in active if g.gap_id not in given or g.attempts > 0]
            active_by_fn[uc.function_stem(fn, layout)] = active
            for group in self._group(active, sut_text)[: self.cfg.max_scenarios_per_function]:     # LA-10
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
                    scenario_id=f"S-{uc.function_stem(fn, layout)}-{lead.line}-{lead.metric}", function=fn.name,
                    function_line=fn.line_start,
                    gap_ids=[g.gap_id for g in group], description=description,
                    required_conditions=reach + required, stub_hints=hints,
                    expected="derive from the evidence when it states the result, otherwise observe it",
                    evidence=evidence, strongest_class=strongest, characterization=strongest == "E8"))
        planned = {sc.function for sc in scenarios}
        if last is not None and last.coverage is not None:
            scenarios += [sc for sc in self._evidence_scenarios(layout, req, disputed) if sc.function not in planned]
        deferred: list[str] = []
        if brain_ok:                                                              # A1-PL1
            infeasible = {g.gap_id for g in gaps if "infeasible" in g.detail or "unreachable_code" in g.detail}
            scenarios = await self._with_brain(scenarios, active_by_fn, infeasible, layout, req, stubs, disputed, notes,
                                               deferred)
        keep = [t.name for t in (last.tests if last else []) if t.status == "passed" and t.name not in drop]
        return uc.UTPlan(**base, stage="coverage", stubs=stubs, scenarios=scenarios, repairs=repairs, gaps=gaps,
                         keep_tests=keep, drop_tests=sorted(set(drop)), open_items=open_items,
                         reason=last.reason if last else "no measurement yet: estimated obligations [A1-18a]",
                         disputed_evidence=sorted(new_disputes), rules_only=not brain_ok and last is not None,
                         deferred=deferred, **notes)

    def _solved(self, fn: uc.FunctionInfo, active: list[uc.GapItem], lines: list[str], layout: uc.LayoutProfile,
                last: uc.VerificationResult | None, solver: dict[str, Any]) -> list[uc.Scenario]:
        """A1-PL15: the open items of ``fn`` whose decision and path compare parameters with constants, planned by
        solving the condition vectors for argument values; one scenario per argument set, with every item it takes.
        A set whose test already ran (and left the item open) is not planned again: the brain has the item."""
        params = solver_params(fn)
        if not params or any(not p.name for p in params) or uc.internal_member(fn, layout):
            return []
        if "constants" not in solver:
            solver["constants"] = self._constants(layout)
        stem = uc.function_stem(fn, layout)
        ran = {t.name for t in (last.tests if last else [])}
        by_args: dict[tuple, uc.Scenario] = {}
        tried: set[tuple] = set()
        for g in active:
            if g.function not in (fn.name, fn.qualified_name):
                continue
            for k, (args, vector) in enumerate(solve_gap(g, fn, lines, solver["constants"]), 1):
                key = tuple((a.name, a.value) for a in args)
                if key in tried:
                    continue
                if key in by_args:
                    by_args[key].gap_ids.append(g.gap_id)
                    continue
                where = re.sub(r"[^A-Za-z0-9]+", "_", g.gap_id.split(":", 2)[-1]).strip("_").lower()
                name = f"test_{stem}_solved_{g.metric.lower()}_{where}_{k}"
                if name in ran:
                    tried.add(key)
                    continue
                by_args[key] = uc.Scenario(
                    scenario_id=f"S-solved-{stem}-{g.line}-{where}-{k}", function=fn.name, function_line=fn.line_start,
                    gap_ids=[g.gap_id], made_by="rule", checked=True, characterization=True, test_name=name,
                    description=f"Arguments solved from the conditions on line {g.line}: {vector} [A1-PL15]",
                    required_conditions=[vector], args=args, expected="observe it",
                    purpose=f"takes {g.gap_id} with arguments solved from its conditions")
        return list(by_args.values())

    @staticmethod
    def _input_scenarios(layout: uc.LayoutProfile, req: uc.PlanRequest) -> list[uc.Scenario]:
        """A1-18c: the inputs the coverage-guided search found, as characterization scenarios (no brain)."""
        out = []
        for k, inp in enumerate(req.fuzz.inputs if req.fuzz else []):
            fn = next((f for f in layout.functions if inp.function in (f.name, f.qualified_name)), None)
            if fn is None:
                continue
            out.append(uc.Scenario(
                scenario_id=f"S-fuzz-{uc.function_stem(fn, layout)}-{req.iteration}-{k + 1}", function=fn.name,
                function_line=fn.line_start,
                gap_ids=inp.covers, description=f"Inputs found by coverage-guided search reach {', '.join(inp.covers[:4])}",
                inputs=[inp], expected="observe it", characterization=True))
        return out

    @staticmethod
    def _open_gaps(last: uc.VerificationResult) -> bool:
        return last.coverage is not None and any(fc.gaps for fc in last.coverage.functions)

    async def _available_brains(self) -> tuple[list[str], dict[str, str]]:
        """The allowed brains this run can still reach, in the brain order (ARCH-1a), and the model of each."""
        try:
            router = await self._brains()
        except ba.AgentFault:
            return [], {}
        names = [b for b in router.available if b in self.allowed_brains]
        return names, {b: router.family_of(b) for b in names}

    async def _with_brain(self, scenarios: list[uc.Scenario], active_by_fn: dict[str, list[uc.GapItem]],
                          infeasible: set[str], layout: uc.LayoutProfile, req: uc.PlanRequest, stubs: list[uc.StubSpec],
                          disputed: set[str], notes: dict[str, list[str]], deferred: list[str] | None = None
                          ) -> list[uc.Scenario]:
        """Per planned function: the brain's checked scenarios replace the rule scenarios; the coverage-guided search
        inputs and the solved scenarios (A1-PL15) stay. Without a usable answer the planning fails (BRAIN_PLAN_FAILED) and the run stops [A1-PL8].
        More functions than one round takes: the others are left for later rounds, with no scenario now [A1-PL16]."""
        sut_text = (Path(layout.repo_root) / layout.sut_file).read_text(encoding="utf-8", errors="replace")
        out = list(scenarios)
        work = []
        def mine(s: uc.Scenario, fn: uc.FunctionInfo) -> bool:            # the function, not another overload [NC-4]
            return s.function == fn.name and (s.function_line in (0, fn.line_start) or len(uc.overloads(fn, layout)) < 2)
        for name, line in list(dict.fromkeys((s.function, s.function_line) for s in scenarios if not s.inputs)):
            fn = uc.function_of(name, layout, line)
            if fn is None or fn.name != name:
                fn = next((f for f in layout.functions if f.name == name), None)
            if fn is None or any(w[1] is fn for w in work):
                continue
            gaps = active_by_fn.get(uc.function_stem(fn, layout), [])
            open_ids = {g.gap_id for g in gaps}
            if any(mine(s, fn) and s.scenario_id.startswith("S-evidence-") for s in scenarios):
                open_ids.add(f"EVIDENCE:{name}")
            if not open_ids:
                continue
            evidence = [e for e in self.collect_evidence(layout, name).evidence if uc.evidence_key(e) not in disputed]
            work.append((name, fn, gaps, open_ids, evidence))
        later = self._this_round(work, req)                                                         # A1-PL16
        if later:
            names = {w[0] for w in later}
            work = [w for w in work if w[0] not in names]
            out = [s for s in out if s.function not in names or s.inputs or s.scenario_id.startswith("S-solved-")]
            if deferred is not None:
                deferred.extend(sorted(names))
            notes["planning"].append(f"{len(work)} function(s) planned with the brain in this round, {len(later)} left "
                                     f"for later rounds: {', '.join(sorted(names)[:8])}"
                                     f"{' ...' if len(names) > 8 else ''} [A1-PL16]")
        carried: dict[str, list[uc.Scenario]] = {}                                               # A1-PL17
        shapes, fam = ({}, {})
        if self.cfg.carry_families:
            shapes = function_shapes(layout, sut_text)
            fam = function_families(shapes)
            store = self._families.setdefault(layout.sut_file, {})
            first: dict[tuple, str] = {}
            waiting: list[tuple] = []
            for w in work:
                key = fam.get(w[0])
                if key is None:
                    continue
                entry = store.get(key)
                if entry is not None:                         # a member was planned in an earlier round
                    if entry["source"] != w[0] and entry["kept"]:
                        carried[w[0]] = self._carry(w[1], w[2], w[3], entry, shapes[w[0]][1], infeasible, layout,
                                                    stubs, w[4], req.iteration)
                    continue
                if key in first:
                    waiting.append(w)                         # the family's first member is planned first
                else:
                    first[key] = w[0]
            if waiting:
                names = {w[0] for w in waiting}
                work = [w for w in work if w[0] not in names]
                out = [s for s in out if s.function not in names or s.inputs or s.scenario_id.startswith("S-solved-")]
                if deferred is not None:
                    deferred.extend(sorted(names))
                notes["planning"].append(f"{len(names)} function(s) wait for the first member of their family: "
                                         f"{', '.join(sorted(names)[:8])}{' ...' if len(names) > 8 else ''} [A1-PL17]")
            for name, found in carried.items():
                if found:
                    notes["planning"].append(f"{name}: {len(found)} scenario(s) carried over from "
                                             f"{store[fam[name]]['source']} [A1-PL17]")
            reduced = []
            for name, fn, gaps, open_ids, evidence in work:     # the brain only for what the carried ones do not aim at
                aimed = {g for s in carried.get(name, []) for g in s.gap_ids}
                reduced.append((name, fn, [g for g in gaps if g.gap_id not in aimed], open_ids - aimed, evidence))
            work = reduced
        done = [0]
        total = len(work)

        # ARCH-18: the functions are planned at the same time, each with notes of its own; the
        # results are combined below in the order of the functions, never in the order they arrive.
        kept: dict[str, list[tuple[uc.Scenario, dict]]] = {}

        def job(fn: uc.FunctionInfo, gaps: list[uc.GapItem], open_ids: set[str], evidence: list[uc.EvidenceRef]):
            async def run() -> tuple[list[uc.Scenario], dict[str, list[str]], dict[str, int]]:
                own = {key: [line for line in lines if key == "lookups" and line.startswith(fn.name + ":")]
                       for key, lines in notes.items()}
                before = {key: len(lines) for key, lines in own.items()}
                try:
                    found = await self._brain_scenarios(fn, gaps, open_ids, infeasible, layout, req, sut_text, stubs,
                                                        evidence, own, kept.setdefault(fn.name, [])) \
                        if open_ids else []                     # all of it carried over [A1-PL17]
                finally:                                                                       # VIEW-16
                    done[0] += 1
                    self.emit("stage", data={"stage": "A1.plan", "done": done[0], "of": total},
                              summary=f"UT-Analyzer: brain plans for {done[0]} of {total} functions (last: {fn.name})")
                return found, own, before
            return run
        if total > 1:                                                                              # VIEW-16
            self.emit("stage", data={"stage": "A1.plan", "done": 0, "of": total},
                      summary=f"UT-Analyzer: the brain plans {total} functions")
        results = await self.in_parallel([job(fn, gaps, open_ids, evidence) for _, fn, gaps, open_ids, evidence in work])
        if self.cfg.carry_families:                       # the family's plan, in the order of the functions [ARCH-18]
            store = self._families.setdefault(layout.sut_file, {})
            for (name, fn, gaps, *_), result in zip(work, results):
                key = fam.get(name)
                if key is None or key in store or carried.get(name):
                    continue
                by_id = {g.gap_id: g for g in gaps}
                entries = []
                for scenario, answered in kept.get(name, []):
                    offsets = [(by_id[i].metric, by_id[i].line - fn.line_start, _outcome(i))
                               for i in scenario.gap_ids if i in by_id and by_id[i].line]
                    if offsets:
                        entries.append((answered, offsets))
                store[key] = {"source": name, "names": shapes[name][1], "kept": entries,
                              "brain": next((s.made_by for s, _ in kept.get(name, [])), "brain:")}
        for (name, fn, *_), result in zip(work, results):
            own_carried = carried.get(name, [])
            if isinstance(result, ba.AgentFault) and result.code == "BRAIN_PLAN_FAILED" and own_carried:
                notes["planning"].append(f"{name}: only the carried scenarios this time - {result.message[:300]}")
                result = ([], {k: [] for k in notes}, {k: 0 for k in notes})
            if isinstance(result, ba.AgentFault) and result.code == "BRAIN_PLAN_FAILED":
                # REC-6: no model scenario was accepted; retain structural facts and continue other functions.
                notes["planning"].append(f"{name}: no scenario this time - {result.message[:400]}")
                # REC-6: a rejected proposal does not invalidate the baseline's structural facts.
                continue
            if isinstance(result, BaseException):
                raise result
            brain, own, before = result
            for key, lines in own.items():
                notes.setdefault(key, []).extend(lines[before[key]:])
            # REC-6: proposals are additive until Agent3 measures redundancy. Keep baseline controls and sequences.
            brain = own_carried + brain                                                     # A1-PL17
            behaviour = any(f"EVIDENCE:{name}" in s.gap_ids and not s.characterization for s in brain)
            out = [s for s in out if not (mine(s, fn) and s.scenario_id.startswith("S-evidence-") and behaviour)] + brain
        return out

    def _carry(self, fn: uc.FunctionInfo, gaps: list[uc.GapItem], open_ids: set[str], entry: dict[str, Any],
               names: list[str], infeasible: set[str], layout: uc.LayoutProfile, stubs: list[uc.StubSpec],
               evidence: list[uc.EvidenceRef], iteration: int) -> list[uc.Scenario]:
        """A1-PL17: the scenarios accepted for another member of the function's family, with the names mapped and the
        items mapped by their place in the function, checked like any scenario (A1-PL4)."""
        mapping = {a: b for a, b in zip(entry["names"], names) if a != b}
        constants, known = self._constants_for_values(layout), self._known_names(layout, stubs)
        ev_map = {e.evidence_id: e for e in evidence}
        by_place = {(g.metric, g.line - fn.line_start, _outcome(g.gap_id)): g.gap_id for g in gaps if g.line}

        def mapped(value: Any) -> Any:
            if isinstance(value, str):
                return carry_text(value, mapping)
            if isinstance(value, list):
                return [mapped(v) for v in value]
            if isinstance(value, dict):
                return {k: mapped(v) for k, v in value.items()}
            return value
        out = []
        for answered, offsets in entry["kept"]:
            items = [by_place[o] for o in offsets if o in by_place and by_place[o] in open_ids]
            if not items:
                continue
            data = mapped(answered)
            data["targets"], data["gap_ids"] = [], items
            try:
                planned = PlannedScenario.model_validate(data)
            except ValueError:
                continue
            scenario, _ = self._check_planned(planned, fn, open_ids, infeasible, ev_map, stubs, constants, known, layout)
            if scenario is None or scenario.trial:
                continue
            scenario.made_by = entry["brain"]
            scenario.scenario_id = f"S-carried-{uc.function_stem(fn, layout)}-{iteration}-{len(out) + 1}"
            scenario.function_line = fn.line_start
            scenario.description = (scenario.description + f" (carried over from {entry['source']} [A1-PL17])")[:300]
            out.append(scenario)
        return out

    def _this_round(self, work: list[tuple], req: uc.PlanRequest) -> list[tuple]:
        """A1-PL16: the functions left for later rounds - this round takes the configured number, more when the
        iterations left would not give every function its first brain round in their first half; never tried first,
        then the most open items, then the order of the file."""
        size = max(1, self.cfg.brain_functions_per_round)
        if len(work) <= size:
            return []
        tried = set(req.attempts)
        fresh = [w for w in work if not any(g.gap_id in tried for g in w[2])]
        rounds = max(1, (max(req.iterations_left, 0) + 1) // 2)
        size = max(size, -(-len(fresh) // rounds))
        if len(work) <= size:
            return []
        ranked = sorted(range(len(work)), key=lambda k: (any(g.gap_id in tried for g in work[k][2]),
                                                          -len(work[k][3]), work[k][1].line_start))
        keep = set(ranked[:size])
        return [w for k, w in enumerate(work) if k not in keep]

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
                scenario_id=f"S-evidence-{uc.function_stem(fn, layout)}", function=fn.name, gap_ids=[key],
                function_line=fn.line_start,
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
                    g.defect = defect_candidate(expr, "MCDC", g.condition_index, "", fn, use, path, before, proof,
                                                lines, g.line)                                    # A1-37f
            elif g.metric in ("DC", "BC"):
                seen = {o.rsplit("=", 1)[-1] for o in g.observed}
                missing = [o for o in ("T", "F") if o not in seen] if g.metric == "DC" else [g.gap_id.rsplit(":", 1)[-1]]
                index = self._leaf_at(lines[g.line - 1], expr, g.column) if g.metric == "BC" else None
                if g.metric == "BC" and index is None:
                    continue
                proofs = [prove_infeasible(expr, g.metric, index, o, fn, use, path, before) for o in missing]
                if missing and all(proofs):
                    g.detail += f" | infeasible_outcome: {'; '.join(proofs)} [A3-121a]"
                    g.defect = defect_candidate(expr, g.metric, index, missing[0], fn, use, path, before, proofs[0],
                                                lines, g.line)                                    # A1-37f
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
    def _group(gaps: list[uc.GapItem], sut_text: str = "") -> list[list[uc.GapItem]]:
        """One scenario per decision/line; the groups whose decision guards the most other open items first, then
        outer (earlier) lines [LA-10, plan section 3.3]."""
        groups: dict[tuple[int, str], list[uc.GapItem]] = {}
        for g in sorted(gaps, key=lambda g: (g.line, g.metric)):
            key = (g.line, "LC" if g.metric in ("LC", "SC") else "DEC")
            groups.setdefault(key, []).append(g)
        lines = sut_text.splitlines()
        open_lines = sorted({g.line for g in gaps})

        def guarded(line: int) -> int:
            end = block_end(lines, line)
            return sum(1 for n in open_lines if line < n <= end)
        return sorted(groups.values(), key=lambda grp: (-guarded(grp[0].line), grp[0].line))

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
    agent.resolutions = uc.resolution_memory(config_path)           # ARCH-23
    agent.answer_cache = uc.answer_cache(config_path)              # CA-2
    FrontEnd.cache = uc.run_cache(config_path, "code_analysis")     # CA-11
    uc.apply_profiles(agent, config_path)                         # BP-1, BP-3, BP-6
    return agent


if __name__ == "__main__":
    _config = next((sys.argv[i + 1] for i, a in enumerate(sys.argv[:-1]) if a == "--config"), None)
    raise SystemExit(ba.agent_main(lambda s: build_agent(s, _config), "agent_unittest_analyzer (Agent1)"))
