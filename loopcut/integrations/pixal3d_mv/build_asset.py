"""Blender production static-asset build: all seven game-readiness stages.

blender -b --factory-startup --python-exit-code 1 --python build_asset.py -- source.glb output_dir --engine godot
"""
import hashlib
from copy import copy
import json
from pathlib import Path
import shutil
import struct
import sys
import tempfile

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prep_mesh as prep
from asset_config import ENGINES, RED_ENGINES, load_regions, parse_asset
from asset_geometry import (atlas, batch_bake_parts, build_low_parts, collision_meshes, component_pass, join, normalize, shape_error)
from asset_bake import bake_asset, painted_sources
from asset_quality import evaluate, previews, write_gallery
from engine_check import check_engine
from redengine_handoff import export_handoff


def glb_metrics(path):
    data = Path(path).read_bytes()
    magic, version, length = struct.unpack_from("<4sII", data)
    if magic != b"glTF" or version != 2 or length != len(data):
        raise ValueError("Invalid exported GLB header")
    size, kind = struct.unpack_from("<I4s", data, 12)
    if kind != b"JSON":
        raise ValueError("Missing GLB JSON chunk")
    doc = json.loads(data[20:20 + size])
    primitives = [p for mesh in doc.get("meshes", []) for p in mesh["primitives"]]
    if not primitives:
        raise ValueError("Exported GLB contains no primitives")
    for primitive in primitives:
        if not {"POSITION", "NORMAL", "TANGENT", "TEXCOORD_0"} <= primitive["attributes"].keys():
            raise ValueError("Export is missing normals, tangents or UV0")
    return {"triangles": sum(doc["accessors"][p["indices"]]["count"] // 3 for p in primitives),
            "vertices": sum(doc["accessors"][p["attributes"]["POSITION"]]["count"] for p in primitives),
            "materials": len({p.get("material") for p in primitives}), "primitives": len(primitives),
            "bytes": len(data)}


def export_lod(obj, index, colliders, args, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    prep.activate(obj)
    glb = folder / f"{args.name}_LOD{index}.glb"
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", use_selection=True,
                              export_texcoords=True, export_normals=True, export_tangents=True)
    result = {"glb": str(glb)}
    # GLB is always generated for portable validation/preview even if another format is requested.
    if "fbx" in args.formats:
        for collider in colliders if index == 0 and args.engine not in RED_ENGINES else []:
            collider.hide_set(False)
            collider.select_set(True)
        path = folder / f"{args.name}_LOD{index}.fbx"
        bpy.ops.export_scene.fbx(filepath=str(path), use_selection=True, object_types={"MESH"}, bake_anim=False,
                                 add_leaf_bones=False, path_mode="COPY", embed_textures=True,
                                 use_tspace=True,
                                 axis_forward="-Y" if args.engine == "unreal" else "-Z",
                                 axis_up="Z" if args.engine == "unreal" else "Y",
                                 apply_unit_scale=True, apply_scale_options="FBX_SCALE_UNITS")
        result["fbx"] = str(path)
    if "obj" in args.formats:
        prep.activate(obj)
        path = folder / f"{args.name}_LOD{index}.obj"
        bpy.ops.wm.obj_export(filepath=str(path), export_selected_objects=True, export_uv=True, export_normals=True,
                               path_mode="RELATIVE")
        result["obj"] = str(path)
    return result, glb_metrics(glb)


def relative_paths(value, folder):
    if isinstance(value, dict):
        return {k: relative_paths(v, folder) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [relative_paths(v, folder) for v in value]
    if isinstance(value, str):
        try:
            path = Path(value)
            if path.is_absolute() and path.is_relative_to(folder):
                return path.relative_to(folder).as_posix()
        except (ValueError, OSError):
            pass
    return value


def main(argv=None):
    argv = argv if argv is not None else sys.argv[sys.argv.index("--") + 1:]
    args = parse_asset(argv)
    source = Path(args.src).expanduser().resolve()
    destination = Path(args.dst).expanduser().resolve()
    if source.is_relative_to(destination):
        raise ValueError("Choose a separate destination; source models are never overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=destination.name + ".building_", dir=destination.parent))
    report = {"schema_version": 2, "name": args.name, "engine": args.engine, "engine_preset": ENGINES[args.engine],
              "source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "blender": bpy.app.version_string, "options": vars(args), "stages": {}, "status": "building"}
    try:
        print("ASSET stage 1/7 component-aware surface cleanup", flush=True)
        obj = prep.import_mesh(str(source), object_name=args.source_object, visible_only=True,
                               source_up=args.source_up, yaw=args.yaw)
        has_source_materials = bool(obj.data.materials) and args.materials != "discard"
        report["normalization"] = normalize(obj, args)
        highs, originals, records, repair = component_pass(obj, args, load_regions(args.regions))
        report["components"], report["repair"] = records, repair
        (staging / "asset_progress.json").write_text(json.dumps(relative_paths(report, staging), indent=2) + "\n")
        report["stages"]["1_component_cleanup"] = "completed"
        print("ASSET components", len(highs), "protected", sum(x["protected"] for x in records), flush=True)
        print("ASSET stage 2/7 low mesh and high-resolution source", flush=True)
        lod0_parts, targets = build_low_parts(highs, args)
        report["component_triangle_budgets"] = targets
        report["reduction_fallbacks"] = [{"lod": 0, "component": i, "reason": x["pa_reduction_fallback"]}
                                          for i, x in enumerate(lod0_parts) if "pa_reduction_fallback" in x]
        print("ASSET stage 3/7 shared Smart UV atlas", flush=True)
        lod0_parts, uv = atlas(lod0_parts, args)
        report["uv"] = uv
        report["lod_uvs"] = [uv]
        report["stages"]["3_uv_efficiency"] = "completed"
        if args.texture_from:
            print("ASSET colour source", args.texture_from, flush=True)
            originals = painted_sources(lod0_parts, args.texture_from)
            has_source_materials = True
            report["texture_from_sha256"] = hashlib.sha256(Path(args.texture_from).read_bytes()).hexdigest()
        lod_parts = [lod0_parts]
        print("ASSET stage 5/7 LOD geometry", flush=True)
        for i in range(1, len(args.lod_ratios)):
            parts, _ = build_low_parts(lod0_parts, args, lod=i)
            report["reduction_fallbacks"] += [{"lod": i, "component": j, "reason": x["pa_reduction_fallback"]}
                                               for j, x in enumerate(parts) if "pa_reduction_fallback" in x]
            settings = copy(args)
            settings.texture_size = max(256, args.texture_size // (2 ** i))
            settings.uv_margin_px = max(1, args.uv_margin_px * settings.texture_size / args.texture_size)
            parts, lod_uv = atlas(parts, settings)
            report["lod_uvs"].append(lod_uv)
            lod_parts.append(parts)
        highs, originals, lod_parts, batch_info = batch_bake_parts(highs, originals, lod_parts, records, args.ray_distance)
        report["bake_groups"] = batch_info
        print("ASSET bake groups", batch_info, flush=True)
        print("ASSET stage 2/7 matched high-to-low PBR/normal/AO baking", flush=True)
        _, _, bake = bake_asset(highs, originals, lod_parts, args, staging / "textures", has_source_materials)
        report["bake"] = bake
        report["stages"]["2_high_to_low_bake"] = "completed"
        print("ASSET stage 4/7 asset and runtime budgets", flush=True)
        lods = []
        report["lods"] = []
        for i, parts in enumerate(lod_parts):
            obj = join(parts, args.name + f"_LOD{i}")
            obj.hide_render = i != 0
            obj.data.validate()
            lods.append(obj)
        print("ASSET stage 6/7 collision and engine exports", flush=True)
        colliders = collision_meshes(lods[0], args)
        collision = {"mode": args.collision, "hulls": len(colliders),
                     "triangles": sum(len(x.data.polygons) for x in colliders)}
        if colliders:
            prep.activate(colliders[0])
            for collider in colliders:
                collider.select_set(True)
            path = staging / "meshes" / (args.name + "_Collision.glb")
            path.parent.mkdir(exist_ok=True)
            bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=True)
            collision["glb"] = str(path)
        report["collision"] = collision
        high_join = join(highs, "ApprovedHigh")
        highs = [high_join]
        for i, obj in enumerate(lods):
            files, metrics = export_lod(obj, i, colliders, args, staging / "meshes")
            # Per-component distances avoid a nearby link hiding a failed projection on another component.
            # Joined objects retain part geometry; the global high surface is used only for final QA sampling.
            error = shape_error(high_join, obj)
            quality = prep.inspect_mesh(obj, True)
            if metrics["triangles"] != quality["triangles"]:
                raise ValueError("GLB triangle count differs from the checked Blender mesh")
            report["lods"].append({"index": i, "ratio": args.lod_ratios[i], "files": files,
                                   "quality": quality, "export_metrics": metrics, "shape_error": error,
                                   "textures": bake["lod_bakes"][i]["paths"],
                                   "alpha_mode": bake["lod_bakes"][i]["alpha_mode"],
                                   "emission_used": bake["lod_bakes"][i]["emission_used"],
                                   "screen_height": .5 / (2 ** i) if i < len(lods) - 1 else .01})
            # Compatibility provenance for pa.py texture; the game UV layout is preserved.
            provenance = {"status": "exported", "validation_passed": True, "options": {"unwrap": "smart"},
                          "outputs": [str(Path(files["glb"]).relative_to(staging))], "portable_root": "..",
                          "production_manifest": "../asset_report.json"}
            Path(files["glb"]).with_suffix(".report.json").write_text(json.dumps(provenance, indent=2) + "\n")
        if args.engine in RED_ENGINES:
            report["redengine_handoff"] = export_handoff(lods, colliders, args, staging, report)
        report["stages"]["4_budgets"] = "completed"
        report["stages"]["5_lods"] = "completed"
        report["stages"]["6_collision_engine_presets"] = "completed"
        print("ASSET stage 7/7 visual and import quality gates", flush=True)
        if not args.skip_previews:
            report["previews"] = previews(highs, lods, args, staging / "previews")
        report["quality"] = evaluate(report, args)
        # Keep editable high and low meshes. Original UV/color sources are retained hidden.
        for obj in highs + originals + lods[1:] + colliders:
            obj.hide_render = True
            obj.hide_set(True)
        lods[0].hide_render = False
        prep.activate(lods[0])
        if "blend" in args.formats:
            for image in bpy.data.images:
                if image.source in ("FILE", "GENERATED") and image.has_data:
                    try:
                        image.pack()
                    except RuntimeError:
                        pass
            path = staging / (args.name + ".blend")
            bpy.ops.wm.save_as_mainfile(filepath=str(path))
            report["editable_blend"] = str(path)
        report["status"] = "review_required" if report["quality"]["review_required"] else "exported"
        report["validation_passed"] = report["quality"]["technical_passed"]
        portable = relative_paths(report, staging)
        manifest = staging / "asset_report.json"
        manifest.write_text(json.dumps(portable, indent=2) + "\n")
        report["engine_check"] = check_engine(manifest, args.engine, args.engine_bin, args.engine_project, args.engine_check)
        report["stages"]["7_quality_gates"] = "completed"
        if report["engine_check"]["status"] == "failed":
            report["quality"]["technical_passed"] = False
            report["validation_passed"] = False
        if args.engine_check == "required" and report["engine_check"]["status"] != "passed":
            report["validation_passed"] = False
        report["review_page"] = write_gallery(report, staging)
        portable = relative_paths(report, staging)
        manifest.write_text(json.dumps(portable, indent=2) + "\n")
        if not report["validation_passed"] or (args.quality == "strict" and report["quality"]["review_required"]):
            raise ValueError("Asset failed the configured quality gates; inspect the saved report")
        if destination.exists():
            previous = destination.with_name(destination.name + ".previous")
            if previous.exists():
                shutil.rmtree(previous)
            destination.rename(previous)
        staging.rename(destination)
        if report["quality"]["review_required"]:
            print("ASSET NEEDS REVIEW", destination / "asset_report.json", flush=True)
        else:
            print("ASSET OK", destination / "asset_report.json", flush=True)
        return portable
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        (staging / "asset_report.json").write_text(json.dumps(relative_paths(report, staging), indent=2) + "\n")
        print("ASSET FAILED report", staging / "asset_report.json", flush=True)
        raise


if __name__ == "__main__":
    main()
