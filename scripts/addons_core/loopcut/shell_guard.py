"""Thin-shell sculpt protection for Loopcut.

Prevents flattening or aggressive sculpt brushes from collapsing armor shells,
clothing layers, and thin metal plates by enforcing:
- Front-facing normal filtering (preventing back-wall collapse)
- Minimum wall thickness checks
- Maximum displacement bounds based on local shell thickness
"""

import numpy as np


def filter_front_facing_vertices(co: np.ndarray, no: np.ndarray, candidate_idx: np.ndarray,
                                 reference_normal: np.ndarray, dot_threshold: float = 0.0) -> np.ndarray:
    """Filter vertex indices so only those facing roughly the same direction as reference_normal
    are kept. Vertices facing the opposite direction (e.g. inner wall of an armor plate)
    are excluded from sculpt deformation.
    """
    if len(candidate_idx) == 0:
        return candidate_idx

    cand_normals = no[candidate_idx]
    dots = cand_normals @ reference_normal
    front_facing = dots > dot_threshold
    return candidate_idx[front_facing]


def estimate_local_shell_thickness(co: np.ndarray, no: np.ndarray, vertex_idx: int,
                                   search_radius: float = 0.05) -> float:
    """Estimate the local wall thickness around vertex_idx by looking for opposing-normal vertices
    along the inverse normal ray.
    """
    p = co[vertex_idx]
    n = no[vertex_idx]

    # Search candidates within search radius
    diff = co - p
    dist_sq = np.einsum("ij,ij->i", diff, diff)
    in_range = np.nonzero((dist_sq < search_radius * search_radius) & (dist_sq > 1e-8))[0]

    if len(in_range) == 0:
        return search_radius

    # Opposing normals have negative dot product with n (< -0.5)
    cand_no = no[in_range]
    dots = cand_no @ n
    opposing = in_range[dots < -0.5]

    if len(opposing) == 0:
        return search_radius

    # Measure distance along normal
    opposing_diff = co[opposing] - p
    proj_dist = np.abs(opposing_diff @ n)
    return float(np.min(proj_dist))


def guard_shell_displacement(co: np.ndarray, no: np.ndarray, active_idx: np.ndarray,
                             proposed_displacement: np.ndarray, min_thickness: float = 0.005,
                             max_disp_factor: float = 0.4) -> np.ndarray:
    """Clamp proposed displacement vectors so that thin walls do not collapse.
    Displacement along the surface normal is restricted to a fraction of estimated shell thickness.
    """
    clamped_disp = proposed_displacement.copy()

    for i, v_idx in enumerate(active_idx):
        thickness = estimate_local_shell_thickness(co, no, v_idx, search_radius=min_thickness * 4.0)
        max_allowed = max(min_thickness * 0.1, thickness * max_disp_factor)

        disp = clamped_disp[i]
        n = no[v_idx]
        normal_comp = disp @ n

        # If displacement pushes inward (opposite normal) more than allowed
        if normal_comp < -max_allowed:
            clamped_disp[i] = disp - (normal_comp + max_allowed) * n
        elif normal_comp > max_allowed:
            clamped_disp[i] = disp - (normal_comp - max_allowed) * n

    return clamped_disp
