# Review of feature/local-ai-refinements: what is left for agy

Already fixed, don't redo:
- the `bake_maps` NameError
- `pixal3d_refine` editing the active object, running only once, and reporting thickness and
  modifier results it never measured
- the GPU leases, removed again
- task menus that listed commands which don't exist
- the project-memory sidecar beside the .blend (removed; memory lives in the .blend only)

Fix the items below as one batch, following AGENTS.md. Every fix needs a test that fails first. A
Blender behaviour belongs in `loopcut/harness/smoke_check.py`, since the unit tests' fake bpy can't
show it. Finish with `./check.sh main` printing CHECK GREEN.

## Should fix

1. **The add-on recipes in `addons.py` have no caller, and their check is always true.** Nothing
   outside `addons.py` calls `get_recipe`, `recipes_for_installed`, `validate_recipe` or
   `get_verified_recipes`.
   - In real Blender, `hasattr(bpy.ops.uv, "zenuv_unwrap")` is True even when the operator does not
     exist, so `operator_available` is always True.
   - `rna_valid` is True on both branches.
   - The signatures, such as `qremesher.remesh(target_count=..., adaptive_size=...)`, were never
     checked against the real operators.

   Either delete the recipes, or do all three of these:
   - check existence with `op.get_rna_type()`, which raises for a missing operator;
   - compare each signature argument with `get_rna_type().properties`;
   - call the check from `inspect_api`.

2. **These functions have no caller.** Wire each one into a real caller, or delete it together with
   its tests:
   - `project_memory`: `record_completed_stage`, `get_completed_stages`, `is_stage_completed`,
     `resume_pipeline`, `set_target_export_format`, `record_failed_approach`,
     `should_avoid_approach`
   - `tool_repair`: `validate_tool_arguments`, `format_tool_error_with_scene`
   - `strata.is_strata_provider`, whose only caller was the removed GPU lease

## Minor

3. `LOOPCUT_OT_strata_verify_vision` blocks Blender for up to 20 s on the main thread. Its check
   `"red" in reply` also passes for "not red".
4. `tools.py` moved `from . import studio` and the other imports into the middle of the module
   without saying why. Move them back unless a circular import needs this, and if one does, say
   so in a comment.
