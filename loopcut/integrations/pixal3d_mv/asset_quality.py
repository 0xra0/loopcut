"""Render-based quality evidence and an offline HTML asset review gallery."""
import html
import json
import math
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector

from asset_geometry import bounds


def render_material(kind):
    material = bpy.data.materials.new("QA_" + kind)
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    shader = nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (.35, .4, .46, 1)
    shader.inputs["Roughness"].default_value = .6
    if kind == "checker":
        coords = nodes.new("ShaderNodeTexCoord")
        checker = nodes.new("ShaderNodeTexChecker")
        checker.inputs["Scale"].default_value = 24
        checker.inputs["Color1"].default_value = (.08, .15, .35, 1)
        checker.inputs["Color2"].default_value = (.8, .8, .8, 1)
        links.new(coords.outputs["UV"], checker.inputs["Vector"])
        links.new(checker.outputs["Color"], shader.inputs["Base Color"])
    if kind == "wire":
        wire = nodes.new("ShaderNodeWireframe")
        wire.use_pixel_size = True
        wire.inputs["Size"].default_value = .7
        mix = nodes.new("ShaderNodeMixRGB")
        mix.inputs[1].default_value = (.5, .6, .7, 1)
        mix.inputs[2].default_value = (.005, .005, .005, 1)
        links.new(wire.outputs[0], mix.inputs[0])
        links.new(mix.outputs[0], shader.inputs["Base Color"])
    return material


def previews(highs, lods, args, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 4
    scene.cycles.use_denoising = True
    scene.render.film_transparent = True
    scene.render.resolution_x = args.preview_size
    scene.render.resolution_y = args.preview_size
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.world = bpy.data.worlds.new("QA World")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs[0].default_value = (.15, .15, .15, 1)
    scene.world.node_tree.nodes["Background"].inputs[1].default_value = .5
    lo, hi = bounds(lods[0])
    center = (lo + hi) / 2
    size = max(hi - lo)
    up_index = max(range(3), key=lambda i: hi[i] - lo[i])
    forward_index = min(range(3), key=lambda i: hi[i] - lo[i])
    if up_index == forward_index:
        up_index, forward_index = 2, 1
    up = Vector(tuple(1 if i == up_index else 0 for i in range(3)))
    # View 0 is the glTF front (+Z, Blender -Y), the side UltraTex paints with the front reference.
    forward = Vector(tuple(-1 if i == forward_index else 0 for i in range(3)))
    right = up.cross(forward).normalized()
    camera = bpy.data.objects.new("QA Camera", bpy.data.cameras.new("QA Camera"))
    scene.collection.objects.link(camera)
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = (hi - lo).length * 1.12
    scene.camera = camera
    light = bpy.data.objects.new("QA Key", bpy.data.lights.new("QA Key", "AREA"))
    scene.collection.objects.link(light)
    light.data.energy = 350 * size * size
    light.data.size = size * 2
    materials = {kind: render_material(kind) for kind in ("clay", "wire", "checker")}
    silhouettes, images = {}, []
    original_visibility = {obj: obj.hide_render for obj in scene.objects if obj.type == "MESH"}
    for obj in original_visibility:
        obj.hide_render = True
    def render(objects, label, kind, angle):
        back = forward * math.cos(angle) + right * math.sin(angle)
        camera.location = center + back * size * 3
        basis_right = up.cross(back).normalized()
        camera.rotation_euler = Matrix((basis_right, up, back)).transposed().to_euler()
        light.location = center + (back * 2 + up * 2 - basis_right) * size
        light.rotation_euler = (center - light.location).to_track_quat("-Z", "Y").to_euler()
        for obj in objects:
            obj.hide_render = False
        scene.view_layers[0].material_override = materials[kind]
        path = folder / (label + ".png")
        scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(str(path), check_existing=False)
        data = np.empty(len(image.pixels), dtype=np.float32)
        image.pixels.foreach_get(data)
        mask = data.reshape(-1, 4)[:, 3] > .5
        bpy.data.images.remove(image)
        for obj in objects:
            obj.hide_render = True
        images.append({"label": label, "file": str(path)})
        return mask
    try:
        for view in range(args.turntable_views):
            angle = 2 * math.pi * view / args.turntable_views
            reference = render(highs, f"high_{view:02}", "clay", angle)
            for lod, obj in enumerate(lods):
                mask = render([obj], f"lod{lod}_clay_{view:02}", "clay", angle)
                union = np.count_nonzero(mask | reference)
                iou = np.count_nonzero(mask & reference) / max(1, union)
                silhouettes.setdefault(lod, []).append(float(iou))
            render([lods[0]], f"lod0_wire_{view:02}", "wire", angle)
            render([lods[0]], f"lod0_checker_{view:02}", "checker", angle)
    finally:
        scene.view_layers[0].material_override = None
        for obj, hidden in original_visibility.items():
            obj.hide_render = hidden
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.objects.remove(light, do_unlink=True)
    return {"images": images, "silhouette": [{"lod": i, "minimum_iou": min(values), "views": values}
                                              for i, values in silhouettes.items()]}


def evaluate(report, args):
    issues = []
    def flag(code, message, severity="review"):
        issues.append({"code": code, "severity": severity, "message": message})
    if not report["bake"]["source_materials_present"]:
        flag("neutral_material", "Neutral source: create/apply final PBR textures before shipping.")
    if not args.size_meters:
        flag("scale_unverified", "Source units were assumed to be meters; verify the intended real-world dimensions.")
    if report["uv"]["metrics"]["coverage"] < args.min_uv_coverage:
        flag("uv_coverage", "UV atlas coverage is below the selected asset preset target.")
    if report["uv"]["metrics"]["islands_under_one_pixel"]:
        flag("subpixel_islands", "Some UV islands occupy under one texel; inspect checker previews and mip behavior.")
    if report["bake"]["basecolor_black_fraction"] > .99:
        flag("black_texture", "Base color is almost entirely black; verify whether the source is intentionally black.")
    if report["bake"]["runtime_rgba8_mip_estimate_mib"] > args.max_texture_mib:
        flag("texture_budget", "Estimated uncompressed runtime texture memory exceeds the preset budget.")
    if report.get("reduction_fallbacks"):
        flag("preserved_components", "Some components retained extra geometry because reduction would erase them.")
    for miss in report["bake"]["projection_miss"]:
        if miss["fraction"] > args.max_bake_miss:
            flag("bake_miss", f"LOD{miss['lod']} projection misses exceed tolerance; adjust cage or source/target geometry.")
    for lod in report["lods"]:
        quality = lod["quality"]
        if quality["invalid_uv_corners"] or quality["zero_area_uv_faces"] or quality.get("uv_overlap_pairs"):
            flag("lod_uv", f"LOD{lod['index']} UVs failed validation.", "error")
        if quality["boundary_edges"] or quality["overconnected_edges"] or quality["nonmanifold_vertices"]:
            flag("open_topology", f"LOD{lod['index']} contains open/non-manifold geometry.", "error" if args.strict else "review")
        if quality["degenerate_faces"]:
            flag("degenerate_geometry", f"LOD{lod['index']} has degenerate faces.", "error")
        metrics = lod["export_metrics"]
        if lod["index"] == 0 and args.faces and metrics["triangles"] > args.faces:
            flag("triangle_budget", "LOD0 exceeds the requested triangle budget.", "error")
        if metrics["vertices"] > args.max_vertices:
            flag("vertex_budget", f"LOD{lod['index']} exported vertex count exceeds budget.")
        if metrics["materials"] > args.max_materials:
            flag("material_budget", f"LOD{lod['index']} material count exceeds budget.")
        allowed_error = args.max_shape_error * (1 + lod["index"])
        if lod["shape_error"]["max_relative"] > allowed_error:
            flag("shape_error", f"LOD{lod['index']} sampled surface error exceeds tolerance.")
    for silhouette in report.get("previews", {}).get("silhouette", []):
        threshold = max(.7, .97 - .07 * silhouette["lod"])
        if silhouette["minimum_iou"] < threshold:
            flag("silhouette", f"LOD{silhouette['lod']} silhouette changed beyond the preview tolerance.")
    if args.skip_previews:
        flag("previews_skipped", "Visual previews and silhouette checks were skipped.")
    if args.engine in ("cyberpunk", "witcher3"):
        flag("redengine_native_import", "REDengine handoff needs native material, LOD and collision setup, cooking "
             "and in-game validation; see redengine/IMPORT.md.")
        if not args.red_template:
            flag("redengine_template", "Choose a compatible native .mesh/.w2mesh template or REDkit resource before integration.")
    for component in report["components"]:
        for action in component["region_actions"]:
            if "skipped" in action:
                flag("region_skipped", action["skipped"])
    flag("surface_review", "Review the intended shape: automatic smoothing/remeshing cannot identify every generated fold.", "info")
    return {"issues": issues, "technical_passed": not any(x["severity"] == "error" for x in issues),
            "review_required": any(x["severity"] != "info" for x in issues), "shipping_certified": False}


def write_gallery(report, folder):
    folder = Path(folder)
    escape = html.escape
    def relative_image(value):
        path = Path(value)
        return path.relative_to(folder).as_posix() if path.is_absolute() else path.as_posix()
    rows = []
    for lod in report["lods"]:
        m = lod["export_metrics"]
        rows.append(f"<tr><td>LOD{lod['index']}</td><td>{m['triangles']:,}</td><td>{m['vertices']:,}</td>"
                    f"<td>{m['materials']}</td><td>{lod['shape_error']['max_relative']:.3%}</td></tr>")
    issues = "".join("<li>" + escape(x["message"]) + "</li>" for x in report["quality"]["issues"])
    images = "".join(f'<figure><img loading="lazy" src="{escape(relative_image(x["file"]))}">'
                     f'<figcaption>{escape(x["label"])}</figcaption></figure>' for x in report.get("previews", {}).get("images", []))
    red_links = ('<p><a href="redengine/IMPORT.md">REDengine import instructions</a> · '
                 '<a href="redengine/handoff.json">Handoff manifest</a></p>') if report.get('redengine_handoff') else ''
    document = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{escape(report['name'])} asset review</title><style>body{{font:16px system-ui;background:#171b22;color:#edf1f6;
max-width:1400px;margin:auto;padding:32px}}table{{border-collapse:collapse}}td,th{{padding:10px 20px;border:1px solid #48515f}}
.gallery{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}}figure{{margin:0;background:#242b35}}
img{{width:100%}}figcaption{{padding:10px}}li{{margin:8px 0}}a{{color:#8bc4ff}}</style>
<h1>{escape(report['name'])}</h1><p>Engine preset: {escape(report['engine'])}. Review evidence, not a shipping certification.</p>
<table><tr><th>Mesh</th><th>Triangles</th><th>Export vertices</th><th>Materials</th><th>Max sampled error</th></tr>{''.join(rows)}</table>
<p>UV coverage: {report['uv']['metrics']['coverage']:.1%}; islands: {report['uv']['metrics']['islands']:,};
estimated runtime textures: {report['bake']['runtime_rgba8_mip_estimate_mib']:.1f} MiB (RGBA8 with mipmaps).</p>
<h2>Review flags</h2><ul>{issues}</ul><h2>Engine check</h2><pre>{escape(json.dumps(report.get('engine_check', {}), indent=2))}</pre>
{red_links}
<h2>Turntable, wireframe and checker views</h2><div class="gallery">{images}</div></html>'''
    path = folder / "review.html"
    path.write_text(document)
    return str(path)
