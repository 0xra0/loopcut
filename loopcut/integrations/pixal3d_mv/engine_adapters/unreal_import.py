"""Run in UnrealEditor with Python Editor Script Plugin enabled; environment supplied by engine_check.py."""
import json
import os
from pathlib import Path

import unreal


def run():
    manifest = Path(os.environ["PA_ASSET_MANIFEST"])
    data = json.loads(manifest.read_text())
    root = manifest.parent
    destination = "/Game/Pixal3D/" + data["name"]
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    def import_file(path, name, options=None):
        task = unreal.AssetImportTask()
        task.filename = str(path)
        task.destination_path = destination
        task.destination_name = name
        task.automated = True
        task.replace_existing = True
        task.save = True
        if options:
            task.options = options
        tools.import_asset_tasks([task])
        paths = task.get_editor_property("imported_object_paths")
        if not paths:
            raise RuntimeError("No imported asset: " + str(path))
        return unreal.load_asset(paths[0])
    options = unreal.FbxImportUI()
    options.import_mesh = True
    options.import_materials = False
    options.import_textures = False
    options.import_as_skeletal = False
    options.static_mesh_import_data.set_editor_property("auto_generate_collision", False)
    options.static_mesh_import_data.set_editor_property("normal_import_method",
                                                        unreal.FBXNormalImportMethod.FBXNIM_IMPORT_NORMALS_AND_TANGENTS)
    options.static_mesh_import_data.set_editor_property("convert_scene_unit", True)
    mesh = import_file(root / data["lods"][0]["files"]["fbx"], data["name"], options)
    subsystem = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    for lod in data["lods"][1:]:
        result = subsystem.import_lod(mesh, lod["index"], str(root / lod["files"]["fbx"]))
        if result != lod["index"]:
            raise RuntimeError("LOD import failed: " + str(lod["index"]))
    materials = []
    for lod in data["lods"]:
        index = lod["index"]
        paths = lod["textures"]
        textures = {}
        for label, key, srgb in (("BaseColor", "basecolor", True), ("ORM", "orm", False),
                                  ("Normal", "normal_dx_lod0", False), ("Emission", "emission", True)):
            if label == "Emission" and not lod.get("emission_used", True):
                continue
            if key not in paths:
                raise RuntimeError("Missing Unreal texture: " + key)
            tex = import_file(root / paths[key], data["name"] + f"_LOD{index}_" + label)
            tex.set_editor_property("srgb", srgb)
            if label == "Normal":
                tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
            textures[label] = tex
            unreal.EditorAssetLibrary.save_loaded_asset(tex)
        material_name = data["name"] + f"_Material_LOD{index}"
        material = unreal.load_asset(destination + "/" + material_name)
        if material is None:
            material = tools.create_asset(material_name, destination, unreal.Material, unreal.MaterialFactoryNew())
        unreal.MaterialEditingLibrary.delete_all_material_expressions(material)
        alpha_mode = lod.get("alpha_mode", "opaque")
        material.set_editor_property("two_sided", data["options"].get("two_sided", False))
        material.set_editor_property("blend_mode", {"opaque": unreal.BlendMode.BLEND_OPAQUE,
                                                    "mask": unreal.BlendMode.BLEND_MASKED,
                                                    "blend": unreal.BlendMode.BLEND_TRANSLUCENT}[alpha_mode])
        if alpha_mode == "mask":
            material.set_editor_property("opacity_mask_clip_value", .5)
        for label, connections in (("BaseColor", (("RGB", unreal.MaterialProperty.MP_BASE_COLOR),)),
                                    ("ORM", (("R", unreal.MaterialProperty.MP_AMBIENT_OCCLUSION),
                                             ("G", unreal.MaterialProperty.MP_ROUGHNESS),
                                             ("B", unreal.MaterialProperty.MP_METALLIC))),
                                    ("Normal", (("RGB", unreal.MaterialProperty.MP_NORMAL),)),
                                    ("Emission", (("RGB", unreal.MaterialProperty.MP_EMISSIVE_COLOR),))):
            if label not in textures:
                continue
            node = unreal.MaterialEditingLibrary.create_material_expression(material, unreal.MaterialExpressionTextureSample)
            node.texture = textures[label]
            if label == "BaseColor" and alpha_mode != "opaque":
                unreal.MaterialEditingLibrary.connect_material_property(node, "A", unreal.MaterialProperty.MP_OPACITY_MASK
                                                                         if alpha_mode == "mask" else unreal.MaterialProperty.MP_OPACITY)
            if label == "Normal":
                node.sampler_type = unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL
            elif label == "ORM":
                node.sampler_type = unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR
            for output, property_name in connections:
                if not unreal.MaterialEditingLibrary.connect_material_property(node, output, property_name):
                    raise RuntimeError("Material connection failed: " + label)
        unreal.MaterialEditingLibrary.recompile_material(material)
        slots = list(mesh.get_editor_property("static_materials"))
        while len(slots) <= index:
            slots.append(unreal.StaticMaterial())
        slots[index].material_interface = material
        slots[index].material_slot_name = unreal.Name(f"LOD{index}")
        mesh.set_editor_property("static_materials", slots)
        for section in range(mesh.get_num_sections(index)):
            subsystem.set_lod_material_slot(mesh, index, index, section)
        unreal.EditorAssetLibrary.save_loaded_asset(material)
        materials.append(material.get_path_name())
    unreal.EditorAssetLibrary.save_loaded_asset(mesh)
    lod_count = subsystem.get_lod_count(mesh)
    if lod_count != len(data["lods"]):
        raise RuntimeError("Unreal LOD count mismatch")
    collision_count = subsystem.get_convex_collision_count(mesh)
    if data["collision"]["hulls"] and collision_count != data["collision"]["hulls"]:
        raise RuntimeError("Unreal collision count mismatch")
    geometry = []
    for lod in data["lods"]:
        triangles = mesh.get_num_triangles(lod["index"])
        if triangles != lod["export_metrics"]["triangles"]:
            raise RuntimeError("Unreal triangle count mismatch at LOD" + str(lod["index"]))
        geometry.append({"lod": lod["index"], "triangles": triangles})
    return {"status": "passed", "engine": "unreal", "version": unreal.SystemLibrary.get_engine_version(),
            "asset": mesh.get_path_name(), "lod_count": lod_count, "collision_hulls": collision_count,
            "materials": materials, "lods": geometry}


result_path = Path(os.environ["PA_ASSET_CHECK_OUTPUT"])
try:
    result_path.write_text(json.dumps(run(), indent=2) + "\n")
except Exception as exc:
    result_path.write_text(json.dumps({"status": "failed", "engine": "unreal", "reason": str(exc)}, indent=2) + "\n")
    raise
