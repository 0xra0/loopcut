"""reference toolset: build to match a picture.

set_reference ties a picture to world space: the subject's outline is found in the image, its
lowest point is put on base_z and its height scaled to `height` meters, centred on the view's
horizontal axis. compare_to_reference then renders the model with an orthographic camera that
covers exactly the same area, so the picture and the render line up pixel for pixel and can be
compared by eye (side by side and overlaid) and by number (silhouette overlap, width per height).
silhouette_mesh builds a starting mesh whose outline matches the picture(s).
"""

import base64
import json
import math
import os
import tempfile

import bpy
import numpy as np
from mathutils import Euler, Vector

from . import CommandError, command
from .look import color_type
from .util import get_object, link_to_scene

# For each view: the camera's direction from the subject, and the world axis that runs to the
# right in the picture (as a vector). Pictures are upright, so up is always world +Z.
VIEWS = {
    "front": (Vector((0, -1, 0)), Vector((1, 0, 0))),
    "back": (Vector((0, 1, 0)), Vector((-1, 0, 0))),
    "right": (Vector((1, 0, 0)), Vector((0, 1, 0))),
    "left": (Vector((-1, 0, 0)), Vector((0, -1, 0))),
}
ALIASES = {"side": "right"}
STORE = "claude_references"
MASK_SIZE = 768  # longest side of the outline mask
_mask_cache = {}


def _view(view):
    view = ALIASES.get(view, view)
    if view not in VIEWS:
        raise CommandError(f"view must be one of {sorted(VIEWS) + sorted(ALIASES)}")
    return view


def _refs():
    return json.loads(bpy.context.scene.get(STORE, "{}"))


def _save_refs(refs):
    bpy.context.scene[STORE] = json.dumps(refs)


def _get_ref(view):
    ref = _refs().get(view)
    if ref is None:
        raise CommandError(f"No {view} reference. Call set_reference first")
    return ref


def _load_rgba(path):
    """Image as float RGBA rows top-down, shape (h, w, 4)."""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        if not w or not h:
            raise CommandError(f"Could not read the image {path}")
        px = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(px)
    finally:
        bpy.data.images.remove(img)
    return px.reshape(h, w, 4)[::-1]


def _resize(arr, h, w):
    """Nearest-neighbour resize of a 2D or 3D array."""
    rows = np.minimum((np.arange(h) + 0.5) * arr.shape[0] / h, arr.shape[0] - 1).astype(int)
    cols = np.minimum((np.arange(w) + 0.5) * arr.shape[1] / w, arr.shape[1] - 1).astype(int)
    return arr[rows][:, cols]


def _background_fill(rgb, tol):
    """Pixels reachable from the image border through background-coloured pixels."""
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    ref = np.median(border, axis=0)
    # Background must stay close to the border colour, and change smoothly between neighbours,
    # which follows a gradient or vignette but stops at the subject's edge.
    near = np.linalg.norm(rgb - ref, axis=2) < tol * 2.5
    bg = np.zeros(near.shape, bool)
    bg[0], bg[-1], bg[:, 0], bg[:, -1] = near[0], near[-1], near[:, 0], near[:, -1]
    smooth_h = np.linalg.norm(rgb[:, 1:] - rgb[:, :-1], axis=2) < tol
    smooth_v = np.linalg.norm(rgb[1:] - rgb[:-1], axis=2) < tol
    while True:
        grow = bg.copy()
        grow[:, 1:] |= bg[:, :-1] & smooth_h
        grow[:, :-1] |= bg[:, 1:] & smooth_h
        grow[1:] |= bg[:-1] & smooth_v
        grow[:-1] |= bg[1:] & smooth_v
        grow &= near
        if (grow == bg).all():
            return bg
        bg = grow


def _subject_mask(path, tolerance=0.06):
    """Boolean mask of the subject (rows top-down) at up to MASK_SIZE px, and the full-size RGBA."""
    key = (path, os.path.getmtime(path), tolerance)
    if key in _mask_cache:
        return _mask_cache[key]
    rgba = _load_rgba(path)
    h, w = rgba.shape[:2]
    scale = min(1.0, MASK_SIZE / max(h, w))
    small = _resize(rgba, max(1, round(h * scale)), max(1, round(w * scale)))
    alpha = small[..., 3]
    if (alpha < 0.5).mean() > 0.02:  # cut-out with transparency: use it
        mask = alpha >= 0.5
    else:
        mask = ~_background_fill(small[..., :3], tolerance)
    if mask.sum() < 50:
        raise CommandError("Could not separate the subject from the background. Use a picture with a plain "
                           "background, or a PNG with the background removed")
    _mask_cache.clear()
    _mask_cache[key] = (mask, rgba)
    return mask, rgba


def _profile(mask, ref, slices=24):
    """Silhouette width at evenly spaced heights: [{z, spans: [[a, b], ...]}] in world units."""
    rows = np.nonzero(mask.any(axis=1))[0]
    out = []
    for r in np.linspace(rows[0], rows[-1], slices).round().astype(int):
        cols = np.nonzero(mask[r])[0]
        spans = []
        if len(cols):
            breaks = np.nonzero(np.diff(cols) > 1)[0]
            starts = np.concatenate([[cols[0]], cols[breaks + 1]])
            ends = np.concatenate([cols[breaks], [cols[-1]]])
            spans = [[round(_col_to_h(a, ref), 4), round(_col_to_h(b + 1, ref), 4)] for a, b in zip(starts, ends)]
        out.append({"z": round(_row_to_z(r + 0.5, ref), 4), "spans": spans})
    return out


def _row_to_z(row, ref):
    return ref["base_z"] + (ref["bottom_row"] - row) * ref["mpp"]


def _col_to_h(col, ref):
    return (col - ref["center_col"]) * ref["mpp"]


def _frame(ref):
    """World rectangle the whole picture covers: horizontal (left, right) and z (bottom, top)."""
    return (_col_to_h(0, ref), _col_to_h(ref["mask_w"], ref),
            _row_to_z(ref["mask_h"], ref), _row_to_z(0, ref))


def _crop_to_file(path, crop, view):
    """Save the cropped part of a picture (crop = [left, top, right, bottom] in 0-1) as a PNG."""
    left, top, right, bottom = (float(c) for c in crop)
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise CommandError("crop is [left, top, right, bottom] in 0-1, with left < right and top < bottom")
    rgba = _load_rgba(path)
    h, w = rgba.shape[:2]
    part = rgba[round(top * h):round(bottom * h), round(left * w):round(right * w)]
    folder = os.path.join(tempfile.gettempdir(), "claude_references")
    os.makedirs(folder, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    out = os.path.join(folder, f"{stem}_{view}_{'_'.join(f'{c:.3f}' for c in (left, top, right, bottom))}.png")
    _write_png(part, out)
    return out


@command("set_reference", "reference")
def set_reference(image_path, view="front", height=1.8, base_z=0.0, crop=None, tolerance=0.06):
    """Tie a picture to world space for one view (front, back, left, right; side = right).

    The subject is cut out of the background, its lowest point placed at base_z and its height
    scaled to `height` meters (the real height of what the picture shows: a full body, or the
    top of the head to the bottom of the crop for a bust). For a turnaround sheet with several
    views in one picture, crop = [left, top, right, bottom] in 0-1 picks this view's part.
    Shows the picture as a reference image in the viewport. Returns the cut-out size and a width
    profile (spans per height in meters)."""
    view = _view(view)
    path = bpy.path.abspath(os.path.expanduser(image_path))
    if not os.path.isfile(path):
        raise CommandError(f"No image at {path}")
    source = path
    if crop:
        path = _crop_to_file(path, crop, view)
    mask, rgba = _subject_mask(path, float(tolerance))
    rows = np.nonzero(mask.any(axis=1))[0]
    cols = np.nonzero(mask.any(axis=0))[0]
    top, bottom = int(rows[0]), int(rows[-1]) + 1
    mpp = float(height) / (bottom - top)
    ref = {
        "path": path, "source": source, "crop": list(crop) if crop else None, "view": view, "height": float(height), "base_z": float(base_z), "tolerance": float(tolerance),
        "mask_w": int(mask.shape[1]), "mask_h": int(mask.shape[0]), "mpp": mpp, "bottom_row": bottom,
        "center_col": (int(cols[0]) + int(cols[-1]) + 1) / 2,
        "touches_edge": {"top": bool(mask[0].any()), "bottom": bool(mask[-1].any()),
                         "left": bool(mask[:, 0].any()), "right": bool(mask[:, -1].any())},
    }
    refs = _refs()
    refs[view] = ref
    _save_refs(refs)
    _show_in_viewport(ref)
    left, right, z0, z1 = _frame(ref)
    return {
        "view": view,
        "subject_size_m": [round((int(cols[-1]) + 1 - int(cols[0])) * mpp, 4), round(float(height), 4)],
        "picture_covers": {"horizontal": [round(left, 4), round(right, 4)], "z": [round(z0, 4), round(z1, 4)]},
        "cut_off_at_edge": [k for k, v in ref["touches_edge"].items() if v],
        "fill": round(float(mask.mean()), 3),
        "profile": _profile(mask, ref),
    }


def _show_in_viewport(ref):
    name = f"Ref_{ref['view']}"
    old = bpy.data.objects.get(name)
    if old is not None:
        bpy.data.objects.remove(old)
    img = bpy.data.images.load(ref["path"], check_existing=True)
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = "IMAGE"
    obj.data = img
    left, right, z0, z1 = _frame(ref)
    obj.empty_display_size = max(right - left, z1 - z0)
    direction, h_axis = VIEWS[ref["view"]]
    # Image empties lie in their local XY plane: stand it up, turn it to face the camera, and
    # push it back behind the model.
    obj.rotation_euler = Euler((math.pi / 2, 0, math.atan2(h_axis.y, h_axis.x)))
    obj.location = h_axis * ((left + right) / 2) + Vector((0, 0, (z0 + z1) / 2)) - direction * (ref["height"] * 1.5)
    obj.empty_image_depth = "BACK"
    obj.use_empty_image_alpha = True
    obj.color[3] = 0.6
    obj.hide_render = True
    obj.hide_select = True
    obj["claude_reference"] = ref["view"]
    link_to_scene(obj)


@command("list_references", "reference")
def list_references():
    """The pictures set with set_reference and how they map to world space."""
    return [{"view": v, "path": r["path"], "height": r["height"], "base_z": r["base_z"],
             "meters_per_pixel": round(r["mpp"], 6)} for v, r in _refs().items()]


@command("reference_to_world", "reference")
def reference_to_world(view, points):
    """Convert spots in the picture to world coordinates. points: [[u, v], ...] with u 0-1 left to
    right and v 0-1 top to bottom of the whole picture. Returns [x, y, z] on the reference plane
    through the origin (front/back give x and z; left/right give y and z)."""
    ref = _get_ref(_view(view))
    _, h_axis = VIEWS[ref["view"]]
    out = []
    for u, v in points:
        h = _col_to_h(float(u) * ref["mask_w"], ref)
        z = _row_to_z(float(v) * ref["mask_h"], ref)
        p = h_axis * h + Vector((0, 0, z))
        out.append([round(c, 4) for c in p])
    return {"view": ref["view"], "points": out}


def _render_mask(ref, objects, res_w, res_h, path):
    """Orthographic Workbench render covering exactly the picture's area. Returns RGBA top-down."""
    scene = bpy.context.scene
    render = scene.render
    shade = scene.display.shading
    saved = {
        "camera": scene.camera, "engine": render.engine,
        "res": (render.resolution_x, render.resolution_y, render.resolution_percentage),
        "filepath": render.filepath, "format": render.image_settings.file_format,
        "mode": render.image_settings.color_mode, "film_transparent": render.film_transparent,
        "light": shade.light, "color_type": shade.color_type, "show_cavity": shade.show_cavity,
        "show_object_outline": shade.show_object_outline,
    }
    keep = {o.name for o in objects}
    hidden = [o for o in scene.objects if o.name not in keep and not o.hide_render]
    cam_data = bpy.data.cameras.new("_claude_ref_cam")
    cam = bpy.data.objects.new("_claude_ref_cam", cam_data)
    scene.collection.objects.link(cam)
    try:
        for o in hidden:
            o.hide_render = True
        render.engine = "BLENDER_WORKBENCH"
        render.resolution_x, render.resolution_y, render.resolution_percentage = res_w, res_h, 100
        render.image_settings.file_format = "PNG"
        render.image_settings.color_mode = "RGBA"
        render.film_transparent = True
        shade.light = "STUDIO"
        shade.color_type = color_type(objects)
        shade.show_cavity = True
        shade.show_object_outline = False
        left, right, z0, z1 = _frame(ref)
        direction, h_axis = VIEWS[ref["view"]]
        cam_data.type = "ORTHO"
        cam_data.ortho_scale = max(right - left, z1 - z0)
        corners = [o.matrix_world @ Vector(c) for o in objects for c in o.bound_box]
        reach = max((c.length for c in corners), default=1.0) + ref["height"]
        center = h_axis * ((left + right) / 2) + Vector((0, 0, (z0 + z1) / 2))
        cam.location = center + direction * reach * 2
        cam.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()
        cam_data.clip_start = 0.001
        cam_data.clip_end = reach * 4
        scene.camera = cam
        render.filepath = path
        bpy.ops.render.render(write_still=True)
        return _load_rgba(path)
    finally:
        for o in hidden:
            o.hide_render = False
        bpy.data.objects.remove(cam)
        bpy.data.cameras.remove(cam_data)
        scene.camera = saved["camera"]
        render.engine = saved["engine"]
        render.resolution_x, render.resolution_y, render.resolution_percentage = saved["res"]
        render.filepath = saved["filepath"]
        render.image_settings.file_format = saved["format"]
        render.image_settings.color_mode = saved["mode"]
        render.film_transparent = saved["film_transparent"]
        shade.light = saved["light"]
        shade.color_type = saved["color_type"]
        shade.show_cavity = saved["show_cavity"]
        shade.show_object_outline = saved["show_object_outline"]


def _write_png(rgba_topdown, path):
    h, w = rgba_topdown.shape[:2]
    img = bpy.data.images.new("_claude_compare", w, h, alpha=True)
    try:
        img.pixels.foreach_set(np.ascontiguousarray(rgba_topdown[::-1], dtype=np.float32).ravel())
        img.filepath_raw = path
        img.file_format = "PNG"
        img.save()
    finally:
        bpy.data.images.remove(img)


@command("compare_to_reference", "reference")
def compare_to_reference(view="front", objects=None, resolution=512):
    """Render the model exactly over the reference picture's area and compare.

    Returns one PNG with three panels: the picture, the model, and an overlay where red is model
    outside the picture's outline and blue is picture the model doesn't cover. Also returns the
    silhouette overlap (IoU, 1.0 = identical outline) and the heights where widths differ most."""
    ref = _get_ref(_view(view))
    if not os.path.isfile(ref["path"]):
        raise CommandError(f"The reference image moved or was deleted: {ref['path']}")
    if objects:
        objs = [get_object(n) for n in objects]
    else:
        objs = [o for o in bpy.context.scene.objects if o.type == "MESH" and o.visible_get()]
    if not objs:
        raise CommandError("No visible meshes to compare")
    mask, rgba = _subject_mask(ref["path"], ref["tolerance"])
    resolution = max(64, min(int(resolution), 2048))
    k = resolution / max(ref["mask_w"], ref["mask_h"])
    w, h = max(1, round(ref["mask_w"] * k)), max(1, round(ref["mask_h"] * k))
    tmp = tempfile.mkdtemp(prefix="claude_compare_")
    model = _render_mask(ref, objs, w, h, os.path.join(tmp, "model.png"))
    model_mask = model[..., 3] > 0.5
    ref_mask = _resize(mask, h, w)
    picture = _resize(rgba, h, w).copy()
    picture[..., 3] = 1.0

    union = (ref_mask | model_mask).sum()
    iou = float((ref_mask & model_mask).sum() / union) if union else 0.0

    # Panels: picture | model on white | overlay.
    model_panel = np.ones((h, w, 4), np.float32)
    a = model[..., 3:4]
    model_panel[..., :3] = model[..., :3] * a + (1 - a)
    overlay = picture.copy()
    overlay[..., :3] = overlay[..., :3] * 0.5 + 0.5
    overlay[model_mask & ref_mask, :3] = overlay[model_mask & ref_mask, :3] * 0.5 + np.array([0.1, 0.45, 0.1])
    overlay[model_mask & ~ref_mask, :3] = [0.9, 0.1, 0.1]
    overlay[~model_mask & ref_mask, :3] = [0.15, 0.3, 0.95]
    gap = np.ones((h, max(2, w // 64), 4), np.float32)
    out_path = os.path.join(tmp, "compare.png")
    _write_png(np.concatenate([picture, gap, model_panel, gap, overlay], axis=1), out_path)
    with open(out_path, "rb") as fh:
        png = base64.b64encode(fh.read()).decode("ascii")

    # Where the widths disagree most, in world units.
    scale = ref["mpp"] * ref["mask_w"] / w
    rows = np.nonzero(ref_mask.any(axis=1) | model_mask.any(axis=1))[0]
    diffs = []
    if len(rows):
        for r in np.linspace(rows[0], rows[-1], 32).round().astype(int):
            rw, mw = ref_mask[r].sum() * scale, model_mask[r].sum() * scale
            z = ref["base_z"] + (ref["bottom_row"] - (r + 0.5) * ref["mask_h"] / h) * ref["mpp"]
            diffs.append({"z": round(z, 3), "picture_width": round(rw, 4), "model_width": round(mw, 4),
                          "difference": round(mw - rw, 4)})
    diffs.sort(key=lambda d: -abs(d["difference"]))
    return {
        "view": ref["view"],
        "silhouette_iou": round(iou, 4),
        "model_outside_picture": round(float((model_mask & ~ref_mask).sum() / max(ref_mask.sum(), 1)), 4),
        "picture_not_covered": round(float((~model_mask & ref_mask).sum() / max(ref_mask.sum(), 1)), 4),
        "largest_width_differences": diffs[:6],
        "png_base64": png,
    }


@command("silhouette_mesh", "reference")
def silhouette_mesh(name="Silhouette", resolution=160, depth_ratio=0.75, max_depth_ratio=2.0, smooth=True):
    """Build a starting mesh whose outline matches the reference pictures (a visual hull).

    Each horizontal run of the front outline (a limb, the torso) gets an elliptical cross-section.
    With only a front, it is depth_ratio times as deep as it is wide. With a side view too, it is
    as deep as the side outline there, capped at max_depth_ratio times its width (arms in front
    of the torso would otherwise take the torso's depth). resolution is the number of cells over the height. The result is a
    watertight, remeshed blob: refine it with sculpting, it is not a finished shape."""
    from .topology import voxel_remesh

    refs = _refs()
    face = refs.get("front") or refs.get("back")
    side = refs.get("right") or refs.get("left")
    if face is None and side is None:
        raise CommandError("Set a front or side reference first")
    main = face or side
    cell = main["height"] / max(16, min(int(resolution), 400))
    z_lo, z_hi = main["base_z"], main["base_z"] + main["height"]

    def lookup(ref):
        mask, _ = _subject_mask(ref["path"], ref["tolerance"])
        _, h_axis = VIEWS[ref["view"]]
        left, right, _, _ = _frame(ref)
        return mask, ref, h_axis, (left, right)

    face_l = lookup(face) if face else None
    side_l = lookup(side) if side else None
    xs = _axis_cells(face_l, cell) if face_l else None
    ys = _axis_cells(side_l, cell) if side_l else None
    if xs is None:
        xs = ys  # side only: assume as wide as deep
    if ys is None:
        ys = xs
    zs = np.arange(z_lo + cell / 2, z_hi, cell)
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    filled = np.ones(X.shape, bool)
    if face_l:
        filled &= _inside(face_l, X, Y, Z, use_x=True)
    if side_l:
        filled &= _inside(side_l, X, Y, Z, use_x=False)
    if face_l:
        filled &= _ellipse_depth(face_l, side_l, xs, ys, zs, float(depth_ratio), float(max_depth_ratio))
    if not filled.any():
        raise CommandError("The outlines don't overlap; check the references' heights and base_z")
    filled = _blur_occupancy(filled)

    me = _voxel_surface(filled, xs[0] - cell / 2, ys[0] - cell / 2, zs[0] - cell / 2, cell, name)
    obj = bpy.data.objects.new(name, me)
    link_to_scene(obj)
    voxel_remesh(obj.name, voxel_size=cell * 0.75)
    if smooth:
        from .sculpt import smooth_mesh
        smooth_mesh(obj.name, iterations=12, factor=0.6)
    return {"name": obj.name, "cell_size": round(cell, 5), "faces": len(obj.data.polygons),
            "dimensions": [round(v, 4) for v in obj.dimensions], "views_used": [r["view"] for r in (face, side) if r]}


def _axis_cells(lookup, cell):
    _, _, _, (left, right) = lookup
    lo, hi = sorted((left, right))
    return np.arange(lo + cell / 2, hi, cell)


def _sample(lookup, H, Z):
    """Is the picture's subject at horizontal position H, height Z?"""
    mask, ref, _, _ = lookup
    col = np.floor(H / ref["mpp"] + ref["center_col"]).astype(int)
    row = np.floor(ref["bottom_row"] - (Z - ref["base_z"]) / ref["mpp"]).astype(int)
    ok = (col >= 0) & (col < mask.shape[1]) & (row >= 0) & (row < mask.shape[0])
    out = np.zeros(H.shape, bool)
    out[ok] = mask[row[ok], col[ok]]
    return out


def _inside(lookup, X, Y, Z, use_x):
    _, _, h_axis, _ = lookup
    # The picture's horizontal axis is ±X (front/back) or ±Y (left/right).
    H = X * h_axis.x if use_x else Y * h_axis.y
    return _sample(lookup, H, Z)


def _runs_along(lookup, coords, z):
    """For each world coordinate along the picture's horizontal axis at height z: the half-length
    and centre of the outline run it falls in (0, 0 outside the outline)."""
    mask, ref, h_axis, _ = lookup
    sign = h_axis.x + h_axis.y  # +1 or -1: which way the picture's axis runs in world space
    half = np.zeros(len(coords))
    centre = np.zeros(len(coords))
    row = int(np.floor(ref["bottom_row"] - (z - ref["base_z"]) / ref["mpp"]))
    if not 0 <= row < mask.shape[0]:
        return half, centre
    cols = np.nonzero(mask[row])[0]
    if not len(cols):
        return half, centre
    breaks = np.nonzero(np.diff(cols) > 1)[0]
    starts = np.concatenate([[cols[0]], cols[breaks + 1]])
    ends = np.concatenate([cols[breaks], [cols[-1]]]) + 1
    h = coords * sign
    for a, b in zip(starts, ends):
        lo, hi = _col_to_h(a, ref), _col_to_h(b, ref)
        sel = (h >= lo) & (h < hi)
        half[sel] = (hi - lo) / 2
        centre[sel] = (lo + hi) / 2 * sign
    return half, centre


def _ellipse_depth(face, side, xs, ys, zs, ratio, max_ratio):
    """Elliptical cross-sections: each run of the front outline (half-width a) becomes an
    ellipse a deep times `ratio`, or, with a side view, as deep as the side outline run it sits
    in, capped at a * max_ratio so arms in front of the torso don't take the torso's depth."""
    out = np.zeros((len(xs), len(ys), len(zs)), bool)
    for k, z in enumerate(zs):
        a, xc = _runs_along(face, xs, z)
        if not a.any():
            continue
        if side:
            b, yc = _runs_along(side, ys, z)
            depth = np.minimum(b[None, :], a[:, None] * max_ratio)
            yc = yc[None, :]
        else:
            depth = a[:, None] * ratio
            yc = 0.0
        with np.errstate(divide="ignore", invalid="ignore"):
            r = ((xs[:, None] - xc[:, None]) / a[:, None]) ** 2 + ((ys[None, :] - yc) / depth) ** 2
        out[:, :, k] = (a[:, None] > 0) & (depth > 0) & (r <= 1)
    return out


def _blur_occupancy(filled, passes=2):
    """Soften the voxel staircase: blur the occupancy along every axis and threshold it again."""
    f = filled.astype(np.float32)
    for _ in range(passes):
        for axis in range(3):
            p = np.pad(f, [(1, 1) if a == axis else (0, 0) for a in range(3)], mode="edge")
            lo = np.take(p, range(0, f.shape[axis]), axis=axis)
            mid = np.take(p, range(1, f.shape[axis] + 1), axis=axis)
            hi = np.take(p, range(2, f.shape[axis] + 2), axis=axis)
            f = (lo + 2 * mid + hi) / 4
    return f > 0.5


def _voxel_surface(filled, x0, y0, z0, cell, name):
    """Quads on every face between a filled cell and an empty one."""
    p = np.pad(filled, 1)
    nx, ny, nz = p.shape
    verts_index = {}
    faces = []

    def corner(i, j, k):
        key = (i, j, k)
        idx = verts_index.get(key)
        if idx is None:
            idx = verts_index[key] = len(verts_index)
        return idx

    quads = []
    for axis in range(3):
        for sign in (-1, 1):
            nb = np.roll(p, -sign, axis=axis)
            cells = np.argwhere(p & ~nb)
            for c in cells:
                i, j, k = c
                if axis == 0:
                    a = i + (sign > 0)
                    q = [(a, j, k), (a, j + 1, k), (a, j + 1, k + 1), (a, j, k + 1)]
                elif axis == 1:
                    a = j + (sign > 0)
                    q = [(i, a, k), (i, a, k + 1), (i + 1, a, k + 1), (i + 1, a, k)]
                else:
                    a = k + (sign > 0)
                    q = [(i, j, a), (i + 1, j, a), (i + 1, j + 1, a), (i, j + 1, a)]
                if sign < 0:
                    q.reverse()
                quads.append(q)
    for q in quads:
        faces.append([corner(*v) for v in q])
    co = np.zeros((len(verts_index), 3))
    for (i, j, k), idx in verts_index.items():
        co[idx] = (x0 + (i - 1) * cell, y0 + (j - 1) * cell, z0 + (k - 1) * cell)
    me = bpy.data.meshes.new(name)
    me.from_pydata(co.tolist(), [], faces)
    me.validate()
    return me


@command("paint_from_reference", "reference")
def paint_from_reference(name, views=None, attribute="Reference", occlusion=True, material=True):
    """Colour a mesh from the reference pictures: every vertex takes the colour of the picture it
    faces most directly (front, back, side), skipping views where something else is in the way.
    Stores a colour attribute and, with material=True, gives the mesh a material that uses it, so
    bake_maps(maps=["DIFFUSE"]) can turn it into a texture on the low-poly mesh. A dense mesh gives
    sharper colour edges."""
    from .util import get_mesh

    obj = get_mesh(name)
    refs = _refs()
    views = [_view(v) for v in views] if views else list(refs)
    if not views:
        raise CommandError("Set a reference first")
    me = obj.data
    n = len(me.vertices)
    co = np.empty(n * 3)
    me.vertices.foreach_get("co", co)
    nor = np.empty(n * 3)
    me.vertices.foreach_get("normal", nor)
    mw = np.array(obj.matrix_world)
    co_w = co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    nor_w = nor.reshape(-1, 3) @ np.array(obj.matrix_world.inverted().transposed())[:3, :3].T
    nor_w /= np.maximum(np.linalg.norm(nor_w, axis=1, keepdims=True), 1e-9)

    acc = np.zeros((n, 3))
    wsum = np.zeros(n)
    inv = obj.matrix_world.inverted()
    size = max(obj.dimensions) or 1.0
    for view in views:
        ref = refs.get(view)
        if ref is None:
            raise CommandError(f"No {view} reference")
        mask, rgba = _subject_mask(ref["path"], ref["tolerance"])
        direction, h_axis = VIEWS[view]
        d = np.array(direction)
        weight = np.clip(nor_w @ d, 0, None) ** 2
        h = co_w @ np.array(h_axis)
        col = (h / ref["mpp"] + ref["center_col"])
        row = ref["bottom_row"] - (co_w[:, 2] - ref["base_z"]) / ref["mpp"]
        mc, mr = np.floor(col).astype(int), np.floor(row).astype(int)
        ok = (weight > 0) & (mc >= 0) & (mc < mask.shape[1]) & (mr >= 0) & (mr < mask.shape[0])
        ok[ok] = mask[mr[ok], mc[ok]]
        if occlusion:
            d_local = (inv.to_3x3() @ Vector(direction)).normalized()
            for i in np.nonzero(ok)[0]:
                start = inv @ Vector(co_w[i] + d * size * 0.002)
                if obj.ray_cast(start, d_local, distance=size * 4)[0]:
                    ok[i] = False
        H, W = rgba.shape[:2]
        fr = np.clip((row * H / mask.shape[0]).astype(int), 0, H - 1)
        fc = np.clip((col * W / mask.shape[1]).astype(int), 0, W - 1)
        idx = np.nonzero(ok)[0]
        acc[idx] += rgba[fr[idx], fc[idx], :3] * weight[idx, None]
        wsum[idx] += weight[idx]

    colours = np.zeros((n, 3))
    seen = wsum > 0
    colours[seen] = acc[seen] / wsum[seen, None]
    # Vertices no picture sees (under the arms, between the legs) take their neighbours' colour.
    if (~seen).any() and seen.any():
        edges = np.empty(len(me.edges) * 2, dtype=np.int64)
        me.edges.foreach_get("vertices", edges)
        edges = edges.reshape(-1, 2)
        for _ in range(60):
            missing = ~seen
            if not missing.any():
                break
            s = np.zeros((n, 3))
            c = np.zeros(n)
            for a, b in ((edges[:, 0], edges[:, 1]), (edges[:, 1], edges[:, 0])):
                use = seen[b]
                np.add.at(s, a[use], colours[b[use]])
                np.add.at(c, a[use], 1)
            fill = missing & (c > 0)
            colours[fill] = s[fill] / c[fill, None]
            seen = seen | fill
        colours[~seen] = colours[seen].mean(axis=0)

    attr = me.color_attributes.get(attribute) or me.color_attributes.new(attribute, "FLOAT_COLOR", "POINT")
    if attr.domain != "POINT":
        me.color_attributes.remove(attr)
        attr = me.color_attributes.new(attribute, "FLOAT_COLOR", "POINT")
    rgba_out = np.concatenate([colours, np.ones((n, 1))], axis=1).astype(np.float32).ravel()
    attr.data.foreach_set("color_srgb" if hasattr(attr.data[0], "color_srgb") else "color", rgba_out)
    me.color_attributes.active_color = attr
    result = {"object": obj.name, "attribute": attr.name, "views": views,
              "vertices_seen": int((wsum > 0).sum()), "vertices": n}
    if material:
        result["material"] = _colour_material(obj, attr.name)
    return result


def _colour_material(obj, attribute):
    from .util import use_nodes

    mat = bpy.data.materials.get(f"{obj.name}_reference") or bpy.data.materials.new(f"{obj.name}_reference")
    use_nodes(mat)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = next(n for n in nodes if n.type == "BSDF_PRINCIPLED")
    node = next((n for n in nodes if n.type == "VERTEX_COLOR"), None) or nodes.new("ShaderNodeVertexColor")
    node.layer_name = attribute
    node.location = (bsdf.location.x - 300, bsdf.location.y)
    links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.5
    # Viewport colour for solid shading of meshes that mix with others in a render.
    attr = obj.data.color_attributes.get(attribute)
    if attr is not None and len(attr.data):
        c = np.empty(len(attr.data) * 4, np.float32)
        attr.data.foreach_get("color", c)
        mat.diffuse_color = list(c.reshape(-1, 4).mean(axis=0))
    if obj.material_slots:
        obj.material_slots[0].material = mat
    else:
        obj.data.materials.append(mat)
    return mat.name
