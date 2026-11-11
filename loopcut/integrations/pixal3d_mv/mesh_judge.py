"""LLM judge for Pixal3D reconstructions: does the mesh look like the object in the four views?

  mesh_judge.py <views_dir> <mesh.glb> [--backends strata,agy,codex] [--name NAME]

Renders grey clay views of the raw GLB from the four view cameras on the CPU (no GPU: ComfyUI holds it),
puts them under the reference views in one sheet (<mesh>.judge.png), and asks the first backend that
answers. Prints one JSON object: {"verdict": "pass"|"fail", "score": 0-10, "problems": [...],
"backend": ..., "sheet": ...}; exit 0 when judged, 3 when no backend could judge.

Backends (PA_JUDGE / --backends, in order):
  strata  the local Strata server (STRATA_URL, default http://127.0.0.1:8099), only when it is up;
          it cannot share the GPU with ComfyUI, so in a pipeline run it is normally down and skipped
  agy     Antigravity CLI, model PA_JUDGE_AGY_MODEL (default gemini-3.8-flash-high)
  codex   `codex exec -i`, when installed
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from view_qa import CAMERAS, VIEWS   # noqa: E402

TILE = 512
PROMPT = """You are the quality gate of a 3D asset pipeline for game mods.
The image has two rows. TOP: the four reference views (front, left, back, right) that a 3D model was
reconstructed from{what}. BOTTOM: untextured grey clay renders of the reconstructed mesh from the same
four orthographic cameras, in the same order.

Decide whether this mesh is good enough to keep, or whether the pipeline should throw it away and
reconstruct again. FAIL it for any of: a major part missing, added or fused (straps, plates, spikes,
skulls, chain, bail...); melted, blobby or smeared shapes where the reference has crisp ones; wrong
proportions, thickness or curvature; holes or open sides where the reference is closed; ornament or
relief on a side whose reference is plain, or a plain side whose reference is ornate; a side or the
back that does not match its own reference; floating debris.
IGNORE: colour and texture (paint comes later), lighting, the grey background, slightly softer fine
detail than the reference, and small framing or scale differences.

Reply with ONLY one JSON object, no other text:
{{"verdict": "pass" or "fail", "score": <0-10, 10 = matches the references>, "problems": ["<view>: <defect>", ...]}}"""


# --- CPU clay render ---------------------------------------------------------------------------------
# Where each view camera sits, in raw-GLB file coordinates (up is -Z): the cameras of view_qa.CAMERAS
# (right = viewing direction x up gives exactly its image axes). The front camera is at file +Y, as
# Pixal3D's _pixal3d_c2w says: pendant v3 shows its rune ring and skulls there and the engraved back star
# at -Y. The object's left side is at file +X (see view_qa.CAMERAS).
EYE = {"front": (0, 1, 0), "back": (0, -1, 0), "left": (1, 0, 0), "right": (-1, 0, 0)}
UP = np.array([0.0, 0.0, -1.0])


def clay(vertices, faces, view, size=TILE, supersample=2):
    """Painter's-algorithm render of the mesh from one view camera, Lambert-shaded, grey background."""
    toward = np.array(EYE[view], float)                   # from the object to the camera
    right = np.cross(-toward, UP)
    down = -UP
    s = size * supersample
    u, w = vertices @ right, vertices @ down
    span = max(np.ptp(u), np.ptp(w)) or 1.0
    scale = 0.9 * s / span
    u = (u - (u.min() + u.max()) / 2) * scale + s / 2
    w = (w - (w.min() + w.max()) / 2) * scale + s / 2
    tri = vertices[faces]
    normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    normal /= np.linalg.norm(normal, axis=1, keepdims=True) + 1e-12
    light = toward + 0.6 * -down - 0.4 * right             # key light from upper left, in front
    light /= np.linalg.norm(light)
    shade = 40 + 215 * np.abs(normal @ light)              # abs: Pixal3D winding is not trusted
    order = np.argsort((tri @ toward).mean(1))             # far to near
    image = Image.new("L", (s, s), 96)
    draw = ImageDraw.Draw(image)
    pts = np.stack([u, w], 1)[faces]
    for f in order:
        draw.polygon([tuple(p) for p in pts[f].tolist()], fill=int(shade[f]))
    return image.resize((size, size), Image.LANCZOS)


def reference(path, size=TILE):
    """The view, cut out, on the same grey, fitted to the tile the same way the render is."""
    rgba = Image.open(path).convert("RGBA")
    box = rgba.getchannel("A").point(lambda a: 255 if a > 127 else 0).getbbox() or (0, 0, *rgba.size)
    crop = rgba.crop(box)
    k = 0.9 * size / max(crop.size)
    crop = crop.resize((max(1, round(crop.width * k)), max(1, round(crop.height * k))), Image.LANCZOS)
    tile = Image.new("RGB", (size, size), (96, 96, 96))
    tile.paste(crop, ((size - crop.width) // 2, (size - crop.height) // 2), crop)
    return tile


def sheet(views_dir, glb, out):
    import trimesh
    mesh = trimesh.load(glb, force="mesh", process=False)
    vertices, faces = np.asarray(mesh.vertices, float), np.asarray(mesh.faces)
    label = 28
    image = Image.new("RGB", (TILE * 4, 2 * (TILE + label)), (40, 40, 40))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=22)
    except TypeError:
        font = ImageFont.load_default()
    for i, view in enumerate(VIEWS):
        draw.text((i * TILE + 8, 3), f"reference {view}", fill=(255, 255, 255), font=font)
        image.paste(reference(os.path.join(views_dir, f"{view}.png")), (i * TILE, label))
        draw.text((i * TILE + 8, TILE + label + 3), f"mesh {view}", fill=(255, 255, 255), font=font)
        image.paste(clay(vertices, faces, view).convert("RGB"), (i * TILE, TILE + 2 * label))
    image.save(out)
    return out


# --- backends ----------------------------------------------------------------------------------------
def parse(text):
    """The last {...} in the reply that has a verdict; None when there is none."""
    for match in reversed(list(re.finditer(r"\{[^{}]*\}", text or "", re.S))):
        try:
            data = json.loads(match.group(0))
        except ValueError:
            continue
        if data.get("verdict") in ("pass", "fail"):
            data["score"] = float(data.get("score", 0))
            data["problems"] = [str(p) for p in data.get("problems", [])]
            return data
    return None


def strata(prompt, image):
    url = os.environ.get("STRATA_URL", "http://127.0.0.1:8099").rstrip("/")
    try:
        urllib.request.urlopen(url + "/health", timeout=3).read()
    except OSError:
        return None, "not running"
    with open(image, "rb") as stream:
        data = base64.b64encode(stream.read()).decode()
    body = {"model": "local", "temperature": 0, "max_tokens": 4096, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + data}},
        {"type": "text", "text": prompt}]}]}
    request = urllib.request.Request(url + "/v1/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    reply = json.load(urllib.request.urlopen(request, timeout=1800))
    return reply["choices"][0]["message"]["content"], None


def agy(prompt, image):
    exe = shutil.which("agy") or next((p for p in [os.path.expanduser("~/.local/bin/agy")] if os.access(p, os.X_OK)), None)
    if not exe:
        return None, "not installed"
    model = os.environ.get("PA_JUDGE_AGY_MODEL", "gemini-3.8-flash-high")
    # Headless agy auto-denies shell commands and then replies with nothing: on a near-perfect match it
    # tried to diff the two rows by script and gave no verdict (3/3 empty replies), so forbid tools here.
    ask = (f"Open and look at the image file {os.path.basename(image)} in this directory. Judge it by "
           f"looking only: do not run commands or scripts.\n\n{prompt}")
    r = subprocess.run([exe, "-p", ask, "--add-dir", os.path.dirname(image), "--model", model,
                        "--output-format", "json"], cwd=os.path.dirname(image),
                       capture_output=True, text=True, timeout=900)
    if r.returncode:
        return None, f"exit {r.returncode}: {r.stderr.strip()[-300:]}"
    return json.loads(r.stdout).get("response", ""), None


def codex(prompt, image):
    exe = shutil.which("codex")
    if not exe:
        return None, "not installed"
    r = subprocess.run([exe, "exec", "--skip-git-repo-check", "-i", image, prompt],
                       capture_output=True, text=True, timeout=900)
    if r.returncode:
        return None, f"exit {r.returncode}: {r.stderr.strip()[-300:]}"
    return r.stdout, None


BACKENDS = {"strata": strata, "agy": agy, "codex": codex}


def judge(views_dir, glb, backends, what=""):
    image = sheet(views_dir, glb, os.path.splitext(glb)[0] + ".judge.png")
    prompt = PROMPT.format(what=f" ({what})" if what else "")
    tried = {}
    for name in backends:
        try:
            text, why = BACKENDS[name](prompt, image)
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            text, why = None, f"{type(error).__name__}: {error}"
        result = parse(text) if text is not None else None
        if result:
            return dict(result, backend=name, sheet=image, skipped=tried)
        tried[name] = why or "no verdict in the reply: " + (text or "")[-200:]
    return {"verdict": None, "sheet": image, "skipped": tried}


def main(argv):
    args, backends, what = [], os.environ.get("PA_JUDGE", "strata,agy,codex"), ""
    it = iter(argv)
    for a in it:
        if a == "--backends":
            backends = next(it)
        elif a == "--name":
            what = next(it).replace("_", " ")
        else:
            args.append(a)
    if len(args) != 2:
        sys.exit(__doc__)
    names = [b for b in backends.split(",") if b]
    unknown = [b for b in names if b not in BACKENDS]
    if unknown:
        sys.exit(f"unknown judge backend(s) {unknown}; known: {', '.join(BACKENDS)}")
    result = judge(args[0], args[1], names, what)
    print(json.dumps(result))
    return 0 if result["verdict"] else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
