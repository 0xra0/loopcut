"""The shading health check (shading.py) through the real cloud model import (cloud_jobs._model_import).

    Blender --factory-startup --python-exit-code 1 --python loopcut/harness/shading_check.py [-- --keep]
    python3 loopcut/windows/laptop.py check --script loopcut/harness/shading_check.py --wait   (SPLAP)

Fixtures, each written by Blender's own exporters and imported as a cloud result would be:
- Ball.glb (an image-to-3D result): a 48x24 UV sphere with flat custom normals and no normal map. glTF
  splits its vertices at every face. Expected: welded, custom normals cleared, shaded smooth, said so.
- Head.glb (a retopology result): the same sphere with a normal map named retopo_*_normal wired in, the
  pairing old retopology results shipped with. Expected: left as it is, reported as baked against flat
  normals with its source job, its source high-poly's SHA-256 recorded; a moved vertex reported as a
  change after its bake; a Subdivision modifier named as showing the grid.
- Crate.glb: a subdivided cube with flat custom normals (hard surface). Expected: left alone, no problem.
- Ball.fbx: the flat-normal sphere as FBX (vertices kept joined). Expected: fixed like Ball.glb.
- The advisory "shading" kind fails (advisory) on Head and passes on Ball; check_shading lists them all.
- On SPLAP only, when present: C:\\Loopcut\\godot\\retopo_model.fbx (an old result with its baked map)
  reports the baked pair and keeps its custom normals; C:\\Loopcut\\godot\\slice_new\\model.fbx (fixed
  server bake) does not. Absent files are named, never failed.
Writes shading-check.json to the working folder (a SPLAP job's folder). Exits 0 when every check passes.
"""

import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
from loopcut import acceptance, cloud_jobs, shading  # noqa: E402

TIMEOUT = 300
RESULTS = []
REAL = [(Path(r"C:\Loopcut\godot\retopo_model.fbx"), True), (Path(r"C:\Loopcut\godot\slice_new\model.fbx"), False)]
HIGH = "ab" * 32


def check(name: str, ok, detail="") -> None:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:600]})
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  [{str(detail)[:600]}]" if detail and not ok else ""), flush=True)


def new_object(name: str, build):
    """A mesh object from a bmesh builder, linked to the scene (no operators: a GUI run's script has no
    viewport context)."""
    import bmesh
    bm = bmesh.new()
    bm.loops.layers.uv.new("UVMap")
    build(bm)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    ob = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def sphere(name: str, normal_map: bool = False):
    import bmesh
    ob = new_object(name, lambda bm: bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=24, radius=0.5,
                                                                calc_uvs=True))
    mesh = ob.data
    flat_normals(mesh)
    if normal_map:
        image = bpy.data.images.new("retopo_test_normal", 64, 64, alpha=False)
        image.colorspace_settings.name = "Non-Color"
        image.pixels.foreach_set([0.5, 0.5, 1.0, 1.0] * 64 * 64)
        material = bpy.data.materials.new("Skin")
        material.use_nodes = True
        nodes, links = material.node_tree.nodes, material.node_tree.links
        texture = nodes.new("ShaderNodeTexImage")
        texture.image = image
        normal = nodes.new("ShaderNodeNormalMap")
        links.new(texture.outputs["Color"], normal.inputs["Color"])
        links.new(normal.outputs["Normal"], nodes["Principled BSDF"].inputs["Normal"])
        mesh.materials.append(material)
    return ob


def flat_normals(mesh) -> None:
    """Custom normals equal to each corner's face normal (corners run face by face in a fresh primitive)."""
    normals = []
    for polygon in mesh.polygons:
        normals += [tuple(polygon.normal)] * polygon.loop_total
    mesh.normals_split_custom_set(normals)


def crate(name: str):
    import bmesh

    def build(bm):
        bmesh.ops.create_cube(bm, size=1.0, calc_uvs=True)
        bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=2, use_grid_fill=True)
    ob = new_object(name, build)
    flat_normals(ob.data)
    return ob


def export(ob, path: Path) -> None:
    bpy.context.view_layer.update()  # Objects linked or removed through bpy.data: sync the view layer first.
    for other in bpy.context.scene.objects:
        other.select_set(other == ob)
    if path.suffix == ".glb":
        bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=True, export_normals=True)
    else:
        bpy.ops.export_scene.fbx(filepath=str(path), use_selection=True)
    bpy.data.objects.remove(ob)


def job(job_id: str, tool: str, path: Path) -> dict:
    return {"id": job_id, "tool": tool, "status": "succeeded", "error": None, "outputs": [
        {"type": "model_3d", "path": str(path), "name": "model"}],
        "provenance": {"job_id": job_id, "tool": tool, "kind": "gen" if tool == "image_to_3d" else "assist",
                       "vendor": "test", "model": "test", "created_at": "2026-10-31T00:00:00Z"}}


def imported(job_value: dict) -> tuple[list, list]:
    """(mesh objects the import made, its shading lines)."""
    before = {ob.session_uid for ob in bpy.data.objects}
    cloud_jobs._model_import(job_value)
    meshes = [ob for ob in bpy.data.objects if ob.session_uid not in before and ob.type == "MESH"]
    return meshes, list(job_value.get("shading") or [])


def facts(ob) -> dict:
    data = shading.arrays(ob.data)
    return shading.analyse(data["positions"], data["loop_start"], data["loop_total"], data["corner_verts"],
                           data["normals"], data["uvs"])


def main() -> int:
    keep = "--keep" in sys.argv
    folder = Path(tempfile.mkdtemp(prefix="lc_shading_check_")).resolve()
    t0 = time.time()
    try:
        for ob in list(bpy.data.objects):  # The factory scene's cube, camera and light.
            bpy.data.objects.remove(ob)
        bpy.context.view_layer.update()
        export(sphere("Ball"), folder / "Ball.glb")
        export(sphere("Head", normal_map=True), folder / "Head.glb")
        export(crate("Crate"), folder / "Crate.glb")
        export(sphere("BallF"), folder / "Ball.fbx")
        for leftover in list(bpy.data.meshes) + list(bpy.data.materials) + list(bpy.data.images):
            if leftover.users == 0:
                getattr(bpy.data, {"Mesh": "meshes", "Material": "materials", "Image": "images"}[type(leftover).__name__]).remove(leftover)

        # Ball.glb: an image-to-3D result with flat custom normals, split vertices, no map: the safe fix.
        ball, said = imported(job("job_shading_ball", "image_to_3d", folder / "Ball.glb"))
        ball = ball[0]
        fact = facts(ball)
        check("Ball.glb: welded, custom normals cleared, smooth, no open edges",
              not ball.data.has_custom_normals and fact["open"] == 0 and fact["flat"] is not None
              and fact["flat"] < 0.05 and len(ball.data.vertices) == 48 * 23 + 2 and ball.get(shading.FIXED),
              f"{fact} verts={len(ball.data.vertices)} fixed={ball.get(shading.FIXED)}")
        check("Ball.glb: the import says what it did, in plain words",
              any("flat on a curved, organic surface" in line and "shaded it smooth" in line for line in said), said)
        record = shading.record(ball) or {}
        check("Ball.glb: its bake is recorded with its job, after the fix",
              record.get("job") == "job_shading_ball" and record.get("shape") and ball.get(shading.SOURCE_JOB)
              == "job_shading_ball" and not shading.measure(ball)["changed"], record)

        # Head.glb: a retopology result whose normal map was baked against its flat normals.
        with cloud_jobs._lock:
            cloud_jobs._registry["job_shading_head"] = {"files": {"model": HIGH}, "job": {"status": "succeeded"}}
        try:
            head, said = imported(job("job_shading_head", "retopology", folder / "Head.glb"))
        finally:
            with cloud_jobs._lock:
                cloud_jobs._registry.pop("job_shading_head", None)
        head = head[0]
        fact = facts(head)
        check("Head.glb: the baked pair is left as it is (custom normals kept, still flat)",
              head.data.has_custom_normals and fact["flat"] is not None and fact["flat"] > 0.95
              and not head.get(shading.FIXED), fact)
        text = " ".join(said)
        check("Head.glb: reported as baked against flat normals, an earlier retopology result, with its source job",
              all(words in text for words in ("baked against flat normals", "an earlier retopology result",
                                              "prints a grid of quads", "source job job_shading_head")), said)
        check("Head.glb: its source high-poly's SHA-256 and job are recorded on the object",
              head.get(shading.HIGH_SHA) == HIGH and (shading.record(head) or {}).get("high_sha256") == HIGH
              and (shading.record(head) or {}).get("maps"), dict(head.items()) if hasattr(head, "items") else "")
        root = head.parent
        advisory = acceptance.run([{"kind": "shading", "name": root.name}], advisory=True)[0]
        check("the advisory shading kind misses on Head (advisory, never a failure of the worker)",
              advisory["status"] == "fail" and advisory.get("advisory") and "baked against flat normals" in advisory["detail"],
              advisory)
        head.data.vertices[0].co.z += 0.02
        head.data.update()
        output = shading.check_shading(head.name).text
        check("a moved vertex is a change after its bake", "changed after its bake (shape" in output, output)
        head.modifiers.new("Subdivision", "SUBSURF")
        output = shading.check_shading(head.name).text
        check("a Subdivision modifier is named as already showing the grid",
              "Its Subdivision modifier already shows that grid" in output, output)

        # Crate.glb: hard surface, flat custom normals are right for it.
        crate_ob, said = imported(job("job_shading_crate", "image_to_3d", folder / "Crate.glb"))
        crate_ob = crate_ob[0]
        result = shading.measure(crate_ob)
        check("Crate.glb: hard-surface, left alone, nothing reported",
              result["fact"]["shape"] == "hard_surface" and crate_ob.data.has_custom_normals
              and not crate_ob.get(shading.FIXED) and not result["verdict"]["problems"] and not said,
              f"{result['fact']} {result['verdict']} {said}")

        # Ball.fbx: joined vertices, flat custom normals: fixed too.
        ball_fbx, said = imported(job("job_shading_fbx", "image_to_3d", folder / "Ball.fbx"))
        ball_fbx = ball_fbx[0]
        check("Ball.fbx: custom normals cleared and smooth",
              not ball_fbx.data.has_custom_normals and facts(ball_fbx)["flat"] < 0.05 and ball_fbx.get(shading.FIXED),
              f"{facts(ball_fbx)} {said}")
        advisory = acceptance.run([{"kind": "shading", "name": ball.parent.name}], advisory=True)[0]
        check("the advisory shading kind passes on the fixed Ball", advisory["status"] == "pass", advisory)
        for ob in bpy.context.scene.objects:
            ob.select_set(False)
        listing = shading.check_shading().text
        check("check_shading with nothing selected lists every visible mesh",
              listing.startswith("Checked 4 meshes") and all(n in listing for n in (ball.name, head.name, crate_ob.name)),
              listing)

        # Real results on SPLAP, when they are there.
        for path, baked in REAL:
            if not path.is_file():
                print(f"skip {path} (not on this machine)", flush=True)
                continue
            meshes, said = imported(job(f"job_real_{path.parent.name}", "retopology", path))
            text = " ".join(said)
            for result in shading.check(meshes):
                if "fact" in result:
                    print("REAL", path.name, shading.summary(result["name"], result["fact"], custom=result["custom"],
                                                             maps=result["maps"]), flush=True)
            print("REAL SAID", path.name, said, flush=True)
            kept = all(ob.data.has_custom_normals for ob in meshes) if baked else True
            check(f"{path.name} ({'old baked pair' if baked else 'fixed bake'}): "
                  + ("reported and left as it is" if baked else "no baked-pair report"),
                  meshes and kept and (("baked against flat normals" in text) == baked),
                  f"{[shading.summary(r['name'], r['fact'], custom=r['custom'], maps=r['maps']) for r in shading.check(meshes) if 'fact' in r]} {said}")
    except Exception:
        traceback.print_exc()
        check("ran to the end", False, "exception above")
    finally:
        if not keep:
            shutil.rmtree(folder, ignore_errors=True)
    passed = sum(r["ok"] for r in RESULTS)
    summary = {"passed": passed, "total": len(RESULTS), "seconds": round(time.time() - t0, 1), "results": RESULTS}
    try:
        (Path.cwd() / "shading-check.json").write_text(json.dumps(summary, indent=1))
    except OSError:
        pass
    print(f"SHADING {passed}/{len(RESULTS)} in {summary['seconds']}s")
    print("SHADING OK" if passed == len(RESULTS) and RESULTS else "SHADING FAIL", flush=True)
    return 0 if passed == len(RESULTS) and RESULTS else 1


def run_and_exit() -> None:
    """main() with a window's context when there is one (exporters are operators), then exit with its code."""
    code = 1
    try:
        windows = bpy.context.window_manager.windows
        if windows:
            with bpy.context.temp_override(window=windows[0], screen=windows[0].screen):
                code = main()
        else:
            code = main()
    except BaseException:
        traceback.print_exc()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)


if __name__ == "__main__":
    watchdog = threading.Timer(TIMEOUT, lambda: (print(f"SHADING FAIL: timed out after {TIMEOUT}s", flush=True),
                                                  os._exit(3)))
    watchdog.daemon = True
    watchdog.start()
    if bpy.app.background:
        run_and_exit()
    else:  # The GUI runs --python before its windows are up: start once they are.
        bpy.app.timers.register(run_and_exit, first_interval=0.5)
