"""Local Blender Knowledge Base and API Retrieval for Loopcut.

Indexes Blender 5.2 Python documentation, installed add-on APIs,
and Loopcut playbooks for rapid local search when Swift requires syntax
or parameter details for unfamiliar operations.
"""

from pathlib import Path

# Builtin knowledge snippets for common Blender 5.2 patterns
BUILTIN_DOCS = {
    "bmesh_basics": {
        "title": "BMesh Mesh Editing",
        "keywords": ("bmesh", "edit mode", "vertex", "face", "polygon", "edge"),
        "snippet": (
            "import bmesh, bpy\n"
            "me = bpy.context.active_object.data\n"
            "bm = bmesh.from_edit_mesh(me) # If in edit mode\n"
            "# or bm = bmesh.new(); bm.from_mesh(me) # In object mode\n"
            "bm.verts.ensure_lookup_table()\n"
            "bmesh.update_edit_mesh(me)"
        ),
    },
    "shader_nodes": {
        "title": "Shader Nodes Setup",
        "keywords": ("material", "shader", "principled", "nodes", "texture"),
        "snippet": (
            "mat.use_nodes = True\n"
            "nodes = mat.node_tree.nodes\n"
            "bsdf = nodes.get('Principled BSDF')\n"
            "mat.node_tree.links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])"
        ),
    },
    "sculpt_dyntopo": {
        "title": "Sculpt Dynamic Topology",
        "keywords": ("dyntopo", "sculpt", "remesh", "detail size"),
        "snippet": (
            "bpy.ops.sculpt.dynamic_topology_toggle()\n"
            "bpy.context.tool_settings.sculpt.detail_size = 12.0\n"
            "bpy.context.tool_settings.sculpt.detail_refine_method = 'SUBDIVIDE_COLLAPSE'"
        ),
    },
    "geometry_nodes": {
        "title": "Geometry Nodes Setup",
        "keywords": ("geometry nodes", "modifier", "node group", "sockets"),
        "snippet": (
            "mod = obj.modifiers.new('GN', 'NODES')\n"
            "tree = bpy.data.node_groups.new('MyTree', 'GeometryNodeTree')\n"
            "mod.node_group = tree"
        ),
    },
}


def search_knowledge_base(query: str, limit: int = 3) -> list[dict]:
    """Search indexed documentation snippets matching all or some query keywords."""
    terms = query.lower().split()
    if not terms:
        return []

    hits = []
    for doc_id, doc in BUILTIN_DOCS.items():
        score = 0
        text = f"{doc['title']} {' '.join(doc['keywords'])} {doc['snippet']}".lower()
        for t in terms:
            if t in text:
                score += 1
        if score > 0:
            hits.append((score, {"id": doc_id, "title": doc["title"], "snippet": doc["snippet"]}))

    hits.sort(key=lambda x: x[0], reverse=True)
    return [h[1] for h in hits[:limit]]
