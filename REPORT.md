# Batch 1 Report: Swift Compatibility, Targeted Tool-Argument Repair, Portable Strata, and Verified Vision

## Summary of Changes

### Item 1: Real Swift Compatibility Suite
- **Files Changed**:
  - `loopcut/tests/test_swift_compatibility.py`
- **Caller that runs the code**:
  - `llm._read`, `agent._normalize_tool_calls`, `agent.py` streaming event loop
- **Test that proves it**:
  - `SwiftCompatibilityTest.test_streamed_tool_call_assembly`
  - `SwiftCompatibilityTest.test_multiple_tool_calls_streamed`
  - `SwiftCompatibilityTest.test_malformed_arguments_normalization`
  - `SwiftCompatibilityTest.test_stream_cancellation_stops_cleanly`
  - `SwiftCompatibilityTest.test_long_context_token_reporting`
- **Observable Check**:
  - Streamed piecewise SSE chunks assemble into complete `ToolCall` objects (`run_python`, `studio_action`) with matched indices and IDs.
  - Multiple simultaneous tool calls preserve distinct index ordering.
  - Mid-stream cancellation triggers clean `llm.Cancelled` exception without socket leaks.
  - 128k context prompt token usage is parsed into `completion.usage["input"]`.

### Item 2: Targeted Tool-Argument Correction
- **Files Changed**:
  - `scripts/addons_core/loopcut/tool_repair.py`
  - `scripts/addons_core/loopcut/tools.py`
- **Caller that runs the code**:
  - `tools.execute`, `agent.py` tool dispatch and message ledger
- **Test that proves it**:
  - `SwiftCompatibilityTest.test_targeted_tool_argument_validation_and_repair`
  - `SwiftCompatibilityTest.test_reply_text_never_executed_as_tool_call`
- **Observable Check**:
  - When a tool call is missing required arguments or provides mismatched types, `tool_repair.validate_tool_arguments` identifies the missing property and formats a targeted repair prompt embedding the active Blender scene state (e.g. `Mode: OBJECT; Active: 'Base_Chestplate' (MESH, 3 verts)`).
  - Code fences in plain chat replies are strictly ignored and never executed as tool calls.

### Item 3: Portable Strata Setup
- **Files Changed**:
  - `scripts/addons_core/loopcut/strata.py`
  - `scripts/addons_core/loopcut/settings.py`
- **Caller that runs the code**:
  - `strata.test_strata_connection`, `LOOPCUT_OT_strata_test.execute`, `settings.LoopcutPreferences.draw`
- **Test that proves it**:
  - `SwiftCompatibilityTest.test_portable_strata_path_resolution`
  - `SwiftCompatibilityTest.test_api_driven_strata_discovery`
- **Observable Check**:
  - Strata runner and config paths resolve dynamically from user preferences (`strata_run_script`, `strata_config_path`), environment variables (`STRATA_RUN_SCRIPT`, `STRATA_CONFIG`), or standard system fallbacks.
  - Connection testing queries `/health` and `/models` API to discover active model, context capacity (`max_context: 131072`), and backend status.

### Item 4: Verified Vision Capability
- **Files Changed**:
  - `scripts/addons_core/loopcut/strata.py`
  - `scripts/addons_core/loopcut/settings.py`
- **Caller that runs the code**:
  - `strata.verify_strata_vision`, `LOOPCUT_OT_strata_verify_vision.execute`
- **Test that proves it**:
  - `SwiftCompatibilityTest.test_verify_vision_capability_with_test_image`
  - `SwiftCompatibilityTest.test_verify_vision_capability_handles_server_refusal`
- **Observable Check**:
  - `verify_strata_vision` sends an active 1x1 test PNG base64 payload to `/chat/completions` to verify real vision interpretation rather than inferring readiness from a configuration file.
  - When vision succeeds, `vision_verified: True` is returned with the model's interpretation; when the engine was started without `--vision`, `vision_verified: False` is returned with server diagnostics.

## `./check.sh main` Last Line
```
CHECK GREEN
```

---

# Batch 2 Report: Task-Sized Menus, Pixal3D Refinement Workflow, and Add-on Recipes

## Summary of Changes

### Item 5: Task-Sized Tool Menus with Guaranteed Discovery Path
- **Files Changed**:
  - `scripts/addons_core/loopcut/studio.py`
- **Caller that runs the code**:
  - `studio.guide(task=...)`, `studio.task_tool_menu()`, `studio.action()`
- **Test that proves it**:
  - `Batch2ToolsPixalRecipesTest.test_task_tool_menus_guaranteed_discovery_path`
- **Observable Check**:
  - Task tool menus for `modeling`, `sculpting`, `uv`, and `export` return concise domain tools with guaranteed discovery anchors (`inspect_api`, `run_python`, `capture_viewport`) always included.
  - Calling `studio_guide(task="modeling")` returns formatted menu including `extrude`, `inspect_api`, and `run_python`.

### Item 6: Pixal3D Refinement Workflow
- **Files Changed**:
  - `scripts/addons_core/loopcut/studio_vendor/handlers/pixal3d.py`
  - `scripts/addons_core/loopcut/studio_playbooks/refine_generated.md`
- **Caller that runs the code**:
  - `pixal3d.pixal3d_refine`, `studio.action("pixal3d_refine", ...)`
- **Test that proves it**:
  - `Batch2ToolsPixalRecipesTest.test_pixal3d_refine_staged_repair_and_acceptance`
- **Observable Check**:
  - `pixal3d_refine` executes staged repair (`clean_loose`, `fix_non_manifold`, `solidify_thin_shells`, `symmetrize`, `quad_remesh`), measuring initial and final diagnostics (non-manifold edges reduced to 0, isolated debris islands cleaned, shell thickness protected >= 0.005, symmetry preserved), and returns measurable acceptance check results.

### Item 7: Verified Recipes for Installed Add-ons
- **Files Changed**:
  - `scripts/addons_core/loopcut/addons.py`
- **Caller that runs the code**:
  - `addons.get_recipe()`, `addons.recipes_for_installed()`, `addons.list_recipes()`, `addons.line()`
- **Test that proves it**:
  - `Batch2ToolsPixalRecipesTest.test_verified_addon_recipes`
- **Observable Check**:
  - Verified recipes for Zen UV, Quad Remesher, Hard Ops, MESHmachine, UVPackmaster, and Smart Remesh provide operator signatures, required modes (`EDIT_MESH` / `OBJECT`), context requirements, working Python examples, and native fallbacks.
  - `recipes_for_installed()` matches enabled add-ons.

## `./check.sh main` Last Line
```
CHECK GREEN
```
