"""Full UV and texturing controller for Loopcut.

Manages:
- Seam generation (by angle threshold, sharp edges)
- UV unwrapping and packing
- Texel density calculation and normalization
- Map baking (normals, AO, roughness, curvature)
- PBR material creation and shader node graph editing
"""

from typing import Literal


def mark_seams(object_name: str, angle_threshold_deg: float = 60.0) -> dict:
    """Automatically mark seams along sharp edges above the angle threshold."""
    import bmesh
    import bpy
    import math

    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != "MESH":
        raise ValueError(f"Mesh '{object_name}' not found.")

    bm = bmesh.new()
    bm.from_mesh(obj.data)

    radians_threshold = math.radians(angle_threshold_deg)
    seams_marked = 0

    for edge in bm.edges:
        if not edge.is_boundary and len(edge.link_faces) == 2:
            angle = edge.calc_face_angle()
            if angle > radians_threshold:
                edge.seam = True
                seams_marked += 1
        elif edge.is_boundary:
            edge.seam = True
            seams_marked += 1

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()

    return {"status": "ok", "action": "mark_seams", "object": object_name, "seams_marked": seams_marked}


def unwrap_and_pack(object_name: str, method: str = "ANGLE_BASED", margin: float = 0.01) -> dict:
    """Unwrap UVs using marked seams and pack islands."""
    import bpy
    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != "MESH":
        raise ValueError(f"Mesh '{object_name}' not found.")

    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")

    # Unwrap
    bpy.ops.uv.unwrap(method=method, margin=margin)
    # Pack
    bpy.ops.uv.pack_islands(margin=margin)

    bpy.ops.object.mode_set(mode="OBJECT")
    return {"status": "ok", "action": "unwrap_and_pack", "object": object_name, "margin": margin}


def calculate_texel_density(object_name: str, texture_size: int = 2048) -> float:
    """Calculate average texel density (pixels per meter) for object_name."""
    import bmesh
    import bpy

    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != "MESH":
        return 0.0

    me = obj.data
    if not me.uv_layers.active:
        return 0.0

    bm = bmesh.new()
    bm.from_mesh(me)
    uv_layer = bm.loops.layers.uv.verify()

    world_area = sum(f.calc_area() for f in bm.faces)
    uv_area = 0.0

    for f in bm.faces:
        if len(f.loops) >= 3:
            # Triangulate polygon loop UVs to calculate 2D area
            uvs = [l[uv_layer].uv for l in f.loops]
            for i in range(1, len(uvs) - 1):
                u0, v0 = uvs[0]
                u1, v1 = uvs[i]
                u2, v2 = uvs[i + 1]
                tri_uv_area = 0.5 * abs(u0 * (v1 - v2) + u1 * (v2 - v0) + u2 * (v0 - v1))
                uv_area += tri_uv_area

    bm.free()

    if world_area < 1e-8 or uv_area < 1e-8:
        return 0.0

    # Texel density = sqrt(pixel_count_used / world_area)
    pixel_area = uv_area * (texture_size * texture_size)
    density = (pixel_area / world_area) ** 0.5
    return float(density)


def create_pbr_material(material_name: str, base_color=(0.8, 0.8, 0.8, 1.0),
                        metallic: float = 0.0, roughness: float = 0.5) -> dict:
    """Create a new Principled BSDF PBR material with standard inputs."""
    import bpy
    mat = bpy.data.materials.get(material_name)
    if mat is None:
        mat = bpy.data.materials.new(name=material_name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf:
        if "Base Color" in bsdf.inputs:
            bsdf.inputs["Base Color"].default_value = base_color
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = metallic
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = roughness

    return {"status": "ok", "material": material_name, "metallic": metallic, "roughness": roughness}
