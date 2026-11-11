"""Full sculpt-mode controller for Loopcut.

Manages native Blender sculpt operations, brushes, masks, face sets,
symmetry, dynamic topology (dyntopo), remeshing, and multiresolution modifiers.
"""

from typing import Literal


def ensure_sculpt_mode(obj_name: str):
    """Ensure the target mesh object is active and in SCULPT mode."""
    import bpy
    obj = bpy.data.objects.get(obj_name)
    if not obj or obj.type != "MESH":
        raise ValueError(f"Mesh object '{obj_name}' not found.")
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    if bpy.context.mode != "SCULPT":
        bpy.ops.object.mode_set(mode="SCULPT")
    return obj


def set_brush_properties(brush_name: str = "Clay", radius: int = 50, strength: float = 0.5,
                         symmetry: str = "X") -> dict:
    """Configure active sculpt brush settings."""
    import bpy
    tool_settings = bpy.context.tool_settings.sculpt
    if hasattr(bpy.data, "brushes") and brush_name in bpy.data.brushes:
        tool_settings.brush = bpy.data.brushes[brush_name]

    if tool_settings.brush:
        tool_settings.brush.size = max(1, int(radius))
        tool_settings.brush.strength = float(strength)

    # Set symmetry
    tool_settings.use_symmetry_x = "X" in symmetry.upper()
    tool_settings.use_symmetry_y = "Y" in symmetry.upper()
    tool_settings.use_symmetry_z = "Z" in symmetry.upper()

    return {
        "status": "ok",
        "brush": brush_name,
        "radius": radius,
        "strength": strength,
        "symmetry": symmetry,
    }


def dyntopo_toggle(obj_name: str, enable: bool = True, detail_size: float = 12.0,
                   refine_method: str = "SUBDIVIDE_COLLAPSE") -> dict:
    """Enable or disable Dynamic Topology (dyntopo) on a mesh."""
    import bpy
    ensure_sculpt_mode(obj_name)
    sculpt = bpy.context.tool_settings.sculpt

    is_enabled = bpy.context.sculpt_object.use_dynamic_topology_sculpting
    if enable != is_enabled:
        bpy.ops.sculpt.dynamic_topology_toggle()

    if enable:
        sculpt.detail_size = detail_size
        sculpt.detail_refine_method = refine_method

    return {"status": "ok", "dyntopo_active": enable, "detail_size": detail_size}


def sculpt_mask(obj_name: str, action: str = "CLEAR") -> dict:
    """Mask operations in sculpt mode (CLEAR, INVERT)."""
    import bpy
    ensure_sculpt_mode(obj_name)
    if action.upper() == "CLEAR":
        bpy.ops.paint.mask_flood_fill(mode="VALUE", value=0.0)
    elif action.upper() == "INVERT":
        bpy.ops.paint.mask_flood_fill(mode="INVERT")
    return {"status": "ok", "action": f"mask_{action.lower()}"}


def sculpt_face_sets(obj_name: str, action: str = "LOOSE_PARTS") -> dict:
    """Initialize or modify Face Sets in sculpt mode."""
    import bpy
    ensure_sculpt_mode(obj_name)
    if action.upper() == "LOOSE_PARTS":
        bpy.ops.sculpt.face_sets_create(mode="LOOSE_PARTS")
    elif action.upper() == "CLEAR":
        bpy.ops.sculpt.face_sets_create(mode="ALL")
    return {"status": "ok", "action": f"face_sets_{action.lower()}"}


def voxel_remesh(obj_name: str, voxel_size: float = 0.02, adaptivity: float = 0.0) -> dict:
    """Perform voxel remesh on the object."""
    import bpy
    obj = bpy.data.objects.get(obj_name)
    if not obj or obj.type != "MESH":
        raise ValueError(f"Mesh object '{obj_name}' not found.")
    bpy.context.view_layer.objects.active = obj
    obj.data.remesh_voxel_size = max(0.001, voxel_size)
    obj.data.remesh_voxel_adaptivity = adaptivity
    bpy.ops.object.voxel_remesh()
    return {"status": "ok", "action": "voxel_remesh", "voxel_size": voxel_size}
