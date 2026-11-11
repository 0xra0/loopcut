# Working on Loopcut

Loopcut is a Blender 5.2 fork; the AI add-on lives in `scripts/addons_core/loopcut`, its unit tests in
`loopcut/tests`, its real-Blender checks in `loopcut/harness`. `loopcut/DEVELOPING.md` explains the
agent loop, tools and workers; read the section for the part you change before changing it.

## Done means `./check.sh` is green

Run `./check.sh` before every commit (for a branch: `./check.sh main`). It checks the rules below on
the lines you added, registers the add-on in headless Blender, and runs the full unit suite. Paste
its last line into your report. A change is done when it prints `CHECK GREEN`, and only then.

When a flagged line is right as written, end that line with `# check: ok <reason>`. A person
reviews every such line, so the reason has to convince them.

## How to take a list of tasks

- Work in batches of 3 to 5 items, one commit per batch, on a branch.
- Each item has an observable check, such as "a level-3 subsurf cube reports 384 faces". Write the
  test for it first, see it fail, then make it pass.
- For each batch, write `REPORT.md`: per item, the files changed, the caller that runs the new code,
  the test that proves it, and the `check.sh` last line. The reviewer reads this report and the diff,
  so a claim missing from it counts as not done.
- When an item cannot be done as asked, say so in the report and leave it out.

## Rules (each one was broken before, and cost a review)

**Wired.** New code runs from a real caller: a tool, an operator, the agent loop. Name the caller in
`REPORT.md`, and test through it. A module that nothing imports is not a feature, however good its
docstring.

**Main thread for bpy.** The agent turn runs on the `loopcut-turn` thread. Everything that reads or
writes `bpy` runs on Blender's main thread, through `_on_main` in `agent.py`. Read preferences and
scene state there too, and pass the values to the thread.

**Errors travel.** Catch the specific errors you expect and handle them. `tools.execute` lets
`TimeoutError` and `llm.Cancelled` pass through: the user's Stop button depends on it.

**Replies are text.** The model runs code only through tool calls it makes itself. Code in a chat
reply is shown to the user, never run, however much it looks like a tool call.

**Cheap per message and per redraw.** Code that runs on every message (`scene_context.py` and what
it calls) reads meshes in bulk
(`foreach_get` into numpy); a generated mesh can have hundreds of thousands of faces. `draw()` shows
stored values; an operator does the slow or network work and stores its result.

**Only what the user set.** Settings, project constraints and defaults hold what the user chose or
what Blender reports. Leave a value empty rather than guess an engine, a budget or a path.

**Claims match code.** A docstring, preference description or report says what the code does today.
A setting that does nothing yet is left out until it does something.

**The user approves.** Running code, writing files outside the allowed folders, enabling add-ons
and spending credits go through the approval the add-on already has. Keep every permission flag of
an external tool at its asking default.

## Tests

- A test fails if the function under test returns `None`: assert on the value, not that it ran.
- Fake `bpy` per test: patch `sys.modules["bpy"]` in `setUp` (see `FakeBpyTest` in
  `loopcut/tests/test_local_ai_and_advanced_suite.py`). The real `bpy` module is importable on this
  machine, so a global fake leaks into other tests.
- Known failures, allowed by `check.sh`: `test_beta_servers_check` (production 404 page) and
  `test_hosted_prompts` (cached index).
