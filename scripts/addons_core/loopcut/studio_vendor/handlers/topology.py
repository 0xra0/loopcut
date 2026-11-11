"""topology toolset: remeshing, decimation to a budget, cleanup and shading."""

import math

import bmesh
import bpy

from . import CommandError, command
from .util import apply_modifiers, edit_bmesh, get_mesh, mesh_counts, ops_context


def _surface_area(obj):
    scale = obj.matrix_world.to_scale()
    s2 = (abs(scale.x * scale.y) + abs(scale.y * scale.z) + abs(scale.x * scale.z)) / 3
    return sum(p.area for p in obj.data.polygons) * s2


@command("voxel_remesh", "topology")
def voxel_remesh(name, voxel_size=None, target_faces=None, adaptivity=0.0):
    """Rebuild the mesh as an even, watertight quad surface. Give voxel_size (world units) or
    target_faces (estimated from surface area). Destroys UVs; use before sculpting, not after."""
    obj = get_mesh(name)
    if voxel_size is None:
        if not target_faces:
            raise CommandError("Give voxel_size or target_faces")
        area = _surface_area(obj)
        if area <= 0:
            raise CommandError("Mesh has no surface area")
        voxel_size = math.sqrt(area / float(target_faces))
    scale = sum(obj.matrix_world.to_scale()) / 3
    before = mesh_counts(obj)
    mod = obj.modifiers.new("VoxelRemesh", "REMESH")
    mod.mode = "VOXEL"
    mod.voxel_size = float(voxel_size) / scale
    mod.adaptivity = float(adaptivity)
    mod.use_smooth_shade = True
    apply_modifiers(obj, [m.name for m in obj.modifiers][: list(obj.modifiers).index(mod) + 1])
    return {"name": obj.name, "voxel_size": round(float(voxel_size), 6), "before": before, "after": mesh_counts(obj)}


@command("quad_remesh", "topology")
def quad_remesh(name, target_faces=5000, use_symmetry=False, preserve_sharp=False, smooth_normals=True, seed=0):
    """QuadriFlow retopology to roughly target_faces quads. Good for game meshes and for baking
    from a sculpt (duplicate the sculpt first, then remesh the copy). Destroys UVs."""
    obj = get_mesh(name)
    if obj.modifiers:
        apply_modifiers(obj)
    before = mesh_counts(obj)
    with ops_context(obj, mode="OBJECT"):
        res = bpy.ops.object.quadriflow_remesh(target_faces=int(target_faces), use_mesh_symmetry=bool(use_symmetry),
                                               use_preserve_sharp=bool(preserve_sharp), use_preserve_boundary=True,
                                               smooth_normals=bool(smooth_normals), seed=int(seed), mode="FACES")
    after = mesh_counts(obj)
    if "FINISHED" not in res or after == before:
        raise CommandError("QuadriFlow failed. It needs a manifold mesh: run cleanup or voxel_remesh first")
    return {"name": obj.name, "before": before, "after": after}


@command("decimate", "topology")
def decimate(name, target_tris=None, ratio=None, symmetry_axis=None, method="COLLAPSE", planar_angle=5.0, apply=True):
    """Reduce polycount to hit a triangle budget (or a ratio). method: COLLAPSE (keeps shape),
    PLANAR (merges flat areas; uses planar_angle), UNSUBDIV. symmetry_axis X/Y/Z keeps halves matching."""
    obj = get_mesh(name)
    before = mesh_counts(obj)
    method = method.upper()
    mod = obj.modifiers.new("Decimate", "DECIMATE")
    mod.decimate_type = method
    if method == "COLLAPSE":
        if ratio is None:
            if not target_tris:
                obj.modifiers.remove(mod)
                raise CommandError("Give target_tris or ratio")
            ratio = min(1.0, float(target_tris) / max(before["tris"], 1))
        mod.ratio = float(ratio)
        mod.use_collapse_triangulate = False
        if symmetry_axis:
            mod.use_symmetry = True
            mod.symmetry_axis = symmetry_axis.upper()
    elif method == "PLANAR":
        mod.angle_limit = math.radians(float(planar_angle))
    elif method == "UNSUBDIV":
        mod.iterations = 1
    else:
        obj.modifiers.remove(mod)
        raise CommandError("method must be COLLAPSE, PLANAR or UNSUBDIV")
    if apply:
        apply_modifiers(obj, [m.name for m in obj.modifiers][: list(obj.modifiers).index(mod) + 1])
    return {"name": obj.name, "before": before, "after": mesh_counts(obj) if apply else None, "ratio": ratio}


@command("cleanup", "topology")
def cleanup(name, merge_distance=0.0001, delete_loose=True, dissolve_degenerate=True, recalc_normals=True,
            fill_holes=False):
    """Fix common mesh problems: duplicate verts, loose geometry, zero-area faces, flipped normals,
    and optionally small holes. Returns what changed."""
    obj = get_mesh(name)
    before = mesh_counts(obj)
    report = {}
    with edit_bmesh(obj) as bm:
        n = len(bm.verts)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=float(merge_distance))
        report["merged_verts"] = n - len(bm.verts)
        if dissolve_degenerate:
            bmesh.ops.dissolve_degenerate(bm, edges=bm.edges, dist=float(merge_distance))
        if delete_loose:
            loose_e = [e for e in bm.edges if not e.link_faces]
            bmesh.ops.delete(bm, geom=loose_e, context="EDGES")
            loose_v = [v for v in bm.verts if not v.link_edges]
            bmesh.ops.delete(bm, geom=loose_v, context="VERTS")
            report["deleted_loose"] = len(loose_e) + len(loose_v)
        if fill_holes:
            boundary = [e for e in bm.edges if e.is_boundary]
            res = bmesh.ops.holes_fill(bm, edges=boundary, sides=0)
            report["filled_holes"] = len(res["faces"])
        if recalc_normals:
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return {"name": obj.name, "before": before, "after": mesh_counts(obj), **report}


@command("shade", "topology")
def shade(name, smooth=True, sharp_angle=None):
    """Flat or smooth shading. With smooth=True and sharp_angle (degrees), edges sharper than
    that stay crisp (like Shade Auto Smooth)."""
    obj = get_mesh(name)
    me = obj.data
    if not smooth:
        me.shade_flat()
    elif sharp_angle is None:
        me.shade_smooth()
    else:
        me.shade_smooth()
        me.set_sharp_from_angle(angle=math.radians(float(sharp_angle)))
    return {"name": obj.name, "smooth": smooth}
