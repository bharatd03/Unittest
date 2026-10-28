# Unit-test generator: changes while the requirements file is frozen

The requirements file (`unittest_generator_requirments.txt`) is not updated until the core works. Changes are
recorded here and will go into the requirements file afterwards. New code cites no new requirement IDs.

## 2026-10-28 (9): no HTML coverage pages (paths over 260 characters on Windows)

`llvm-cov show -format=html` wrote one page per source under `coverage/coverage/<the source's absolute path>.html`,
with the absolute path repeated inside the run folder. On Windows that is longer than 260 characters, and
`rmdir /s /q ut_tmp` at the start of `test` could not delete the folders the last test run left ("The system cannot
find the path specified"). No step, report or delivery reads the pages (coverage.json, coverage.lcov and mcdc.txt are
the measurement), so the call is removed: one llvm-cov run less per measurement. `test` deletes `ut_tmp` with the
`\\?\` prefix, so the folders an earlier version left are removed too.

## 2026-10-28 (8): refusals the code caused - false refusals, the schema without the type facts, repeated questions

### Why

The pilot-small run of 17:35 (commit 2cefff3, with the structured implementation) refused 57 brain answers. That is
above 5 % on 12 of 23 files, while coverage was 100 % on all but `ctrl.c`. By cause:

| Cause | Refusals | Whose |
|---|---|---|
| A check refused a correct answer | 14 | ours |
| A runaway number (`1000…`, 120 digits) | 16 | the model |
| The same scenario again, with unchanged facts | 15 | ours (asked again) and the model's |
| A wrong form (a structure as one text, a sentence as a value) | 8 | the model |
| An answer stopped (too long, a repeated block) | 4 | the model |

The 14 false refusals:
* `ring_buffer.hpp`: `0uL` for a `const std::int32_t &` was refused 8 times with "give its value".
* `motor_ctrl.cpp`: the injected interface was refused 6 times (already fixed in (6)).

### Change: three fixes, all independent of the model

* **A check never refuses a correct value.**
  * A reference to a scalar (`const T &`, `T &`) becomes a named variable with its value.
  * In C++ a scoped enumerator is cast for the state setter, which takes `long long`.
  * New test `test_every_value_form_of_every_target_is_accepted`, in C and C++. Every literal form within the type's
    range must be accepted, built and compiled for every kind of target: argument, reference, buffer element, field,
    state, member, constructor value, stub return, stub variable. The forms: `0`, `-1`, `+5`, `0u`, `1UL`, `0ull`,
    `1LL`, `0x1F`, `0X1fu`, `07`, `'a'`, `(1 + 2)`, `1 << 2`, true/false, floats, enumerators with every
    qualification. The check is tested by the grammar, not by the cases a run happens to meet.
* **The answer schema carries the type facts (`value_schema`).** It is sent as `response_format: json_schema`, so a
  server that supports it holds the model to it while it writes.
  * An argument entry per parameter:
    * an enumeration takes one of its enumerators, in every qualification;
    * a boolean takes `true`/`false`/`0`/`1`;
    * a number takes at most 32 characters (was 120);
    * a structure, buffer, pointer or injected interface takes no value, only its fields or elements (`NULL` when
      it may be NULL).
  * Object members the same way.
  * The corpus check now also requires that every correct answer fits the schema: a constrained decoder must be able
    to write it. That found five places where the schema was narrower than the checks:
    * nested and indexed fields (`bits.ready` of an unnamed struct in a union, `samples[1]`);
    * the class's own methods were not offered as calls;
    * a register given by base + offset (`PERIPH_BASE + 0x04u`) was not offered;
    * the qualified name of a stub or a stub variable;
    * a partly qualified enumerator.
* **No question with unchanged facts (A1-PL18).** The guard existed, but its fingerprint was the whole prompt. The
  prompt includes the record of the brain's own earlier answers (tried, refused, attempts, journal), which changes
  after every answer, so the guard never fired. The facts are now: the SUT version, the function, its open items,
  the evidence, the names it may use and the brains to avoid.
  * New test `test_unchanged_facts_are_not_asked_again_after_nothing_was_accepted`. It fails with the old
    fingerprint.

### Checked

* The pilot-small run's 112 recorded A1.plan answers, replayed through today's checks: 88 accepted, 24 refused (at
  run time: 57 refused, the repeats included).
  * `ring_buffer.hpp` 8 of 8 and `motor_ctrl.cpp` 16 of 16 are accepted now.
  * 23 of the 24 refused answers hold a value the schema no longer allows. A constrained decoder cannot write them as
    they were.
  * The 6 structures written as one text (`klist.c`, `ring.c`) can no longer be written that way: the schema allows
    only fields.
  * This does not make the other 17 right. A runaway number (`rate_limiter.cpp`, `ctrl.c`, `steering_monitor.cpp`)
    is cut at 32 characters and is still outside the type's range. These are the model's and stay refused, now with
    fewer tokens.
* The repeats cannot be replayed (they depend on the rounds); the next pilot-small run measures them.
* `corpus`: 39 cases and the value forms pass, every answer within the schema.
* The component tests here: 315 run; the 5 that fail need LangGraph or fail without this change too.

## 2026-10-28 (7): the construct corpus in the structured form, and one rule for every failure

### Why

Until now a failure was found in a pilot run and fixed for what that run showed. That is a chain without an end:
every run finds the next file-specific symptom. The construct corpus (`Test/constructs`) already held one minimal case
per construct, but its answers were in the older free-code form, so it no longer tested what Qwen answers.

### Change: the corpus answers like Qwen, one generic check, one rule

* Every case got `answers.structured`: the structured answers (the planning schema) that reach its construct.
* `CorpusStructuredTests` (one test, no brain, about 20 s here) takes each answer through Agent1's check, Agent2's
  assembly and Agent3's build and run. Each answer must be accepted, built, compile, pass and reach the lines the case
  names. `UT_CORPUS=a,b` runs only those cases.
* New command `corpus [CASES]` in both entry files and the README.
* The rule, in `Test/constructs/README.md`: a failure of a run is first reduced to its construct.
  * Covered (`corpus` passes): it is the brain's choice; no code change.
  * Not covered: add a minimal case with its correct answer first (it must fail), then teach the generator the
    construct once, by the facts of the code, never by the names of one file.

### What the corpus found, and the construct fixes

Of 32 cases, 26 passed at first. Seven new cases from the constructs of the medium pilots then found three more gaps.
Each fix is for a construct, not for a file:

| Construct (cases) | Cause | Fix |
|---|---|---|
| Constructor defined inline in a header (`cpp_ctor_pointer_class`, `cpp_ctor_ref_interface`, `cpp_interface_reference_result`, `cpp_nested_class`) | Only the SUT's functions were searched for constructors, so the object was `Logger ut_obj;`. | `layout.constructors`: the public constructors the classes declare (front end; copy and move left out). One helper, `constructor_of`, for the object rule, the rule-based tests and the recipe key. Their parameter types count for the injected interfaces too. |
| An interface method that returns a reference (`cpp_interface_reference_result`) | No fake was written (A2-21aa: "not generated"), so only a brain could write one. | The fake returns a reference to its own `<method>_return` member. Only a reference to another interface (an abstract class) is still left out. *Deviation from A2-21aa, to go into the requirements.* |
| A `.hpp` header-only SUT with C-compatible content (`cpp_header_only`) | It was taken as C (by its content), and its inline functions were not measured: the linker kept the test file's uninstrumented copy. | `.hpp`, `.hh`, `.hxx`, `.ipp`, `.tpp` are C++ by name; `.h` and `.inl` are still decided by content. For a header SUT the test suite is instrumented too, so whichever copy the linker keeps is measured. |
| C code built as C++, `unsigned char *` (`c_as_cpp`) | The rule tests passed `""` and `char[256]`, which C++ does not convert. | The literal is cast to the parameter's type, and the buffer has the parameter's element type. |
| One name, two overloaded stubs (`cpp_overloaded_dependency`) | The stub value set only the last overload. | A stub name sets every overload of that name. |
| A struct parameter with an array field (`c_struct_param_array_field`, new) | `samples[1]` was refused: "not a field". | A field path may index an array field within its length (`samples[1]`, `pts[0].x`); an index out of range is refused. |
| A stubbed variable of a namespace (`cpp_namespaced_enum_stub_variable`, new) | It was defined and reset outside its namespace, so it did not link. | Defined inside its namespace and reset by its full name. |
| A constructor that validates its arguments (`cpp_ctor_throws`, new; `SteerLimiter` of chassis_ctrl) | The object was always built with `{}`, so every method test threw at construction. | The class's constructor is listed in CALLABLE FUNCTIONS. Named in `calls`, its values build the object (`SteerLimiter ut_obj(30)`, typed like every value). |

The other new cases passed without a change:
* `cpp_uint8_buffer`
* `cpp_template_out_of_line`
* `c_struct_array_state`
* `c_accumulated_timer`

STAGE_A_VERSION 4: cached Stage A results without `constructors` are computed again.

### Checked

* `corpus`: 39 of 39 cases built, compiled, run and reaching their lines (27 s).
* The component tests here: 313 run; the 5 that fail need LangGraph or fail without this change too.
* `test_a_fake_implements_an_injected_interface` updated (a reference result is faked; a reference to an
  interface is not).

## 2026-10-28 (6): constructors and injected interfaces in structured scenarios - from the pilot-small run

In the pilot-small run (23 files, 27 min 56 s), the brain calls went almost all to one place: the requirement
checks (`EVIDENCE:<function>`) of files that already had 100 % coverage after their first round. That round is
intended: it checks the documented behaviour (A1-25y, within `after_target_seconds`). But many of its questions
could never become a test:
* **Constructors** (`MotorCtrl::MotorCtrl`, `RateLimiter::RateLimiter`, `TorqueMap::TorqueMap`,
  `SteeringMonitor::SteeringMonitor`): 18 of about 90 planning calls on those four files. The generator built no
  scenario of a constructor (it built objects only to call a method on them). So the plan was asked again until its
  attempts were used.
* **An injected interface parameter** (`IEncoder& enc`): every value was refused, the name of the generator's fake
  (`ut_dep0`) too. That was 6 of `motor_ctrl.cpp`'s 12 refusals.

Change:
* A constructor scenario is built: `Class ut_obj(args);`, with the class template's instance (`test_instances`).
  * A stated exception becomes `UT_EXPECT_THROW(([&] { Class ut_obj(args); }()), type);` (STR-1 of the chassis
    pilot: a rate of 0 is refused with `std::invalid_argument`). The lambda in parentheses keeps a template's commas
    in one macro argument.
  * A characterization scenario of a constructor that throws observes the exception (A3-45e).
  * A normative one asserts its `expected_after` on the object.
* An injected interface parameter takes the generator's fake (the type decides, like every value):
  `ut_fake_<Interface> ut_<param>;`, passed by reference or address.

Checked:
* `test_constructors_and_injected_interfaces_are_built` (`SteerLimiter(0)` throws; `BrakeBlender` gets
  `ut_fake_IDiagSink`).
* The C++ answers of the pilot-medium run, replayed: every accepted scenario is built now (55, none left out; before,
  4 were not built), and 2 do not compile (values the checks marked as predictions).
* 30 test classes pass except the one that fails in this container without the change too.

## 2026-10-28 (5): every structured value is checked by the type of what it sets - from the pilot-medium run

### What the run showed (13:21, commit 328430c)

| File | Coverage | Brain plans refused | Brain tests not kept |
|---|---|---|---|
| `climate_ctrl.c` | LC 88.3 %, MC/DC 53.4 % | 194 of 300 (64.7 %) | 102 of 102 |
| `chassis_ctrl.cpp` | LC 71.9 %, MC/DC 39.0 % | 43 of 102 (42.2 %) | 37 of 41 (36 did not compile) |

The brain added almost nothing after the rule round. The causes were in the structured lowering, not in the brain:
* **C:** 170 of the 194 refusals were "state s_mode.s_mode: not a variable": for a plain variable the brain repeated
  its name as the field. An array field was assigned as one value (`ut_out.mix_pct = [0, 0, 0, 0]`).
* **C++:** a plain argument was checked against its type, but a value for a member, state, a field, an element, an
  earlier call or a stub variable was not. So these reached the compiler:
  * a huge integer for an `int16_t` member;
  * a private member's name as a value (`implausible_`);
  * a constant only the SUT file defines (`kPumpMaxOnMs`);
  * `Mode::Service` without its namespace;
  * `g_supply_mv` without `hal::`.
* **C++:** the arguments of a member function were never checked at all (`if not fn.member`).
* **C++:** `MovingAverage ut_obj;` without its template arguments: the template fact covered header SUTs only.
* **C++:** `const std::uint8_t *` had the kind "other" (std:: was not read as the builtin). So the search could not
  generate `crc8`'s data, and a buffer's elements were refused.

### Change: one rule, the type is the fact

* `typed` in `_lower_structured`: every value is handled the same way. First the rest of the JSON is cut off. A
  name only the SUT file sees becomes its value (A1-PL2a). An enumerator or a namespaced name is written in full
  (A2-21ai). Then the value must be one C expression and pass the check of its target's type (`_value_problem`, the
  check plain arguments get: range, form, known names). The target's type is a fact (`target_param`, through the
  typedefs).
* It applies to argument elements and fields, state (index within the array's length), earlier calls, stub
  writes, stub variables (with their full name), registers (32 bits) and members.
* The form follows the type:
  * a plain variable takes no index or field;
  * an array field takes a list, element by element;
  * a structure argument given as a value is its initializer (zeroed when no field is given);
  * a container member takes a list, a scalar member one value.
* The arguments of a member function are checked like any other (all parameters named, each value by its type).
* `integral_constants`: C/C++ `const` and `constexpr` integers are constants of the source, like integer macros
  (A1-PL2a, `_constants_for_values`).
* `test_instances`: the instantiation a test uses of a class template, for every SUT. A header SUT uses its own
  templates. Any other SUT uses the templates its headers define, with the arguments the SUT's code uses
  (`MovingAverage<std::uint16_t, 4>`). The object rule, the parameters, calls and the member accessors all use it.
  The explicit instantiation in the access file stays for header SUTs only.
* Front end: `std::uint8_t` is the builtin `uint8_t` (and so on), so `const std::uint8_t *` is a buffer. The Stage A
  cache version is now 3, so a cached layout with the old kinds is not reused.

### Checked: Qwen's own answers of that run, replayed (A1's checks, the generator, the compile gate)

| | Plans kept | Tests built | Do not compile |
|---|---|---|---|
| C, in the run | 106 of 300 | | 8 (the rest failed or added nothing) |
| C, replayed with the change | 222 of 300 | 220 | 2 |
| C++, in the run | 59 of 102 | 41 | 36 |
| C++, replayed with the change | 54 of 102 | 51 | 2 |

* The plans still refused are right to be refused: numbers outside the type, JSON in a value, NULL where the
  function does not check it, parameters left out.
* The tests still not compiling are values the checks marked as predictions; the compiler decided (NC-7).
* Whether the kept tests reach new items can only be measured by a pipeline run.
* New: `TypedValueTests` (C and C++, from the slips of the run). Changed:
  `test_a_scenario_refused_only_by_predictions_is_tried` gives the method its argument.
* 28 test classes (209 tests) pass except one that also fails without the change in this container. The pipeline
  tests were not run here.

## 2026-10-28 (4): steps 5, 7 and 8 - a decision instead of a rewrite, short answer fields, dead code removed

### Step 5: a failing asserted test is decided, then a rule acts - no brain rewrites it

Before, a failing test with E1-E7 evidence was classified (the rules, then the brain: source or test?). When the
test was judged wrong, or Agent4 found no source change (A4-23j), Agent2's brain rewrote it as code. In 17 of 143
runs the rewrite was the same as the failing test, 73 times ("the rewrite is the same as the failing test"), and the
file stopped without progress (`ctrl.c`, 20261028-113342).

Now the decision stays, and its follow-up is a rule (`observed_instead`):
* **test judged wrong** (an assertion failed): the test keeps its inputs. Its assertions become observations, and the
  observe run and finalize give them the run's values. That is the reference form of the oracle check (A3-85b): the
  change is accepted only when the test detects every mutant the reference detects, and it is reported as a change
  of expectation. The values now come from the run, so the test is E8. The requirement item stays open and is
  planned again;
* **crash or timeout**: its inputs miss. The test is dropped, and its items are planned again with the reason.

The compile gate (A3-CL15) asks the brain to write a test again only for a brain whose profile writes whole tests.
For Qwen, the compiler's message goes into the journal and the next plan changes the scenario.

### Step 7: short, closed answer fields (ARCH-20c)

The planning schema now carries the limits its checks enforce anyway:
* a value has at most 120 characters, an element, field or return value at most 60;
* at most 12 earlier calls, 16 return values per stub, 64 elements, 32 fields, 8 expected checks and 4 quotes;
* at most as many arguments as the function has parameters.

A decoder that follows the schema stops a runaway answer ("0uLL, 0uLL, ...") where it starts. The checks stay the
judge. The schema grows by about 100 tokens.

### Step 8: removed, by the evidence of 143 runs

Counted in every report, log and brain-answer file of the validation runs:

| Repair or check | Fired | Action |
|---|---|---|
| "the rewrite is the same as the failing test" (A2-21p) | 73 times in 17 runs | removed with the rewrite (step 5), with `normalized_body` |
| "a value set twice in a row" (A2-21t, a prediction) | never | removed |
| the rewrite context, task text and name reservation in Agent2 | dead after step 5 | removed |
| `parse_suite`'s own brace count | | now the shared lexer |
| A1's value clean-up `fix_value` | 221 times in 71 runs | kept |
| A1's checks of a plan (A1-PL4) | 48 times | kept |
| predictions built in a trial (NC-7) | 97 times in 28 runs | kept |
| Agent2's free-code repairs: "checks nothing", unstated expected values, namespaces, void values, shared set-up | fired only for Qwen's free-code tests, which no longer exist | kept for the full-profile writer - see the decision below |

**Open decision:** the whole-test writer of Agent2 now serves only full-profile brains (Opus and the others, without
access in these runs). Removing it removes its repairs too (about 400 lines: `_llm_tests`, `_grounding`, the shared
set-up of a suite part, the kinds setup, stub, test and suite_part). Then no brain writes code anywhere.

### Checked

* New and changed tests: `test_a_failing_e2_test_judged_wrong_takes_its_values_from_the_run` (the observation, then
  the reference form after finalize, and a crash dropped); `test_well_formed_bodies_pass_the_body_check`. The A4-23j
  pipeline test now expects the inputs kept and the value from the run (sensor 18 gives 0). The tests that need
  Agent2's writer use a full-profile brain (`writer_brain`, which also patches the profiles of `ut_config.json`).
* 25 test classes around the change (197 tests): all pass except one that also fails without the change in this
  container. The pipeline tests (which need LangGraph) were not run here.

## 2026-10-28 (3): structured A2 - Qwen3 8B never writes a test as code

Step 3 of the plan from fact-based instead of guess-based (project doc
`unittest-generator-guess-vs-fact-analysis-2026-10-28.md`).

### Where free code came from

Agent2's brain wrote whole tests as C code in four cases:
* a requirement item (`EVIDENCE:<function>`) whose structured plan failed its checks: the rule left a placeholder
  scenario "to the test writer" (A2-21n);
* a checked structured scenario the generator could not build;
* a characterization scenario without a rule test (A2-21o);
* the profile ladder (BP-3): for the minimum profile, every set-up cause climbed straight to the kind `test`.

These produced most of A2's refusals: "checks nothing", invented setters, names only the SUT file sees, and the
same refusal in every round (`uart_put`, `RingBuffer::push`, `MotorCtrl::sync`, `body_ctrl.c`).

### Change

* **The minimum profile (Qwen3 8B) has no kind `test`.** Its kinds are values, scenario, call_sequence (the
  structured `calls`), repair, decision and file_edit. The ladder ends at the structured forms; a cause it cannot
  answer is reported as needing a wider profile (BP-9), not answered with free code.
* **Agent2 writes a whole test only for a brain whose profile has the kind `test`** (the full profile). For the
  others, a scenario the generator cannot build is not built. The note "<function>: not built (<reason>) ..." goes into
  the journal, and Agent1 plans the item again with that reason, within its attempts. For Qwen, every new brain test
  now comes from a structured scenario, built and checked by the generator.
* **A stated exception is a structured field** (`expected_exception`, C++). It is offered only when the function or
  a function it calls throws. The brain picks the type from EXCEPTIONS, a list read from the source's throw
  expressions, and the generator writes `UT_EXPECT_THROW(call, type)`. A type the function does not throw is left out.
  With an exception, the expected return value is left out (a call that throws returns nothing).
* Not changed: the rewrite of a failing asserted test (A2-21p) still asks the brain for code. It becomes a decision
  in step 5.

### Checked

* New tests:
  * `test_a_structured_brain_never_writes_a_whole_test`: with the minimum profile, Agent2 is not asked, the rules
    still work, and the note goes to the journal;
  * `test_a_stated_exception_is_planned_and_asserted`: `MotorCtrl::step` with a target out of range gives
    `UT_EXPECT_THROW(ut_obj.step(100001), std::invalid_argument);`.
* The tests of Agent2's writer now use a brain with the full profile (`writer_brain`), the only one that still gets it.
* 23 test classes around the change (192 tests): all pass except one that also fails without the change in this
  container. The full test suite was not run.

## 2026-10-28 (2): one fact instead of several checks - well-formed C and class templates

### What happened

* `pilot-embedded`, `src/gpio_driver.c` stopped with CONTRACT_VIOLATION "suite braces are unbalanced". Qwen wrote the
  rest of the JSON into a value (`"c": "0x42'}], "`). The value check called it a prediction, so the scenario was
  kept as a trial "for the compiler to decide". The test check counted braces with a regular expression and also
  called the result a prediction. The suite check counted braces the same way, but as a hard error, and stopped the
  file before the compiler ever saw it.
* `include/ring_buffer.hpp`: the tests written from a structured scenario declared `RingBuffer ut_obj;` and `T ut_v`
  although the instantiation `RingBuffer<std::int32_t, 4>` was known. Only the generator's rule tests used it, so
  the same function failed to compile in every round. The brain had also put member values into fields of the
  `const T &` parameter, which was accepted because it is a reference.

### Root cause

The same fact was decided in several places, in several ways:
* "is this text well-formed C?": a regular expression in `code_only`, brace counts in the generator's test check,
  the analyzer's set-up check and the suite check;
* the template instantiation: computed in the generator only.

### Change

* **One lexer for C text** (`ut_contracts.c_lex`): comments, string and character literals (prefixes, escapes, raw
  strings, digit separators `1'000`) and nested brackets, as the compiler reads them. `code_only`, the test check,
  the set-up check and the suite check all use it. Its result is a fact (a guard), no longer a prediction, so a test
  that is not well-formed C is refused where it is made and never reaches the suite. The suite check stays, as a
  check of the generator itself. The regular expression `CODE_NOISE` and the prediction "unbalanced" are removed.
* **A brain value is one C expression** (`c_expression_problem`): well-formed, no `;`, no assignment, and no braced
  list for a scalar. A value followed only by JSON punctuation is cut where C ends (`0x42'}], ` -> `0x42`,
  `without_json_tail`). Anything else is refused with the reason. This applies to arguments, elements, fields, state,
  stub writes, registers and members.
* **The class templates of a header SUT are one fact in `ut_contracts`** (`class_templates`, `template_args`,
  `instantiated_class`, `instantiated`). The generator's copy is removed. The rule tests, the object of a structured
  scenario (`object_recipe`) and the parameters a scenario is checked against all use it.
* **Elements and fields only where the type has them:** elements for a pointer (never a reference), fields for a
  structure whose fields are known (field names checked). For a scalar parameter with a value, stray elements and
  fields are left out (the type is a fact). For a scalar member, a list becomes its one value.

### Checked

* The brain's answers of the two runs, through the changed checks, with the C++ front end:
  * `RingBuffer::push`: the object is `RingBuffer<std::int32_t, 4> ut_obj;`, `v` is `0uLL`, and `count_ = 4` is set
    through the member accessor, so the test reaches the full buffer;
  * `uart_put`: `0x42'}], ` becomes `0x42`, and `0x42'}]x` is refused as a guard.
* The test classes for contracts, the analyzer, the generator, the embedded pilots, brain planning and generation,
  and the pilot fixes (132 tests). The full test suite was not run.

## 2026-10-28: medium pilots, namespaces, structured scenarios

### Medium pilots (`pilot-medium`)

| Pilot | File | Lines | What it exercises |
|---|---|---|---|
| `c_medium_pilot` | `src/climate_ctrl.c` | about 740 | macros only the file defines, timers accumulated beyond `uint16_t`, an array of structures as state, buffers and structures as parameters, a register read and written through a pointer macro, a stub that writes into a buffer (`nvm_read`), CRC-protected CAN frames |
| `cpp_medium_pilot` | `src/chassis_ctrl.cpp` | about 500, plus 230 in headers | calls into several namespaces (see below) |

The C++ pilot's namespaces:
* `hal::adc`, `hal::gpio` and `drv::can` each have a function named `read`;
* `hal::pwm::set` is overloaded and called through a namespace alias (`namespace io = hal::pwm`);
* `hal::now_ms` and the namespaced variable `hal::g_supply_mv`;
* `drv::can` is written as a C++17 nested namespace, and `using drv::can::Frame` is a using-declaration;
* a `using hal::adc::read;` sits inside a block;
* `svc::IDiagSink` is an injected interface;
* a C library with C linkage (`extern "C"`);
* the SUT's own namespace has nested namespaces, an inline namespace (`v2`) and an unnamed namespace.

Without a brain, Stage A and the rule round both pass on Linux:
* C++: 528 tests, line coverage 70.7 %;
* C: 995 tests, line coverage 84.8 %.

### Namespaces

* **The large C++ pilot.** The stub failure of `powertrain.cpp` (run of 2026-10-25) came from two entries for `hal::pwm`. One was the rule's. The other was a declaration the brain had proposed, with an invented type. The code of that run was older; the current code finds the declaration in the header, refuses invented types, and keeps the namespace out of the return type. With the current code Stage A passes in round 1 (5558 rule tests, line coverage 67.8 %).
* **Declarations inside their own namespace.** A declaration missing from the dependency plan is now looked up inside its own namespace, written either as nested blocks or as `a::b`. Of several overloads, the one whose number of parameters matches the linker's symbol is chosen. Before, the first declaration with the same short name anywhere in a header was used, which is wrong for `hal::gpio::read` versus `hal::adc::read`.
* **One definition per stub.** A second entry for the same stubbed function is no longer rendered, so it can no longer define the function and its controls twice.

### Structured scenarios: the brain says what, the generator writes how

The planning answer no longer contains free C code. The `setup` field is gone from the schema; it is still read in answers in the older form. The brain fills these fields, and Agent1 writes the code from them with fixed rules:

| Field | The brain gives | The generator writes |
|---|---|---|
| `args[].elements` | the values a buffer, array or pointer parameter points to | `T ut_<param>[n] = {...};`, passed by its name |
| `args[].fields` | the fields of a structure parameter | `T ut_<param> = {0}; ut_<param>.f = v;`, passed by value, by address or by reference as the parameter requires |
| `state` | variable, index (-1 if not an array), field ("" if not a structure), value | `ut_state_set_<var>(v)`, or a generated element setter `ut_state_set_<var>__<field>(i, v)` that checks the index |
| `calls` | a function from CALLABLE FUNCTIONS and its arguments | the call: a static function through its wrapper, a method on `ut_obj` |
| `writes` | stub, output parameter, elements or fields | the stub's output controls `ut_stub_<stub>_<param>_out` / `_out_len`; the stub copies them into the parameter, never more than its size parameter or the referenced object |
| `registers` | address from REGISTERS, value | `UT_REG32(address) = value;` |
| `members` (C++) | a data member of the object, private ones included; elements for a container | `ut_obj.*ut_member(ut_tag_<class>__<member>{}) = value;`. The accessor uses an explicit template instantiation, which is legal C++ and ignores access. It is written only for members the tests use. |

The names come from closed lists built from the front end and shown in the facts:
* SUT STATE, with the length and fields of arrays and structures;
* CALLABLE FUNCTIONS;
* STUB OUTPUTS;
* REGISTERS;
* OBJECT MEMBERS.

A name that is not on a list is refused with that reason. The facts describe a target's state as "STATE TO SET: var = value".

Front-end additions to the layout:
* `StateVar.array_len` and `StateVar.fields`;
* `LayoutProfile.class_members`: scalar, enumeration and `std::array` / `std::vector` members whose type a test can spell.

The Stage A cache key carries a version (2), so earlier cached results without these fields are not reused.

### Checked

* The structured form, on the C medium pilot with a scripted brain (no real brain): buffers, a structure output argument, an element setter of an array of structures (`s_faults[3].counter`) and a stub writing into a buffer (`nvm_read`) were all written by the generator, built and run. All 21 tests written from the scripted answers were kept, and the second round covered 80 more items.
* The rule rounds of both medium pilots and of `powertrain.cpp`, without a brain.
* The full test suite was not run.

### Not done yet

* Constructor arguments of the object under test (the rule passes `{}`; `SteerLimiter` throws on 0).
* A value computed by a call (`seed_key(0u)`).
* Failing asserted tests that loop between the root-cause brain and Agent4.
