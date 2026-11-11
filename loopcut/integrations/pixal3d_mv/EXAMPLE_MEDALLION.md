# Tested medallion outputs

`examples/medallion_closed/` contains GLB, FBX and editable Blender files plus the report. This variant passes strict closed-manifold and UV checks.

```bash
blender -b --factory-startup --disable-autoexec --python-exit-code 1 \
  --python prep_mesh.py -- /path/to/medallion.blend /path/to/medallion_closed.glb \
  --faces 40000 --materials discard --hole-max-edges 256 \
  --hole-max-diameter 0 --min-component-faces 4 --strict --formats glb,fbx,blend
```

The `--min-component-faces 4` option removes tiny open fragments. It is appropriate for this closed prop; it could remove intentional cards/planes in another asset. The default preserves these parts.

`examples/medallion_conservative/` contains the ordinary `pa.py prep` result, which preserves the open fragments and reports them:

```bash
python pa.py prep medallion /path/to/medallion.blend --faces 40000 --materials discard --formats glb,fbx,blend
```

Apply textures next. If the front reference is already available in ComfyUI:

```bash
python pa.py texture medallion /path/to/medallion_closed.glb 2048
```

Keep the adjacent `.report.json` with the processed GLB so `pa.py texture` recognizes the prepared UV layout. If moved elsewhere, its stored absolute output path no longer matches; the pipeline safely prepares another copy instead. For your newly processed local models, paths already match automatically.

The previews use the same neutral clay material and lighting to compare shape. They show that generation-induced folds remain in the surface: this is technical cleanup and UV preparation, rather than a reconstruction of the medallion's intended design.
