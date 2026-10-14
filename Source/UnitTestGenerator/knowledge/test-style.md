---
name: test-style
version: 1.0
used_by: [A2.tests]
summary: How a generated unit test is written with the ut_harness, so that every test reads the same way. A project replaces this pack with its own .ut/knowledge/test-style.md.
---

# Test style for ut_harness tests

## Shape of a test
1. One behaviour per test, in three parts: set up (stub values, local variables), one call of the function
   under test, then the checks. A sequence of calls only when the scenario is about state (a state machine,
   a counter); then each call is its own line.
2. The first line of the body is a comment: scenario id, one-line purpose and, for asserted values, the
   evidence ids, for example `/* S-brain-clamp_percent-2-1: values above 100 are limited to 100 [EV-clamp_percent-1] */`.
3. Statements only: no `#include`, no function definitions, no loops that wait, no files, no sleeping, no
   global variables of your own. Stubs are reset before every test, so no clean-up is needed.

## Names
- `test_<function>_<behaviour>`, lower case with underscores, at most 60 characters, for example
  `test_clamp_percent_limits_high_values`, `test_thermostat_command_reports_sensor_error`.
- A variant of the same behaviour gets a suffix `_2`, `_3`; never `test_1`, `test_new`, `test_case_a`.
- A rewritten test keeps its name.

## Checks
- Integer, enumeration and boolean results: `UT_EXPECT_EQ_INT(actual, expected);`
- Floating point: `UT_EXPECT_EQ_DBL(actual, expected, tolerance);` with the tolerance the evidence states,
  otherwise 1e-6.
- Strings: `UT_EXPECT_EQ_STR(actual, expected);` Pointers: `UT_EXPECT_TRUE(p == &var);` or `UT_EXPECT_NULL(p);`
- Write the evidence id after an asserted value: `UT_EXPECT_EQ_INT(clamp_percent(150), 100); /* EV-clamp_percent-1 */`
- A value no evidence states is observed, not asserted:
  `UT_OBSERVE_INT("<test name>_<n>", expression);` with a unique id per observation.

## Stubs
- Return value: `ut_stub_<name>_return = value;` before the call.
- One value per call: `ut_stub_<name>_returns[0] = a; ut_stub_<name>_returns[1] = b; ut_stub_<name>_returns_len = 2;`
- Call count and arguments are checked only when the evidence says how often or with what:
  `UT_EXPECT_EQ_INT(ut_stub_<name>_calls, 1);`, `UT_EXPECT_EQ_INT(ut_stub_<name>_<parameter>, value);`

## Example
```c
/* S-brain-thermostat_command-2-1: a sensor error stops the heater [EV-thermostat_command-1] */
ut_stub_read_sensor_return = SENSOR_ERROR;
UT_EXPECT_EQ_INT(thermostat_command(20, 2), -1); /* EV-thermostat_command-1 */
```

Not wanted:
```c
int r;                                  /* no purpose comment */
ut_stub_read_sensor_return = 5;
r = thermostat_command(1, 1);
UT_EXPECT_TRUE(r == 0 || r == 1);       /* checks nothing specific */
```

## A project's own style
Copy this file to `.ut/knowledge/test-style.md` in the project's repository, keep `name: test-style`, and describe
the project's naming, comment and check conventions there. It replaces this pack for that project. The harness
(ut_harness) and the checks of the pipeline stay the same.
