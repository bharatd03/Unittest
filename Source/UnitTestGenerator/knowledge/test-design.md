---
name: test-design
version: 1.0
used_by: [A1.plan, A2.tests]
summary: How to choose test scenarios and input values for C/C++ unit tests, so that every run chooses them the same way.
---

# Test design: choosing scenarios and values

## Scenarios
1. One scenario closes one gap, or the gaps of one decision. Name the gap ids it targets.
2. Read the PATH CONDITIONS of a gap first: every condition on the way must hold, not only the last one.
3. Error paths are scenarios too: error codes, NULL checks, sensor or device errors, limits. They are often the
   behaviour the evidence states most clearly.
4. A gap listed under TRIED was not reached with those values. Change the value its path condition depends on;
   do not repeat a tried combination.

## Values
1. A comparison `x < LIMIT` or `x <= LIMIT`: use the boundary pair, one value on each side (LIMIT - 1 and LIMIT for
   `<`). For `==` use the value and one other value.
2. A range check: both ends, and one value just outside each end when the function handles it.
3. Prefer names that exist in the code over raw numbers: macros, enumerators, constants (`SENSOR_ERROR`,
   `MODE_ECO`, `LIMIT + 1`). Never an unexplained number such as 37 or 12345.
4. Enumerations: the first and the last enumerator; another value only when the function checks for it.
5. Pointers: NULL only where PARAMETERS says "may be NULL". Otherwise declare a local variable in setup and pass
   `&var`. Structures: an initializer that sets every field the path depends on.
6. MC/DC (`needs:` vectors): change exactly one condition between the two tests of a pair and keep the others
   fixed. With `a && b` the second condition is only evaluated when `a` is true; with `a || b` only when `a` is
   false.

## Stubs
1. When a decision depends on a stub's result, set that stub's return value.
2. Use a sequence of return values only when the function calls the stub more than once and needs different
   results per call (retries, polling, state machines).
3. Leave stubs that are not on the path alone; they are reset before every test.

## Expected values
1. An expected value comes only from EVIDENCE: copy the exact words into quotes and classify confirmed_gap.
2. Never derive an expected value by reading the function's code: the code is what is under test.
3. When no evidence states the result, classify characterization_gap and leave the expected value empty; the
   result is observed.
4. When two evidence items disagree, classify conflicting_evidence and name both ids.

## Example
Code under test:
```c
int thermostat_command(int setpoint, int hysteresis)
{
    int t = read_sensor(SENSOR_ROOM);
    if (t == SENSOR_ERROR) { return -1; }
    if (t < setpoint - hysteresis && hysteresis >= 0) { return 1; }
    return 0;
}
```
Evidence: `@return 1 to heat, 0 to stay off, -1 when the sensor reports an error` (E2).

Good:
- Sensor error: args setpoint=20, hysteresis=2; stub read_sensor returns SENSOR_ERROR; expected_return -1;
  quote "-1 when the sensor reports an error"; confirmed_gap.
- Boundary pair of `t < setpoint - hysteresis` (threshold 18): read_sensor returns 17 in one scenario and 18 in
  the other, setpoint=20, hysteresis=2. The evidence does not state the exact threshold, so both are
  characterization_gap with an empty expected value.
- MC/DC of `hysteresis >= 0`: keep t=17, setpoint=20, change only hysteresis: 2 and -1.

Not wanted:
- setpoint=37, hysteresis=12345: no reason, not a boundary.
- expected_return 1 "because line 5 returns 1": taken from the code, not from evidence.
- A scenario that sets a stub the function does not call.
