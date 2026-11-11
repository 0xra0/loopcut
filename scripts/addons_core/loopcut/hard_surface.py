"""Hard-surface reconstruction tools for Loopcut.

Rebuilds clean armor plates, straight edges, circular emblems, precise symbols,
and curved metal trim from noisy AI-generated or Pixal3D meshes:
- Planar flattening (SVD plane fitting and projection)
- Linear edge loop straightening
- Circular emblem and cylindrical trim regularization
- Crease and bevel weight assignment
"""

import numpy as np


def planarize_vertices(co: np.ndarray, vertex_indices: list[int] | np.ndarray) -> np.ndarray:
    """Fit a plane via Singular Value Decomposition (SVD) to the selected vertices
    and project them onto the plane.
    """
    idx = np.array(vertex_indices, dtype=np.int64)
    if len(idx) < 3:
        return co

    subset = co[idx]
    center = np.mean(subset, axis=0)
    centered = subset - center

    # SVD on centered coordinates to find plane normal (direction of minimum variance)
    _, _, vh = np.linalg.svd(centered)
    normal = vh[2]  # Smallest singular value corresponds to normal

    # Distance from center along normal
    dists = centered @ normal
    projected = subset - np.outer(dists, normal)

    new_co = co.copy()
    new_co[idx] = projected
    return new_co


def straighten_edge_path(co: np.ndarray, vertex_path: list[int]) -> np.ndarray:
    """Straighten an ordered sequence of vertices between path[0] and path[-1]."""
    if len(vertex_path) < 3:
        return co

    p_start = co[vertex_path[0]]
    p_end = co[vertex_path[-1]]
    vec = p_end - p_start
    length_sq = float(np.dot(vec, vec))
    if length_sq < 1e-8:
        return co

    unit_vec = vec / np.sqrt(length_sq)
    new_co = co.copy()

    for v_idx in vertex_path[1:-1]:
        p = co[v_idx]
        proj_length = float(np.dot(p - p_start, unit_vec))
        new_co[v_idx] = p_start + proj_length * unit_vec

    return new_co


def circularize_vertices(co: np.ndarray, vertex_indices: list[int],
                         target_radius: float | None = None) -> np.ndarray:
    """Project vertices to a best-fit circle in their best-fit plane, creating clean circular emblems."""
    idx = np.array(vertex_indices, dtype=np.int64)
    if len(idx) < 3:
        return co

    # 1. Planarize first
    co_flat = planarize_vertices(co, idx)
    subset = co_flat[idx]
    center = np.mean(subset, axis=0)

    # 2. Compute radii from center
    diff = subset - center
    radii = np.linalg.norm(diff, axis=1)
    avg_radius = target_radius if target_radius is not None else float(np.mean(radii))
    if avg_radius < 1e-6:
        return co

    # 3. Project to uniform radius
    unit_dirs = diff / np.maximum(radii[:, None], 1e-8)
    circular = center + unit_dirs * avg_radius

    new_co = co_flat.copy()
    new_co[idx] = circular
    return new_co


def rebuild_armor_plate(object_name: str, face_indices: list[int] | None = None) -> dict:
    """Perform hard-surface reconstruction on an armor plate of object_name."""
    import bpy
    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != "MESH":
        raise ValueError(f"Mesh '{object_name}' not found.")

    me = obj.data
    co = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)

    if face_indices is None:
        # Planarize entire mesh or selected faces
        idx = np.arange(len(co))
    else:
        v_set = set()
        for fi in face_indices:
            if fi < len(me.polygons):
                v_set.update(me.polygons[fi].vertices)
        idx = np.array(sorted(v_set), dtype=np.int64)

    new_co = planarize_vertices(co, idx)
    me.vertices.foreach_set("co", new_co.ravel())
    me.update()

    return {"status": "ok", "action": "rebuild_armor_plate", "object": object_name, "vertices_reconstructed": len(idx)}
