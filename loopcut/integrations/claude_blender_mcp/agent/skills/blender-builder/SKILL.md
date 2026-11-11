---
name: blender-builder
description: Plan and build 3D content in a live Blender session through the blender MCP tools. Use for any Blender modeling, sculpting, retopology, UV/baking, rigging, animation or game-engine export request that takes more than a couple of tool calls, including building a model to match a reference picture.
---

# Blender builder

You drive a running Blender through the `blender` MCP server. Your job is to decide how to build what the user asked for, build it in stages, and prove each stage worked before moving on.

## Before you touch the scene

1. `get_scene_state` to see what's already there. Build on the user's objects rather than starting over.
2. `get_playbook("planning")` for the planning method, defaults and the map from request to playbook.
3. Load the playbook for this kind of build with `get_playbook(name)`. `list_playbooks` shows them all. Take the stage list, the tools and the acceptance numbers from it.
4. Settle the target: what it's for (engine and platform, render, print), the polycount budget, the style and the scale. Ask only about what changes the result. Otherwise pick the playbook's default and say so.

## The plan

Write the build as stages. Each stage has a goal, the tools it uses, and an acceptance check you can measure: counts from `mesh_report`, `uv_report` or `rig_report`, plus the views you'll look at. For a big build, show the stages to the user in a few lines, then start.

Choosing between approaches:

- **Dedicated tool or `run_python`?** Use the tool when one exists. It handles modes, selection and edge cases. Use `run_python` for anything the tools don't cover, with bpy.data and bmesh rather than operators.
- **Modeling or sculpting?** Hard edges and exact dimensions mean modeling (`hard_surface_prop`). Organic forms mean a blockout, then a sculpt (`detailed_sculpt`).
- **Game mesh?** Never ship the sculpt. Retopologize a copy and bake the detail (`retopo_and_bake`). Use quads for anything that deforms.
- **Matching a picture?** Use `from_reference`. Set the picture up as references. Model it by hand, the way a 3D artist would: blockout, then forms, then every visible detail, then real PBR textures. Use `image_to_3d` only if the user asks for AI generation. Judge likeness from side-by-side renders at 3/4 and side angles, not from the outline score alone. Finish with an honest review of what's missing.
- **Which rig?** `rig_template` for game bipeds, Rigify when the user will animate in Blender, `edit_bones` for extra limbs, UniRig only when the user has it installed on an NVIDIA machine (`rig_and_animate`).

## Running each stage

```
checkpoint -> act in small steps -> capture_views (look) -> report tools (measure) -> compare to the check
   pass -> next stage
   fail -> fix (at most twice) -> still failing -> rollback and change approach
```

- Always look. A tool can return ok and still leave a wrong shape. Use `capture_views` with the views that show the change, `wireframe` for topology and `xray` for bones inside a mesh.
- Keep sources: duplicate before destructive steps, and name objects `<Thing>_high`, `<Thing>_low`, `Rig`.
- Iterate cheap (low resolution, low face counts, 1k bakes), and do the expensive version once at the end.
- If a tool errors, read the message: it usually says what to fix. For repeated failures, `get_playbook("troubleshooting")`.

## Finishing

Report to the user:

- what's in the file, with object names, triangle counts, texture sizes, bones and clips;
- where exported files are;
- anything that missed its target, and why.

Keep the report short. The user can look at the scene.
