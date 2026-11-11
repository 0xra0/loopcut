# Pixal3D multiview kit (PixelArtistry method)

Method: github.com/pixelartistry/PixelArtistry-Pixal3D-Multiview. Tuned on the RX 6800 (ROCm) in October 2026.

```
your image ─┐
            ├─► 02 Qwen-Image 2.1 edit ─► 2048×1024 sheet ─► split + rembg ─► 4 RGBA views ─► 03 Pixal3D MV ─► GLB
text ─► 01 front view ┘     (front | left | back | right)                         (1536_cascade)
```

## Production pipeline update

Primary targets are **Cyberpunk 2077** (`--engine cyberpunk`) and **The Witcher 3 Remastered** (`--engine witcher3`). See **[REDENGINE.md](REDENGINE.md)** for WolvenKit/REDkit handoffs and remaining native integration steps. Godot, Unity, Unreal and generic support remains available.

`pa.py mesh` and `pa.py all` now run all seven production stages automatically: component-aware cleanup, high-to-low PBR/normal/AO baking, UV efficiency checks, asset budgets, LODs, collision/engine presets and visual/import quality gates.

Read **[GAME_ASSET_PIPELINE.md](GAME_ASSET_PIPELINE.md)** for current commands, settings and limitations. Use `pa.py asset <name> [input-mesh] --preset prop --engine generic` to build an existing model. Production output is `ComfyUI/output/GameAssets/<name>/`, including `review.html` and `asset_report.json`.

The `prep`/`fix` commands below remain available for the legacy mesh-only pass. Production defaults use the selected asset preset rather than the legacy 40,000-triangle default.

## One-time setup
```bash
bash /mnt/ssd/work/pixal3d-mv/setup_models.sh     # ~10 GB: Qwen3-VL GGUF Q8 text encoder + mmproj
```
Every line should say `have`, `done` or `ok`. Don't use the int8 or w4a8 encoders, which are very slow on this GPU.

## Every session
1. Start ComfyUI **with `--cache-none`**:
   `bash /mnt/ssd/work/pixal3d-mv/launch_comfy_qwen.sh`
   (It sets real NAF by default (`TRELLIS2_REAL_NAF=1 TRELLIS2_NATTEN_IMPL=flex`; pass `TRELLIS2_REAL_NAF=0` for the old bilinear dummy), then runs your normal `launch_best_cpu_vae.sh` plus `--cache-none`; keep the flags there. Extra arguments pass through, e.g. `bash launch_comfy_qwen.sh --verbose`.)
2. Then `$P pa.py check` - it prints one line per thing that can break (all 15 model files, the Pixal3D MultiView fix in the Trellis2-GGUF node, the UltraTex commit, whether the server answers). Exit code 1 means something is wrong. Run it any time a ComfyUI update or a reboot might have reset something.
3. Close big RAM users first: browser tabs, the game and Blender. Otherwise earlyoom kills ComfyUI (exit 144, no error).

   Both the launcher and `pa.py` read `COMFY_PORT` (default 8188). `export COMFY_PORT=8199` once if 8188 is taken; you do not have to edit either file.

## Make a model
```bash
cd /mnt/ssd/work/pixal3d-mv
P=/mnt/ssd/conda-envs/ComfyUI/bin/python3.12

# step by step (recommended: look at each result before the next step)
$P pa.py sheet  lantern  ~/refs/lantern.png  "an iron oil lantern with a glass body"   # ~10 min
$P pa.py split  lantern  /mnt/ssd/ComfyUI/output/PixelArtistry/lantern_02_sheet_00001_.png
$P pa.py mesh   lantern                                                                 # ~12.5 min

# no image? make the front view from text first (~4 min)
$P pa.py front  lantern  "an iron oil lantern with a glass body"

# or everything in one go
$P pa.py all    lantern  ~/refs/lantern.png  "an iron oil lantern with a glass body"
$P pa.py all    lantern  "text:an iron oil lantern with a glass body"  "an iron oil lantern"
```
- `<name>` must be a single word with no spaces. It names every output.
- Each stage prints the seed it used; `front`, `sheet`, `mesh` and `all` take an optional `[seed]` as the last argument to replay a result you like. `PA_MAX_WAIT` (seconds, default 5400) caps one ComfyUI wait before `pa.py` errors instead of hanging.
- The description is a short noun phrase. Name the materials, because the sheet copies them.
- **Input image tips:** use one object only, the whole object in frame, upright, on a plain or white background, seen straight from the front (not at 3/4).

## Where things land
| What | Path |
|---|---|
| Front view / sheet | `/mnt/ssd/ComfyUI/output/PixelArtistry/<name>_01_front_*.png`, `<name>_02_sheet_*.png` |
| 4 views | `/mnt/ssd/ComfyUI/input/<name>_views/{front,left,back,right}.png` |
| Raw mesh | `/mnt/ssd/ComfyUI/output/3D/<name>_*.glb` (original generated mesh) |
| Processed mesh | `/mnt/ssd/ComfyUI/output/3D/<name>_lp.glb`, report and optional texture sidecars |

## Check before you go on
- **Sheet:** there must be 4 separate panels, and the side views must be real 90° profiles. A thin object should look like a thin line from the side. If a panel shows a 3/4 view or the object rotated, run `sheet` again; each run uses a new random seed, unless you pass a seed that worked.
- **Views:** open the 4 PNGs. They should have a clean cut-out with no white halo, and no piece of a neighbouring panel.
- **Mesh:** an all-black texture is a known random NaN bug (about 2 in 9 runs), so run `mesh` again. A different look per run is normal, because the pipeline is not deterministic.
- **Known limits:** thin tips get cut flat (taper_tip.py in mv_recovered fixes swords), and small round details like a pommel wheel come out blocky.
- **If the Trellis2/Pixal3D node disappears or changes after a ComfyUI update**, run `$P pa.py check` first. Do **not** "fix it" by re-running `/mnt/ssd/ComfyUI/Pixal3D_GGUF_MultiView_Fix/install.sh`: the node installed here already has that fix plus later patches (rocm_safe_bake, camera calibration) that folder predates, so installing from it today would downgrade them. The installer knows this and refuses; only `FORCE=1` overrides it.

## Legacy Blender pass: prep / fix

Blender **4.2 or newer** is required; tested with Blender 5.2.3 LTS. Static props only.
Set `BLENDER_BIN=/absolute/path/to/blender` if Blender is not on PATH.
`COMFY_DIR` and `COMFY_PYTHON` can override the existing ComfyUI paths.

The processing order is:

1. Generate the original Pixal3D GLB and keep it untouched.
2. Repair topology using scale-relative boundary welding, degenerate/duplicate cleanup, separation of overconnected edges and bow-tie vertices, limited closed-hole filling and loose-element removal.
3. Optionally smooth/remesh; triangulate and reduce to the triangle budget.
4. Run cleanup again, recalculate normals and mark sharp shading edges without splitting the mesh.
5. xatlas charts + pack (fold-free; `--uv-method smart` for the old path), or Smart UV Project, normalize island scale, pack with pixel-based spacing, then isolate and repack faces with UV foldovers.
6. Transfer existing standard PBR materials onto the new UVs using CPU baking, or discard materials explicitly before retexturing.
7. Validate UV bounds, zero-area UVs, triangle overlap and topology; export with a JSON report.
8. Optionally run UltraTex using `keep_existing` UVs. Do not unwrap after the final texture bake.

The legacy `prep` command creates `<name>_lp.glb` with a default **40,000-triangle** budget. `mesh` and `all` now use the full production build described above. The original generated GLB remains separate. `PA_NO_FIX=1` explicitly skips production processing for a raw preview.

```bash
# Existing workflows automatically produce a cleaned, unwrapped copy:
$P pa.py mesh medallion
$P pa.py all medallion ~/refs/medallion.png "a metal medallion"

# Reprocess an existing generated mesh (or an external .blend/.obj/.fbx/.ply/.stl/.gltf):
$P pa.py prep medallion /path/to/medallion.blend --faces 40000 --formats glb,fbx,blend

# Preserve the full triangle count; 0 is explicitly forwarded to Blender:
$P pa.py fix medallion /path/to/medallion.blend --materials discard

# Smart UV normally rebakes existing Principled PBR material inputs:
$P pa.py prep lantern --texture-size 2048 --uv-margin-px 4

# Skip CPU texture transfer if you will immediately use UltraTex:
$P pa.py prep lantern --materials discard
$P pa.py texture lantern 2048

# Preserve an already textured model's UVs; errors if no UVs exist:
$P pa.py fix lantern /path/to/textured.glb --unwrap keep

# Sculpt base (Blender/ZBrush/SculptForge): floaters dropped, voxel remesh to one closed
# manifold of even quads, no UVs. Raw Pixal3D output has non-manifold edges, floaters and
# uneven decimated triangles that break brushes and dyntopo. Bake/retexture after sculpting.
$P pa.py sculpt lantern [mesh.glb] [0.001]   # voxel = detail captured (fraction of diagonal)
# -> _sculpt.blend: ~25-45k QuadriFlow quad cage + 3 multires levels holding the detail (sculpt here)
#    _sculpt.glb/.obj: the cage alone, for other sculpt apps (subdivide + project there)
```

Direct Blender use does not need ComfyUI:

```bash
blender -b --factory-startup --disable-autoexec --python-exit-code 1 \
  --python prep_mesh.py -- /path/to/input.blend /path/to/output.glb \
  --faces 40000 --materials discard --formats glb,obj,fbx,blend
```

### Repair controls

| Flag | Default | Purpose |
|---|---|---|
| `--repair off/light/full` | full | Full uses a larger permitted hole-edge count; neither mode deletes nearby thin surfaces |
| `--faces N` | 40000 for prep; 0 for fix | Triangle budget; zero keeps all triangles after cleanup |
| `--angle DEG` | 35 | Sharp shading threshold; zero smooths all edges |
| `--uv-method M` | xatlas | `xatlas` charts and packs triangle meshes in `PA_XATLAS_PYTHON` (default the ComfyUI env), splitting folded faces off until none fold; `smart` = Blender Smart UV. Quad cages (sculpt) always use Smart UV; an xatlas failure falls back to Smart UV and is reported as `uv_method_fallback` |
| `--uv-angle DEG` | 66 | Smart UV projection threshold |
| `--uv-merge-faces N` | 20 | Merge Smart UV islands under N faces into a neighbour; 0 disables |
| `--texture-size N` | 2048 | Texture transfer resolution and UV margin reference |
| `--uv-margin-px N` | 4 | Island spacing at that resolution |
| `--weld-distance N` | 0.000001 | Boundary/loose vertices only, as fraction of diagonal; avoids blind interior welding |
| `--hole-max-edges N` | 12 light / 64 full | Only closed, unbranched boundary loops; zero disables filling |
| `--hole-max-diameter N` | 0.03 | Fraction of diagonal; zero permits any diameter |
| `--min-component-faces N` | 0 | Optional shard deletion; zero preserves small parts and chain links |
| `--source-up auto\|y\|z` | auto | `z` stands up a source that stores Z-up geometry in a Y-up GLB. **Pixal3D multiview outputs do** (they import lying on their back, so GameAssets pivots, engine imports and UltraTex's canonical views are wrong). `auto` picks `z` for glTF written by trimesh (raw ComfyUI/Trellis2 export) and `y` for everything else, so Blender-exported `_lp`/`_sculpt`/GameAssets files are never rotated twice. Pass `y` for a raw plain-Trellis2 (single-view) GLB, which is already Y-up |
| `--yaw DEG` | 0 | Turn the model about its up axis after `--source-up`; positive is counter-clockwise seen from above. Use 180 when Pixal3D reconstructed the model facing away from the front view. Recorded as `options.yaw`; the build log prints `PREP yaw <deg>` |
| `--dust-size F` | 0.015 | Remove open shells smaller than F × diagonal (zero-volume fins on generated surfaces); closed parts such as chain links stay; 0 disables |
| `--materials bake/discard` | bake | Transfer standard Principled PBR inputs, or prepare neutral UV mesh for texturing |
| `--strict` | off | Reject remaining open/non-manifold geometry |

No automatic pass can distinguish every intended opening from a missing face. Large/branched boundaries are left for review by default. `--strict` rejects those models instead of reporting them as closed. Open surfaces can be valid game geometry, so non-strict mode can export them with warnings.

For physically noisy geometry, try mild smoothing first:

```bash
$P pa.py prep medallion /path/to/medallion.blend --smooth-iterations 2 --smooth-factor 0.15 --materials discard
```

For severe intersections, an **opt-in voxel remesh** is available:

```bash
$P pa.py prep medallion /path/to/medallion.blend --voxel-size 0.0015 --materials discard
$P pa.py texture medallion 2048
```

Voxel size is relative to the mesh diagonal. Remeshing destroys old UVs and may erase thin chain links, tips or small holes. It is never enabled automatically. Smoothing can shrink details. Neither operation reconstructs the artist's intended hard-surface design from malformed generated geometry.

### Materials and exports

Smart UV mode transfers Base Color, Alpha, Roughness, Metallic and tangent-space Normal for standard Principled BSDF materials directly connected to Material Output. It uses surviving source UV data after cleanup; newly filled areas and removed details cannot be reconstructed. Nonstandard shaders require `--materials discard` and retexturing. This is not a general shader/material converter.

GLB embeds the resulting textures. Baked PNGs are also written to `<output>_textures/`; keep OBJ/MTL and FBX sidecars together when moving them. `--formats glb,obj,fbx,blend` writes all four. Blender supports both `.blend` input and a cleaned editable `.blend` output.

UV packing uses AABB island bounds for predictable headless performance. It favors reliability over maximum atlas occupancy. UV bounds, zero-area faces and actual triangle overlaps are checked. If Smart UV leaves foldovers, affected triangles get separate islands and are repacked. Subpixel slivers get a small minimum UV height to avoid numerical collapse. This can increase island count and reduce atlas efficiency. The overlap test stops after two million candidate tests; Smart UV export is rejected if validation cannot complete. `--unwrap keep` allows existing texture-reuse overlaps and reports them.

The pass fixes technical mesh problems; it does **not** certify a shipping game asset. Check the silhouette, remaining surface folds, UV texel density, engine scale, material appearance, LODs, collision and rigging requirements for the intended game.

### Reports and failures

Each output gets `<name>.report.json` and, through `pa.py`, `<name>.blender.log`.
The report includes input/output triangle counts, repairs, remaining boundary/non-manifold edges, UV bounds/area/overlaps, material state and exported paths. Reported mesh vertices are Blender topology vertices; game-format exports can have more vertices because UV seams and sharp normals split vertex attributes.
Invalid UVs, collapsed UV triangles, geometric degenerates or a failed strict topology check reject export. A Blender script exception produces a nonzero process exit. The runner requires a fresh successful report and the actual output file; an old file or a progress line cannot masquerade as success.

Coincident duplicate faces are removed. Closed islands consisting entirely of duplicate coincident faces have zero volume and are removed as collapse artifacts; ordinary small closed components are preserved. Isolated flat patches are not capped onto themselves.

### UltraTex

One-time setup: `bash setup_ultratex.sh`. Then `pa.py texture <name> [mesh-path] [1024|2048]`.
The default input prefers `<name>_lp.glb` over the full-resolution `<name>_game.glb`.
External models and older outputs without a current cleanup report are automatically prepared first. UltraTex always receives `uv_mode=keep_existing`, so it textures the Smart UV layout. Outputs remain under `ComfyUI/output/UltraTex/<name>*`.

The reference remains `ComfyUI/input/<name>_views/front.png`. UltraTex needs its existing ComfyUI nodes, weights and reference view. The Blender pass works independently of those GPU dependencies.

### Validation

```bash
python -m unittest discover -s tests -v
blender -b --factory-startup --python-exit-code 1 --python tests/blender_integration.py
```

The Blender tests exercise damaged closed boundaries, loose geometry, preservation of tiny components, sharp normals without topology splitting, Smart UV, PBR transfer, UV preservation failure, and GLB/OBJ/FBX/BLEND exports.
