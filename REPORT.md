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
