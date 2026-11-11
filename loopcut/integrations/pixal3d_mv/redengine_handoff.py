"""Static mesh handoffs for WolvenKit/REDkit; never writes native game resources."""
import json
from pathlib import Path
import struct

import bpy

import prep_mesh as prep

SOURCES = {
    "cyberpunk": "https://wiki.redmodding.org/wolvenkit/wolvenkit-app/usage/import-export/models",
    "witcher3": "https://cdprojektred.atlassian.net/wiki/spaces/W3REDkit/pages/6326771",
}


def _glb(path):
    data = Path(path).read_bytes()
    if data[:4] != b"glTF":
        raise ValueError("Invalid REDengine handoff GLB")
    size = struct.unpack_from("<I", data, 12)[0]
    return json.loads(data[20:20 + size])


def cyberpunk_mesh(obj, path, triangle_limit=20000):
    """Bound each submesh by <=60k triangle corners, including UV/normal splits.

    Each file represents one independently textured native mesh target at LOD_1.
    Lower-detail reference files are deliberately not combined: their UVs differ.
    """
    if not 1 <= triangle_limit <= 20000:
        raise ValueError("Cyberpunk chunks must contain 1..20000 triangles")
    mesh = obj.data
    mesh.calc_loop_triangles()
    if not mesh.uv_layers.active or not mesh.loop_triangles:
        raise ValueError("Cyberpunk export needs triangles and UV0")
    triangles = list(mesh.loop_triangles)
    uv_data = mesh.uv_layers.active.data
    normals = [n.vector.copy() for n in mesh.corner_normals]
    temporary, renamed = [], []
    try:
        for index, start in enumerate(range(0, len(triangles), triangle_limit)):
            name = f"submesh_{index:02d}_LOD_1"
            for collection in (bpy.data.objects, bpy.data.meshes):
                conflict = collection.get(name)
                if conflict:
                    renamed.append((conflict, name))
                    conflict.name = "PA_handoff_reserved_name"
            vertex_map, positions, faces, uvs, split_normals = {}, [], [], [], []
            for triangle in triangles[start:start + triangle_limit]:
                face = []
                for loop_index in triangle.loops:
                    vertex_index = mesh.loops[loop_index].vertex_index
                    if vertex_index not in vertex_map:
                        vertex_map[vertex_index] = len(positions)
                        positions.append(mesh.vertices[vertex_index].co[:])
                    face.append(vertex_map[vertex_index])
                    uvs.append(uv_data[loop_index].uv[:])
                    split_normals.append(normals[loop_index])
                faces.append(face)
            data = bpy.data.meshes.new(name)
            part = bpy.data.objects.new(name, data)
            temporary.append(part)
            bpy.context.scene.collection.objects.link(part)
            part.matrix_world = obj.matrix_world.copy()
            data.from_pydata(positions, [], faces)
            data.update()
            uv = data.uv_layers.new(name="UVMap")
            for loop, value in zip(uv.data, uvs):
                loop.uv = value
            for polygon in data.polygons:
                polygon.use_smooth = True
            data.normals_split_custom_set(split_normals)
            # Avoid Blender's automatic .001 suffix on either name namespace.
            if part.name != name or data.name != name:
                raise ValueError("Reserved Cyberpunk submesh name already exists: " + name)
        prep.activate(temporary[0])
        for part in temporary:
            part.select_set(True)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=True,
                                  export_texcoords=True, export_normals=True, export_tangents=True,
                                  export_apply=True, export_materials="NONE")
        doc = _glb(path)
        entries = []
        for mesh_data in doc.get("meshes", []):
            if not mesh_data.get("name", "").startswith("submesh_"):
                raise ValueError("Invalid Cyberpunk mesh data-block name")
            for primitive in mesh_data["primitives"]:
                attributes = primitive["attributes"]
                if not {"POSITION", "NORMAL", "TANGENT", "TEXCOORD_0"} <= attributes.keys():
                    raise ValueError("Cyberpunk handoff is missing vertex attributes")
                vertices = doc["accessors"][attributes["POSITION"]]["count"]
                triangles_count = doc["accessors"][primitive["indices"]]["count"] // 3
                if vertices >= 65535 or triangles_count >= 65535:
                    raise ValueError("Cyberpunk submesh exceeds the conservative 16-bit import limit")
                entries.append({"name": mesh_data["name"], "vertices": vertices, "triangles": triangles_count,
                                "target_material_slot": 0})
        if not entries or sum(x["triangles"] for x in entries) != len(triangles):
            raise ValueError("Cyberpunk handoff lost triangles")
        if doc.get("materials") or doc.get("images"):
            raise ValueError("Cyberpunk native handoff must not export preview materials")
        return entries
    finally:
        for part in temporary:
            data = part.data
            bpy.data.objects.remove(part, do_unlink=True)
            bpy.data.meshes.remove(data)
        for item, name in renamed:
            item.name = name


def export_handoff(lods, colliders, args, root, report):
    root = Path(root)
    folder = root / "redengine"
    folder.mkdir(exist_ok=True)
    def relative(path):
        return Path(path).relative_to(root).as_posix()
    data = {"schema_version": 1, "engine": args.engine, "requested_game_version": args.game_version,
            "version_compatibility": "unverified", "native_import_status": "not_run", "asset_type": "static_prop",
            "native_template_depot_path": args.red_template, "native_resources_generated": False,
            "lod_policy": "Separate native mesh targets with independent textures; no automatic native LOD assembly",
            "normal_policy": "GLB preview uses OpenGL. OpenGL and DirectX source maps supplied; match the native shader template.",
            "material_policy": "Source PBR maps require native shader assignment; ORM is a preview, not a REDengine convention",
            "collision_policy": "Reference hulls only; assign/cook native physics in the game tools",
            "reference": SOURCES[args.engine], "lods": []}
    for index, obj in enumerate(lods):
        lod = report["lods"][index]
        entry = {"source_lod": index, "role": "primary" if index == 0 else "alternative mesh target",
                 "textures": {key: relative(value) for key, value in lod["textures"].items()},
                 "alpha_mode": lod["alpha_mode"], "emission_used": lod["emission_used"]}
        if args.engine == "cyberpunk":
            path = folder / f"lod{index}" / (args.name + ".glb")
            entry["submeshes"] = cyberpunk_mesh(obj, path)
            entry["mesh"] = relative(path)
        else:
            entry["mesh"] = relative(lod["files"]["fbx"])
        data["lods"].append(entry)
    if colliders:
        prep.activate(colliders[0])
        for collider in colliders:
            collider.hide_set(False)
            collider.select_set(True)
        path = folder / (args.name + "_collision_reference.fbx")
        bpy.ops.export_scene.fbx(filepath=str(path), use_selection=True, object_types={"MESH"}, bake_anim=False,
                                 add_leaf_bones=False, axis_forward="-Z", axis_up="Y", apply_unit_scale=True,
                                 apply_scale_options="FBX_SCALE_UNITS", path_mode="RELATIVE")
        data["collision_reference"] = relative(path)
    (folder / "handoff.json").write_text(json.dumps(data, indent=2) + "\n")
    target = args.red_template or "Choose a compatible native mesh/template in your mod project."
    if args.engine == "cyberpunk":
        steps = ("1. Create a WolvenKit mod project and add a compatible static `.mesh` template.\n"
                 "2. Use the primary `redengine/lod0/" + args.name + ".glb`. Copy it to the raw path matching "
                 "the target `.mesh` in source, or select the base mesh in the import UI.\n"
                 "3. Run mesh import with strict glTF validation. Do not import a fabricated Material.json; "
                 "this bundle does not generate one.\n"
                 "4. Configure the target's material entries and chunk mapping. Every exported chunk uses "
                 "the same LOD0 atlas and needs the same native material assignment.\n"
                 "5. Assign the separate source maps in a suitable native material. Check tangent/green-channel "
                 "convention, alpha, emission, scale and pivot.\n"
                 "6. Configure native collision/entities, build the mod and test in the target game.\n")
    else:
        steps = ("1. Open the matching Witcher 3 REDkit and your mod depot.\n"
                 "2. In Asset Browser use Import > Mesh and select `meshes/" + args.name + "_LOD0.fbx`.\n"
                 "3. Use an appropriate native `.w2mesh`/material resource and assign the separate source textures "
                 "in its shader. No native material XML is fabricated.\n"
                 "4. Confirm size, orientation and pivot against an exported game reference. The FBX uses "
                 "Blender's -Z forward/Y up and explicit unit metadata.\n"
                 "5. Configure native collision and entity/item attachment. Cook/build the mod and test in the target game.\n")
    rows = "\n".join(f"| {x['source_lod']} | `{x['mesh']}` | `{x['textures']['basecolor']}` |" for x in data["lods"])
    text = (f"# {args.engine} static mesh handoff\n\nRequested game version: {args.game_version}. "
            "Compatibility with that installed game/tool version is unverified.\n\n"
            f"Native target: `{target}`\n\n{steps}\n"
            "All paths below are relative to the asset root.\n\n| Source LOD | Mesh | Its own base color |\n|---|---|---|\n" + rows +
            "\n\nOther LOD files are separately textured alternative mesh targets. Do not combine them as native "
            "LODs with one material: independent atlases need separate native material bindings. For a shared-material "
            "native LOD chain, reproject to one common atlas in the mod tooling first. Cyberpunk alternatives are each "
            "named LOD_1 intentionally; 1/2/4/8 native LOD assembly is not performed.\n\n"
            "Collision FBX/GLB files are reference geometry; UCX naming does not create REDengine physics. "
            "ORM/MetallicSmoothness maps are not native REDengine materials. Match normal-map channels and any "
            "specular/gloss inputs to the chosen native shader; roughness-to-gloss alone does not reconstruct a Witcher material.\n\n"
            "This pass supports static props. Clothing, characters, deforming weapons/accessories and vehicles need "
            "game-specific rigs, weights, physics and attachment checks. Existing rigged/animated inputs are rejected.\n\n"
            "Native .mesh/.w2mesh/.xbm resources, entity files, cooked mods and in-game validation are not generated "
            "by this pass. Review the asset's technical/art flags before integration.\n\n"
            f"Import reference: {SOURCES[args.engine]}\n")
    (folder / "IMPORT.md").write_text(text)
    return {"manifest": str(folder / "handoff.json"), "instructions": str(folder / "IMPORT.md"),
            "status": "prepared_for_manual_import", "native_import_status": "not_run", "details": data}
