# Game-ready rigged character

> When: a character or creature that has to be animated in a game engine. This runs the whole pipeline from blockout to engine file, and points to the other playbooks for detail.

## Targets

| Platform | Triangles | Textures | Bones | Influences per vertex |
| --- | --- | --- | --- | --- |
| Mobile | 3k-10k | 512-1024 | 20-40 | 4 (2 on low-end) |
| PC/console, crowd or NPC | 10k-30k | 1024-2048 | 30-70 | 4 |
| PC/console, hero | 30k-80k | 2048-4096 | 50-150 | 4 (8 in Unreal if needed) |

The defaults are the NPC row, Unity and 1.8 m tall.

## Stages

### 1. Blockout (proportions)

- Build from primitives with `create_primitive(...)`, `transform(...)` and `mesh_edit(...)`. Use a MIRROR modifier via `add_modifier(name, "MIRROR")` for a symmetric body.
- Pose: T-pose (arms straight out along ±X), facing -Y, feet on Z = 0. That matches `rig_template(...)`, so the rig fits without edits.
- Leave space between the legs and between the arms and torso. Parts that touch fuse during remeshing and bind badly.
- Check: front and right views show the right proportions (a head is about 1/7 to 1/8 of the height for realistic, 1/3 to 1/5 for chibi).

### 2. Sculpt (high-poly)

Follow `detailed_sculpt`. Name the result `<Name>_high`. Stop at the level of detail the texture size can hold. A 1024 map won't show pores.

### 3. Low-poly game mesh

Follow `retopo_and_bake` stages 1-2: `duplicate(...)` the sculpt to `<Name>_low`, then `quad_remesh(...)` to the face budget.
- Use quads for deforming meshes. Don't `decimate(...)` a character.
- Check: `mesh_report(...)` shows the triangle count within budget, 0 non-manifold edges and 0 loose geometry. Elbows, knees and shoulders need at least 3 edge loops each. Check with a wireframe `capture_views(...)`.

### 4. UVs and bake

Follow `retopo_and_bake` stages 3-5. Bake NORMAL and AO from `<Name>_high` onto `<Name>_low`, plus DIFFUSE if the sculpt has vertex or material color.

### 5. Material

`create_material(...)` with the base color, then `set_material_texture(...)` for any baked maps that `bake_maps` didn't already connect. Skin: subsurface_weight 0.1-0.2, roughness 0.5-0.6.

### 6. Rig, skin, test

Follow `rig_and_animate`. In short: `rig_template(fit_to=...)`, check joints in xray, `edit_bones(...)`, `bind_mesh(...)`, `pose_test(...)`, then `limit_weights(limit=4)`.

### 7. Animation clips

`procedural_cycle(...)` for walk, run and idle, with `push_to_nla(...)` after each one so it exports as its own clip. Use `keyframe(...)` for custom moves.

### 8. Export and verify

Follow `export_to_engine`: `export_model(...)` with the engine preset, then `reimport_check(...)`.

## Final checks before reporting

- [ ] Triangles, texture size and bone count are within the budget row.
- [ ] `rig_report(...)`: 0 unweighted verts and 0 verts over 4 influences.
- [ ] `pose_test(...)` images show no collapsing joints or stretched parts.
- [ ] `reimport_check(...)` reports scale 1 and the same triangle count, bones and clip names as the source.
