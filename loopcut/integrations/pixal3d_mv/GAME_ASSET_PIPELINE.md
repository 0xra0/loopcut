# Production asset pipeline

`mesh` and `all` now automatically run a production build after Pixal3D generation. The original generated model remains untouched. `prep` and `fix` retain the faster legacy mesh-only pass.

```bash
# Rebuild an existing model using all seven stages:
python pa.py asset medallion /path/to/medallion.blend --preset hero --engine godot --size-meters .12

# New generation with production settings:
python pa.py mesh lantern --preset prop --engine unreal
python pa.py all lantern ~/refs/lantern.png "an iron lantern" --preset prop --engine unity

# Direct Blender use, without ComfyUI:
blender -b --factory-startup --disable-autoexec --python-exit-code 1 \
  --python build_asset.py -- /path/to/input.glb /path/to/asset_directory \
  --name lantern --preset prop --engine generic --size-meters .5
```

Set `BLENDER_BIN` if Blender is not on PATH. `COMFY_DIR` overrides `/mnt/ssd/ComfyUI`. `PA_ASSET_TIMEOUT` defaults to 7200 seconds. Stage progress streams to the terminal; complete Blender output is retained beside the asset directory.

## The seven stages

| Stage | Implementation |
|---|---|
| 1. Component-aware surface cleanup | Repair topology, split loose components, protect small components from smoothing/remeshing, then apply optional spatial region masks. Original color/UV sources remain separate. |
| 2. High-to-low baking | Maintain high-resolution detail sources and low-poly targets. Cycles uses selected-to-active projection with matched source/target component groups. Bake base color, alpha, roughness, metalness, emission, AO and tangent-space normals for each LOD. |
| 3. UV efficiency | xatlas charts and packs (folded faces are cut loose and re-charted until none fold; `--uv-method smart` falls back to Smart UV Project), foldover isolation, shared atlas, pixel margins, bounded comparison of pack candidates, overlap checks, island counts, subpixel-island flags and texel-density percentiles. |
| 4. Asset budgets | Presets for triangles, exported vertices, texture resolution, material count, UV occupancy and estimated runtime texture memory. |
| 5. LODs | Independently reduce LOD0 into each requested ratio while preserving component identity. Build a checked atlas and rebake all PBR/detail maps for each LOD. |
| 6. Collision and engine presets | Optional boxes, convex hulls or spatial compound hulls; real-world size and pivot controls; engine-specific FBX axes, data textures and normal conventions. |
| 7. Visual/import checks | High/low turntables, wireframe and UV-checker views, silhouette IoU, sampled surface distance, bake-miss detection, exported GLB attribute/count validation, offline HTML review and actual editor import adapters. |

This is a **static-prop pipeline**. Skinned meshes, shape keys and animated objects are rejected. A separate character pipeline would need deformation-aware retopology, rigging and animation tests.

## Presets and LODs

| Preset | LOD0 triangle budget | Exported vertex budget | Texture size |
|---|---:|---:|---:|
| background | 5,000 | 15,000 | 1024 |
| prop | 20,000 | 60,000 | 2048 |
| hero | 40,000 | 120,000 | 2048 |

These are starting budgets, not universal limits. Explicit flags override preset values.

```bash
python pa.py asset sword sword.glb --preset hero --faces 25000 --texture-size 2048 \
  --lod-ratios 1,.5,.25,.125 --max-vertices 75000 --max-texture-mib 96
```

LOD ratios are measured against LOD0 and support up to six levels. Each LOD has its own validated atlas and a full bake; reusing decimated UVs can introduce foldovers. Texture resolution halves at each level, with a 256 minimum. Small components receive a minimum budget. If reduction would erase one, the original component is retained and a review flag is written. LOD0 exceeding the requested total budget rejects the build. Preserved components can make distant LODs larger than their requested ratio; inspect the report.

### Final texture from UltraTex

`pa.py texture <name>` paints the package's LOD0 on its own atlas and writes `output/UltraTex/<name>_NNNNN_.glb`. To ship that texture, rebuild the package from the same source and geometry options, adding `--texture-from`:

```bash
python pa.py asset necklace output/3D/necklace_00002_.glb --engine witcher3 --pivot base --size-meters .45 \
  --texture-from output/UltraTex/necklace_00002_.glb
```

The painted material replaces the source colours (base colour, roughness, metalness) in every LOD bake. Normals and AO are still baked from the high mesh. The rebuild must reproduce the painted LOD0 atlas. The build compares every UV triangle and stops with "painted on a different LOD0 atlas" otherwise.

Keep the source, `--faces`, `--size-meters`, `--yaw` and `--pivot` of the build that was painted. The pivot matters because it moves the mesh before xatlas runs, and xatlas charting is float-sensitive. A `generic` build painted with its `base` pivot needs `--pivot base` when rebuilt as `witcher3` or `cyberpunk`, since those default to `source`. Changing the engine alone does not alter the atlas.

Optional `--retopology quadriflow` uses Blender's QuadriFlow before enforcing the triangle budget. It is for suitable manifold static geometry; it does not reconstruct an intended design. Decimation remains the default.

## Surface cleanup and region masks

Default surface smoothing is two iterations at factor 0.15. Boundary vertices and vertices incident to sharp features are preserved. Components smaller than 12% of the global diagonal, or under 32 triangles, are protected from automatic surface smoothing/remeshing.

```bash
python pa.py asset medallion medallion.blend --surface-smooth 2 --surface-factor .15 \
  --feature-angle 55 --protect-component-size .12 --regions examples/regions.example.json
```

Regions use coordinates normalized to the full asset bounds. Partial masks smooth the interior of the selection while fixing its border. A voxel region must contain a whole component; partial voxel cuts are skipped and reported. `force` explicitly overrides small-component protection.

```json
{
  "regions": [
    {"name":"surface_region", "bounds":[[0,0,0],[1,0.55,1]],
     "smooth_iterations":3, "smooth_factor":0.15, "force":false}
  ]
}
```

The example bounds demonstrate the schema. They are not an automatic pendant detector. Adjust them to the orientation of your model. Voxel remeshing is optional because it can erase links or join nearby surfaces.

For a previously built editable `.blend`, select the intended mesh rather than all hidden sources:

```bash
python pa.py asset edited edited.blend --source-object ApprovedHigh --surface-smooth 0
```

Production `.blend` imports use render-visible meshes by default. Original world transforms are evaluated before parent removal. The editable output keeps the approved high, color sources, LODs and collision hidden alongside the visible LOD0.

## UVs and baking

With `--uv-method smart`, Smart UV supplies automatic seam seeds. Chestplate LOD0 (20k tris): xatlas 44.1% coverage, 3.1k charts, 0 isolated faces vs Smart UV 23.7%, 6.5k islands, 938 isolated. Relaxation is skipped for xatlas charts. An optional angle-based chart relaxation merges gentle boundaries as a forest, limited by `--chart-merge-angle 20` and `--max-chart-faces 512`. Its layout is retained only if valid and more space-efficient than the original Smart UV result; `--no-uv-relax` disables this candidate.

`--pack-method CONVEX` is the default; a valid AABB-packed atlas is always retained as a baseline. `--pack-search` also tries concave packing. Each additional candidate runs in an isolated Blender process with `--pack-timeout 20`; a timeout keeps the existing valid layout. Only a valid candidate with higher UV occupancy is accepted.

```bash
python pa.py asset prop prop.glb --uv-margin-px 4 --uv-angle 66 \
  --pack-search --pack-timeout 30 --min-uv-coverage .45
```

`--cage-extrusion .01` uses 1% of each component's diagonal. Set it to zero to use `--ray-distance .05` without a cage. The UV margin is a total gutter between islands; bake dilation uses at most half that spacing per side. Projection misses are measured on rasterized UV interiors, rather than unused image background. For hundreds of microscopic shells, geometry reduction remains individual; baking uses spatially matched groups of at most 32 small components with a small cage scale.

Existing standard Principled materials supply PBR inputs. Their source UVs remain separate from the new atlas. Existing tangent-space shader normals are retained when source UVs survive; remeshed components use geometric normals. Emission color and strength are baked together.

`--materials discard` uses neutral source materials while still producing normal and AO bakes. The report marks missing final texturing. Source materials without an alpha input remain opaque. `--alpha-mode opaque|mask|blend` can override automatic alpha classification; `--two-sided` explicitly enables double-sided rendering.

The model's intended surface must be reviewed before treating its high-resolution mesh as approved detail. An automatic normal bake can preserve unwanted folds as well as intended engravings.

## Engine presets

For the primary Cyberpunk/Witcher targets, see **[REDENGINE.md](REDENGINE.md)**. `--engine cyberpunk` adds a checked WolvenKit GLB handoff; `--engine witcher3` prepares REDkit FBX and ensures FBX output is enabled. Both provide material source maps and separate collision references. Native conversion, material/LOD/physics setup and in-game checks remain manual. Other presets are retained.

| Engine | Preferred mesh | Data texture | Normal texture |
|---|---|---|---|
| generic | GLB | ORM: R=AO, G=roughness, B=metalness | OpenGL |
| cyberpunk | Separate WolvenKit GLB handoff | Separate source maps; native packing is template-specific | OpenGL and DirectX source maps |
| witcher3 | REDkit FBX | Separate source maps; native packing is template-specific | OpenGL and DirectX source maps |
| Godot | GLB | ORM | OpenGL |
| Unreal | FBX | ORM | DirectX; green channel flipped in separate files |
| Unity | FBX | MetallicSmoothness: R=metalness, A=1−roughness; separate AO | OpenGL; imported as a normal map |

GLB always retains OpenGL normals, including when the engine preset is Unreal. GLB is generated for every LOD for portable checking. The Unreal FBX import adapter uses the separate DirectX textures. Unity targets URP Lit when available and falls back to Standard, recording the actual shader used.

`--size-meters` sets the longest dimension. Zero preserves source size and flags its units for review; Pixal3D outputs every model at roughly the same size (about 0.8–0.9 m tall), so set it for anything that is not that size. `--yaw DEG` turns the model about its up axis after it is stood upright (180 for a back-facing reconstruction). `--pivot source|center|base` controls the origin; base uses Blender's Z axis. Engine exporters perform axis/unit conversion. Placement and orientation should be checked in the target game.

`--collision box` is the default. `hull` produces one convex hull; `compound` builds bounded spatial convex hulls, controlled by `--collision-max-hulls`. Compound hulls are an approximation, not V-HACD or a physical chain simulation. Convex colliders can bridge openings. Unreal uses UCX names; Unity and Godot adapters create native collision objects.

## Real engine import checks

```bash
# Godot: creates an isolated, import-tested project and reusable asset.tscn.
GODOT_BIN=/path/to/godot python pa.py asset item item.glb --engine godot --engine-check required

# Unity: requires an installed/activated editor and an existing project.
UNITY_BIN=/path/to/Unity python pa.py asset item item.glb --engine unity \
  --engine-project /path/to/UnityProject --engine-check required

# Unreal: enable Python Editor Script Plugin in the project.
UNREAL_BIN=/path/to/UnrealEditor python pa.py asset item item.glb --engine unreal \
  --engine-project /path/to/Game.uproject --engine-check required

# Rerun just an engine check on a moved/exported bundle:
python engine_check.py /path/to/asset_report.json --engine godot --engine-bin /path/to/godot
```

`auto` runs a check when the editor and required project are available; otherwise it records `not_run`. `required` rejects a build unless the actual editor returns a fresh structured success result. Stale results and successful process exits without a report are rejected.

The Godot adapter checks every LOD's imported triangle count, UVs, normals, tangents, material texture and collider count, then saves a scene with distance-based LOD visibility ranges. Treat its switching distances as initial settings.

Unity creates an LODGroup prefab, imported PBR materials and convex MeshColliders under `Assets/Pixal3D/<name>`, plus a dedicated editor integration file. It checks imported triangle counts and required vertex attributes. Rebuilds update the same named assets.

Unreal imports FBX LODs and UCX collision, creates a PBR material, checks imported triangle/LOD/collider counts, and saves assets under `/Game/Pixal3D/<name>`. The adapter creates a material for each LOD and assigns its section to the corresponding material slot, so independent UVs and bakes stay aligned.

Godot is exercised in the supplied validation. Unreal and Unity adapters are included but require validation against your installed editor/project versions; they are not reported as passed without running those editors.

## Output and quality policy

Outputs land in `ComfyUI/output/GameAssets/<name>/`:

- `asset_report.json`: portable relative paths, stage results, repairs, geometry/UV/material budgets, bake coverage and editor results.
- `review.html`: offline review gallery, metrics and flags.
- `meshes/`: individual LOD GLBs, optional FBX/OBJ, collision GLB and UV provenance.
- `textures/`: PBR maps, packed engine maps and per-LOD normal maps.
- `previews/`: high/low turntable frames, wireframes and UV checkers. View `_00` is the glTF front (+Z, Blender −Y), the side UltraTex paints with the front reference; `_03` (of the default 6) is the back.
- `<name>.blend`: editable, packed high/color sources, game LODs and collision.
- `engine_check/`: editor evidence and, for Godot, a reusable test project/scene.

`--quality review` exports technically valid assets with flags for low UV efficiency, subpixel islands, material gaps, shape changes and missed bakes. `--quality strict` rejects those review conditions. `--strict` additionally rejects remaining open/non-manifold geometry. No report claims an automatic shipping certification.

Builds use staging directories. The previous successful output is replaced only after the complete build passes configured gates, and retained as `<name>.previous`. Failed staging directories retain diagnostic reports. JSON report paths remain portable when the bundle is moved.

For quick previews, use a 512 texture, fewer turntable views, or `--skip-previews`. The supplied medallion production example uses 1024 textures to keep the example compact; use the preset's final resolution for your actual asset.

`PA_NO_FIX=1` skips production processing after generation. `PA_LEGACY_PREP=1` selects the older mesh-only pass. These do not apply to an explicit `pa.py asset` build.

## Tests

```bash
python -m unittest discover -s tests -v
blender -b --factory-startup --python-exit-code 1 --python tests/blender_integration.py
blender -b --factory-startup --python-exit-code 1 --python tests/blender_assets.py
```

An actual engine import is additional to the Blender and Python regression suites.
