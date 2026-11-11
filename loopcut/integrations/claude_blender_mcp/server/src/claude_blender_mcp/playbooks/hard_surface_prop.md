# Hard-surface prop

> When: weapons, vehicles, furniture, tools, buildings and kit pieces. They have crisp edges and are mostly not deforming.

## Targets

| Use | Triangles | Texture |
| --- | --- | --- |
| Small prop (cup, tool) | 200-2k | 256-1024 |
| Medium (weapon, chair) | 2k-10k | 1024-2048 |
| Large or hero (vehicle) | 10k-60k | 2048-4096, or several materials |

## Stages

### 1. Blockout

- Start from primitives sized in meters with `create_primitive(...)`. Put the origin where the object would be held or placed, for example the grip of a weapon or the floor under furniture.
- `apply_transforms(name)` once the shape is right, so later bevels and booleans aren't skewed by scale.
- Check: front, right and top views against the real dimensions.

### 2. Shape

- Use `mesh_edit(...)` for extrudes, insets and bevels on faces selected by direction or bounds.
- Use `boolean(...)` for cut-outs. `checkpoint(...)` first, then `cleanup(...)` and `mesh_report(...)` after: the target must stay manifold.
- Use MIRROR and ARRAY modifiers through `add_modifier(...)` for repeated or symmetric parts. Apply them with `apply_modifiers(...)` before UVs.

### 3. Edges

- Game: a BEVEL modifier of 1-3 segments with a small width, then `shade(name, sharp_angle=30)`. That reads crisp with few triangles.
- Render: a BEVEL modifier plus SUBSURF, or bevel only.
- Check: a wireframe `capture_views(...)` shows no long thin triangles or n-gons that will shade badly. `mesh_report(...)` shows n-gons near 0 for game use.

### 4. UVs and materials

- `uv_unwrap(name, method="smart")`, then check `uv_report(...)`: overlap 0 and faces_out_of_bounds 0.
- Use one material per surface type (metal, wood, rubber) with `create_material(...)` and `assign_material(..., select=...)`. For a game, keep to 1-3 materials per prop.
- If there is a high-poly version, follow `retopo_and_bake` stage 5 to bake its detail.

### 5. Finish

- `cleanup(...)`, then `mesh_report(...)`: within budget, 0 non-manifold, 0 loose.
- Export with `export_to_engine`. Props use `animation=False` unless they have moving parts.
