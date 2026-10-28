# Unit-test generator: changes while the requirements file is frozen

The requirements file (`unittest_generator_requirments.txt`) is not updated until the core works. Changes are
recorded here and will go into the requirements file afterwards. New code cites no new requirement IDs.

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
