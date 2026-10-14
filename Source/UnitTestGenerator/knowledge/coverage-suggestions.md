---
name: coverage-suggestions
version: 1.0
used_by: [A4.suggest]
summary: How to reason, step by step, about a coverage item no test reached, and how to propose the smallest source change that makes it reachable.
---

# Source changes that make coverage items reachable

## Read the facts first
1. For every item: the metric, the line, the PATH CONDITIONS on the way to it and, for MC/DC, the condition vectors
   still missing. Write down which condition blocks it.
2. Read what was TRIED: scenarios, stub return sequences, coverage-guided search, other brains. A value range that
   was tried and failed tells you which inputs cannot produce the blocking condition.
3. Read the REQUIREMENTS that name the function. Quote them word for word; a requirement you cannot quote does
   not exist for this task.

## Decide the cause
1. condition_defect: the condition contradicts itself or the requirement (`x > 5 && x < 3`, `>=` where the
   requirement says "above"). The change is the condition the requirement states.
2. dead_code: no input can reach the code and no requirement asks for it. The change removes it.
3. required_defensive_code: a requirement demands the check, but a test cannot trigger it (a value the caller
   already limits). The change makes the input controllable: a parameter, or a call to a function a test can stub.
4. not_controllable: the code depends on something a test cannot set (a register read inline, a static
   initialised once). The change moves that read behind a function or a variable a test can set.

## Propose the change
1. The smallest change: one condition, one statement, or one function extracted. Never a new behaviour.
2. Every behaviour a requirement states stays the same; name the requirement it keeps.
3. original_fragment is copied from the SOURCE exactly once, without line numbers.
4. expected_effect names each item and the test that reaches it after the change, with its input values.
