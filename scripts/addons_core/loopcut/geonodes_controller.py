"""Geometry Nodes controller for Loopcut.

Provides structured tools to:
- Inspect Geometry Nodes modifier inputs, outputs, and sockets
- Create nodes and connect links
- Configure parameters and defaults
- Construct reusable node groups
- Inspect evaluated geometry (vertex counts, bounding box) via depsgraph
"""


def inspect_geometry_nodes(object_name: str, modifier_name: str = "") -> dict:
    """Inspect geometry nodes modifiers on object_name."""
    import bpy
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found.")

    mods = [m for m in obj.modifiers if m.type == "NODES"]
    if not mods:
        return {"status": "none", "modifiers": []}

    target_mod = next((m for m in mods if not modifier_name or m.name == modifier_name), mods[0])
    tree = target_mod.node_group
    if not tree:
        return {"status": "empty", "modifier": target_mod.name}

    inputs = []
    outputs = []
    for item in getattr(tree, "interface", tree).items_tree if hasattr(tree, "interface") else []:
        if getattr(item, "item_type", "") == "SOCKET":
            data = {"name": item.name, "type": getattr(item, "socket_type", "VALUE")}
            if item.in_out == "INPUT":
                inputs.append(data)
            else:
                outputs.append(data)

    nodes_summary = [{"name": n.name, "type": n.type, "label": n.label} for n in tree.nodes]

    return {
        "status": "ok",
        "modifier": target_mod.name,
        "node_tree": tree.name,
        "inputs": inputs,
        "outputs": outputs,
        "nodes_count": len(tree.nodes),
        "nodes": nodes_summary,
    }


def add_geometry_nodes_modifier(object_name: str, modifier_name: str = "GeometryNodes") -> dict:
    """Add a fresh Geometry Nodes modifier to object_name."""
    import bpy
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found.")

    mod = obj.modifiers.new(name=modifier_name, type="NODES")
    tree = bpy.data.node_groups.new(name=f"{object_name}_GN", type="GeometryNodeTree")
    mod.node_group = tree

    # Add standard Group Input and Group Output nodes
    in_node = tree.nodes.new("NodeGroupInput")
    out_node = tree.nodes.new("NodeGroupOutput")
    in_node.location = (-200, 0)
    out_node.location = (200, 0)

    return {"status": "ok", "action": "add_geometry_nodes", "object": object_name, "node_group": tree.name}


def get_evaluated_geometry_metrics(object_name: str) -> dict:
    """Query the evaluated geometry of object_name after all modifiers (including GN) execute."""
    import bpy
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found.")

    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    me = eval_obj.to_mesh() if hasattr(eval_obj, "to_mesh") else None

    if not me:
        return {"status": "error", "message": "Could not evaluate mesh geometry"}

    metrics = {
        "status": "ok",
        "object": object_name,
        "evaluated_verts": len(me.vertices),
        "evaluated_polygons": len(me.polygons),
        "bounding_box_dimensions": list(eval_obj.dimensions),
    }
    eval_obj.to_mesh_clear()
    return metrics
