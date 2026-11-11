"""Re-bake in place: the add-on side of the rebake cloud tool (game-asset-quality §2).

A baked normal map describes one particular surface; once that surface is edited the map stops agreeing with it.
For a retopology (or re-bake) result changed since its bake, the cloud tool bakes again from the stored high-poly
onto the mesh in its present state. The model names only the object (or nothing: the selection); Loopcut builds
the request:
- model: the object's mesh, before modifiers and without materials or shape keys, with only its render UV map,
  in the space it was baked in (shading.stamp records it at import), as GLB, or binary FBX when it has quads or
  ngons so they stay quads. It is marked (SENT) with the file's SHA-256 and a fingerprint, so the result finds it.
- high: the source high-poly by hash (blobs.Stored), never uploaded: the one recorded at import, else read from the
  source job's files (contract 0.11) on the agent thread (cloud_jobs.resolve_stored) and recorded then. Without
  either, it refuses in plain words before anything is quoted.
The result is applied in place (apply): with its own UVs kept, the new images go on its material; with them
unwrapped again, the new UVs are matched onto its faces first (or, when its faces cannot be matched and nothing
else rides on the mesh, the returned mesh replaces it). Then it is shaded the way the bake target was (exact
duplicate vertices welded, custom normals, sharp edges and flat faces cleared) and its bake record is taken
again, so check_shading reads it clean. A mesh deleted or changed while baking gets the result as a new object.

harness/rebake_apply_check.py runs the runner's own rebake_job.py on SPLAP and applies its result through here.
"""

import json
import math
import re
from pathlib import Path

TOOL = "rebake"
SENT = "loopcut_rebake_sent"   # JSON: the file a re-bake sent from this object and its fingerprint then.
MAX_TRIANGLES = 200_000        # The runner's low-poly limit (rebake descriptor, workers/runner).
MAX_BYTES = 50 * 1024 * 1024   # Its file_inputs max_mb for model.
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_prepared = {}                 # Exported file path -> the object it came from (prepared_name).

NO_SOURCE = ("{name} has no source to re-bake from. Re-baking needs a mesh made by a cloud retopology or re-bake, "
             "which records the high-poly behind it. To move maps from another object in this scene, use "
             "transfer_textures.")
SOURCE_JOB_GONE = "Its source job {job} was not found for this account, so there is no high-poly to re-bake from."
SOURCE_NOT_LISTED = ("Its source job {job} does not list its high-poly (the job took none, or the service is older), "
                     "so it can't be re-baked.")
SOURCE_GONE = "Its source high-poly is no longer stored, so it can't be re-baked. Nothing was charged."
DESCRIPTION = ("Re-bakes a mesh's texture set (base colour, normal, roughness and metallic) against the high-poly it "
               "was first made from, onto its current shape, in place: for a retopology or re-bake result changed "
               "since its bake (subdivided, edited, decimated, reshaded), or one check_shading says no longer "
               "matches its maps. Loopcut sends the mesh (before modifiers) and finds its source. Its UVs stay "
               "unless they can't hold a bake.")
MODEL_TEXT = "Mesh object; default the selected one"

GLB_OPTIONS = {"export_format": "GLB", "use_selection": True, "export_apply": False, "export_materials": "NONE",
               "export_animations": False, "export_skins": False, "export_morph": False, "export_texcoords": True,
               "export_normals": True, "export_tangents": False, "export_yup": True, "export_extras": False,
               "export_cameras": False, "export_lights": False}
FBX_OPTIONS = {"use_selection": True, "object_types": {"MESH"}, "use_mesh_modifiers": False, "mesh_smooth_type": "FACE",
               "use_tspace": False, "use_custom_props": False, "add_leaf_bones": False, "bake_anim": False,
               "path_mode": "AUTO", "embed_textures": False, "colors_type": "NONE", "use_mesh_edges": False}
ROLES = (("base_color", "Base Color"), ("normal", "Normal"), ("roughness", "Roughness"), ("metallic", "Metallic"))
_SEPARATES = {"SEPARATE_COLOR", "SEPRGB", "SEPARATE_RGB"}
_CHANNEL = {"R": "Red", "G": "Green", "B": "Blue"}


# ------------------------------------------------------------------ the tool's surface

def is_rebake(descriptor) -> bool:
    return isinstance(descriptor, dict) and descriptor.get("name") == TOOL and descriptor.get("apply") == "import_model"


def tool_name() -> str:
    from .cloud import PREFIX
    return PREFIX + TOOL


def offered() -> bool:
    """Whether the account's catalog offers the re-bake tool now (check_shading names it then)."""
    from . import cloud
    runtime = cloud._runtime
    return bool(runtime) and any(d.get("name") == TOOL for d in runtime.descriptors())


def client_schema(description: str, properties: dict, required: list) -> str:
    """The tool as this client offers it: the model names an object (or none); Loopcut fills both files. The
    service's own cost and time sentences are kept."""
    properties.pop("high", None)
    properties["model"] = {"type": "string", "description": MODEL_TEXT}
    for name in ("high", "model"):
        if name in required:
            required.remove(name)
    said = [s for s in re.split(r"(?<=\.)\s+", description or "") if "credit" in s or "minute" in s]
    return " ".join([DESCRIPTION, *said])


def prepared_name(files) -> str | None:
    return _prepared.get(str((files or {}).get("model")))


# ------------------------------------------------------------------ request (main thread)

def _record(ob) -> dict:
    from . import shading
    return shading.record(ob) or {}


def _pick(name):
    """The mesh to re-bake: the one named (a mesh, or the one recorded mesh under an empty), else the selection's."""
    import bpy
    from .tools import ToolError
    if name:
        ob = bpy.data.objects.get(str(name))
        if ob is None:
            raise ToolError(f"No object named {name!r}.")
        if ob.type == "MESH":
            return ob
        pool = [c for c in ob.children_recursive if c.type == "MESH"]
    else:
        layer = bpy.context.view_layer
        active = layer.objects.active
        chosen = ([active] if active is not None and active.select_get() else []) + \
            [ob for ob in layer.objects if ob.select_get() and ob != active]
        pool = []
        for ob in chosen:
            pool += [ob] if ob.type == "MESH" else [c for c in ob.children_recursive if c.type == "MESH"]
    pool = list(dict.fromkeys(pool))
    recorded = [ob for ob in pool if _record(ob)]
    if len(recorded) == 1 or (recorded and not name and recorded[0] == bpy.context.view_layer.objects.active):
        return recorded[0]
    if len(recorded) > 1:
        raise ToolError("Several meshes could be re-baked; name one: " + ", ".join(ob.name for ob in recorded[:6]))
    if pool:
        raise ToolError(NO_SOURCE.format(name=pool[0].name))
    raise ToolError("Select the mesh to re-bake, or name it." if not name else f"{name} holds no mesh to re-bake.")


def _space(record):
    """The 4x4 its mesh was baked in (shading.stamp's "space"), or None for the identity."""
    from mathutils import Matrix
    values = (record or {}).get("space")
    if not isinstance(values, list) or len(values) != 16 or not all(
            isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        return None
    matrix = Matrix([values[0:4], values[4:8], values[8:12], values[12:16]])
    identity = Matrix.Identity(4)
    if all(abs(matrix[i][j] - identity[i][j]) < 1e-7 for i in range(4) for j in range(4)):
        return None
    return matrix


def _layer(mesh):
    """Its render UV map (what an Image Texture without a UV Map node reads), else the active one."""
    return next((layer for layer in mesh.uv_layers if layer.active_render), None) or mesh.uv_layers.active


def fingerprint(mesh) -> dict:
    """Shape and render UVs (shading.digests), to tell whether it changed while it was baking."""
    from . import shading
    data = shading.arrays(mesh)
    found = shading.digests(data["positions"], data["loop_total"], data["corner_verts"], data["uvs"])
    return {k: found[k] for k in ("verts", "faces", "shape", "uv")}


def export(ob, directory: Path, record: dict) -> tuple[Path, dict]:
    """Its mesh as the service's low-poly: before modifiers, no materials or shape keys, one UV map, in its bake
    space; FBX when it has quads or ngons (FBX keeps them), else GLB."""
    import bpy
    import numpy as np
    from . import engine_export
    from .tools import ToolError
    mesh = ob.data
    totals = np.empty(len(mesh.polygons), dtype=np.int64)
    mesh.polygons.foreach_get("loop_total", totals)
    if not len(totals):
        raise ToolError(f"{ob.name} has no faces to bake onto.")
    triangles = int((totals - 2).sum())
    if triangles > MAX_TRIANGLES:
        raise ToolError(f"{ob.name} has {triangles:,} triangles; a re-bake takes at most {MAX_TRIANGLES:,}. "
                        f"Bake onto a lighter version of it (decimate a copy first).")
    fmt = "fbx" if int(totals.max()) > 3 else "glb"
    layer = _layer(mesh)
    keep = layer.name if layer is not None else ""
    copy = mesh.copy()
    temp = bpy.data.objects.new("loopcut_rebake_lowpoly", copy)
    path = Path(directory) / f"lowpoly.{fmt}"
    try:
        bpy.context.scene.collection.objects.link(temp)
        if copy.shape_keys is not None:
            temp.shape_key_clear()
        copy.materials.clear()
        for other in [uv.name for uv in copy.uv_layers if uv.name != keep]:
            copy.uv_layers.remove(copy.uv_layers[other])
        if keep:
            copy.uv_layers.active = copy.uv_layers[keep]
            copy.uv_layers[keep].active_render = True
        space = _space(record)
        if space is not None:
            copy.transform(space)
            if space.determinant() < 0:
                copy.flip_normals()
        copy.update()
        try:
            engine_export._export(fmt, path, [temp], FBX_OPTIONS if fmt == "fbx" else GLB_OPTIONS)
        except (RuntimeError, TypeError) as ex:
            raise ToolError(f"{ob.name} could not be exported to send: {ex}") from ex
    finally:
        bpy.data.objects.remove(temp)
        if copy.users == 0:
            bpy.data.meshes.remove(copy)
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ToolError(f"{ob.name} exports to {size / 1048576:.0f} MB; a re-bake takes at most "
                        f"{MAX_BYTES // 1048576} MB. Bake onto a lighter version of it.")
    return path, {"format": fmt, "triangles": triangles, "uv_layer": keep}


def _found(name: str):
    """Records a source high-poly read from the source job on the object (main thread)."""
    def found(sha256: str) -> None:
        import bpy
        from . import shading
        ob = bpy.data.objects.get(name)
        if ob is None or not _HASH.fullmatch(sha256 or ""):
            return
        ob[shading.HIGH_SHA] = sha256
        record = shading.record(ob)
        if record is not None:
            record["high_sha256"] = sha256
            ob[shading.RECORD] = json.dumps(record, sort_keys=True)
    return found


def prepare(arguments: dict, directory: Path) -> dict:
    """cloud_jobs.resolve_files for rebake: {"high": blobs.Stored, "model": the exported low-poly}."""
    from . import blobs, shading
    from .tools import ToolError
    ob = _pick(arguments.get("model"))
    if ob.mode == "EDIT":
        raise ToolError(f"{ob.name} is in Edit Mode; leave it (Tab) so its edits are what gets baked onto.")
    record = _record(ob)
    high = ob.get(shading.HIGH_SHA) or record.get("high_sha256") or ""
    high = high if isinstance(high, str) and _HASH.fullmatch(high) else ""
    job = record.get("job") if record.get("tool") in shading.BAKED_FROM else ""
    if not high and not job:
        raise ToolError(NO_SOURCE.format(name=ob.name))
    path, facts = export(ob, Path(directory), record)
    sent = blobs.sha256(path)
    ob[SENT] = json.dumps({"model": sent, **fingerprint(ob.data), **facts}, sort_keys=True)
    _prepared[str(path)] = ob.name
    while len(_prepared) > 32:
        del _prepared[next(iter(_prepared))]
    return {"high": blobs.Stored(high, job=job if not high else "", found=_found(ob.name)), "model": path}


# ------------------------------------------------------------------ the result (main thread)

def _sent(ob) -> dict | None:
    try:
        value = json.loads(ob.get(SENT)) if isinstance(ob.get(SENT), str) else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _target(job: dict, sent: str):
    """(the object the job's model came from, unchanged since, or None, why not)."""
    import bpy
    from . import cloud_jobs
    marked = [ob for ob in bpy.data.objects if ob.type == "MESH" and sent and (_sent(ob) or {}).get("model") == sent]
    for ob in marked:
        mark = _sent(ob)
        if ob.mode == "EDIT":
            return None, f"{ob.name} is in Edit Mode"
        now = fingerprint(ob.data)
        if all(now.get(k) == mark.get(k) for k in ("verts", "faces", "shape", "uv")):
            return ob, ""
    if marked:
        return None, f"{marked[0].name} changed while it was baking, so the new maps would not fit it"
    name = cloud_jobs._kept(job, "references").get("model")
    if isinstance(name, str) and name:
        return None, f"{name} is no longer the mesh it sent"
    return None, "the mesh it was sent from is no longer in the scene"


def _as_new(job: dict, why: str) -> list[str]:
    from . import cloud_jobs
    job["imported"] = True  # result_summary: imported, not re-baked in place.
    return [f"{name}, as a new object: {why}" for name in cloud_jobs._model_import(job)]


def _import(path: Path, previous) -> list:
    """The result's objects, imported as a cloud model is (checked first, outside textures dropped)."""
    import bpy
    from . import cloud_http, cloud_jobs
    if path.suffix.lower() == ".fbx":
        cloud_jobs._check_fbx(path)
        try:
            result = cloud_jobs._import_fbx(path)
        finally:
            cloud_jobs._drop_outside_textures(path, previous)
    elif path.suffix.lower() in (".glb", ".gltf"):
        cloud_jobs._check_gltf(path)
        result = bpy.ops.import_scene.gltf(filepath=str(path), import_scene_extras=False,
                                           import_scene_as_collection=False)
    else:
        raise cloud_http.CloudError("The re-bake result is not a GLB or FBX model", "apply_failed")
    if "FINISHED" not in result:
        raise cloud_http.CloudError("Blender could not import the re-baked model", "apply_failed")
    bpy.context.view_layer.update()
    return [ob for ob in bpy.data.objects if cloud_jobs._identity(ob) not in previous]


def _remove_new(previous) -> None:
    """Remove what the import made and nothing uses now (its objects always); images the mesh took stay."""
    import bpy
    from . import cloud_jobs

    def new(collection):
        return [block for block in collection if cloud_jobs._identity(block) not in previous]
    for ob in new(bpy.data.objects):
        bpy.data.objects.remove(ob)
    for collection in new(bpy.data.collections):
        bpy.data.collections.remove(collection)
    for kind in ("meshes", "materials", "images", "node_groups"):
        for block in new(getattr(bpy.data, kind)):
            if block.users == 0:
                getattr(bpy.data, kind).remove(block)


def _corners(mesh, matrix=None):
    """(corner positions (C, 3) in matrix's space, loop_start, loop_total) in the mesh's own corner order."""
    import numpy as np
    count = len(mesh.vertices)
    co = np.empty(count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3).astype(np.float64)
    if matrix is not None:
        m = np.array(matrix, dtype=np.float64)
        co = co @ m[:3, :3].T + m[:3, 3]
    starts = np.empty(len(mesh.polygons), dtype=np.int64)
    totals = np.empty(len(mesh.polygons), dtype=np.int64)
    mesh.polygons.foreach_get("loop_start", starts)
    mesh.polygons.foreach_get("loop_total", totals)
    verts = np.empty(len(mesh.loops), dtype=np.int64)
    mesh.loops.foreach_get("vertex_index", verts)
    return co[verts], starts, totals


def _centres(points, starts, totals):
    import numpy as np
    face = np.empty(len(points), dtype=np.int64)
    offsets = np.arange(len(points)) - np.repeat(np.cumsum(totals) - totals, totals)
    face[np.repeat(starts, totals) + offsets] = np.repeat(np.arange(len(totals)), totals)
    return np.stack([np.bincount(face, weights=points[:, k], minlength=len(totals)) for k in range(3)],
                    axis=1) / totals[:, None]


def match_corners(points, starts, totals, their_points, their_starts, their_totals, their_values, tolerance):
    """Each of our corners' value (a UV) from the returned mesh's matching corner: faces matched by centre, corners
    by position (all in one space). (values (C, k), None) or (None, why not)."""
    import numpy as np
    from mathutils.kdtree import KDTree
    if len(totals) != len(their_totals):
        return None, f"{len(totals):,} faces here, {len(their_totals):,} returned"
    ours, theirs = _centres(points, starts, totals), _centres(their_points, their_starts, their_totals)
    tree = KDTree(len(theirs))
    for index, centre in enumerate(theirs):
        tree.insert(centre.tolist(), index)
    tree.balance()
    found = np.empty(len(ours), dtype=np.int64)
    gap = np.empty(len(ours))
    for index, centre in enumerate(ours):
        _, found[index], gap[index] = tree.find(centre.tolist())
    if gap.max() > tolerance or len(np.unique(found)) != len(found) or np.any(their_totals[found] != totals):
        return None, "its faces no longer line up with the returned ones"
    their_values = np.asarray(their_values)
    uvs = np.zeros((len(points), their_values.shape[1]))
    for size in np.unique(totals):
        faces = np.nonzero(totals == size)[0]
        mine = starts[faces][:, None] + np.arange(size)
        other = their_starts[found[faces]][:, None] + np.arange(size)
        distance = np.linalg.norm(points[mine][:, :, None, :] - their_points[other][:, None, :, :], axis=3)
        if distance.min(axis=2).max() > tolerance:
            return None, "its corners no longer line up with the returned ones"
        uvs[mine] = their_values[np.take_along_axis(other, distance.argmin(axis=2), axis=1)]
    return uvs, None


def _uv_array(mesh, layer):
    import numpy as np
    uvs = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    layer.uv.foreach_get("vector", uvs)
    return uvs.reshape(-1, 2).astype(np.float64)


def _take_uvs(target, space, result, name: str) -> str | None:
    """The returned UVs onto its render UV map (made when it had none); None when done, else why not."""
    import numpy as np
    their_layer = _layer(result.data)
    if their_layer is None:
        return "the returned mesh has no UVs"
    points, starts, totals = _corners(target.data, space)
    their_points, their_starts, their_totals = _corners(result.data, result.matrix_world)
    size = float(np.linalg.norm(points.max(axis=0) - points.min(axis=0))) if len(points) else 0.0
    uvs, why = match_corners(points, starts, totals, their_points, their_starts, their_totals,
                         _uv_array(result.data, their_layer), max(size, 1e-6) * 1e-4)
    if uvs is None:
        return why
    mesh = target.data
    layer = mesh.uv_layers.get(name) if name else None
    if layer is None:
        layer = _layer(mesh) or mesh.uv_layers.new(name=name or "UVMap")
        mesh.uv_layers.active = layer
        layer.active_render = True
    layer.uv.foreach_set("vector", uvs.astype(np.float32).ravel())
    mesh.update()
    return None


def _replaceable(target) -> bool:
    """Nothing but its shape rides on its mesh: no weights, shape keys, rig or other users."""
    return (not target.vertex_groups and target.data.shape_keys is None and target.data.users == 1
            and not any(m.type == "ARMATURE" for m in target.modifiers))


def _replace_mesh(target, space, result, name: str) -> None:
    """The returned mesh, back in its object's space, in place of its mesh (its materials kept)."""
    import bpy
    mesh = result.data.copy()
    mesh.transform(result.matrix_world)
    if space is not None:
        inverse = space.inverted()
        mesh.transform(inverse)
        if inverse.determinant() < 0:
            mesh.flip_normals()
    mesh.materials.clear()
    for material in target.data.materials:
        mesh.materials.append(material)
    if mesh.uv_layers and name:
        _layer(mesh).name = name
    old = target.data
    mesh.name = old.name
    target.data = mesh
    if old.users == 0:
        bpy.data.meshes.remove(old)


def _bsdf(material):
    """Its Principled BSDF: the one on the active output's Surface, else the first."""
    if material is None or not material.use_nodes or material.node_tree is None:
        return None
    nodes = material.node_tree.nodes
    output = next((n for n in nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
    surface = output.inputs.get("Surface") if output is not None else None
    if surface is not None and surface.is_linked and surface.links[0].from_node.type == "BSDF_PRINCIPLED":
        return surface.links[0].from_node
    return next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)


def _feed(socket, depth: int = 0):
    """(the Image Texture feeding socket, its chain: (node type, output) from the image out), through a Normal Map,
    a Separate Color or a few other nodes (a tint)."""
    if not socket.is_linked or socket.links[0].is_muted:
        return None, ()
    link = socket.links[0]
    node, out = link.from_node, link.from_socket.name
    if node.type == "TEX_IMAGE":
        return (node, (("TEX_IMAGE", out),)) if node.image is not None and not node.mute else (None, ())
    if depth >= 4 or node.mute:
        return None, ()
    if node.type in _SEPARATES:
        inner = [node.inputs[0]]
        out = _CHANNEL.get(out, out)
    elif node.type == "NORMAL_MAP":
        inner = [node.inputs["Color"]]
    else:
        inner = [i for i in node.inputs if i.is_linked]
    for socket_in in inner:
        image, chain = _feed(socket_in, depth + 1)
        if image is not None:
            return image, chain + (("SEPARATE" if node.type in _SEPARATES else node.type, out),)
    return None, ()


def _key(chain) -> tuple:
    """What a chain says the image is: which output, which channel, whether through a Normal Map."""
    return tuple(step for step in chain if step[0] in ("TEX_IMAGE", "SEPARATE", "NORMAL_MAP"))


def _rewire(material, bsdf, socket: str, image, chain) -> bool:
    """The returned image wired into socket the way the returned material wired it (its old link replaced)."""
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    x, y = bsdf.location
    row = [name for _, name in ROLES].index(socket)
    texture = nodes.new("ShaderNodeTexImage")
    texture.image, texture.location, texture.label = image, (x - 700, y - 280 * row), f"Re-baked {socket.lower()}"
    made = [texture]
    previous = texture.outputs[chain[0][1]]
    for kind, out in chain[1:]:
        if kind == "NORMAL_MAP":
            node = nodes.new("ShaderNodeNormalMap")
            links.new(previous, node.inputs["Color"])
        elif kind == "SEPARATE":
            node = nodes.new("ShaderNodeSeparateColor")
            links.new(previous, node.inputs[0])
        else:
            for node in made:
                nodes.remove(node)
            return False
        node.location = (x - 300, y - 280 * row)
        made.append(node)
        previous = node.outputs[out]
    for link in list(bsdf.inputs[socket].links):
        links.remove(link)
    links.new(previous, bsdf.inputs[socket])
    return True


def put_maps(target, result, unwrapped: bool) -> list[str]:
    """The returned images on its own materials: an image node that means the same (same output, channel and
    Normal Map) gets the new image; otherwise the socket is wired the returned way. With new UVs, a map the
    re-bake returned as a constant is unlinked to that constant (its old texture used the old layout)."""
    theirs = next((slot.material for slot in result.material_slots if _bsdf(slot.material) is not None), None)
    their_bsdf = _bsdf(theirs)
    if their_bsdf is None:
        return ["the re-bake returned no maps"]
    returned = {}
    for role, socket in ROLES:  # Each role's returned image (not its node) and how it was wired.
        node, chain = _feed(their_bsdf.inputs[socket])
        returned[role] = (node.image, chain) if node is not None else (None, ())
    materials = list(dict.fromkeys(slot.material for slot in target.material_slots if slot.material is not None))
    if not materials:
        target.data.materials.append(theirs)
        return [f"its new material {theirs.name} (it had none)"]
    said = []
    for material in materials:
        bsdf = _bsdf(material)
        if bsdf is None:
            said.append(f"{material.name} left as it is (no Principled BSDF)")
            continue
        plan, rewire, took, constant = {}, [], [], []
        for role, socket in ROLES:
            image, chain = returned[role]
            node, mine = _feed(bsdf.inputs[socket])
            if image is None:
                if unwrapped and node is not None:
                    for link in list(bsdf.inputs[socket].links):
                        material.node_tree.links.remove(link)
                    bsdf.inputs[socket].default_value = their_bsdf.inputs[socket].default_value
                    constant.append(role.replace("_", " "))
                continue
            if node is not None and _key(mine) == _key(chain):
                plan.setdefault(node, []).append((role, socket, image, chain))
            else:
                rewire.append((role, socket, image, chain))
        for node, rows in plan.items():
            if len({row[2] for row in rows}) == 1:
                node.image = rows[0][2]
                took += [row[0] for row in rows]
            else:
                rewire += rows
        for role, socket, image, chain in rewire:
            if _rewire(material, bsdf, socket, image, chain):
                took.append(role)
        words = [r.replace("_", " ") for _, r in sorted((n, r) for n, (r, _) in enumerate(ROLES) if r in took)]
        line = f"new {', '.join(words) or 'no'} maps on its material {material.name}"
        if constant:
            line += f" ({', '.join(constant)} now a constant, as re-baked)"
        said.append(line)
    if unwrapped:
        stale = sorted({node.image.name for material in materials if material.node_tree is not None
                        for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image is not None
                        and node.image.users and not node.image.name.startswith("rebake_")
                        and any(link.to_node is not None for out in node.outputs for link in out.links)})
        if stale:
            said.append(f"its other textures ({', '.join(stale[:3])}) still follow its old UV layout")
    return said


def apply(job: dict) -> list[str]:
    """cloud_jobs.apply_result's import_model handler for rebake: the new maps (and UVs) in place."""
    import bpy
    from . import cloud_http, cloud_jobs, shading
    output = next((o for o in job["outputs"] if o["type"] == "model_3d"), None)
    if output is None:
        raise cloud_http.CloudError("The re-bake result has no model", "apply_failed")
    files = cloud_jobs._job_files(job)
    target, why = _target(job, files.get("model", ""))
    if target is None:
        return _as_new(job, why)
    meta = output.get("meta") if isinstance(output.get("meta"), dict) else {}
    uv = meta.get("uv") if isinstance(meta.get("uv"), dict) else {}
    unwrapped = uv.get("source") == "unwrapped"
    mark, record = _sent(target) or {}, shading.record(target) or {}
    space = _space(record)
    previous = cloud_jobs.datablock_snapshot(bpy.data)
    elsewhere = ""  # Set when the result goes in as a new object after all (its faces could not be matched).
    try:
        made = _import(Path(output["path"]), previous)
        meshes = [ob for ob in made if ob.type == "MESH" and ob.data is not None and len(ob.data.polygons)]
        if len(meshes) != 1:
            raise cloud_http.CloudError(f"The re-bake result holds {len(meshes)} meshes, not one", "apply_failed")
        result = meshes[0]
        how = "its own UVs kept"
        if unwrapped:
            reason = uv.get("reason") if isinstance(uv.get("reason"), str) else ""
            problem = _take_uvs(target, space, result, mark.get("uv_layer", ""))
            if problem and _replaceable(target):
                _replace_mesh(target, space, result, mark.get("uv_layer", ""))
                how = f"its mesh replaced by the re-baked one ({problem}), shape unchanged"
            elif problem:
                elsewhere = f"{problem}, and its weights or shape keys ride on that mesh"
            else:
                how = "new UVs from the re-bake" + (f" (its own were {reason})" if reason and reason != "requested"
                                                    else "")
        maps = [] if elsewhere else put_maps(target, result, unwrapped)
    finally:
        _remove_new(previous)  # The import's objects, and whatever of it nothing uses now.
    if elsewhere:
        return _as_new(job, elsewhere)
    done = shading.smooth_mesh(target.data)
    high = files.get("high") or record.get("high_sha256") or target.get(shading.HIGH_SHA) or ""
    identity = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    shading.stamp(target, job["id"], TOOL, high if isinstance(high, str) else "", space=record.get("space") or identity)
    if SENT in target:
        del target[SENT]
    bpy.context.view_layer.update()  # The import's objects were removed through bpy.data.
    for ob in bpy.context.view_layer.objects:
        if ob is not None:
            ob.select_set(False)
    if target.name in bpy.context.view_layer.objects:
        target.select_set(True)
        bpy.context.view_layer.objects.active = target
    shade = meta.get("shading") if isinstance(meta.get("shading"), dict) else {}
    cleared = shade.get("sharp_edges_cleared")
    smoothing = ", ".join(done) + (f" (the bake cleared {cleared:,} sharp edges)" if isinstance(cleared, int)
                                   and cleared else "")
    job.setdefault("shading", []).extend(f"Shading: {line}" for line in shading.lines(shading.check([target])))
    return [f"{target.name}: {'; '.join(maps)}; {how}; {smoothing}; its bake record updated"]
