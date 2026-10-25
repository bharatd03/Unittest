# Unit-test generator: agent orchestration

Requirement IDs in brackets refer to `unittest_generator_requirments.txt`.

| Agent | Python file | Role |
| --- | --- | --- |
| Orchestrator | `ut_pipeline.py` | Runs the pipeline, the only MCP client of the agents [ORC-1] |
| Agent1 | `agent_unittest_analyzer.py` | Discovery, evidence, setup-repair plan, coverage plan |
| Agent2 | `agent_unittest_generator.py` | Test suites, test cases, stubs, test-side fixes |
| Agent3 | `agent_unittest_compilation.py` | Harness, build, run, coverage, classification, routing |
| Agent4 | `agent_sourcecode_corrector.py` | Production-source corrections |
| KB | `ut_fix_knowledgebase.py` | Advisory examples from verified results |
| Contracts | `ut_contracts.py` | Every handoff schema, the tool manifest reader |
| Setup | `ut_setup.py` via `unittest_generator.bat` / `.sh` | Checks and installs the tools into `tools\` [TP] |

Three layers everywhere: the brains (the Claude models the API key can access, else Qwen3 8B) only suggest; the agents check every suggestion
deterministically and decide; the tools (compiler, coverage, git) act. Where the brain is used:

| Agent | The brain suggests | Checked by |
| --- | --- | --- |
| Agent1 | stub or real per dependency [A1-DA8] | rules, then Agent3's build [A1-DA9, A1-DA12] |
| Agent1 | test scenarios per function, from the second plan on [A1-PL1] | A1-PL4: gap ids, quotes word for word, value ranges, stubs, no E8 or conflicting expectation |
| Agent1 | which functions a requirement in prose describes [A1-PL13] | id and quote exist; such evidence never justifies a source fix [A4-23i] |
| Agent1 | a declaration for a symbol that has none [A1-PL14] | parses as that symbol; Agent3's build |
| Agent2 | test code the scenarios cannot be assembled into; rewrites of failing E1-E7 tests [A2-21m..A2-21p] | symbols that exist, readable names; Agent3 runs them; oracle check for changed expectations |
| Agent3 | root cause of a failing test with stated evidence [A3-CL] | p >= 0.8 for a source defect; linked evidence -> requirement_mismatch [A3-CL11] |
| Agent3 | missing flags of an estimated build [A3-BD11] | allowed flags, cited text exists, kept only if the SUT's errors drop [A3-BD12] |
| Agent4 | up to 3 candidate fixes [A4-23g] | fragment checks, Agent3 reverifies each, publication rules; smallest one wins |
| Agent4 | a second opinion from another brain [A4-23h] | can only block |
| Agent4 | after a stop below target: source changes that let tests reach the open items, from the requirements [COV-12] | fragment and quotes word for word; Agent3 measures each on a trial copy; shown only when it reaches more and lowers nothing, and the tests still detect every compared mutant they detected before (Agent3 fault_check, no brain); never applied [COV-13, COV-14 .. COV-19] |

## 0. Tools first

```mermaid
flowchart LR
    cmd(["unittest_generator.bat / .sh<br/>any command"]) --> chk["ut_setup.py ensure<br/>(fast when nothing changed)"]
    chk --> have{"Usable tool<br/>already installed?<br/>TP-3, TP-8"}
    have -- yes --> use["use it<br/>no second copy"]
    have -- no --> dl["download the pinned version<br/>official release page, HTTPS"]
    dl --> sha{"SHA-256<br/>matches?"}
    sha -- no --> refuse(["delete, stop<br/>TP-4"])
    sha -- yes --> unpack["unpack into tools/&lt;tool&gt;"]
    unpack --> check["functional check<br/>C, C++, MC/DC, libFuzzer, analyzer;<br/>platform probes (NC-10)"]
    check -- blocked by Windows --> blocked(["stop and report<br/>TP-9"])
    use --> manifest["tools/toolchain.json"]
    check -- ok --> manifest
    manifest --> pipe(["pipeline: tools only<br/>from the manifest, TP-10"])
```

The functional check also runs small probe programs and writes the platform facts into the manifest: how a crash, an abort, an uncaught exception, a stack overflow and the watchdog's end appear, the register page unit, whether file names keep their letter case and how the compiler prints paths [NC-10]. The agents read platform behaviour only from these facts [NC-11].

The application never searches for tools and has no fallback compiler. Windows uses llvm-mingw (self-contained, no administrator rights) unless a working Visual Studio/LLVM installation exists; Linux uses an installed Clang 18+ or the official LLVM release. Per repository, Make, Meson and Bazel are added when its build files need them [TP-5].

## 1. The whole pipeline

```mermaid
flowchart TD
    start(["run_pipeline: repository + SUT<br/>+ requirement documents (A1-RQ1)"]) --> disc["Agent1 discover<br/>layout, tests, stubs, build system,<br/>requirement documents read (A1-RQ2)<br/>no LLM"]
    disc --> capture["Agent3 capture<br/>the project's own build recorded<br/>CMake, Meson, Bazel, Ninja, Make, MSBuild<br/>no LLM; reused per tested commit (ORC-22)"]
    capture --> est{"estimated?"}
    est -- yes --> flags["Agent3 suggest_flags<br/>brain reads the build files,<br/>a compile decides (A3-BD11)"]
    est -- no --> deps
    flags --> deps["Agent1 dependencies<br/>Clang front end with the recorded flags<br/>stub / real / system;<br/>requirements in prose linked (A1-PL13)"]
    deps --> probe["Agent3 probe<br/>build, link, run, baseline coverage,<br/>static-analyzer baseline, no LLM"]
    probe --> builds{"Builds, links<br/>and runs?"}

    subgraph stageA ["Stage A: make it build"]
        builds -- no --> setupPlan["Agent1 setup-repair plan<br/>missing declarations proposed (A1-PL14)"]
        setupPlan --> testFix["Agent2 test-side fixes"]
        setupPlan --> srcFix["Agent4 candidates, each built,<br/>smallest passing one reviewed<br/>and published"]
        testFix --> buildCheck["Agent3 build check"]
        srcFix --> buildCheck
        buildCheck --> builds
    end

    subgraph stageB ["Stage B: coverage"]
        builds -- yes --> covPlan["Agent1 coverage plan<br/>iteration 1 with nothing measured: cheap tests first<br/>(rule-based tests + search, A1-PL1);<br/>then the brain plans every gap left, with what was tried,<br/>suggested values and earlier refusals as facts;<br/>format fixed, content checked (A1-PL, ARCH-20)"]
        covPlan --> kb["KB guidance<br/>fetched by the orchestrator"]
        kb --> gen["Agent2 generate<br/>suites, test cases, stubs"]
        gen --> verify["Agent3 verify<br/>build, run all tests, measure"]
        verify --> journal["Failure journal (LA-2): root cause of every miss<br/>by rules from the tools' results (LA-3);<br/>covered fixes become lessons in the KB (LA-8)"]
        journal --> route{"Agent3 routing<br/>A3-177"}
        route -- "test or stub defect" --> covPlan
        route -- "gaps remain" --> covPlan
        route -- "gaps remain,<br/>no new tests reach them" --> fuzz["Agent3 fuzz<br/>libFuzzer or ut_search, inputs that reach the gaps;<br/>runs while the brain plans the next iteration,<br/>one time budget per run (ORC-5a)"]
        fuzz --> covPlan
        covPlan -. "coverage 100%,<br/>stated behaviour unchecked<br/>A1-25y" .-> gen
        route -- "undetermined" --> covPlan
        route -- "source defect" --> corr["Agent4 candidates<br/>staging copy only"]
        corr --> reverify["Agent3 reverifies each,<br/>Agent4 checks, second brain<br/>reviews the smallest, re-baseline"]
        reverify --> route
        corr -- "no source change passes:<br/>the test is rewritten,<br/>open item for the developer (A4-23j)" --> covPlan
    end

    route -- "every ASIL target reached,<br/>measured, behaviour checked" --> done(["verified_measured<br/>(COV-1, COV-3)"])
    route -- "every open item has a final status (LA-6),<br/>no new coverage and no new root cause twice,<br/>or the safety ceiling scaled with the file (LA-7)" --> handover["hand-over: evidence per item,<br/>Agent4 suggestions checked on<br/>a trial copy, action needed<br/>(COV-9 .. COV-13)"]
    handover --> stop(["incomplete,<br/>with reason"])
    route -- "environment" --> stop
    builds -- "budget exhausted" --> stop
    covPlan -- "no brain answers at all<br/>(A1-PL8)" --> stopped(["stopped: what Agent3 verified<br/>is delivered (ORC-34)"])
```

Agent1 is the only agent that sends work to Agent2 [A1-35]. Agent3 decides the routing; the orchestrator executes it [ORC-6].

## 2. Routing by failure category

```mermaid
flowchart LR
    fail["Agent3 classifies<br/>the failure"] --> c1["test_generation_defect"]
    fail --> c2["stub_defect"]
    fail --> c3["test_framework_defect"]
    fail --> c4["build_configuration_defect"]
    fail --> c5["source_compilation_defect"]
    fail --> c6["source_behavior_defect"]
    fail --> c7["requirement_mismatch"]
    fail --> c8["undetermined"]
    fail --> c9["environment_defect"]

    c1 --> a1["Agent1 re-plan"]
    c2 --> a1
    c2 -.-> kbneg["KB negative example"]
    c7 --> a1
    c8 --> ev["Agent1 more evidence,<br/>then reclassify"]
    a1 --> a2["Agent2 repair"]
    c3 --> self["Agent3 repairs<br/>its own harness or build"]
    c4 --> self
    c5 --> a4["Agent4 source fix"]
    c6 --> a4
    a4 --> rv["Agent3 reverify"]
    c9 --> halt(["report and stop"])
```

## 3. One coverage iteration, call by call

Every arrow is an MCP tool call made by the orchestrator. Every payload is a contract from `ut_contracts.py`, checked by the receiver (preconditions) and by the orchestrator (postconditions), and written to the ledger [ARCH-4, ORC-8].

```mermaid
sequenceDiagram
    autonumber
    participant O as Orchestrator
    participant A1 as Agent1
    participant KB as KB
    participant A2 as Agent2
    participant A3 as Agent3
    participant A4 as Agent4
    participant L as Ledger

    O->>A1: plan(PlanRequest: layout, last VerificationResult, attempts, fuzz inputs, per-test items, kind per item)
    Note over A1: facts first; from the 2nd plan the brain suggests<br/>scenarios per function, Agent1 checks each (A1-PL4)
    A1-->>O: UTPlan (scenarios with who made them, repair items, conflicts, look-ups)
    O->>KB: guidance(GuidanceRequest)
    KB-->>O: KBGuidance (advisory examples)
    O->>A2: generate(GenerationRequest: UTPlan + KBGuidance + current suite)
    Note over A2: checked scenarios assembled directly; the LLM writes<br/>requirement tests, hard set-ups and rewrites (A2-21m..p)
    A2-->>O: FileActionBundle (stubs, suite, tests, probes for E8 values; tests refused only by predictions)
    opt tests refused only by predictions (NC-7)
        O->>A3: observe(VerifyRequest: only those tests, each measured alone)
        A3-->>O: compiled? passed? items reached - kept, or out with the tool's message for the journal
    end
    opt the bundle observes values (A2-21f)
        O->>A3: observe(VerifyRequest: bundle)
        A3-->>O: observed values + per-test coverage (A3-OBS5)
        O->>A2: finalize(FinalizeRequest)
        A2-->>O: FileActionBundle (assertions, redundant probes removed, A2-21i)
    end
    opt Agent2 declared expectation changes
        O->>A3: oracle_check(OracleRequest)
        A3-->>O: OracleResult (weakening -> original test kept)
    end
    O->>A3: verify(VerifyRequest: layout + bundle + previous result)
    A3-->>O: VerificationResult (build, tests, coverage, classification, routing)
    O->>L: append every call (hashes, signatures, status)
    O->>KB: learn(LearningEvent), only from Agent3's evidence
    alt complete and verified
        O->>A1: plan - done, or behaviour scenarios still to check (A1-25y)
    else gaps without failures, not fuzzed yet
        O->>A3: fuzz(FuzzRequest: functions, gaps, covered items)
        A3-->>O: FuzzResult (concrete inputs, the gaps they reach)
        O->>A1: plan(PlanRequest with the inputs, A1-18c)
    else gaps, test or stub defects, undetermined
        O->>A1: plan(PlanRequest with the new VerificationResult)
    else source defect (route agent4)
        O->>A1: collect_evidence(function)
        O->>A4: candidates(CorrectionRequest: failure, two independent evidence items, test)
        A4-->>O: CandidateSet (up to 3, never applied to your files)
        loop every candidate
            O->>A3: verify(VerifyRequest with the patched source + static analyzer)
            A3-->>O: VerificationResult (reverified, analyzer findings)
            O->>A4: check(PublishRequest)
            A4-->>O: PublishCheck
        end
        O->>A4: review(ReviewRequest: smallest passing candidate)
        A4-->>O: ReviewResult (another brain; can only block)
        O->>A4: publish(PublishRequest: patch + Agent3's result + analyzer baseline)
        A4-->>O: PublishResult (patch file; branch, or the review pull request for a link)
        O->>A1: discover + dependencies (re-baseline on the fixed source, A4-40)
    end
```

## 4. Inside every agent: BaseAgent-derived agentic RAG

```mermaid
flowchart LR
    req["MCP tool call<br/>BaseAgent envelope<br/>+ contract payload"] --> pre["check preconditions<br/>and signatures"]
    pre --> retrieve["retrieve evidence<br/>from the agent's<br/>knowledge store"]
    retrieve --> det["deterministic tools<br/>parse, build, measure"]
    det --> need{"LLM needed?"}
    need -- no --> act
    need -- yes --> packs["knowledge packs<br/>for this step (KP-3)"]
    packs --> budget["token budget per call<br/>window and answer limit (ARCH-11)"]
    budget --> brain["brain router<br/>accessible Claude model,<br/>else remote Qwen3 8B,<br/>else Qwen3 8B in Ollama"]
    brain --> check["validate against<br/>the contract schema"]
    check -- invalid --> brain
    check -- valid --> act["act and build<br/>the response contract"]
    act --> resp["AgentResponse<br/>status, data, meta, error"]
```

An invalid LLM answer is asked again once and otherwise rejected [ARCH-5].

### 4a. Rule first, brain when the rule fails, the tool decides [ARCH-21 .. ARCH-23]

Every agent runs its deterministic rules first: they are fast and repeatable. A rule that meets code it was
not written for does not end with "cannot". It hands the case to the brain through BaseAgent's
`AgentCore.resolve()`, and the tool that rejected the rule's result judges the brain's answer:

```mermaid
flowchart LR
    rule["deterministic rule"] -- result --> tool["tool: front end,<br/>compiler, test run"]
    rule -- "cannot (type, value,<br/>header, generated file)" --> mem{"stored resolution<br/>for this failure?<br/>(ARCH-23)"}
    tool -- rejects --> mem
    mem -- yes --> verify["the same tool<br/>checks it"]
    mem -- no --> brain["brain: the rule, why it failed,<br/>the facts, the tool's message"]
    brain --> verify
    verify -- accepted --> keep["used; stored per repository;<br/>meta.escalations"]
    verify -- "rejected (tool message)" --> brain
    verify -- "attempts used up / no brain" --> report["reported: rule, attempts,<br/>tool's last message"]
```

| Agent | Rule that can fail | What the brain gets | The tool that decides |
|---|---|---|---|
| Agent1 | a header the front end does not find (A1-DA20); after a search by name with one candidate | candidates with that name, the folders with headers | the front end parses again: the header is found and errors do not grow |
| Agent2 | a parameter whose value no rule can build (A2-21ac) | the function, the type's declarations, the SUT's functions | declared symbols only, no assertion; then Agent3's build; stored only once tests using it are in the verified suite |
| Agent2 | the object a member function is called on, or a fake of an interface the rules cannot fake (NC-1) | the constructors, the fakes, the headers, the journal's compiler message | the guards of BP-4, then Agent3's build; stored as a recipe with parameters once its test is in the measured suite (NC-2) |
| Agent3 | the test program does not build in generated files (A3-CL14) | compiler errors, the generated files, the SUT's include lines | the compiler: fewer errors; assertions, test names and calls unchanged |
| Agent3 | the SUT does not compile on the host after the keyword and assembly tables (A3-BD16) | compiler errors, the SUT, the flags | the compiler: fewer SUT errors; no SUT function defined, no user file changed |
| Agent4, Agent1 planning | already brain steps (A4-23g, COV-12, A1-PL1) | as before | Agent3's verify |

The result and the report list every case under "Rules that needed the brain".

### 4b. A brain answer: form forgiven, content checked [ARCH-20]

```mermaid
flowchart LR
    ans(["brain answer"]) --> fix["agent's format fixes<br/>':1' -> 1, copied gap line -> its id;<br/>each fix recorded"]
    fix --> check{"agent's strict check<br/>(unchanged)"}
    check -- passes --> use(["used; Agent3 compiles,<br/>runs and measures"])
    check -- refused --> reask["re-ask: each refused item,<br/>its reason and the form expected;<br/>other sampling"]
    reask --> ans
    reask -- "still refused" --> other["next brain of ANOTHER model<br/>(never the same model on a slower server)"]
    other -- "none" --> later(["no scenario this time; the refusals<br/>are facts of the next plan"])
```

Every answer, fix and refusal is written to `<run>/brain_answers.jsonl`.

## 5. Which brain answers, and how the agents are reached

Every brain call goes to the first available brain in `brain.engine.brain_order` of `ut_config.json` [ARCH-1a].
When the agents start, BaseAgent checks which Claude models the API key in `ANTHROPIC_API_KEY` can access (no
tokens are spent). Models without access are skipped for the whole run. When no Claude model is available, Qwen3 8B
on the remote server answers (`brain.qwen_remote`, key in the environment variable `QWEN_REMOTE_API_KEY`), and
Qwen3 8B through Ollama on this PC only when the remote server is not available either. The same code, contracts and
checks are used for every brain; what a brain may write and how much context it gets is its profile (section 7a2)
[ARCH-1b, BP-1]; `unittest_generator.bat check` shows which brains are in use.

```mermaid
flowchart LR
    call(["brain call<br/>of any agent"]) --> o{"Opus 5.5<br/>access?"}
    o -- yes --> ans(["answer, checked<br/>by the agent"])
    o -- no --> f{"Fable 5.1?"}
    f -- yes --> ans
    f -- no --> s{"Sonnet 5.5?"}
    s -- yes --> ans
    s -- no --> h{"Haiku 5.5?"}
    h -- yes --> ans
    h -- no --> r{"Qwen3 8B on the<br/>remote server?"}
    r -- yes --> ans
    r -- no --> q{"Qwen3 8B<br/>in Ollama?"}
    q -- yes --> ans
    q -- no --> stop(["no brain: the run stops<br/>or does not start (ARCH-24)"])
```

The order, the model ids and which brains are enabled are configuration only. A brain that is only unreachable
for a moment is skipped for the cooldown and tried again; a brain that is not enabled is never sent anything
[ARCH-9].

The agents themselves are reached in one of two ways, again only by configuration (`pipeline.agents`): started
by the orchestrator as subprocesses (MCP over stdio, the default), or as FastMCP HTTP servers with a bearer token
each, for example on a build server next to the code and the compiler [ARCH-7, ARCH-8].

```mermaid
flowchart LR
    o1["ut_pipeline"] -- "stdio: starts as subprocess<br/>or HTTPS: bearer token" --> a["Agent1 .. Agent4, KB"]
    a --> r["BaseAgent brain router<br/>(ARCH-1a)"]
```

## 6. A repository link: cache, worktrees, delivery

```mermaid
flowchart TD
    link(["run: repository link, SUT files,<br/>branch (default: the server's default)"]) --> head["git ls-remote: the branch head<br/>= the tested commit (ORC-19)"]
    head --> cache["cache: one bare clone per PC,<br/>later runs only fetch (ORC-20)"]
    cache --> named{"SUT files named?"}
    named -- no --> select["Agent1 select_files in a worktree<br/>of the tested commit: production files,<br/>no tests, third-party, generated code (ORC-35)"]
    select --> wt
    named -- yes --> wt["one git worktree per SUT file<br/>at the tested commit"]
    wt --> pipe["the whole pipeline per SUT file<br/>(in parallel up to max_parallel_suts, ORC-23)"]
    pipe --> kind{"per SUT file"}
    kind -- "verified, tests only" --> moved{"branch still at<br/>the tested commit?"}
    moved -- yes --> push["one commit to the branch:<br/>tests, stubs, harness,<br/>ut_reports/&lt;SUT&gt;/ (ORC-24, ORC-27)"]
    moved -- no --> again["verify the tests again<br/>on the new head (ORC-29)"]
    again -- "same final state" --> push
    again -- otherwise --> pr
    push -- "refused: protected" --> testsPr["review branch ut-review/tests-...<br/>pull request, tests only (ORC-30)"]
    push -- "no write access" --> local["branch in the cache,<br/>patch + git bundle in the output (ORC-30)"]
    kind -- "production source changed,<br/>or not verified" --> pr["review branch ut-review/&lt;SUT&gt;-&lt;commit&gt;:<br/>fixed source + tests + reports;<br/>pull request (ORC-25, ORC-28)"]
    pr --> human(["a person reviews; merging is the approval (ORC-26)"])
```

**Several files in parallel (ORC-23).** `pipeline.max_parallel_suts` (default 2) files run at the same time - for a
repository link and for a local folder alike; an interactive run (LA-9) runs them one after another. Each file works
in its own copy (a worktree, or a copy of the folder taken when it starts), with its own output folder and agent
processes; the answer log of a run goes to its own agents only. Each file's generated files carry its unit name
(`test_<unit>.c`, `ut_stubs_<unit>.c/.h`, `ut_sut_access_<unit>.c`, A2-21af), so two files of one component never
replace each other's stubs. What the files share is written safely: the knowledge base and the resolution memory wait
for each other's writes, one build capture per cache key runs at a time and the others reuse it, Agent3 never
removes a run folder younger than two hours, and findings are appended as whole lines. A lesson (LA-8) learned by one
file reaches another only if it was stored before that file asked.

Git uses the login already configured on the PC (Git Credential Manager, SSH key) or a token in the environment
variable named in `ut_config.json`; it is never written anywhere [ORC-21]. The pipeline never force-pushes,
rewrites history, or deletes or renames a branch [ORC-31].

## 6a. Requirement documents given for a run [A1-RQ1 .. A1-RQ6]

`--requirements FILE_OR_FOLDER` (repeatable; in the menu of `unittest_generator.bat` one question), the request of
`run_pipeline` / `run_repository` (paths inside the repository only) or `"requirements"` per repository in
`ut_config.json`. The orchestrator copies every ReqIF, Markdown, text or CSV document unchanged into
`<run>/requirements/` and records its SHA-256 in the ledger; Word, PDF and Excel files are reported, not read.
Agent1 reads them as E1 evidence (an ID stated differently in the repository is a conflict; the given text is
used), links them by rule or by the brain (A1-PL13), and at the end reports per function which requirements a
passed test asserts and which functions have no stated behaviour at all (tool `requirement_status`, A1-RQ5).
The documents never reach the repository.

## 7. Knowledge packs and the token budget of every brain call

**Knowledge packs.** Short Markdown files with rules and worked examples, added to a brain's prompt after the facts,
so that every run, both brains and every project choose the same way [KP-1 .. KP-9]. They are advisory: no check is
relaxed, and expected values still come only from evidence [KP-5, KP-6]. Not the KB (learned examples), and not
`.github/skills` (guidance for whoever changes this application).

| Pack | Read by | Status |
| --- | --- | --- |
| `knowledge/test-design.md` | Agent1 planning (A1.plan), Agent2 tests (A2.tests) | version 1.0, used |
| `knowledge/test-style.md` | Agent2 tests (A2.tests); a project's `.ut/knowledge/test-style.md` replaces it | version 1.0, used |
| embedded C patterns | Agent1 dependencies and planning, Agent2 tests | planned |
| fix and review checklist | Agent4 candidates and review | planned |
| reading build files | Agent3 flag suggestions | planned |
| reading requirements | Agent1 requirement links | planned |

Every agent loads the packs when it starts (`knowledge` in `ut_config.json`; BaseAgent's `KnowledgePacks`) and
each brain call names its step, so it gets exactly the packs whose `used_by` names that step. A project adds or
replaces packs in `.ut/knowledge/` of its repository. A pack without a valid header or with an unknown step is
not used and is reported in the agent's log. `UT_KNOWLEDGE=off` switches all packs off;
`unittest_generator.bat benchmark both` runs the benchmark with and without them and compares the results [KP-8].

**Token budget.** No call has a fixed size [ARCH-11 .. ARCH-17]. The agent passes its context as parts with a
priority and says how long the answer will be (for example 320 tokens per scenario for the open gaps). BaseAgent
then plans the call for each brain:

```mermaid
flowchart TD
    call(["brain call of a step<br/>parts + expected answer"]) --> work{"anything to ask?<br/>ARCH-16"}
    work -- no --> rules(["no call, no tokens"])
    work -- yes --> est["estimate tokens<br/>characters per token learned per brain [ARCH-12]"]
    est --> fit{"fits a window?<br/>Qwen 4k / 8k / 12k / 16k<br/>Claude models 1M [ARCH-14]"}
    fit -- yes --> send["smallest window that fits<br/>answer limit = expected + 30 %"]
    fit -- no --> cut["give up parts: 4 examples, 3 tried and build files,<br/>2 packs, 1 evidence; facts (0) never [ARCH-13, ARCH-17]"]
    cut --> fit2{"fits now?"}
    fit2 -- yes --> send
    fit2 -- no --> next(["next brain, or the agent's rules"])
    send --> rec["plan recorded: window, tokens,<br/>parts shortened, packs used [ARCH-15]"]
```

| Priority | Parts |
| --- | --- |
| 0, never cut | function source and signature, gaps or failure, parameters, scenarios, stub header, re-ask problems |
| 1 | expected-behaviour evidence; headers of the SUT (their beginning is kept) |
| 2 | knowledge packs |
| 3 | what was tried before, existing test names, build files, related source |
| 4 | KB examples, the existing test style |

Qwen keeps a loaded window while it is large enough (`sticky_window`), because Ollama reloads the model on every
change of the window. The result and `report.md` list per step how many calls were made, with which windows and
tokens, and what was shortened; the ledger has every single plan [ORC-12].

## 7a. Coverage targets and the stop [COV]

Every SUT file is ASIL D unless `coverage.files` in `ut_config.json` names another level for it [COV-2]. The targets
cannot be lowered and nothing is excluded: only measured coverage counts [COV-1, COV-3, COV-5].

| ASIL | Line, statement, branch, decision | MC/DC | Iterations (default) |
| --- | --- | --- | --- |
| D, C | 100 % | 100 % | 5 |
| B, A, QM | 100 % | above 95 % | 5 |

```mermaid
flowchart LR
    item["open item"] --> s1["brain scenarios<br/>(A1-PL1)"] --> s2["other values,<br/>stub return sequences"] --> s3["coverage-guided<br/>search"] --> s4["the next brain<br/>in the brain order"] --> s5["deterministic analysis:<br/>a proof is a diagnosis,<br/>never an exclusion"]
    s5 --> stop{"iteration limit<br/>or stagnation"}
    stop --> ev["evidence per item<br/>(COV-10)"] --> sug["Agent4 suggestions,<br/>hybrid reasoning over the<br/>facts and requirements"] --> trial["Agent3: trial copy<br/>builds, passes, reaches more"] --> faults["Agent3 fault_check:<br/>mutants on unchanged lines<br/>still detected (COV-14 .. COV-19)"] --> act["action needed<br/>report, PR, viewer (COV-11)"]
```

The run does not wait: a later run with requirement documents, or on a new commit after the developer changed the
source, continues from the stored build capture and the verified tests.

## 7a1. Learning between attempts [LA]

A repeated attempt with the same information gives the same answer, so every attempt after the first carries the reason the earlier one missed:

1. **Journal (LA-2).** After every iteration the orchestrator writes `journal/<file>.jsonl` and `journal/<file>.md` in the run folder. Only deviations are recorded: the test or scenario tried, the tool's message, the root cause and the next step.
2. **Root cause (LA-3).** Rules decide from the tools' results:
   - a compiler message means *test did not compile* or *test setup*;
   - a test that passed but did not reach the item means *wrong inputs*, *needs several calls* or *needs control of a dependency*;
   - a failing assertion means *wrong expectation* - unless the test did not reach the item it targets: then its
     inputs miss the path conditions, which is *wrong inputs*, and the next attempt gets the code of the real
     dependencies the function calls (A1-PL2);
   - a proof means *unreachable*.
3. **The next attempt (LA-4).** Agent1's plan request and Agent2's test request carry a short excerpt of the open entries of the function. A scenario or rewrite that repeats an earlier one unchanged is refused.
4. **Attempts (LA-5).**
   - At most 2 per item, counted only for tests that were built and measured.
   - Items blocked by one cause share their attempts.
   - A third attempt only by a brain of another model.
5. **Final status (LA-6).** Every item not covered at the stop is one of: covered, proven unreachable, needs a requirement, needs a source change, pipeline limitation, or undetermined. Each has a reason and an owner.
6. **Stop (LA-7).** The loop continues while an iteration covers something or finds a new root cause. It stops when every item has a final status, at the safety ceiling that grows with the file, or after two iterations with nothing new. A verified, complete result is never lost by a later round: if a later round breaks the build or covers less, the best result is delivered and the later round is reported as left out (ORC-33).
7. **Lessons (LA-8).** A cause that a verified test removed is stored in the knowledge base with the construct it occurred in. It goes to the next attempts, files and runs; a recurring one is proposed as a rule.
8. **Decision point (LA-9).** Only in an interactive run (from the menu, or `--interactive`). When items need a source change or a requirement, the user keeps the suggestions, gives requirement documents (the file is run again with them), or leaves the items open.
9. **Solved scenarios (A1-PL15).** Before the brain plans an item, Agent1 tries to solve it. This works when the item's
   decision, and every condition on the path to it, compares a parameter with a constant (a literal, a macro or an
   enumerator) or tests a boolean parameter. The path includes the if/else chain and the guard clauses
   (`if (c) return ...;`) before the decision. Agent1 picks the condition vector the item needs: the condition's
   outcome for a branch, an outcome for a decision, or both sides of the unique-cause pair for MC/DC. It then looks
   for parameter values among the constants and their neighbours and checks the values against every condition. A
   test is assembled from those values without a brain. Any other form of condition is left to the brain, which
   gets the vector as before. A solved test that ran and still left its item open is not planned again: the brain
   gets the item.
10. **A "no change" verdict (COV-12a).** The suggestion brain may answer `reachable_by_test` and name the input
    values. The report then says that no source change is needed for the item. This is a note, not a suggestion,
    and it does not change the item's status.

## 7a2. Brain profiles and new code without pipeline changes [BP, NC]

The pipeline uses what the answering brain can do. Qwen3 8B is the floor every requirement works with, not the size the
pipeline is built around; a stronger brain is allowed to write more, and everything any brain writes passes the same
checks (BP-4) and Agent3's measurement.

**Profiles (BP-1, BP-2).** `ut_config.json` → `profiles`: each brain has a profile with the answer kinds it may give and
its context limits. Default: the Claude models "full", Qwen3 8B "minimum" (exactly what it was given before).

| Kind | What the brain writes | minimum | full |
| --- | --- | --- | --- |
| values | inputs, argument values (A2-21ac) | yes | yes |
| scenario | a coverage scenario (A1-PL1) | yes | yes |
| call_sequence | earlier calls that build up the state | - | yes |
| setup | the object and constructor arguments (NC-1) | - | yes |
| stub | a fake the rules cannot write (NC-1) | - | yes |
| test | a whole test function (A2-21n) | yes | yes |
| suite_part | the tests of a function with shared set-up | - | yes |
| repair, decision | repairs of generated files, typed choices | yes | yes |

**The ladder of kinds (BP-3).**

```mermaid
flowchart LR
    a1["attempt: narrowest kind<br/>(scenario; the rules build the rest)"] --> m{"measured:<br/>covered?"}
    m -- yes --> done(["covered"])
    m -- "no: test_setup, needs_call_sequence,<br/>needs_dependency_control, pipeline_limitation" --> next{"a wider kind that<br/>holds the missing piece<br/>and an available brain<br/>allows?"}
    next -- yes --> a2["next attempt with that kind<br/>(call_sequence, setup, stub, test, suite_part)"]
    a2 --> m
    next -- no --> lim(["pipeline limitation:<br/>needs answer kind X (BP-9)"])
```

A step whose kind no available brain may give is never sent to a brain (BaseAgent's per-step gate). The attempts of
LA-5 count over the ladder; the third goes to another model with the widest profile.

**Building blocks (NC-1 .. NC-3).** Agent2 builds the object of a member function and the fakes from a stored recipe
first, then its rules, then the brain within its profile; Agent3's build decides. A block the compiler accepted is
stored as a recipe of its construct (class, method and constructor types as parameters) per repository; a recipe used
often is proposed for the application's reviewed library (`recipes/`).

**Checks that guard and checks that predict (NC-6 .. NC-9).** A guard (expected values from evidence, no way out, no
SUT change, no weaker test, no repeat) is never overruled. A prediction (an unknown name, unbalanced braces, a value's
form) never removes an item by itself: the brain sees the refusal in its re-ask and may correct it; otherwise the item
is built and run alone in a trial (Agent3 observe), and kept when it compiles, passes and reaches what it targets -
recorded as a false refusal of that check. A stub or stub variable the dependency plan does not have is a guard.

**Platform facts (NC-10 .. NC-13).** Measured by the setup, read through `ut_contracts.py` (`same_file`,
`program_outcome`); an end the facts do not describe is an unknown outcome: run again alone, then classified by the
brain, never a pass.

**Operators and overloads (NC-14).** An operator (`operator+`, `operator()`, `operator bool`) is a function like any
other; its tests get a safe name (`Money_operator_plus_assign`), and every overload of a name gets its own scenarios and
test names (the second one `_2`), found by its line. A set-up never declares the object of the building block again
(A2-21ae), and the SUT signature is that of the file as given, not of the copy adapted for the host (ORC-10).

**Generic by construction (NC-15, A2-21ag).** No rule, check, prompt or default is written for one file, project,
target or brain; the pilots and the corpus only test the rules. Functions are found in any common form (return type
on the line before, attributes, AUTOSAR-style FUNC(...) macros, #if/#else); a dependency is identified by name and
parameter types, so overloads get their own stubs, and variadic, unnamed and function-pointer parameters are kept;
a definition is never a static function of another file; headers come from "..." and project <...> includes, the
nearest file first; macros from -D flags and every included header; a register wait is recognised in any form of
its condition, its polarity found by evaluating it; register windows follow the SUT's own addresses; templates of a
header SUT (class and function) are instantiated with arguments the repository uses; C++ tests compile with
-fno-exceptions and -fno-rtti; brain tasks show forms, never pilot names; and the answer of any brain is read the
same way (any reasoning tag, fences and prose around the JSON).

**Measured right (A3-OBS6, A2-21ah, LA-7 c, ARCH-1c).**
- Agent3 checks its per-test measurement before Agent2 may drop a test as redundant: a passed test that calls the
  SUT but covers nothing when run alone, or an item of the whole run that no single run covers, means the per-test
  items are not used for that run (nothing is dropped; a trial is judged by the whole run) and a finding names the
  platform and how the single runs ended.
- Agent2 reads the harness API from the harness header itself (`uc.HARNESS_H`): every `UT_` macro is a known name,
  a macro called with the wrong number of arguments is refused with its parameters (the brain's re-ask corrects
  it), and a `void` call inside `UT_OBSERVE_`/`UT_EXPECT_` becomes a statement.
- The safety ceiling counts the pipeline's own time: agent calls that waited for a brain answer are not counted, as
  the brain is shared by every file of the run; the number of iterations still bounds the brain's work.
- A brain server behind a gateway (Cloudflare ends a silent request after 100 s): answers are streamed, every agent
  process of the computer shares the server's `max_concurrency`, and a gateway timeout (524, 504, 502 ...) is asked
  again on the same server after a pause before the next brain answers.

**Findings across runs (NC-5).** Pipeline limitations, false refusals and unknown outcomes go to
`ut_output/pipeline_findings.jsonl`, counted by signature; the report lists them first.

**Construct corpus and calibration (NC-4, BP-8).** `Test/constructs` holds one small example per construct with known
correct answers; `test` checks the answers against the checks, `test-full` runs the pipeline on every case, and
`unittest_generator.bat calibrate BRAIN` runs the corpus with one real brain and proposes its profile (never applied by
itself).

## 7b. The live viewer [VIEW]

Every run writes `events.jsonl` into its output folder and serves a page on 127.0.0.1 (a free port, printed at the
start; the browser opens by itself on a desktop). The brain is in the centre with the model that answered last; the
agents UT-Analyzer, UT-Generator, UT-Compilation, UT-Source Code Corrector and UT-KB are around it with their tools, and
UT-Orchestrator below them with the input of the run (SUT, repository, ASIL level) and its output (final state,
delivery). Every agent box shows, at all times, the input of its latest call (and who handed it over) and the output it
returned; clicking an agent lists every call of the run with tool, input, output and outcome [VIEW-8]. An agent that
was not called is greyed and says why, for example UT-Source Code Corrector when no source defect was found and every target was
reached [VIEW-9]. Every handoff and brain call is animated; the cards show line, statement, branch, decision and MC/DC coverage against the
file's ASIL targets and the tests passed; the end panel shows the final state and the action needed. At the end
`viewer.html` is written next to the events: it replays the whole run offline on any computer.

**Details [VIEW-13].** Until you click something, the Details panel shows an overview of the file on display. It
lists the file's state and the time so far, what the run is doing now, and the targets not yet met with their
values. It also shows the action needed once the file has ended, and the latest agent calls for this file with
their input and output. The panel updates with every event. Click an agent, the orchestrator or a metric to see its
own details. Click the heading **Details** to return to the overview.

**Test programs [VIEW-14].** A test program can run to its summary and report failed tests. The page then shows
"k of n tests failed" in grey, not "exit 1". When each test runs alone to measure what it covers (A3-OBS5), all
those runs appear as a single entry.

The small circles around an agent box are its tools (MCP contract tools), one circle per tool; a circle is filled in
the agent's colour while that tool runs, and hovering over it shows the tool's name.

**One page for a whole batch [VIEW-12a].** `pilot-all` (and `ut_pipeline.py batch --repo A --repo B ...`) runs every
folder in one command, so one page shows all of them: the chips read `folder: file`, the progress counts every file
of every folder, and the page reads "finished" only after the last folder. Each folder keeps its own output folder,
reports and summary.

**Several files at once [VIEW-12].** With `max_parallel_suts` above 1, every event names its SUT file. A row of chips
under the title lists the files with their state (running, verified, not verified). The cards, title and end panel
show one file: the one you clicked (click it again to follow the run), else the file shown so far while it runs,
else the running file that started first. A file that starts never clears another file's cards; the agent boxes show
the latest call of any file, led by its name, and the page reads "finished" only after every file has ended.

| Command | What it does |
| --- | --- |
| `unittest_generator.bat pilot-deps` | three C files, one page for all of them |
| `... run REPO SUT --no-view` | the run without the viewer |
| `unittest_generator.bat view ut_output\<run id>` | the page of an earlier run again, and its `viewer.html` |

Settings: the `viewer` section of `ut_config.json` (enabled, open_browser, host, port, replay, token_env, metrics). A
host other than 127.0.0.1 requires a bearer token from the environment variable named in `token_env` [VIEW-3]. The
events never contain source code, keys or tokens [VIEW-6].

## 8. What a run leaves behind

```mermaid
flowchart LR
    run(["ut_pipeline run"]) --> ws["working copy<br/>of your repository"]
    run --> outdir["ut_output/&lt;run id&gt;/"]
    outdir --> r1["result.json and report.md"]
    outdir --> r2["ledger.jsonl<br/>hash-chained handoffs"]
    outdir --> r3["generated/<br/>suite, tests, stubs"]
    outdir --> r4["source_fixes/<br/>patch files"]
    outdir --> r5["logs/<br/>one per agent"]
    outdir --> r6["events.jsonl and viewer.html<br/>(VIEW-4)"]
    outdir --> r7["coverage_suggestions/<br/>checked patches, never applied,<br/>with their fault-check records"]
    run --> r8["ut_output/pipeline_findings.jsonl<br/>limitations, false refusals,<br/>unknown outcomes across runs (NC-5)"]
    run --> repo["your repository"]
    repo --> fix{"verified<br/>source fix?"}
    fix -- no --> t1["tests, stubs and harness<br/>in the test folders"]
    fix -- yes --> t2["branch ut-source-fix/...<br/>the fix and its tests"]
```

For a local folder (pilots, development): your working branch and production files are never changed. When a source fix was verified, the tests expect the corrected code, so they are committed to the fix branch next to the fix instead of the working tree [ORC-13a]. Every delivered file starts with `/* ut-generated-sha256: ... */`; a later run replaces it only while that hash still matches, so neither your own files nor files you edited are overwritten [ORC-13, ORC-14, ORC-14a, ORC-15, A4-23c].
