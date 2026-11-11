# Pixal3D: safe GLB export recovery

**Problem:** `pa.py mesh` reports "Pixal3D returned no GLB" although ComfyUI executed `Trellis2ExportMesh` and wrote a `.glb` to `output/3D`.

## Install (project directory)

```bash
cp /path/to/extracted/install_export_recovery.py .
python install_export_recovery.py pa.py
python -m py_compile pa.py
```

The installer preserves your current `pa.py` (including prompt support and REDengine stages), makes `pa.py.bak-export-recovery`, and changes **only** GLB discovery in `mesh()`. It never accepts a stale file: recovered GLBs must have been written during this specific mesh invocation and have a valid glTF v2 header. If multiple fresh candidates exist, it refuses to guess.

## Before generating another mesh

If a valid GLB already exists at `/mnt/ssd/ComfyUI/output/3D/medallion_necklace_00001_.glb`, there is NO need to regenerate:

```bash
cd /mnt/ssd/work/pixal3d-mv
P=/mnt/ssd/conda-envs/ComfyUI/bin/python3.12
ls -lh /mnt/ssd/ComfyUI/output/3D/medallion_necklace_*.glb
$P pa.py asset medallion_necklace /mnt/ssd/ComfyUI/output/3D/medallion_necklace_00001_.glb
```

**Caution:** The `asset` stage repairs and optimizes geometry, but will NOT reconstruct skull/rune detail lost in the raw GLB.

## Diagnose exact ComfyUI history output format

```bash
python inspect_history.py 09d66962-1bc4-45a6-beb9-42e075742ce7
```

That ID was from the interrupted run; a newer prompt id may be more useful. ComfyUI history can be missing after restart. This is a read-only diagnostic.

## Geometry problems: critical findings from user log

- `DummyNAFModel (bilinear fallback)` is present even while the console claims "STRICT NAF". This warrants examining the actual custom-node path. The run may not be using the intended learned high-resolution feature processing.
- The chain takes substantial canvas space; remove it from all four multiview inputs for a pendant-only reconstruction, then create linked chain components procedurally in Blender.
- Source geometry has visibly melted relief and severe cleanup: 1,475 holes found. No decimator can recover original skull/engraving shapes that were never reconstructed.
- Preserve the original `medallion_necklace` images/GLB. Use a new name for a pendant-only rerun, so the old file is not overwritten.

The patch is tested against the archived `pixal3d-mv/pa.py` version from this chat, but not against your live system. It does not change the GGUF model, workflow settings, or VRAM flags.

## Optional: create pendant-only multiview images (to prioritize relief accuracy)

The existing split images contain an extremely long chain that consumes most of the field of view. To make a *new* set of 4 views that crops the long chain, copy `pendant_only_views.py` alongside `pa.py` and run:

```bash
P=/mnt/ssd/conda-envs/ComfyUI/bin/python3.12
$P pendant_only_views.py \
  /mnt/ssd/ComfyUI/input/medallion_necklace_views \
  /mnt/ssd/ComfyUI/input/medallion_pendant_views
```

Inspect the 4 output PNGs before running `$P pa.py mesh medallion_pendant`. The utility crops a common vertical interval to preserve multi-view scale, estimates horizontal center from the pendant body, and tapers away outer chain links near the bail. It does **not** invent missing detail or guarantee perfect masking. Adjust `--top 0.34` and `--bottom 0.94` if needed.

For a different look at real high-res conditioning (optional and potentially much more memory-intensive), run a controlled test by setting `TRELLIS2_REAL_NAF=1` for the ComfyUI launcher. The supplied log explicitly says the current run uses `DummyNAFModel (bilinear fallback)`; enabling the learned NAF may improve quality but can fail with 16GB VRAM. This environment change is **not** part of the patch.
