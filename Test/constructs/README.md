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
  `structured`: answers in the structured form Qwen gives (the planning schema): a list of
  `{"function", "answer", "reaches", "contains", "evidence", "overload"}` - `answer` holds only the
  fields the construct needs (args with elements or fields, state, calls, writes, registers, members,
  stubs, variables, expected_exception ...), `reaches` the source texts whose lines the test must
  run, `contains` texts the built test must hold, `evidence` the evidence items a normative answer
  cites, `overload` the n-th overload of the name by line.

`test` checks the answers against the checks (NC-9); `test-full` runs the whole pipeline on every
case on Windows and on Linux (NC-4, NC-13); `calibrate BRAIN` runs the corpus with one real brain
(BP-8).

`corpus` (`unittest_generator.bat corpus [CASES]`) takes every `structured` answer through Agent1's
check, Agent2's assembly, Agent3's build and run, without a brain, in about a minute: each one must
be accepted, built, compile, pass and reach its lines.

## One rule for every failure

A failure of a run (a refusal, a test that does not compile, a gap that stays open) is first reduced
to the construct behind it - never fixed for the file it occurred in:

1. **The construct is covered here** (`corpus` passes for it): the generator handles it, so the
   failure is the brain's choice of values. No code changes; the brain gets the failure back as
   feedback (REFUSED, TRIED) and the next round decides.
2. **The construct is not covered**: add a minimal case with its correct structured answer first.
   `corpus` must fail on it. Then teach the generator the construct once - by the facts of the code
   (types, declarations, the front end), never by the names of one file - until `corpus` passes
   again for every case.

So every change of the generator answers a case here, and the cases are the finite list of what it
handles. Every false refusal is added as a correct answer, so that it does not come back.
