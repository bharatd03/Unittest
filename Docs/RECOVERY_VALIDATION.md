# Recovery fixes: validation record

Validated on Linux on 2026-10-31. The implementation commits are `5bf330a` and `cfec7a3`, based on incoming main
`6c45292`. The latest component and release checks pass. No live remote model was contacted. The tests use scripted brains; the remote thinking-off request
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
- Baseline structural scenarios remain available alongside checked LLM proposals until measured redundancy.
  Rejected proposals cannot erase working fixture controls and call sequences.
- Coverage-only minimization preserves repaired observation tests and their inputs, identity and expectation-change
  provenance. The existing classification and oracle checks remain required.
- Compile-gate exclusions include the same per-test quarantine evidence as build-stage exclusions.

## Results

| Check | Result |
| --- | --- |
| Formerly failing release tests | All 7 passed individually and in the full run |
| Focused planner/repair-retention tests | 39 tests, passed |
| Component suite | 333 tests: 293 passed, 40 skipped |
| BaseAgent suite (unchanged; previous recovery run) | 227 tests: 212 passed, 15 skipped |
| Full release checks | 377 test invocations: 376 passed, 0 failed, 0 errors, 1 skipped |
| Follow-up of the skipped Make check | 1 test executed and passed, 0 skipped |
| Full-pipeline construct corpus | All 45 cases passed their existing expectations |

The component skips comprise 39 whole-pipeline tests and one Make-manifest check. BaseAgent skips optional live
model and browser checks. The full run skips the Make-manifest check. Corpus expectations include explicit
incomplete/unsupported outcomes: passing the corpus does not mean every construct reached 100% coverage.

The seven formerly failing tests were executed and passed in the full run; none was skipped there. The Make
check was subsequently enabled by registering the installed GNU Make 4.4.1 through `ut_setup.py ensure --repo`
for a Makefile project. `CaptureAndDependencyTests.test_make_dry_run_is_captured` then executed its real build
capture assertions and passed in a separate run with zero skips. Thus all 377 selected release-check invocations
have successful execution evidence across the full run and this follow-up. The original full-run result above
is preserved, including its one skip; it has not been rewritten as a zero-skip full run.

The seven release tests retain their original outcome and coverage assertions. The release run uses unchanged search budgets. The corpus's
single serial method was invoked separately for each case, in four worker processes, alongside the other release
tests. This adds 44 invocations to the usual 333-test count. New regression coverage checks that repaired
observation tests survive minimization despite overlapping coverage. Planner tests separately validate checked
model proposals and retained baseline facts under the additive-planning contract (REC-6).

Standard entry points, from the repository root:

```sh
bash Source/UnitTestGenerator/unittest_generator.sh test
bash Source/UnitTestGenerator/unittest_generator.sh baseagent-test
UT_TEST_WORKERS=4 bash Source/UnitTestGenerator/unittest_generator.sh test-full
```

## Resolved release failures

Each of these seven tests failed against an isolated archive of unchanged incoming main `6c45292` with the same
interpreter and compiler. All seven now pass:

1. `PipelineTests.test_a_brain_test_that_does_not_compile_costs_no_iteration`
2. `PipelineTests.test_a_second_run_reuses_what_the_first_verified`
3. `PipelineTests.test_a_wrong_brain_test_takes_its_values_from_the_run_when_no_source_change_exists`
4. `PipelineTests.test_c_pilot_reaches_full_coverage`
5. `PipelineTests.test_embedded_cpp_files_reach_their_targets_without_a_brain`
6. `RepositoryTests.test_source_fix_opens_a_review_pull_request`
7. `PipelineTests.test_real_repository_patterns_reach_their_targets_without_a_brain`

The fixes cover compile-gate reporting, repaired-test retention, and baseline scenarios lost during LLM planning.
The C pilot, verified-test cache and source-review fixtures now explicitly enable a whole-test writer profile,
matching their existing assertions about whole-test generation. Minimum-profile planning remains covered
separately; the template and real-repository pilots still run with their original minimum-profile fixture.
No coverage targets, compiler checks, oracle checks or source-review assertions were disabled or lowered.

The two compiler fixtures for sanitizer execution and damaged-profile merging now explicitly request the
model-free baseline suite. They test those compiler subsystems independently of first-iteration LLM planning;
their sanitizer and measurement assertions are unchanged.

## Remaining scope

The automated release checks pass. This does not establish live Qwen convergence or guarantee 100% of all five
coverage metrics within three rounds for arbitrary source code. Typed validation diagnostic routing and
concolic/SMT-assisted generation remain follow-up architecture work. Live model and Windows validation were not
performed in this Linux workspace.
