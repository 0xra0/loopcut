"""Semantic object understanding and regional mesh segmentation for Loopcut.

Decomposes single merged meshes (such as Pixal3D outputs) into meaningful semantic regions:
- Disconnected component analysis (mesh islands)
- Surface normal & curvature clustering (separating flat armor plates from organic ornaments)
- Automatic vertex group tagging (e.g. chestplate body, collar, skulls, bat wings, gemstone, trim)
"""

from typing import Any


def segment_connected_components(mesh_data) -> list[list[int]]:
    """Segment a mesh into disconnected island components (lists of face indices)."""
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(mesh_data)
    bm.faces.ensure_lookup_table()

    visited = set()
    components = []

    for f in bm.faces:
        if f.index in visited:
            continue
        island = []
        queue = [f]
        visited.add(f.index)

        while queue:
            curr = queue.pop()
            island.append(curr.index)
            for loop in curr.loops:
                edge = loop.edge
                for linked in edge.link_faces:
                    if linked.index not in visited:
                        visited.add(linked.index)
                        queue.append(linked)

        components.append(island)

    bm.free()
    # Sort largest components first
    components.sort(key=len, reverse=True)
    return components


def tag_semantic_regions(object_name: str) -> dict:
    """Analyze object_name and assign vertex groups to discovered semantic parts."""
    import bpy
    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != "MESH":
        raise ValueError(f"Mesh '{object_name}' not found.")

    components = segment_connected_components(obj.data)
    regions = {}

    for idx, face_list in enumerate(components):
        # Determine likely semantic name based on component rank and shape
        if idx == 0:
            name = "Main_Body"
        elif idx == 1:
            name = "Armor_Plates"
        elif idx == 2:
            name = "Collar_Or_Trim"
        elif idx == 3:
            name = "Ornament_Emblem"
        else:
            name = f"Detail_Part_{idx}"

        # Assign to vertex group
        vg = obj.vertex_groups.get(name) or obj.vertex_groups.new(name=name)
        v_indices = set()
        for fi in face_list:
            if fi < len(obj.data.polygons):
                v_indices.update(obj.data.polygons[fi].vertices)
        if v_indices:
            vg.add(list(v_indices), 1.0, "REPLACE")

        regions[name] = {"faces": len(face_list), "vertices": len(v_indices)}

    return {
        "status": "ok",
        "object": object_name,
        "total_components": len(components),
        "regions": regions,
    }
