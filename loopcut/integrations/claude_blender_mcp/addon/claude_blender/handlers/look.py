"""look toolset: renders from fixed angles and numeric reports, so Claude can check its own work."""

import base64
import math
import os
import tempfile

import bmesh
import bpy
from mathutils import Euler, Vector

from . import CommandError, command

# Direction the camera sits in, relative to the target's center.
VIEWS = {
    "front": Vector((0, -1, 0)),
    "back": Vector((0, 1, 0)),
    "right": Vector((1, 0, 0)),
    "left": Vector((-1, 0, 0)),
    "top": Vector((0, 0, 1)),
    "bottom": Vector((0, 0, -1)),
    "iso": Vector((1, -1, 0.8)).normalized(),
}


def view_rotation(view):
    """Camera rotation for a named view. Its local +X is image right and +Y is image up."""
    if view == "top":
        # Looking down with world +Y up, so the model's front is at the bottom of the image.
        return Euler((0, 0, 0))
    if view == "bottom":
        return Euler((math.pi, 0, 0))
    # Camera looks down its -Z with local +Y up; keep world Z up.
    return (-VIEWS[view]).to_track_quat("-Z", "Y").to_euler()


def _get_object(name):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise CommandError(f"No object named {name!r}")
    return obj


def color_type(objs):
    """Workbench colour source that shows what the meshes really look like: image textures if
    their materials have any, else colour attributes if most of the surface has one, else
    material colours."""
    meshes = [o for o in objs if o.type == "MESH"]
    for o in meshes:
        for slot in o.material_slots:
            mat = slot.material
            if mat and mat.node_tree and any(n.type == "TEX_IMAGE" and n.image for n in mat.node_tree.nodes):
                return "TEXTURE"
    painted = sum(len(o.data.polygons) for o in meshes if o.data.color_attributes.active_color)
    if meshes and painted * 2 > sum(len(o.data.polygons) for o in meshes):
        return "VERTEX"
    return "MATERIAL"


def _bone_proxy(arm):
    """Armatures don't render, so build a temporary mesh with one octahedral bone shape per bone
    (in the current pose) to show where the joints are."""
    bm = bmesh.new()
    for pb in arm.pose.bones:
        head = arm.matrix_world @ pb.head
        tail = arm.matrix_world @ pb.tail
        axis = tail - head
        length = axis.length
        if length < 1e-6:
            continue
        d = axis / length
        side = d.cross(Vector((0, 0, 1)) if abs(d.z) < 0.9 else Vector((1, 0, 0))).normalized() * length * 0.1
        up = d.cross(side).normalized() * length * 0.1
        mid = head + axis * 0.15
        a = bm.verts.new(head)
        b = bm.verts.new(tail)
        ring = [bm.verts.new(mid + v) for v in (side, up, -side, -up)]
        for i in range(4):
            bm.faces.new((a, ring[i], ring[(i + 1) % 4]))
            bm.faces.new((b, ring[(i + 1) % 4], ring[i]))
    me = bpy.data.meshes.new("_claude_bones")
    bm.to_mesh(me)
    bm.free()
    mat = bpy.data.materials.new("_claude_bones")
    mat.diffuse_color = (0.1, 0.9, 0.3, 1.0)
    me.materials.append(mat)
    obj = bpy.data.objects.new("_claude_bones", me)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _targets(objects):
    if objects:
        return [_get_object(n) for n in objects]
    return [o for o in bpy.context.scene.objects if o.visible_get() and o.type in {"MESH", "CURVE", "SURFACE", "META", "FONT", "ARMATURE", "CURVES", "POINTCLOUD", "VOLUME"}]


def _bounds(objs):
    corners = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
    if not corners:
        raise CommandError("Nothing visible to frame")
    lo = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    hi = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    center = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 1e-3)
    return center, radius


@command("capture_views", "look")
def capture_views(views=("front", "right", "iso"), objects=None, resolution=512, shading="solid", isolate=False):
    """Render the scene or given objects from fixed angles with Workbench.

    shading: solid | wireframe | xray. Returns PNGs as base64.
    """
    bad = [v for v in views if v not in VIEWS]
    if bad:
        raise CommandError(f"Unknown views {bad}. Use: {sorted(VIEWS)}")
    if shading not in {"solid", "wireframe", "xray"}:
        raise CommandError("shading must be solid, wireframe or xray")
    resolution = max(64, min(int(resolution), 2048))
    scene = bpy.context.scene
    targets = _targets(objects)
    center, radius = _bounds(targets)

    render = scene.render
    shade = scene.display.shading
    saved = {
        "camera": scene.camera,
        "engine": render.engine,
        "res": (render.resolution_x, render.resolution_y, render.resolution_percentage),
        "filepath": render.filepath,
        "format": render.image_settings.file_format,
        "film_transparent": render.film_transparent,
        "light": shade.light,
        "color_type": shade.color_type,
        "show_xray": shade.show_xray,
        "show_cavity": shade.show_cavity,
        "show_object_outline": shade.show_object_outline,
    }
    hidden = []
    wire_mods = []
    proxies = [_bone_proxy(o) for o in targets if o.type == "ARMATURE"]
    cam_data = bpy.data.cameras.new("_claude_capture_cam")
    cam = bpy.data.objects.new("_claude_capture_cam", cam_data)
    scene.collection.objects.link(cam)
    tmpdir = tempfile.mkdtemp(prefix="claude_capture_")
    images = []
    try:
        render.engine = "BLENDER_WORKBENCH"
        render.resolution_x = render.resolution_y = resolution
        render.resolution_percentage = 100
        render.image_settings.file_format = "PNG"
        render.film_transparent = False
        shade.light = "STUDIO"
        shade.color_type = color_type(targets)
        shade.show_cavity = True
        shade.show_object_outline = True
        shade.show_xray = shading == "xray"
        if shading == "wireframe":
            for o in targets:
                if o.type == "MESH":
                    wire_mods.append((o, o.modifiers.new("_claude_wire", "WIREFRAME")))
            for o, m in wire_mods:
                # Replace faces with thin struts so the edge flow itself is what renders.
                m.thickness = radius * 0.006
                m.use_replace = True
                m.use_even_offset = False
        if isolate and objects:
            keep = {o.name for o in targets} | {o.name for o in proxies}
            for o in scene.objects:
                if o.name not in keep and o is not cam and not o.hide_render:
                    o.hide_render = True
                    hidden.append(o)

        cam_data.lens = 50
        fov = cam_data.angle
        distance = radius / math.sin(fov / 2) * 1.05
        cam_data.clip_start = max(distance - radius * 2, 0.001)
        cam_data.clip_end = distance + radius * 2
        scene.camera = cam

        for view in views:
            direction = VIEWS[view]
            cam.location = center + direction * distance
            cam.rotation_euler = view_rotation(view)
            path = os.path.join(tmpdir, f"{view}.png")
            render.filepath = path
            bpy.ops.render.render(write_still=True)
            with open(path, "rb") as fh:
                images.append({"view": view, "png_base64": base64.b64encode(fh.read()).decode("ascii")})
    finally:
        for o in proxies:
            mat = o.data.materials[0]
            me = o.data
            bpy.data.objects.remove(o)
            bpy.data.meshes.remove(me)
            bpy.data.materials.remove(mat)
        for o, m in wire_mods:
            o.modifiers.remove(m)
        for o in hidden:
            o.hide_render = False
        bpy.data.objects.remove(cam)
        bpy.data.cameras.remove(cam_data)
        scene.camera = saved["camera"]
        render.engine = saved["engine"]
        render.resolution_x, render.resolution_y, render.resolution_percentage = saved["res"]
        render.filepath = saved["filepath"]
        render.image_settings.file_format = saved["format"]
        render.film_transparent = saved["film_transparent"]
        shade.light = saved["light"]
        shade.color_type = saved["color_type"]
        shade.show_xray = saved["show_xray"]
        shade.show_cavity = saved["show_cavity"]
        shade.show_object_outline = saved["show_object_outline"]
    return {
        "objects": [o.name for o in targets],
        "center": [round(v, 4) for v in center],
        "radius": round(radius, 4),
        "images": images,
    }


@command("object_details", "look")
def object_details(name):
    """Transforms, modifiers with settings, materials, vertex groups, shape keys, bones, animation."""
    obj = _get_object(name)
    mods = []
    for m in obj.modifiers:
        settings = {}
        for prop in m.bl_rna.properties:
            if prop.identifier in {"rna_type", "name", "type"} or prop.type in {"POINTER", "COLLECTION"}:
                continue
            settings[prop.identifier] = getattr(m, prop.identifier)
        mods.append({"name": m.name, "type": m.type, "show_viewport": m.show_viewport, "settings": settings})
    info = {
        "name": obj.name,
        "type": obj.type,
        "data": obj.data.name if obj.data else None,
        "parent": obj.parent.name if obj.parent else None,
        "parent_type": obj.parent_type,
        "parent_bone": obj.parent_bone or None,
        "children": [c.name for c in obj.children],
        "location": list(obj.location),
        "rotation_mode": obj.rotation_mode,
        "rotation_euler": list(obj.rotation_euler),
        "scale": list(obj.scale),
        "dimensions": list(obj.dimensions),
        "scale_applied": all(abs(s - 1) < 1e-6 for s in obj.scale),
        "modifiers": mods,
        "materials": [s.material.name if s.material else None for s in obj.material_slots],
        "vertex_groups": [g.name for g in obj.vertex_groups],
        "constraints": [f"{c.name} ({c.type})" for c in obj.constraints],
    }
    if obj.type == "MESH":
        me = obj.data
        info["shape_keys"] = [k.name for k in me.shape_keys.key_blocks] if me.shape_keys else []
        info["uv_layers"] = [uv.name for uv in me.uv_layers]
    if obj.type == "ARMATURE":
        info["bones"] = [
            {"name": b.name, "parent": b.parent.name if b.parent else None, "deform": b.use_deform,
             "head": list(b.head_local), "tail": list(b.tail_local)}
            for b in obj.data.bones
        ]
    ad = obj.animation_data
    if ad:
        info["animation"] = {
            "action": ad.action.name if ad.action else None,
            "frame_range": list(ad.action.frame_range) if ad.action else None,
            "nla_tracks": [t.name for t in ad.nla_tracks],
        }
    return info


@command("mesh_report", "look")
def mesh_report(name, evaluated=True):
    """Topology and game-readiness numbers for one mesh (with modifiers applied when evaluated)."""
    obj = _get_object(name)
    if obj.type != "MESH":
        raise CommandError(f"{name!r} is a {obj.type}, not a MESH")
    if evaluated:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph)
        me = eval_obj.to_mesh()
    else:
        me = obj.data
    try:
        bm = bmesh.new()
        bm.from_mesh(me)
        faces = bm.faces
        tris = sum(len(f.verts) - 2 for f in faces)
        report = {
            "name": obj.name,
            "evaluated": evaluated,
            "verts": len(bm.verts),
            "edges": len(bm.edges),
            "faces": len(faces),
            "tris": tris,
            "triangles": sum(1 for f in faces if len(f.verts) == 3),
            "quads": sum(1 for f in faces if len(f.verts) == 4),
            "ngons": sum(1 for f in faces if len(f.verts) > 4),
            "non_manifold_edges": sum(1 for e in bm.edges if not e.is_manifold and not e.is_boundary),
            "boundary_edges": sum(1 for e in bm.edges if e.is_boundary),
            "loose_verts": sum(1 for v in bm.verts if not v.link_edges),
            "loose_edges": sum(1 for e in bm.edges if not e.link_faces),
            "zero_area_faces": sum(1 for f in faces if f.calc_area() < 1e-10),
            "uv_layers": [uv.name for uv in me.uv_layers],
            "material_slots": len(obj.material_slots),
            "scale_applied": all(abs(s - 1) < 1e-6 for s in obj.scale),
            "dimensions": list(obj.dimensions),
        }
        bm.free()
    finally:
        if evaluated:
            eval_obj.to_mesh_clear()
    return report
