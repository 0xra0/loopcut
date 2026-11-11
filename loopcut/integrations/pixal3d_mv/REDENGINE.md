# Cyberpunk 2077 and The Witcher 3

These are the primary target games for this project. Generic, Godot, Unity and Unreal support remains available. Choose a target explicitly; the default remains generic.

```bash
# Cyberpunk 2077 / REDengine 4 / WolvenKit
python pa.py asset medallion /path/to/medallion.blend --preset hero --engine cyberpunk --game-version 2.5

# The Witcher 3 Remastered / REDengine 3 / REDkit
python pa.py asset medallion /path/to/medallion.blend --preset hero --engine witcher3 --game-version 5.01

# The same targets work directly after Pixal3D generation:
python pa.py mesh lantern --engine cyberpunk
python pa.py mesh lantern --engine witcher3
```

Use your installed game version label with `--game-version`. The defaults record the 2.5 and 5.01 targets (Witcher Remastered patch **5.01**, October 8, 2026); they do not certify compatibility. Geometry export does not depend on this label.

Each target still runs all seven processing stages and adds `redengine/handoff.json` plus `redengine/IMPORT.md`. Read those files with `review.html` before native integration. RED presets preserve the source pivot by default; other engines retain the base-pivot default. Set `--size-meters` only when you know the intended longest dimension, and verify placement against a game reference.

| Target | Prepared output | Native integration still needed |
|---|---|---|
| Cyberpunk | Material-free GLB, names such as `submesh_00_LOD_1` on both object and mesh data; triangulated, UV0/normals/tangents; chunks with at most 20,000 triangles / 60,000 triangle corners | WolvenKit import into a compatible existing `.mesh`, native material/chunk mapping, entity setup, physics, mod build and game test |
| Witcher 3 | FBX with tangents and unit metadata, render meshes separated from collision references | REDkit mesh import, native `.w2mesh`/shader resource, material inputs, native collision, entity/item attachment, cooking and game test |

Cyberpunk's GLB handoff is separate from the canonical PBR preview GLB. Large meshes are split conservatively to accommodate UV/normal vertex splits; every chunk must be mapped to the same native atlas material. Individual exported chunk counts are checked against a conservative 65,535 triangle/vertex boundary. This preparation does not guarantee acceptance by every template or tool version.

An optional target resource reference is recorded without reading or modifying the game installation:

```bash
python pa.py asset item item.glb --engine cyberpunk --red-template base/props/item.mesh
python pa.py asset item item.glb --engine witcher3 --red-template environment/props/item.w2mesh
```

Choose actual compatible resources from your mod project; the paths above are examples. This pipeline does not invent a WolvenKit Material.json or REDkit material XML.

## Materials, LODs and collision

Both targets receive separate base color/alpha, roughness, metalness, AO, emission and OpenGL/DirectX normal source maps. Preview GLBs always use OpenGL normals. Choose the native normal convention and texture packing from the material template. ORM is a portable preview map, not a universal REDengine material. Witcher shader inputs may require a specular/gloss workflow; simply inverting roughness does not reconstruct it.

Every generated LOD has its own atlas and full texture set. The RED handoff therefore exposes each as a **separate native mesh target**, with its matching maps. It does not automatically assemble native LOD chains. Cyberpunk alternatives intentionally all use `LOD_1` names; native LOD masks 1/2/4/8 must not be applied while sharing mismatched atlases. To assemble a shared-material chain, first reproject every LOD into one common atlas, or explicitly configure separate native bindings in suitable tooling.

Colliders are reference hulls in separate files. UCX names are not REDengine collision instructions. Native physics/flags/cooking must be configured in the mod tools.

`engine_check.status` remains `not_run` for these targets, including when a binary is supplied. `--engine-check required` is rejected early because there is no native REDengine import adapter. Technical Blender/export checks do run; the asset remains `review_required` until native integration and game testing are completed externally.

## Asset scope and validation

The current pipeline supports **static props**. A pendant as static geometry can be prepared, but equipping it on a character may require an attachment bone, skin weights or cloth/dangle physics. Characters, clothing, deforming weapons and vehicles need a dedicated rig/attachment workflow. Animated/skinned inputs remain rejected.

Validation for this update includes 24 Python tests, nine Blender production tests (including complete builds for both RED targets), lint, and the existing legacy repair suite. The earlier medallion sample remains a Godot-verified portable example; it is not represented as a native Cyberpunk/Witcher mod. No game installation, WolvenKit or REDkit editor was available for native import certification.

## References

- WolvenKit mesh import/export and submesh requirements: https://wiki.redmodding.org/wolvenkit/wolvenkit-app/usage/import-export/models
- CDPR REDkit FBX import workflow: https://cdprojektred.atlassian.net/wiki/spaces/W3REDkit/pages/6326771
- Cyberpunk 2.5 release: https://www.cyberpunk.net/en/news/52125/cyberpunk-2077s-edgerunners-2-update-2-5-is-out-now
- Witcher Remastered patch 5.01: https://www.thewitcher.com/us/en/news/52085/patch-5-01-for-the-witcher-3-wild-hunt-remastered-is-live
