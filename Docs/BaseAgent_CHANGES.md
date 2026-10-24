# BaseAgent changes

## 2026-10-24 (late night): answers of any brain are read the same way

**Why.** A brain without JSON-schema enforcement may write "Here is the JSON:" before a fenced block, use another
reasoning tag than `<think>` (`<thinking>`, `<reasoning>`), or add a sentence after the JSON. These answers were
refused as `MODEL_OUTPUT_INVALID` and counted as a brain failure, although they carried a valid answer.

* **`parse_generated`** removes any leading reasoning blocks (`strip_reasoning`, tags think, thinking, reasoning,
  reflection, analysis, scratchpad) and a code fence; when the text is still not JSON, the first complete JSON object
  or array in it is the answer (`first_json`, strings with braces handled). The schema and every agent check still
  decide whether the answer is used.
* **`salvage_json`** (an answer cut at its token limit) does the same before it closes the last complete list item.
* **Tests.** `test_parse_generated` covers the new forms.

## 2026-10-24 (night): brains per step - what a brain may answer, and a step's own brain order

For the unit-test generator's brain profiles (BP-1 .. BP-10). BaseAgent stays application-agnostic: it does not know
answer kinds or profiles, only which brains may answer a step.

* **`AgentCore.brains_for_step`** (optional, set by the application): `brains_for_step(step, available brain names)`
  returns the names allowed for that step, or `None` for no restriction. `generate()` and `decide()` apply it before
  any brain is asked. A step no available brain may answer fails at once with **`NO_BRAIN_FOR_STEP`** (in
  `NO_BRAIN_CODES`, so `resolve()` records the case as "needs a brain"); no brain is called.
* **`AgentCore.step_order`** (optional): `step_order(step)` returns a step's own brain order, or `None`. The router
  tries those brains first, the others after them in the configured order (`prefer=` of `BrainRouter.generate` and
  `BrainRouter.decide`).
* Without either hook every agent behaves exactly as before.

## 2026-10-24: progress at a glance in the live viewer

**Why.** A run over many files takes an hour or more, and the viewer showed no overall progress (VIEW-11).

* **New viewer event `progress`** (`VIEW_KINDS`). The application sends a short text, such as "35 % - file 8 of 23 - iteration 2 - about 30-60 min left". The page shows it in a pill next to the run status.
* Nothing else in BaseAgent changed; the unit-test pipeline computes the text.

## 2026-10-23 (night): decisions and refused answers in the answer log

**Why.** In the first pilot run with the remote Qwen, a failing test was classified by `decide()`, and Agent4's three requests for source-change candidates ended as `MODEL_OUTPUT_INVALID`. None of these answers was in `brain_answers.jsonl`, so the log could not show why the run stopped.

* **`AgentCore.decide()` logs its answer (ARCH-20)** as kind `decision`, with the brain, the model and the validated answers.
* **`AgentCore.generate()` logs an answer that no check let through** as kind `refused`, with the brain, the task, the fault code and its reason, and then raises as before.
* **Tests.** `AnswerHandlingTests.test_decisions_and_answers_no_check_let_through_are_logged_too`.

## 2026-10-23 (evening): form is forgiven, content is not

**Why.** In the first run with the remote Qwen, Agent1 refused 6 of 7 answers. The scenarios were right in substance (for `select_mode`: `1, 0, 0` reaches `return MODE_ECO`), but every value had a stray `:` in front (`":1"`), or the gap id was an evidence id or a copied line of the request. The re-ask ran with the same seed and temperature 0, so Qwen gave the same answer again. Any brain can make such slips; the checks are the same for every brain.

* **Re-ask with other sampling (ARCH-20).** `AgentCore.generate(..., vary=True)` and `BrainRouter.generate(..., vary=True)`: an agent that refused the last answer asks again with other sampling (seed + 1, temperature at least 0.4 for Qwen), so a deterministic brain does not repeat itself. Engines without the parameter are called as before.
* **Answer log (ARCH-20).** `AgentCore.log_answer(step, kind, **data)` appends to the file named in `BASEAGENT_ANSWER_LOG`:
  * every structured answer;
  * what the agent did with it: `fixed`, with the value before and after, or `refused`, with the reasons.

  It never raises, and keys and tokens are never part of it. The unit-test pipeline sets it to `<run>/brain_answers.jsonl`.
* **The agents declare the format fixes.** BaseAgent only provides the re-ask and the log; what counts as harmless noise is domain knowledge. The fixed value always goes through the agent's unchanged strict check.
* **Tests.** `AnswerHandlingTests` checks that the re-ask is sampled differently and that the answer log is written. The rule "the next brain is another model, not the same model on another server" is tested in the unit-test generator's tests (`test_the_next_brain_is_another_model_not_the_same_model_on_another_server`). 202 BaseAgent tests in total, with the 3 known stand-in errors.

## 2026-10-23: Qwen on a remote server before the local Ollama

**Why.** Qwen3 8B now also runs on a server with an OpenAI-compatible API. BaseAgent should use it whenever no Claude model is available, and fall back to Ollama on this PC only when that server cannot be reached.

* **New brain `qwen_remote` (`QwenRemoteEngine`).** The default order is opus, fable, sonnet, haiku, **qwen_remote**, qwen. Everything above the transport is the local Qwen's: prompts, the `_analysis` lead, re-asks, repetition handling, token budgets. The request goes to `{base_url}/chat/completions`.
* **Settings `qwen_remote`.**
  * `base_url` (default `https://llm.openroute-ai.com/v1`); https only, http only on this computer, no credentials in the URL.
  * `model` (default `qwen3-8b`).
  * `api_key_env` (default `QWEN_REMOTE_API_KEY`). The key is read from that environment variable at every request and goes only into the `Authorization` header. It is never in a configuration file, log, result or error message.
  * `windows` (default 16,384; the server's `max_model_len` wins when `/models` reports one).
  * `max_concurrency` (default 1).
  * `json_schema`, `no_think_tag`, and the usual answer limits and timeouts.
* **Thinking off.** Every request carries `chat_template_kwargs: {"enable_thinking": false}`, and `/no_think` at the end of the last user message (switch it off with `no_think_tag: false`). With `think: true` both are left out.
* **Output format.** JSON schema (`response_format` `json_schema`). A server that refuses it gets `json_object`, then no format. The choice is kept for the following calls. BaseAgent validates every answer either way.
* **Fallback.**
  * No key, or the key refused (401/403): `BRAIN_NO_ACCESS`, skipped for the run.
  * Unreachable, 429 or 5xx, or too slow: `MODEL_UNAVAILABLE` / `MODEL_TIMEOUT`. The router skips it for the cooldown, and Qwen through Ollama answers the same call.
  * Model not served: `MODEL_NOT_FOUND`.
  * The answer cut at its limit: as before (re-ask with more room, then salvage).
* **Proxy.** The remote server is reached through the system proxy settings; the local Ollama still never is.
* **Same model, one brain (ARCH-19).** Each brain has a `family`: the model behind it (Claude: its model id; Qwen: `qwen.family` / `qwen_remote.family`, default `qwen3-8b`). An answer that stays unusable is passed on only to a brain of another family. The Qwen in Ollama is the same model as the remote one, only slower, so it answers only when the remote server is not available. `BrainRouter.family_of(name)` gives the family. Found in the first run on the user's PC: one plan answer of the remote Qwen failed Agent1's check, and Agent1 asked Ollama on the laptop's CPU twice - 333 s and 303 s for the same kind of answer.
* **User-Agent.** Requests carry `User-Agent: BaseAgent/1.0` (`qwen_remote.user_agent`). On the user's PC, the server refused Python's default `Python-urllib/3.12` with HTTP 403 while the same key worked with curl.
* **Unchanged.** `qwen` (Ollama on the loopback address), the Claude models, and configurations that name their own `brain_order`. A configuration that lists `brain_order` without `qwen_remote` does not use the remote server.
* **baseagent.bat.** Shows whether `QWEN_REMOTE_API_KEY` is set; the brain list names `qwen_remote`.
* **Tests.**
  * `QwenRemoteEngineTests` (6) run against a fake OpenAI-compatible server. They cover:
    * the request shape, the key in the header and thinking off;
    * no key and a refused key;
    * the format fallback;
    * a cut answer and an unknown model;
    * the local Qwen answering only when the remote one is not available (overloaded or unreachable).
  * `QwenRemoteLiveTests` asks the real server one question, only when `QWEN_REMOTE_API_KEY` is set. The key is removed from the environment of every other test.
  * 199 tests in total.

## 2026-10-19 (evening): rule first, brain when a rule fails, the tool decides

**Why.** On 15 files of five open-source code bases, 10 files ended with "cannot": a rule met a pattern it was not written for (include order, `T * const`, a namespace, a target keyword), and no brain was ever asked. Every agent of every application should hand such a case to the brain and let its own tool judge the answer.

* **`AgentCore.resolve(failure, task, context, output_model, verify, ...)` (ARCH-21, ARCH-22).** `failure` is a `RuleFailure(rule, failure, subject, message)`. The order is:
  1. a stored resolution, if there is one;
  2. otherwise the brain, with the rule, why it failed, the facts and the tool's message;
  3. the agent's `check`;
  4. `verify(answer)`, the tool that rejected the rule's result (sync or async): "" accepts, otherwise its message goes back to the brain.

  This repeats up to `attempts` times. The method returns the accepted answer or `None`; it never raises for a missing brain.
* **`meta.escalations`.** Every case is recorded as an `Escalation`: agent, step, rule, failure, source (memory or brain, with its name), the tool's message per rejected attempt, and the outcome (`resolved`, `rejected` or `needs_brain`). It is emitted to the live page as an `escalation` event.
* **`ResolutionMemory(path, project)` (ARCH-23).** SQLite; one row per project, agent, rule and failure signature. The signature is the tool's message with paths, numbers and generated names removed, so the same failure in another run matches. Stored answers are checked by the tool like new ones; a rejected one is deleted. Every error is swallowed. `remember=False` lets an agent store the answer itself, once a later tool accepted it.
* **Tests.** `ResolveTests` (4):
  * the tool's message goes back to the brain;
  * a stored resolution is used without the brain, and a rejected one is forgotten; projects do not share resolutions;
  * without a brain the case is recorded as needing one;
  * after the attempts, the case is rejected and recorded in the contract's metadata.

  192 tests in total.

## 2026-10-16 (night): unusable brain answers, handled once for every agent

**Why.** In the first C++ run with Qwen, one A2.tests call ended with Ollama's HTTP 500 "token repeat limit reached". BaseAgent reported every HTTP 500 as `MODEL_UNAVAILABLE`, so a healthy Qwen was put on a 30 s cooldown and the function got no LLM tests. Each agent also repeated the same item-by-item filtering code.

* **Repetition (ARCH-19).** An answer that repeats itself until Ollama stops it is `MODEL_OUTPUT_INVALID`, not `MODEL_UNAVAILABLE`: no cooldown. It is asked once more with other sampling, still seeded so a run can be repeated: seed + 1, temperature at least 0.4, `repeat_penalty` 1.2. Other HTTP 500s stay `MODEL_UNAVAILABLE`.
* **Cut answers (ARCH-19).** After the existing retry with twice the room, `salvage_json()` keeps the complete items of the answer's top-level list (`{"tests": [a, b, {"na` gives `{"tests": [a, b]}`) and drops the item cut in the middle. The result goes through the same schema and agent checks; when nothing passes, the original "cut" fault is raised. Qwen and the Claude models both. The token plan and the live page show `salvaged`.
* **Next brain for unusable work output (ARCH-19).** For `generate` (plans, tests, flags, patches) an answer that stays invalid after the brain's own re-ask is passed to the next allowed brain, without a cooldown. Decisions (`decide`) are never sent to another brain because of an invalid answer.
* **`keep_valid(field, rule, ...)` (ARCH-20).** A generic `check` for `generate()`: the agent gives the rule for one item; unusable items are dropped with their reasons; the answer is invalid only when none is left. Agent4 uses it for fix candidates and suggestions.
* **Tests.** `BrainAnswerRobustnessTests` (7): salvage only of complete items, repetition asked again with other sampling and no cooldown, repeating twice is an output failure, a real outage is still `MODEL_UNAVAILABLE`, a cut answer uses its complete items (Qwen and Claude), a cut answer that fails the check keeps the cut fault, `keep_valid`. `test_router_generate_falls_back` now expects the next brain for invalid work output.


## 2026-10-16: brain calls at the same time

**Why.** A production file has 100 or more functions; asking the brain about one function at a time makes a run take hours with a Claude model, although the calls for different functions do not depend on each other.

* **`AgentCore.in_parallel(jobs)`** runs independent jobs, each of which may call the brain, at most `parallel_slots()` at a time, and returns the results **in the order of the jobs**, whatever order they finish in. A job's exception is returned in its place.
* **`AgentCore.parallel_slots()`** is the limit of the first brain that would answer: `anthropic.parallel_calls` (default 4) for the Claude models, `qwen.max_concurrency` (default 1) for Qwen through Ollama, 1 without a brain. With Qwen nothing changes: the calls wait for each other as before.
* **`AgentCore.brain_of_call()`** is the brain of the latest call of the current task; with calls at the same time `last_brain` may belong to another call.
* **Rate limits.** A Claude model that answers 429 (rate limited) or 529 (overloaded) is asked again after a wait (at least the server's retry-after; 5, 10, 20 ... s) up to `anthropic.rate_limit_wait` (default 300 s), then the call fails as `BRAIN_RATE_LIMITED`. It is never replaced by the next brain for that reason, so one file does not get tests of uneven quality.
* **Tests.** `ParallelCallTests`: results in the order of the jobs and at most the brain's limit at once; Qwen one call at a time; a rate-limited model asked again and never replaced.


## 2026-10-16: live viewer on Windows

**Why.** On the first Windows run the live page showed "connection closed" while the run went on. The page never returned to "live" after the browser lost its event stream, a reconnect would have started the stream from the first event again, and the stream counted its position in characters of a file that Windows had written with CRLF line ends.

* **`emit_event`** writes LF line ends on every operating system.
* **`/events`** reads the events file by bytes and sends each event with an `id` (its byte position). A reconnecting browser sends `Last-Event-ID`, and the stream continues after that event: nothing is shown twice or lost.
* **The page** shows "live", "reconnecting" or "finished", and "connection closed" only when the browser has given up. A run of several SUT files has one page: after one file's end it shows "finished" and keeps listening; the next file's start shows "live" again. (A first version closed the stream at the first file's end, so the page stopped after the first of several files.)
* **Tests.** `ViewerTests.test_live_server` covers the event ids and a reconnect over a CRLF file; `test_live_page_stays_live_across_several_files` a second file after the first one's end.

## 2026-10-14: repository layout

**Why.** BaseAgent now lives in the UnitTest git repository together with the applications built on it, so that a fresh clone works on any computer.

* **Where the files are.** `Source/BaseAgent/baseagent.py` and `Source/BaseAgent/baseagent.bat`; the tests in `Test/test_baseagent.py`; this file in `Docs/BaseAgent_CHANGES.md`. BaseAgent is still one Python file, one test file and one batch file.
* **`baseagent.bat test`** runs the tests from the `Test` folder; `test_results.txt` is still written next to `baseagent.bat`.
* **`test_baseagent.py`** finds `baseagent.py` in `Source/BaseAgent` by itself, so it also runs from any folder.
* **Not in the repository.** `.venv`, `test_results.txt` and the local service state in `demo` (data, principals and `client.token`). `baseagent.bat start` and `check` create `demo` again on first use (`init-demo`, `renew-token`).
* No change in `baseagent.py`.

## 2026-10-13: live viewer for every application

**Why.** Every pipeline built on BaseAgent can be watched while it works and replayed afterwards, with the same page for every application and for combined applications. Each application declares its own agents, labels and metrics; the viewer itself knows none of them.

**Changes in `baseagent.py`:**

* **Events.** `emit_event(kind, **fields)` appends one JSON line to the file named by `BASEAGENT_EVENTS`; `read_events(path)` reads them back.
  * Kinds: run_start, stage, run_end, tool_start, tool_end, handoff, brain_call, brain_answer, program, metric.
  * Values of environment variables whose names contain KEY, TOKEN, SECRET, PASSWORD or CREDENTIAL are masked in every event. Events carry counts, states and summaries, never source code.
* **`ViewHub(run_dir, app=, metrics=)`.** Holds `events.jsonl` of one run. `activate()` points `BASEAGENT_EVENTS` at it, so agents started afterwards (stdio subprocesses, in-process servers) write into the same file.
* **`MetricDeclaration`.** The metrics an application shows: id, label, kind (percent, count, fraction, breakdown, table), unit, goal, better, categories, per, note.
* **`serve_viewer(hub, host="127.0.0.1", port=0, open_browser=True, token=None, title=)`.** Serves the page, `/events` (server-sent events) and `/events.json` with uvicorn in a background thread; returns a `ViewerServer` with `url` and `stop()`.
  * A host other than 127.0.0.1 is refused without a bearer token. The printed address never contains the token.
  * The browser opens by itself only where there is a desktop (`has_desktop()`).
* **`write_replay(run_dir, title)`** writes `viewer.html` with the events embedded: the whole run, replayable offline on any computer. `view_page(events, title)` returns the same page as text.
* **The page.**
  * The brain is in the centre with the model that answered last. The agents sit around it with their tools.
  * Every agent box shows, at all times, the input of its latest call (and who handed it over) and the output it returned. Clicking an agent lists every call of the run, newest first: tool, input, output and outcome, plus the programs it ran.
  * The orchestrator is a node of its own when the application sends `orchestrator` in run_start (label, description, the input of the run). Its output (final state, delivery) appears at run_end.
  * An agent without a call in a run is greyed out and shows the reason, taken from `idle` in run_end.
  * Also on the page: animated handoffs (also from the orchestrator) and brain calls; metric cards against their goals; the latest events; the final panel; replay controls and a presentation mode. Pastel colours, light and dark.
* **`AgentCore`.**
  * `application`, `view_role`, `view_description` and `view_support` give the label (for example UT-Analyzer) and the description shown.
  * `view_summary(model)` turns a contract into one plain sentence; `emit()` and `emit_program()` add events.
  * Every tool call emits tool_start and tool_end; `generate` and `decide` emit brain_call and brain_answer with model, tokens and outcome.
  * `generate(..., allowed=)` narrows the brains for one call (used to ask the next brain).
* **`AgentClient.describe()`** returns an agent's catalogue (label, description, tools) for the page.

**Tests (`test_baseagent.py`).** `ViewerTests`: events only while a viewer is active and never secrets, tool and brain events of an agent, metric declarations, the self-contained replay file, the live server (page, `/events.json`, no non-loopback host without a token), and the rendered page (input and output on every box, the orchestrator node, an agent not called, the call list), when Playwright is installed.

## 2026-10-12 (later): any Claude model the API key can access, Qwen3 8B as the fallback

**Why.** The applications built on BaseAgent must not be designed around one brain or one installation. Whichever Claude models the configured API key can use are used; Qwen3 8B through Ollama answers only when none is available. Nothing in the code or the defaults depends on where the application runs.

**Changes in `baseagent.py`:**

* **Four Claude brains.** `opus` (claude-opus-5-5), `fable` (claude-fable-5-1), `sonnet` (claude-sonnet-5-5) and `haiku` (claude-haiku-5-5) are each a brain of their own.
  * The default `engine.brain_order` is opus → fable → sonnet → haiku → qwen. The order can be changed in the configuration.
  * All four models take the same request: effort (`high`), adaptive thinking, and JSON-schema output. The models' own default efforts differ, so effort is always sent.
* **`ClaudeEngine(settings, brain)`** replaces `OpusEngine`. `OpusEngine` stays as an alias, and `ClaudeEngine(settings)` is Opus.
* **Access is checked when the brains start.** Each model is checked with `models.retrieve`, which spends no tokens.
  * No key, an invalid key, or a model the key may not use gives `BRAIN_NO_ACCESS`. The router skips that brain for the whole run, so nothing is ever sent to it.
  * A brain that is only unreachable at the moment is skipped for `engine.brain_cooldown`, as before.
  * Losing access during a run (403 or 404) is treated the same way.
* **Settings.**
  * `anthropic.models` maps each brain name to its model id, for example to pin a dated model version.
  * `anthropic.context_window` is now 1,000,000 tokens, the window of all four models.
  * The old `anthropic.model` still loads, as the Opus id.
* **Use cases and agents.** `allowed_brains` and `engine.mode` accept the four names and `qwen`. In `allowed_brains`, `"claude"` stands for all four Claude models.
  * The default allows every brain. Stored results of older profiles therefore replay as `STALE_RESULT`; send a new request id.
  * `AgentCore.allowed_brains` is all brains.
* **Command line.** `serve --brain` and `probe --brain` accept the new names. `probe` lists every brain with its status: `available`, `BRAIN_NO_ACCESS` or the fault.
* **Neutral wording.** Comments and messages name the model that answered rather than "Opus", and no longer speak of local or customer installations.

**`baseagent.bat` and `demo\`:**

* The script's texts describe the brains by access.
* `check` shows every brain's status.
* The demo configuration and profile list all five brains.

**Changes in `test_baseagent.py`:**

* `ClaudeAccessTests` covers:
  * the first accessible model answering, and nothing being sent to the others;
  * no access staying no access after the cooldown;
  * no Claude model at all, so Qwen answers;
  * the same request for all four models;
  * access lost during a run;
  * the settings, including old configs;
  * a missing key.
* The router, mode and service tests were updated for the new default order.

171 tests in total.

## 2026-10-12: adaptive token budget and knowledge packs

**Why.** Until now every brain call got the same fixed sizes (Qwen: a window of 8,192 tokens for `decide()`, 16,384 for `generate()`; answers of 1,024 or 4,096 tokens; Opus: 16,000). But the content of a call varies a lot: a small function with one gap needs little; a large source file with large dependency headers and many gaps needs much more. A fixed size wastes memory and time on the small calls, and it rejects or cuts the large ones. The knowledge packs (curated guidance for the brains) also had no loader yet, so no agent read them. Both now live in BaseAgent, so every application built on it gets them, not only the unit-test generator.

**Changes in `baseagent.py`:**

* **Prioritized context parts.** An agent can pass its context as a list of `ContextPart(name, text, priority, min_chars)` instead of one string.
  * Priority 0 is required. Higher numbers are given up first.
  * It states the expected answer as `OutputNeed(items, tokens_per_item, overhead)`.
* **Planning per call: `plan_context()`.** The router plans every call for each brain it tries, because their windows differ.
  * It estimates the input from the parts and adds the expected answer, then picks the smallest window that holds both.
  * If even the largest window is too small, parts are given up by priority, the highest number first and, within a priority, the largest first. A part is shortened as far as needed (not below its `min_chars`) and marked `[... shortened to fit the context window]`, or else left out.
  * Required parts are never cut. If they do not fit, the call is not made (`CONTEXT_BUDGET_EXCEEDED`) and the next brain, or the agent's own rules, take over.
* **Calibrated estimates: `TokenMeter`.** The router learns the characters per token of each brain from the token counts the brains report.
  * It uses the 20th percentile of what it has observed, so it rather overestimates the tokens.
  * Until three calls have been seen it uses `chars_per_token` (3.0).
* **Qwen.**
  * New settings: `adaptive` (true), `windows` ([4096, 8192, 12288, 16384]), `max_num_predict` (8192) and `sticky_window` (true).
  * The answer limit is the expected answer plus 30 %, at least 256 tokens and at most `max_num_predict`; with thinking it is at least 4,096.
  * A re-ask whose messages no longer fit moves to the next larger window.
  * An answer cut at an estimated limit is asked again once with twice the limit.
  * Ollama reloads the model whenever the window changes, which takes seconds. With `sticky_window`, a window that is already loaded and large enough is therefore kept.
  * The largest window is limited by the PC's memory: the KV cache of Qwen3 8B needs about 0.14 MB per token, so 16,384 tokens need about 2.3 GB on top of the model.
* **Opus.**
  * New settings: `adaptive` (true), `context_window` (200,000) and `thinking_reserve` (8,000).
  * With an expected answer, `max_tokens` is that answer plus 30 % plus the thinking reserve, never more than `max_tokens`. An answer cut at that limit is asked again once with `max_tokens`.
* **Compatibility.**
  * Without an expected answer (all existing callers), the configured answer sizes apply as before; only Qwen's window is now chosen from `windows`.
  * `adaptive: false` restores the fixed sizes completely.
  * Engines called directly, without the router, behave exactly as before.
  * Custom brains without a `plan` parameter keep working: the router passes `plan` only to methods that accept it.
* **Recorded: `TokenPlan`.** Each plan is recorded in the response's metadata as `ContractMeta.token_plans`. It holds the step, brain, window, estimated and reported tokens, characters per token, parts kept, shortened and left out, packs used, and the outcome.
* **No work, no call.** `AgentCore.generate()` and `decide()` refuse with `NO_WORK`, and spend no tokens, when the expected answer has 0 items.
* **Knowledge packs: `KnowledgePacks`.** Packs are Markdown files with a header (`name`, `version`, `used_by`, `summary`).
  * The application gives its pack folders, the steps it knows, and a project folder (for example `.ut/knowledge`). A project pack with the same name replaces the application's, and a new name is added.
  * A pack without a valid header, naming an unknown step, empty, unreadable, or pointing outside the project is not used. It is listed in `problems` and logged.
  * `AgentCore.packs` holds the loader. `generate(..., step=..., project=...)` and `decide(..., step=...)` add the step's packs after the facts, as parts of priority 2, marked as advisory guidance. The packs' names, versions and SHA-256 go into the token plan.

**Changes in `test_baseagent.py`:**

* `TokenBudgetTests`: window choice, giving up parts by priority, the required-part limit, the meter, Qwen windows and answer limits (sticky, non-sticky, `adaptive=false`), the re-ask of cut answers for Qwen and Opus, the hand-over to a brain with more room, calibration through the router, `NO_WORK`, and the plans in the metadata.
* `KnowledgePackTests`: loading and selection by step, unusable packs, project packs replacing and adding, refusing a pack outside the project, packs placed after the facts and recorded, and packs giving way to facts in a small window.

165 tests in total.

## 2026-10-09: agent framework for the unit-test generator (AgentCore, generate, contracts)

**Why.** The unit-test generator in `..\unittest_generator` needs five agents that are built on BaseAgent, talk only through MCP, and exchange checked contracts. Everything shared by those agents lives here, so no agent talks to Ollama or Anthropic directly.

**Changes in `baseagent.py` (additions; existing behaviour is unchanged):**

* **`generate()` for structured output.** `decide()` returns typed choices with probabilities. `generate(task, context, schema, validate)` returns a JSON object, such as a test plan, test code or a patch.
  * Implemented for Opus and Qwen and in the brain router, with the same fallback as `decide()`.
  * An invalid answer is asked again once with the validation error, then rejected.
  * New Qwen settings: `generate_num_ctx` (16384), `generate_num_predict` (4096), `generate_timeout` (900 s).
  * Helpers: `llm_schema()` (inlines `$ref`s and closes objects so Ollama can enforce them), `parse_generated()`, `generate_prompt()`.
* **Contracts.** `ContractRequest` and `ContractResponse` are the envelope of every call between agents: request id, contract name and version, SUT and test-suite signatures, status, data and metadata (brain, fallbacks, LLM calls, output hash). Also new: `ContractRejected` (a failed precondition), `contract_digest()`, `validation_summary()`, `model_validator_for()`.
* **`AgentCore`.** The base class of every agent:
  * Each tool is a `ContractTool(name, input model, output model, work, precondition, postcondition)`. Each call runs as a small LangGraph graph: precondition → work → postcondition.
  * Brain access goes through `self.decide()` and `self.generate()` only.
  * `AgentKnowledge` is a small local retrieval store (the RAG part).
  * Repeated request ids return the stored answer (idempotency).
  * `contract_catalog()` describes the tools.
* **MCP.** `create_agent_server(agent)` turns an agent into a FastMCP server, with optional bearer-token principals. `AgentClient` is its client over stdio, HTTP or in-process. `agent_main()` is the command line (`serve`).

**Changes in `test_baseagent.py`:**

* `GenerateTests`: the schema helpers, parsing, settings limits, and the re-ask on invalid output.
* `AgentCoreTests`: contract invariants, preconditions and postconditions, idempotency, and the MCP round trip.

147 tests in total.

## 2026-10-08 (evening): Qwen writes a short analysis before it answers

**Why.** In the first live run on the laptop, 129 of 130 tests passed. In the failing test, Qwen judged a safe parameterized query (`"... WHERE id = %s"`, with the value passed to `execute()`) as `sql_injection`, with probability 1.0. Its answer was identical to its answer for the vulnerable code. There were two causes:

* **No room to reason.** With thinking off, Ollama's JSON format made the probabilities Qwen's very first output tokens. An 8B model then judges by surface patterns: SQL plus a user variable means injection.
* **An ambiguous question.** The test's option text said "concatenating or *formatting* variables into database executions", which also describes a `%s` placeholder.

**Changes.**

* **`baseagent.py`: the `_analysis` field.** Qwen's JSON now starts with `_analysis`: at most three sentences comparing the evidence with each option, then the probabilities.
  * It is controlled by `qwen.analysis` (default `true`) and `qwen.analysis_max_chars` (default 600).
  * The analysis is untrusted model text. It goes only to the local DEBUG log and `QwenEngine.last_analysis`, and is never returned to callers.
  * Question ids cannot start with `_`, so the field never collides with a question.
  * Opus is unchanged.
* **`test_baseagent.py`:**
  * **Clearer question.** The live code question now states exactly what counts as SQL injection: a variable written into the SQL text. It also states what doesn't: placeholders with the values passed as parameters. The yes/no question asks the same thing, instead of "unescaped".
  * **New tests.** Two tests check that the analysis comes first and is never returned, and that it can be switched off.
  * **Analysis shown.** The live tests print Qwen's analysis next to each answer.
  * **Thinking mode.** `set BASEAGENT_QWEN_THINK=1` runs the live tests with Qwen3's thinking mode.
* **`baseagent.bat`:**
  * `baseagent.bat test QwenLive` runs only the live Qwen tests (about 6 minutes instead of 10).
  * The Qwen answers now appear in `test_results.txt` next to their test names.

**Cost.** The analysis adds roughly 10–30 s per answer on a laptop CPU.

**If the test still fails, it is still a quality result.** Try `set BASEAGENT_QWEN_THINK=1`. If that fails too, treat Qwen's security judgements as needing review, and let Opus decide them once you have an API key.

## 2026-10-08: Qwen3 8B replaces Laya and Nimble

Laya and Nimble only answer typed questions with a fixed set of options, and could not produce the plans your analyzer agents need, so both are removed. The local brain is now **Qwen3 8B** on your Ollama server, a general model that also follows instructions. Opus 5.5 stays the default; Qwen answers when Opus is unavailable (today: no API key yet).

### What changed in `baseagent.py`

| Area | Before | Now |
|---|---|---|
| Brains | `opus` → `nimble` → `laya` | `opus` → `qwen` (and `demo`, an explicit simulation) |
| Local model | Nimble via Ollama `/v1/systemone`; Laya in a PyTorch worker process | `QwenEngine`: Qwen3 8B via Ollama `/api/chat` |
| Demo mode | Ran inside the Laya worker process | `DemoEngine`, in-process, no model |
| Removed code | | `NimbleSettings`, `NimbleEngine`, `ProcessEngine`, `laya_preflight`, the Laya worker, Laya-only answer fields (`answer_confidence`, `action`, `low_confidence`) |
| Shared answer logic | Inside `OpusEngine` | `answer_schema`, `brain_prompt`, `answers_from_probabilities`: one contract for Opus, Qwen and demo |
| Request deadline | `request_timeout` 300 s, client 310 s | 600 s and 610 s (Qwen on a laptop CPU is slow; the graph can ask twice) |
| New commands | | `probe --config <config.json> [--ask] [--brain qwen]`, `renew-token`, `serve --brain/--qwen-model` |
| Renamed | `UseCase.laya_questions()` | `UseCase.brain_questions()` |
| MCP server name | `LayaBaseAgent` | `BaseAgent` (tool names unchanged) |

### How Qwen answers

* **Same contract as Opus.** Qwen gets the same JSON schema: one probability per allowed answer of every question. Ollama constrains generation to that schema, and BaseAgent still re-validates everything and derives `choice`/`score`/`noul`/`confidence` itself.
* **Repeatable.** `temperature` 0 and a fixed `seed`: the same question gets the same answer.
* **Never truncated.** Ollama silently drops the start of a prompt that is longer than `num_ctx`. BaseAgent estimates the size first and refuses with `CONTEXT_BUDGET_EXCEEDED`. A reply that fills the whole window is refused too.
* **One corrective re-ask.** If the JSON is not a valid probability distribution, Qwen is told why and asked once more (`output_retries`). Otherwise `MODEL_OUTPUT_INVALID`; an invalid answer is never returned.
* **Loaded at startup.** The ~6 GB model is loaded when BaseAgent starts (`preload`), so the first request does not wait for it. If loading takes longer than `preload_timeout`, BaseAgent starts anyway and the first request waits.
* **Local only.** `qwen.base_url` must be a loopback address; the HTTP client never uses a proxy. Evidence sent to Qwen stays on your PC.
* **Ollama's error text stays local.** Ollama's reason, such as "model requires more system memory", is written to the BaseAgent log. Callers get a fixed message.
* **Thinking mode is off by default.** `"think": true` lets Qwen reason before answering. That is slower on a CPU, and it needs `num_predict` ≥ 4096. BaseAgent refuses to start if the model has no thinking support.
* **Fallback.** These mean "Qwen cannot serve right now", and the router moves to the other brain:
  * Ollama not running or unreachable;
  * the model not pulled;
  * a timeout;
  * Ollama out of memory.

  Invalid answers and rejected requests are returned to the caller instead.

### Configuration

```json
{
  "engine":    {"mode": "auto", "brain_order": ["opus", "qwen"], "brain_cooldown": 30},
  "anthropic": {"enabled": true, "model": "claude-opus-5-5", "effort": "high"},
  "qwen":      {"enabled": true, "base_url": "http://127.0.0.1:11434", "model": "qwen3:8b",
                "think": false, "num_ctx": 8192, "num_predict": 1024,
                "inference_timeout": 240, "preload": true, "preload_timeout": 300},
  "request_timeout": 600
}
```

* **`qwen.model`:** the Ollama tag. Check yours with `ollama list`; the Qwen3 8B download is about 5–6 GB.
* **`engine.mode`:** `"qwen"` uses only Qwen; `"opus"` uses only Opus; `"demo"` is the simulation.
* **Local-only data:** set `"allowed_brains": ["qwen"]` in a use case so its evidence never goes to the Anthropic API.
* **Memory:** if your laptop is short of memory, lower `num_ctx` (for example to 4096) and `max_evidence`.

### Your existing configuration still loads

* **Removed settings are reported, then ignored.** These are the `nimble` section, and `engine.model_path`, `device`, `startup_timeout`, `inference_timeout`, `queue_timeout` and `cooldown`.
* **Old brain names become `qwen`.** This covers `"nimble"` and `"laya"` in `engine.mode`, `engine.brain_order` and a use case's `allowed_brains`. Both were local brains, so a local-only use case stays local-only.
* **Nimble's Ollama address is reused for Qwen.**
* **`demo\config.json` in this folder is already in the new format.**
* **Old results don't replay.** Results stored before this change replay as `STALE_RESULT`, because the use-case policy changed. Send a new request ID.

### The folder now has three files

| File | What it is |
|---|---|
| `baseagent.py` | BaseAgent itself (the only program file). |
| `test_baseagent.py` | All tests in one file, including the FastMCP tests and the live tests on your Qwen. |
| `baseagent.bat` | The only script. It runs on Windows directly; WSL is no longer used. |

Plus `CHANGES.md` and the `demo` folder (configuration, `principals.json`, `client.token`, knowledge data). The former helper files are deleted: `baseagent_testkit.py`, `test_mcp_smoke.py`, `test_integration_local.py`, every `.sh` and `*_wsl.bat` script, and the old backup and cache folders.

### Running it

Double-click `baseagent.bat`, or run it in a Windows terminal, and choose:

1. **Run all tests.** Results are written to `test_results.txt`, and a summary is shown.
2. **Start BaseAgent** at `http://127.0.0.1:8765/mcp`. Keep the window open while agents use it; stop with Ctrl+C.
3. **Tests, then start.**
4. **Quick check.** Asks each available brain one test question (expected: `sql_injection`) and shows the time taken, without starting the server.

The same choices work as arguments: `baseagent.bat test`, `start`, `all` or `check`.

What the script does first, each time:

* **Python.** It uses `..\static_analysis\.venv` if that already has every package. Otherwise it uses its own `.venv`, created on the first run, and installs pydantic, langgraph, fastmcp, uvicorn and anthropic there once. `BASEAGENT_PYTHON` overrides the choice.
* **Ollama.** If Ollama is installed but not running, it starts it.
* **The model.** If the model is not pulled yet, it offers to download it (about 5 GB). Without Ollama the live Qwen tests skip, and BaseAgent can only use Opus.
* **Before starting the server,** it creates `demo` on the first run, and renews `demo\client.token` when it expires within an hour.

Optional settings, set in the same terminal before running:

* `set BASEAGENT_QWEN_MODEL=<tag>`: if `ollama list` shows another name than `qwen3:8b`.
* `set BASEAGENT_BRAIN=qwen`: Qwen only, so evidence never leaves the PC. The default is `auto`: Opus first, then Qwen.

`ANTHROPIC_API_KEY` stays an environment variable; the script never writes it to a file.

The script calls these `baseagent.py` commands, which also work directly:

* `serve --brain {auto,opus,qwen,demo} --qwen-model <tag>`: these override `engine.mode` and `qwen.model` from `config.json`.
* `renew-token --config demo\config.json [--principal remediation-agent] [--hours 24] [--force]`. This writes a fresh `client.token`, and only its SHA-256 goes into `principals.json`. A token still valid for more than an hour is kept unless you pass `--force`. Agents need no restart, because BaseAgent re-reads `principals.json` on every request.

### Tests (`test_baseagent.py`, 132 tests)

Run with `baseagent.bat test`, or `python -m unittest -v test_baseagent` (pytest is not needed).

1. **Shared fakes.** A fake Anthropic client and a fake Ollama server on a real socket.
2. **Unit and component tests**, needing no model:
   * JSON safety, the contracts, identity, the knowledge store, the ledger and the answer contract;
   * the demo, Opus and Qwen brains, and the Opus → Qwen fallback;
   * the LangGraph service, end to end through a fake Qwen;
   * the command line, including config migration, `probe`, `renew-token` and the `--brain`/`--qwen-model` overrides.

   `QwenEngine` is checked for:
   * request shape;
   * startup and preload;
   * an old Ollama, a missing model, out of memory, nothing listening, a dropped connection and a slow reply;
   * that Ollama's error text never reaches callers;
   * the context pre-check, the corrective re-ask, and truncated answers;
   * `<think>` blocks;
   * queueing;
   * the loopback-only URL.
3. **FastMCP tests.** These run the real FastMCP server and `fastmcp.Client`, in memory and over HTTP (uvicorn), as derived agents use them:
   * authentication;
   * tenant isolation;
   * the shared derived-agent client;
   * refusing simulated answers;
   * deadlines.
4. **Live tests on your Qwen3 8B**, skipped with the reason when Ollama or the model is unavailable. There are two kinds:
   * **Contract tests.** A failure is a BaseAgent or setup defect:
     * the model loads;
     * all three question types pass;
     * answers are repeatable;
     * oversized evidence is refused;
     * a decision is answered by `qwen` through the service and through FastMCP;
     * auto mode without an API key falls back to Qwen.
   * **Judgement tests.** A failure means Qwen is not good enough for that decision: SQL injection, a hard-coded secret, a safe parameterized query, and an instruction hidden in the code.

In the workspace where this was written, the 94 tests that need no FastMCP or Ollama passed with real pydantic (LangGraph was simulated). The FastMCP and live Qwen tests have to run on your PC: run `baseagent.bat test` and send `test_results.txt`.

### Still not in BaseAgent: plan generation

BaseAgent's contract is still typed questions with probability answers, for Opus and Qwen alike. Qwen *can* write plans, unlike Laya and Nimble, but returning a structured plan (for SCA_Analyzer) is a new kind of request and contract, not part of this change.

---

# History (before 2026-10-08)

_The WSL scripts, `baseagent_testkit.py`, `test_mcp_smoke.py`, `test_integration_local.py` and `explore_laya.py` mentioned below no longer exist._

The sections below describe earlier versions. Everything about Laya, Nimble, PyTorch, Smart App Control and `ProcessEngine` no longer applies.

## 2026-09-30: inconsistency fixes and Opus 5.5 default brain

Scope: this changes the existing `baseagent.py` only. It does two things:

1. Resolves the inconsistencies listed below.
2. Makes Claude Opus 5.5 at effort `high` the default brain, falling back to Nimble and/or Laya when Opus isn't available.

The control layer and limbs of the wider multi-agent system are not part of this change.

## 1. Inconsistencies resolved

Items 1–3 were reproduced against the original file. The rest were confirmed by reading the code.

| # | Inconsistency | Effect | Resolution |
|---|---|---|---|
| 1 | The `validate_answers` allow-list lacked `answer_confidence`, a field real Laya 0.3.x adds to every answer. | **Every real Laya decision failed with `MODEL_OUTPUT_INVALID`.** Only demo mode worked. | Accepts Laya's real fields (`answer_confidence`, `action`, `low_confidence`). Unknown fields are still rejected. |
| 2 | `_demo_result` returned a different shape from real Laya. | Demo runs passed a friendlier contract, which hid #1. | The demo now emits exactly Laya's field set. |
| 3 | `json_dumps` always sorted keys, including the payload sent to the model. | Choice options reached the brain alphabetically, not in profile order. The demo therefore always answered `human_review`. It also matters for Nimble, which breaks ties by request order. | `sort_keys=False` for brain payloads. Hashes and storage stay canonical (sorted). |
| 4 | `SerialThreadRunner` failed immediately when busy, although `max_inflight=8`. | A second concurrent request always got `RETRIEVAL_BUSY`. | Calls now queue, with a bounded wait. |
| 5 | `generation()` and `evidence_current()` ran on the event loop and competed for a 100 ms lock with the retrieval thread. | `STORAGE_BUSY` errors and event-loop stalls under load. | All store calls go through the serialized runner. `_replay` is now async. |
| 6 | `ProcessEngine` killed the worker and opened the circuit on any `MODEL_EXECUTION_FAILED` reply. | One bad request forced a full Laya reload plus a cooldown for all callers. | A well-formed error reply leaves the worker running. Restart happens only on timeout, disconnect or death. |
| 7 | `mode="laya"` with an empty `model_path` was accepted. | Failed late inside the worker. | Rejected at config load. In `auto` mode, Laya is simply not a candidate without a checkpoint. |
| 8 | The engine was typed `Any`. Nimble's `noul` answers have no `confidence` field. The docstring pointed to a README that wasn't shipped. | No contract for adding brains, and Nimble answers would be rejected. | `DecisionEngine` protocol; `noul` confidence is derived as `max(p, 1-p)`; the docstring now points to this file. |

## 2. Opus 5.5 as default brain, with fallback

`engine.mode` now defaults to `"auto"`, which tries brains in `engine.brain_order`. The default order is `["opus", "nimble", "laya"]`.

**At startup**, each brain is probed:

* **Opus:** the API key is present in `ANTHROPIC_API_KEY`, the `anthropic` SDK is installed, and `models.retrieve("claude-opus-5-5")` succeeds. The probe uses no tokens.
* **Nimble:** Ollama is version 0.35 or later and the `nimble` model is pulled.
* **Laya:** the checkpoint loads.

An unavailable brain is logged and skipped. Startup fails only if **no** brain is available.

**Per request**, the first allowed and available brain answers. If it hits an availability fault, the next brain answers the same questions, and the failed brain is skipped for `engine.brain_cooldown` seconds (default 30). Availability faults are:

* no key or SDK;
* connection, auth, rate-limit or overload errors;
* timeouts;
* Ollama down or model missing;
* the Laya worker failing.

**Refusals, invalid output and context-budget errors are not routed to another brain.** Their errors are returned to the caller.

**Every response records which brain answered.** `meta.brain` is `opus`, `nimble`, `laya` or `demo`; `meta.model_id` is the model; `meta.fallbacks` lists the brains that were skipped.

**How Opus answers.** Opus returns the same typed answers as Laya and Nimble (`choice`, `score`, `noul`):

* It is called with `output_config={"effort": "high", "format": json_schema}`, which gives exactly one probability per allowed answer. Effort is always sent explicitly, because Opus 5.5 defaults to `medium`.
* No `thinking` field is sent. Adaptive thinking is always on for Opus 5.5, and disabling it returns a 400.
* No tools are offered, so the brain is read-only.
* `baseagent.py` derives `choice`, `score`, `noul` and `confidence` from those probabilities. The model therefore cannot return inconsistent fields.
* Opus's probabilities are self-reported and uncalibrated, so no `answer_confidence` is emitted. Use cases remain review-gated until you set `calibration_approved`.

**Data egress.** Opus runs on the Anthropic API, so evidence leaves the machine. For any use case whose data must stay local, set `"allowed_brains": ["nimble", "laya"]` in `profiles.json`.

## 3. Configuration

```json
{
  "engine":    {"mode": "auto", "brain_order": ["opus", "nimble", "laya"], "brain_cooldown": 30,
                "model_path": "C:/models/laya", "device": "cpu"},
  "anthropic": {"enabled": true, "model": "claude-opus-5-5", "effort": "high", "max_tokens": 16000,
                "timeout": 120},
  "nimble":    {"enabled": true, "base_url": "http://127.0.0.1:11434", "model": "nimble"}
}
```

```bash
pip install anthropic            # plus your existing pydantic, langgraph, fastmcp, uvicorn
set ANTHROPIC_API_KEY=...        # macOS/Linux: export ANTHROPIC_API_KEY=...
ollama pull nimble               # Ollama >= 0.35
python -m unittest -v test_baseagent
```

## 4. Compatibility notes

* **Default mode.** The default `engine.mode` changed from `"laya"` to `"auto"`. Existing configs that say `"mode": "laya"` keep using Laya only. Change them to `"auto"` to get Opus first.
* **Old results won't replay.** The new `UseCase.allowed_brains` field changes each profile's policy hash. Results stored before this version replay as `STALE_RESULT`; send a new request ID.
* **Longer request deadline.** The default `request_timeout` rose from 45 s to 300 s, because Opus at high effort can think for a while and the graph may call the brain twice.
* **Nimble is loopback only.** It is never reached through an HTTP proxy, and requests are capped at 64 KiB (Ollama's limit).

## 5. Testing

24 tests pass on Python 3.11. They cover:

* real Laya and Nimble response shapes;
* a fake Ollama server;
* a fake Anthropic client that records the exact request;
* Opus as default;
* fallback at startup, at runtime and through cooldown;
* refusal not being routed around;
* excluding Opus per use case;
* the Laya worker restart policy;
* queued concurrency;
* the demo end to end.

This workspace had no package-index access, so real `langgraph`, the real `anthropic` SDK (checked against the SDK 1.10 source) and a live Ollama were not run. Run the tests on your machine.

## 6. Fixes from the 2026-10-01 test run

The expanded tests found eight defects. Two were introduced on 2026-09-30, when knowledge-store calls were moved off the event loop; one came from the original file; one is a fallback improvement.

| # | Defect | Effect | Fix |
|---|---|---|---|
| A | `Ledger.begin()` returned the existing row for a duplicate request ID, and `handle()` ignored it. | Two simultaneous calls with the same `request_id` **both executed the full graph**. | `begin()` raises `IN_PROGRESS`. Completed requests are still replayed. |
| B | `handle()` checked `max_inflight`, then awaited, then incremented the counter. | Concurrent requests could exceed `max_inflight`. | The request is counted before the await. |
| C | A concurrent insert of the same request ID raised a raw `sqlite3.IntegrityError`. | `INTERNAL_ERROR` instead of a clean answer. | Mapped to `IN_PROGRESS`. |
| D | An anchor equal to the document ID admitted **every** paragraph of that document. | Paragraphs with no relation to the question were sent to the brain as evidence. | A document-ID anchor still narrows the scope, but each paragraph also needs query overlap. An anchor written in the paragraph's own text still admits it. |
| F | The MCP `decide` tool answered an unauthenticated caller with a **new random** `request_id`. Found on your machine with fastmcp 4.0.9. | The client rejected the reply as a correlation mismatch (`INVALID_SERVER_RESPONSE`) instead of reporting `UNAUTHORIZED`. | The error echoes the caller's `request_id` when it is a valid UUID. |
| G | The Laya worker discarded the reason it failed to start, and the parent replaced even its code with a generic `MODEL_STARTUP_FAILED`. | A failed Laya load on your machine could not be diagnosed. | The worker prints the traceback to stderr and sends a one-line cause. The parent logs it locally and keeps specific codes (`MODEL_NOT_FOUND`, `DEPENDENCY_MISSING`). Callers still get the fixed message. Run `diagnose_laya.py` to load Laya directly. |
| H | The brain router required a `brain_name` attribute on every engine. The original contract (`start`/`predict`/`close`, `model_id`, `simulation`) has none, and `test_mcp_smoke.py`'s `ScriptedEngine` follows it. | All 20 smoke tests failed with `AttributeError` before reaching MCP. | Engines without `brain_name` are accepted again, named `injected`. Like `demo`, they are always permitted, because they are supplied in code rather than configuration. |
| I | Validation errors in `handle()` were answered with a random `request_id`. | A derived agent could not correlate the error with its request. | A well-formed caller `request_id` is echoed. |
| E | The router only fell back on availability faults. | When Laya returned `CONTEXT_BUDGET_EXCEEDED` (its English model reads 512 tokens), no brain with a bigger context was tried. | A context-budget fault now tries the next allowed brain, with no cooldown for the healthy brain. |

### Tests

| File | Scope |
|---|---|
| `test_baseagent.py` | 81 unit and component tests, no network and no models. Covers JSON safety, data contracts, identity registry, knowledge store (lexical, anchors, authorization, versions, revocation, capacity, semantic and reranker paths), ledger, runtime lease, worker runner, answer contract, Laya pre-check, Laya process engine, Opus, Nimble, fallback router, the LangGraph workflow and service policy, the MCP server and derived-agent client (needs fastmcp), the HTTP guard, and the CLI. |
| `test_integration_local.py` | Real Ollama/Nimble and real Laya; nothing faked. Each test skips with a reason when its brain isn't available to the interpreter. |
| `baseagent_testkit.py` | Shared fakes and fixtures. Uses real langgraph when installed. |
| `run_tests.bat` | Runs both suites with `..\.venv` and writes `test_results.txt`. |

**Laya must be importable by the same interpreter as `baseagent.py`.** The Laya worker is a child process of that interpreter. Your `.venv` has no `laya`, and `laya_env` has no pydantic or langgraph. Either run `..\.venv\Scripts\python -m pip install laya`, or install the BaseAgent dependencies into `laya_env`.

### Laya on this machine (2026-10-01)

The 4 Laya integration failures were **not a BaseAgent defect**. Windows Application Control blocked PyTorch:

```
OSError: [WinError 4551] An Application Control policy has blocked this file.
Error loading ...\.venv\Lib\site-packages\torch\lib\torch.dll
```

`pip install laya` had pulled torch 2.14.1, a build published days earlier. Smart App Control, or an organization policy, typically blocks new unsigned DLLs without a reputation. `laya_env` has torch 2.14.0, which loaded earlier on this machine. BaseAgent itself behaved correctly: Laya was marked unavailable and Nimble kept answering.

* **`ensure_torch.py`** is run by `run_tests.bat`. If torch cannot load in `.venv` but loads in `..\laya_env`, it installs that same, already-accepted torch version into `.venv`. If Windows blocks every torch build, it says so; the only remedies then are a Windows policy change (by whoever manages the PC) or leaving Laya disabled.
* **The Laya integration tests now skip** with the Windows error as the reason when torch cannot load, the same way they skip when `laya` is not installed. A blocked prerequisite is reported, not counted as a code failure.

### MCP / FastMCP

* The MCP section matches the original `baseagent.py` (tool names, `ToolError` envelopes, lifespan, strict input validation), plus the `request_id` fixes (F, I).
* **`create_http_app(service, settings)`** is the single FastMCP HTTP builder: streamable HTTP at `/mcp`, bearer tokens verified against `principals.json` on every call, behind the loopback Host/Origin guard. `serve --transport http` uses it, and `McpHttpTests` runs it with uvicorn on a real port.
* **`baseagent_get_contract`** now also returns `brains: {order, status}`, so derived agents can see which brain is live.
* **`test_mcp_smoke.py`** (yours, unchanged) is copied into `BaseAgent\` and run by `run_tests.bat` against the new file. The copy in `static_analysis\` imports the original `baseagent.py` next to it.

### Running everything including Laya: WSL2

Smart App Control is **On** on this PC (`VerifiedAndReputablePolicyState = 1`) and blocks PyTorch's unsigned DLLs, with no per-app exceptions. The Linux side of WSL2 is outside Windows Application Control, so PyTorch and Laya load there.

* **`run_tests_wsl.bat`**: double-click on Windows. It starts `run_tests_wsl.sh` in WSL Ubuntu.
* **`run_tests_wsl.sh`**:
  * Creates `~/.venvs/baseagent` in Linux, with CPU-only PyTorch and the BaseAgent packages (first run only).
  * Reuses the Laya model from the Windows Hugging Face cache.
  * Checks that Windows Ollama is reachable.
  * Runs `test_baseagent.py`, `test_mcp_smoke.py` and `test_integration_local.py`, and writes `test_results_wsl.txt`.
* For Nimble from WSL, set `networkingMode=mirrored` under `[wsl2]` in `C:\Users\<you>\.wslconfig`, then run `wsl --shutdown`.
* `run_tests.bat` (Windows) still works; there, the Laya tests skip while Smart App Control is on.


### First WSL2 run (Ubuntu 26.04, Python 3.14): 110 passed, 2 failed, both fixed

Laya, PyTorch 2.14.1 (CPU) and Nimble all loaded. The Laya service tests and the Nimble-down-to-Laya fallback passed for real.

* **Fixed in `baseagent.py`: Nimble connect timeout reported as `MODEL_TIMEOUT`.** With WSL2 mirrored networking, a connection to a port nothing listens on is silently dropped instead of refused. `NimbleEngine` saw "timed out" and reported `MODEL_TIMEOUT` ("Ollama is slow"), although no connection was ever made. It now reports a failure while connecting as `MODEL_UNAVAILABLE` ("unreachable"). `MODEL_TIMEOUT` is kept for a request Ollama accepted but answered too slowly. Routing was already correct, because both are availability faults; the error the caller sees is now accurate. A new test covers both cases.
* **Fixed in `test_integration_local.py`: the test asserted Laya's verdict.** On the `example.py` snippet without its "DANGER" comment, the Laya checkpoint answered `none` (0.51) instead of `sql_injection` (0.35). It also warned that the checkpoint's temperatures are uncalibrated. That is the model's quality, not a BaseAgent defect. The test now checks the contract with the real model: option order, `choice` is the most probable option, probabilities sum to 1, value ranges, and `answer_confidence` is present. The verdict is printed. This is why Laya answers stay review-gated and Laya is last in the default brain order.

### What Laya can and cannot judge (`explore_laya.py`)

`explore_laya_wsl.bat [--typed]` runs the real Laya model on three snippets: vulnerable SQL, parameterized SQL, and a hard-coded key. It uses 3 input formats × 3 question wordings, on the English and `typed-decisions` checkpoints.

* **SQL injection: not reliable.** In most variants Laya answered `none` for the vulnerable code. P(sql_injection) was barely higher for concatenated SQL than for parameterized SQL (+0.02 to +0.18, and once lower). The two variants that scored 3/3 won by **0.02**, which is chance among 18 attempts. A test pinned to them would flip with any Laya, torch or whitespace change.
* **Hard-coded secrets: reliable.** Laya was right in 35 of 36 runs; with the original wording and JSON input, P=0.73 against 0.19.
* **Test:** `LayaLiveTests.test_laya_verdict_on_hardcoded_secret` checks this verdict through BaseAgent's Laya worker. Laya must flag the key, must not flag code without one, and needs a gap of more than 0.3. Nimble's live test keeps the SQL-injection verdict.
* **For SCA_Analyzer:** do not let Laya decide rule violations on its own. Opus, then Nimble, should judge code; Laya is a last-resort, review-gated fallback (the current defaults).

### Derived-agent client deadline

* **Fixed: `BaseAgentClient` gave up before the server did.** Its default timeout was 50 s, but `request_timeout` on the server is 300 s, because Opus at high effort can think for minutes. A derived agent would have reported `COMMUNICATION_FAILURE` while BaseAgent was still producing the answer. The client default is now 310 s. `ClientDeadlineTests` checks that the client always waits longer than the server deadline. The simulated brains answer instantly, which is why the existing tests did not catch this.

### First live run with Nimble and Laya (`start_baseagent_wsl.bat`)

* **`start_baseagent_wsl.bat` / `check_baseagent_wsl.bat`** start BaseAgent in WSL with every available brain and send one test decision. `start_baseagent_wsl.bat laya,nimble` changes the order. `ANTHROPIC_API_KEY` is passed through from Windows when set.
* **Fixed: Nimble's first request timed out.** After `keep_alive` (5 min), Ollama unloads the model and reloads it on the next request. That can exceed the old 30 s `nimble.inference_timeout` on a CPU, so the first live request fell back to Laya. The default is now 120 s, which still fits twice within the 300 s request deadline. The start script also raises it in existing configs.
* **Fixed: runtime fallbacks were silent.** The router now logs `Brain <name> failed (<code>); falling back ...`. `baseagent_get_contract` reports the failure code in `brains.status`, and the check script shows it.
