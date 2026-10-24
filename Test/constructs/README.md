# Construct corpus [NC-4, NC-9]

One small C or C++ example per construct the pipeline must handle. Each folder holds the source
(`src/`, `include/`) and `case.json`:

- `construct`: what the case is about, in one line.
- `sut`: the SUT file, relative to the folder.
- `expect`: the expected result of a whole pipeline run with the scripted brain of the tests:
  `final_state` (`verified_measured`, `incomplete`, or `any` while it is not yet recorded) and
  `statuses` (item or function -> the final status of LA-6 it must end with).
- `answers`: known correct answers that every check must let through (NC-9):
  `scenarios` (as the brain returns them to Agent1, A1-PL3), `tests` (as the brain returns them to
  Agent2, A2-21n), `setup` (building blocks of NC-1) and `fakes` (fake classes of NC-1).

`test` checks the answers against the checks (NC-9); `test-full` runs the whole pipeline on every
case on Windows and on Linux (NC-4, NC-13); `calibrate BRAIN` runs the corpus with one real brain
(BP-8).

Every pipeline limitation found in a run or a pilot is added here as a minimal case before it is
fixed, and every false refusal as a correct answer, so that neither comes back.
