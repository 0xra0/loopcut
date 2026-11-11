"""Local eval tasks: a prompt, a scene to start from, a check that reads the evaluated scene, and a reference
solution. selfcheck.py proves every check fails on the untouched scene and passes on its reference; run.py sends
each prompt to the configured model (Strata on this computer) and records the check.

Runs inside Blender 5.2 (bpy). OUT is the task's own output folder (run_task.py sets it).
"""
import json
import math
import os
import struct

import bpy
from mathutils import Vector

OUT = os.environ.get("LOCAL_EVAL_OUT", "/mnt/ssd/work/loopcut-local/eval-out")


# ------------------------------------------------------------------ helpers

def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.actions, bpy.data.node_groups):
        for block in list(coll):
            if block.users == 0:
                coll.remove(block)


def mesh_object(name, op, **kw):
    getattr(bpy.ops.mesh, op)(**kw)
    o = bpy.context.object
    o.name = o.data.name = name
    return o


def get(name):
    return bpy.data.objects.get(name)


def evaluated(o):
    return o.evaluated_get(bpy.context.evaluated_depsgraph_get())


def world_bounds(o):
    e = evaluated(o)
    pts = [e.matrix_world @ Vector(c) for c in e.bound_box]
    return (Vector([min(p[i] for p in pts) for i in range(3)]), Vector([max(p[i] for p in pts) for i in range(3)]))


def tri_count(o, use_eval=True):
    src = evaluated(o) if use_eval else o
    mesh = src.to_mesh()
    mesh.calc_loop_triangles()
    n = len(mesh.loop_triangles)
    src.to_mesh_clear()
    return n


def principled(o):
    mat = o.active_material if o else None
    if not mat or not mat.use_nodes:
        return None
    return next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)


def near(a, b, tol):
    return abs(a - b) <= tol


def fmt(v):
    return "(" + ", ".join(f"{x:.3f}" for x in v) + ")"


def select_only(o):
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.context.view_layer.objects.active = o


# ------------------------------------------------------------------ tasks

def red_sphere_setup():
    clear()
    mesh_object("Cube", "primitive_cube_add", size=2, location=(0, 0, 0))


def red_sphere_check():
    cube = get("Cube")
    spheres = [o for o in bpy.data.objects if o.type == "MESH" and o is not cube and len(o.data.vertices) > 100]
    if not cube or len(spheres) != 1:
        return False, f"want the cube and one sphere; got {[o.name for o in bpy.data.objects]}"
    s = spheres[0]
    lo, hi = world_bounds(s)
    size = hi - lo
    centre = (lo + hi) / 2
    p = principled(s)
    colour = tuple(p.inputs["Base Color"].default_value)[:3] if p else None
    ok = (all(near(d, 1.0, 0.03) for d in size) and near(lo.z, 1.0, 0.02) and near(centre.x, 0, 0.02)
          and near(centre.y, 0, 0.02) and colour is not None and colour[0] > 0.8 and max(colour[1:]) < 0.1)
    return ok, f"size {fmt(size)}, bottom {lo.z:.3f}, centre {fmt(centre)}, base colour {colour}"


def red_sphere_reference():
    s = mesh_object("Ball", "primitive_uv_sphere_add", radius=0.5, location=(0, 0, 1.5))
    mat = bpy.data.materials.new("Red")
    mat.use_nodes = True
    principled(type("o", (), {"active_material": mat})).inputs["Base Color"].default_value = (1, 0, 0, 1)
    s.data.materials.append(mat)


def steel_setup():
    clear()
    mesh_object("Plate", "primitive_cube_add", size=1)


def steel_check():
    p = principled(get("Plate"))
    if not p:
        return False, "Plate has no node material with a Principled BSDF"
    metal, rough = p.inputs["Metallic"].default_value, p.inputs["Roughness"].default_value
    colour = tuple(p.inputs["Base Color"].default_value)[:3]
    ok = near(metal, 1, 0.01) and near(rough, 0.35, 0.03) and all(0.02 <= c <= 0.1 for c in colour)
    return ok, f"metallic {metal:.2f}, roughness {rough:.2f}, base colour {fmt(colour)}"


def steel_reference():
    mat = bpy.data.materials.new("Steel")
    mat.use_nodes = True
    get("Plate").data.materials.append(mat)
    p = principled(get("Plate"))
    p.inputs["Metallic"].default_value = 1
    p.inputs["Roughness"].default_value = 0.35
    p.inputs["Base Color"].default_value = (0.05, 0.05, 0.05, 1)


def stack_setup():
    clear()
    hull = mesh_object("Hull", "primitive_cube_add", size=1, location=(0.6, 0, 0))
    hull.scale = (1, 0.5, 0.3)


def stack_check():
    hull = get("Hull")
    mods = [(m.type, m) for m in hull.modifiers] if hull else []
    kinds = [k for k, _ in mods]
    if kinds != ["MIRROR", "BEVEL"]:
        return False, f"modifier stack {kinds}"
    mirror, bevel = mods[0][1], mods[1][1]
    ok = mirror.use_axis[0] and near(bevel.width, 0.02, 0.001) and bevel.segments == 3
    return ok, f"mirror x {mirror.use_axis[0]}, bevel width {bevel.width:.4f}, segments {bevel.segments}"


def stack_reference():
    hull = get("Hull")
    hull.modifiers.new("Mirror", "MIRROR").use_axis[0] = True
    bevel = hull.modifiers.new("Bevel", "BEVEL")
    bevel.width, bevel.segments = 0.02, 3


def budget_setup():
    clear()
    rock = mesh_object("Rock", "primitive_ico_sphere_add", subdivisions=6, radius=1)  # 20,480 triangles
    rock.scale = (1.2, 1, 0.8)
    bpy.ops.object.transform_apply(scale=True)


def budget_check():
    rock = get("Rock")
    if not rock:
        return False, "Rock is gone"
    n = tri_count(rock, use_eval=False)
    lo, hi = world_bounds(rock)
    size = hi - lo
    ok = 2500 <= n <= 5000 and not rock.modifiers and all(near(a, b, 0.05) for a, b in zip(size, (2.4, 2, 1.6)))
    return ok, f"{n} triangles in the mesh itself, modifiers {[m.type for m in rock.modifiers]}, size {fmt(size)}"


def budget_reference():
    rock = get("Rock")
    select_only(rock)
    m = rock.modifiers.new("Decimate", "DECIMATE")
    m.ratio = 4500 / tri_count(rock)
    bpy.ops.object.modifier_apply(modifier=m.name)


def cleanup_setup():
    clear()
    import bmesh
    mesh = bpy.data.meshes.new("Scan")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1)
    bmesh.ops.create_cube(bm, size=1)  # The same cube again: every vertex doubled.
    for x in range(5):
        bm.verts.new((3 + x * 0.1, 0, 0))  # Loose vertices.
    a, b = bm.verts.new((0, 3, 0)), bm.verts.new((0, 3.5, 0))
    bm.edges.new((a, b))  # A loose edge.
    bm.to_mesh(mesh)
    bm.free()
    o = bpy.data.objects.new("Scan", mesh)
    bpy.context.collection.objects.link(o)


def cleanup_check():
    o = get("Scan")
    if not o:
        return False, "Scan is gone"
    me = o.data
    keys = {tuple(round(c, 5) for c in v.co) for v in me.vertices}
    used = {i for p in me.polygons for i in p.vertices}
    loose = len(me.vertices) - len(used)
    ok = len(keys) == len(me.vertices) == 8 and loose == 0 and len(me.polygons) >= 6
    return ok, f"{len(me.vertices)} vertices ({len(keys)} distinct), {loose} not on a face, {len(me.polygons)} faces"


def cleanup_reference():
    o = get("Scan")
    select_only(o)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=0.0001)
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.delete_loose()
    bpy.ops.object.mode_set(mode="OBJECT")
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(o.data)
    faces = list(bm.faces)
    seen = set()
    dupes = [f for f in faces if (k := frozenset(v.index for v in f.verts)) in seen or seen.add(k)]
    bmesh.ops.delete(bm, geom=dupes, context="FACES_ONLY")
    bm.to_mesh(o.data)
    bm.free()


def base_pivot_setup():
    clear()
    mesh_object("Statue", "primitive_cylinder_add", radius=0.4, depth=2, location=(2, 1, 1.5))


def base_pivot_check():
    o = get("Statue")
    if not o:
        return False, "Statue is gone"
    lo, hi = world_bounds(o)
    local_min_z = min(v.co.z for v in o.data.vertices) * o.matrix_world.to_scale().z
    ok = (o.location.length < 0.005 and near(lo.z, 0, 0.005) and near((lo.x + hi.x) / 2, 0, 0.005)
          and near((lo.y + hi.y) / 2, 0, 0.005) and near(local_min_z, 0, 0.005) and near(hi.z - lo.z, 2, 0.01))
    return ok, f"origin {fmt(o.location)}, world bounds {fmt(lo)}..{fmt(hi)}, lowest vertex above origin {local_min_z:.3f}"


def base_pivot_reference():
    o = get("Statue")
    lo, hi = world_bounds(o)
    bpy.context.scene.cursor.location = ((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z)
    select_only(o)
    bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
    o.location = (0, 0, 0)


def real_size_setup():
    clear()
    o = mesh_object("Sword", "primitive_cube_add", size=1)
    o.scale = (1, 0.1, 5)


def real_size_check():
    o = get("Sword")
    if not o:
        return False, "Sword is gone"
    lo, hi = world_bounds(o)
    size = hi - lo
    ok = (near(size.z, 1.28, 0.01) and all(near(s, 1, 1e-4) for s in o.scale)
          and near(size.x / size.z, 0.2, 0.01) and near(size.y / size.z, 0.02, 0.005))
    return ok, f"size {fmt(size)}, scale {fmt(o.scale)}"


def real_size_reference():
    o = get("Sword")
    f = 1.28 / 5
    o.scale = (f, 0.1 * f, 5 * f)
    select_only(o)
    bpy.ops.object.transform_apply(scale=True)


def uv_setup():
    clear()
    o = mesh_object("Crate", "primitive_cube_add", size=1)
    while o.data.uv_layers:
        o.data.uv_layers.remove(o.data.uv_layers[0])


def _uv_overlap(o, cells=256):
    """Count grid cells covered by more than one UV face (sampled at cell centres) and the out-of-range UVs."""
    me = o.data
    uv = me.uv_layers.active.data
    grid = [0] * (cells * cells)
    outside = sum(1 for d in uv if not (-1e-4 <= d.uv.x <= 1 + 1e-4 and -1e-4 <= d.uv.y <= 1 + 1e-4))
    me.calc_loop_triangles()
    for tri in me.loop_triangles:
        (ax, ay), (bx, by), (cx, cy) = (uv[i].uv for i in tri.loops)
        den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(den) < 1e-12:
            continue
        x0, x1 = max(0, int(min(ax, bx, cx) * cells)), min(cells - 1, int(max(ax, bx, cx) * cells))
        y0, y1 = max(0, int(min(ay, by, cy) * cells)), min(cells - 1, int(max(ay, by, cy) * cells))
        for gy in range(y0, y1 + 1):
            for gx in range(x0, x1 + 1):
                px, py = (gx + 0.5) / cells, (gy + 0.5) / cells
                w1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / den
                w2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / den
                if w1 >= 0 and w2 >= 0 and w1 + w2 <= 1:
                    grid[gy * cells + gx] += 1
    return sum(1 for g in grid if g > 1), sum(1 for g in grid if g), outside


def uv_check():
    o = get("Crate")
    if not o or not o.data.uv_layers:
        return False, "Crate has no UV map"
    overlap, covered, outside = _uv_overlap(o)
    ok = overlap <= 2 and outside == 0 and covered > 0.05 * 256 * 256
    return ok, f"{overlap} grid cells overlap, {covered} covered, {outside} UVs outside 0-1"


def uv_reference():
    o = get("Crate")
    o.data.uv_layers.new(name="UVMap")
    select_only(o)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(island_margin=0.02)
    bpy.ops.object.mode_set(mode="OBJECT")


def lods_setup():
    clear()
    mesh_object("Barrel", "primitive_cylinder_add", vertices=64, radius=0.5, depth=1.2)
    o = get("Barrel")
    select_only(o)
    m = o.modifiers.new("Sub", "SUBSURF")
    m.levels = 2
    bpy.ops.object.modifier_apply(modifier=m.name)


def lods_check():
    base = get("Barrel")
    if not base:
        return False, "Barrel is gone"
    n0 = tri_count(base)
    found, ok = [], n0 == lods_check.base_tris
    for i, ratio in ((1, 0.5), (2, 0.25), (3, 0.125)):
        o = get(f"Barrel_LOD{i}")
        n = tri_count(o) if o else 0
        found.append(f"LOD{i} {n}")
        ok = ok and o is not None and o.data is not base.data and abs(n / n0 - ratio) <= ratio * 0.25
    return ok, f"Barrel {n0} (was {lods_check.base_tris}); " + ", ".join(found)


def lods_reference():
    base = get("Barrel")
    for i, ratio in ((1, 0.5), (2, 0.25), (3, 0.125)):
        copy = base.copy()
        copy.data = base.data.copy()
        copy.name = copy.data.name = f"Barrel_LOD{i}"
        bpy.context.collection.objects.link(copy)
        copy.modifiers.new("Decimate", "DECIMATE").ratio = ratio


def export_setup():
    clear()
    mesh_object("Gem", "primitive_ico_sphere_add", subdivisions=1, radius=0.1)
    get("Gem").modifiers.new("Bevel", "BEVEL").width = 0.005
    mesh_object("Table", "primitive_cube_add", size=1, location=(0, 0, -1))
    export_setup.path = os.path.join(OUT, "gem.glb")
    if os.path.exists(export_setup.path):
        os.remove(export_setup.path)


def _glb_json(path):
    with open(path, "rb") as f:
        magic, _, _ = struct.unpack("<4sII", f.read(12))
        length, kind = struct.unpack("<II", f.read(8))
        return magic, json.loads(f.read(length)) if kind == 0x4E4F534A else {}


def export_check():
    path = os.path.join(OUT, "gem.glb")
    if not os.path.isfile(path):
        return False, f"no file at {path}"
    magic, doc = _glb_json(path)
    names = [n.get("name") for n in doc.get("nodes", [])]
    gem_tris = tri_count(get("Gem")) if get("Gem") else 0
    accessor = doc["accessors"][doc["meshes"][0]["primitives"][0]["indices"]] if doc.get("meshes") else {"count": 0}
    ok = magic == b"glTF" and names == ["Gem"] and accessor["count"] == gem_tris * 3 and gem_tris > 80
    return ok, f"magic {magic}, nodes {names}, exported triangles {accessor['count'] // 3}, Gem evaluated {gem_tris}"


def export_reference():
    select_only(get("Gem"))
    bpy.ops.export_scene.gltf(filepath=os.path.join(OUT, "gem.glb"), export_format="GLB", use_selection=True,
                              export_apply=True)


def door_setup():
    clear()
    mesh_object("Door", "primitive_cube_add", size=1)
    bpy.context.scene.frame_end = 250


def door_check():
    from bpy_extras import anim_utils
    o, scene = get("Door"), bpy.context.scene
    ad = o.animation_data if o else None
    if not ad or not ad.action:
        return False, "Door has no animation"
    bag = anim_utils.action_get_channelbag_for_slot(ad.action, ad.action_slot)
    curve = bag.fcurves.find("rotation_euler", index=2) if bag else None
    if not curve:
        return False, "no Z rotation curve"
    a, b = curve.evaluate(1), curve.evaluate(48)
    ok = near(a, 0, 0.01) and near(abs(b), math.pi / 2, 0.01) and scene.frame_end == 48
    return ok, f"Z at 1: {math.degrees(a):.1f} deg, at 48: {math.degrees(b):.1f} deg, frame_end {scene.frame_end}"


def door_reference():
    o = get("Door")
    o.rotation_euler.z = 0
    o.keyframe_insert("rotation_euler", index=2, frame=1)
    o.rotation_euler.z = math.pi / 2
    o.keyframe_insert("rotation_euler", index=2, frame=48)
    bpy.context.scene.frame_end = 48


def scatter_setup():
    clear()
    mesh_object("Ground", "primitive_plane_add", size=10)
    mesh_object("Pebble", "primitive_ico_sphere_add", subdivisions=1, radius=0.05, location=(0, 0, 3))


def scatter_check():
    ground, pebble = get("Ground"), get("Pebble")
    if not ground or not pebble:
        return False, "Ground or Pebble is gone"
    kinds = [m.type for m in ground.modifiers]
    geometry = evaluated(ground).evaluated_geometry()
    points = geometry.instances_pointcloud()
    count = len(points.points) if points else 0
    refs = [getattr(r, "name", "") for r in geometry.instance_references()]
    mesh = geometry.mesh
    ground_faces = len(mesh.polygons) if mesh else 0
    ok = "NODES" in kinds and 190 <= count <= 210 and "Pebble" in refs and ground_faces >= 1
    return ok, f"modifiers {kinds}, {count} instances of {refs}, ground faces {ground_faces}"


def scatter_reference():
    ground, pebble = get("Ground"), get("Pebble")
    tree = bpy.data.node_groups.new("Scatter", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    n = tree.nodes
    gin, gout = n.new("NodeGroupInput"), n.new("NodeGroupOutput")
    dist = n.new("GeometryNodeDistributePointsOnFaces")
    dist.distribute_method = "RANDOM"
    dist.inputs["Density"].default_value = 2.0  # 100 m2 x 2 = about 200
    dist.inputs["Seed"].default_value = 7
    info = n.new("GeometryNodeObjectInfo")
    info.inputs["Object"].default_value = pebble
    inst = n.new("GeometryNodeInstanceOnPoints")
    join = n.new("GeometryNodeJoinGeometry")
    links = tree.links
    links.new(gin.outputs[0], dist.inputs["Mesh"])
    links.new(dist.outputs["Points"], inst.inputs["Points"])
    links.new(info.outputs["Geometry"], inst.inputs["Instance"])
    links.new(gin.outputs[0], join.inputs[0])
    links.new(inst.outputs[0], join.inputs[0])
    links.new(join.outputs[0], gout.inputs[0])
    ground.modifiers.new("Scatter", "NODES").node_group = tree
    # The density is random: settle the count by picking the seed that gives 200 +- 10.
    for seed in range(200):
        dist.inputs["Seed"].default_value = seed
        if scatter_check()[0]:
            return


def sword_setup():
    clear()


def sword_check():
    o = get("Longsword")
    meshes = [x for x in bpy.data.objects if x.type == "MESH"]
    if not o or o.type != "MESH":
        return False, f"no mesh object named Longsword; objects {[x.name for x in bpy.data.objects]}"
    lo, hi = world_bounds(o)
    size = hi - lo
    origin_t = (o.matrix_world.translation.z - lo.z) / size.z if size.z else -1
    widest = max(size.x, size.y)
    ok = (near(size.z, 1.2, 0.05) and near(widest, 0.25, 0.06) and 0.03 <= origin_t <= 0.3
          and len(o.data.vertices) > 50 and len(meshes) == 1)
    return ok, (f"size {fmt(size)}, origin at {origin_t:.0%} of the height, {len(o.data.vertices)} vertices, "
                f"{len(meshes)} mesh objects")


def sword_reference():
    import bmesh
    bm = bmesh.new()
    def box(cx, cy, cz, sx, sy, sz):
        geom = bmesh.ops.create_cube(bm, size=1)["verts"]
        bmesh.ops.scale(bm, vec=(sx, sy, sz), verts=geom)
        bmesh.ops.translate(bm, vec=(cx, cy, cz), verts=geom)
    box(0, 0, 0.0, 0.05, 0.05, 0.06)      # pommel, z -0.03..0.03
    box(0, 0, 0.11, 0.035, 0.035, 0.16)   # grip, 0.03..0.19
    box(0, 0, 0.205, 0.25, 0.04, 0.03)    # crossguard, 0.19..0.22
    blade = bmesh.ops.create_cone(bm, cap_ends=True, segments=8, radius1=0.03, radius2=0.002, depth=0.95)["verts"]
    bmesh.ops.scale(bm, vec=(1, 0.2, 1), verts=blade)
    bmesh.ops.translate(bm, vec=(0, 0, 0.695), verts=blade)
    bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1)
    mesh = bpy.data.meshes.new("Longsword")
    bm.to_mesh(mesh)
    bm.free()
    o = bpy.data.objects.new("Longsword", mesh)
    bpy.context.collection.objects.link(o)
    for v in mesh.vertices:
        v.co.z -= 0.11 - 0.03  # Origin at the middle of the grip: z 0.11 in the boxes above.
    o.location.z = 0.08


def pixal3d_armor_setup():
    clear()
    o = mesh_object("PixalArmor", "primitive_cube_add", size=1.0)
    o.scale = (0.5, 0.2, 0.8)
    bpy.ops.object.transform_apply(scale=True)
    # Open a face so mesh has non-manifold boundary edges
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')
    if o.data.polygons:
        o.data.polygons[0].select = True
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.delete(type='ONLY_FACE')
    bpy.ops.object.mode_set(mode='OBJECT')


def pixal3d_armor_check():
    o = get("PixalArmor")
    if not o or o.type != "MESH":
        return False, "no PixalArmor mesh"
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(o.data)
    non_manifold = sum(1 for e in bm.edges if not e.is_manifold)
    bm.free()
    dims = o.dimensions
    min_dim = min(dims)
    ok = (non_manifold == 0 and min_dim >= 0.05)
    return ok, f"{non_manifold} non-manifold edges, min dimension {min_dim:.3f}m"


def pixal3d_armor_reference():
    o = get("PixalArmor")
    if o:
        bpy.ops.object.select_all(action='DESELECT')
        o.select_set(True)
        bpy.context.view_layer.objects.active = o
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        if hasattr(bpy.ops.mesh, "fill_holes"):
            bpy.ops.mesh.fill_holes(sides=4)
        bpy.ops.object.mode_set(mode='OBJECT')


def pixal3d_islands_setup():
    clear()
    mesh_object("PixalScan", "primitive_cylinder_add", radius=0.4, depth=1.0)
    bpy.ops.mesh.primitive_cube_add(size=0.05, location=(1.0, 1.0, 1.0))
    debris1 = bpy.context.object
    debris1.name = "Debris1"


def pixal3d_islands_check():
    o = get("PixalScan")
    if not o or o.type != "MESH":
        return False, "no PixalScan mesh"
    debris = get("Debris1")
    ok = (debris is None or debris.name not in bpy.data.objects)
    return ok, f"debris present: {debris is not None}"


def pixal3d_islands_reference():
    d = get("Debris1")
    if d:
        bpy.data.objects.remove(d, do_unlink=True)


TASKS = {
    "red_sphere": ("Add a UV sphere of radius 0.5 m resting on top of the cube, centred on it, with a pure red "
                   "material (red base colour).", red_sphere_setup, red_sphere_check, red_sphere_reference),
    "steel_material": ("Give Plate a dark brushed steel material: metallic 1, roughness 0.35, base colour dark grey "
                       "(about 0.05).", steel_setup, steel_check, steel_reference),
    "modifier_stack": ("Add a Mirror modifier on X to Hull, and after it a Bevel modifier with width 0.02 m and 3 "
                       "segments.", stack_setup, stack_check, stack_reference),
    "triangle_budget": ("Reduce Rock to at most 5000 triangles, keeping its shape, and apply it so the mesh itself has "
                        "the lower count.", budget_setup, budget_check, budget_reference),
    "mesh_cleanup": ("Clean up Scan: merge vertices closer than 0.0001 m, remove the duplicate faces that leaves, and "
                     "delete the loose vertices and edges.", cleanup_setup, cleanup_check, cleanup_reference),
    "base_pivot": ("Set Statue's origin to the centre of its base (the bottom middle), and move Statue so that point "
                   "sits at the world origin.", base_pivot_setup, base_pivot_check, base_pivot_reference),
    "real_size": ("Scale Sword uniformly so it is 1.28 m tall, then apply the scale.",
                  real_size_setup, real_size_check, real_size_reference),
    "uv_unwrap": ("Give Crate a UV map: every face unwrapped, no overlapping islands, everything inside 0-1.",
                  uv_setup, uv_check, uv_reference),
    "lods": ("Make three LOD copies of Barrel named Barrel_LOD1, Barrel_LOD2 and Barrel_LOD3 with about 50%, 25% and "
             "12.5% of its triangles. Leave Barrel itself unchanged.", lods_setup, lods_check, lods_reference),
    "export_glb": ("Export only Gem, with its modifiers applied, as a binary glTF file to {OUT}/gem.glb.",
                   export_setup, export_check, export_reference),
    "door_keys": ("Animate Door rotating 90 degrees around Z from frame 1 to frame 48, and make the scene end at "
                  "frame 48.", door_setup, door_check, door_reference),
    "gn_scatter": ("With a geometry nodes modifier on Ground, scatter about 200 instances of Pebble over its surface "
                   "(random positions, fixed seed), keeping the ground itself visible.",
                   scatter_setup, scatter_check, scatter_reference),
    "longsword": ("Model a simple longsword as one mesh object named Longsword: blade along +Z, total length 1.2 m, a "
                  "crossguard about 0.25 m wide, a grip and a pommel, origin at the middle of the grip.",
                  sword_setup, sword_check, sword_reference),
    "pixal3d_repair": ("Refine PixalArmor: eliminate all non-manifold edges, preserve shell thickness (at least 0.05 m), and ensure symmetry across X.",
                       pixal3d_armor_setup, pixal3d_armor_check, pixal3d_armor_reference),
    "pixal3d_islands": ("Clean PixalScan: remove floating disconnected debris pieces (such as Debris1) so only the main object remains without geometry damage.",
                        pixal3d_islands_setup, pixal3d_islands_check, pixal3d_islands_reference),
}
lods_check.base_tris = 0


def setup(task_id):
    _, setup_fn, _, _ = TASKS[task_id]
    os.makedirs(OUT, exist_ok=True)
    setup_fn()
    if task_id == "lods":
        lods_check.base_tris = tri_count(get("Barrel"))
    bpy.context.view_layer.update()


def prompt(task_id):
    return TASKS[task_id][0].replace("{OUT}", OUT)


def check(task_id):
    bpy.context.view_layer.update()
    try:
        return TASKS[task_id][2]()
    except Exception as ex:  # noqa: BLE001 - a check that crashes on the agent's scene is a failed task, said why.
        return False, f"check raised {type(ex).__name__}: {ex}"


def reference(task_id):
    TASKS[task_id][3]()
