# Side views go to the cameras that see them — 2026-11-08

- Pixal3D's `left_image` camera (az270) sees the object's **right** side: the object faces the front camera at internal −Y, so its left side is at +X, where the az90 `right_image` camera sits. `pa.generate` fed `left.png` to `left_image`, so both profiles were mirrored, contradicted front/back, and the reconstruction put the front ornament on the back. Cuirass v3 (seed 1777044552): 3 of 3 tries had the medallion, skulls and bat wings on the back and the back's centre ridge on the front (judge 2/2/2). Same seed with only left/right exchanged (`vampire_chestplate_v3lr`): medallion on the front, centre ridge on the back (judge 3; what remains is melted fine detail). Chestplate v2's ornate back and pendant v3's 3/10 tries were made the same way.
- `pa.generate` now feeds `right.png` to node 2 (`left_image`) and `left.png` to node 4 (`right_image`). The view files keep their names (left.png = the object's left side). `view_qa.CAMERAS` and `mesh_judge.EYE` swap left/right to match (object's left = file +X), which also moves `parts.py` labelling. Tests: `test_side_views_go_to_the_cameras_that_see_those_sides` (new), `test_parts.Cameras.test_forward_points_from_camera_into_scene` updated; 59 OK. Backups: `backup/lrfix-20261108/`.

# LLM mesh judge — 2026-11-08

- Silhouette IoU cannot tell a good reconstruction from a bad one: medallion v2's three tries scored 0.68/0.69/0.6x while every one had a melted star, melted skulls and the front relief copied onto the back; chestplate v2 scored 1.30 with an open upper back and an ornate back. `mesh_judge.py` renders grey clay views of the raw GLB from the four view cameras **on the CPU** (painter's algorithm, no GPU while ComfyUI holds it), puts them under the reference views in `<mesh>.judge.png`, and asks an LLM for `{"verdict", "score", "problems"}`.
- Backends in order from `PA_JUDGE` (default `strata,agy,codex`; the first that answers judges; `PA_JUDGE=off` disables): `strata` = local Strata server (`STRATA_URL`, skipped in 3 s when it is down, which it is during a ComfyUI run: they cannot share the GPU), `agy` = Antigravity CLI (`PA_JUDGE_AGY_MODEL`, default `gemini-3.8-flash-high`; found in `~/.local/bin` even with it stripped from PATH), `codex` = `codex exec -i` when installed (it is not, now).
- `pa.py mesh --tries N`: a judged try that fails is rerolled even when its silhouettes pass, a judged pass stops even when they fail, and the kept mesh is the best judged one (pass, then judge score, then silhouette score). No backend answering = the old silhouette behaviour. Each try writes `<mesh>.judge.json`; `run.json` records every candidate's verdict. Tests: 5 new, 58 OK.
- Cameras: the clay renders use exactly `view_qa.CAMERAS` (front camera at file +Y, as Pixal3D's `_pixal3d_c2w` says; test `test_judge_cameras_see_the_views_with_view_qa_image_axes`). A first version put the front at −Y from a triangle-density reading of chestplate v2, but that mesh had its relief on the back; pendant v3 (rune ring and skulls at +Y, engraved back star at −Y) settled it. The pendant v3 tries 1–3 were judged with front/back swapped in the pipeline (2, 1, 2) and re-judged by hand: 3, 3, 3.
- First verdicts (agy, Gemini 3.8 Flash): chestplate v2 FAIL 2/10 (ornate back, missing straps), medallion v2 tries 1–3 FAIL 2/2/3 (melted star and skulls, back copies the front). The medallion v2 paint was stopped: the chain makes the pendant ~10% of each view, so its detail gets few voxels. v3 meshes the pendant alone; the chain is built in Blender (`work/regen/chain/make_chain.py`).

# Separate views are cut out again — 2026-11-08

- `--separate-views` meshed uncut views. Qwen saves its views as RGBA with alpha 247–255 on every pixel, and `split_sheet.py` took any image whose minimum alpha was below 255 as already cut out, so rembg never ran: every view had 100% alpha coverage, and Pixal3D got the white background. The chestplate v2 run's three tries all scored IoU front 0.55, sides 0.22–0.26 against a full square. Now `view_qa.is_cut_out(image)` requires ≥1% transparent pixels (alpha ≤ 32). Test: `test_only_a_transparent_background_counts_as_cut_out`. Recut chestplate v2 views: coverage front 0.46, left 0.22, back 0.45, right 0.22. Uncut copies: `work/regen/uncut-views/`.
- The sheet path (`split`) always ran rembg and was not affected. Note: `all --resume` with an image source never reuses the views stage (`done` starts as `False`), so `work/regen/regen.sh` meshes the chestplate with `pa.py mesh` on the recut views.
- Tests: 52 Python tests OK in the ComfyUI env.
- UltraTex 2048 on chestplate v2 ran out of VRAM twice (30,462 target + 37,375 reference tokens): at the auto reserve (6.3 GB) in the metal/rough pass after the albedo pass, and at `PA_TEXTURE_MEMORY_FACTOR=16` (8.3 GB reserve, 3.5 GB of weights offloaded, 483 s per step) in the albedo pass. Both failed allocating 534 MiB, one q/k/v copy (68,349 tokens x 32 heads x 128 x fp16) in `sparse_attention/core.py` `forward`. Measured with these sizes (`work/regen/qa/attn_peak.py`): the q/k/v hand-off peaks at 9 copies; making v contiguous before the norm saves 1, which is exactly the shortfall, so it was not applied. Chestplate v2 is painted at 1536 instead.
- `pa.py texture`: `PA_TEXTURE_MEMORY_FACTOR` sets UltraTex's `memory_factor` (unset: the workflow's 0 = auto). Test: `test_texture_memory_factor_comes_from_the_environment_only_when_set`. 53 tests OK.

# UltraTex into the package — 2026-11-07

- `pa.py asset ... --texture-from <UltraTex GLB>`: the painted material (base colour, roughness, metalness) becomes the colour source for every LOD bake. Copies of the LOD0 parts carry it, so the existing per-component pairing bakes LOD1–3 from it. Normals and AO still come from the high mesh. The build compares every UV triangle with the painted mesh and stops ("painted on a different LOD0 atlas") on a mismatch. Blender test: `test_texture_from_bakes_painted_lod0_onto_every_lod` (blue paint on a red source; a 60-face rebuild is rejected).
- `--pivot` changes the atlas: the pivot moves the mesh before xatlas, and a translated input charted 2503 instead of 2522 islands. A `generic` build (pivot `base`) rebuilt as `witcher3` needs `--pivot base` to match its texture.
- Witcher 3 default `--game-version` 5.1 → 5.01 (the installed Remastered patch).
- Rebuilt as `--engine witcher3 --pivot base` with their UltraTex textures: medallion_necklace (`UltraTex/medallion_necklace_00002_.glb`, 3:50) and vampire_chestplate (`00006`, 3:45). Projection miss 0 on all LODs. The previews now show the front. Previous generic packages: `work/overnight/backup_*_generic/`.
- LOD budgets: LOD1+ now shrink every LOD0 component by the LOD ratio, weighting each by what it lacks after its 4-triangle minimum. The sqrt(area) weighting (kept for LOD0, where it keeps small parts readable) let the chestplate's 205 studs take most of LOD2, leaving the 16,868-triangle plate at ~15%: big facets cut across the relief and showed as flat grey shards (also in the old generic LOD2). `allocate_budget(counts, weights, ...)` now takes final weights. Blender test: `test_lod_reduces_every_component_by_the_lod_ratio` (shell kept 12% before, ≥22% after). LOD0 is unchanged, so painted textures still fit.
- LOD folds measured (LOD faces whose normal opposes the nearest LOD0 face, by area), before → after the budget fix: chestplate LOD1 6.8 → 3.8%, LOD2 33 → 21%, LOD3 33 → 33%; necklace LOD1 3.2 → 1.0%, LOD2 24.5 → 10.4%, LOD3 37 → 37%. At LOD2's switch size (12.5% screen height, ~135 px at 1080p) the chestplate's LOD2 now reads like LOD0. What remains is a triangle-budget limit, not a decimator bug: the chestplate is a thin two-sided shell, and at ~1,400 triangles per side the two surfaces collapse through each other. MeshLab quadric collapse to the same count does worse (44%, with or without `preservenormal`).
- LOD3 > LOD2 fixed (chestplate 5,734 vs 5,624 triangles). `reduce_part` aimed decimation at `target - 1`, so a closed part at its 4-triangle LOD minimum was asked for 3; no closed solid has 3, the collapse left two coincident faces, sanitize erased them as duplicates, and the empty result fell back to the full part. Of the chestplate's 114 parts with 5–64 triangles, 90 were erased this way at target 3 and 8 at target 4. The aim is now `max(4, target - 1)`: LOD0 targets are ≥ 12, so LOD0 and the painted atlas are unchanged (aiming at `target` itself also changed LOD0, and the `--texture-from` atlas check rejected that build). Blender test: `test_closed_part_reduces_to_its_minimum_instead_of_being_restored` (8-sided cone at target 4; restored before, ≤ 4 triangles after).
- `--lod-ratios` (already in `asset_config.py`, default `1,.5,.25,.125`) now set to `1,.5,.35,.15` for the chestplate, a thin two-sided shell. Chestplate LOD1/2/3 triangles 10,122/5,624/5,734 → 9,888/6,906/4,496; folds 3.8/21.4/33.5% → 3.8/8.9/33.5%; projection miss 0. LOD3's folds stay at the shell's limit. Necklace (default ratios) 10,160/5,792/4,392 → 9,854/4,928/2,884; folds 1.0/10.4/37% → 1.1/10.4/37.4%; miss 0. Previous packages: `work/overnight/backup_vampire_chestplate_w3_backfix/`, `backup_medallion_necklace_w3_lodfix/`.
- Chestplate back medallion removed. Back-view normals show a smooth round boss, so the sigil, runes and gem were paint only (UltraTex copied the front reference onto the back). RePaint keeps a masked area within ~4/255 of its init at any denoise (0.65 and 0.9 both reproduced a flat fill), so the init image decides the result. Init: plate colour (non-stroke pixels, σ30 blur) + multi-scale grain at the plate's std 6 + faint scratches (`work/overnight/chest_backfix/view2_edit2.png`), denoise 0.65 → `UltraTex/vampire_chestplate_backfix_00003_.glb`. Rebuilt `--engine witcher3 --pivot base --texture-from` it (4:00, projection miss 0); necklace rebuilt with the LOD fix (3:53, miss 0). Previous packages: `work/overnight/backup_vampire_chestplate_w3_00006/`, `backup_medallion_necklace_w3_prelodfix/`.
- Tests: Blender `blender_assets.py` + `blender_integration.py` OK; 51 Python tests OK when run as documented in the ComfyUI env (`LD_LIBRARY_PATH=/opt/rocm/lib`). System python3 lacks trimesh, so 5 `test_parts` tests error there.
- Pivot for REDengine: a raw Pixal3D origin is the centre of the generation cube, so `source` means nothing for generated meshes; `base` is right for static props. Wearable Witcher 3 items are authored in character space and skinned (see the vampire sword's scabbard), which this static-prop pipeline does not do.

# Yaw + hand picks — 2026-11-07

- `--yaw DEG` (all mesh commands and `asset`): turns about world Z after the `--source-up` stand-up, recorded as `options.yaw`, logged as `PREP yaw <deg>`. Blender test: `test_yaw_turns_about_up_after_standing_upright`.
- Correction: medallion_necklace 00002 was **not** reconstructed back to front. Preview `high_00` looks from +Y, the asset's back (glTF front = +Z = Blender −Y = UltraTex front camera = `high_03`); comparing the necklace's `high_00` with the chestplate's `high_00` compared two backs. The `--yaw 180 --size-meters 0.45` build puts the hexagram at +Y, so UltraTex painted the reference front onto the plain back (`UltraTex/medallion_necklace_00001_.glb`). Rebuilt with `--yaw 0 --size-meters 0.45` (exact 180° turn of the yaw-180 build; hexagram now in `high_00`) and re-textured: `UltraTex/medallion_necklace_00002_.glb` (1024, 22:24); front render shows the hexagram texture on the −Y relief, matching the reference. `00001` is superseded.
- Previews: turntable view 0 (`high_00`, `lod*_00`) now looks from the glTF front (Blender −Y) instead of the back; the turn direction reverses with it. Blender test: `test_first_preview_is_the_gltf_front`.
- `pa.py asset <name> <glb>` now writes the run record. When `<glb>` is one of the `mesh` stage's candidates it becomes the recorded reconstruction (`picked: true`), and the asset stage is recorded. Before, a hand pick was invisible to `all --resume`, which would have rebuilt from the top QA score (necklace: 00003 instead of the picked 00002).
- Checked, not bugs: `texture` keeps production xatlas UVs (each LOD has a `<lod>.report.json` stub with `unwrap: smart`, so the re-prep is skipped); `PREP source-up` is in `GameAssets/<name>.build.log`, only the terminal filters it.

# Cleanup default — 2026-11-05

- `--dust-size` default 0.01 → 0.015. vampire_chestplate_ab kept one loose two-triangle sliver (11.8 × 0.4 × 0.6 mm, 1.15% of the diagonal) whose 4 open edges flagged `open_topology` on every LOD; hole filling rightly skips it (filling a flat patch's rim duplicates its faces). At 0.015 it is the only extra shell removed (dust 388 → 390 faces).

# UV + cleanup update — 2026-11-04

- `--uv-method xatlas` (default): `xatlas_worker.py` runs in `PA_XATLAS_PYTHON` (ComfyUI env), charts + packs triangle meshes, cuts folded faces loose as one-face meshes and re-charts until fold-free, at a normalized 100-unit scale (xatlas epsilons are absolute). Falls back to Smart UV with `uv_method_fallback` in the report; quad cages keep Smart UV. Asset builds skip Smart-UV relaxation and Blender repack candidates for xatlas atlases.
- `--dust-size 0.01` (default): removes open shells under 1% of the diagonal. Chestplate source: 91 lone open triangles on a closed surface caused all `open_topology` review issues.
- Tiny-part bake batching now applies at any part count (it was skipped at ≤32 parts, so 25 parts meant 25 bake groups instead of 3).
- Smart UV: `--uv-merge-faces 20` merges tiny islands along one border.
- Bake: channels that are an unlinked constant in every source material (Roughness/Metallic/Alpha, black Emission) are filled instead of baked — 4 of 7 passes for Pixal3D GLBs, which only link Base Color. Also fixes island-edge texels that the 1-sample bake blended toward black.
- Bake: larger parts share a bake call when their ray-grown boxes are disjoint and sizes are within 2×. Necklace 34 → 16 bake groups.
- Tried and rejected: Cycles HIP bake on the RX 6800 (355 s vs 320 s CPU — per-call sync dominates); xatlas on quad cages (charts tear quads, never converges).
- `--source-up z`: Pixal3D multiview GLBs are Z-up inside Y-up glTF (sword, chestplate: tallest axis glTF Z; plain Trellis2: Y). Default stays `y`. Lying input made UltraTex fit the front reference onto the top view (grey camouflage texture). Verified: UltraTex on the upright `--source-up z` LOD0 (1024, 59 min) gives the dark plate with medallion and eye sockets in place; the lying mesh gave camouflage.
- Workflows: `04_texture.json` node 3 input renamed `bg_removal_name` (ComfyUI core rename); view atlas + normal views now saved as `UltraTex/<name>_views*`/`_normals*`. `pa.py check` reports workflow/node drift against `/object_info`.
- Sculpt: multires hidden during UV work (271 → 201 s, identical output).
- Necklace end-to-end: 10:41 → 3:17 with identical UVs (texels changed > 8/255: basecolor 0.05%, normal 0.2%).

| asset (LOD0 20k tris) | coverage | islands | subpixel | isolated | open_topology | wall |
|---|---|---|---|---|---|---|
| chestplate before | 23.7% | 6455 | 187 | 938 | 4 LODs | ~3:15 |
| chestplate after | 44.0% | 3099 | 4 | 0 | none | 3:19 |
| necklace before | 27.9% | 6092 | 829 | 1151 | 4 LODs | 15:44 (xatlas-fallback run) |
| necklace after | 45.2% | 3219 | 59 | 0 | none | 11:07 (before batch/repack fixes) |

# Mesh cleanup update — 2026-10-27

The Blender pass now runs automatically after `mesh` and `all`, before texture baking. Original generated meshes remain separate.

- Explicit `--faces 0`; fixes the old silent fallback to 40,000 faces.
- Supported BMesh operations and current OBJ/PLY/STL import/export APIs.
- Boundary-only relative welding, degenerate and duplicate cleanup, bow-tie vertex repair, bounded hole filling and loose-element cleanup.
- Preserves small closed components by default. Does not cap isolated flat patches onto themselves.
- Sharp shading without the topology-splitting Edge Split modifier; clears stale custom normals.
- Smart UV Project, pixel-based packing and automatic separation of faces with UV foldovers.
- Stable UV area calculations and actual triangle-overlap validation.
- CPU transfer of standard PBR texture inputs onto the new UVs; explicit discard mode before UltraTex.
- Optional smoothing, voxel remeshing, shard removal and strict closed-mesh validation.
- .blend input/output, GLB/OBJ/FBX export, JSON reports and runner logs.
- UltraTex receives the prepared Smart UVs for explicit and automatically selected inputs.
- Blender exceptions return failure; a fresh successful report is required.

## Validation

22 regression checks passed: 11 Python tests and 11 real Blender tests on Blender 5.2.3 LTS. Ruff checks passed for changed Python files. The actual `pa.py prep` runner was exercised on the attached medallion.

The supplied sample started at 331,729 triangles with no UV layer. The strict sample has 39,494 triangles, zero boundary edges, zero non-manifold vertices, zero degenerate faces, zero invalid/collapsed UV faces and zero detected UV overlaps. Its exported GLB includes normals, tangents and TEXCOORD_0. It contains 72,481 exported vertices after UV/shading splits.

The conservative sample has 39,493 triangles and reports 33 boundary edges on retained open fragments. The strict sample opts into removal of components under four faces and unrestricted hole diameter. These aggressive choices are not imposed on every model.

The sample is neutral because its input has no UVs or source material. Surface folds already modeled into the generated geometry still need visual/artistic review. Neither topology checks nor Smart UV reconstruct the intended design.

ComfyUI GPU generation and UltraTex inference were not run in this environment; routing is covered by Python tests. Use your existing model/node installation to run those stages.

- `--source-up` now defaults to `auto`: z for trimesh-written glTF (raw Pixal3D output), y for Blender-exported and non-glTF files. `prep`, `fix`, `sculpt`, `asset` and `mesh` all stand raw Pixal3D GLBs upright without a flag; the build log prints `PREP source-up <axis>`. A raw plain-Trellis2 GLB needs `--source-up y`.
