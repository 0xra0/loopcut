"""PixelArtistry Pixal3D multiview pipeline: image or text -> turnaround sheet -> 4 views -> GLB.
ComfyUI must be running with --cache-none (launch_comfy_qwen.sh). See README.md.

  pa.py front <name> "<object description>" [seed]     text -> front view (01, ~4 min)
  pa.py sheet <name> <front.png> "<object description>" [seed]   front view -> 2048x1024 turnaround sheet (02, ~10 min)
  pa.py split <name> <sheet.png>                        sheet -> ComfyUI/input/<name>_views/{front,left,back,right}.png
                                                        (halo removed; views_qa.json = mirror/scale consistency check)
  pa.py views <name> <front.png> "<object description>" [seed]   left/back/right generated one by one from the
                                                        front (shared seed), cut out, scaled to the front's height
  pa.py mesh  <name> [seed] [--tries N] [mesh flags]     4 views -> original GLB + automatic production GameAssets/<name>/
                                                        (refuses a server without strict real NAF; scores each GLB
                                                        against the views; --tries N rerolls failing GLBs, keeps the best)
  pa.py prep  <name> [mesh.glb] [faces]                 Blender headless: game-ready pass -> output/3D/<name>_lp.glb
                                                        (default: newest raw mesh, 40000 triangles, full repair)
                                                        flags: --repair off|light|full  --angle DEG  --unwrap keep|smart
                                                               --formats glb,obj,fbx,blend --faces N --materials bake|discard
  pa.py asset <name> [mesh] [--preset prop --engine generic]   all seven stages -> output/GameAssets/<name>/
  pa.py fix   <name> [mesh.glb]                         same pass without decimation (game-ready <name>_game.glb)
  pa.py sculpt <name> [mesh.glb] [voxel]               quad cage .glb/.obj + 3-level multires .blend, Smart UV cage
                                                        (voxel = fraction of diagonal, default 0.0015; smaller = denser)
  pa.py texture <name> [mesh.glb] [1024|2048]           UltraTex PBR re-texture (04) -> ComfyUI/output/UltraTex/
                                                        (prefers cleaned low-poly mesh; prepares external inputs as needed;
                                                        1024 test, 2048 final; keeps the Smart UV layout)
  pa.py all   <name> <image.png | "text:<desc>"> "<object description>" [seed]   everything in one go
                                                        (ends with automatic cleanup; PA_NO_FIX=1 skips it)
                                                        --resume reuses stages recorded in PixelArtistry/<name>.run.json
                                                        --separate-views uses `views` instead of one sheet; --tries N
  pa.py prompts <name> "<object description>"           dry run: print the exact prompt, negative and CFG that front,
                                                        sheet and each separate view would send (no ComfyUI needed)
  --detail-spec spec.json (any generation command)      details by scale (large/medium/small/materials), per view
                                                        (front/left/back/right) and an avoid list; see
                                                        examples/detail_spec_example.json
  pa.py parts <name> [mesh.glb] [--device cpu|cuda]     name the mesh's parts from the views (<name>_parts.json:
                                                        Florence-2 + SAM per view, cast onto faces) -> output/3D/<name>_parts/
  pa.py check                                     is the rig intact? models, MV fix, UltraTex pin, server up

The object description is a short noun phrase, e.g. "a steel longsword with a leather grip".
<name> must be one word (letters, digits or _). Every stage prints the seed it used; pass it back
to replay a good roll. PA_MAX_WAIT (seconds) caps one ComfyUI wait before erroring (default 5400).
launch_comfy_qwen.sh's server and pa.py both read COMFY_PORT (default 8188); export it once to move the port.
Before each stage pa.py unloads the previous stage's models and checks free RAM/VRAM (PA_MIN_RAM_GB,
PA_MIN_VRAM_GB override the floors); PA_ALLOW_BILINEAR_NAF=1 accepts a server without strict real NAF.
"""
import argparse, glob, json, os, random, shutil, subprocess, sys, time, urllib.error, urllib.request
import signal
import threading
from mesh_config import add_options, validate
from asset_config import parse_asset
import pa_runtime as rt

HERE = os.path.dirname(os.path.abspath(__file__))
COMFY = os.environ.get("COMFY_DIR", "/mnt/ssd/ComfyUI")
PORT = os.environ.get("COMFY_PORT", "8188")   # launch_best_cpu_vae.sh reads the same variable, so server and pa.py agree
URL = f"http://127.0.0.1:{PORT}"
PY = os.environ.get("COMFY_PYTHON", "/mnt/ssd/conda-envs/ComfyUI/bin/python3.12")
# UltraTex attention by resolution (views tests, RX 6800, 2026-11-07, s/step and front band score; ref 0.884):
#   1024: sparse bands the chest (1.913), dense is clean and faster (0.731, 37 s vs 53 s).
#   1536: both clean (dense 0.731, sparse 0.749), but dense is 53% slower (238 s vs 155 s).
#   2048: dense runs out of VRAM in step 1, so only sparse works.
DENSE_MAX_RES = 1024
MAX_WAIT = int(os.environ.get("PA_MAX_WAIT", 5400))   # seconds to wait for one prompt; a 1024 UltraTex run on a 20k LOD0 takes ~61 min

from pa_prompt_support import configure_from_argv, apply_prompt_settings, prompt_fingerprint

FRONT_PROMPT = ("{desc}. A single object, front view, orthographic, centered, the whole object in frame, "
                "upright, flat even studio lighting, plain white background, no shadow, no text.")
# Generic version of the sword prompt that worked: exact 90° steps, static object, same heights in every panel.
SHEET_PROMPT = (
    "Turn the object in <image1> ({desc}) into an orthographic 3D modeling reference sheet: a single horizontal "
    "row of four equal-width tall panels, evenly spaced. From left to right: 1) front view, identical to <image1>. "
    "2) left side view, an exact 90° profile. 3) back view, exactly 180°, the same outline as the front view. "
    "4) right side view, an exact 90° profile, mirror of view 2. The camera orbits a completely static object at "
    "the same height: the object never turns toward the camera. Identical shapes, colors, materials and scale in "
    "all views, each view centered in its panel, top and bottom at the same heights in every panel, no overlap "
    "between views. Flat, even lighting, plain white background, no text, no labels.")
# One view at a time (item 8): the front image is the shared reference, the seed is shared too.
VIEW_PROMPT = (
    "Show the object in <image1> ({desc}) from {where}. The same static object: identical shape, proportions, "
    "colors, materials and scale as <image1>. Orthographic camera at the same height as in <image1>, the whole "
    "object in frame, upright, centered, flat even studio lighting, plain white background, no shadow, no text.")
VIEW_WHERE = {"left": "its left side: an exact 90 degree side profile",
              "back": "behind: an exact 180 degree back view with the same outline as the front, mirrored",
              "right": "its right side: an exact 90 degree side profile, the mirror image of the left side view"}


def wf(name):
    with open(os.path.join(HERE, "workflows", name)) as stream:
        return json.load(stream)


def new_seed(seed=None, bits=2 ** 48):
    """Pass a seed to replay a good roll; otherwise pick a random one. Echo it either way."""
    seed = int(seed) if seed else random.randrange(bits)
    print(f"  seed {seed}")
    return seed


_last_stage = None


def prepare_stage(stage):
    """Unload the previous stage's models (once per stage change), then refuse to start without the memory it needs."""
    global _last_stage
    if stage == _last_stage:
        return
    if rt.free_models(URL):
        print(f"  unloaded models before {stage}")
    _last_stage = stage
    problem = rt.memory_problem(URL, stage)
    if problem:
        sys.exit(f"not starting {stage}: {problem}")


def comfy_died(message):
    path, lines = rt.log_tail(COMFY, PORT)
    tail = "".join(lines) or "(log not found)\n"
    sys.exit(f"{message}\nRAM available now: {rt.ram_available_gib():.1f} GiB. Last lines of {path}:\n{tail}"
             "earlyoom kills ComfyUI without a traceback; a HIP OOM or a Python error shows above.")


def run(prompt, stage=None):
    """Submit an API prompt, wait, return the list of output file paths (absolute)."""
    if stage:
        prepare_stage(stage)
    req = urllib.request.Request(URL + "/prompt", json.dumps({"prompt": prompt}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        pid = json.load(urllib.request.urlopen(req, timeout=30))["prompt_id"]
    except urllib.error.HTTPError as e:  # prompt rejected: the response body says why
        sys.exit(f"prompt rejected: {e.read() or e}")
    except urllib.error.URLError as e:   # server down or unreachable
        sys.exit(f"ComfyUI not reachable: {e.reason}")
    t0 = time.time()
    while True:
        time.sleep(5)
        try:
            h = json.load(urllib.request.urlopen(f"{URL}/history/{pid}", timeout=30))
        except (urllib.error.URLError, OSError):
            # earlyoom kills ComfyUI mid-run (exit 144, no error): say that, don't raise a traceback
            comfy_died(f"ComfyUI died or became unreachable while {pid} ran: check --cache-none and free RAM")
        if pid in h:
            break
        elapsed = time.time() - t0
        if elapsed > MAX_WAIT:
            sys.exit(f"no result after {elapsed:.0f}s for {pid}: it may still be running; poll {URL}/history/{pid} or raise PA_MAX_WAIT")
        print(f"\r  running {elapsed:.0f}s", end="", flush=True)
    st = h[pid]["status"]
    print(f"\r  {st.get('status_str')} in {time.time() - t0:.0f}s")
    for m in st.get("messages", []):
        if m[0] == "execution_error":
            detail = m[1]
            if "NAF" in str(detail.get("exception_message", "")):
                print("  real NAF failed inside Pixal3D (TRELLIS2_NAF_STRICT stopped a bilinear fallback)")
            sys.exit(json.dumps(detail)[:2000])
    files = []
    for o in h[pid]["outputs"].values():
        for k in ("images", "3d", "result"):
            for it in o.get(k) or []:
                if isinstance(it, dict) and "filename" in it:
                    files.append(os.path.join(COMFY, it.get("type", "output"), it.get("subfolder", ""), it["filename"]))
                elif isinstance(it, str) and it.endswith((".glb", ".obj", ".ply")):
                    files.append(it if os.path.isabs(it) else os.path.join(COMFY, "output", it))
    for f in files:
        print("  ->", f)
    return files


def front(name, desc, seed=None):
    p = wf("01_front.json")
    apply_prompt_settings(p, "front", FRONT_PROMPT.format(desc=desc))
    p["7"]["inputs"]["seed"] = new_seed(seed)
    p["9"]["inputs"]["filename_prefix"] = f"PixelArtistry/{name}_01_front"
    return run(p, "front")


def sheet(name, image, desc, seed=None):
    dst = f"pa_{name}_front.png"
    shutil.copy(image, os.path.join(COMFY, "input", dst))
    p = wf("02_sheet.json")
    p["10"]["inputs"]["image"] = dst
    apply_prompt_settings(p, "sheet", SHEET_PROMPT.format(desc=desc))
    p["7"]["inputs"]["seed"] = new_seed(seed)
    p["9"]["inputs"]["filename_prefix"] = f"PixelArtistry/{name}_02_sheet"
    return run(p, "sheet")


def show_prompts(desc):
    """Dry run: the exact Qwen prompt, negative and CFG each generation stage would send. No ComfyUI needed."""
    jobs = [("front", "01_front.json", "front", FRONT_PROMPT.format(desc=desc), None),
            ("sheet", "02_sheet.json", "sheet", SHEET_PROMPT.format(desc=desc), None)]
    jobs += [(f"view {v}", "02_sheet.json", "sheet", VIEW_PROMPT.format(desc=desc, where=VIEW_WHERE[v]), v)
             for v in VIEW_WHERE]
    for label, workflow, stage, positive, view in jobs:
        p = wf(workflow)
        apply_prompt_settings(p, stage, positive, view=view)
        i = p["5"]["inputs"]
        print(f"===== {label}: {len(i['prompt'].split())} words, cfg {float(p['7']['inputs']['cfg']):g}\n{i['prompt']}\n"
              f"----- negative\n{i['negative_prompt'] or '(none)'}\n")


def split(name, sheet_png):
    out = os.path.join(COMFY, "input", f"{name}_views")
    subprocess.run([PY, os.path.join(HERE, "split_sheet.py"), sheet_png, out], check=True)
    print("  views ->", out, "(look at them before meshing!)")
    return out


def views(name, image, desc, seed=None):
    """Item 8: left, back and right generated separately from the front image, then cut out and normalised."""
    dst = f"pa_{name}_front.png"
    shutil.copy(image, os.path.join(COMFY, "input", dst))
    seed = new_seed(seed)
    paths = [os.path.abspath(image)]
    for view in ("left", "back", "right"):
        p = wf("02_sheet.json")
        p["10"]["inputs"]["image"] = dst
        apply_prompt_settings(p, "sheet", VIEW_PROMPT.format(desc=desc, where=VIEW_WHERE[view]), view=view)
        p["6"]["inputs"]["width"] = p["6"]["inputs"]["height"] = 1024
        p["7"]["inputs"]["seed"] = seed
        p["9"]["inputs"]["filename_prefix"] = f"PixelArtistry/{name}_02_{view}"
        print(f"  {view} view")
        paths.append(run(p, "view")[0])
    out = os.path.join(COMFY, "input", f"{name}_views")
    subprocess.run([PY, os.path.join(HERE, "split_sheet.py"), "--views", out, *paths], check=True)
    print("  views ->", out, "(look at them before meshing!)")
    return out


def qa_json(*args):
    """Run view_qa.py in the ComfyUI env (numpy/PIL/trimesh). Returns its report, or None if it could not run."""
    try:
        r = subprocess.run([PY, os.path.join(HERE, "view_qa.py"), *args], capture_output=True, text=True, timeout=600)
        return json.loads(r.stdout) if r.returncode in (0, 2) else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def check_views(views_dir):
    """Item 2: warn before reconstruction when the four views disagree (Pixal3D will average the conflict)."""
    report = qa_json("check", views_dir)
    if report is None:
        print("  view check could not run")
    elif report["passed"]:
        print(f"  views consistent (mirror IoU front/back {report['mirror_iou']['front_back']:.2f}, "
              f"left/right {report['mirror_iou']['left_right']:.2f})")
    else:
        for issue in report["issues"]:
            print("  VIEW WARNING:", issue)
    return report


def mesh_qa(views_dir, glb):
    """Items 5 + 13: silhouettes from the view cameras vs the views, fragments/holes/non-manifold, score."""
    report = qa_json("mesh", views_dir, glb)
    if report is None:
        print("  mesh QA could not run")
        return None
    print("  mesh QA: score {:.3f}, IoU ".format(report["score"]) +
          " ".join(f"{v} {r['iou']:.2f}" for v, r in report["views"].items()))
    for issue in report["issues"]:
        print("  MESH WARNING:", issue)
    with open(os.path.splitext(glb)[0] + ".qa.json", "w") as stream:
        json.dump(report, stream, indent=1)
    return report


def mesh_judge(views_dir, glb, name):
    """An LLM compares clay renders of the mesh with the views (mesh_judge.py): silhouettes cannot see a melted
    star, a missing back plate or front relief copied onto the back. PA_JUDGE lists the backends in order
    (default strata,agy,codex; the first that answers judges); PA_JUDGE=off skips it. None when nobody judged."""
    backends = os.environ.get("PA_JUDGE", "strata,agy,codex")
    if backends == "off":
        return None
    try:
        r = subprocess.run([PY, os.path.join(HERE, "mesh_judge.py"), views_dir, glb, "--backends", backends,
                            "--name", name], capture_output=True, text=True, timeout=1800,
                           env=dict(os.environ, LD_LIBRARY_PATH=TORCH_LIBS + ":" + os.environ.get("LD_LIBRARY_PATH", "")))
        result = json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as error:
        print(f"  mesh judge could not run ({type(error).__name__}); falling back to the silhouette check")
        return None
    with open(os.path.splitext(glb)[0] + ".judge.json", "w") as stream:
        json.dump(result, stream, indent=1)
    if not result.get("verdict"):
        print("  mesh judge: no backend answered (" +
              "; ".join(f"{k}: {v}" for k, v in result.get("skipped", {}).items()) + "); falling back to the silhouette check")
        return None
    print(f"  mesh judge ({result['backend']}): {result['verdict'].upper()} {result['score']:g}/10  sheet {result['sheet']}")
    for problem in result["problems"]:
        print("    -", problem)
    return result


def candidate_rank(c):
    """Judged candidates by the judge (pass first, then score), the silhouette score breaking ties."""
    judge, qa = c["judge"], c["qa"]
    return (judge["verdict"] == "pass" if judge else False, judge["score"] if judge else -1.0,
            qa["score"] if qa else float("-inf"))


TORCH_LIBS = "/mnt/ssd/ComfyUI/.runtime-deps/llama_cpp/lib:/opt/rocm/lib"   # what launch_best_cpu_vae.sh exports


def parts(name, glb=None, device="cpu"):
    """Item 4: name the mesh's parts from the views. Florence-2 finds each part of <name>_parts.json in each
    view, SAM cuts it out, and the view cameras cast those labels onto the faces -> output/3D/<name>_parts/.
    Detection is cached until the spec or the views change, so a new mesh only re-runs the cheap labelling."""
    spec = os.path.join(HERE, f"{name}_parts.json")
    if not os.path.exists(spec):
        sys.exit(f"missing {spec}; list the parts to find, e.g.\n"
                 '  {"parts": [{"name": "skull", "prompt": "skull", "kind": "ornament", "max": 2}]}\n'
                 "  kinds: plate, ornament, round, gem, chain")
    glb = glb or newest_mesh(name)
    views_dir = os.path.join(COMFY, "input", f"{name}_views")
    out = os.path.join(COMFY, "output", "3D", f"{name}_parts")
    script = os.path.join(HERE, "parts.py")
    inputs = [spec] + [os.path.join(views_dir, f"{v}.png") for v in ("front", "left", "back", "right")]
    labels = [os.path.join(out, f"labels_{v}.png") for v in ("front", "left", "back", "right")]
    if all(os.path.exists(x) for x in labels) and \
            min(map(os.path.getmtime, labels)) > max(map(os.path.getmtime, inputs)):
        print("  part detection cached in", out)
    else:
        print(f"  detecting parts in the views (Florence-2 + SAM on {device})")
        env = dict(os.environ, LD_LIBRARY_PATH=TORCH_LIBS + ":" + os.environ.get("LD_LIBRARY_PATH", ""))
        subprocess.run([PY, script, "detect", views_dir, spec, out, "--device", device], check=True, env=env)
    r = subprocess.run([PY, script, "label", glb, spec, out], check=True, capture_output=True, text=True)
    report = json.loads(r.stdout)
    for part, info in report["parts"].items():
        print(f"  {part:<12} {info['kind']:<9} {info['faces']:>8} faces  {info['area_share']:6.1%} of the area")
    for part in report["missing"]:
        print(f"  PARTS WARNING: {part} was not found in any view (check its prompt in {os.path.basename(spec)})")
    print("  parts ->", os.path.join(out, "parts.glb"), "(transfer_<view>.png: views vs mesh labels)")
    return out


def naf_preflight():
    """Item 1: refuse to reconstruct on a server that would use the bilinear DummyNAF stand-in."""
    problems = rt.naf_problems(rt.server_env(PORT))
    if problems is None:
        print("  NAF check: cannot read the server's environment (not running, or not local); relying on TRELLIS2_NAF_STRICT")
    elif problems and os.environ.get("PA_ALLOW_BILINEAR_NAF") != "1":
        sys.exit("ComfyUI is not running strict real NAF: " + ", ".join(problems) +
                 "\nRestart it with launch_comfy_qwen.sh (or set PA_ALLOW_BILINEAR_NAF=1 to accept bilinear features).")
    elif problems:
        print("  NAF check: accepting a non-strict server (PA_ALLOW_BILINEAR_NAF=1):", ", ".join(problems))
    else:
        print("  NAF check: strict real NAF")




def _pa_recover_fresh_glb(name, started_ns):
    """Recover an exporter-written GLB not listed in ComfyUI history.

    Never return an older run, a malformed GLB, or a processed game asset.
    """
    import struct
    root = os.path.join(COMFY, "output", "3D")
    pattern = os.path.join(root, f"{name}_*.glb")
    matches = []
    for path in glob.glob(pattern):
        try:
            stat = os.stat(path)
            if stat.st_mtime_ns < started_ns - 2_000_000_000 or stat.st_size < 24:
                continue
            if path.endswith(("_lp.glb", "_game.glb", "_repaired.glb")):
                continue
            with open(path, "rb") as stream:
                header = stream.read(20)
            if len(header) != 20 or header[:4] != b"glTF":
                continue
            version, size = struct.unpack("<II", header[4:12])
            if version != 2 or size != stat.st_size:
                continue
            matches.append((stat.st_mtime_ns, path))
        except (OSError, ValueError):
            continue
    if len(matches) == 1:
        return matches[0][1]
    if len(matches) > 1:
        print("  GLB recovery ambiguous: multiple fresh exports:",
              *[p for _, p in sorted(matches)], sep="\n    ")
    return None
def generate(name, seed):
    """One Pixal3D reconstruction. Returns (raw GLB or None, all output files)."""
    p = wf("03_mesh.json")
    # Pixal3D's left_image camera (node 2, az270) sees the object's right side and right_image (node 4, az90)
    # its left: the object faces -Y and az90 sits at +X. Fed by name, the sides told it the front was the back.
    for nid, view in zip("1234", ("front", "right", "back", "left")):
        p[nid]["inputs"]["image"] = f"{name}_views/{view}.png"
    p["6"]["inputs"]["seed"] = seed
    p["8"]["inputs"]["filename_prefix"] = f"3D/{name}"
    _pa_mesh_started_ns = time.time_ns()
    files = run(p, "mesh")
    if not any(isinstance(f, str) and f.lower().endswith(".glb") for f in files):
        _pa_fresh = _pa_recover_fresh_glb(name, _pa_mesh_started_ns)
        if _pa_fresh:
            print("  GLB export recovered from this run ->", _pa_fresh)
            files.append(_pa_fresh)
    return next((f for f in reversed(files) if f.lower().endswith(".glb")), None), files


def mesh(name, seed=None, mesh_options=None, tries=1):
    """Item 6: up to `tries` reconstructions with fresh seeds; stop at the first that passes mesh QA,
    else keep the best-scoring one (never simply the latest)."""
    views_dir = os.path.join(COMFY, "input", f"{name}_views")
    for view in ("front", "left", "back", "right"):
        if not os.path.exists(os.path.join(views_dir, f"{view}.png")):
            sys.exit(f"missing ComfyUI/input/{name}_views/{view}.png - run split first")
    naf_preflight()
    check_views(views_dir)
    candidates = []
    for attempt in range(max(1, int(tries))):
        if tries > 1:
            print(f"  reconstruction {attempt + 1}/{tries}")
        used = new_seed(seed if attempt == 0 else None, 2 ** 31)
        glb, files = generate(name, used)
        if not glb:
            print("  Pixal3D returned no GLB for seed", used)
            continue
        qa = mesh_qa(views_dir, glb)
        verdict = mesh_judge(views_dir, glb, name)
        candidates.append({"seed": used, "glb": glb, "files": files, "qa": qa, "judge": verdict})
        if verdict:   # an LLM looked at it: its verdict decides, the silhouette score only breaks ties
            if verdict["verdict"] == "pass":
                break
        elif qa is None or qa["passed"]:
            break
    if not candidates:
        sys.exit("Pixal3D returned no GLB; refusing to process an unrelated older generation")
    best = max(candidates, key=candidate_rank)
    if len(candidates) > 1:
        judged = f"judge {best['judge']['score']:g}/10, " if best["judge"] else ""
        print(f"  keeping seed {best['seed']} ({judged}score {best['qa']['score']:.3f}) of {len(candidates)} reconstructions")
    if best["judge"] and best["judge"]["verdict"] != "pass":
        print(f"  MESH WARNING: no reconstruction passed the {best['judge']['backend']} judge; keeping the best-rated one")
    rt.record(COMFY, name, "mesh", seed=best["seed"], outputs=[best["glb"]], qa_passed=bool(best["qa"] and best["qa"]["passed"]),
              judge=best["judge"],
              candidates=[{"seed": c["seed"], "glb": c["glb"], "score": c["qa"] and c["qa"]["score"],
                           "judge": c["judge"] and {k: c["judge"][k] for k in ("verdict", "score", "backend")}}
                          for c in candidates])
    return finish(name, best["glb"], best["files"], mesh_options)


def finish(name, raw, files, mesh_options=None):
    """Production processing of a chosen raw reconstruction; records the asset stage for --resume."""
    if os.environ.get("PA_NO_FIX") == "1":
        print("  PA_NO_FIX=1: keeping raw generation only")
        return files
    if os.environ.get("PA_LEGACY_PREP") == "1":
        allowed = {"faces", "repair", "angle", "unwrap", "formats", "uv_angle", "uv_method", "uv_merge_faces", "dust_size", "source_up", "yaw", "texture_size", "uv_margin_px",
                   "weld_distance", "hole_max_edges", "hole_max_diameter", "min_component_faces", "smooth_iterations",
                   "smooth_factor", "voxel_size", "materials", "strict"}
        return [prep(name, raw, **{k: v for k, v in (mesh_options or {}).items() if k in allowed})] + files
    lod0 = asset(name, raw, **(mesh_options or {}))
    rt.record(COMFY, name, "asset", source=raw, outputs=[lod0])
    return [lod0] + files


def asset(name, glb=None, **options):
    """Complete seven-stage production build, returning the canonical LOD0 GLB."""
    glb = glb or newest_mesh(name)
    directory = os.path.join(COMFY, "output", "GameAssets", name)
    os.makedirs(os.path.dirname(directory), exist_ok=True)
    args = [os.environ.get("BLENDER_BIN", "blender"), "-b", "--factory-startup", "--disable-autoexec",
            "--python-exit-code", "1", "--python", os.path.join(HERE, "build_asset.py"), "--",
            os.path.abspath(glb), directory, "--name", name]
    for key, value in options.items():
        if key == "uv_relax":
            if not value:
                args.append("--no-uv-relax")
            continue
        if value is None or value is False:
            continue
        args.append("--" + key.replace("_", "-"))
        if value is not True:
            args.append(",".join(map(str, value)) if isinstance(value, (tuple, list)) else str(value))
    started = time.time_ns()
    log = directory + ".build.log"
    try:
        returncode = run_asset_process(args, log, int(os.environ.get("PA_ASSET_TIMEOUT", 7200)))
    except FileNotFoundError:
        sys.exit("Blender not found; install Blender 4.2+ or set BLENDER_BIN")
    except subprocess.TimeoutExpired:
        sys.exit("Production build timed out; inspect the staging report and increase PA_ASSET_TIMEOUT if needed")
    try:
        path = os.path.join(directory, "asset_report.json")
        with open(path) as stream:
            report = json.load(stream)
        fresh = os.stat(path).st_mtime_ns >= started
        output = os.path.join(directory, report["lods"][0]["files"]["glb"])
    except (OSError, ValueError, KeyError, IndexError):
        report, fresh, output = {}, False, ""
    if returncode or not fresh or report.get("status") not in ("exported", "review_required") or \
            not report.get("validation_passed") or not os.path.isfile(output):
        with open(log) as stream:
            detail = stream.read()[-4500:]
        sys.exit(f"Production asset build failed. See {log}\n" + detail)
    if report["status"] == "review_required":
        print("  NOT production-ready - review these before shipping:")
        for issue in report.get("quality", {}).get("issues", []):
            if issue.get("severity") != "info":
                print("   -", issue.get("code"), ":", issue.get("message"))
    keep_source(glb, directory)
    print("  production asset ->", directory)
    print("  report ->", path)
    print("  review ->", os.path.join(directory, "review.html"))
    print("  engine check:", report.get("engine_check", {}).get("status", "not_run"))
    return output


def keep_source(glb, directory):
    """Item 10: the untouched generated high-poly (and its QA) beside the LODs; the .blend keeps the cleaned high."""
    source = os.path.join(directory, "source")
    os.makedirs(source, exist_ok=True)
    for path in (glb, os.path.splitext(glb)[0] + ".qa.json"):
        if os.path.isfile(path):
            shutil.copy2(path, source)


def run_asset_process(args, log, timeout):
    """Stream stage progress, retain the complete log, and kill the process group on timeout."""
    timed_out = False
    with open(log, "w") as stream:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                   start_new_session=os.name != "nt")
        def stop():
            nonlocal timed_out
            timed_out = True
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        timer = threading.Timer(timeout, stop)
        timer.daemon = True
        timer.start()
        try:
            for line in process.stdout:
                stream.write(line)
                if line.startswith(("ASSET", "PREP WARNING")):
                    print(" ", line.rstrip(), flush=True)
            returncode = process.wait()
        except BaseException:
            stop()
            process.wait()
            raise
        finally:
            timer.cancel()
            process.stdout.close()
        if timed_out:
            raise subprocess.TimeoutExpired(args, timeout)
    return returncode


def newest_mesh(name):
    """Newest raw Pixal3D GLB for <name> (skips the processed _lp and _game outputs)."""
    glbs = sorted((g for g in glob.glob(os.path.join(COMFY, "output", "3D", f"{name}_*.glb"))
                   if not g.endswith(("_lp.glb", "_game.glb", "_repaired.glb")) and
                   not os.path.exists(os.path.splitext(g)[0] + ".report.json")), key=os.path.getmtime)
    if not glbs:
        sys.exit(f"no ComfyUI/output/3D/{name}_*.glb - run mesh first or pass the GLB path")
    return glbs[-1]


def prep(name, glb=None, faces=40000, repair="full", angle=35, unwrap="smart", formats=("glb",), **options):
    """Repair -> triangle budget -> final normals -> Smart UV -> PBR transfer -> export."""
    glb = glb or newest_mesh(name)
    out = os.path.join(COMFY, "output", "3D", f"{name}_lp.glb")
    if os.path.abspath(glb) == os.path.abspath(out):
        out = os.path.join(COMFY, "output", "3D", f"{name}_lp_repaired.glb")
    return blender_pass(name, glb, out, faces=faces, repair=repair, angle=angle,
                        unwrap=unwrap, formats=formats, **options)


def fix(name, glb=None, repair="full", angle=35, unwrap="smart", formats=("glb", "obj", "fbx"), faces=0, **options):
    """Game-ready fix without decimation: full repair, normals, Smart UV, multi-format export."""
    glb = glb or newest_mesh(name)
    out = os.path.join(COMFY, "output", "3D", f"{name}_game.glb")
    if os.path.abspath(glb) == os.path.abspath(out):
        out = os.path.join(COMFY, "output", "3D", f"{name}_game_repaired.glb")
    return blender_pass(name, glb, out, faces=faces, repair=repair, angle=angle,
                        unwrap=unwrap, formats=formats, **options)


def sculpt(name, glb=None, voxel=0.0015, quads=25000, levels=3):
    """Sculpt base: drop floaters, voxel remesh to a closed manifold, QuadriFlow cage + multires detail; Smart UV on the cage (levels inherit it)."""
    glb = glb or newest_mesh(name)
    out = os.path.join(COMFY, "output", "3D", f"{name}_sculpt.glb")
    return blender_pass(name, glb, out, faces=0, repair="full", angle=0, unwrap="smart", formats=("glb", "obj", "blend"),
                        voxel_size=voxel, min_component_faces=200, materials="discard",
                        quad_faces=quads, multires=levels if quads else 0)


def blender_pass(name, glb, out, faces, repair, angle, unwrap, formats, **options):
    # blender -b takes its Python home from the first python3.13 on PATH; uv's static one in ~/.local/bin breaks it
    env = dict(os.environ, PATH=":".join(p for p in os.environ.get("PATH", "").split(":") if not p.endswith("/.local/bin")))
    print("  mesh:", glb)
    formats = tuple(dict.fromkeys(("glb",) + tuple(formats)))
    args = [os.environ.get("BLENDER_BIN", "blender"), "-b", "--factory-startup", "--disable-autoexec",
            "--python-exit-code", "1", "--python", os.path.join(HERE, "prep_mesh.py"),
            "--", os.path.abspath(glb), out, "--faces", str(faces)]
    args += ["--repair", repair, "--angle", str(angle), "--unwrap", unwrap, "--formats", ",".join(formats)]
    for key, value in options.items():
        if value is None or value is False:
            continue
        args.append("--" + key.replace("_", "-"))
        if value is not True:
            args.append(str(value))
    started = time.time_ns()
    try:
        r = subprocess.run(args, env=env, capture_output=True, text=True,
                           timeout=int(os.environ.get("PA_BLENDER_TIMEOUT", 3600)))
    except FileNotFoundError:
        sys.exit("Blender not found. Install Blender 4.2+ or set BLENDER_BIN to its executable.")
    except subprocess.TimeoutExpired:
        sys.exit("Blender timed out (PA_BLENDER_TIMEOUT). Original mesh is unchanged.")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    log = os.path.splitext(out)[0] + ".blender.log"
    with open(log, "w") as stream:
        stream.write(r.stdout + "\n" + r.stderr)
    stats = [line for line in r.stdout.splitlines() if line.startswith("PREP OK")]
    report_path = os.path.splitext(out)[0] + ".report.json"
    try:
        with open(report_path) as stream:
            report = json.load(stream)
        fresh = os.stat(report_path).st_mtime_ns >= started
    except (OSError, ValueError):
        report, fresh = {}, False
    if (r.returncode or not stats or not fresh or report.get("status") != "exported" or
            not report.get("validation_passed") or not os.path.isfile(out)):
        sys.exit(f"Blender cleanup failed; see {log}\n" + r.stdout[-2500:] + r.stderr[-2000:])
    for line in r.stdout.splitlines():
        if line.startswith(("PREP OK", "PREP WARNING")):
            print(" ", line)
    return out


def workflow_drift(workflow, info):
    """Node/input mismatches between an API workflow and the server's /object_info (ComfyUI renames inputs)."""
    problems = []
    for nid, node in workflow.items():
        spec = info.get(node.get("class_type"))
        if spec is None:
            problems.append(f"node {nid}: {node.get('class_type')} not installed")
            continue
        required, optional = spec["input"].get("required", {}), spec["input"].get("optional", {})
        given = {key.split(".")[0] for key in node.get("inputs", {})}  # autogrow inputs arrive as images.image_1
        for key in required:
            autogrow = isinstance(required[key][0], str) and required[key][0].startswith("COMFY_AUTOGROW")
            if key not in given and not autogrow:
                problems.append(f"node {nid} {node['class_type']}: missing input {key}")
        for key in sorted(given - required.keys() - optional.keys()):
            problems.append(f"node {nid} {node['class_type']}: unknown input {key}")
    return problems


def check():
    """One-command rig check (run it after a reboot or a ComfyUI update).
    Prints one line per item and returns the number of problems, so pa.py check exits 1."""
    bad = []
    blender = os.environ.get("BLENDER_BIN", "blender")
    if shutil.which(blender):
        print("ok   Blender: " + blender)
    else:
        print("MISSING Blender (install 4.2+ or set BLENDER_BIN)")
        bad.append("Blender")
    M = os.path.join(COMFY, "models")
    for rel in ("diffusion_models/qwen-image-2.1-Q8_0.gguf", "vae/qwen_image_2.1_vae_bf16.safetensors",
                "vae/trellis_2_texture_vae_bf16.safetensors", "vae/trellis_2_shape_vae_bf16.safetensors",
                "clip_vision/dino_v3_L_naf_fp32.safetensors", "text_encoders/Qwen3-VL-8B-Instruct-Q8_0.gguf",
                "text_encoders/Qwen3-VL-8B-Instruct-mmproj-F16.gguf", "diffusion_models/flux-2-klein-base-9b-Q8_0.gguf",
                "text_encoders/Qwen3-8B-Q8_0.gguf", "vae/flux2-vae.safetensors", "loras/ultratex_flux2.safetensors",
                "loras/ultratex_flux2_mr.safetensors", "ultratex/ultratex_flux2_decoder.pt",
                "background_removal/birefnet.safetensors", "background_removal/isnet-general-use.onnx"):
        # same two places setup_models.sh accepts: models/<type>/ and the models/unet/ fallback
        ok = any(os.path.exists(os.path.join(M, d, rel)) for d in ("", "unet"))
        print(("ok   " if ok else "MISSING ") + rel)
        bad += [rel] if not ok else []
    node = os.path.join(COMFY, "custom_nodes", "ComfyUI-Trellis2-GGUF")
    src = "".join(open(f).read() for f in (os.path.join(node, "trellis2_gguf", "pipelines", "trellis2_image_to_3d.py"),
                                           os.path.join(node, "model_manager.py")) if os.path.exists(f))
    for needle in ("def run_pixal3d_multiview", "def get_proj_cond_shape_mv",
                   "if getattr(self, 'isPixal3D', False):", "Pixal3D-GGUF-MV"):
        print(("ok   " if needle in src else "MISSING ") + f"trellis2 node: {needle}")
        bad += [needle] if needle not in src else []
    head = subprocess.run(["git", "-C", os.path.join(COMFY, "custom_nodes", "ComfyUI-UltraTex"),
                           "log", "--oneline", "-1"], capture_output=True, text=True).stdout
    print(("ok   " if "8137da5" in head else "CHECK  ") + "ComfyUI-UltraTex pinned at 8137da5, the commit PixelArtistry's "
          f"workflows target (got {head.strip() or 'no clone'})")
    bad += ["ultratex pin"] if "8137da5" not in head else []
    try:
        urllib.request.urlopen(URL + "/system_stats", timeout=5)
        print("ok   ComfyUI server reachable at " + URL)
        info = json.load(urllib.request.urlopen(URL + "/object_info", timeout=60))
        for name in sorted(os.listdir(os.path.join(HERE, "workflows"))):
            problems = workflow_drift(wf(name), info)
            print(("ok   " if not problems else "DRIFT  ") + f"workflow {name}" + "".join("\n       " + p for p in problems))
            bad += problems
    except (urllib.error.URLError, OSError) as e:
        print(f"DOWN   ComfyUI server at {URL}: {e} - start it with launch_comfy_qwen.sh")
        bad += ["server down"]
    if bad:
        print(f"{len(bad)} problem{'s' if len(bad) != 1 else ''} above")
    else:
        print("all checks passed")
    return len(bad)


def texture(name, glb=None, res=1024):
    """UltraTex: re-texture a mesh from the front view (PBR albedo + ORM). res 1024 = test, 2048 = final (~4x slower)."""
    if not glb:
        lp = os.path.join(COMFY, "output", "3D", f"{name}_lp.glb")
        game = os.path.join(COMFY, "output", "3D", f"{name}_game.glb")
        # Prefer the game triangle budget over an old high-resolution _game output.
        manifest = os.path.join(COMFY, "output", "GameAssets", name, "asset_report.json")
        try:
            with open(manifest) as stream:
                production = json.load(stream)
            glb = os.path.join(os.path.dirname(manifest), production["lods"][0]["files"]["glb"])
        except (OSError, ValueError, KeyError, IndexError):
            glb = lp if os.path.exists(lp) else game if os.path.exists(game) else newest_mesh(name)
    ref = f"{name}_views/front.png"
    if not os.path.exists(os.path.join(COMFY, "input", ref)):
        sys.exit(f"missing ComfyUI/input/{ref} - run split first")
    # Check provenance, not the filename suffix. Explicit external models also get the pass.
    try:
        report_path = os.path.splitext(glb)[0] + ".report.json"
        with open(report_path) as stream:
            report = json.load(stream)
        report_root = os.path.abspath(os.path.join(os.path.dirname(report_path), report.get("portable_root", "")))
        outputs = [p if os.path.isabs(p) else os.path.join(report_root, p) for p in report.get("outputs", [])]
        prepared = (report.get("status") == "exported" and report.get("validation_passed") and
                    report.get("options", {}).get("unwrap") == "smart" and
                    os.path.abspath(glb) in map(os.path.abspath, outputs) and
                    os.path.getmtime(report_path) >= os.path.getmtime(glb))
    except (OSError, ValueError):
        prepared = False
    if not prepared:
        glb = prep(name, glb, materials="discard")
    print("  mesh:", glb)
    p = wf("04_texture.json")
    p["1"]["inputs"]["image"] = ref
    p["2"]["inputs"]["glb_path"] = os.path.abspath(glb)
    p["14"]["inputs"]["uv_mode"] = "keep_existing"
    seed = new_seed(None, 2 ** 31)
    for nid in ("18", "20"):  # albedo and metal/rough passes must share resolution and seed
        p[nid]["inputs"]["resolution"] = int(res)
        p[nid]["inputs"]["seed"] = seed
        p[nid]["inputs"]["attention"] = "comfy_default" if int(res) <= DENSE_MAX_RES else "ultratex_sparse"
        # UltraTex's own advice on out-of-memory: raise memory_factor (0 = auto, 12 with token chunking)
        p[nid]["inputs"]["memory_factor"] = float(os.environ.get("PA_TEXTURE_MEMORY_FACTOR",
                                                                 p[nid]["inputs"]["memory_factor"]))
    p["23"]["inputs"]["filename_prefix"] = f"UltraTex/{name}"
    # The generated view atlas and the normal views it was conditioned on: what to look at when a texture is wrong.
    p["30"]["inputs"]["filename_prefix"] = f"UltraTex/{name}_views"
    p["31"]["inputs"]["filename_prefix"] = f"UltraTex/{name}_normals"
    return run(p, "texture")


def mesh_arguments(rest, command):
    parser = argparse.ArgumentParser(prog=f"pa.py {command}")
    parser.add_argument("items", nargs="*")
    add_options(parser, faces=0 if command == "fix" else 40000,
                formats="glb,obj,fbx" if command == "fix" else "glb")
    args = parser.parse_args(rest)
    items = args.items
    del args.items
    if command in ("prep", "fix"):
        paths = [x for x in items if not x.isdigit()]
        counts = [x for x in items if x.isdigit()]
        if len(paths) > 1 or len(counts) > 1:
            parser.error("expected at most one input mesh and one triangle count")
        if counts and not any(x == "--faces" or x.startswith("--faces=") for x in rest):
            args.faces = int(counts[0])
        source = paths[0] if paths else None
    else:
        if len(items) > 1 or (items and not items[0].isdigit()):
            parser.error("expected at most one numeric seed")
        source = items[0] if items else None
    return source, vars(validate(parser, args))


def production_arguments(rest, command):
    items = list(rest)
    source = items.pop(0) if items and not items[0].startswith("--") else None
    if command in ("mesh", "all") and source is not None and not source.isdigit():
        sys.exit("Expected a numeric seed before production options")
    options = vars(parse_asset(["generated.glb", "output", "--name", "asset", *items]))
    for key in ("src", "dst", "name"):
        options.pop(key)
    return source, options


def pa_flags(rest):
    """Strip pa-level flags before the production options parser sees them."""
    rest, tries, resume, separate = list(rest), 1, False, False
    for flag in ("--resume", "--separate-views"):
        while flag in rest:
            rest.remove(flag)
            resume, separate = resume or flag == "--resume", separate or flag == "--separate-views"
    for i, item in enumerate(rest):
        if item == "--tries" or item.startswith("--tries="):
            value = item.split("=", 1)[1] if "=" in item else (rest[i + 1] if i + 1 < len(rest) else "")
            if not value.isdigit() or int(value) < 1:
                sys.exit("--tries needs a positive whole number")
            del rest[i:i + (1 if "=" in item else 2)]
            tries = int(value)
            break
    return rest, tries, resume, separate


def all_stages(name, src, desc, rest):
    """Everything in one go. --resume reuses each stage recorded in PixelArtistry/<name>.run.json when it ran
    with the same inputs and its files still exist; a regenerated stage invalidates everything after it."""
    rest, tries, resume, separate = pa_flags(rest)
    seed, options = production_arguments(rest, "all")
    if src.startswith("text:"):
        done = resume and rt.reusable(COMFY, name, "front", desc=src[5:], seed=seed, prompts=prompt_fingerprint())
        if done:
            img = done["outputs"][0]
            print("  resume: front view", img)
        else:
            img = front(name, src[5:], seed)[0]
            rt.record(COMFY, name, "front", desc=src[5:], seed=seed, prompts=prompt_fingerprint(), outputs=[img])
    else:
        done, img = False, src
    views_dir = os.path.join(COMFY, "input", f"{name}_views")
    view_inputs = dict(image=os.path.abspath(img), desc=desc, separate=separate, seed=seed,
                       prompts=prompt_fingerprint())
    done = done is not False and resume and rt.reusable(COMFY, name, "views", **view_inputs)
    if done:
        print("  resume: views", views_dir)
    else:
        if separate:
            views(name, img, desc, seed)
        else:
            split(name, sheet(name, img, desc, seed)[0])
        rt.record(COMFY, name, "views", **view_inputs,
                  outputs=[os.path.join(views_dir, f"{v}.png") for v in ("front", "left", "back", "right")])
    made = done and rt.reusable(COMFY, name, "mesh")
    if not made:
        return mesh(name, seed, options, tries)
    raw = made["outputs"][0]
    print("  resume: reconstruction", raw)
    built = rt.reusable(COMFY, name, "asset", source=raw)
    if built:
        print("  resume: production asset already built ->", built["outputs"][0])
        return built["outputs"]
    return finish(name, raw, [raw], options)


if __name__ == "__main__":
    configure_from_argv(sys.argv)
    a = sys.argv[1:]
    if not a or a[0] not in ("check", "front", "sheet", "split", "views", "mesh", "prep", "fix", "sculpt", "asset",
                             "texture", "parts", "prompts", "all") or (a[0] != "check" and len(a) < 2):
        sys.exit(__doc__)
    cmd = a[0]
    if cmd == "check":
        sys.exit(1 if check() else 0)
    name = a[1]
    if not (name[:1].isalpha() and all(c.isalnum() or c == "_" for c in name)):
        sys.exit(f"<name> must be one word of letters, digits or _: got {name!r}")
    if cmd == "front":
        front(name, a[2], next((x for x in a[3:] if x.isdigit()), None))
    elif cmd == "sheet":
        sheet(name, a[2], a[3], next((x for x in a[4:] if x.isdigit()), None))
    elif cmd == "prompts":
        show_prompts(a[2] if len(a) > 2 else name)
    elif cmd == "split":
        split(name, a[2])
    elif cmd == "views":
        views(name, a[2], a[3], next((x for x in a[4:] if x.isdigit()), None))
    elif cmd == "mesh":
        rest, tries, _, _ = pa_flags(a[2:])
        seed, options = production_arguments(rest, cmd)
        mesh(name, seed, options, tries)
    elif cmd == "asset":
        source, options = production_arguments(a[2:], cmd)
        raw = os.path.abspath(source or newest_mesh(name))
        if rt.pick(COMFY, name, raw):
            print("  run record: mesh pick ->", raw)
        rt.record(COMFY, name, "asset", source=raw, outputs=[asset(name, raw, **options)])
    elif cmd in ("prep", "fix"):
        source, options = mesh_arguments(a[2:], cmd)
        (prep if cmd == "prep" else fix)(name, source, **options)
    elif cmd == "sculpt":
        rest = a[2:]
        glb = next((x for x in rest if not x.replace(".", "", 1).isdigit()), None)
        voxel = next((float(x) for x in rest if x.replace(".", "", 1).isdigit()), 0.0015)
        sculpt(name, glb, voxel)
    elif cmd == "texture":
        rest = a[2:]
        res = int(next((x for x in rest if x in ("1024", "1536", "2048")), 1024))
        glb = next((x for x in rest if os.path.splitext(x)[1].lower() in
                    (".glb", ".gltf", ".obj", ".ply", ".fbx", ".stl", ".blend")), None)
        texture(name, glb, res)
    elif cmd == "parts":
        rest = a[2:]
        device = rest[rest.index("--device") + 1] if "--device" in rest else "cpu"
        glb = next((x for x in rest if x.lower().endswith(".glb")), None)
        parts(name, glb, device)
    else:
        all_stages(name, a[2], a[3], a[4:])
