"""Pixal3D bridge: run the local Pixal3D multiview pipeline (loopcut/integrations/pixal3d_mv/pa.py),
then import its game-ready LOD0.

pa.py drives a local ComfyUI (Qwen-Image views -> Pixal3D MultiView mesh -> headless Blender asset
build). A run takes tens of minutes to over an hour, so it runs as a child process in its own session;
pixal3d_status reports progress and imports the result on the main thread once it's ready.
"""

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import bpy

from . import CommandError, command
from .image_to_3d import _import

# scripts/addons_core/loopcut/studio_vendor/handlers -> repository root
_REPO_COPY = Path(__file__).resolve().parents[5] / "loopcut" / "integrations" / "pixal3d_mv"
_jobs = {}


def _pipeline_dir():
    """PIXAL3D_DIR when set, else the copy in this Loopcut checkout."""
    path = Path(os.environ.get("PIXAL3D_DIR") or _REPO_COPY)
    if not (path / "pa.py").is_file():
        raise CommandError(f"No Pixal3D pipeline (pa.py) at {path}. Set PIXAL3D_DIR to the pixal3d_mv folder "
                           "before starting Blender")
    return path


def _comfy_dir():
    return os.environ.get("COMFY_DIR", "/mnt/ssd/ComfyUI")   # pa.py's own default


def _result(name):
    """The production LOD0 GLB recorded in pa.py's run manifest, else the raw reconstruction (PA_NO_FIX=1)."""
    path = os.path.join(_comfy_dir(), "output", "PixelArtistry", f"{name}.run.json")
    try:
        with open(path) as stream:
            stages = json.load(stream)["stages"]
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(f"pa.py finished but its run record {path} is unreadable: {exc}") from exc
    for stage in ("asset", "mesh"):
        outputs = (stages.get(stage) or {}).get("outputs") or []
        if outputs and os.path.isfile(outputs[0]):
            return outputs[0]
    raise RuntimeError(f"pa.py finished but {path} records no existing mesh")


def _last_line(path):
    try:
        with open(path, "rb") as stream:
            stream.seek(max(0, os.path.getsize(path) - 4096))
            lines = [ln.strip() for ln in stream.read().decode(errors="replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    return lines[-1][:300] if lines else ""


def _run(job, args, cwd, env, max_seconds):
    try:
        with open(job["log"], "wb") as log:
            proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            job["pid"] = proc.pid
            try:
                code = proc.wait(timeout=max_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)   # pa.py's own Blender and QA children too
                proc.wait()
                raise RuntimeError(f"No result after {max_seconds} s; stopped pa.py") from None
        if code:
            raise RuntimeError(f"pa.py exited with code {code}: {_last_line(job['log'])}")
        job["output"] = _result(job["name"])
        job["state"] = "done"
    except Exception as exc:  # reported through pixal3d_status
        job["state"] = "failed"
        job["error"] = str(exc)
    finally:
        job["finished"] = time.time()


@command("pixal3d_generate", "reference")
def pixal3d_generate(name, description, image_path=None, seed=None, tries=1, separate_views=False, resume=False,
                     max_minutes=180):
    """Make a game-ready textured model with the local Pixal3D multiview pipeline (pa.py all): front view
    (from image_path, or generated from description), 4-view turnaround, Pixal3D MultiView mesh, then a
    production asset build (cleanup, UVs, PBR bake, LODs). Needs ComfyUI running locally
    (launch_comfy_qwen.sh in the pipeline folder; COMFY_PORT, default 8188).

    name: one word (letters, digits, _), the asset name. description: a short noun phrase, e.g.
    "a steel longsword with a leather grip". tries: rerolls of failing meshes. resume: reuse stages
    recorded for this name. Returns a job id at once; poll pixal3d_status, which imports the LOD0."""
    if not (isinstance(name, str) and name[:1].isalpha() and all(c.isalnum() or c == "_" for c in name)):
        raise CommandError(f"name must be one word of letters, digits or _ starting with a letter: got {name!r}")
    if not isinstance(description, str) or not description.strip():
        raise CommandError("description is required: a short noun phrase for the object")
    running = next((j for j in _jobs.values() if j["state"] == "running"), None)
    if running:
        raise CommandError(f"Pixal3D job {running['id']} ({running['name']}) is still running; it needs the "
                           "whole GPU. Poll pixal3d_status until it ends")
    if image_path:
        source = bpy.path.abspath(os.path.expanduser(image_path))
        if not os.path.isfile(source):
            raise CommandError(f"No image at {source}")
    else:
        source = "text:" + description.strip()
    root = _pipeline_dir()
    python = os.environ.get("COMFY_PYTHON") or shutil.which("python3")
    if not python:
        raise CommandError("No python3 to run pa.py; set COMFY_PYTHON")
    url = f"http://127.0.0.1:{os.environ.get('COMFY_PORT', '8188')}"
    try:
        urllib.request.urlopen(url + "/system_stats", timeout=5).close()
    except (urllib.error.URLError, OSError) as exc:
        raise CommandError(f"No ComfyUI server at {url} ({exc}). Start it with "
                           f"`bash {root / 'launch_comfy_qwen.sh'}`, then try again") from exc

    args = [python, str(root / "pa.py"), "all", name, source, description.strip()]
    if seed is not None:
        args.append(str(int(seed)))
    if int(tries) > 1:
        args += ["--tries", str(int(tries))]
    if separate_views:
        args.append("--separate-views")
    if resume:
        args.append("--resume")
    # pa.py's asset build runs headless Blender: use this same Blender unless told otherwise.
    env = dict(os.environ, BLENDER_BIN=os.environ.get("BLENDER_BIN") or bpy.app.binary_path)
    job = {"id": uuid.uuid4().hex[:8], "name": name, "state": "running", "started": time.time(),
           "log": os.path.join(bpy.app.tempdir or "/tmp", f"pixal3d_{name}_{int(time.time())}.log")}
    _jobs[job["id"]] = job
    threading.Thread(target=_run, args=(job, args, str(root), env, int(max_minutes) * 60), daemon=True).start()
    return {"job_id": job["id"], "log": job["log"],
            "note": "A full run takes about 30-90 minutes on a 16 GB GPU. Poll pixal3d_status every few "
                    "minutes; Blender stays usable meanwhile"}


@command("pixal3d_status", "reference")
def pixal3d_status(job_id, name=None, height=None, base_z=0.0):
    """Check a pixal3d_generate job: state, the pipeline's latest log line and elapsed time. When done,
    imports the LOD0 once as one object called `name` (default: the asset name). height (meters) scales
    it to that height; it is then stood on base_z and centred on X/Y."""
    job = _jobs.get(job_id)
    if job is None:
        raise CommandError(f"No job {job_id!r}")
    out = {k: job.get(k) for k in ("id", "name", "state", "error", "output", "log")}
    out["step"] = _last_line(job["log"])
    out["elapsed_seconds"] = round((job.get("finished") or time.time()) - job["started"])
    if job["state"] == "done" and not job.get("imported"):
        job["imported"] = _import(job["output"], name or job["name"], height, float(base_z))
    if job.get("imported"):
        out["imported"] = job["imported"]
        out["next"] = "Look at it and fix what is wrong: studio_guide(playbook='refine_generated')"
    return out



def _measure_pixal3d_flaws(mesh, dimensions) -> dict:
    """Islands, non-manifold edges, shell thickness and X-mirror residual, all read from `mesh`.

    min_thickness is the 5th percentile of inward ray hits from sampled vertices, or None when no
    ray hit a surface (an open sheet has no thickness to measure).
    """
    import bmesh
    import numpy as np
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    dims = [round(float(d), 4) for d in dimensions]
    n_verts = len(mesh.vertices)
    if n_verts == 0:
        return {"islands": 0, "non_manifold_edges": 0, "min_thickness": None, "symmetry_residual": 0.0,
                "bbox_dimensions": dims}
    co = np.empty(n_verts * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)

    parent = list(range(n_verts))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    edge_verts = np.empty(len(mesh.edges) * 2, dtype=np.int32)
    mesh.edges.foreach_get("vertices", edge_verts)
    for v1, v2 in edge_verts.reshape(-1, 2).tolist():
        r1, r2 = find(v1), find(v2)
        if r1 != r2:
            parent[r1] = r2
    islands = len({find(i) for i in range(n_verts)})

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        non_manifold = sum(1 for e in bm.edges if not e.is_manifold)
    finally:
        bm.free()

    sample = np.linspace(0, n_verts - 1, min(100, n_verts), dtype=int)
    mirrored = co[sample] * np.array([-1.0, 1.0, 1.0], dtype=np.float32)
    symmetry_residual = float(np.mean(np.min(np.linalg.norm(co[None, :, :] - mirrored[:, None, :], axis=-1), axis=1)))

    hits = []
    if len(mesh.polygons):
        bvh = BVHTree.FromPolygons(co.tolist(), [tuple(p.vertices) for p in mesh.polygons])
        normals = np.empty(n_verts * 3, dtype=np.float32)
        mesh.vertices.foreach_get("normal", normals)
        normals = normals.reshape(-1, 3)
        for idx in range(0, n_verts, max(1, n_verts // 100)):
            inward = -Vector(normals[idx].tolist())
            if inward.length_squared < 1e-12:
                continue
            inward.normalize()
            loc, _normal, _index, dist = bvh.ray_cast(Vector(co[idx].tolist()) + inward * 1e-4, inward)
            if loc is not None and dist > 1e-4:
                hits.append(dist)

    return {
        "islands": islands,
        "non_manifold_edges": non_manifold,
        "min_thickness": round(float(np.percentile(hits, 5)), 5) if hits else None,
        "symmetry_residual": round(symmetry_residual, 5),
        "bbox_dimensions": dims,
    }


def _measure_evaluated(obj) -> dict:
    """Measure the mesh as it renders, modifiers included."""
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    try:
        return _measure_pixal3d_flaws(evaluated.to_mesh(), obj.dimensions)
    finally:
        evaluated.to_mesh_clear()


def _apply_refinement_stages(obj, target_symmetry: bool, min_thickness: float, quad_target: int) -> list[str]:
    from .topology import quad_remesh
    from .util import ops_context

    stages = []
    with ops_context(obj, mode="EDIT"):
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.delete_loose()
        stages.append("delete_loose")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.fill_holes(sides=4)
        stages.append("fill_holes_up_to_4_sides")
        if target_symmetry:
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.mesh.symmetrize(direction="NEGATIVE_X")
            stages.append("symmetrize_minus_x_onto_plus_x")
    if quad_target > 0:
        quad_remesh(obj.name, target_faces=int(quad_target))
        stages.append("quadriflow_remesh")
    if min_thickness > 0:
        mod = obj.modifiers.new(name="Pixal3D_Solidify", type="SOLIDIFY")
        mod.thickness = float(min_thickness)
        stages.append("solidify_modifier")
    return stages


@command("pixal3d_refine", "topology")
def pixal3d_refine(object_name=None, target_symmetry=False, min_thickness=0.005, quad_target=0):
    """Repair a generated mesh in stages and measure it before and after.

    Deletes loose vertices and edges, fills holes of up to 4 sides, mirrors -X onto +X only when
    target_symmetry, QuadriFlow-remeshes to quad_target faces when above 0 (destroys UVs), and adds a
    Solidify modifier of min_thickness when above 0. object_name defaults to the active object.
    The after numbers are measured on the evaluated mesh, modifiers included.
    """
    from .util import get_mesh

    if object_name:
        target = get_mesh(object_name)
    else:
        target = bpy.context.active_object
        if target is None or target.type != "MESH":
            raise CommandError("Name the mesh to refine: object_name, or make it the active object")

    initial = _measure_evaluated(target)
    stages = _apply_refinement_stages(target, bool(target_symmetry), float(min_thickness), int(quad_target))
    final = _measure_evaluated(target)

    checks = [
        {"check": "no_non_manifold_edges", "passed": final["non_manifold_edges"] == 0,
         "value": final["non_manifold_edges"]},
        {"check": "islands_not_increased", "passed": final["islands"] <= initial["islands"],
         "value": final["islands"]},
    ]
    if float(min_thickness) > 0:
        thickness = final["min_thickness"]
        checks.append({"check": "shell_thickness_at_least_min", "value": thickness,
                       "passed": thickness is not None and thickness >= float(min_thickness) * 0.8})
    if target_symmetry:
        checks.append({"check": "symmetry_residual_not_increased", "value": final["symmetry_residual"],
                       "passed": final["symmetry_residual"] <= initial["symmetry_residual"]})
    return {
        "object": target.name,
        "initial_diagnostics": initial,
        "final_diagnostics": final,
        "stages_completed": stages,
        "acceptance_passed": all(c["passed"] for c in checks),
        "acceptance_checks": checks,
    }
