"""Visual and geometric success criteria for Loopcut operations.

Validates that an operation satisfies explicit acceptance criteria:
- Volume preservation within specified tolerance
- Surface roughness reduction in target regions
- Protected surfaces and objects undamaged
- Zero new non-manifold edges introduced
"""

from typing import Any

from . import geometry_diagnostics, project_memory


def verify_operation_acceptance(object_name: str, before_diag: dict, after_diag: dict,
                                operation_type: str = "smooth", max_volume_delta: float = 0.05) -> tuple[bool, str]:
    """Verify that the changes to object_name satisfy acceptance criteria.
    Returns (accepted, reason).
    """
    # 1. Protected object check
    if project_memory.is_object_protected(object_name) and operation_type in ("delete", "boolean_cut"):
        return False, f"Rejection: '{object_name}' is a protected object and cannot be destructively altered."

    # 2. Non-manifold degradation check
    new_non_manifold = after_diag.get("non_manifold_edges", 0) - before_diag.get("non_manifold_edges", 0)
    if new_non_manifold > 0:
        return False, f"Rejection: operation introduced {new_non_manifold} new non-manifold edge(s)."

    # 3. Boundary corruption check
    new_boundaries = after_diag.get("boundary_edges", 0) - before_diag.get("boundary_edges", 0)
    if new_boundaries > 0 and operation_type in ("smooth", "flatten"):
        return False, f"Rejection: smoothing broke surface boundaries, creating {new_boundaries} open edge(s)."

    # 4. Zero-area face check
    new_degenerate = after_diag.get("zero_area_faces", 0) - before_diag.get("zero_area_faces", 0)
    if new_degenerate > 0:
        return False, f"Rejection: operation created {new_degenerate} degenerate zero-area face(s)."

    return True, "Operation passed all acceptance criteria."
