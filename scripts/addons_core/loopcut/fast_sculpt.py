"""Regional smooth and flatten in NumPy: a CSR adjacency built per call from the mesh's edges,
a bounding-box prefilter for the vertices in reach, and updates to that subset only.
"""

import numpy as np


def build_adjacency(edges: np.ndarray, num_verts: int) -> tuple[np.ndarray, np.ndarray]:
    """Build Compressed Sparse Row (CSR) adjacency representation:
    (start_indices, neighbor_indices).
    """
    if len(edges) == 0 or num_verts == 0:
        return np.zeros(num_verts + 1, dtype=np.int64), np.zeros(0, dtype=np.int64)

    src = np.concatenate([edges[:, 0], edges[:, 1]])
    dst = np.concatenate([edges[:, 1], edges[:, 0]])
    counts = np.bincount(src, minlength=num_verts)
    start = np.zeros(num_verts + 1, dtype=np.int64)
    np.cumsum(counts, out=start[1:])
    perm = np.argsort(src, kind="stable")
    return start, dst[perm]


def fast_neighbor_mean(co: np.ndarray, adjacency: tuple[np.ndarray, np.ndarray], idx: np.ndarray) -> np.ndarray:
    """Compute neighbor mean for ONLY the affected vertices in idx."""
    start, neighbors = adjacency
    counts = start[idx + 1] - start[idx]
    has_nbrs = counts > 0

    if not np.any(has_nbrs):
        return co[idx].copy()

    row_ids = np.repeat(np.arange(len(idx)), counts)
    offsets = np.repeat(start[idx], counts)
    cum_counts = np.cumsum(counts) - counts
    nbr_positions = np.arange(len(row_ids)) - np.repeat(cum_counts, counts) + offsets
    nbr_coords = co[neighbors[nbr_positions]]

    totals = np.stack(
        [np.bincount(row_ids, weights=nbr_coords[:, dim], minlength=len(idx)) for dim in range(3)],
        axis=1
    )
    result = co[idx].copy()
    result[has_nbrs] = totals[has_nbrs] / counts[has_nbrs, None]
    return result


def find_candidates_in_radius(co: np.ndarray, center: np.ndarray, radius: float) -> tuple[np.ndarray, np.ndarray]:
    """Fast candidate filtering using bounding box first, then exact Euclidean distance.
    Returns (vertex_indices, distances).
    """
    lo = center - radius
    hi = center + radius

    # Bounding box filter (fast vector comparison)
    in_box = np.all((co >= lo) & (co <= hi), axis=1)
    cand_indices = np.nonzero(in_box)[0]

    if len(cand_indices) == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float64)

    cand_co = co[cand_indices]
    diff = cand_co - center
    dist_sq = np.einsum("ij,ij->i", diff, diff)
    r_sq = radius * radius
    inside = dist_sq < r_sq

    return cand_indices[inside], np.sqrt(dist_sq[inside])


def smooth_region(co: np.ndarray, adjacency: tuple[np.ndarray, np.ndarray],
                  center: np.ndarray, radius: float, strength: float = 0.5,
                  iterations: int = 1) -> np.ndarray:
    """Perform fast local regional Laplacian smoothing on vertices within radius of center."""
    active_idx, dist = find_candidates_in_radius(co, center, radius)
    if len(active_idx) == 0:
        return co

    weights = (1.0 - (dist / radius)) ** 2  # Smooth falloff
    s = float(np.clip(strength, 0.0, 1.0))

    new_co = co.copy()
    for _ in range(iterations):
        target = fast_neighbor_mean(new_co, adjacency, active_idx)
        delta = (target - new_co[active_idx]) * (weights[:, None] * s)
        new_co[active_idx] += delta

    return new_co


def flatten_region(co: np.ndarray, no: np.ndarray, center: np.ndarray,
                   radius: float, strength: float = 0.5) -> np.ndarray:
    """Perform fast local regional planar flattening towards the average surface plane."""
    active_idx, dist = find_candidates_in_radius(co, center, radius)
    if len(active_idx) == 0:
        return co

    weights = (1.0 - (dist / radius)) ** 2
    avg_normal = np.sum(no[active_idx] * weights[:, None], axis=0)
    norm = np.linalg.norm(avg_normal)
    if norm < 1e-6:
        return co
    plane_normal = avg_normal / norm

    # Project vertices onto plane passing through center
    cand_co = co[active_idx]
    diff = cand_co - center
    plane_dist = diff @ plane_normal
    disp = np.outer(plane_dist, plane_normal)

    new_co = co.copy()
    new_co[active_idx] -= disp * (weights[:, None] * float(np.clip(strength, 0.0, 1.0)))
    return new_co
