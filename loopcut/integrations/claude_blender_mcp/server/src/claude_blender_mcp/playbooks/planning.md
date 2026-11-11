# Planning a Blender build

> When: read first for any request that takes more than a few tool calls. It explains how to plan, how to check each stage and which playbook to load next.

## 1. Pin down the target

Ask only about things that change the result, and assume sensible defaults for the rest. Say which defaults you picked.

| Question | Default if the user doesn't say |
| --- | --- |
| What is it for? A game engine (which one), a render, or 3D printing | Game-ready for Unity |
| Polycount budget | See the budget table in `game_character` or `hard_surface_prop` |
| Style: stylised or realistic, and any reference | Stylised, clean forms |
| Does it need to move? | Characters and creatures: yes. Props: no |
| Scale | Real-world meters. A human is about 1.8 m |

## 2. Pick the playbooks

| Request | Playbooks, in order |
| --- | --- |
| Match a picture the user gives (sheet, concept, photo) | `from_reference`, which pulls in the others |
| Character, creature or anything rigged for a game | `game_character`, which pulls in the others |
| Detailed sculpt (bust, creature, statue, rock) | `detailed_sculpt` |
| Weapon, vehicle, furniture, building piece | `hard_surface_prop` |
| Low-poly game mesh from an existing high-poly | `retopo_and_bake` |
| Rig or animate a mesh that already exists | `rig_and_animate` |
| Get something into Unity, Unreal or Godot | `export_to_engine` |
| A tool keeps failing or the result looks wrong | `troubleshooting` |

Load one with `get_playbook(name)`. Load only what the current stage needs.

## 3. Write the plan as stages

Each stage has a goal, the tools it uses and an acceptance check you can measure. Copy the checks from the playbook and adjust the numbers to the target. For example:

```
Stage 3: Low-poly game mesh
  Tools: duplicate, quad_remesh, cleanup
  Check: 8k-12k faces, 0 non-manifold edges, silhouette matches the sculpt in front/right/iso
```

For a big build, show the stages to the user in a few lines before you start, then keep going unless they object.

## 4. Run each stage in a loop

1. `checkpoint(label="before <stage>")`.
2. Act in small steps. Use dedicated tools first, and `run_python` only for what they don't cover.
3. Look: `capture_views(...)` from the views that show the change. Use wireframe for topology and xray for bones.
4. Measure: `mesh_report(...)`, `uv_report(...)` or `rig_report(...)`.
5. Compare with the acceptance check.
   - It passes: go to the next stage.
   - It fails: fix it, at most twice. If it still fails, `rollback(...)` and change approach. Don't keep patching a broken mesh.

Never call a stage done from tool output alone. A tool can succeed and still produce the wrong shape, so always look.

## 5. Rules that save time

- Keep sources. Duplicate the sculpt before retopology, and keep the blockout hidden rather than deleting it.
- Name objects for what they are, such as `Body_high`, `Body_low`, `Rig` or `Sword`. Later tools take names.
- Apply transforms before rigging, baking and export: `apply_transforms(name)`.
- Characters face -Y (toward the front view) and stand on Z = 0, with the origin at the feet.
- Run heavy steps once at the end, at full size: high voxel counts, 2k-4k bakes. Iterate at low settings.
- If the request is ambiguous, build the most likely reading, show it, and ask about that one point.

## 6. Finish with a report

Tell the user what is in the file (object names, polycounts, texture sizes, bone count, clips) and where exported files were written. Name anything that missed its target and why.
