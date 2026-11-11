"""Blender 4.2+ / 5.x static-prop cleanup, Smart UV and PBR transfer.

blender -b --factory-startup --python-exit-code 1 --python prep_mesh.py -- input.glb output.glb --faces 40000
Use --materials discard before UltraTex; --unwrap keep after final texturing.
Full repair is conservative: no proximity-ray deletion of thin surfaces, no forced hole closure.
"""
from collections import Counter, defaultdict
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import bpy
import bmesh
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mesh_config import parse, resolve_source_up  # noqa: E402
from uv_quality import area, overlaps  # noqa: E402


def activate(obj):
    for selected in bpy.context.selected_objects:
        selected.select_set(False)
    if obj.hide_get():
        obj.hide_set(False)
    if obj.hide_viewport:
        obj.hide_viewport = False
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def import_mesh(src, object_name=None, visible_only=False, source_up="auto", yaw=0):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ext = Path(src).suffix.lower()
    source_up = resolve_source_up(src, source_up)
    print("PREP source-up", source_up, flush=True)
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=src)
    elif ext == ".blend":
        # Append objects only; do not run a supplied file's scripts or replace the UI.
        with bpy.data.libraries.load(src, link=False) as (source, target):
            if object_name and object_name not in source.objects:
                raise ValueError("Source object not found: " + object_name)
            target.objects = [object_name] if object_name else source.objects
        for obj in target.objects:
            if obj and obj.type == "MESH" and (object_name or not visible_only or not obj.hide_render):
                bpy.context.collection.objects.link(obj)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=src)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=src)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=src)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=src)
    else:
        raise ValueError(f"Unsupported input: {ext}")
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not meshes:
        raise ValueError("No mesh objects found")
    # Appended objects need dependency-graph evaluation before matrix_world is
    # read. Snapshot every world matrix before detaching any parent objects.
    bpy.context.view_layer.update()
    world_transforms = {obj: obj.matrix_world.copy() for obj in meshes}
    if source_up == "z":
        # Pixal3D multiview GLBs store Z-up geometry in Y-up glTF, so they import lying on their back.
        from mathutils import Matrix
        stand = Matrix.Rotation(math.radians(90), 4, "X")
        world_transforms = {obj: stand @ world for obj, world in world_transforms.items()}
    if yaw:
        # After standing up, Blender's +Z is the model's up axis, so facing is a turn about world Z.
        from mathutils import Matrix
        turn = Matrix.Rotation(math.radians(yaw), 4, "Z")
        world_transforms = {obj: turn @ world for obj, world in world_transforms.items()}
    print("PREP yaw", yaw, flush=True)
    for obj in meshes:
        if obj.data.shape_keys or obj.animation_data or any(m.type == "ARMATURE" for m in obj.modifiers):
            raise ValueError(f"{obj.name}: animated/skinned assets need a separate pipeline")
        activate(obj)
        world = world_transforms[obj]
        obj.parent = None
        obj.matrix_world = world
        obj.data = obj.data.copy()
        for mod in list(obj.modifiers):
            bpy.ops.object.modifier_apply(modifier=mod.name)
        # Standardize UV names before join, preserving explicit material references.
        if obj.data.uv_layers:
            old = obj.data.uv_layers.active.name
            obj.data.uv_layers.active.name = "SourceUV"
            for slot in obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    slot.material = slot.material.copy()
                    for node in slot.material.node_tree.nodes:
                        if node.type == "UVMAP" and node.uv_map == old:
                            node.uv_map = "SourceUV"
                        if node.type == "NORMAL_MAP" and node.uv_map in (old, ""):
                            node.uv_map = "SourceUV"
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    activate(meshes[0])
    for obj in meshes:
        obj.select_set(True)
    if len(meshes) > 1:
        bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def diagonal(bm):
    if not bm.verts:
        return 0.0
    lo = Vector(tuple(min(v.co[i] for v in bm.verts) for i in range(3)))
    hi = Vector(tuple(max(v.co[i] for v in bm.verts) for i in range(3)))
    return (hi - lo).length


def closed_boundaries(bm):
    """Return boundary components that are closed degree-two cycles.

    BMesh indices may be -1/stale following edits; use element identity sets.
    Branched boundaries and open chains are not safe hole-fill inputs.
    """
    boundary = [e for e in bm.edges if e.is_boundary]   # BMesh order, not a set's (memory-address) order
    pending = set(boundary)
    loops, skipped = [], 0
    for first in boundary:
        if first not in pending:
            continue
        pending.remove(first)
        component, stack = [first], [first]
        while stack:
            for vert in stack.pop().verts:
                for neighbor in vert.link_edges:
                    if neighbor in pending:
                        pending.remove(neighbor)
                        component.append(neighbor)
                        stack.append(neighbor)
        degree = {}
        for edge in component:
            for vert in edge.verts:
                degree[vert] = degree.get(vert, 0) + 1
        if len(component) >= 3 and all(n == 2 for n in degree.values()):
            loops.append(list(component))
        else:
            skipped += 1
    return loops, skipped


def remove_loose(bm):
    edges = [e for e in bm.edges if not e.link_faces]
    if edges:
        bmesh.ops.delete(bm, geom=edges, context="EDGES")
    verts = [v for v in bm.verts if not v.link_faces]
    if verts:
        bmesh.ops.delete(bm, geom=verts, context="VERTS")
    return len(edges) + len(verts)


def components(bm):
    pending = set(bm.faces)
    while pending:
        face = pending.pop()
        component, stack = [face], [face]
        while stack:
            for edge in stack.pop().edges:
                for neighbor in edge.link_faces:
                    if neighbor in pending:
                        pending.remove(neighbor)
                        component.append(neighbor)
                        stack.append(neighbor)
        yield component


def drop_components(bm, threshold):
    if not threshold:
        return 0
    drop = [face for component in components(bm) if len(component) < threshold for face in component]
    if drop:
        bmesh.ops.delete(bm, geom=drop, context="FACES")
    return len(drop)


def drop_open_dust(bm, size):
    """Delete open shells smaller than size x diagonal: zero-volume fins left on generated surfaces.
    Small closed parts (chain links, beads) are solids and stay."""
    if not size:
        return 0
    limit = diagonal(bm) * size
    drop = []
    for component in components(bm):
        verts = {v for face in component for v in face.verts}
        extent = Vector(tuple(max(v.co[i] for v in verts) - min(v.co[i] for v in verts) for i in range(3))).length
        if extent < limit and any(not edge.is_manifold for face in component for edge in face.edges):
            drop.extend(component)
    if drop:
        bmesh.ops.delete(bm, geom=drop, context="FACES")
    return len(drop)


def remove_duplicate_faces(bm):
    bm.verts.index_update()
    groups, duplicate = {}, []
    for face in bm.faces:
        key = tuple(sorted(v.index for v in face.verts))
        groups.setdefault(key, []).append(face)
    for group in groups.values():
        if len(group) < 2:
            continue
        faces = set(group)
        isolated = all(all(f in faces for f in edge.link_faces) for face in group for edge in face.edges)
        # Two coincident faces forming a closed zero-volume island are a collapse
        # artifact. Drop the whole island rather than leave a floating triangle.
        duplicate.extend(group if isolated else group[1:])
    if duplicate:
        bmesh.ops.delete(bm, geom=duplicate, context="FACES_ONLY")
        remove_loose(bm)
    return len(duplicate)


def repair(bm, args):
    stats = {"welded_vertices": 0, "degenerate_faces": 0, "split_edges": 0,
             "holes_filled": 0, "holes_skipped": 0, "loose_elements": 0, "component_faces_removed": 0,
             "dust_faces_removed": 0, "split_vertices": 0, "duplicate_faces_removed": 0}
    scale = diagonal(bm)
    if not scale or not bm.faces:
        raise ValueError("Empty or zero-size mesh")
    if args.repair != "off":
        # Do not merge already-connected interior detail. Near-coincident opposing
        # surfaces are common in generated props and are not necessarily duplicates.
        before = len(bm.faces)
        zero = [f for f in bm.faces if f.calc_area() <= scale * scale * 1e-16]
        if zero:
            bmesh.ops.delete(bm, geom=zero, context="FACES")
        count = len(bm.verts)
        if args.weld_distance:
            candidates = [v for v in bm.verts if v.is_boundary or not v.link_faces]
            print("PREP boundary weld", len(candidates), flush=True)
            if candidates:
                bmesh.ops.remove_doubles(bm, verts=candidates, dist=scale * args.weld_distance)
        stats["welded_vertices"] = count - len(bm.verts)
        print("PREP degenerates", flush=True)
        bmesh.ops.dissolve_degenerate(bm, edges=list(bm.edges), dist=scale * 1e-9)
        zero = [f for f in bm.faces if f.calc_area() <= scale * scale * 1e-16]
        if zero:
            bmesh.ops.delete(bm, geom=zero, context="FACES")
        stats["degenerate_faces"] = max(0, before - len(bm.faces))
        stats["duplicate_faces_removed"] = remove_duplicate_faces(bm)
        bad = [e for e in bm.edges if len(e.link_faces) > 2]
        print("PREP overconnected edges", len(bad), flush=True)
        if bad:
            stats["split_edges"] = len(bad)
            bmesh.ops.split_edges(bm, edges=bad)
        # Separate disconnected face fans at bow-tie vertices, then rejoin each fan
        # internally. This keeps each shell intact instead of exploding its faces.
        for vert in list(bm.verts):
            if not vert.is_valid or vert.is_manifold or not vert.link_faces:
                continue
            fan = list(vert.link_faces)          # BMesh order: a set's pop() order follows memory addresses
            pending, groups = set(fan), []
            for face in fan:
                if face not in pending:
                    continue
                pending.remove(face)
                group, stack = {face}, [face]
                while stack:
                    for edge in stack.pop().edges:
                        if vert not in edge.verts:
                            continue
                        for neighbor in edge.link_faces:
                            if neighbor in pending:
                                pending.remove(neighbor)
                                group.add(neighbor)
                                stack.append(neighbor)
                groups.append(group)
            if len(groups) < 2:
                continue
            group_of = {face: i for i, group in enumerate(groups) for face in group}
            separated = bmesh.utils.vert_separate(vert, list(vert.link_edges))
            representative, targets = {}, {}
            for new_vert in separated:
                if not new_vert.link_faces:
                    continue
                group = group_of[new_vert.link_faces[0]]
                if group in representative:
                    targets[new_vert] = representative[group]
                else:
                    representative[group] = new_vert
            if targets:
                bmesh.ops.weld_verts(bm, targetmap=targets)
            stats["split_vertices"] += len(groups) - 1
        loops, branched = closed_boundaries(bm)
        print("PREP hole boundaries", len(loops), flush=True)
        stats["holes_skipped"] = branched
        limit = args.hole_max_edges
        if limit is None:
            limit = 12 if args.repair == "light" else 64
        for edges in loops:
            verts = {v for e in edges for v in e.verts}
            adjacent = {face for edge in edges for face in edge.link_faces}
            # An isolated triangle/flat patch's outer rim is not a missing cap.
            # Filling it creates coincident duplicate faces, later rejected by Mesh.validate.
            if adjacent and all(all(v in verts for v in face.verts) for face in adjacent):
                stats["holes_skipped"] += 1
                continue
            extent = Vector(tuple(max(v.co[i] for v in verts) - min(v.co[i] for v in verts) for i in range(3))).length
            if not limit or len(edges) > limit or (args.hole_max_diameter and extent > scale * args.hole_max_diameter):
                stats["holes_skipped"] += 1
                continue
            result = bmesh.ops.holes_fill(bm, edges=edges, sides=limit)
            if result.get("faces"):
                stats["holes_filled"] += 1
            else:
                stats["holes_skipped"] += 1
        stats["component_faces_removed"] = drop_components(bm, args.min_component_faces)
        stats["dust_faces_removed"] = drop_open_dust(bm, args.dust_size)
        stats["loose_elements"] = remove_loose(bm)
    if not bm.faces:
        raise ValueError("Repair removed every face; check scale and cleanup settings")
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    bm.normal_update()
    return stats


def edit_mesh(obj, callback):
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        result = callback(bm)
        bm.to_mesh(obj.data)
        obj.data.update()
        return result
    finally:
        bm.free()


def shade(obj, angle):
    if obj.data.has_custom_normals:
        activate(obj)
        bpy.ops.mesh.customdata_custom_splitnormals_clear()
    def update(bm):
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.normal_update()
        for f in bm.faces:
            f.smooth = True
        for e in bm.edges:
            e.smooth = not (angle > 0 and (not e.is_manifold or e.calc_face_angle(0) > math.radians(angle)))
    edit_mesh(obj, update)
    # Sharp edges split exported shading normals, without tearing topology open.
    if hasattr(obj.data, "use_auto_smooth"):
        obj.data.use_auto_smooth = True
        obj.data.auto_smooth_angle = math.radians(angle) if angle else math.pi


def smart_uv(obj, args):
    activate(obj)
    uv = obj.data.uv_layers.new(name="GameUV")
    name = uv.name
    obj.data.uv_layers.active = uv
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bpy.ops.mesh.select_all(action="SELECT")
        print("PREP UV project", flush=True)
        bpy.ops.uv.smart_project(angle_limit=math.radians(args.uv_angle), island_margin=0,
                                 area_weight=0, correct_aspect=True, scale_to_bounds=False)
        if args.uv_merge_faces:
            bpy.ops.uv.select_all(action="SELECT")
            bpy.ops.uv.seams_from_islands(mark_seams=True)
            bpy.ops.object.mode_set(mode="OBJECT")
            merged = edit_mesh(obj, lambda bm: merge_small_islands(bm, args.uv_merge_faces))
            print("PREP UV merged small islands", merged, flush=True)
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.unwrap(method="ANGLE_BASED", margin=0)
        print("PREP UV scale", flush=True)
        bpy.ops.uv.select_all(action="SELECT")
        bpy.ops.uv.average_islands_scale()
        print("PREP UV pack", flush=True)
        pack_uv(args)
        print("PREP UV packed", flush=True)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    return name


XATLAS_PYTHON = os.environ.get("PA_XATLAS_PYTHON", "/mnt/ssd/conda-envs/ComfyUI/bin/python3.12")


def unwrap_uv(obj, args):
    """Return (uv layer name, method used, fallback reason).
    Quad cages keep Smart UV: xatlas only takes triangles and its charts cut through quads,
    and splitting the torn quads loose never converged (sculpt cage: 6.6k -> 3.6k torn after 8 passes)."""
    if args.uv_method == "xatlas" and all(len(p.vertices) == 3 for p in obj.data.polygons):
        try:
            return xatlas_uv(obj, args), "xatlas", None
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            print("PREP xatlas failed, using Smart UV:", error, flush=True)
            return smart_uv(obj, args), "smart", str(error)
    return smart_uv(obj, args), "smart", None


def xatlas_uv(obj, args):
    import numpy as np
    mesh = obj.data
    starts = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("loop_start", starts)
    if not np.array_equal(starts, np.arange(len(starts)) * 3):
        raise ValueError("xatlas needs contiguous triangle loops")
    positions = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", positions)
    triangles = np.empty(len(mesh.loops), dtype=np.uint32)
    mesh.loops.foreach_get("vertex_index", triangles)
    payload = io.BytesIO()
    np.savez(payload, positions=positions.reshape(-1, 3), triangles=triangles.reshape(-1, 3))
    print("PREP xatlas", len(starts), "triangles", flush=True)
    process = subprocess.run([XATLAS_PYTHON, "-I", str(Path(__file__).with_name("xatlas_worker.py")),
                              str(args.texture_size), str(args.uv_margin_px)],
                             input=payload.getvalue(), capture_output=True, timeout=600)
    if process.returncode:
        raise RuntimeError(process.stderr.decode(errors="replace")[-1000:])
    print("PREP", process.stderr.decode(errors="replace").strip()[-200:], flush=True)
    uvs = np.load(io.BytesIO(process.stdout), allow_pickle=False)
    if uvs.shape != (len(starts), 3, 2) or not np.isfinite(uvs).all():
        raise ValueError("xatlas worker returned invalid UVs")
    layer = mesh.uv_layers.new(name="GameUV")
    mesh.uv_layers.active = layer
    layer.data.foreach_set("uv", uvs.ravel())
    return layer.name


def merge_small_islands(bm, max_faces):
    """Join each seam island under max_faces to the neighbour it borders most, one seam run per island."""
    bm.faces.index_update()
    parent = list(range(len(bm.faces)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seams = []
    for edge in bm.edges:
        if len(edge.link_faces) != 2:
            continue
        a, b = (face.index for face in edge.link_faces)
        if edge.seam:
            seams.append((edge, a, b))
        else:
            parent[find(a)] = find(b)
    island = [find(i) for i in range(len(parent))]
    size = Counter(island)
    border = defaultdict(Counter)
    for _, a, b in seams:
        a, b = island[a], island[b]
        if a != b:
            border[a][b] += 1
            border[b][a] += 1
    # Merging along one border per island keeps charts disc-like; opening every seam bridges big charts into folds.
    parent[:] = range(len(parent))
    joined = set()
    for small in sorted((r for r in size if size[r] < max_faces), key=size.get):
        for other, _ in border[small].most_common():
            if find(other) != find(small):
                parent[find(small)] = find(other)
                joined.add(frozenset((small, other)))
                break
    for edge, a, b in seams:
        if frozenset((island[a], island[b])) in joined:
            edge.seam = False
    return len(joined)


def pack_uv(args):
    bpy.ops.uv.pack_islands(rotate=True, shape_method="CONCAVE", margin_method="FRACTION",
                            margin=args.uv_margin_px / args.texture_size)


def reunwrap_clusters(obj, bad, args):
    """Re-flatten each connected patch of folded faces as its own angle-based chart."""
    def select(bm):
        for face in bm.faces:
            face.select_set(face.index in bad)
    edit_mesh(obj, select)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bpy.ops.uv.select_all(action="SELECT")
        bpy.ops.uv.seams_from_islands(mark_seams=True)
        bpy.ops.mesh.region_to_loop()
        bpy.ops.mesh.mark_seam(clear=False)
        bpy.ops.object.mode_set(mode="OBJECT")
        edit_mesh(obj, select)
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.uv.unwrap(method="ANGLE_BASED", margin=0)
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.select_all(action="SELECT")
        bpy.ops.uv.average_islands_scale()
        pack_uv(args)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")


def fix_uv_overlaps(obj, args):
    """Keep Smart UV charts, re-flatten folded patches, then isolate faces still folded or collapsed."""
    folded = inspect_mesh(obj, check_overlap=True).get("uv_overlap_faces", ())
    if folded:
        print("PREP UV re-unwrap foldovers", len(folded), flush=True)
        reunwrap_clusters(obj, set(folded), args)
    isolated = 0
    for _ in range(3):
        quality = inspect_mesh(obj, check_overlap=True)
        bad = set(quality.get("uv_overlap_faces", ()))
        def project(bm):
            uv = bm.loops.layers.uv.active
            bm.normal_update()
            total_area = sum(face.calc_area() for face in bm.faces)
            for i, face in enumerate(bm.faces):
                coords = [loop[uv].uv for loop in face.loops]
                uv_area = area(coords)
                if uv_area <= 1e-18:
                    bad.add(i)
            if quality["uv_overlap_check"] == "budget_exceeded":
                bad.update(range(len(bm.faces)))
            for i, face in enumerate(bm.faces):
                if i not in bad:
                    continue
                edge = max(face.edges, key=lambda e: e.calc_length())
                a, b = edge.verts
                origin = a.co
                length = max(edge.calc_length(), 1e-30)
                axis = (b.co - origin).normalized()
                up = face.normal.cross(axis).normalized()
                scale = max(math.sqrt(face.calc_area() / max(total_area, 1e-30)), .001)
                angle = (i * 2.399963229728653) % (2 * math.pi)
                if len(face.loops) > 3:
                    # Projecting a twisted quad gives a bowtie; an isolated island only needs to be convex.
                    n = len(face.loops)
                    for k, loop in enumerate(face.loops):
                        t = angle + 2 * math.pi * k / n
                        loop[uv].uv = (0.7 * scale * math.cos(t), 0.7 * scale * math.sin(t))
                    continue
                cos, sin = math.cos(angle), math.sin(angle)
                # Unique rotations break UV edge connectivity without large offsets.
                # A small minimum height avoids float32 collapse of subpixel slivers.
                for loop in face.loops:
                    delta = (loop.vert.co - origin) / length
                    x, y = delta.dot(axis), delta.dot(up)
                    if loop.vert not in (a, b):
                        y = max(abs(y), 1e-5 / scale)
                    loop[uv].uv = (scale * (x * cos - y * sin), scale * (x * sin + y * cos))
        edit_mesh(obj, project)
        if not bad:
            return isolated
        isolated += len(bad)
        print("PREP UV isolate foldovers", len(bad), flush=True)
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.select_all(action="SELECT")
            pack_uv(args)
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
    return isolated


def transfer_materials(obj, source_uv, target_uv, args, base):
    """Transfer Principled PBR inputs using surviving source UVs, without a ray cage."""
    import numpy as np
    materials = []
    for slot in obj.material_slots:
        if slot.material is None:
            raise ValueError("Empty material slot: use --materials discard and retexture")
        slot.material = slot.material.copy()
        mat = slot.material
        mat.use_nodes = True
        nodes, links = mat.node_tree.nodes, mat.node_tree.links
        output = next((n for n in nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
        surface = output.inputs["Surface"] if output else None
        shader = surface.links[0].from_node if surface and surface.is_linked else None
        if not shader or shader.type != "BSDF_PRINCIPLED":
            raise ValueError("PBR transfer requires Principled BSDF -> Material Output; "
                             "use --materials discard and retexture for other shaders")
        uv_node = nodes.new("ShaderNodeUVMap")
        uv_node.uv_map = source_uv
        for node in list(nodes):
            if node.type == "TEX_IMAGE" and not node.inputs["Vector"].is_linked:
                links.new(uv_node.outputs["UV"], node.inputs["Vector"])
            elif node.type == "TEX_COORD":
                for link in list(node.outputs["UV"].links):
                    links.new(uv_node.outputs["UV"], link.to_socket)
            elif node.type == "NORMAL_MAP" and not node.uv_map:
                node.uv_map = source_uv
        bake_node = nodes.new("ShaderNodeTexImage")
        emission = nodes.new("ShaderNodeEmission")
        materials.append((mat, shader, output, emission, bake_node))
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    scene.render.bake.use_selected_to_active = False
    scene.render.bake.margin = max(1, int(args.uv_margin_px))
    scene.render.bake.use_clear = True
    obj.data.uv_layers[source_uv].active_render = True
    images = {}
    for label, socket in (("basecolor", "Base Color"), ("roughness", "Roughness"),
                          ("metallic", "Metallic"), ("alpha", "Alpha"), ("normal", None)):
        print("PREP bake", label, flush=True)
        image = bpy.data.images.new(base.name + "_" + label, width=args.texture_size,
                                    height=args.texture_size, alpha=True)
        image.colorspace_settings.name = "sRGB" if label == "basecolor" else "Non-Color"
        for mat, shader, output, emission, bake_node in materials:
            nodes, links = mat.node_tree.nodes, mat.node_tree.links
            for node in nodes:
                node.select = False
            bake_node.image = image
            bake_node.select = True
            nodes.active = bake_node
            if socket:
                color = emission.inputs["Color"]
                for link in list(color.links):
                    links.remove(link)
                source = shader.inputs[socket]
                if source.is_linked:
                    links.new(source.links[0].from_socket, color)
                else:
                    value = source.default_value
                    color.default_value = (value, value, value, 1) if isinstance(value, (int, float)) else value
                links.new(emission.outputs[0], output.inputs["Surface"])
            else:
                links.new(shader.outputs[0], output.inputs["Surface"])
        bpy.ops.object.bake(type="EMIT" if socket else "NORMAL", uv_layer=target_uv,
                            normal_space="TANGENT", use_selected_to_active=False)
        images[label] = image
    rgba = np.empty(args.texture_size * args.texture_size * 4, dtype=np.float32)
    alpha = np.empty_like(rgba)
    images["basecolor"].pixels.foreach_get(rgba)
    images["alpha"].pixels.foreach_get(alpha)
    rgba[3::4] = alpha[0::4]
    images["basecolor"].pixels.foreach_set(rgba)
    mat = bpy.data.materials.new("GamePBR")
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    shader = next(n for n in nodes if n.type == "BSDF_PRINCIPLED")
    for label, socket in (("basecolor", "Base Color"), ("roughness", "Roughness"), ("metallic", "Metallic")):
        node = nodes.new("ShaderNodeTexImage")
        node.image = images[label]
        links.new(node.outputs["Color"], shader.inputs[socket])
        if label == "basecolor":
            links.new(node.outputs["Alpha"], shader.inputs["Alpha"])
    normal = nodes.new("ShaderNodeNormalMap")
    normal.uv_map = target_uv
    node = nodes.new("ShaderNodeTexImage")
    node.image = images["normal"]
    links.new(node.outputs["Color"], normal.inputs["Color"])
    links.new(normal.outputs["Normal"], shader.inputs["Normal"])
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    for poly in obj.data.polygons:
        poly.material_index = 0
    paths = []
    folder = base.parent / (base.name + "_textures")
    folder.mkdir(parents=True, exist_ok=True)
    for label in ("basecolor", "roughness", "metallic", "normal"):
        image = images[label]
        path = folder / (label + ".png")
        image.filepath_raw = str(path)
        image.file_format = "PNG"
        image.save()
        paths.append(str(path))
    return paths


def inspect_mesh(obj, check_overlap=False):
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        scale = diagonal(bm)
        uv = bm.loops.layers.uv.active
        invalid, zero_uv, uv_area = 0, 0, 0.0
        for f in bm.faces:
            if uv:
                coords = [loop[uv].uv for loop in f.loops]
                invalid += sum(not (math.isfinite(p.x) and math.isfinite(p.y)) or
                               min(p) < -1e-5 or max(p) > 1.00001 for p in coords)
                face_area = area(coords)
                zero_uv += face_area <= 1e-18
                uv_area += face_area
        quality = {"uv_overlap_check": "not_measured"}
        if check_overlap and uv and not invalid:
            # Check the exporter's tessellation: a fan from loop 0 can cut a concave UV quad outside itself.
            bm.faces.index_update()
            tris = bm.calc_loop_triangles()
            owner = [tri[0].face.index for tri in tris]
            quality = overlaps([tuple(tuple(loop[uv].uv) for loop in tri) for tri in tris])
            quality["uv_overlap_faces"] = sorted({owner[t] for t in quality["uv_overlap_faces"]})
        return {"vertices": len(bm.verts), "faces": len(bm.faces),
                "triangles": sum(len(f.verts) - 2 for f in bm.faces),
                "boundary_edges": sum(e.is_boundary for e in bm.edges),
                "overconnected_edges": sum(len(e.link_faces) > 2 for e in bm.edges),
                "nonmanifold_vertices": sum(not v.is_manifold for v in bm.verts),
                "loose_edges": sum(not e.link_faces for e in bm.edges),
                "loose_vertices": sum(not v.link_edges for v in bm.verts),
                "degenerate_faces": sum(f.calc_area() <= scale * scale * 1e-16 for f in bm.faces),
                "uv_layer": obj.data.uv_layers.active.name if uv else None,
                "invalid_uv_corners": invalid, "zero_area_uv_faces": zero_uv,
                "uv_area_sum": uv_area, **quality}
    finally:
        bm.free()


def quad_multires(obj, quad_faces, levels, min_component_faces):
    """QuadriFlow a low quad cage, then rebuild the remeshed detail as multires levels."""
    target = obj.copy()
    target.data = obj.data.copy()
    bpy.context.scene.collection.objects.link(target)
    activate(obj)
    # QuadriFlow on the full remesh needs >20 GB; a 4x coarser voxel pass is a manifold, light input.
    obj.data.remesh_voxel_size *= 4
    bpy.ops.object.voxel_remesh()
    bpy.ops.object.quadriflow_remesh(target_faces=quad_faces, use_preserve_sharp=True, use_mesh_symmetry=False)
    edit_mesh(obj, lambda bm: drop_components(bm, min_component_faces))  # QuadriFlow leaves tiny islands
    if levels:
        mod = obj.modifiers.new("Sculpt", "MULTIRES")
        for _ in range(levels):
            bpy.ops.object.multires_subdivide(modifier=mod.name, mode="CATMULL_CLARK")
        # Reshape needs a mesh with the top level's topology: bake one, snap it onto the detail, copy it back.
        dense = obj.copy()
        dense.data = obj.data.copy()
        bpy.context.scene.collection.objects.link(dense)
        activate(dense)
        bpy.ops.object.modifier_apply(modifier=dense.modifiers[0].name)
        wrap = dense.modifiers.new("Project", "SHRINKWRAP")
        wrap.target = target
        # Nearest-point snapping bunches vertices at creases; cast along the normal both ways instead.
        wrap.wrap_method = "PROJECT"
        wrap.use_negative_direction = True
        wrap.project_limit = 0.01 * obj.dimensions.length  # cage is within ~0.3%; longer rays hit the far side
        bpy.ops.object.modifier_apply(modifier=wrap.name)
        activate(obj)
        dense.select_set(True)
        bpy.ops.object.multires_reshape(modifier=mod.name)
        bpy.data.objects.remove(dense, do_unlink=True)
    bpy.data.objects.remove(target, do_unlink=True)
    activate(obj)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[sys.argv.index("--") + 1:]
    args = parse(argv)
    src = Path(args.src).expanduser().resolve()
    base = Path(args.dst).expanduser().resolve().with_suffix("")
    outputs = [base.with_suffix("." + fmt) for fmt in args.formats]
    if src in outputs:
        raise ValueError("Use a separate output path; the original model is never overwritten")
    base.parent.mkdir(parents=True, exist_ok=True)
    print("PREP import", src, flush=True)
    obj = import_mesh(str(src), source_up=args.source_up, yaw=args.yaw)
    input_validated = obj.data.validate(verbose=True)
    before = inspect_mesh(obj)
    print("PREP input", json.dumps(before), flush=True)
    print("PREP repair", flush=True)
    stats = edit_mesh(obj, lambda bm: repair(bm, args))
    stats["input_mesh_data_corrected"] = input_validated
    if args.voxel_size:
        scale = edit_mesh(obj, diagonal)
        obj.data.remesh_voxel_size = scale * args.voxel_size
        activate(obj)
        bpy.ops.object.voxel_remesh()
    if args.smooth_iterations:
        def smooth(bm):
            verts = [v for v in bm.verts if v.is_manifold and not v.is_boundary]
            for _ in range(args.smooth_iterations):
                bmesh.ops.smooth_vert(bm, verts=verts, factor=args.smooth_factor,
                                      use_axis_x=True, use_axis_y=True, use_axis_z=True)
        edit_mesh(obj, smooth)
    if args.unwrap != "none":
        edit_mesh(obj, lambda bm: bmesh.ops.triangulate(bm, faces=list(bm.faces)))
    stats["duplicate_faces_removed"] += edit_mesh(obj, remove_duplicate_faces)
    if args.faces and len(obj.data.polygons) > args.faces:
        print("PREP decimate", len(obj.data.polygons), "->", args.faces, flush=True)
        activate(obj)
        mod = obj.modifiers.new("TriangleBudget", "DECIMATE")
        mod.ratio = max(0, args.faces - 1) / len(obj.data.polygons)
        mod.use_collapse_triangulate = True
        bpy.ops.object.modifier_apply(modifier=mod.name)
    # Applying modifiers can replace obj.data. Never retain the old Mesh reference.
    post_stats = edit_mesh(obj, lambda bm: repair(bm, args))
    for key, value in post_stats.items():
        stats[key] += value
    if args.unwrap != "none":
        edit_mesh(obj, lambda bm: bmesh.ops.triangulate(bm, faces=list(bm.faces)))
    if args.faces and len(obj.data.polygons) > args.faces:
        activate(obj)
        mod = obj.modifiers.new("FinalTriangleBudget", "DECIMATE")
        mod.ratio = max(0, args.faces - 1) / len(obj.data.polygons)
        mod.use_collapse_triangulate = True
        bpy.ops.object.modifier_apply(modifier=mod.name)
    stats["duplicate_faces_removed"] += edit_mesh(obj, remove_duplicate_faces)
    stats["mesh_data_corrected_before_uv"] = obj.data.validate(verbose=True)
    if args.quad_faces:
        # After every bmesh pass: topology edits would scramble multires displacement.
        print("PREP QuadriFlow", len(obj.data.polygons), "->", args.quad_faces, "+ multires", args.multires, flush=True)
        quad_multires(obj, args.quad_faces, args.multires, max(args.min_component_faces, 100))
    shade(obj, args.angle)
    texture_paths = []
    textured = bool(obj.data.materials)
    if args.unwrap == "smart":
        print("PREP Smart UV", flush=True)
        source_uv = obj.data.uv_layers.active.name if obj.data.uv_layers else None
        # UV work only touches the cage; each mode switch would otherwise re-evaluate every multires level.
        hidden = [m for m in obj.modifiers if m.type == "MULTIRES" and m.show_viewport]
        for mod in hidden:
            mod.show_viewport = False
        try:
            target_uv, stats["uv_method"], stats["uv_method_fallback"] = unwrap_uv(obj, args)
            stats["uv_faces_isolated"] = fix_uv_overlaps(obj, args)
        finally:
            for mod in hidden:
                mod.show_viewport = True
        if textured and args.materials == "bake":
            if not source_uv:
                raise ValueError("Cannot transfer materials without source UVs; use --materials discard and retexture")
            texture_paths = transfer_materials(obj, source_uv, target_uv, args, base)
        else:
            obj.data.materials.clear()
            textured = False
        # Material-linked vertex colors are already included in the PBR bake.
        for attr in list(obj.data.color_attributes):
            obj.data.color_attributes.remove(attr)
        # UV layer RNA handles are invalidated when layers are removed or Edit Mode ends.
        for name in [layer.name for layer in obj.data.uv_layers]:
            if name != target_uv:
                obj.data.uv_layers.remove(obj.data.uv_layers[name])
        obj.data.uv_layers.active_index = 0
        obj.data.uv_layers[0].active_render = True
    elif args.unwrap == "none":
        # A sculpt base: remeshed geometry has no meaningful UVs or textures; bake after sculpting.
        obj.data.materials.clear()
        textured = False
        for name in [layer.name for layer in obj.data.uv_layers]:
            obj.data.uv_layers.remove(obj.data.uv_layers[name])
        for attr in list(obj.data.color_attributes):
            obj.data.color_attributes.remove(attr)
    elif not obj.data.uv_layers:
        raise ValueError("--unwrap keep requires existing UVs; choose --unwrap smart")
    if not obj.data.materials:
        mat = bpy.data.materials.new("NeedsTexture")
        mat.use_nodes = True
        obj.data.materials.append(mat)  # ensures trimesh retains TEXCOORD_0
        for poly in obj.data.polygons:
            poly.material_index = 0
    # Validate before our own report, rather than letting an exporter silently
    # repair data after it has already been counted and approved.
    stats["output_mesh_data_corrected"] = obj.data.validate(verbose=True)
    after = inspect_mesh(obj, check_overlap=True)
    print("PREP validate", json.dumps(after), flush=True)
    warnings = []
    if after["boundary_edges"] or after["nonmanifold_vertices"] or after["overconnected_edges"]:
        warnings.append("Open/non-manifold topology remains; inspect it or try opt-in voxel remeshing.")
    if args.faces and after["triangles"] > args.faces:
        warnings.append("Final triangle count exceeds requested budget after cleanup; review the model.")
    if not textured:
        warnings.append("Neutral material: bake/apply textures before shipping.")
    if args.smooth_iterations or args.voxel_size:
        warnings.append("Smoothing/remeshing changes shape; inspect thin parts and silhouette.")
    if args.unwrap != "none" and after["uv_overlap_check"] != "complete":
        warnings.append("UV overlap validation did not complete; inspect UVs before shipping.")
    if args.unwrap == "keep" and after.get("uv_overlap_pairs"):
        warnings.append("Existing UV overlaps preserved by --unwrap keep; they may be intentional texture reuse.")
    closed = not (after["boundary_edges"] or after["nonmanifold_vertices"] or after["overconnected_edges"])
    if args.unwrap == "none":
        valid = bool(after["faces"] and closed and not after["degenerate_faces"])
    else:
        valid = bool(after["faces"] and after["uv_layer"] and not after["invalid_uv_corners"] and
                 not after["zero_area_uv_faces"] and not after["degenerate_faces"] and
                 (args.unwrap == "keep" or (not after.get("uv_overlap_pairs", 0) and
                                            after["uv_overlap_check"] == "complete")))
    report = {"schema_version": 1, "source": str(src), "options": vars(args), "before": before,
              "after": after, "repair": stats, "warnings": warnings, "validation_passed": valid,
              "closed_manifold": closed, "has_materials": textured, "textures": texture_paths,
              "outputs": [], "status": "pending"}
    report_path = base.with_suffix(".report.json")
    if not valid or (args.strict and not closed):
        report["status"] = "rejected"
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        raise ValueError(f"Mesh validation failed; see {report_path}")
    activate(obj)
    for fmt, path in zip(args.formats, outputs):
        if fmt == "glb":
            bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=True,
                                      export_texcoords=True, export_normals=True, export_tangents=True)
        elif fmt == "obj":
            bpy.ops.wm.obj_export(filepath=str(path), export_selected_objects=True, export_uv=True, export_normals=True,
                                 apply_modifiers=False)
        elif fmt == "fbx":
            bpy.ops.export_scene.fbx(filepath=str(path), use_selection=True, object_types={"MESH"},
                                     add_leaf_bones=False, bake_anim=False, path_mode="COPY", embed_textures=True)
        else:
            for other in list(bpy.context.scene.objects):
                if other != obj:
                    bpy.data.objects.remove(other, do_unlink=True)
            bpy.ops.wm.save_as_mainfile(filepath=str(path))
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Exporter did not write {path}")
        report["outputs"].append(str(path))
    report["status"] = "exported"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    for message in warnings:
        print("PREP WARNING", message)
    print(f"PREP OK triangles {before['triangles']} -> {after['triangles']}; "
          f"boundary edges {after['boundary_edges']}; UV {after['uv_layer']}; report {report_path}")
    return report


if __name__ == "__main__":
    main()
