---
name: "agent-live-viewer"
description: "Add or update the live pipeline viewer (brain in the centre, agents, tools, inputs and outputs) for any BaseAgent application or combined application, whenever one is created or changed."
---

# Live viewer for BaseAgent applications

Every application built on BaseAgent ships with the same live viewer. Examples: unit-test generation, static analysis, reverse detailed design, and any application that combines them. The viewer shows:
- the brain in the centre;
- the agents around it, with their tools and with the contracts they receive and return;
- live updates while a run is going, and a replay afterwards.

Follow this skill without being asked again whenever you:
- create an application;
- add or change an agent, a tool or a contract;
- combine applications.

## Order of work

1. **Requirements first.**
   - Add or update the VIEW section of the application's requirements file (IDs VIEW-1, VIEW-2, ...).
   - Then write the code, then the tests.
   - Keep the user's style: short numbered requirements, plain English.
2. **BaseAgent part.**
   - Check `baseagent.py` for `ViewHub`. If it is missing, build the BaseAgent part first (section "BaseAgent part").
   - BaseAgent stays one Python file (`Source/BaseAgent/baseagent.py`), one test file (`Test/test_baseagent.py`) and one batch file (`Source/BaseAgent/baseagent.bat`), with an entry in `Docs/BaseAgent_CHANGES.md`.
   - The viewer page is embedded in `baseagent.py` as a string, in plain HTML, SVG and JavaScript. Add no separate HTML or JS files, no JS library, and no Node or npm.
3. **Wiring.** Wire the application (section "Wiring an application").
4. **Tests.** Write the tests (section "Tests"), run the application's test suite, then run one pilot with `--view`.

## Names: every agent carries its application's prefix

Role names such as "Analyzer" repeat across applications, so every name the viewer shows starts with the application's prefix:

| Application | Prefix | Agent labels |
| --- | --- | --- |
| Unit-test generation | `UT` | UT-Analyzer, UT-Generator, UT-Compilation, UT-Source Code Corrector, UT-KB |
| Static code analysis | `SCA` | SCA-Analyzer, ... |
| Further applications | Decided by the user | `<PREFIX>-<Role>` |

- **Fixing the prefix.**
  - The prefix is short, upper case and unique, and it is fixed when the application is created.
  - If the user has not named one, ask before wiring.
  - The application id in code and events is that prefix (`application = "UT"`).
- **Where the label is used.**
  - The label is `<PREFIX>-<Role>`.
  - It appears everywhere a person reads an agent name: the ring, satellites, handoff lines, the feed, inputs and outputs panels, the final panel, and the requirements and reports that describe the viewer.
  - Example caption: "UT-Compilation built and ran 7 tests, all passed".
- **Technical names stay as they are.** Python files, MCP tool names and the event field `agent` (for example `agent_unittest_analyzer`) are unchanged; the label is for people.
- **Uniqueness.** A combined application must never show two agents with the same label, and a test checks this.

## What the viewer shows

- **Centre:** the brain (BaseAgent's brain router), labelled with the model that answered the last call: Claude Opus, Fable, Sonnet, Haiku, or Qwen. Never hard-code one model.
- **Ring:** one node per agent, showing its label ("UT-Analyzer") with a short role line below ("plans the tests"). In a combined application each application gets its own sector of the ring, titled with its prefix and name.
- **Satellites around each agent:**
  - its contract tools (the `ContractTool` names from `contract_catalog()`);
  - the external programs it runs (compiler, coverage tool, fuzzer, git, ...).

  A satellite lights up while it is in use.
- **Lines:**
  - agent to brain for brain calls;
  - agent to agent for handoffs.

  Handoffs pass through the orchestrator, drawn as a dashed ring. Agents never talk to each other directly, so never draw it that way.
- **Inputs and outputs:**
  - Clicking an agent or tool shows its last input and output contract: name, version, a plain-language summary and the key numbers.
  - In a local view it also links to the full content in the run folder.
- **Metrics row:** the metrics the application declares (section "Metrics: declared by each application"), plus brain calls and tokens, which every application shows. In a combined application each metric carries its prefix ("UT MC/DC coverage", "SCA open violations").
- **Feed:** the last events in plain language, with the time.
- **Final panel:** the result, the time taken and what was delivered.

## Metrics: declared by each application

This skill never lists an application's metrics. Each application declares its own, because they differ completely: coverage for unit tests, violations for static analysis, and design items for reverse detailed design. The viewer only knows a few metric kinds and draws whatever the application declares.

### Where the declaration lives

- **Requirements:** the application's requirements file, section VIEW (for example "VIEW-5. The viewer shall show these metrics: ...").
- **Configuration:** the `viewer.metrics` list in the application's config.
- **Single source:** the config list is the one place the code reads. Requirements and config must name the same metrics, and a test checks this.
- **When none are given:** if the user has not said which metrics a new application shows, propose a list derived from its requirements and ask before wiring. Never copy another application's metrics.

### One declaration per metric

```
{"id": "open_violations", "label": "Open violations", "kind": "count", "unit": "violations",
 "goal": 0, "better": "lower", "by": "severity", "per": "file", "note": "after suppressions and fixes"}
```

- **`id`:** stable identifier, lower case; it is used in `metric` events.
- **`label`:** shown to people, sentence case. The prefix is added automatically in a combined application.
- **`kind`:** one of the following.
  - `percent`: a gauge from 0 to 100, for example coverage.
  - `count`: a number with an optional target, for example open violations.
  - `fraction`: "x of y", for example tests passed or violations resolved.
  - `breakdown`: one bar split into categories, for example violations by severity, or resolved, suppressed and open.
  - `table`: one row per item with the metric's columns, for example per file or per function. It is shown when the metric is clicked.
- **`goal`** (optional): the target value. Reaching it turns the metric green, otherwise it stays neutral. Red is used only when the declaration says the value is a failure.
- **`better`:** `higher` or `lower`, so that progress arrows point the right way.
- **`by`** (optional): the category for a breakdown, with its categories listed in `categories` in their display order (for example `["critical", "major", "minor", "info"]`).
- **`per`** (optional): the item for a drill-down table (`file`, `function`, `module`, ...).
- **`note`** (optional): one line explaining how the number is counted, shown on hover. Use it where a value could be misread, for example "57 % after proving the rest unreachable".

### Emitting values

- **Who emits:** the orchestrator emits `metric` events with `{"id", "value", "total", "by": {...}, "per": [{"item", ...}]}` whenever the value changes, never on a timer.
- **Final values:** the values in the final panel equal those in the application's result file, and a test checks this.
- **Undeclared metrics:** a metric that is emitted but not declared is ignored by the viewer and logged, so the declaration stays the single source.

### Examples of declarations (for orientation only, not to be copied)

- **UT:** `lc`, `sc`, `bc`, `dc`, `mcdc` (`percent`, goal 100, `per: function`); `tests_passed` (`fraction`).
- **SCA:**
  - `violations` (`count`, `by: severity`, `per: file`);
  - `resolution` (`breakdown` into resolved, suppressed and open);
  - `open_violations` (`count`, goal 0, `better: lower`).
- **Reverse detailed design:** to be declared by the user when that application is created.

## Events: the only interface between a pipeline and the viewer

One JSON object per line, appended to `<run folder>/events.jsonl` and streamed live:

```
{"seq": 12, "time": "...", "app": "UT", "kind": "handoff", "agent": "agent_unittest_analyzer", "label": "UT-Analyzer",
 "to": "agent_unittest_generator", "to_label": "UT-Generator", "tool": "plan", "contract": "UTPlan",
 "summary": "UT-Analyzer: test plan with 5 scenarios for 3 functions", "data": {"scenarios": 5}}
```

- **Kinds:**
  - `run_start`, `stage`, `run_end`;
  - `tool_start`, `tool_end`;
  - `handoff`;
  - `brain_call`, `brain_answer`;
  - `program`, for an external program run;
  - `metric`.
- **Who emits what:**
  - BaseAgent: `tool_start` and `tool_end` (from `AgentCore.handle`), and `brain_call` and `brain_answer` (from the brain router, with step, model, window, tokens and outcome).
  - The agent: `program`, via `self.emit_program(name, outcome, seconds)`.
  - The orchestrator: `run_start` (with the metric declarations), `stage`, `handoff`, `metric` and `run_end`.
  - Every event that names an agent carries both `agent` (technical name) and `label` (`<PREFIX>-<Role>`).
- **Summaries.** `summary` is plain language of at most 120 characters, built by the agent's `view_summary(contract)`. The default is the agent's label, the contract name and the counts of its list fields; agents override it for their main contracts.
- **What never goes into an event:**
  - credentials, git tokens or API keys;
  - source code or generated code, which stays in the run folder and is reached by a link from the local view.

## BaseAgent part (generic, built once)

- **Event store.**
  - `ViewEvent` (pydantic) and `ViewHub(run_dir)` with `emit(...)`. The hub writes `events.jsonl`, numbers the events and keeps the subscribers.
  - Agents started as subprocesses find the events file through the environment variable `BASEAGENT_EVENTS`, set by the orchestrator, and append to it. The hub tails the file, so stdio, HTTP and in-process agents all work the same way.
- **Serving.**
  - `serve_viewer(hub, host="127.0.0.1", port=0, open_browser=True, token=None) -> url` serves the embedded page and `/events` (Server-Sent Events) with the already-installed uvicorn and starlette.
  - It opens the browser only when a desktop is present.
- **Replay.** `write_replay(run_dir) -> path` writes `viewer.html` with the events embedded. It opens offline on any computer, with no external URL inside.
- **Metric widgets.**
  - The embedded page draws the five metric kinds (`percent`, `count`, `fraction`, `breakdown`, `table`) from the declarations it receives in the `run_start` event.
  - It contains no application-specific metric code.
  - `MetricDeclaration` (pydantic) validates a declaration when the application starts.
- **Agent fields.**
  - `AgentCore.application`: the application prefix, for example "UT".
  - `AgentCore.view_role`: the plain role, for example "Analyzer".
  - `AgentCore.view_label`: read-only, computed as `f"{application}-{view_role}"`.
  - `AgentCore.view_summary(model) -> str`.
  - `AgentCore.emit_program(...)`.

## Wiring an application

- **Each agent** sets `application` (the prefix), `view_role`, a one-line role description, and `view_summary` for its main contracts.
- **The orchestrator:**
  - creates a `ViewHub` in the run folder;
  - sets `BASEAGENT_EVENTS` for the agents it starts;
  - emits handoffs in its single `call()` function, so there is one place for every handoff;
  - emits stages and metrics;
  - writes the replay file at the end and copies it into the delivered reports.
- **Configuration** (`viewer` section of the application's config):
  - `enabled` (true);
  - `open_browser` (true);
  - `host` ("127.0.0.1");
  - `port` (0, meaning a free port);
  - `replay` (true);
  - `metrics` (the application's metric declarations; see "Metrics: declared by each application");
  - `token_env` (empty).
- **Command line:** `--view` and `--no-view` on `run`, and `view <run folder>` to replay a finished run.
- **Where it runs:**
  - **Computer with a desktop:** the browser opens by itself.
  - **Server, CI or client build machine without a desktop:** no browser. Print the URL and keep the replay file.
  - **Watching from another computer:**
    - by default through an SSH tunnel (`ssh -L 8790:127.0.0.1:8790 user@host`);
    - or, only when configured, with `host` set to an address and a bearer token read from `token_env`, required for the page and `/events`, behind TLS.
  - Never listen on all interfaces by default.
- **Combined application:**
  - one orchestrator and one hub;
  - each application keeps its prefix;
  - the viewer draws one sector per application around the shared brain;
  - the metrics row shows the declared metrics of every included application, grouped by prefix.

## Design rules (presentable to a CEO)

- **Colour.**
  - Light canvas, one pastel colour family per agent: the lightest shade as fill, a mid shade as outline and the darkest shade as text.
  - Brain: lavender (purple family). Knowledge and support agents: grey. Red only for real failures.
  - In a combined application the agents of one application share related colour families, so each sector reads as one group.
  - In dark mode the same families are used with a dark fill and light text.
- **Restraint.** Thin lines (0.5 px), generous space, one typeface, sentence case for captions. Labels keep their prefix as written ("UT-Analyzer"). No glow, neon, gradients, shadows, particles or 3D.
- **Motion only on real events.**
  - A dot travels a line for a handoff or a brain call.
  - A node's outline thickens briefly while a tool runs.
  - Respect `prefers-reduced-motion`.
- **Plain-language captions**, for example "UT-Compilation built and ran 7 tests, all passed", never contract dumps.
- **Presentation mode:** a larger canvas, the product name and the repository at the top, a replay speed control, and a final results panel.

## Tests (every application)

- An in-process run with a scripted brain writes `events.jsonl` from `run_start` to `run_end`.
- Every handoff in the ledger has a matching `handoff` event.
- Every brain call has a `brain_call` and `brain_answer` pair naming the model.
- Every event that names an agent carries a label starting with the application's prefix and a hyphen.
- No two agents share a label, including across the applications of a combined application.
- No event contains the API key, a git token or any line of the source under test (check against the source file).
- The replay file contains every event and no `http://` or `https://` URL to load.
- With the viewer disabled, no server starts and no events file is written.
- The metrics in `viewer.metrics` are exactly those named in the VIEW requirements, each with a valid `kind`.
- Every `metric` event refers to a declared metric, and the final values match the application's result file.
- A combined application's events carry the right prefixes, and the viewer groups them into one sector each.