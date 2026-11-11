# Refine a generated model

> When: a model just came in from `pixal3d_status` or `image_to_3d_status` (or any imported scan or AI mesh), and it must look right and hold up as a game asset.

Generated meshes look right from the generator's camera and wrong elsewhere: the back and underside are guessed, thin parts melt together, and the shading shows faceting or seams. Look before you fix, and fix only what you saw.

## Stages

### 1. Save and measure

- `checkpoint("before refine")`, so any step below can be undone with `rollback`.
- `mesh_report(name)`: triangles, non-manifold edges, loose parts, flipped normals. `object_details(name)`: materials and the images they use.
- `capture_views(objects=[name], views=["front","right","back","iso"], shading="material")`. Write down what is wrong in each view before changing anything.

### 2. Compare with the source picture

- If the run started from an image: `set_reference(image_path, view="front", height=<the height you imported at>)`, then `compare_to_reference(view="front", objects=[name])`.
- A low overlap score from a wrong size or position: `transform` the model and compare again. A wrong silhouette (a missing handle, a merged gap): that part needs modelling, step 4.

### 3. Clean the surface

- `cleanup(name)` merges duplicate vertices, deletes loose bits and fixes normals. Add `fill_holes=True` only for a closed object (a prop, not a cloth or a leaf).
- Faceting or noise on smooth surfaces: `shade(name, smooth=True, sharp_angle=40)`. Bumps that are noise, not detail: `smooth_mesh(name, iterations=3, factor=0.3)`. Never on a hard-surface prop's edges.
- Over budget (hard_surface_prop has the tables): `decimate(name, target_tris=...)`. Check the silhouette again with `capture_views`.

### 4. Fix the shape

- Small dents and lumps: `sculpt_prepare` and `sculpt_stroke` (detailed_sculpt has how).
- A missing or merged part: model it from primitives (`create_primitive`, `boolean`, `mesh_edit`) and `join` it, or cut the bad part away first.
- Large errors everywhere: `rollback` and run `pixal3d_generate` again with a clearer description, another seed, or `tries=3`.

### 5. UVs and textures

- `uv_report(name)`: overlap ≈ 0 and coverage above 0.5 are fine. After a decimate or a shape fix the UVs and the baked textures no longer match. Unwrap again (retopo_and_bake, stage 3), and bake from a copy kept before the edit.

### 6. Installed add-ons

- The add-ons line names what is on. Many more are installed but off. `inspect_api(search="uv")`, `"retopology"`, `"pbr"`, `"decal"` or `"rig"` lists them, and `inspect_api(path="<module>")` turns one on and lists its operators.
- Call their operators from `run_python`. Modal tools that need the mouse (RetopoFlow, BoxCutter, the modal parts of HardOps) do not run from a script. Use their non-modal operators, or tell the user which one to click.

### 7. Check and hand over

- `capture_views(objects=[name], shading="material")` from the same views as stage 1. Each problem written down there is fixed, or you say why not.
- `mesh_report(name)`: 0 non-manifold edges on a closed object, and triangles in budget.
- For a game engine, export_to_engine has the rest.
