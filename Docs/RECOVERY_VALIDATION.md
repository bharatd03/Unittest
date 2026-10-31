# Recovery fixes: validation record

Validated on Linux on 2026-10-31. The recovery implementation is commit `5bf330a`, based on incoming main
`6c45292`. No live remote model was contacted. The tests use scripted brains; the remote thinking-off request
was checked using a mocked transport.

## Implemented

- Journal entries close only with explicit coverage or passing-test evidence. Removing, renaming, skipping or
  quarantining a failing candidate does not resolve it. Another test's coverage cannot resolve its failure.
- Failed test names can be reused during repair. Repeat suppression records executed candidates and includes
  source, build, dependency, header and fixture identity.
- Updated diagnostic feedback invalidates the no-answer fingerprint.
- The bounded third attempt prefers another model but remains available with a single model family.
- Remote Qwen thinking is disabled for every configured step. Model capabilities, evidence checks and existing
  iteration/attempt limits remain in force.

## Results

| Check | Result |
| --- | --- |
| Targeted recovery/planning/learning tests | 40 tests, passed |
| Component suite | 332 tests: 292 passed, 40 skipped |
| BaseAgent suite | 227 tests: 212 passed, 15 skipped |
| Full release checks | 376 test invocations: 368 passed, 7 failed, 1 skipped |
| Full-pipeline construct corpus | All 45 cases passed their existing expectations |

The component skips comprise 39 whole-pipeline tests and one Make-manifest check. BaseAgent skips optional live
model and browser checks. The full run skips the Make-manifest check. Corpus expectations include explicit
incomplete/unsupported outcomes: passing the corpus does not mean every construct reached 100% coverage.

The release checks used the existing test methods and unchanged assertions and search budgets. The corpus's
single serial method was invoked separately for each case, in four worker processes, alongside the other release
tests. This adds 44 invocations to the usual 332-test count. An earlier serial validation attempt was interrupted
to switch to this parallel run; it is not counted as a passing full-suite run.

Standard entry points, from the repository root:

```sh
bash Source/UnitTestGenerator/unittest_generator.sh test
bash Source/UnitTestGenerator/unittest_generator.sh baseagent-test
UT_TEST_WORKERS=4 bash Source/UnitTestGenerator/unittest_generator.sh test-full
```

## Existing release failures

Each of these seven failing tests was also executed against an isolated archive of unchanged incoming main
`6c45292`, with the same interpreter and compiler, and failed there too:

1. `PipelineTests.test_a_brain_test_that_does_not_compile_costs_no_iteration`
2. `PipelineTests.test_a_second_run_reuses_what_the_first_verified`
3. `PipelineTests.test_a_wrong_brain_test_takes_its_values_from_the_run_when_no_source_change_exists`
4. `PipelineTests.test_c_pilot_reaches_full_coverage`
5. `PipelineTests.test_embedded_cpp_files_reach_their_targets_without_a_brain`
6. `RepositoryTests.test_source_fix_opens_a_review_pull_request`
7. `PipelineTests.test_real_repository_patterns_reach_their_targets_without_a_brain`

The failures concern compile-gate reporting, verified-test cache reuse, expected writer/oracle execution,
source-review routing, and unresolved coverage in the ring-buffer and real-repository list pilots. They remain
open; their assertions were not disabled or relaxed to obtain a passing release result.

The two compiler fixtures for sanitizer execution and damaged-profile merging now explicitly request the
model-free baseline suite. They test those compiler subsystems independently of first-iteration LLM planning;
their sanitizer and measurement assertions are unchanged.

## Remaining scope

The full release suite is not green. These changes address recovery control flow; they do not establish live
Qwen convergence or guarantee 100% of all five coverage metrics within three rounds. Typed validation diagnostic
routing and concolic/SMT-assisted generation remain follow-up architecture work. The existing release failures
need separate diagnosis before treating this branch as release-ready.
