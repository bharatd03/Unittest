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
import unittest
import uuid
from pathlib import Path
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
                 name="qwen", suggestions=None):
        self.tests = CANNED_TESTS if tests is None else tests
        self.patch = patch or {"original_fragment": "return 99;", "corrected_fragment": "return 100;",
                               "rationale": "EV-clamp_percent-1 (E2): the value is limited to 0..100."}
        self.root_cause, self.fail, self.dependency = root_cause, fail, dependency
        self.plans, self.links, self.declarations = plans or {}, links or [], declarations or {}
        self.flags, self.review, self.candidates = flags or [], review, candidates
        self.brain_name, self.suggestions = name, suggestions
        self.generate_calls, self.decide_calls, self.questions = [], 0, []

    async def start(self):
        return None

    async def close(self):
        return None

    async def predict(self, state, questions):
        self.decide_calls += 1
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
        if "could not reach the ITEMS NOT REACHED" in task:                         # COV-12
            found = self.suggestions(context) if callable(self.suggestions) else (self.suggestions or [])
            reply = {"suggestions": found}
        elif "smallest change" in task:
            reply = {"candidates": self.candidates or [self.patch]}
        elif task.startswith("Plan unit-test scenarios"):
            fn = re.search(r"for the function (\S+)", task).group(1).split("::")[-1]
            plans = self.plans.get(fn, [])
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


class DownBrain(UTBrain):
    async def start(self):
        raise ba.AgentFault("MODEL_UNAVAILABLE", "Ollama is not running", True)


@contextlib.contextmanager
def brain(b: UTBrain):
    with mock.patch.object(ba, "build_router", lambda settings, injected=None: ba.BrainRouter([b], 0.0)):
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

    def test_every_test_starts_from_the_initial_sut_state(self):
        layout = uc.LayoutProfile(repo_root=".", sut_file="src/a.c", language="c", component_root=".",
                                  writable_test_dir="tests", writable_stub_dir="tests/stubs", suite_file="tests/test_a.c",
                                  sut_sha256="0" * 64, sut_state=[uc.StateVar(name="g_faults", type="int")])
        plan = uc.UTPlan(plan_id="p1", layout=layout, stage="coverage")
        path, access = a2.render_sut_access(plan)
        self.assertEqual(path, "tests/ut_sut_access.c")
        self.assertIn("void ut_sut_reset(void)", access)
        self.assertIn("memcpy((void *)&g_faults, ut_state_0, sizeof ut_state_0);", access)
        _, stubs, header = a2.render_stubs(plan)
        self.assertIn("void ut_sut_reset(void);", header)
        self.assertIn("void ut_before_each(void)\n{\n    ut_sut_reset();", stubs)
        cpp = [uc.StateVar(name="n", type="int"), uc.StateVar(name="s", type="std::string")]
        self.assertIn("&n", uc.sut_reset_source(cpp, True))
        self.assertNotIn("&s", uc.sut_reset_source(cpp, True))                     # only plain C++ state


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
        self.assertEqual(path, "tests/stubs/ut_stubs.c")
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
        with brain(b):
            bundle = run(agent.generate_bundle(uc.GenerationRequest(plan=self.plan)))
        self.assertNotIn("invented_api", uc.suite_of(bundle))                    # A2-20, A2-21
        self.assertTrue(any("no LLM tests" in n for n in bundle.notes))

    def test_llm_tests_keep_their_evidence_class(self):
        agent = a2.GeneratorAgent(config=a2.GeneratorConfig(mode="llm"))
        with brain(UTBrain()):
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
        original = (self.repo / "src/thermostat.c").read_text()
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

    @unittest.skipUnless(have_tool("cmake") and have_tool("ninja"), "cmake and ninja are not in the tool manifest")
    def test_cmake_build_is_captured(self):
        repo = copy_pilot("c_deps_pilot", self.d)
        build, layout = self.analyze(repo, "src/ctrl.c")
        self.assertEqual(build.source, "cmake")                                   # A3-BD2
        self.assertFalse(build.estimated)
        self.assertEqual(set(build.sut_flags), {"-DBOARD_REV=2", "-DMINI_LIMIT=40", "-I{repo}/include"})
        self.assertEqual({e.file for e in build.entries}, {"src/ctrl.c", "src/brake_ctrl.c", "src/steer_assist.c",
                                                           "lib/util.c", "lib/hw.c"})              # A3-BD8
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
        suite = ('#include "ut_harness.h"\n#include "ut_stubs.h"\nint ut_static_scale(int v);\n'
                 'UT_TEST(test_scale) { UT_OBSERVE_INT("s", ut_static_scale(3)); }\n'
                 'UT_SUITE_BEGIN\nUT_CASE(test_scale)\nUT_SUITE_END\n')
        bundle = uc.FileActionBundle(bundle_id="b", actions=[
            uc.FileAction(path=stub_path, action="create", role="stub", content=stub_src),
            uc.FileAction(path=stub_path.replace("ut_stubs.c", "ut_stubs.h"), action="create", role="helper",
                          content=stub_h),
            uc.FileAction(path=access[0], action="create", role="sut_access", content=access[1]),
            uc.FileAction(path=layout.suite_file, action="create", role="testsuite", content=suite)])
        result = self.a3.execute(uc.VerifyRequest(layout=layout, stage="verify", bundle=bundle))
        self.assertTrue(result.link_ok, [d.message for d in result.diagnostics])
        scale = next(f for f in result.coverage.functions if f.name == "scale")
        self.assertTrue(scale.executed)                                           # measured in the SUT's lines
        self.assertEqual(scale.metrics.LC.percent, 100.0)

    @unittest.skipUnless(_tools().fuzzer if not TOOLCHAIN_SKIP else False, "libFuzzer not available")
    def test_fuzzer_finds_inputs_for_gaps(self):
        """[A3-FZ1..A3-FZ4] coverage-guided search returns readable inputs that reach a gap."""
        repo = copy_pilot("c_deps_pilot", self.d)
        with brain(DownBrain()):
            _, layout = self.analyze(repo, "src/ctrl.c")
        plan = run(self.a1.plan(uc.PlanRequest(layout=layout)))
        stub_path, stub_src, stub_h = a2.render_stubs(plan)
        bundle = uc.FileActionBundle(bundle_id="b", actions=[
            uc.FileAction(path=stub_path, action="create", role="stub", content=stub_src),
            uc.FileAction(path=stub_path.replace("ut_stubs.c", "ut_stubs.h"), action="create", role="helper",
                          content=stub_h)])
        gap = uc.GapItem(gap_id="BC:control:13:9:T", function="control", metric="BC", line=13, column=9)
        result = self.a3.fuzz(uc.FuzzRequest(layout=layout, bundle=bundle, functions=["control"], gaps=[gap], seconds=3))
        self.assertIn("control", result.functions, result.unsupported)
        self.assertTrue(result.inputs, result.reason)
        self.assertIn("BC:control:13:9:T", result.inputs[0].covers)
        self.assertTrue(any(line.startswith("STATUS_REG = ") for line in result.inputs[0].setup))


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
    config["brain"] = {"engine": {"mode": "qwen"}, "qwen": {"startup_timeout": 2}}
    path = d / "ut_config.json"
    path.write_text(json.dumps(config, indent=1))
    return path


@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
@unittest.skipUnless(HAVE_FASTMCP and HAVE_LANGGRAPH, "fastmcp and langgraph are required")
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
        with brain(fix_brain):
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

    def test_brain_plans_the_documented_behaviour(self):
        """A1-PL1, A1-25y, A2-21m: after the deterministic tests, the brain plans a check of the stated
        behaviour; Agent1 checks it and Agent2 assembles it into an asserting test."""
        def clamp_plan(context):
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
        self.assertFalse(result.build_estimated)
        self.assertTrue(any(line.startswith("clamp: stub") for line in result.dependencies))   # A1-DA11
        self.assertIn("scale", {f.name for f in result.function_coverage})                  # A3-63
        delivered = repo / "tests" / "ut_sut_access.c"
        self.assertTrue(delivered.is_file())                                                # A2-21k

    def test_new_pilots_reach_every_target_without_a_brain(self):
        """The ASIL D pilots: stub sequences, struct fields, SUT state, classes with a clock, std::array - every
        metric at 100 % by measurement, with rules and coverage-guided search only (COV-1, COV-6)."""
        cases = [("c_deps_pilot", "src/brake_ctrl.c"), ("c_deps_pilot", "src/steer_assist.c"),
                 ("cpp_pilot", "src/steering_monitor.cpp"), ("cpp_pilot", "src/torque_map.cpp")]
        if not (have_tool("cmake") and have_tool("ninja")):
            cases = cases[2:]
        for pilot, sut in cases:
            with self.subTest(sut=sut):
                d = self.d / Path(sut).stem
                d.mkdir()
                repo = copy_pilot(pilot, d)
                with brain(DownBrain()):
                    result = run(up.Orchestrator(pipeline_config(d, fuzz_seconds=10)).run(
                        uc.PipelineRequest(repo_root=str(repo), sut_file=sut)))
                self.assertEqual(result.final_state, "verified_measured", up.report(result))
                self.assertTrue(all(getattr(result.metrics, m).percent == 100.0 for m in uc.METRICS))
                self.assertEqual(result.failed, 0)

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
            result = run(up.Orchestrator(config).run(uc.PipelineRequest(repo_root=str(repo), sut_file="src/thermostat.c")))
            print("\n" + up.report(result))
            self.assertGreater(len(result.iterations), 2, result.reason)
            self.assertGreater(result.passed, 0)
            self.assertTrue(up.Ledger.verify(Path(result.ledger)))
            logs = list((Path(result.output_dir) / "logs").glob("*.log"))
            self.assertEqual(len(logs), 5)                                          # five agent processes


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

    def test_brain_scenarios_are_checked_before_agent2(self):
        bad_range = self.good(name="test_thermostat_command_huge", args=[
            {"name": "setpoint", "value": "99999999999"}, {"name": "hysteresis", "value": "2"}])
        bad_stub = self.good(name="test_thermostat_command_display", stubs=[{"stub": "write_display", "returns": ["1"]}])
        bad_quote = self.good(name="test_thermostat_command_quote", quotes=[
            {"evidence_id": self.e2.evidence_id, "text": "returns 42 on Mondays"}])
        b = UTBrain(plans={"thermostat_command": [self.good(), bad_range, bad_stub, bad_quote]})
        last = measured(self.layout, self.gaps)
        with brain(b):
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=last, iteration=2)))
        mine = [s for s in plan.scenarios if s.function == "thermostat_command"]
        self.assertEqual(len(mine), 1, plan.planning)                                   # A1-PL4: three dropped
        s = mine[0]
        self.assertTrue(s.made_by.startswith("brain:qwen") and s.checked)              # A1-PL10
        self.assertFalse(s.characterization)                                            # A1-PL4 c): normative
        self.assertEqual((s.expected_return, s.strongest_class), ("-1", "E2"))
        self.assertEqual(s.stub_values[0].returns, ["SENSOR_ERROR"])
        notes = " ".join(plan.planning)
        self.assertIn("outside the range of int", notes)                               # A1-PL4 e)
        self.assertIn("write_display is not a stubbed function", notes)                 # A1-PL4 d)
        self.assertIn("word for word", notes)                                           # A1-PL4 b)
        self.assertEqual(len([t for t in b.generate_calls if "function thermostat_command " in t]), 2)   # A1-PL9

    def test_without_a_quote_a_scenario_only_characterizes(self):
        b = UTBrain(plans={"thermostat_command": [self.good(quotes=[])]})
        with brain(b):
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps),
                                                      iteration=2)))
        s = next(s for s in plan.scenarios if s.function == "thermostat_command")
        self.assertTrue(s.characterization)                                             # A1-25h, A1-PL4 c)
        self.assertEqual(s.expected_return, "")

    def test_first_plan_and_no_brain_use_the_rules(self):
        with brain(UTBrain(plans={"thermostat_command": [self.good()]})) as b:
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout)))          # nothing measured yet
        self.assertFalse(b.generate_calls)                                              # A1-PL1
        self.assertTrue(all(s.made_by == "rule" for s in plan.scenarios))
        with brain(DownBrain()):
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps),
                                                      iteration=2)))
        self.assertTrue(all(s.made_by == "rule" for s in plan.scenarios))              # A1-PL8
        self.assertTrue(any("rules - brain not available" in p for p in plan.planning))

    def test_hard_gap_shows_what_was_tried(self):
        suite = ('/* evidence: E8 */\nUT_TEST(test_thermostat_command_auto_1)\n{\n    ut_stub_read_sensor_return = 5;\n'
                 '    UT_EXPECT_EQ_INT(thermostat_command(1, 0), 0);\n}\n')
        req = uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps, tests=[
            uc.TestResult(name="test_thermostat_command_auto_1", status="passed")]), iteration=3,
            attempts={"BC:thermostat_command:7:9:T": 2}, existing_suite=suite,
            test_items={"test_thermostat_command_auto_1": ["LC:6", "LC:13", "BC:7:9:F"]})
        contexts = []
        with brain(UTBrain(plans={"thermostat_command": lambda ctx: contexts.append(ctx) or []})):
            run(self.agent.plan(req))
        self.assertIn("TRIED 2 time(s)", contexts[0])                                   # A1-PL12
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
        with brain(DownBrain()):
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
        self.assertIn("if (ut_stub_read_sensor_calls <= ut_stub_read_sensor_returns_len) "
                      "return ut_stub_read_sensor_returns[ut_stub_read_sensor_calls - 1];", source)

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
        self.assertIn(f"/* S-brain-thermostat_command-2-1: the second reading heats [{self.e2.evidence_id}] */",
                      suite)                                                                      # A2-21q, A2-21a
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
        with brain(UTBrain()) as b:
            bundle = run(a2.GeneratorAgent().generate_bundle(uc.GenerationRequest(plan=plan)))
        self.assertTrue(any("_auto_" in n for n in bundle.tests))                       # deterministic first
        self.assertIn("test_clamp_percent_upper_limit", bundle.tests)                   # A2-21n
        self.assertTrue(b.generate_calls)

    def test_failing_e2_test_is_rewritten_with_the_diagnostic(self):
        old = ('/* evidence: E2 */\nUT_TEST(test_clamp_percent_upper_limit)\n{\n    UT_EXPECT_EQ_INT(clamp_percent(150), 99);\n}\n'
               'UT_SUITE_BEGIN\n    UT_CASE(test_clamp_percent_upper_limit)\nUT_SUITE_END\n')
        repair = uc.RepairItem(item_id="test-test_clamp_percent_upper_limit", owner="agent2",
                               target_file=self.layout.suite_file, issue="test_generation_defect",
                               diagnostic="expected 99, actual 100", affected_test="test_clamp_percent_upper_limit")
        plan = self.plan.model_copy(update={"repairs": [repair], "drop_tests": ["test_clamp_percent_upper_limit"]})
        with brain(UTBrain()) as b:
            bundle = run(a2.GeneratorAgent().generate_bundle(uc.GenerationRequest(plan=plan, existing_suite=old)))
        self.assertIn("UT_EXPECT_EQ_INT(clamp_percent(150), 100);", uc.suite_of(bundle))  # A2-21p
        change = next(c for c in bundle.expectation_changes if c.test == "test_clamp_percent_upper_limit")
        self.assertIn("expected 99, actual 100", change.reason)                         # A2-21e
        self.assertTrue(any("expected 99, actual 100" in c for c in b.generate_calls) or bundle.expectation_changes)


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
        self.assertLess(context.index("OPEN GAPS"), context.index("GUIDANCE (knowledge pack test-design 1.0"))  # KP-3
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
        self.assertIn("OPEN GAPS", context)

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
        self.assertEqual(uc.DEFAULT_ITERATIONS, 5)                                          # COV-9

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
        self.assertNotIn("without requirements", result.suggestions[0])                     # cites GRD-1 [COV-12]
        self.assertFalse(any("Provide the requirement documents" in a for a in result.action_needed))
        patches = list((Path(result.output_dir) / "coverage_suggestions").glob("*.patch"))
        self.assertEqual(len(patches), 1)
        patch = patches[0].read_text()
        self.assertIn("-            return -1;", patch)
        self.assertIn("not applied, not committed, not counted as coverage", patch)
        self.assertTrue(any("cov-" in n and "not shown" in n and "[COV-13]" in n for n in result.open_items),
                        result.open_items)                                                  # the guess is not shown
        self.assertEqual((self.repo / "src/guard.c").read_text(), GUARD_C)                # COV-7: never applied
        for title in ("## Coverage targets: ASIL D (default)", "## Action needed", "## Items not reached: evidence",
                      "## Source-change suggestions"):
            self.assertIn(title, text)
        self.assertNotIn("xclusion", text)
        self.assertIn("| MCDC | 0 | 0 | NA | not applicable |", text)                       # guard.c has no such decision
        self.assertTrue(any("could not reach the ITEMS NOT REACHED" in c for c in b.generate_calls))

    def test_without_a_brain_the_evidence_is_still_handed_over(self):
        with brain(DownBrain()):
            result = run(up.Orchestrator(pipeline_config(self.d, coverage=cov(2))).run(
                uc.PipelineRequest(repo_root=str(self.repo), sut_file="src/guard.c")))
        self.assertEqual(result.final_state, "incomplete")
        self.assertTrue(result.not_reached and result.action_needed)
        self.assertEqual(result.suggestions, [])
        self.assertTrue(any("Change the source" in a for a in result.action_needed), result.action_needed)


@unittest.skipIf(TOOLCHAIN_SKIP, TOOLCHAIN_SKIP or "")
@unittest.skipUnless(HAVE_FASTMCP and HAVE_LANGGRAPH, "fastmcp and langgraph are required")
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


# ============================================================================ 9. requirements and skill
class RequirementsTests(unittest.TestCase):
    REQ = DOCS / "unittest_generator_requirments.txt"
    ID = re.compile(r"\b(?:GEN-\d+|ORC-\d+[a-z]?|ARCH-\d+|TP-\d+|A1-(?:DA|PL|RQ)?\d+[a-z]?|KP-\d+|A2-\d+[a-z]?|"
                    r"A3-(?:PR|OBS|CL|BD|FZ)?\d+[a-z]?|A4-\d+[a-z]?|KB-\d+[a-z]?|SC-\d+[a-z]?|COV-\d+|VIEW-\d+)\b")

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
        self.assertEqual(router.names, ["opus", "fable", "sonnet", "haiku", "qwen"])
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


if __name__ == "__main__":
    unittest.main()
