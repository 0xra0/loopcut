"""Re-bake in place (rebake.py) with real re-bake results: the runner's own rebake_job.py bakes what the add-on sent.

    python3 loopcut/windows/laptop.py check --script loopcut/harness/rebake_apply_check.py --wait   (SPLAP)

Needs, on SPLAP: the runner's rebake_job.py and bake_common.py in LOOPCUT_RUNNER_DIR (default
C:\\Loopcut\\godot\\ga2\\runner, copied from loopcut-web workers/runner), C:\\Loopcut\\godot\\muse_high.glb (the Muse
high-poly), retopo_model.fbx (an old flat-normal retopology result of it) and slice_new\\model.fbx (a fixed one).
Missing files fail the check by name.

The job HTTP layer is a fake service in this process (blob check and upload, job create, job view, downloads), as the
cloud_jobs tests mock it; its job create runs rebake_job.py in a background Blender on exactly the uploaded bytes,
with the kept high-poly, and answers as the platform's rebake adapter does. The add-on side runs as cloud.execute
runs it: _validate_arguments, resolve_files (rebake.prepare's export), prepare_inputs, resolve_stored, run, then
apply_result.

- A: the old flat-normal result, imported as a retopology result (its high-poly's hash recorded) and subdivided once
  (quads: FBX). check_shading names the re-bake tool; the re-bake keeps its UVs and puts the new maps on its own
  material in place; afterwards its normals are smooth, no hard edges, its record names the re-bake and matches,
  check_shading reports no problem, and its corner normals now equal those of the bake target the runner baked against
  (normal_gap), where before they did not: a map shows the quad grid exactly where those differ.
- B: the fixed result, imported without a recorded hash, triangulated (GLB), re-baked with keep_uvs false: the hash is
  read from the source job's view and recorded, the new UVs are matched onto its faces (UV area and flipped share as
  returned), the maps go on its material, its record matches.
- C: a mesh with no source is refused in plain words before any request.
- D: a mesh changed while baking gets the result as a new object, never in place.
Writes rebake-apply-check.json to the working folder. Exits 0 when every check passes.
"""

import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
from loopcut import cloud, cloud_http, cloud_jobs, rebake, shading  # noqa: E402

TIMEOUT = 1200
RESULTS = []
GODOT = Path(r"C:\Loopcut\godot")
RUNNER = Path(os.environ.get("LOOPCUT_RUNNER_DIR", str(GODOT / "ga2" / "runner")))
HIGH_PATH, OLD_PATH, FIXED_PATH = GODOT / "muse_high.glb", GODOT / "retopo_model.fbx", GODOT / "slice_new" / "model.fbx"
PROJECT = {"id": "rebake-check", "no_generation": False}
DESCRIPTOR = {
    "name": "rebake", "namespace": "loopcut_cloud", "version": 1, "title": "Re-bake textures", "kind": "assist",
    "status": "live", "available": True, "backend": "managed", "backends": ["managed"], "apply": "import_model",
    "latency": "minutes", "description": "Bakes fresh maps. It usually costs 5 credits, never more than the quote.",
    "input_schema": {"type": "object", "properties": {
        "texture_size": {"type": "integer", "enum": [1024, 2048]}, "keep_uvs": {"type": "boolean"}},
        "required": [], "additionalProperties": False},
    "file_inputs": [{"field": "high", "accept": ["model/gltf-binary"], "max_mb": 150, "required": True},
                    {"field": "model", "accept": ["model/gltf-binary", "model/vnd.fbx"], "max_mb": 50,
                     "required": True}],
    "cost": {"credits_estimate": 5, "quote_required": False}, "approval": {"confirm_over_credits": 100}}


def check(name: str, ok, detail="") -> None:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:900]})
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  [{str(detail)[:900]}]" if detail else ""), flush=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Reply(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(json.dumps(data).encode() if isinstance(data, dict) else data)
        self.status, self.headers = status, headers or {}


class Service:
    """Loopcut's job API as far as a re-bake uses it; the runner's rebake_job.py does the job."""

    def __init__(self, folder: Path, high: str):
        self.folder, self.high = folder, high
        self.kept = {high: HIGH_PATH}
        self.mimes, self.served, self.views, self.calls, self.reports = {}, {}, {}, [], []
        self.count = 0

    def __call__(self, request, *, timeout):
        url = request.full_url
        self.calls.append((request.method, url.split("?")[0]))
        if url.endswith("/blobs/check"):
            return Reply({"missing": [h for h in json.loads(request.data)["sha256"] if h not in self.kept]})
        if url.endswith("/blobs/upload-urls"):
            items = json.loads(request.data)["items"]
            for item in items:
                self.mimes[item["sha256"]] = item["mime"]
            return Reply({"items": [{"sha256": item["sha256"], "put_url": f"https://storage.test/put/{item['sha256']}"
                                     "?signature=test", "expires_at": "2099-01-01T00:00:00Z"} for item in items]})
        if request.method == "PUT":
            digest = url.split("/put/")[1].split("?")[0]
            path = self.folder / f"blob-{digest[:16]}"
            path.write_bytes(request.data.read())
            self.kept[digest] = path
            return Reply(b"")
        if url.endswith("/v1/jobs") and request.method == "POST":
            return Reply(self.create(json.loads(request.data)))
        if url.startswith("https://storage.test/out/"):
            return Reply(self.served[url.split("/out/")[1].split("?")[0]].read_bytes())
        for job_id, view in self.views.items():
            if url.split("?")[0].endswith("/v1/jobs/" + job_id):
                return Reply(view)
        raise AssertionError(f"unexpected request {request.method} {url}")

    def create(self, payload: dict) -> dict:
        files, inputs = payload["files"], payload["input"]
        assert set(files) == {"high", "model"} and files["high"] == self.high, files
        model = self.kept[files["model"]]
        fmt = "fbx" if self.mimes.get(files["model"]) == "model/vnd.fbx" else "glb"
        self.count += 1
        job_id = f"job_rebake_check{self.count}"
        base = self.folder / job_id
        for name in ("in", "out", "work"):
            (base / name).mkdir(parents=True)
        shutil.copyfile(model, base / "in" / f"model.{fmt}")
        spec = {"input": str(HIGH_PATH), "model": str(base / "in" / f"model.{fmt}"), "model_format": fmt,
                "out_dir": str(base / "out"), "work_dir": str(base / "work"),
                "texture_size": inputs.get("texture_size", 2048), "keep_uvs": inputs.get("keep_uvs", True),
                "threads": 8, "max_input_triangles": 2_000_000, "max_model_triangles": rebake.MAX_TRIANGLES,
                "name_stem": f"rebake_{files['model'][:8]}_{job_id[-8:]}", "log_tag": "rebake",
                "deadline": time.time() + 600}
        t0 = time.time()
        run = subprocess.run([bpy.app.binary_path, "-b", "--factory-startup", "--python",
                              str(RUNNER / "rebake_job.py"), "--", json.dumps(spec)],
                             capture_output=True, text=True, timeout=600)
        seconds = round(time.time() - t0, 1)
        report = json.loads((base / "out" / "report.json").read_text())
        self.reports.append({"job": job_id, "seconds": seconds, "exit": run.returncode, "report": report})
        print(f"RUNNER {job_id} exit {run.returncode} in {seconds}s: {json.dumps(report)[:700]}", flush=True)
        if run.returncode != 0:
            print("RUNNER LOG", (run.stdout + run.stderr)[-3000:], flush=True)
            return self.view(job_id, files, "failed", error={"code": "invalid_input", "type": "invalid_request",
                                                             "message": report.get("message", "failed")})
        out = base / "out" / report["file"]
        digest = sha256(out)
        self.served[digest] = out
        uv = report.get("uv") or {}
        shading_report = (report.get("bake") or {}).get("shading") or {}
        meta = {"format": fmt, "size": out.stat().st_size, "sha256": digest, **(report.get("mesh") or {}),
                "input_triangles": report.get("input_triangles"), "high_sha256": files["high"],
                "uv": {"source": uv.get("source"), "reason": uv.get("reason"), "area": uv.get("area"),
                       "outside_pct": uv.get("outside_pct"), "overlap_pct": uv.get("overlap_pct")},
                "shading": {"custom_normals_cleared": shading_report.get("custom_normals_cleared") is True,
                            "sharp_edges_cleared": shading_report.get("sharp_edges_cleared")}}
        output = {"name": "model", "type": "model_3d", "mime": "model/vnd.fbx" if fmt == "fbx" else "model/gltf-binary",
                  "sha256": digest, "size": out.stat().st_size, "meta": meta,
                  "url": f"https://storage.test/out/{digest}?signature=test", "expires_at": "2099-01-01T00:00:00Z"}
        return self.view(job_id, files, "succeeded", outputs=[output])

    @staticmethod
    def view(job_id, files, status, outputs=(), error=None, tool="rebake"):
        return {"id": job_id, "tool": tool, "status": status, "progress": {"pct": 100, "message": "Done"},
                "credits": {"reserved": 5, "charged": 5 if status == "succeeded" else 0, "refunded": 0},
                "outputs": list(outputs), "error": error, "files": dict(files),
                "provenance": {"job_id": job_id, "tool": tool, "vendor": "gcp_runner", "model": "loopcut-rebake-1",
                               "kind": "assist", "created_at": "2026-10-31T00:00:00Z"},
                "created_at": "2026-10-31T00:00:00Z", "finished_at": "2026-10-31T00:01:00Z"}

    def client(self):
        return cloud_http.Client(base_url="https://service.test", api_key="lc_test", transport=self,
                                 client_headers={"x-loopcut-contract": "0.11", "x-loopcut-client": "rebake-check"})


def imported(job_id: str, path: Path, high: str = "") -> object:
    """A retopology result imported as a cloud model is; its high-poly's hash known only when high is given."""
    job = {"id": job_id, "tool": "retopology", "status": "succeeded", "error": None,
           "outputs": [{"type": "model_3d", "path": str(path), "name": "model"}],
           "provenance": {"job_id": job_id, "tool": "retopology", "kind": "assist", "vendor": "test", "model": "test",
                          "created_at": "2026-10-31T00:00:00Z"}}
    if high:
        with cloud_jobs._lock:
            cloud_jobs._registry[job_id] = {"files": {"model": high}, "job": {"status": "succeeded"}}
    before = {ob.session_uid for ob in bpy.data.objects}
    try:
        cloud_jobs._model_import(job)
    finally:
        with cloud_jobs._lock:
            cloud_jobs._registry.pop(job_id, None)
    meshes = [ob for ob in bpy.data.objects if ob.session_uid not in before and ob.type == "MESH"]
    assert len(meshes) == 1, [ob.name for ob in meshes]
    return meshes[0]


def replace_mesh(ob, mesh) -> None:
    old = ob.data
    ob.data = mesh
    if old.users == 0:
        bpy.data.meshes.remove(old)


def subdivided(ob) -> None:
    """Its mesh subdivided once (Catmull-Clark, UVs kept), as a user applying a Subdivision modifier."""
    modifier = ob.modifiers.new("Subdivision", "SUBSURF")
    modifier.levels = modifier.render_levels = 1
    bpy.context.view_layer.update()
    mesh = bpy.data.meshes.new_from_object(ob.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    ob.modifiers.remove(modifier)
    replace_mesh(ob, mesh)


def triangulated(ob) -> None:
    import bmesh
    bm = bmesh.new()
    try:
        bm.from_mesh(ob.data)
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        bm.to_mesh(ob.data)
    finally:
        bm.free()
    ob.data.update()


def facts(ob) -> dict:
    data = shading.arrays(ob.data)
    return shading.analyse(data["positions"], data["loop_start"], data["loop_total"], data["corner_verts"],
                           data["normals"], data["uvs"])


def bsdf_image(ob, socket: str):
    material = ob.material_slots[0].material
    image, _ = rebake._feed(rebake._bsdf(material).inputs[socket])
    return image.image if image is not None else None


def normal_gap(ob, path: Path) -> dict:
    """Angles (degrees) between its corner normals and the bake target's (the returned model, its normals as the
    runner baked against them), corner by corner. A tangent-space map shows a grid wherever these differ: baked
    against flat faces, shaded smooth."""
    import numpy as np
    from mathutils import Matrix
    before = {o.session_uid for o in bpy.data.objects}
    if path.suffix.lower() == ".fbx":
        cloud_jobs._import_fbx(path)
    else:
        bpy.ops.import_scene.gltf(filepath=str(path))
    bpy.context.view_layer.update()
    made = [o for o in bpy.data.objects if o.session_uid not in before]
    try:
        theirs = next(o for o in made if o.type == "MESH")
        space = rebake._space(shading.record(ob)) or Matrix.Identity(4)

        def normals(mesh, matrix):
            values = np.empty(len(mesh.loops) * 3, dtype=np.float32)
            mesh.corner_normals.foreach_get("vector", values)
            rotation = np.array(matrix.to_3x3().inverted().transposed(), dtype=np.float64)
            out = values.reshape(-1, 3).astype(np.float64) @ rotation.T
            return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)
        points, starts, totals = rebake._corners(ob.data, space)
        their_points, their_starts, their_totals = rebake._corners(theirs.data, theirs.matrix_world)
        size = float(np.linalg.norm(points.max(axis=0) - points.min(axis=0)))
        matched, why = rebake.match_corners(points, starts, totals, their_points, their_starts, their_totals,
                                            normals(theirs.data, theirs.matrix_world), size * 1e-4)
        if matched is None:
            return {"why": why}
        mine = normals(ob.data, space)
        angle = np.degrees(np.arccos(np.clip((mine * matched).sum(axis=1), -1, 1)))
        return {"p50": round(float(np.percentile(angle, 50)), 3), "p95": round(float(np.percentile(angle, 95)), 3),
                "max": round(float(angle.max()), 2), "over_2deg": round(float((angle > 2).mean()), 4)}
    finally:
        for o in made:
            bpy.data.objects.remove(o)


def uv_shape(mesh) -> dict:
    """The render UV map's summed triangle area and the share of triangles it flips."""
    import numpy as np
    mesh.calc_loop_triangles()
    count = len(mesh.loop_triangles)
    loops = np.empty(count * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("loops", loops)
    layer = rebake._layer(mesh)
    uv = np.empty(len(mesh.loops) * 2, dtype=np.float64)
    layer.uv.foreach_get("vector", uv)
    tri = uv.reshape(-1, 2)[loops].reshape(count, 3, 2)
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    signed = 0.5 * ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0]))
    return {"area": round(float(np.abs(signed).sum()), 5), "flipped": round(float((signed < 0).mean()), 4),
            "inside": bool(uv.min() >= -1e-4 and uv.max() <= 1 + 1e-4)}


def call(service, arguments: dict) -> tuple[dict, str]:
    """The rebake call as cloud.execute makes it, minus its card: (job, apply_result's summary)."""
    cloud._validate_arguments(DESCRIPTOR, arguments)
    folder = Path(tempfile.mkdtemp(prefix="lc_rebake_call_"))
    files = cloud_jobs.resolve_files(DESCRIPTOR, arguments, folder)
    arguments = cloud_jobs.prepare_inputs(DESCRIPTOR, arguments, files)
    client = service.client()
    cloud_jobs.resolve_stored(files, client, None, lambda fn: fn())
    job = cloud_jobs.run(DESCRIPTOR, arguments, files, project=PROJECT, client=client)
    return job, cloud_jobs.apply_result(DESCRIPTOR, job)


def mesh_names():
    return sorted(ob.name for ob in bpy.data.objects if ob.type == "MESH")


def main() -> int:
    folder = Path(tempfile.mkdtemp(prefix="lc_rebake_check_")).resolve()
    t0 = time.time()
    try:
        missing = [str(p) for p in (RUNNER / "rebake_job.py", RUNNER / "bake_common.py", HIGH_PATH, OLD_PATH,
                                    FIXED_PATH) if not p.is_file()]
        check("inputs are on this machine", not missing, missing)
        if missing:
            return 1
        for ob in list(bpy.data.objects):
            bpy.data.objects.remove(ob)
        bpy.context.view_layer.update()
        high = sha256(HIGH_PATH)
        service = Service(folder, high)

        # C: a mesh with no source is refused before any request.
        plain = bpy.data.objects.new("Plain", bpy.data.meshes.new("Plain"))
        import bmesh
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=1.0)
        bm.to_mesh(plain.data)
        bm.free()
        bpy.context.scene.collection.objects.link(plain)
        try:
            cloud_jobs.resolve_files(DESCRIPTOR, {"model": "Plain"}, folder / "c")
            refused = ""
        except Exception as ex:  # noqa: BLE001
            refused = str(ex)
        check("C: a mesh with no source is refused in plain words, nothing sent",
              refused == rebake.NO_SOURCE.format(name="Plain") and not service.calls, refused)
        bpy.data.objects.remove(plain)

        # A: the old flat-normal result, subdivided: kept UVs, FBX, maps in place.
        old = imported("job_src_old", OLD_PATH, high)
        subdivided(old)
        before = facts(old)
        material = old.material_slots[0].material.name
        with patch.object(rebake, "offered", return_value=True):
            said = shading.check_shading(old.name).text
        check("A: check_shading names the re-bake tool for the subdivided result",
              "changed after its bake" in said and f"Fix: loopcut_cloud__rebake with model={old.name!r}" in said, said)
        meshes, uid = mesh_names(), old.session_uid
        old.select_set(True)
        bpy.context.view_layer.objects.active = old
        normals_before = {}
        real_apply = cloud_jobs.apply_result

        def measured_apply(descriptor, value, **options):  # Its normals against the bake target's, before it applies.
            normals_before.update(normal_gap(old, Path(value["outputs"][0]["path"])))
            return real_apply(descriptor, value, **options)
        with patch.object(cloud_jobs, "apply_result", measured_apply):
            job, summary = call(service, {"texture_size": 1024})  # No model named: the selection.
        print("A SUMMARY", summary, flush=True)
        meta = job["outputs"][0]["meta"]
        after = facts(old)
        record = shading.record(old) or {}
        check("A: sent as FBX (quads), the high-poly by hash, only the low-poly uploaded",
              service.mimes and list(service.mimes.values()) == ["model/vnd.fbx"] and high not in service.mimes,
              service.mimes)
        check("A: the runner kept its UVs", meta["uv"]["source"] == "kept", meta["uv"])
        check("A: in place: same object, no new mesh, same material",
              old.session_uid == uid and mesh_names() == meshes and old.material_slots[0].material.name == material
              and summary.startswith("Re-baked in place"), (mesh_names(), meshes, summary))
        new_map, base = bsdf_image(old, "Normal"), bsdf_image(old, "Base Color")
        check("A: its material now samples the re-baked normal and base colour maps",
              new_map is not None and new_map.name.startswith("rebake_") and base is not None
              and base.name.startswith("rebake_"), (getattr(new_map, "name", None), getattr(base, "name", None)))
        check("A: shaded as the bake target: no custom normals, smooth, no hard edges",
              not old.data.has_custom_normals and after["flat"] is not None and after["flat"] < 0.05
              and after["hard"] < 0.01, {"before": before, "after": after})
        check("A: its bake record names the re-bake and matches the mesh",
              record.get("job") == job["id"] and record.get("tool") == "rebake" and record.get("high_sha256") == high
              and not shading.measure(old)["changed"] and rebake.SENT not in old, record)
        listing = shading.check_shading(old.name).text
        check("A: check_shading reads it clean", "bake: matches its record" in listing and "Problem" not in listing,
              listing)
        normals_after = normal_gap(old, Path(job["outputs"][0]["path"]))
        check("A: no quad grid: it now shades with the normals its new maps were baked against (it did not before)",
              normals_after.get("p95", 99) < 1.0 and normals_after.get("over_2deg", 1) < 0.001
              and normals_before.get("over_2deg", 0) > 0.05,
              {"before": normals_before, "after": normals_after, "flat_before": before["flat"],
               "flat_after": after["flat"], "hard_before": before["hard"], "hard_after": after["hard"]})

        # D: changed while baking: the result goes in as a new object. A's job again, with A's mark put back and a
        # vertex moved since.
        mark = {"model": cloud_jobs._job_files(job)["model"], **rebake.fingerprint(old.data)}
        old[rebake.SENT] = json.dumps(mark)
        old.data.vertices[0].co.z += 0.01
        old.data.update()
        count = len(mesh_names())
        again = cloud_jobs.apply_result(DESCRIPTOR, job)
        check("D: a mesh changed while baking gets the result as a new object, never in place",
              "as a new object" in again and "changed while it was baking" in again and len(mesh_names()) == count + 1,
              again)

        # B: the fixed result, triangulated (GLB), no recorded hash: looked up from its source job; unwrapped.
        for ob in list(bpy.data.objects):
            bpy.data.objects.remove(ob)
        fixed = imported("job_src_fixed", FIXED_PATH)
        service.views["job_src_fixed"] = Service.view("job_src_fixed", {"model": high}, "succeeded",
                                                      tool="retopology")
        triangulated(fixed)
        old_uv = uv_shape(fixed.data)
        faces = len(fixed.data.polygons)
        job, summary = call(service, {"model": fixed.name, "keep_uvs": False, "texture_size": 1024})
        print("B SUMMARY", summary, flush=True)
        meta = job["outputs"][0]["meta"]
        check("B: sent as GLB (triangles); its high-poly read from the source job and recorded",
              service.mimes.get(cloud_jobs._job_files(job)["model"]) == "model/gltf-binary"
              and ("GET", "https://service.test/v1/jobs/job_src_fixed") in service.calls
              and fixed.get(shading.HIGH_SHA) == high, (service.calls[-6:], dict(fixed.items())))
        check("B: the runner unwrapped it as asked", meta["uv"]["source"] == "unwrapped"
              and meta["uv"]["reason"] == "requested", meta["uv"])
        returned = Path(job["outputs"][0]["path"])
        before_ids = {ob.session_uid for ob in bpy.data.objects}
        bpy.ops.import_scene.gltf(filepath=str(returned))
        theirs = next(ob for ob in bpy.data.objects if ob.session_uid not in before_ids and ob.type == "MESH")
        their_uv = uv_shape(theirs.data)
        for ob in [ob for ob in bpy.data.objects if ob.session_uid not in before_ids]:
            bpy.data.objects.remove(ob)
        mine = uv_shape(fixed.data)
        check("B: its UVs are the returned ones, matched onto its own faces",
              len(fixed.data.polygons) == faces and mine != old_uv and mine["inside"]
              and abs(mine["area"] - their_uv["area"]) <= 1e-4 * max(their_uv["area"], 1e-6)
              and abs(mine["flipped"] - their_uv["flipped"]) < 1e-3 and "new UVs from the re-bake" in summary,
              {"before": old_uv, "after": mine, "returned": their_uv})
        gap = normal_gap(fixed, returned)
        check("B: it shades with the normals its new maps were baked against",
              gap.get("p95", 99) < 1.0 and gap.get("over_2deg", 1) < 0.001, gap)
        new_map = bsdf_image(fixed, "Normal")
        check("B: maps in place, record matches, check_shading clean",
              new_map is not None and new_map.name.startswith("rebake_") and not shading.measure(fixed)["changed"]
              and (shading.record(fixed) or {}).get("job") == job["id"]
              and "Problem" not in shading.check_shading(fixed.name).text, shading.check_shading(fixed.name).text)
        print("RUNS", json.dumps(service.reports)[:3000], flush=True)
    except Exception:
        traceback.print_exc()
        check("ran to the end", False, "exception above")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    passed = sum(r["ok"] for r in RESULTS)
    summary = {"passed": passed, "total": len(RESULTS), "seconds": round(time.time() - t0, 1), "results": RESULTS}
    try:
        (Path.cwd() / "rebake-apply-check.json").write_text(json.dumps(summary, indent=1))
    except OSError:
        pass
    print(f"REBAKE {passed}/{len(RESULTS)} in {summary['seconds']}s")
    ok = passed == len(RESULTS) and RESULTS
    print("REBAKE OK" if ok else "REBAKE FAIL", flush=True)
    return 0 if ok else 1


def run_and_exit() -> None:
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
    watchdog = threading.Timer(TIMEOUT, lambda: (print(f"REBAKE FAIL: timed out after {TIMEOUT}s", flush=True),
                                                  os._exit(3)))
    watchdog.daemon = True
    watchdog.start()
    if bpy.app.background:
        run_and_exit()
    else:
        bpy.app.timers.register(run_and_exit, first_interval=0.5)
