"""export_for_engine: one checked export per game engine (game-asset-quality §4).

Built on glb.py's checked export: the file is written beside its destination, read back, compared with
the scene (each object a node of the same name and triangle count) and only then renamed over it; a
failed check leaves the destination as it was. FBX is read back with Blender's own FBX parser
(io_scene_fbx.parse_fbx) and fbx_summary below.

Each engine's preset (PRESETS) sets the format, axes and scale, triangulation, tangents (MikkTSpace, the
space Blender bakes normal maps in), modifiers applied, and every action as its own named clip. Before
exporting, Loopcut runs the shading check on what it exports (shading.py; the safe fix included) and
makes temporary copies of textures over the engine's cap, of normal maps the engine reads the other way
up, and of images an FBX could not embed (unsaved or generated): the user's own images and materials
are put back as they were. lods adds decimated copies for Unity, whose importer groups *_LOD0, *_LOD1...
into an LOD Group; the other engines make their own. A triangle budget, the engine's per-mesh cap and
unapplied object scales are reported, never refused.

The pure half (presets, options, fbx_summary, the report lines) is tested in tests/test_engine_export.py;
harness/engine_export_check.py exports real scenes and imports the Godot file headless in Godot.
"""

import os
import shutil
import tempfile
import uuid
from pathlib import Path

ENGINES = ("godot", "unity", "unreal", "roblox")
MAX_LODS = 3
MAX_LINES = 12

# Engine limits, checked against each engine's documentation on 2026-10-31.
# https://create.roblox.com/docs/art/modeling/specifications: "Individual meshes can't exceed 20,000 triangles."
ROBLOX_MESH_TRIANGLES = 20_000
# https://create.roblox.com/docs/art/modeling/texture-specifications: "Roblox supports up to 4096×4096 pixel
# texture resolutions (4K)" (its UV mapping section still says 1024×1024). Accepted: .fbx, .obj, .gltf
# (https://create.roblox.com/docs/art/modeling/3d-importer); Blender settings from
# https://create.roblox.com/docs/art/modeling/export-requirements (FBX Unit Scale, embedded textures, no leaf bones).
ROBLOX_TEXTURE = 4096
# https://docs.unity.com/en-us/engine/6000.6/manual/materials-and-shaders/textures/importing-textures: textures up
# to 16384 x 16384; Unity expects 1 m to be 1 unit (FBX importer, Scale Factor).
UNITY_TEXTURE = 16384
# https://dev.epicgames.com/documentation/en-us/unreal-engine/textures-in-unreal-engine: some GPUs do not support
# textures larger than 8192. Unreal reads normal maps DirectX-style (green down): Interchange's
# bFlipNormalMapGreenChannel exists to convert others.
UNREAL_TEXTURE = 8192
# https://docs.godotengine.org/en/stable/tutorials/assets_pipeline/importing_images.html: no hard cap, but mobile and
# web "usually can't display textures larger than 4096×4096"; Godot wants OpenGL-style (Y+) normal maps. glTF is its
# recommended format (importing_3d_scenes/available_formats.html), and it makes LODs on import.
GODOT_TEXTURE_NOTE = 4096

PRESETS = {
    "godot": {"title": "Godot", "formats": ("glb",), "texture_cap": None, "texture_note": GODOT_TEXTURE_NOTE,
              "mesh_triangles": None, "flip_green": False, "lods": False,
              "axes": "Y up, metres (glTF)"},
    "unity": {"title": "Unity", "formats": ("fbx",), "texture_cap": UNITY_TEXTURE, "texture_note": None,
              "mesh_triangles": None, "flip_green": False, "lods": True,
              "axes": "Y up, -Z forward, metres, axis conversion applied",
              "fbx": {"axis_forward": "-Z", "axis_up": "Y", "apply_scale_options": "FBX_SCALE_ALL",
                      "bake_space_transform": True}},
    "unreal": {"title": "Unreal", "formats": ("fbx",), "texture_cap": UNREAL_TEXTURE, "texture_note": None,
               "mesh_triangles": None, "flip_green": True, "lods": False,
               "axes": "Y up, -Z forward in the file, units in its header (Unreal converts to Z up, cm)",
               "fbx": {"axis_forward": "-Z", "axis_up": "Y", "apply_scale_options": "FBX_SCALE_ALL",
                       "bake_space_transform": False}},
    "roblox": {"title": "Roblox", "formats": ("fbx", "gltf"), "texture_cap": ROBLOX_TEXTURE, "texture_note": None,
               "mesh_triangles": ROBLOX_MESH_TRIANGLES, "flip_green": False, "lods": False,
               "axes": "Y up, -Z forward, metres (set the importer's Scale Unit to match)",
               "fbx": {"axis_forward": "-Z", "axis_up": "Y", "apply_scale_options": "FBX_SCALE_UNITS",
                       "bake_space_transform": False}},
}
FBX_COMMON = {
    "use_selection": True, "object_types": {"ARMATURE", "EMPTY", "MESH", "OTHER"}, "use_mesh_modifiers": True,
    "use_mesh_modifiers_render": True, "mesh_smooth_type": "FACE", "use_subsurf": False, "use_mesh_edges": False,
    "use_tspace": True, "use_triangles": True, "use_custom_props": False, "add_leaf_bones": False,
    "primary_bone_axis": "Y", "secondary_bone_axis": "X", "use_armature_deform_only": True,
    "armature_nodetype": "NULL", "bake_anim_use_all_bones": True, "bake_anim_use_nla_strips": False,
    "bake_anim_use_all_actions": True, "bake_anim_force_startend_keying": True, "bake_anim_step": 1.0,
    "bake_anim_simplify_factor": 1.0, "path_mode": "COPY", "embed_textures": True, "batch_mode": "OFF",
    "global_scale": 1.0, "apply_unit_scale": True, "use_space_transform": True, "colors_type": "SRGB",
    "use_metadata": True,
}
GLTF_COMMON = {
    "use_selection": True, "export_apply": True, "export_yup": True, "export_tangents": True,
    "export_texcoords": True, "export_normals": True, "export_materials": "EXPORT", "export_image_format": "AUTO",
    "export_animations": True, "export_skins": True, "export_morph": True, "export_cameras": False,
    "export_lights": False, "export_extras": False, "export_force_sampling": True,
}
TIPS = {
    "godot": "Godot imports the .glb as a scene: its clips are in its AnimationPlayer; Godot makes LODs itself.",
    "unity": ("Unity: clips are named Object|Action; tangents can stay on Import. Tick Bake Axis Conversion in the "
              "model's import settings if a rig comes in rotated."),
    "unreal": "Unreal: leave Convert Scene on; normal maps are already DirectX-style (green flipped on copies).",
    "roblox": "Roblox 3D Importer: Scale Unit to metres; each mesh becomes a MeshPart.",
}

SCHEMAS = [{"type": "function", "function": {  # Sent with every request: every word here is paid for on every step.
    "name": "export_for_engine",
    "title": "Exporting for a game engine",
    "description": ("Checked export for a game engine: its axes, scale, triangles, tangents, named clips, texture "
                    "caps; shading checked first. Asks the user."),
    "parameters": {"type": "object", "required": ["engine"], "properties": {
        "engine": {"type": "string", "enum": list(ENGINES)},
        "objects": {"type": "array", "items": {"type": "string"}},
        "path": {"type": "string", "description": "Default //<blend>_<engine>.fbx (Godot .glb, Roblox .fbx or .gltf)"},
        "triangle_budget": {"type": "integer"},
        "lods": {"type": "integer", "description": "Unity LOD copies, 0-3"},
        "overwrite": {"type": "boolean"}}}}}]


# ------------------------------------------------------------------ pure

def preset(engine) -> dict:
    from .tools import InputMistake
    key = str(engine or "").strip().lower()
    if key not in PRESETS:
        raise InputMistake(f"engine: one of {', '.join(ENGINES)}")
    return {"engine": key, **PRESETS[key]}


def fbx_options(engine: str, *, armature: bool, animated: bool) -> dict:
    """The FBX exporter's arguments for an engine. Unity bakes the axis conversion into the mesh, except
    with a rig (Blender's bake is experimental with armatures: Unity's Bake Axis Conversion does it)."""
    chosen = {**FBX_COMMON, **PRESETS[engine]["fbx"], "bake_anim": bool(animated)}
    if armature:
        chosen["bake_space_transform"] = False
    return chosen


def gltf_options(*, fmt: str = "glb", animated_objects: int = 0, shape_keys: bool = False) -> dict:
    """The glTF exporter's arguments: every action as its own named animation (with one animated object,
    every action compatible with it, as FBX's all-actions does), deform bones only with shape keys
    (Godot's advice)."""
    chosen = {**GLTF_COMMON, "export_format": "GLB" if fmt == "glb" else "GLTF_EMBEDDED",
              "export_animation_mode": "BROADCAST" if animated_objects == 1 else "ACTIONS"}
    if shape_keys:
        chosen["export_def_bones"] = True
    return chosen


def scaled(size, cap) -> tuple | None:
    """The (width, height) an image of size gets under cap on its longer side, or None when it fits."""
    width, height = int(size[0]), int(size[1])
    if not cap or max(width, height) <= cap or not width or not height:
        return None
    factor = cap / max(width, height)
    return max(1, round(width * factor)), max(1, round(height * factor))


def _fbx_name(elem) -> str:
    raw = elem.props[1] if len(elem.props) > 1 else b""
    raw = bytes(raw) if isinstance(raw, (bytes, bytearray)) else str(raw).encode()
    return raw.split(b"\x00\x01")[0].decode("utf-8", "replace")


def _child(elem, name: bytes):
    return next((e for e in elem.elems if e.id == name), None)


def fbx_summary(root) -> dict:
    """What an FBX holds, from io_scene_fbx.parse_fbx's element tree: {"nodes": {model name: {"tris",
    "size": None, "tangents"}}, "meshes", "tris", "materials", "images" (embedded with content),
    "clips" (animation stack names), "warnings"}."""
    objects, connections = _child(root, b"Objects"), _child(root, b"Connections")
    geometry, models, clips, images, materials = {}, {}, [], 0, 0
    for elem in (objects.elems if objects is not None else ()):
        if elem.id == b"Geometry" and len(elem.props) > 2 and bytes(elem.props[2]) == b"Mesh":
            indices = _child(elem, b"PolygonVertexIndex")
            tris = run = 0
            for value in (indices.props[0] if indices is not None and indices.props else ()):
                run += 1
                if value < 0:
                    tris += max(0, run - 2)
                    run = 0
            geometry[elem.props[0]] = {"tris": tris, "size": None,
                                       "tangents": _child(elem, b"LayerElementTangent") is not None}
        elif elem.id == b"Model":
            models[elem.props[0]] = _fbx_name(elem)
        elif elem.id == b"AnimationStack":
            clips.append(_fbx_name(elem))
        elif elem.id == b"Material":
            materials += 1
        elif elem.id == b"Video":
            content = _child(elem, b"Content")
            images += bool(content is not None and content.props and len(content.props[0]))
    nodes = {}
    for link in (connections.elems if connections is not None else ()):
        if link.id == b"C" and len(link.props) >= 3 and bytes(link.props[0]) == b"OO":
            child, parent = link.props[1], link.props[2]
            if child in geometry and parent in models:
                nodes[models[parent]] = geometry[child]
    return {"nodes": nodes, "meshes": len(geometry), "tris": sum(g["tris"] for g in geometry.values()),
            "materials": materials, "images": images, "clips": clips, "warnings": []}


def gltf_extra(gltf: dict) -> dict:
    """What glb.summary leaves out and an engine needs: clip names, primitives with tangents of those with UVs."""
    animations = [a.get("name") or f"animation {i}" for i, a in enumerate(gltf.get("animations") or [])
                  if isinstance(a, dict)]
    mapped = tangents = 0
    for mesh in gltf.get("meshes") or []:
        for primitive in (mesh.get("primitives") or []) if isinstance(mesh, dict) else []:
            attributes = primitive.get("attributes") or {} if isinstance(primitive, dict) else {}
            if "TEXCOORD_0" in attributes:
                mapped += 1
                tangents += "TANGENT" in attributes
    return {"clips": animations, "mapped": mapped, "tangents": tangents}


def budget_lines(engine: str, tris: dict, budget, cap) -> list[str]:
    """The triangle budget and the engine's per-mesh cap, plain: tris is {name: triangles} of what was
    exported (LOD copies left out)."""
    out = []
    total = sum(tris.values())
    if budget:
        if total > budget:
            out.append(f"Over the triangle budget: {total:,} triangles, {total - budget:,} over {budget:,} "
                       f"({total / budget - 1:.0%}). Decimate or re-topologise the densest: "
                       + ", ".join(f"{n} {t:,}" for n, t in sorted(tris.items(), key=lambda i: -i[1])[:3]) + ".")
        else:
            out.append(f"Within the triangle budget: {total:,} of {budget:,}.")
    if cap:
        over = sorted(((n, t) for n, t in tris.items() if t > cap), key=lambda i: -i[1])
        if over:
            out.append(f"{PRESETS[engine]['title']} takes at most {cap:,} triangles per mesh: "
                       + ", ".join(f"{n} has {t:,}" for n, t in over[:4]) + ". Split or decimate those.")
    return out


def _human(size: int) -> str:
    return f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"


# ------------------------------------------------------------------ Blender (main thread)

def _destination(path, engine: str, formats) -> Path:
    import bpy
    from . import files
    from .tools import ToolError
    if not path:
        stem = Path(bpy.data.filepath).stem if bpy.data.filepath else "export"
        path = f"//{stem}_{engine}.{formats[0]}"
    target = files.check(files.resolve(str(path)))
    if not target.suffix:
        target = target.with_name(f"{target.name}.{formats[0]}")
    if target.suffix.lower().lstrip(".") not in formats:
        raise ToolError(f"{target.name}: {PRESETS[engine]['title']} takes {' or '.join('.' + f for f in formats)} here.")
    if target.is_dir():
        raise ToolError(f"{target} is a folder.")
    if not target.parent.is_dir():
        raise ToolError(f"The folder {target.parent} does not exist.")
    return target


def _chosen(objects) -> tuple[list, list[str]]:
    """glb._targets plus the armature deforming each chosen mesh (a skinned mesh without its rig exports
    unskinned): (objects, what was added)."""
    import bpy
    from . import glb
    from .tools import ToolError
    names = [objects] if isinstance(objects, str) else [str(n) for n in objects or ()]
    missing = [n for n in names if n not in bpy.data.objects]
    if missing:
        raise ToolError(f"No object named {', '.join(map(repr, missing[:5]))}; export_for_engine takes object names.")
    chosen = glb._targets(names)
    have = {ob.session_uid for ob in chosen}
    added = []
    for ob in list(chosen):
        for modifier in getattr(ob, "modifiers", ()):
            rig = getattr(modifier, "object", None) if modifier.type == "ARMATURE" else None
            if rig is not None and rig.session_uid not in have:
                have.add(rig.session_uid)
                chosen.append(rig)
                added.append(f"{rig.name} (it deforms {ob.name})")
    return chosen, added


def _images(objects) -> dict:
    """{image: [nodes using it]} over the materials of these objects; normal-map images flagged."""
    seen, found = set(), {}
    for ob in objects:
        for slot in getattr(ob, "material_slots", ()):
            material = slot.material
            if material is None or material.node_tree is None or material.name in seen:
                continue
            seen.add(material.name)
            for node in material.node_tree.nodes:
                if node.bl_idname == "ShaderNodeTexImage" and node.image is not None:
                    found.setdefault(node.image, []).append(node)
    return found


def _normal_images(objects) -> set:
    from . import shading
    import bpy
    names = {name for ob in objects for name in shading.normal_maps(ob)}
    return {bpy.data.images[n] for n in names if n in bpy.data.images}


def _unembeddable(image) -> bool:
    """An image an FBX could not embed as it is: generated, unsaved edits, or no file and not packed."""
    import bpy
    if image.packed_file is not None:
        return bool(image.is_dirty)
    if image.source != "FILE" or image.is_dirty:
        return True
    return not os.path.isfile(bpy.path.abspath(image.filepath_raw))


class _Stage:
    """Temporary changes for one export, all undone by restore(): texture copies swapped into nodes,
    LOD copies, renamed objects, the temporary folder."""

    def __init__(self):
        self.folder = Path(tempfile.mkdtemp(prefix="loopcut_engine_"))
        self.swapped, self.copies, self.lods, self.renamed, self.modifiers = [], [], [], [], []
        self.undo = []  # Callables run first by restore(), last added first (motion_bake.Baked.undo).

    def texture(self, image, nodes, size, flip: bool, engine: str):
        """A PNG copy of image (scaled, green flipped) in the temporary folder, loaded as its own image
        (a file both exporters embed) and swapped into nodes."""
        import bpy
        import numpy as np
        work = image.copy()
        try:
            if image.is_dirty or image.source != "FILE":
                # A copy regenerates or reloads, dropping unsaved pixels (paint, a generated image's edits).
                width, height = image.size
                if tuple(work.size) != (width, height):
                    work.scale(width, height)
                pixels = np.empty(width * height * image.channels, dtype=np.float32)
                image.pixels.foreach_get(pixels)
                work.pixels.foreach_set(pixels)
            if size:
                work.scale(*size)
            if flip:
                pixels = np.empty(work.size[0] * work.size[1] * work.channels, dtype=np.float32)
                work.pixels.foreach_get(pixels)
                if work.channels >= 3:
                    pixels[1::work.channels] = 1.0 - pixels[1::work.channels]
                work.pixels.foreach_set(pixels)
            safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in Path(image.name).stem)[:60] or "image"
            path = self.folder / f"{safe}.{engine}.png"
            work.file_format = "PNG"
            work.save(filepath=str(path), save_copy=True)
        finally:
            bpy.data.images.remove(work)
        copy = bpy.data.images.load(str(path), check_existing=False)
        copy.name = f"{image.name}.{engine}"
        copy.colorspace_settings.name = image.colorspace_settings.name
        copy.alpha_mode = image.alpha_mode
        self.copies.append(copy)
        for node in nodes:
            self.swapped.append((node, image))
            node.image = copy
        return copy

    def triangulate(self, ob):
        """Ngons triangulated for this export (glTF's tangents need triangles or quads)."""
        modifier = ob.modifiers.new("Loopcut ngons", "TRIANGULATE")
        modifier.min_vertices = 5
        self.modifiers.append((ob, modifier.name))

    def lod(self, ob, base: str, level: int, ratio: float):
        copy = ob.copy()
        copy.data = ob.data.copy()
        for collection in ob.users_collection:
            collection.objects.link(copy)
        copy.name = f"{base}_LOD{level}"
        modifier = copy.modifiers.new("Loopcut LOD", "DECIMATE")
        modifier.ratio = ratio
        copy.modifiers.move(len(copy.modifiers) - 1, 0)
        self.lods.append(copy)
        return copy

    def rename(self, ob, name: str):
        self.renamed.append((ob, ob.name))
        ob.name = name

    def restore(self):
        import bpy
        while self.undo:
            self.undo.pop()()
        for node, image in reversed(self.swapped):
            try:
                node.image = image
            except ReferenceError:
                pass
        for copy in self.lods:
            data = copy.data
            bpy.data.objects.remove(copy)
            if data is not None and data.users == 0:
                bpy.data.meshes.remove(data)
        for ob, name in reversed(self.renamed):
            ob.name = name
        for ob, name in self.modifiers:
            modifier = ob.modifiers.get(name)
            if modifier is not None:
                ob.modifiers.remove(modifier)
        for copy in self.copies:
            bpy.data.images.remove(copy)
        shutil.rmtree(self.folder, ignore_errors=True)


def _has_ngons(mesh) -> bool:
    import numpy as np
    totals = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", totals)
    return bool(len(totals) and totals.max() > 4)


def _animated(objects) -> tuple[int, bool]:
    """(objects with animation data that plays something, whether any armature with actions in the file)."""
    import bpy
    count = sum(1 for ob in objects if ob.animation_data is not None
                and (ob.animation_data.action is not None or len(ob.animation_data.nla_tracks)))
    rigs = any(ob.type == "ARMATURE" for ob in objects) and len(bpy.data.actions) > 0
    return count, rigs


def _export(fmt: str, path: Path, objects: list, options: dict) -> None:
    """The exporter on just these objects; the selection and active object put back after."""
    import bpy
    layer = bpy.context.view_layer
    layer.update()  # LOD copies were linked through bpy.data: the view layer's objects are synced first.
    selected, active = [ob for ob in layer.objects if ob is not None and ob.select_get()], layer.objects.active
    try:
        for ob in layer.objects:
            if ob is not None:
                ob.select_set(False)
        for ob in objects:
            if ob.name in layer.objects:
                ob.select_set(True)
        if fmt == "fbx":
            result = bpy.ops.export_scene.fbx(filepath=str(path), **options)
        else:
            result = _gltf(path, options)
        if "FINISHED" not in result:
            raise RuntimeError(f"the exporter returned {sorted(result)}")
    finally:
        for ob in layer.objects:
            if ob is not None:
                ob.select_set(False)
        for ob in selected:
            try:
                ob.select_set(True)
            except ReferenceError:
                pass
        layer.objects.active = active


def _gltf(path: Path, options: dict):
    """The glTF exporter; a .gltf in one file needs the add-on's "allow embedded" preference, on just
    for this export."""
    import bpy
    prefs = None
    if options.get("export_format") == "GLTF_EMBEDDED":
        addon = bpy.context.preferences.addons.get("io_scene_gltf2")
        prefs = addon.preferences if addon is not None else None
    before = getattr(prefs, "allow_embedded_format", None)
    try:
        if prefs is not None:
            prefs.allow_embedded_format = True
        return bpy.ops.export_scene.gltf(filepath=str(path), **options)
    finally:
        if prefs is not None and before is not None:
            prefs.allow_embedded_format = before


def _read_back(fmt: str, data: bytes, path: Path) -> tuple[dict | None, dict, list[str]]:
    """(summary, extra, problems) of the written file."""
    import base64
    import json
    from . import glb
    if fmt == "fbx":
        from io_scene_fbx import parse_fbx
        if not data.startswith(b"Kaydara FBX Binary"):
            return None, {}, ["not a binary FBX"]
        root, _version = parse_fbx.parse(str(path))
        found = fbx_summary(root)
        meshes = list(found["nodes"].values())
        extra = {"clips": found["clips"], "mapped": len(meshes), "tangents": sum(m["tangents"] for m in meshes)}
        return found, extra, []
    try:
        if fmt == "glb":
            gltf, binary = glb.parse(data)
        else:
            gltf = json.loads(data.decode("utf-8"))
            buffers = gltf.get("buffers") or []
            binary = b""
            if buffers and str(buffers[0].get("uri", "")).startswith("data:"):
                binary = base64.b64decode(buffers[0]["uri"].split(",", 1)[1])
                gltf = {**gltf, "buffers": [{k: v for k, v in buffers[0].items() if k != "uri"}, *buffers[1:]]}
        problems = glb.validate(gltf, binary)
    except (ValueError, KeyError, UnicodeDecodeError) as ex:
        return None, {}, [str(ex)]
    return (glb.summary(gltf) if not problems else None), gltf_extra(gltf), problems


def _scale_notes(objects) -> list[str]:
    notes = []
    for ob in objects:
        if ob.type not in ("MESH", "ARMATURE"):
            continue
        scale = tuple(ob.scale)
        if any(abs(v - 1.0) > 1e-4 for v in scale):
            kind = "a negative" if any(v < 0 for v in scale) else "a non-uniform" if max(scale) - min(scale) > 1e-4 \
                else "an unapplied"
            notes.append(f"{ob.name} has {kind} scale ({', '.join(f'{v:g}' for v in scale)}): engines keep it on the "
                         f"node; apply it (Object > Apply > Scale) if physics or colliders use this mesh.")
    return notes[:4]


def export_for_engine(engine: str, objects: list | None = None, path: str | None = None,
                      triangle_budget: int | None = None, lods: int = 0, overwrite: bool = False):
    """The tool (see SCHEMAS and the module docstring). Main thread."""
    import bpy
    from . import detail_maps, glb, motion_bake, shading
    from .tools import InputMistake, ToolError, ToolResult
    chosen_preset = preset(engine)
    engine = chosen_preset["engine"]
    if triangle_budget is not None and (isinstance(triangle_budget, bool) or not isinstance(triangle_budget, int)
                                        or triangle_budget <= 0):
        raise InputMistake("triangle_budget: a whole number of triangles above 0")
    if isinstance(lods, bool) or not isinstance(lods, int) or not 0 <= lods <= MAX_LODS:
        raise InputMistake(f"lods: 0 to {MAX_LODS}")
    target = _destination(path, engine, chosen_preset["formats"])
    fmt = target.suffix.lower().lstrip(".")
    replacing = target.exists()
    if replacing and not overwrite:
        raise ToolError(f"{target} already exists; pass overwrite=true to replace it (only once the new file checks out).")
    chosen, added = _chosen(objects)
    meshes = [ob for ob in chosen if ob.type == "MESH"]
    if not meshes:
        raise ToolError("Nothing to export: name the objects (meshes, with their rig).")
    shading_lines = [f"Shading: {line}" for line in shading.lines(shading.check(meshes, fix=True), limit=6)]
    stage = _Stage()
    notes, temp = [], target.with_name(f".{target.stem}.loopcut-{uuid.uuid4().hex[:8]}{target.suffix}")
    try:
        # Detail normals: glTF has no slot for them and engines' FBX importers read one normal map, so the
        # combined normal goes out as the normal map (detail_maps.py); before the texture copies, so it is capped
        # and flipped like any normal map.
        notes += detail_maps.bake_for_export(stage, meshes)
        # Textures: copies over the engine's cap, normal maps the other way up, and what an FBX can't embed.
        cap, flip = chosen_preset["texture_cap"], chosen_preset["flip_green"]
        normal = _normal_images(meshes) if flip else set()
        for image, nodes in _images(meshes).items():
            if not image.size[0]:
                notes.append(f"{image.name}: its file is missing, so the export has no pixels for it.")
                continue
            size = scaled(image.size, cap)
            turn = image in normal
            if size or turn or (fmt == "fbx" and _unembeddable(image)):
                stage.texture(image, nodes, size, turn, engine)
                what = [f"{image.size[0]}x{image.size[1]} to {size[0]}x{size[1]}" if size else "",
                        "green flipped (DirectX style)" if turn else "", "saved so it embeds" if not (size or turn) else ""]
                notes.append(f"{image.name}: {', '.join(w for w in what if w)} on a copy; yours is unchanged.")
            elif chosen_preset["texture_note"] and max(image.size) > chosen_preset["texture_note"]:
                notes.append(f"{image.name} is {image.size[0]}x{image.size[1]}: mobile and web builds usually show at "
                             f"most {chosen_preset['texture_note']}; set the texture's Size Limit on import for those.")
        # LODs: decimated copies for Unity; the other engines make their own.
        exported = list(chosen)
        if lods and chosen_preset["lods"]:
            for ob in meshes:
                base = ob.name
                stage.rename(ob, f"{base}_LOD0")
                for level in range(1, lods + 1):
                    exported.append(stage.lod(ob, base, level, 0.5 ** level))
        elif lods:
            notes.append(f"{chosen_preset['title']} makes its own LODs on import: no LOD copies added.")
        if fmt != "fbx":  # FBX triangulates on export (use_triangles); glTF's tangents fail on ngons.
            for ob in meshes:
                if _has_ngons(ob.data):
                    stage.triangulate(ob)
        # Bone motion from constraints, drivers and frame handlers: baked into keys on copies (motion_bake.py).
        try:
            motion = motion_bake.prepare(exported, bpy.context.scene)
            stage.undo.append(motion.undo)
        except Exception as ex:  # noqa: BLE001 - the export stands; the exporter samples the motion as it plays.
            motion = motion_bake.Baked()
            motion.lines.append(f"Secondary motion not baked ({type(ex).__name__}: {str(ex)[:120]}); the exporter "
                                f"sampled it as it plays.")
        # Skinned meshes are written in their rest pose: measure them so (a posed character measured as posed
        # failed the size check: Muse's arms down, T-pose in the file).
        posed = [ob.data for ob in exported if ob.type == "ARMATURE" and ob.data.pose_position != "REST"]
        try:
            for data in posed:
                data.pose_position = "REST"
            bpy.context.view_layer.update()
            expected = glb._expected(exported)
        finally:
            for data in posed:
                data.pose_position = "POSE"
            bpy.context.view_layer.update()
        if fmt == "fbx":
            expected = {name: {**value, "size": None} for name, value in expected.items()}
        animated, rigs = _animated(exported)
        if fmt == "fbx":
            options = fbx_options(engine, armature=any(ob.type == "ARMATURE" for ob in exported),
                                  animated=bool(animated or rigs))
            if motion.nla:  # The baked copies, one NLA strip each, named "Object|Action" as before.
                options.update(bake_anim_use_all_actions=False, bake_anim_use_nla_strips=True)
        else:
            options = gltf_options(fmt=fmt, animated_objects=animated, shape_keys=any(
                ob.type == "MESH" and ob.data.shape_keys is not None for ob in exported))
            if motion.nla:  # Each single-strip track's action as its own clip, by name; only those (a lone
                # armature otherwise gets every action in the file, the originals included).
                options.update(export_animation_mode="ACTIONS", export_anim_single_armature=False)
        try:
            _export(fmt, temp, exported, options)
            data = temp.read_bytes()
        except (RuntimeError, OSError, TypeError) as ex:
            return ToolResult(f"Not exported: the exporter failed ({ex}); {target} is as it was.", ok=False)
        found, extra, problems = _read_back(fmt, data, temp)
        problems += glb.compare(found, expected) if found else []
        if problems:
            return ToolResult("\n".join([f"Not exported: the new file failed its checks, so {target} is "
                                         f"{'as it was' if replacing else 'not written'}."]
                                        + [f"- {p}" for p in problems[:MAX_LINES]]), ok=False)
        os.replace(temp, target)
        lod_names = {ob.name for ob in stage.lods}
        tris = {name: value["tris"] for name, value in expected.items() if name not in lod_names}
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        stage.restore()
    title = chosen_preset["title"]
    clips = extra.get("clips") or []
    lines = [f"Exported for {title}: {target} ({_human(len(data))}){', replacing the earlier file' if replacing else ''}. "
             f"{fmt.upper()}, {chosen_preset['axes']}; modifiers applied, "
             + ("triangulated, " if fmt == "fbx" else "")
             + f"tangents on {extra.get('tangents', 0)} of {extra.get('mapped', 0)} UV-mapped meshes; "
             + (f"{len(clips)} clip{'s' * (len(clips) != 1)}: {', '.join(clips[:8])}" if clips else "no clips")
             + f". {len(expected)} objects match the scene in name and triangles"
             + (" and size." if fmt != "fbx" else ".")]
    for name in list(expected)[:MAX_LINES]:
        lines.append(f"  {name}: {expected[name]['tris']:,} triangles")
    if len(expected) > MAX_LINES:
        lines.append(f"  and {len(expected) - MAX_LINES} more")
    if added:
        lines.append("Added its rig: " + ", ".join(added) + ".")
    lines += budget_lines(engine, tris, triangle_budget, chosen_preset["mesh_triangles"])
    lines += notes
    lines += motion.lines
    lines += shading_lines
    lines += _scale_notes(chosen)
    if fmt != "fbx":
        lines += [f"Warning: glTF leaves out {line} (bake it to an image to keep the look)"
                  for line in glb.dropped_nodes(meshes)]
    lines.append(TIPS[engine])
    return ToolResult("\n".join(lines))


def describe(name: str, arguments: dict) -> tuple[str, str] | None:
    if name != "export_for_engine":
        return None
    engine = PRESETS.get(str(arguments.get("engine", "")).lower(), {}).get("title", str(arguments.get("engine", "")))
    what = ", ".join(map(str, arguments.get("objects") or [])) or "every deliverable"
    where = f" to {arguments['path']}" if arguments.get("path") else ""
    verb = "Export (replacing)" if arguments.get("overwrite") else "Export"
    return f"{verb} {what} for {engine}{where}", ""


DISPATCH = {"export_for_engine": export_for_engine}
