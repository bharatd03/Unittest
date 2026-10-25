"""KB - ut_fix_knowledgebase: advisory examples learned only from verified results.

The KB is isolated to the unit-test agents [KB-1, KB-2]. It stores positive examples
(verified complete), gap-closure examples (a measured gap closed, KB-5a) and negative
examples (failed or rejected patterns with their reason) [KB-3..KB-7]. It never decides
anything and is never evidence of expected behaviour [SC-22, SC-26a]; the orchestrator
hands its guidance to Agent2 together with Agent1's plan [A3-145a].

No LLM is used. Storage is SQLite; when the database cannot be opened, an in-memory
store is used and learning still never blocks generation [KB-18..KB-21].

Examples for the brains (KB-23 to KB-25): single tests and planned scenarios that Agent3 measured, found per
function by a full-text search over code words (SQLite FTS5), constructs in common and no near-duplicates.

Tools (MCP, each a contract): guidance, learn.
Run as an MCP server:  python ut_fix_knowledgebase.py serve [--config ut_config.json]
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

from pydantic import BaseModel

import ut_contracts as uc
from ut_contracts import ba

LOG = ba.logging.getLogger("ut.kb")
KIND_RANK = {"lesson": 3, "positive": 2, "gap_closure": 1, "negative": 0, "test_example": 0, "plan_example": 0}


class KBConfig(BaseModel):
    db_path: str = "ut_kb.sqlite3"
    max_positive: int = 500          # positive + gap_closure examples kept [KB-11]
    max_negative: int = 500
    max_snippet_chars: int = 4000
    examples: bool = True            # examples per function for the brains [KB-23 .. KB-25]
    max_examples: int = 3000         # examples kept per brain step [KB-23, ORC-7]
    examples_per_function: int = 2   # [KB-25, ORC-7]


def normalize(snippet: str) -> str:
    """Whitespace-insensitive form used for de-duplication [KB-9]."""
    return re.sub(r"\s+", " ", snippet).strip()


class KnowledgeBaseAgent(ba.AgentCore):
    name = "ut_fix_knowledgebase"
    application = "UT"                                            # VIEW-2
    view_role, view_description, view_support = uc.VIEW_AGENTS["ut_fix_knowledgebase"]

    def view_summary(self, model):
        return uc.view_summary(self.view_label, model) or super().view_summary(model)
    allowed_brains: list[str] = []           # advisory store: never calls an LLM

    def __init__(self, settings: ba.Settings | None = None, *, config: KBConfig | None = None,
                 router: ba.BrainRouter | None = None):
        self.cfg = config or KBConfig()
        self._lock = threading.Lock()
        self.backend = "sqlite"
        try:
            Path(self.cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
            # ORC-23 (d): several runs share the KB; a writer waits for the other instead of failing
            self.db = sqlite3.connect(self.cfg.db_path, check_same_thread=False, timeout=30)
            self._init_schema()
        except (sqlite3.Error, OSError, ValueError) as exc:   # KB-21: local fallback
            LOG.warning("KB database unavailable (%s); using an in-memory store.", type(exc).__name__)
            self.db = sqlite3.connect(":memory:", check_same_thread=False)
            self.backend = "memory"
            self._init_schema()
        super().__init__(settings, router=router)

    def _init_schema(self) -> None:
        self.db.execute("""CREATE TABLE IF NOT EXISTS examples (
            example_id TEXT PRIMARY KEY, kind TEXT NOT NULL, language TEXT NOT NULL, framework TEXT NOT NULL,
            category TEXT NOT NULL, snippet TEXT NOT NULL, reason TEXT NOT NULL, uses INTEGER NOT NULL,
            created REAL NOT NULL, verification TEXT NOT NULL)""")
        for column in ("pattern", "step", "function", "signature", "context", "project"):    # LA-8, KB-22, KB-23
            try:
                self.db.execute(f"ALTER TABLE examples ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass                                             # the column exists already
        self.fts = True
        try:                                     # KB-24: the full-text index of the examples (SQLite FTS5)
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS example_words USING fts5(example_id UNINDEXED, words)")
        except sqlite3.OperationalError:
            self.fts = False                     # an SQLite without FTS5: the same ranking in Python
        self.db.commit()

    async def close(self) -> None:
        await super().close()

    # ---------------------------------------------------------------- tools
    def tools(self) -> list[ba.ContractTool]:
        def learn_pre(event: uc.LearningEvent, request):
            v = event.verification
            if event.kind == "positive":
                # KB-4, A3-150, A3-151: only with Agent3's verification evidence
                if not (v.get("compile") == "passed" and v.get("tests") == "passed"
                        and v.get("coverage") == "complete" and v.get("verified") is True):
                    raise ba.ContractRejected("a positive example needs compile=passed, tests=passed, "
                                              "coverage=complete and verified=true from Agent3 [KB-4]")
            elif event.kind == "gap_closure":
                if not (v.get("gap_covered") is True and v.get("tests") == "passed" and v.get("regression") is False):
                    raise ba.ContractRejected("a gap-closure example needs gap_covered=true, tests=passed and "
                                              "regression=false from Agent3 [KB-5a]")
            elif event.kind == "lesson":
                # KB-22, LA-8: a lesson only after Agent3 verified the fix
                if not (v.get("verified") is True and v.get("gap_covered") is True and v.get("tests") == "passed"):
                    raise ba.ContractRejected("a lesson needs verified=true, gap_covered=true and tests=passed from "
                                              "Agent3 [KB-22]")
                if not event.reason.strip() or not event.pattern.strip():
                    raise ba.ContractRejected("a lesson needs its root cause and its pattern [KB-22]")
            elif event.kind in uc.EXAMPLE_KINDS:
                # KB-23: only what Agent3 measured - the test passed and, alone, reached items of its function
                if not (v.get("verified") is True and v.get("tests") == "passed" and v.get("reached")):
                    raise ba.ContractRejected("an example needs verified=true, tests=passed and the items it reached "
                                              "from Agent3 [KB-23]")
                if event.step not in ("A1.plan", "A2.tests") or not event.function.strip():
                    raise ba.ContractRejected("an example needs its brain step and its function [KB-23]")
            elif not event.reason.strip():
                raise ba.ContractRejected("a negative example needs its failure reason [KB-7]")
            if not event.snippet.strip():
                raise ba.ContractRejected("empty snippet")

        async def learn(event: uc.LearningEvent, request, scratch):
            return self.learn(event)

        async def guidance(req: uc.GuidanceRequest, request, scratch):
            return self.guidance(req)

        return [ba.ContractTool("guidance", uc.GuidanceRequest, uc.KBGuidance, guidance, read_only=True,
                                description="Advisory positive and negative examples for Agent2 [SC-22..SC-26a]."),
                ba.ContractTool("learn", uc.LearningEvent, uc.LearnAck, learn, learn_pre,
                                description="Store a verified or a negative example [KB-3..KB-12].")]

    # ---------------------------------------------------------------- learning
    def learn(self, event: uc.LearningEvent) -> uc.LearnAck:
        snippet = event.snippet[: self.cfg.max_snippet_chars]
        key = "|".join([event.kind, event.language, event.framework, event.category, normalize(snippet)]
                       + ([event.pattern] if event.kind == "lesson" else [])
                       + ([event.step] if event.kind in uc.EXAMPLE_KINDS else []))
        example_id = "KB-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        try:
            with self._lock:
                row = self.db.execute("SELECT uses FROM examples WHERE example_id = ?", (example_id,)).fetchone()
                if row:                                                   # KB-9, KB-10
                    self.db.execute("UPDATE examples SET uses = uses + 1 WHERE example_id = ?", (example_id,))
                    self.db.commit()
                    return uc.LearnAck(stored=True, example_id=example_id, duplicate=True,
                                       reason=f"uses={row[0] + 1}")
                self.db.execute("INSERT INTO examples (example_id, kind, language, framework, category, snippet, "
                                "reason, uses, created, verification, pattern, step, function, signature, context, "
                                "project) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (example_id, event.kind, event.language, event.framework, event.category, snippet,
                                 event.reason, 1, time.time(), json.dumps(event.verification, sort_keys=True),
                                 event.pattern, event.step, event.function, event.signature,
                                 event.context[:2000], event.project))
                if event.kind in uc.EXAMPLE_KINDS and self.fts:                               # KB-24
                    self.db.execute("INSERT INTO example_words (example_id, words) VALUES (?, ?)",
                                    (example_id, " ".join(self._words(event.signature, event.pattern, event.context,
                                                                      snippet))))
                self._evict(event.kind, event.step)
                self.db.commit()
            return uc.LearnAck(stored=True, example_id=example_id)
        except sqlite3.Error as exc:                                      # KB-18: never blocks generation
            LOG.warning("KB write failed: %s", type(exc).__name__)
            return uc.LearnAck(stored=False, example_id=example_id, reason="storage failure")

    def _evict(self, kind: str, step: str = "") -> None:
        """Bounded working sets; the least valuable examples go first [KB-11, KB-12, KB-23]."""
        if kind in uc.EXAMPLE_KINDS:
            count = self.db.execute("SELECT COUNT(*) FROM examples WHERE kind = ? AND step = ?", (kind, step)).fetchone()[0]
            if count > self.cfg.max_examples:
                victims = self.db.execute("SELECT example_id FROM examples WHERE kind = ? AND step = ? ORDER BY uses, "
                                          "created LIMIT ?", (kind, step, count - self.cfg.max_examples)).fetchall()
                self.db.executemany("DELETE FROM examples WHERE example_id = ?", victims)
                if self.fts:
                    self.db.executemany("DELETE FROM example_words WHERE example_id = ?", victims)
            return
        if kind == "negative":
            kinds, limit = ("negative",), self.cfg.max_negative
        elif kind == "lesson":
            kinds, limit = ("lesson",), self.cfg.max_positive
        else:
            kinds, limit = ("positive", "gap_closure"), self.cfg.max_positive
        marks = ",".join("?" * len(kinds))
        count = self.db.execute(f"SELECT COUNT(*) FROM examples WHERE kind IN ({marks})", kinds).fetchone()[0]
        if count <= limit:
            return
        victims = self.db.execute(
            f"SELECT example_id FROM examples WHERE kind IN ({marks}) "
            "ORDER BY CASE kind WHEN 'positive' THEN 1 ELSE 0 END, uses, created LIMIT ?",
            (*kinds, count - limit)).fetchall()
        self.db.executemany("DELETE FROM examples WHERE example_id = ?", victims)

    # ---------------------------------------------------------------- retrieval
    def guidance(self, req: uc.GuidanceRequest) -> uc.KBGuidance:
        """Only examples of the same language and framework are offered [SC-24]; ranked by
        category match, kind (verified complete first) and how often they were seen [KB-13, KB-14]."""
        with self._lock:
            rows = self.db.execute(
                "SELECT example_id, kind, language, framework, category, snippet, reason, uses, created, pattern "
                "FROM examples WHERE language = ? AND (framework = ? OR kind = 'lesson') "
                "AND kind NOT IN ('test_example', 'plan_example')",
                (req.language, req.framework)).fetchall()
        wanted = set(req.categories)
        patterns = set(req.patterns)

        def score(row) -> tuple:
            return (row[4] in wanted, KIND_RANK[row[1]], row[7], row[8])

        examples = [uc.KBExample(example_id=r[0], kind=r[1], language=r[2], framework=r[3], category=r[4],
                                 snippet=r[5], reason=r[6], uses=r[7], pattern=r[9] or "")
                    for r in sorted(rows, key=score, reverse=True)]
        # LA-8, KB-22: lessons of the same pattern first - a lesson of another construct does not apply
        lessons = sorted([e for e in examples if e.kind == "lesson" and set(e.pattern.split("; ")) & patterns],
                         key=lambda e: (len(set(e.pattern.split("; ")) & patterns), e.category in wanted, e.uses),
                         reverse=True)
        found: dict[str, list[uc.KBExample]] = {}
        k = min(req.examples_per_function, self.cfg.examples_per_function)
        if req.functions and k > 0 and self.examples_on():
            for query in req.functions:                                                  # KB-24
                hits = self.search(query, req.language, k)
                if hits:
                    found[uc.example_key(query.step, query.function)] = hits
        return uc.KBGuidance(positive=[e for e in examples if e.kind in ("positive", "gap_closure")][: req.k],
                             negative=[e for e in examples if e.kind == "negative"][: req.k],
                             lessons=lessons[: req.k], examples=found)

    # ---------------------------------------------------------------- examples per function [KB-23 .. KB-25]
    def examples_on(self) -> bool:
        """KB-25: kb.examples, and UT_KNOWLEDGE=off (the benchmark without knowledge, KP-8)."""
        import os
        return self.cfg.examples and os.environ.get("UT_KNOWLEDGE", "").lower() not in ("0", "off", "false", "no")

    @staticmethod
    def _words(*texts: str) -> list[str]:
        return uc.code_words(" ".join(texts))

    @staticmethod
    def _shape(snippet: str, function: str) -> str:
        """An example without its own names (the function, the test), for telling near-duplicates apart."""
        short = function.split("::")[-1]
        text = re.sub(r"\bUT_TEST\s*\(\s*\w+\s*\)", "UT_TEST(T)", snippet)
        return re.sub(r"\b" + re.escape(short) + r"\b", "FN", text) if short else text

    def search(self, query: uc.ExampleQuery, language: str, k: int) -> list[uc.KBExample]:
        """KB-24: the examples of a step most like the function - full-text rank over code words, a bonus for each
        construct in common, examples nearly equal to one already chosen left out."""
        kind = "plan_example" if query.step == "A1.plan" else "test_example"
        words = self._words(query.text, " ".join(query.patterns))[:48]
        if not words:
            return []
        columns = ("e.example_id, e.kind, e.language, e.framework, e.category, e.snippet, e.reason, e.uses, e.pattern, "
                   "e.step, e.function, e.signature, e.context")
        with self._lock:
            if self.fts:
                match = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
                rows = self.db.execute(
                    f"SELECT {columns}, bm25(example_words) FROM example_words JOIN examples e USING (example_id) "
                    "WHERE example_words MATCH ? AND e.kind = ? AND e.step = ? AND e.language = ? "
                    "ORDER BY bm25(example_words) LIMIT 40", (match, kind, query.step, language)).fetchall()
                scored = [(-r[-1], r[:-1]) for r in rows]
            else:
                rows = self.db.execute(f"SELECT {columns} FROM examples e WHERE e.kind = ? AND e.step = ? AND "
                                       "e.language = ?", (kind, query.step, language)).fetchall()
                wanted = set(words)
                scored = sorted(((len(wanted & set(self._words(r[11], r[8], r[12], r[5]))) / len(wanted), r)
                                 for r in rows), key=lambda x: -x[0])[:40]
                scored = [x for x in scored if x[0] > 0]
        patterns = set(query.patterns)
        ranked = sorted(((score + 0.5 * len(patterns & set(r[8].split("; "))), r) for score, r in scored),
                        key=lambda x: (-x[0], -x[1][7], x[1][0]))
        chosen, seen = [], []
        for _, r in ranked:
            words_of = set(self._words(self._shape(r[5], r[10])))
            if any(len(words_of & s) / max(1, len(words_of | s)) > 0.7 for s in seen):
                continue                                           # nearly the same as one chosen [KB-24]
            seen.append(words_of)
            chosen.append(uc.KBExample(example_id=r[0], kind=r[1], language=r[2], framework=r[3], category=r[4],
                                       snippet=r[5], reason=r[6], uses=r[7], pattern=r[8], step=r[9], function=r[10],
                                       signature=r[11], context=r[12]))
            if len(chosen) >= k:
                break
        return chosen


def build_agent(settings: ba.Settings, config_path: str | None = None) -> KnowledgeBaseAgent:
    section = {}
    if config_path:
        path = Path(config_path)
        section = dict(json.loads(path.read_text(encoding="utf-8")).get("kb", {}))
        if "db_path" in section and not Path(section["db_path"]).is_absolute():
            section["db_path"] = str((path.resolve().parent / section["db_path"]).resolve())
    return KnowledgeBaseAgent(settings, config=KBConfig(**section))


if __name__ == "__main__":
    _config = next((sys.argv[i + 1] for i, a in enumerate(sys.argv[:-1]) if a == "--config"), None)
    raise SystemExit(ba.agent_main(lambda s: build_agent(s, _config), "ut_fix_knowledgebase (KB)"))
