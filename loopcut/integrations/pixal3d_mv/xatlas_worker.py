"""xatlas chart + pack worker, run in a Python that has xatlas (Blender's does not).

stdin: .npz with float32 positions (V, 3) and uint32 triangles (F, 3).
stdout: .npy float32 per-corner UVs (F, 3, 2), in input triangle and corner order.
argv: texture resolution, padding in texels.
"""
import io
from pathlib import Path
import sys

import numpy as np
import xatlas

sys.path.insert(0, str(Path(__file__).resolve().parent))
from uv_quality import area, overlaps  # noqa: E402

resolution, padding = int(sys.argv[1]), int(round(float(sys.argv[2])))
data = np.load(io.BytesIO(sys.stdin.buffer.read()), allow_pickle=False)
positions, triangles = data["positions"].astype(np.float64), data["triangles"]
# xatlas uses absolute epsilons; at real-world scale (a 0.5 m necklace) sub-millimetre
# triangles come back collapsed. UVs are scale-free, so chart a 100-unit copy.
positions = ((positions - positions.min(0)) * (100 / max(np.ptp(positions, 0).max(), 1e-30))).astype(np.float32)


def generate(split):
    """Atlas with each split triangle as its own mesh: xatlas welds colocal vertices within a mesh."""
    split = sorted(split)
    keep = np.setdiff1d(np.arange(len(triangles)), split)
    atlas = xatlas.Atlas()
    atlas.add_mesh(positions, triangles[keep])
    for face in split:
        atlas.add_mesh(positions[triangles[face]], np.arange(3, dtype=np.uint32).reshape(1, 3))
    pack = xatlas.PackOptions()
    pack.resolution = resolution
    pack.padding = padding
    pack.bilinear = True
    pack.rotate_charts = True
    atlas.generate(xatlas.ChartOptions(), pack)
    if atlas.atlas_count != 1:
        sys.exit(f"xatlas needed {atlas.atlas_count} atlases at {resolution}px")
    result = np.empty((len(triangles), 3, 2), dtype=np.float32)
    vmapping, indices, uvs = atlas[0]
    if indices.shape != (len(keep), 3) or not np.array_equal(vmapping[indices], triangles[keep]):
        sys.exit("xatlas changed triangle order or corners")
    result[keep] = uvs[indices]
    for mesh, face in enumerate(split, 1):
        vmapping, indices, uvs = atlas[mesh]
        result[face] = uvs[indices[0][np.argsort(vmapping[indices[0]])]]
    return result, atlas


# xatlas charts can still fold on noisy scans; cutting the folded faces loose and
# re-charting keeps one xatlas pack instead of a lossy repack afterwards.
split, bad = set(), set()
for attempt in range(8):
    uvs, atlas = generate(split)
    tris = [tuple(map(tuple, tri.astype(np.float64))) for tri in uvs]
    quality = overlaps(tris)
    if quality["uv_overlap_check"] != "complete":
        sys.exit("UV overlap check budget exceeded")
    bad = set(quality["uv_overlap_faces"]) | {i for i, tri in enumerate(tris) if area(tri) <= 1e-18}
    print("xatlas pass", attempt, "charts", atlas.chart_count, "utilization", round(float(atlas.utilization), 4),
          "folded", len(bad), file=sys.stderr)
    if not bad:
        break
    split |= bad
else:
    sys.exit(f"xatlas still folds {len(bad)} faces")
out = io.BytesIO()
np.save(out, uvs, allow_pickle=False)
sys.stdout.buffer.write(out.getvalue())
