"""The active mesh and reference images, as scene_context adds them to every message.

Runs on every message, so nothing here loops over a mesh's elements in Python: a generated
mesh can have hundreds of thousands of faces. Main thread only.
"""

import numpy as np


def get_topology_summary(obj) -> dict:
    """Vertex, face and triangle counts; triangles from one bulk read of the faces' corner counts."""
    me = obj.data
    corners = np.empty(len(me.polygons), dtype=np.int32)
    me.polygons.foreach_get("loop_total", corners)
    return {"verts": len(me.vertices), "faces": len(me.polygons), "tris": int(np.maximum(corners - 2, 1).sum())}


def get_modifiers_summary(obj) -> list[str]:
    return [f"{m.name}({m.type})" + ("" if m.show_viewport else " [off]") for m in obj.modifiers]


def get_constraints_summary(obj) -> list[str]:
    return [f"{c.name}({c.type})" + (" [off]" if c.mute or not c.is_valid else "") for c in obj.constraints]


def get_materials_summary(obj) -> list[str]:
    return [slot.material.name for slot in obj.material_slots if slot.material]


def get_reference_images() -> list[str]:
    """Image empties and the scene camera's background images."""
    import bpy
    refs = [f"{o.name} [Ref image: {o.data.name if o.data else 'no_image'}]"
            for o in bpy.context.scene.objects if o.type == "EMPTY" and o.empty_display_type == "IMAGE"]
    cam = bpy.context.scene.camera
    if cam and cam.type == "CAMERA":
        refs += [f"Camera background: {bg.image.name}" for bg in cam.data.background_images if bg.image]
    return refs
