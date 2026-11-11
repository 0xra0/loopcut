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
