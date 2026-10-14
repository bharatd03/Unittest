"""Agent4 - agent_sourcecode_corrector: minimal production-source corrections.

Agent4 is separate from Agent2 [A4-12]. It changes production code only for a failure that
Agent3 classified as a source defect [A4-16], never to gain coverage or to make a wrong test
pass [A4-17, A4-18, A4-54, A4-55]. A behaviour fix additionally needs expected-behaviour
evidence of class E1 to E7; an LLM classification alone is not enough [A4-23b].

Every correction is a *candidate* applied only to a staging copy [A4-23c, A4-25]. Agent3
recompiles and re-runs everything; only then may ``publish`` deliver it, as a patch file and
as a commit on a separate branch of the repository, never on the working branch [A4-23c,
A4-26, A4-33, A4-58]. Agent4 never declares its own correction successful. Publication also
needs two independent pieces of expected-behaviour evidence [A4-23f] and no new static-analyzer
finding in the patched source [A4-32a]. Evidence the brain linked to a function never counts [A4-23i].

Several different candidates are asked for at once [A4-23g]; the orchestrator has Agent3 reverify each,
``check`` applies the publication rules, and the smallest passing one is reviewed by a second brain -
another one than its author when available - whose review can only block [A4-23h].

After a run stopped below its coverage targets, ``suggest`` proposes source changes that would let
tests reach the items not reached, from the requirements (COV-12). These are suggestions for the
developer only: the orchestrator checks each on a trial copy (COV-13); they are never published,
committed or counted as coverage (COV-7).

Tools (MCP, each a contract): correct, candidates, check, review, publish, suggest.
Run as an MCP server:  python agent_sourcecode_corrector.py serve [--config ut_config.json]
"""
from __future__ import annotations

import difflib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

from typing import Literal

from pydantic import BaseModel, Field

import ut_contracts as uc
from ut_contracts import ba

LOG = ba.logging.getLogger("ut.agent4")
SOURCE_DEFECTS = ("source_behavior_defect", "source_compilation_defect")
STRONG_EVIDENCE = {"E1", "E2", "E3", "E4", "E5", "E6", "E7"}


class CorrectorConfig(BaseModel):
    max_fragment_lines: int = 30
    min_independent_evidence: int = 2        # A4-23f
    context_lines: int = 12
    # Expected answer per candidate fix, in tokens: sets the answer limit and window [ARCH-11]
    tokens_per_candidate: int = 450
    candidates: int = 3                      # A4-23g, ORC-7


class PatchProposals(BaseModel):
    candidates: list["PatchProposal"]


REVIEW_QUESTION = {
    "review": ba.Question(type="choice", instructions=(
        "A candidate fix of production source is shown with the failure it should fix, the expected-behaviour "
        "evidence and the results of reverifying it. Decide whether it may be published. Use only what is shown."),
        criteria={"agree": "The change is supported by the evidence, removes exactly the failure and changes no other "
                           "behaviour.",
                  "block": "The change is not supported by the evidence, changes behaviour beyond the failure, or "
                           "hides the failure instead of fixing it."})}


def change_size(original: str, corrected: str) -> tuple[int, int]:
    """(changed lines, changed characters) of one replacement [A4-23g]."""
    a, b = original.splitlines(), corrected.splitlines()
    lines = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes()
                if tag != "equal")
    chars = sum((i2 - i1) + (j2 - j1) for tag, i1, i2, j1, j2 in
                difflib.SequenceMatcher(None, original, corrected).get_opcodes() if tag != "equal")
    return lines, chars


class PatchProposal(BaseModel):
    """What the brain returns: one localized replacement [A4-19]."""
    original_fragment: str = Field(description="exact text copied from the FUNCTION source (without line numbers)")
    corrected_fragment: str = Field(description="the replacement text")
    rationale: str = Field(description="why this fixes the failure, citing the evidence id")


PatchProposals.model_rebuild()


class SuggestionQuote(BaseModel):
    evidence_id: str
    text: str = Field(description="exact words copied from that evidence item")


class SuggestionProposal(BaseModel):
    """What the brain returns per suggestion (COV-12). No cause lets an item be excluded (COV-5)."""
    item_ids: list[str] = Field(description="ids from ITEMS NOT REACHED that the change makes reachable")
    cause: Literal["condition_defect", "dead_code", "required_defensive_code", "not_controllable"] = Field(
        description="condition_defect: the condition is wrong; dead_code: the code can never run and is not required; "
                    "required_defensive_code: a requirement demands it, so make it controllable from a test; "
                    "not_controllable: the input it depends on cannot be set from a test")
    original_fragment: str = Field(description="exact text copied from the SOURCE (without line numbers)")
    corrected_fragment: str = Field(description="the replacement text")
    rationale: str = Field(description="step by step: why the items are not reached and why the change fixes it")
    requirement_ids: list[str] = Field(description="evidence ids of the requirements the change relies on")
    quotes: list[SuggestionQuote]
    expected_effect: str = Field(description="which items become reachable and how a test reaches them")


class SuggestionProposals(BaseModel):
    suggestions: list[SuggestionProposal]


SUGGEST_TASK = (
    "A unit-test pipeline could not reach the ITEMS NOT REACHED in {file} with tests: the facts below show what "
    "was tried. Work step by step: for each item, read its path conditions and missing condition vectors, read the "
    "REQUIREMENTS, and decide why no test reaches it: a wrong condition (condition_defect), code that can never "
    "run and that no requirement demands (dead_code), code a requirement demands that a test cannot trigger "
    "(required_defensive_code: make it controllable, for example through a parameter or a stubbed function), or "
    "an input a test cannot set (not_controllable: make it settable). Then propose the smallest change of the "
    "SOURCE that lets a test reach the items and keeps every behaviour the REQUIREMENTS state. Copy "
    "original_fragment exactly from the SOURCE (without line numbers); it must occur once. Cite the requirements "
    "you rely on with evidence ids and quotes copied word for word. Return JSON {{\"suggestions\": [...]}} with "
    "at most {limit} suggestions, the smallest first.")


def region_of(source: str, layout: uc.LayoutProfile, failure: uc.Failure, context: int) -> tuple[int, int, str]:
    """(first line, last line, function name) that a correction may touch."""
    fn = next((f for f in layout.functions if failure.affected_function in (f.name, f.qualified_name)), None)
    if fn is None and failure.line:
        fn = next((f for f in layout.functions if f.line_start <= failure.line <= f.line_end), None)
    if fn is not None:
        return fn.line_start, fn.line_end, fn.name
    total = len(source.splitlines())
    line = failure.line or 1
    return max(1, line - context), min(total, line + context), failure.affected_function or "<file>"


class CorrectorAgent(ba.AgentCore):
    name = "agent_sourcecode_corrector"
    application = "UT"                                            # VIEW-2
    view_role, view_description, view_support = uc.VIEW_AGENTS["agent_sourcecode_corrector"]

    def view_summary(self, model):
        return uc.view_summary(self.view_label, model) or super().view_summary(model)

    def __init__(self, settings: ba.Settings | None = None, *, config: CorrectorConfig | None = None,
                 router: ba.BrainRouter | None = None):
        self.cfg = config or CorrectorConfig()
        super().__init__(settings, router=router)

    # ---------------------------------------------------------------- tools
    def tools(self) -> list[ba.ContractTool]:
        def correct_pre(req: uc.CorrectionRequest, request):
            f = req.failure
            uc.check_signature(req.layout, request)
            if req.layout.blocked:
                raise ba.ContractRejected("layout is blocked")
            if f.classification not in SOURCE_DEFECTS or f.route != "agent4":
                raise ba.ContractRejected("only failures classified as a source defect are corrected [A4-16, A4-54]")
            if f.classification == "source_behavior_defect":
                if not f.affected_test:
                    raise ba.ContractRejected("a behaviour fix needs the failing test [A4-15]")
                if not any(e.evidence_class in STRONG_EVIDENCE and not e.linked_by for e in req.evidence):
                    raise ba.ContractRejected("a behaviour fix needs expected-behaviour evidence E1-E7; "
                                              "an LLM classification alone is not enough [A4-23b]")
                problem = self.evidence_problem(req.evidence)
                if problem:
                    raise ba.ContractRejected(problem)
            sut = Path(req.layout.repo_root) / req.layout.sut_file
            if not sut.is_file() or uc.file_sha256(sut) != req.layout.sut_sha256:
                raise ba.ContractRejected("the SUT changed since the layout was made [ORC-10]")

        async def correct(req: uc.CorrectionRequest, request, scratch):
            return await self.correct(req)

        def correct_post(req: uc.CorrectionRequest, patch: uc.SourcePatch):
            if patch.status != "candidate_source_fix":
                raise ba.AgentFault("CONTRACT_VIOLATION", "Agent4 never declares its own fix verified [A4-58]")

        async def candidates(req: uc.CorrectionRequest, request, scratch):
            return await self.candidates(req)

        def candidates_post(req: uc.CorrectionRequest, out: uc.CandidateSet):
            if any(p.status != "candidate_source_fix" for p in out.patches):
                raise ba.AgentFault("CONTRACT_VIOLATION", "Agent4 never declares its own fix verified [A4-58]")

        async def check(req: uc.PublishRequest, request, scratch):
            reason = self.verification_problem(req)
            return uc.PublishCheck(ok=not reason, reason=reason or "every publication check passed")

        def review_pre(req: uc.ReviewRequest, request):
            uc.check_signature(req.layout, request)
            if req.patch.status != "candidate_source_fix":
                raise ba.ContractRejected("only a candidate is reviewed")

        async def review(req: uc.ReviewRequest, request, scratch):
            return await self.review(req)

        def publish_pre(req: uc.PublishRequest, request):
            reason = self.verification_problem(req)
            if reason:
                raise ba.ContractRejected(reason)

        async def publish(req: uc.PublishRequest, request, scratch):
            return self.publish(req)

        def suggest_pre(req: uc.SuggestRequest, request):
            uc.check_signature(req.layout, request)
            if not req.items:
                raise ba.ContractRejected("no item to suggest a change for [COV-12]")

        async def suggest(req: uc.SuggestRequest, request, scratch):
            return await self.suggest(req)

        return [ba.ContractTool("suggest", uc.SuggestRequest, uc.SuggestionSet, suggest, suggest_pre, read_only=True,
                                description="Source-change suggestions for coverage items no test reached, from the "
                                            "requirements; for the developer only, never applied [COV-12]."),
                ba.ContractTool("correct", uc.CorrectionRequest, uc.SourcePatch, correct, correct_pre, correct_post,
                                description="Candidate minimal source fix for a classified source defect [A4-15..A4-25]."),
                ba.ContractTool("candidates", uc.CorrectionRequest, uc.CandidateSet, candidates, correct_pre,
                                candidates_post, description="Up to the configured number of different candidate "
                                                             "fixes [A4-23g]."),
                ba.ContractTool("check", uc.PublishRequest, uc.PublishCheck, check, read_only=True,
                                description="The publication rules applied to one reverified candidate [A4-26..A4-33]."),
                ba.ContractTool("review", uc.ReviewRequest, uc.ReviewResult, review, review_pre, read_only=True,
                                description="Second opinion by another brain; it can only block [A4-23h]."),
                ba.ContractTool("publish", uc.PublishRequest, uc.PublishResult, publish, publish_pre,
                                description="Deliver an Agent3-verified fix as a patch file and a separate branch "
                                            "[A4-23c, A4-33].")]

    # ---------------------------------------------------------------- correction
    async def correct(self, req: uc.CorrectionRequest) -> uc.SourcePatch:
        result = await self.candidates(req, count=1)
        return result.patches[0]

    async def candidates(self, req: uc.CorrectionRequest, count: int | None = None) -> uc.CandidateSet:
        """A4-23g: different candidates, each checked as a single one is (A4-19..A4-21)."""
        layout, failure = req.layout, req.failure
        count = max(1, count or self.cfg.candidates)
        root = Path(layout.repo_root)
        raw = uc.read_source(root / layout.sut_file)
        crlf = "\r\n" in raw
        source = raw.replace("\r\n", "\n")            # work in LF, keep the file's line endings
        first, last, fn_name = region_of(source, layout, failure, self.cfg.context_lines)
        lines = source.splitlines()
        region = "\n".join(lines[first - 1: last])
        numbered = "\n".join(f"{n:4d}| {lines[n - 1]}" for n in range(first, last + 1))
        signature = lines[first - 1] if any(f.name == fn_name for f in layout.functions) else ""
        # Agentic RAG: index the SUT and its headers, retrieve what matches the failure.
        self.knowledge.clear()
        self.knowledge.add_text(layout.sut_file, source, kind="source")
        for header in layout.headers:
            if (root / header).is_file():
                self.knowledge.add_text(header, (root / header).read_text(encoding="utf-8", errors="replace"),
                                        kind="header")
        hits = self.knowledge.search(" ".join([fn_name, failure.diagnostic, failure.symbol]), k=4)
        # ARCH-17: the failure, the test and the function are the facts; related source gives way first.
        context = [
            uc.part("facts", "\n".join([
                f"LANGUAGE: {'C++' if layout.language == 'cpp' else 'C'}",
                f"FAILURE ({failure.classification}): {failure.diagnostic}",
                f"FAILING TEST {failure.affected_test or '-'}:\n{req.test_source or '-'}",
                f"FUNCTION {fn_name} ({layout.sut_file}, lines {first}-{last}):\n{numbered}"])),
            uc.part("evidence", "EXPECTED-BEHAVIOUR EVIDENCE:\n" + ("\n".join(
                f"- {e.evidence_id} {e.evidence_class} {e.source}:{e.line}: {e.text}" for e in req.evidence) or "- none"),
                uc.P_EVIDENCE),
            uc.part("related", "RELATED SOURCE AND HEADERS:\n" + ba.AgentKnowledge.render(hits, 6000), uc.P_TRIED),
        ]
        task = ("Propose the smallest change to the FUNCTION that removes the FAILURE so that the behaviour "
                "matches the EXPECTED-BEHAVIOUR EVIDENCE. Copy original_fragment exactly from the FUNCTION source "
                f"(without line numbers, at most {self.cfg.max_fragment_lines} lines); keep the function signature "
                "unchanged; do not touch any other function; do not change the test. "
                f"Return JSON {{\"candidates\": [...]}} with up to {count} candidate(s) that differ from one another, "
                "the smallest first.")
        check = self._fragment_check(region, signature)
        need = ba.OutputNeed(items=count, tokens_per_item=self.cfg.tokens_per_candidate, overhead=60)
        proposals = await self._generate_candidates(task, context, check, need, layout.repo_root)
        start = source.index(region)
        patches, seen, notes = [], set(), []
        author = self.last_brain.get("brain", "")
        for proposal in proposals[:count]:
            key = (proposal.original_fragment, proposal.corrected_fragment)
            if key in seen:
                continue
            seen.add(key)
            patched_region = region.replace(proposal.original_fragment, proposal.corrected_fragment, 1)
            patched = source[:start] + patched_region + source[start + len(region):]
            if crlf:
                patched = patched.replace("\n", "\r\n")
            lines, chars = change_size(proposal.original_fragment, proposal.corrected_fragment)
            patches.append(uc.SourcePatch(patch_id=f"fix-{uuid.uuid4().hex[:8]}", file=layout.sut_file, function=fn_name,
                                          original_fragment=proposal.original_fragment,
                                          corrected_fragment=proposal.corrected_fragment, rationale=proposal.rationale,
                                          failure_id=failure.failure_id, patched_content=patched, author=author,
                                          changed_lines=lines, changed_chars=chars))
        return uc.CandidateSet(patches=patches, notes=notes)

    def _fragment_check(self, region: str, signature: str):
        limit = self.cfg.max_fragment_lines

        def check(p: PatchProposal) -> None:
            p.original_fragment = p.original_fragment.replace("\r\n", "\n")
            p.corrected_fragment = p.corrected_fragment.replace("\r\n", "\n")
            if not p.original_fragment.strip():
                raise ba.AgentFault("MODEL_OUTPUT_INVALID", "original_fragment is empty")
            count = region.count(p.original_fragment)
            if count != 1:
                raise ba.AgentFault("MODEL_OUTPUT_INVALID", "original_fragment must occur exactly once in the "
                                    f"FUNCTION (found {count}); copy it exactly, without line numbers")
            if p.original_fragment == p.corrected_fragment:
                raise ba.AgentFault("MODEL_OUTPUT_INVALID", "corrected_fragment does not change anything")
            if max(p.original_fragment.count("\n"), p.corrected_fragment.count("\n")) + 1 > limit:
                raise ba.AgentFault("MODEL_OUTPUT_INVALID", f"the change is larger than {limit} lines [A4-19]")
            if signature and signature.strip() in p.original_fragment and signature.strip() not in p.corrected_fragment:
                raise ba.AgentFault("MODEL_OUTPUT_INVALID", "the function signature must stay unchanged [A4-20]")
        return check

    async def _generate_candidates(self, task: str, context: list[ba.ContextPart], check, need: ba.OutputNeed,
                                   project: str) -> list[PatchProposal]:
        """generate() with the fragment check inside the brain's validate step: candidates that fail it
        are left out; when none passes, the brain is asked once more with the reason [ARCH-5]."""
        def keep_good(parsed: PatchProposals) -> None:
            good, reasons = [], []
            for proposal in parsed.candidates:
                try:
                    check(proposal)
                    good.append(proposal)
                except ba.AgentFault as exc:
                    reasons.append(exc.message)
            if not good:
                raise ba.AgentFault("MODEL_OUTPUT_INVALID", "no candidate passed the checks: " +
                                    ("; ".join(reasons[:3]) or "no candidates"))
            parsed.candidates[:] = good
        answer = await self.generate(task, context, PatchProposals, need=need, step="A4.candidates", project=project,
                                     check=keep_good)
        return list(answer.candidates)

    async def review(self, req: uc.ReviewRequest) -> uc.ReviewResult:
        return await review_patch(self, req)

    # ---------------------------------------------------------------- suggestions [COV-12]
    async def suggest(self, req: uc.SuggestRequest) -> uc.SuggestionSet:
        """Facts first (deterministic), then the brain reasons step by step over them; every suggestion is
        checked here (fragment, quotes) and then on a trial copy by the orchestrator (COV-13)."""
        layout = req.layout
        raw = uc.read_source(Path(layout.repo_root) / layout.sut_file)
        crlf = "\r\n" in raw
        source = raw.replace("\r\n", "\n")
        lines = source.splitlines()
        names = sorted({i.function for i in req.items})
        functions = [f for f in layout.functions if f.name in names or f.qualified_name in names]
        listing = "\n\n".join(f"FUNCTION {f.qualified_name} (lines {f.line_start}-{f.line_end}):\n" + "\n".join(
            f"{n:4d}| {lines[n - 1]}" for n in range(f.line_start, min(f.line_end, len(lines)) + 1)) for f in functions)
        item_rows = []
        for i in req.items:
            row = f"- {i.item_id} [{i.metric}] {i.function} line {i.line}: {i.detail[:200]}"
            if i.missing:
                row += " | missing vectors: " + "; ".join(i.missing[:4])
            if i.path:
                row += " | path: " + " && ".join(i.path[:6])
            if i.proof:
                row += " | proof: " + i.proof[:300]
            if i.strategies:
                row += " | tried: " + "; ".join(i.strategies[-4:])
            item_rows.append(row)
        requirements = [e for e in req.evidence if e.evidence_class in ("E1", "E2", "E3") and not e.linked_by]
        context = [
            uc.part("source", f"SOURCE {layout.sut_file}:\n" + (listing or source[:20000])),
            uc.part("items", "ITEMS NOT REACHED:\n" + "\n".join(item_rows)),
            uc.part("requirements", "REQUIREMENTS:\n" + ("\n".join(
                f"- {e.evidence_id} {e.evidence_class} {e.source}:{e.line}: {e.text}" for e in requirements)
                or "- none given: say so in the rationale"), uc.P_EVIDENCE)]
        known = {e.evidence_id: e for e in req.evidence}
        limit = max(1, min(req.max_suggestions, 5))

        def keep_good(parsed: SuggestionProposals) -> None:
            good, reasons = [], []
            for s in parsed.suggestions:
                problem = ""
                original = s.original_fragment.replace("\r\n", "\n")
                if not original.strip() or source.count(original) != 1:
                    problem = "original_fragment must occur exactly once in the SOURCE"
                elif original == s.corrected_fragment.replace("\r\n", "\n"):
                    problem = "the change does not change anything"
                for q in s.quotes:
                    ev = known.get(q.evidence_id)
                    if ev is None or not q.text.strip() or " ".join(q.text.split()) not in " ".join(ev.text.split()):
                        problem = f"the quote of {q.evidence_id} does not occur word for word"
                if problem:
                    reasons.append(problem)
                else:
                    good.append(s)
            if not good:
                raise ba.AgentFault("MODEL_OUTPUT_INVALID", "no suggestion passed the checks: " + "; ".join(reasons[:3]))
            parsed.suggestions[:] = good[:limit]

        need = ba.OutputNeed(items=limit, tokens_per_item=self.cfg.tokens_per_candidate + 200, overhead=100)
        try:
            answer = await self.generate(SUGGEST_TASK.format(file=layout.sut_file, limit=limit), context,
                                         SuggestionProposals, need=need, step="A4.suggest", project=layout.repo_root,
                                         check=keep_good)
        except ba.AgentFault as exc:
            return uc.SuggestionSet(notes=[f"no suggestion: brain not available or no usable answer ({exc.code}) [COV-12]"])
        author = self.last_brain.get("brain", "")
        out = []
        for s in answer.suggestions:
            original = s.original_fragment.replace("\r\n", "\n")
            patched = source.replace(original, s.corrected_fragment.replace("\r\n", "\n"), 1)
            line = source[:source.index(original)].count("\n") + 1
            fn = next((f.name for f in layout.functions if f.line_start <= line <= f.line_end), "")
            cited = [r for r in s.requirement_ids if r in known and known[r].evidence_class == "E1"]
            out.append(uc.CoverageSuggestion(
                suggestion_id=f"cov-{uuid.uuid4().hex[:8]}", file=layout.sut_file, function=fn,
                item_ids=[i for i in s.item_ids if any(i == it.item_id for it in req.items)], cause=s.cause,
                original_fragment=original, corrected_fragment=s.corrected_fragment,
                patched_content=patched.replace("\n", "\r\n") if crlf else patched,
                rationale=(s.rationale + " Expected effect: " + s.expected_effect)[:2000],
                requirement_ids=s.requirement_ids, with_requirements=bool(cited), author=author))
        return uc.SuggestionSet(suggestions=out)

    def evidence_problem(self, evidence: list[uc.EvidenceRef]) -> str:
        """A4-23f: at least two independent expected-behaviour evidence items (different places,
        class E1-E7), at least one of them a stated requirement or contract (E1, E2 or E3)."""
        strong = [e for e in evidence if e.evidence_class in STRONG_EVIDENCE and not e.linked_by]      # A4-23i
        places = {(e.source, e.line) for e in strong}
        if len(places) < self.cfg.min_independent_evidence:
            return (f"a behaviour fix needs {self.cfg.min_independent_evidence} independent evidence items of class "
                    f"E1-E7; found {len(places)} [A4-23f]")
        if not any(e.evidence_class in ("E1", "E2", "E3") for e in strong):
            return "a behaviour fix needs a stated requirement or contract (E1, E2 or E3) among its evidence [A4-23f]"
        return ""

    # ---------------------------------------------------------------- delivery
    def verification_problem(self, req: uc.PublishRequest) -> str:
        """Agent3's reverification must show the fix works [A4-26..A4-33, A4-57..A4-59]."""
        v, p = req.verification, req.patch
        if p.status != "candidate_source_fix" or not p.patched_content:
            return "only a candidate fix with its patched content can be published"
        if not uc.safe_relpath(p.file) or p.file != req.layout.sut_file:
            return "a fix may only change the SUT file of the layout [A4-21]"
        if v.sut_sha256 != uc.sha256_bytes(uc.encode_like(Path(req.layout.repo_root) / p.file, p.patched_content)):
            return "the verification was not made on this patched source [A4-50, A4-51]"
        if not (v.build_ok and v.link_ok):
            return "the patched source does not build and link [A4-32]"
        stale = set(req.characterization_tests)                   # A4-23e
        real = [r for r in v.regressions if not any(f"test {name} fails" in r for name in stale)]
        if real:
            return "the patched source causes regressions: " + "; ".join(real)[:200] + " [A4-32]"
        if p.failure_id.startswith("test-"):
            test = p.failure_id[len("test-"):]
            outcome = next((t.status for t in v.tests if t.name == test), "")
            if outcome != "passed":
                return f"the test that identified the defect ({test}) does not pass [A4-30, A4-32]"
        broken = [t.name for t in v.tests if t.status != "passed" and t.name not in stale]
        if broken:
            return "tests other than characterization tests fail with the patched source: " + \
                ", ".join(broken[:5]) + " [A4-29, A4-32]"
        if not v.analyzer_run:
            return "the patched source was not checked by the static analyzer [A4-32a]"
        before = {(f.checker, f.function, re.sub(r"\d+", "#", f.message)) for f in req.baseline_findings}
        new = [f for f in v.analyzer_findings if (f.checker, f.function, re.sub(r"\d+", "#", f.message)) not in before]
        if new:
            return "the patched source has new static-analyzer findings: " + \
                "; ".join(f"{f.function}:{f.line} {f.message}" for f in new[:3]) + " [A4-32a]"
        if req.evidence:
            problem = self.evidence_problem(req.evidence) if p.failure_id.startswith("test-") else ""
            if problem:
                return problem
        return ""

    def publish(self, req: uc.PublishRequest) -> uc.PublishResult:
        p = req.patch
        repo = Path(req.original_repo)
        original = uc.read_source(repo / p.file) if (repo / p.file).is_file() else ""
        diff = "".join(difflib.unified_diff(original.splitlines(keepends=True), p.patched_content.splitlines(keepends=True),
                                            fromfile=f"a/{p.file}", tofile=f"b/{p.file}"))
        out = Path(req.output_dir) / "source_fixes"
        out.mkdir(parents=True, exist_ok=True)
        patch_file = out / f"{p.patch_id}.patch"
        header = (f"# {p.patch_id}: {p.function} in {p.file}\n# failure: {p.failure_id}\n"
                  f"# rationale: {p.rationale.replace(chr(10), ' ')}\n# verified by Agent3 run {req.verification.run_id}\n")
        patch_file.write_text(header + diff, encoding="utf-8", newline="")
        (out / f"{p.patch_id}.json").write_text(p.model_dump_json(indent=2), encoding="utf-8")
        branch, commit, reason = "", "", "patch file written"
        if not req.commit:
            return uc.PublishResult(patch_id=p.patch_id, status="verified_source_fix", patch_file=str(patch_file),
                                    reason="patch file written; the fix reaches the repository only through the "
                                           "review pull request [ORC-25]")
        try:
            if (repo / ".git").exists() and uc.git_dirty(repo, p.file):
                reason = (f"patch file only: {p.file} has uncommitted changes in your working tree, so the fix is "
                          "not committed to a branch")
            else:
                branch, commit = uc.commit_files_to_branch(
                    repo, {p.file: uc.encode_like(repo / p.file, p.patched_content)}, req.branch_prefix + p.patch_id,
                    f"Unit-test source fix {p.patch_id}: {p.function}\n\n{p.rationale}\n\n"
                    f"Failure {p.failure_id}; verified by Agent3 run {req.verification.run_id}.")
                if branch:
                    reason = f"committed to branch {branch}; the working branch is unchanged"
        except (OSError, subprocess.SubprocessError, RuntimeError, UnicodeError) as exc:
            reason = f"patch file only (git: {exc})"[:300]
        return uc.PublishResult(patch_id=p.patch_id, status="verified_source_fix", patch_file=str(patch_file),
                                branch=branch, commit=commit, reason=reason)


def _review_state(req: uc.ReviewRequest) -> str:
    v, p = req.verification, req.patch
    diff = "".join(difflib.unified_diff(p.original_fragment.splitlines(keepends=True),
                                        p.corrected_fragment.splitlines(keepends=True), "before", "after"))
    return json.dumps({
        "failure": {"classification": req.failure.classification, "diagnostic": req.failure.diagnostic[:600],
                    "test": req.failure.affected_test, "function": req.failure.affected_function},
        "evidence": [f"{e.evidence_id} {e.evidence_class} {e.source}:{e.line}: {e.text[:300]}"
                     for e in req.evidence if not e.linked_by][:8],
        "change": diff[:3000], "rationale": p.rationale[:600],
        "reverification": {"builds": v.build_ok and v.link_ok, "tests": f"{v.passed} passed, {v.failed} failed",
                           "regressions": v.regressions[:5],
                           "new_analyzer_findings": len(v.analyzer_findings)}})


async def review_patch(agent: "CorrectorAgent", req: uc.ReviewRequest) -> uc.ReviewResult:
    """A4-23h: another brain than the author when one is available, else the same brain in a separate
    request without its proposal. The review can only block."""
    router = await agent._brains()
    profile = ba.UseCase(domain=agent.name, questions=REVIEW_QUESTION)
    author = req.patch.author
    # Any other brain the run can use, in the configured order (e.g. Sonnet reviews Opus) [ARCH-1a]
    usable = getattr(router, "available", router.names)
    others = [n for n in usable if n != author and n in agent.allowed_brains]
    attempts = [(others, False)] if others else []
    attempts.append(([author] if author in router.names else router.names[:1], True))
    last_error = ""
    parts = agent.context_parts([uc.part("review", _review_state(req))], "A4.review", req.layout.repo_root)
    for allowed, same in attempts:
        plans: list[ba.TokenPlan] = []
        agent.emit("brain_call", data={"step": "A4.review"}, summary=f"{agent.view_label} asks for a second opinion")
        try:
            raw, brain, _ = await router.decide("", profile.brain_questions(), allowed, parts=parts, step="A4.review",
                                                need=ba.OutputNeed(items=1, tokens_per_item=60, overhead=250).tokens,
                                                plans=plans)
        except ba.AgentFault as exc:
            last_error = exc.code
            continue
        finally:
            agent.token_plans.extend(plan.record() for plan in plans)
            agent._brain_answer("A4.review", plans)
        agent.llm_calls += 1
        answer = ba.validate_answers(raw, profile)["review"]
        p = answer["probabilities"][answer["choice"]]
        name = router.name_of(brain)
        same = same or name == author
        return uc.ReviewResult(decision=answer["choice"], reviewer=f"{name}/{getattr(brain, 'model_id', '')}",
                               same_brain=same, reason=f"{answer['choice']} p={p:.2f}" +
                               (" - same brain as the author, separate request [A4-23h]" if same else ""))
    return uc.ReviewResult(decision="agree", reviewer="none", same_brain=True,
                           reason=f"not reviewed: no brain available ({last_error}) [A4-23h]")


def build_agent(settings: ba.Settings, config_path: str | None = None) -> CorrectorAgent:
    section = {}
    if config_path:
        section = json.loads(Path(config_path).read_text(encoding="utf-8")).get("corrector", {})
    agent = CorrectorAgent(settings, config=CorrectorConfig(**section))
    agent.packs = uc.knowledge_packs(config_path)                   # KP-3
    return agent


if __name__ == "__main__":
    _config = next((sys.argv[i + 1] for i, a in enumerate(sys.argv[:-1]) if a == "--config"), None)
    raise SystemExit(ba.agent_main(lambda s: build_agent(s, _config), "agent_sourcecode_corrector (Agent4)"))
