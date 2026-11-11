"""pa.py runtime helpers: real-NAF preflight, RAM/VRAM checks between stages, ComfyUI diagnostics,
and the per-asset run manifest that lets `pa.py all --resume` skip stages that already succeeded."""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

# The server must have been started by launch_comfy_qwen.sh: real NAF, flex natten, fallbacks are errors.
NAF_ENV = {"TRELLIS2_REAL_NAF": "1", "TRELLIS2_NATTEN_IMPL": "flex", "TRELLIS2_NAF_STRICT": "1"}
# Free memory a stage needs before it is submitted, GiB (RAM, VRAM). Measured peaks on the 32 GB / RX 6800 rig
# are higher; these are the floors below which earlyoom or a HIP OOM ends the run. PA_MIN_RAM_GB overrides RAM.
STAGE_NEEDS = {"front": (10, 10), "sheet": (10, 10), "view": (10, 10), "mesh": (12, 10), "texture": (12, 10)}


def server_pid(port):
    """PID of the process listening on 127.0.0.1:<port>, or None (ss is iproute2, present on Void)."""
    try:
        out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"pid=(\d+)", out)
    return int(match.group(1)) if match else None


def server_env(port):
    pid = server_pid(port)
    if pid is None:
        return None
    try:
        with open(f"/proc/{pid}/environ", "rb") as stream:
            items = stream.read().split(b"\0")
    except OSError:
        return None
    return dict(item.decode(errors="replace").split("=", 1) for item in items if b"=" in item)


def naf_problems(env):
    """[] when the server runs real, strict NAF; otherwise one line per wrong variable. None: cannot tell."""
    if env is None:
        return None
    return [f"{key}={env.get(key, '<unset>')} (needs {value})" for key, value in NAF_ENV.items() if env.get(key) != value]


def ram_available_gib():
    with open("/proc/meminfo") as stream:
        for line in stream:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2 ** 20
    return 0.0


def vram_free_gib(url):
    try:
        stats = json.load(urllib.request.urlopen(url + "/system_stats", timeout=10))
        return max(d.get("vram_free", 0) for d in stats.get("devices", [{}])) / 2 ** 30
    except (urllib.error.URLError, OSError, ValueError):
        return None


def free_models(url):
    """Ask ComfyUI to unload every model and drop cached memory (POST /free)."""
    req = urllib.request.Request(url + "/free", json.dumps({"unload_models": True, "free_memory": True}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30)
        time.sleep(3)   # unloading is asynchronous; the next /system_stats should see the freed memory
        return True
    except (urllib.error.URLError, OSError):
        return False


def memory_problem(url, stage):
    """None if the stage can start, else a one-line reason. Thresholds: STAGE_NEEDS, PA_MIN_RAM_GB/PA_MIN_VRAM_GB."""
    need_ram, need_vram = STAGE_NEEDS.get(stage, (8, 8))
    need_ram = float(os.environ.get("PA_MIN_RAM_GB", need_ram))
    need_vram = float(os.environ.get("PA_MIN_VRAM_GB", need_vram))
    ram, vram = ram_available_gib(), vram_free_gib(url)
    print(f"  memory before {stage}: RAM {ram:.1f} GiB available, VRAM "
          + (f"{vram:.1f} GiB free" if vram is not None else "unknown"))
    if ram < need_ram:
        return f"only {ram:.1f} GiB RAM available, {stage} needs {need_ram:.0f} (close other programs or set PA_MIN_RAM_GB)"
    if vram is not None and vram < need_vram:
        return f"only {vram:.1f} GiB VRAM free, {stage} needs {need_vram:.0f} (another GPU job? set PA_MIN_VRAM_GB)"
    return None


def log_tail(comfy, port, lines=25):
    """The last lines of ComfyUI's own log: the traceback or the last stage it reached."""
    path = os.path.join(comfy, "user", f"comfyui_{port}.log")
    try:
        with open(path, errors="replace") as stream:
            return path, stream.readlines()[-lines:]
    except OSError:
        return path, []


# --- run manifest (item 15) -----------------------------------------------------------------------
def manifest_path(comfy, name):
    return os.path.join(comfy, "output", "PixelArtistry", f"{name}.run.json")


def load_manifest(comfy, name):
    try:
        with open(manifest_path(comfy, name)) as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return {"name": name, "stages": {}}


def record(comfy, name, stage, **data):
    manifest = load_manifest(comfy, name)
    manifest["stages"][stage] = dict(data, time=time.strftime("%Y-%m-%d %H:%M:%S"))
    path = manifest_path(comfy, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as stream:
        json.dump(manifest, stream, indent=1)
    os.replace(path + ".tmp", path)   # never leave a half-written manifest after a crash
    return manifest


def pick(comfy, name, glb):
    """Make a hand-picked mesh candidate the recorded reconstruction, so `all --resume` builds from it
    instead of the best-scoring one. False when glb is not a recorded candidate (or already the pick)."""
    mesh = load_manifest(comfy, name)["stages"].get("mesh") or {}
    hit = next((c for c in mesh.get("candidates", []) if os.path.abspath(c["glb"]) == os.path.abspath(glb)), None)
    if not hit or mesh.get("outputs") == [hit["glb"]]:
        return False
    record(comfy, name, "mesh", **dict(mesh, seed=hit["seed"], outputs=[hit["glb"]], picked=True))
    return True


def reusable(comfy, name, stage, **inputs):
    """The recorded stage if it ran with these inputs and every output still exists, else None.
    An input passed as None (e.g. no seed given) is not compared."""
    done = load_manifest(comfy, name)["stages"].get(stage)
    if not done:
        return None
    if any(value is not None and done.get(key) != value for key, value in inputs.items()):
        return None
    outputs = done.get("outputs", [])
    return done if outputs and all(os.path.exists(p) for p in outputs) else None
