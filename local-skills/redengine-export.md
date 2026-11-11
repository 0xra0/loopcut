---
id: local/redengine-export
version: 1
title: Witcher 3 and Cyberpunk 2077 game-ready export
when: the user wants an asset ready for The Witcher 3 5.01 (REDkit, wcc_lite) or Cyberpunk 2077 2.5 (WolvenKit)
roles: [single, workspace]
requires_tools: []
tier: free
kind: assist
part: single
words: witcher|\bw3\b|redkit|wcc_lite|cyberpunk|cp2077|wolvenkit|redengine|game.?ready|\bmods?\b|\blods?\b
---
"Game ready" here means exactly two games: The Witcher 3 Remastered 5.01 and Cyberpunk 2077 2.5. Ask which if unclear.
Build the package with the pipeline instead of exporting by hand. It decimates, repairs, unwraps (xatlas), bakes, makes LODs and collision, and writes a review page. Run it from run_python with subprocess. It starts its own headless Blender, so strip ~/.local/bin from PATH:
```
import os, subprocess
env = dict(os.environ, PATH=":".join(p for p in os.environ["PATH"].split(":") if ".local/bin" not in p))
subprocess.run(["python3", "/mnt/ssd/work/pixal3d-mv/pa.py", "asset", NAME, MESH_GLB,
                "--engine", "witcher3", "--pivot", "base"], env=env, check=True, timeout=3600)
```
Use `--engine cyberpunk` for Cyberpunk. Optional flags: `--size-meters 0.45` (real size), `--lod-ratios 1,.5,.35,.15` (thin shells), `--texture-from PAINTED.glb`. The output is /mnt/ssd/ComfyUI/output/GameAssets/NAME/ (meshes, textures, redengine/, review.html, asset_report.json); read asset_report.json for the result.
Do not run `pa.py all|mesh|views|texture`: they need ComfyUI and the GPU, and this model is using the GPU. Give the user the command instead.
Witcher 3 facts, checked against retail meshes and in game:
- wcc_lite imports FBX. It honours the FBX axis header and node rotations. REDkit's own header is FrontAxisSign +1, CoordAxisSign -1, in cm. A plain Blender export with axis_forward Y lands rotated 180 degrees about Z; the pipeline's w3_export.w3_axes() patches the header.
- LODs are FBX nodes named Mesh_lod0, Mesh_lod1, Mesh_lod2. The FBX material name is the W3 material name, with `<name>.xml` beside the FBX.
- Materials: pbr_std (Diffuse, Normal, RSpecBase/RSpecScale) or pbr_spec (adds SpecularTexture). Diffuse is dark (retail mean about 0.15). Normal is NormalmapGloss with gloss in alpha.
- Swords: blade along +Z, flats facing Y, pivot at mid-grip, about 1.28 m long.
Never overwrite anything in GameAssets/ that the user did not name: build under a new NAME.
