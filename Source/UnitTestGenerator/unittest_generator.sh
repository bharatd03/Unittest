#!/usr/bin/env bash
# ===========================================================================
#  Unit-test generator on Linux - the same commands as unittest_generator.bat.
#
#    ./unittest_generator.sh setup             check every tool again and install what is missing
#    ./unittest_generator.sh check             show the tools, Python packages and brains in use
#    ./unittest_generator.sh test              the component tests (a few minutes), results in test_results.txt
#    ./unittest_generator.sh test-full         every test incl. whole pipeline runs, in parallel (release validation)
#    ./unittest_generator.sh corpus [CASES]    the construct corpus in Qwen's structured answer form, no brain (about a
#                                              minute): every construct built, compiled, run; CASES = name1,name2 [NC-4]
#    ./unittest_generator.sh pilot-c | pilot-cpp | pilot-deps | pilot-embedded | pilot-embedded-cpp | pilot-realrepo
#                                              every SUT file of a pilot, one live viewer page for all
#    ./unittest_generator.sh pilot-medium      the two medium pilots (about 700 lines each; the C++ file calls into
#                                              several namespaces: nested, C++17 nested, alias, overloads, extern "C")
#    ./unittest_generator.sh pilot-large       the two large pilots (about 5000 lines each, C and C++ with templates)
#    ./unittest_generator.sh pilot-all         every pilot, every production file (25 files), one summary
#    ./unittest_generator.sh pilot-small       the eight small pilots, every production file, one summary
#    ./unittest_generator.sh pilot-ordered     the small pilots, then the medium, then the large ones, one after another
#    ./unittest_generator.sh view RUN_FOLDER   show an earlier run again in the viewer (and viewer.html)
#    ./unittest_generator.sh baseagent-test    the BaseAgent tests, results in test_results_baseagent.txt [TP-24]
#    ./unittest_generator.sh benchmark [on|off|both]
#                                              seeded-defect benchmark on the pilots, with the knowledge
#                                              packs (on, default), without (off) or both to compare them
#    ./unittest_generator.sh calibrate BRAIN   the construct corpus with one real brain (e.g. sonnet, qwen_remote):
#                                              which answer kinds it passes, and a profile proposal [BP-8]
#    ./unittest_generator.sh run REPO [SUT|all] [--branch NAME] [--sut FILE] [--requirements FILE_OR_FOLDER]
#                                [--build-command "..."] [--compile-commands FILE]
#                                [--interactive]  ask when a source change or a requirement is needed [LA-9]
#                                              REPO = git repository link (https://... or git@...) or a folder
#
#  A repository link: the default branch is tested unless --branch names another one. Tests only and
#  verified: one commit to that branch. Production source changed, or not verified: a review branch and a
#  pull request - merging it is your approval. Git uses the login already set up on this PC.
#
#  Every command first runs ut_setup.py [TP-12]: missing tools are installed into ./tools from the
#  official release pages with SHA-256 checks; C library headers come from the distribution's
#  package manager (without a password prompt, otherwise the command to run is shown).
#  Brains: the Claude models ANTHROPIC_API_KEY can access (Opus 5.5, Fable 5.1, Sonnet 5.5, Haiku 5.5,
#  in the order of ut_config.json), else Qwen3 8B on the remote server (QWEN_REMOTE_API_KEY), else
#  Qwen3 8B through Ollama. "check" shows which are available.
#  ANTHROPIC_API_KEY and QWEN_REMOTE_API_KEY stay environment variables; never written to a file.
#  Live viewer: every run opens it in the browser (on a desktop) or prints its address; --no-view
#  after the command switches it off. Settings: "viewer" in ut_config.json.
# ===========================================================================
set -u
CALLER="$(pwd)"                                 # a relative repository folder is meant from here [TP-23]
export UT_CALLER="$CALLER"                       # and a relative requirement document outside it [A1-RQ1]
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
export PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
export BASEAGENT_HOME="${BASEAGENT_HOME:-$HERE/../BaseAgent}"
# Repository layout: Source/UnitTestGenerator (this file), Source/BaseAgent, Test (tests and pilots).
TESTS="$HERE/../../Test"
PILOTS="$TESTS/pilots"
export BASEAGENT_QWEN_MODEL="${BASEAGENT_QWEN_MODEL:-qwen3:8b}"
ACTION="${1:-}"
if [ -z "$ACTION" ]; then
  echo "Usage: $0 setup|check|test|test-full|corpus [CASES]|baseagent-test|pilot-c|pilot-cpp|pilot-deps|pilot-embedded|pilot-embedded-cpp|pilot-realrepo|pilot-medium|pilot-large|pilot-all|pilot-small|pilot-ordered|benchmark|calibrate BRAIN|view RUN_FOLDER|run REPO SUT [options]"
  exit 2
fi
shift

# TP-23: a relative folder (not a link, not an absolute path) is resolved from the caller's folder
from_caller() {
  case "$1" in
    http://*|https://*|git@*|ssh://*|/*|"") printf '%s' "$1" ;;
    *) if [ -e "$CALLER/$1" ]; then (cd "$CALLER" && cd "$(dirname "$1")" && printf '%s/%s' "$(pwd)" "$(basename "$1")")
       else printf '%s' "$1"; fi ;;
  esac
}
if [ "$ACTION" = run ] && [ $# -ge 1 ]; then
  REPO_ARG="$(from_caller "${1%/}")"; shift
  set -- "$REPO_ARG" "$@"
fi
if [ "$ACTION" = view ] && [ $# -ge 1 ]; then
  VIEW_ARG="$(from_caller "${1%/}")"; shift
  set -- "$VIEW_ARG" "$@"
fi

# ---------------------------------------------------------------- Python 3.12
PY="${BASEAGENT_PYTHON:-$BASEAGENT_HOME/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  if command -v python3.12 >/dev/null 2>&1; then
    echo "Creating the BaseAgent Python environment with Python 3.12 ..."
    python3.12 -m venv "$BASEAGENT_HOME/.venv" || exit 1
  else
    echo "Python 3.12 was not found (python3.12). Install it, or set BASEAGENT_PYTHON."
    exit 1
  fi
fi
if [ ! -f "$BASEAGENT_HOME/baseagent.py" ]; then
  echo "baseagent.py was not found in $BASEAGENT_HOME. Set BASEAGENT_HOME to the BaseAgent folder."
  exit 1
fi

if [ "$ACTION" = view ]; then                  # an earlier run: no tools, no brain needed
  if [ $# -lt 1 ]; then echo "Usage: $0 view RUN_FOLDER"; exit 2; fi
  exec "$PY" ut_pipeline.py view "$@"
fi
if [ "$ACTION" = baseagent-test ]; then        # TP-24: the BaseAgent tests (no tools, no brain needed)
  RESULTS="$HERE/test_results_baseagent.txt"
  echo "Running the BaseAgent tests ..."
  (cd "$TESTS" && "$PY" -u -m unittest -v test_baseagent "$@") > "$RESULTS" 2>&1
  RC=$?
  grep -E "^(FAIL|ERROR):|^Ran [0-9]+ test|^OK|^FAILED" "$RESULTS"
  echo "Full results: $RESULTS"
  exit $RC
fi

# ---------------------------------------------------------------- tools [TP-12]
SETUP=(ensure)
case "$ACTION" in
  setup) SETUP=(ensure --force) ;;
  run)
    if [ $# -ge 1 ]; then
      SETUP=(ensure --repo "${1%/}")
      for ((i = 1; i <= $#; i++)); do
        if [ "${!i}" = "--branch" ]; then j=$((i + 1)); SETUP+=(--branch "${!j}"); fi
      done
    fi ;;
  pilot-deps|pilot-all|pilot-small|pilot-ordered) SETUP=(ensure --repo "$PILOTS/c_deps_pilot") ;;
  pilot-large) SETUP=(ensure --repo "$PILOTS/c_large_pilot") ;;
  pilot-medium) SETUP=(ensure --repo "$PILOTS/c_medium_pilot") ;;
esac
"$PY" ut_setup.py "${SETUP[@]}"
RC=$?
if [ $RC -ne 0 ]; then
  [ $RC -eq 3 ] && echo "An action by you is needed - see above." || echo "Setup did not complete. The pipeline does not start without its tools."
  exit $RC
fi
[ "$ACTION" = setup ] && exit 0

# ---------------------------------------------------------------- Ollama (Qwen3 8B, the fallback brain)
if [ "$ACTION" != test ] && [ "$ACTION" != corpus ] && [ "$ACTION" != check ] && command -v ollama >/dev/null 2>&1; then
  if ! ollama list >/dev/null 2>&1; then
    echo "Starting Ollama ..."
    (ollama serve >/dev/null 2>&1 &)
    for _ in $(seq 1 15); do sleep 2; ollama list >/dev/null 2>&1 && break; done
  fi
fi

pilot() {                     # pilot NAME SUT [SUT ...] [--no-view]: every SUT file, one viewer page
  local name="$1"; shift
  local repo="$HERE/ut_output/pilots/$name" args=()
  for s in "$@"; do
    case "$s" in --*) args+=("$s") ;; *) args+=(--sut "$s") ;; esac
  done
  rm -rf "$repo"; mkdir -p "$(dirname "$repo")"; cp -r "$PILOTS/$name" "$repo"
  echo "Running the pipeline on $repo ..."
  "$PY" ut_pipeline.py run --repo "$repo" --config ut_config.json "${args[@]}"
}

case "$ACTION" in
  check) "$PY" ut_pipeline.py check --config ut_config.json ;;
  test|test-full)
    # TP-20: "test" the component tests, "test-full" every test in parallel; TP-21: never a real brain
    RESULTS="$HERE/test_results.txt"
    { echo "==== Unit-test generator tests ($ACTION) $(date) ===="; "$PY" ut_pipeline.py check --config ut_config.json; echo; } > "$RESULTS" 2>&1
    if [ "$ACTION" = test-full ]; then
      (cd "$TESTS" && UT_TESTS=full "$PY" -u test_unittest_generator.py --parallel) >> "$RESULTS" 2>&1
      RC=$?
      grep -E "^(FAIL|ERROR):|^Summary:" "$RESULTS"
    else
      (cd "$TESTS" && UT_TESTS=quick "$PY" -u -m unittest -v test_unittest_generator) >> "$RESULTS" 2>&1
      RC=$?
      grep -E "^(FAIL|ERROR):|^Ran [0-9]+ test|^OK|^FAILED" "$RESULTS"
    fi
    echo "Full results: $RESULTS"
    exit $RC ;;
  corpus)
    # NC-4: every construct of Test/constructs in Qwen's structured form, no brain. The rule for every failure of a
    # run: reduce it to a construct - covered here, it is the brain's choice and no code changes; not covered, it
    # gets a case here first, then the generator learns it once (Test/constructs/README.md)
    (cd "$TESTS" && UT_CORPUS="${1:-}" "$PY" -u -m unittest test_unittest_generator.CorpusStructuredTests)
    exit $? ;;
  pilot-c) pilot c_pilot src/thermostat.c "$@" ;;
  pilot-cpp) pilot cpp_pilot src/rate_limiter.cpp src/steering_monitor.cpp src/torque_map.cpp "$@" ;;
  pilot-deps) pilot c_deps_pilot src/ctrl.c src/brake_ctrl.c src/steer_assist.c "$@" ;;
  pilot-embedded) pilot c_embedded_pilot src/gpio_driver.c src/scheduler.c src/critical.c src/main_loop.c "$@" ;;
  pilot-embedded-cpp) pilot cpp_embedded_pilot src/motor_ctrl.cpp src/counter.cpp src/app_main.cpp include/ring_buffer.hpp "$@" ;;
  pilot-large|pilot-medium|pilot-realrepo)
    # two pilot folders in one command, so one viewer page from the first file to the last [VIEW-12a]
    mkdir -p "$HERE/ut_output/pilots"
    if [ "$ACTION" = pilot-large ]; then
      names=(c_large_pilot cpp_large_pilot)
      suts=(--sut c_large_pilot=src/body_ctrl.c --sut cpp_large_pilot=src/powertrain.cpp)
    elif [ "$ACTION" = pilot-medium ]; then
      names=(c_medium_pilot cpp_medium_pilot)
      suts=(--sut c_medium_pilot=src/climate_ctrl.c --sut cpp_medium_pilot=src/chassis_ctrl.cpp)
    else
      names=(c_realrepo_pilot cpp_realrepo_pilot)
      suts=(--sut c_realrepo_pilot=kernel/src/klist.c --sut c_realrepo_pilot=kernel/src/kqueue.c
            --sut c_realrepo_pilot=codec/ring.c --sut c_realrepo_pilot=drivers/crc_drv.c
            --sut cpp_realrepo_pilot=src/dispatcher.cpp)
    fi
    repos=()
    for name in "${names[@]}"; do
      repo="$HERE/ut_output/pilots/$name"; rm -rf "$repo"; cp -r "$PILOTS/$name" "$repo"; repos+=(--repo "$repo")
    done
    "$PY" ut_pipeline.py batch "${repos[@]}" "${suts[@]}" --config ut_config.json "$@"
    exit $? ;;
  pilot-small)
    # the eight small pilots, every production file Agent1 selects; the header-only SUT is named; one summary
    SUMMARY="$HERE/ut_output/pilots/pilot_small_summary.txt"; mkdir -p "$HERE/ut_output/pilots"
    repos=()
    for name in c_pilot cpp_pilot c_deps_pilot c_embedded_pilot cpp_embedded_pilot c_hal_pilot c_realrepo_pilot \
                cpp_realrepo_pilot; do
      repo="$HERE/ut_output/pilots/$name"; rm -rf "$repo"; cp -r "$PILOTS/$name" "$repo"; repos+=(--repo "$repo")
    done
    "$PY" ut_pipeline.py batch "${repos[@]}" --sut cpp_embedded_pilot=src/motor_ctrl.cpp \
      --sut cpp_embedded_pilot=src/counter.cpp --sut cpp_embedded_pilot=src/app_main.cpp \
      --sut cpp_embedded_pilot=include/ring_buffer.hpp --summary "$SUMMARY" --config ut_config.json "$@"
    exit $? ;;
  pilot-ordered)
    # small, then medium, then large: each group its own batch; the next starts when the one before ends
    RC=0
    for group in pilot-small pilot-medium pilot-large; do
      "$0" "$group" "$@" || RC=$?
    done
    echo "Small, medium and large pilots done. Small pilots: $HERE/ut_output/pilots/pilot_small_summary.txt"
    exit $RC ;;
  pilot-all)
    # every pilot folder, every production file Agent1 selects [ORC-35]; the header-only SUT is named;
    # one command for all of them, so one viewer page from the first file to the last [VIEW-12a]
    SUMMARY="$HERE/ut_output/pilots/pilot_all_summary.txt"; mkdir -p "$HERE/ut_output/pilots"
    repos=()
    for dir in "$PILOTS"/*/; do
      name="$(basename "$dir")"; repo="$HERE/ut_output/pilots/$name"
      rm -rf "$repo"; cp -r "$PILOTS/$name" "$repo"; repos+=(--repo "$repo")
    done
    "$PY" ut_pipeline.py batch "${repos[@]}" --sut cpp_embedded_pilot=src/motor_ctrl.cpp \
      --sut cpp_embedded_pilot=src/counter.cpp --sut cpp_embedded_pilot=src/app_main.cpp \
      --sut cpp_embedded_pilot=include/ring_buffer.hpp --summary "$SUMMARY" --config ut_config.json "$@"
    exit $? ;;
  benchmark) "$PY" ut_pipeline.py benchmark --config ut_config.json --knowledge "${1:-on}" ;;
  calibrate)                                    # BP-8: one real brain on the construct corpus
    if [ $# -lt 1 ]; then echo "Usage: $0 calibrate BRAIN, for example sonnet or qwen_remote [BP-8]"; exit 2; fi
    "$PY" ut_pipeline.py calibrate "$1" --config ut_config.json ;;
  run)
    if [ $# -lt 1 ]; then echo "Usage: $0 run REPO [SUT|all] [options]"; exit 2; fi
    REPO="${1%/}"; shift
    SUT_ARGS=()                                 # no SUT, or "all": every production file [ORC-35]
    if [ $# -ge 1 ] && [ "${1#--}" = "$1" ]; then
      if [ "$1" != "all" ]; then SUT_ARGS=(--sut "$1"); fi
      shift
    fi
    "$PY" ut_pipeline.py run --repo "$REPO" "${SUT_ARGS[@]}" --config ut_config.json "$@" ;;
  *) echo "Unknown option $ACTION"; exit 2 ;;
esac
