"""Item 4: split a Pixal3D mesh into named parts (shell, skulls, gem, medallion...) using the turnaround views.

  parts.py detect <views_dir> <spec.json> <out_dir> [--device cpu|cuda]
      Florence-2 finds each part by name in each view, SAM cuts its mask
      -> out_dir/labels_<view>.png (one grey level per label), overlay_<view>.png, detect.json
  parts.py label <mesh.glb> <spec.json> <out_dir>
      votes detect's 2D labels (out_dir/labels_<view>.png) onto mesh faces through the view cameras
      -> out_dir/faces.npy (label per face), parts.glb (one node per part), parts.json, transfer_<view>.png

A Pixal3D mesh is one fused surface, so parts cannot come from mesh connectivity: a skull is the same
shell as the plate it sits on. The views say which pixels are a skull, and the orthographic view cameras
say which face each pixel sees, so the labels are cast onto the faces with one ray per pixel.

Spec (<name>_parts.json next to pa.py):
  {"parts": [{"name": "skull", "prompt": "skull", "kind": "ornament", "max": 2}, ...]}
kind steers the later stages: plate (broad armor), ornament (sculpted relief), round (disc/bezel), gem, chain.
Labels: 0 = background, 1 = shell (object pixels no part claims), 2.. = the spec's parts in order.
"""
import json
import os
import sys

import numpy as np
from PIL import Image

import view_qa as q

COMFY = os.environ.get("COMFY_DIR", "/mnt/ssd/ComfyUI")
FLORENCE = os.path.join(COMFY, "models", "LLM", "florence-community--Florence-2-large")
SAM = os.path.join(COMFY, "models", "sams", "sam_vit_b_01ec64.pth")
KINDS = ("plate", "ornament", "round", "gem", "chain")
MAX_BOX_SHARE = 0.6      # a "part" box covering more of the object than this is the whole object: reject
MIN_PART_PIXELS = 150    # SAM masks smaller than this are noise at 1024 px


def load_spec(path):
    with open(path) as stream:
        spec = json.load(stream)
    parts = spec.get("parts")
    if not parts:
        sys.exit(f"{path}: needs a non-empty \"parts\" list")
    for p in parts:
        if not p.get("name") or p.get("kind") not in KINDS:
            sys.exit(f"{path}: every part needs a name and a kind in {KINDS}: {p}")
        p.setdefault("prompt", p["name"])
        p.setdefault("max", 4)
    return spec


def names(spec):
    return ["background", "shell"] + [p["name"] for p in spec["parts"]]


PALETTE = np.array([(0, 0, 0), (150, 150, 150), (230, 60, 60), (60, 200, 90), (70, 110, 240), (240, 200, 40),
                    (200, 80, 220), (40, 210, 220), (250, 140, 60), (140, 90, 40)], np.uint8)


def colorize(labels):
    return PALETTE[np.minimum(labels, len(PALETTE) - 1)]


# ---------------------------------------------------------------- detect (2D, needs torch)

def flatten(rgba, grey=128):
    """Composite on mid grey: both models were trained on opaque photos."""
    a = rgba[..., 3:4].astype(np.float32) / 255
    return (rgba[..., :3] * a + grey * (1 - a)).astype(np.uint8)


def plausible(boxes, alpha, limit):
    """Drop boxes that are the whole object or off the object; keep at most `limit`, largest first."""
    y0, y1, x0, x1 = q.bbox(alpha)
    object_area = (y1 - y0) * (x1 - x0)
    kept = []
    for b in boxes:
        bx0, by0, bx1, by1 = (int(round(c)) for c in b)
        area = max(0, bx1 - bx0) * max(0, by1 - by0)
        cx, cy = (bx0 + bx1) // 2, (by0 + by1) // 2
        if area == 0 or area > MAX_BOX_SHARE * object_area:
            continue
        if not (0 <= cy < alpha.shape[0] and 0 <= cx < alpha.shape[1]) or not alpha[cy, cx]:
            continue
        kept.append((area, [bx0, by0, bx1, by1]))
    return [b for _, b in sorted(kept, key=lambda t: -t[0])[:limit]]


def ground(model, processor, image, prompt, device):
    """Florence-2 phrase grounding: boxes (x0, y0, x1, y1) in pixels for `prompt`."""
    import torch
    task = "<CAPTION_TO_PHRASE_GROUNDING>"
    inputs = processor(text=task + prompt, images=Image.fromarray(image), return_tensors="pt").to(device)
    with torch.no_grad():
        ids = model.generate(**inputs, max_new_tokens=256, num_beams=3, do_sample=False)
    text = processor.batch_decode(ids, skip_special_tokens=False)[0]
    parsed = processor.post_process_generation(text, task=task, image_size=(image.shape[1], image.shape[0]))
    return parsed[task]["bboxes"]


def resolve(alpha, masks):
    """Paint part masks over the shell, largest first, so a smaller, more specific part (the gem inside
    the medallion's box) wins where two masks overlap."""
    labels = alpha.astype(np.uint8)
    for _, label, m in sorted(masks, key=lambda t: -t[0]):
        labels[m & alpha] = label
    return labels


def detect(views_dir, spec_path, out_dir, device="cpu"):
    import torch
    from transformers import AutoProcessor, Florence2ForConditionalGeneration
    from segment_anything import SamPredictor, sam_model_registry
    spec = load_spec(spec_path)
    os.makedirs(out_dir, exist_ok=True)
    processor = AutoProcessor.from_pretrained(FLORENCE)
    model = Florence2ForConditionalGeneration.from_pretrained(FLORENCE, torch_dtype=torch.float32).to(device).eval()
    predictor = SamPredictor(sam_model_registry["vit_b"](checkpoint=SAM).to(device))
    report = {}
    for view in q.VIEWS:
        rgba = np.asarray(Image.open(os.path.join(views_dir, f"{view}.png")).convert("RGBA"))
        alpha = rgba[..., 3] > q.ALPHA
        rgb = flatten(rgba)
        predictor.set_image(rgb)
        masks, found = [], []
        for label, part in enumerate(spec["parts"], start=2):
            boxes = plausible(ground(model, processor, rgb, part["prompt"], device), alpha, part["max"])
            for box in boxes:
                m = predictor.predict(box=np.array(box), multimask_output=False)[0][0] & alpha
                if m.sum() >= MIN_PART_PIXELS:
                    masks.append((int(m.sum()), label, m))
                    found.append({"part": part["name"], "box": box, "pixels": int(m.sum())})
        labels = resolve(alpha, masks)
        Image.fromarray(labels).save(os.path.join(out_dir, f"labels_{view}.png"))
        overlay = (0.5 * rgb + 0.5 * colorize(labels)).astype(np.uint8)
        Image.fromarray(overlay).save(os.path.join(out_dir, f"overlay_{view}.png"))
        report[view] = found
        print(f"  {view}: " + (", ".join(f"{f['part']} {f['pixels']}px" for f in found) or "no parts"))
    with open(os.path.join(out_dir, "detect.json"), "w") as stream:
        json.dump(report, stream, indent=1)
    return report


# ---------------------------------------------------------------- label (3D, numpy + open3d)

def camera_axes(camera):
    """Unit right/up/forward vectors in file coordinates for a view_qa camera ((hx, hs), (vx, vs))."""
    (hx, hs), (vx, vs) = camera
    right, up = np.zeros(3), np.zeros(3)
    right[hx], up[vx] = hs, vs          # view_qa: w = -x[vx]*vs grows downward, so image-up is +vs along vx
    return right, up, np.cross(up, right)


def pixel_rays(alpha, vertices, camera):
    """One ray per object pixel. The view's silhouette box is matched to the mesh's projected box the way
    view_qa.align does it: same height, centred horizontally, tops level."""
    right, up, forward = camera_axes(camera)
    u, w = vertices @ right, -(vertices @ up)                   # image coordinates of the mesh, y down
    y0, y1, x0, x1 = q.bbox(alpha)
    scale = np.ptp(w) / (y1 - y0)                               # mesh units per pixel
    rows, cols = np.nonzero(alpha)
    pu = (u.min() + u.max()) / 2 + (cols + 0.5 - (x0 + x1) / 2) * scale
    pw = w.min() + (rows + 0.5 - y0) * scale
    centre = (vertices.min(0) + vertices.max(0)) / 2
    far = 2 * float(np.linalg.norm(np.ptp(vertices, 0))) + 1.0
    origins = (np.outer(pu, right) - np.outer(pw, up) + (centre @ forward - far) * forward)
    return origins, forward, rows, cols


def cast(vertices, faces, origins, direction):
    import open3d as o3d
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(vertices.astype(np.float32)), o3d.core.Tensor(faces.astype(np.uint32)))
    rays = np.hstack([origins, np.broadcast_to(direction, origins.shape)]).astype(np.float32)
    hit = scene.cast_rays(o3d.core.Tensor(rays))
    ids = hit["primitive_ids"].numpy().astype(np.int64)
    ids[ids == scene.INVALID_ID] = -1
    return ids, hit["primitive_normals"].numpy()


def vote(n_faces, n_labels, ids, labels, weights):
    votes = np.zeros((n_faces, n_labels))
    ok = ids >= 0
    np.add.at(votes, (ids[ok], labels[ok]), weights[ok])
    return votes


def propagate(label, adjacency, unknown=-1, rounds=200):
    """Faces no ray hit (hidden folds, inner walls) take the most common label of their labelled neighbours."""
    label = label.copy()
    a, b = adjacency[:, 0], adjacency[:, 1]
    n_labels = int(label.max()) + 1
    for _ in range(rounds):
        todo = label == unknown
        if not todo.any():
            break
        counts = np.zeros((len(label), n_labels))
        for src, dst in ((a, b), (b, a)):
            m = (label[src] != unknown) & (label[dst] == unknown)
            np.add.at(counts, (dst[m], label[src[m]]), 1)
        reached = counts.sum(1) > 0
        if not reached.any():
            break
        label[reached] = counts[reached].argmax(1)
    label[label == unknown] = 1                                 # unreachable islands are shell
    return label


def absorb_islands(label, adjacency, min_faces):
    """Connected same-label patches under `min_faces` faces are noise at a part border: give them the
    label most of their neighbours have. The shell is never dissolved into a part."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    a, b = adjacency[:, 0], adjacency[:, 1]
    same = label[a] == label[b]
    n = len(label)
    graph = coo_matrix((np.ones(same.sum()), (a[same], b[same])), shape=(n, n))
    _, comp = connected_components(graph, directed=False)
    sizes = np.bincount(comp)
    small = (sizes[comp] < min_faces) & (label > 1)
    if not small.any():
        return label
    counts = np.zeros((comp.max() + 1, int(label.max()) + 1))
    for src, dst in ((a, b), (b, a)):
        m = small[dst] & (comp[src] != comp[dst])
        np.add.at(counts, (comp[dst[m]], label[src[m]]), 1)
    label = label.copy()
    fix = small & (counts[comp].sum(1) > 0)
    label[fix] = counts[comp[fix]].argmax(1)
    return label


def face_labels(view_labels, vertices, faces, adjacency, cameras=q.CAMERAS, min_island=None):
    """Per-face label from per-view label maps. Votes are weighted by how squarely the face faces the
    camera, so grazing hits on a side wall do not outvote the view that sees the face head-on."""
    n_labels = int(max(m.max() for m in view_labels.values())) + 1
    votes = np.zeros((len(faces), n_labels))
    hits = {}
    for view, labels in view_labels.items():
        origins, forward, rows, cols = pixel_rays(labels > 0, vertices, cameras[view])
        ids, normals = cast(vertices, faces, origins, forward)
        weights = np.abs(normals @ forward)
        votes += vote(len(faces), n_labels, ids, labels[rows, cols].astype(np.int64), weights)
        hits[view] = (ids, rows, cols)
    label = np.where(votes.sum(1) > 0, votes.argmax(1), -1)
    label = propagate(label, adjacency)
    label = absorb_islands(label, adjacency, min_island or max(20, len(faces) // 5000))
    return label, hits


def label_mesh(glb, spec_path, out_dir):
    import trimesh
    spec = load_spec(spec_path)
    part_names = names(spec)
    mesh = trimesh.load(glb, force="mesh", process=False)
    vertices, faces = np.asarray(mesh.vertices, float), np.asarray(mesh.faces)
    welded = trimesh.Trimesh(vertices, faces, process=False)
    welded.merge_vertices(merge_tex=True, merge_norm=True)     # UV seams split vertices; adjacency needs them joined
    view_labels = {v: np.asarray(Image.open(os.path.join(out_dir, f"labels_{v}.png"))) for v in q.VIEWS}
    label, hits = face_labels(view_labels, vertices, faces, welded.face_adjacency)
    np.save(os.path.join(out_dir, "faces.npy"), label.astype(np.int16))
    for view, (ids, rows, cols) in hits.items():               # what the mesh now says, seen from each view
        image = np.zeros(view_labels[view].shape, np.uint8)
        ok = ids >= 0
        image[rows[ok], cols[ok]] = label[ids[ok]]
        side = np.hstack([colorize(view_labels[view]), colorize(image)])
        Image.fromarray(side).save(os.path.join(out_dir, f"transfer_{view}.png"))
    scene, report = trimesh.Scene(), {"mesh": glb, "faces": int(len(faces)), "parts": {}}
    area = mesh.area_faces
    kinds = {p["name"]: p["kind"] for p in spec["parts"]}
    for index in np.unique(label):
        part = part_names[index]
        chosen = np.flatnonzero(label == index)
        scene.add_geometry(mesh.submesh([chosen], append=True), node_name=part, geom_name=part)
        report["parts"][part] = {"label": int(index), "kind": kinds.get(part, "plate"), "faces": int(len(chosen)),
                                 "area_share": float(area[chosen].sum() / area.sum())}
    missing = [p for p in part_names[2:] if p not in report["parts"]]
    report["missing"] = missing
    scene.export(os.path.join(out_dir, "parts.glb"))
    with open(os.path.join(out_dir, "parts.json"), "w") as stream:
        json.dump(report, stream, indent=1)
    return report


if __name__ == "__main__":
    args = sys.argv[1:]
    device = "cpu"
    if "--device" in args:
        i = args.index("--device")
        device = args[i + 1]
        del args[i:i + 2]
    if len(args) == 4 and args[0] == "detect":
        detect(args[1], args[2], args[3], device)
    elif len(args) == 4 and args[0] == "label":
        print(json.dumps(label_mesh(*args[1:]), indent=1))
    else:
        sys.exit(__doc__)
