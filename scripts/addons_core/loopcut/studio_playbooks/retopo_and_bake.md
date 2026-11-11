# Retopology, UVs and baking

> When: making a game mesh from a dense sculpt or scan, and transferring the detail to textures.

## Stages

### 1. Duplicate the source

- `duplicate(high, new_name="<Name>_low")`. The high-poly mesh must stay untouched: it's the bake source.
- If the high-poly has modifiers or several parts, make the low copy from the combined shape: `apply_modifiers(...)` on the copy, then `join(...)` if needed.

### 2. Retopologize

- Deforming meshes (characters, creatures, cloth): `quad_remesh(name, target_faces=..., use_symmetry=True)`. target_faces is quads, and the triangle count is about twice that.
- Static meshes (rocks, props): `decimate(name, target_tris=...)` is fine and keeps the silhouette better at low counts.
- `cleanup(...)` afterwards.
- Check: `mesh_report(...)` shows the count in budget and 0 non-manifold edges. Compare silhouettes between high and low in front/right/iso, showing just the two objects with `capture_views(objects=[...])`. If QuadriFlow loses thin parts (fingers, ears, horns), raise target_faces or model those parts separately.

### 3. UVs

- Hard-surface, or organic without hand-marked seams: `uv_unwrap(name, method="smart", angle_limit=55)`, then `uv_pack(names=[name])`.
- angle_based with seams_from_angle only works when the mesh has sharp edges. On a smooth character it finds no seams and makes one stretched island. Check `uv_report(...)`: islands = 1 with a texel_density_spread above about 3 means this happened.
- Check: `uv_report(...)` shows overlap ≈ 0, faces_out_of_bounds 0 and coverage above 0.6. A texel_density_spread above about 3 means some parts get far less texture than others. Unwrap again with different seams or angle.

### 4. Pack

- For several objects that share one texture: `uv_pack(names=[...])`.

### 5. Bake

- `apply_transforms(...)` on both meshes first, and make sure they overlap in space.
- `bake_maps(low, high, maps=["NORMAL","AO"], size=..., cage_extrusion=...)`. cage_extrusion should be just more than the largest gap between the surfaces: start at about 1-2% of the object size.
- Check with `capture_views(objects=[low], shading="solid")`: the low mesh should show the sculpt's detail.
  - Blotchy or inverted patches: cage_extrusion is too small. Raise it.
  - Smeared detail from a neighbouring part (inner arm onto torso): it's too large. Lower it, or bake parts separately.
- The images are saved as PNGs and wired into the low mesh's material when connect=True.
