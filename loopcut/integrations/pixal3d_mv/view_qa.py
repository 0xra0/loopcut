"""Turnaround-view cleanup and checks, before and after Pixal3D.

  view_qa.py check <views_dir>              mirror/scale consistency of front/left/back/right -> JSON
  view_qa.py mesh  <views_dir> <mesh.glb>   mesh silhouettes along the view axes vs the views, plus
                                            fragments/holes/non-manifold edges and a ranking score -> JSON

Orthographic facts the checks rely on: the back silhouette is the front one mirrored, the right is the
left mirrored, and all four share one height (the camera orbits a vertical axis). Silhouettes are
compared after scaling to a common height, so the result does not depend on framing.
Needs numpy + PIL; the mesh command also needs trimesh (the ComfyUI env has it).
"""
import json
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

VIEWS = ("front", "left", "back", "right")
ALPHA = 32          # alpha above this counts as object
GRID = 256          # comparison height in pixels
# Below these, the views (or the mesh) disagree enough to break or distort a reconstruction.
MIN_MIRROR_IOU = 0.80
MAX_HEIGHT_SPREAD = 0.04
MIN_COVERAGE = 0.10
MIN_SIDE_COVERAGE = 0.01
MIN_MESH_IOU = 0.75


def mask(image):
    return np.asarray(image.convert("RGBA"))[..., 3] > ALPHA


def is_cut_out(image, share=0.01):
    """Already background-free: at least `share` of the pixels are transparent. Qwen saves RGBA views whose alpha
    is 247-255 everywhere, which a "min alpha < 255" test took for a cut-out."""
    return image.mode == "RGBA" and float((~mask(image)).mean()) >= share


def bbox(m):
    rows, cols = np.flatnonzero(m.any(1)), np.flatnonzero(m.any(0))
    if not len(rows):
        return None
    return int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1


def align(m, height=GRID):
    """Crop to the silhouette, scale to `height`, centre on a 2*height-wide canvas."""
    box = bbox(m)
    canvas = np.zeros((height, 2 * height), bool)
    if box is None:
        return canvas
    y0, y1, x0, x1 = box
    width = max(1, min(2 * height, round((x1 - x0) * height / (y1 - y0))))
    crop = Image.fromarray(m[y0:y1, x0:x1].astype(np.uint8) * 255).resize((width, height), Image.Resampling.BILINEAR)
    left = (2 * height - width) // 2
    canvas[:, left:left + width] = np.asarray(crop) > 127
    return canvas


def iou(a, b):
    union = np.logical_or(a, b).sum()
    return float(np.logical_and(a, b).sum() / union) if union else 0.0


def consistency(masks):
    """masks: dict view -> bool array (same pixel scale). Returns metrics and issues."""
    issues, boxes = [], {v: bbox(masks[v]) for v in VIEWS}
    empty = [v for v in VIEWS if boxes[v] is None]
    if empty:
        return {"passed": False, "issues": [f"empty view: {v}" for v in empty]}
    heights = {v: boxes[v][1] - boxes[v][0] for v in VIEWS}
    widths = {v: boxes[v][3] - boxes[v][2] for v in VIEWS}
    spread = (max(heights.values()) - min(heights.values())) / max(heights.values())
    coverage = {v: float(masks[v].mean()) for v in VIEWS}
    mirror = {"front_back": iou(align(masks["front"]), align(np.fliplr(masks["back"]))),
              "left_right": iou(align(masks["left"]), align(np.fliplr(masks["right"])))}
    if spread > MAX_HEIGHT_SPREAD:
        issues.append(f"object height differs between views by {spread:.1%}: scale drift, or a part (collar, tip, "
                      "finial) drawn in one view and missing in another")
    for pair, value in mirror.items():
        if value < MIN_MIRROR_IOU:
            issues.append(f"{pair.replace('_', '/')} silhouettes are not mirror images (IoU {value:.2f}): "
                          "inconsistent proportions or a rotated view")
    for v in VIEWS:   # a thin object's side view (medallion, blade edge) is legitimately a sliver
        if coverage[v] < (MIN_COVERAGE if v in ("front", "back") else MIN_SIDE_COVERAGE):
            issues.append(f"{v} covers only {coverage[v]:.1%} of its canvas")
    return {"passed": not issues, "issues": issues, "height_px": heights, "width_px": widths,
            "height_spread": spread, "mirror_iou": mirror, "coverage": coverage}


def defringe(rgba, erode=1, background=255.0):
    """Remove the white halo rembg leaves: un-mix edge colours from the white background, trim the rim."""
    data = np.asarray(rgba.convert("RGBA")).astype(np.float32)
    rgb, alpha = data[..., :3], data[..., 3:] / 255.0
    edge = (alpha > 0) & (alpha < 1)
    unmixed = (rgb - (1 - alpha) * background) / np.maximum(alpha, 1e-3)
    rgb = np.where(edge, np.clip(unmixed, 0, 255), rgb)
    out = Image.fromarray(np.concatenate([rgb, alpha * 255], -1).round().astype(np.uint8), "RGBA")
    if erode:
        out.putalpha(out.getchannel("A").filter(ImageFilter.MinFilter(2 * erode + 1)))
    return out


def normalize(views, size, height=None, bottom=None):
    """Same foreground scale and baseline in every view, each centred horizontally on a size x size canvas.
    height/bottom default to the front view's, so separately generated views match the front."""
    boxes = {v: bbox(mask(views[v])) for v in views}
    if any(b is None for b in boxes.values()):
        raise ValueError("a view has no foreground: " + ", ".join(v for v, b in boxes.items() if b is None))
    y0, y1, _, _ = boxes["front"]
    height = height or (y1 - y0)
    bottom = bottom or y1
    out = {}
    for v, image in views.items():
        b0, b1, x0, x1 = boxes[v]
        scale = height / (b1 - b0)
        crop = image.crop((x0, b0, x1, b1))
        crop = crop.resize((max(1, round(crop.width * scale)), height), Image.Resampling.LANCZOS)
        if crop.width > size:
            raise ValueError(f"{v} is wider than the canvas after matching the front's height")
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(crop, ((size - crop.width) // 2, bottom - height))
        out[v] = canvas
    return out


# --- reference matching against a generated mesh (item 13) ---------------------------------------
# Raw Pixal3D GLBs as trimesh loads them. Pixal3D's world is Z-up with the front camera at (0,-d,0); the object
# faces that camera, so its LEFT side is at +X, where the az90 camera sits (pa.generate feeds left.png there);
# the exported file is that world turned 180 degrees about X (x, -y, -z):
# up = file -Z, image-right = file +X (front), -Y (left), -X (back), +Y (right).
# Each view: (horizontal axis, sign), (vertical axis, sign) of the image plane, in file coordinates.
CAMERAS = {"front": ((0, 1), (2, -1)), "left": ((1, -1), (2, -1)),
           "back": ((0, -1), (2, -1)), "right": ((1, 1), (2, -1))}


def project(vertices, faces, horizontal, vertical, size=512):
    """Rasterise the mesh's orthographic silhouette onto a size x size canvas (the mesh fills 90%)."""
    (hx, hs), (vx, vs) = horizontal, vertical
    u, w = vertices[:, hx] * hs, -vertices[:, vx] * vs           # image y grows downward
    span = max(np.ptp(u), np.ptp(w)) or 1.0
    scale = 0.9 * size / span
    u = (u - (u.min() + u.max()) / 2) * scale + size / 2
    w = (w - (w.min() + w.max()) / 2) * scale + size / 2
    image = Image.new("1", (size, size), 0)
    draw = ImageDraw.Draw(image)
    points = np.stack([u, w], 1)[faces]                           # (F, 3, 2)
    for tri in points.tolist():
        draw.polygon([tuple(p) for p in tri], fill=1)
    return np.asarray(image, bool)


def topology(vertices, faces):
    """Raw-mesh geometry QA (item 5) on position-welded vertices: fragments, holes, non-manifold edges.
    A fragment is a shell whose bounding box is under 2% of the mesh diagonal: dust, not a chain link."""
    import trimesh
    mesh = trimesh.Trimesh(vertices, faces, process=False)
    mesh.merge_vertices()
    _, counts = np.unique(np.sort(mesh.edges, axis=1), axis=0, return_counts=True)
    labels = trimesh.graph.connected_component_labels(mesh.face_adjacency, node_count=len(mesh.faces))
    diagonal = float(np.linalg.norm(np.ptp(mesh.vertices, 0))) or 1.0
    tri = mesh.vertices[mesh.faces]                                   # (F, 3, 3)
    lo = np.full((labels.max() + 1, 3), np.inf); hi = np.full_like(lo, -np.inf)
    np.minimum.at(lo, labels, tri.min(1)); np.maximum.at(hi, labels, tri.max(1))
    small = np.linalg.norm(hi - lo, axis=1) < 0.02 * diagonal
    sizes = np.bincount(labels)
    return {"faces": int(len(mesh.faces)), "components": int(len(sizes)), "fragments": int(small.sum()),
            "fragment_share": float(sizes[small].sum() / len(mesh.faces)),
            "boundary_edge_ratio": float((counts == 1).sum() / len(counts)),
            "nonmanifold_edge_ratio": float((counts > 2).sum() / len(counts))}


def topology_issues(topo):
    issues = []
    if topo["fragment_share"] > 0.01:
        issues.append(f"{topo['fragments']} loose fragments ({topo['fragment_share']:.1%} of faces)")
    if topo["boundary_edge_ratio"] > 0.005:
        issues.append(f"open surface: {topo['boundary_edge_ratio']:.2%} boundary edges (holes)")
    if topo["nonmanifold_edge_ratio"] > 0.005:
        issues.append(f"{topo['nonmanifold_edge_ratio']:.2%} non-manifold edges")
    return issues


def mesh_score(report):
    """Rank reconstruction candidates; higher is better. The reference match dominates (mean plus the
    worst view, so one melted side is not averaged away); geometry defects subtract."""
    worst = min(v["iou"] for v in report["views"].values())
    topo = report["topology"]
    return (report["mean_iou"] + 0.5 * worst - 2.0 * topo["fragment_share"]
            - 10.0 * topo["boundary_edge_ratio"] - 10.0 * topo["nonmanifold_edge_ratio"])


def mesh_match(views_dir, glb, cameras=CAMERAS):
    import trimesh
    mesh = trimesh.load(glb, force="mesh", process=False)
    vertices, faces = np.asarray(mesh.vertices, float), np.asarray(mesh.faces)
    result, issues = {}, []
    for v in VIEWS:
        ref = align(mask(Image.open(f"{views_dir}/{v}.png")))
        got = align(project(vertices, faces, *cameras[v]))
        missing = float((ref & ~got).sum() / max(ref.sum(), 1))    # reference pixels the mesh does not cover
        extra = float((got & ~ref).sum() / max(got.sum(), 1))
        result[v] = {"iou": iou(ref, got), "missing": missing, "extra": extra}
        if result[v]["iou"] < MIN_MESH_IOU:
            issues.append(f"{v}: mesh silhouette matches the reference poorly (IoU {result[v]['iou']:.2f}, "
                          f"{missing:.0%} of the reference missing)")
    mean = sum(r["iou"] for r in result.values()) / len(result)
    topo = topology(vertices, faces)
    issues += topology_issues(topo)
    report = {"passed": not issues, "issues": issues, "mean_iou": mean, "views": result, "topology": topo}
    report["score"] = mesh_score(report)
    return report


def load_views(views_dir):
    return {v: mask(Image.open(f"{views_dir}/{v}.png")) for v in VIEWS}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "check":
        report = consistency(load_views(sys.argv[2]))
    elif len(sys.argv) == 4 and sys.argv[1] == "mesh":
        report = mesh_match(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
    print(json.dumps(report, indent=1))
    sys.exit(0 if report["passed"] else 2)
