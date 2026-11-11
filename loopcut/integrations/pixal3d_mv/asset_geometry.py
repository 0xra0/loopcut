"""Component-aware geometry, UV metrics, LODs and collision for Blender."""
import math
from pathlib import Path
import subprocess
import tempfile

import bpy
import bmesh
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

import prep_mesh as prep
from asset_config import allocate_budget
from uv_quality import area


def bounds(obj):
    points = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
    lo = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    hi = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    return lo, hi


def duplicate(obj, name):
    result = obj.copy()
    result.data = obj.data.copy()
    result.name = name
    bpy.context.collection.objects.link(result)
    result.hide_set(False)
    result.hide_render = False
    return result


def join(objects, name):
    prep.activate(objects[0])
    for obj in objects:
        obj.hide_set(False)
        obj.select_set(True)
    if len(objects) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    obj.name = name
    return obj


def normalize(obj, args):
    prep.activate(obj)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    lo, hi = bounds(obj)
    factor = args.size_meters / max(hi - lo) if args.size_meters else 1.0
    if args.pivot == "center":
        pivot = (lo + hi) / 2
    elif args.pivot == "base":
        pivot = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
    else:
        pivot = Vector((0, 0, 0))
    for vertex in obj.data.vertices:
        vertex.co = (vertex.co - pivot) * factor
    obj.data.update()
    return {"scale_factor": factor, "source_pivot": list(pivot), "units": "meters",
            "size_was_explicit": bool(args.size_meters)}


def separate_loose(obj):
    prep.activate(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.separate(type="LOOSE")
    bpy.ops.object.mode_set(mode="OBJECT")
    return sorted([x for x in bpy.context.selected_objects if x.type == "MESH"],
                  key=lambda x: (-len(x.data.polygons), x.name))


def smooth_region(obj, iterations, factor, feature_angle, selector=None):
    def process(bm):
        bm.normal_update()
        threshold = math.radians(feature_angle)
        verts = [v for v in bm.verts if v.is_manifold and not v.is_boundary and
                 all(e.calc_face_angle(0) < threshold for e in v.link_edges) and
                 (selector is None or selector(v.co))]
        # Keep vertices at the edge of a spatial mask fixed, avoiding torn region borders.
        selected = set(verts)
        if selector:
            verts = [v for v in verts if all(e.other_vert(v) in selected for e in v.link_edges)]
        for _ in range(iterations):
            bmesh.ops.smooth_vert(bm, verts=verts, factor=factor, use_axis_x=True, use_axis_y=True, use_axis_z=True)
        return len(verts)
    return prep.edit_mesh(obj, process)


def component_pass(obj, args, regions):
    """Split loose shells and preserve matched original/color and cleaned/detail sources."""
    obj.data.validate()
    repair_stats = prep.edit_mesh(obj, lambda bm: prep.repair(bm, args))
    prep.edit_mesh(obj, lambda bm: bmesh.ops.triangulate(bm, faces=list(bm.faces)))
    prep.edit_mesh(obj, prep.remove_duplicate_faces)
    lo, hi = bounds(obj)
    global_diagonal = (hi - lo).length
    parts, sources, records = [], [], []
    for index, part in enumerate(separate_loose(obj)):
        part.name = f"High_{index:04}"
        source = duplicate(part, f"Original_{index:04}")
        source.hide_render = True
        source.hide_set(True)
        sources.append(source)
        part["pa_area_epsilon"] = global_diagonal * global_diagonal * 1e-16
        source["pa_area_epsilon"] = part["pa_area_epsilon"]
        low, high = bounds(part)
        size = high - low
        part_diagonal = size.length
        protected = part_diagonal < global_diagonal * args.protect_component_size or len(part.data.polygons) < 32
        record = {"id": index, "source_triangles": len(part.data.polygons), "protected": protected,
                  "diagonal": part_diagonal, "bounds": [list(low), list(high)], "region_actions": []}
        if args.surface_smooth and not protected:
            count = smooth_region(part, args.surface_smooth, args.surface_factor, args.feature_angle)
            record["region_actions"].append({"region": "automatic", "smoothed_vertices": count})
        if args.voxel_size and not protected:
            prep.activate(part)
            part.data.remesh_voxel_size = part_diagonal * args.voxel_size
            bpy.ops.object.voxel_remesh()
            record["region_actions"].append({"region": "automatic", "voxel_size": part.data.remesh_voxel_size})
        for region in regions:
            minimum = Vector(tuple(lo[i] + region["bounds"][0][i] * (hi[i] - lo[i]) for i in range(3)))
            maximum = Vector(tuple(lo[i] + region["bounds"][1][i] * (hi[i] - lo[i]) for i in range(3)))
            def inside(point):
                return all(minimum[i] - 1e-9 <= point[i] <= maximum[i] + 1e-9 for i in range(3))
            if protected and not region.get("force"):
                continue
            iterations = region.get("smooth_iterations", 0)
            if iterations:
                count = smooth_region(part, iterations, region.get("smooth_factor", args.surface_factor),
                                      args.feature_angle, inside)
                record["region_actions"].append({"region": region.get("name", "region"), "smoothed_vertices": count})
            voxel = region.get("voxel_size", 0)
            if voxel:
                if all(inside(v.co) for v in part.data.vertices):
                    prep.activate(part)
                    part.data.remesh_voxel_size = part_diagonal * voxel
                    bpy.ops.object.voxel_remesh()
                    record["region_actions"].append({"region": region.get("name", "region"), "voxel_size": voxel})
                elif any(inside(v.co) for v in part.data.vertices):
                    record["region_actions"].append({"region": region.get("name", "region"),
                                                     "skipped": "voxel region cuts a component; use a whole-component mask"})
        prep.edit_mesh(part, lambda bm: bmesh.ops.triangulate(bm, faces=list(bm.faces)))
        prep.shade(part, args.angle)
        record["high_triangles"] = len(part.data.polygons)
        parts.append(part)
        records.append(record)
    return parts, sources, records, repair_stats


def reduce_part(source, target, name, args, quadriflow=False):
    result = duplicate(source, name)
    if len(source.data.polygons) <= target:
        return result  # already triangulated, sanitized and shaded by the previous stage
    if quadriflow and len(result.data.polygons) > target:
        prep.activate(result)
        try:
            bpy.ops.object.quadriflow_remesh(target_faces=max(4, target // 2), use_preserve_sharp=True,
                                             use_preserve_boundary=True)
        except RuntimeError as exc:
            print("ASSET WARNING QuadriFlow failed; using decimation:", exc, flush=True)
    prep.edit_mesh(result, lambda bm: bmesh.ops.triangulate(bm, faces=list(bm.faces)))
    if len(result.data.polygons) > target:
        prep.activate(result)
        mod = result.modifiers.new("Budget", "DECIMATE")
        # Aim one below the target, but never below 4: no closed solid has 3 triangles, so a closed 4-triangle
        # LOD minimum collapsed into two coincident faces that sanitize erased and the fallback restored at full
        # size. LOD0 targets are >= 12, so LOD0 (and the atlas painted textures depend on) is unchanged.
        mod.ratio = max(4, target - 1) / len(result.data.polygons)
        mod.use_collapse_triangulate = True
        bpy.ops.object.modifier_apply(modifier=mod.name)
    # Do not add caps after this point: LODs share texture coordinates with LOD0.
    def sanitize(bm):
        epsilon = source.get("pa_area_epsilon", 1e-20)
        zero = [face for face in bm.faces if face.calc_area() <= epsilon]
        if zero:
            bmesh.ops.delete(bm, geom=zero, context="FACES")
        prep.remove_duplicate_faces(bm)
        prep.remove_loose(bm)
    prep.edit_mesh(result, sanitize)
    if not result.data.polygons:
        bpy.data.objects.remove(result, do_unlink=True)
        result = duplicate(source, name)
        result["pa_reduction_fallback"] = "Preserved source: reduction would erase this component"
        print("ASSET WARNING", name, "preserved because reduction would erase it", flush=True)
        return result
    prep.shade(result, args.angle)
    return result


def build_low_parts(highs, args, lod=0, originals=None):
    counts = [len(x.data.polygons) for x in highs]
    budget = args.faces if lod == 0 else max(len(highs) * 4, int(sum(counts) * args.lod_ratios[lod]))
    minimum = 12 if lod == 0 else 4
    # LOD0 favours small parts (sqrt of area keeps studs readable). Later LODs shrink every LOD0 part by the
    # same ratio, weighted by what each still lacks after its minimum: sqrt(area) let 205 chestplate studs
    # take most of LOD2 and left the plate at 12% of its triangles (flat shards).
    weights = ([math.sqrt(sum(p.area for p in x.data.polygons)) for x in highs] if lod == 0 else
               [max(0, n * args.lod_ratios[lod] - minimum) for n in counts])
    targets = allocate_budget(counts, weights, budget, minimum=minimum)
    # Modifier operators otherwise reevaluate hundreds of unrelated high/color
    # source objects for every small component. A private scene keeps that work local.
    collection = bpy.context.collection
    work = bpy.data.scenes.new("PixalReductionWork")
    window = bpy.context.window
    previous_scene = window.scene
    previous_layer = window.view_layer
    parts = []
    try:
        window.scene = work
        for i, (source, target) in enumerate(zip(highs, targets)):
            if i % 50 == 0:
                print("ASSET reduce LOD", lod, "component", i, "/", len(highs), flush=True)
            parts.append(reduce_part(source, target, f"{args.name}_LOD{lod}_part{i:04}", args,
                                     quadriflow=lod == 0 and args.retopology == "quadriflow"))
        window.scene = previous_scene
        window.view_layer = previous_layer
        for obj in parts:
            collection.objects.link(obj)
            work.collection.objects.unlink(obj)
    finally:
        window.scene = previous_scene
        window.view_layer = previous_layer
        bpy.data.scenes.remove(work)
    return parts, targets


def atlas(parts, args):
    """Join for a shared atlas, use temporary per-part materials to split back for matched baking."""
    for i, obj in enumerate(parts):
        prep.edit_mesh(obj, lambda bm: bmesh.ops.triangulate(bm, faces=list(bm.faces)))
        obj.data.materials.clear()
        obj.data.materials.append(bpy.data.materials.new(f"PartIndex_{i:04}"))
        for polygon in obj.data.polygons:
            polygon.material_index = 0
        while obj.data.uv_layers:
            obj.data.uv_layers.remove(obj.data.uv_layers[0])
    obj = join(parts, args.name + "_Atlas")
    expected_parts = len(parts)
    def remove_degenerates(bm):
        scale = prep.diagonal(bm)
        zero = [face for face in bm.faces if face.calc_area() <= scale * scale * 1e-16]
        if zero:
            bmesh.ops.delete(bm, geom=zero, context="FACES")
        prep.remove_duplicate_faces(bm)
        prep.remove_loose(bm)
        return len(zero)
    removed = prep.edit_mesh(obj, remove_degenerates)
    _, uv_method, uv_fallback = prep.unwrap_uv(obj, args)
    isolated = prep.fix_uv_overlaps(obj, args)
    relax = {"enabled": False}
    # Relaxation re-unfolds Smart UV's planar projections; xatlas charts are already unfolded.
    if args.uv_relax and uv_method == "smart":
        baseline = np.empty(len(obj.data.uv_layers.active.data) * 2, dtype=np.float32)
        obj.data.uv_layers.active.data.foreach_get("uv", baseline)
        base_quality = prep.inspect_mesh(obj, True)
        relax = relax_charts(obj, args)
        candidate_isolated = prep.fix_uv_overlaps(obj, args)
        candidate = prep.inspect_mesh(obj, True)
        valid = not candidate["invalid_uv_corners"] and not candidate["zero_area_uv_faces"] and \
            not candidate.get("uv_overlap_pairs") and candidate["uv_overlap_check"] == "complete"
        accepted = valid and candidate["uv_area_sum"] > base_quality["uv_area_sum"]
        relax.update({"accepted": accepted, "baseline_coverage": base_quality["uv_area_sum"],
                      "candidate_coverage": candidate["uv_area_sum"]})
        if accepted:
            isolated = candidate_isolated
        else:
            obj.data.uv_layers.active.data.foreach_set("uv", baseline)
    layer = obj.data.uv_layers.active
    best = np.empty(len(layer.data) * 2, dtype=np.float32)
    layer.data.foreach_get("uv", best)
    quality = prep.inspect_mesh(obj, True)
    best_coverage = quality["uv_area_sum"] if not quality.get("uv_overlap_pairs") else -1
    candidates = [{"method": "xatlas" if uv_method == "xatlas" else "AABB", "coverage": best_coverage}]
    methods = [args.pack_method] if args.pack_method != "AABB" else []
    if uv_method == "xatlas":
        methods = []  # Blender repacks of xatlas charts lost every trial (chestplate 44% -> 30%).
    if args.pack_search:
        methods = list(dict.fromkeys(methods + ["CONVEX", "CONCAVE"]))
    for method in methods:
        print("ASSET UV pack candidate", method, "timeout", args.pack_timeout, flush=True)
        # Blender's shape packer can take unbounded time on pathological islands.
        # A worker process bounds this operation without interrupting the build.
        with tempfile.TemporaryDirectory(prefix="pixal_uv_") as tmp:
            src = Path(tmp) / "atlas.blend"
            dst = Path(tmp) / "uv.npy"
            bpy.data.libraries.write(str(src), {obj})
            command = [bpy.app.binary_path, "-b", "--factory-startup", "--disable-autoexec", "--python-exit-code", "1",
                       "--python", str(Path(__file__).with_name("uv_pack_worker.py")), "--", str(src), str(dst),
                       method, str(args.uv_margin_px / args.texture_size)]
            try:
                process = subprocess.run(command, capture_output=True, text=True, timeout=args.pack_timeout)
                if process.returncode or not dst.is_file():
                    candidates.append({"method": method, "status": "failed", "detail": process.stderr[-1000:]})
                    continue
                data = np.load(dst, allow_pickle=False)
                if data.shape != best.shape or not np.isfinite(data).all():
                    raise ValueError("UV pack worker returned invalid coordinates")
                obj.data.uv_layers.active.data.foreach_set("uv", data)
            except subprocess.TimeoutExpired:
                candidates.append({"method": method, "status": "timeout"})
                continue
        trial = prep.inspect_mesh(obj, True)
        valid = not trial["invalid_uv_corners"] and not trial["zero_area_uv_faces"] and \
            not trial.get("uv_overlap_pairs") and trial["uv_overlap_check"] == "complete"
        candidates.append({"method": method, "coverage": trial["uv_area_sum"], "valid": valid})
        if valid and trial["uv_area_sum"] > best_coverage:
            best_coverage = trial["uv_area_sum"]
            obj.data.uv_layers.active.data.foreach_get("uv", best)
    obj.data.uv_layers.active.data.foreach_set("uv", best)
    quality = prep.inspect_mesh(obj, True)
    if quality["invalid_uv_corners"] or quality["zero_area_uv_faces"] or quality.get("uv_overlap_pairs") or \
            quality["uv_overlap_check"] != "complete":
        raise ValueError("UV atlas failed validation")
    metrics = uv_metrics(obj, args.texture_size)
    prep.activate(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.separate(type="MATERIAL")
    bpy.ops.object.mode_set(mode="OBJECT")
    results = list(bpy.context.selected_objects)
    indexed = []
    for part in results:
        indices = {p.material_index for p in part.data.polygons}
        if len(indices) != 1:
            raise RuntimeError("Part/material identity was lost during atlas separation")
        material = part.data.materials[next(iter(indices))]
        index = int(material.name.split("_")[1].split(".")[0])
        indexed.append((index, part))
    indexed.sort(key=lambda pair: pair[0])
    if [i for i, _ in indexed] != list(range(expected_parts)):
        raise ValueError("A component has no usable faces after numerical cleanup; remove it explicitly or increase its detail")
    return [part for _, part in indexed], {"quality": quality, "metrics": metrics,
                                          "pack_candidates": candidates, "isolated_faces": isolated, "chart_relaxation": relax,
                                          "method": uv_method, "method_fallback": uv_fallback,
                                          "degenerate_faces_removed": removed}


def relax_charts(obj, args):
    """Use Smart UV as automatic seam seeds, merge gentle chart boundaries as a
    forest, then unfold charts with angle-based parameterization instead of
    keeping a planar projection that can fold over a noisy 3D surface.
    """
    def seams(bm):
        bm.faces.index_update()
        bm.normal_update()
        uv = bm.loops.layers.uv.active
        parent = list(range(len(bm.faces)))
        sizes = [1] * len(parent)
        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        def merge(a, b):
            a, b = find(a), find(b)
            if a != b:
                parent[a] = b
                sizes[b] += sizes[a]
        candidates = []
        for edge in bm.edges:
            edge.seam = True
            if len(edge.link_faces) != 2:
                continue
            a, b = edge.link_faces
            coords = [{loop.vert: loop[uv].uv.copy() for loop in face.loops} for face in (a, b)]
            same = all((coords[0][v] - coords[1][v]).length < 1e-7 for v in edge.verts)
            if same:
                edge.seam = False
                merge(a.index, b.index)
            else:
                candidates.append((edge.calc_face_angle(0), edge))
        initial = len({find(i) for i in parent})
        joined = 0
        for angle, edge in sorted(candidates, key=lambda pair: pair[0]):
            if angle > math.radians(args.chart_merge_angle):
                break
            a, b = [find(f.index) for f in edge.link_faces]
            if a != b and sizes[a] + sizes[b] <= args.max_chart_faces:
                edge.seam = False
                merge(a, b)
                joined += 1
        return {"enabled": True, "smart_seed_charts": initial, "merged_boundaries": joined,
                "unfold_charts": initial - joined}
    info = prep.edit_mesh(obj, seams)
    print("ASSET UV chart relaxation", info, flush=True)
    prep.activate(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.select_all(action="SELECT")
        bpy.ops.uv.unwrap(method="ANGLE_BASED", fill_holes=True, margin=0)
        bpy.ops.uv.average_islands_scale()
        bpy.ops.uv.pack_islands(rotate=True, shape_method="AABB", margin_method="FRACTION",
                                margin=args.uv_margin_px / args.texture_size)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    return info


def batch_bake_parts(highs, originals, lod_parts, records, reach=.05):
    """Batch shells into shared bake calls after individual geometry reduction.

    Geometry budgets still preserve each component. Matching source/target index
    sets and a small cage scale prevent full-asset projection across nearby links.
    Larger shells share a call only when their boxes, grown by the ray reach, are
    disjoint (no ray can land on a neighbour) and sizes are within 2x (the group
    cage uses the smallest member).
    """
    # Batch at any part count: each group costs a full bake pass per map and LOD.
    global_size = max(x["diagonal"] for x in records)
    tiny = [i for i, record in enumerate(records) if record["protected"] and record["diagonal"] < global_size * .03]
    tiny.sort(key=lambda i: tuple(sum(row[a] for row in records[i]["bounds"]) for a in range(3)))
    tiny_set = set(tiny)
    def grown(i):
        pad = records[i]["diagonal"] * reach
        return [[v - pad for v in records[i]["bounds"][0]], [v + pad for v in records[i]["bounds"][1]]]
    def apart(a, b):
        return any(a[1][k] < b[0][k] or b[1][k] < a[0][k] for k in range(3))
    groups = []
    for i in sorted((i for i in range(len(highs)) if i not in tiny_set), key=lambda i: -records[i]["diagonal"]):
        box = grown(i)
        for group in groups:
            if len(group) < 32 and records[group[0]]["diagonal"] <= 2 * records[i]["diagonal"] and \
                    all(apart(box, grown(j)) for j in group):
                group.append(i)
                break
        else:
            groups.append([i])
    groups += [tiny[i:i + 32] for i in range(0, len(tiny), 32)]
    result_high, result_original = [], []
    result_lows = [[] for _ in lod_parts]
    for group_index, indices in enumerate(groups):
        high = join([highs[i] for i in indices], f"HighBatch_{group_index:04}")
        original = join([originals[i] for i in indices], f"ColorBatch_{group_index:04}")
        scale = min(records[i]["diagonal"] for i in indices)
        high["pa_cage_diagonal"] = scale
        original["pa_cage_diagonal"] = scale
        result_high.append(high)
        result_original.append(original)
        for lod, parts in enumerate(lod_parts):
            result_lows[lod].append(join([parts[i] for i in indices], f"LowBatch_LOD{lod}_{group_index:04}"))
    return result_high, result_original, result_lows, {"groups": len(groups), "physical_components": len(highs),
                                                     "max_components_per_group": 32}


def uv_metrics(obj, texture_size):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    uv = bm.loops.layers.uv.active
    bm.faces.index_update()
    parent = list(range(len(bm.faces)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for edge in bm.edges:
        if len(edge.link_faces) != 2:
            continue
        a, b = edge.link_faces
        coords = [{loop.vert: loop[uv].uv.copy() for loop in f.loops} for f in (a, b)]
        if all((coords[0][v] - coords[1][v]).length < 1e-7 for v in edge.verts):
            parent[find(a.index)] = find(b.index)
    islands, density = {}, []
    for face in bm.faces:
        pixel_area = area([loop[uv].uv for loop in face.loops]) * texture_size * texture_size
        root = find(face.index)
        islands[root] = islands.get(root, 0) + pixel_area
        if face.calc_area() > 1e-20:
            density.append(math.sqrt(pixel_area / face.calc_area()))
    bm.free()
    return {"islands": len(islands), "islands_under_one_pixel": sum(x < 1 for x in islands.values()),
            "coverage": sum(islands.values()) / texture_size ** 2,
            "texel_density_pixels_per_meter": {"p10": float(np.percentile(density, 10)),
                                               "median": float(np.median(density)),
                                               "p90": float(np.percentile(density, 90))}}


def shape_error(source, target, samples=3000):
    source.data.calc_loop_triangles()
    target.data.calc_loop_triangles()
    tree = BVHTree.FromPolygons([v.co for v in source.data.vertices],
                               [tuple(t.vertices) for t in source.data.loop_triangles], all_triangles=True)
    step = max(1, len(target.data.vertices) // samples)
    distances = []
    for vertex in list(target.data.vertices)[::step]:
        hit = tree.find_nearest(vertex.co)
        if hit[0] is not None:
            distances.append(hit[3])
    lo, hi = bounds(source)
    diagonal = max((hi - lo).length, 1e-20)
    return {"max_relative": float(max(distances, default=0) / diagonal),
            "p95_relative": float(np.percentile(distances, 95) / diagonal) if distances else 0,
            "samples": len(distances), "direction": "low_vertices_to_high_surface"}


def collision_meshes(lod0, args):
    if args.collision == "none":
        return []
    if args.collision == "compound":
        # Spatial bins bound hull count; each bin produces one true convex hull.
        all_points = [v.co.copy() for v in lod0.data.vertices]
        lo, hi = bounds(lod0)
        axis = max(range(3), key=lambda i: hi[i] - lo[i])
        all_points.sort(key=lambda p: p[axis])
        n = min(args.collision_max_hulls, max(1, len(all_points) // 256))
        groups = [all_points[i * len(all_points) // n:(i + 1) * len(all_points) // n] for i in range(n)]
    else:
        groups = [[v.co.copy() for v in lod0.data.vertices]]
    colliders = []
    for i, points in enumerate(groups):
        bm = bmesh.new()
        if args.collision == "box":
            low = Vector(tuple(min(p[a] for p in points) for a in range(3)))
            high = Vector(tuple(max(p[a] for p in points) for a in range(3)))
            center = (low + high) / 2
            extent = high - low
            bmesh.ops.create_cube(bm, size=1)
            for vertex in bm.verts:
                vertex.co = center + Vector(tuple(vertex.co[a] * max(extent[a], 1e-5) for a in range(3)))
        else:
            # Exact convex hull; sample only when a very dense render mesh exceeds 20k verts.
            step = max(1, len(points) // 20000)
            verts = [bm.verts.new(p) for p in points[::step]]
            result = bmesh.ops.convex_hull(bm, input=verts, use_existing_faces=False)
            unused = list(result.get("geom_interior", ())) + list(result.get("geom_unused", ()))
            unused = list({x for x in unused if x.is_valid and isinstance(x, bmesh.types.BMVert)})
            if unused:
                bmesh.ops.delete(bm, geom=unused, context="VERTS")
            prep.remove_loose(bm)
        bmesh.ops.triangulate(bm, faces=list(bm.faces))
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        data = bpy.data.meshes.new("Collision")
        bm.to_mesh(data)
        bm.free()
        name = f"UCX_{args.name}_LOD0_{i:02}"
        obj = bpy.data.objects.new(name, data)
        bpy.context.collection.objects.link(obj)
        obj.hide_render = True
        obj.display_type = "WIRE"
        colliders.append(obj)
    return colliders
