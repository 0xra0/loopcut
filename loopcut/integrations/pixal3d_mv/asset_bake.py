"""Matched-component high-to-low Cycles baking and engine texture conventions."""
from pathlib import Path
from copy import copy

import bpy
from mathutils.kdtree import KDTree
import numpy as np

import prep_mesh as prep
from asset_geometry import bounds, duplicate


def pixels(image):
    result = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(result)
    return result.reshape(-1, 4)


def new_image(name, size, color="Non-Color"):
    image = bpy.data.images.new(name, width=size, height=size, alpha=True)
    image.colorspace_settings.name = color
    image.pixels.foreach_set(np.zeros(size * size * 4, dtype=np.float32))
    return image


def save_image(image, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.filepath_raw = str(path)
    image.file_format = "PNG"
    image.save()
    return str(path)


def source_materials(obj):
    if not obj.data.materials:
        material = bpy.data.materials.new("NeutralSource")
        material.use_nodes = True
        obj.data.materials.append(material)
    records = []
    uv_name = obj.data.uv_layers.active.name if obj.data.uv_layers else None
    if uv_name:
        obj.data.uv_layers.active.active_render = True
    for slot in obj.material_slots:
        if slot.material is None:
            raise ValueError("Empty source material slot; use --materials discard to build neutral textures")
        slot.material = slot.material.copy()
        mat = slot.material
        mat.use_nodes = True
        nodes, links = mat.node_tree.nodes, mat.node_tree.links
        output = next((n for n in nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
        shader = output.inputs["Surface"].links[0].from_node if output and output.inputs["Surface"].is_linked else None
        if shader is None or shader.type != "BSDF_PRINCIPLED":
            raise ValueError("Production PBR transfer requires Principled BSDF -> Material Output; "
                             "use --materials discard for unsupported shaders")
        for node in nodes:
            if node.type != "TEX_IMAGE" or not any(output.is_linked for output in node.outputs):
                continue
            if node.image is None:
                raise ValueError("A connected source texture node has no image: " + mat.name)
            image = node.image
            if image.source == "FILE" and not image.has_data and not image.packed_file:
                path = Path(bpy.path.abspath(image.filepath, library=image.library))
                if not path.is_file():
                    raise ValueError("Missing source texture: " + str(path))
        if uv_name:
            uv = nodes.new("ShaderNodeUVMap")
            uv.uv_map = uv_name
            for node in list(nodes):
                if node.type == "TEX_IMAGE" and not node.inputs["Vector"].is_linked:
                    links.new(uv.outputs[0], node.inputs["Vector"])
                elif node.type == "TEX_COORD":
                    for link in list(node.outputs["UV"].links):
                        links.new(uv.outputs[0], link.to_socket)
        records.append((mat, shader, output, nodes.new("ShaderNodeEmission")))
    return records


def uv_triangles(objects):
    """Per-triangle UV corners, (n, 3, 2), of the active UV layers."""
    result = []
    for obj in objects:
        mesh = obj.data
        mesh.calc_loop_triangles()
        uv = np.empty(len(mesh.loops) * 2, np.float32)
        mesh.uv_layers.active.data.foreach_get("uv", uv)
        loops = np.empty(len(mesh.loop_triangles) * 3, np.int32)
        mesh.loop_triangles.foreach_get("loops", loops)
        result.append(uv.reshape(-1, 2)[loops].reshape(-1, 3, 2))
    return np.concatenate(result)


def painted_sources(parts, path):
    """Colour sources for --texture-from: copies of the LOD0 parts wearing the painted GLB's material.

    pa.py texture paints LOD0's own atlas (UltraTex uv_mode keep_existing), so the material fits
    these UVs as they are. Only the UVs are compared, so a pivot change does not matter."""
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path))
    imported = [x for x in bpy.data.objects if x not in before]
    meshes = [x for x in imported if x.type == "MESH"]
    if len(meshes) != 1 or len(meshes[0].data.materials) != 1 or not meshes[0].data.uv_layers:
        raise ValueError("--texture-from needs one mesh with one material and UVs: " + str(path))
    ours, theirs = uv_triangles(parts).mean(axis=1), uv_triangles(meshes).mean(axis=1)
    tree = KDTree(len(theirs))
    for i, (u, v) in enumerate(theirs):
        tree.insert((u, v, 0), i)
    tree.balance()
    hits = [tree.find((u, v, 0)) for u, v in ours]
    if len(ours) != len(theirs) or len({i for _, i, _ in hits}) != len(ours) or max(d for _, _, d in hits) > 1e-5:
        raise ValueError("--texture-from was painted on a different LOD0 atlas: rebuild with the painted build's "
                         "source, --faces and --pivot (the pivot moves the mesh before xatlas), or re-run pa.py texture")
    material = meshes[0].data.materials[0]
    sources = []
    for i, part in enumerate(parts):
        source = duplicate(part, f"PaintedSource_{i:04}")
        source.data.materials.clear()
        source.data.materials.append(material)
        for polygon in source.data.polygons:
            polygon.material_index = 0
        sources.append(source)
    for obj in imported:
        bpy.data.objects.remove(obj)
    return sources


def source_channel(records, socket):
    for mat, shader, output, emission in records:
        links = mat.node_tree.links
        target = emission.inputs["Color"]
        for link in list(target.links):
            links.remove(link)
        if socket:
            source = shader.inputs[socket]
            if source.is_linked:
                links.new(source.links[0].from_socket, target)
            else:
                value = source.default_value
                target.default_value = (value, value, value, 1) if isinstance(value, (float, int)) else value
            if socket == "Emission Color":
                multiply = mat.node_tree.nodes.new("ShaderNodeVectorMath")
                multiply.operation = "SCALE"
                if target.is_linked:
                    links.new(target.links[0].from_socket, multiply.inputs[0])
                else:
                    multiply.inputs[0].default_value = target.default_value[:3]
                strength = shader.inputs["Emission Strength"]
                if strength.is_linked:
                    links.new(strength.links[0].from_socket, multiply.inputs[3])
                else:
                    multiply.inputs[3].default_value = strength.default_value
                links.new(multiply.outputs[0], target)
            links.new(emission.outputs[0], output.inputs["Surface"])
        else:
            links.new(shader.outputs[0], output.inputs["Surface"])


def constant_channel(records, socket):
    """The RGBA value of an unlinked input shared by every source material, else None.
    Only linear data and black emission: other sRGB constants would need a colour-space conversion."""
    values = set()
    for record in records:
        for _, shader, _, _ in record:
            source = shader.inputs[socket]
            if source.is_linked:
                return None
            if socket == "Emission Color":
                strength = shader.inputs["Emission Strength"]
                if strength.is_linked:
                    return None
                values.add((0.0, 0.0, 0.0, 1.0) if not strength.default_value or max(source.default_value[:3]) == 0 else None)
            elif socket in ("Roughness", "Metallic", "Alpha"):
                values.add((float(source.default_value),) * 3 + (1.0,))
            else:
                return None
    return values.pop() if len(values) == 1 and None not in values else None


def target_material(obj, image):
    mat = bpy.data.materials.new("BakeTarget")
    mat.use_nodes = True
    node = mat.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    mat.node_tree.nodes.active = node
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    for polygon in obj.data.polygons:
        polygon.material_index = 0
    return node


def uv_mask(parts, size):
    mask = np.zeros((size, size), dtype=bool)
    for obj in parts:
        obj.data.calc_loop_triangles()
        uv = obj.data.uv_layers.active
        for triangle in obj.data.loop_triangles:
            coords = np.array([uv.data[i].uv[:] for i in triangle.loops], dtype=np.float64) * size
            low = np.maximum(0, np.ceil(coords.min(axis=0) - .5).astype(int))
            high = np.minimum(size - 1, np.floor(coords.max(axis=0) - .5).astype(int))
            if np.any(high < low):
                continue
            x, y = np.meshgrid(np.arange(low[0], high[0] + 1) + .5, np.arange(low[1], high[1] + 1) + .5)
            signs = []
            for i, a in enumerate(coords):
                b = coords[(i + 1) % 3]
                signs.append((b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]))
            inside = (np.minimum.reduce(signs) >= -1e-8) | (np.maximum.reduce(signs) <= 1e-8)
            mask[low[1]:high[1] + 1, low[0]:high[0] + 1] |= inside
    return mask.reshape(-1)


def project_channel(highs, lows, image, args, kind="NORMAL", records=None, socket=None, all_highs=None):
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    # HIP on the RX 6800 was no faster (necklace 355 s vs 320 s): per-call sync dominates, not rays.
    scene.cycles.device = "CPU"
    scene.cycles.samples = args.bake_samples if kind == "AO" else 1
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH":
            obj.hide_render = True
    for obj in all_highs or []:
        obj.hide_render = False
    for i, (source, target) in enumerate(zip(highs, lows)):
        prep.activate(target)
        target.hide_render = False
        source.hide_set(False)
        source.hide_render = False
        source.select_set(True)
        if records:
            source_channel(records[i], socket)
        target_material(target, image)
        if target.data.uv_layers.active:
            target.data.uv_layers.active.active_render = True
        lo, hi = bounds(source)
        diagonal = source.get("pa_cage_diagonal", (hi - lo).length)
        # The UV option is the total inter-island gutter. Each island may dilate
        # into at most half of it without overwriting a neighboring island.
        scene.render.bake.margin = max(0, int(args.uv_margin_px * image.size[0] / args.texture_size / 2))
        scene.render.bake.use_clear = i == 0
        bpy.ops.object.bake(type=kind, use_selected_to_active=True, use_clear=i == 0,
                            use_cage=args.cage_extrusion > 0, cage_extrusion=diagonal * args.cage_extrusion,
                            max_ray_distance=diagonal * args.ray_distance,
                            normal_space="TANGENT", uv_layer=target.data.uv_layers.active.name)
        target.hide_render = True
        if source not in (all_highs or []):
            source.hide_render = True
    values = pixels(image)
    if not np.isfinite(values).all():
        raise ValueError(f"Non-finite pixels in {image.name}")
    return values


def game_material(images, normal, name, alpha_mode="opaque", two_sided=False, emission_used=True):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.use_backface_culling = not two_sided
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    shader = nodes.get("Principled BSDF")
    for label, socket in (("basecolor", "Base Color"), ("roughness", "Roughness"),
                          ("metallic", "Metallic"), ("emission", "Emission Color")):
        if label == "emission" and not emission_used:
            continue
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = images[label]
        links.new(tex.outputs["Color"], shader.inputs[socket])
        if label == "basecolor" and alpha_mode != "opaque":
            if alpha_mode == "mask":
                clip = nodes.new("ShaderNodeMath")
                clip.operation = "GREATER_THAN"
                clip.inputs[1].default_value = .5
                links.new(tex.outputs["Alpha"], clip.inputs[0])
                links.new(clip.outputs[0], shader.inputs["Alpha"])
            else:
                links.new(tex.outputs["Alpha"], shader.inputs["Alpha"])
        if label == "emission":
            shader.inputs["Emission Strength"].default_value = 1
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = normal
    node = nodes.new("ShaderNodeNormalMap")
    node.uv_map = "GameUV"
    links.new(tex.outputs["Color"], node.inputs["Color"])
    links.new(node.outputs["Normal"], shader.inputs["Normal"])
    # glTF exporter recognizes the dedicated occlusion node group.
    group = bpy.data.node_groups.get("glTF Material Output")
    if group is None:
        group = bpy.data.node_groups.new("glTF Material Output", "ShaderNodeTree")
        group.interface.new_socket(name="Occlusion", in_out="INPUT", socket_type="NodeSocketFloat")
    out = nodes.new("ShaderNodeGroup")
    out.node_tree = group
    ao = nodes.new("ShaderNodeTexImage")
    ao.image = images["ao"]
    links.new(ao.outputs["Color"], out.inputs["Occlusion"])
    return mat


def bake_lod_set(highs, originals, lod_parts, args, folder, has_source_materials):
    folder = Path(folder)
    records = []
    for obj in originals:
        if args.materials == "discard":
            obj.data.materials.clear()
        records.append(source_materials(obj))
    source_uses_alpha = any(shader.inputs["Alpha"].is_linked or shader.inputs["Alpha"].default_value < .999
                            for record in records for _, shader, _, _ in record)
    # Detail geometry uses geometric normals. Color sources retain their original UV/material setup.
    geometry_mat = bpy.data.materials.new("GeometrySource")
    geometry_mat.use_nodes = True
    for obj in highs:
        # Preserve existing tangent-space shader detail when its source UVs survive.
        # A remesh with no source UVs uses geometric normals on the approved surface.
        if args.materials == "discard" or not obj.data.materials or not obj.data.uv_layers:
            obj.data.materials.clear()
            obj.data.materials.append(geometry_mat)
            for poly in obj.data.polygons:
                poly.material_index = 0
    images, paths = {}, {}
    for label, socket in (("basecolor", "Base Color"), ("roughness", "Roughness"),
                          ("metallic", "Metallic"), ("alpha", "Alpha"), ("emission", "Emission Color")):
        print("ASSET bake", label, flush=True)
        image = new_image(args.name + "_" + label, args.texture_size, "sRGB" if label in ("basecolor", "emission") else "Non-Color")
        constant = constant_channel(records, socket)
        if constant is None:
            project_channel(originals, lod_parts[0], image, args, "EMIT", records, socket)
        else:
            # A bake pass per group would only write this value; texels outside islands are unused.
            image.pixels.foreach_set(np.tile(np.array(constant, dtype=np.float32), image.size[0] * image.size[1]))
            print("ASSET constant", label, constant[0], flush=True)
        images[label] = image
    rgba = pixels(images["basecolor"])
    rgba[:, 3] = pixels(images["alpha"])[:, 0]
    images["basecolor"].pixels.foreach_set(rgba.reshape(-1))
    for record in records:
        source_channel(record, None)
    print("ASSET bake AO", flush=True)
    images["ao"] = new_image(args.name + "_AO", args.texture_size)
    project_channel(highs, lod_parts[0], images["ao"], args, "AO", all_highs=highs)
    normals, miss = [], []
    for index, lows in enumerate(lod_parts):
        print("ASSET bake high-to-low normal LOD", index, flush=True)
        size = max(256, args.texture_size // (2 ** index))
        normal = new_image(args.name + f"_Normal_LOD{index}", size)
        values = project_channel(highs, lows, normal, args, "NORMAL")
        mask = uv_mask(lows, size)
        missed = np.linalg.norm(values[:, :3], axis=1) < 1e-6
        fraction = float(np.count_nonzero(missed & mask) / max(1, np.count_nonzero(mask)))
        miss.append({"lod": index, "uv_pixels": int(mask.sum()), "fraction": fraction})
        normals.append(normal)
        paths[f"normal_lod{index}"] = save_image(normal, folder / f"normal_lod{index}.png")
        if args.engine in ("unreal", "cyberpunk", "witcher3"):
            directx = new_image(args.name + f"_NormalDX_LOD{index}", size)
            values[:, 1] = 1 - values[:, 1]
            directx.pixels.foreach_set(values.reshape(-1))
            paths[f"normal_dx_lod{index}"] = save_image(directx, folder / f"normal_dx_lod{index}.png")
    for label in ("basecolor", "roughness", "metallic", "ao", "emission"):
        paths[label] = save_image(images[label], folder / (label + ".png"))
    packed = new_image(args.name + "_ORM", args.texture_size)
    values = np.ones((args.texture_size ** 2, 4), dtype=np.float32)
    values[:, 0] = pixels(images["ao"])[:, 0]
    values[:, 1] = pixels(images["roughness"])[:, 0]
    values[:, 2] = pixels(images["metallic"])[:, 0]
    packed.pixels.foreach_set(values.reshape(-1))
    paths["orm"] = save_image(packed, folder / "orm.png")
    if args.engine == "unity":
        unity = new_image(args.name + "_MetallicSmoothness", args.texture_size)
        values[:, 0] = values[:, 2]
        values[:, 3] = 1 - values[:, 1]
        values[:, 1:3] = 0
        unity.pixels.foreach_set(values.reshape(-1))
        paths["metallic_smoothness"] = save_image(unity, folder / "metallic_smoothness.png")
    mask = uv_mask(lod_parts[0], args.texture_size)
    color = pixels(images["basecolor"])
    alpha_mode = args.alpha_mode
    if alpha_mode == "auto":
        successful = np.linalg.norm(pixels(normals[0])[:, :3], axis=1) > 1e-6
        alpha_mask = mask & successful
        covered_alpha = color[alpha_mask if alpha_mask.any() else mask, 3]
        fraction = float(np.mean(covered_alpha < .99)) if covered_alpha.size else 0
        intermediate = float(np.mean((covered_alpha > .01) & (covered_alpha < .99))) if covered_alpha.size else 0
        alpha_mode = "opaque" if not source_uses_alpha else "blend" if intermediate > .01 else "mask" if fraction > .01 else "opaque"
    emission_used = bool(np.max(pixels(images["emission"])[:, :3]) > .001)
    for index, lows in enumerate(lod_parts):
        material = game_material(images, normals[index], args.name + f"_PBR_LOD{index}", alpha_mode, args.two_sided, emission_used)
        for obj in lows:
            obj.data.materials.clear()
            obj.data.materials.append(material)
            for polygon in obj.data.polygons:
                polygon.material_index = 0
            for attr in list(obj.data.color_attributes):
                obj.data.color_attributes.remove(attr)
    # Runtime set: base color, packed data, normals for each LOD, plus optional emission.
    base_and_data = 3 if args.engine == "unity" else 2
    if args.engine in ("cyberpunk", "witcher3"):
        # Conservative uncompressed source-map set; native material packing is template-specific.
        base_and_data = 4  # base color, roughness, metalness, AO; one normal counted below
    byte_count = args.texture_size ** 2 * 4 * (base_and_data + int(emission_used)) + sum(n.size[0] ** 2 * 4 for n in normals)
    report = {"method": "matched_component_selected_to_active", "cage_fraction": args.cage_extrusion,
              "ray_distance_fraction_without_cage": args.ray_distance, "projection_miss": miss,
              "source_materials_present": has_source_materials and args.materials == "bake", "alpha_mode": alpha_mode,
              "emission_used": emission_used,
              "basecolor_black_fraction": float(np.mean(np.max(color[mask, :3], axis=1) < .001)) if mask.any() else 1,
              "runtime_rgba8_mip_estimate_mib": byte_count * 4 / 3 / 2 ** 20,
              "runtime_estimate_excludes_platform_compression": True, "paths": paths}
    if args.engine in ("cyberpunk", "witcher3"):
        report["runtime_estimate_basis"] = "Separate source maps; native texture packing/compression not configured"
    return images, normals, report


def bake_asset(highs, originals, lod_parts, args, folder, has_source_materials):
    """Each LOD has its own checked atlas and full bake, preventing reduction
    from introducing UV foldovers while accidentally reusing a mismatched texture.
    """
    reports, images, normals = [], [], []
    for index, parts in enumerate(lod_parts):
        settings = copy(args)
        settings.name = args.name + f"_LOD{index}"
        settings.texture_size = max(256, args.texture_size // (2 ** index))
        settings.uv_margin_px = max(1, args.uv_margin_px * settings.texture_size / args.texture_size)
        print("ASSET full PBR bake LOD", index, "resolution", settings.texture_size, flush=True)
        maps, normal, report = bake_lod_set(highs, originals, [parts], settings, Path(folder) / f"lod{index}", has_source_materials)
        report["index"] = index
        reports.append(report)
        images.append(maps)
        normals.extend(normal)
    report = dict(reports[0])
    report["lod_bakes"] = reports
    report["projection_miss"] = [{**value["projection_miss"][0], "lod": i} for i, value in enumerate(reports)]
    report["runtime_rgba8_mip_estimate_mib"] = sum(value["runtime_rgba8_mip_estimate_mib"] for value in reports)
    report["paths"] = dict(reports[0]["paths"])
    for i, value in enumerate(reports[1:], 1):
        report["paths"][f"normal_lod{i}"] = value["paths"]["normal_lod0"]
        if "normal_dx_lod0" in value["paths"]:
            report["paths"][f"normal_dx_lod{i}"] = value["paths"]["normal_dx_lod0"]
    report["lods_use_independent_validated_atlases"] = True
    return images, normals, report
