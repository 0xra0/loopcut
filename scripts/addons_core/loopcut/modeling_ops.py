"""Structured safe, typed modeling operations for Loopcut.

Wraps core Blender mesh operations with context validation, bmesh updates,
and typed parameters: extrude, inset, bevel, boolean, mirror, knife, bridge,
separate, merge, solidify, shrinkwrap, and subdivision.
"""

from typing import Literal


def _ensure_active_mesh(obj_name: str, target_mode: str = "EDIT"):
    """Ensure the target object is active and in the specified mode."""
    import bpy
    obj = bpy.data.objects.get(obj_name)
    if not obj or obj.type != "MESH":
        raise ValueError(f"Mesh object '{obj_name}' not found.")
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    if bpy.context.mode != f"{target_mode}_MESH" and bpy.context.mode != target_mode:
        bpy.ops.object.mode_set(mode=target_mode)
    return obj


def extrude(object_name: str, distance: float = 0.1, direction: tuple[float, float, float] | None = None) -> dict:
    """Extrude selected faces or all faces of a mesh along normal or given vector."""
    import bpy
    obj = _ensure_active_mesh(object_name, "EDIT")
    dir_vec = direction or (0.0, 0.0, distance)
    bpy.ops.mesh.extrude_region_move(
        TRANSFORM_OT_translate={"value": dir_vec, "orient_type": "NORMAL"}
    )
    bpy.ops.object.mode_set(mode="OBJECT")
    return {"status": "ok", "action": "extrude", "object": object_name, "distance": distance}


def inset(object_name: str, thickness: float = 0.05, depth: float = 0.0) -> dict:
    """Inset selected or all faces."""
    import bpy
    obj = _ensure_active_mesh(object_name, "EDIT")
    bpy.ops.mesh.inset(thickness=thickness, depth=depth)
    bpy.ops.object.mode_set(mode="OBJECT")
    return {"status": "ok", "action": "inset", "object": object_name, "thickness": thickness, "depth": depth}


def bevel(object_name: str, width: float = 0.02, segments: int = 2, profile: float = 0.5, affect: str = "EDGES") -> dict:
    """Apply bevel to selected edges/vertices."""
    import bpy
    obj = _ensure_active_mesh(object_name, "EDIT")
    bpy.ops.mesh.bevel(offset=width, segments=segments, profile=profile, affect=affect)
    bpy.ops.object.mode_set(mode="OBJECT")
    return {"status": "ok", "action": "bevel", "object": object_name, "width": width, "segments": segments}


def boolean_op(object_name: str, target_object: str, operation: str = "DIFFERENCE", solver: str = "EXACT") -> dict:
    """Apply a boolean modifier between object_name and target_object and apply it."""
    import bpy
    obj = _ensure_active_mesh(object_name, "OBJECT")
    tgt = bpy.data.objects.get(target_object)
    if not tgt or tgt.type != "MESH":
        raise ValueError(f"Target boolean mesh '{target_object}' not found.")

    mod = obj.modifiers.new(name="Loopcut_Boolean", type="BOOLEAN")
    mod.operation = operation.upper()
    mod.object = tgt
    mod.solver = solver.upper()

    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=mod.name)
    return {"status": "ok", "action": "boolean", "object": object_name, "target": target_object, "operation": operation}


def mirror(object_name: str, axis: str = "X", use_clip: bool = True, merge_threshold: float = 0.001) -> dict:
    """Add a mirror modifier to object."""
    import bpy
    obj = _ensure_active_mesh(object_name, "OBJECT")
    mod = obj.modifiers.new(name="Loopcut_Mirror", type="MIRROR")
    mod.use_axis[0] = "X" in axis.upper()
    mod.use_axis[1] = "Y" in axis.upper()
    mod.use_axis[2] = "Z" in axis.upper()
    mod.use_clip = use_clip
    mod.merge_threshold = merge_threshold
    return {"status": "ok", "action": "mirror", "object": object_name, "axis": axis}


def bridge(object_name: str) -> dict:
    """Bridge selected edge loops."""
    import bpy
    obj = _ensure_active_mesh(object_name, "EDIT")
    bpy.ops.mesh.bridge_edge_loops()
    bpy.ops.object.mode_set(mode="OBJECT")
    return {"status": "ok", "action": "bridge", "object": object_name}


def separate(object_name: str, mode: str = "LOOSE") -> dict:
    """Separate mesh parts (LOOSE, SELECTED, MATERIAL)."""
    import bpy
    obj = _ensure_active_mesh(object_name, "EDIT")
    bpy.ops.mesh.separate(type=mode.upper())
    bpy.ops.object.mode_set(mode="OBJECT")
    return {"status": "ok", "action": "separate", "object": object_name, "mode": mode}


def merge_verts(object_name: str, mode: str = "DISTANCE", distance: float = 0.0001) -> dict:
    """Merge vertices (DISTANCE, CENTER, COLLAPSE)."""
    import bpy
    obj = _ensure_active_mesh(object_name, "EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    if mode.upper() == "DISTANCE":
        bpy.ops.mesh.remove_doubles(threshold=distance)
    elif mode.upper() == "CENTER":
        bpy.ops.mesh.merge(type="CENTER")
    elif mode.upper() == "COLLAPSE":
        bpy.ops.mesh.merge(type="COLLAPSE")
    bpy.ops.object.mode_set(mode="OBJECT")
    return {"status": "ok", "action": "merge", "object": object_name, "mode": mode, "distance": distance}


def solidify(object_name: str, thickness: float = 0.02, offset: float = -1.0) -> dict:
    """Add a solidify modifier with given thickness."""
    import bpy
    obj = _ensure_active_mesh(object_name, "OBJECT")
    mod = obj.modifiers.new(name="Loopcut_Solidify", type="SOLIDIFY")
    mod.thickness = thickness
    mod.offset = offset
    return {"status": "ok", "action": "solidify", "object": object_name, "thickness": thickness}


def shrinkwrap(object_name: str, target_object: str, offset: float = 0.0) -> dict:
    """Add a shrinkwrap modifier projecting object onto target."""
    import bpy
    obj = _ensure_active_mesh(object_name, "OBJECT")
    tgt = bpy.data.objects.get(target_object)
    if not tgt:
        raise ValueError(f"Target object '{target_object}' not found.")
    mod = obj.modifiers.new(name="Loopcut_Shrinkwrap", type="SHRINKWRAP")
    mod.target = tgt
    mod.offset = offset
    return {"status": "ok", "action": "shrinkwrap", "object": object_name, "target": target_object}


def subdivide(object_name: str, cuts: int = 1) -> dict:
    """Subdivide mesh edges/faces."""
    import bpy
    obj = _ensure_active_mesh(object_name, "EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.subdivide(number_cuts=cuts)
    bpy.ops.object.mode_set(mode="OBJECT")
    return {"status": "ok", "action": "subdivide", "object": object_name, "cuts": cuts}
