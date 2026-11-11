# Production medallion example

Open `production_medallion/review.html` for metrics, flags, clay turntables, wireframes and UV checker views. `production_medallion/engine_check/` is the actual Godot import-test project; its cache is regenerated when opened. GLBs, FBXs, editable Blender data and all four LODs' textures are included.

This example uses 1024 textures and an illustrative 12 cm longest dimension. Rebuild at your actual size and final texture resolution. The source remains the original attached `medallion.blend`.

```bash
GODOT_BIN=/path/to/godot python pa.py asset medallion /path/to/medallion.blend \
  --preset hero --engine godot --engine-check required --size-meters .12 \
  --texture-size 1024 --min-component-faces 4 --hole-max-edges 256 \
  --hole-max-diameter 0 --collision compound --uv-margin-px 2
```

Use `--engine generic` to build without an editor import test, or select the Unity/Unreal preset and provide the matching installed editor/project. The source has no final color/material data, so the example uses neutral material inputs and reports final texturing as required.

The earlier `medallion_closed` sample is the legacy mesh-only repair. The production sample demonstrates LODs, bakes, collision, engine import and review gates. Open-edge flags are deliberately retained in the production report rather than claiming every generated surface is closed.
