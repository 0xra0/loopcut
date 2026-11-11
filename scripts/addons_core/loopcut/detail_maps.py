"""Detail normal maps (game-asset-quality §8): a tiling CC0 micro-normal over a material's own normal map.

A baked normal map holds the shape at its own resolution; close up, a game character's cloth, skin or wood reads as
plastic. add_detail_normal lays a tiling micro-normal (Poly Haven, CC0, through polyhaven.py's API, checked download
and cache: only its OpenGL normal map, nor_gl, 1k) over the material's normal map, by material type:

    felt polar_fleece | fabric cotton_jersey | skin leather_white (fine, even grain; Poly Haven has no skin set) |
    leather brown_leather | wood oak_veneer_01 | metal metal_plate_02 | stone rock_01

In Blender the two are combined by reoriented normal mapping (RNM, Barré-Brisebois and Hill 2012), in the node group
"Loopcut Detail Normal" between the base map's Image Texture and its Normal Map node: base n1 and detail n2 decoded
to [-1, 1], the detail's x and y scaled by strength, t = n1 + (0, 0, 1), u = n2 * (-1, -1, 1),
r = t * dot(t, u) / t.z - u, normalised and encoded back. The detail repeats `scale` times per UV unit (default from
the texture's real size and the mesh's size per UV unit, so a jersey weave is jersey-sized) and its strength defaults
low. Without a normal map, one is made with a flat base. Calling it again on a material updates its detail.

glTF has no detail-map slot and engines' FBX importers read one normal map, so export_for_engine bakes the combined
normal into a temporary image at the base map's size (rnm() below, the same math in numpy) and exports that as the
material's normal map, for every engine; the material is put back after.

harness/detail_maps_check.py checks it on SPLAP (the export bake against a Cycles bake of the node group).
"""

import json
import math
import urllib.parse
from pathlib import Path

KINDS = {  # kind: (Poly Haven texture, default strength)
    "felt": ("polar_fleece", 0.35), "fabric": ("cotton_jersey", 0.35), "skin": ("leather_white", 0.2),
    "leather": ("brown_leather", 0.35), "wood": ("oak_veneer_01", 0.3), "metal": ("metal_plate_02", 0.25),
    "stone": ("rock_01", 0.35)}
TILE_METRES = {"polar_fleece": 0.273, "cotton_jersey": 0.264, "leather_white": 0.3, "brown_leather": 0.4,
               "oak_veneer_01": 1.83, "metal_plate_02": 2.0, "rock_01": 1.5}  # Poly Haven's published dimensions.
RESOLUTION = "1k"
GROUP = "Loopcut Detail Normal"
GROUP_VERSION = 1
TAG = "loopcut_detail"          # JSON on the nodes this adds: {kind, texture, scale, strength}.
MIN_SCALE, MAX_SCALE = 1.0, 200.0
FLAT = (0.5, 0.5, 1.0, 1.0)
EXPORT_SIZE = 2048              # The combined map's size when the material had no normal map of its own.

SCHEMAS = [{"type": "function", "function": {  # Sent with every request: every word here is paid for on every step.
    "name": "add_detail_normal",
    "title": "Adding detail",
    "description": "Tiling CC0 micro-normal over a material's normal map; engine exports bake it in.",
    "parameters": {"type": "object", "required": ["object", "kind"], "properties": {
        "object": {"type": "string"},
        "kind": {"type": "string", "enum": list(KINDS)},
        "material": {"type": "string"},
        "scale": {"type": "number", "description": "Repeats per UV unit"},
        "strength": {"type": "number", "description": "0-1"}}}}}]


# ------------------------------------------------------------------ pure

def default_scale(metres_per_uv: float, tile_metres: float) -> float:
    """Repeats per UV unit so one repeat covers the texture's real size on the mesh, clamped."""
    if not (metres_per_uv > 0 and tile_metres > 0) or not math.isfinite(metres_per_uv):
        return 10.0
    return round(min(MAX_SCALE, max(MIN_SCALE, metres_per_uv / tile_metres)), 2)


def rnm(base, detail, strength: float):
    """Reoriented normal mapping of encoded tangent-space normals ([0, 1] RGB, (..., 3)): the node group's math."""
    import numpy as np
    n1 = np.asarray(base, dtype=np.float64)[..., :3] * 2 - 1
    n2 = np.asarray(detail, dtype=np.float64)[..., :3] * 2 - 1
    n1 /= np.maximum(np.linalg.norm(n1, axis=-1, keepdims=True), 1e-9)
    n2 = n2 * np.array([strength, strength, 1.0])
    n2 /= np.maximum(np.linalg.norm(n2, axis=-1, keepdims=True), 1e-9)
    t = n1 + np.array([0.0, 0.0, 1.0])
    u = n2 * np.array([-1.0, -1.0, 1.0])
    r = t * ((t * u).sum(axis=-1, keepdims=True) / np.maximum(t[..., 2:3], 1e-9)) - u
    r /= np.maximum(np.linalg.norm(r, axis=-1, keepdims=True), 1e-9)
    return r * 0.5 + 0.5


def tiled(detail, width: int, height: int, scale: float):
    """The detail (H, W, C, rows from v = 0) sampled at the texel centres of a width x height map, repeated scale times
    per UV unit (nearest texel of a copy already sized to one repeat)."""
    import numpy as np
    rows, cols = detail.shape[:2]
    u = (np.arange(width) + 0.5) / width * scale
    v = (np.arange(height) + 0.5) / height * scale
    x = np.minimum((np.mod(u, 1.0) * cols).astype(np.int64), cols - 1)
    y = np.minimum((np.mod(v, 1.0) * rows).astype(np.int64), rows - 1)
    return detail[y][:, x]


# ------------------------------------------------------------------ Blender

def _fetch(texture: str) -> Path:
    """Its nor_gl map into polyhaven's cache (checked against the published md5); the cached file when there."""
    from . import polyhaven
    from .tools import ToolError
    folder = polyhaven.cache_root() / texture / RESOLUTION
    cached = sorted(folder.glob(f"{texture}_nor_gl_{RESOLUTION}.*")) if folder.is_dir() else []
    cached = [p for p in cached if p.suffix.lower() in (".jpg", ".png")]
    if cached:
        return cached[0]
    files = polyhaven.get_json(f"{polyhaven.API}/files/{urllib.parse.quote(texture, safe='')}")
    sizes = (files.get("nor_gl") or {}).get(RESOLUTION) or {}
    record = sizes.get("jpg") or sizes.get("png")
    if not record:
        raise ToolError(f"Poly Haven's {texture} has no {RESOLUTION} OpenGL normal map now; pick another kind.")
    return polyhaven._file(record, folder / Path(urllib.parse.urlparse(record["url"]).path).name)


def _image(texture: str):
    import bpy
    path = _fetch(texture)
    name = f"{texture}_nor_gl_{RESOLUTION}"
    image = bpy.data.images.get(name)
    if image is None:
        image = bpy.data.images.load(str(path), check_existing=False)
        image.name = name
        image.colorspace_settings.name = "Non-Color"
        image.pack()  # The .blend works without Loopcut's cache.
        image["polyhaven"] = texture
        image["polyhaven_authors"] = "Poly Haven (CC0)"
    return image


def _group():
    """The RNM node group (made once, rebuilt when its version is older)."""
    import bpy
    group = bpy.data.node_groups.get(GROUP)
    if group is not None and group.get("loopcut_version") == GROUP_VERSION:
        return group
    if group is None:
        group = bpy.data.node_groups.new(GROUP, "ShaderNodeTree")
    group.nodes.clear()
    group.interface.clear()
    base = group.interface.new_socket("Base", in_out="INPUT", socket_type="NodeSocketColor")
    base.default_value = FLAT
    group.interface.new_socket("Detail", in_out="INPUT", socket_type="NodeSocketColor").default_value = FLAT
    strength = group.interface.new_socket("Strength", in_out="INPUT", socket_type="NodeSocketFloat")
    strength.default_value, strength.min_value, strength.max_value = 0.35, 0.0, 1.0
    group.interface.new_socket(name="Color", socket_type="NodeSocketColor", in_out="OUTPUT")
    nodes, links = group.nodes, group.links
    inputs, outputs = nodes.new("NodeGroupInput"), nodes.new("NodeGroupOutput")

    def vmath(operation, *sources, values=()):
        node = nodes.new("ShaderNodeVectorMath")
        node.operation = operation
        for index, source in enumerate(sources):
            links.new(source, node.inputs[index])
        for index, value in values:
            node.inputs[index].default_value = value
        return node.outputs["Vector"] if operation != "DOT_PRODUCT" else node.outputs["Value"]

    def decode(socket):
        return vmath("NORMALIZE", vmath("MULTIPLY_ADD", socket, values=((1, (2, 2, 2)), (2, (-1, -1, -1)))))
    n1 = decode(inputs.outputs["Base"])
    raw = vmath("MULTIPLY_ADD", inputs.outputs["Detail"], values=((1, (2, 2, 2)), (2, (-1, -1, -1))))
    scale = nodes.new("ShaderNodeCombineXYZ")
    links.new(inputs.outputs["Strength"], scale.inputs["X"])
    links.new(inputs.outputs["Strength"], scale.inputs["Y"])
    scale.inputs["Z"].default_value = 1.0
    n2 = vmath("NORMALIZE", vmath("MULTIPLY", raw, scale.outputs["Vector"]))
    t = vmath("ADD", n1, values=((1, (0, 0, 1)),))
    u = vmath("MULTIPLY", n2, values=((1, (-1, -1, 1)),))
    dot = vmath("DOT_PRODUCT", t, u)
    split = nodes.new("ShaderNodeSeparateXYZ")
    links.new(t, split.inputs["Vector"])
    ratio = nodes.new("ShaderNodeMath")
    ratio.operation = "DIVIDE"
    links.new(dot, ratio.inputs[0])
    links.new(split.outputs["Z"], ratio.inputs[1])
    scaled = nodes.new("ShaderNodeVectorMath")
    scaled.operation = "SCALE"
    links.new(t, scaled.inputs[0])
    links.new(ratio.outputs["Value"], scaled.inputs["Scale"])
    r = vmath("NORMALIZE", vmath("SUBTRACT", scaled.outputs["Vector"], u))
    links.new(vmath("MULTIPLY_ADD", r, values=((1, (0.5, 0.5, 0.5)), (2, (0.5, 0.5, 0.5)))), outputs.inputs["Color"])
    for index, node in enumerate(nodes):
        node.location = (index * 180 - 900, 0)
    group["loopcut_version"] = GROUP_VERSION
    return group


def _metres_per_uv(ob, layer_name: str, material_index: int | None) -> float:
    """sqrt(world area / UV area) over its faces (with this material), in metres per UV unit."""
    import numpy as np
    mesh = ob.data
    layer = mesh.uv_layers.get(layer_name) if layer_name else None
    if layer is None or not len(mesh.polygons):
        return 0.0
    mesh.calc_loop_triangles()
    count = len(mesh.loop_triangles)
    loops = np.empty(count * 3, dtype=np.int64)
    verts = np.empty(count * 3, dtype=np.int64)
    faces = np.empty(count, dtype=np.int64)
    mesh.loop_triangles.foreach_get("loops", loops)
    mesh.loop_triangles.foreach_get("vertices", verts)
    mesh.loop_triangles.foreach_get("polygon_index", faces)
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", co)
    matrix = np.array(ob.matrix_world, dtype=np.float64)
    co = co.reshape(-1, 3) @ matrix[:3, :3].T
    uv = np.empty(len(mesh.loops) * 2, dtype=np.float64)
    layer.uv.foreach_get("vector", uv)
    keep = np.ones(count, dtype=bool)
    if material_index is not None:
        index = np.empty(len(mesh.polygons), dtype=np.int64)
        mesh.polygons.foreach_get("material_index", index)
        keep = index[faces] == material_index
    p = co[verts].reshape(count, 3, 3)[keep]
    q = uv.reshape(-1, 2)[loops].reshape(count, 3, 2)[keep]
    world = 0.5 * np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1).sum()
    flat = 0.5 * np.abs((q[:, 1, 0] - q[:, 0, 0]) * (q[:, 2, 1] - q[:, 0, 1])
                        - (q[:, 1, 1] - q[:, 0, 1]) * (q[:, 2, 0] - q[:, 0, 0])).sum()
    return float(math.sqrt(world / flat)) if flat > 1e-12 else 0.0


def _ours(material):
    """(group node, its Normal Map node) this added to the material, or (None, None)."""
    tree = material.node_tree
    group = next((n for n in tree.nodes if n.get(TAG) and n.bl_idname == "ShaderNodeGroup"), None) if tree else None
    if group is None:
        return None, None
    target = next((link.to_node for link in group.outputs["Color"].links if link.to_node.type == "NORMAL_MAP"), None)
    return group, target


def _layer_name(mesh) -> str:
    layer = next((layer for layer in mesh.uv_layers if layer.active_render), None) or mesh.uv_layers.active
    return layer.name if layer is not None else ""


def apply(material, image, kind: str, texture: str, scale: float, strength: float, uv_map: str) -> str:
    """The detail on one material (see the module docstring); what it did, in words."""
    from . import rebake
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    tag = json.dumps({"kind": kind, "texture": texture, "scale": scale, "strength": strength}, sort_keys=True)
    group, normal_map = _ours(material)
    if group is not None:  # Again: update it in place.
        for node in nodes:
            if node.get(TAG):
                node[TAG] = tag
                if node.bl_idname == "ShaderNodeTexImage":
                    node.image = image
                if node.bl_idname == "ShaderNodeMapping":
                    node.inputs["Scale"].default_value = (scale, scale, 1.0)
        group.inputs["Strength"].default_value = strength
        return f"{material.name}: its {kind} detail updated ({texture}, {scale:g} repeats per UV unit, strength {strength:g})"
    bsdf = rebake._bsdf(material)
    if bsdf is None:
        return f"{material.name}: left as it is (no Principled BSDF to put a normal on)"
    socket = bsdf.inputs["Normal"]
    source = socket.links[0].from_node if socket.is_linked else None
    if source is not None and source.type == "NORMAL_MAP" and source.space == "TANGENT":
        normal_map = source
    elif source is not None and source.type == "BUMP" and not source.inputs["Normal"].is_linked:
        normal_map = nodes.new("ShaderNodeNormalMap")  # Under the bump: the bump keeps working on top.
        links.new(normal_map.outputs["Normal"], source.inputs["Normal"])
    elif source is None:
        normal_map = nodes.new("ShaderNodeNormalMap")
        links.new(normal_map.outputs["Normal"], socket)
    else:
        return (f"{material.name}: left as it is (its normal comes from a {source.name} node; give it a tangent-space "
                f"Normal Map node first)")
    normal_map.uv_map = normal_map.uv_map or uv_map
    base = normal_map.inputs["Color"].links[0].from_socket if normal_map.inputs["Color"].is_linked else None
    x, y = normal_map.location
    group = nodes.new("ShaderNodeGroup")
    group.node_tree = _group()
    group.label, group.location = "Detail normal", (x - 220, y)
    group.inputs["Strength"].default_value = strength
    texture_node = nodes.new("ShaderNodeTexImage")
    texture_node.image, texture_node.label, texture_node.location = image, f"Detail {kind}", (x - 520, y - 260)
    texture_node.extension = "REPEAT"
    mapping = nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (scale, scale, 1.0)
    mapping.location = (x - 720, y - 260)
    coordinates = nodes.new("ShaderNodeUVMap")
    coordinates.uv_map, coordinates.location = normal_map.uv_map or uv_map, (x - 900, y - 260)
    links.new(coordinates.outputs["UV"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], texture_node.inputs["Vector"])
    links.new(texture_node.outputs["Color"], group.inputs["Detail"])
    if base is not None:
        links.new(base, group.inputs["Base"])
    links.new(group.outputs["Color"], normal_map.inputs["Color"])
    for node in (group, texture_node, mapping, coordinates):
        node[TAG] = tag
    over = "over its normal map" if base is not None else "(it had no normal map: a flat one under it)"
    return f"{material.name}: {kind} detail {over} ({texture}, {scale:g} repeats per UV unit, strength {strength:g})"


def add_detail_normal(object: str, kind: str, material: str | None = None, scale: float | None = None,  # noqa: A002
                      strength: float | None = None):
    """The tool (see SCHEMAS and the module docstring). Main thread; downloads once, then from the cache."""
    import bpy
    from .tools import InputMistake, ToolError, ToolResult
    if kind not in KINDS:
        raise InputMistake(f"kind: one of {', '.join(KINDS)}")
    for name, value, low, high in (("scale", scale, MIN_SCALE, MAX_SCALE), ("strength", strength, 0.0, 1.0)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or not low <= value <= high):
            raise InputMistake(f"{name}: a number from {low:g} to {high:g}")
    ob = bpy.data.objects.get(str(object))
    if ob is None or ob.type != "MESH":
        raise ToolError(f"No mesh object named {object!r}.")
    slots = [(index, slot.material) for index, slot in enumerate(ob.material_slots) if slot.material is not None]
    if material:
        slots = [(index, m) for index, m in slots if m.name == material]
        if not slots:
            raise ToolError(f"{ob.name} has no material named {material!r}.")
    if not slots:
        raise ToolError(f"{ob.name} has no material; give it one first.")
    uv_map = _layer_name(ob.data)
    if not uv_map:
        raise ToolError(f"{ob.name} has no UV map for a tiling detail; unwrap it first (uv_unwrap).")
    texture, default_strength = KINDS[kind]
    image = _image(texture)
    said = []
    for index, mat in dict((m.name, (i, m)) for i, m in slots).values():
        if not mat.use_nodes:
            mat.use_nodes = True
        repeat = float(scale) if scale is not None else default_scale(
            _metres_per_uv(ob, uv_map, index if len(ob.material_slots) > 1 else None), TILE_METRES[texture])
        said.append(apply(mat, image, kind, texture, repeat, float(default_strength if strength is None else strength),
                          uv_map))
    return ToolResult("\n".join(said + [f"Detail: Poly Haven {texture}, CC0; export_for_engine bakes it into the "
                                        f"exported normal map."]))


def describe(name: str, arguments: dict):
    if name != "add_detail_normal":
        return None
    return f"Add {arguments.get('kind', '')} detail to {arguments.get('object', '')}", ""


DISPATCH = {"add_detail_normal": add_detail_normal}


# ------------------------------------------------------------------ export (engine_export.py)

def _pixels(image):
    import numpy as np
    width, height = image.size
    data = np.empty(width * height * image.channels, dtype=np.float32)
    image.pixels.foreach_get(data)
    data = data.reshape(height, width, image.channels)
    if image.channels < 3:
        data = np.repeat(data[..., :1], 3, axis=2)
    return data[..., :3]


def combined(group, size=None):
    """The material's combined normal (base RNM detail) at the base map's size, as an (H, W, 3) array in [0, 1]."""
    import bpy
    info = json.loads(group[TAG])
    base_link = group.inputs["Base"].links[0] if group.inputs["Base"].is_linked else None
    base_node = base_link.from_node if base_link is not None else None
    base_image = base_node.image if base_node is not None and base_node.type == "TEX_IMAGE" else None
    width, height = tuple(base_image.size) if base_image is not None and base_image.size[0] else (size or (EXPORT_SIZE,) * 2)
    import numpy as np
    base = _pixels(base_image) if base_image is not None and base_image.size[0] else np.broadcast_to(
        np.array(FLAT[:3], dtype=np.float32), (height, width, 3))
    detail_node = group.inputs["Detail"].links[0].from_node if group.inputs["Detail"].is_linked else None
    detail_image = detail_node.image if detail_node is not None and detail_node.type == "TEX_IMAGE" else None
    if detail_image is None or not detail_image.size[0]:
        return np.array(base, dtype=np.float32)
    scale = float(info.get("scale") or 1.0)
    tile = (max(4, round(width / scale)), max(4, round(height / scale)))
    work = detail_image.copy()
    try:
        if tuple(work.size) != tile and tile[0] < work.size[0]:  # Shrunk to one repeat's texels, filtered.
            work.scale(*tile)
        detail = _pixels(work)
    finally:
        bpy.data.images.remove(work)
    strength = float(group.inputs["Strength"].default_value)
    return rnm(base, tiled(detail, width, height, scale), strength).astype(np.float32)


def bake_for_export(stage, meshes) -> list[str]:
    """For each exported material with a detail: its combined normal as a temporary image, wired in place of the node
    group for the export (stage.undo puts the group back). What it did, in words."""
    import bpy
    import numpy as np
    done, seen = [], set()
    for ob in meshes:
        for slot in getattr(ob, "material_slots", ()):
            material = slot.material
            if material is None or material.name in seen or material.node_tree is None:
                continue
            seen.add(material.name)
            group, normal_map = _ours(material)
            if group is None or normal_map is None:
                continue
            pixels = combined(group)
            height, width = pixels.shape[:2]
            image = bpy.data.images.new(f"{material.name}_normal_detail", width, height, alpha=False)
            image.colorspace_settings.name = "Non-Color"
            image.pixels.foreach_set(np.concatenate([pixels, np.ones((height, width, 1), np.float32)], axis=2).ravel())
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in material.name)[:60] or "material"
            path = stage.folder / f"{safe}_normal_detail.png"
            image.filepath_raw, image.file_format = str(path), "PNG"
            image.save()
            bpy.data.images.remove(image)
            image = bpy.data.images.load(str(path), check_existing=False)
            image.colorspace_settings.name = "Non-Color"
            image.name = f"{material.name}_normal_detail"
            tree = material.node_tree
            node = tree.nodes.new("ShaderNodeTexImage")
            node.image, node.location = image, (normal_map.location[0] - 300, normal_map.location[1] + 200)
            tree.links.new(node.outputs["Color"], normal_map.inputs["Color"])

            def put_back(tree=tree, node=node, group=group, normal_map=normal_map, image=image):
                tree.nodes.remove(node)
                tree.links.new(group.outputs["Color"], normal_map.inputs["Color"])
                if image.users == 0:
                    bpy.data.images.remove(image)
            stage.undo.append(put_back)
            info = json.loads(group[TAG])
            done.append(f"{material.name}: its {info.get('kind')} detail baked into the exported normal map "
                        f"({width}x{height}); yours is unchanged.")
    return done
