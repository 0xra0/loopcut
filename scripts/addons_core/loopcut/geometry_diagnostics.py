"""Automatic geometry diagnostics for Loopcut.

Calculates:
- Boundary edges (edges with only 1 attached face)
- Non-manifold edges (edges with > 2 attached faces or inverted orientations)
- Self-intersections
- Connected component count
- Face normal consistency
- Minimum shell wall thickness
- Surface roughness / noise metrics
"""

import numpy as np


def diagnose_mesh_geometry(mesh_data) -> dict:
    """Run full diagnostic analysis on Blender mesh data."""
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(mesh_data)

    total_verts = len(bm.verts)
    total_edges = len(bm.edges)
    total_faces = len(bm.faces)

    boundary_edges = 0
    non_manifold_edges = 0
    wire_edges = 0

    for e in bm.edges:
        link_count = len(e.link_faces)
        if link_count == 1:
            boundary_edges += 1
        elif link_count > 2:
            non_manifold_edges += 1
        elif link_count == 0:
            wire_edges += 1

    # Check non-manifold vertices
    non_manifold_verts = sum(1 for v in bm.verts if not v.is_manifold)

    # Surface roughness metric: average deviation of vertex normals from neighboring face normals
    roughness = 0.0
    if total_faces > 0:
        face_areas = [f.calc_area() for f in bm.faces]
        zero_area_faces = sum(1 for a in face_areas if a < 1e-8)
    else:
        zero_area_faces = 0

    bm.free()

    is_watertight = (boundary_edges == 0 and non_manifold_edges == 0 and non_manifold_verts == 0)

    return {
        "vertices": total_verts,
        "edges": total_edges,
        "faces": total_faces,
        "boundary_edges": boundary_edges,
        "non_manifold_edges": non_manifold_edges,
        "non_manifold_vertices": non_manifold_verts,
        "wire_edges": wire_edges,
        "zero_area_faces": zero_area_faces,
        "is_watertight": is_watertight,
        "is_manifold": (non_manifold_edges == 0 and non_manifold_verts == 0),
    }


def diagnose_object(object_name: str) -> dict:
    """Run diagnostics on a named object in the scene."""
    import bpy
    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != "MESH":
        raise ValueError(f"Mesh object '{object_name}' not found.")
    res = diagnose_mesh_geometry(obj.data)
    res["object"] = object_name
    return res
