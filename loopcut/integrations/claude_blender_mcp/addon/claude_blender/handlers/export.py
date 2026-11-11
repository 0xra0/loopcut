"""export toolset: FBX/glTF/OBJ with engine presets, and a re-import check."""

import os

import bpy

from . import CommandError, command
from .animate import fcurves
from .util import get_object, ops_context

FORMATS = ("fbx", "glb", "gltf", "obj")
PRESETS = ("unity", "unreal", "godot", "generic")

FBX_COMMON = {
    "use_selection": True,
    "object_types": {"ARMATURE", "MESH", "EMPTY"},
    "add_leaf_bones": False,
    "use_armature_deform_only": True,
    "mesh_smooth_type": "FACE",
    "use_tspace": True,
    "path_mode": "COPY",
    "embed_textures": True,
    "bake_anim_use_nla_strips": True,
    "bake_anim_use_all_actions": False,
}
FBX_PRESETS = {
    # Unity: Y up, -Z forward, with the axis conversion and scale baked into the data.
    "unity": {"axis_forward": "-Z", "axis_up": "Y", "apply_scale_options": "FBX_SCALE_ALL",
              "bake_space_transform": True, "primary_bone_axis": "Y", "secondary_bone_axis": "X"},
    # Unreal: same axes, unit scale left as authored.
    "unreal": {"axis_forward": "-Z", "axis_up": "Y", "apply_scale_options": "FBX_SCALE_NONE",
               "bake_space_transform": False, "primary_bone_axis": "Y", "secondary_bone_axis": "X"},
    "godot": {"axis_forward": "-Z", "axis_up": "Y", "apply_scale_options": "FBX_SCALE_ALL"},
    "generic": {},
}


def _gather(names):
    if names:
        objs = [get_object(n) for n in names]
    else:
        objs = [o for o in bpy.context.scene.objects if o.type in {"MESH", "ARMATURE"} and o.visible_get()]
    # Bring along armatures that deform the chosen meshes, and meshes parented to chosen armatures.
    extra = []
    for o in objs:
        for m in getattr(o, "modifiers", []):
            if m.type == "ARMATURE" and m.object and m.object not in objs + extra:
                extra.append(m.object)
        if o.type == "ARMATURE":
            extra += [c for c in o.children if c.type == "MESH" and c not in objs + extra]
    objs += extra
    if not objs:
        raise CommandError("Nothing to export")
    return objs


@command("export_model", "export")
def export_model(path, format=None, objects=None, preset="generic", animation=True, apply_modifiers=True):
    """Export meshes (and their armatures and animation) to a file.
    format: fbx, glb, gltf or obj (default from the extension). preset (FBX): unity, unreal,
    godot, generic. NLA strips become separate named clips. Textures are embedded/copied."""
    path = bpy.path.abspath(path)
    fmt = (format or os.path.splitext(path)[1].lstrip(".")).lower()
    if fmt not in FORMATS:
        raise CommandError(f"format must be one of {FORMATS}")
    if preset not in PRESETS:
        raise CommandError(f"preset must be one of {PRESETS}")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    objs = _gather(objects)
    active = next((o for o in objs if o.type == "ARMATURE"), objs[0])
    with ops_context(active, selected=objs, mode="OBJECT"):
        if fmt == "fbx":
            kw = {**FBX_COMMON, **FBX_PRESETS[preset], "filepath": path, "bake_anim": bool(animation),
                  "use_mesh_modifiers": bool(apply_modifiers)}
            bpy.ops.export_scene.fbx(**kw)
        elif fmt in ("glb", "gltf"):
            bpy.ops.export_scene.gltf(filepath=path, export_format="GLB" if fmt == "glb" else "GLTF_SEPARATE",
                                      use_selection=True, export_apply=bool(apply_modifiers),
                                      export_animations=bool(animation), export_yup=True)
        else:
            bpy.ops.wm.obj_export(filepath=path, export_selected_objects=True,
                                  apply_modifiers=bool(apply_modifiers), export_materials=True)
    if not os.path.exists(path):
        raise CommandError(f"Exporter reported success but {path} was not written")
    return {"path": path, "format": fmt, "preset": preset, "objects": [o.name for o in objs],
            "bytes": os.path.getsize(path)}


@command("reimport_check", "export")
def reimport_check(path):
    """Import an exported file into a throwaway scene and report what came back (objects, tris,
    bones, clips, materials, scale), then delete it. Compare with the source to catch export problems."""
    path = bpy.path.abspath(path)
    if not os.path.isfile(path):
        raise CommandError(f"No file at {path}")
    ext = os.path.splitext(path)[1].lower()
    collections = ("objects", "meshes", "armatures", "actions", "materials", "images", "cameras", "lights")
    before = {c: set(getattr(bpy.data, c)) for c in collections}
    window = bpy.context.window_manager.windows[0] if bpy.context.window_manager.windows else None
    main_scene = window.scene if window else bpy.context.scene
    tmp = bpy.data.scenes.new("_claude_reimport")
    try:
        override = {"scene": tmp}
        if window:
            window.scene = tmp
        with bpy.context.temp_override(**override):
            if ext == ".fbx":
                bpy.ops.import_scene.fbx(filepath=path)
            elif ext in (".glb", ".gltf"):
                bpy.ops.import_scene.gltf(filepath=path)
            elif ext == ".obj":
                bpy.ops.wm.obj_import(filepath=path)
            else:
                raise CommandError(f"Can't import {ext}")
        new = {c: [x for x in getattr(bpy.data, c) if x not in before[c]] for c in collections}
        objs = new["objects"]
        report = {
            "path": path,
            "objects": [{"name": o.name, "type": o.type, "scale": [round(v, 4) for v in o.scale],
                         "rotation_deg": [round(v * 57.29578, 2) for v in o.rotation_euler],
                         "dimensions": [round(v, 4) for v in o.dimensions]} for o in objs],
            "tris": sum(sum(len(p.vertices) - 2 for p in o.data.polygons) for o in objs if o.type == "MESH"),
            "bones": sum(len(o.data.bones) for o in objs if o.type == "ARMATURE"),
            "skinned_meshes": [o.name for o in objs if o.type == "MESH" and any(m.type == "ARMATURE" for m in o.modifiers)],
            "actions": [{"name": a.name, "frame_range": list(a.frame_range), "fcurves": len(fcurves(a))} for a in new["actions"]],
            "materials": [m.name for m in new["materials"]],
            "images": [i.name for i in new["images"]],
        }
    finally:
        if window:
            window.scene = main_scene
        for c in reversed(collections):
            for x in [x for x in getattr(bpy.data, c) if x not in before[c]]:
                getattr(bpy.data, c).remove(x)
        bpy.data.scenes.remove(tmp)
    return report
