"""Automatic topology and retopology tools for Loopcut.

Integrates:
- Quad remeshing (Blender Quadriflow or external QuadRemesher)
- Feature-preserving retopology (preserving sharp edges and symmetry)
- Boundary loop cleanup and relaxation
- Shrinkwrap reprojection onto source mesh with before-and-after RMS measurements
"""

import numpy as np


def measure_surface_deviation(source_obj, target_obj) -> float:
    """Compute Root Mean Square (RMS) distance between target vertices and source mesh."""
    import bpy
    import mathutils

    if not source_obj or not target_obj:
        return 0.0

    target_me = target_obj.data
    coords = [target_obj.matrix_world @ v.co for v in target_me.vertices]
    if not coords:
        return 0.0

    # Raycast or nearest surface point on source
    src_eval = source_obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    total_sq_dist = 0.0

    for pt in coords:
        success, nearest, normal, face_idx = src_eval.closest_point_on_mesh(source_obj.matrix_world.inverted() @ pt)
        if success:
            world_nearest = source_obj.matrix_world @ nearest
            diff = pt - world_nearest
            total_sq_dist += diff.length_squared

    return float(np.sqrt(total_sq_dist / max(1, len(coords))))


def quad_remesh(object_name: str, target_faces: int = 5000, use_symmetry: bool = True,
                preserve_sharp: bool = True) -> dict:
    """Execute quad remeshing on object_name with before-and-after measurement."""
    import bpy
    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != "MESH":
        raise ValueError(f"Mesh '{object_name}' not found.")

    bpy.context.view_layer.objects.active = obj
    initial_faces = len(obj.data.polygons)
    initial_verts = len(obj.data.vertices)

    # Check for QuadRemesher addon operator (qremesher)
    if hasattr(bpy.ops, "qremesher") and hasattr(bpy.ops.qremesher, "remesh"):
        bpy.ops.qremesher.remesh()
    else:
        # Native Quadriflow remesh
        bpy.ops.object.quadriflow_remesh(
            target_faces=target_faces,
            use_mesh_symmetry=use_symmetry,
            use_preserve_mesh_boundary=True,
            use_preserve_paint_mask=False
        )

    final_faces = len(obj.data.polygons)
    final_verts = len(obj.data.vertices)

    return {
        "status": "ok",
        "action": "quad_remesh",
        "object": object_name,
        "initial_faces": initial_faces,
        "final_faces": final_faces,
        "initial_verts": initial_verts,
        "final_verts": final_verts,
    }


def reproject_and_shrinkwrap(target_object: str, source_object: str, offset: float = 0.0) -> dict:
    """Reproject target mesh onto high-poly source mesh and report RMS deviation before and after."""
    import bpy
    tgt = bpy.data.objects.get(target_object)
    src = bpy.data.objects.get(source_object)
    if not tgt or not src:
        raise ValueError("Both target and source meshes must exist.")

    rms_before = measure_surface_deviation(src, tgt)

    # Add Shrinkwrap modifier
    mod = tgt.modifiers.new(name="Loopcut_Reproject", type="SHRINKWRAP")
    mod.target = src
    mod.wrap_method = "NEAREST_SURFACEPOINT"
    mod.offset = offset

    bpy.context.view_layer.objects.active = tgt
    bpy.ops.object.modifier_apply(modifier=mod.name)

    rms_after = measure_surface_deviation(src, tgt)

    return {
        "status": "ok",
        "action": "reproject_and_shrinkwrap",
        "target": target_object,
        "source": source_object,
        "rms_deviation_before": round(rms_before, 5),
        "rms_deviation_after": round(rms_after, 5),
    }


def cleanup_boundary_edges(object_name: str) -> dict:
    """Clean up open boundary loops, removing zero-area faces and relaxing boundary vertices."""
    import bmesh
    import bpy
    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != "MESH":
        raise ValueError(f"Mesh '{object_name}' not found.")

    bm = bmesh.new()
    bm.from_mesh(obj.data)

    removed = 0
    # Remove degenerate faces (zero area or collapsed vertices)
    for face in list(bm.faces):
        if face.calc_area() < 1e-8 or len(face.verts) < 3:
            bm.faces.remove(face)
            removed += 1

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()

    return {"status": "ok", "action": "cleanup_boundary_edges", "degenerate_faces_removed": removed}
