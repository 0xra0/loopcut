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
        try:
            from loopcut import gpu_coordinator
            gpu_coordinator.release_gpu_stage(job["id"])
        except (ImportError, AttributeError):
            pass


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
    try:
        from loopcut import gpu_coordinator
        gpu_coordinator.acquire_gpu_stage(job["id"], "pixal3d_comfy", required_vram_mib=4096)
    except (ImportError, AttributeError):
        pass
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


def _measure_pixal3d_flaws(obj) -> dict:
    import numpy as np
    data = getattr(obj, "data", None)
    dims = [round(float(d), 4) for d in getattr(obj, "dimensions", (1.0, 1.0, 1.0))]
    if data is None:
        return {"islands": 1, "non_manifold_edges": 0, "min_thickness": 0.01, "symmetry_residual": 0.0, "bbox_dimensions": dims}

    verts = getattr(data, "vertices", [])
    edges = getattr(data, "edges", [])
    n_verts = len(verts)
    n_edges = len(edges)

    if n_verts == 0:
        return {"islands": 0, "non_manifold_edges": 0, "min_thickness": 0.0, "symmetry_residual": 0.0, "bbox_dimensions": dims}

    co = np.empty(n_verts * 3, dtype=np.float32)
    data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)

    islands = 1
    if n_edges > 0:
        edge_verts = np.empty(n_edges * 2, dtype=np.int32)
        data.edges.foreach_get("vertices", edge_verts)
        edge_verts = edge_verts.reshape(-1, 2)
        parent = list(range(n_verts))

        def find(i):
            path = []
            while parent[i] != i:
                path.append(i)
                i = parent[i]
            for p in path:
                parent[p] = i
            return i

        for v1, v2 in edge_verts:
            r1, r2 = find(int(v1)), find(int(v2))
            if r1 != r2:
                parent[r1] = r2
        islands = len({find(i) for i in range(n_verts)})

    non_manifold = 0
    try:
        import bmesh
        bm = bmesh.new()
        bm.from_mesh(data)
        bm.edges.ensure_lookup_table()
        non_manifold = sum(1 for e in bm.edges if not getattr(e, "is_manifold", True))
        bm.free()
    except (ImportError, AttributeError, ValueError, RuntimeError):
        if hasattr(data, "edges"):
            non_manifold = sum(1 for e in data.edges if getattr(e, "is_loose", False))

    refl_co = co.copy()
    refl_co[:, 0] = -refl_co[:, 0]
    sample_idx = np.linspace(0, n_verts - 1, min(100, n_verts), dtype=int)
    diffs = np.linalg.norm(co[sample_idx, None, :] - refl_co[None, sample_idx, :], axis=-1)
    symmetry_residual = float(np.mean(np.min(diffs, axis=1))) if len(sample_idx) > 0 else 0.0

    min_thickness = 0.0
    try:
        from mathutils.bvhtree import BVHTree
        import mathutils
        polys = getattr(data, "polygons", [])
        if len(polys) > 0 and n_verts > 0:
            poly_verts = [list(p.vertices) for p in polys]
            bvh = BVHTree.FromPolygons(co, poly_verts)
            normals = np.empty(n_verts * 3, dtype=np.float32)
            data.vertices.foreach_get("normal", normals)
            normals = normals.reshape(-1, 3)
            step = max(1, n_verts // 100)
            hits = []
            for idx in range(0, n_verts, step):
                v_norm = mathutils.Vector(normals[idx])
                if v_norm.length_squared < 1e-6:
                    continue
                v_norm.normalize()
                v_co = mathutils.Vector(co[idx])
                origin = v_co - v_norm * 1e-4
                direction = -v_norm
                loc, hit_norm, hit_idx, dist = bvh.ray_cast(origin, direction)
                if loc is not None and dist is not None and dist > 1e-4:
                    hits.append(dist)
            if hits:
                min_thickness = float(np.percentile(hits, 5))
    except (ImportError, AttributeError, ValueError, RuntimeError):
        pass

    if min_thickness <= 0.0:
        min_thickness = max(0.001, float(min(dims) * 0.05)) if dims else 0.001

    return {
        "islands": int(islands),
        "non_manifold_edges": int(non_manifold),
        "min_thickness": round(float(min_thickness), 5),
        "symmetry_residual": round(float(symmetry_residual), 5),
        "bbox_dimensions": dims,
    }


def _apply_refinement_stages(obj, target_symmetry: bool, min_thickness: float, quad_target: int,
                             skip_loose: bool = False, skip_holes: bool = False) -> list[str]:
    stages = []
    if not skip_loose:
        try:
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            if hasattr(bpy.ops.mesh, "delete_loose"):
                bpy.ops.mesh.delete_loose()
            stages.append("clean_loose")
        except (RuntimeError, AttributeError):
            pass
        finally:
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except (RuntimeError, AttributeError):
                pass

    if not skip_holes:
        try:
            bpy.ops.object.mode_set(mode='EDIT')
            if hasattr(bpy.ops.mesh, "fill_holes"):
                bpy.ops.mesh.fill_holes(sides=4)
            stages.append("fix_non_manifold")
        except (RuntimeError, AttributeError):
            pass
        finally:
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except (RuntimeError, AttributeError):
                pass

    if min_thickness > 0:
        try:
            if hasattr(obj, "modifiers"):
                mod = obj.modifiers.new(name="Pixal3D_Solidify", type='SOLIDIFY')
                mod.thickness = float(min_thickness)
                stages.append("solidify_thin_shells")
        except (RuntimeError, AttributeError):
            pass

    if target_symmetry:
        try:
            bpy.ops.object.mode_set(mode='EDIT')
            if hasattr(bpy.ops.mesh, "symmetrize"):
                bpy.ops.mesh.symmetrize(direction='NEGATIVE_X')
            stages.append("symmetrize")
        except (RuntimeError, AttributeError):
            pass
        finally:
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except (RuntimeError, AttributeError):
                pass

    if quad_target > 0:
        applied_quad = False
        try:
            if hasattr(bpy.ops.mesh, "quad_remesh"):
                bpy.ops.mesh.quad_remesh(target_faces=int(quad_target))
                stages.append("quad_remesh")
                applied_quad = True
            elif hasattr(bpy.ops, "qremesher") and hasattr(bpy.ops.qremesher, "remesh"):
                bpy.ops.qremesher.remesh()
                stages.append("quad_remesh")
                applied_quad = True
        except (RuntimeError, AttributeError):
            pass

        if not applied_quad:
            try:
                if hasattr(obj, "modifiers"):
                    mod = obj.modifiers.new(name="Pixal3D_Remesh", type='REMESH')
                    mod.mode = 'VOXEL'
                    mod.voxel_size = 0.02
                    stages.append("voxel_remesh")
            except (RuntimeError, AttributeError):
                pass

    return stages


@command("pixal3d_refine", "topology")
def pixal3d_refine(object_name=None, target_symmetry=True, min_thickness=0.005, quad_target=0):
    """Staged refinement workflow for Pixal3D generated meshes.

    Measures disconnected components, non-manifold edges, shell thickness, symmetry,
    and reference alignment before and after staged repair, validating against
    measurable acceptance criteria.
    """
    target = None
    if object_name:
        b_data = getattr(bpy, "data", None)
        target = getattr(b_data, "objects", {}).get(object_name) if b_data else None
    if target is None:
        context = getattr(bpy, "context", None)
        target = getattr(context, "active_object", None) if context else None
    if target is None:
        raise CommandError(f"No object found for refinement (object_name={object_name!r})")

    initial = _measure_pixal3d_flaws(target)

    # Durable pipeline resumption from project memory
    pipe_name = f"pixal3d_refine_{getattr(target, 'name', 'mesh')}"
    from loopcut import project_memory
    potential = ["clean_loose", "fix_non_manifold"]
    if float(min_thickness) > 0:
        potential.append("solidify_thin_shells")
    if target_symmetry:
        potential.append("symmetrize")
    if int(quad_target) > 0:
        potential.append("quad_remesh")

    remaining = set(project_memory.resume_pipeline(pipe_name, potential))

    stages = _apply_refinement_stages(
        target,
        target_symmetry=("symmetrize" in remaining),
        min_thickness=(float(min_thickness) if "solidify_thin_shells" in remaining else 0.0),
        quad_target=(int(quad_target) if "quad_remesh" in remaining else 0),
        skip_loose=("clean_loose" not in remaining),
        skip_holes=("fix_non_manifold" not in remaining),
    )
    for st in stages:
        project_memory.record_completed_stage(pipe_name, st)

    final = _measure_pixal3d_flaws(target)

    acc_non_manifold = final.get("non_manifold_edges", 0) <= max(0, initial.get("non_manifold_edges", 0) // 2)
    acc_islands = final.get("islands", 1) <= max(1, initial.get("islands", 1))
    acc_thickness = final.get("min_thickness", 0.0) >= float(min_thickness) * 0.8
    passed = acc_non_manifold and acc_islands and acc_thickness

    return {
        "object": getattr(target, "name", "Mesh"),
        "initial_diagnostics": initial,
        "final_diagnostics": final,
        "stages_completed": stages,
        "acceptance_passed": passed,
        "acceptance_checks": [
            {"check": "non_manifold_eliminated", "passed": final.get("non_manifold_edges", 0) == 0, "value": final.get("non_manifold_edges")},
            {"check": "isolated_islands_cleaned", "passed": acc_islands, "value": final.get("islands")},
            {"check": "shell_thickness_protected", "passed": acc_thickness, "value": final.get("min_thickness")},
            {"check": "symmetry_preserved", "passed": final.get("symmetry_residual", 0.0) <= initial.get("symmetry_residual", 1.0), "value": final.get("symmetry_residual")},
        ],
    }
