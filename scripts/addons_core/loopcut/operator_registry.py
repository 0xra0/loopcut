"""Universal Blender Operator Registry for Loopcut.

Discovers and describes registered bpy.ops, RNA properties, and operators supplied
by installed add-ons, including polling requirements, context prerequisites,
and supported Blender modes.
"""

from typing import Any


def _get_bpy_op(op_id: str):
    """Resolve an operator string e.g. 'mesh.primitive_cube_add' to bpy.ops function."""
    import bpy
    ops = getattr(bpy, "ops", None)
    if ops is None:
        return None
    parts = op_id.replace("bpy.ops.", "").split(".")
    if len(parts) != 2:
        return None
    cat, name = parts
    sub = getattr(ops, cat, None)
    if sub is None:
        return None
    return getattr(sub, name, None)


def test_poll(op_id: str) -> tuple[bool, str | None]:
    """Test if an operator can be called in current context without raising an exception.
    Returns (can_run, reason_if_cannot).
    """
    import bpy
    op = _get_bpy_op(op_id)
    if op is None:
        return False, f"Operator '{op_id}' is not registered or found in bpy.ops."

    mode = getattr(bpy.context, "mode", "OBJECT")
    act = getattr(bpy.context.view_layer.objects, "active", None) if hasattr(bpy.context, "view_layer") else None

    # Check operator poll
    poll_fn = getattr(op, "poll", None)
    if callable(poll_fn):
        try:
            if not poll_fn():
                # Provide diagnostic context reason
                cat = op_id.split(".")[0]
                if cat == "mesh" and mode != "EDIT_MESH":
                    return False, f"Operator '{op_id}' poll() failed: mesh operators generally require EDIT_MESH mode (current mode: {mode})."
                if cat == "sculpt" and mode != "SCULPT":
                    return False, f"Operator '{op_id}' poll() failed: sculpt operators require SCULPT mode (current mode: {mode})."
                if not act:
                    return False, f"Operator '{op_id}' poll() failed: no active object in current view layer."
                return False, f"Operator '{op_id}' poll() returned False in current context ({mode}, active: {act.name if act else 'none'})."
        except Exception as ex:
            return False, f"Operator poll() raised {type(ex).__name__}: {ex}"

    return True, None


def describe_operator(op_id: str) -> dict:
    """Return detailed metadata, docstrings, and RNA parameters for an operator."""
    import bpy
    op = _get_bpy_op(op_id)
    clean_id = op_id.replace("bpy.ops.", "")
    res = {
        "id": clean_id,
        "full_name": f"bpy.ops.{clean_id}",
        "registered": op is not None,
        "doc": "",
        "parameters": {},
        "poll_ok": False,
        "poll_note": None,
    }
    if op is None:
        res["poll_note"] = "Operator not found"
        return res

    can_poll, poll_err = test_poll(op_id)
    res["poll_ok"] = can_poll
    res["poll_note"] = poll_err

    # Read RNA properties
    rna = getattr(op, "get_rna_type", None)
    if callable(rna):
        try:
            rna_type = rna()
            res["doc"] = getattr(rna_type, "description", "")
            for prop in getattr(rna_type, "properties", []):
                if prop.identifier in ("rna_type",):
                    continue
                res["parameters"][prop.identifier] = {
                    "name": getattr(prop, "name", prop.identifier),
                    "type": getattr(prop, "type", "UNKNOWN"),
                    "description": getattr(prop, "description", ""),
                    "default": getattr(prop, "default", None),
                }
        except Exception:
            pass

    return res


def search_operators(query: str, category: str = "", limit: int = 25) -> list[dict]:
    """Search registered bpy.ops operators by name or category."""
    import bpy
    ops = getattr(bpy, "ops", None)
    if ops is None:
        return []
    terms = query.lower().split()
    results = []

    for cat_name in dir(ops):
        if cat_name.startswith("_"):
            continue
        if category and cat_name.lower() != category.lower():
            continue
        cat_module = getattr(bpy.ops, cat_name, None)
        if not cat_module:
            continue

        for op_name in dir(cat_module):
            if op_name.startswith("_"):
                continue
            full_id = f"{cat_name}.{op_name}"
            if terms and not all(t in full_id.lower() for t in terms):
                continue

            op = getattr(cat_module, op_name, None)
            doc = ""
            rna = getattr(op, "get_rna_type", None)
            if callable(rna):
                try:
                    doc = getattr(rna(), "description", "")
                except Exception:
                    pass

            results.append({
                "id": full_id,
                "category": cat_name,
                "name": op_name,
                "description": doc,
            })
            if len(results) >= limit:
                return results

    return results
