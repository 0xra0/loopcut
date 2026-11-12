"""uv_bake toolset: unwrapping, packing, UV quality numbers and texture baking."""

import math
import os
import tempfile

import bmesh
import bpy
import numpy as np

from . import CommandError, command
from .util import get_mesh, ops_context, use_nodes

UNWRAP_METHODS = ("smart", "angle_based", "conformal", "cube")
BAKE_MAPS = ("NORMAL", "AO", "DIFFUSE", "ROUGHNESS", "EMIT", "COMBINED")


def _mark_seams_by_angle(obj, angle):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    limit = math.radians(angle)
    for e in bm.edges:
        if len(e.link_faces) == 2 and e.calc_face_angle(0.0) >= limit:
            e.seam = True
        elif e.is_boundary:
            e.seam = True
    bm.to_mesh(obj.data)
    bm.free()


@command("uv_unwrap", "uv_bake")
def uv_unwrap(name, method="smart", angle_limit=66.0, island_margin=0.02, seams_from_angle=None, cube_size=1.0):
    """Unwrap a mesh. smart: automatic islands split at angle_limit (degrees), good default for
    hard-surface and props. angle_based / conformal: classic unwrap along seams; pass
    seams_from_angle (degrees) to mark seams at sharp edges first, better for organic meshes.
    cube: box projection for blockouts."""
    if method not in UNWRAP_METHODS:
        raise CommandError(f"Unknown method {method!r}. Use one of {UNWRAP_METHODS}")
    obj = get_mesh(name)
    if not obj.data.uv_layers:
        obj.data.uv_layers.new(name="UVMap")
    if seams_from_angle is not None:
        _mark_seams_by_angle(obj, float(seams_from_angle))
    with ops_context(obj, mode="EDIT"):
        bpy.ops.mesh.select_all(action="SELECT")
        if method == "smart":
            bpy.ops.uv.smart_project(angle_limit=math.radians(float(angle_limit)), island_margin=float(island_margin))
        elif method == "cube":
            bpy.ops.uv.cube_project(cube_size=float(cube_size))
        else:
            bpy.ops.uv.unwrap(method=method.upper(), margin=float(island_margin))
    return uv_report(name)


@command("uv_pack", "uv_bake")
def uv_pack(names, margin=0.01, rotate=True):
    """Pack the UV islands of one or more meshes into the 0-1 square without overlaps.
    Several names share one texture atlas."""
    objs = [get_mesh(n) for n in names]
    for o in objs:
        if not o.data.uv_layers:
            raise CommandError(f"{o.name!r} has no UVs; run uv_unwrap first")
    with ops_context(objs[0], selected=objs[1:], mode="EDIT"):
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.select_all(action="SELECT")
        bpy.ops.uv.pack_islands(margin=float(margin), rotate=bool(rotate))
    return {n: uv_report(n) for n in names}


def _islands(bm, uv_layer, eps=1e-5):
    """Union faces that share an edge with matching UVs on both sides."""
    parent = list(range(len(bm.faces)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for e in bm.edges:
        if len(e.link_faces) != 2 or e.seam:
            continue
        f1, f2 = e.link_faces
        l1 = [l for l in f1.loops if l.edge == e][0]
        l2 = [l for l in f2.loops if l.edge == e][0]
        # Shared edge runs opposite directions in the two faces.
        a1, b1 = l1[uv_layer].uv, l1.link_loop_next[uv_layer].uv
        a2, b2 = l2.link_loop_next[uv_layer].uv, l2[uv_layer].uv
        if (a1 - a2).length < eps and (b1 - b2).length < eps:
            ra, rb = find(f1.index), find(f2.index)
            if ra != rb:
                parent[ra] = rb
    return [find(i) for i in range(len(bm.faces))]


def _raster(tris_uv, island_of_tri, res):
    owner = np.full((res, res), -1, dtype=np.int64)
    overlap = np.zeros((res, res), dtype=bool)
    for (a, b, c), isl in zip(tris_uv, island_of_tri):
        pts = np.array([a, b, c]) * res
        x0, y0 = np.floor(pts.min(axis=0)).astype(int)
        x1, y1 = np.ceil(pts.max(axis=0)).astype(int)
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, res), min(y1, res)
        if x0 >= x1 or y0 >= y1:
            continue
        xs, ys = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
        p = np.stack([xs.ravel(), ys.ravel()], axis=1)
        v0, v1 = pts[1] - pts[0], pts[2] - pts[0]
        den = v0[0] * v1[1] - v1[0] * v0[1]
        if abs(den) < 1e-12:
            continue
        d = p - pts[0]
        u = (d[:, 0] * v1[1] - v1[0] * d[:, 1]) / den
        v = (v0[0] * d[:, 1] - d[:, 0] * v0[1]) / den
        inside = (u >= 0) & (v >= 0) & (u + v <= 1)
        if not inside.any():
            continue
        px = p[inside].astype(int)
        cols, rows = px[:, 0], px[:, 1]
        cur = owner[rows, cols]
        overlap[rows, cols] |= (cur >= 0) & (cur != isl)
        owner[rows, cols] = np.where(cur < 0, isl, cur)
    return owner, overlap


@command("uv_report", "uv_bake")
def uv_report(name, resolution=256):
    """UV quality: island count, faces outside 0-1, how much of the square is used, how much of it
    overlaps (different islands on the same texels), and texel-density spread (stretch)."""
    obj = get_mesh(name)
    me = obj.data
    if not me.uv_layers:
        return {"name": obj.name, "uv_layers": [], "note": "No UVs"}
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.active
    island = _islands(bm, uv_layer)
    out_of_bounds = 0
    tris, tri_island = [], []
    densities = []
    for f in bm.faces:
        uvs = [tuple(l[uv_layer].uv) for l in f.loops]
        if any(not (-1e-4 <= u <= 1.0001 and -1e-4 <= v <= 1.0001) for u, v in uvs):
            out_of_bounds += 1
        for i in range(1, len(uvs) - 1):
            tris.append((uvs[0], uvs[i], uvs[i + 1]))
            tri_island.append(island[f.index])
        area3d = f.calc_area()
        uv_area = 0.0
        for i in range(1, len(uvs) - 1):
            (ax, ay), (bx, by), (cx, cy) = uvs[0], uvs[i], uvs[i + 1]
            uv_area += abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) / 2
        if area3d > 1e-12:
            densities.append(math.sqrt(uv_area / area3d))
    bm.free()
    res = int(max(32, min(resolution, 1024)))
    step = max(1, len(tris) // 200000)  # sample huge meshes
    owner, overlap = _raster(tris[::step], tri_island[::step], res)
    d = np.array(densities) if densities else np.array([0.0])
    mean = float(d.mean()) or 1.0
    return {
        "name": obj.name,
        "uv_layers": [uv.name for uv in me.uv_layers],
        "islands": len(set(island)),
        "faces_out_of_bounds": out_of_bounds,
        "coverage": round(float((owner >= 0).mean()), 4),
        "overlap": round(float(overlap.mean()), 4),
        "texel_density_spread": round(float(d.std() / mean), 4),
    }


def _output_dir(output_dir):
    if output_dir:
        path = bpy.path.abspath(output_dir)
    elif bpy.data.filepath:
        path = os.path.join(os.path.dirname(bpy.data.filepath), "textures")
    else:
        path = os.path.join(tempfile.gettempdir(), "claude_bakes")
    os.makedirs(path, exist_ok=True)
    return path


def _ensure_materials(obj):
    if not obj.material_slots or not any(s.material for s in obj.material_slots):
        mat = bpy.data.materials.new(f"{obj.name}_Baked")
        use_nodes(mat)
        if obj.material_slots:
            obj.material_slots[0].material = mat
        else:
            obj.data.materials.append(mat)
    mats = []
    for s in obj.material_slots:
        if s.material is None:
            continue
        use_nodes(s.material)
        mats.append(s.material)
    return mats


def _principled(mat):
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            return n
    return None


def _hook_up(mat, img_node, kind):
    """Connect a baked image into the material so the result is visible and exports."""
    bsdf = _principled(mat)
    if bsdf is None:
        return
    links = mat.node_tree.links
    if kind == "NORMAL":
        nm = mat.node_tree.nodes.new("ShaderNodeNormalMap")
        nm.location = (img_node.location.x + 300, img_node.location.y)
        links.new(img_node.outputs["Color"], nm.inputs["Color"])
        links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    elif kind == "DIFFUSE":
        links.new(img_node.outputs["Color"], bsdf.inputs["Base Color"])
    elif kind == "ROUGHNESS":
        links.new(img_node.outputs["Color"], bsdf.inputs["Roughness"])


@command("bake_maps", "uv_bake")
def bake_maps(low, high=None, maps=("NORMAL",), size=1024, cage_extrusion=0.05, max_ray_distance=0.0,
              samples=16, margin=16, output_dir=None, connect=True):
    """Bake texture maps for `low` with Cycles, saved as PNGs.

    high: name (or list) of high-poly meshes to bake from (selected-to-active), e.g. the sculpt.
    maps: NORMAL, AO, DIFFUSE, ROUGHNESS, EMIT, COMBINED. low needs UVs without overlaps.
    cage_extrusion: how far (world units) rays start outside the low mesh; raise it if the
    normal map has holes, lower it if details from other parts bleed in.
    connect: wire the baked images into the low mesh's material.
    """
    lo = get_mesh(low)
    if not lo.data.uv_layers:
        raise CommandError(f"{low!r} has no UVs; run uv_unwrap first")
    highs = [get_mesh(h) for h in ([high] if isinstance(high, str) else (high or []))]
    maps = [m.upper() for m in maps]
    bad = [m for m in maps if m not in BAKE_MAPS]
    if bad:
        raise CommandError(f"Unknown maps {bad}. Use {BAKE_MAPS}")
    if "NORMAL" in maps and not highs:
        raise CommandError("A NORMAL bake needs high (the detailed mesh to bake from)")
    size = int(max(64, min(int(size), 8192)))
    out = _output_dir(output_dir)
    scene = bpy.context.scene
    saved = {"engine": scene.render.engine, "samples": scene.cycles.samples, "device": scene.cycles.device}
    mats = _ensure_materials(lo)
    # Hidden sources can't be selected, and render-hidden ones are skipped by the bake.
    hidden = [(h, h.hide_get(), h.hide_render) for h in highs]
    for h, _, _ in hidden:
        h.hide_set(False)
        h.hide_render = False
    results = []
    try:
        scene.render.engine = "CYCLES"
        scene.cycles.samples = int(samples)
        scene.cycles.device = "CPU"
        for kind in maps:
            img = bpy.data.images.new(f"{lo.name}_{kind.lower()}", size, size, alpha=False)
            if kind in ("NORMAL", "ROUGHNESS"):
                img.colorspace_settings.name = "Non-Color"
            nodes = []
            for mat in mats:
                node = mat.node_tree.nodes.new("ShaderNodeTexImage")
                node.image = img
                node.label = f"Baked {kind.title()}"
                node.location = (-600, 300 - 280 * len(results))
                mat.node_tree.nodes.active = node
                nodes.append((mat, node))
            kwargs = {"type": kind, "margin": int(margin), "use_clear": True}
            if highs:
                kwargs.update(use_selected_to_active=True, cage_extrusion=float(cage_extrusion),
                              max_ray_distance=float(max_ray_distance))
            if kind == "NORMAL":
                kwargs["normal_space"] = "TANGENT"
            if kind == "DIFFUSE":
                kwargs["pass_filter"] = {"COLOR"}
            with ops_context(lo, selected=highs, mode="OBJECT"):
                bpy.ops.object.bake(**kwargs)
            path = os.path.join(out, f"{lo.name}_{kind.lower()}.png")
            img.filepath_raw = path
            img.file_format = "PNG"
            img.save()
            if connect:
                for mat, node in nodes:
                    _hook_up(mat, node, kind)
            results.append({"map": kind, "path": path, "size": size, "nodes": nodes})
        # Leave the colour map as the active image node: viewport texture shading (and
        # capture_views) shows the active one, and a normal map there looks like a blue model.
        colour = next((r for r in results if r["map"] in ("DIFFUSE", "COMBINED", "EMIT")), None)
        if colour:
            for mat, node in colour["nodes"]:
                mat.node_tree.nodes.active = node
        for r in results:
            del r["nodes"]
    finally:
        scene.render.engine = saved["engine"]
        scene.cycles.samples = saved["samples"]
        scene.cycles.device = saved["device"]
        for h, was_hidden, was_render_hidden in hidden:
            h.hide_set(was_hidden)
            h.hide_render = was_render_hidden
    return {"low": lo.name, "high": [h.name for h in highs], "maps": results}
