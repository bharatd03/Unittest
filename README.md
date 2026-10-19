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
├── Test/                       test_unittest_generator.py, test_baseagent.py, pilots/
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
  * otherwise Qwen3 8B through [Ollama](https://ollama.com) (`ollama pull qwen3:8b`).

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
| `benchmark [on\|off\|both]` | seeded-defect benchmark on the pilots |
| `run REPO SUT [--branch NAME]` | a git repository link or a folder, and the source file to test |
| `view RUN_FOLDER` | replay an earlier run in the viewer |
| `baseagent-test` | the BaseAgent tests (Windows and Linux); results in `test_results_baseagent.txt` |

BaseAgent on its own: `Source\BaseAgent\baseagent.bat test`, `start` or `check`; on Linux the BaseAgent tests run with `Source/UnitTestGenerator/unittest_generator.sh baseagent-test`.

`run` accepts a folder relative to the folder you start the command from.

Results of a run go to `Source/UnitTestGenerator/ut_output/`.

Embedded code runs on the host: target registers are mapped in host memory, target compiler keywords get host definitions and target assembly is replaced in the run's copy of the source (never in your files); the report lists all of it under "Target code on the host". When the target is 32-bit and the build was not captured from its cross-compiler, set `compilation.target_data_model` to `ILP32` in `ut_config.json`, so the report names the functions whose `long` arithmetic differs on the host.

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
