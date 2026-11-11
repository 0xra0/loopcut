"""sculpt toolset: brush strokes on dense meshes, computed with numpy.

Blender's own sculpt operators need a 3D viewport under the mouse, so they can't be driven
reliably from a script. These brushes work on vertex positions directly instead, which is
deterministic, works headless, and gives the same result every run. Sculpt on a dense,
even mesh (sculpt_prepare does a voxel remesh) and retopologize afterwards.
"""

import math

import bmesh
import bpy
import numpy as np

from mathutils import Vector

from . import CommandError, command
from .look import VIEWS, view_rotation
from .topology import voxel_remesh
from .util import apply_modifiers, edit_bmesh, get_mesh, mesh_counts, vec

BRUSHES = ("draw", "clay", "inflate", "smooth", "flatten", "pinch", "crease", "grab", "blob")
FALLOFFS = ("smooth", "sphere", "sharp", "linear", "constant")


def _falloff(t, kind):
    """t = distance / radius in [0, 1). Returns weights in [0, 1]."""
    if kind == "constant":
        return np.ones_like(t)
    if kind == "linear":
        return 1.0 - t
    if kind == "sharp":
        return (1.0 - t) ** 2
    if kind == "sphere":
        return np.sqrt(np.clip(1.0 - t * t, 0.0, 1.0))
    s = 1.0 - t  # smooth: smoothstep
    return s * s * (3.0 - 2.0 * s)


def _read(me):
    n = len(me.vertices)
    co = np.empty(n * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    no = np.empty(n * 3, dtype=np.float64)
    me.vertices.foreach_get("normal", no)
    return co.reshape(n, 3), no.reshape(n, 3)


def _edges(me):
    e = np.empty(len(me.edges) * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", e)
    return e.reshape(-1, 2)


def _neighbor_mean(co, edges, idx):
    """Mean position of each vertex's neighbors, for the vertices in idx."""
    n = len(co)
    total = np.zeros((n, 3))
    count = np.zeros(n)
    np.add.at(total, edges[:, 0], co[edges[:, 1]])
    np.add.at(total, edges[:, 1], co[edges[:, 0]])
    np.add.at(count, edges[:, 0], 1)
    np.add.at(count, edges[:, 1], 1)
    count = np.maximum(count, 1)
    return total[idx] / count[idx, None]


def _resample(points, spacing):
    """Dab centers every `spacing` along the polyline."""
    if len(points) == 1:
        return points
    out = [points[0]]
    carry = 0.0
    for a, b in zip(points[:-1], points[1:]):
        seg = b - a
        length = float(np.linalg.norm(seg))
        if length == 0:
            continue
        d = spacing - carry
        while d <= length:
            out.append(a + seg * (d / length))
            d += spacing
        carry = length - (d - spacing)
    if np.linalg.norm(out[-1] - points[-1]) > spacing * 0.25:
        out.append(points[-1])
    return np.array(out)


@command("sculpt_prepare", "sculpt")
def sculpt_prepare(name, target_faces=200000, voxel_size=None, smooth_shading=True):
    """Voxel-remesh to an even, dense surface ready for brushes. Applies modifiers first.
    Pick target_faces from the detail you need: ~50k for forms, 200k-1M for fine detail."""
    obj = get_mesh(name)
    if obj.modifiers:
        apply_modifiers(obj)
    result = voxel_remesh(name, voxel_size=voxel_size, target_faces=None if voxel_size else target_faces)
    if smooth_shading:
        obj.data.shade_smooth()
    return result


@command("surface_points", "sculpt")
def surface_points(name, view=None, coords=None, points=None):
    """Find points on a mesh's surface, to aim sculpt strokes.
    view + coords: [[u, v], ...] in 0-1 across the object's bounding box as seen from that
    capture_views view (u left to right, v bottom to top). Each is cast from the camera's side
    and returns the first surface it hits. points: world positions; returns the closest surface
    point to each. Results are world-space {co, normal, hit}."""
    obj = get_mesh(name)
    if (view is None) == (points is None):
        raise CommandError("Pass either view + coords or points")
    mw = obj.matrix_world
    inv = mw.inverted()
    normal_mat = inv.transposed().to_3x3()

    def result(hit, loc, normal):
        if not hit:
            return {"hit": False, "co": None, "normal": None}
        return {"hit": True, "co": [round(v, 5) for v in mw @ loc],
                "normal": [round(v, 4) for v in (normal_mat @ normal).normalized()]}

    out = []
    if points is not None:
        for p in points:
            hit, loc, normal, _ = obj.closest_point_on_mesh(inv @ vec(p))
            out.append(result(hit, loc, normal))
        return {"object": obj.name, "points": out}
    if view not in VIEWS:
        raise CommandError(f"Unknown view {view!r}. Use: {sorted(VIEWS)}")
    if not coords:
        raise CommandError("coords is a list of [u, v] pairs in 0-1")
    rot = view_rotation(view).to_matrix()
    right, up, toward = rot @ Vector((1, 0, 0)), rot @ Vector((0, 1, 0)), rot @ Vector((0, 0, 1))
    corners = [mw @ Vector(c) for c in obj.bound_box]
    center = sum(corners, Vector()) / 8

    def span(axis):
        d = [(c - center).dot(axis) for c in corners]
        return min(d), max(d)

    (r0, r1), (u0, u1), (_, t1) = span(right), span(up), span(toward)
    size = max(obj.dimensions) or 1.0
    direction = inv.to_3x3() @ -toward
    for u, v in coords:
        start = center + right * (r0 + (r1 - r0) * u) + up * (u0 + (u1 - u0) * v) + toward * (t1 + size)
        hit, loc, normal, _ = obj.ray_cast(inv @ start, direction.normalized())
        out.append(result(hit, loc, normal))
    return {"object": obj.name, "view": view, "points": out}


@command("sculpt_stroke", "sculpt")
def sculpt_stroke(name, brush, points, radius, strength=0.5, symmetry="X", falloff="smooth", invert=False,
                  spacing=0.25, delta=None):
    """Apply a brush along a path of world-space points.

    brush: draw (raise/lower along the surface), clay (build up flat layers), blob (rounder draw),
      inflate (along each vertex normal), smooth, flatten, pinch (pull toward the stroke),
      crease (sharp groove), grab (move by `delta`, world space, at the first point).
    radius: world units. strength: 0-1 (draw moves up to radius*0.15*strength per dab).
    symmetry: "X", "Y", "Z", "XY"... mirrors in the object's local space, or "" for none.
    invert: carve in instead of building up (draw, clay, blob, inflate, pinch).
    """
    if brush not in BRUSHES:
        raise CommandError(f"Unknown brush {brush!r}. Use one of {BRUSHES}")
    if falloff not in FALLOFFS:
        raise CommandError(f"Unknown falloff {falloff!r}. Use one of {FALLOFFS}")
    if not points:
        raise CommandError("points needs at least one [x, y, z]")
    obj = get_mesh(name)
    if obj.mode != "OBJECT":
        raise CommandError(f"{name!r} is in {obj.mode} mode; switch to object mode first")
    if obj.modifiers:
        raise CommandError(f"{name!r} has modifiers; apply them (or run sculpt_prepare) before sculpting")
    me = obj.data
    if len(me.vertices) == 0:
        raise CommandError("Mesh has no vertices")

    inv = obj.matrix_world.inverted()
    scale = sum(obj.matrix_world.to_scale()) / 3
    r = float(radius) / scale
    pts = np.array([list(inv @ vec(p)) for p in points])
    co, no = _read(me)
    edges = _edges(me) if brush in ("smooth", "crease") else None
    sign = -1.0 if invert else 1.0
    s = float(np.clip(strength, 0.0, 1.0))
    mirrors = [np.array([-1.0 if a in axes else 1.0 for a in "XYZ"]) for axes in _mirror_sets(symmetry or "")]

    if brush == "grab":
        if delta is None:
            raise CommandError("grab needs delta: [dx, dy, dz] in world units")
        d_local = np.array(list(inv.to_3x3() @ vec(delta)))
        centers = pts[:1]
    else:
        centers = _resample(pts, max(r * float(spacing), 1e-6))
    touched = np.zeros(len(co), dtype=bool)

    for c in centers:
        for m in mirrors:
            cc = c * m
            diff = co - cc
            dist = np.sqrt(np.einsum("ij,ij->i", diff, diff))
            idx = np.nonzero(dist < r)[0]
            if idx.size == 0:
                continue
            w = _falloff(dist[idx] / r, falloff)[:, None]
            touched[idx] = True
            n_area = (no[idx] * w).sum(axis=0)
            norm = np.linalg.norm(n_area)
            n_area = n_area / norm if norm > 0 else np.array([0.0, 0.0, 1.0])
            if brush == "grab":
                co[idx] += (d_local * m) * w
            elif brush in ("draw", "blob"):
                amount = r * 0.15 * s * sign
                if brush == "blob":
                    w = np.sqrt(w)
                co[idx] += n_area * amount * w
            elif brush == "inflate":
                co[idx] += no[idx] * (r * 0.1 * s * sign) * w
            elif brush in ("flatten", "clay"):
                centroid = (co[idx] * w).sum(axis=0) / w.sum()
                if brush == "clay":
                    centroid = centroid + n_area * r * 0.1 * s * sign
                height = (co[idx] - centroid) @ n_area
                if brush == "clay":
                    # Clay only fills toward the raised plane; it never cuts what is above it.
                    height = np.minimum(height, 0.0) if sign > 0 else np.maximum(height, 0.0)
                co[idx] -= np.outer(height, n_area) * (w * min(1.0, s * 1.5))
            elif brush == "pinch":
                to_c = cc - co[idx]
                to_c -= np.outer(to_c @ n_area, n_area)
                co[idx] += to_c * (w * 0.5 * s * sign)
            elif brush == "smooth":
                target = _neighbor_mean(co, edges, idx)
                co[idx] += (target - co[idx]) * (w * s)
            elif brush == "crease":
                to_c = cc - co[idx]
                to_c -= np.outer(to_c @ n_area, n_area)
                co[idx] += to_c * (w * 0.4 * s)
                co[idx] -= n_area * (r * 0.12 * s * (1 if not invert else -1)) * (w ** 2)

    me.vertices.foreach_set("co", co.ravel())
    me.update()
    return {"name": obj.name, "brush": brush, "dabs": int(len(centers) * len(mirrors)),
            "verts_moved": int(touched.sum()), "verts": len(co)}


def _mirror_sets(symmetry):
    """All mirror combinations for the given axes, e.g. "XY" -> "", X, Y, XY."""
    axes = [a for a in symmetry.upper() if a in "XYZ"]
    sets = [""]
    for a in axes:
        sets += [s + a for s in sets]
    return sets


@command("symmetrize", "sculpt")
def symmetrize(name, direction="+X"):
    """Copy one half onto the other, in local space. direction: +X copies +X onto -X, -X the reverse; same for Y, Z."""
    obj = get_mesh(name)
    d = direction.upper().strip()
    if len(d) != 2 or d[0] not in "+-" or d[1] not in "XYZ":
        raise CommandError("direction must be one of +X -X +Y -Y +Z -Z")
    enum = d[1] if d[0] == "+" else d
    with edit_bmesh(obj) as bm:
        bmesh.ops.symmetrize(bm, input=bm.verts[:] + bm.edges[:] + bm.faces[:], direction=enum, dist=1e-4)
    return {"name": obj.name, **mesh_counts(obj)}


@command("smooth_mesh", "sculpt")
def smooth_mesh(name, iterations=5, factor=0.5, preserve_volume=True):
    """Relax the whole surface (or use sculpt_stroke with brush=smooth for a local area)."""
    obj = get_mesh(name)
    with edit_bmesh(obj) as bm:
        for _ in range(int(iterations)):
            if preserve_volume:
                bmesh.ops.smooth_laplacian_vert(bm, verts=bm.verts, lambda_factor=float(factor),
                                                lambda_border=0.0, use_x=True, use_y=True, use_z=True,
                                                preserve_volume=True)
            else:
                bmesh.ops.smooth_vert(bm, verts=bm.verts, factor=float(factor),
                                      use_axis_x=True, use_axis_y=True, use_axis_z=True)
    return {"name": obj.name, "iterations": int(iterations)}


NOISE_TEXTURES = ("CLOUDS", "VORONOI", "MARBLE", "STUCCI", "DISTORTED_NOISE", "MUSGRAVE", "WOOD")


@command("surface_noise", "sculpt")
def surface_noise(name, texture="CLOUDS", scale=0.25, strength=0.02, detail=2, apply=True):
    """Procedural surface detail (skin pores, rock, bark) displaced along normals.
    scale is the noise size in world units; strength the displacement in world units."""
    if texture.upper() not in NOISE_TEXTURES:
        raise CommandError(f"Unknown texture {texture!r}. Use one of {NOISE_TEXTURES}")
    obj = get_mesh(name)
    tex = bpy.data.textures.new(f"{obj.name}_noise", texture.upper())
    if hasattr(tex, "noise_scale"):
        tex.noise_scale = float(scale)
    if hasattr(tex, "noise_depth"):
        tex.noise_depth = int(detail)
    mod = obj.modifiers.new("SurfaceNoise", "DISPLACE")
    mod.texture = tex
    mod.texture_coords = "GLOBAL"
    mod.strength = float(strength) * 2  # texture values centre on 0.5
    mod.mid_level = 0.5
    if apply:
        apply_modifiers(obj, [m.name for m in obj.modifiers][: list(obj.modifiers).index(mod) + 1])
        bpy.data.textures.remove(tex)
    return {"name": obj.name, "texture": texture.upper(), "applied": bool(apply)}
