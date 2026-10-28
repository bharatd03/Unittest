"""All tests for the unit-test generator, in one file.

Run:  Source/UnitTestGenerator/unittest_generator.bat test   (Linux: unittest_generator.sh test)
 or:  python -m unittest -v test_unittest_generator   (in the Test folder)

Parts:
1. Contracts and shared helpers.
2. Each agent on its own: Agent1 (analyzer), Agent2 (generator), Agent3 (compilation),
   Agent4 (source corrector) and the knowledge base. A scripted brain stands in for the LLM.
3. The orchestrator: ledger, and full pipeline runs over MCP on the C and C++ pilots, including
   a seeded production defect that must be fixed on a separate branch, never by weakening a test,
   and the dependency pilot that reaches every ASIL D target over several iterations (COV-1).
3b. The brain where it adds quality, always checked: planning (A1-PL), requirement links, missing
   declarations, assembled and rewritten tests (A2-21m..q), flags of an estimated build (A3-BD11),
   several fix candidates and a second opinion (A4-23g, A4-23h).
3c. A repository link: cache, worktrees, direct commit or review pull request (ORC-17..ORC-31),
   against a local bare repository standing in for the server.
4. Tool setup (ut_setup.py): safe unpacking, checksum refusal, the tool manifest.
5. Requirements and skill: every requirement ID cited in the code exists.

Tests that need the tools of tools/toolchain.json (run setup first), FastMCP or git are skipped
with the reason when those are missing.
"""
from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import types
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

# Repository layout: Source/UnitTestGenerator (the application), Source/BaseAgent, Test (this file and
# the pilots), Docs (requirements, ORCHESTRATION.md) and .github (skills) at the root.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "Source" / "UnitTestGenerator"
DOCS = ROOT / "Docs"
os.environ.setdefault("BASEAGENT_HOME", str(ROOT / "Source" / "BaseAgent"))
sys.path.insert(0, str(SRC))

import ut_contracts as uc  # noqa: E402
from ut_contracts import ba  # noqa: E402
import agent_unittest_analyzer as a1  # noqa: E402
import agent_unittest_generator as a2  # noqa: E402
import agent_unittest_compilation as a3  # noqa: E402
import agent_sourcecode_corrector as a4  # noqa: E402
import ut_fix_knowledgebase as kb  # noqa: E402
import ut_pipeline as up  # noqa: E402
import ut_setup as us  # noqa: E402

PILOTS = HERE / "pilots"

# TP-21: no test reaches a real brain, whatever this computer offers (an API key, Ollama with Qwen). The key
# is removed from this process and the agents it starts; Qwen is pointed at an address where nothing listens.
# A test that needs a brain patches in the scripted stand-in (brain(...)).
NO_BRAIN_URL = "http://127.0.0.1:9"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("QWEN_REMOTE_API_KEY", None)                # the remote Qwen server is never reached either
_REAL_BUILD_ROUTER = ba.build_router


def _hermetic_router(settings, *, injected=None):
    qwen = settings.qwen.model_copy(update={"base_url": NO_BRAIN_URL})
    remote = settings.qwen_remote.model_copy(update={"base_url": NO_BRAIN_URL + "/v1"})
    return _REAL_BUILD_ROUTER(settings.model_copy(update={"qwen": qwen, "qwen_remote": remote}), injected=injected)


ba.build_router = _hermetic_router

# TP-20: "test" (UT_TESTS=quick) skips the tests that run a whole pipeline; "test-full" runs every test.
QUICK = os.environ.get("UT_TESTS", "full") == "quick"
# CA-1: the tests measure the agents and the scripted brain, never a cache of an earlier test; the cache tests switch
# it on for themselves
os.environ["UT_CACHE"] = "off"


def full_run(item):
    """A whole pipeline run (minutes on Windows): only in test-full [TP-20]."""
    item.ut_full_run = True
    return unittest.skipIf(QUICK, "a whole pipeline run - included in test-full [TP-20]")(item)


try:
    import fastmcp  # noqa: F401
    HAVE_FASTMCP = True
except ImportError:
    HAVE_FASTMCP = False
try:
    import langgraph.graph  # noqa: F401
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False
HAVE_GIT = shutil.which("git") is not None


def _toolchain_reason() -> str | None:
    try:
        a3.Toolchain.load(a3.CompilationConfig())
        return None
    except ba.AgentFault as exc:
        return "tools not set up: " + exc.message[:300]


TOOLCHAIN_SKIP = _toolchain_reason()


def _tools():
    return uc.Tools.load()


def have_tool(name: str) -> bool:
    try:
        return bool(_tools().command(name))
    except ba.AgentFault:
        return False


def tmpdir():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def copy_pilot(name: str, dest: Path) -> Path:
    target = dest / name
    shutil.copytree(PILOTS / name, target)
    return target


def run(coro):
    return asyncio.run(coro)


# ============================================================================ 1. scripted brain
CANNED_TESTS = {
    "thermostat_command": [{"name": "test_thermostat_heats_below_band", "scenario_ids": [],
                            "body": "ut_stub_read_sensor_return = 10;\nUT_EXPECT_EQ_INT(thermostat_command(20, 2), 1);",
                            "evidence_class": "E2"}],
    "select_mode": [{"name": "test_select_mode_nobody_home", "scenario_ids": [],
                     "body": "UT_EXPECT_EQ_INT(select_mode(0, 0, 0), MODE_ECO);", "evidence_class": "E1"}],
    "clamp_percent": [{"name": "test_clamp_percent_upper_limit", "scenario_ids": [],
                       "body": "UT_EXPECT_EQ_INT(clamp_percent(150), 100);", "evidence_class": "E2"}],
}


def planned(**kw):
    """One scenario as the brain returns it [A1-PL3], with empty defaults."""
    base = {"gap_ids": [], "name": "", "purpose": "", "args": [], "setup": [], "stubs": [], "variables": [],
            "expected_return": "", "expected_after": [], "evidence_ids": [], "quotes": [],
            "classification": "characterization_gap", "conflict": "", "information_sought": ""}
    base.update(kw)
    return base


class UTBrain:
    """Scripted stand-in for Opus/Qwen: canned tests, plans, links, declarations, flags, patches and
    answers to the typed questions (root cause, stub or real, review)."""
    brain_name, model_id, simulation = "qwen", "scripted", False

    def __init__(self, tests=None, patch=None, root_cause="source_behavior_defect", fail=None, dependency="real",
                 plans=None, links=None, declarations=None, flags=None, review="agree", candidates=None,
                 name="qwen", suggestions=None, repairs=None, values=None, headers=None, host=None, setups=None,
                 fakes=None):
        self.tests = CANNED_TESTS if tests is None else tests
        self.patch = patch or {"original_fragment": "return 99;", "corrected_fragment": "return 100;",
                               "rationale": "EV-clamp_percent-1 (E2): the value is limited to 0..100."}
        self.root_cause, self.fail, self.dependency = root_cause, fail, dependency
        self.plans, self.links, self.declarations = plans or {}, links or [], declarations or {}
        self.flags, self.review, self.candidates = flags or [], review, candidates
        self.brain_name, self.suggestions = name, suggestions
        # rule failures handed to the brain [ARCH-21]: a reply, a list of replies (one per call) or a function
        self.rule_replies = {"repair": repairs, "value": values, "header": headers, "host": host,
                             "setup": setups, "stub": fakes}                                # NC-1 building blocks
        self.generate_calls, self.decide_calls, self.questions = [], 0, []

    def _rule_reply(self, kind, task, context):
        reply = self.rule_replies[kind]
        if callable(reply):
            return reply(task, context)
        if isinstance(reply, list):
            return reply.pop(0) if reply else {}
        return reply or {}

    async def start(self):
        return None

    async def close(self):
        return None

    async def predict(self, state, questions):
        self.decide_calls += 1
        self.states = getattr(self, "states", []) + [state]
        key = next(iter(questions))
        self.questions.append(key)
        labels = list(questions[key]["criteria"])
        wanted = {"root_cause": self.root_cause, "review": self.review}.get(key, self.dependency)
        probs = {label: (0.9 if label == wanted else round(0.1 / (len(labels) - 1), 6)) for label in labels}
        return {"model": self.model_id, "answers": ba.answers_from_probabilities({key: probs}, questions),
                "usage": {"input_tokens": 1, "output_tokens": 1}}

    async def generate(self, task, context, schema, validate):
        self.generate_calls.append(task)
        if self.fail:
            raise ba.AgentFault(self.fail, "scripted failure", self.fail in ba.AVAILABILITY_FAULTS)
        if task.startswith("The unit-test program does not build"):                 # A3-CL14
            reply = self._rule_reply("repair", task, context)
        elif task.startswith("A deterministic test generator must call"):            # A2-21ac
            reply = self._rule_reply("value", task, context)
        elif task.startswith("Build the object for a unit test"):                   # NC-1 setup
            reply = self._rule_reply("setup", task, context)
        elif task.startswith("Write a fake of the C++ interface"):                  # NC-1 stub
            reply = self._rule_reply("stub", task, context)
        elif task.startswith("The C/C++ front end cannot find a header"):           # A1-DA20
            reply = self._rule_reply("header", task, context)
        elif task.startswith("This C/C++ file is target code"):                     # A3-BD16
            reply = self._rule_reply("host", task, context)
        elif "could not reach the ITEMS NOT REACHED" in task:                       # COV-12
            found = self.suggestions(context) if callable(self.suggestions) else (self.suggestions or [])
            reply = {"suggestions": found}
        elif "smallest change" in task:
            reply = {"candidates": self.candidates or [self.patch]}
        elif task.startswith("Plan unit-test scenarios"):
            fn = re.search(r"for the function (\S+)", task).group(1).split("::")[-1]
            plans = self.plans.get(fn)
            if plans is None:                       # ARCH-24: the brain always plans; a plain, valid scenario
                plans = scripted_plan(fn, context)
            reply = {"scenarios": plans(context) if callable(plans) else plans}
        elif "REQUIREMENTS below" in task:
            reply = {"links": self.links}
        elif "no declaration of it exists" in task:
            reply = {"declaration": self.declarations.get(re.search(r"symbol (\S+) is used", task).group(1), "")}
        elif "give the flags that are missing" in task:
            reply = {"flags": self.flags}
        else:
            m = re.search(r"FUNCTION UNDER TEST \([^)]*\):\n\s*\d+\|\s*(.*)", context)
            header = m.group(1) if m else ""
            reply = {"tests": next((t for name, t in self.tests.items() if re.search(r"\b" + name + r"\s*\(", header)),
                                   [])}
        return {"model": self.model_id, "output": validate(reply), "usage": {}}


def scripted_plan(fn: str, context: str) -> list[dict]:
    """The scripted brain's own plan for a function it has no canned plan for (ARCH-24: the brain always plans):
    one characterization scenario for the first open gap, with a plain value for every parameter, built only from
    the facts Agent1 gives the brain (OPEN GAPS, PARAMETERS)."""
    gaps = re.findall(r"^- (EVIDENCE:\S+): check|^- (\S+) \[", context, re.M)
    ids = [a or b for a, b in gaps]
    if not ids:
        return []
    block = re.search(r"PARAMETERS:\n((?:- .*\n?)*)", context)
    args, setup = [], []
    rows = re.findall(r"^- (\w+) \(type (.*?), (\w+)(?:; (.*?))?\)(?: - suggested values: (.*))?$",
                      block.group(1) if block else "", re.M)                 # "name (type T, kind; ...)" [ARCH-20]
    for k, row in enumerate(rows):
        name, ptype, kind, extra, suggested = row
        base = re.sub(r"\b(const|volatile)\b", "", ptype).replace("&", "").strip()
        value = suggested.split(",")[0].strip() if suggested and kind in ("int", "bool") else "0"
        if kind == "enum":
            m = re.search(r"enumerators ([^,;]+)", extra)
            value = m.group(1).strip() if m else "0"
        elif kind == "cstring":
            value = '""'
        elif kind in ("buffer", "ptr_void"):
            setup.append(f"unsigned char ut_s{k}[64] = {{0}};" if kind == "ptr_void" else f"char ut_s{k}[16] = {{0}};")
            value = f"ut_s{k}"
        elif kind in ("ptr_scalar", "ptr_struct", "struct"):
            target = base.rstrip("*").strip() if kind != "struct" else base
            setup.append(f"{target} ut_s{k} = {{0}};")
            value = f"ut_s{k}" if kind == "struct" else f"&ut_s{k}"
        elif kind not in ("int", "float", "bool"):
            value = "nullptr" if "C++" in context.split("\n", 1)[0] else "NULL"
        args.append({"name": name, "value": value})
    return [{"gap_ids": ids[:1], "name": f"test_{fn}_scripted", "purpose": "reach the first open gap",
             "args": args, "setup": setup, "stubs": [], "variables": [], "expected_return": "", "expected_after": [],
             "evidence_ids": [], "quotes": [], "classification": "characterization_gap", "conflict": "",
             "information_sought": ""}]


class DownBrain(UTBrain):
    async def start(self):
        raise ba.AgentFault("MODEL_UNAVAILABLE", "Ollama is not running", True)


@contextlib.contextmanager
def brain(b: UTBrain):
    with mock.patch.object(ba, "build_router", lambda settings, injected=None: ba.BrainRouter([b], 0.0)):
        yield b


@contextlib.contextmanager
def writer_brain(b: UTBrain):
    """A brain whose profile writes whole tests (BP-2, the full profile): only such a brain gets Agent2's test writer;
    the minimum profile (Qwen3 8B) answers in the structured form only."""
    loader = uc.brain_profiles

    def full(config_path):                           # the configuration's profiles, this brain as full
        book = loader(config_path)
        book.brains[b.brain_name] = "full"
        return book
    with brain(b), mock.patch.dict(uc.DEFAULT_BRAIN_PROFILES, {b.brain_name: "full"}), \
            mock.patch.object(uc, "brain_profiles", full):
        yield b


# ============================================================================ 2. contracts
class ContractTests(unittest.TestCase):
    def test_metric_status(self):
        self.assertEqual(uc.metric(3, 3).status, "complete")
        self.assertEqual(uc.metric(1, 3).percent, 33.33)
        self.assertEqual(uc.metric(0, 0).status, "not_applicable")
        self.assertEqual(uc.metric(0, 0, measured=False).status, "not_measured")    # never 100% [A3-60]

    def test_files_signature_is_order_independent_and_content_bound(self):
        with tmpdir() as d:
            root = Path(d)
            (root / "a.c").write_text("int a;")
            (root / "b.c").write_text("int b;")
            first = uc.files_signature(root, ["a.c", "b.c"])
            self.assertEqual(first, uc.files_signature(root, ["b.c", "a.c", "a.c"]))
            (root / "b.c").write_text("int b2;")
            self.assertNotEqual(first, uc.files_signature(root, ["a.c", "b.c"]))

    def test_test_sources_and_suite_of(self):
        suite = ('/* evidence: E2 */\nUT_TEST(test_a)\n{\n    if (1) { UT_EXPECT_TRUE(1); }\n}\n\n'
                 'UT_TEST(test_b)\n{\n}\nUT_SUITE_BEGIN\nUT_SUITE_END\n')
        tests = uc.test_sources(suite)
        self.assertEqual(list(tests), ["test_a", "test_b"])
        self.assertTrue(tests["test_a"].startswith("/* evidence: E2 */"))
        self.assertTrue(tests["test_a"].endswith("}\n}"))
        bundle = uc.FileActionBundle(bundle_id="b", actions=[uc.FileAction(path="t/x.c", action="create",
                                                                           role="testsuite", content=suite)])
        self.assertEqual(uc.suite_of(bundle), suite)
        self.assertEqual(uc.suite_of(None), "")

    def test_stale_signature_is_refused(self):
        layout = uc.LayoutProfile(repo_root="/r", sut_file="s.c", language="c", component_root=".",
                                  writable_test_dir="t", writable_stub_dir="t/s", suite_file="t/x.c", sut_sha256="a" * 64)
        ok = ba.ContractRequest(request_id=str(uuid.uuid4()), contract="X", contract_version="1.0", payload={},
                                signatures={"sut": "a" * 64})
        stale = ok.model_copy(update={"signatures": {"sut": "b" * 64}})
        uc.check_signature(layout, ok)
        with self.assertRaises(ba.ContractRejected):
            uc.check_signature(layout, stale)

    def test_routes_cover_every_category(self):
        self.assertEqual(set(uc.ROUTES), set(uc.FAILURE_CATEGORIES))      # A3-177 is complete


# ============================================================================ 3. Agent1
class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.agent = a1.AnalyzerAgent()

    def tearDown(self):
        self.tmp.cleanup()

    def discover(self, pilot, sut):
        repo = copy_pilot(pilot, Path(self.tmp.name))
        return self.agent.discover(uc.DiscoverRequest(repo_root=str(repo), sut_file=sut))

    def test_discover_c_pilot(self):
        layout = self.discover("c_pilot", "src/thermostat.c")
        self.assertEqual(layout.language, "c")
        self.assertEqual({f.name for f in layout.functions}, {"thermostat_command", "clamp_percent", "select_mode"})
        self.assertIn("include", layout.include_dirs)
        self.assertEqual(layout.suite_file, "tests/test_thermostat.c")
        self.assertFalse(layout.blocked)
        fn = next(f for f in layout.functions if f.name == "thermostat_command")
        self.assertIn("read_sensor", fn.calls)

    def test_discover_cpp_pilot(self):
        layout = self.discover("cpp_pilot", "src/rate_limiter.cpp")
        self.assertEqual(layout.language, "cpp")
        names = {f.qualified_name for f in layout.functions}
        self.assertTrue({"RateLimiter::allow", "RateLimiter::remaining", "valid_burst"} <= names, names)

    def test_mcdc_pairs_are_computed(self):
        leaves, pairs, infeasible = a1.mcdc_pairs("(window_open || !occupied) && !frost")
        self.assertEqual(len(leaves), 3)
        self.assertEqual(set(pairs), {0, 1, 2})
        self.assertEqual(infeasible, [])
        _, pairs2, infeasible2 = a1.mcdc_pairs("x > 0 && x > 0")          # coupled conditions [A3-121a]
        self.assertTrue(infeasible2)
        _, pairs3, infeasible3 = a1.mcdc_pairs("read() && !read()")       # a call may differ: not coupled
        self.assertEqual(infeasible3, [])
        self.assertEqual(set(pairs3), {0, 1})

    def test_decision_is_chosen_by_column(self):
        line = "if (a) x(); if (b && c) y();"
        self.assertEqual(a1.decision_text(line), "a")
        self.assertEqual(a1.decision_text(line, line.index("b") + 1), "b && c")

    def test_translate_flags_for_the_host(self):
        """[A3-BD7] defines, include paths, forced includes and the standard are kept; target options are
        dropped and listed; paths of the original checkout become {repo}/..."""
        mapper = uc.path_mapper(Path(self.tmp.name), ["C:/work/thermo"])
        kept, dropped = uc.translate_flags(
            ["arm-none-eabi-gcc", "-mcpu=cortex-m4", "-mthumb", "-O2", "-Wall", "-DUNIT=1", "-I", "C:/work/thermo/cfg",
             "-I../include", "-isystem", "C:/sdk/inc", "-include", "cfg.h", "-std=gnu11", "-fshort-enums", "-c",
             "src/a.c", "-o", "a.o"], "C:/work/thermo/build", mapper)
        self.assertEqual(kept, ["-DUNIT=1", "-I{repo}/cfg", "-I{repo}/include", "-isystem", "C:/sdk/inc",
                                "-include", "{repo}/build/cfg.h", "-std=gnu11", "-fshort-enums"])
        self.assertEqual(dropped, ["-mcpu=cortex-m4", "-mthumb"])
        kept, _ = uc.translate_flags(["cl.exe", "/DWIN32", "/Iinc", "/std:c++17", "/GR-", "/c", "x.cpp"],
                                     "C:/work/thermo", mapper)
        self.assertEqual(kept, ["-DWIN32", "-I{repo}/inc", "-std=c++17", "-fno-rtti"])

    def test_requirement_documents_are_e1(self):
        """[A1-25x] ReqIF and Markdown requirement documents become E1 evidence."""
        repo = copy_pilot("c_deps_pilot", Path(self.tmp.name))
        (repo / "docs/spec.reqif").write_text(
            '<REQ-IF xmlns="http://www.omg.org/spec/ReqIF/20110401/reqif.xsd"><CORE-CONTENT><REQ-IF-CONTENT>'
            '<SPEC-TYPES><SPEC-OBJECT-TYPE IDENTIFIER="t"><SPEC-ATTRIBUTES>'
            '<ATTRIBUTE-DEFINITION-STRING IDENTIFIER="a-id" LONG-NAME="ReqIF.ForeignID"/>'
            '<ATTRIBUTE-DEFINITION-STRING IDENTIFIER="a-text" LONG-NAME="ReqIF.Text"/></SPEC-ATTRIBUTES>'
            '</SPEC-OBJECT-TYPE></SPEC-TYPES><SPEC-OBJECTS><SPEC-OBJECT IDENTIFIER="o1"><VALUES>'
            '<ATTRIBUTE-VALUE-STRING THE-VALUE="CTL-9"><DEFINITION><ATTRIBUTE-DEFINITION-STRING-REF>a-id'
            '</ATTRIBUTE-DEFINITION-STRING-REF></DEFINITION></ATTRIBUTE-VALUE-STRING>'
            '<ATTRIBUTE-VALUE-STRING THE-VALUE="control clamps its result to the limit"><DEFINITION>'
            '<ATTRIBUTE-DEFINITION-STRING-REF>a-text</ATTRIBUTE-DEFINITION-STRING-REF></DEFINITION>'
            '</ATTRIBUTE-VALUE-STRING></VALUES></SPEC-OBJECT></SPEC-OBJECTS></REQ-IF-CONTENT></CORE-CONTENT></REQ-IF>')
        layout = self.agent.discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/ctrl.c"))
        self.assertIn("docs/requirements.md", layout.requirement_files)
        e1 = [e for e in self.agent.collect_evidence(layout, "in_range").evidence if e.evidence_class == "E1"]
        self.assertTrue(any(e.text.startswith("CTL-1:") and e.source == "docs/requirements.md" for e in e1), e1)
        e1 = [e for e in self.agent.collect_evidence(layout, "control").evidence if e.evidence_class == "E1"]
        self.assertTrue(any(e.text.startswith("CTL-9:") for e in e1), e1)

    def test_infeasibility_is_proven(self):
        """[A3-121a] a remaining item is excluded only with a deterministic proof."""
        fn = uc.FunctionInfo(name="f", qualified_name="f", signature="", file="", line_start=1, line_end=2, params=[
            uc.ParamInfo(name="x", type="int", kind="int"), uc.ParamInfo(name="flags", type="unsigned int", kind="int")])
        expr = "flags >= 0 && x > 5 && x < 3"
        self.assertTrue(a1.prove_infeasible(expr, "DC", None, "T", fn, {}))
        self.assertEqual(a1.prove_infeasible(expr, "DC", None, "F", fn, {}), "")
        self.assertTrue(a1.prove_infeasible(expr, "BC", 0, "F", fn, {}))         # unsigned >= 0
        self.assertTrue(a1.prove_infeasible(expr, "MCDC", 2, "", fn, {}))
        self.assertEqual(a1.prove_infeasible("(a || b) && c", "MCDC", 0, "", fn, {}), "")
        self.assertTrue(a1.prove_infeasible("LEVEL > 2 && x", "DC", None, "T", fn, {"LEVEL": 1}))
        self.assertEqual(a1.prove_infeasible("read() > 2", "DC", None, "T", fn, {}), "")   # no proof with calls

    def test_estimated_obligations_without_measurement(self):
        layout = self.discover("c_pilot", "src/thermostat.c")
        plan = run(self.agent.plan(uc.PlanRequest(layout=layout)))
        self.assertEqual(plan.stage, "coverage")
        self.assertTrue(plan.gaps and all(g.estimated for g in plan.gaps))       # A1-18a
        self.assertTrue(plan.create_suite)
        mcdc = [g for g in plan.gaps if g.metric == "MCDC" and g.function == "select_mode"]
        self.assertEqual(len(mcdc), 3)
        self.assertTrue(all(len(g.required) == 2 for g in mcdc))               # A1-18b
        self.assertEqual({s.function for s in plan.stubs}, {"read_sensor"})       # A1-22b

    def test_setup_plan_comes_first(self):
        layout = self.discover("c_pilot", "src/thermostat.c")
        probe = uc.VerificationResult(run_id="r", stage="probe", sut_sha256=layout.sut_sha256, suite_sha256="",
                                      build_ok=True, link_ok=False, undefined_symbols=["read_sensor"],
                                      decision="setup_blocked",
                                      failures=[uc.Failure(failure_id="build-0", kind="link", classification="stub_defect",
                                                           route="agent1", symbol="read_sensor")])
        plan = run(self.agent.plan(uc.PlanRequest(layout=layout, last=probe)))
        self.assertEqual(plan.stage, "setup")                                     # A1-22, A1-22a
        self.assertEqual(plan.repairs, [])          # the planned stub resolves the undefined reference
        self.assertFalse(plan.scenarios)

    def test_evidence_classes(self):
        layout = self.discover("c_pilot", "src/thermostat.c")
        ev = self.agent.collect_evidence(layout, "select_mode")
        classes = {e.evidence_class for e in ev.evidence}
        self.assertIn("E1", classes)                                              # REQ-TH-7
        self.assertIn("E2", classes)
        self.assertTrue(all(e.source and e.line for e in ev.evidence))           # A1-25b

    def test_discover_rejects_paths_outside_the_repository(self):
        if not HAVE_LANGGRAPH:
            self.skipTest("langgraph not installed")
        repo = copy_pilot("c_pilot", Path(self.tmp.name))
        resp = run(self.agent.call_local("discover", uc.DiscoverRequest(repo_root=str(repo), sut_file="../x.c")))
        self.assertEqual(resp.status, "rejected")


# ============================================================================ 4. Agent2
class RuleValueTests(unittest.TestCase):
    """A2-21r, A2-21s: the values and combinations of the tests written without a brain."""
    def test_struct_fields_get_their_own_boundaries_and_masks(self):
        body = ("if (in == NULL || (in->status & BRK_STATUS_FAULT)) return 3;\n"
                "if (in->pedal_pct > 10 && (in->speed_kph > 3 || (in->status & BRK_STATUS_HOLD))) return 2;")
        macros = {"BRK_STATUS_HOLD": "0x1u", "BRK_STATUS_FAULT": "0x2u"}
        self.assertEqual(a2.field_values("pedal_pct", body, macros), [9, 10, 11])
        self.assertEqual(a2.field_values("status", body, macros), [1, 2, 3])
        self.assertEqual(a2.int_literal("0x10UL"), 16)
        self.assertIn(16, a2.literal_values("x > 0x10u", {}))
        self.assertIn(-101, a2.literal_values("if (a < -limit_) return 100;", {}))   # negated limits

    def test_one_value_at_a_time_from_a_neutral_base_and_a_flipped_flag(self):
        combos = a2.one_at_a_time([[-1, 0, 11], [False, True]])
        self.assertEqual(combos[:2], [(0, False), (0, True)])                       # both bases first
        self.assertIn((11, False), combos)
        self.assertIn((11, True), combos)                                           # not hidden by the flag

    def test_a_stub_called_twice_gets_sequences(self):
        spec = uc.StubSpec(function="pressure_sensor", declaration="int pressure_sensor(int channel)", return_type="int",
                           header="brake_hw.h")
        layout = uc.LayoutProfile(repo_root=".", sut_file="a.c", language="c", component_root=".",
                                  writable_test_dir="tests", writable_stub_dir="tests/stubs", suite_file="tests/t.c",
                                  sut_sha256="0" * 64)
        choices = a2._stub_choices(spec, "a = pressure_sensor(0); b = pressure_sensor(1);", [0, 5, 6], layout)
        self.assertIn((0, 6), choices)
        self.assertIn((6, 0), choices)
        self.assertEqual(a2._stub_choices(spec, "a = pressure_sensor(0);", [0, 5, 6], layout), [0, 5, 6])
        combos = a2.combine([[0, -2, 50], [0, 6, (0, 6)]], 4)
        self.assertIn((50, (0, 6)), combos)                                        # a sequence with an input

    def test_stubs_used_like_a_clock_advance_and_never_include_a_source_file(self):
        """A2-21x: hal_micros() - t0 compared with ECHO_START_US / ECHO_MAX_US advances by 30000 / 8 per call;
        A2-21w: a declaration placed in the SUT .c file is never included; A2-21g: the counter stops at INT_MAX."""
        with tmpdir() as d:
            repo = copy_pilot("c_hal_pilot", Path(d))
            layout = uc.LayoutProfile(repo_root=str(repo), sut_file="src/echo_sensor.c", language="c",
                                      component_root=".", writable_test_dir="tests", writable_stub_dir="tests/stubs",
                                      suite_file="tests/test_echo_sensor.c", sut_sha256="0" * 64,
                                      headers=["include/echo_sensor.h", "include/hal.h"],
                                      compile_flags=["-I{repo}/include"])
            specs = [uc.StubSpec(function="hal_micros", declaration="uint32_t hal_micros(void)", return_type="uint32_t",
                                 header="include/hal.h"),
                     uc.StubSpec(function="hal_echo_read", declaration="int hal_echo_read(void)", return_type="int",
                                 header="include/hal.h"),
                     uc.StubSpec(function="hal_trig_write", declaration="void hal_trig_write(hal_level_t level)",
                                 return_type="void", params=[["hal_level_t", "level"]], header="src/echo_sensor.c")]
            sut = (repo / "src/echo_sensor.c").read_text()
            self.assertEqual(a2.clock_steps(specs, sut, a2.header_macros(layout)), {"hal_micros": 3750})
            _, source, header = a2.render_stubs(uc.UTPlan(plan_id="p", stage="coverage", layout=layout, stubs=specs))
        self.assertNotIn(".c\"", header)
        self.assertIn('#include "hal.h"', header)
        self.assertIn("extern uint32_t ut_stub_hal_micros_step;", header)
        self.assertIn("    ut_stub_hal_micros_step = 3750;", source)
        self.assertIn("return (uint32_t)(ut_stub_hal_micros_return + (uint32_t)ut_stub_hal_micros_step * "
                      "(uint32_t)(ut_stub_hal_micros_calls - 1));", source)
        self.assertNotIn("ut_stub_hal_echo_read_step", source)                  # not used like a clock
        self.assertIn("if (ut_stub_hal_echo_read_calls < INT_MAX) ++ut_stub_hal_echo_read_calls;", source)

    def test_every_test_starts_from_the_initial_sut_state(self):
        layout = uc.LayoutProfile(repo_root=".", sut_file="src/a.c", language="c", component_root=".",
                                  writable_test_dir="tests", writable_stub_dir="tests/stubs", suite_file="tests/test_a.c",
                                  sut_sha256="0" * 64, sut_state=[uc.StateVar(name="g_faults", type="int")])
        plan = uc.UTPlan(plan_id="p1", layout=layout, stage="coverage")
        path, access = a2.render_sut_access(plan)
        self.assertEqual(path, "tests/ut_sut_access_a.c")                                   # A2-21af
        self.assertIn("void ut_sut_reset(void)", access)
        self.assertIn("memcpy((void *)&g_faults, ut_state_0, sizeof ut_state_0);", access)
        _, stubs, header = a2.render_stubs(plan)
        self.assertIn("void ut_sut_reset(void);", header)
        self.assertIn("void ut_before_each(void)\n{\n    ut_sut_reset();", stubs)
        cpp = [uc.StateVar(name="n", type="int"), uc.StateVar(name="s", type="std::string")]
        self.assertIn("&n", uc.sut_reset_source(cpp, True))
        self.assertNotIn("&s", uc.sut_reset_source(cpp, True))                     # only plain C++ state


class AnswersThatCanBeRightTests(unittest.TestCase):
    """ARCH-20a, A1-PL3, A1-PL4: the forms that refused 124 planning answers in the pilot runs - closed lists in the
    schema, names resolved, an unusable expectation kept as characterization."""
    SRC = """#include "brk.h"
typedef unsigned int TickType_t;
TickType_t ticks(void);
void log_event(int id);
static int g_last_torque;
int brake_step(int state, const brake_input_t *in)
{
    if (in->pedal_pct > 10 && in->speed_kph > 1) { g_last_torque = state; log_event(1); return state + ticks(); }
    return 0;
}
int limit(int request, int top)
{
    return request > top ? top : request;
}
"""

    def setUp(self):
        self.tmp = tmpdir()
        self.addCleanup(self.tmp.cleanup)
        repo = Path(self.tmp.name)
        (repo / "src").mkdir()
        (repo / "include").mkdir()
        (repo / "src/brk.c").write_text(self.SRC)
        (repo / "include/brk.h").write_text("typedef struct { int pedal_pct; int speed_kph; int status; } brake_input_t;\n"
                                            "int brake_step(int state, const brake_input_t *in);\nint limit(int request, int top);\n")
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/brk.c"))
        self.layout = layout.model_copy(update={
            "sut_state": [uc.StateVar(name="ctrl::g_last_torque", type="int")], "typedefs": {"TickType_t": "unsigned int"},
            "enums": [uc.EnumInfo(name="MonitorState", values={"MonitorState::Ok": 0, "MonitorState::Fault": 1})]})
        self.fn = {f.name: f for f in self.layout.functions}
        self.stubs = [uc.StubSpec(function="ticks", declaration="TickType_t ticks(void)", return_type="TickType_t"),
                      uc.StubSpec(function="log_event", declaration="void log_event(int id)", return_type="void",
                                  params=[["int", "id"]])]
        self.agent = a1.AnalyzerAgent()
        self.known = self.agent._known_names(self.layout, self.stubs)

    def planned(self, **kw):
        return a1.PlannedScenario(**{**dict(gap_ids=["LC:brake_step:9"], name="test_brake_step_case", purpose="p",
                                            args=[], setup=[], stubs=[], variables=[], expected_return="",
                                            expected_after=[], evidence_ids=[], quotes=[],
                                            classification="characterization_gap", conflict="",
                                            information_sought=""), **kw})

    def check(self, planned, fn="brake_step", evidence=None):
        evidence = evidence or {}
        return self.agent._check_planned(planned, self.fn[fn], set(planned.gap_ids), set(), evidence, self.stubs, {},
                                         self.known, self.layout)

    def test_the_answer_schema_lists_what_exists(self):                                  # ARCH-20a, A1-PL3
        model = self.agent._plan_model(self.fn["brake_step"], {"LC:brake_step:9", "EVIDENCE:brake_step"},
                                       [uc.EvidenceRef(evidence_id="EV-brake_step-1", evidence_class="E2",
                                                       source="include/brk.h", line=2, text="x")], self.stubs)
        schema = json.dumps(ba.llm_schema(model.model_json_schema()))
        for listed in ('["EVIDENCE:brake_step", "LC:brake_step:9"]', '"enum": ["in"]', '"enum": ["state"]',
                       '["ticks"]', '["EV-brake_step-1"]'):          # one entry per parameter, typed by its type
            self.assertIn(listed, schema)                         # log_event returns nothing: not a choice
        parsed = model.model_validate({"scenarios": [self.planned(gap_ids=["XX"]).model_dump()]})
        self.assertEqual(parsed.scenarios[0].gap_ids, ["XX"])      # still parsed: the check is the judge [ARCH-20]

    def test_names_are_resolved_and_an_unusable_expectation_is_observed(self):          # A1-PL4
        fields = [a1.PlannedValue(name="state", value="2"), a1.PlannedValue(name="in->pedal_pct", value="11"),
                  a1.PlannedValue(name="in->speed_kph", value="2")]
        scenario, notes = self.check(self.planned(
            args=fields, setup=["ut_state_set_g_last_torque(0);"],             # the variable's short name
            stubs=[a1.PlannedStub(stub="ticks", returns=["0", "5"]),           # TickType_t is a scalar
                   a1.PlannedStub(stub="log_event", returns=["0"])]))          # returns nothing: dropped
        self.assertIsNotNone(scenario, notes)
        self.assertEqual({a.name: a.value for a in scenario.args}, {"state": "2", "in": "&in_v"})
        self.assertIn("in_v.pedal_pct = 11;", scenario.setup)
        self.assertIn("ut_state_set_ctrl_g_last_torque(0);", scenario.setup)
        self.assertEqual([s.stub for s in scenario.stub_values], ["ticks"])
        self.assertTrue(any("log_event return nothing" in f for f in self.agent.last_fixes))
        ev = {"EV-limit-1": uc.EvidenceRef(evidence_id="EV-limit-1", evidence_class="E1", source="r.md", line=1,
                                           text="limit returns the request below the top")}
        confirmed = dict(gap_ids=["LC:limit:14"], classification="confirmed_gap", evidence_ids=["EV-limit-1"],
                         quotes=[a1.PlannedQuote(evidence_id="EV-limit-1", text="returns the request")],
                         args=[a1.PlannedValue(name="request", value="50"), a1.PlannedValue(name="top", value="100")])
        scenario, _ = self.check(self.planned(expected_return="request", **confirmed), "limit", ev)
        self.assertEqual((scenario.expected_return, scenario.characterization), ("50", False))   # the parameter's value
        scenario, _ = self.check(self.planned(expected_after=[a1.PlannedCheck(expr="g_last_torque", value="MonitorState::Ok")],
                                              **confirmed), "limit", ev)
        self.assertEqual(scenario.expected_after[0].expr, "ut_state_get_ctrl_g_last_torque()")  # read by its getter
        scenario, notes = self.check(self.planned(expected_return="top_local_name", **confirmed), "limit", ev)
        self.assertIsNotNone(scenario)                                          # kept, observed [A1-PL4]
        self.assertTrue(scenario.characterization)
        self.assertIn("expectation not usable", " ".join(notes))
        literal, why = self.check(self.planned(setup=["int a = 100; int b = 100;", "brake_input_t v = {0};"],
                                               args=fields[:1] + [a1.PlannedValue(name="in", value="&v")],
                                               stubs=[a1.PlannedStub(stub="ticks", returns=["a", "b"])]))
        self.assertIsNotNone(literal, why)
        self.assertEqual(literal.stub_values[0].returns, ["100", "100"])
        self.assertIn("UT_EXPECT_NO_RETURN", self.known)                      # every harness macro


class LearningBetweenAttemptsTests(unittest.TestCase):
    """LA-1 .. LA-10, A2-21ae, A1-PL4, A2-21p, A3-CL14, KB-22, VIEW-11: the pieces, without a whole run."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tmpdir()
        repo = copy_pilot("cpp_embedded_pilot", Path(cls.tmp.name))
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/motor_ctrl.cpp"))
        agent = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(Path(cls.tmp.name) / "w")))
        cls.layout = run(a1.AnalyzerAgent().dependencies(uc.DependencyRequest(          # with the C++ front end
            layout=layout, build=agent.capture(uc.CaptureRequest(layout=layout)))))
        cls.fn = {f.qualified_name.replace("(anonymous namespace)::", ""): f for f in cls.layout.functions}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_cpp_objects_are_built_as_named_variables(self):                          # A2-21ae
        lines, call = uc.object_recipe(self.fn["MotorCtrl::wait_update"], self.layout)
        self.assertEqual(lines, ["ut_fake_IEncoder ut_dep0;", "MotorCtrl ut_obj(ut_dep0);"])  # never a temporary
        self.assertEqual(call, "ut_obj.wait_update")
        self.assertTrue(uc.static_member(self.fn["MotorCtrl::sat_add"], self.layout))
        self.assertIn("MotorCtrl::sat_add(...)", uc.call_form(self.fn["MotorCtrl::sat_add"], self.layout))
        self.assertTrue(uc.internal_member(self.fn["Integrator::add"], self.layout))
        self.assertIn("reach it through MotorCtrl::step", uc.call_form(self.fn["Integrator::add"], self.layout))
        self.assertIn("integ", uc.keeps_state(self.fn["MotorCtrl::step"], self.layout))
        self.assertIn("constructor takes an injected interface", uc.patterns_of(self.fn["MotorCtrl::step"], self.layout))

    def test_a_member_scenario_is_assembled_on_a_named_object(self):                  # A2-21ae, A2-21m
        plan = uc.UTPlan(plan_id="p", layout=self.layout, stage="coverage", reason="")
        sc = uc.Scenario(scenario_id="S-1", function="MotorCtrl::wait_update", description="", checked=True,
                         characterization=True, args=[uc.NamedValue(name="max_loops", value="1")],
                         setup=["ut_dep0.fault_return = false;"])
        body = a2.assemble_scenario(sc, plan, "test_wait")
        self.assertIsNotNone(body)
        self.assertLess(body.index("ut_fake_IEncoder ut_dep0;"), body.index("MotorCtrl ut_obj(ut_dep0);"))
        self.assertIn("ut_obj.wait_update(1)", body)

    def test_the_facts_tell_the_brain_how_to_reach_a_member_no_test_can_name(self):    # A2-21ae, A1-PL2
        agent = a1.AnalyzerAgent()
        step, add = self.fn["MotorCtrl::step"], self.fn["Integrator::add"]
        gaps = [uc.GapItem(gap_id=f"LC:Integrator::add:{add.line_start + 5}", function="Integrator::add", metric="LC",
                           line=add.line_start + 5, detail="line never executed")]
        sut = (Path(self.layout.repo_root) / self.layout.sut_file).read_text()
        facts = "\n".join(p.text for p in agent._facts(step, gaps, {gaps[0].gap_id}, self.layout, uc.PlanRequest(
            layout=self.layout, journal={"MotorCtrl::step": "- LC:x (attempt 1): tried t -> did not compile"}),
            sut, [], []))
        self.assertIn("REACHED THROUGH THIS FUNCTION: Integrator::add", facts)
        self.assertIn("OBJECT: a member function: declare the object", facts)
        self.assertIn("STATE: MotorCtrl::step keeps state", facts)
        self.assertIn("JOURNAL (why earlier attempts missed", facts)

    def test_a_stated_exception_is_planned_and_asserted(self):                         # structured
        """The brain picks the exception from the types the function throws (a fact of the source); the generator
        writes UT_EXPECT_THROW. A type the function does not throw is left out."""
        agent = a1.AnalyzerAgent()
        fn = self.fn["MotorCtrl::step"]
        known = agent._known_names(self.layout, [])
        ev = {"EV-step-1": uc.EvidenceRef(evidence_id="EV-step-1", evidence_class="E1", source="docs/r.md", line=1,
                                          text="A target outside -100000..100000 is refused with invalid_argument.")}
        self.assertEqual(a1.thrown_types(a1.uc.c_masked('if (x) throw std::invalid_argument("a"); /* throw X() */')),
                         ["std::invalid_argument"])

        def check(exception):
            planned = a1.PlannedScenario(gap_ids=["LC:1"], name="test_step_refuses_a_far_target", purpose="p",
                                         args=[{"name": "target", "value": "100001"}], stubs=[], variables=[],
                                         expected_return="", expected_exception=exception, expected_after=[],
                                         evidence_ids=["EV-step-1"], quotes=[{"evidence_id": "EV-step-1", "text":
                                         "refused with invalid_argument"}], classification="confirmed_gap",
                                         conflict="", information_sought="")
            return agent._check_planned(planned, fn, {"LC:1"}, set(), ev, [], {}, known, self.layout)
        scenario, _ = check("std::invalid_argument")
        self.assertEqual((scenario.expected_exception, scenario.characterization), ("std::invalid_argument", False))
        plan = uc.UTPlan(plan_id="p", layout=self.layout, stage="coverage", scenarios=[scenario])
        body = a2.assemble_scenario(scenario, plan, "test_step_refuses_a_far_target")
        self.assertIn("UT_EXPECT_THROW(ut_obj.step(100001), std::invalid_argument);", body)
        scenario, notes = check("std::out_of_range")                            # not thrown here: left out
        self.assertEqual(scenario.expected_exception, "")
        self.assertTrue(any("throws std::invalid_argument" in n for n in notes), notes)

    def test_a_scenario_refused_only_by_predictions_is_tried(self):                    # NC-6, NC-7, BP-2
        agent = a1.AnalyzerAgent()
        fn = self.fn["MotorCtrl::wait_update"]
        known = agent._known_names(self.layout, [])

        def check(**kw):
            planned = a1.PlannedScenario(**{**dict(gap_ids=["LC:1"], name="test_wait_update_case", purpose="p",
                                                   args=[{"name": "max_loops", "value": "1"}], setup=[], stubs=[],
                                                   variables=[], expected_return="",
                                                   expected_after=[], evidence_ids=[], quotes=[],
                                                   classification="characterization_gap", conflict="",
                                                   information_sought=""), **kw})
            return agent._check_planned(planned, fn, {"LC:1"}, set(), {}, [], {}, known, self.layout)
        scenario, _ = check(setup=["mystery_init();"])                       # a prediction: the tool decides
        self.assertIn("mystery_init", scenario.trial[0])
        scenario, _ = check(calls_before=["ut_obj.step(1);", "ut_obj.step(2);"])     # a call sequence [BP-2]
        self.assertEqual((scenario.calls_before, scenario.trial), (["ut_obj.step(1);", "ut_obj.step(2);"], []))
        scenario, _ = check(calls_before=['puts("x");'])
        self.assertIn("not a call of the SUT", scenario.trial[0])
        scenario, reasons = check(purpose="this gap is unreachable", setup=["mystery_init();"])   # a guard [COV-5]
        self.assertIsNone(scenario)
        self.assertNotEqual(uc.scenario_signature(check(calls_before=["ut_obj.step(1);"])[0]),
                            uc.scenario_signature(check()[0]))                  # another attempt [A1-PL4 g]

    def test_a_wider_profile_writes_the_object_and_the_minimum_keeps_the_rule(self):  # NC-1, BP-3, BP-7, BP-10
        sc = uc.Scenario(scenario_id="S-brain-MotorCtrl_wait_update-2-1", function="MotorCtrl::wait_update",
                         description="", gap_ids=["LC:MotorCtrl::wait_update:62"], checked=True, kind="setup",
                         characterization=True, made_by="brain:opus/scripted",
                         args=[uc.NamedValue(name="max_loops", value="1")])
        plan = uc.UTPlan(plan_id="p", layout=self.layout, stage="coverage", reason="", scenarios=[sc])
        block = {"lines": ["ut_fake_IEncoder ut_e;", "MotorCtrl ut_m(ut_e);"], "call": "ut_m.wait_update"}
        for name in ("opus", "qwen"):
            agent = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="llm"))
            uc.apply_profiles(agent, None)
            b = UTBrain(name=name, setups=block)
            agent.router = ba.BrainRouter([b], 0.0)
            bundle = run(agent.generate_bundle(uc.GenerationRequest(plan=plan)))
            suite = uc.suite_of(bundle)
            asked = [c for c in b.generate_calls if c.startswith("Build the object")]
            if name == "opus":                                       # the brain's block, recorded and kept
                self.assertEqual(len(asked), 1)
                self.assertIn("MotorCtrl ut_m(ut_e);", suite)
                self.assertIn("ut_m.wait_update(1)", suite)
                self.assertIn("[NC-2]", suite)
                self.assertTrue(any("setup" in m for m in bundle.made.values()))
                self.assertEqual(len(bundle.recipes), 1)
            else:                                                    # never asked; the rule's object [BP-3]
                self.assertEqual(asked, [])
                self.assertIn("MotorCtrl ut_obj(ut_dep0);", suite)
                self.assertEqual(bundle.recipes, {})

    def test_the_construct_of_an_object(self):                                        # NC-2
        signature, params = uc.construct_of(self.fn["MotorCtrl::wait_update"], self.layout)
        self.assertEqual(signature, "object: ctor(ref interface)")
        self.assertEqual((params["cls"], params["method"], params["fake0"]), ("MotorCtrl", "wait_update",
                                                                              "ut_fake_IEncoder"))
        self.assertEqual(uc.construct_of(self.fn["MotorCtrl::sat_add"], self.layout), ("", {}))   # static member

    def test_setup_form_fixes_and_harness_macros(self):                                 # A1-PL4 d, ARCH-20
        layout = self.layout.model_copy(update={"sut_state": [uc.StateVar(name="g_hidden", type="int")]})
        planned = a1.PlannedScenario(gap_ids=["LC:1"], name="t", purpose="p", args=[], setup=[
            "g_hidden = 3;", "ut_state_set_hw_regs(1);", "UT_REG_SET_LATER(UT_REG32(0x40000010u), 1u, 20);"],
            stubs=[], variables=[], expected_return="", expected_after=[], evidence_ids=[], quotes=[],
            classification="characterization_gap", conflict="", information_sought="")
        fixes = a1.AnalyzerAgent._normalize_planned(planned, self.fn["MotorCtrl::sync"], {"LC:1"}, {}, layout)
        self.assertEqual(planned.setup[0], "ut_state_set_g_hidden(3);")              # a hidden variable: its setter
        self.assertTrue(fixes)
        self.assertIn("UT_REG_SET_LATER", a1.PLAN_SAFE_CALLS)
        self.assertEqual(uc.norm_quote("/// @return true when the flag is set"), uc.norm_quote("true when the flag is  set"))

    def test_a_repeated_scenario_is_recognised(self):                                  # A1-PL4 g
        one = uc.Scenario(scenario_id="S-1", function="f", description="", gap_ids=["LC:f:3"],
                          args=[uc.NamedValue(name="a", value="1")])
        two = one.model_copy(update={"scenario_id": "S-9", "args": [uc.NamedValue(name="a", value=" 1 ")]})
        self.assertEqual(uc.scenario_signature(one), uc.scenario_signature(two))

    def test_repair_edits_name_a_generated_file_in_any_path_form(self):                # A3-CL14
        bundle = uc.FileActionBundle(bundle_id="b", actions=[uc.FileAction(
            path="tests/test_motor_ctrl.cpp", action="create", role="testsuite", content="int a = 1;\n")])
        repair = a3.BuildRepair(edits=[a3.FileEdit(path="C:\\Users\\x\\ut_work\\run\\repo\\tests\\Test_Motor_Ctrl.cpp",
                                                   old="int a = 1;", new="int a = 2;")], reason="r")
        changed, problem = a3.CompilationAgent._apply_edits(bundle, repair)
        self.assertEqual(problem, "")
        self.assertIn("int a = 2;", changed.actions[0].content)

    def test_the_journal_records_the_cause_and_gives_the_next_attempt_the_reason(self):  # LA-2 .. LA-6
        with tmpdir() as d:
            journal = up.Journal("src/motor_ctrl.cpp", Path(d))
            sc = uc.Scenario(scenario_id="S-brain-MotorCtrl_wait_update-2-1", function="MotorCtrl::wait_update",
                             description="", gap_ids=["BC:MotorCtrl::wait_update:62:13:T"], made_by="brain:qwen/q",
                             checked=True, args=[uc.NamedValue(name="max_loops", value="1")])
            plan = uc.UTPlan(plan_id="p", layout=self.layout, stage="coverage", reason="", scenarios=[sc])
            source = ("UT_TEST(test_wait)\n{\n    /* S-brain-MotorCtrl_wait_update-2-1: x */\n"
                      "    MotorCtrl ctrl(ut_fake_IEncoder()); UT_EXPECT_TRUE(ctrl.wait_update(1));\n}\n")
            gap = uc.GapItem(gap_id="BC:MotorCtrl::wait_update:62:13:T", function="MotorCtrl::wait_update",
                             metric="BC", line=62)
            result = uc.VerificationResult(run_id="r", stage="verify", sut_sha256="x", suite_sha256="",
                                           decision="gaps_remain", coverage=uc.CoverageReport(toolchain="t", definitions="d", functions=[
                                               uc.FunctionCoverage(name="MotorCtrl::wait_update", gaps=[gap])]))
            new = journal.record(2, plan, "", {"test_wait": ("no matching constructor for 'MotorCtrl'", source)},
                                 result, {gap.gap_id: 1}, self.layout, [], set())
            self.assertTrue(new)                                                   # a new cause is progress [LA-7]
            entry = journal.entries[0]
            self.assertEqual((entry.cause, entry.items), ("test_setup", [gap.gap_id]))
            excerpt = journal.excerpt()["MotorCtrl::wait_update"]
            self.assertIn("no matching constructor", excerpt)
            self.assertIn("build the object or argument as shown under OBJECT", excerpt)
            self.assertFalse(journal.record(2, plan, "", {"test_wait": ("no matching constructor for 'MotorCtrl'", source)},
                                            result, {gap.gap_id: 1}, self.layout, [], set()))     # nothing new
            status = journal.final_status(gap.gap_id, "MotorCtrl::wait_update", "", {})
            self.assertEqual((status.status, status.owner), ("pipeline_limitation", "the pipeline's developers"))
            covered = result.model_copy(update={"coverage": uc.CoverageReport(toolchain="t", definitions="d")})
            journal.record(3, uc.UTPlan(plan_id="p", layout=self.layout, stage="coverage", reason=""), "", {}, covered, {},
                           self.layout, [], set())
            self.assertEqual([e.final_status for e in journal.just_closed], ["covered"])     # LA-2: closed
            path = Path(journal.write())
            self.assertTrue(path.is_file() and path.with_suffix(".md").is_file())

    def test_attempts_then_another_model_then_a_final_status(self):                    # LA-5, LA-6
        orch = object.__new__(up.Orchestrator)
        orch.cfg = up.PipelineConfig()
        s = types.SimpleNamespace(attempts={"LC:1": 2, "LC:2": 1}, final_items=set(), other_model_items=set(),
                                  avoid_brains=set(), item_brains={"LC:1": {"qwen_remote"}})
        plan = uc.UTPlan(plan_id="p", layout=self.layout, stage="coverage", reason="")
        orch._next_attempts(s, plan, set())
        self.assertEqual((s.other_model_items, s.avoid_brains), ({"LC:1"}, {"qwen_remote"}))
        orch._next_attempts(s, plan, {"LC:1"})                   # asked, but no other model planned it
        self.assertEqual(s.final_items, {"LC:1"})

    def test_lessons_only_when_verified_and_found_by_their_pattern(self):              # KB-22, LA-8
        with tmpdir() as d:
            store = kb.KnowledgeBaseAgent(config=kb.KBConfig(db_path=str(Path(d) / "kb.sqlite3")))
            unverified = uc.LearningEvent(kind="lesson", language="cpp", framework="ut_harness", category="test_setup",
                                          snippet="build the fake first", reason="x", pattern="member function")
            resp = run(store.call_local("learn", unverified))
            self.assertEqual(resp.status, "rejected")
            verified = unverified.model_copy(update={"verification": {"verified": True, "gap_covered": True,
                                                                      "tests": "passed"}})
            self.assertTrue(store.learn(verified).stored)
            self.assertEqual(store.learn(verified).reason, "uses=2")                       # recurrence counted
            found = store.guidance(uc.GuidanceRequest(language="cpp", framework="googletest",
                                                      patterns=["member function", "language cpp"]))
            self.assertEqual([e.snippet for e in found.lessons], ["build the fake first"])
            self.assertEqual(store.guidance(uc.GuidanceRequest(language="cpp", framework="ut_harness",
                                                               patterns=["static function"])).lessons, [])

    def test_progress_line_and_unattended_questions(self):                              # VIEW-11, LA-9
        progress = up.Progress(4)
        progress.done = 1
        self.assertEqual(progress.line("a.c", 0.5, 2), "38 % - file 2 of 4 - iteration 2 - estimating the time left")
        progress.update("a.c", left=600)                                   # from the work left, not the share [VIEW-11]
        self.assertRegex(progress.line("a.c"), r"iteration 2 - about \d+-\d+ min left$")
        with mock.patch.object(sys, "stdin", None):
            self.assertEqual(up.ask_user("?", {"1": "a", "2": "b"}, 5, "1"), ("1", ""))

    def test_globals_of_real_dependencies_are_reset_before_each_test(self):           # A2-21s
        layout = self.layout.model_copy(update={"dependencies": uc.DependencyPlan(entries=[
            uc.Dependency(name="STATUS_REG", kind="variable", decision="real", definition="lib/hw.c", type="unsigned"),
            uc.Dependency(name="calib", kind="variable", decision="real", definition="lib/hw.c", is_const=True)],
            real_sources=["lib/hw.c"])})
        names = [v.name for v in uc.reset_state(layout)]
        self.assertIn("STATUS_REG", names)
        self.assertNotIn("calib", names)


class BodyCheckTests(unittest.TestCase):
    def test_well_formed_bodies_pass_the_body_check(self):                             # A2-20, A2-21t
        """The prediction "a value set twice in a row" is gone: it never fired in 143 runs (step 8)."""
        allowed = {"thermostat_command", "UT_EXPECT_EQ_INT"}
        for body in ("ut_stub_read_sensor_return = 17; ut_stub_read_sensor_return = 18; "
                     "UT_EXPECT_EQ_INT(thermostat_command(20, 2), 1);",
                     "ut_stub_read_sensor_return = 17; UT_EXPECT_EQ_INT(thermostat_command(20, 2), 1);",
                     "x = 1; thermostat_command(1, 2); x = 2;", "s.a = 1; s.b = 2;", "x = 1; x = x + 1;",
                     "x = 1; x += 2;", "volatile int k = 1; k = 2;"):
            self.assertEqual(a2.GeneratorAgent._body_problem(body, allowed), "", body)

    def test_objects_braces_in_comments_and_tests_that_check_nothing(self):               # A2-20, A2-21t, A2-24
        """From the first run on every pilot file: "MotorCtrl ctrl(enc);" constructs an object, it does not call a
        function ctrl; a purpose such as "if (sum > 5000) {" in a comment is not code; a test with no check is
        dropped."""
        body = "MotorCtrl ctrl(enc); RateLimiter lim{3, 100}; UT_EXPECT_EQ_INT(ctrl.step(lim.left()), 0);"
        allowed = {"step", "left", "UT_EXPECT_EQ_INT"} | a2.setup_names([body])
        self.assertEqual(a2.GeneratorAgent._body_problem(body, allowed), "")
        self.assertIn("ctrl", a2.GeneratorAgent._body_problem("int x = ctrl(1); UT_EXPECT_EQ_INT(x, 0);",
                                                             {"UT_EXPECT_EQ_INT"}))           # a real unknown call
        self.assertEqual(a2.GeneratorAgent._body_problem('/* if (sum > 5000) { */ UT_EXPECT_EQ_INT(f(1), 0); '
                                                         '/* "}" */', {"f", "UT_EXPECT_EQ_INT"}), "")
        check = a2.GeneratorAgent._grounding({"ut_stub_call", "f", "UT_EXPECT_EQ_INT"}, set(), function="f")
        answer = a2.GeneratedTests.model_validate({"tests": [
            {"name": "test_f_nothing_checked", "scenario_ids": [], "body": "ut_stub_call(); /* line never executed */",
             "evidence_class": "E2"},
            {"name": "test_f_returns_zero", "scenario_ids": [], "body": "UT_EXPECT_EQ_INT(f(1), 0);",
             "evidence_class": "E2"}]})
        check(answer)
        self.assertEqual([t.name for t in answer.tests], ["test_f_returns_zero"])

    def test_a_suite_with_braces_in_comments_is_valid(self):                              # A2-24
        plan = types.SimpleNamespace(layout=types.SimpleNamespace(writable_test_dir="tests", writable_stub_dir="tests/stubs"))
        suite = ('/* Cover BC at line 13: if (sum > 5000) { */\nUT_TEST(test_a) { UT_EXPECT_EQ_INT(1, 1); }\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_a)\nUT_SUITE_END\n')
        bundle = uc.FileActionBundle(bundle_id="b", actions=[uc.FileAction(
            path="tests/test_a.c", action="create", role="testsuite", content=suite)])
        self.assertIn("balanced suite", a2.GeneratorAgent.validate(bundle, plan))

    def test_no_finalize_when_every_observing_test_was_left_out(self):                    # A2-21f, A3-CL12
        """From the first run on every pilot file: the only probe was in a test that did not compile; the bundle
        goes on without the finalize step (which refuses a bundle without probes) instead of ending the run."""
        layout = types.SimpleNamespace(writable_test_dir="tests", writable_stub_dir="tests/stubs", language="cpp",
                                       framework="ut_harness", sut_sha256="abc")
        suite = ("UT_TEST(test_a) { UT_EXPECT_EQ_INT(1, 1); }\nUT_TEST(test_probe) { UT_OBSERVE_INT(\"test_probe_1\", 2); }\n"
                 "UT_SUITE_BEGIN\nUT_CASE(test_a)\nUT_CASE(test_probe)\nUT_SUITE_END\n")
        bundle = uc.FileActionBundle(bundle_id="b", observations=["test_probe_1"], tests=["test_a", "test_probe"],
                                     actions=[uc.FileAction(path="tests/test_a.cpp", action="create", role="testsuite",
                                                            content=suite)])
        observed = uc.VerificationResult(run_id="r", stage="observe", sut_sha256="abc", suite_sha256="", decision="gaps_remain",
                                       quarantined=[
            uc.Failure(failure_id="q", kind="compile", classification="test_generation_defect", route="agent1",
                       affected_test="test_probe", diagnostic="'state_' is a private member")])
        calls = []

        async def call(agent, tool, request, model):
            calls.append(tool)
            return {"generate": bundle, "observe": observed}.get(tool, uc.KBGuidance())
        orch = object.__new__(up.Orchestrator)
        orch.layout, orch.suite, orch.call, orch._stage = layout, "", call, lambda *a, **k: None
        orch.layout.functions = []
        s = types.SimpleNamespace(notes=[], quarantined=[], test_items={}, quarantine={},
                                  journal=up.Journal("a.cpp", Path(".")))
        loose = lambda **fields: fields                           # the requests are not the subject here
        with mock.patch.multiple(up.uc, GenerationRequest=loose, GuidanceRequest=loose, VerifyRequest=loose):
            out = run(orch._generate(s, types.SimpleNamespace(repairs=[], scenarios=[])))
        self.assertNotIn("finalize", calls)
        self.assertEqual(out.tests, ["test_a"])
        self.assertEqual(out.observations, [])

    def test_the_computer_stays_awake_during_a_run(self):                                  # ORC-36
        self.assertFalse(up.keep_awake()) if os.name != "nt" else None
        asked = []
        fake = types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=types.SimpleNamespace(
            SetThreadExecutionState=lambda flags: asked.append(flags) or 1)))
        with mock.patch.object(up.os, "name", "nt"), mock.patch.dict(sys.modules, {"ctypes": fake}):
            self.assertTrue(up.keep_awake())
        self.assertEqual(asked, [0x80000001])                         # ES_CONTINUOUS | ES_SYSTEM_REQUIRED

    def test_back_to_the_test_when_no_source_change_exists(self):                         # A4-23j
        from types import SimpleNamespace
        s = SimpleNamespace(open_items=set())
        failure = uc.Failure(failure_id="test-t1", kind="test", classification="source_behavior_defect",
                             route="agent4", affected_test="t1", diagnostic="expected 1, actual 0")
        up.Orchestrator._back_to_the_test(s, failure)
        up.Orchestrator._back_to_the_test(s, failure)                       # again: the same note once
        self.assertEqual((failure.classification, failure.route), ("undetermined", "agent1"))
        self.assertEqual(failure.diagnostic.count("[A4-23j]"), 1)
        self.assertEqual(len(s.open_items), 1)
        self.assertIn("t1: failed against its evidence (expected 1, actual 0)", next(iter(s.open_items)))


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        repo = copy_pilot("c_pilot", Path(self.tmp.name))
        analyzer = a1.AnalyzerAgent()
        self.layout = analyzer.discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/thermostat.c"))
        self.plan = run(analyzer.plan(uc.PlanRequest(layout=self.layout)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_stubs_are_deterministic(self):
        path, source, header = a2.render_stubs(self.plan)
        self.assertEqual(path, "tests/stubs/ut_stubs_thermostat.c")                         # A2-21af
        self.assertIn("int read_sensor(int channel)", source)
        self.assertIn("extern int ut_stub_read_sensor_return;", header)
        self.assertIn("void ut_before_each(void)", source)                       # reset before each test
        self.assertEqual(a2.render_stubs(self.plan), (path, source, header))

    def test_stubs_for_methods_constructors_and_variables(self):
        """[A2-21g, A2-21j] C++ methods and constructors under their qualified names; variables under their
        own name and exact type, const ones initialized once."""
        layout = uc.LayoutProfile(repo_root=self.tmp.name, sut_file="src/a.cpp", language="cpp", component_root=".",
                                  writable_test_dir="tests", writable_stub_dir="tests/stubs", suite_file="tests/test_a.cpp",
                                  sut_sha256="0" * 64, compile_flags=["-I{repo}/include"])
        plan = uc.UTPlan(plan_id="p", stage="coverage", layout=layout, stubs=[
            uc.StubSpec(function="now", declaration="long long now() const", return_type="long long", kind="method",
                        qualified_name="hal::Clock::now", method_suffix=" const", header="include/hal/clock.h"),
            uc.StubSpec(function="Clock", declaration="explicit Clock(int id)", return_type="", kind="constructor",
                        qualified_name="hal::Clock::Clock", params=[["int", "id"]], header="include/hal/clock.h"),
            uc.StubSpec(function="calib", declaration="extern const int calib[4]", return_type="", kind="variable",
                        qualified_name="hal::calib", var_type="const int hal::calib[4]", is_const=True,
                        initializer="{0}", header="include/hal/clock.h"),
            uc.StubSpec(function="STATUS", declaration="extern volatile unsigned STATUS", return_type="",
                        kind="variable", var_type="volatile unsigned int STATUS", header="include/hal/clock.h")])
        _, source, header = a2.render_stubs(plan)
        self.assertIn('#include "hal/clock.h"', header)                          # include path of the build
        self.assertIn("long long hal::Clock::now() const\n{", source)
        self.assertIn("extern long long ut_stub_hal_Clock_now_return;", header)
        self.assertIn("hal::Clock::Clock(int id)\n{", source)
        self.assertIn("const int hal::calib[4] = {0};", source)
        self.assertIn("volatile unsigned int STATUS;", source)
        self.assertIn("memset((void *)&STATUS, 0, sizeof STATUS);", source)       # reset before each test

    def test_first_bundle_is_deterministic_characterization(self):
        agent = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="deterministic"))
        bundle = run(agent.generate_bundle(uc.GenerationRequest(plan=self.plan)))
        self.assertFalse(bundle.claims_verification)                             # A2-33
        suite = uc.suite_of(bundle)
        self.assertIn("UT_SUITE_BEGIN", suite)
        self.assertTrue(bundle.tests)
        self.assertEqual(set(bundle.characterization_tests), set(bundle.tests))  # observed, not guessed
        self.assertTrue(bundle.observations)                                     # A2-21f
        self.assertIn("single writer per file", bundle.validation)
        self.assertEqual(agent.llm_calls, 0)

    def test_finalize_turns_observations_into_assertions(self):
        bundle = uc.FileActionBundle(bundle_id="b", actions=[uc.FileAction(
            path="tests/test_thermostat.c", action="create", role="testsuite",
            content='UT_TEST(test_x)\n{\n    UT_OBSERVE_INT("o1", clamp_percent(150));\n    UT_OBSERVE_INT("o2", f());\n}\n')],
            observations=["o1", "o2"])
        out = a2.finalize_bundle(bundle, {"o1": "100"})
        text = uc.suite_of(out)
        self.assertIn("UT_EXPECT_EQ_INT(clamp_percent(150), 100);", text)
        self.assertIn("not observed", text)
        self.assertEqual(out.observations, [])

    def test_llm_tests_are_grounded(self):
        b = UTBrain(tests={"select_mode": [
            {"name": "test_bad", "scenario_ids": [], "body": "UT_EXPECT_EQ_INT(invented_api(1), 2);", "evidence_class": "E2"}]})
        agent = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="llm"))
        with writer_brain(b):
            bundle = run(agent.generate_bundle(uc.GenerationRequest(plan=self.plan)))
        # A2-20, A2-21: an unknown name is a prediction of the compiler's verdict - the test is not used as it is,
        # it is built alone in a trial and the tool decides [NC-6, NC-7]
        self.assertEqual(list(bundle.trial), ["test_select_mode_bad"])
        self.assertIn("invented_api", bundle.trial["test_select_mode_bad"])

    def test_one_faulty_test_does_not_cost_the_others(self):
        """[A2-21t, A2-21q] the answer is checked test by test: a long or duplicate name is repaired, a test
        with an invented call goes to a trial build (NC-7), the others are used - without asking the brain again."""
        long_name = "test_select_mode_returns_eco_when_nobody_is_home_and_the_window_is_open_and_it_is_night"
        body = "UT_EXPECT_EQ_INT(select_mode(0, 0, 0), MODE_ECO);"
        b = UTBrain(tests={"select_mode": [
            {"name": long_name, "scenario_ids": [], "body": body, "evidence_class": "E1"},
            {"name": "test_select_mode_invents", "scenario_ids": [], "body": "UT_EXPECT_EQ_INT(invented_api(1), 2);",
             "evidence_class": "E2"},
            {"name": "test_select_mode_nobody_home", "scenario_ids": [], "body": body, "evidence_class": "E1"},
            {"name": "test_select_mode_nobody_home", "scenario_ids": [], "body": body, "evidence_class": "E1"},
            {"name": "select mode/away", "scenario_ids": [], "body": body, "evidence_class": "E1"}]})
        agent = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="llm", max_llm_tests_per_function=8))
        mode = [sc for sc in self.plan.scenarios if sc.function == "select_mode"]
        extra = [mode[0].model_copy(update={"scenario_id": f"S-x{k}", "checked": False, "made_by": "rule",
                                            "characterization": False}) for k in range(5)]
        plan = self.plan.model_copy(update={"scenarios": self.plan.scenarios + extra})   # five tests asked [ARCH-11a]
        with writer_brain(b):
            bundle = run(agent.generate_bundle(uc.GenerationRequest(plan=plan)))
        suite = uc.suite_of(bundle)
        self.assertEqual(list(bundle.trial), ["test_select_mode_invents"])          # the tool decides [NC-7]
        names = re.findall(r"UT_TEST\((\w+)\)", suite)
        self.assertTrue(all(len(n) <= a2.NAME_LIMIT for n in names), names)
        self.assertIn("test_select_mode_returns_eco_when_nobody_is_home_and_the_window", names)
        self.assertIn("test_select_mode_nobody_home", names)
        self.assertIn("test_select_mode_nobody_home_2", names)
        self.assertIn("test_select_mode_away", names)
        self.assertTrue(any("test_select_mode_invents refused only by predictions" in n and "NC-7" in n
                            for n in bundle.notes), bundle.notes)
        self.assertEqual(sum("select_mode" in t for t in b.generate_calls), 1)      # no second ask

    def test_tests_asked_at_the_same_time_give_the_same_suite(self):
        """ARCH-18: several functions' tests are asked for at once; the answers arrive in another order,
        yet the suite is the same as with one call at a time, and the names stay unique."""
        class ParallelBrain(UTBrain):
            def __init__(self, parallel, delays, **kw):
                super().__init__(**kw)
                self.parallel_calls, self.delays, self.running, self.peak = parallel, delays, 0, 0

            async def generate(self, task, context, schema, validate):
                fn = next((name for name in self.delays if f"tests for {name} " in task), "")
                self.running += 1
                self.peak = max(self.peak, self.running)
                try:
                    await asyncio.sleep(self.delays.get(fn, 0))
                    return await super().generate(task, context, schema, validate)
                finally:
                    self.running -= 1

        same_name = {fn: [{"name": "test_shared_name_case", "scenario_ids": [], "body": body, "evidence_class": "E8"}]
                     for fn, body in (("select_mode", "UT_OBSERVE_INT(\"o1\", select_mode(0, 0, 0));"),
                                      ("clamp_percent", "UT_OBSERVE_INT(\"o2\", clamp_percent(150));"),
                                      ("thermostat_command", "UT_OBSERVE_INT(\"o3\", thermostat_command(20, 2));"))}
        suites = []
        for parallel, delays in ((1, {}), (3, {"thermostat_command": 0.3, "select_mode": 0.1, "clamp_percent": 0.0})):
            b = ParallelBrain(parallel, delays, tests=same_name)
            agent = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="llm"))
            with writer_brain(b):
                bundle = run(agent.generate_bundle(uc.GenerationRequest(plan=self.plan)))
            suites.append(uc.suite_of(bundle))
            if parallel > 1:
                self.assertGreater(b.peak, 1)                                   # really at the same time
        self.assertEqual(suites[0], suites[1])
        names = re.findall(r"UT_TEST\((\w+)\)", suites[1])
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("test_shared_name_case_2", names)

    def test_expected_values_must_be_stated_by_the_cited_evidence(self):
        """A2-21u: an expected value the LLM invented is observed instead of asserted; a stated one stays."""
        tqm2 = "TQM-2: the maximum torque for an engine speed, interpolated linearly; 0 below 0 rpm and above 6000 rpm."
        tqm4 = "TQM-4: a request is valid when the speed lies in 500..6000 rpm and the request in 0..400 Nm"
        ground = a2.ground_expectations
        body, n = ground("UT_EXPECT_EQ_DBL(TorqueMap().max_torque(4000), 300.0, 1e-6); /* EV-max_torque-1 */",
                         "t", [tqm2], False)
        self.assertEqual((body, n), ('UT_OBSERVE_DBL("t_g1", TorqueMap().max_torque(4000));', 1))   # 300 is invented
        self.assertEqual(ground("UT_EXPECT_EQ_INT(TorqueMap().max_torque(-1), 0);", "t", [tqm2], False)[1], 0)
        self.assertEqual(ground("UT_EXPECT_EQ_INT(valid_request(500, 200, true, false), true);", "t", [tqm4],
                                True)[1], 0)                                     # bool function, E1-E3 evidence
        body, n = ground("UT_EXPECT_EQ_INT(valid_request(500, 200, true, false), true);", "t", [], False)
        self.assertEqual(body, 'UT_OBSERVE_INT("t_g1", valid_request(500, 200, true, false));')    # E8: never asserted
        body, n = ground("UT_EXPECT_TRUE(map.limit(1000, 50, false) == 77);", "t", [tqm2], False)
        self.assertEqual(body, 'UT_OBSERVE_INT("t_g1", map.limit(1000, 50, false));')
        body, n = ground('UT_EXPECT_EQ_STR(name(), "abc");', "t", ["xyz"], False)
        self.assertIn("not asserted [A2-21u]", body)
        b = UTBrain(tests={"clamp_percent": [{"name": "test_clamp_percent_invented_limit", "scenario_ids": [],
                                              "body": "UT_EXPECT_EQ_INT(clamp_percent(150), 77);",
                                              "evidence_class": "E2"}]})
        with writer_brain(b):
            bundle = run(a2.GeneratorAgent(config=a2.GeneratorConfig(mode="llm")).generate_bundle(
                uc.GenerationRequest(plan=self.plan)))
        suite = uc.suite_of(bundle)
        self.assertNotIn("clamp_percent(150), 77", suite)
        self.assertIn("test_clamp_percent_invented_limit_g1", bundle.observations)        # observed (A2-21f)
        self.assertIn("test_clamp_percent_invented_limit", bundle.characterization_tests)   # E8, not E2
        self.assertTrue(any("A2-21u" in n for n in bundle.notes), bundle.notes)

    def test_readable_test_name_repairs_instead_of_rejecting(self):
        """[A2-21q] deterministic repair; the result is a valid C identifier within 63 characters."""
        self.assertEqual(a2.readable_test_name("test_clamp_percent_upper_limit", "clamp_percent", set()),
                         "test_clamp_percent_upper_limit")
        self.assertEqual(a2.readable_test_name("limit!", "clamp_percent", set()), "test_clamp_percent_limit")
        self.assertEqual(a2.readable_test_name("", "ns::clamp", set()), "test_ns_clamp_checks_behaviour")
        cut = a2.readable_test_name("test_" + "_".join(["word"] * 30), "f", set())
        self.assertLessEqual(len(cut), 63)
        self.assertTrue(re.fullmatch(r"test(_word)+", cut))
        taken = {"test_a_b"}
        self.assertEqual(a2.readable_test_name("test_a_b", "f", taken), "test_a_b_2")
        self.assertLessEqual(len(a2.readable_test_name("test_" + "x" * 80, "f", set())), 63)

    def test_llm_tests_keep_their_evidence_class(self):
        agent = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="llm"))
        with writer_brain(UTBrain()):
            bundle = run(agent.generate_bundle(uc.GenerationRequest(plan=self.plan)))
        suite = uc.suite_of(bundle)
        self.assertIn("UT_TEST(test_select_mode_nobody_home)", suite)
        classes = next(a for a in bundle.actions if a.role == "testsuite").evidence_classes
        self.assertEqual(classes["test_select_mode_nobody_home"], "E1")         # A2-21d
        self.assertNotIn("test_select_mode_nobody_home", bundle.characterization_tests)

    def test_validation_rejects_paths_outside_writable_dirs(self):
        bundle = uc.FileActionBundle(bundle_id="b", actions=[
            uc.FileAction(path="src/thermostat.c", action="replace", role="testsuite", content="x")])
        with self.assertRaises(ba.AgentFault):
            a2.GeneratorAgent.validate(bundle, self.plan)

    def test_blocked_plan_is_refused(self):
        if not HAVE_LANGGRAPH:
            self.skipTest("langgraph not installed")
        blocked = self.plan.model_copy(update={"stage": "blocked"})
        resp = run(a2.GeneratorAgent().call_local("generate", uc.GenerationRequest(plan=blocked)))
        self.assertEqual(resp.status, "rejected")                                 # A2-4


# ============================================================================ 5. Agent3
@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
class CompilationTests(unittest.TestCase):
    STUB = ('#include "ut_harness.h"\n#include "sensor.h"\nint ut_stub_read_sensor_return;\n'
            'int read_sensor(int channel) { (void)channel; return ut_stub_read_sensor_return; }\n'
            'void ut_before_each(void) { ut_stub_read_sensor_return = 0; }\n')

    def setUp(self):
        self.tmp = tmpdir()
        repo = copy_pilot("c_pilot", Path(self.tmp.name))
        self.layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/thermostat.c"))
        self.agent = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(Path(self.tmp.name) / "work")))

    def tearDown(self):
        self.tmp.cleanup()

    def bundle(self, tests: str, classes=None):
        suite = ('#include "ut_harness.h"\n#include "thermostat.h"\n#include "sensor.h"\n'
                 'extern int ut_stub_read_sensor_return;\n' + tests)
        return uc.FileActionBundle(bundle_id="b", actions=[
            uc.FileAction(path="tests/stubs/ut_stubs.c", action="create", role="stub", content=self.STUB),
            uc.FileAction(path="tests/test_thermostat.c", action="create", role="testsuite", content=suite,
                          evidence_classes=classes or {})])

    def test_probe_reports_missing_stub(self):
        r = self.agent.execute(uc.VerifyRequest(layout=self.layout, stage="probe"))
        self.assertEqual(r.decision, "setup_blocked")                            # A3-PR2
        self.assertIn("read_sensor", r.undefined_symbols)
        self.assertEqual({f.classification for f in r.failures}, {"stub_defect"})
        self.assertTrue(Path(r.manifest).is_file())                              # A3-29a

    def test_verify_measures_and_classifies(self):
        b = self.bundle('UT_TEST(test_heat) { ut_stub_read_sensor_return = 10; UT_EXPECT_EQ_INT(thermostat_command(20, 2), 1); }\n'
                        'UT_TEST(test_char) { UT_EXPECT_EQ_INT(clamp_percent(50), 51); }\n'
                        'UT_SUITE_BEGIN\nUT_CASE(test_heat) UT_CASE(test_char)\nUT_SUITE_END\n')
        r = self.agent.execute(uc.VerifyRequest(layout=self.layout, stage="verify", bundle=b))
        self.assertTrue(r.build_ok and r.link_ok)
        self.assertEqual((r.total, r.passed, r.failed), (2, 1, 1))
        self.assertIsNotNone(r.coverage)
        self.assertEqual(r.coverage.file_metrics.LC.status, "incomplete")
        failure = next(f for f in r.failures if f.affected_test == "test_char")
        self.assertEqual(failure.classification, "test_generation_defect")      # E8 never a source defect [A1-25g]
        self.assertEqual(failure.route, "agent1")
        self.assertFalse(r.verified)
        run_dir = Path(r.run_dir)
        self.assertTrue((run_dir / "junit.xml").is_file())                       # A3-29c

    def test_tests_that_do_not_compile_are_left_out_and_the_rest_measured(self):
        tests = ('/* evidence: E8 */\nUT_TEST(test_heat)\n{\n    ut_stub_read_sensor_return = 10;\n'
                 '    UT_EXPECT_EQ_INT(thermostat_command(20, 2), 1);\n}\n\n'
                 '/* evidence: E8 */\nUT_TEST(test_broken)\n{\n    UT_EXPECT_EQ_INT(no_such_value, 1);\n}\n\n'
                 'UT_SUITE_BEGIN\n    UT_CASE(test_heat)\n    UT_CASE(test_broken)\nUT_SUITE_END\n')
        b = self.bundle(tests)
        with self.assertLogs(level="WARNING") as logs:
            r = self.agent.execute(uc.VerifyRequest(layout=self.layout, stage="verify", bundle=b))
        self.assertTrue(any("A3-CL12" in line for line in logs.output))
        self.assertTrue(r.build_ok and r.link_ok)                                    # A3-CL12
        self.assertEqual((r.total, r.passed), (1, 1))
        self.assertEqual([f.affected_test for f in r.quarantined], ["test_broken"])
        q = r.quarantined[0]
        self.assertEqual((q.classification, q.kind, q.route), ("test_generation_defect", "compile", "agent1"))
        self.assertIn("no_such_value", q.diagnostic)
        self.assertFalse(any(f.affected_test == "test_broken" for f in r.failures))
        self.assertIsNotNone(r.coverage)
        # the suite Agent3 measured is exactly the bundle the orchestrator keeps
        kept = uc.without_tests(b, ["test_broken"])
        self.assertEqual(r.suite_sha256, uc.files_signature(Path(r.run_dir) / "repo", ["tests/stubs/ut_stubs.c",
                                                                                    "tests/test_thermostat.c"]))
        self.assertEqual((Path(r.run_dir) / "repo" / "tests/test_thermostat.c").read_text(encoding="utf-8"),
                         uc.suite_of(kept))
        self.assertNotIn("test_broken", uc.suite_of(kept))
        self.assertIn("UT_CASE(test_heat)", uc.suite_of(kept))

    def test_errors_outside_tests_are_not_quarantined(self):
        tests = ('UT_TEST(test_heat) { UT_EXPECT_EQ_INT(thermostat_command(20, 2), 1); }\n'
                 'int broken_helper = no_such_value;\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_heat)\nUT_SUITE_END\n')
        r = self.agent.execute(uc.VerifyRequest(layout=self.layout, stage="verify", bundle=self.bundle(tests)))
        self.assertFalse(r.link_ok)
        self.assertEqual(r.quarantined, [])
        self.assertTrue(r.reason.startswith("the build did not link: "), r.reason)
        self.assertIn("no_such_value", r.reason)                           # the compiler's own message [A3-29d]
        only = ('UT_TEST(test_broken) { UT_EXPECT_EQ_INT(no_such_value, 1); }\n'
                'UT_SUITE_BEGIN\nUT_CASE(test_broken)\nUT_SUITE_END\n')            # nothing would be left
        r = self.agent.execute(uc.VerifyRequest(layout=self.layout, stage="verify", bundle=self.bundle(only)))
        self.assertFalse(r.link_ok)
        self.assertEqual(r.quarantined, [])
        two = ('UT_TEST(test_heat) { UT_EXPECT_EQ_INT(thermostat_command(20, 2), 1); }\n'
               'UT_TEST(test_broken) { UT_EXPECT_EQ_INT(no_such_value, 1); }\n'
               'UT_SUITE_BEGIN\nUT_CASE(test_heat)\nUT_CASE(test_broken)\nUT_SUITE_END\n')
        source = (Path(self.layout.repo_root) / "src/thermostat.c").read_text(encoding="utf-8")
        r = self.agent.execute(uc.VerifyRequest(layout=self.layout, stage="verify", bundle=self.bundle(two),
                                                source_overrides={"src/thermostat.c": source}))
        self.assertFalse(r.link_ok)                                     # a source-fix trial is never quarantined
        self.assertEqual(r.quarantined, [])

    HANG_STUB = ('#include "ut_harness.h"\n#include "sensor.h"\nint ut_stub_read_sensor_return;\n'
                 'int read_sensor(int channel) { (void)channel; ut_stub_call(); return ut_stub_read_sensor_return; }\n'
                 'void ut_before_each(void) { ut_stub_read_sensor_return = 0; }\n')

    def hang_bundle(self, tests: str):
        b = self.bundle(tests)
        b.actions[0].content = self.HANG_STUB
        return b

    def test_a_test_over_its_stub_call_budget_ends_and_the_next_one_runs(self):          # A3-45b
        tests = ('UT_TEST(test_waits_forever) { while (read_sensor(0) == 0) { } }\n'
                 'UT_TEST(test_after) { UT_EXPECT_EQ_INT(clamp_percent(50), 50); }\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_waits_forever)\nUT_CASE(test_after)\nUT_SUITE_END\n')
        agent = a3.CompilationAgent(config=self.agent.cfg.model_copy(update={"stub_call_budget": 1000}))
        r = agent.execute(uc.VerifyRequest(layout=self.layout, stage="verify", bundle=self.hang_bundle(tests)))
        status = {t.name: t for t in r.tests}
        self.assertEqual(status["test_waits_forever"].status, "timeout")
        self.assertIn("stub call budget exceeded (1000 calls)", status["test_waits_forever"].message)
        self.assertEqual(status["test_after"].status, "passed")
        self.assertTrue(r.run_ok)                                          # the program itself ended normally

    def test_after_an_abnormal_end_every_test_runs_alone(self):                           # A3-45a
        tests = ('UT_TEST(test_first) { UT_EXPECT_EQ_INT(clamp_percent(50), 50); }\n'
                 'UT_TEST(test_crash) { volatile int *p = 0; *p = 1; }\n'
                 'UT_TEST(test_spins) { volatile int k = 1; while (k) { } }\n'
                 'UT_TEST(test_last) { UT_EXPECT_EQ_INT(clamp_percent(-5), 0); }\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_first)\nUT_CASE(test_crash)\nUT_CASE(test_spins)\nUT_CASE(test_last)\n'
                 'UT_SUITE_END\n')
        agent = a3.CompilationAgent(config=self.agent.cfg.model_copy(update={"run_timeout": 5, "test_timeout": 2}))
        with self.assertLogs(level="WARNING") as logs:
            r = agent.execute(uc.VerifyRequest(layout=self.layout, stage="verify", bundle=self.hang_bundle(tests)))
        status = {t.name: t for t in r.tests}
        self.assertEqual(set(status), {"test_first", "test_crash", "test_spins", "test_last"})   # none lost
        self.assertEqual((status["test_first"].status, status["test_last"].status), ("passed", "passed"))
        self.assertEqual(status["test_crash"].status, "crashed")
        if os.name != "nt":
            self.assertIn("SIGSEGV", status["test_crash"].message)
        self.assertEqual(status["test_spins"].status, "timeout")
        # ended by Agent3's time limit for the single run (2 s) or by the harness's own time limit per test
        # (A3-45d, also 2 s): whichever comes first, both are a timeout
        self.assertRegex(status["test_spins"].message, r"within 2 s|time limit")
        self.assertTrue(any("one by one [A3-45a]" in line for line in logs.output))
        self.assertIsNotNone(r.coverage)                                   # the tests after the crash count

    def test_a_sut_compiled_twice_is_named_with_the_linker_message(self):                # A3-CL13, A3-29d
        tests = ('UT_TEST(test_a) { UT_EXPECT_EQ_INT(clamp_percent(50), 50); }\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_a)\nUT_SUITE_END\n')
        b = self.bundle(tests)
        b.actions[0].content = '#include "../../src/thermostat.c"\n' + self.STUB    # what issue 1 produced
        r = self.agent.execute(uc.VerifyRequest(layout=self.layout, stage="verify", bundle=b))
        self.assertFalse(r.link_ok)
        self.assertRegex(r.reason, r"the build did not link: .*(multiple definition|duplicate symbol)")
        twice = [f for f in r.failures if any("[A3-CL13]" in e for e in f.evidence)]
        self.assertTrue(twice, [f.model_dump() for f in r.failures])
        self.assertEqual({f.classification for f in twice}, {"test_framework_defect"})
        self.assertTrue((Path(r.run_dir) / "build_output.txt").is_file())

    def test_e2_failure_is_decided_by_the_brain(self):
        b = self.bundle('UT_TEST(test_clamp) { UT_EXPECT_EQ_INT(clamp_percent(150), 99); }\n'
                        'UT_SUITE_BEGIN\nUT_CASE(test_clamp)\nUT_SUITE_END\n', {"test_clamp": "E2"})
        with brain(UTBrain(root_cause="test_generation_defect")) as fake:
            resp = run(self.agent.call_local("verify", uc.VerifyRequest(layout=self.layout, stage="verify", bundle=b)))
        self.assertEqual(resp.status, "success", resp.error)
        r = uc.VerificationResult.model_validate(resp.data)
        failure = next(f for f in r.failures if f.affected_test == "test_clamp")
        self.assertEqual(failure.classification, "test_generation_defect")
        self.assertEqual(fake.decide_calls, 1)
        self.assertIn("UT_EXPECT_EQ_INT(clamp_percent(150), 99);", str(fake.states[0]))   # its own code [A3-156]
        self.assertTrue(resp.meta.model_invoked)

    def test_no_brain_means_undetermined_not_guessed(self):
        b = self.bundle('UT_TEST(test_clamp) { UT_EXPECT_EQ_INT(clamp_percent(150), 99); }\n'
                        'UT_SUITE_BEGIN\nUT_CASE(test_clamp)\nUT_SUITE_END\n', {"test_clamp": "E2"})
        with brain(DownBrain()):                                  # no brain can decide
            r = uc.VerificationResult.model_validate(run(self.agent.call_local(
                "verify", uc.VerifyRequest(layout=self.layout, stage="verify", bundle=b))).data)
        self.assertEqual(next(f for f in r.failures if f.affected_test == "test_clamp").classification, "undetermined")

    def test_observe_returns_values(self):
        b = self.bundle('UT_TEST(test_o) { UT_OBSERVE_INT("v1", clamp_percent(-5)); }\n'
                        'UT_SUITE_BEGIN\nUT_CASE(test_o)\nUT_SUITE_END\n')
        r = self.agent.execute(uc.VerifyRequest(layout=self.layout, stage="observe", bundle=b))
        self.assertEqual(r.observed, {"v1": "0"})                                # A3-OBS1
        self.assertFalse(r.verified)                                             # A3-OBS4

    def test_bundle_outside_test_dirs_is_rejected(self):
        if not HAVE_LANGGRAPH:
            self.skipTest("langgraph not installed")
        b = uc.FileActionBundle(bundle_id="b", actions=[uc.FileAction(
            path="src/evil.c", action="create", role="testsuite", content="int x;")])
        resp = run(self.agent.call_local("verify", uc.VerifyRequest(layout=self.layout, stage="verify", bundle=b)))
        self.assertEqual(resp.status, "rejected")

    def test_oracle_check_detects_weakening(self):
        original = self.bundle('UT_TEST(test_clamp) { UT_EXPECT_EQ_INT(clamp_percent(150), 100); UT_EXPECT_EQ_INT(clamp_percent(-1), 0); }\n'
                               'UT_SUITE_BEGIN\nUT_CASE(test_clamp)\nUT_SUITE_END\n')
        weaker_suite = uc.suite_of(original).replace(" UT_EXPECT_EQ_INT(clamp_percent(-1), 0);", "")
        res = self.agent.oracle_check(uc.OracleRequest(layout=self.layout, test="test_clamp", function="clamp_percent",
                                                       original_suite=uc.suite_of(original), changed_suite=weaker_suite,
                                                       bundle=original))
        self.assertEqual(res.decision, "oracle_weakening")                       # A3-85d
        same = self.agent.oracle_check(uc.OracleRequest(layout=self.layout, test="test_clamp", function="clamp_percent",
                                                        original_suite=uc.suite_of(original),
                                                        changed_suite=uc.suite_of(original), bundle=original))
        self.assertEqual(same.decision, "accepted")
        self.assertGreater(same.mutants, 0)

    # ---------------------------------------------------------------- fault check of suggestions [COV-14..19]
    CLAMP_TESTS = ('UT_TEST(test_clamp) { UT_EXPECT_EQ_INT(clamp_percent(150), 100); '
                   'UT_EXPECT_EQ_INT(clamp_percent(-1), 0); UT_EXPECT_EQ_INT(clamp_percent(50), 50); }\n'
                   'UT_SUITE_BEGIN\nUT_CASE(test_clamp)\nUT_SUITE_END\n')

    def fault(self, change, agent=None):
        original = (Path(self.layout.repo_root) / self.layout.sut_file).read_text()
        return (agent or self.agent).fault_check(uc.FaultCheckRequest(
            layout=self.layout, bundle=self.bundle(self.CLAMP_TESTS), suggestion_id="cov-test",
            original_source=original, suggested_source=change(original), tests=["test_clamp"]))

    def test_fault_check_keeps_a_suggestion_that_detects_as_much(self):            # COV-15 .. COV-18
        res = self.fault(lambda s: s.replace("    return value;\n", "    /* in range */\n    return value;\n"))
        self.assertEqual(res.decision, "kept", res.reason)
        self.assertEqual(res.functions, ["clamp_percent"])
        self.assertGreater(res.detected_before, 0)
        self.assertEqual(res.still_detected, res.detected_before)
        self.assertIn(f"{res.still_detected} of {res.detected_before} compared mutants still detected", res.summary())
        moved = [m for m in res.mutants if m.label.endswith("return->0") and m.suggested_line != m.original_line]
        self.assertTrue(moved, "the line after the inserted one is still paired [COV-16]")
        self.assertTrue(res.operators and res.compiler and res.workers >= 1)               # COV-18

    def test_fault_check_rejects_a_suggestion_that_hides_a_check(self):             # COV-17
        early = "{\n    if (value == 150) {\n        return 100;\n    }\n    if (value < 0) {"
        res = self.fault(lambda s: s.replace("{\n    if (value < 0) {", early, 1))
        self.assertEqual(res.decision, "weakened", res.summary())
        self.assertTrue(any("return->0" in m for m in res.missed), res.missed)
        self.assertIn("no longer detected", res.summary())

    def test_a_failing_test_is_mapped_to_the_method_it_calls(self):
        """A failing C++ assertion on TorqueMap().max_torque(...) belongs to max_torque, not to the
        constructor and not to nobody (the corrector refused an empty function name)."""
        repo = copy_pilot("cpp_pilot", Path(self.tmp.name) / "cpp")
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/torque_map.cpp"))
        names = {f.name for f in layout.functions}
        test = uc.TestResult(name="test_TorqueMap_max_torque_returns_interpolated_value_for_rpm_2", status="failed",
                             message="UT_EXPECT_EQ_DBL(TorqueMap().max_torque(4000), 300.0)")
        found = self.agent._function_of_test(layout, test, repo)
        self.assertEqual(found, next(n for n in names if n.endswith("max_torque")))
        free = uc.TestResult(name="test_x", status="failed", message="UT_EXPECT_EQ_INT(valid_request(1, 2, true, false), 1)")
        self.assertEqual(self.agent._function_of_test(layout, free, repo), "valid_request")

    def test_fault_check_is_na_when_every_line_with_a_mutant_changes(self):        # COV-17
        def rewrite(s):
            return (s.replace("if ((window_open || !occupied) && !frost) {",
                              "if ((window_open != 0 || occupied == 0) && frost == 0) {")
                    .replace("return MODE_ECO;", "return (MODE_ECO);").replace("return MODE_COMFORT;", "return (MODE_COMFORT);"))
        res = self.fault(rewrite)
        self.assertEqual(res.decision, "not_applicable", res.summary())
        self.assertGreater(res.changed_line_mutants, 0)
        self.assertIn("NA", res.summary())

    def test_fault_check_over_the_limit_is_not_checked(self):                       # COV-17, A3-85h
        small = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(Path(self.tmp.name) / "w2"),
                                                                max_mutants=1))
        res = self.fault(lambda s: s.replace("    return value;\n", "    /* in range */\n    return value;\n"), small)
        self.assertEqual(res.decision, "not_checked")
        self.assertIn("limit", res.reason)

    def test_a_blocked_test_program_is_reported_once(self):                         # TP-9a
        real, runs = a3._run, []

        def blocked(cmd, cwd, timeout, env=None, quiet=False):
            if Path(str(cmd[0])).stem == "ut_runner":
                runs.append(cmd)
                return -126, f"{cmd[0]}: [WinError 4551] An Application Control policy has blocked this file"
            return real(cmd, cwd, timeout, env, quiet)
        with mock.patch.object(a3, "_run", blocked):
            r = self.agent.execute(uc.VerifyRequest(layout=self.layout, stage="verify", bundle=self.bundle(
                self.CLAMP_TESTS)))
        self.assertEqual(r.decision, "environment_error")
        self.assertIn("refused to run", r.reason)
        self.assertEqual([f.classification for f in r.failures], ["environment_defect"])
        self.assertEqual(len(runs), 1)                                   # no test-by-test reruns

    def test_mutant_sites_are_the_mutants_of_the_oracle_check(self):                # COV-15: same operators
        source = (Path(self.layout.repo_root) / self.layout.sut_file).read_text()
        for fn in self.layout.functions:
            sites = self.agent.mutant_sites(source, range(fn.line_start, fn.line_end + 1))
            self.assertEqual([s[3] for s in sites][: self.agent.cfg.max_mutants],
                             [label for label, _ in self.agent.mutants(source, fn)])
            self.assertEqual([s[4] for s in sites][: self.agent.cfg.max_mutants],
                             [text for _, text in self.agent.mutants(source, fn)])

    # ---------------------------------------------------------------- ut_harness 1.2: embedded targets
    def test_target_registers_live_in_host_memory_cleared_before_each_test(self):            # A3-HW1
        tests = ('UT_TEST(test_write) { UT_REG32(0x40021010u) = 5u; UT_EXPECT_EQ_INT(UT_REG32(0x40021010u), 5); }\n'
                 'UT_TEST(test_cleared) { UT_EXPECT_EQ_INT(UT_REG32(0x40021010u), 0); }\n'
                 'UT_TEST(test_outside) { UT_REG32(0x10u) = 1u; }\n'
                 'UT_TEST(test_after) { UT_EXPECT_EQ_INT(clamp_percent(50), 50); }\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_write)\nUT_CASE(test_cleared)\nUT_CASE(test_outside)\nUT_CASE(test_after)\n'
                 'UT_SUITE_END\n')
        with self.assertLogs(level="WARNING"):
            r = self.agent.execute(uc.VerifyRequest(layout=self.layout, stage="verify", bundle=self.bundle(tests)))
        status = {t.name: t.status for t in r.tests}
        self.assertEqual((status["test_write"], status["test_cleared"], status["test_after"]),
                         ("passed", "passed", "passed"), r.tests)
        self.assertEqual(status["test_outside"], "crashed")                 # outside every window: a real fault
        size = 0x10000 if os.name == "nt" else os.sysconf("SC_PAGE_SIZE")   # Windows maps in 64 KiB units
        self.assertTrue(any(int(p, 16) <= 0x40021010 < int(p, 16) + size for p in r.register_pages), r.register_pages)

    def test_an_endless_loop_is_expected_and_a_spin_ends_at_the_time_limit(self):        # A2-21y, A3-45d
        tests = ('UT_TEST(test_superloop) { UT_EXPECT_NO_RETURN(for (;;) { (void)clamp_percent(1); }); }\n'
                 'UT_TEST(test_spins) { volatile int k = 1; while (k) { } }\n'
                 'UT_TEST(test_returns) { UT_EXPECT_NO_RETURN((void)clamp_percent(1)); }\n'
                 'UT_TEST(test_after) { UT_EXPECT_EQ_INT(clamp_percent(-5), 0); }\n'
                 'UT_TEST(test_ready_later) { UT_REG_SET_LATER(UT_REG32(0x40021000u), 0x20u, 20);\n'
                 '    while ((UT_REG32(0x40021000u) & 0x20u) == 0u) { }\n'
                 '    UT_EXPECT_EQ_INT(UT_REG32(0x40021000u), 0x20); }\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_superloop)\nUT_CASE(test_spins)\nUT_CASE(test_returns)\nUT_CASE(test_after)\n'
                 'UT_CASE(test_ready_later)\nUT_SUITE_END\n')
        agent = a3.CompilationAgent(config=self.agent.cfg.model_copy(update={"test_time_limit_ms": 500}))
        with contextlib.ExitStack() as stack:
            if os.name == "nt":
                stack.enter_context(self.assertLogs(level="WARNING"))
            r = agent.execute(uc.VerifyRequest(layout=self.layout, stage="verify", bundle=self.bundle(tests)))
        status = {t.name: t for t in r.tests}
        self.assertEqual(status["test_superloop"].status, "passed")
        self.assertEqual(status["test_spins"].status, "timeout")
        self.assertEqual(status["test_returns"].status, "failed")           # it returned: not an endless loop
        self.assertEqual(status["test_ready_later"].status, "passed")       # the "hardware" set the bit [A2-21ab]
        self.assertEqual(status["test_after"].status, "passed")
        if os.name != "nt":
            self.assertIn("time limit", status["test_spins"].message)
            self.assertTrue(r.run_ok)                                        # the program itself went on

    def test_function_local_statics_run_every_test_in_its_own_process(self):               # A3-45c
        tests = ('static int ut_count;\n'
                 'UT_TEST(test_first) { UT_EXPECT_EQ_INT(++ut_count, 1); }\n'
                 'UT_TEST(test_second) { UT_EXPECT_EQ_INT(++ut_count, 1); }\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_first)\nUT_CASE(test_second)\nUT_SUITE_END\n')
        shared = self.agent.execute(uc.VerifyRequest(layout=self.layout, stage="verify", bundle=self.bundle(tests)))
        self.assertEqual({t.name: t.status for t in shared.tests}["test_second"], "failed")
        layout = self.layout.model_copy(update={"local_statics": [uc.StateVar(name="f::count", type="int")]})
        isolated = self.agent.execute(uc.VerifyRequest(layout=layout, stage="verify", bundle=self.bundle(tests)))
        self.assertEqual({t.name: t.status for t in isolated.tests}, {"test_first": "passed", "test_second": "passed"})
        self.assertIsNotNone(isolated.coverage)                     # the child processes' profiles are merged

    def test_a_probe_without_tests_is_measured_when_every_test_runs_alone(self):         # A3-45c, A3-60
        """From test-full on Windows: an SUT with a function-local static runs every test in a run of its own;
        a suite without tests (the probe of a new SUT) is measured by one run of the program, as on Linux and macOS."""
        layout = self.layout.model_copy(update={"local_statics": [uc.StateVar(name="f::count", type="int")]})
        with mock.patch.object(a3, "ONE_BY_ONE", True):                    # the Windows path, on this system
            r = self.agent.execute(uc.VerifyRequest(layout=layout, stage="verify", bundle=self.bundle(
                "UT_SUITE_BEGIN\nUT_SUITE_END\n")))                                # a suite without tests
            tests = ('UT_TEST(test_first) { UT_EXPECT_EQ_INT(clamp_percent(50), 50); }\n'
                     'UT_SUITE_BEGIN\nUT_CASE(test_first)\nUT_SUITE_END\n')
            one = self.agent.execute(uc.VerifyRequest(layout=layout, stage="verify", bundle=self.bundle(tests)))
        self.assertNotEqual(r.decision, "environment_error", r.reason)
        self.assertIsNotNone(r.coverage)
        self.assertEqual(r.tests, [])
        self.assertEqual([(t.name, t.status) for t in one.tests], [("test_first", "passed")])
        self.assertIsNotNone(one.coverage)

    def test_code_after_an_endless_loop_is_never_counted_as_executed(self):                # A3-HW5
        repo = Path(self.layout.repo_root)
        sut = repo / "src" / "thermostat.c"
        sut.write_text(sut.read_text() + "\nint superloop(void)\n{\n    for (;;) {\n        (void)clamp_percent(1);\n"
                       "    }\n    return 3;\n}\n")
        dead = len(sut.read_text().splitlines()) - 1                       # the line "return 3;"
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/thermostat.c"))
        tests = ('int superloop(void);\n'
                 'UT_TEST(test_loop) { UT_EXPECT_NO_RETURN(superloop()); }\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_loop)\nUT_SUITE_END\n')
        r = self.agent.execute(uc.VerifyRequest(layout=layout, stage="verify", bundle=self.bundle(tests)))
        self.assertEqual([t.status for t in r.tests], ["passed"])
        gaps = {g.line: g for f in r.coverage.functions for g in f.gaps if g.metric == "LC"}
        self.assertIn(dead, gaps)                                  # LLVM alone would report it as executed
        self.assertIn("A3-HW5", gaps[dead].detail)
        self.assertNotIn(dead - 2, gaps)                           # the loop body did run

    def test_replaced_target_assembly_takes_its_result_from_the_test(self):                # A3-HW3
        repo = Path(self.layout.repo_root)
        (repo / "include" / "core.h").write_text(
            "#ifndef CORE_H\n#define CORE_H\nstatic inline unsigned get_primask(void)\n{\n    unsigned r;\n"
            "    __asm volatile (\"MRS %0, primask\" : \"=r\" (r));\n    return r;\n}\n#endif\n")
        sut = repo / "src" / "thermostat.c"
        sut.write_text(sut.read_text().replace('#include "thermostat.h"', '#include "thermostat.h"\n#include "core.h"', 1)
                       + "\nint irq_masked(void) { return get_primask() & 1u; }\n")
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/thermostat.c"))
        tests = ('int irq_masked(void);\n'
                 'UT_TEST(test_zero) { UT_EXPECT_EQ_INT(irq_masked(), 0); }\n'
                 'UT_TEST(test_set) { ut_asm_value = 1; UT_EXPECT_EQ_INT(irq_masked(), 1); }\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_zero)\nUT_CASE(test_set)\nUT_SUITE_END\n')
        r = self.agent.execute(uc.VerifyRequest(layout=layout, stage="verify", bundle=self.bundle(tests)))
        if not any("MRS" in n for n in r.host_notes):
            self.skipTest("the host assembler accepts the instruction: " + "; ".join(r.host_notes))
        self.assertEqual({t.name: t.status for t in r.tests}, {"test_zero": "passed", "test_set": "passed"}, r.reason)
        self.assertIn("include/core.h:6", " ".join(r.host_notes))
        self.assertIn("MRS %0, primask", (repo / "include" / "core.h").read_text())   # the user's file is unchanged


# ============================================================================ build variants [A3-BD14, A1-37a..e]
CHAN_C = """#include "chan.h"

int channel_gate(int ch)
{
    if (ch < 2) {
        if (ch >= BRAKE_CHANNELS) {
            return -1;
        }
        return ch;
    }
    return 0;
}
"""


class RuleFailureTests(unittest.TestCase):
    """ARCH-21 .. ARCH-23 in the agents: a rule that fails hands the case to the brain, the tool decides; and the
    rule fixes found on real repositories (A1-DA20, A1-DA21, A2-21ac, A2-21ad, A3-BD16, A3-CL14, A3-FZ12)."""

    def setUp(self):
        self.tmp = tmpdir()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_prologue_keeps_the_sut_order_and_closes_its_conditionals(self):
        text = ('/* header */\n#define MPU_FROM_API 1\n#include "kernel.h"\n#include "klist.c"\n#ifdef HAL_ENABLED\n'
                '#include "klist.h" // the list\n#define LOCAL(x) \\\n  ((x) + 1)\nstatic int helper(void);\n'
                'int f(void) { return 0; }\n#endif\n')
        fn = uc.FunctionInfo(name="f", qualified_name="f", signature="int f(void)", file="a.c", line_start=10, line_end=10)
        self.assertEqual(uc.sut_prologue(text, [fn]), ['#define MPU_FROM_API 1', '#include "kernel.h"', '#ifdef HAL_ENABLED',
                                                       '#include "klist.h"', '#define LOCAL(x) ((x) + 1)', '#endif'])
        self.assertEqual(uc.sut_prologue(text, [fn], header_sut=True), [])          # a header's own guard stays out

    def test_argument_variables_drop_only_top_level_qualifiers(self):
        cases = {"char const * const": "char const *", "const QEvt *const": "const QEvt *", "const int": "int",
                 "volatile uint32_t": "uint32_t", "int (*)(int)": None, "int[4]": None}
        for ptype, expected in cases.items():
            self.assertEqual(a2._value_type(ptype), expected, ptype)

    def test_types_come_from_the_syntax_tree(self):
        """A1-DA21: T * const is a pointer; a pointer field has its pointee; a function pointer field is a callback;
        _Atomic(size_t) is an integer; typedefs are resolved."""
        repo = copy_pilot("c_realrepo_pilot", self.d)
        a1_agent, a3_agent = a1.AnalyzerAgent(), a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "w")))
        kinds = {}
        for sut in ("kernel/src/klist.c", "codec/ring.c", "drivers/crc_drv.c"):
            layout = a1_agent.discover(uc.DiscoverRequest(repo_root=str(repo), sut_file=sut))
            build = a3_agent.capture(uc.CaptureRequest(layout=layout))
            layout = run(a1_agent.dependencies(uc.DependencyRequest(layout=layout, build=build)))
            for f in layout.functions:
                for p in f.params:
                    kinds[(f.name, p.name)] = (p.kind, {x.name: (x.kind, x.base) for x in p.fields})
            if sut.endswith("klist.c"):
                self.assertIn(layout.typedefs.get("kBase_t"), ("int32_t", "int"))
        kind, fields = kinds[("klist_insert_end", "list")]
        self.assertEqual(kind, "ptr_struct")
        self.assertEqual(fields["index"], ("pointer", "kItem_t"))
        kind, fields = kinds[("ring_free", "rb")]
        self.assertEqual((kind, fields["r"][0], fields["evt"][0]), ("ptr_struct", "int", "callback"))
        self.assertEqual(kinds[("crc_calc", "hcrc")][1]["Instance"], ("pointer", "CRC_Regs"))
        self.assertEqual(kinds[("crc_calc", "buf")][0], "ptr_scalar")

    # ------------------------------------------------------------ A3-CL14: generated files repaired by the brain
    STUB = ('#include "ut_harness.h"\n#include "sensor.h"\nstatic const int ut_stub_read_sensor_return;\n'
            'int read_sensor(int channel) { (void)channel; return ut_stub_read_sensor_return; }\n'
            'void ut_before_each(void) { ut_stub_read_sensor_return = 0; }\n')

    def _thermostat(self):
        repo = copy_pilot("c_pilot", self.d)
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/thermostat.c"))
        suite = ('#include "ut_harness.h"\n#include "thermostat.h"\nextern int ut_stub_read_sensor_return;\n'
                 '/* evidence: E8 */\nUT_TEST(test_heat)\n{\n    ut_stub_read_sensor_return = 10;\n'
                 '    UT_EXPECT_EQ_INT(thermostat_command(20, 2), 1);\n}\n\nUT_SUITE_BEGIN\n    UT_CASE(test_heat)\nUT_SUITE_END\n')
        bundle = uc.FileActionBundle(bundle_id="b", actions=[
            uc.FileAction(path="tests/stubs/ut_stubs.c", action="create", role="stub", content=self.STUB),
            uc.FileAction(path="tests/test_thermostat.c", action="create", role="testsuite", content=suite)])
        return layout, bundle

    def _agent3(self, b):
        agent = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "work")),
                                    router=ba.BrainRouter([b], 0.0))
        agent.resolutions = ba.ResolutionMemory(self.d / "resolutions.sqlite3", project="pilot")
        return agent

    def test_a_generated_file_the_compiler_rejects_is_repaired_by_the_brain(self):
        """A3-CL14: the stub's const control variable is assigned; the brain's edit makes it build; an edit that
        changes an assertion is refused; the same failure in a later run is resolved from memory (ARCH-23)."""
        layout, bundle = self._thermostat()
        bad = {"edits": [{"path": "tests/test_thermostat.c", "old": "thermostat_command(20, 2), 1)",
                          "new": "thermostat_command(20, 2), 0)"}], "reason": "weaker"}
        good = {"edits": [{"path": "tests/stubs/ut_stubs.c", "old": "static const int ut_stub_read_sensor_return;",
                           "new": "int ut_stub_read_sensor_return;"}], "reason": "the control variable must be writable"}
        scripted = UTBrain(repairs=[bad, good])
        agent = self._agent3(scripted)
        req = uc.VerifyRequest(layout=layout, stage="verify", bundle=bundle)
        response = run(agent.call_local("verify", req, signatures={"sut": layout.sut_sha256}))
        self.assertEqual(response.status, "success", response.error)
        result = uc.VerificationResult.model_validate(response.data)
        self.assertTrue(result.link_ok, result.reason)
        self.assertEqual((result.total, result.passed), (1, 1))
        self.assertIn("int ut_stub_read_sensor_return;", result.repaired_files["tests/stubs/ut_stubs.c"])
        (record,) = response.meta.escalations
        self.assertEqual((record["rule"], record["outcome"], record["source"]), ("A3-CL14 generated files", "resolved", "brain"))
        self.assertIn("UT_EXPECT", record["attempts"][0])                     # the weakening edit was refused
        later = self._agent3(UTBrain(repairs=[]))                             # another run, same failure, no answer
        again = run(later.call_local("verify", req, signatures={"sut": layout.sut_sha256}))
        self.assertTrue(uc.VerificationResult.model_validate(again.data).link_ok)
        self.assertEqual(again.meta.escalations[0]["source"], "memory")

    def test_without_a_brain_the_build_failure_stays_and_is_recorded(self):
        layout, bundle = self._thermostat()
        agent = self._agent3(DownBrain())
        response = run(agent.call_local("verify", uc.VerifyRequest(layout=layout, stage="verify", bundle=bundle),
                                        signatures={"sut": layout.sut_sha256}))
        result = uc.VerificationResult.model_validate(response.data)
        self.assertFalse(result.link_ok)
        self.assertEqual(response.meta.escalations[0]["outcome"], "needs_brain")

    # ------------------------------------------------------------ A3-BD16: host adaptation of target code
    def test_target_code_the_tables_do_not_know_gets_host_definitions_from_the_brain(self):
        repo = self.d / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "ccm.c").write_text("#include <stdint.h>\n__ccm_data uint32_t ccm_buf[4];\n"
                                            "uint32_t ccm_first(void) { return ccm_buf[0]; }\n")
        database = repo / "compile_commands.json"
        database.write_text(json.dumps([{"directory": str(repo), "file": "src/ccm.c",
                                         "arguments": ["arm-none-eabi-gcc", "-mcpu=cortex-m4", "-c", "src/ccm.c"]}]))
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/ccm.c"))
        scripted = UTBrain(host=[{"flags": ["-DNOPE=1"], "compat_header": "", "reason": "wrong"},
                                 {"flags": [], "compat_header": "#define __ccm_data", "reason": "a section attribute"}])
        agent = self._agent3(scripted)
        build = agent.capture(uc.CaptureRequest(layout=layout, compile_commands=str(database)))
        self.assertFalse(build.estimated)
        adapted = run(agent.suggest_flags(uc.FlagRequest(layout=layout, build=build)))
        self.assertEqual(len(adapted.host_adaptation), 1, adapted.notes)
        self.assertIn("ut_target_compat.h", " ".join(adapted.sut_flags))
        probe = layout.model_copy(update={"compile_flags": adapted.sut_flags, "build": adapted})
        self.assertEqual(agent._sut_errors(probe, repo, agent.toolchain(), []), [])
        self.assertEqual(agent.escalations[-1]["outcome"], "resolved")
        self.assertEqual(len(agent.escalations[-1]["attempts"]), 1)            # the first answer did not help

    # ------------------------------------------------------------ A1-DA20: missing headers
    def test_a_missing_header_is_found_by_name_or_chosen_by_the_brain(self):
        repo = copy_pilot("c_realrepo_pilot", self.d)
        a3_agent = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "w")))
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="kernel/src/klist.c"))
        build = uc.BuildDescription(source="compile_commands", estimated=False, sut_flags=[],
                                    entries=[uc.CompileEntry(file="kernel/src/klist.c", flags=[])])
        layout = layout.model_copy(update={"include_dirs": []})
        analyzer = a1.AnalyzerAgent(router=ba.BrainRouter([DownBrain()], 0.0))
        found = run(analyzer.dependencies(uc.DependencyRequest(layout=layout, build=build)))
        self.assertIn("-I{repo}/kernel/include", found.build.sut_flags)            # one file of that name
        self.assertFalse(found.dependencies.missing_headers)
        (repo / "legacy").mkdir()
        (repo / "legacy" / "kernel.h").write_text("#error the old kernel\n")
        chooser = UTBrain(headers=[{"folder": "legacy", "reason": "wrong"},
                                   {"folder": "kernel/include", "reason": "the kernel's headers"}])
        analyzer = a1.AnalyzerAgent(router=ba.BrainRouter([chooser], 0.0))
        chosen = run(analyzer.dependencies(uc.DependencyRequest(layout=layout, build=build)))
        self.assertIn("-I{repo}/kernel/include", chosen.build.sut_flags)
        self.assertEqual(analyzer.escalations[-1]["outcome"], "resolved")
        self.assertEqual(len(analyzer.escalations[-1]["attempts"]), 1)          # legacy/ gave more errors

    # ------------------------------------------------------------ A2-21ac: values the rules cannot build
    def test_a_value_no_rule_builds_comes_from_the_brain(self):
        repo = copy_pilot("c_realrepo_pilot", self.d)
        a1_agent = a1.AnalyzerAgent()
        layout = a1_agent.discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="kernel/src/klist.c"))
        build = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "w"))).capture(
            uc.CaptureRequest(layout=layout))
        layout = run(a1_agent.dependencies(uc.DependencyRequest(layout=layout, build=build)))
        for f in layout.functions:                    # as if the rules could not classify the list parameter
            for p in f.params:
                if f.name == "klist_count":
                    p.kind, p.fields = "other", []
        plan = run(a1_agent.plan(uc.PlanRequest(layout=layout)))
        recipe = {"setup": ["kList_t ut_r_list;", "klist_init(&ut_r_list);"], "argument": "&ut_r_list"}
        generator = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="deterministic"),
                                      router=ba.BrainRouter([UTBrain(values=[{"setup": ["UT_EXPECT_TRUE(1);"],
                                                                              "argument": "0"}, recipe])], 0.0))
        bundle = run(generator.generate_bundle(uc.GenerationRequest(plan=plan)))
        suite = next(a.content for a in bundle.actions if a.role == "testsuite")
        self.assertIn("klist_init(&ut_r_list);", suite)
        self.assertIn("klist_count(&ut_r_list)", suite)
        self.assertIn("built by the brain [A2-21ac]", suite)
        record = generator.escalations[-1]
        self.assertEqual((record["rule"], record["outcome"]), ("A2-21ac value", "resolved"))
        self.assertIn("must not assert", record["attempts"][0])

    # ------------------------------------------------------------ A3-FZ12: fuzzing with registers
    def test_fuzz_targets_map_registers_and_go_on_after_a_crash(self):
        repo = self.d / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "regs.c").write_text(
            "#include <stdint.h>\nuint32_t reg_read(uint32_t sel)\n{\n    if (sel == 3U) {\n"
            "        return *(volatile uint32_t *)0x10U;\n    }\n    if (sel == 77U) {\n"
            "        *(volatile uint32_t *)0x40021000U = 5U;\n        return *(volatile uint32_t *)0x40021000U;\n"
            "    }\n    return 0U;\n}\n")
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/regs.c"))
        agent = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "w"), search_executions=40_000))
        layout = run(a1.AnalyzerAgent().dependencies(uc.DependencyRequest(layout=layout, build=agent.capture(
            uc.CaptureRequest(layout=layout)))))
        gap = uc.GapItem(gap_id="LC:reg_read:8", metric="LC", function="reg_read", line=8, detail="line never executed")
        result = agent.fuzz(uc.FuzzRequest(layout=layout, functions=["reg_read"], gaps=[gap], seconds=20))
        self.assertFalse(result.unsupported, result.unsupported)
        self.assertTrue(any("reg_read" in c for c in result.crashes), result.crashes)       # sel == 3 crashed ...
        self.assertTrue(any(i.function == "reg_read" for i in result.inputs), result.reason)  # ... and sel == 77 found


class EmbeddedRuleTests(unittest.TestCase):
    """The rules for embedded code that need no compiler: endless loops, register polling, state, fakes,
    templates, exceptions, target assembly and the data model (A2-21y..ab, A2-21aa, A1-DA19, A3-45e, A3-HW3/4)."""
    C = ROOT / "Test" / "pilots" / "c_embedded_pilot"
    CPP = ROOT / "Test" / "pilots" / "cpp_embedded_pilot"

    @staticmethod
    def body(path: Path, name: str) -> str:
        text = path.read_text()
        start = re.search(r"\b" + re.escape(name) + r"\s*\([^;{]*\)\s*\{", text).start()
        depth, k = 0, text.index("{", start)
        for k in range(k, len(text)):
            depth += {"{": 1, "}": -1}.get(text[k], 0)
            if depth == 0:
                break
        return text[start: k + 1]

    @staticmethod
    def fn(name: str, params: list[tuple[str, str]] = ()) -> uc.FunctionInfo:
        return uc.FunctionInfo(name=name, qualified_name=name, signature=f"int {name}()", file="x.c", line_start=1,
                               line_end=1, params=[uc.ParamInfo(name=n, type=t) for t, n in params])

    def test_endless_loops_are_found_and_bounded_loops_are_not(self):                     # A2-21y
        main = self.body(self.C / "src" / "main_loop.c", "main")
        self.assertTrue(a2.endless_loop(main))
        self.assertFalse(a2.endless_loop(self.body(self.C / "src" / "main_loop.c", "app_step")))
        self.assertFalse(a2.endless_loop("void f(void) { for (;;) { if (g()) break; } }"))
        self.assertTrue(a2.endless_loop("void f(void) { for (;;) { switch (s) { case 1: break; } while (k) { break; } } }"))
        self.assertFalse(a2.endless_loop("void f(void) { while (1) { if (g()) return; } }"))
        # code after an endless loop is never reached; LLVM would count it from the loop's entry [A3-HW5]
        main = "int main(void)\n{\n    for (;;) {\n        step();\n    }\n    return 0;\n}"
        fn = uc.FunctionInfo(name="main", qualified_name="main", signature="int main(void)", file="m.c",
                             line_start=1, line_end=7)
        self.assertEqual(uc.unreachable_lines(main, [fn]), {6})

    def test_register_bits_a_function_waits_for(self):                                    # A2-21ab
        gpio = self.C / "src" / "gpio_driver.c"
        wait = self.body(gpio, "gpio_wait_ready")
        self.assertEqual(a2.register_polls(self.fn("gpio_wait_ready", [("uint32_t", "max_loops")]), wait),
                         [("GPIOA->IDR", "READY_PIN")])
        self.assertEqual(a2.register_waits(self.fn("gpio_wait_ready", [("uint32_t", "max_loops")]), wait), [])  # bounded
        put = self.body(gpio, "uart_put")
        self.assertEqual(a2.register_waits(self.fn("uart_put", [("uint8_t", "c")]), put), [("UART1_SR", "UART_TXE", True)])
        sync = self.body(self.CPP / "src" / "motor_ctrl.cpp", "MotorCtrl::sync")
        self.assertEqual(a2.register_waits(self.fn("sync"), sync), [("hw::tim2()->SR", "hw::SR_UIF", True)])
        busy = "void tx(void) { while ((UART_SR & BUSY) != 0u) { } }"
        self.assertEqual(a2.register_waits(self.fn("tx"), busy), [("UART_SR", "BUSY", False)])
        # the "hardware" changes the bit while the function waits, so the call returns [A2-21ab]
        self.assertEqual(a2.wait_setup([("UART1_SR", "UART_TXE", True)], set()),
                         ["UT_REG_SET_LATER(UART1_SR, UART_TXE, 20);   /* the hardware sets the bit after 20 ms [A2-21ab] */"])
        self.assertEqual(a2.wait_setup([("UART1_SR", "UART_TXE", True)], {("UART1_SR", "UART_TXE")}), [])
        self.assertIn("UT_REG_CLEAR_LATER(UART_SR, BUSY, 20);", a2.wait_setup([("UART_SR", "BUSY", False)],
                                                                                {("UART_SR", "BUSY")})[0])
        # a parameter's bits are an input, not a register
        self.assertEqual(a2.register_polls(self.fn("f", [("struct s *", "p")]), "int f(struct s *p) { if ((p->flags & 4u) != 0u) return 1; return 0; }"), [])

    def test_state_read_in_decisions_gets_values_and_call_sequences(self):                 # A2-21z
        repo = self.C
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/scheduler.c"))
        layout = layout.model_copy(update={"sut_state": [uc.StateVar(name="g_tick_flag", type="volatile int")],
                                           "local_statics": [uc.StateVar(name="sched_debounce::state", type="int")]})
        poll = self.body(repo / "src" / "scheduler.c", "sched_poll")
        state = a2.decision_state(self.fn("sched_poll"), poll, layout, {})
        self.assertEqual([(v.name, vals) for v, vals in state], [("g_tick_flag", [0, 1])])
        self.assertEqual(a2.setter_source(state[0][0]), "void ut_state_set_g_tick_flag(long long v) { g_tick_flag = (int)v; }")
        debounce = self.body(repo / "src" / "scheduler.c", "sched_debounce")
        self.assertEqual(a2.repeat_count(self.fn("sched_debounce"), debounce, layout, {}), 4)   # compares with 3
        self.assertEqual(a2.repeat_count(self.fn("sched_sat_add"), "u32_t sched_sat_add(u32_t a, u32_t b) { return a; }",
                                         layout, {}), 1)

    def test_a_fake_implements_an_injected_interface(self):                                 # A2-21aa
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(self.CPP), sut_file="src/motor_ctrl.cpp"))
        iface = uc.InterfaceInfo(name="IEncoder", header="include/encoder.hpp", methods=[
            uc.InterfaceMethod(name="position", return_type="std::int32_t", suffix=" const"),
            uc.InterfaceMethod(name="fault", return_type="bool", suffix=" const"),
            uc.InterfaceMethod(name="reset", return_type="void", params=[["int", "level"]])])
        layout = layout.model_copy(update={"interfaces": [iface]})
        headers, rows = a2.render_fakes(layout)
        text = "\n".join(rows)
        self.assertEqual(headers, ["encoder.hpp"])
        self.assertIn("class ut_fake_IEncoder : public IEncoder {", text)
        self.assertIn("std::int32_t position() const override {", text)
        self.assertIn("mutable std::int32_t position_returns[UT_STUB_SEQUENCE]{};", text)
        self.assertIn("void reset(int level) override {", text)
        self.assertIn("reset_level = level;", text)
        self.assertIn("ut_stub_call();", text)                                   # counts against the budget
        p = uc.ParamInfo(name="enc", type="IEncoder &", kind="interface", base="IEncoder", by_ref=True)
        options = a2._fake_options(p, 0, layout, [0, 1, 5])
        self.assertIn(["ut_fake_IEncoder ut_f0;", "ut_f0.position_return = 0;", "ut_f0.fault_return = false;"],
                      [o[0] for o in options])
        self.assertTrue(all(o[1] == "ut_f0" for o in options))                   # passed by reference
        ref = uc.InterfaceInfo(name="IBus", methods=[uc.InterfaceMethod(name="buf", return_type="const int &")])
        held = layout.model_copy(update={"interfaces": [ref]})
        self.assertTrue(a2.fake_supported(ref, held))                            # a reference to the fake's member
        self.assertIn("const int & buf() override {", "\n".join(a2.render_fakes(held)[1]))
        self.assertIn("mutable int buf_return{};", "\n".join(a2.render_fakes(held)[1]))
        abstract = uc.InterfaceInfo(name="IHub", methods=[uc.InterfaceMethod(name="bus", return_type="IBus &")])
        self.assertFalse(a2.fake_supported(abstract, held))                       # an interface cannot be held

    def test_class_templates_of_a_header_sut_are_instantiated_as_the_repository_uses_them(self):   # A1-DA19
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(self.CPP), sut_file="include/ring_buffer.hpp"))
        self.assertTrue(layout.header_sut)
        self.assertEqual(layout.language, "cpp")                                # by the content, not ".hpp" alone
        plan = mock.Mock(layout=layout)
        self.assertEqual(a2.class_templates(plan), {"RingBuffer": ("RingBuffer", "std::int32_t, 4")})
        self.assertEqual(uc.instantiated_class("RingBuffer", layout), "RingBuffer<std::int32_t, 4>")  # one fact for
        self.assertEqual(uc.template_args(layout, "RingBuffer"), {"T": "std::int32_t", "N": "4"})  # every agent
        self.assertIn("template class RingBuffer<std::int32_t, 4>;", a2.template_instances(plan))
        p = a2._instantiate_param(uc.ParamInfo(name="v", type="const T &", kind="other", base="T", by_ref=True),
                                  {"T": "std::int32_t", "N": "4"})
        self.assertEqual((p.kind, p.type, p.base), ("ptr_scalar", "const std::int32_t &", "std::int32_t"))
        self.assertTrue(a1.cpp_content("namespace a { int f(); }"))
        self.assertFalse(a1.cpp_content("/* class */ int f(void);"))

    def test_observed_exceptions_become_assertions(self):                                   # A3-45e, A2-21f
        suite = ('UT_TEST(t) {\n'
                 '    UT_OBSERVE_THROW("t_r1_x", UT_OBSERVE_INT("t_r1", ut_obj.step(-100001)));\n'
                 '    UT_OBSERVE_THROW("t_r2_x", UT_OBSERVE_INT("t_r2", ut_obj.step(5)));\n'
                 '    UT_OBSERVE_THROW("t_r3_x", ut_obj.push(1));\n'
                 '}\n')
        bundle = uc.FileActionBundle(bundle_id="b", actions=[uc.FileAction(
            path="tests/t.cpp", action="create", role="testsuite", content=suite)], tests=["t"])
        observed = {"t_r1_x": "std::invalid_argument", "t_r2_x": "none", "t_r2": "7", "t_r3_x": "Overflow"}
        out = a2.finalize_bundle(bundle, observed).actions[0].content
        self.assertIn("UT_EXPECT_THROW(ut_obj.step(-100001), std::invalid_argument);", out)
        self.assertIn("UT_EXPECT_EQ_INT(ut_obj.step(5), 7);", out)
        self.assertIn("UT_EXPECT_ANY_THROW(ut_obj.push(1));", out)                     # A2-21ag: no try in a test
        self.assertNotIn("UT_OBSERVE", out)
        self.assertEqual(observed["t_r2"], "7")                                  # the caller's observations unchanged

    def test_target_assembly_is_replaced_line_for_line(self):                                # A3-HW3
        text = (self.C / "include" / "cmsis_compat.h").read_text()
        new, replaced = a3.neutralize_asm(text, mnemonics={"MRS"})
        self.assertEqual(new.count("\n"), text.count("\n"))
        self.assertEqual([what for _, what in replaced], ["MRS %0, primask"])
        self.assertIn("(result) = (__typeof__((result)))ut_asm_result()", new)
        self.assertIn('"cpsid i"', new)                                          # only the named instruction
        new, replaced = a3.neutralize_asm(text, line=6)
        self.assertEqual(replaced, [(6, "cpsid i")])
        macro = '#define NOP() __asm volatile ( \\\n    "nop" )\nint x;\n'
        new, _ = a3.neutralize_asm(macro, line=1)
        self.assertEqual(new.count("\\\n"), 1)                                   # the continuation stays
        self.assertTrue(a2.uses_target_asm(self.fn("critical_irq_enabled"), "return (__get_PRIMASK() & 1u);",
                                           a1.AnalyzerAgent().discover(uc.DiscoverRequest(
                                               repo_root=str(self.C), sut_file="src/critical.c"))))

    def test_data_model_of_the_target(self):                                                # A3-HW4
        cross = uc.BuildDescription(source="make", command="arm-none-eabi-gcc -mcpu=cortex-m4 -c src/x.c")
        self.assertEqual(a3.target_data_model("", cross), "ILP32")
        self.assertEqual(a3.target_data_model("lp64", cross), "LP64")
        self.assertEqual(a3.target_data_model("", uc.BuildDescription(source="make", command="gcc -c x.c")), "")
        long_param = uc.ParamInfo(name="a", type="u32_t", kind="int", canonical="unsigned long")
        self.assertEqual(a2.width_values(long_param), [0x7FFFFFFF, 0x80000000, 0xFFFFFFFF])
        self.assertEqual(a2.width_values(uc.ParamInfo(name="a", type="int", kind="int", canonical="int")), [])

@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
class BuildVariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.d = Path(self.tmp.name)
        self.repo = self.d / "chan"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "include").mkdir()
        (self.repo / "src/chan.c").write_text(CHAN_C)
        (self.repo / "include/chan.h").write_text("#ifndef CHAN_H\n#define CHAN_H\nint channel_gate(int ch);\n#endif\n")

    def tearDown(self):
        self.tmp.cleanup()

    def database(self, *variants):
        items = [{"directory": str(self.repo), "file": "src/chan.c", "output": f"CMakeFiles/{name}.dir/src/chan.c.o",
                  "arguments": ["cc", "-Iinclude", f"-DBRAKE_CHANNELS={n}", "-c", "src/chan.c",
                                "-o", f"CMakeFiles/{name}.dir/src/chan.c.o"]} for name, n in variants]
        db = self.d / "compile_commands.json"
        db.write_text(json.dumps(items))
        return db

    def capture(self, *variants):
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/chan.c"))
        agent = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "work")))
        build = agent.capture(uc.CaptureRequest(layout=layout, compile_commands=str(self.database(*variants))))
        layout = run(a1.AnalyzerAgent().dependencies(uc.DependencyRequest(layout=layout, build=build)))
        return layout, agent, build

    def test_variants_are_listed_and_the_tested_one_marked(self):                   # A3-BD14
        layout, _, build = self.capture(("brake_ecu", 2), ("brake_ecu_1ch", 1))
        self.assertEqual([(v.name, v.tested) for v in build.variants], [("brake_ecu", True), ("brake_ecu_1ch", False)])
        self.assertIn("-DBRAKE_CHANNELS=2", " ".join(build.sut_flags))                  # tests use the tested one
        self.assertEqual(self.capture(("brake_ecu", 2))[2].variants, [])               # one variant: as before
        self.assertEqual(self.capture(("a", 2), ("b", 2))[2].variants, [])             # same flags: one variant
        old = uc.BuildDescription.model_validate_json(build.model_dump_json(exclude={"variants"}))
        self.assertEqual(old.variants, [])                                              # older caches still load

    def measured_gaps(self, layout, agent):
        suite = ('#include "ut_harness.h"\n#include "chan.h"\nvoid ut_before_each(void) {}\n'
                 'UT_TEST(test_gate) { UT_EXPECT_EQ_INT(channel_gate(0), 0); UT_EXPECT_EQ_INT(channel_gate(5), 0); }\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_gate)\nUT_SUITE_END\n')
        bundle = uc.FileActionBundle(bundle_id="b", actions=[uc.FileAction(
            path=f"{layout.writable_test_dir}/test_chan.c", action="create", role="testsuite", content=suite)])
        last = agent.execute(uc.VerifyRequest(layout=layout, stage="verify", bundle=bundle))
        self.assertTrue(last.coverage and last.coverage.measured, last.reason)
        return a1.AnalyzerAgent()._gaps(layout, last, CHAN_C)

    def test_proof_per_variant_only_the_tested_one_decides(self):                   # A1-37a .. A1-37d
        layout, agent, _ = self.capture(("brake_ecu", 2), ("brake_ecu_1ch", 1))
        gaps = self.measured_gaps(layout, agent)
        dc = next(g for g in gaps if g.metric == "DC" and g.line == 6)
        self.assertIn("infeasible_outcome", dc.detail)                     # tested: ch < 2 and ch >= 2 never hold
        proofs = {v.variant: v for v in dc.variant_proofs}
        self.assertTrue(proofs["brake_ecu"].proof and proofs["brake_ecu"].tested)
        self.assertEqual(proofs["brake_ecu_1ch"].proof, "")                # ch == 1 reaches it there
        self.assertEqual(proofs["brake_ecu_1ch"].macros, {"BRAKE_CHANNELS": 1})
        text = uc.variant_text(dc.variant_proofs)
        self.assertTrue(any("brake_ecu_1ch (BRAKE_CHANNELS = 1): no proof - may be reachable there" in t for t in text),
                        text)
        line = next(g for g in gaps if g.metric == "LC" and g.line == 7)
        self.assertIn("unreachable_code", line.detail)
        self.assertEqual({v.variant for v in line.variant_proofs}, {"brake_ecu", "brake_ecu_1ch"})   # inherited

    def test_one_variant_proves_as_before(self):                                    # nothing changes with one
        layout, agent, _ = self.capture(("brake_ecu", 2))
        gaps = self.measured_gaps(layout, agent)
        dc = next(g for g in gaps if g.metric == "DC" and g.line == 6)
        self.assertEqual(dc.variant_proofs, [])
        self.assertNotIn("infeasible_outcome", dc.detail)        # a macro set by -D is no constant (A1-37)


# ============================================================================ 6. knowledge base
class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.kb = kb.KnowledgeBaseAgent(config=kb.KBConfig(db_path=str(Path(self.tmp.name) / "kb.sqlite3"),
                                                           max_negative=2))

    def tearDown(self):
        self.kb.db.close()
        self.tmp.cleanup()

    def event(self, kind="negative", snippet="x = 1;", category="stub_defect", **verification):
        return uc.LearningEvent(kind=kind, language="c", framework="ut_harness", category=category, snippet=snippet,
                                reason="it failed", verification=verification)

    @unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
    def test_positive_needs_verification(self):
        resp = run(self.kb.call_local("learn", self.event(kind="positive")))
        self.assertEqual(resp.status, "rejected")                                 # KB-4, A3-151
        ok = run(self.kb.call_local("learn", self.event(kind="positive", compile="passed", tests="passed",
                                                         coverage="complete", verified=True)))
        self.assertEqual(ok.status, "success")

    def test_deduplication_and_bounds(self):
        first = self.kb.learn(self.event(snippet="a  =  1;"))
        again = self.kb.learn(self.event(snippet="a = 1;"))                       # same after normalization
        self.assertTrue(again.duplicate)
        self.assertEqual(first.example_id, again.example_id)
        self.kb.learn(self.event(snippet="b = 2;"))
        self.kb.learn(self.event(snippet="c = 3;"))
        count = self.kb.db.execute("SELECT COUNT(*) FROM examples WHERE kind='negative'").fetchone()[0]
        self.assertEqual(count, 2)                                                # KB-11
        kept = {r[0] for r in self.kb.db.execute("SELECT snippet FROM examples")}
        self.assertIn("a  =  1;", kept)                                           # most used survives [KB-12]

    def test_guidance_filters_and_ranks(self):
        self.kb.learn(self.event(kind="gap_closure", snippet="g1", category="gap_closure", gap_covered=True,
                                 tests="passed", regression=False))
        self.kb.learn(self.event(kind="positive", snippet="p1", category="coverage_complete", compile="passed",
                                 tests="passed", coverage="complete", verified=True))
        self.kb.learn(uc.LearningEvent(kind="negative", language="cpp", framework="ut_harness", category="x",
                                       snippet="other language", reason="r"))
        g = self.kb.guidance(uc.GuidanceRequest(language="c", framework="ut_harness"))
        self.assertEqual([e.snippet for e in g.positive], ["p1", "g1"])          # verified complete first
        self.assertEqual(g.negative, [])                                          # other language filtered [SC-24]

    def example(self, function, signature, snippet, step="A2.tests", language="c", pattern="language c"):
        return uc.LearningEvent(kind="plan_example" if step == "A1.plan" else "test_example", language=language,
                                framework="ut_harness", category="BC", snippet=snippet, step=step, function=function,
                                signature=signature, pattern=pattern, context="reached: BC:x:3:1:T `if (len == 0)`",
                                verification={"verified": True, "tests": "passed", "reached": ["BC:x:3:1:T"]})

    @unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
    def test_an_example_needs_its_measurement(self):                                     # KB-23
        unmeasured = self.example("crc8", "uint8_t crc8(const uint8_t *d, size_t n)", "UT_TEST(t) { crc8(0, 0); }")
        unmeasured.verification = {"verified": True, "tests": "passed", "reached": []}
        self.assertEqual(run(self.kb.call_local("learn", unmeasured)).status, "rejected")
        ok = self.example("crc8", "uint8_t crc8(const uint8_t *d, size_t n)", "UT_TEST(t) { crc8(0, 0); }")
        self.assertEqual(run(self.kb.call_local("learn", ok)).status, "success")

    def test_examples_are_found_per_function(self):                                     # KB-24, KB-25
        for fn, sig, body in (
                ("crc8", "uint8_t crc8(const uint8_t *data, size_t len)", "UT_TEST(t_crc8) { uint8_t b[2] = {1, 2}; "
                 "UT_EXPECT_EQ_INT(crc8(b, 2), 7); }"),
                ("crc8_copy", "uint8_t crc8_copy(const uint8_t *data, size_t len)", "UT_TEST(t_crc8_copy) { uint8_t b[2] = "
                 "{1, 2}; UT_EXPECT_EQ_INT(crc8_copy(b, 2), 7); }"),                  # nearly the same: left out
                ("bcm_setBits", "void bcm_setBits(uint8_t *reg, int bits)", "UT_TEST(t_bits) { uint8_t r = 0; "
                 "bcm_setBits(&r, 3); UT_EXPECT_EQ_INT(r, 8); }"),
                ("filter_step", "float filter_step(float x)", "UT_TEST(t_f) { UT_EXPECT_EQ_DBL(filter_step(1.0f), 0.5, 1e-6); }")):
            self.assertTrue(self.kb.learn(self.example(fn, sig, body)).stored)
        self.kb.learn(self.example("crc16", "uint16_t crc16(const uint8_t *data, size_t len)", "UT_TEST(cpp) {}",
                                   language="cpp"))
        self.kb.learn(self.example("crc8", "uint8_t crc8(const uint8_t *data, size_t len)",
                                   "args data=b, len=0; setup uint8_t b[1] = {0};", step="A1.plan"))
        query = uc.ExampleQuery(step="A2.tests", function="crc16_ccitt", patterns=["language c"],
                                text="uint16_t crc16_ccitt(const uint8_t *buf, size_t len) BC if (len == 0)")
        request = uc.GuidanceRequest(language="c", framework="ut_harness", functions=[
            query, uc.ExampleQuery(step="A1.plan", function="crc16_ccitt", text=query.text),
            uc.ExampleQuery(step="A2.tests", function="set_bits", text="void set_bits(uint8_t *r, int bits)")])
        found = self.kb.guidance(request).examples
        first = [e.function for e in found[uc.example_key("A2.tests", "crc16_ccitt")]]
        self.assertEqual(first[0], "crc8")                                     # the most similar first
        self.assertNotIn("crc8_copy", first)                                   # no near-duplicate
        self.assertNotIn("crc16", first)                                       # the same language only [SC-24]
        self.assertEqual([e.step for e in found[uc.example_key("A1.plan", "crc16_ccitt")]], ["A1.plan"])
        self.assertEqual(found[uc.example_key("A2.tests", "set_bits")][0].function, "bcm_setBits")   # setBits: set, bits
        self.assertEqual(self.kb.guidance(uc.GuidanceRequest(language="c", framework="ut_harness")).positive, [])
        self.kb.fts = False                                                    # an SQLite without FTS5
        fallback = [e.function for e in self.kb.guidance(request).examples[uc.example_key("A2.tests", "crc16_ccitt")]]
        self.assertIn(fallback[0], ("crc8", "crc8_copy"))                      # equally similar
        self.assertFalse({"crc8", "crc8_copy"} <= set(fallback))
        with mock.patch.dict(os.environ, {"UT_KNOWLEDGE": "off"}):                  # KP-8: the variant without
            self.assertEqual(self.kb.guidance(request).examples, {})

    def test_memory_fallback(self):
        (Path(self.tmp.name) / "blocker").write_text("a file where a folder is expected")
        store = kb.KnowledgeBaseAgent(config=kb.KBConfig(db_path=str(Path(self.tmp.name) / "blocker" / "kb.sqlite3")))
        self.assertEqual(store.backend, "memory")                                 # KB-21
        self.assertTrue(store.learn(self.event()).stored)


# ============================================================================ 7. Agent4
class CorrectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.repo = copy_pilot("c_pilot", Path(self.tmp.name))
        sut = self.repo / "src" / "thermostat.c"
        sut.write_text(sut.read_text().replace("return 100;", "return 99;"))      # seeded defect
        self.analyzer = a1.AnalyzerAgent()
        self.layout = self.analyzer.discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/thermostat.c"))
        self.evidence = self.analyzer.collect_evidence(self.layout, "clamp_percent").evidence
        self.failure = uc.Failure(failure_id="test-test_clamp_percent_upper_limit", kind="test",
                                  classification="source_behavior_defect", confidence=0.9, deterministic=False,
                                  route="agent4", affected_test="test_clamp_percent_upper_limit",
                                  affected_function="clamp_percent", diagnostic="expected 100, actual 99")
        self.agent = a4.CorrectorAgent()

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
    def test_preconditions(self):
        gap = self.failure.model_copy(update={"classification": "test_generation_defect", "route": "agent1"})
        resp = run(self.agent.call_local("correct", uc.CorrectionRequest(layout=self.layout, failure=gap)))
        self.assertEqual(resp.status, "rejected")                                 # A4-16, A4-54
        weak = [e for e in self.evidence if e.evidence_class == "E8"]
        resp = run(self.agent.call_local("correct", uc.CorrectionRequest(layout=self.layout, failure=self.failure,
                                                                          evidence=weak)))
        self.assertEqual(resp.status, "rejected")                                 # A4-23b
        self.assertIn("E1-E7", resp.reason)
        resp = run(self.agent.call_local("correct", uc.CorrectionRequest(layout=self.layout, failure=self.failure,
                                                                          evidence=self.evidence[:1])))
        self.assertEqual(resp.status, "rejected")                                 # A4-23f
        self.assertIn("A4-23f", resp.reason)

    def test_correct_is_minimal_and_candidate_only(self):
        with brain(UTBrain()):
            patch = run(self.agent.correct(uc.CorrectionRequest(layout=self.layout, failure=self.failure,
                                                                 evidence=self.evidence)))
        self.assertEqual(patch.status, "candidate_source_fix")                   # A4-25
        self.assertEqual(patch.function, "clamp_percent")
        # As stored on disk: on Windows the seeded copy has CRLF line ends, and the corrector keeps them.
        original = (self.repo / "src/thermostat.c").read_bytes().decode("utf-8")
        self.assertEqual(patch.patched_content, original.replace("return 99;", "return 100;"))
        self.assertIn("return 99;", original)                                     # source tree untouched [A4-23c]

    def test_fragment_outside_function_is_re_asked_then_rejected(self):
        b = UTBrain(patch={"original_fragment": "int read_sensor(int channel);", "corrected_fragment": "x",
                           "rationale": "r"})
        with brain(b):
            with self.assertRaises(ba.AgentFault) as ctx:
                run(self.agent.correct(uc.CorrectionRequest(layout=self.layout, failure=self.failure,
                                                             evidence=self.evidence)))
        self.assertEqual(ctx.exception.code, "MODEL_OUTPUT_INVALID")              # A4-19

    def publish_request(self, patch, **result):
        v = uc.VerificationResult(run_id="r1", stage="verify", suite_sha256="", decision="complete",
                                  sut_sha256=uc.sha256_bytes(patch.patched_content.encode()), build_ok=True,
                                  link_ok=True, tests=[uc.TestResult(name="test_clamp_percent_upper_limit",
                                                                     status=result.pop("status", "passed"))],
                                  analyzer_run=result.pop("analyzer_run", True), **result)
        return uc.PublishRequest(layout=self.layout, patch=patch, verification=v, original_repo=str(self.repo),
                                 output_dir=str(Path(self.tmp.name) / "out"))

    def patch(self):
        content = (self.repo / "src/thermostat.c").read_text().replace("return 99;", "return 100;")
        return uc.SourcePatch(patch_id="fix-1", file="src/thermostat.c", function="clamp_percent",
                              original_fragment="return 99;", corrected_fragment="return 100;", rationale="E2",
                              failure_id=self.failure.failure_id, patched_content=content)

    def test_publish_needs_agent3_verification(self):
        p = self.patch()
        check = self.agent.verification_problem
        self.assertIn("does not pass", check(self.publish_request(p, status="failed")))
        req = self.publish_request(p)
        req.verification.sut_sha256 = "0" * 64
        self.assertIn("not made on this patched source", check(req))
        self.assertEqual(check(self.publish_request(p)), "")
        self.assertIn("static analyzer", check(self.publish_request(p, analyzer_run=False)))       # A4-32a
        finding = uc.Finding(file="src/thermostat.c", line=20, function="clamp_percent", checker="core.DivideZero",
                             message="Division by zero")
        self.assertIn("new static-analyzer findings", check(self.publish_request(p, analyzer_findings=[finding])))
        req = self.publish_request(p, analyzer_findings=[finding])
        req.baseline_findings = [finding.model_copy(update={"line": 18})]                         # already there
        self.assertEqual(check(req), "")

    def test_a_fix_needs_two_independent_pieces_of_evidence(self):
        """[A4-23f] one header comment alone is not enough to change production code."""
        e2 = uc.EvidenceRef(evidence_id="e1", evidence_class="E2", source="include/thermostat.h", line=14, text="0..100")
        e4 = uc.EvidenceRef(evidence_id="e2", evidence_class="E4", source="include/limits.h", line=3, text="#define MAX 100")
        e6 = uc.EvidenceRef(evidence_id="e3", evidence_class="E6", source="src/ui.c", line=9, text="clamp_percent(v)")
        self.assertIn("A4-23f", self.agent.evidence_problem([e2]))
        self.assertIn("E1, E2 or E3", self.agent.evidence_problem([e4, e6]))
        self.assertEqual(self.agent.evidence_problem([e2, e4]), "")

    @unittest.skipUnless(HAVE_GIT, "git not installed")
    def test_publish_commits_to_a_separate_branch(self):
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], capture_output=True, text=True, check=True).stdout
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
        before = git("rev-parse", "HEAD").strip()
        result = self.agent.publish(self.publish_request(self.patch()))
        self.assertEqual(result.status, "verified_source_fix")
        self.assertEqual(result.branch, "ut-source-fix/fix-1")
        self.assertTrue(Path(result.patch_file).is_file())
        self.assertIn("+        return 100;", Path(result.patch_file).read_text())
        self.assertEqual(git("rev-parse", "HEAD").strip(), before)                # working branch unchanged
        self.assertIn("return 99;", (self.repo / "src/thermostat.c").read_text())
        self.assertEqual(git("status", "--porcelain").strip(), "")
        self.assertIn("return 100;", git("show", f"{result.branch}:src/thermostat.c"))


# ============================================================================ 8. orchestrator
# ============================================================================ build capture and dependencies
@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
class CaptureAndDependencyTests(unittest.TestCase):
    """Phase 1: the project's own build is captured [A3-BD1] and the SUT analyzed with those flags [A1-DA1]."""
    def setUp(self):
        self.tmp = tmpdir()
        self.d = Path(self.tmp.name)
        self.a1 = a1.AnalyzerAgent()
        self.a3 = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "work")))

    def tearDown(self):
        self.tmp.cleanup()

    def analyze(self, repo: Path, sut: str, **capture):
        layout = self.a1.discover(uc.DiscoverRequest(repo_root=str(repo), sut_file=sut))
        build = self.a3.capture(uc.CaptureRequest(layout=layout, **capture))
        return build, run(self.a1.dependencies(uc.DependencyRequest(layout=layout, build=build)))

    def test_nested_included_from_does_not_change_the_file(self):
        """A1-DA1: clang prints a location's file only when it changes; an "includedFrom" block nested deep
        inside an enum of a header is not a change of file. hal_trig_write follows such an enum in hal.h and
        must be declared there, not in the SUT .c file (which a stub header would then include)."""
        repo = copy_pilot("c_hal_pilot", self.d)
        _, layout = self.analyze(repo, "src/echo_sensor.c")
        deps = {e.name: e for e in layout.dependencies.entries}
        for name in ("hal_trig_write", "hal_echo_read", "hal_micros", "hal_delay_us"):
            self.assertEqual(deps[name].header, "include/hal.h", (name, deps[name]))

    @unittest.skipUnless(have_tool("cmake") and have_tool("ninja"), "cmake and ninja are not in the tool manifest")
    def test_cmake_build_is_captured(self):
        repo = copy_pilot("c_deps_pilot", self.d)
        with mock.patch.dict(os.environ, {"RC": "0"}):          # what the entry file once left behind [A3-BD15]
            build, layout = self.analyze(repo, "src/ctrl.c")
        self.assertEqual(build.source, "cmake", build.notes)                      # A3-BD2
        self.assertIn("SUT object built", build.outcome)                          # the tested variant's [A3-BD15]
        self.assertFalse(build.estimated)
        self.assertEqual(set(build.sut_flags), {"-DBOARD_REV=2", "-DMINI_LIMIT=40", "-I{repo}/include"})
        self.assertEqual({e.file for e in build.entries}, {"src/ctrl.c", "src/brake_ctrl.c", "src/steer_assist.c",
                                                           "lib/util.c", "lib/hw.c"})              # A3-BD8
        self.assertEqual([(v.name, v.tested) for v in build.variants], [("mini", True), ("mini_4ch", False)])  # A3-BD14
        self.assertEqual(layout.build_flags_source, "cmake")                      # A1-16a
        self.assertIn("include/ctrl.h", layout.headers)                           # not include/brake_ctrl.h
        self.assertNotIn("include/brake_ctrl.h", layout.headers)
        self.assertEqual(layout.sut_state, [])                                    # ctrl.c changes no global
        deps = {d.name: d for d in layout.dependencies.entries}
        self.assertEqual(set(deps), {"STATUS_REG", "calib", "clamp", "hw_read"})  # A1-DA4
        self.assertEqual(deps["clamp"].definition, "lib/util.c")                   # A1-DA5
        self.assertEqual(deps["calib"].kind, "variable")
        self.assertTrue(deps["calib"].is_const)
        self.assertEqual(deps["hw_read"].declaration, "int hw_read(int ch)")
        statics = [f.name for f in layout.functions if f.is_static]
        self.assertEqual(statics, ["scale"])                                       # A1-DA2
        in_range = next(f for f in layout.functions if f.name == "in_range")
        self.assertEqual(in_range.params[0].kind, "ptr_struct")
        self.assertTrue(in_range.params[0].nullable)
        self.assertEqual([f.name for f in in_range.params[0].fields], ["lo", "hi"])
        control = next(f for f in layout.functions if f.name == "control")
        self.assertEqual(control.params[2].enum_values, ["MODE_OFF", "MODE_ON", "MODE_AUTO"])

    @unittest.skipUnless(have_tool("cmake") and have_tool("ninja"), "cmake and ninja are not in the tool manifest")
    def test_the_suts_own_globals_are_its_state_not_dependencies(self):
        """A1-DA15: a static global of the SUT is neither stubbed nor searched for; it is the SUT state."""
        repo = copy_pilot("c_deps_pilot", self.d)
        _, layout = self.analyze(repo, "src/brake_ctrl.c")
        self.assertEqual({d.name for d in layout.dependencies.entries}, {"pressure_sensor"})
        self.assertEqual([(v.name, v.type) for v in layout.sut_state], [("g_faults", "int")])
        _, layout = self.analyze(repo, "src/steer_assist.c")
        self.assertEqual([v.name for v in layout.sut_state], ["g_last_torque"])      # const tables are not state

    @unittest.skipUnless(have_tool("cmake") and have_tool("ninja"), "cmake and ninja are not in the tool manifest")
    def test_llm_decides_real_and_its_unit_is_linked(self):
        """[A1-DA8, A1-DA10] with a brain answering real, the definition's file is compiled and linked."""
        repo = copy_pilot("c_deps_pilot", self.d)
        with brain(UTBrain(dependency="real")):
            build, layout = self.analyze(repo, "src/ctrl.c")
        deps = {d.name: d for d in layout.dependencies.entries}
        self.assertEqual(deps["clamp"].decision, "real")
        self.assertTrue(deps["clamp"].decided_by.startswith("llm"))
        self.assertIn("lib/util.c", layout.dependencies.real_sources)
        # hw.c is real too, so STATUS_REG and calib come with it (one translation unit) [A1-DA10]
        self.assertEqual({deps[n].decision for n in ("hw_read", "STATUS_REG", "calib")}, {"real"})
        result = self.a3.execute(uc.VerifyRequest(layout=layout, stage="probe"))
        self.assertTrue(result.link_ok, [d.message for d in result.diagnostics])   # A3-38a

    def test_without_brain_every_repository_dependency_is_stubbed(self):
        repo = copy_pilot("c_deps_pilot", self.d)
        with brain(DownBrain()):
            _, layout = self.analyze(repo, "src/ctrl.c")
        for dep in layout.dependencies.entries:
            self.assertEqual(dep.decision, "stub", dep)                           # A1-DA9
        stubs = self.a1._stubs(layout, None)
        var = next(s for s in stubs if s.function == "STATUS_REG")
        self.assertEqual(var.var_type, "volatile unsigned int STATUS_REG")        # A2-21j: exact type, same name

    def test_client_compilation_database_is_mapped(self):
        """[A3-BD2] a compile_commands.json from another checkout is mapped onto the working copy."""
        repo = copy_pilot("c_pilot", self.d)
        db = self.d / "client_db.json"
        db.write_text(json.dumps([{"directory": "C:/work/thermo/build", "file": "C:/work/thermo/src/thermostat.c",
                                   "command": "clang -O2 -DUNIT=1 -I../include -IC:/sdk/inc -std=c11 -c "
                                              "C:/work/thermo/src/thermostat.c"}]))
        build, layout = self.analyze(repo, "src/thermostat.c", compile_commands=str(db))
        self.assertEqual(build.source, "client_compile_commands")
        self.assertEqual(build.sut_flags, ["-DUNIT=1", "-I{repo}/include", "-IC:/sdk/inc", "-std=c11"])

    @unittest.skipUnless(have_tool("make") or shutil.which("make"), "make not available")
    def test_make_dry_run_is_captured(self):
        repo = copy_pilot("c_pilot", self.d)
        (repo / "Makefile").write_text("CFLAGS = -O2 -Iinclude -DTHERMO=3\nall: src/thermostat.o\n"
                                       "src/thermostat.o: src/thermostat.c\n\t$(CC) $(CFLAGS) -c $< -o $@\n")
        if not have_tool("make"):
            self.skipTest("make is not in the tool manifest (run setup for a Makefile repository)")
        build, _ = self.analyze(repo, "src/thermostat.c")
        self.assertEqual(build.source, "make")
        self.assertIn("-DTHERMO=3", build.sut_flags)

    def test_static_function_is_tested_through_the_access_file(self):
        """[A2-21k] static functions are called through wrappers; coverage still measures the SUT."""
        repo = copy_pilot("c_deps_pilot", self.d)
        with brain(DownBrain()):
            _, layout = self.analyze(repo, "src/ctrl.c")
        plan = run(self.a1.plan(uc.PlanRequest(layout=layout)))
        stub_path, stub_src, stub_h = a2.render_stubs(plan)
        access = a2.render_sut_access(plan)
        self.assertIn('#include "../src/ctrl.c"', access[1])
        suite = ('#include "ut_harness.h"\n#include "ut_stubs_ctrl.h"\nint ut_static_scale(int v);\n'
                 'UT_TEST(test_scale) { UT_OBSERVE_INT("s", ut_static_scale(3)); }\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_scale)\nUT_SUITE_END\n')
        bundle = uc.FileActionBundle(bundle_id="b", actions=[
            uc.FileAction(path=stub_path, action="create", role="stub", content=stub_src),
            uc.FileAction(path=stub_path[:-2] + ".h", action="create", role="helper",
                          content=stub_h),
            uc.FileAction(path=access[0], action="create", role="sut_access", content=access[1]),
            uc.FileAction(path=layout.suite_file, action="create", role="testsuite", content=suite)])
        result = self.a3.execute(uc.VerifyRequest(layout=layout, stage="verify", bundle=bundle))
        self.assertTrue(result.link_ok, [d.message for d in result.diagnostics])
        scale = next(f for f in result.coverage.functions if f.name == "scale")
        self.assertTrue(scale.executed)                                           # measured in the SUT's lines
        self.assertEqual(scale.metrics.LC.percent, 100.0)

    def fuzz_control(self, seconds=3):
        repo = copy_pilot("c_deps_pilot", self.d)
        with brain(DownBrain()):
            _, layout = self.analyze(repo, "src/ctrl.c")
        plan = run(self.a1.plan(uc.PlanRequest(layout=layout)))
        stub_path, stub_src, stub_h = a2.render_stubs(plan)
        bundle = uc.FileActionBundle(bundle_id="b", actions=[
            uc.FileAction(path=stub_path, action="create", role="stub", content=stub_src),
            uc.FileAction(path=stub_path[:-2] + ".h", action="create", role="helper",
                          content=stub_h)])
        gap = uc.GapItem(gap_id="BC:control:13:9:T", function="control", metric="BC", line=13, column=9)
        return self.a3.fuzz(uc.FuzzRequest(layout=layout, bundle=bundle, functions=["control"], gaps=[gap],
                                           seconds=seconds))

    def assert_control_input(self, result):
        self.assertIn("control", result.functions, result.unsupported)
        self.assertTrue(result.inputs, result.reason)
        self.assertIn("BC:control:13:9:T", result.inputs[0].covers)
        self.assertTrue(any(line.startswith("STATUS_REG = ") for line in result.inputs[0].setup))

    @full_run
    def test_fuzzer_finds_inputs_for_gaps(self):
        """[A3-FZ1..A3-FZ4, A3-FZ11] coverage-guided search returns readable inputs that reach a gap - with
        libFuzzer where the toolchain has it, otherwise with ut_search."""
        result = self.fuzz_control()
        self.assertEqual(result.engine, "libFuzzer" if self.a3.toolchain().tools.fuzzer else "ut_search")
        self.assert_control_input(result)

    def test_ut_search_without_libfuzzer_is_deterministic(self):
        """[A3-FZ9, A3-FZ10] without libFuzzer (llvm-mingw on Windows) Agent3's own search finds the input;
        with the same seed and number of executions it finds the same inputs again."""
        chain = self.a3.toolchain()
        self.a3.cfg.search_executions = 20_000
        with mock.patch.object(chain.tools, "fuzzer", False):
            first = self.fuzz_control(seconds=60)
            shutil.rmtree(self.d / "c_deps_pilot")
            second = self.fuzz_control(seconds=60)
        self.assertEqual(first.engine, "ut_search")
        self.assert_control_input(first)
        self.assertEqual(first.executions, second.executions)
        self.assertEqual([(i.args, i.setup, i.covers) for i in first.inputs],
                         [(i.args, i.setup, i.covers) for i in second.inputs])


# ============================================================================ tool setup [TP]
class SetupTests(unittest.TestCase):
    def test_unpack_refuses_paths_outside_the_target(self):
        import zipfile
        with tmpdir() as d:
            d = Path(d)
            bad = d / "bad.zip"
            with zipfile.ZipFile(bad, "w") as z:
                z.writestr("top/../../evil.txt", "x")
            with self.assertRaises(us.SetupFailed):
                us.unpack(bad, d / "out", "top")
            self.assertFalse((d.parent / "evil.txt").exists())
            good = d / "good.zip"
            with zipfile.ZipFile(good, "w") as z:
                z.writestr("top/bin/tool", "x")
                z.writestr("top/doc/readme", "y")
            us.unpack(good, d / "out2", "top", keep=lambda rel: rel.startswith("bin/"))
            self.assertTrue((d / "out2/bin/tool").is_file())
            self.assertFalse((d / "out2/doc").exists())

    def test_download_with_a_wrong_checksum_is_refused(self):
        with tmpdir() as d:
            with self.assertRaises(us.SetupFailed):
                us.download("http://example.com/x.zip", "0" * 64, Path(d) / "x.zip")    # not HTTPS
            target = Path(d) / "cached.bin"
            target.write_bytes(b"abc")
            sha = __import__("hashlib").sha256(b"abc").hexdigest()
            self.assertEqual(us.download("https://example.invalid/cached.bin", sha, target), target)  # cached, verified

    def test_every_pinned_download_is_official_and_checksummed(self):
        cfg = json.loads((SRC / "ut_config.json").read_text())["setup"]["downloads"]
        official = ("https://github.com/llvm/", "https://github.com/mstorsjo/llvm-mingw/", "https://github.com/Kitware/",
                    "https://github.com/ninja-build/", "https://github.com/git-for-windows/",
                    "https://github.com/bazelbuild/", "https://github.com/mesonbuild/")
        for os_name, pins in cfg.items():
            for name, pin in pins.items():
                self.assertTrue(pin["url"].startswith(official), f"{os_name}/{name}")    # TP-4
                self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")

    def test_build_systems_are_detected(self):
        with tmpdir() as d:
            d = Path(d)
            (d / "a").mkdir()
            (d / "a/meson.build").write_text("")
            (d / "MODULE.bazel").write_text("")
            (d / "app.vcxproj").write_text("")
            self.assertEqual(us.Setup.build_systems(d), ["bazel", "meson", "msbuild"])     # TP-5

    @unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
    def test_manifest_toolchain_passes_the_functional_check(self):
        tools = _tools()
        with tmpdir() as d:
            features = us.check_toolchain(dict(tools.tc), Path(d))                          # TP-8
        self.assertTrue(features["mcdc"] and features["cpp"])

    def test_missing_manifest_stops_with_the_setup_instruction(self):
        with tmpdir() as d:
            with self.assertRaises(ba.AgentFault) as ctx:
                uc.Tools.load(Path(d) / "toolchain.json")
            self.assertIn("setup", ctx.exception.message)                                    # TP-10, no fallback


class LedgerTests(unittest.TestCase):
    def test_hash_chain_detects_tampering(self):
        with tmpdir() as d:
            path = Path(d) / "ledger.jsonl"
            ledger = up.Ledger(path)
            ledger.append(agent="a", tool="t", status="success")
            ledger.append(agent="b", tool="t", status="rejected")
            self.assertTrue(up.Ledger.verify(path))
            path.write_text(path.read_text().replace('"rejected"', '"success"'))
            self.assertFalse(up.Ledger.verify(path))                              # ORC-8


def cov(iterations: int = 5, **extra) -> dict:
    """A coverage section (COV-1, COV-9) with the same iteration limit for every ASIL level."""
    return {"default_level": "D", "files": {}, "iterations": {lvl: iterations for lvl in uc.ASIL_LEVELS}, **extra}


def pipeline_config(d: Path, transport="inprocess", viewer: dict | None = None, coverage: dict | None = None,
                    **pipeline) -> Path:
    config = json.loads((SRC / "ut_config.json").read_text())
    config["viewer"] = {"enabled": False, "open_browser": False, **(viewer or {})}       # VIEW-3: off in tests
    if coverage is not None:
        config["coverage"] = coverage
    for role in config["pipeline"]["agents"]:
        config["pipeline"]["agents"][role]["transport"] = transport
    config["pipeline"].update({"output_dir": str(d / "ut_output"), "fuzz_seconds": 3, **pipeline})
    config["compilation"]["work_dir"] = str(d / "ut_work")
    config["compilation"]["tools_manifest"] = str(uc.DEFAULT_MANIFEST)
    config["kb"]["db_path"] = str(d / "kb.sqlite3")
    config["brain"] = {"engine": {"mode": "qwen"}, "qwen": {"startup_timeout": 2, "base_url": NO_BRAIN_URL},
                       "anthropic": {"enabled": False}}                                     # TP-21
    path = d / "ut_config.json"
    path.write_text(json.dumps(config, indent=1))
    return path


@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
@unittest.skipUnless(HAVE_FASTMCP and HAVE_LANGGRAPH, "fastmcp and langgraph are required")
@full_run
class PipelineTests(unittest.TestCase):
    """The whole pipeline over MCP (in-process FastMCP transport), scripted brain."""
    def setUp(self):
        self.tmp = tmpdir()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_pipeline(self, repo: Path, sut: str, **pipeline) -> uc.PipelineResult:
        orchestrator = up.Orchestrator(pipeline_config(self.d, **pipeline))
        return run(orchestrator.run(uc.PipelineRequest(repo_root=str(repo), sut_file=sut)))

    def test_c_pilot_reaches_full_coverage(self):
        repo = copy_pilot("c_pilot", self.d)
        with brain(UTBrain()):
            result = self.run_pipeline(repo, "src/thermostat.c")
        print("\n" + up.report(result))
        self.assertEqual(result.final_state, "verified_measured", result.reason)
        for name in uc.METRICS:
            self.assertEqual(getattr(result.metrics, name).percent, 100.0, name)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.iterations[0].stage, "probe")                     # ORC-3
        self.assertTrue(any(i.stage.startswith("stage A") for i in result.iterations))   # ORC-4
        delivered = (repo / "tests/test_thermostat.c").read_text()                  # delivered tests
        self.assertTrue(up.replaceable(delivered.encode()))                         # stamped [ORC-14]
        self.assertTrue((repo / "tests/ut_harness.c").is_file())                    # builds without us
        self.assertTrue(up.Ledger.verify(Path(result.ledger)))
        entries = [json.loads(l) for l in Path(result.ledger).read_text().splitlines()]
        self.assertEqual(entries[0]["tool"], "discover")
        tools = [e["tool"] for e in entries[1:] if e["tool"] != "suggest_flags"]     # only after an estimated capture
        self.assertEqual(tools[:3], ["capture", "dependencies", "probe"])                  # ORC-3
        self.assertTrue(all(e["signatures"]["sut"] for e in entries))           # ORC-10
        self.assertTrue((Path(result.output_dir) / "report.md").is_file())       # ORC-12
        self.assertTrue(any(line.startswith("A2.tests: ") for line in result.token_use), result.token_use)  # ARCH-15
        self.assertTrue(any(line.startswith("test-style 1.0 (application)") for line in result.knowledge_packs))
        self.assertTrue(any(e.get("token_plans") for e in entries))                 # in the ledger [KP-7]
        self.assertIn("## Brain calls and tokens per step", (Path(result.output_dir) / "report.md").read_text())

    def test_a_second_run_reuses_what_the_first_verified(self):                    # CA-1 .. CA-6
        """The same file twice with the caches on: the second run takes the brain's answers, the verified tests, the
        objects and the measurements of tests alone from the cache, asks the brain less and ends the same."""
        config = pipeline_config(self.d)
        raw = json.loads(config.read_text())
        raw["cache"] = {"dir": str(self.d / "run_cache")}
        config.write_text(json.dumps(raw))
        results, calls = [], []
        with mock.patch.dict(os.environ, {"UT_CACHE": ""}):
            for k in range(2):                                    # the same folder each time, freshly copied
                shutil.rmtree(self.d / "r", ignore_errors=True)
                repo = copy_pilot("c_pilot", self.d / "r")
                scripted = UTBrain()
                with brain(scripted):
                    results.append(run(up.Orchestrator(config).run(uc.PipelineRequest(
                        repo_root=str(repo), sut_file="src/thermostat.c"))))
                calls.append(len(scripted.generate_calls) + scripted.decide_calls)
        first, second = results
        print("\n" + up.report(second))
        self.assertEqual(first.final_state, "verified_measured", first.reason)
        self.assertEqual(second.final_state, "verified_measured", second.reason)
        self.assertEqual(second.metrics.model_dump(), first.metrics.model_dump())        # the same result
        self.assertLess(calls[1], calls[0])                                               # fewer brain calls
        use = "\n".join(second.cache_use)
        self.assertIn("[CA-2]", use)
        self.assertIn("compiled objects [CA-5]", use)
        self.assertIn("verified test(s) of earlier runs reused", use)                     # CA-4
        self.assertIn("## What the caches saved [CA-1]", (Path(second.output_dir) / "report.md").read_text())
        with mock.patch.dict(os.environ, {"UT_CACHE": "off"}):                              # --no-cache
            shutil.rmtree(self.d / "r", ignore_errors=True)
            repo = copy_pilot("c_pilot", self.d / "r")
            scripted = UTBrain()
            with brain(scripted):
                third = run(up.Orchestrator(config).run(uc.PipelineRequest(repo_root=str(repo),
                                                                           sut_file="src/thermostat.c")))
        self.assertEqual(third.cache_use, [])
        self.assertEqual(len(scripted.generate_calls) + scripted.decide_calls, calls[0])
        self.assertIn("dependency analysis of an earlier run", use)                     # CA-10
        import sqlite3                                                                     # KB-23, KB-25
        with contextlib.closing(sqlite3.connect(self.d / "kb.sqlite3")) as db:
            kept = dict(db.execute("SELECT kind, COUNT(*) FROM examples GROUP BY kind").fetchall())
        print("KB examples:", kept, "\n".join(first.brain_answers), sep="\n")
        self.assertGreater(kept.get("test_example", 0) + kept.get("plan_example", 0), 0)
        self.assertTrue(any(row.startswith("A1.plan:") for row in first.brain_answers), first.brain_answers)
        # CA-10: a stored Stage A result that does not build here is replaced, never the end of the run
        for entry in (self.d / "run_cache" / "stage_a").rglob("*.json"):
            stored = json.loads(entry.read_text())
            fields = json.loads(stored["fields"])
            for key in ("sut_flags",):
                fields["build"][key] = fields["build"][key] + ["-include", "ut_no_such_header.h"]
            fields["compile_flags"] = fields["compile_flags"] + ["-include", "ut_no_such_header.h"]
            stored["fields"] = json.dumps(fields)
            entry.write_text(json.dumps(stored))
        with mock.patch.dict(os.environ, {"UT_CACHE": ""}):
            shutil.rmtree(self.d / "r", ignore_errors=True)
            repo = copy_pilot("c_pilot", self.d / "r")
            with brain(UTBrain()):
                fourth = run(up.Orchestrator(config).run(uc.PipelineRequest(repo_root=str(repo),
                                                                            sut_file="src/thermostat.c")))
        self.assertEqual(fourth.final_state, "verified_measured", fourth.reason)
        self.assertEqual(fourth.metrics.model_dump(), first.metrics.model_dump())
        self.assertIn("did not build with this run", "\n".join(fourth.cache_use))
        self.assertIn("front-end parses [CA-11]", "\n".join(fourth.cache_use))      # the analysis anew: parse reused

    def test_seeded_defect_is_fixed_on_a_branch(self):
        """Phase 5 gate: a valid E2 test exposes a real defect; the source is corrected and
        reverified, the test is not weakened, and the user's working branch is unchanged."""
        repo = copy_pilot("c_pilot", self.d)
        sut = repo / "src/thermostat.c"
        sut.write_text(sut.read_text().replace("return 1;", "return 2;"))      # heating path, E2 says 1
        if HAVE_GIT:
            for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "base")):
                subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args],
                               check=True, capture_output=True)
        fix_brain = UTBrain(candidates=[
            {"original_fragment": "return 2;", "corrected_fragment": "return 1; /* heat, see @return */",
             "rationale": "E2: @return 1 to heat"},
            {"original_fragment": "return 2;", "corrected_fragment": "return 1;", "rationale": "E2: @return 1 to heat"}])
        with writer_brain(fix_brain):                 # its E2 test is a whole test, written by a full profile [BP-2]
            result = self.run_pipeline(repo, "src/thermostat.c")
        print("\n" + up.report(result))
        self.assertEqual(len(result.source_fixes), 1, result.open_items)
        fix = result.source_fixes[0]
        self.assertEqual(fix.function, "thermostat_command")
        self.assertEqual(fix.corrected_fragment, "return 1;")                       # the smaller candidate [A4-23g]
        self.assertTrue(any("is smaller" in c for c in result.fix_candidates), result.fix_candidates)
        self.assertTrue(result.reviews and "agree" in result.reviews[0])            # second opinion [A4-23h]
        self.assertEqual(fix.status, "verified_source_fix")
        self.assertIn("return 2;", sut.read_text())                               # working tree unchanged
        # The tests expect the corrected source, so they never go into the working tree [A4-33].
        self.assertFalse((repo / "tests/test_thermostat.c").exists())
        suite = (Path(result.output_dir) / "generated/tests/test_thermostat.c").read_text()
        self.assertIn("UT_EXPECT_EQ_INT(thermostat_command(20, 2), 1);", suite)  # test kept, not weakened
        patch_files = list((Path(result.output_dir) / "source_fixes").glob("*.patch"))
        self.assertEqual(len(patch_files), 1)
        self.assertEqual(result.final_state, "verified_measured", result.reason)
        if HAVE_GIT:
            branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "ut-source-fix/*"],
                                      capture_output=True, text=True).stdout
            self.assertIn("ut-source-fix/", branches)
            branch = branches.strip().lstrip("* ").strip()
            show = lambda path: subprocess.run(["git", "-C", str(repo), "show", f"{branch}:{path}"],
                                               capture_output=True, text=True).stdout
            self.assertIn("return 1;", show("src/thermostat.c"))                 # the fix ...
            self.assertIn("thermostat_command(20, 2), 1);", show("tests/test_thermostat.c"))   # ... and its tests
            self.assertIn("UT_TEST", show("tests/ut_harness.h"))
            status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                                    text=True).stdout
            self.assertEqual(status, "")                                         # nothing changed in the worktree

    def test_a_wrong_brain_test_takes_its_values_from_the_run_when_no_source_change_exists(self):
        """A4-23j, from the first pilot run with the remote Qwen: the brain's test sets a temperature at the edge of the
        band and expects heating; the classification blames the source, Agent4 finds no change - the test keeps its
        inputs and takes its values from the run (A2-21p), the run ends verified and the report keeps the item for the
        developer."""
        wrong = "ut_stub_read_sensor_return = 18;\nUT_EXPECT_EQ_INT(thermostat_command(20, 2), 1);"
        tests = dict(CANNED_TESTS, thermostat_command=[{"name": "test_thermostat_heats_below_band", "scenario_ids": [],
                                                         "body": wrong, "evidence_class": "E2"}])
        same = {"original_fragment": "return 1;", "corrected_fragment": "return 1;", "rationale": "E2: @return 1"}
        repo = copy_pilot("c_pilot", self.d)
        with writer_brain(UTBrain(tests=tests, candidates=[same])):     # a brain that writes whole tests [BP-2]
            result = self.run_pipeline(repo, "src/thermostat.c")
        print("\n" + up.report(result))
        self.assertEqual(result.final_state, "verified_measured", result.reason)
        self.assertEqual(result.source_fixes, [])
        self.assertTrue(any("[A4-23j]" in item and "test_thermostat_heats_below_band" in item
                            for item in result.open_items), result.open_items)
        suite = (repo / "tests/test_thermostat.c").read_text()
        self.assertIn("ut_stub_read_sensor_return = 18;", suite)                     # its inputs kept ...
        self.assertIn("UT_EXPECT_EQ_INT(thermostat_command(20, 2), 0);", suite)      # ... its value from the run
        self.assertEqual(sum("agent_sourcecode_corrector.candidates" in item for item in result.open_items), 1)
        journal = Path(result.journal)                                           # LA-2: the deviation is recorded
        self.assertTrue(journal.is_file(), result.journal)
        self.assertIn("the test contradicts its own scenario or evidence", journal.with_suffix(".md").read_text())
        self.assertTrue(any(line.startswith("learned (") for line in result.lessons), result.lessons)   # LA-8

    def test_brain_plans_the_documented_behaviour(self):
        """A1-PL1, A1-25y, A2-21m: after the deterministic tests, the brain plans a check of the stated
        behaviour; Agent1 checks it and Agent2 assembles it into an asserting test."""
        def clamp_plan(context):
            if "EVIDENCE:clamp_percent" not in context:                  # the coverage gaps first
                return scripted_plan("clamp_percent", context)
            ev = re.search(r"- (EV-clamp_percent-\d+) E2 [^\n]*0\.\.100", context)
            return [planned(gap_ids=["EVIDENCE:clamp_percent"], name="test_clamp_percent_limits_high_values",
                            purpose="values above 100 are limited to 100", args=[{"name": "value", "value": "150"}],
                            expected_return="100", evidence_ids=[ev.group(1)], classification="confirmed_gap",
                            quotes=[{"evidence_id": ev.group(1), "text": "value limited to the range 0..100"}])] if ev else []
        repo = copy_pilot("c_pilot", self.d)
        with brain(UTBrain(tests={}, plans={"clamp_percent": clamp_plan})):
            result = self.run_pipeline(repo, "src/thermostat.c")
        print("\n" + up.report(result))
        self.assertEqual(result.final_state, "verified_measured", result.reason)
        suite = (repo / "tests/test_thermostat.c").read_text()
        self.assertIn("UT_TEST(test_clamp_percent_limits_high_values)", suite)
        self.assertIn("UT_EXPECT_EQ_INT(clamp_percent(150), 100);", suite)
        self.assertIn("values above 100 are limited to 100", suite)                      # A2-21q
        self.assertTrue(any("clamp_percent: 1 scenario(s) by qwen" in p for p in result.planning), result.planning)

    def test_cheap_tests_first_and_the_search_during_the_planning(self):
        """A1-PL1: with nothing measured, iteration 1 runs the rule-based tests and the search; the brain plans what
        they left. ORC-5a: the search runs while the brain plans, its inputs join that plan (inputs only, no brain),
        and the search of the run stays within its budget (A3-FZ3)."""
        repo = copy_pilot("c_deps_pilot", self.d)
        if not (have_tool("cmake") and have_tool("ninja")):
            self.skipTest("cmake and ninja are not in the tool manifest")
        calls = []
        original = up.Orchestrator.call

        async def timed(orch, role, tool, payload, expect):
            start = time.monotonic()
            try:
                return await original(orch, role, tool, payload, expect)
            finally:
                calls.append((tool, start, time.monotonic(), payload))
        with brain(UTBrain()), mock.patch.object(up.Orchestrator, "call", timed):
            result = run(up.Orchestrator(pipeline_config(self.d, fuzz_seconds=10, fuzz_budget_seconds=40)).run(
                uc.PipelineRequest(repo_root=str(repo), sut_file="src/brake_ctrl.c")))
        plans = [c for c in calls if c[0] == "plan" and c[3].iteration >= 1]
        first = [c for c in plans if c[3].iteration == 1 and not c[3].inputs_only]
        self.assertTrue(first, plans)
        self.assertTrue(any(p.startswith("cheap tests first") for p in result.planning), result.planning)
        searches = [c for c in calls if c[0] == "fuzz"]
        self.assertTrue(searches, result.fuzzing)
        self.assertTrue(any(p[1] < s[2] and s[1] < p[2] for s in searches for p in plans if p[3].iteration > 1),
                        [(c[0], round(c[1], 2), round(c[2], 2)) for c in calls])          # at the same time
        granted = sum(s[3].seconds * len(s[3].functions) for s in searches)
        self.assertLessEqual(granted, 40)                                                   # one budget per run
        self.assertTrue(all(s[3].seconds >= 5 for s in searches))
        orch = up.Orchestrator(pipeline_config(self.d, fuzz_seconds=3, fuzz_budget_seconds=0))
        state = up.RunState(uc.PipelineRequest(repo_root=str(repo), sut_file="src/brake_ctrl.c"), repo, self.d, repo)
        last = next(c for c in calls if c[0] == "fuzz")[3]
        verified = uc.VerificationResult(run_id="r", stage="verify", sut_sha256="x", suite_sha256="", build_ok=True, decision="gaps_remain",
                                         coverage=uc.CoverageReport(toolchain="t", definitions="d", functions=[
                                             uc.FunctionCoverage(name=g.function, gaps=[g]) for g in last.gaps[:1]]))
        self.assertIsNone(run(orch._fuzz(state, verified, 3)))                             # the budget is used up
        self.assertIn("budget", state.fuzzing[-1])

    def test_requirement_documents_given_for_the_run(self):
        """A1-RQ1, A1-RQ3, A1-RQ5: a document from outside the repository is copied, recorded in the ledger,
        linked and reported per function; the repository does not receive it."""
        repo = copy_pilot("c_pilot", self.d)
        spec = self.d / "spec.md"
        spec.write_text("- REQ-TH-30: clamp_percent limits the value to the range 0..100.\n")
        with brain(UTBrain()):
            result = run(up.Orchestrator(pipeline_config(self.d)).run(uc.PipelineRequest(
                repo_root=str(repo), sut_file="src/thermostat.c", requirements=[str(spec), "docs/none.md"])))
        self.assertEqual(result.final_state, "verified_measured", result.reason)
        self.assertEqual(result.requirement_documents, [f"requirements/spec.md sha256 {uc.file_sha256(spec)} "
                                                        f"(given as {spec})"])
        copied = Path(result.output_dir) / "requirements" / "spec.md"
        self.assertEqual(copied.read_bytes(), spec.read_bytes())
        entries = [json.loads(line) for line in Path(result.ledger).read_text().splitlines()]
        self.assertTrue(any(e["tool"] == "requirement_document" and e.get("sha256") == uc.file_sha256(spec)
                            for e in entries))
        status = {f.function: f for f in result.requirement_status}
        self.assertTrue(status["clamp_percent"].requirements[0].startswith("REQ-TH-30 (rule) requirements/spec.md:1"))
        self.assertTrue(any("docs/none.md" in item and "does not exist" in item for item in result.open_items))
        text = up.report(result)
        self.assertIn("## Requirement documents given for the run [A1-RQ3]", text)
        self.assertIn("## Requirements per function [A1-RQ5]", text)
        self.assertFalse((repo / "requirements").exists())

    def test_embedded_driver_with_a_polled_clock_reaches_its_targets_without_a_brain(self):
        """The first embedded-driver run (19 Oct 2026): a HAL header with an enum before its functions, a timeout
        loop on a stubbed microsecond clock and the stubs of static helpers. A2-21w, A2-21x, A2-21r, A3-45a/b."""
        repo = copy_pilot("c_hal_pilot", self.d)
        with brain(UTBrain()):
            result = self.run_pipeline(repo, "src/echo_sensor.c")
        self.assertEqual(result.final_state, "verified_measured", up.report(result))
        self.assertEqual(result.unfinished_tests, [])
        header = (repo / "tests/stubs/ut_stubs_echo_sensor.h").read_text()
        self.assertNotRegex(header, r'#include "[^"]+\.c"')
        self.assertIn("ut_stub_hal_micros_step", header)

    @full_run
    def test_embedded_c_files_reach_their_targets_without_a_brain(self):
        """Registers in host memory, register polling, a superloop main, interrupt flags, function-local statics,
        target keywords and inline assembly of the target (A3-HW1..HW3, A2-21y, A2-21z, A2-21ab, A3-45c)."""
        for sut, check in (("src/gpio_driver.c", "register"), ("src/critical.c", "asm"),
                           ("src/main_loop.c", "superloop"), ("src/scheduler.c", "state")):
            with self.subTest(sut=sut):
                d = self.d / Path(sut).stem
                d.mkdir()
                repo = copy_pilot("c_embedded_pilot", d)
                orchestrator = up.Orchestrator(pipeline_config(d))
                with brain(UTBrain()):
                    result = run(orchestrator.run(uc.PipelineRequest(repo_root=str(repo), sut_file=sut)))
                self.assertEqual(result.final_state, "verified_measured", up.report(result))
                suite = (repo / "tests" / f"test_{Path(sut).stem}.c").read_text()
                if check == "register":
                    self.assertTrue(result.register_pages)
                    self.assertIn("UT_REG_SET_LATER(UART1_SR, UART_TXE, 20);", suite)
                    self.assertNotIn("UT_EXPECT_NO_RETURN", suite)
                elif check == "asm":
                    notes = " ".join(result.host_notes)
                    self.assertIn("__packed", notes)
                    self.assertIn("cpsid i", notes)
                    self.assertIn("ut_asm_value = 1;", suite)
                    self.assertIn("__packed", (repo / "include" / "critical.h").read_text())   # never changed
                elif check == "superloop":
                    self.assertIn("UT_EXPECT_NO_RETURN(main());", suite)
                else:
                    self.assertIn("ut_state_set_g_tick_flag(1);", suite)
                self.assertEqual(result.unfinished_tests, [])

    @full_run
    def test_embedded_cpp_files_reach_their_targets_without_a_brain(self):
        """An injected interface and its fake, exceptions, a static object in a function, a register wait,
        a header-only template, inline assembly and a superloop (A2-21aa, A3-45e, A3-45c, A1-DA19, A3-HW3)."""
        for sut, check in (("src/motor_ctrl.cpp", "motor"), ("include/ring_buffer.hpp", "template"),
                           ("src/counter.cpp", "asm"), ("src/app_main.cpp", "superloop")):
            with self.subTest(sut=sut):
                d = self.d / Path(sut).stem
                d.mkdir()
                repo = copy_pilot("cpp_embedded_pilot", d)
                orchestrator = up.Orchestrator(pipeline_config(d))
                with brain(UTBrain()):
                    result = run(orchestrator.run(uc.PipelineRequest(repo_root=str(repo), sut_file=sut)))
                self.assertEqual(result.final_state, "verified_measured", up.report(result))
                self.assertEqual(result.language, "cpp")
                suite = (repo / "tests" / f"test_{Path(sut).stem}.cpp").read_text()
                if check == "motor":
                    self.assertIn("class ut_fake_IEncoder : public IEncoder",
                                  (repo / f"tests/stubs/ut_stubs_{Path(sut).stem}.h").read_text())
                    self.assertIn("std::invalid_argument);", suite)
                    self.assertIn("UT_REG_SET_LATER(hw::tim2()->SR, hw::SR_UIF, 20);", suite)
                elif check == "template":
                    self.assertIn("extern template class RingBuffer<std::int32_t, 4>;", suite)
                    self.assertIn("std::overflow_error);", suite)
                elif check == "asm":
                    self.assertIn("cpsid i", " ".join(result.host_notes))
                else:
                    self.assertIn("UT_EXPECT_NO_RETURN(main());", suite)

    def test_real_repository_patterns_reach_their_targets_without_a_brain(self):
        """Patterns of five open-source code bases, rebuilt from scratch: a header that needs its configuration
        header first, T * const parameters, a header found only next to the SUT, _Atomic fields and a callback
        field, a handle whose Instance points to registers, typedef'd return types, C++ namespaces with const
        pointers to const data (A2-21ad, A1-DA21, A2-21f)."""
        for pilot, sut, check in (("c_realrepo_pilot", "kernel/src/klist.c", "order"),
                                  ("c_realrepo_pilot", "kernel/src/kqueue.c", "typedef"),
                                  ("c_realrepo_pilot", "codec/ring.c", "neighbour"),
                                  ("c_realrepo_pilot", "drivers/crc_drv.c", "handle"),
                                  ("cpp_realrepo_pilot", "src/dispatcher.cpp", "namespace")):
            with self.subTest(sut=sut):
                d = self.d / Path(sut).stem
                d.mkdir()
                repo = copy_pilot(pilot, d)
                with brain(UTBrain()):
                    result = run(up.Orchestrator(pipeline_config(d)).run(uc.PipelineRequest(repo_root=str(repo),
                                                                                            sut_file=sut)))
                self.assertEqual(result.final_state, "verified_measured", up.report(result))
                header = repo / f"tests/stubs/ut_stubs_{Path(sut).stem}.h"
                stubs = header.read_text() if header.exists() else ""
                suite = next((repo / "tests").glob(f"test_{Path(sut).stem}.c*")).read_text()
                if check == "order":                                   # the SUT's own order, kernel.h first
                    self.assertLess(suite.index('#include "kernel.h"'), suite.index('#include "klist.h"'))
                    self.assertIn("kList_t ut_v0 = {", suite)
                elif check == "typedef":
                    self.assertLess(stubs.index('#include "kernel.h"'), stubs.index('#include "ktask.h"'))
                    self.assertIn("UT_EXPECT_EQ_INT(kqueue_wait(", suite)
                elif check == "neighbour":
                    self.assertIn('#include "ring_util.h"', suite)
                elif check == "handle":
                    self.assertRegex(suite, r"CRC_Regs ut_p0_0 = \{0\};")
                    self.assertIn("left out: the function crashed", " ".join(result.open_items))
                else:
                    self.assertIn("namespace fw {", stubs)
                    self.assertIn("extern const fw::Evt * ut_stub_fw_post_e;".replace("fw::", ""), stubs)
                    self.assertIn("using fw::ut_stub_fw_post_e;", stubs)
                    self.assertIn("namespace fw { int dispatch(", suite)
                    self.assertIn("fw::Evt ut_v0 = {(std::uint16_t)(", suite)

    def test_a_brain_test_that_does_not_compile_costs_no_iteration(self):
        """A3-CL12: one generated test does not compile; the others are still run and measured in the same
        iteration, the broken test is removed from the bundle and never delivered."""
        tests = dict(CANNED_TESTS)
        tests["clamp_percent"] = [*CANNED_TESTS["clamp_percent"],
                                  {"name": "test_clamp_percent_two_arguments", "scenario_ids": [],
                                   "body": "UT_EXPECT_EQ_INT(clamp_percent(150, 1), 100);", "evidence_class": "E2"}]
        repo = copy_pilot("c_pilot", self.d)
        with writer_brain(UTBrain(tests=tests)):      # whole tests: a full profile [BP-2]
            result = self.run_pipeline(repo, "src/thermostat.c")
        self.assertEqual(result.final_state, "verified_measured", result.reason)
        suite = (repo / "tests/test_thermostat.c").read_text()
        self.assertNotIn("test_clamp_percent_two_arguments", suite)
        self.assertIn("UT_TEST(test_select_mode_nobody_home)", suite)          # the brain's other tests stay
        self.assertTrue(any("A3-CL12" in n and "clamp_percent" in n for n in result.open_items), result.open_items)
        self.assertFalse(any("did not link" in i.decision for i in result.iterations if i.stage == "verify"))

    def test_behaviour_round_after_the_targets_has_a_time_limit(self):
        """ORC-32: once every target is met and verified, the documented-behaviour round (A1-25y) runs only within
        its time; then the run finishes with the verified result instead of waiting for a slow brain."""
        repo = copy_pilot("c_pilot", self.d)
        original, slowed = up.Orchestrator._generate, []

        async def slow_after_targets(orch, s, plan, **kw):
            if s.targets_met_at is not None:
                slowed.append(time.monotonic())
                await asyncio.sleep(30)                       # a CPU brain writing tests
            return await original(orch, s, plan, **kw)

        with brain(UTBrain(tests={})), mock.patch.object(up.Orchestrator, "_generate", slow_after_targets):
            result = self.run_pipeline(repo, "src/thermostat.c", after_target_seconds=1)
        self.assertTrue(slowed, "the run never reached the documented-behaviour round")
        self.assertLess(time.monotonic() - slowed[0], 20)              # not the 30 s of the slow brain
        self.assertEqual(result.final_state, "verified_measured", result.reason)
        self.assertIn("ORC-32", result.reason)
        self.assertTrue(any("time limit" in i and "ORC-32" in i for i in result.open_items), result.open_items)
        self.assertTrue((repo / "tests/test_thermostat.c").is_file())                  # the verified tests

    def test_a_stopped_run_delivers_what_was_verified(self):
        """ORC-33: Ctrl+C (the task is cancelled) after a verified result: the verified tests, report and ledger
        are delivered; the state comes from that verified result and the reason names the stop."""
        repo = copy_pilot("c_pilot", self.d)
        original, calls = up.Orchestrator._learn, []

        async def stop_on_second(orch, s, result, bundle):
            calls.append(1)
            if len(calls) == 2:
                raise asyncio.CancelledError()               # what asyncio.run does on Ctrl+C
            return await original(orch, s, result, bundle)

        with brain(UTBrain(tests={})), mock.patch.object(up.Orchestrator, "_learn", stop_on_second):
            result = self.run_pipeline(repo, "src/thermostat.c")
        self.assertEqual(len(calls), 2)
        self.assertIn("stopped by the user", result.reason)
        self.assertIn("ORC-33", result.reason)
        self.assertEqual(result.final_state, "verified_measured", result.reason)
        self.assertTrue((repo / "tests/test_thermostat.c").is_file())
        self.assertTrue((Path(result.output_dir) / "report.md").is_file())
        self.assertTrue(up.Ledger.verify(Path(result.ledger)))

    @unittest.skipUnless(have_tool("cmake") and have_tool("ninja"), "cmake and ninja are not in the tool manifest")
    def test_dependency_pilot_reaches_every_target(self):
        """Phases 1 and 2 together: CMake build captured, dependencies stubbed, static function, struct and
        enum inputs, fuzzing for the rest; every metric at 100 % by measurement (ASIL D, COV-1, COV-3)."""
        repo = copy_pilot("c_deps_pilot", self.d)
        with brain(UTBrain(tests={}, dependency="stub")):
            result = self.run_pipeline(repo, "src/ctrl.c")
        print("\n" + up.report(result))
        self.assertEqual(result.final_state, "verified_measured", result.reason)          # COV-1
        self.assertEqual((result.asil, result.asil_source), ("D", "default"))              # COV-2
        for name in uc.METRICS:
            self.assertEqual(getattr(result.metrics, name).percent, 100.0, name)
        self.assertTrue(all(line.endswith("- met") for line in result.target_check), result.target_check)
        self.assertEqual(result.not_reached, [])
        self.assertTrue(result.build.startswith("cmake"))                                   # A3-BD10
        self.assertIn("mini (tested), mini_4ch (analysed only)", result.build)              # A3-BD14
        self.assertFalse(result.build_estimated)
        self.assertTrue(any(line.startswith("clamp: stub") for line in result.dependencies))   # A1-DA11
        self.assertIn("scale", {f.name for f in result.function_coverage})                  # A3-63
        delivered = repo / "tests" / "ut_sut_access_ctrl.c"
        self.assertTrue(delivered.is_file())                                                # A2-21k

    def new_pilot_reaches_every_target_without_a_brain(self, pilot: str, sut: str):
        """The ASIL D pilots: stub sequences, struct fields, SUT state, classes with a clock, std::array - every
        metric at 100 % by measurement, with rules and coverage-guided search only (COV-1, COV-6). One test per
        file, so test-full runs them in parallel (TP-20)."""
        if pilot == "c_deps_pilot" and not (have_tool("cmake") and have_tool("ninja")):
            self.skipTest("cmake and ninja are not in the tool manifest")
        d = self.d / Path(sut).stem
        d.mkdir()
        repo = copy_pilot(pilot, d)
        with brain(UTBrain()):
            result = run(up.Orchestrator(pipeline_config(d, fuzz_seconds=10)).run(
                uc.PipelineRequest(repo_root=str(repo), sut_file=sut)))
        self.assertEqual(result.final_state, "verified_measured", up.report(result))
        self.assertTrue(all(getattr(result.metrics, m).percent == 100.0 for m in uc.METRICS))
        self.assertEqual(result.failed, 0)

    def test_new_pilot_brake_ctrl(self):
        self.new_pilot_reaches_every_target_without_a_brain("c_deps_pilot", "src/brake_ctrl.c")

    def test_new_pilot_steer_assist(self):
        self.new_pilot_reaches_every_target_without_a_brain("c_deps_pilot", "src/steer_assist.c")

    def test_new_pilot_steering_monitor(self):
        self.new_pilot_reaches_every_target_without_a_brain("cpp_pilot", "src/steering_monitor.cpp")

    def test_new_pilot_torque_map(self):
        self.new_pilot_reaches_every_target_without_a_brain("cpp_pilot", "src/torque_map.cpp")

    def test_cpp_pilot_runs(self):
        repo = copy_pilot("cpp_pilot", self.d)
        with brain(UTBrain(tests={})):
            result = self.run_pipeline(repo, "src/rate_limiter.cpp")
        print("\n" + up.report(result))
        self.assertIn(result.final_state, ("verified_measured", "incomplete"))
        self.assertEqual(result.language, "cpp")
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.metrics.LC.percent, 100.0)         # deterministic class tests

    def test_existing_user_file_is_never_overwritten(self):
        repo = copy_pilot("c_pilot", self.d)
        (repo / "tests").mkdir()
        (repo / "tests/test_thermostat.c").write_text("/* the user's own test */\n")
        with brain(UTBrain()):
            result = self.run_pipeline(repo, "src/thermostat.c", coverage=cov(1))
        self.assertEqual((repo / "tests/test_thermostat.c").read_text(), "/* the user's own test */\n")
        self.assertTrue(any("not overwritten" in item for item in result.open_items))

    def test_edited_delivery_is_not_replaced(self):
        """[ORC-14] a delivered file is replaced on the next run only while it is unedited."""
        self.assertTrue(up.replaceable(None))
        stamped = up.stamp("int x;\n")
        self.assertTrue(up.replaceable(stamped.encode()))
        self.assertEqual(up.stamp(stamped), stamped)                               # no double stamp
        self.assertFalse(up.replaceable((stamped + "int y;\n").encode()))         # edited by the user
        self.assertFalse(up.replaceable(b"int x;\n"))                              # never ours


@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
@unittest.skipUnless(HAVE_FASTMCP and HAVE_LANGGRAPH, "fastmcp and langgraph are required")
@full_run
class StdioPipelineTests(unittest.TestCase):
    """The agents as real subprocesses, MCP over stdio, exactly as on your laptop [ARCH-7].
    No LLM: deterministic generation only."""
    def test_stdio_agents(self):
        with tmpdir() as d:
            d = Path(d)
            repo = copy_pilot("c_pilot", d)
            config = pipeline_config(d, transport="stdio", coverage=cov(2))
            raw = json.loads(config.read_text())
            raw["generator"]["mode"] = "deterministic"
            config.write_text(json.dumps(raw))
            # The agent processes have no brain (TP-21). As if the orchestrator had found one, the run goes over
            # stdio through discovery, capture, the probe and Stage A, and stops at the first plan [ARCH-24, ORC-34].
            with mock.patch.object(up.Orchestrator, "_brains_available", mock.AsyncMock(return_value="")):
                result = run(up.Orchestrator(config).run(uc.PipelineRequest(repo_root=str(repo),
                                                                            sut_file="src/thermostat.c")))
            print("\n" + up.report(result))
            self.assertGreaterEqual(len(result.iterations), 2, result.reason)          # probe and Stage A over stdio
            self.assertIn("ORC-34", result.reason)
            self.assertIn("NO_BRAIN_AVAILABLE", result.reason)
            self.assertTrue(up.Ledger.verify(Path(result.ledger)))
            logs = list((Path(result.output_dir) / "logs").glob("*.log"))
            self.assertEqual(len(logs), 5)                                          # five agent processes
            stopped = run(up.Orchestrator(config).run(uc.PipelineRequest(repo_root=str(repo), sut_file="src/thermostat.c")))
            self.assertIn("no brain is available, so the run was not started", stopped.reason)     # ARCH-24
            self.assertEqual(stopped.iterations, [])


# ============================================================================ 8b. the brain where it adds quality
def measured(layout, gaps, decision="gaps_remain", tests=()):
    """A verification result as Agent3 returns it after the first coverage bundle (stage verify)."""
    functions = {}
    for g in gaps:
        functions.setdefault(g.function, []).append(g)
    return uc.VerificationResult(
        run_id="r", stage="verify", sut_sha256=layout.sut_sha256, suite_sha256="", build_ok=True, link_ok=True,
        run_ok=True, decision=decision, tests=list(tests),
        coverage=uc.CoverageReport(toolchain="t", definitions="d", functions=[
            uc.FunctionCoverage(name=name, gaps=items) for name, items in functions.items()]))


# The answers of the remote Qwen3 8B (qwen3-8b, thinking off, JSON schema) to Agent1's plan requests for the C pilot,
# recorded on the user's PC on 2026-10-23 - every scenario but one refused by the check before ARCH-20's format fixes.
REMOTE_QWEN_ANSWERS = json.loads('[["select_mode", {"scenarios": [{"gap_ids": ["BC:select_mode:29:10:T"], "name": "Window open or unoccupied and no frost", "purpose": "Ensure the function returns MODE_ECO when the window is open or the house is unoccupied and frost protection is not active.", "args": [{"name": "window_open", "value": ":1"}, {"name": "occupied", "value": ":0"}, {"name": "frost", "value": ":0"}], "setup": [], "stubs": [], "variables": [], "expected_return": ":1", "expected_after": [], "evidence_ids": ["EV-select_mode-1", "EV-select_mode-2"], "quotes": [{"evidence_id": "EV-select_mode-1", "text": "REQ-TH-7: ECO when the window is open or nobody is home, unless frost protection is active."}, {"evidence_id": "EV-select_mode-2", "text": "REQ-TH-7: ECO when the window is open or nobody is home, unless frost protection is active."}], "classification": "confirmed_gap", "conflict": "", "information_sought": ""}, {"gap_ids": ["LC:select_mode:30"], "name": "Window closed and occupied with frost", "purpose": "Ensure the function returns MODE_COMFORT when the window is closed, the house is occupied, and frost protection is active.", "args": [{"name": "window_open", "value": ":0"}, {"name": "occupied", "value": ":1"}, {"name": "frost", "value": ":1"}], "setup": [], "stubs": [], "variables": [], "expected_return": ":2", "expected_after": [], "evidence_ids": [], "quotes": [], "classification": "characterization_gap", "conflict": "", "information_sought": ""}]}], ["select_mode", {"scenarios": [{"gap_ids": ["BC:select_mode:29:10:T"], "name": "Ensure the function returns MODE_ECO when window is open or nobody is home, and frost protection is not active", "purpose": "Ensure the function returns MODE_ECO when the condition (window_open || !occupied) is true and frost is not active.", "args": [{"name": "window_open", "value": ":1"}, {"name": "occupied", "value": ":0"}, {"name": "frost", "value": ":0"}], "setup": [], "stubs": [], "variables": [], "expected_return": ":1", "expected_after": [], "evidence_ids": ["EV-select_mode-1"], "quotes": [{"evidence_id": "EV-select_mode-1", "text": "REQ-TH-7: ECO when the window is open or nobody is home, unless frost protection is active."}], "classification": "confirmed_gap", "conflict": "", "information_sought": ""}, {"gap_ids": ["LC:select_mode:30"], "name": "Ensure the function returns MODE_COMFORT when the condition is not met", "purpose": "Ensure the function returns MODE_COMFORT when the condition (window_open || !occupied) is false or frost is active.", "args": [{"name": "window_open", "value": ":0"}, {"name": "occupied", "value": ":1"}, {"name": "frost", "value": ":1"}], "setup": [], "stubs": [], "variables": [], "expected_return": ":2", "expected_after": [], "evidence_ids": ["EV-select_mode-2"], "quotes": [{"evidence_id": "EV-select_mode-2", "text": "REQ-TH-7: ECO when the window is open or nobody is home, unless frost protection is active."}], "classification": "confirmed_gap", "conflict": "", "information_sought": ""}]}], ["thermostat_command", {"scenarios": [{"gap_ids": ["EV-thermostat_command-1"], "name": "Test thermostat_command when sensor returns error", "purpose": "Check the behaviour when the sensor returns an error.", "args": [{"name": "setpoint", "value": "20"}, {"name": "hysteresis", "value": "5"}], "setup": ["int t = SENSOR_ERROR;"], "stubs": [{"stub": "read_sensor", "returns": ["SENSOR_ERROR"]}], "variables": [], "expected_return": "-1", "expected_after": [], "evidence_ids": ["EV-thermostat_command-1"], "quotes": [{"evidence_id": "EV-thermostat_command-1", "text": "Heater command for the current room temperature. @param setpoint target temperature in degrees Celsius @param hysteresis allowed band below the setpoint, must be >= 0 @return 1 to heat, 0 to stay off, -1 when the sensor reports an error"}], "classification": "confirmed_gap", "conflict": "", "information_sought": ""}]}], ["thermostat_command", {"scenarios": [{"gap_ids": ["EV-thermostat_command-1"], "name": "Check the behaviour when the sensor returns an error", "purpose": "Verify that the function returns -1 when the sensor reports an error.", "args": [{"name": "setpoint", "value": "20"}, {"name": "hysteresis", "value": "5"}], "setup": ["int t = SENSOR_ERROR;"], "stubs": [{"stub": "read_sensor", "returns": ["SENSOR_ERROR"]}], "variables": [], "expected_return": "-1", "expected_after": [], "evidence_ids": ["EV-thermostat_command-1"], "quotes": [{"evidence_id": "EV-thermostat_command-1", "text": "Heater command for the current room temperature. @return 1 to heat, 0 to stay off, -1 when the sensor reports an error"}], "classification": "confirmed_gap", "conflict": "", "information_sought": ""}]}], ["clamp_percent", {"scenarios": [{"gap_ids": ["EVIDENCE:clamp_percent: check the behaviour the EVIDENCE states (results, error values, limits) with asserted expected values"], "name": "Test clamp_percent with value less than 0", "purpose": "Verify that the function returns 0 when the input value is less than 0.", "args": [{"name": "value", "value": "-5"}], "setup": [], "stubs": [], "variables": [], "expected_return": "0", "expected_after": [], "evidence_ids": ["EV-clamp_percent-1"], "quotes": [], "classification": "confirmed_gap", "conflict": "", "information_sought": ""}]}], ["clamp_percent", {"scenarios": [{"gap_ids": ["EVIDENCE:clamp_percent"], "name": "Verify that the function returns 0 when value is negative", "purpose": "Check the behavior when the input value is negative, as specified in the evidence.", "args": [{"name": "value", "value": "-5"}], "setup": [], "stubs": [], "variables": [], "expected_return": "0", "expected_after": [], "evidence_ids": ["EV-clamp_percent-1"], "quotes": [{"evidence_id": "EV-clamp_percent-1", "text": "@return value limited to the range 0..100"}], "classification": "confirmed_gap", "conflict": "", "information_sought": ""}]}]]')


class BrainPlanningTests(unittest.TestCase):
    """A1-PL1 .. A1-PL14: the brain suggests, Agent1 checks every scenario deterministically."""
    def setUp(self):
        self.tmp = tmpdir()
        self.repo = copy_pilot("c_pilot", Path(self.tmp.name))
        self.agent = a1.AnalyzerAgent()
        self.layout = self.agent.discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/thermostat.c"))
        self.gaps = [uc.GapItem(gap_id="BC:thermostat_command:7:9:T", function="thermostat_command", metric="BC",
                                line=7, column=9, detail="(t == SENSOR_ERROR) never true"),
                     uc.GapItem(gap_id="LC:thermostat_command:11", function="thermostat_command", metric="LC", line=11)]
        ev = self.agent.collect_evidence(self.layout, "thermostat_command").evidence
        self.e2 = next(e for e in ev if e.evidence_class == "E2")

    def tearDown(self):
        self.tmp.cleanup()

    def good(self, **kw):
        base = dict(gap_ids=["BC:thermostat_command:7:9:T"], name="test_thermostat_command_sensor_error",
                    purpose="a sensor error stops the heater",
                    args=[{"name": "setpoint", "value": "20"}, {"name": "hysteresis", "value": "2"}],
                    stubs=[{"stub": "read_sensor", "returns": ["SENSOR_ERROR"]}], expected_return="-1",
                    evidence_ids=[self.e2.evidence_id],
                    quotes=[{"evidence_id": self.e2.evidence_id, "text": "-1 when the sensor reports an error"}],
                    classification="confirmed_gap")
        base.update(kw)
        return planned(**base)

    def test_functions_planned_at_the_same_time_give_the_same_plan(self):
        """ARCH-18: two functions are planned at once and the slower one answers last; the plan, the brain
        recorded per scenario and the planning notes are the same as with one call at a time."""
        class ParallelBrain(UTBrain):
            def __init__(self, parallel, delays, **kw):
                super().__init__(**kw)
                self.parallel_calls, self.delays, self.running, self.peak = parallel, delays, 0, 0

            async def generate(self, task, context, schema, validate):
                fn = next((name for name in self.delays if f"function {name} " in task), "")
                self.running += 1
                self.peak = max(self.peak, self.running)
                try:
                    await asyncio.sleep(self.delays.get(fn, 0))
                    return await super().generate(task, context, schema, validate)
                finally:
                    self.running -= 1

        clamp = planned(gap_ids=["LC:clamp_percent:19"], name="test_clamp_percent_negative", purpose="below 0",
                        args=[{"name": "value", "value": "-5"}])
        gaps = self.gaps + [uc.GapItem(gap_id="LC:clamp_percent:19", function="clamp_percent", metric="LC", line=19)]
        plans = {"thermostat_command": [self.good()], "clamp_percent": [clamp]}
        seen = []
        for parallel, delays in ((1, {}), (2, {"thermostat_command": 0.3, "clamp_percent": 0.0})):
            b = ParallelBrain(parallel, delays, plans=plans)
            with brain(b):
                plan = run(a1.AnalyzerAgent().plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, gaps),
                                                                  iteration=2)))
            seen.append(([(s.scenario_id, s.function, s.made_by, s.gap_ids) for s in plan.scenarios], plan.planning))
            if parallel > 1:
                self.assertEqual(b.peak, 2)                                          # really at the same time
        self.assertEqual(seen[0], seen[1])
        self.assertTrue(any(s[2].startswith("brain:qwen") for s in seen[1][0]), seen[1])

    def test_brain_scenarios_are_checked_before_agent2(self):
        """A1-PL4 on three answers: the planned number of scenarios each (ARCH-11a, ARCH-20c), every bad one
        refused with its reason, a quote that is not word for word left out (characterization)."""
        bad_range = self.good(name="test_thermostat_command_huge", gap_ids=["LC:thermostat_command:11"], args=[
            {"name": "setpoint", "value": "99999999999"}, {"name": "hysteresis", "value": "2"}])
        bad_stub = self.good(name="test_thermostat_command_display", stubs=[{"stub": "write_display", "returns": ["1"]}])
        bad_quote = self.good(name="test_thermostat_command_quote", gap_ids=["LC:thermostat_command:11"], quotes=[
            {"evidence_id": self.e2.evidence_id, "text": "returns 42 on Mondays"}])
        answers = [[self.good(), bad_stub], [bad_range], [bad_quote]]
        b = UTBrain(plans={"thermostat_command": lambda context: answers.pop(0) if answers else []})
        self.agent.cfg.max_brain_calls_per_function = 3
        last = measured(self.layout, self.gaps)
        with brain(b):
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=last, iteration=2)))
        mine = [s for s in plan.scenarios if s.function == "thermostat_command"]
        self.assertEqual(len(mine), 2, plan.planning)                                   # A1-PL4: two dropped
        quoted = next(s for s in mine if s.test_name == "test_thermostat_command_quote")     # A1-PL4: bad quote left out
        self.assertTrue(quoted.characterization and not quoted.expected_return)          # -> characterization only
        s = next(s for s in mine if s.test_name == "test_thermostat_command_sensor_error")
        self.assertTrue(s.made_by.startswith("brain:qwen") and s.checked)              # A1-PL10
        self.assertFalse(s.characterization)                                            # A1-PL4 c): normative
        self.assertEqual((s.expected_return, s.strongest_class), ("-1", "E2"))
        self.assertEqual(s.stub_values[0].returns, ["SENSOR_ERROR"])
        notes = " ".join(plan.planning)
        self.assertIn("outside the range of int", notes)                               # A1-PL4 e)
        self.assertIn("write_display is not a stubbed function", notes)                 # A1-PL4 d)
        self.assertIn("word for word", notes)                                           # A1-PL4 b)
        self.assertEqual(len([t for t in b.generate_calls if "function thermostat_command " in t]), 3)   # A1-PL9
        self.assertIn("at most 2 scenarios", b.generate_calls[0])                        # two targets [A1-PL3a]

    def test_without_a_quote_a_scenario_only_characterizes(self):
        b = UTBrain(plans={"thermostat_command": [self.good(quotes=[])]})
        with brain(b):
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps),
                                                      iteration=2)))
        s = next(s for s in plan.scenarios if s.function == "thermostat_command")
        self.assertTrue(s.characterization)                                             # A1-25h, A1-PL4 c)
        self.assertEqual(s.expected_return, "")

    def test_first_plan_by_the_brain_and_without_a_brain_the_planning_stops(self):
        """A1-PL1: before anything is measured the rules estimate the obligations; from the first coverage plan
        on the brain plans. A1-PL8, ARCH-24: no brain answers -> NO_BRAIN_AVAILABLE, never a rule plan instead."""
        with brain(UTBrain(plans={"thermostat_command": [self.good()]})) as b:
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout)))          # nothing measured yet
        self.assertFalse(b.generate_calls)
        self.assertTrue(all(s.made_by == "rule" for s in plan.scenarios))
        with brain(UTBrain(plans={"thermostat_command": [self.good()]})) as b:      # A1-PL1: cheap tests first
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps),
                                                      iteration=1)))                   # no test measured yet
        self.assertFalse(b.generate_calls)
        self.assertTrue(any(p.startswith("cheap tests first") for p in plan.planning), plan.planning)
        tested = measured(self.layout, self.gaps, tests=[uc.TestResult(name="test_x_auto_1", status="passed")])
        with brain(UTBrain(plans={"thermostat_command": [self.good()]})) as b:
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=tested, iteration=1)))   # tests exist
        mine = [s for s in plan.scenarios if s.function == "thermostat_command" and not s.inputs]
        self.assertTrue(mine and all(s.made_by.startswith("brain:") for s in mine), plan.scenarios)
        with brain(UTBrain(plans={"thermostat_command": [self.good()]})) as b:
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps),
                                                      iteration=2)))                   # after the cheap tests
        mine = [s for s in plan.scenarios if s.function == "thermostat_command" and not s.inputs]
        self.assertTrue(mine and all(s.made_by.startswith("brain:") for s in mine), plan.scenarios)
        with brain(DownBrain()):                                                       # a new agent, a new router
            with self.assertRaises(ba.AgentFault) as caught:
                run(a1.AnalyzerAgent().plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps),
                                                           iteration=2)))
        self.assertEqual(caught.exception.code, "NO_BRAIN_AVAILABLE")
        self.assertIn("ARCH-24", caught.exception.message)

    def test_a_function_without_a_usable_scenario_does_not_stop_the_others(self):
        """A1-PL8: the function gets no scenario this time (never a rule plan); the other functions are planned."""
        clamp = planned(gap_ids=["LC:clamp_percent:19"], name="test_clamp_percent_negative", purpose="below 0",
                        args=[{"name": "value", "value": "-5"}])
        gaps = self.gaps + [uc.GapItem(gap_id="LC:clamp_percent:19", function="clamp_percent", metric="LC", line=19)]
        with brain(UTBrain(plans={"thermostat_command": [], "clamp_percent": [clamp]})):
            plan = run(a1.AnalyzerAgent().plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, gaps),
                                                              iteration=2)))
        planned_fns = {s.function for s in plan.scenarios if not s.inputs}
        self.assertIn("clamp_percent", planned_fns)
        self.assertNotIn("thermostat_command", planned_fns)                                # no rule plan instead
        self.assertTrue(all(s.made_by.startswith("brain:qwen") for s in plan.scenarios if s.function == "clamp_percent"))
        self.assertTrue(any(p.startswith("thermostat_command: no scenario this time") for p in plan.planning),
                        plan.planning)

    def test_the_next_brain_is_another_model_not_the_same_model_on_another_server(self):
        """A1-PL8, ARCH-19: the remote Qwen's scenarios failed the check - the Qwen in Ollama (the same model, on a
        slower computer) is not asked; a brain of another model would be."""
        remote, local = UTBrain(name="qwen_remote", plans={"thermostat_command": []}), UTBrain(name="qwen")
        remote.family = local.family = "qwen3-8b"
        with mock.patch.object(ba, "build_router", lambda settings, injected=None: ba.BrainRouter([remote, local], 0.0)):
            plan = run(a1.AnalyzerAgent().plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps),
                                                              iteration=2)))
        self.assertFalse([c for c in local.generate_calls if "function thermostat_command " in c])
        self.assertTrue(any(p.startswith("thermostat_command: no scenario this time") for p in plan.planning))
        other = UTBrain(name="haiku", plans={"thermostat_command": [self.good()]})
        remote2 = UTBrain(name="qwen_remote", plans={"thermostat_command": []})
        remote2.family = "qwen3-8b"
        with mock.patch.object(ba, "build_router", lambda settings, injected=None: ba.BrainRouter([remote2, other], 0.0)):
            plan = run(a1.AnalyzerAgent().plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps),
                                                              iteration=2)))
        self.assertTrue(any(s.made_by.startswith("brain:haiku") for s in plan.scenarios
                            if s.function == "thermostat_command"), plan.planning)

    def test_when_no_function_gets_a_scenario_the_run_goes_on(self):
        """A1-PL8, ORC-34: no function got a scenario that passed the check - no rule plan in the brain's place,
        but no stop either: the gaps are left to the rule-based tests and the search; the failures are recorded."""
        gaps = [uc.GapItem(gap_id=f"LC:{fn}:{line}", function=fn, metric="LC", line=line)
                for fn, line in (("thermostat_command", 11), ("clamp_percent", 19), ("select_mode", 30))]
        with brain(UTBrain(plans={"thermostat_command": [], "clamp_percent": [], "select_mode": []})):
            plan = run(a1.AnalyzerAgent().plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, gaps),
                                                              iteration=2)))
        self.assertFalse([s for s in plan.scenarios if not s.inputs and s.made_by != "rule" and
                          not s.scenario_id.startswith("S-evidence-")])
        for fn in ("thermostat_command", "clamp_percent", "select_mode"):
            self.assertTrue(any(p.startswith(f"{fn}: no scenario this time") for p in plan.planning), plan.planning)

    def test_the_remote_qwen_answers_of_the_first_pc_run_are_accepted(self):
        """ARCH-20, A1-PL4: the six answers the remote Qwen gave on the user's PC. The content was right; a stray
        ':' before every value, an evidence id or a copied OPEN GAPS line as the gap id made the check refuse
        them. Now the form is fixed and recorded, and the unchanged checks accept what is right."""
        gaps = [uc.GapItem(gap_id="BC:select_mode:29:10:T", function="select_mode", metric="BC", line=29, column=10,
                           detail="(window_open || !occupied) never true"),
                uc.GapItem(gap_id="LC:select_mode:30", function="select_mode", metric="LC", line=30)]
        answers: dict[str, list] = {}
        for fn, answer in REMOTE_QWEN_ANSWERS:
            answers.setdefault(fn, []).append(answer["scenarios"])
        replies = {fn: (lambda queue: (lambda ctx: queue.pop(0) if queue else []))(list(seq))
                   for fn, seq in answers.items()}
        with brain(UTBrain(plans=replies)):
            plan = run(a1.AnalyzerAgent().plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, gaps),
                                                              iteration=2)))                # after the cheap tests
        by_fn = {}
        for s in plan.scenarios:
            if s.made_by.startswith("brain:"):
                by_fn.setdefault(s.function, []).append(s)
        modes = [{a.name: a.value for a in s.args} for s in by_fn.get("select_mode", [])]
        self.assertIn({"window_open": "1", "occupied": "0", "frost": "0"}, modes, plan.planning)
        self.assertIn({"window_open": "0", "occupied": "1", "frost": "1"}, modes)
        self.assertTrue(all(g in {"BC:select_mode:29:10:T", "LC:select_mode:30"}
                            for s in by_fn["select_mode"] for g in s.gap_ids))
        thermo = by_fn.get("thermostat_command", [])
        self.assertTrue(thermo and thermo[0].gap_ids == ["EVIDENCE:thermostat_command"], plan.planning)
        self.assertEqual((thermo[0].expected_return, thermo[0].stub_values[0].returns), ("-1", ["SENSOR_ERROR"]))
        self.assertFalse(thermo[0].characterization)                       # normative: quoted E2 evidence
        self.assertTrue(by_fn.get("clamp_percent"), plan.planning)
        notes = " ".join(plan.planning)
        self.assertIn("args.window_open: ':1' -> '1'", notes)              # every fix recorded [ARCH-20]
        self.assertIn("-> 'EVIDENCE:thermostat_command'", notes)

    def test_format_fixes_never_change_content(self):
        """ARCH-20: only noise is removed; what is left is checked by the unchanged strict checks."""
        cases = {":1": "1", " = 0": "0", "window_open: 1": "1", "window_open = MODE_ECO": "MODE_ECO",
                 "`MODE_ECO`": "MODE_ECO", '"1"': "1", "1;": "1", "1, ": "1", "1 /* open */": "1",
                 "-5 // below": "-5", "::Mode::ECO": "::Mode::ECO", "/dev/null": "/dev/null", "{setpoint}": "{setpoint}",
                 "'a'": "'a'", '"abc def"': '"abc def"'}
        for raw, expected in cases.items():
            self.assertEqual(a1.fix_value(raw, "window_open"), expected, raw)
        self.assertEqual(a1.fix_value('"OK"', "name", keep_quotes=True), '"OK"')     # a string parameter
        open_ids = {"BC:select_mode:29:10:T", "LC:select_mode:30"}
        self.assertEqual(a1.fix_gap_id("LC:select_mode:30 [LC] line 30: `return MODE_ECO;`", open_ids, "select_mode",
                                       set()), "LC:select_mode:30")
        self.assertEqual(a1.fix_gap_id("LC:select_mode:3", open_ids, "select_mode", set()), "LC:select_mode:3")
        self.assertEqual(a1.fix_gap_id("EV-f-1", {"EVIDENCE:f"}, "f", {"EV-f-1"}), "EVIDENCE:f")
        self.assertEqual(a1.fix_gap_id("EV-f-1", {"EVIDENCE:f", "LC:f:3"}, "f", {"EV-f-1"}), "EV-f-1")  # ambiguous
        constants = {"MODE_ECO": 1}
        for value in ("!0", "1.0", "(int)1"):                                # valid C, accepted [A1-PL4]
            self.assertIsNotNone(a1.eval_int(value, constants) if value != "1.0" else 1.0, value)
        fn = next(f for f in self.layout.functions if f.name == "select_mode")
        p = a1.text_params(fn)[0]
        for value, ok in (("!0", True), ("1.0", True), ("/dev/null", False), ("{setpoint}", False), ("1.5", False)):
            problem = self.agent._value_problem(p, value, fn, constants, set(), set(), self.layout)
            self.assertEqual(problem == "", ok, (value, problem))

    def test_the_request_shows_suggested_values_an_example_and_the_history(self):
        """A1-PL2, A1-PL3, A1-PL8, A2-21f: what the brain sees."""
        gaps = [uc.GapItem(gap_id="LC:select_mode:30", function="select_mode", metric="LC", line=30)]
        seen, tasks = [], []
        b = UTBrain(plans={"select_mode": lambda ctx: seen.append(ctx) or []})
        req = uc.PlanRequest(layout=self.layout, last=measured(self.layout, gaps), iteration=3,
                             refused={"select_mode": ["select_mode: no scenario this time - window_open: ':1' is not an "
                                                      "integer value"]},
                             crashed_calls=["select_mode(INT_MIN, 0, 0)", "clamp_percent(5)"])
        with brain(b):
            run(a1.AnalyzerAgent().plan(req))
        context = seen[0]
        self.assertIn("- window_open (type int, int) - suggested values: 0, 1", context)
        self.assertNotIn("- window_open: int", context)                       # the form Qwen copied into values
        self.assertIn("REFUSED earlier: select_mode: no scenario this time - window_open: ':1'", context)
        self.assertIn("CRASHED or did not end: select_mode(INT_MIN, 0, 0)", context)
        self.assertNotIn("clamp_percent(5)", context)
        task = next(t for t in b.generate_calls if "function select_mode " in t)
        self.assertIn('"args": [{"name": "a", "value": "1"}', task)          # the example scenario [A1-PL3]
        self.assertIn("never a ':' or '=' before a value", task)
        seen.clear()                                                   # a refused answer: the re-ask shows the form
        bad = planned(gap_ids=["LC:select_mode:30"], name="test_select_mode_x", purpose="x",
                      args=[{"name": "window_open", "value": "/dev/null"}, {"name": "occupied", "value": "0"},
                            {"name": "frost", "value": "0"}])
        with brain(UTBrain(plans={"select_mode": lambda ctx: seen.append(ctx) or [bad]})):
            run(a1.AnalyzerAgent().plan(req))
        self.assertIn("window_open: '/dev/null' is not an integer value", seen[1])
        self.assertIn('Expected form: every value exactly as C code, for example {"name": "window_open", "value": "1"}',
                      seen[1])

    def test_a_scenario_whose_test_failed_is_not_accepted_again(self):
        """A1-PL8: the brain sees the failed test among the facts; the same scenario again is refused by name."""
        contexts = []

        def again(context):
            contexts.append(context)
            return [self.good()]
        req = uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps), iteration=3,
                             failed_tests={"test_thermostat_command_sensor_error": "crashed: signal 11 (SIGSEGV)"})
        with brain(UTBrain(plans={"thermostat_command": again})):
            plan = run(a1.AnalyzerAgent().plan(req))
        self.assertFalse([s for s in plan.scenarios if s.function == "thermostat_command" and not s.inputs])
        self.assertTrue(any(p.startswith("thermostat_command: no scenario this time") and "crashed: signal 11" in p
                            for p in plan.planning), plan.planning)
        self.assertIn("test_thermostat_command_sensor_error: FAILED in this run (crashed: signal 11 (SIGSEGV))",
                      contexts[0])
        self.assertEqual(len(contexts), 1)        # the answer repeated what failed: not asked again [A1-PL18]

    def test_unchanged_facts_are_not_asked_again_after_nothing_was_accepted(self):
        """A1-PL18: the facts are the code, its open targets and the evidence. A round whose brain call gave no
        accepted scenario is not asked again while they stay the same - also when only the record of the earlier
        answers (failed tests, journal, tried) changed; changed targets are asked again."""
        calls = []

        def nothing(context):
            calls.append(context)
            return []
        agent = a1.AnalyzerAgent()
        with brain(UTBrain(plans={"thermostat_command": nothing})):
            run(agent.plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps), iteration=2)))
            first = len(calls)
            plan = run(agent.plan(uc.PlanRequest(
                layout=self.layout, last=measured(self.layout, self.gaps), iteration=3,
                failed_tests={"test_thermostat_command_x": "failed"})))                  # only the record changed
            self.assertEqual(len(calls), first, "asked again with the same facts")
            self.assertTrue(any("facts unchanged since its last brain call" in p for p in plan.planning), plan.planning)
            run(agent.plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps[:1]), iteration=4)))
        self.assertGreater(len(calls), first, "changed targets are asked again")

    def test_without_a_next_brain_the_same_brain_is_asked_again(self):
        """COV-6: the next AVAILABLE brain; when the brain that planned last is the only one, it is asked again
        with the facts of what was tried, and the run goes on."""
        with brain(UTBrain(plans={"thermostat_command": [self.good()]})) as b:
            plan = run(a1.AnalyzerAgent().plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps),
                                                              iteration=4, avoid_brains=["qwen"])))
        self.assertTrue(any(s.made_by.startswith("brain:qwen") for s in plan.scenarios), plan.planning)
        self.assertTrue(b.generate_calls)

    def test_hard_gap_shows_what_was_tried(self):
        suite = ('/* evidence: E8 */\nUT_TEST(test_thermostat_command_auto_1)\n{\n    ut_stub_read_sensor_return = 5;\n'
                 '    UT_EXPECT_EQ_INT(thermostat_command(1, 0), 0);\n}\n')
        req = uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps, tests=[
            uc.TestResult(name="test_thermostat_command_auto_1", status="passed")]), iteration=3,
            attempts={"BC:thermostat_command:7:9:T": 1}, existing_suite=suite,
            test_items={"test_thermostat_command_auto_1": ["LC:6", "LC:13", "BC:7:9:F"]})
        contexts = []
        with brain(UTBrain(plans={"thermostat_command": lambda ctx: contexts.append(ctx) or []})):
            plan = run(self.agent.plan(req))
        self.assertFalse([s for s in plan.scenarios if s.function == "thermostat_command" and not s.inputs])
        self.assertTrue(any(p.startswith("thermostat_command: no scenario this time") for p in plan.planning))  # A1-PL8
        self.assertIn("TRIED 1 time(s)", contexts[0])                    # A1-PL12; a second attempt [LA-5]
        self.assertIn("reached: LC:6, LC:13, BC:7:9:F", contexts[0])                    # A1-PL7
        self.assertIn("PATH CONDITIONS", contexts[0])

    def test_requirement_written_in_prose_is_linked_and_checked(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs/requirements.md").write_text(
            "# Requirements\n\n- REQ-TH-9: The heater shall stay off whenever the sensor fails.\n")
        layout = self.agent.discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/thermostat.c"))
        links = [{"requirement_id": "REQ-TH-9", "functions": ["thermostat_command"],
                  "quote": "The heater shall stay off whenever the sensor fails", "probability": 0.8},
                 {"requirement_id": "REQ-TH-9", "functions": ["invented_function"], "quote": "The heater shall",
                  "probability": 0.9},
                 {"requirement_id": "REQ-TH-9", "functions": ["clamp_percent"], "quote": "never written there",
                  "probability": 0.9},
                 {"requirement_id": "REQ-XX-1", "functions": ["select_mode"], "quote": "x", "probability": 0.9}]
        with brain(UTBrain(links=links)):
            layout.requirement_links = run(self.agent._link_requirements(layout))
        self.assertEqual([(l.requirement_id, l.function) for l in layout.requirement_links],
                         [("REQ-TH-9", "thermostat_command")])                             # A1-PL13
        ev = self.agent.collect_evidence(layout, "thermostat_command").evidence
        linked = [e for e in ev if e.linked_by]
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0].evidence_class, "E1")
        self.assertTrue(linked[0].linked_by.startswith("brain p=0.80"))
        self.assertIn("A4-23f", a4.CorrectorAgent().evidence_problem(linked + [linked[0].model_copy(update={
            "line": 99, "evidence_id": "x"})]))                                          # A4-23i

    def test_failing_linked_test_disputes_the_link(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs/requirements.md").write_text("- REQ-TH-9: The heater shall stay off when the sensor fails.\n")
        layout = self.agent.discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/thermostat.c"))
        layout.requirement_links = [uc.RequirementLink(requirement_id="REQ-TH-9", function="thermostat_command",
                                                       source="docs/requirements.md", line=1, probability=0.8,
                                                       quote="The heater shall stay off when the sensor fails")]
        suite = ('/* evidence: E1 linked */\nUT_TEST(test_thermostat_command_heater_off)\n{\n'
                 '    /* S-brain-x: off [EV-thermostat_command-9 @docs/requirements.md:1] */\n'
                 '    UT_EXPECT_EQ_INT(thermostat_command(20, 2), 0);\n}\n')
        failure = uc.Failure(failure_id="test-test_thermostat_command_heater_off", kind="test",
                             classification="requirement_mismatch", route="agent1",
                             affected_test="test_thermostat_command_heater_off", affected_function="thermostat_command")
        last = measured(layout, self.gaps, decision="failures").model_copy(update={"failures": [failure]})
        with brain(UTBrain()):
            plan = run(self.agent.plan(uc.PlanRequest(layout=layout, last=last, existing_suite=suite, iteration=2)))
        self.assertEqual(plan.disputed_evidence, ["docs/requirements.md:1"])            # A1-PL13
        self.assertEqual(plan.repairs[0].action, "drop")
        self.assertTrue(any("REQ-TH-9" in i and "review" in i for i in plan.open_items))
        later = run(self.agent.plan(uc.PlanRequest(layout=layout, last=measured(layout, [], decision="complete"),
                                                   disputed_evidence=plan.disputed_evidence)))
        for s in later.scenarios:
            self.assertFalse(any(e.linked_by for e in s.evidence))                       # not planned again

    def test_missing_declaration_is_proposed_and_checked(self):
        (self.repo / "src/drv.c").write_text("int drv_set(int x)\n{\n    return hal_write(3, x) == 0;\n}\n")
        layout = self.agent.discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/drv.c"))
        with brain(UTBrain(declarations={"hal_write": "int hal_write(int reg, int value);",
                                         "hal_other": "static int hal_other(void) { return 1; }"})):
            deps, items = run(self.agent._missing_declarations(layout, ["hal_write", "hal_other", "read_sensor"]))
        by = {d.name: d for d in deps}
        self.assertTrue(by["hal_write"].decided_by.startswith("brain declaration"))      # A1-PL14
        self.assertEqual(by["hal_write"].params, [["int", "reg"], ["int", "value"]])
        self.assertEqual(by["read_sensor"].decided_by, "rule")                            # from the headers first
        self.assertTrue(any("hal_other" in i for i in items))                             # a definition: refused
        self.assertIsNotNone(a1.stub_spec(by["hal_write"]))


    def test_a_declaration_in_a_namespace(self):                                            # A1-PL14
        """The linker names hal::pwm: the header's declaration inside namespace hal is the stub, no brain; a brain's
        declaration with a type nobody declares is refused; the namespace is never part of the return type."""
        self.assertEqual(a1.parse_declaration("std::uint16_t hal::pwm(hal::pwm_channel_t, std::uint16_t);"),
                         ("std::uint16_t", [["hal::pwm_channel_t", "arg0"], ["std::uint16_t", "arg1"]]))
        self.assertEqual(a1.unknown_types(["std::uint16_t", "hal::pwm_channel_t", "const Foo &"],
                                          ["typedef struct Foo_s Foo;", "namespace hal { void pwm(int); }"]),
                         ["hal::pwm_channel_t"])
        (self.repo / "include/hal.hpp").write_text("#include <cstdint>\nnamespace hal {\n"
                                                   "void pwm(std::uint8_t channel, std::uint16_t duty);\n}\n")
        (self.repo / "src/fan.cpp").write_text('#include "hal.hpp"\nint fan(int d)\n{\n    hal::pwm(1, d);\n'
                                              "    hal::led(d);\n    return d;\n}\n")
        layout = self.agent.discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/fan.cpp"))
        with brain(UTBrain(declarations={"hal::led": "std::uint16_t hal::led(hal::led_t, std::uint16_t);"})):
            deps, items = run(self.agent._missing_declarations(layout, ["hal::pwm", "hal::led"]))
        self.assertEqual([(d.name, d.qualified_name, d.decided_by, d.return_type) for d in deps],
                         [("pwm", "hal::pwm", "rule", "void")])
        self.assertTrue(any("hal::led" in i and "hal::led_t" in i for i in items), items)
        asked = UTBrain(declarations={"hal::led": "void hal::led(int);"})
        with brain(asked):                                       # a declaration that did not build: not asked again
            deps, items = run(self.agent._missing_declarations(layout, ["hal::led"], {"hal::led"}))
        self.assertEqual((deps, asked.generate_calls), ([], []))
        self.assertTrue(any("a declaration in the repository is needed" in i for i in items), items)


class BrainGenerationTests(unittest.TestCase):
    """A2-21g sequences, A2-21m assembly, A2-21n..q."""
    def setUp(self):
        self.tmp = tmpdir()
        repo = copy_pilot("c_pilot", Path(self.tmp.name))
        analyzer = a1.AnalyzerAgent()
        self.layout = analyzer.discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/thermostat.c"))
        self.plan = run(analyzer.plan(uc.PlanRequest(layout=self.layout)))
        self.e2 = next(e for e in analyzer.collect_evidence(self.layout, "thermostat_command").evidence
                       if e.evidence_class == "E2")

    def tearDown(self):
        self.tmp.cleanup()

    def scenario(self, **kw):
        base = dict(scenario_id="S-brain-thermostat_command-2-1", function="thermostat_command", gap_ids=["LC:x"],
                    description="d", purpose="the second reading heats", test_name="test_thermostat_command_heats_later",
                    made_by="brain:qwen/scripted", checked=True, characterization=False, classification="confirmed_gap",
                    args=[uc.NamedValue(name="setpoint", value="20"), uc.NamedValue(name="hysteresis", value="2")],
                    stub_values=[uc.StubValues(stub="read_sensor", returns=["SENSOR_ERROR", "10"])],
                    expected_return="-1", evidence=[self.e2], strongest_class="E2")
        base.update(kw)
        return uc.Scenario(**base)

    def test_stub_returns_a_sequence(self):
        _, source, header = a2.render_stubs(self.plan)
        self.assertIn("extern int ut_stub_read_sensor_returns[UT_STUB_SEQUENCE];", header)       # A2-21g
        self.assertIn("if (ut_stub_read_sensor_calls >= 1 && ut_stub_read_sensor_calls <= ut_stub_read_sensor_returns_len "
                      "&& ut_stub_read_sensor_returns_len <= UT_STUB_SEQUENCE) "
                      "return ut_stub_read_sensor_returns[ut_stub_read_sensor_calls - 1];", source)
        self.assertIn("if (ut_stub_read_sensor_calls < INT_MAX) ++ut_stub_read_sensor_calls;", source)   # never overflows
        self.assertIn("    ut_stub_call();", source)                                                 # A3-45b

    def test_examples_reach_both_brains(self):                                          # KB-24, KB-25
        example = uc.KBExample(example_id="KB-1", kind="test_example", language="c", framework="ut_harness",
                               category="BC", step="A2.tests", function="crc8", signature="uint8_t crc8(int n)",
                               context="reached: BC:4:9:T `if (n == 0)`", snippet="UT_TEST(t_example) { crc8(0); }")
        scenario = self.scenario(gap_ids=["EVIDENCE:thermostat_command"], characterization=True, expected_return="",
                                 classification="characterization_gap", evidence=[])
        plan = self.plan.model_copy(update={"stage": "coverage", "scenarios": [scenario]})
        req = uc.GenerationRequest(plan=plan, guidance=uc.KBGuidance(examples={
            uc.example_key("A2.tests", "thermostat_command"): [example]}))
        agent = a2.GeneratorAgent()
        seen = []

        async def generate(task, context, model, **kw):
            seen.append("\n".join(p.text for p in context))
            raise ba.AgentFault("MODEL_UNAVAILABLE", "scripted")
        agent.generate = generate
        with brain(DownBrain()):                                                   # TP-21
            run(agent._llm_tests(plan, req, set(), [scenario], set()))
        self.assertTrue(seen)
        self.assertIn("EXAMPLES (verified tests of similar functions in other code", seen[0])
        self.assertIn("UT_TEST(t_example) { crc8(0); }", seen[0])
        self.assertNotIn("KB GUIDANCE", seen[0])                          # no suite openings any more [KB-25]
        fn = next(f for f in self.layout.functions if f.name == "thermostat_command")
        sut = (Path(self.layout.repo_root) / self.layout.sut_file).read_text()
        facts = a1.AnalyzerAgent()._facts(fn, [], {"LC:x"}, self.layout, uc.PlanRequest(
            layout=self.layout, examples={"thermostat_command": "- for f: items ... chosen: args n=0"}), sut, [], [])
        self.assertIn("EXAMPLES (scenarios that reached similar items", "\n".join(p.text for p in facts))

    def test_the_orchestrator_asks_for_examples_per_function(self):                    # KB-24
        orch = up.Orchestrator.__new__(up.Orchestrator)
        orch.layout = self.layout
        s = up.RunState(req=uc.PipelineRequest(repo_root=self.layout.repo_root, sut_file="src/thermostat.c"),
                        repo=Path(self.layout.repo_root), out=Path(self.tmp.name) / "out",
                        workspace=Path(self.layout.repo_root))
        fn = next(f for f in self.layout.functions if f.name == "thermostat_command")
        line = fn.line_start + 2
        query = orch._example_queries(s, "A1.plan", {"thermostat_command": [f"BC:thermostat_command:{line}:5:T"]})[0]
        code = (Path(self.layout.repo_root) / self.layout.sut_file).read_text().splitlines()[line - 1].strip()
        self.assertIn(code[:40], query.text)
        self.assertEqual(query.function, fn.qualified_name)
        asked = []

        async def call(role, tool, payload, expect):
            asked.append(payload)
            return uc.KBGuidance(examples={uc.example_key("A1.plan", fn.qualified_name): [uc.KBExample(
                example_id="KB-2", kind="plan_example", language="c", framework="ut_harness", category="BC",
                step="A1.plan", function="g", signature="int g(int)", context="items: BC:3:1:T", snippet="args a=1")]})
        orch.call = call
        last = uc.VerificationResult.model_construct(stage="verify", coverage=uc.CoverageReport(
            toolchain="llvm", definitions="d", functions=[
            uc.FunctionCoverage(name="thermostat_command", gaps=[uc.GapItem(
                gap_id=f"BC:thermostat_command:{line}:5:T", function="thermostat_command", metric="BC", line=line)])]))
        self.assertEqual(run(orch._planning_examples(s, last, 1)), {})            # the rule round: no brain plans
        found = run(orch._planning_examples(s, last, 2))
        self.assertIn("chosen: args a=1", found[fn.qualified_name])
        self.assertEqual(s.examples_given, {"A1.plan": 1})

    def test_checked_brain_scenario_is_assembled_without_llm(self):
        plan = self.plan.model_copy(update={"stage": "coverage", "scenarios": [self.scenario()]})
        agent = a2.GeneratorAgent()
        with brain(DownBrain()):
            bundle = run(agent.generate_bundle(uc.GenerationRequest(plan=plan)))
        suite = uc.suite_of(bundle)
        self.assertIn("/* evidence: E2 */\nUT_TEST(test_thermostat_command_heats_later)", suite)    # A2-21m
        self.assertIn("ut_stub_read_sensor_returns[1] = 10;", suite)
        self.assertIn("ut_stub_read_sensor_returns_len = 2;", suite)
        self.assertIn("UT_EXPECT_EQ_INT(thermostat_command(20, 2), -1);", suite)
        self.assertIn(f"/* S-brain-thermostat_command-2-1: the second reading heats [{self.e2.evidence_id}] - by "
                      "qwen/scripted scenario [BP-7] */", suite)                       # A2-21q, A2-21a, BP-7
        self.assertNotIn("test_thermostat_command_heats_later", bundle.characterization_tests)

    def test_linked_scenario_is_marked(self):
        linked = self.e2.model_copy(update={"evidence_class": "E1", "linked_by": "brain p=0.80"})
        plan = self.plan.model_copy(update={"stage": "coverage", "scenarios": [
            self.scenario(evidence=[linked], strongest_class="E1", linked=True)]})
        with brain(DownBrain()):
            bundle = run(a2.GeneratorAgent().generate_bundle(uc.GenerationRequest(plan=plan)))
        suite_action = next(a for a in bundle.actions if a.role == "testsuite")
        self.assertIn("test_thermostat_command_heats_later", suite_action.linked_tests)          # A3-CL11
        self.assertIn("/* evidence: E1 linked */", suite_action.content)
        self.assertIn(f"@{uc.evidence_key(linked)}", suite_action.content)
        self.assertTrue(a2.parse_suite(suite_action.content)["test_thermostat_command_heats_later"]["linked"])

    def test_requirement_scenario_goes_to_the_llm_even_with_deterministic_tests(self):
        normative = uc.Scenario(scenario_id="S-evidence-clamp_percent", function="clamp_percent",
                                gap_ids=["EVIDENCE:clamp_percent"], description="check the stated range",
                                characterization=False, evidence=[self.e2], strongest_class="E2")
        plan = self.plan.model_copy(update={"scenarios": list(self.plan.scenarios) + [normative]})
        with writer_brain(UTBrain()) as b:
            bundle = run(a2.GeneratorAgent().generate_bundle(uc.GenerationRequest(plan=plan)))
        self.assertTrue(any("_auto_" in n for n in bundle.tests))                       # deterministic first
        self.assertIn("test_clamp_percent_upper_limit", bundle.tests)                   # A2-21n
        self.assertTrue(b.generate_calls)

    def test_a_structured_brain_never_writes_a_whole_test(self):                   # BP-2, structured
        """Qwen3 8B (the minimum profile) answers in the structured form only: a requirement scenario no checked plan
        states is not given to a test writer; it stays open, and the reason goes to the journal for the next plan."""
        normative = uc.Scenario(scenario_id="S-evidence-clamp_percent", function="clamp_percent",
                                gap_ids=["EVIDENCE:clamp_percent"], description="check the stated range",
                                characterization=False, evidence=[self.e2], strongest_class="E2")
        plan = self.plan.model_copy(update={"scenarios": list(self.plan.scenarios) + [normative]})
        with brain(UTBrain()) as b:
            bundle = run(a2.GeneratorAgent().generate_bundle(uc.GenerationRequest(plan=plan)))
        self.assertTrue(any("_auto_" in n for n in bundle.tests))                       # the rules still work
        self.assertNotIn("test_clamp_percent_upper_limit", bundle.tests)
        self.assertFalse([c for c in b.generate_calls if "Write at most" in str(c)])   # A2 was not asked
        self.assertTrue(any(n.startswith("clamp_percent: not built (") for n in bundle.notes), bundle.notes)

    def test_a_failing_e2_test_judged_wrong_takes_its_values_from_the_run(self):        # A2-21p, A3-85b
        """No brain rewrites the test: it keeps its inputs, its assertions become observations, and the observe run
        and finalize give them the run's values - the reference form of the oracle check."""
        old = ('/* evidence: E2 */\nUT_TEST(test_clamp_percent_upper_limit)\n{\n    UT_EXPECT_EQ_INT(clamp_percent(150), 99);\n}\n'
               'UT_SUITE_BEGIN\n    UT_CASE(test_clamp_percent_upper_limit)\nUT_SUITE_END\n')
        repair = uc.RepairItem(item_id="test-test_clamp_percent_upper_limit", owner="agent2",
                               target_file=self.layout.suite_file, issue="test_generation_defect",
                               diagnostic="expected 99, actual 100", affected_test="test_clamp_percent_upper_limit")
        plan = self.plan.model_copy(update={"repairs": [repair], "drop_tests": ["test_clamp_percent_upper_limit"]})
        with brain(UTBrain()) as b:
            bundle = run(a2.GeneratorAgent().generate_bundle(uc.GenerationRequest(plan=plan, existing_suite=old)))
        suite = uc.suite_of(bundle)
        self.assertIn('UT_OBSERVE_INT("test_clamp_percent_upper_limit_g1", clamp_percent(150));', suite)  # same input
        self.assertNotIn("clamp_percent(150), 99", suite)
        self.assertEqual(bundle.actions[-1].evidence_classes["test_clamp_percent_upper_limit"], "E8")  # from the run
        change = next(c for c in bundle.expectation_changes if c.test == "test_clamp_percent_upper_limit")
        self.assertIn("expected 99, actual 100", change.reason)                         # A2-21e: reported
        self.assertFalse([c for c in b.generate_calls if "clamp_percent" in str(c)])   # no brain rewrite
        done = a2.finalize_bundle(bundle, {"test_clamp_percent_upper_limit_g1": "100"})
        self.assertIn("UT_EXPECT_EQ_INT(clamp_percent(150), 100);", uc.suite_of(done))   # the reference form
        crashed = repair.model_copy(update={"failure_kind": "crash"})
        bundle = run(a2.GeneratorAgent().generate_bundle(uc.GenerationRequest(
            plan=plan.model_copy(update={"repairs": [crashed]}), existing_suite=old)))
        self.assertNotIn("test_clamp_percent_upper_limit", bundle.tests)                # its inputs miss: dropped
        self.assertTrue(any("inputs miss" in n for n in bundle.notes), bundle.notes)


class FixCandidateTests(unittest.TestCase):
    """A4-23g: several candidates, each checked; A4-23h: a second brain reviews and can only block."""
    def setUp(self):
        self.tmp = tmpdir()
        self.repo = copy_pilot("c_pilot", Path(self.tmp.name))
        sut = self.repo / "src/thermostat.c"
        sut.write_text(sut.read_text().replace("return 100;", "return 99;"))
        analyzer = a1.AnalyzerAgent()
        self.layout = analyzer.discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/thermostat.c"))
        self.evidence = analyzer.collect_evidence(self.layout, "clamp_percent").evidence
        self.failure = uc.Failure(failure_id="test-test_clamp_percent_upper_limit", kind="test",
                                  classification="source_behavior_defect", confidence=0.9, deterministic=False,
                                  route="agent4", affected_test="test_clamp_percent_upper_limit",
                                  affected_function="clamp_percent", diagnostic="expected 100, actual 99")

    def tearDown(self):
        self.tmp.cleanup()

    def test_candidates_are_checked_and_sized(self):
        big = {"original_fragment": "    if (value > 100) {\n        return 99;\n    }",
               "corrected_fragment": "    if (value >= 100) {\n        return 100;\n    }", "rationale": "E2"}
        small = {"original_fragment": "return 99;", "corrected_fragment": "return 100;", "rationale": "E2"}
        outside = {"original_fragment": "int read_sensor(int channel);", "corrected_fragment": "x", "rationale": "r"}
        with brain(UTBrain(candidates=[big, small, outside, small])):
            result = run(a4.CorrectorAgent().candidates(uc.CorrectionRequest(
                layout=self.layout, failure=self.failure, evidence=self.evidence)))
        self.assertEqual(len(result.patches), 2)                                          # A4-23g: checked, deduplicated
        sizes = sorted((p.changed_lines, p.changed_chars) for p in result.patches)
        self.assertEqual(sizes[0][0], 1)
        self.assertGreater(sizes[1][0], 1)
        self.assertTrue(all(p.author == "qwen" and p.status == "candidate_source_fix" for p in result.patches))

    def review_request(self):
        content = (self.repo / "src/thermostat.c").read_text().replace("return 99;", "return 100;")
        patch = uc.SourcePatch(patch_id="fix-1", file="src/thermostat.c", function="clamp_percent", author="qwen",
                               original_fragment="return 99;", corrected_fragment="return 100;", rationale="E2",
                               failure_id=self.failure.failure_id, patched_content=content)
        v = uc.VerificationResult(run_id="r", stage="verify", sut_sha256="0" * 64, suite_sha256="", build_ok=True,
                                  link_ok=True, decision="complete", passed=5, total=5)
        return uc.ReviewRequest(layout=self.layout, failure=self.failure, patch=patch, verification=v,
                                evidence=self.evidence)

    def test_second_brain_reviews_and_can_block(self):
        author, reviewer = UTBrain(name="qwen"), UTBrain(name="opus", review="block")
        router = ba.BrainRouter([reviewer, author], 0.0)
        agent = a4.CorrectorAgent(router=router)
        result = run(agent.review(self.review_request()))
        self.assertEqual(result.decision, "block")                                        # A4-23h
        self.assertFalse(result.same_brain)
        self.assertTrue(result.reviewer.startswith("opus"))
        self.assertEqual(author.questions, [])                                            # the author did not review

    def test_without_a_second_brain_the_same_brain_reviews_separately(self):
        only = UTBrain(name="qwen", review="agree")
        result = run(a4.CorrectorAgent(router=ba.BrainRouter([only], 0.0)).review(self.review_request()))
        self.assertEqual(result.decision, "agree")
        self.assertTrue(result.same_brain)
        self.assertIn("same brain", result.reason)


@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
class EstimatedBuildTests(unittest.TestCase):
    """A3-BD11 .. A3-BD13 and ORC-22."""
    def setUp(self):
        self.tmp = tmpdir()
        self.d = Path(self.tmp.name)
        self.a1 = a1.AnalyzerAgent()
        self.a3 = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "work")))

    def tearDown(self):
        self.tmp.cleanup()

    def test_brain_suggests_flags_and_a_compile_decides(self):
        repo = self.d / "board"
        (repo / "src").mkdir(parents=True)
        (repo / "src/board.c").write_text('#ifndef BOARD_REV\n#error "BOARD_REV must be defined"\n#endif\n'
                                          "int board_rev(void)\n{\n    return BOARD_REV;\n}\n")
        (repo / "README.md").write_text("Build the firmware with -DBOARD_REV=3 for the current board.\n")
        layout = self.a1.discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/board.c"))
        build = self.a3.capture(uc.CaptureRequest(layout=layout))
        self.assertTrue(build.estimated)
        flags = [{"flag": "-DBOARD_REV=3", "file": "README.md", "text": "-DBOARD_REV=3"},
                 {"flag": "-I/etc", "file": "README.md", "text": "Build the firmware"},
                 {"flag": "-DOTHER=1", "file": "README.md", "text": "not in the file"},
                 {"flag": "-O2", "file": "README.md", "text": "Build"}]
        with brain(UTBrain(flags=flags)):
            out = run(self.a3.suggest_flags(uc.FlagRequest(layout=layout, build=build)))
        self.assertIn("-DBOARD_REV=3", out.sut_flags)                                       # A3-BD12
        self.assertNotIn("-DOTHER=1", out.sut_flags)
        self.assertTrue(out.estimated)                                                      # A3-BD13
        self.assertEqual(len(out.suggested_flags), 1)
        self.assertEqual(sum("refused" in n for n in out.notes), 3)
        layout = layout.model_copy(update={"compile_flags": out.sut_flags, "build": out})
        result = self.a3.execute(uc.VerifyRequest(layout=layout, stage="probe"))
        self.assertTrue(result.build_ok, [d.message for d in result.diagnostics])

    @unittest.skipUnless(have_tool("cmake") and have_tool("ninja"), "cmake and ninja are not in the tool manifest")
    def test_capture_is_kept_per_commit(self):
        repo = copy_pilot("c_deps_pilot", self.d)
        first = self.a3.capture(uc.CaptureRequest(layout=self.a1.discover(uc.DiscoverRequest(
            repo_root=str(repo), sut_file="src/ctrl.c")), cache_key="commit1"))
        second = self.a3.capture(uc.CaptureRequest(layout=self.a1.discover(uc.DiscoverRequest(
            repo_root=str(repo), sut_file="lib/util.c")), cache_key="commit1"))
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)                                                      # ORC-22
        self.assertEqual(second.source, "cmake")
        self.assertEqual({e.file for e in second.entries}, {e.file for e in first.entries})


# ============================================================================ 8c. repository link and delivery
class FakePullRequests(http.server.BaseHTTPRequestHandler):
    """A stand-in for the GitHub REST API: records what it receives."""
    received: list[dict] = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        FakePullRequests.received.append({"path": self.path, "auth": self.headers.get("Authorization", ""), "body": body})
        data = json.dumps({"html_url": "https://example.test/o/r/pull/7", "number": 7}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        data = json.dumps({"state": "closed", "merged_at": "2026-10-11T10:00:00Z"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def git_cmd(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=dev", "-c", "user.email=dev@example.test", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()


@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
@unittest.skipUnless(HAVE_FASTMCP and HAVE_LANGGRAPH and have_tool("git"), "fastmcp, langgraph and git are required")
@full_run
class RepositoryTests(unittest.TestCase):
    """ORC-17 .. ORC-31 against a local bare repository standing in for the git server."""
    def setUp(self):
        self.tmp = tmpdir()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def server(self, pilot: str = "c_pilot", seed: tuple[str, str, str] | None = None, protect: bool = False) -> str:
        work = copy_pilot(pilot, self.d / "dev")
        if seed:
            path = work / seed[0]
            path.write_text(path.read_text().replace(seed[1], seed[2]))
        server = self.d / "server.git"
        git_cmd(self.d, "init", "-q", "--bare", "-b", "main", str(server))
        git_cmd(work, "init", "-q", "-b", "main")
        git_cmd(work, "add", "-A")
        git_cmd(work, "commit", "-q", "-m", "base")
        git_cmd(work, "push", "-q", str(server), "main")
        if protect:
            hook = server / "hooks" / "pre-receive"
            hook.write_text('#!/bin/sh\nwhile read old new ref; do\n  if [ "$ref" = refs/heads/main ]; then\n'
                            '    echo "protected branch: main"; exit 1\n  fi\ndone\nexit 0\n')
            hook.chmod(0o755)
        self.work = work
        return server.as_uri()

    def run_repo(self, url: str, suts: list[str], **pipeline) -> uc.RepositoryResult:
        config = pipeline_config(self.d, repo_cache=str(self.d / "cache"), **pipeline)
        return run(up.RepositoryRun(config).run(uc.RepositoryRequest(repository=url, sut_files=suts)))

    def branches(self) -> list[str]:
        return git_cmd(self.d / "server.git", "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()

    def test_without_sut_files_every_production_file_is_tested(self):
        """ORC-35: no SUT file named - Agent1 selects the production files of the tested commit; the summary
        lists them and every file left out with its reason."""
        work = copy_pilot("c_pilot", self.d / "dev")
        (work / "tests").mkdir()
        (work / "tests/legacy_test.c").write_text("int legacy(void)\n{\n    return 0;\n}\n")
        (work / "third_party/minilib").mkdir(parents=True)
        (work / "third_party/minilib/mini.c").write_text("int mini(void)\n{\n    return 1;\n}\n")
        server = self.d / "server.git"
        git_cmd(self.d, "init", "-q", "--bare", "-b", "main", str(server))
        git_cmd(work, "init", "-q", "-b", "main")
        git_cmd(work, "add", "-A")
        git_cmd(work, "commit", "-q", "-m", "base")
        git_cmd(work, "push", "-q", str(server), "main")
        with brain(UTBrain()):
            result = self.run_repo(server.as_uri(), [])
        self.assertEqual(result.selection.selected, ["src/thermostat.c"])
        self.assertEqual(sorted(result.selection.left_out), ["tests/legacy_test.c: test code",
                                                             "third_party/minilib/mini.c: third-party code (folder name)"])
        self.assertEqual([r.sut_file for r in result.results], ["src/thermostat.c"])
        summary = (Path(result.output_dir) / "summary.md").read_text()
        self.assertIn("## Production files selected (1) [ORC-35]", summary)
        self.assertIn("- tests/legacy_test.c: test code", summary)
        self.assertFalse((Path(result.output_dir) / "worktrees" / "select").exists())

    def test_tests_only_are_committed_to_the_default_branch(self):
        url = self.server()
        base = git_cmd(self.d / "server.git", "rev-parse", "main")
        with brain(UTBrain()):
            result = self.run_repo(url, ["src/thermostat.c"])
        print("\n" + (Path(result.output_dir) / "summary.md").read_text())
        self.assertEqual((result.branch, result.commit), ("main", base))                   # ORC-18, ORC-19
        record = result.deliveries[0]
        self.assertEqual(record.kind, "direct_commit", record.reason)                        # ORC-24
        head = git_cmd(self.d / "server.git", "rev-parse", "main")
        self.assertEqual(head, record.commit)
        self.assertEqual(git_cmd(self.d / "server.git", "rev-parse", "main~1"), base)       # one new commit
        files = git_cmd(self.d / "server.git", "ls-tree", "-r", "--name-only", "main").splitlines()
        self.assertIn("tests/test_thermostat.c", files)
        self.assertIn("tests/ut_harness.c", files)
        for name in ("report.md", "result.json", "ledger.jsonl", "coverage.json"):
            self.assertIn(f"ut_reports/src/thermostat.c/{name}", files)                    # ORC-27
        message = git_cmd(self.d / "server.git", "log", "-1", "--format=%B", "main")
        self.assertIn(base, message)
        self.assertIn("src/thermostat.c: verified_measured", message)
        self.assertEqual(self.branches(), ["main"])                                          # ORC-31
        ledger = Path(result.results[0].ledger).read_text()
        self.assertIn(base, ledger)                                                           # ORC-19
        self.assertEqual(result.results[0].delivery.split()[0:2], ["direct", "commit"])
        self.assertTrue(any(p.name.endswith(".git") for p in (self.d / "cache" / "repos").iterdir()))   # ORC-20

    def test_second_run_keeps_the_delivered_tests(self):
        """A run on a branch that already holds an earlier delivery: the pipeline's own files are neither
        existing tests nor E5 evidence nor the project's stubs; the tests stay as they are."""
        url = self.server()
        with brain(UTBrain(tests={})):
            first = self.run_repo(url, ["src/thermostat.c"])
            second = self.run_repo(url, ["src/thermostat.c"])
        self.assertEqual([d.kind for d in first.deliveries + second.deliveries], ["direct_commit", "direct_commit"],
                         [d.reason for d in second.deliveries])
        self.assertEqual(second.commit, first.deliveries[0].commit)                       # tested the delivery
        changed = git_cmd(self.d / "server.git", "diff", "--name-only", "main~1", "main").splitlines()
        self.assertTrue(changed and all(c.startswith("ut_reports/") for c in changed), changed)
        self.assertEqual(second.results[0].final_state, "verified_measured")

    def test_source_fix_opens_a_review_pull_request(self):
        FakePullRequests.received = []
        server = http.server.HTTPServer(("127.0.0.1", 0), FakePullRequests)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = self.server(seed=("src/thermostat.c", "return 1;", "return 2;"))
        base = git_cmd(self.d / "server.git", "rev-parse", "main")
        settings = {url: {"kind": "github", "api": f"http://127.0.0.1:{server.server_port}", "project": "o/r",
                          "token_env": "UT_TEST_PR_TOKEN"}}
        fix = UTBrain(patch={"original_fragment": "return 2;", "corrected_fragment": "return 1;",
                             "rationale": "E2: @return 1 to heat"})
        with mock.patch.dict(os.environ, {"UT_TEST_PR_TOKEN": "s3cr3t-token", "NO_PROXY": "127.0.0.1",
                                          "no_proxy": "127.0.0.1"}), brain(fix):
            result = self.run_repo(url, ["src/thermostat.c"], repositories=settings)
        server.shutdown()
        server.server_close()
        record = result.deliveries[0]
        self.assertEqual(record.kind, "pull_request", record.reason)                          # ORC-25
        self.assertEqual(record.url, "https://example.test/o/r/pull/7")
        self.assertEqual(git_cmd(self.d / "server.git", "rev-parse", "main"), base)          # main unchanged
        branch = record.branch
        self.assertTrue(branch.startswith("ut-review/src-thermostat.c-"))
        show = lambda path: git_cmd(self.d / "server.git", "show", f"{branch}:{path}")
        self.assertIn("return 1;", show("src/thermostat.c"))                                 # the fix ...
        self.assertIn("thermostat_command(20, 2), 1);", show("tests/test_thermostat.c"))      # ... its tests
        self.assertIn("final_state", show("ut_reports/src/thermostat.c/result.json"))         # ... and reports
        sent = FakePullRequests.received[0]
        self.assertEqual(sent["auth"], "Bearer s3cr3t-token")
        self.assertEqual((sent["body"]["head"], sent["body"]["base"]), (branch, "main"))
        self.assertIn("Source change", sent["body"]["body"])
        self.assertIn("Merging this pull request is the approval", sent["body"]["body"])      # ORC-26
        for path in Path(result.output_dir).rglob("*"):                                      # ORC-21
            if path.is_file() and path.suffix in (".json", ".md", ".jsonl", ".log", ".patch"):
                self.assertNotIn("s3cr3t-token", path.read_text(errors="replace"), path)
        self.assertNotIn("s3cr3t-token", git_cmd(self.d / "server.git", "log", "-p", "--all"))
        state = json.loads(next((self.d / "cache" / "state").glob("*.json")).read_text())
        self.assertEqual(state["src/thermostat.c"]["number"], "7")                         # ORC-26: next run
        cache = up.RepoCache(self.d / "cache", url, up.Git("git"))
        prs = up.PullRequests(url, settings, up.Git("git"))
        server2 = http.server.HTTPServer(("127.0.0.1", 0), FakePullRequests)
        threading.Thread(target=server2.serve_forever, daemon=True).start()
        prs.api = f"http://127.0.0.1:{server2.server_port}"
        with mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
            self.assertIn("(merged)", up.RepositoryRun._previous(prs, cache.load_state()["src/thermostat.c"]))
        server2.shutdown()
        server2.server_close()

    def test_protected_branch_gets_a_tests_only_review(self):
        url = self.server(protect=True)
        with brain(UTBrain()):
            result = self.run_repo(url, ["src/thermostat.c"])
        record = result.deliveries[0]
        self.assertEqual(record.kind, "review_branch", record.reason)                        # ORC-30 (no PR service)
        self.assertTrue(record.branch.startswith("ut-review/tests-"))
        self.assertIn("ORC-30", record.reason)
        self.assertIn("tests/test_thermostat.c",
                      git_cmd(self.d / "server.git", "ls-tree", "-r", "--name-only", record.branch))

    def test_moved_branch_is_verified_again_before_the_commit(self):
        url = self.server()
        original = up.Delivery.deliver

        async def deliver_after_a_push(delivery, runs):
            (self.work / "NOTES.txt").write_text("someone else committed\n")
            git_cmd(self.work, "add", "-A")
            git_cmd(self.work, "commit", "-q", "-m", "other change")
            git_cmd(self.work, "push", "-q", str(self.d / "server.git"), "main")
            return await original(delivery, runs)

        with mock.patch.object(up.Delivery, "deliver", deliver_after_a_push), brain(UTBrain()):
            result = self.run_repo(url, ["src/thermostat.c"])
        record = result.deliveries[0]
        self.assertEqual(record.kind, "direct_commit", record.reason)                         # ORC-29
        moved = git_cmd(self.d / "server.git", "rev-parse", "main~1")
        self.assertNotEqual(moved, result.commit)
        self.assertEqual(git_cmd(self.d / "server.git", "log", "-1", "--format=%s", "main~1"), "other change")
        self.assertIn("verified again", git_cmd(self.d / "server.git", "log", "-1", "--format=%B", "main"))
        self.assertTrue(list(Path(result.output_dir).rglob("ledger-reverify.jsonl")))

    def test_refusals_and_local_delivery(self):
        """ORC-30: what a refused push means; without write access a branch, a patch and a bundle stay local."""
        refusal = up.Git.refusal
        self.assertEqual(refusal("! [rejected] main -> main (fetch first)"), "moved")
        self.assertEqual(refusal("remote: error: GH006: Protected branch update failed"), "protected")
        self.assertEqual(refusal("remote: Permission to o/r.git denied to dev.\nfatal: ... 403"), "no_access")
        url = self.server()
        git = up.Git(shutil.which("git"))
        cache = up.RepoCache(self.d / "cache", url, git)
        cache.update()
        base = cache.branch_head("main")
        sha = cache.commit(base, {"tests/new.c": b"int x;\n", "include/sensor.h": None}, "test commit")
        self.assertEqual(git.run("--git-dir", str(cache.dir), "rev-parse", f"{sha}~1"), base)
        files = git.run("--git-dir", str(cache.dir), "ls-tree", "-r", "--name-only", sha).splitlines()
        self.assertIn("tests/new.c", files)
        self.assertNotIn("include/sensor.h", files)
        kept = cache.keep_local(sha, "ut-local/tests-x", self.d / "out", base)
        self.assertTrue(all(Path(k).is_file() and Path(k).stat().st_size for k in kept))
        self.assertEqual(self.branches(), ["main"])                                        # nothing pushed
        self.assertEqual(up.Git("git", token_env="UT_T").clean("https://u:pw@host/x"), "https://***@host/x")

    def test_unreadable_repository_is_an_environment_defect(self):
        result = self.run_repo((self.d / "missing.git").as_uri(), ["src/a.c"])
        self.assertTrue(result.reason.startswith("environment_defect"), result.reason)     # ORC-21
        self.assertEqual(result.deliveries, [])

    @unittest.skipUnless(have_tool("cmake") and have_tool("ninja"), "cmake and ninja are not in the tool manifest")
    def test_several_sut_files_share_the_build_capture(self):
        url = self.server("c_deps_pilot")
        with brain(UTBrain(tests={}, dependency="stub")):
            result = self.run_repo(url, ["src/ctrl.c", "lib/util.c"], coverage=cov(3))
        print("\n" + (Path(result.output_dir) / "summary.md").read_text())
        self.assertEqual([r.sut_file for r in result.results], ["src/ctrl.c", "lib/util.c"])   # ORC-23
        notes = [r.build for r in result.results]
        self.assertTrue(all(n.startswith("cmake") for n in notes), notes)
        self.assertIn("reused", notes[1])                                                     # ORC-22
        ledgers = [Path(r.ledger).read_text() for r in result.results]
        self.assertTrue(all(result.commit in text for text in ledgers))
        summary = (Path(result.output_dir) / "summary.md").read_text()
        self.assertIn("src/ctrl.c", summary)
        self.assertIn("lib/util.c", summary)


# ============================================================================ 8b. token budget and knowledge packs
class RecordingBrain(UTBrain):
    """The scripted brain, also recording the context and token plan of every generate call."""
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.contexts, self.token_plans = [], []

    async def generate(self, task, context, schema, validate, plan=None):
        self.contexts.append((task, context))
        self.token_plans.append(plan)
        return await super().generate(task, context, schema, validate)


class BudgetAndPackTests(unittest.TestCase):
    """ARCH-11..ARCH-17 and KP-1..KP-9: the brains get prioritized parts, an expected answer, and the
    knowledge packs of their step; every plan is recorded."""
    def setUp(self):
        self.tmp = tmpdir()
        self.repo = copy_pilot("c_pilot", Path(self.tmp.name))
        self.agent = a1.build_agent(ba.Settings(), str(SRC / "ut_config.json"))
        self.layout = self.agent.discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/thermostat.c"))
        self.gaps = [uc.GapItem(gap_id="BC:thermostat_command:7:9:T", function="thermostat_command", metric="BC",
                                line=7, column=9, detail="(t == SENSOR_ERROR) never true")]

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, b: UTBrain) -> uc.UTPlan:
        with brain(b):
            return run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps),
                                                      iteration=2)))

    def test_the_application_packs_are_usable(self):
        packs = uc.knowledge_packs(SRC / "ut_config.json")
        self.assertEqual(packs.problems, [])                                        # KP-2
        self.assertEqual([p["name"] for p in packs.for_step("A1.plan")], ["test-design"])
        self.assertEqual([p["name"] for p in packs.for_step("A2.tests")], ["test-design", "test-style"])
        with mock.patch.dict(os.environ, {"UT_KNOWLEDGE": "off"}):
            self.assertIsNone(uc.knowledge_packs(SRC / "ut_config.json"))          # KP-8, KP-9

    def test_planning_gets_the_pack_after_the_facts(self):
        b = RecordingBrain()
        self.plan(b)
        task, context = next(c for c in b.contexts if c[0].startswith("Plan unit-test scenarios"))
        self.assertLess(context.index("TARGETS ("), context.index("GUIDANCE (knowledge pack test-design 1.0"))  # KP-3
        self.assertLess(context.index("EVIDENCE:"), context.index("GUIDANCE"))
        record = next(p for p in self.agent.token_plans if p["step"] == "A1.plan")                    # ARCH-15
        self.assertEqual(record["outcome"], "answered")
        self.assertEqual([(p["name"], p["use"], p["source"]) for p in record["packs"]],
                         [("test-design", "whole", "application")])                                  # KP-7
        self.assertEqual(len(record["packs"][0]["sha256"]), 64)
        self.assertGreater(record["output_tokens"], 0)
        self.assertEqual(record["kept"][:2], ["facts", "evidence"])

    def test_a_project_pack_replaces_the_application_pack(self):
        pack = self.repo / ".ut" / "knowledge" / "test-design.md"
        pack.parent.mkdir(parents=True)
        pack.write_text("---\nname: test-design\nversion: 7.1\nused_by: [A1.plan]\nsummary: ours\n---\n"
                        "Use the boundary values of our sensor table.\n", encoding="utf-8")
        b = RecordingBrain()
        self.plan(b)
        _, context = next(c for c in b.contexts if c[0].startswith("Plan unit-test scenarios"))
        self.assertIn("knowledge pack test-design 7.1", context)                   # KP-3
        self.assertIn("our sensor table", context)
        self.assertNotIn("Boundary pairs", context)

    def test_packs_give_way_before_facts_in_a_small_window(self):
        b = RecordingBrain()
        b.call_limits = lambda kind, need: ([2048], need or 1000)        # a very small local model
        b.settings = mock.Mock(chars_per_token=3.0, adaptive=True)
        self.plan(b)
        record = next(p for p in self.agent.token_plans if p["step"] == "A1.plan")
        self.assertEqual(record["outcome"], "answered")
        self.assertIn("facts", record["kept"])                                      # ARCH-13: never cut
        self.assertIn("pack:test-design", record["shortened"] + record["dropped"])  # KP-4: the pack gives way
        self.assertLessEqual(record["input_tokens"] + record["output_tokens"], 2048)
        _, context = next(c for c in b.contexts if c[0].startswith("Plan unit-test scenarios"))
        self.assertIn("TARGETS (", context)

    def test_token_summary(self):
        plans = [{"step": "A1.plan", "brain": "qwen", "window": 8192, "input_tokens": 3000, "output_tokens": 900,
                  "used_input_tokens": 2800, "used_output_tokens": 700, "outcome": "answered",
                  "shortened": ["tried"], "dropped": [], "packs": [{"name": "test-design", "version": "1.0",
                                                                    "sha256": "a" * 64, "source": "application",
                                                                    "use": "whole"}]},
                 {"step": "A1.plan", "brain": "qwen", "window": 0, "outcome": "CONTEXT_BUDGET_EXCEEDED",
                  "shortened": [], "dropped": ["style"], "packs": []}]
        lines, packs = uc.token_summary(plans)
        self.assertEqual(lines, ["A1.plan: 1 of 2 call(s) answered (qwen 1); windows 8192; tokens estimated 3,000 in, "
                                 "limit 900 out; reported 2,800 in, 700 out; shortened: tried x1; left out: style x1; "
                                 "not answered: CONTEXT_BUDGET_EXCEEDED x1"])
        self.assertEqual(packs, ["test-design 1.0 (application) sha256:aaaaaaaaaaaaaaaa: 1 call(s) whole"])


# ============================================================================ 8d. coverage targets [COV] and live viewer [VIEW]
GUARD_C = """#include "guard.h"

int guard(int v)
{
    if (v > 10) {
        if (v < 5) {
            return -1;
        }
        return 10;
    }
    return v;
}
"""
GUARD_H = """#ifndef GUARD_H
#define GUARD_H
/** @return v limited to at most 10 */
int guard(int v);
#endif
"""
DEAD = "        if (v < 5) {\n            return -1;\n        }\n"


class CoveragePolicyTests(unittest.TestCase):
    def test_every_file_is_asil_d_by_default(self):                                       # COV-2
        level, source, targets, iterations = uc.coverage_policy("src/a.c", None)
        self.assertEqual((level, source, iterations), ("D", "default", uc.DEFAULT_ITERATIONS))
        self.assertEqual(targets, {"LC": "100", "SC": "100", "BC": "100", "DC": "100", "MCDC": "100"})
        self.assertEqual(uc.DEFAULT_ITERATIONS, 10)                            # the safety ceiling [COV-9, LA-7]

    def test_lower_levels_need_mcdc_above_95_and_everything_else_100(self):             # COV-1
        section = {"files": {"src/body/*.c": "B"}, "iterations": {"B": 3}}
        level, source, targets, iterations = uc.coverage_policy("src/body/door.c", section)
        self.assertEqual((level, source, iterations), ("B", "configuration: src/body/*.c", 3))
        self.assertEqual(targets["MCDC"], ">95")
        self.assertTrue(all(targets[m] == "100" for m in ("LC", "SC", "BC", "DC")))
        self.assertFalse(uc.target_met(95.0, ">95"))                                        # above, not equal
        self.assertTrue(uc.target_met(95.01, ">95"))
        self.assertEqual(uc.coverage_policy("src/brake.c", section)[0], "D")              # not named: D
        self.assertEqual(uc.coverage_policy("src/c.c", {"files": {"*.c": "C"}})[2]["MCDC"], "100")

    def test_no_configuration_can_lower_a_target(self):                                    # COV-1, COV-5
        for section in ({"levels": {"D": {"MCDC": "95"}}}, {"levels": {"B": {"LC": "99"}}, "default_level": "B"},
                        {"levels": {"QM": {"MCDC": "95"}}, "default_level": "QM"}, {"default_level": "X"},
                        {"iterations": {"D": 0}}):
            with self.assertRaises(ba.AgentFault, msg=section):
                uc.coverage_policy("a.c", section)
        stricter = uc.coverage_policy("a.c", {"default_level": "B", "levels": {"B": {"MCDC": "100"}}})[2]
        self.assertEqual(stricter["MCDC"], "100")

    def test_a_brain_cannot_talk_its_way_out(self):                                        # COV-5
        for claim in ("this branch is unreachable", "infeasible combination", "dead code, not worth testing",
                      "cannot be covered by a unit test", "excluded from coverage", "justified deviation: waiver"):
            self.assertTrue(a1.WAY_OUT.search(claim), claim)
        for fine in ("reach the else branch with v = 11", "the deviation of the sensor is 2", "excluding NULL"):
            self.assertFalse(a1.WAY_OUT.search(fine), fine)
        self.assertIsNone(a1.WAY_OUT.search(a1.PLAN_TASK))                            # no option in the task
        for pack in sorted((SRC / "knowledge").glob("*.md")):                       # nor in a planning pack
            text = pack.read_text(encoding="utf-8")
            steps = re.search(r"^used_by:\s*\[(.*)\]", text, re.M).group(1)
            if steps.strip() != "A4.suggest":
                self.assertIsNone(a1.WAY_OUT.search(text), pack.name)

    def test_the_path_to_an_item_is_part_of_the_proof(self):                              # A1-37
        fn = uc.FunctionInfo(name="guard", qualified_name="guard", signature="int guard(int v)", file="a.c",
                             line_start=1, line_end=9, params=[uc.ParamInfo(name="v", type="int", kind="int")])
        proof = a1.prove_infeasible("v < 5", "DC", None, "T", fn, {}, ["(v > 10) is true"], "int guard(int v)\n{")
        self.assertIn("v < 5 cannot hold where the path to it requires v > 10", proof)
        self.assertEqual(a1.prove_infeasible("v < 5", "DC", None, "T", fn, {}, ["(v > 10) is true"],
                                             "int guard(int v)\n{\n v = v - 20;"), "")      # changed on the way
        self.assertEqual(a1.prove_infeasible("v < 15", "DC", None, "T", fn, {}, ["(v > 10) is true"], ""), "")

    def test_nothing_to_measure_is_na(self):                                              # COV-8, VIEW-5
        self.assertEqual(uc.metric_text(uc.metric(0, 0)), "NA")
        self.assertEqual(uc.metric_text(uc.metric(0, 0, measured=False)), "not measured")
        self.assertEqual(uc.metric_text(uc.metric(7, 9)), "77.78 %")
        self.assertTrue(uc.target_met(uc.metric(0, 0).percent, "100"))                 # NA meets its target

    def test_strategies_are_merged_and_proofs_plain(self):                                # COV-10
        self.assertEqual(up.merge_strategies(["iteration 1: A", "iteration 1: B", "iteration 2: A", "iteration 3: A"]),
                         ["iterations 1-3: A", "iteration 1: B"])
        self.assertEqual(up.plain_proof("line never executed | unreachable_code: the branch is never taken (x) "
                                        "[A3-116, A3-121a]"), "the branch is never taken (x)")

    def test_open_items_count_and_report_without_exclusions(self):
        cov = uc.CoverageReport.model_construct(file_metrics=uc.Metrics(
            LC=uc.metric(9, 10), SC=uc.metric(10, 10), BC=uc.metric(3, 4), DC=uc.metric(1, 1), MCDC=uc.metric(0, 0)))
        self.assertEqual(uc.open_items_count(cov), 2)
        self.assertNotIn("exclusions", uc.PipelineResult.model_fields)                        # COV-3, COV-4
        self.assertEqual(set(uc.PipelineResult.model_fields["final_state"].annotation.__args__),
                         {"verified_measured", "incomplete"})


@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
@unittest.skipUnless(HAVE_FASTMCP and HAVE_LANGGRAPH, "fastmcp and langgraph are required")
@full_run
class HandOverTests(unittest.TestCase):
    """COV-9 .. COV-13: a stop below target hands the evidence, checked suggestions and the action needed to the
    developer. The SUT here has code no test can reach; the pilots have none."""
    def setUp(self):
        self.tmp = tmpdir()
        self.d = Path(self.tmp.name)
        self.repo = self.d / "guard"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "include").mkdir()
        (self.repo / "docs").mkdir()
        (self.repo / "src/guard.c").write_text(GUARD_C)
        (self.repo / "include/guard.h").write_text(GUARD_H)
        (self.repo / "docs/requirements.md").write_text("# Requirements\n\n- GRD-1: guard returns v limited to at most 10.\n")

    def tearDown(self):
        self.tmp.cleanup()

    def suggestion(self, context):
        ev = re.search(r"- (EV-guard-\d+) E1 [^\n]*GRD-1", context)                     # the requirement document
        items = re.findall(r"^- (\S+) \[LC\]", context, re.M)
        return [{"item_ids": items, "cause": "dead_code", "original_fragment": DEAD, "corrected_fragment": "",
                 "rationale": "1. Inside v > 10, v < 5 never holds. 2. No requirement asks for -1. 3. Remove it.",
                 "requirement_ids": [ev.group(1)] if ev else [],
                 "quotes": [{"evidence_id": ev.group(1), "text": "v limited to at most 10"}] if ev else [],
                 "expected_effect": "every line is reachable"},
                {"item_ids": items, "cause": "condition_defect", "original_fragment": "if (v < 5) {",
                 "corrected_fragment": "if (v < 5 || v > 1000) {",            # builds, reaches nothing new
                 "rationale": "a guess", "requirement_ids": [], "quotes": [], "expected_effect": "-"}]

    def test_stop_below_target_hands_over_evidence_and_checked_suggestions(self):
        b = UTBrain(tests={}, suggestions=self.suggestion)
        with brain(b):
            result = run(up.Orchestrator(pipeline_config(self.d, coverage=cov(3))).run(
                uc.PipelineRequest(repo_root=str(self.repo), sut_file="src/guard.c")))
        text = up.report(result)
        print("\n" + text)
        self.assertEqual(result.final_state, "incomplete", result.reason)                   # COV-1, COV-3
        self.assertLess(result.metrics.LC.percent, 100.0)                                   # counted as not covered
        self.assertTrue(any("not met" in line for line in result.target_check), result.target_check)   # COV-8
        self.assertTrue(result.not_reached)                                                 # COV-10
        for item in result.not_reached:
            self.assertEqual(item.function, "guard")
            self.assertTrue(item.strategies, item)
        self.assertLessEqual(sum(1 for i in result.iterations if i.stage == "verify"), 3)   # COV-9
        self.assertTrue(result.action_needed)                                               # COV-11
        self.assertEqual(len(result.suggestions), 1, (result.suggestions, result.open_items))   # COV-13
        self.assertIn("dead code", result.suggestions[0])
        self.assertIn("compared mutants still detected", result.suggestions[0])            # COV-17, COV-18
        self.assertNotIn("without requirements", result.suggestions[0])                     # cites GRD-1 [COV-12]
        self.assertFalse(any("Provide the requirement documents" in a for a in result.action_needed))
        patches = list((Path(result.output_dir) / "coverage_suggestions").glob("*.patch"))
        self.assertEqual(len(patches), 1)
        patch = patches[0].read_text()
        self.assertIn("-            return -1;", patch)
        self.assertIn("not applied, not committed, not counted as coverage", patch)
        self.assertIn("Fault detection:", patch)                                            # COV-18
        record = uc.FaultCheckResult.model_validate_json(patches[0].with_suffix(".faults.json").read_text())
        self.assertEqual(record.decision, "kept")
        self.assertTrue(record.mutants and record.operators)
        self.assertTrue(any("cov-" in n and "not shown" in n and "[COV-13]" in n for n in result.open_items),
                        result.open_items)                                                  # the guess is not shown
        self.assertEqual((self.repo / "src/guard.c").read_text(), GUARD_C)                # COV-7: never applied
        for title in ("## Coverage targets: ASIL D (default)", "## Action needed", "## Items not reached: evidence",
                      "## Source-change suggestions"):
            self.assertIn(title, text)
        self.assertNotIn("xclusion", text)
        self.assertIn("| MCDC | 0 | 0 | NA | not applicable |", text)                       # guard.c has no such decision
        self.assertTrue(any("could not reach the ITEMS NOT REACHED" in c for c in b.generate_calls))

    def test_without_a_brain_the_run_is_not_started(self):
        """ARCH-24, ORC-34: the brain plans; without one the run does not start and nothing is written."""
        before = sorted(p.relative_to(self.repo).as_posix() for p in self.repo.rglob("*") if ".git" not in p.parts)
        with brain(DownBrain()):
            result = run(up.Orchestrator(pipeline_config(self.d, coverage=cov(2))).run(
                uc.PipelineRequest(repo_root=str(self.repo), sut_file="src/guard.c")))
        self.assertEqual(result.final_state, "incomplete")
        self.assertIn("no brain is available, so the run was not started", result.reason)
        self.assertFalse(result.iterations)
        self.assertEqual(sorted(p.relative_to(self.repo).as_posix() for p in self.repo.rglob("*")
                                if ".git" not in p.parts), before)


@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
@unittest.skipUnless(HAVE_FASTMCP and HAVE_LANGGRAPH, "fastmcp and langgraph are required")
@full_run
class ViewerPipelineTests(unittest.TestCase):
    """VIEW-1 .. VIEW-7: every run writes its events; the live page serves them; the replay works offline."""
    def test_run_writes_events_and_replay(self):
        with tmpdir() as d:
            d = Path(d)
            repo = copy_pilot("c_pilot", d)
            os.environ["UT_TEST_SECRET_TOKEN"] = "s3cr3t-value-123"
            try:
                with brain(UTBrain()):
                    orch = up.Orchestrator(pipeline_config(d, viewer={"enabled": True, "open_browser": False}))
                    result = run(orch.run(uc.PipelineRequest(repo_root=str(repo), sut_file="src/thermostat.c")))
            finally:
                os.environ.pop("UT_TEST_SECRET_TOKEN", None)
            out = Path(result.output_dir)
            events = ba.read_events(out / "events.jsonl")
            kinds = {e["kind"] for e in events}
            self.assertTrue({"run_start", "stage", "handoff", "tool_start", "tool_end", "metric", "run_end"} <= kinds,
                            kinds)
            start = next(e for e in events if e["kind"] == "run_start")
            labels = {a["label"] for a in start["data"]["agents"]}
            self.assertTrue({"UT-Analyzer", "UT-Generator", "UT-Compilation", "UT-Source Code Corrector"} <= labels, labels)
            self.assertEqual({m["id"] for m in start["data"]["metrics"]},
                             {"lc", "sc", "bc", "dc", "mcdc", "tests_passed"})
            self.assertIn("ASIL D", start["data"]["subtitle"])
            node = start["data"]["orchestrator"]                                           # VIEW-1
            self.assertEqual(node["label"], "UT-Orchestrator")
            self.assertIn("src/thermostat.c", node["input"])
            sources = {e["agent"] for e in events if e["kind"] == "handoff"}
            self.assertIn("orchestrator", sources)                                        # it hands work out too
            self.assertTrue(all(e.get("summary") for e in events if e["kind"] in ("tool_start", "tool_end")))
            last = {e["data"]["id"]: e["data"] for e in events if e["kind"] == "metric"}
            self.assertEqual(last["mcdc"]["value"], 100.0)
            self.assertEqual(last["mcdc"]["goal"], 100.0)
            end = events[-1]
            self.assertEqual((end["kind"], end["data"]["state"]), ("run_end", "verified_measured"))
            idle = end["data"]["idle"]                                                     # VIEW-9
            self.assertIn("agent_sourcecode_corrector", idle)
            self.assertIn("no failing test was classified as a source defect", idle["agent_sourcecode_corrector"])
            self.assertNotIn("agent_unittest_analyzer", idle)
            raw = (out / "events.jsonl").read_text()
            self.assertNotIn("s3cr3t-value-123", raw)                                     # VIEW-6
            replay = (out / "viewer.html").read_text()                                    # VIEW-4
            self.assertIn("run_end", replay)
            self.assertIsNone(orch.hub)
            self.assertNotIn(ba.VIEW_ENV, os.environ)

    def test_every_tool_contract_has_a_plain_summary(self):
        """VIEW-8: no input or output is shown only by its type name."""
        import inspect
        source = inspect.getsource(uc.view_summary)
        missing = []
        for module in (a1, a2, a3, a4, kb):
            agent = module.build_agent(ba.Settings.model_validate({"engine": {"mode": "qwen"}}), None)
            for tool in agent.tools():
                for model in (tool.input_model, tool.output_model):
                    if f"isinstance(model, {model.__name__})" not in source:
                        missing.append(f"{agent.name}.{tool.name}: {model.__name__}")
        self.assertEqual(missing, [])

    def test_view_command_needs_a_run_folder(self):
        with tmpdir() as d:
            self.assertEqual(up.show_run(Path(d), open_browser=False), 2)


# ============================================================================ 8b. brain profiles, new code [BP, NC]
class BrainProfileTests(unittest.TestCase):
    """BP-1 .. BP-10: what each brain may write, the ladder of kinds, the gate per step, the same checks for every
    brain."""

    def test_default_profiles_and_the_configuration(self):                             # BP-1, BP-2
        book = uc.brain_profiles(None)
        self.assertEqual(book.of("sonnet").name, "full")
        self.assertEqual(book.of("qwen_remote").name, "minimum")
        self.assertEqual(book.of("a_model_of_the_customer").name, "minimum")          # no profile: the floor
        self.assertEqual(book.of("qwen").kinds, list(uc.MINIMUM_KINDS))                # Qwen runs unchanged
        self.assertTrue(book.allows("opus", "setup") and not book.allows("qwen", "setup"))
        with tmpdir() as d:
            cfg = Path(d) / "ut_config.json"
            cfg.write_text(json.dumps({"profiles": {"brains": {"mine": "mid"}, "definitions": {
                "mid": {"kinds": ["scenario", "setup", "no_such_kind"], "journal_tokens": 3000}},
                "steps": {"A2.value": ["haiku"]}}}))
            book = uc.brain_profiles(cfg)
        self.assertEqual(book.of("mine").kinds, ["scenario", "setup"])                # an unknown kind is ignored
        self.assertEqual(book.order(["opus", "haiku", "qwen"], "A2.value"), ["haiku", "opus", "qwen"])   # BP-6
        self.assertEqual(book.order(["opus", "haiku"], "A1.plan"), ["opus", "haiku"])
        self.assertRegex(book.record(["mine"])[0], r"^mine: mid \([0-9a-f]{16}\) - scenario, setup$")      # BP-7

    def test_the_ladder_of_kinds(self):                                                 # BP-3, BP-9
        full, minimum = list(uc.ANSWER_KINDS), list(uc.MINIMUM_KINDS)
        self.assertEqual(uc.next_kind("scenario", "test_setup", full), "setup")
        self.assertEqual(uc.next_kind("setup", "test_setup", full), "stub")          # the next wider kind
        self.assertEqual(uc.next_kind("scenario", "test_setup", minimum), "")        # structured only: no free code
        self.assertEqual(uc.next_kind("scenario", "needs_call_sequence", minimum), "call_sequence")
        self.assertEqual(uc.next_kind("scenario", "needs_call_sequence", full), "call_sequence")
        self.assertEqual(uc.next_kind("scenario", "wrong_inputs", full), "scenario")  # not a building cause
        self.assertEqual(uc.next_kind("test", "pipeline_limitation", ["values", "scenario", "test"]), "")   # BP-9
        self.assertEqual(uc.next_kind("suite_part", "test_setup", full), "")          # the top of the ladder

    def test_a_kind_the_profile_does_not_allow_is_never_asked(self):                  # BP-3, BP-10
        for name, allowed in (("qwen", False), ("opus", True)):
            agent = a2.GeneratorAgent()
            uc.apply_profiles(agent, None)
            b = UTBrain(name=name, setups={"lines": ["Heater ut_obj(ut_dep0);"], "call": "ut_obj.demand"})
            agent.router = ba.BrainRouter([b], 0.0)
            if allowed:
                out = run(agent.generate(a2.SETUP_TASK.format(method="demand", cls="Heater"), "facts", a2.SetupBlock,
                                         step="A2.setup"))
                self.assertEqual(out.call, "ut_obj.demand")
            else:
                with self.assertRaises(ba.AgentFault) as caught:
                    run(agent.generate("Build the object for a unit test", "facts", a2.SetupBlock, step="A2.setup"))
                self.assertEqual(caught.exception.code, "NO_BRAIN_FOR_STEP")
                self.assertEqual(b.generate_calls, [])                                 # not even asked
            gate = agent.brains_for_step
            self.assertIsNone(gate("A1.links", ["qwen"]))                              # not a kind of BP-2: open
            self.assertEqual(run(uc.brains_for(agent, "setup")), [name] if allowed else [])

    def test_the_same_checks_for_the_same_kind(self):                                  # BP-4, BP-10
        good = ["ut_fake_ISensor ut_dep0;", "Heater ut_obj(ut_dep0);"]
        self.assertEqual(a2.GeneratorAgent._block_problem(good, "ut_obj.demand"), "")
        self.assertIn("assert", a2.GeneratorAgent._block_problem(good + ["UT_EXPECT_TRUE(1);"], "ut_obj.demand"))
        self.assertIn("forbidden", a2.GeneratorAgent._block_problem(["system(\"x\");"], "ut_obj.demand"))
        self.assertEqual(a2.GeneratorAgent._block_problem(["class ut_fake_I : public I { int execute() override "
                                                           "{ return 0; } };"], fake=True), "")   # any method name
        self.assertIn("only helpers named ut_", a2.GeneratorAgent._shared_problem("static int helper(void) { return 1; }"))
        self.assertEqual(a2.GeneratorAgent._shared_problem("static int ut_helper(void) { return 1; }"), "")

    def test_profile_limits_of_the_context(self):                                      # BP-5, LA-10
        minimum, full = uc.DEFAULT_PROFILES["minimum"], uc.DEFAULT_PROFILES["full"]
        self.assertTrue(minimum.short_function(150, 6000) and not minimum.short_function(151, 6000))
        self.assertTrue(full.short_function(3000, 120_000))                            # the whole function fits
        self.assertFalse(full.model_copy(update={"window_tokens": 16384}).short_function(3000, 120_000))


class NewCodeTests(unittest.TestCase):
    """NC-1 .. NC-13 without a whole run: checks that guard or predict, recipes, platform facts, findings, corpus."""

    def test_guards_and_predictions(self):                                              # NC-6
        for reason in ("setup calls functions that do not exist here: mystery_init",
                       "x: 'abc' is not an integer value (literal, enumerator or macro)"):
            self.assertEqual(uc.check_kind(reason), "prediction", reason)
        for reason in ("claims the gap cannot or need not be covered ('unreachable'); ignored [COV-5]",
                       "the quote of EV-1 does not occur word for word in that evidence",
                       "checks nothing: no UT_EXPECT_... or UT_OBSERVE_...", "a reason no check declares",
                       uc.c_form_problem("f(0x42'});"), "c: not one C expression: it contains ';'"):
            self.assertEqual(uc.check_kind(reason), "guard", reason)       # well-formed C is a fact, not a prediction
        self.assertTrue(uc.only_predictions(["x: 'abc' is not an integer value"]))
        self.assertFalse(uc.only_predictions(["x: 'abc' is not an integer value", "checks nothing"]))
        self.assertFalse(uc.only_predictions([]))

    def test_well_formed_c_is_one_fact_for_every_check(self):
        """One lexer decides "is this well-formed C?" for every agent (run 2026-10-28, gpio_driver.c: a value with the
        rest of the JSON in it, 0x42'}], , reached the suite as a trial and stopped the file)."""
        for text in ("int a = 1'000;", "x = 'a'; y = \"}{\";", "/* { */ g();", 's = R"x(})x";', "u8'a' + L'}'"):
            self.assertEqual(uc.c_form_problem(text), "", text)
        for text in ("uart_put(0x42'});", "a[(1];", "f(1));", "/* open"):
            self.assertTrue(uc.c_form_problem(text), text)
        self.assertEqual(uc.check_kind(uc.c_form_problem("uart_put(0x42'});")), "guard")     # never a trial
        self.assertEqual(a1.fix_value("0x42'}], ", "c"), "0x42")              # the value ends where C ends
        self.assertEqual(uc.without_json_tail("0x42'}]x"), "0x42'}]x")         # anything else stays and is refused
        for value, problem in (("0x42", ""), ("a == 1", ""), ("a <= b", ""), ("x = 3", "assignment"),
                               ("x += 1", "assignment"), ("f(1);", "';'"), ("{1, 2}", "braced")):
            self.assertIn(problem, uc.c_expression_problem(value, braces=False), value)
        trial: dict[str, str] = {}
        dropped: list[str] = []
        with self.assertRaises(ba.AgentFault):                    # refused where it is made, not in the suite
            a2.GeneratorAgent._grounding({"uart_put", "UT_EXPECT_EQ_INT"}, set(), function="uart_put", trial=trial,
                                         dropped=dropped)(a2.GeneratedTests.model_validate({"tests": [
                {"name": "test_uart_put_x", "scenario_ids": [], "evidence_class": "E8",
                 "body": "uart_put(0x42'}]); UT_EXPECT_EQ_INT(1, 1);"}]}))
        self.assertEqual(trial, {})

    def test_a_test_refused_only_by_a_prediction_goes_to_a_trial(self):                # NC-7
        trial: dict[str, str] = {}
        check = a2.GeneratorAgent._grounding({"f", "UT_EXPECT_EQ_INT"}, set(), function="f", trial=trial)
        answer = a2.GeneratedTests.model_validate({"tests": [
            {"name": "test_f_with_helper_init", "scenario_ids": [], "body": "helper_init(); UT_EXPECT_EQ_INT(f(1), 0);",
             "evidence_class": "E8"},
            {"name": "test_f_nothing_checked", "scenario_ids": [], "body": "f(2);", "evidence_class": "E8"}]})
        check(answer)
        self.assertEqual([x.name for x in answer.tests], ["test_f_with_helper_init"])     # kept, for the tool
        self.assertIn("helper_init", trial["test_f_with_helper_init"])
        dropped: list[str] = []                                   # without a trial: refused as before [A2-20]
        with self.assertRaises(ba.AgentFault):
            a2.GeneratorAgent._grounding({"f", "UT_EXPECT_EQ_INT"}, set(), function="f", dropped=dropped)(
                a2.GeneratedTests.model_validate({"tests": [answer.tests[0].model_dump()]}))

    def test_the_trial_keeps_what_the_tool_accepts(self):                              # NC-7
        suite = ("UT_TEST(test_a)\n{\n    UT_EXPECT_EQ_INT(f(1), 1);\n}\n\nUT_TEST(test_b)\n{\n    UT_EXPECT_EQ_INT(g(1), 1);\n}"
                 "\n\nUT_SUITE_BEGIN\n    UT_CASE(test_a)\n    UT_CASE(test_b)\nUT_SUITE_END\n")
        bundle = uc.FileActionBundle(bundle_id="b", tests=["test_a", "test_b"], actions=[uc.FileAction(
            path="tests/t.c", action="create", role="testsuite", content=suite)],
            trial={"test_a": "calls functions that do not exist here: f", "test_b": "calls functions that do not exist "
                   "here: g"}, trial_targets={"test_a": ["LC:f:3"], "test_b": ["LC:g:9"]})
        seen = []

        async def call(role, tool, request, model):
            seen.append(uc.test_sources(uc.suite_of(request.bundle)))
            return uc.VerificationResult(run_id="r", stage="observe", sut_sha256="s", suite_sha256="t", link_ok=True,
                                         decision="complete", tests=[uc.TestResult(name="test_a", status="passed"),
                                                                     uc.TestResult(name="test_b", status="failed")],
                                         test_items={"test_a": ["LC:f:3"], "test_b": []})
        orch = object.__new__(up.Orchestrator)
        orch.cfg, orch.layout, orch.call = up.PipelineConfig(), types.SimpleNamespace(sut_sha256="s", sut_file="a.c"), call
        orch.check_result = lambda *a: None
        s = types.SimpleNamespace(findings=[], trials=[], quarantine={}, failed_tests={}, test_items={}, open_now=set())
        with mock.patch.object(up.uc, "VerifyRequest", lambda **kw: types.SimpleNamespace(**kw)):   # the layout is a stand-in
            out = run(orch._trial(s, bundle))
        self.assertEqual(sorted(seen[0]), ["test_a", "test_b"])                       # built alone: only the trials
        self.assertEqual(out.tests, ["test_a"])                                        # the tool accepted test_a
        self.assertEqual(out.trial, {})
        self.assertEqual([f.kind for f in s.findings], ["false_refusal"])              # NC-5
        self.assertIn("test_b", s.failed_tests)

    def test_recipes_are_templates_of_a_construct(self):                               # NC-2, NC-3
        lines, call = uc.templated(["ut_fake_ISensor ut_dep0;", "Heater ut_obj(ut_dep0);"], "ut_obj.demand",
                                   {"cls": "Heater", "method": "demand", "t0": "ISensor", "fake0": "ut_fake_ISensor"})
        recipe = uc.Recipe(kind="setup", signature="object: ctor(ref interface)", lines=lines, call=call)
        self.assertEqual(recipe.lines, ["@@fake0@@ ut_dep0;", "@@cls@@ ut_obj(ut_dep0);"])
        got = recipe.instantiate({"cls": "Cooler", "method": "need", "t0": "IThermo", "fake0": "ut_fake_IThermo"})
        self.assertEqual(got, (["ut_fake_IThermo ut_dep0;", "Cooler ut_obj(ut_dep0);"], "ut_obj.need"))
        self.assertFalse(recipe.complete({"cls": "X"}))
        with tmpdir() as d:
            (Path(d) / "object.json").write_text(json.dumps([recipe.model_dump()]))
            (Path(d) / "broken.json").write_text("{")
            library = uc.application_recipes(Path(d))
        self.assertEqual([r.made_by for r in library], ["library"])                    # NC-3: reviewed data

    def test_paths_and_outcomes_from_the_platform_facts(self):                          # NC-10, NC-11, NC-12
        windows = uc.PlatformFacts(os="windows", case_sensitive=False, page_bytes=65536, measured="2026-10-24",
                                   outcomes={"crash": [0xC0000005], "abort": [3]})
        linux = uc.PlatformFacts(os="linux", case_sensitive=True, measured="2026-10-24", outcomes={"crash": [-11]})
        self.assertTrue(uc.same_file("C:\\Users\\x\\repo\\Tests\\T.CPP", "tests/t.cpp", windows))
        self.assertTrue(uc.same_file("/home/x/repo/tests/t.cpp", "tests/t.cpp", linux))
        self.assertFalse(uc.same_file("/home/x/repo/tests/T.cpp", "tests/t.cpp", linux))   # Linux tells them apart
        self.assertFalse(uc.same_file("/home/x/repo/mytests/t.cpp", "tests/t.cpp", linux))  # a folder boundary
        self.assertEqual(uc.program_outcome(3, "", windows), "abort")
        self.assertEqual(uc.program_outcome(-11, "", linux), "crash")
        self.assertEqual(uc.program_outcome(4, "UT_WATCHDOG|t\n", windows), "watchdog")
        self.assertEqual(uc.program_outcome(77, "", linux), "unknown")                 # never a pass [NC-12]
        self.assertIn("unknown outcome", a3._exit_text(77, "", linux))
        self.assertEqual(uc.platform_facts({"platform": windows.model_dump()}).page_bytes, 65536)
        self.assertEqual(uc.platform_facts({}).measured, "")                           # the host until measured

    @unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
    def test_the_setup_measures_the_platform(self):                                    # NC-10
        with tmpdir() as d:
            facts = us.measure_platform(_tools().tc, Path(d))
        self.assertTrue(facts["measured"])
        self.assertIn(0, facts["outcomes"]["normal"])
        self.assertTrue(facts["outcomes"].get("crash"))
        self.assertNotIn(0, facts["outcomes"]["crash"])
        self.assertEqual(facts["watchdog_marker"], "UT_WATCHDOG")
        self.assertEqual(facts["case_sensitive"], os.name != "nt" and sys.platform != "darwin")
        uc.PlatformFacts(**facts)                                                       # the manifest's form

    def test_findings_are_counted_across_runs(self):                                   # NC-5
        with tmpdir() as d:
            path = Path(d) / "pipeline_findings.jsonl"
            f = uc.PipelineFinding(kind="pipeline_limitation", signature=uc.finding_signature(
                "pipeline_limitation", "object: ctor(ref interface)", "error at line 12"), subject="object: ctor")
            uc.record_findings(path, [f])
            again = f.model_copy(update={"signature": uc.finding_signature(
                "pipeline_limitation", "object: ctor(ref interface)", "error at line 97")})   # numbers removed
            lines = uc.record_findings(path, [again])
        self.assertEqual(again.count, 2)
        self.assertTrue(lines[0].startswith("2x pipeline_limitation"))

    def test_operators_and_overloads_get_their_own_names(self):                        # NC-4
        self.assertEqual(a1.function_name("Money operator+(const Money& a, const Money& b)"), "operator+")
        self.assertEqual(a1.function_name("Money& Money::operator += (const Money& o)"), "Money::operator+=")
        self.assertEqual(a1.function_name("int F::operator()(int x) const"), "F::operator()")
        self.assertEqual(a1.function_name("double scale(double x, double k)"), "scale")
        functions = [a1.find_functions("int scale(int x, int k) {\n  return x * k;\n}\n"
                                       "double scale(double x, double k) {\n  return x * k;\n}\n"
                                       "bool operator==(const M& a, const M& b) {\n  return a.c == b.c;\n}\n",
                                       "src/s.cpp")][0]
        self.assertEqual([f.name for f in functions], ["scale", "scale", "operator=="])
        layout = uc.LayoutProfile(repo_root="/r", sut_file="src/s.cpp", language="cpp", component_root=".",
                                  writable_test_dir="t", writable_stub_dir="t/s", suite_file="t/x.cpp", sut_sha256="a" * 64,
                                  functions=functions)
        self.assertEqual([uc.function_stem(f, layout) for f in functions], ["scale", "scale_2", "operator_eq"])
        self.assertIs(uc.function_of("scale", layout, functions[1].line_start), functions[1])
        self.assertIs(uc.function_of("scale", layout, functions[1].line_start + 1), functions[1])   # a line inside
        self.assertIs(uc.function_of("scale", layout), functions[0])

    def test_the_object_is_declared_once(self):                                         # A2-21ae, ARCH-20
        kept, dropped = uc.without_redeclarations(["TorqueMap ut_obj; ut_obj.reset();", "int x = 3;",
                                                   "ut_fake_IEncoder ut_dep0;"], {"ut_obj", "ut_dep0"})
        self.assertEqual(kept, ["ut_obj.reset();", "int x = 3;"])
        self.assertEqual(len(dropped), 2)
        self.assertEqual(uc.declared_names("std::vector<int> v{1, 2};"), {"v"})
        self.assertEqual(uc.declared_names("return x;") | uc.declared_names("x = 5;"), set())

    def test_a_failing_test_that_did_not_reach_its_items_has_wrong_inputs(self):       # LA-3
        with tmpdir() as d:
            layout = uc.LayoutProfile(repo_root=d, sut_file="c.c", language="c", component_root=".",
                                      writable_test_dir="t", writable_stub_dir="t/s", suite_file="t/x.c",
                                      sut_sha256="a" * 64, functions=[uc.FunctionInfo(
                                          name="control", qualified_name="control", signature="int control(int x)",
                                          file="c.c", line_start=12, line_end=17)])
            journal = up.Journal("c.c", Path(d))
            sc = uc.Scenario(scenario_id="S-brain-control-2-1", function="control", description="",
                             gap_ids=["BC:control:14:30:T"], made_by="brain:qwen/q", checked=True)
            plan = uc.UTPlan(plan_id="p", layout=layout, stage="coverage", reason="", scenarios=[sc])
            suite = "UT_TEST(test_control_hw)\n{\n    /* S-brain-control-2-1: x */\n    UT_EXPECT_EQ_INT(control(10), 7);\n}\n"
            gap = uc.GapItem(gap_id="BC:control:14:30:T", function="control", metric="BC", line=14)
            result = uc.VerificationResult(
                run_id="r", stage="verify", sut_sha256="a", suite_sha256="b", decision="failures", link_ok=True,
                tests=[uc.TestResult(name="test_control_hw", status="failed", expected="7", actual="10")],
                coverage=uc.CoverageReport(toolchain="t", definitions="d", functions=[uc.FunctionCoverage(
                    name="control", gaps=[gap])]))
            journal.record(2, plan, suite, {}, result, {"BC:control:14:30:T": 1}, layout, [], set())
        entry = journal.entries[-1]
        self.assertEqual(entry.cause, "wrong_inputs")                  # not a wrong expectation: it never got there
        self.assertIn("did not reach BC:control:14:30:T", entry.result)
        self.assertEqual([e.cause for e in journal.entries], ["wrong_inputs"])   # one cause per failing test

    def test_a_verified_result_is_never_lost_by_a_later_round(self):                   # ORC-32, ORC-33
        orch = object.__new__(up.Orchestrator)
        orch.cfg = up.PipelineConfig()
        done = uc.VerificationResult(run_id="r6", stage="verify", sut_sha256="a", suite_sha256="b", decision="complete",
                                     link_ok=True, build_ok=True, verified=True, total=14, passed=14)
        broken = uc.VerificationResult(run_id="r7", stage="verify", sut_sha256="a", suite_sha256="c",
                                       decision="failures", reason="the build did not link")
        s = types.SimpleNamespace(best_result=done, finish=lambda state, reason, last: (state, reason, last.run_id))
        state, reason, run_id = run(orch._stop(s, "safety ceiling of the file reached: 10 min [LA-7]", broken))
        self.assertEqual((state, run_id), ("verified_measured", "r6"))
        self.assertIn("a later round was then stopped", reason)

    def test_every_corpus_case_is_complete(self):                                       # NC-4
        cases = up.corpus_cases()
        self.assertGreaterEqual(len(cases), 25)
        for name, folder, spec in cases:
            self.assertTrue((folder / spec["sut"]).is_file(), name)
            self.assertIn(spec["language"], ("c", "cpp"), name)
            self.assertTrue(spec["construct"], name)
            self.assertIn(spec["expect"]["final_state"], ("verified_measured", "incomplete", "any"), name)


@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
class CorpusAnswerTests(unittest.TestCase):
    """NC-9: known correct answers of the construct corpus pass every check."""

    def _layout(self, folder: Path, spec: dict, d: str) -> uc.LayoutProfile:
        repo = Path(d) / folder.name
        shutil.copytree(folder, repo)
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file=spec["sut"]))
        agent = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(Path(d) / "w")))
        return run(a1.AnalyzerAgent().dependencies(uc.DependencyRequest(
            layout=layout, build=agent.capture(uc.CaptureRequest(layout=layout)))))

    def test_correct_answers_pass_every_check(self):
        checked = 0
        for name, folder, spec in up.corpus_cases():
            answers = spec.get("answers") or {}
            if not answers:
                continue
            with tmpdir() as d:
                for block in answers.get("setup", []):
                    self.assertEqual(a2.GeneratorAgent._block_problem(block["lines"], block["call"]), "", name)
                    checked += 1
                for fake in answers.get("fakes", []):
                    self.assertEqual(a2.GeneratorAgent._block_problem([fake["code"]], fake=True), "", name)
                    checked += 1
                if not (answers.get("scenarios") or answers.get("tests")):
                    continue
                layout = self._layout(folder, spec, d)
                agent = a1.AnalyzerAgent()
                for planned in answers.get("scenarios", []):
                    fn_name = planned["gap_ids"][0].split(":")[1]
                    fn = next(f for f in layout.functions if fn_name in (f.name, f.qualified_name))
                    scenario, reasons = agent._check_planned(
                        a1.PlannedScenario(**planned), fn, set(planned["gap_ids"]), set(), {}, [],
                        agent._constants_for_values(layout), agent._known_names(layout, []), layout)
                    self.assertIsNotNone(scenario, f"{name}: {reasons}")
                    self.assertEqual(scenario.trial, [], f"{name}: {scenario.trial}")
                    checked += 1
                plan = uc.UTPlan(plan_id="p", layout=layout, stage="coverage", reason="")
                _, _, stub_header = a2.render_stubs(plan)
                allowed = a2.GeneratorAgent._allowed_symbols(plan, a2.GeneratorAgent._headers_text(layout), stub_header)
                for test in answers.get("tests", []):
                    trial: dict[str, str] = {}
                    parsed = a2.GeneratedTests.model_validate({"tests": [test]})
                    a2.GeneratorAgent._grounding(allowed, set(), trial=trial)(parsed)
                    self.assertEqual(([x.name for x in parsed.tests], trial), ([test["name"]], {}), name)
                    checked += 1
        self.assertGreaterEqual(checked, 12)


def corpus_lines(layout: uc.LayoutProfile, function: str, texts: list[str]) -> list[str]:
    """The line items (LC:<line>, as per-test coverage names them) of the lines of a function that contain these
    texts."""
    fn = function if isinstance(function, uc.FunctionInfo) else uc.function_of(function, layout)
    lines = (Path(layout.repo_root) / layout.sut_file).read_text(encoding="utf-8").splitlines()
    out = []
    for text in texts:
        hit = next((n for n in range(fn.line_start, fn.line_end + 1) if text in lines[n - 1]), None)
        if hit is None:
            raise AssertionError(f"{function}: no line contains {text!r}")
        out.append(f"LC:{hit}")                                   # per-test items name the line only
    return out


VALUE_FORMS_HEADER = """#pragma once
#include <cstddef>
#include <cstdint>
enum class Mode : std::uint8_t { Off, On };
struct Rec { int a; std::uint8_t b; };
namespace dev { extern int g_level; int read(); }
int f_i8(std::int8_t v);
int f_u8(std::uint8_t v);
int f_i16(std::int16_t v);
int f_u32(std::uint32_t v);
int f_i64(long long v);
int f_bool(bool v);
int f_float(float v);
int f_double(double v);
int f_enum(Mode m);
int f_ref(const std::int32_t &v);
int f_out(std::int32_t &v);
int f_buf(const std::uint8_t *d, std::size_t n);
int f_rec(const Rec *r);
int f_state();
int f_dep();
class Box {
public:
    Box(int start, int step);
    int get() const;
private:
    std::int16_t value_;
    int step_;
};
"""
VALUE_FORMS_SOURCE = """#include "forms.hpp"
static std::uint16_t s_count;
static Mode s_mode;
int f_i8(std::int8_t v) { return v > 0 ? 1 : 0; }
int f_u8(std::uint8_t v) { return v > 0U ? 1 : 0; }
int f_i16(std::int16_t v) { return v > 0 ? 1 : 0; }
int f_u32(std::uint32_t v) { return v > 0U ? 1 : 0; }
int f_i64(long long v) { return v > 0 ? 1 : 0; }
int f_bool(bool v) { return v ? 1 : 0; }
int f_float(float v) { return v > 0.0f ? 1 : 0; }
int f_double(double v) { return v > 0.0 ? 1 : 0; }
int f_enum(Mode m) { return m == Mode::On ? 1 : 0; }
int f_ref(const std::int32_t &v) { return v > 0 ? 1 : 0; }
int f_out(std::int32_t &v) { v += 1; return 0; }
int f_buf(const std::uint8_t *d, std::size_t n) { return (d != nullptr && n > 0U) ? d[0] : -1; }
int f_rec(const Rec *r) { return r != nullptr ? r->a + r->b : 0; }
int f_state() { return s_count > 10U ? (s_mode == Mode::On ? 2 : 1) : 0; }
int f_dep() { return dev::g_level > 0 ? dev::read() : -1; }
Box::Box(int start, int step) : value_(static_cast<std::int16_t>(start)), step_(step) {}
int Box::get() const { return value_ + step_; }
"""


C_FORM_FUNCTIONS = {"f_i8", "f_u8", "f_i16", "f_u32", "f_i64", "f_bool", "f_float", "f_double", "f_enum", "f_buf",
                    "f_rec", "f_state", "f_dep"}
VALUE_FORMS_C_HEADER = """#ifndef FORMS_H
#define FORMS_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef enum { Off, On } Mode;
typedef struct { int a; uint8_t b; } Rec;
extern int g_level;
int read(void);
int f_i8(int8_t v);
int f_u8(uint8_t v);
int f_i16(int16_t v);
int f_u32(uint32_t v);
int f_i64(long long v);
int f_bool(bool v);
int f_float(float v);
int f_double(double v);
int f_enum(Mode m);
int f_buf(const uint8_t *d, size_t n);
int f_rec(const Rec *r);
int f_state(void);
int f_dep(void);
#endif
"""
VALUE_FORMS_C_SOURCE = """#include "forms.h"
static uint16_t s_count;
static Mode s_mode;
int f_i8(int8_t v) { return v > 0 ? 1 : 0; }
int f_u8(uint8_t v) { return v > 0U ? 1 : 0; }
int f_i16(int16_t v) { return v > 0 ? 1 : 0; }
int f_u32(uint32_t v) { return v > 0U ? 1 : 0; }
int f_i64(long long v) { return v > 0 ? 1 : 0; }
int f_bool(bool v) { return v ? 1 : 0; }
int f_float(float v) { return v > 0.0f ? 1 : 0; }
int f_double(double v) { return v > 0.0 ? 1 : 0; }
int f_enum(Mode m) { return m == On ? 1 : 0; }
int f_buf(const uint8_t *d, size_t n) { return (d != NULL && n > 0U) ? d[0] : -1; }
int f_rec(const Rec *r) { return r != NULL ? r->a + r->b : 0; }
int f_state(void) { return s_count > 10U ? (s_mode == On ? 2 : 1) : 0; }
int f_dep(void) { return g_level > 0 ? read() : -1; }
"""


def value_forms(lo: int, hi: int) -> list[str]:
    """Every integer literal form of C and C++ whose value lies in lo..hi."""
    forms = [("0", 0), ("1", 1), ("-1", -1), ("+5", 5), ("0u", 0), ("1U", 1), ("0uL", 0), ("1UL", 1), ("0ull", 0),
             ("1LL", 1), ("0x0", 0), ("0x1F", 31), ("0X1fu", 31), ("07", 7), ("'a'", 97), ("(1 + 2)", 3),
             ("1 << 2", 4), ("100", 100)]
    return [f for f, v in forms if lo <= v <= hi]


def value_form_entries() -> list[dict]:
    """The structured answers of the value-form check: one per form and target."""
    out = []

    def add(function: str, **answer):
        out.append({"function": function, "answer": answer, "reaches": []})
    for fn, lo, hi in (("f_i8", -128, 127), ("f_u8", 0, 255), ("f_i16", -32768, 32767), ("f_u32", 0, 2 ** 32 - 1),
                       ("f_i64", -2 ** 63, 2 ** 63 - 1), ("f_ref", -2 ** 31, 2 ** 31 - 1), ("f_out", -2 ** 31, 2 ** 31 - 1)):
        for v in value_forms(lo, hi):
            add(fn, args=[{"name": "v", "value": v}])
    for v in ("true", "false", "0", "1"):
        add("f_bool", args=[{"name": "v", "value": v}])
    for fn in ("f_float", "f_double"):
        for v in ("0", "1", "-1", "1.0", "1.0f", "-2.5", "1e3", "0.5F", ".5", "2.", "1.5e-3"):
            add(fn, args=[{"name": "v", "value": v}])
    for v in ("Off", "On", "Mode::On", "Mode::Off"):
        add("f_enum", args=[{"name": "m", "value": v}])
        add("f_state", args=[], state=[{"variable": "s_mode", "index": -1, "field": "", "value": v}])
    for v in value_forms(0, 255):
        add("f_buf", args=[{"name": "d", "elements": [v, v]}, {"name": "n", "value": "2"}])
        add("f_rec", args=[{"name": "r", "fields": [{"field": "a", "value": v}, {"field": "b", "value": v}]}])
        add("f_state", args=[], state=[{"variable": "s_count", "index": -1, "field": "", "value": v}])
        add("f_dep", args=[], variables=[{"name": "g_level", "value": v}],
            stubs=[{"stub": "dev::read", "returns": [v, v]}])
        add("f_dep", args=[], variables=[{"name": "dev::g_level", "value": v}],
            stubs=[{"stub": "read", "returns": [v]}])
        add("Box::get", args=[], calls=[{"function": "Box::Box", "args": [v, v]}])
        add("Box::get", args=[], calls=[{"function": "Box::Box", "args": [v]}])   # a value not given: {}
        add("Box::get", args=[], members=[{"member": "value_", "value": v}])
    return out


def schema_problems(node: Any, value: Any, where: str = "") -> list[str]:
    """Where a value breaks the answer schema the brain is given (its enums, lengths and list bounds; a missing
    field is not checked): a correct answer must fit it, or a constrained decoder could never write it."""
    if not isinstance(node, dict):
        return []
    if "anyOf" in node:
        branches = [schema_problems(b, value, where) for b in node["anyOf"]]
        return [] if any(not b for b in branches) else min(branches, key=len)
    if "enum" in node and value not in node["enum"]:
        return [f"{where}: {value!r} is not one of {node['enum'][:8]}"]
    if isinstance(value, str) and isinstance(node.get("maxLength"), int) and len(value) > node["maxLength"]:
        return [f"{where}: {value!r} is longer than {node['maxLength']}"]
    if isinstance(value, list):
        out = [f"{where}: {len(value)} items, at most {node['maxItems']}"] \
            if isinstance(node.get("maxItems"), int) and len(value) > node["maxItems"] else []
        return out + [x for k, v in enumerate(value) for x in schema_problems(node.get("items"), v, f"{where}[{k}]")]
    if isinstance(value, dict):
        props = node.get("properties") or {}
        return [x for k, v in value.items() for x in
                (schema_problems(props[k], v, f"{where}.{k}") if k in props else [f"{where}.{k}: not in the schema"])]
    return []


def check_corpus_case(name: str, folder: Path, spec: dict, d: str) -> list[str]:
    """The structured answers of one construct (answers.structured), as Qwen gives them: Agent1 accepts each (no
    refusal, no trial), the generator builds a test from it, the test compiles, runs, passes, and reaches the lines
    the case names. No brain is asked. Returns the problems ("" none)."""
    entries = (spec.get("answers") or {}).get("structured") or []
    if not entries:
        return []
    repo = Path(d) / name
    shutil.copytree(folder, repo)
    layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file=spec["sut"]))
    comp = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(Path(d) / "w")), router=ba.BrainRouter([DownBrain()], 0.0))
    layout = run(a1.AnalyzerAgent().dependencies(uc.DependencyRequest(
        layout=layout, build=comp.capture(uc.CaptureRequest(layout=layout)))))
    agent = a1.AnalyzerAgent()
    stubs = agent._stubs(layout, None)
    constants, known = agent._constants_for_values(layout), agent._known_names(layout, stubs)
    problems, scenarios, wanted = [], [], {}
    for k, entry in enumerate(entries, 1):
        fn = uc.function_of(entry["function"], layout)
        if fn is not None and entry.get("overload"):                      # the n-th overload of the name, by line
            fn = sorted(uc.overloads(fn, layout), key=lambda f: f.line_start)[entry["overload"] - 1]
        if fn is None:
            problems.append(f"{name}: {entry['function']} is not a function of the layout")
            continue
        evidence = {e["evidence_id"]: uc.EvidenceRef(**e) for e in entry.get("evidence", [])}
        answer = {**dict(gap_ids=["LC:corpus"], name=f"test_corpus_{k}", purpose="the construct", args=[], stubs=[],
                         variables=[], expected_return="", expected_after=[], evidence_ids=[], quotes=[],
                         classification="characterization_gap", conflict="", information_sought=""),
                  **entry["answer"]}
        sut_text = (repo / spec["sut"]).read_text(encoding="utf-8", errors="replace")
        schema = ba.bound_schema(ba.llm_schema(agent._plan_model(                # what a brain is allowed to write
            fn, {"LC:corpus"}, list(evidence.values()), stubs, None, 3,
            agent._structured_lists(fn, layout, stubs, sut_text)).model_json_schema()))
        problems += [f"{name}: {entry['function']}: the schema refuses it: {x}"
                     for x in schema_problems(schema, {"scenarios": [answer]})]
        try:                                         # and the request that carries the schema can be sent
            ba.json_dumps({"model": "m", "messages": [{"role": "user", "content": "x"}], "response_format": {
                "type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}},
                max_bytes=ba.OllamaHTTP.MAX_REQUEST_BYTES, sort_keys=False, max_depth=ba.OllamaHTTP.MAX_REQUEST_DEPTH)
        except ba.AgentFault as exc:
            problems.append(f"{name}: {entry['function']}: the request with its schema cannot be sent: {exc.message}")
        scenario, reasons = agent._check_planned(a1.PlannedScenario.model_validate(answer), fn, {"LC:corpus"}, set(),
                                                 evidence, stubs, constants, known, layout)
        if scenario is None or scenario.trial:
            problems.append(f"{name}: {entry['function']}: refused: {reasons or scenario.trial}")
            continue
        scenario.scenario_id, scenario.function_line = f"S-corpus-{k}", fn.line_start
        scenarios.append(scenario)
        wanted[answer["name"]] = (corpus_lines(layout, fn, entry.get("reaches", [])),
                                  entry.get("contains", []))
    if not scenarios:
        return problems

    async def no_brains(*a, **kw):
        return []
    gen = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="deterministic", max_auto_tests_per_function=0))
    async def no_values(*a, **kw):
        return {}
    gen._brain_values = no_values
    with mock.patch.object(uc, "brains_for", no_brains):
        bundle = run(gen.generate_bundle(uc.GenerationRequest(plan=uc.UTPlan(
            plan_id="corpus", layout=layout, stage="coverage", scenarios=scenarios, stubs=stubs))))
    sources = uc.test_sources(uc.suite_of(bundle))
    for test, (_, contains) in wanted.items():
        if test not in sources:
            problems.append(f"{name}: {test}: not built - {'; '.join(n for n in bundle.notes if 'not built' in n)[:300]}")
        for text in contains:
            if test in sources and text not in sources[test]:
                problems.append(f"{name}: {test}: the test does not contain {text!r}: {sources[test][-400:]}")
    gate = comp.execute(uc.VerifyRequest(layout=layout, stage="gate", bundle=bundle))
    for f in gate.quarantined:
        problems.append(f"{name}: {f.affected_test or 'suite'}: does not compile: {f.diagnostic[:300]}")
    if any(p.startswith(f"{name}:") and "compile" in p for p in problems):
        return problems
    obs = comp.execute(uc.VerifyRequest(layout=layout, stage="observe", bundle=bundle,
                                        measure_tests=[t for t in wanted if t in sources]))
    status = {t.name: t for t in obs.tests}
    for test, (lines, _) in wanted.items():
        if test not in sources:
            continue
        t = status.get(test)
        if t is None or t.status != "passed":
            problems.append(f"{name}: {test}: {t.status if t else 'did not run'} {t.message[:200] if t else obs.reason[:200]}")
            continue
        missing = [i for i in lines if i not in set(obs.test_items.get(test, []))]
        if missing:
            problems.append(f"{name}: {test}: does not reach {missing} (it reached {sorted(obs.test_items.get(test, []))[:12]})")
    return problems


@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
class CorpusStructuredTests(unittest.TestCase):
    """The construct corpus in the form Qwen answers in (structured): every construct is built, compiled, run and
    reaches its lines - without a brain, in minutes. A pipeline failure is first reduced to a construct: covered here,
    it is the brain's choice; not covered, it gets a case here first (the rule of Test/constructs/README.md).
    UT_CORPUS=name1,name2 runs only those cases."""

    def test_every_construct_is_built_compiled_and_run(self):
        only = [x for x in os.environ.get("UT_CORPUS", "").split(",") if x]
        problems, covered, missing = [], [], []
        for name, folder, spec in up.corpus_cases(only or None):
            if not (spec.get("answers") or {}).get("structured"):
                missing.append(name)
                continue
            with tmpdir() as d:
                found = check_corpus_case(name, folder, spec, d)
            problems += found
            covered.append(name + (" - FAILS" if found else ""))
        print("\nConstruct corpus, structured: " + ", ".join(covered) + (
            ("\nwithout a structured answer: " + ", ".join(missing)) if missing else ""))
        self.assertEqual(problems, [], "\n" + "\n".join(problems))
        self.assertEqual(missing, [], "every construct has a structured answer")

    def test_every_value_form_of_every_target_is_accepted(self):
        """A check must never refuse a correct value: every C/C++ literal form within the target's range, for every
        kind of target (argument, reference, buffer element, field, state, member, constructor value, stub return,
        stub variable), is accepted, built and compiled - checked by the grammar, not by the cases one meets."""
        with tmpdir() as d:
            folder = Path(d) / "src_forms"
            folder.joinpath("include").mkdir(parents=True)
            folder.joinpath("src").mkdir()
            folder.joinpath("include/forms.hpp").write_text(VALUE_FORMS_HEADER, encoding="utf-8")
            folder.joinpath("src/forms.cpp").write_text(VALUE_FORMS_SOURCE, encoding="utf-8")
            spec = {"sut": "src/forms.cpp", "answers": {"structured": value_form_entries()}}
            problems = check_corpus_case("value_forms", folder, spec, d)
            folder = Path(d) / "src_forms_c"                                 # and C: its own types and forms
            folder.joinpath("include").mkdir(parents=True)
            folder.joinpath("src").mkdir()
            folder.joinpath("include/forms.h").write_text(VALUE_FORMS_C_HEADER, encoding="utf-8")
            folder.joinpath("src/forms.c").write_text(VALUE_FORMS_C_SOURCE, encoding="utf-8")
            c_only = [json.loads(json.dumps(e).replace("dev::", "")) for e in value_form_entries()
                      if e["function"] in C_FORM_FUNCTIONS and "Mode::" not in json.dumps(e)]
            problems += check_corpus_case("value_forms_c", folder, {"sut": "src/forms.c",
                                                                    "answers": {"structured": c_only}}, d)
        self.assertEqual(problems, [], "\n" + "\n".join(problems))


@full_run
@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
@unittest.skipUnless(HAVE_FASTMCP and HAVE_LANGGRAPH, "needs fastmcp and langgraph")
class CorpusRunTests(unittest.TestCase):
    """NC-4, NC-13: every construct with the scripted brain - the run ends, and every item it did not cover has a
    final status with a reason; where the case states its expected result, the run reaches it."""

    def test_every_construct(self):
        outcomes = []
        for name, folder, spec in up.corpus_cases():
            with tmpdir() as d, brain(UTBrain()):
                repo = Path(d) / name
                shutil.copytree(folder, repo)
                cfg = Path(d) / "ut_config.json"
                raw = json.loads((SRC / "ut_config.json").read_text(encoding="utf-8"))
                raw["viewer"]["enabled"] = False
                raw["pipeline"].update(output_dir=str(Path(d) / "out"), deliver_tests_to_repo=False,
                                       agents={r: {"transport": "inprocess"} for r in
                                               ("analyzer", "generator", "compilation", "corrector", "kb")})
                raw["compilation"].update(tools_manifest=str(uc.manifest_path(SRC / "ut_config.json")),
                                          work_dir=str(Path(d) / "w"))
                raw["kb"] = {"db_path": str(Path(d) / "kb.sqlite3"), "resolutions_path": str(Path(d) / "r.sqlite3")}
                cfg.write_text(json.dumps(raw), encoding="utf-8")
                result = run(up.Orchestrator(cfg).run(uc.PipelineRequest(repo_root=str(repo), sut_file=spec["sut"])))
                keep = SRC / "ut_output" / "corpus" / name       # the report and journal of every case, to read later
                shutil.rmtree(keep, ignore_errors=True)
                keep.mkdir(parents=True, exist_ok=True)
                out = Path(result.output_dir) if result.output_dir else None
                for f in ([out / "report.md", out / "result.json"] + sorted((out / "journal").glob("*.md"))
                          if out and out.is_dir() else []):
                    if f.is_file():
                        shutil.copy2(f, keep / f.name)
            outcomes.append(f"{name}: {result.final_state} - {result.reason[:120]}")
            if result.final_state == "incomplete" and result.item_status:
                for st in result.item_status:
                    self.assertTrue(st.reason or st.status == "undetermined", f"{name}: {st}")
            expected = spec["expect"]["final_state"]
            if expected != "any":
                self.assertEqual(result.final_state, expected, f"{name}: {result.reason}")
            for item, status in spec["expect"].get("statuses", {}).items():
                self.assertIn(status, [s.status for s in result.item_status if s.item_id.startswith(item)
                                       or s.function == item], name)
        print("\nConstruct corpus [NC-4]:\n" + "\n".join(outcomes))


class UssPilotFixTests(unittest.TestCase):
    """From the USS pilot run: else-if chains, the false branch of an if that returns, a wrong operator as a defect
    candidate (A1-37, A1-37f), quotes written as text (A1-PL3, A1-PL4) and project stubs driven through their own
    controls (A1-DA7a, A2-21ak, A3-FZ13)."""
    BODY = ("int pick(int a)\n{\n    if (a > 10) {\n        return 1;\n    } else if (a > 5) {\n        return 2;\n"
            "    } else {\n        return 3;\n    }\n}\n")

    def fn(self, name="pick", start=1, end=10, param="a", ptype="int"):
        return uc.FunctionInfo(name=name, qualified_name=name, signature="", file="a.c", line_start=start,
                               line_end=end, params=[uc.ParamInfo(name=param, type=ptype, kind="int")])

    def test_an_else_if_branch_needs_every_earlier_condition_false(self):            # A1-37
        fn = self.fn()
        self.assertEqual(a1.enclosing_conditions(self.BODY, fn, 4), ["(a > 10) is true"])
        self.assertEqual(a1.enclosing_conditions(self.BODY, fn, 6), ["(a > 10) is false", "(a > 5) is true"])
        self.assertEqual(a1.enclosing_conditions(self.BODY, fn, 8), ["(a > 10) is false", "(a > 5) is false"])

    def test_the_false_branch_of_an_if_that_returns_is_the_rest_of_the_block(self):  # A1-37
        lines = ["int f(int d)", "{", "    if (d > 3) {", "        return 1;", "    }", "    d++;", "    return 0;", "}"]
        self.assertEqual(a1.block_lines(lines, 3, "F"), (6, 7, 0))
        lines[3] = "        d--;"                                                      # does not return: unknown
        self.assertIsNone(a1.block_lines(lines, 3, "F"))

    def test_a_wrong_operator_is_a_defect_candidate(self):                             # A1-37f
        fn = self.fn("level_valid", 3, 10, "d", "int32_t")
        constants = {"MIN_LEVEL": 20, "MAX_LEVEL": 4000}
        lines = ["", "int level_valid(int32_t d)", "{", "    /* inside sensor range */",
                 "    if ((d >= MIN_LEVEL) || (d <= MAX_LEVEL)) {", "        return 1;", "    }", "    return 0;", "}"]
        wrong = "(d >= MIN_LEVEL) || (d <= MAX_LEVEL)"
        proof = a1.prove_infeasible(wrong, "DC", None, "F", fn, constants)
        self.assertTrue(proof)
        d = a1.defect_candidate(wrong, "DC", None, "F", fn, constants, [], "", proof, lines, 5)
        self.assertIsNotNone(d)
        self.assertEqual(d.change, wrong + " -> (d >= MIN_LEVEL) && (d <= MAX_LEVEL)")
        self.assertIn("inside sensor range", d.evidence)
        right = "(d >= MIN_LEVEL) && (d <= MAX_LEVEL)"                                 # correct: nothing proven
        self.assertEqual(a1.prove_infeasible(right, "DC", None, "F", fn, constants), "")
        redundant = "(d >= 0) || (d < 0)"                                              # swapped it is never true
        proof = a1.prove_infeasible(redundant, "DC", None, "F", fn, constants)
        self.assertTrue(proof)
        self.assertIsNone(a1.defect_candidate(redundant, "DC", None, "F", fn, constants, [], "", proof, lines, 5))

    def test_a_quote_written_as_text_is_its_evidence_id_and_words(self):            # A1-PL3, ARCH-20b
        for text in ("EV-f-1: returns -1 on error", "[EV-f-1] returns -1 on error", 'EV-f-1 "returns -1 on error"'):
            q = a1.PlannedQuote.model_validate(text)
            self.assertEqual((q.evidence_id, q.text), ("EV-f-1", "returns -1 on error"), text)
        self.assertEqual(a1.PlannedQuote.model_validate("no id at all").evidence_id, "")
        self.assertRegex(a1.PLAN_TASK, r'"quotes": \[\{+"evidence_id": "EV-f-1", "text": "\w')   # one filled

    @unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
    def test_a_project_stub_is_driven_through_its_controls(self):                     # A1-DA7a, A2-21ak, A3-FZ13
        folder = up.CONSTRUCTS / "c_project_stub_controls"
        with tmpdir() as d:
            layout = CorpusAnswerTests._layout(None, folder, {"sut": "src/height.c"}, d)
            [ps] = layout.dependencies.project_stubs
            self.assertEqual((ps.file, ps.header, ps.reset), ("stubs/hal_stub.c", "stubs/hal_stub.h", "sim_reset"))
            self.assertEqual({c.name for c in ps.controls}, {"sim_set_echo_us", "sim_set_no_echo", "sim_reset"})
            self.assertEqual(set(ps.serves), {"hal_trigger", "hal_echo_width_us"})
            fn = next(f for f in layout.functions if f.name == "height_distance_mm")      # through height_ping_us
            self.assertIn("sim_set_echo_us", a2.project_stub_text(fn, layout))
            sut = (Path(d) / "c_project_stub_controls" / "src/height.c").read_text().splitlines()
            drivers = {c.name: values for c, values in a2.project_drivers(fn, layout, sut, {})}
            self.assertEqual(drivers["sim_set_no_echo"], [None, 0, 1])
            self.assertIn(None, drivers["sim_set_echo_us"])
            self.assertTrue(all(v is None or v >= 0 for v in drivers["sim_set_echo_us"]))   # unsigned
            plan = uc.UTPlan(plan_id="p", layout=layout, stage="coverage", reason="")
            _, source, header = a2.render_stubs(plan)
            self.assertIn('#include "hal_stub.h"', header)
            self.assertIn("sim_reset();", source)
            repo = Path(d) / "c_project_stub_controls"
            self.assertEqual(a3.reused_stub_sources(layout, [], repo), ["stubs/hal_stub.c"])   # test and fuzz build
            self.assertEqual(a3.project_stub_includes(layout, repo), [f"-I{repo / 'stubs'}"])

    @unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
    def test_a_project_stub_without_controls_is_replaced(self):                       # A1-DA7a
        folder = up.CONSTRUCTS / "c_project_stub_controls"
        with tmpdir() as d:
            work = Path(d) / "src_copy"
            shutil.copytree(folder, work / "c_project_stub_controls")
            stub = work / "c_project_stub_controls" / "stubs"
            (stub / "hal_stub.h").write_text("#ifndef HAL_STUB_H\n#define HAL_STUB_H\n#endif\n")
            (stub / "hal_stub.c").write_text('#include "hal.h"\nvoid hal_trigger(void) { }\n'
                                             "uint32_t hal_echo_width_us(void) { return 0u; }\n")
            layout = CorpusAnswerTests._layout(None, work / "c_project_stub_controls", {"sut": "src/height.c"}, d)
            self.assertEqual(layout.dependencies.project_stubs, [])
            hal = [e for e in layout.dependencies.entries if e.name in ("hal_trigger", "hal_echo_width_us")]
            self.assertTrue(hal and all(e.decision == "stub" and not e.existing_stub for e in hal), hal)
            self.assertTrue(any("A1-DA7a" in n for n in layout.dependencies.notes))

    @unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
    def test_items_after_a_stub_no_test_drives_have_one_cause(self):                  # LA-3a
        folder = up.CONSTRUCTS / "c_project_stub_controls"
        with tmpdir() as d:
            layout = CorpusAnswerTests._layout(None, folder, {"sut": "src/height.c"}, d)
            suite = ("UT_TEST(test_height_distance_mm_zero) {\n    int32_t mm = 0;\n    height_distance_mm(&mm);\n}\n"
                     "UT_TEST(test_height_ping_us_zero) {\n    height_ping_us();\n}\n")
            bundle = uc.FileActionBundle(bundle_id="b", actions=[uc.FileAction(
                path="tests/test_height.c", action="create", role="testsuite", content=suite)])
            gaps = lambda fn, lines: [types.SimpleNamespace(gap_id=f"LC:{fn}:{n}", line=n) for n in lines]
            last = types.SimpleNamespace(coverage=types.SimpleNamespace(functions=[
                types.SimpleNamespace(name="height_ping_us", gaps=gaps("height_ping_us", [15, 16, 18])),
                types.SimpleNamespace(name="height_distance_mm", gaps=gaps("height_distance_mm", [27, 29, 30, 31]))]))
            s = types.SimpleNamespace(best_bundle=bundle, bundle=None, workspace=str(Path(d) / "c_project_stub_controls"),
                                      proofs={}, journal=up.Journal("src/height.c", Path(d)), findings=[], iterations=[1],
                                      item_status=[], defects={})
            orch = up.Orchestrator.__new__(up.Orchestrator)
            orch.layout = layout
            shared = orch._stubs_not_driven(s, last)
            self.assertEqual(shared, {"LC:height_ping_us:15", "LC:height_ping_us:16", "LC:height_ping_us:18",
                                      "LC:height_distance_mm:27", "LC:height_distance_mm:29",
                                      "LC:height_distance_mm:30", "LC:height_distance_mm:31"})
            self.assertEqual(len(s.journal.entries), 2)                                    # one per function
            entry = s.journal.cause_of("LC:height_distance_mm:30")
            self.assertEqual(entry.cause, "stub_not_driven")
            self.assertIn("(through height_ping_us)", entry.result)
            self.assertEqual([f.kind for f in s.findings], ["pipeline_limitation"] * 2)
            s.item_status = [uc.ItemStatus(item_id=i, function=i.split(":")[1], status="pipeline_limitation",
                                           owner="pipeline", reason="r") for i in sorted(shared)]
            self.assertEqual(up.Orchestrator._open_summary(s), "7 open - 7 stub_not_driven")
            suite = suite.replace("    int32_t mm = 0;", "    int32_t mm = 0;\n    sim_set_echo_us(8746u);")
            bundle.actions[0].content = suite.replace("    height_ping_us();", "    sim_set_no_echo(true);\n"
                                                                                  "    height_ping_us();")
            s.journal, s.findings = up.Journal("src/height.c", Path(d)), []
            self.assertEqual(orch._stubs_not_driven(s, last), set())                       # driven: not this cause

    def test_a_failed_build_names_its_first_error(self):                               # A3-FZ13
        out = "cc -c x.c\nwarning: unused\n/usr/bin/ld: x.o: undefined reference to `hal_micros'\nmore\nend"
        self.assertIn("undefined reference to `hal_micros'", a3._first_message(out))


class TargetsAndFamiliesTests(unittest.TestCase):
    """A1-PL3a, ARCH-20d, ARCH-19b, A1-PL17: the brain names targets, a cut answer is split up, and a family of
    similar functions is planned once."""
    def setUp(self):
        self.tmp = tmpdir()
        self.repo = Path(self.tmp.name) / "c_function_family"
        shutil.copytree(up.CONSTRUCTS / "c_function_family", self.repo)
        self.agent = a1.AnalyzerAgent()
        self.layout = self.agent.discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/doors.c"))
        self.text = (self.repo / "src/doors.c").read_text()

    def tearDown(self):
        self.tmp.cleanup()

    def fn(self, name):
        return next(f for f in self.layout.functions if f.name == name)

    def gaps(self, name):
        """The open items of one door after the rule round: its main path and its error branch."""
        f = self.fn(name)
        line = {k: f.line_start + k for k in range(20)}
        lc = lambda k: uc.GapItem(gap_id=f"LC:{name}:{line[k]}", function=name, metric="LC", line=line[k])
        return [lc(k) for k in (8, 9, 10, 11, 12, 17)] + [
            uc.GapItem(gap_id=f"BC:{name}:{line[13]}:9:T", function=name, metric="BC", line=line[13], column=9),
            lc(14), lc(15)]

    def test_items_of_one_path_form_one_target(self):                                  # A1-PL3a
        targets = a1.make_targets(self.fn("door_fl_receive"), self.gaps("door_fl_receive"), self.text, self.layout)
        self.assertEqual([t.tid for t in targets], ["T1", "T2"])
        main, error = targets
        self.assertEqual((main.counts, error.counts), ({"LC": 6}, {"BC": 1, "LC": 2}))   # the outcome enters its block
        self.assertEqual(error.conditions, ["(fl_open != 0u && fl_locked != 0u) is true"])
        answer = a1.PlannedScenario.model_validate(planned(gap_ids=[], targets=["t2: lines 25-27"]))
        fixes = a1.expand_targets(answer, {t.tid: t for t in targets})
        self.assertEqual((answer.targets, sorted(answer.gap_ids)), (["T2"], sorted(error.items)))
        self.assertEqual(fixes, ["targets: 't2: lines 25-27' -> 'T2'"])
        schema = ba.llm_schema(self.agent._plan_model(self.fn("door_fl_receive"), set(main.items), [], [],
                                                      ["T1", "T2"]).model_json_schema())
        item = schema["properties"]["scenarios"]["items"]["properties"]
        self.assertEqual((item["targets"]["items"]["enum"], item["targets"]["maxItems"]), (["T1", "T2"], 3))
        self.assertNotIn("gap_ids", item)                                                   # never enumerated
        self.assertEqual([item[k]["maxLength"] for k in ("name", "purpose", "information_sought", "conflict")],
                         [63, 160, 160, 300])                                         # short texts [A1-PL3, ARCH-20c]
        rambling = planned(targets=["T1"], name="test_door_fl_receive_valid", purpose="a valid frame",
                           information_sought="The result will be observed during testing. " * 20)
        bounded = ba.bound_schema(schema, 2)
        fixes = []
        kept = ba.apply_bounds(bounded, {"scenarios": [rambling]}, fixes)["scenarios"]
        self.assertEqual(len(kept), 1)                                                    # cut, not dropped
        self.assertEqual(len(kept[0]["information_sought"]), 160 - 1)                   # cut, the trailing blank too
        self.assertIn("cut to 160", fixes[0])

    def test_a_cut_planning_answer_is_split_up(self):                                   # ARCH-19b
        class Cut(UTBrain):
            async def generate(self, task, context, schema, validate):
                if task.startswith("Plan unit-test scenarios") and len(self.generate_calls) == 0:
                    self.generate_calls.append(task)
                    fault = ba.AgentFault("MODEL_OUTPUT_INVALID", "the answer was stopped")
                    fault.truncated, fault.ended, fault.partial = True, "a repeated block", ""
                    raise fault
                return await super().generate(task, context, schema, validate)
        name = "door_fl_receive"
        b = Cut(plans={name: [planned(gap_ids=["T1"], name="test_door_fl_receive_valid", args=[
            {"name": "data", "value": "&buf"}, {"name": "len", "value": "4u"}],
            setup=["uint8_t buf[4] = {DOOR_FL_ID, 0u, 0u, 0u};"])]})
        self.agent.cfg.carry_families = False
        with brain(b):
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps(name)),
                                                      iteration=2)))
        asked = [t for t in b.generate_calls if t.startswith("Plan unit-test scenarios")]
        self.assertEqual(len(asked), 3, plan.planning)               # cut, then smaller: never the same request
        self.assertIn("at most 2 scenarios", asked[0])
        self.assertIn("at most 1 scenarios", asked[1])
        self.assertTrue(any("ARCH-19b" in n for n in plan.planning), plan.planning)
        self.assertTrue(any(s.function == name and s.made_by.startswith("brain:") for s in plan.scenarios))

    def test_a_family_is_planned_once(self):                                            # A1-PL17
        fl = planned(gap_ids=["T1"], name="test_door_fl_receive_valid_frame", purpose="a valid frame of the door",
                     args=[{"name": "data", "value": "&buf"}, {"name": "len", "value": "4u"}],
                     setup=["uint8_t buf[4] = {DOOR_FL_ID, 0u, 7u, 1u};"])
        b = UTBrain(plans={"door_fl_receive": [fl]})
        gaps = self.gaps("door_fl_receive") + self.gaps("door_fr_receive")
        with brain(b):
            first = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, gaps),
                                                       iteration=2)))
            self.assertEqual(first.deferred, ["door_fr_receive"])              # waits for its family's first member
            self.assertTrue(any("A1-PL17" in n for n in first.planning), first.planning)
            second = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=measured(
                self.layout, self.gaps("door_fr_receive")), iteration=3)))
        carried = [s for s in second.scenarios if s.function == "door_fr_receive" and s.scenario_id.startswith("S-carried")]
        self.assertEqual(len(carried), 1, second.planning)
        self.assertIn("uint8_t buf[4] = {DOOR_FR_ID, 0u, 7u, 1u};", carried[0].setup)       # names mapped
        self.assertTrue(set(carried[0].gap_ids) <= {g.gap_id for g in self.gaps("door_fr_receive")})
        self.assertTrue(carried[0].made_by.startswith("brain:"))
        asked = [t for t in b.generate_calls if "door_fr_receive" in t]
        self.assertEqual(len(asked), 1)                       # only for the target the carried one does not aim at
        self.assertNotIn("door_fr_receive", " ".join(t for t in b.generate_calls[:1]))


# ============================================================================ 9. requirements and skill
class RequirementsTests(unittest.TestCase):
    REQ = DOCS / "unittest_generator_requirments.txt"
    ID = re.compile(r"\b(?:GEN-\d+|ORC-\d+[a-z]?|ARCH-\d+|TP-\d+|A1-(?:DA|PL|RQ)?\d+[a-z]?|KP-\d+|A2-\d+[a-z]?|"
                    r"A3-(?:PR|OBS|CL|BD|FZ|HW)?\d+[a-z]?|A4-\d+[a-z]?|KB-\d+[a-z]?|SC-\d+[a-z]?|COV-\d+|VIEW-\d+|"
                    r"LA-\d+|BP-\d+|NC-\d+)\b")

    def test_every_cited_requirement_exists(self):
        text = self.REQ.read_text(encoding="utf-8")
        defined = set(re.findall(r"^(" + self.ID.pattern[2:-2] + r")\.", text, re.M))
        defined |= set(re.findall(r"^(" + self.ID.pattern[2:-2] + r")\b", text, re.M))
        missing = {}
        for path in sorted(SRC.glob("*.py")) + sorted(ROOT.glob(".github/**/*.md")):
            if path.name == Path(__file__).name:
                continue
            for cited in set(self.ID.findall(path.read_text(encoding="utf-8"))):
                if cited not in defined:
                    missing.setdefault(cited, []).append(path.name)
        self.assertEqual(missing, {}, "requirement IDs cited in code but not defined")

    def test_skill_points_to_the_requirements(self):
        skill = ROOT / ".github" / "skills" / "ut-agent-builder" / "SKILL.md"
        if not skill.is_file():
            self.skipTest("copy SKILL.md to .github\\skills\\ut-agent-builder\\ to enable the skill")
        text = skill.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: ut-agent-builder\n"))
        for link in re.findall(r"\]\(([^)#]+)\)", text):
            self.assertTrue((skill.parent / link).resolve().exists(), link)

    def test_brains_by_access_not_by_installation(self):
        """ARCH-1a, ARCH-1b: every Claude model first, Qwen3 8B as the fallback, the same for every agent."""
        settings = ba.Settings.model_validate(json.loads((SRC / "ut_config.json").read_text())["brain"])
        router = ba.build_router(settings)
        self.assertEqual(router.names, ["opus", "fable", "sonnet", "haiku", "qwen_remote", "qwen"])
        self.assertEqual([b.model_id for b in router.brains[:4]],
                         ["claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5", "claude-haiku-5-5"])
        for role, (name, script) in up.AGENTS.items():
            agent = sys.modules[script[:-3]].build_agent(ba.Settings())
            self.assertIn(agent.allowed_brains, ([], list(ba.BRAIN_NAMES)), name)   # the KB uses no brain
        for path in sorted(SRC.glob("*.py")) + [DOCS / "ORCHESTRATION.md", SRC / "unittest_generator.bat"]:
            if path.name != Path(__file__).name:
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"(?i)\bcustomer|laptop", path.name)

    def test_one_python_file_per_agent(self):
        with tmpdir() as d, contextlib.chdir(d):
            self._check_agents()

    def _check_agents(self):
        for role, (name, script) in up.AGENTS.items():                            # ARCH-10
            module = sys.modules[script[:-3]]
            self.assertTrue(hasattr(module, "build_agent"), script)
            agent = module.build_agent(ba.Settings())
            self.assertEqual(agent.name, name)
            self.assertTrue(agent.contract_catalog()["tools"])


class RequirementDocumentTests(unittest.TestCase):
    """A1-RQ1 .. A1-RQ6: requirement documents given for a run - read, copied, linked and reported."""
    def setUp(self):
        self.tmp = tmpdir()
        self.d = Path(self.tmp.name)
        self.repo = copy_pilot("c_pilot", self.d)
        (self.repo / "docs").mkdir()
        (self.repo / "docs/requirements.md").write_text("- REQ-TH-20: clamp_percent keeps 0..99 only.\n")
        self.outside = self.d / "spec"
        self.outside.mkdir()
        (self.outside / "srs.csv").write_text("Req ID;Requirement\nREQ-TH-20;clamp_percent limits the value to 0..100\n"
                                              "REQ-TH-21;select_mode returns MODE_ECO when nobody is home\n")
        (self.outside / "notes.md").write_text("# Notes\n\nNothing with an ID here.\n")
        (self.outside / "srs.docx").write_bytes(b"PK\x03\x04 not read")

    def tearDown(self):
        self.tmp.cleanup()

    def test_documents_are_copied_unchanged_and_what_cannot_be_read_is_reported(self):
        target = self.d / "out" / "requirements"
        docs, notes = up.gather_requirements([str(self.outside), "docs/requirements.md", "missing.reqif"],
                                             self.repo, target)
        names = sorted(doc.name for doc in docs)
        self.assertEqual(names, ["requirements/notes.md", "requirements/requirements.md", "requirements/srs.csv"])
        csv_doc = next(doc for doc in docs if doc.name.endswith("srs.csv"))
        self.assertEqual(Path(csv_doc.path).read_bytes(), (self.outside / "srs.csv").read_bytes())    # A1-RQ3
        self.assertEqual(csv_doc.sha256, uc.file_sha256(self.outside / "srs.csv"))
        self.assertTrue(any("srs.docx" in n and "A1-RQ2" in n for n in notes), notes)
        self.assertTrue(any("missing.reqif" in n and "does not exist" in n for n in notes), notes)  # A1-RQ6
        self.assertFalse(any(p.name == "srs.csv" for p in self.repo.rglob("*")))         # never into the repository

    def test_documents_per_repository_in_the_configuration(self):
        raw = {"requirements": {"_comment": "x", "https://git.example.com/team/fw.git/": ["docs/srs.reqif"],
                                str(self.repo): ["docs/requirements.md"]}}
        self.assertEqual(up.configured_requirements(raw, "https://git.example.com/team/fw"), ["docs/srs.reqif"])
        self.assertEqual(up.configured_requirements(raw, str(self.repo) + "/"), ["docs/requirements.md"])
        self.assertEqual(up.configured_requirements(raw, "https://git.example.com/team/other"), [])

    def test_given_documents_are_e1_evidence_and_win_a_conflict(self):
        docs, _ = up.gather_requirements([str(self.outside)], self.repo, self.d / "out" / "requirements")
        agent = a1.AnalyzerAgent()
        layout = agent.discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/thermostat.c",
                                                   requirement_documents=docs))
        self.assertEqual([d.name for d in layout.given_requirements], ["requirements/srs.csv"])
        notes = " | ".join(layout.requirement_notes)
        self.assertIn("notes.md", notes)                                                    # no requirement ID
        self.assertIn("A1-RQ6", notes)
        self.assertIn("conflict: REQ-TH-20 is stated differently in requirements/srs.csv:2", notes)   # A1-RQ4
        self.assertIn("docs/requirements.md:1", notes)
        ev = agent.collect_evidence(layout, "clamp_percent").evidence
        e1 = [e for e in ev if e.evidence_class == "E1"]
        self.assertEqual([(e.source, e.line) for e in e1], [("requirements/srs.csv", 2)])  # the given text is used
        self.assertIn("0..100", e1[0].text)

    def test_status_per_function_counts_only_asserting_tests_that_passed(self):
        docs, _ = up.gather_requirements([str(self.outside / "srs.csv")], self.repo, self.d / "out" / "requirements")
        agent = a1.AnalyzerAgent()
        layout = agent.discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/thermostat.c",
                                                   requirement_documents=docs))
        e1 = next(e for e in agent.collect_evidence(layout, "clamp_percent").evidence if e.evidence_class == "E1")
        suite = (f"/* evidence: E1 */\nUT_TEST(test_clamp_high)\n{{\n    /* S-1: high [{e1.evidence_id}] */\n"
                 f"    UT_EXPECT_EQ_INT(clamp_percent(150), 100);\n}}\n\n"
                 f"/* evidence: E8 */\nUT_TEST(test_clamp_seen)\n{{\n    /* S-2: seen [{e1.evidence_id}] */\n"
                 f"    UT_EXPECT_EQ_INT(clamp_percent(5), 5);\n}}\n")
        status = {f.function: f for f in agent.requirement_status(uc.RequirementStatusRequest(
            layout=layout, suite=suite, passed=["test_clamp_high", "test_clamp_seen"])).functions}
        self.assertEqual(status["clamp_percent"].requirements,
                         ["REQ-TH-20 (rule) requirements/srs.csv:2: asserted by test_clamp_high"])
        self.assertTrue(status["clamp_percent"].stated)
        failed = {f.function: f for f in agent.requirement_status(uc.RequirementStatusRequest(
            layout=layout, suite=suite, passed=[])).functions}
        self.assertIn("not asserted by a test", failed["clamp_percent"].requirements[0])   # a failed test counts not
        (self.repo / "src/plain.c").write_text("int plain(int x)\n{\n    return x + 1;\n}\n")
        plain = agent.discover(uc.DiscoverRequest(repo_root=str(self.repo), sut_file="src/plain.c"))
        listed = agent.requirement_status(uc.RequirementStatusRequest(layout=plain)).functions
        self.assertEqual([(f.function, f.requirements, f.stated) for f in listed], [("plain", [], False)])

    def test_mcp_clients_name_documents_inside_the_repository_only(self):
        tools = {t.name: t for t in up.PipelineAgent().tools()}
        for name, model in (("run_pipeline", uc.PipelineRequest(repo_root=str(self.repo), sut_file="src/thermostat.c",
                                                                 requirements=[str(self.outside / "srs.csv")])),
                            ("run_repository", uc.RepositoryRequest(repository=str(self.repo), sut_files=["src/a.c"],
                                                                    requirements=["../spec/srs.csv"]))):
            with self.assertRaises(ba.ContractRejected):
                tools[name].precondition(model, None)
        tools["run_pipeline"].precondition(uc.PipelineRequest(repo_root=str(self.repo), sut_file="src/thermostat.c",
                                                              requirements=["docs/requirements.md"]), None)


class SelectionTests(unittest.TestCase):
    """ORC-35: the production files of a working tree, without an LLM."""
    def test_production_files_and_the_reasons_for_the_others(self):
        with tmpdir() as d:
            root = Path(d)
            files = {
                "src/motor.c": "int motor(int x)\n{\n    return x;\n}\n",
                "src/Engine.cpp": "namespace fw {\nint Engine::run(int a) const\n{\n    return a;\n}\n}\n",
                "src/tables.c": "const int table[3] = {1, 2, 3};\n",
                "src/gen_regs.c": "/* Auto-generated by regtool - DO NOT EDIT */\nint regs(void)\n{\n    return 0;\n}\n",
                "src/motor_test.c": "int t(void)\n{\n    return 0;\n}\n",
                "src/MotorTest.cpp": "int t2(void)\n{\n    return 0;\n}\n",
                "src/old_suite.c": "/* test_x.c - generated by agent_unittest_generator for src/x.c */\nint o(void)\n{\n    return 0;\n}\n",
                "unit_tests/check.c": "int c(void)\n{\n    return 0;\n}\n",
                "examples/demo.c": "int main(void)\n{\n    return 0;\n}\n",
                "build-arm/obj.c": "int b(void)\n{\n    return 0;\n}\n",
                "lib/fatfs/LICENSE.txt": "BSD",
                "lib/fatfs/ff.c": "int ff(void)\n{\n    return 0;\n}\n",
                "ext/sub/s.c": "int s(void)\n{\n    return 0;\n}\n",
                "include/motor.h": "int motor(int x);\n",
                ".gitmodules": "[submodule \"ext/sub\"]\n\tpath = ext/sub\n\turl = https://example.com/sub.git\n",
                "LICENSE": "MIT",
            }
            for rel, text in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text)
            sel = a1.select_production_files(root)
            self.assertEqual(sel.selected, ["src/Engine.cpp", "src/motor.c"])
            why = dict(line.split(": ", 1) for line in sel.left_out)
            self.assertEqual(why, {
                "build-arm/obj.c": "build output", "examples/demo.c": "example or documentation",
                "ext/sub/s.c": "third-party code (git submodule)",
                "lib/fatfs/ff.c": "third-party code (a folder with its own license file)",
                "src/MotorTest.cpp": "test code", "src/gen_regs.c": "generated code", "src/motor_test.c": "test code",
                "src/old_suite.c": "the pipeline's own file", "src/tables.c": "no function definition",
                "unit_tests/check.c": "test code"})
            (root / "compile_commands.json").write_text(json.dumps([{"directory": str(root / "build"),
                                                                     "file": "../src/motor.c", "command": "cc"}]))
            sel = a1.select_production_files(root, str(root / "compile_commands.json"), limit=5)
            self.assertEqual(sel.selected, ["src/motor.c"])
            self.assertIn("src/Engine.cpp: not compiled by the build (compile database)", sel.left_out)
            sel = a1.select_production_files(root, limit=1)
            self.assertEqual(sel.selected, ["src/Engine.cpp"])
            self.assertIn("src/motor.c: over the limit of 1 files (ORC-7)", sel.left_out)


class TargetDependencyTests(unittest.TestCase):
    """A1-DA12: a real dependency whose file does not compile on the host (an RTOS port) is stubbed."""
    def test_error_in_the_file_of_a_real_definition(self):
        port = "FreeRTOS-Kernel/portable/GCC/ARM_CM4F/port.c"
        message = ("the build did not link: port.c:38:6: error: This port can only be used when the project options "
                   "are configured to enable hardware floating point support.")
        self.assertTrue(uc.real_source_error(port, message))
        self.assertTrue(uc.real_source_error(port, "/w/FreeRTOS-Kernel/portable/GCC/ARM_CM4F/port.c:12: fatal error: x"))
        self.assertFalse(uc.real_source_error(port, "export.c:38:6: error: x"))
        self.assertFalse(uc.real_source_error(port, "port.c:38:6: warning: unused"))
        dep = uc.Dependency(name="vPortEnterCritical", declaration="void vPortEnterCritical(void);", decision="real",
                            decided_by="llm", definition=port)
        last = uc.VerificationResult(run_id="r", stage="build_check", sut_sha256="x", suite_sha256="", build_ok=False,
                                     decision="setup_blocked", reason=message)
        self.assertIn("does not compile here", a1.AnalyzerAgent._revision(dep, last, None))


class MemberNameTests(unittest.TestCase):
    """A1-DA21: a member function defined outside its class but inside a namespace keeps its class in its name.
    Found on QP/C++ qf_mem.cpp: `namespace QP { void * QMPool::get(...) {...} }` was named QP::get, so no test and
    no search could call it (5.7 % lines; 52.9 % after the fix, with the scripted brain)."""
    def test_semantic_owner_wins_over_the_enclosing_namespace(self):
        index = a1.AstIndex("", Path("."), Path("."))
        record = {"id": "0x2", "kind": "CXXRecordDecl", "name": "QMPool", "inner": []}
        method = {"id": "0x3", "kind": "CXXMethodDecl", "name": "get", "parentDeclContextId": "0x2"}
        free = {"id": "0x4", "kind": "FunctionDecl", "name": "helper"}
        index._index({"id": "0x1", "kind": "NamespaceDecl", "name": "QP", "inner": [record, method, free]}, "", "")
        self.assertEqual(index.qualified_name(method), "QP::QMPool::get")
        self.assertEqual(index.qualified_name(free), "QP::helper")
        outside = {"id": "0x5", "kind": "CXXMethodDecl", "name": "put", "parentDeclContextId": "0x2"}
        index._index(outside, "", "")                                   # `void QP::QMPool::put() {}` at file scope
        self.assertEqual(index.qualified_name(outside), "QP::QMPool::put")


class GenericFormTests(unittest.TestCase):
    """NC-15, A2-21af, A2-21ag, ORC-23: rules that hold for any C/C++ file, project and brain, not only the pilots."""

    def test_each_sut_file_has_its_own_generated_files(self):                               # A2-21af
        self.assertEqual(uc.unit_name_of("src/ctrl.c", ["src/ctrl.c", "src/brake_ctrl.c", "include/ctrl.h"]), "ctrl")
        self.assertEqual(uc.unit_name_of("src/uart.c", ["src/uart.c", "hal/uart.c"]), "src_uart")
        self.assertEqual(uc.unit_name_of("include/ring.hpp", ["include/ring.hpp", "src/ring.cpp"]), "include_ring")
        layout = uc.LayoutProfile(repo_root=".", sut_file="hal/uart.c", language="c", component_root=".",
                                  writable_test_dir="tests", writable_stub_dir="tests/stubs",
                                  suite_file="tests/test_hal_uart.c", sut_sha256="0" * 64)
        self.assertEqual(uc.stub_names(layout), ("tests/stubs/ut_stubs_hal_uart.c", "ut_stubs_hal_uart.h"))
        self.assertTrue(uc.STUB_HEADER.search("tests/stubs/ut_stubs_hal_uart.h"))
        self.assertTrue(uc.STUB_HEADER.search("tests/stubs/ut_stubs.h"))

    def test_two_files_of_one_component_keep_their_own_stubs(self):                          # A2-21af, ORC-23
        with tmpdir() as d:
            repo = copy_pilot("c_deps_pilot", Path(d))
            analyzer = a1.AnalyzerAgent()
            paths = set()
            for sut in ("src/ctrl.c", "src/brake_ctrl.c"):
                layout = analyzer.discover(uc.DiscoverRequest(repo_root=str(repo), sut_file=sut))
                plan = uc.UTPlan(plan_id="p", layout=layout, stage="coverage", reason="")
                paths.add(a2.render_stubs(plan)[0])
                paths.add(layout.suite_file)
            self.assertEqual(len(paths), 4, paths)

    def test_function_definitions_in_any_common_form(self):                                  # NC-15 (a)
        cases = {
            "static int\nfoo(int x)\n{\n  return x;\n}\n": ["foo"],
            "FUNC(void, COM_CODE) Com_Init(P2CONST(Cfg, AUTOMATIC, X) cfg)\n{\n}\n": ["Com_Init"],
            "__attribute__((weak)) void SysTick_Handler(void)\n{\n}\n": ["SysTick_Handler"],
            "typedef struct __attribute__((packed)) {\n int a;\n} pk_t;\nint g(void) { return 1; }\n": ["g"],
            "#if A\nint f(int a) {\n#else\nint f(int a, int b) {\n#endif\n return a;\n}\nint later(void) { return 2; }\n":
                ["f", "later"],
            "__declspec(dllexport) int api(void) { return 0; }\n[[nodiscard]] int nd(void) { return 0; }\n": ["api", "nd"],
        }
        for source, names in cases.items():
            self.assertEqual([f.qualified_name for f in a1.find_functions(source, "x.c")], names, source)

    def test_declarations_keep_variadic_unnamed_and_function_pointer_parameters(self):      # NC-15 (b)
        self.assertEqual(a1.parse_declaration("int log_printf(const char *fmt, ...)")[1],
                         [["const char *", "fmt"], ["...", ""]])
        self.assertEqual(a1.parse_declaration("void f(const my_t, struct cfg)")[1],
                         [["const my_t", "arg0"], ["struct cfg", "arg1"]])
        self.assertEqual(a1.parse_declaration("void reg(void (*cb)(int), unsigned n)")[1],
                         [["void (*)(int)", "cb"], ["unsigned", "n"]])
        self.assertIn("pure_fn", a1.find_declarations("int pure_fn(int) __attribute__((pure));\n"))
        spec = uc.StubSpec(function="log_printf", declaration="", return_type="int",
                           params=[["const char *", "fmt"], ["...", ""]])
        layout = uc.LayoutProfile(repo_root=".", sut_file="a.c", language="c", component_root=".",
                                  writable_test_dir="t", writable_stub_dir="t/s", suite_file="t/test_a.c",
                                  sut_sha256="0" * 64)
        _, source, _ = a2.render_stubs(uc.UTPlan(plan_id="p", layout=layout, stage="coverage", stubs=[spec]))
        self.assertIn("int log_printf(const char * fmt, ...)", source)

    def test_overloads_of_a_dependency_are_stubbed_apart(self):                              # NC-15 (b)
        one = uc.Dependency(name="write", qualified_name="hw::write", params=[["uint8_t", "v"]], header="hw.h", line=3)
        two = uc.Dependency(name="write", qualified_name="hw::write", params=[["uint16_t", "v"]], header="hw.h", line=4)
        self.assertNotEqual(uc.dependency_key(one), uc.dependency_key(two))
        uc.number_overloads([one, two])
        names = {uc.stub_base(uc.StubSpec(function="write", declaration="", return_type="void",
                                          qualified_name="hw::write", overload=d.overload)) for d in (one, two)}
        self.assertEqual(names, {"hw_write", "hw_write_2"})

    def test_dependency_code_is_the_definition_never_a_call(self):                           # NC-15 (d)
        text = "int caller(int x)\n{\n    if (hw_read(x)) {\n        return 1;\n    }\n    return 0;\n}\n" \
               "static int\nhw_read(int ch)\n{\n    return STATUS + ch;\n}\n"
        m = uc.definition_match(text, "hw_read")
        self.assertTrue(text[m.start():].startswith("static int\nhw_read(int ch)"))

    def test_register_waits_in_any_form(self):                                              # NC-15 (f)
        fn = uc.FunctionInfo(name="f", qualified_name="f", signature="void f(void)", file="x.c", line_start=1, line_end=3)
        cases = {"while ((SR & RDY) == 0u) {}": ("SR", "RDY", True),
                 "while (0u == (UART1_SR & UART_TXE)) { }": ("UART1_SR", "UART_TXE", True),
                 "while ((SR & RDY) != RDY);": ("SR", "RDY", True),
                 "while (UART_SR & BUSY) {}": ("UART_SR", "BUSY", False),
                 "while (!(USART1->SR & USART_SR_TXE)) {}": ("USART1->SR", "USART_SR_TXE", True),
                 "while ((REG & 0x20u) == 0x20u) {}": ("REG", "0x20u", False)}
        for body, wait in cases.items():
            self.assertEqual(a2.register_waits(fn, "{ " + body + " }"), [wait], body)
        macros = {"READ_BIT": (["REG", "BIT"], "((REG) & (BIT))")}
        cond = a2.expand_macros("!READ_BIT(USART1->SR, TXE)", macros)
        self.assertEqual(a2.wait_polarity(cond, "TXE"), True)

    def test_register_windows_follow_the_sut(self):                                          # NC-15 (g), A3-HW1
        with tmpdir() as d:
            Path(d, "r.h").write_text("#define PERIPH_BASE (0xF0000000UL)\n"
                                      "#define TIM0 (*(volatile uint32_t *)(PERIPH_BASE + 0x2010u))\n"
                                      "#define PORTB (*(volatile uint8_t *)0x25)\n")
            Path(d, "s.c").write_text('#include "r.h"\nint f(void) { return TIM0; }\n')
            layout = uc.LayoutProfile(repo_root=d, sut_file="s.c", language="c", component_root=".",
                                      writable_test_dir="t", writable_stub_dir="t/s", suite_file="t/test_s.c",
                                      sut_sha256="0" * 64, headers=["r.h"])
            windows, notes = uc.register_windows(layout, [[0x40000000, 0x60000000]])
        self.assertIn([0xF0000000, 0xF0100000], windows)
        self.assertIn([0x40000000, 0x60000000], windows)
        self.assertTrue(any("0x25" in n for n in notes))

    def test_macros_from_flags_included_headers_and_continuation_lines(self):              # NC-15 (e)
        with tmpdir() as d:
            Path(d, "inc").mkdir()
            Path(d, "inc/hw.h").write_text('#include "deep.h"\n#define TIMEOUT_US \\\n   (5000u)\n')
            Path(d, "inc/deep.h").write_text("#define DEEP 7 // seven\n")
            Path(d, "s.c").write_text('#include "hw.h"\n')
            layout = uc.LayoutProfile(repo_root=d, sut_file="s.c", language="c", component_root=".",
                                      writable_test_dir="t", writable_stub_dir="t/s", suite_file="t/test_s.c",
                                      sut_sha256="0" * 64, include_dirs=["inc"], headers=["inc/hw.h"],
                                      compile_flags=["-DMODE=2"])
            self.assertEqual(a2.header_macros(layout), {"MODE": "2", "TIMEOUT_US": "(5000u)", "DEEP": "7"})

    def test_function_templates_of_a_header_sut_are_instantiated(self):                    # NC-15 (h)
        with tmpdir() as d:
            Path(d, "m.hpp").write_text("#pragma once\nnamespace util {\ntemplate <typename T>\n"
                                        "inline T maxv(T a, T b) { return a > b ? a : b; }\n"
                                        "template <typename T> auto bad(T a) -> T { return a; }\n}\n")
            Path(d, "use.cpp").write_text('#include "m.hpp"\nlong k = util::maxv<long>(1, 2);\n')
            layout = uc.LayoutProfile(repo_root=d, sut_file="m.hpp", language="cpp", component_root=".",
                                      writable_test_dir="t", writable_stub_dir="t/s", suite_file="t/test_m.cpp",
                                      sut_sha256="0" * 64, header_sut=True)
            rows = a2.function_templates(uc.UTPlan(plan_id="p", layout=layout, stage="coverage"))
        self.assertEqual(rows, ["template long util::maxv<long>(long a, long b);"])

    @unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
    def test_cpp_tests_compile_without_exceptions_and_rtti(self):                           # A2-21ag
        tools = _tools()
        with tmpdir() as d:
            Path(d, "ut_harness.h").write_text(a3.HARNESS_H)
            Path(d, "t.cpp").write_text('#include "ut_harness.h"\nint f(int x) { return x; }\n'
                                        "void t(void) { UT_EXPECT_NO_THROW(f(1)); UT_CALL_ANY(f(2)); "
                                        'UT_OBSERVE_THROW("o", f(3)); UT_EXPECT_ANY_THROW(f(4)); }\n')
            for flags in ([], ["-fno-exceptions", "-fno-rtti"]):
                proc = subprocess.run([tools.cxx, "-std=c++17", "-fsyntax-only", *flags, str(Path(d, "t.cpp"))],
                                      capture_output=True, text=True, env={**os.environ, **tools.env()})
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_answers_of_any_brain_are_read(self):                                           # NC-15 (k)
        self.assertEqual(ba.parse_generated('Here is the JSON:\n```json\n{"a": 1}\n```\nDone.'), {"a": 1})
        self.assertEqual(ba.parse_generated("<reasoning>r</reasoning>[1]"), [1])

    def test_no_brain_task_names_a_pilot(self):                                             # NC-15 (j)
        pilot_names = set()
        for f in PILOTS.rglob("*"):
            if f.suffix in (".c", ".cpp", ".h", ".hpp"):
                for fn in a1.find_functions(f.read_text(encoding="utf-8", errors="replace"), f.name):
                    pilot_names |= set(fn.qualified_name.split("::"))
        # identifiers, not English words: with an underscore or a capital inside (clamp_percent, MotorCtrl)
        pilot_names = {n for n in pilot_names if not n.startswith("__")                 # reserved: vendor intrinsics
                       and ("_" in n.strip("_") or re.search(r"[a-z][A-Z]", n))}
        for module in (a1, a2, a3):
            text = Path(module.__file__).read_text(encoding="utf-8")
            prompts = " ".join(re.findall(r'"([^"\n]{40,})"', text))              # long string literals: brain tasks
            used = sorted(n for n in pilot_names if re.search(r"\b" + re.escape(n) + r"\b", prompts))
            self.assertEqual(used, [], module.__name__)

    def test_a_shared_lock_is_held_by_one_run_at_a_time(self):                              # ORC-23 (d)
        with tmpdir() as d:
            lock = Path(d) / "x.lock"
            with uc.file_lock(lock, wait_s=5):
                with self.assertRaises(uc.LockBusy):
                    with uc.file_lock(lock, wait_s=0.3, poll_s=0.05):
                        pass
            self.assertFalse(lock.exists())
            lock.write_text("1 0\n")
            os.utime(lock, (time.time() - 600, time.time() - 600))                     # a run that ended without releasing
            with uc.file_lock(lock, wait_s=1, stale_s=60):
                pass

    def test_files_of_a_folder_run_in_parallel_and_report_in_order(self):                  # ORC-23
        with tmpdir() as d:
            repo = copy_pilot("c_deps_pilot", Path(d))
            runner = up.RepositoryRun(None)
            runner.cfg = runner.cfg.model_copy(update={"max_parallel_suts": 2})
            active, peak = [0], [0]

            class Fake:
                async def run(self, req):
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                    await asyncio.sleep(0.2 if req.sut_file.endswith("ctrl.c") and "brake" not in req.sut_file else 0.05)
                    active[0] -= 1
                    return uc.PipelineResult(final_state="verified_measured", reason="ok", sut_file=req.sut_file,
                                             output_dir=str(Path(d) / req.sut_file.replace("/", "_")))
            runner._orchestrator = lambda: Fake()
            suts = ["src/ctrl.c", "src/brake_ctrl.c", "src/steer_assist.c"]
            out = Path(d) / "out"
            out.mkdir()
            result = run(runner._folder(uc.RepositoryRequest(repository=str(repo), sut_files=suts), out))
        self.assertEqual(peak[0], 2)                                       # two at a time, never more
        self.assertEqual([r.sut_file for r in result.results], suts)       # reported in the order named
        runner.orchestrator.cfg.interactive = True                          # LA-9: one after another
        peak[0] = 0
        with tmpdir() as d:
            repo = copy_pilot("c_deps_pilot", Path(d))
            out = Path(d) / "out"
            out.mkdir()
            run(runner._folder(uc.RepositoryRequest(repository=str(repo), sut_files=suts), out))
        self.assertEqual(peak[0], 1)

    @unittest.skipUnless(HAVE_FASTMCP, "fastmcp not installed")
    def test_parallel_files_of_a_folder_do_not_share_the_answer_log(self):                 # ORC-23 (c)
        orch = up.Orchestrator(None)
        orch.run_env = {"BASEAGENT_ANSWER_LOG": "/tmp/one.jsonl"}
        before = os.environ.get("BASEAGENT_ANSWER_LOG")
        transport, _ = orch._transport("analyzer", Path(tempfile.gettempdir()))
        self.assertEqual(transport.env["BASEAGENT_ANSWER_LOG"], "/tmp/one.jsonl")
        self.assertEqual(os.environ.get("BASEAGENT_ANSWER_LOG"), before)


class SolvedScenarioTests(unittest.TestCase):
    """A1-PL15: items whose decision and path compare parameters with constants are planned without a brain."""
    SOURCE = ("#include <stdbool.h>\n#define RPM_MIN 500\n\n"
              "bool valid_request(int rpm, int request, bool gear_engaged, bool clutch_open)\n{\n"
              "    if (rpm < 0)\n        return false;\n"
              "    return rpm >= RPM_MIN && rpm <= 6000 && request >= 0 && request <= 400 && (gear_engaged || clutch_open);\n"
              "}\n\nint scale(int raw, int *out)\n{\n    if (raw > 10) { *out = raw; }\n    return raw;\n}\n")

    def setUp(self):
        self.tmp = tmpdir()
        root = Path(self.tmp.name) / "repo"
        (root / "src").mkdir(parents=True)
        (root / "src" / "torque.c").write_text(self.SOURCE, encoding="utf-8")
        self.agent = a1.AnalyzerAgent()
        self.layout = self.agent.discover(uc.DiscoverRequest(repo_root=str(root), sut_file="src/torque.c"))
        self.lines = self.SOURCE.splitlines()
        self.col = self.lines[7].find("request >= 0") + 1

    def tearDown(self):
        self.tmp.cleanup()

    def gaps(self):
        return [uc.GapItem(gap_id=f"BC:valid_request:8:{self.col}:F", function="valid_request", metric="BC", line=8,
                           column=self.col, detail="never false"),
                uc.GapItem(gap_id="MCDC:valid_request:8:12:C3", function="valid_request", metric="MCDC", line=8,
                           column=12, condition_index=2)]

    def test_the_vector_an_item_needs_is_solved_for_arguments(self):
        fn = next(f for f in self.layout.functions if f.name == "valid_request")
        self.assertEqual(a1.guard_conditions(self.lines, fn, 8), [("rpm < 0", False)])     # the guard clause
        bc, mcdc = self.gaps()
        (args, vector), = a1.solve_gap(bc, fn, self.lines, {"RPM_MIN": 500})
        self.assertEqual([a.value for a in args], ["500", "-1", "false", "true"])
        self.assertIn("(request >= 0) is false", vector)
        pair = a1.solve_gap(mcdc, fn, self.lines, {"RPM_MIN": 500})                        # both sides of C3
        self.assertEqual([[a.value for a in args] for args, _ in pair],
                         [["500", "-1", "false", "true"], ["500", "0", "false", "true"]])
        # Not of this form: a pointer parameter, a macro the build may change, a parameter changed before
        self.assertEqual(a1.solve_gap(bc, fn, self.lines, {}), [])
        scale = next(f for f in self.layout.functions if f.name == "scale")
        g = uc.GapItem(gap_id="BC:scale:14:9:T", function="scale", metric="BC", line=14, column=9)
        self.assertEqual(a1.solve_gap(g, scale, self.lines, {}), [])
        changed = self.lines[:6] + ["    rpm = rpm / 2;"] + self.lines[7:]
        self.assertEqual(a1.solve_gap(bc, fn, changed, {"RPM_MIN": 500}), [])

    def test_a_solved_item_becomes_a_test_without_a_brain(self):
        b = UTBrain(plans={})
        with brain(b):
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps()),
                                                      iteration=2)))
        solved = [s for s in plan.scenarios if s.scenario_id.startswith("S-solved-")]
        self.assertEqual(len(solved), 2, plan.scenarios)
        self.assertEqual(sorted(solved[0].gap_ids), sorted(g.gap_id for g in self.gaps()))   # one test, two items
        self.assertTrue(all(s.checked and s.made_by == "rule" for s in solved))
        self.assertEqual(b.generate_calls, [])                                               # no brain asked
        agent = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="llm"))
        uc.apply_profiles(agent, None)
        agent.router = ba.BrainRouter([b], 0.0)
        suite = uc.suite_of(run(agent.generate_bundle(uc.GenerationRequest(plan=plan))))
        self.assertIn("valid_request(500, -1, false, true)", suite)
        self.assertIn("valid_request(500, 0, false, true)", suite)
        # A solved test that ran and left its item open is not planned again: the brain has the item
        tests = [uc.TestResult(name=s.test_name, status="passed") for s in solved]
        again = [g.model_copy(update={"attempts": 1}) for g in self.gaps()]
        with brain(UTBrain(plans={})):
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout, iteration=3, attempts={
                g.gap_id: 1 for g in again}, last=measured(self.layout, again, tests=tests))))
        self.assertFalse([s for s in plan.scenarios if s.scenario_id.startswith("S-solved-")])


    def test_a_no_change_answer_is_a_verdict_not_a_refusal(self):                         # COV-12a
        bc, mcdc = self.gaps()
        items = [uc.ItemEvidence(item_id=g.gap_id, metric=g.metric, function="valid_request", line=8) for g in (bc, mcdc)]
        line = self.lines[7].strip()
        same = {"item_ids": [bc.gap_id], "cause": "condition_defect", "original_fragment": line,
                "corrected_fragment": line, "rationale": "the condition is right", "requirement_ids": [], "quotes": [],
                "expected_effect": "valid_request(500, -1, false, true) reaches it"}
        verdict = dict(same, item_ids=[mcdc.gap_id], cause="reachable_by_test", original_fragment="",
                       corrected_fragment="")
        with brain(UTBrain(suggestions=[same, verdict])):
            result = run(a4.CorrectorAgent().suggest(uc.SuggestRequest(layout=self.layout, items=items)))
        self.assertEqual(result.suggestions, [])
        self.assertEqual(len(result.notes), 2, result.notes)
        self.assertIn("no source change needed", result.notes[0])
        self.assertIn("valid_request(500, -1, false, true)", result.notes[1])

    def test_a_test_program_that_reports_failed_tests_is_no_error(self):                   # VIEW-14
        seen = []
        with mock.patch.object(a3.ba, "emit_event", lambda kind, **kw: seen.append(kw)):
            a3._program_event(["/x/ut_runner.exe"], 1, 0.1, "UT_RESULT|t|FAIL\nUT_SUMMARY|9|7|2\n")
            a3._program_event(["/x/ut_runner.exe"], 3, 0.1, "")
        self.assertEqual(seen[0]["data"]["outcome"], "tests_failed")
        self.assertIn("2 of 9 tests failed", seen[0]["summary"])
        self.assertEqual(seen[1]["data"]["outcome"], "exit 3")


class PilotRunFixesTests(unittest.TestCase):
    """From the pilot-all run of 2026-10-25: A2-21z, A2-21ai, A2-21s, A2-21k, A3-156a, A3-85k, A3-OBS5, VIEW-15."""

    def layout(self, language="cpp", **kw):
        base = dict(repo_root=".", sut_file="src/counter.cpp", language=language, component_root=".",
                    writable_test_dir="tests", writable_stub_dir="tests", suite_file="tests/test_counter.cpp",
                    sut_sha256="0" * 64)
        base.update(kw)
        return uc.LayoutProfile(**base)

    def test_one_list_of_state_setters_for_both_agents(self):                       # A2-21z
        layout = self.layout(sut_state=[uc.StateVar(name="counter::total", type="volatile std::uint32_t"),
                                        uc.StateVar(name="g_buf", type="int[4]"),
                                        uc.StateVar(name="g_obj", type="Foo"),
                                        uc.StateVar(name="g_mode", type="Mode")],
                             enums=[uc.EnumInfo(name="Mode")])
        self.assertEqual([v.name for v in uc.settable_state(layout)], ["counter::total", "g_mode"])
        self.assertEqual(uc.state_setter_name(layout.sut_state[0]), "ut_state_set_counter_total")
        plan = uc.UTPlan(plan_id="p", layout=layout, stage="coverage", reason="")
        with mock.patch.object(Path, "is_file", return_value=True):
            self.assertEqual([v.name for v in a2.setter_vars(plan)], ["counter::total", "g_mode"])
        offered = a1.AnalyzerAgent()._known_names(layout, [])
        self.assertIn("ut_state_set_counter_total", offered)                     # what the brain is told of exists
        self.assertNotIn("ut_state_set_g_buf", offered)
        self.assertEqual(a2.setter_source(layout.sut_state[0], True),
                         "void ut_state_set_counter_total(long long v) { counter::total = (decltype(counter::total))v; }")
        self.assertTrue(uc.state_resettable(layout.sut_state[0], True))           # std:: integers too [A2-21s]

    def test_names_are_checked_before_the_build(self):                              # A2-21ai
        layout = self.layout(sut_state=[uc.StateVar(name="counter::total", type="volatile std::uint32_t")],
                             functions=[uc.FunctionInfo(name="dispatch", qualified_name="fw::dispatch", signature="",
                                                        file="a.cpp", line_start=1, line_end=2)])
        allowed = {"UT_EXPECT_EQ_INT", "add", "ut_state_set_counter_total", uc.HIDDEN_MARK + "counter::total"}
        problem = a2.GeneratorAgent._body_problem("UT_EXPECT_EQ_INT(ut_state_get_counter_total(), 0);", allowed)
        self.assertIn("helpers the pipeline does not write", problem)
        self.assertEqual(uc.check_kind(problem), "guard")                        # refused, never a trial build
        problem = a2.GeneratorAgent._body_problem("total = 3; UT_EXPECT_EQ_INT(add(1), 4);", allowed)
        self.assertIn("ut_state_set_counter_total(value)", problem)
        self.assertEqual(uc.check_kind(problem), "guard")
        self.assertEqual(a2.GeneratorAgent._body_problem(
            "ut_state_set_counter_total(3); UT_EXPECT_EQ_INT(add(1), 4);", allowed), "")
        self.assertEqual(uc.hidden_state(layout, "namespace counter { std::uint32_t add(std::uint32_t n); }"),
                         {"counter::total"})
        names = uc.namespace_names(layout, "namespace fw { struct Evt { int sig; }; }")
        self.assertEqual(names, {"dispatch": "fw::dispatch", "Evt": "fw::Evt"})
        body, fixed = uc.qualify_names('Evt e{1}; dispatch(&e); fw::dispatch(&e); UT_OBSERVE_INT("Evt", e.sig);', names)
        self.assertEqual(body, 'fw::Evt e{1}; fw::dispatch(&e); fw::dispatch(&e); UT_OBSERVE_INT("Evt", e.sig);')
        self.assertEqual(fixed, ["Evt", "dispatch"])
        self.assertEqual(uc.qualify_names("using namespace fw; Evt e{1};", names)[1], [])

    def test_only_free_internal_functions_get_a_wrapper(self):                      # A2-21k
        tmp = tmpdir()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "src").mkdir()
        (root / "src/a.cpp").write_text("namespace {\nint helper(int x) { return x; }\ntemplate <typename T>\n"
                                        "T twice(T x) { return x + x; }\n}\n")
        fns = [uc.FunctionInfo(name="helper", qualified_name="(anonymous namespace)::helper", signature="int helper(int x)",
                               file="src/a.cpp", line_start=2, line_end=2, is_static=True, from_frontend=True),
               uc.FunctionInfo(name="twice", qualified_name="(anonymous namespace)::twice", signature="T twice(T x)",
                               file="src/a.cpp", line_start=4, line_end=4, is_static=True, from_frontend=True),
               uc.FunctionInfo(name="Fixed::from_raw", qualified_name="pt::Fixed::from_raw", signature="",
                               file="src/a.cpp", line_start=1, line_end=1, is_static=True, from_frontend=True, member=True)]
        layout = self.layout(repo_root=str(root), sut_file="src/a.cpp", functions=fns)
        self.assertEqual([f.name for f in a2.static_functions(layout)], ["helper"])

    def test_the_root_cause_sees_the_state_the_function_reads(self):                 # A3-156a
        src = "static int g_last;\nint step(int t)\n{\n    int out = t - g_last > 5 ? g_last + 5 : t;\n    g_last = out;\n    return out;\n}\n"
        layout = self.layout(language="c", sut_file="src/a.c", sut_state=[uc.StateVar(name="g_last", type="int")],
                             functions=[uc.FunctionInfo(name="step", qualified_name="step", signature="int step(int t)",
                                                        file="src/a.c", line_start=2, line_end=7)])
        reads, how = a3.state_facts(layout, src, "step", "UT_EXPECT_EQ_INT(step(100), 100);")
        self.assertEqual(reads, ["g_last"])
        self.assertIn("not set", how)
        _, how = a3.state_facts(layout, src, "step", "ut_state_set_g_last(95); UT_EXPECT_EQ_INT(step(100), 100);")
        self.assertIn("ut_state_set_g_last", how)
        _, how = a3.state_facts(layout, src, "step", "step(50); UT_EXPECT_EQ_INT(step(100), 55);")
        self.assertIn("1 earlier call", how)

    def test_mutants_run_in_parallel_without_per_test_measurement(self):           # A3-85k, A3-OBS5, VIEW-14
        order, seen = [], []
        jobs = [lambda k=k: (time.sleep(0.02 * (3 - k)), order.append(k), k)[2] for k in range(3)]
        self.assertEqual(a3._in_parallel(jobs, 3), [0, 1, 2])                     # results in order
        self.assertNotEqual(order, [0, 1, 2])                                      # ran at the same time
        with mock.patch.object(a3.ba, "emit_event", lambda kind, **kw: seen.append(kw)):
            a3._in_parallel([lambda: a3._run([sys.executable, "-c", "pass"], Path("."), 10)] * 2, 2)
        self.assertEqual(seen, [])                                                 # one entry by the caller instead
        comp = a3.CompilationAgent(config=a3.CompilationConfig())
        asked = []

        def execute(req):
            asked.append(req.measure_tests)
            return uc.VerificationResult(run_id="r", stage="observe", sut_sha256="", suite_sha256="", build_ok=True,
                                         link_ok=True, run_ok=True, decision="complete", run_dir=str(Path(tmpdir().name)),
                                         tests=[uc.TestResult(name="t", status="passed")])
        fn = uc.FunctionInfo(name="f", qualified_name="f", signature="int f(int x)", file="a.c", line_start=1, line_end=1)
        tmp = tmpdir()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "a.c").write_text("int f(int x) { return x <= 3 ? x + 1 : 0; }\n")
        layout = self.layout(language="c", repo_root=tmp.name, sut_file="a.c", suite_file="t.c", functions=[fn])
        bundle = uc.FileActionBundle(bundle_id="b", actions=[uc.FileAction(path="t.c", action="create",
                                                                                         role="testsuite", content="")])
        with mock.patch.object(comp, "execute", execute), \
                mock.patch.object(a3.ba, "emit_event", lambda kind, **kw: seen.append(kw)):
            result = comp.oracle_check(uc.OracleRequest(layout=layout, function="f", test="t", original_suite="",
                                                        changed_suite="", bundle=bundle))
        self.assertTrue(result.mutants)
        self.assertTrue(asked and all(m == [] for m in asked))                     # no per-test measurement
        self.assertEqual(len([s for s in seen if "mutants" in s.get("summary", "")]), 1)


class LargeFileRoundTests(unittest.TestCase):
    """A1-PL16, A1-PL1, LA-5: on a large file the brain's work is split into rounds, and the rule round is no attempt."""

    def test_the_brain_plans_a_large_file_in_rounds(self):                                 # A1-PL16
        agent = a1.AnalyzerAgent()
        fn = lambda k: uc.FunctionInfo(name=f"f{k}", qualified_name=f"f{k}", signature="", file="a.c",
                                       line_start=k * 10, line_end=k * 10 + 5)
        gap = lambda k, j: uc.GapItem(gap_id=f"BC:{k}:{j}", function=f"f{k}", metric="BC", line=k * 10 + 1)
        work = [(f"f{k}", fn(k), [gap(k, j) for j in range(1 + k % 3)], {f"BC:{k}:{j}" for j in range(1 + k % 3)}, [])
                for k in range(100)]
        later = agent._this_round(work, uc.PlanRequest(layout=uc.LayoutProfile(
            repo_root=".", sut_file="a.c", language="c", component_root=".", writable_test_dir="t",
            writable_stub_dir="t", suite_file="t/x.c", sut_sha256="0" * 64), iterations_left=9,
            attempts={"BC:0:0": 1}))
        self.assertEqual(len(later), 70)                                    # 30 per round, 5 rounds for 100
        names = {w[0] for w in later}
        self.assertIn("f0", names)                                          # tried before: after the others
        self.assertNotIn("f2", names)                                       # most open items first
        req = uc.PlanRequest(layout=uc.LayoutProfile(
            repo_root=".", sut_file="a.c", language="c", component_root=".", writable_test_dir="t",
            writable_stub_dir="t", suite_file="t/x.c", sut_sha256="0" * 64), iterations_left=1)
        self.assertEqual(agent._this_round(work, req), [])                  # the last rounds take every function
        self.assertEqual(agent._this_round(work[:30], req.model_copy(update={"iterations_left": 9})), [])

    def test_a_rule_test_that_crashed_is_left_out(self):                                    # A2-21f
        suite = ("/* evidence: E8 */\nUT_TEST(test_f_auto_1)\n{\n    UT_OBSERVE_INT(\"o1\", f(1));\n}\n"
                 "/* evidence: E8 */\nUT_TEST(test_f_auto_2)\n{\n    UT_OBSERVE_INT(\"o2\", f(2));\n}\n"
                 "UT_SUITE_BEGIN\n    UT_CASE(test_f_auto_1)\n    UT_CASE(test_f_auto_2)\nUT_SUITE_END\n")
        bundle = uc.FileActionBundle(bundle_id="b", actions=[uc.FileAction(path="t/test_a.c", action="create",
                                                                           role="testsuite", content=suite)],
                                     tests=["test_f_auto_1", "test_f_auto_2"], new_tests=["test_f_auto_1", "test_f_auto_2"],
                                     characterization_tests=["test_f_auto_1", "test_f_auto_2"], observations=["o1", "o2"])
        out = a2.finalize_bundle(bundle, {"o1": "3", "o2": "4"}, {}, {"test_f_auto_2": "crashed: access violation"})
        self.assertEqual(out.tests, ["test_f_auto_1"])                    # observed, but it crashed: left out
        self.assertEqual(out.crashed_calls, ["f(2)"])

    def test_a_pointer_to_a_scalar_points_to_an_array(self):                                # A2-21h
        fn = uc.FunctionInfo(name="set_bits", qualified_name="set_bits", signature="", file="a.c", line_start=1,
                             line_end=3, from_frontend=True)
        p = uc.ParamInfo(name="data", type="uint8_t *", kind="ptr_scalar", base="uint8_t")
        layout = uc.LayoutProfile(repo_root=".", sut_file="a.c", language="c", component_root=".",
                                  writable_test_dir="t", writable_stub_dir="t", suite_file="t/x.c", sut_sha256="0" * 64)
        opts = a2._param_options(fn, 0, p, [0, 5], layout)
        self.assertEqual(opts[1], ([f"uint8_t ut_v0[{a2.POINTEE_ELEMENTS}] = {{(uint8_t)5}};"], "ut_v0",
                                   ['UT_OBSERVE_INT("@_o0", ut_v0[0]);']))
        ref = a2._param_options(fn, 0, p.model_copy(update={"by_ref": True, "type": "std::uint8_t &"}), [5],
                                layout.model_copy(update={"language": "cpp"}))
        self.assertEqual(ref[0][1], "ut_v0")                                # a reference stays one variable
        self.assertIn("uint8_t ut_v0 = (uint8_t)5;", ref[0][0])

    def test_profiles_are_merged_through_a_list_file(self):                                 # A3-60a
        source = (SRC / "agent_unittest_compilation.py").read_text(encoding="utf-8")
        self.assertIn('"--input-files={listing}"', source)
        err = FileNotFoundError(2, "The filename or extension is too long")
        err.winerror = 206
        with mock.patch.object(a3.subprocess, "run", side_effect=err), mock.patch.object(a3.os, "name", "nt"):
            code, text = a3._run(["llvm-profdata.exe", "merge", "x" * 10], Path("."), 5)
        self.assertEqual(code, -126)
        self.assertIn("command line is too long", text)                   # never "tool not found"

    def test_a_round_that_was_not_measured_is_not_the_end(self):                           # A3-60b
        stop = lambda fid: uc.VerificationResult(run_id="r", stage="verify", sut_sha256="", suite_sha256="",
                                                 decision="environment_error", failures=[uc.Failure(
                                                     failure_id=fid, kind="environment", route="stop",
                                                     classification="environment_defect", diagnostic="x")])
        self.assertTrue(up.Orchestrator._not_measured(stop("env-coverage")))
        self.assertFalse(up.Orchestrator._not_measured(stop("env-coverage-tool")))   # a missing tool stops
        self.assertFalse(up.Orchestrator._not_measured(stop("env-toolchain")))
        source = (SRC / "ut_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("if self._not_measured(result) and s.unmeasured == 0:", source)

    def test_a_setup_round_without_the_brains_declarations(self):                       # A1-PL14, ARCH-21a
        rule = uc.Dependency(name="pwm", qualified_name="hal::pwm", declaration="void pwm(int);", decision="stub",
                             decided_by="rule", header="include/hal.hpp")
        invented = rule.model_copy(update={"declaration": "int hal::pwm(hal::x);", "decided_by": "brain declaration (q)"})
        other = uc.Dependency(name="adc", decision="stub", decided_by="brain declaration (q)")
        layout = uc.LayoutProfile(repo_root=".", sut_file="a.cpp", language="cpp", component_root=".",
                                  writable_test_dir="t", writable_stub_dir="t", suite_file="t/x.cpp", sut_sha256="0" * 64,
                                  dependencies=uc.DependencyPlan(entries=[invented, other]))
        orch = up.Orchestrator.__new__(up.Orchestrator)
        orch.layout, orch.hub = layout, None
        s = types.SimpleNamespace(layout=layout, notes=[], declarations_dropped=[], replaced_dependencies={"pwm": rule})
        with mock.patch.object(up.Orchestrator, "_stage", lambda self, text: None):
            self.assertTrue(orch._drop_brain_declarations(s))
            self.assertFalse(orch._drop_brain_declarations(s))   # once
        self.assertEqual(s.declarations_dropped, ["adc", "hal::pwm"])
        self.assertEqual([(d.name, d.decided_by) for d in orch.layout.dependencies.entries], [("pwm", "rule")])

    def test_two_pilot_folders_are_one_command(self):                                       # VIEW-12a
        bat = (SRC / "unittest_generator.bat").read_text(encoding="utf-8")
        sh = (SRC / "unittest_generator.sh").read_text(encoding="utf-8")
        self.assertNotIn("NEXT_PILOT", bat)
        self.assertIn('ut_pipeline.py batch %REPOS% %PAIR_SUTS%', bat)
        self.assertIn("--sut c_large_pilot=src/body_ctrl.c --sut cpp_large_pilot=src/powertrain.cpp", bat)
        self.assertIn('ut_pipeline.py batch "${repos[@]}" "${suts[@]}"', sh)
        self.assertNotIn("pilot c_large_pilot", sh)

    def test_the_rule_round_is_no_attempt(self):                                            # A1-PL1, LA-5
        plan = uc.UTPlan(plan_id="p", stage="coverage", rules_only=True, layout=uc.LayoutProfile(
            repo_root=".", sut_file="a.c", language="c", component_root=".", writable_test_dir="t",
            writable_stub_dir="t", suite_file="t/x.c", sut_sha256="0" * 64), scenarios=[uc.Scenario(
                scenario_id="S-evidence-f", function="f", gap_ids=["EVIDENCE:f"], description="d")])
        self.assertEqual(len(up.Journal.attempts_of(plan, 1)), 1)          # would count without the flag
        source = (SRC / "ut_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("attempted = [] if plan.rules_only else Journal.attempts_of(plan, iteration)", source)


class CacheTests(unittest.TestCase):
    """CA-1 .. CA-14: what a cache keeps, when it is a miss, and that it never decides a result."""
    def setUp(self):
        self.tmp = tmpdir()
        self.d = Path(self.tmp.name)
        self.addCleanup(setattr, a1.FrontEnd, "cache", a1.FrontEnd.cache)
        a1.FrontEnd.cache = None                          # CA-11: no store of another test
        patcher = mock.patch.dict(os.environ, {"UT_CACHE": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_the_store_keeps_values_and_files_and_a_missing_entry_is_a_miss(self):       # CA-1
        cache = uc.RunCache(self.d / "c", "kind")
        key = cache.key("a", "ut_work/20261025-171605-e0f3ef8a/repo/x.c")
        self.assertEqual(key, cache.key("a", "ut_work/20261026-090000-aaaaaa/repo/x.c"))     # a run's id left out
        self.assertIsNone(cache.get_json(key))
        cache.put_json(key, {"x": [1, 2]})
        self.assertEqual(cache.get_json(key), {"x": [1, 2]})
        (self.d / "f.o").write_bytes(b"obj")
        cache.put_file(key, self.d / "f.o", ".o")
        self.assertTrue(cache.get_file(key, self.d / "g.o", ".o"))
        self.assertEqual((self.d / "g.o").read_bytes(), b"obj")
        self.assertEqual((cache.hits, cache.misses), (2, 1))
        with mock.patch.dict(os.environ, {"UT_CACHE": "off"}):                             # switched off
            self.assertIsNone(cache.get_json(key))
        config = self.d / "ut_config.json"
        config.write_text(json.dumps({"cache": {"dir": "rc", "max_mb": 0, "objects": False}}))
        self.assertTrue(uc.cache_on(config, "test_coverage"))
        self.assertFalse(uc.cache_on(config, "objects"))
        self.assertIsNone(uc.run_cache(config, "objects"))
        store = uc.run_cache(config, "test_coverage")
        store.put_json("ab" + "0" * 62, [1])
        self.assertGreater(uc.prune_cache(config), 0)                                  # the size limit
        self.assertIsNone(store.get_json("ab" + "0" * 62))

    def test_a_brain_answer_is_reused_and_still_checked(self):                          # CA-2
        class Brain:
            brain_name, model_id, simulation = "qwen", "scripted", False

            def __init__(self):
                self.calls = 0

            async def start(self):
                return None

            async def close(self):
                return None

            async def generate(self, task, context, schema, validate):
                self.calls += 1
                return {"model": self.model_id, "output": validate({"declaration": "int f(void);"}), "usage": {}}
        brain_ = Brain()
        agent = a1.AnalyzerAgent(router=ba.BrainRouter([brain_], 0.0))
        agent.answer_cache = ba.AnswerCache(self.d / "answers.sqlite3")
        ask = lambda check=None: run(agent.generate("Declare f for run 20261025-171605-e0f3ef.", "ctx",
                                                    a1.DeclarationAnswer, step="A1.declaration", check=check))
        self.assertEqual(ask().declaration, "int f(void);")
        self.assertEqual(ask().declaration, "int f(void);")
        self.assertEqual(brain_.calls, 1)                                       # the same question: from the cache
        self.assertEqual(agent.token_plans[-1]["outcome"], "cached")

        def refuse(answer):
            raise ba.AgentFault("MODEL_OUTPUT_INVALID", "not this one")
        with self.assertRaises(ba.AgentFault):
            ask(refuse)                                                         # the checks run on a cached answer
        self.assertEqual(brain_.calls, 2)
        with mock.patch.dict(os.environ, {"UT_CACHE": "off"}):
            ask()
        self.assertEqual(brain_.calls, 3)

    def test_every_agent_process_is_built_with_its_caches(self):                          # CA-1, the stdio wiring
        """What an agent process does at its start (build_agent with the configuration of the run) - so a cache a run
        needs is never missing, and the switch reaches every process."""
        config = self.d / "ut_config.json"
        raw = json.loads((SRC / "ut_config.json").read_text())
        raw["cache"] = {"dir": str(self.d / "rc")}
        raw["kb"] = {"db_path": str(self.d / "kb.sqlite3")}
        config.write_text(json.dumps(raw))
        settings = ba.Settings.model_validate(raw.get("brain", {}))
        import agent_sourcecode_corrector as a4
        for module in (a1, a2, a3, a4):
            with self.subTest(module.__name__):
                agent = module.build_agent(settings, str(config))
                self.assertIsInstance(agent.answer_cache, ba.AnswerCache)
                self.assertEqual(Path(agent.answer_cache.path).parent, self.d / "rc")
        agent3 = a3.build_agent(settings, str(config))
        self.assertEqual(sorted(k for k, v in agent3.run_caches.items() if v is not None),
                         ["failure_analysis", "mutations", "objects", "search_corpus", "test_coverage"])
        self.assertIsNotNone(a4.build_agent(settings, str(config)).run_caches["failure_analysis"])     # CA-9
        self.addCleanup(setattr, a1.FrontEnd, "cache", None)
        a1.build_agent(settings, str(config))
        self.assertEqual(a1.FrontEnd.cache.root.parent, self.d / "rc")                              # CA-11
        with mock.patch.dict(os.environ, {"UT_CACHE": "off"}):
            self.assertIsNone(a2.build_agent(settings, str(config)).answer_cache)
            self.assertTrue(all(v is None for v in a3.build_agent(settings, str(config)).run_caches.values()))
            self.assertIsNone(a4.build_agent(settings, str(config)).run_caches["failure_analysis"])
            a1.build_agent(settings, str(config))
            self.assertIsNone(a1.FrontEnd.cache)

    def test_a_folder_capture_key_follows_the_build_files(self):                          # CA-6
        repo = copy_pilot("c_pilot", self.d)
        orch = up.Orchestrator.__new__(up.Orchestrator)
        orch.config_path = self.d / "missing.json"
        s = types.SimpleNamespace(mode="folder", workspace=repo, req=uc.PipelineRequest(repo_root=str(repo),
                                                                                       sut_file="src/thermostat.c"))
        first = orch._folder_capture_key(s)
        self.assertTrue(first.startswith("folder"))
        self.assertEqual(first, orch._folder_capture_key(s))
        (repo / "src/thermostat.c").write_text((repo / "src/thermostat.c").read_text() + "\n/* edit */\n")
        self.assertEqual(first, orch._folder_capture_key(s))                   # a source edit: the same build
        (repo / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.10)\nproject(x C)\n")
        self.assertNotEqual(first, orch._folder_capture_key(s))
        s.mode = "repository"
        self.assertEqual(orch._folder_capture_key(s), "")                      # a commit has its own key

    @unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
    def test_objects_and_coverage_of_tests_alone_come_from_the_cache(self):             # CA-3, CA-5
        repo = copy_pilot("c_pilot", self.d)
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/thermostat.c"))
        plan = run(a1.AnalyzerAgent().plan(uc.PlanRequest(layout=layout)))
        bundle = run(a2.GeneratorAgent(config=a2.GeneratorConfig(mode="deterministic")).generate_bundle(
            uc.GenerationRequest(plan=plan)))

        def agent():
            comp = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "w")),
                                       router=ba.BrainRouter([DownBrain()], 0.0))
            comp.run_caches = {k: uc.RunCache(self.d / "rc", k) for k in ("test_coverage", "objects")}
            return comp
        first = agent()
        req = uc.VerifyRequest(layout=layout, stage="observe", bundle=bundle, measure_tests=list(bundle.tests))
        one = first.execute(req)
        self.assertTrue(one.link_ok, one.reason)
        self.assertEqual(first.run_caches["objects"].hits, 0)
        second = agent()
        two = second.execute(req)
        self.assertGreater(second.run_caches["objects"].hits, 0)              # harness, stubs, suite [CA-5]
        self.assertEqual(two.test_items, one.test_items)                       # the same measurement [CA-3]
        self.assertGreater(second.run_caches["test_coverage"].hits, 0)
        self.assertTrue(all("from the cache" in second._per_test_runs[n] for n in two.test_items))
        self.assertEqual([(t.name, t.status) for t in two.tests], [(t.name, t.status) for t in one.tests])
        # a header the stubs read changes: their object is compiled again
        header = next(p for p in (repo / "include").glob("*.h"))
        header.write_text(header.read_text() + "\n/* changed */\n")
        third = agent()
        third.execute(req)
        self.assertGreater(third.run_caches["objects"].misses, 0)


    # ---------------------------------------------------------------- CA-8 .. CA-14
    def _orchestrator(self, repo: Path):
        config = self.d / "ut_config.json"
        config.write_text(json.dumps({"cache": {"dir": str(self.d / "rc")}}))
        orch = up.Orchestrator.__new__(up.Orchestrator)
        orch.config_path, orch.hub, orch.no_cache = config, None, False
        orch.layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/thermostat.c"))
        return orch

    def test_what_missed_in_an_earlier_run_goes_to_the_brain(self):                   # CA-8
        repo = copy_pilot("c_pilot", self.d)
        orch = self._orchestrator(repo)
        fn = orch.layout.functions[0]
        req = uc.PipelineRequest(repo_root=str(repo), sut_file="src/thermostat.c")

        def state():
            s = up.RunState(req=req, repo=repo, out=self.d / f"out{uuid.uuid4().hex[:6]}", workspace=repo)
            s.origin = {}
            return s
        s = state()
        s.journal._entry(fn.qualified_name, ["G1"], "wrong_inputs", "test_x failed: expected 1, actual 0",
                         tried="UT_TEST(test_x) ...", iteration=1, attempt=1, brain="qwen", strategy="brain scenario")
        s.journal._entry(fn.qualified_name, ["G2"], "wrong_inputs", "covered since", iteration=1)
        s.crashed_calls = [f"{fn.name}(-1)"]
        gaps = [types.SimpleNamespace(gap_id="G1")]                        # G2 was covered by the end of the run
        s.best_result = types.SimpleNamespace(coverage=types.SimpleNamespace(functions=[types.SimpleNamespace(gaps=gaps)]))
        self.assertEqual(orch._store_journal(s), 1)
        later = state()
        self.assertEqual(orch._reuse_journal(later), 2)
        excerpt = later.journal.excerpt()[fn.qualified_name]
        self.assertIn("earlier run: G1", excerpt)
        self.assertNotIn("G2", excerpt)
        self.assertIn(f"{fn.name}(-1)", later.crashed_calls)
        self.assertEqual(later.attempts, {})                                    # not an attempt of this run [LA-5]
        self.assertTrue(any("[CA-8]" in n for n in later.notes))
        # the function changes: nothing of it is shown again
        sut = repo / "src/thermostat.c"
        lines = sut.read_text().splitlines()
        lines.insert(fn.line_start, "    /* changed */")
        sut.write_text("\n".join(lines) + "\n")
        orch.layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/thermostat.c"))
        self.assertEqual(orch._reuse_journal(state()), 0)

    def test_a_decision_on_a_failure_is_reused_for_the_same_failure(self):            # CA-9
        comp = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "w")),
                                   router=ba.BrainRouter([DownBrain()], 0.0))
        comp.run_caches = {"failure_analysis": uc.RunCache(self.d / "rc", "failure_analysis")}
        layout = types.SimpleNamespace(sut_sha256="abc")
        asked = []

        async def ask():
            asked.append(1)
            return {"root_cause": {"choice": "test_generation_defect",
                                   "probabilities": {"test_generation_defect": 0.9}}}
        facts = lambda run_dir, address: uc.neutral_failure(json.dumps(
            {"test_code": "UT_TEST(t) { f(); }", "actual": address, "evidence": [f"{run_dir}/repo/x.c:3"]}), run_dir)
        first = run(comp._analysed("A3.root_cause", layout, facts("/w/20261025-171605-e0f3ef8a", "0x7ffd12345678"),
                                   set(uc.ROUTES), ask, "root_cause"))
        again = run(comp._analysed("A3.root_cause", layout, facts("/w/20261026-090000-aaaaaa11", "0x7ffe99999999"),
                                   set(uc.ROUTES), ask, "root_cause"))
        self.assertEqual(len(asked), 1)                                 # other run, other address: the same failure
        self.assertEqual(again[0], first[0])
        self.assertIn("[CA-9]", again[2])
        run(comp._analysed("A3.root_cause", types.SimpleNamespace(sut_sha256="other"),
                           facts("/w/x", "0x7ffd12345678"), set(uc.ROUTES), ask, "root_cause"))
        self.assertEqual(len(asked), 2)                                 # another SUT version: asked
        # Agent4: stored candidates pass the same fragment check before they are used
        agent4 = a4.CorrectorAgent(router=ba.BrainRouter([DownBrain()], 0.0))
        agent4.run_caches = {"failure_analysis": uc.RunCache(self.d / "rc", "failure_analysis")}
        made = []

        async def generate(task, context, model, **kw):
            made.append(1)
            return a4.PatchProposals(candidates=[a4.PatchProposal(original_fragment="return 2;",
                                                                  corrected_fragment="return 1;", rationale="E2")])
        agent4.generate = generate
        context = [ba.ContextPart("failure", "test failed in /w/20261025-171605-e0f3ef8a/repo")]
        need = ba.OutputNeed(items=1, tokens_per_item=100, overhead=10)
        check = lambda p: None
        one = run(agent4._generate_candidates("fix", context, check, need, "/p", "sha"))
        two = run(agent4._generate_candidates("fix", context, check, need, "/p", "sha"))
        self.assertEqual(len(made), 1)
        self.assertEqual([p.corrected_fragment for p in two], [p.corrected_fragment for p in one])

        def refuse(p):
            raise ba.AgentFault("MODEL_OUTPUT_INVALID", "not in this source")
        run(agent4._generate_candidates("fix", context, refuse, need, "/p", "sha"))
        self.assertEqual(len(made), 2)                                  # a stored candidate is checked like a new one

    @unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
    def test_a_parse_of_unchanged_files_is_reused_in_another_folder(self):            # CA-11
        self.addCleanup(setattr, a1.FrontEnd, "cache", None)
        a1.FrontEnd.cache = uc.RunCache(self.d / "rc", "code_analysis")
        tools = uc.Tools.load(uc.DEFAULT_MANIFEST)

        def project(name: str) -> Path:
            repo = self.d / name
            (repo / "src").mkdir(parents=True)
            (repo / "src/a.h").write_text("int g(int x);\n")
            (repo / "src/a.c").write_text('#include "a.h"\nint f(int x) { return g(x) + 1; }\n')
            return repo

        def parse(repo: Path):
            fe = a1.FrontEnd(tools, repo, "src/a.c", ["-I{repo}/src"], "c")
            self.assertTrue(fe.parse(), fe.errors)
            return fe
        first = parse(project("one"))
        cache = a1.FrontEnd.cache
        self.assertEqual((cache.hits, cache.misses), (0, 1))
        second = parse(project("20261025-171605-e0f3ef8a"))           # another folder, the same files
        self.assertEqual(cache.hits, 1)
        self.assertEqual([q for _, q in second.definitions()], [q for _, q in first.definitions()])
        self.assertEqual(second.headers, ["src/a.h"])                 # paths of this folder
        repo = self.d / "20261025-171605-e0f3ef8a"
        (repo / "src/a.h").write_text("int g(int x);\nint h(void);\n")      # a header changes: parsed anew
        parse(repo)
        self.assertEqual((cache.hits, cache.misses), (1, 2))

    def test_an_identical_mutation_check_is_not_run_again(self):                      # CA-12
        comp = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "w")))
        comp.run_caches = {"mutations": uc.RunCache(self.d / "rc", "mutations")}
        comp._mutation_context = lambda layout: "headers|compiler"
        repo = copy_pilot("c_pilot", self.d)
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/thermostat.c"))
        req = uc.OracleRequest(layout=layout, test="t", function="f", original_suite="A", changed_suite="B",
                               bundle=uc.FileActionBundle(bundle_id="b1"))
        runs = []

        def work(r):
            runs.append(1)
            return uc.OracleResult(test=r.test, decision="accepted", mutants=4)
        self.assertEqual(comp._mutations_cached("oracle", req, work, ("accepted",)).mutants, 4)
        self.assertEqual(comp._mutations_cached("oracle", req.model_copy(update={"bundle": uc.FileActionBundle(
            bundle_id="b1")}), work, ("accepted",)).decision, "accepted")
        self.assertEqual(len(runs), 1)
        comp._mutations_cached("oracle", req.model_copy(update={"changed_suite": "C"}), work, ("accepted",))
        self.assertEqual(len(runs), 2)                                  # another text: run
        cut = lambda r: (runs.append(1), uc.FaultCheckResult(suggestion_id=r.suggestion_id, decision="not_checked"))[1]
        fault = uc.FaultCheckRequest(layout=layout, suggestion_id="S-1", original_source="a", suggested_source="b")
        comp._mutations_cached("fault", fault, cut, ("kept",))
        again = comp._mutations_cached("fault", fault.model_copy(update={"suggestion_id": "S-2"}), cut, ("kept",))
        self.assertEqual(len(runs), 4)                                  # a check cut by its time limit is not kept
        self.assertEqual(again.suggestion_id, "S-2")

    def test_a_search_starts_from_the_inputs_an_earlier_search_kept(self):           # CA-13
        comp = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "w"), max_fuzz_replays=4))
        comp.run_caches = {"search_corpus": uc.RunCache(self.d / "rc", "search_corpus")}
        key = comp._corpus_key("int target(void);", "ut_search", self.d)
        corpus = self.d / "c1"
        corpus.mkdir()
        for name, data in (("seed_0", b"\0" * 8), ("s_1", b"one"), ("s_2", b"two"), ("s_3", b"three"),
                           ("s_big", b"x" * (64 * 1024 + 1))):
            (corpus / name).write_bytes(data)
        comp._keep_corpus(corpus, key)
        fresh = self.d / "c2"
        fresh.mkdir()
        self.assertEqual(comp._seed_corpus(fresh, key), 2)              # half of the replay limit
        kept = {p.read_bytes() for p in fresh.iterdir()}
        self.assertTrue(all(p.name.startswith("seed_c") for p in fresh.iterdir()))
        self.assertTrue(kept <= {b"one", b"two", b"three"})
        self.assertEqual(comp._seed_corpus(self.d, comp._corpus_key("int other(void);", "ut_search", self.d)), 0)

    def test_a_large_suite_splits_only_when_its_parts_mean_the_same(self):           # CA-14
        tests = "".join(f"/* evidence: E8 */\nUT_TEST(t{k})\n{{\n    UT_EXPECT_EQ_INT(f({k}), {k});\n}}\n\n" for k in range(5))
        table = "UT_SUITE_BEGIN\n" + "".join(f"    UT_CASE(t{k})\n" for k in range(5)) + "UT_SUITE_END\n"
        head = '#include "ut_harness.h"\n#include "x.h"\nint ut_static_f(int x);\nstatic int twice(int x) { return 2 * x; }\n' \
               "typedef struct { int a; } pair_t;\nstruct s { int b; };\n\n"
        split = a3.split_suite(head + tests + table, 3)
        self.assertIsNotNone(split)
        prologue, parts, text, line = split
        self.assertEqual([p[0] for p in parts], [f"t{k}" for k in range(5)])
        self.assertEqual(((head + tests + table).splitlines()[parts[1][1] - 1]), "UT_TEST(t1)")
        self.assertTrue(text.startswith("UT_SUITE_BEGIN") and (head + tests + table).splitlines()[line - 1] == "UT_SUITE_BEGIN")
        self.assertIsNone(a3.split_suite(head + tests + table, 5))                       # not more than the limit
        for form in ("static int calls;\n", "int (*hook)(void);\n", "struct s g_s;\n",
                     "static int counter(void) { static int n; return ++n; }\n"):
            with self.subTest(form):
                self.assertIsNone(a3.split_suite(head + form + tests + table, 3))       # state of its own per part
        between = tests.replace("UT_TEST(t3)", "static int helper(void) { return 1; }\nUT_TEST(t3)")
        self.assertIsNone(a3.split_suite(head + between + table, 3))                    # code between tests
        braces = tests.replace("UT_EXPECT_EQ_INT(f(2), 2);", 'UT_EXPECT_EQ_STR(s("}{"), "}"); /* } */')
        self.assertEqual(len(a3.split_suite(head + braces + table, 3)[1]), 5)            # braces in text and comments

    @unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
    def test_a_suite_in_parts_runs_like_the_one_file(self):                         # CA-14
        repo = copy_pilot("c_pilot", self.d)
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/thermostat.c"))
        plan = run(a1.AnalyzerAgent().plan(uc.PlanRequest(layout=layout)))
        bundle = run(a2.GeneratorAgent(config=a2.GeneratorConfig(mode="deterministic")).generate_bundle(
            uc.GenerationRequest(plan=plan)))
        suite = next(a for a in bundle.actions if a.role == "testsuite")
        name = bundle.tests[1]
        at = suite.content.index("{", suite.content.index(f"UT_TEST({name})"))
        suite.content = suite.content[:at + 1] + "\n    UT_EXPECT_EQ_INT(1, 2);" + suite.content[at + 1:]   # one fails

        def agent(split: int):
            comp = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(self.d / "w"), split_suite_tests=split),
                                       router=ba.BrainRouter([DownBrain()], 0.0))
            comp.run_caches = {"objects": uc.RunCache(self.d / "rc", "objects")}
            return comp
        req = uc.VerifyRequest(layout=layout, stage="observe", bundle=bundle, measure_tests=[])
        one = agent(0).execute(req)
        parts = agent(2).execute(req)
        self.assertTrue(one.link_ok and parts.link_ok, parts.reason)
        self.assertTrue((Path(parts.run_dir) / "suite_parts").is_dir())
        self.assertFalse((Path(one.run_dir) / "suite_parts").exists())
        view = lambda r: [(t.name, t.status, t.file.replace(r.run_dir, "<run>"), t.line, t.expected, t.actual)
                          for t in r.tests]
        self.assertEqual(view(parts), view(one))                            # the same results, files and lines
        self.assertTrue(any(t.status == "failed" and t.line for t in parts.tests))
        cover = lambda r: r.coverage.covered_items if r.coverage else None
        self.assertEqual(cover(parts), cover(one))
        again = agent(2)
        again.execute(req)
        self.assertGreater(again.run_caches["objects"].hits, 2)             # unchanged parts [CA-5]
        broken = bundle.model_copy(deep=True)
        bad = next(a for a in broken.actions if a.role == "testsuite")
        bad.content = bad.content.replace("UT_EXPECT_EQ_INT(1, 2);", "UT_EXPECT_EQ_INT(undeclared_name, 2);", 1)
        quarantined = agent(2).execute(req.model_copy(update={"bundle": broken}))
        self.assertIn(name, {f.affected_test for f in quarantined.quarantined})   # messages of the one file [A3-CL12]


@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
class LargePilotFindingsTests(unittest.TestCase):
    """TP-25: what the large pilots found, on a small file of the same forms (A1-DA10, A1-DA15, A2-21k)."""
    def test_unnamed_namespaces_and_dependencies_without_a_definition(self):
        tmp = tmpdir()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "repo"
        (root / "src").mkdir(parents=True)
        (root / "include").mkdir()
        (root / "include/x.hpp").write_text(
            "namespace hal { void pwm(int duty); }\nnamespace pt {\nclass IBus {\npublic:\n    virtual ~IBus() = default;\n"
            "    virtual bool read(int channel) = 0;\n};\nint use(IBus& bus, int v);\n}\n")
        (root / "src/x.cpp").write_text(
            '#include "x.hpp"\nnamespace pt {\nnamespace {\nint g_last = 0;\nint key_of(int s)\n{\n    return s * 3;\n}\n}\n'
            "int use(IBus& bus, int v)\n{\n    hal::pwm(v);\n    g_last = v;\n    return bus.read(v) ? key_of(v) : 0;\n}\n}\n")
        agent = a1.AnalyzerAgent()
        layout = agent.discover(uc.DiscoverRequest(repo_root=str(root), sut_file="src/x.cpp"))
        build = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(Path(tmp.name) / "w"))).capture(
            uc.CaptureRequest(layout=layout))
        layout = run(agent.dependencies(uc.DependencyRequest(layout=layout, build=build)))
        key_of = next(f for f in layout.functions if f.name.endswith("key_of"))
        self.assertEqual(key_of.qualified_name, "pt::key_of")                     # the name a test reads
        self.assertTrue(key_of.is_static)
        self.assertEqual([f.name for f in a2.static_functions(layout)], [key_of.name])
        self.assertTrue(any(v.name == "pt::g_last" for v in layout.sut_state), layout.sut_state)
        pwm = next(d for d in layout.dependencies.entries if d.name == "pwm")
        self.assertEqual(pwm.decision, "stub", pwm.reason)                         # no definition is never "linked"

    @unittest.skipIf(os.name == "nt", "the sanitizers of TP-26 run on Linux")
    def test_rule_tests_pass_the_sanitizers(self):                             # TP-26, A2-21h, A2-21f, A3-45e
        """Forms where a rule test wrote or read past its data on Windows: a pointer written at an index, a loop over
        a buffer bounded by a length parameter, a C++ function that throws for a solved argument."""
        tmp = tmpdir()
        self.addCleanup(tmp.cleanup)
        san = ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        for name, sut, text in (
                ("c", "src/bits.c",
                 "#include <stdint.h>\n#include <stddef.h>\n"
                 "int set_bits(uint8_t *data, uint8_t start, uint8_t length)\n{\n"
                 "    if (data == NULL || length > 32u || start + length > 64u) {\n        return 0;\n    }\n"
                 "    for (uint8_t i = 0u; i < length; i++) {\n        uint8_t bit = (uint8_t)(start + i);\n"
                 "        data[bit / 8u] = (uint8_t)(data[bit / 8u] | (1u << (bit % 8u)));\n    }\n    return 1;\n}\n"
                 "uint32_t sum(const uint32_t buf[], uint32_t len)\n{\n    uint32_t s = 0u;\n"
                 "    for (uint32_t i = 0u; i < len; i++) {\n        s += buf[i];\n    }\n    return s;\n}\n"),
                ("cpp", "src/lim.cpp",
                 "#include <stdexcept>\nint limit(int v)\n{\n    if (v > 1000 || v < -1000) {\n"
                 "        throw std::invalid_argument(\"out of range\");\n    }\n    return v * 2;\n}\n")):
            with self.subTest(name):
                root = Path(tmp.name) / name
                (root / "src").mkdir(parents=True)
                (root / sut).write_text(text)
                agent = a1.AnalyzerAgent()
                layout = agent.discover(uc.DiscoverRequest(repo_root=str(root), sut_file=sut))
                comp = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(root / "w"), extra_flags=san))
                link = a3.Toolchain.link_flags
                with mock.patch.object(a3.Toolchain, "link_flags", lambda self, cpp: link(self, cpp) + san), \
                        mock.patch.dict(os.environ, {"ASAN_OPTIONS": "detect_leaks=0"}):
                    layout = run(agent.dependencies(uc.DependencyRequest(layout=layout, build=comp.capture(
                        uc.CaptureRequest(layout=layout)))))
                    last = comp.execute(uc.VerifyRequest(layout=layout, stage="probe"))
                    plan = run(agent.plan(uc.PlanRequest(layout=layout, last=last, iteration=1)))
                    gen = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="deterministic"))
                    with mock.patch.object(gen, "_brain_values", return_value={}):
                        bundle = run(gen.generate_bundle(uc.GenerationRequest(plan=plan)))
                    result = comp.execute(uc.VerifyRequest(layout=layout, stage="verify", bundle=bundle))
                self.assertTrue(result.link_ok, result.reason)
                self.assertGreater(len(result.tests), 3)
                self.assertEqual([(t.name, t.message[:200]) for t in result.tests if t.status != "passed"], [])

    def test_a_damaged_profile_is_left_out_of_the_merge(self):                       # A3-60a
        tmp = tmpdir()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "repo"
        (root / "src").mkdir(parents=True)
        (root / "src/a.c").write_text("int twice(int x)\n{\n    return x > 3 ? 2 * x : x;\n}\n")
        agent = a1.AnalyzerAgent()
        layout = agent.discover(uc.DiscoverRequest(repo_root=str(root), sut_file="src/a.c"))
        comp = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(Path(tmp.name) / "w")))
        layout = run(agent.dependencies(uc.DependencyRequest(layout=layout, build=comp.capture(
            uc.CaptureRequest(layout=layout)))))
        plan = run(agent.plan(uc.PlanRequest(layout=layout, last=comp.execute(uc.VerifyRequest(layout=layout,
                                                                                               stage="probe")),
                                             iteration=1)))
        bundle = run(a2.GeneratorAgent(config=a2.GeneratorConfig(mode="deterministic")).generate_bundle(
            uc.GenerationRequest(plan=plan)))
        result = comp.execute(uc.VerifyRequest(layout=layout, stage="verify", bundle=bundle))
        run_dir = Path(result.run_dir)
        (run_dir / "prof" / "crashed_test.profraw").write_bytes(b"\x81rforpl\xff truncated")
        binary = next(p for p in run_dir.rglob("*") if p.is_file() and p.name.startswith("ut_runner")
                      and p.suffix in ("", ".exe"))
        report = comp._coverage(comp.toolchain(), {"binary": binary}, run_dir, Path(result.run_dir) / "repo", layout)
        self.assertTrue(report.measured)
        self.assertTrue(any("crashed_test" in n for n in comp._measure_notes), comp._measure_notes)

    def test_a_failing_stub_gets_its_origin_and_rule_repairs(self):            # A3-CL14a, A3-CL14b, ARCH-21a
        """Two entries of hal::pwm - the header's and one the brain invented: only the first is rendered, so the
        program links (one definition per stub). The invented one alone does not build: the repair's facts name its
        origin, and the rules' own repairs, tried without a brain, take the build forward."""
        tmp = tmpdir()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "repo"
        (root / "src").mkdir(parents=True)
        (root / "include").mkdir()
        (root / "include/hal.hpp").write_text("#pragma once\n#include <cstdint>\nnamespace hal {\n"
                                              "void pwm(std::uint8_t channel, std::uint16_t duty);\n}\n")
        (root / "src/fan.cpp").write_text('#include "hal.hpp"\nint fan(int d)\n{\n    hal::pwm(1, '
                                          'static_cast<std::uint16_t>(d));\n    return d > 10 ? 1 : 0;\n}\n')
        agent = a1.AnalyzerAgent()
        layout = agent.discover(uc.DiscoverRequest(repo_root=str(root), sut_file="src/fan.cpp"))
        comp = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(Path(tmp.name) / "w")),
                                   router=ba.BrainRouter([DownBrain()], 0.0))           # TP-21: no real brain
        layout = run(agent.dependencies(uc.DependencyRequest(layout=layout, build=comp.capture(
            uc.CaptureRequest(layout=layout)))))
        rule = next(d for d in layout.dependencies.entries if d.name == "pwm")
        decl = "std::uint16_t hal::pwm(hal::pwm_channel_t, std::uint16_t);"
        rt, params = a1.parse_declaration(decl)
        invented = uc.Dependency(name="pwm", qualified_name="hal::pwm", declaration=decl, decision="stub",
                                 decided_by="brain declaration (qwen_remote)", return_type=rt, params=params)
        layout = layout.model_copy(update={"dependencies": layout.dependencies.model_copy(
            update={"entries": layout.dependencies.entries + [invented]})})
        plan = uc.UTPlan(plan_id="p", stage="setup", layout=layout,
                         stubs=[s for s in (a1.stub_spec(rule), a1.stub_spec(invented)) if s])
        generator = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="deterministic"))
        bundle = run(generator.generate_bundle(uc.GenerationRequest(plan=plan)))
        self.assertTrue(comp.execute(uc.VerifyRequest(layout=layout, stage="build_check", bundle=bundle)).link_ok)
        plan = plan.model_copy(update={"stubs": [s for s in (a1.stub_spec(invented),) if s]})     # the invented alone
        bundle = run(generator.generate_bundle(uc.GenerationRequest(plan=plan)))
        req = uc.VerifyRequest(layout=layout, stage="build_check", bundle=bundle)
        result = comp.execute(req)
        self.assertFalse(result.link_ok)
        origins = comp._line_origins(req, result)
        self.assertIn("PROPOSED BY THE BRAIN", origins)
        self.assertIn("include/hal.hpp", comp._project_declarations(req, result))
        options = comp._repair_options(req, comp._repairable(result, req))
        self.assertTrue(any(text.startswith("remove the") for text, _ in options.values()), options)
        for _, repair in options.values():                       # a test, the SUT or a project file: never
            self.assertTrue(all(e.path.startswith("tests/") for e in repair.edits))
        before = len([d for d in result.diagnostics if d.severity == "error"])
        repaired = run(comp._repair_generated(result, req))      # no brain here: the rules' order, the tool decides
        after = len([d for d in repaired.diagnostics if d.severity == "error"])
        self.assertLess(after, before)
        self.assertTrue(any("option" in a for e in comp.escalations for a in e["attempts"]), comp.escalations)

    def test_several_namespaces_in_one_file(self):            # A2-21z, A2-21ai, A2-21aj, A2-21k, A1-PL1
        """The same names in several namespaces (functions, statics, state, enums, stubs): the files build, the rule
        round builds and runs without a brain, state is set and read by its own helpers, and the brain is told the
        state of the function's own namespace."""
        tmp = tmpdir()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "repo"
        (root / "src").mkdir(parents=True)
        (root / "include").mkdir()
        (root / "include/m.hpp").write_text(
            "#pragma once\nnamespace a { enum class Mode { Off, On }; int step(int v); }\n"
            "namespace b { enum class Mode { Idle, Run, Fault }; int step(int v); }\n"
            "namespace b { namespace inner { int step(int v); } }\n"
            "namespace hal { int init(int ch); }\nnamespace nvm { int init(int ch); }\n"
            "struct Limits { int limit; int counter; };\nvoid configure(int limit);\n")
        (root / "src/m.cpp").write_text(
            '#include "m.hpp"\nnamespace a {\n'
            "namespace {\nint counter = 0;\nMode g_mode = Mode::Off;\nint helper(int x) { return x + 1; }\n}\n"
            "static int limit = 10;\n"
            "int step(int v) { if (v > limit) { counter++; g_mode = Mode::On; } return helper(v) + counter + "
            "hal::init(v) + nvm::init(v); }\n}\n"
            "namespace b {\nnamespace {\nint counter = 0;\nMode g_mode = Mode::Idle;\nint helper(int x) { return x * 2; }\n}\n"
            "static int limit = 20;\n"
            "int step(int v) { if (v > limit && g_mode != Mode::Fault) { counter += 2; g_mode = Mode::Run; } "
            "return helper(v) - counter; }\n"
            "namespace inner { static int limit = 5; int step(int v) { return v < limit ? a::step(v) : b::step(v); } }\n}\n")
        agent = a1.AnalyzerAgent()
        layout = agent.discover(uc.DiscoverRequest(repo_root=str(root), sut_file="src/m.cpp"))
        comp = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(Path(tmp.name) / "w")))
        layout = run(agent.dependencies(uc.DependencyRequest(layout=layout, build=comp.capture(
            uc.CaptureRequest(layout=layout)))))
        self.assertEqual(sorted(a2.wrapper_name(f) for f in a2.static_functions(layout)),
                         ["ut_static_a__helper", "ut_static_b__helper"])
        # a field or a parameter of the same name in a header does not make a variable of the SUT file visible
        self.assertEqual(uc.hidden_state(layout, uc.headers_text(layout)), {v.name for v in layout.sut_state})
        lines = (root / "src/m.cpp").read_text().splitlines()
        b_step = next(f for f in layout.functions if f.qualified_name == "b::step")
        listed, _ = uc.function_state(b_step, layout, lines)
        self.assertEqual({v.name for v in listed}, {"b::limit", "b::g_mode", "b::counter"})   # its own namespace
        self.assertNotIn("a::counter", [v.name for v in listed])
        inner = next(f for f in layout.functions if f.qualified_name == "b::inner::step")
        self.assertEqual([v.name for v in uc.function_state(inner, layout, lines)[0]][0], "b::inner::limit")
        self.assertIn("a::counter", [v.name for v in uc.function_state(inner, layout, lines)[0]])   # through a::step
        part = a2.state_part(b_step, layout, lines)
        self.assertIn("ut_state_get_b_g_mode(void)", part)
        allowed = {uc.HIDDEN_MARK + v.name for v in layout.sut_state} | {
            uc.state_setter_name(v) for v in layout.sut_state}
        problem = uc.name_problem("counter = 1;", set(), allowed, "b::step")
        self.assertLess(problem.index("ut_state_set_b_counter"), problem.index("ut_state_set_a_counter"))
        self.assertNotIn("ut_state_set_b_counter", uc.name_problem("a::counter = 1;", set(), allowed, "b::step"))
        # the rule round: tests built, measured and run; no brain is asked for the scenarios left [A1-PL1]
        plan = run(agent.plan(uc.PlanRequest(layout=layout, last=None, iteration=1)))
        plan = plan.model_copy(update={"rules_only": True, "scenarios": plan.scenarios + [uc.Scenario(
            scenario_id="S-x", function="b::step", function_line=b_step.line_start, gap_ids=["BC:x"],
            description="d", characterization=False)]})
        gen = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="deterministic_then_llm"))
        with mock.patch.object(gen, "generate", side_effect=AssertionError("no brain test in the rule round")), \
                mock.patch.object(gen, "_brain_values", return_value={}):
            bundle = run(gen.generate_bundle(uc.GenerationRequest(plan=plan)))
        self.assertTrue(any("[A1-PL1]" in n for n in bundle.notes), bundle.notes)
        suite = next(a for a in bundle.actions if a.role == "testsuite")
        extra = ("UT_TEST(test_b_state_helpers)\n{\n    ut_state_set_b_limit(1);\n    ut_state_set_b_g_mode(0);\n"
                 "    UT_EXPECT_EQ_INT(b::step(3), 6 - 2);\n    UT_EXPECT_EQ_INT(ut_state_get_b_counter(), 2);\n"
                 "    UT_EXPECT_EQ_INT(ut_state_get_b_g_mode(), 1);\n    UT_EXPECT_EQ_INT(ut_state_get_a_counter(), 0);\n}\n\n")
        suite.content = suite.content.replace("UT_SUITE_BEGIN", extra + "UT_SUITE_BEGIN").replace(
            "UT_SUITE_END", "    UT_CASE(test_b_state_helpers)\nUT_SUITE_END")
        bundle.tests.append("test_b_state_helpers")
        result = comp.execute(uc.VerifyRequest(layout=layout, stage="verify", bundle=bundle))
        self.assertTrue(result.link_ok, result.reason)
        failed = [(t.name, t.message) for t in result.tests if t.status != "passed"]
        self.assertEqual(failed, [])
        self.assertIn("test_b_state_helpers", [t.name for t in result.tests])


class AgentProgramTests(unittest.TestCase):
    """An agent runs as its own program: everything it uses is defined before its __main__ block starts the server
    (a definition after it never runs there, while an import in a test still sees it)."""
    def test_nothing_is_defined_after_the_main_block(self):
        import ast
        for path in sorted(SRC.glob("*.py")) + [SRC.parent / "BaseAgent" / "baseagent.py"]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            main = next((k for k, n in enumerate(tree.body) if isinstance(n, ast.If)
                         and "__name__" in ast.unparse(n.test)), None)
            with self.subTest(path.name):
                self.assertEqual([ast.unparse(n)[:60] for n in tree.body[main + 1:]] if main is not None else [], [])


class MeasuredRightTests(unittest.TestCase):
    """A3-OBS6, A2-21ah, VIEW-12a, LA-7 c: what a run measures and writes is checked against the tool itself."""

    def layout(self):
        return uc.LayoutProfile(repo_root=".", sut_file="a.c", language="c", component_root=".", writable_test_dir="t",
                                writable_stub_dir="t/s", suite_file="t/test_a.c", sut_sha256="0" * 64,
                                functions=[uc.FunctionInfo(name="f", qualified_name="f", signature="int f(int x)",
                                                           file="a.c", line_start=1, line_end=3, return_type="int"),
                                           uc.FunctionInfo(name="g", qualified_name="g", signature="void g(void)",
                                                           file="a.c", line_start=4, line_end=5, return_type="void")])

    def test_a_per_test_measurement_is_never_used_unchecked(self):                           # A3-OBS6
        comp = a3.CompilationAgent(config=a3.CompilationConfig())
        sources = {"t1": "UT_TEST(t1) { UT_OBSERVE_INT(\"o\", f(1)); }", "t2": "UT_TEST(t2) { int k = 1; }"}
        problem = comp._per_test_problem({"t1": [], "t2": []}, ["t1", "t2"], sources, False, None, {}, Path("."),
                                         Path("."), self.layout())
        self.assertIn("1 of 2 passed tests cover nothing when run alone (t1", problem)   # t2 calls no SUT code
        self.assertEqual(comp._per_test_problem({"t1": ["LC:2"], "t2": []}, ["t1", "t2"], sources, False, None, {},
                                                Path("."), Path("."), self.layout()), "")
        # nothing is dropped as redundant without a checked measurement
        self.assertEqual(a2.redundant_tests(["t1", "t2"], {}), [])

    def test_tests_are_checked_against_the_harness_itself(self):                              # A2-21ah
        api = uc.harness_api()
        for name in ("UT_REG_SET_LATER", "UT_CALL_ANY", "UT_EXPECT_ANY_THROW", "ut_later", "ut_asm_value"):
            self.assertIn(name, api)                                       # (c) known names, from the header
        self.assertEqual(api["UT_EXPECT_EQ_INT"], 2)
        problem = uc.macro_arity_problem("UT_EXPECT_EQ_INT(ut_obj.step(100000));")
        self.assertIn("UT_EXPECT_EQ_INT takes 2 argument(s), this call has 1", problem)       # (a)
        self.assertEqual(uc.check_kind(problem), "prediction")             # the brain's re-ask may correct it
        self.assertEqual(uc.macro_arity_problem('UT_EXPECT_EQ_INT(f(a, b), g(1)); UT_OBSERVE_INT("x", h(1, 2));'), "")
        body, changed = uc.void_values_as_statements('UT_OBSERVE_INT("t_1", (ut_obj.sync()));\n'
                                                      'UT_EXPECT_EQ_INT(g(), 0);', {"sync", "g"})   # (b)
        self.assertEqual(body, "ut_obj.sync();\ng();")
        self.assertEqual(len(changed), 2)
        self.assertEqual(a2.void_functions(self.layout()), {"g"})
        allowed = a2.GeneratorAgent._allowed_symbols(uc.UTPlan(plan_id="p", layout=self.layout(), stage="coverage"),
                                                     "", "")
        self.assertIn("UT_REG_SET_LATER", allowed)

    def test_the_ceiling_does_not_count_waiting_for_a_brain(self):                           # LA-7 c
        with tmpdir() as d:
            orch = up.Orchestrator(None)
            orch.ledger, orch.hub, orch.layout, orch.suite = up.Ledger(Path(d) / "l.jsonl"), None, None, ""
            orch.token_log, orch.escalation_log, orch._called, orch._last_agent = [], [], set(), ""
            orch.brain_seconds = 0.0

            class Client:
                def __init__(self, calls):
                    self.calls = calls

                async def call(self, tool, payload, signatures=None):
                    await asyncio.sleep(0.2)
                    return ba.ContractResponse(request_id="r", status="success", contract="EchoOut", data={},
                                               meta=ba.ContractMeta(llm_calls=self.calls))
            orch.clients = {"analyzer": Client(0), "generator": Client(2)}
            class Any_(up.BaseModel):
                pass
            run(orch.call("analyzer", "discover", uc.DiscoverRequest(repo_root=d, sut_file="a.c"), Any_))
            self.assertEqual(orch.brain_seconds, 0.0)                          # no brain: the pipeline's own time
            run(orch.call("generator", "generate", uc.DiscoverRequest(repo_root=d, sut_file="a.c"), Any_))
            self.assertGreater(orch.brain_seconds, 0.15)                       # with the brain: not counted

    def test_several_folders_run_on_one_page(self):                                          # VIEW-12a
        with tmpdir() as d:
            one, two = copy_pilot("c_pilot", Path(d)), copy_pilot("c_deps_pilot", Path(d))
            runner = up.RepositoryRun(None)
            runner.orchestrator.output_root = Path(d) / "out"
            hub = ba.ViewHub(Path(d) / "page", app="UT")
            runner._open_viewer = lambda out: (hub, None)
            seen = []

            class Fake:
                def __init__(self, prefix):
                    self.prefix = prefix

                async def run(self, req):
                    seen.append(self.prefix + req.sut_file)
                    return uc.PipelineResult(final_state="verified_measured", reason="ok", sut_file=req.sut_file,
                                             output_dir=str(Path(d) / "o"))
            runner._orchestrator = lambda: Fake(runner.view_prefix)
            results = run(runner.run_many(
                [uc.RepositoryRequest(repository=str(one), sut_files=["src/thermostat.c"]),
                 uc.RepositoryRequest(repository=str(two), sut_files=["src/ctrl.c", "src/brake_ctrl.c"])],
                ["c_pilot", "c_deps_pilot"]))
            self.assertEqual(seen, ["c_pilot: src/thermostat.c", "c_deps_pilot: src/ctrl.c",
                                    "c_deps_pilot: src/brake_ctrl.c"])         # every file named with its folder
            self.assertEqual(runner.progress.files, 3)                           # the progress counts all of them
            self.assertEqual([len(r.results) for r in results], [1, 2])
            self.assertTrue(all((Path(r.output_dir) / "summary.md").is_file() for r in results))
            pending = [e["data"]["pending"] for e in hub.events() if e["kind"] == "progress"]
            self.assertEqual(pending, [1, 0, 0])               # folders to come, never the running one [VIEW-12a]


class TypedValueTests(unittest.TestCase):
    """Structured scenarios, from the pilot-medium run of 2026-10-28: every value is checked and shaped by the type of
    what it sets - the same check a plain argument gets - and templates, enumerators and constants are written from
    the facts. The brain's answers of that run, replayed: C 106 -> 222 of 300 plans kept, C++ 36 of 41 -> 2 of 51
    tests that do not compile."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tmpdir()
        cls.layouts = {}
        for pilot, sut in (("c_medium_pilot", "src/climate_ctrl.c"), ("cpp_medium_pilot", "src/chassis_ctrl.cpp")):
            repo = copy_pilot(pilot, Path(cls.tmp.name))
            layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(repo), sut_file=sut))
            agent = a3.CompilationAgent(config=a3.CompilationConfig(work_dir=str(Path(cls.tmp.name) / f"w_{pilot}")))
            cls.layouts[pilot] = run(a1.AnalyzerAgent().dependencies(uc.DependencyRequest(
                layout=layout, build=agent.capture(uc.CaptureRequest(layout=layout)))))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def check(self, pilot: str, function: str, **answer):
        layout = self.layouts[pilot]
        agent = a1.AnalyzerAgent()
        fn = uc.function_of(function, layout)
        self.assertIsNotNone(fn, function)
        stubs = agent._stubs(layout, None)
        planned = a1.PlannedScenario.model_validate({**dict(
            gap_ids=["LC:1"], name="test_typed_case", purpose="p", args=[], stubs=[], variables=[], expected_return="",
            expected_after=[], evidence_ids=[], quotes=[], classification="characterization_gap", conflict="",
            information_sought=""), **answer})
        return agent._check_planned(planned, fn, {"LC:1"}, set(), {}, stubs, agent._constants_for_values(layout),
                                    agent._known_names(layout, stubs), layout)

    def test_c_values_take_the_form_of_their_type(self):
        args = [{"name": "in", "value": "1"}, {"name": "req", "value": "{0}"},
                {"name": "out", "fields": [{"field": "mix_pct", "value": "[0, 1, 2, 3]"}]}, {"name": "dt_ms", "value": "10"}]
        scenario, notes = self.check("c_medium_pilot", "clim_step", args=args,
                                     state=[{"variable": "s_mode", "index": -1, "field": "s_mode", "value": "CLIM_MODE_AUTO"}])
        self.assertIsNotNone(scenario, notes)
        setup = " ".join(scenario.setup)
        self.assertIn("ut_state_set_s_mode(CLIM_MODE_AUTO);", setup)              # a plain variable: no field
        self.assertIn("clim_inputs_t ut_in = {0};", setup)                          # a structure is its initializer
        self.assertIn("ut_out.mix_pct[3] = 3;", setup)                              # an array field, element by element
        self.assertEqual([a.value for a in scenario.args], ["&ut_in", "&ut_req", "&ut_out", "10"])
        scenario, notes = self.check("c_medium_pilot", "clim_step", args=args[:3] + [{"name": "dt_ms", "value": "120000"}])
        self.assertIsNone(scenario)
        self.assertTrue(any("outside the range of uint16_t" in n for n in notes), notes)

    def test_cpp_values_come_from_the_facts(self):
        layout = self.layouts["cpp_medium_pilot"]
        self.assertEqual(uc.instantiated_class("vehicle::chassis::MovingAverage", layout),
                         "vehicle::chassis::MovingAverage<std::uint16_t, 4>")       # the instance the SUT uses
        crc = uc.function_of("crc8", layout)
        self.assertEqual(next(p.kind for p in crc.params if p.name == "data"), "ptr_scalar")  # std::uint8_t is uint8_t
        scenario, notes = self.check("cpp_medium_pilot", "vehicle::chassis::select_mode",
                                     args=[{"name": "current", "value": "Mode::Service", "elements": ["Mode::Service"]},
                                           {"name": "button", "value": "2"}, {"name": "vehicle_stopped", "value": "0"}])
        self.assertEqual(scenario.args[0].value, "vehicle::chassis::Mode::Service", notes)   # the enumerator in full
        scenario, notes = self.check("cpp_medium_pilot", "vehicle::chassis::pump_step",
                                     args=[{"name": "request", "value": "1"}, {"name": "dt_ms", "value": "0"}],
                                     state=[{"variable": "g_pump_on_ms", "index": -1, "field": "", "value": "kPumpMaxOnMs"}])
        self.assertIn("ut_state_set_g_pump_on_ms(300000);", " ".join(scenario.setup), notes)   # A1-PL2a: a constexpr
        scenario, notes = self.check("cpp_medium_pilot", "vehicle::chassis::SteerLimiter::limit",
                                     args=[{"name": "request_deci_deg", "value": "2" + "0" * 60}])
        self.assertIsNone(scenario)                                         # a method's arguments are checked too
        self.assertTrue(any("outside the range" in n for n in notes), notes)
        scenario, notes = self.check("cpp_medium_pilot", "vehicle::chassis::SteerLimiter::limit",
                                     args=[{"name": "request_deci_deg", "value": "5"}],
                                     members=[{"member": "max_rate_", "value": "1" + "0" * 30}])
        self.assertIsNone(scenario)                                         # a member's value against its type
        self.assertTrue(any("members max_rate_" in n and "range" in n for n in notes), notes)

    def test_constructors_and_injected_interfaces_are_built(self):
        """From the pilot-small run of 2026-10-28: a requirement of a constructor (STR-1: a rate of 0 is refused with
        an exception) could not be built, so the brain was asked for it round after round; an injected interface
        parameter refused every value, the fake's name too."""
        layout = self.layouts["cpp_medium_pilot"]
        ev = uc.EvidenceRef(evidence_id="EV-steer-1", evidence_class="E1", source="docs/requirements.md", line=9,
                            text="STR-1: the steering request is limited to the rate given at construction, per second; a "
                                 "rate of 0 or below is refused with an exception")
        agent = a1.AnalyzerAgent()
        fn = uc.function_of("vehicle::chassis::SteerLimiter::SteerLimiter", layout)
        planned = a1.PlannedScenario.model_validate(dict(
            gap_ids=["LC:1"], name="test_steer_limiter_refuses_a_zero_rate", purpose="p",
            args=[{"name": "max_rate_per_s", "value": "0"}], stubs=[], variables=[], expected_return="",
            expected_exception="std::invalid_argument", expected_after=[], evidence_ids=["EV-steer-1"],
            quotes=[{"evidence_id": "EV-steer-1", "text": "a rate of 0 or below is refused with an exception"}],
            classification="confirmed_gap", conflict="", information_sought=""))
        stubs = agent._stubs(layout, None)
        scenario, notes = agent._check_planned(planned, fn, {"LC:1"}, set(), {"EV-steer-1": ev}, stubs,
                                               agent._constants_for_values(layout), agent._known_names(layout, stubs),
                                               layout)
        self.assertEqual(scenario.expected_exception, "std::invalid_argument", notes)
        plan = uc.UTPlan(plan_id="p", layout=layout, stage="coverage", scenarios=[scenario], stubs=stubs)
        self.assertIn("UT_EXPECT_THROW(([&] { vehicle::chassis::SteerLimiter ut_obj(0); }()), std::invalid_argument);",
                      a2.assemble_scenario(scenario, plan, "test_steer_limiter_refuses_a_zero_rate"))
        scenario, notes = self.check("cpp_medium_pilot", "vehicle::chassis::BrakeBlender::BrakeBlender",
                                     args=[{"name": "diag", "value": "ut_dep0", "elements": ["x"]}])
        self.assertIsNotNone(scenario, notes)
        self.assertIn("ut_fake_IDiagSink ut_diag;", scenario.setup)                 # the type decides: a fake
        plan = uc.UTPlan(plan_id="p", layout=layout, stage="coverage", scenarios=[scenario], stubs=stubs)
        self.assertIn("vehicle::chassis::BrakeBlender ut_obj(ut_diag);", a2.assemble_scenario(scenario, plan, "t"))


class ReadOnceTests(unittest.TestCase):
    """A1-31b: the working copy is read once per run; a changed file is read again."""
    def test_sources_are_read_again_only_when_changed(self):
        with tmpdir() as d:
            repo = copy_pilot("c_pilot", Path(d))
            (repo / "src/caller.c").write_text("void tick(void) { (void)clamp_percent(3); }\n")
            agent = a1.AnalyzerAgent()
            layout = agent.discover(uc.DiscoverRequest(repo_root=str(repo), sut_file="src/thermostat.c"))
            first = [e for e in agent.collect_evidence(layout, "clamp_percent").evidence if e.evidence_class == "E6"]
            self.assertEqual([(e.source, e.text) for e in first], [("src/caller.c", "void tick(void) { (void)clamp_percent(3); }")])
            with mock.patch.object(Path, "read_text", side_effect=AssertionError("read again")) as reads:
                agent._source_texts(repo)                                                 # nothing changed
            self.assertFalse(reads.called)
            time.sleep(0.01)
            (repo / "src/caller.c").write_text("void tick(void)\n{\n    (void)clamp_percent(4);\n}\n")
            again = [e for e in agent.collect_evidence(layout, "clamp_percent").evidence if e.evidence_class == "E6"]
            self.assertEqual([(e.line, e.text) for e in again], [(3, "(void)clamp_percent(4);")])


# ============================================================================ the parallel runner [TP-20]
def _test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _test_ids(item)
        else:
            yield item


def parallel_main(workers: int) -> int:
    """test-full: every test, spread over parallel processes. A test that runs a whole pipeline gets a process
    of its own and starts first; the other tests run class by class. Prints one summary at the end, in the
    words unittest uses, so the entry files can show it."""
    import concurrent.futures
    import time as _time
    module = sys.modules[__name__]
    tests = list(_test_ids(unittest.defaultTestLoader.loadTestsFromModule(module)))
    jobs: list[list[str]] = []
    classes: dict[str, list[str]] = {}
    for test in tests:
        cls, method = type(test), getattr(type(test), test._testMethodName, None)
        name = f"test_unittest_generator.{cls.__name__}.{test._testMethodName}"
        if getattr(cls, "ut_full_run", False) or getattr(method, "ut_full_run", False):
            jobs.append([name])
        else:
            classes.setdefault(cls.__name__, []).append(name)
    jobs += list(classes.values())
    env = {**os.environ, "UT_TESTS": "full", "PYTHONDONTWRITEBYTECODE": "1"}
    started = _time.time()

    def run_job(names: list[str]) -> tuple[list[str], str, int]:
        proc = subprocess.run([sys.executable, "-u", "-m", "unittest", "-v", *names], cwd=str(HERE), env=env,
                              capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL)
        return names, proc.stdout + proc.stderr, proc.returncode

    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failed_jobs = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for names, output, code in pool.map(run_job, jobs):
            print(output, flush=True)
            m = re.search(r"^Ran (\d+) tests?", output, re.M)
            totals["tests"] += int(m.group(1)) if m else 0
            for key in ("failures", "errors", "skipped"):
                totals[key] += sum(int(n) for n in re.findall(rf"{key}=(\d+)", output.splitlines()[-1] if output.strip() else ""))
            if code != 0:
                failed_jobs += 1
                if not m:                                   # the process itself failed
                    totals["errors"] += len(names)
                    print("ERROR: " + ", ".join(names) + " - the test process ended without a result", flush=True)
    print("=" * 70)
    print(f"Summary: Ran {totals['tests']} tests in {_time.time() - started:.1f}s "
          f"(test-full, {workers} parallel processes)")
    extra = f"skipped={totals['skipped']}" if totals["skipped"] else ""
    if failed_jobs or totals["failures"] or totals["errors"]:
        parts = [f"failures={totals['failures']}", f"errors={totals['errors']}"] + ([extra] if extra else [])
        print(f"Summary: FAILED ({', '.join(parts)})")
        return 1
    print("Summary: OK" + (f" ({extra})" if extra else ""))
    return 0


if __name__ == "__main__":
    if "--parallel" in sys.argv:
        sys.exit(parallel_main(int(os.environ.get("UT_TEST_WORKERS") or max(1, (os.cpu_count() or 2) - 1))))
    unittest.main()
