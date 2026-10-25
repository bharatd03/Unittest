# UnitTest – evidence-gated unit-test generator for C and C++

A multi-agent application built on **BaseAgent** that analyses C/C++ source, generates unit tests,
builds and runs them, measures line, statement, branch, decision and MC/DC coverage, classifies
every failure and repairs the right artifact – without weakening a test or hiding a real defect.

## Repository layout

```
UnitTest/
├── Source/
│   ├── BaseAgent/              baseagent.py, baseagent.bat – the base framework
│   └── UnitTestGenerator/      the derived agents, orchestrator, setup, ut_config.json,
│                               knowledge packs, unittest_generator.bat / .sh
├── Test/                       test_unittest_generator.py, test_baseagent.py, pilots/,
│                               constructs/ (one small example per C/C++ construct)
├── Docs/                       requirements, ORCHESTRATION.md, BaseAgent_CHANGES.md
├── .github/                    skills (ut-agent-builder, agent-live-viewer), Copilot instructions
└── Patents/                    kept on your computer only – not in Git
```

## Prerequisites

* **Python 3.12** – on Windows from python.org with the `py` launcher; on Linux `python3.12`.
* **Git.**
* **A brain** (optional for the tests):
  * `ANTHROPIC_API_KEY` as an environment variable – the Claude models the key can access are
    used in the order of `ut_config.json`. The key is never written to a file.
  * otherwise Qwen3 8B on the remote server in `brain.qwen_remote` of `ut_config.json` (OpenAI-compatible API; its key as the environment variable `QWEN_REMOTE_API_KEY`, never in a file),
  * otherwise Qwen3 8B through [Ollama](https://ollama.com) on this PC (`ollama pull qwen3:8b`).

Nothing else needs to be installed by hand.

## First run

Windows:

```bat
git clone https://github.com/novaqtek-art/UnitTest.git
cd UnitTest
Source\UnitTestGenerator\unittest_generator.bat check
Source\UnitTestGenerator\unittest_generator.bat test
Source\UnitTestGenerator\unittest_generator.bat pilot-c
```

Linux:

```sh
git clone https://github.com/novaqtek-art/UnitTest.git
cd UnitTest
Source/UnitTestGenerator/unittest_generator.sh check
Source/UnitTestGenerator/unittest_generator.sh test
Source/UnitTestGenerator/unittest_generator.sh pilot-c
```

The first command:

1. creates the Python environment in `Source/BaseAgent/.venv` and installs pydantic, langgraph,
   fastmcp, uvicorn and anthropic;
2. installs the tools – Clang/LLVM with MC/DC coverage, libFuzzer (Linux; on Windows the tool's own
   search ut_search takes its place) and the static analyzer, CMake,
   Ninja, Git – into `Source/UnitTestGenerator/tools`, from the official release pages with
   SHA-256 checks. This takes a few minutes once; later runs only check them (about a second).

Run without a command, the `.bat` shows a menu.

## Commands

| Command | What it does |
| --- | --- |
| `check` | tools, Python packages and available brains |
| `test` | the component tests, a few minutes; results in `test_results.txt` |
| `test-full` | every test, including whole pipeline runs, in parallel processes (release validation) |
| `pilot-c`, `pilot-cpp`, `pilot-deps` | the full pipeline on a pilot project, with the live viewer |
| `pilot-embedded`, `pilot-embedded-cpp` | the full pipeline on the embedded pilots: registers, register polling, a superloop, interrupt flags, target keywords and assembly; an injected interface, exceptions, a static object in a function and a header-only template |
| `pilot-realrepo` | the full pipeline on patterns found in real repositories: a header that needs its configuration header first, `T * const` parameters, a header next to the source only, `_Atomic` and callback fields, a handle whose `Instance` points to registers, C++ namespaces |
| `pilot-large` | the full pipeline on the two large pilots: a body control module in C and a powertrain controller in C++, about 5000 lines each, with generic code by macros and `_Generic` (C) and by class and function templates, specializations, CRTP and lambdas (C++) |
| `pilot-all` | every pilot folder, every production file Agent1 selects (25 files, the header-only template of the embedded C++ pilot included), each pilot on a fresh copy; one summary at the end (`ut_output/pilots/pilot_all_summary.txt`) |
| `--no-cache` (with `run` or `batch`) | no cache of earlier work: every brain question is asked, every file parsed, compiled and every test measured again [CA-1]. The caches - brain answers, decisions on failures, Stage A results, front-end parses, objects, measurements of tests alone and mutation checks, verified tests, the failure journal and the search inputs of earlier runs (CA-2 to CA-13) - are in `ut_cache/run_cache` (at most 4 GB; `cache` in `ut_config.json`); what they saved is in the report. A suite of more than 200 tests is compiled in parts at the same time (`compilation.split_suite_tests`, CA-14) |
| `--interactive` (with `run` or a `pilot-...`) | ask at a decision point when items need a source change or a requirement; the menu always does, other runs never ask and put the question into the report [LA-9] |
| `benchmark [on\|off\|both]` | seeded-defect benchmark on the pilots |
| `calibrate BRAIN` | runs the construct corpus (`Test/constructs`) with one real brain, e.g. `sonnet` or `qwen_remote`, and writes which answer kinds it passes and a profile proposal to `ut_output/calibration/BRAIN.md` (BP-8); your configuration is never changed |
| `run REPO [SUT\|all] [--branch NAME] [--requirements FILE_OR_FOLDER]` | a git repository link or a folder, and the source file to test; without a file (or `all`) every production file of the commit is selected and tested (ORC-35); `--requirements` adds the project's requirement documents - ReqIF, Markdown, text or CSV - for the run (A1-RQ1) |
| `view RUN_FOLDER` | replay an earlier run in the viewer |
| `baseagent-test` | the BaseAgent tests (Windows and Linux); results in `test_results_baseagent.txt` |

BaseAgent on its own: `Source\BaseAgent\baseagent.bat test`, `start` or `check`; on Linux the BaseAgent tests run with `Source/UnitTestGenerator/unittest_generator.sh baseagent-test`.

`run` accepts a folder relative to the folder you start the command from.

Results of a run go to `Source/UnitTestGenerator/ut_output/`.

Embedded code runs on the host: target registers are mapped in host memory, target compiler keywords get host definitions and target assembly is replaced in the run's copy of the source (never in your files); the report lists all of it under "Target code on the host". When the target is 32-bit and the build was not captured from its cross-compiler, set `compilation.target_data_model` to `ILP32` in `ut_config.json`, so the report names the functions whose `long` arithmetic differs on the host.

What each brain may write is its profile, in the `profiles` section of `ut_config.json`: the Claude models get the
profile `full` (they may also write call sequences, objects, fakes and tests with shared set-up), Qwen3 8B gets
`minimum` (scenarios, values, tests, repairs and decisions, as before). A model of your own without an entry gets
`minimum`; `calibrate` measures what it can do. Whatever a brain writes passes the same checks and Agent3's
measurement. Items the pipeline itself could not handle - with every answer kind the available brains may give - are
listed first in the report and collected across runs in `ut_output/pipeline_findings.jsonl`; send that file with a
report when a new kind of code ends incomplete.

Two SUT files run at the same time (`pipeline.max_parallel_suts` in `ut_config.json`, default 2; 1 runs them one after another). Set `brain.qwen_remote.max_concurrency` (and `brain.qwen.max_concurrency`) to the number of requests your Qwen server answers at once - `OLLAMA_NUM_PARALLEL` on an Ollama server; it holds for all files of the run together. Answers of the remote server are streamed, so a tunnel such as Cloudflare does not cut a long answer, and a gateway timeout is asked again on the same server before the local Ollama is used (`stream`, `gateway_retries`, `gateway_pause`). `pilot-all` shows every pilot on one viewer page; the **Stop** button there ends the run at its next safe point and keeps what was verified. A request waits up to `slot_wait` (30 min) for a free slot of the remote server rather than going to the local Ollama on the same GPU. Each file gets its own test, stub and access files (`test_<file>.c`, `ut_stubs_<file>.c/.h`, `ut_sut_access_<file>.c`).

With a Claude model, the brain is asked about several functions at the same time (`brain.anthropic.parallel_calls` in `ut_config.json`, default 4; match it to your API quota); with Qwen one at a time. The results are combined in a fixed order, so the same input gives the same tests.

Stopping a run: press Ctrl+C once. The run stops and still delivers the tests Agent3 has verified so far, with the report and the ledger (answer `N` if Windows asks "Terminate batch job" before the report is printed). A second Ctrl+C ends at once. Once every coverage target is met, the run spends at most 10 minutes checking the documented behaviour (`pipeline.after_target_seconds` in `ut_config.json`, 0 = no limit).

## Working on the code

The requirements in [`Docs/unittest_generator_requirments.txt`](Docs/unittest_generator_requirments.txt)
are the contract: change them first, then the code, then run the tests. The rules are in
[`.github/skills/ut-agent-builder/SKILL.md`](.github/skills/ut-agent-builder/SKILL.md); the agent
flow is drawn in [`Docs/ORCHESTRATION.md`](Docs/ORCHESTRATION.md).

The tests never use a real brain, even when an API key is set or Ollama is running, so they cost no tokens and give the same result on every computer.

Windows: the pipeline compiles and runs test programs. Smart App Control, or a company's application-control policy, must allow programs built in the tool's work folder; otherwise the run stops with a message that names the blocked program.

Not in the repository (see `.gitignore`): the installed tools, `.venv`, run results, the
knowledge-base database, test results, BaseAgent's local `demo` state with its client token, and
the patent material.
