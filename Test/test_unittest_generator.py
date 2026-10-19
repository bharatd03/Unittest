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

# TP-21: no test reaches a real brain, whatever this computer offers (an API key, Ollama with Qwen). The key
# is removed from this process and the agents it starts; Qwen is pointed at an address where nothing listens.
# A test that needs a brain patches in the scripted stand-in (brain(...)).
NO_BRAIN_URL = "http://127.0.0.1:9"
os.environ.pop("ANTHROPIC_API_KEY", None)
_REAL_BUILD_ROUTER = ba.build_router


def _hermetic_router(settings, *, injected=None):
    qwen = settings.qwen.model_copy(update={"base_url": NO_BRAIN_URL})
    return _REAL_BUILD_ROUTER(settings.model_copy(update={"qwen": qwen}), injected=injected)


ba.build_router = _hermetic_router

# TP-20: "test" (UT_TESTS=quick) skips the tests that run a whole pipeline; "test-full" runs every test.
QUICK = os.environ.get("UT_TESTS", "full") == "quick"


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
                 name="qwen", suggestions=None, repairs=None, values=None, headers=None, host=None):
        self.tests = CANNED_TESTS if tests is None else tests
        self.patch = patch or {"original_fragment": "return 99;", "corrected_fragment": "return 100;",
                               "rationale": "EV-clamp_percent-1 (E2): the value is limited to 0..100."}
        self.root_cause, self.fail, self.dependency = root_cause, fail, dependency
        self.plans, self.links, self.declarations = plans or {}, links or [], declarations or {}
        self.flags, self.review, self.candidates = flags or [], review, candidates
        self.brain_name, self.suggestions = name, suggestions
        # rule failures handed to the brain [ARCH-21]: a reply, a list of replies (one per call) or a function
        self.rule_replies = {"repair": repairs, "value": values, "header": headers, "host": host}
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
    for k, row in enumerate(re.findall(r"^- (\w+): (.*?) \((\w+)(?:; (.*))?\)$", block.group(1) if block else "", re.M)):
        name, ptype, kind, extra = row
        base = re.sub(r"\b(const|volatile)\b", "", ptype).replace("&", "").strip()
        value = "0"
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

    def test_one_faulty_test_does_not_cost_the_others(self):
        """[A2-21t, A2-21q] the answer is checked test by test: a long or duplicate name is repaired, a test
        with an invented call is dropped and noted, the others are used - without asking the brain again."""
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
        with brain(b):
            bundle = run(agent.generate_bundle(uc.GenerationRequest(plan=self.plan)))
        suite = uc.suite_of(bundle)
        self.assertNotIn("invented_api", suite)
        names = re.findall(r"UT_TEST\((\w+)\)", suite)
        self.assertTrue(all(len(n) <= a2.NAME_LIMIT for n in names), names)
        self.assertIn("test_select_mode_returns_eco_when_nobody_is_home_and_the_window", names)
        self.assertIn("test_select_mode_nobody_home", names)
        self.assertIn("test_select_mode_nobody_home_2", names)
        self.assertIn("test_select_mode_away", names)
        self.assertTrue(any("dropped test_select_mode_invents" in n and "A2-21t" in n for n in bundle.notes),
                        bundle.notes)
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
            with brain(b):
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
        with brain(b):
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
        self.assertIn("within 2 s", status["test_spins"].message)
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
        self.assertTrue(any(p.endswith("40021000") for p in r.register_pages), r.register_pages)

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
        ref = uc.InterfaceInfo(name="IBus", methods=[uc.InterfaceMethod(name="buf", return_type="int &")])
        self.assertFalse(a2.fake_supported(ref))                                 # no fake for reference results

    def test_class_templates_of_a_header_sut_are_instantiated_as_the_repository_uses_them(self):   # A1-DA19
        layout = a1.AnalyzerAgent().discover(uc.DiscoverRequest(repo_root=str(self.CPP), sut_file="include/ring_buffer.hpp"))
        self.assertTrue(layout.header_sut)
        self.assertEqual(layout.language, "cpp")                                # by the content, not ".hpp" alone
        plan = mock.Mock(layout=layout)
        self.assertEqual(a2.class_templates(plan), {"RingBuffer": ("RingBuffer", "std::int32_t, 4")})
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
        self.assertIn("try { ut_obj.push(1); } catch (...) { ut_thrown = true; } UT_EXPECT_TRUE(ut_thrown);", out)
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

    def fuzz_control(self, seconds=3):
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
        header = (repo / "tests/stubs/ut_stubs.h").read_text()
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
                    self.assertIn("class ut_fake_IEncoder : public IEncoder", (repo / "tests/stubs/ut_stubs.h").read_text())
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
                stubs = (repo / "tests/stubs/ut_stubs.h").read_text() if (repo / "tests/stubs/ut_stubs.h").exists() else ""
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
        with brain(UTBrain(tests=tests)):
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
        delivered = repo / "tests" / "ut_sut_access.c"
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

    def test_first_plan_by_the_brain_and_without_a_brain_the_planning_stops(self):
        """A1-PL1: before anything is measured the rules estimate the obligations; from the first coverage plan
        on the brain plans. A1-PL8, ARCH-24: no brain answers -> NO_BRAIN_AVAILABLE, never a rule plan instead."""
        with brain(UTBrain(plans={"thermostat_command": [self.good()]})) as b:
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout)))          # nothing measured yet
        self.assertFalse(b.generate_calls)
        self.assertTrue(all(s.made_by == "rule" for s in plan.scenarios))
        with brain(UTBrain(plans={"thermostat_command": [self.good()]})) as b:
            plan = run(self.agent.plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, self.gaps),
                                                      iteration=1)))                   # the first coverage plan
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

    def test_when_no_function_gets_a_scenario_the_planning_fails(self):
        """A1-PL8, ORC-34: no function of the plan got a scenario that passed the check - BRAIN_PLAN_FAILED."""
        gaps = [uc.GapItem(gap_id=f"LC:{fn}:{line}", function=fn, metric="LC", line=line)
                for fn, line in (("thermostat_command", 11), ("clamp_percent", 19), ("select_mode", 30))]
        with brain(UTBrain(plans={"thermostat_command": [], "clamp_percent": [], "select_mode": []})):
            with self.assertRaises(ba.AgentFault) as caught:
                run(a1.AnalyzerAgent().plan(uc.PlanRequest(layout=self.layout, last=measured(self.layout, gaps),
                                                           iteration=2)))
        self.assertEqual(caught.exception.code, "BRAIN_PLAN_FAILED")
        self.assertIn("clamp_percent", caught.exception.message)

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
            attempts={"BC:thermostat_command:7:9:T": 2}, existing_suite=suite,
            test_items={"test_thermostat_command_auto_1": ["LC:6", "LC:13", "BC:7:9:F"]})
        contexts = []
        with brain(UTBrain(plans={"thermostat_command": lambda ctx: contexts.append(ctx) or []})):
            plan = run(self.agent.plan(req))
        self.assertFalse([s for s in plan.scenarios if s.function == "thermostat_command" and not s.inputs])
        self.assertTrue(any(p.startswith("thermostat_command: no scenario this time") for p in plan.planning))  # A1-PL8
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
