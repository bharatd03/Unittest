# Copilot instructions: unit-test generator

Repository layout: the application is in `Source/UnitTestGenerator`, BaseAgent in `Source/BaseAgent`,
the tests and pilots in `Test`, the requirements and other documents in `Docs`.

Read `.github/skills/ut-agent-builder/SKILL.md` before changing anything in the application. The
requirements in `Docs/unittest_generator_requirments.txt` are the contract; change them first, then the
code, then run `Source\UnitTestGenerator\unittest_generator.bat test`. Tools come only from
`Source/UnitTestGenerator/tools/toolchain.json` (`ut_setup.py`); agents have no tool fallbacks.
