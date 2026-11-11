"""Image-to-3D bridge: send a picture to a local Hunyuan3D-2 / 2.1 API server, import the mesh.

The server runs on the user's NVIDIA GPU (python api_server.py in a Hunyuan3D checkout, port
8081 by default). Generation takes from tens of seconds to minutes, so it runs in a background
thread and image_to_3d_status imports the result on the main thread once it's ready.
"""

import base64
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

import bpy
import numpy as np

from . import CommandError, command
from .reference import _subject_mask, _write_png
from .util import link_to_scene

DEFAULT_SERVER = "http://127.0.0.1:8081"
_jobs = {}


def _post(url, payload, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _cutout_png(path, crop, tolerance):
    """The subject on a transparent background as PNG bytes. Hunyuan3D-2.1 expects the
    background removed already; 2.0 works either way."""
    from .reference import _crop_to_file

    if crop:
        path = _crop_to_file(path, crop, "i2m")
    mask, rgba = _subject_mask(path, tolerance)
    h, w = rgba.shape[:2]
    rows = np.minimum((np.arange(h) + 0.5) * mask.shape[0] / h, mask.shape[0] - 1).astype(int)
    cols = np.minimum((np.arange(w) + 0.5) * mask.shape[1] / w, mask.shape[1] - 1).astype(int)
    out = rgba.copy()
    out[..., 3] = mask[rows][:, cols]
    # Pad to a square with a margin, as the models are trained on centred objects.
    ys, xs = np.nonzero(out[..., 3] > 0)
    out = out[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    side = int(max(out.shape[:2]) * 1.15)
    square = np.zeros((side, side, 4), np.float32)
    y0, x0 = (side - out.shape[0]) // 2, (side - out.shape[1]) // 2
    square[y0:y0 + out.shape[0], x0:x0 + out.shape[1]] = out
    tmp = os.path.join(tempfile.mkdtemp(prefix="claude_i2m_"), "input.png")
    _write_png(square, tmp)
    with open(tmp, "rb") as fh:
        return fh.read(), tmp


def _run(job, server, payload, poll, max_seconds):
    try:
        job["step"] = "sending"
        uid = _post(server + "/send", payload, timeout=60)["uid"]
        job["uid"] = uid
        job["step"] = "generating"
        texturing_since = None
        while True:
            if time.time() - job["started"] > max_seconds:
                raise RuntimeError(f"No result after {max_seconds} s")
            time.sleep(poll)
            try:
                st = _get(f"{server}/status/{uid}", timeout=60)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"Status check failed: HTTP {exc.code}") from exc
            status = st.get("status")
            job["server_status"] = status
            if status == "completed":
                data = base64.b64decode(st["model_base64"])
                path = os.path.join(job["workdir"], "result.glb")
                with open(path, "wb") as fh:
                    fh.write(data)
                job["output"] = path
                job["state"] = "done"
                break
            if status == "error":
                raise RuntimeError(st.get("message", "server reported an error"))
            if status == "texturing":
                # 2.1 stays at "texturing" forever if the texture step crashes.
                texturing_since = texturing_since or time.time()
                if time.time() - texturing_since > job["texture_timeout"]:
                    raise RuntimeError("Texturing never finished (Hunyuan3D-2.1 does this when the paint step "
                                       "fails). Try texture=False, or check the server's console")
    except Exception as exc:  # reported through image_to_3d_status
        job["state"] = "failed"
        job["error"] = str(exc)
    finally:
        job["finished"] = time.time()


@command("image_to_3d", "reference")
def image_to_3d(image_path, server=DEFAULT_SERVER, crop=None, texture=False, seed=1234, octree_resolution=256,
                num_inference_steps=None, guidance_scale=None, face_count=40000, tolerance=0.06, max_minutes=30):
    """Generate a 3D mesh from one picture with a local Hunyuan3D-2 or 2.1 API server.

    Uses the front view of a character: crop picks it out of a turnaround sheet ([left, top,
    right, bottom] in 0-1). The background is removed before sending. Returns a job id at once;
    poll image_to_3d_status, which imports the mesh when it's ready."""
    path = bpy.path.abspath(os.path.expanduser(image_path))
    if not os.path.isfile(path):
        raise CommandError(f"No image at {path}")
    server = server.rstrip("/")
    try:
        _get(server + "/health", timeout=5)
    except urllib.error.HTTPError:
        pass  # Hunyuan3D-2.0 has no /health; any HTTP answer means the server is up
    except (urllib.error.URLError, OSError) as exc:
        raise CommandError(
            f"No Hunyuan3D server at {server} ({exc}). Start it in the Hunyuan3D folder with "
            "`python api_server.py --port 8081` (add --enable_tex on 2.0 for textures), then try again") from exc
    png, cutout = _cutout_png(path, crop, float(tolerance))
    payload = {
        "image": base64.b64encode(png).decode("ascii"), "remove_background": False, "texture": bool(texture),
        "seed": int(seed), "octree_resolution": int(octree_resolution), "face_count": int(face_count), "type": "glb",
    }
    # Leave these to the server unless asked: its defaults suit the model it loaded (turbo or not).
    if num_inference_steps is not None:
        payload["num_inference_steps"] = int(num_inference_steps)
    if guidance_scale is not None:
        payload["guidance_scale"] = float(guidance_scale)
    job = {"id": uuid.uuid4().hex[:8], "state": "running", "step": "starting", "started": time.time(),
           "workdir": os.path.dirname(cutout), "texture_timeout": 900}
    _jobs[job["id"]] = job
    threading.Thread(target=_run, args=(job, server, payload, 3.0, int(max_minutes) * 60), daemon=True).start()
    return {"job_id": job["id"], "input_image": cutout,
            "note": "Poll image_to_3d_status every 20-30 s. Shape only takes about a minute on a recent GPU; "
                    "texture adds several minutes"}


@command("image_to_3d_status", "reference")
def image_to_3d_status(job_id, name="Generated", height=None, base_z=0.0):
    """Check an image_to_3d job. When done, imports the mesh once as one object called `name`,
    with scale applied. height (meters) scales it to that height; it is then stood on base_z and
    centred on X/Y. Check which way it faces with compare_to_reference."""
    job = _jobs.get(job_id)
    if job is None:
        raise CommandError(f"No job {job_id!r}")
    out = {k: job.get(k) for k in ("id", "state", "step", "server_status", "error")}
    out["elapsed_seconds"] = round((job.get("finished") or time.time()) - job["started"])
    if job["state"] == "done" and not job.get("imported"):
        job["imported"] = _import(job["output"], name, height, float(base_z))
    if job.get("imported"):
        out["imported"] = job["imported"]
    return out


def _import(path, name, height, base_z):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new if o.type == "MESH"]
    if not meshes:
        raise CommandError("The generated file had no mesh in it")
    # Bake every transform into the mesh data and merge into one object.
    dg = bpy.context.evaluated_depsgraph_get()
    import bmesh

    bm = bmesh.new()
    materials = []
    for o in meshes:
        me = o.evaluated_get(dg).to_mesh()
        me.transform(o.matrix_world)
        offset = len(materials)
        materials += [s.material for s in o.material_slots]
        for p in me.polygons:
            p.material_index += offset
        bm.from_mesh(me)
        o.evaluated_get(dg).to_mesh_clear()
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    for m in materials:
        me.materials.append(m)
    for o in new:
        bpy.data.objects.remove(o)
    obj = bpy.data.objects.new(name, me)
    link_to_scene(obj)
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    lo, hi = co.min(axis=0), co.max(axis=0)
    k = float(height) / (hi[2] - lo[2]) if height else 1.0
    centre = (lo + hi) / 2
    co = (co - [centre[0], centre[1], lo[2]]) * k + [0, 0, base_z]
    me.vertices.foreach_set("co", co.ravel())
    me.update()
    return {"name": obj.name, "verts": len(me.vertices), "faces": len(me.polygons),
            "dimensions": [round(v, 4) for v in obj.dimensions], "materials": [m.name for m in materials if m]}
