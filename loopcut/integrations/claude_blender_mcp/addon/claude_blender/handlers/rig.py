"""rig toolset: skeletons (simple game rig, Rigify, UniRig), binding, weights, posing, checks."""

import contextlib
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import bpy
from mathutils import Euler, Vector

from . import CommandError, command
from .util import get_mesh, get_object, link_to_scene, ops_context, vec

# Humanoid template in units of total height, facing -Y, +Z up, feet at z=0, in a T-pose.
# (name, head, tail, parent). Mirrored .L/.R are generated from the .L entries.
HUMANOID = [
    ("root", (0, 0, 0), (0, 0.1, 0), None),
    ("hips", (0, 0, 0.53), (0, 0, 0.6), "root"),
    ("spine", (0, 0, 0.6), (0, 0, 0.68), "hips"),
    ("chest", (0, 0, 0.68), (0, 0, 0.8), "spine"),
    ("neck", (0, 0, 0.83), (0, -0.01, 0.88), "chest"),
    ("head", (0, -0.01, 0.88), (0, -0.01, 1.0), "neck"),
    ("shoulder.L", (0.02, 0, 0.81), (0.09, 0, 0.82), "chest"),
    ("upper_arm.L", (0.09, 0, 0.82), (0.25, 0, 0.82), "shoulder.L"),
    ("forearm.L", (0.25, 0, 0.82), (0.39, 0, 0.82), "upper_arm.L"),
    ("hand.L", (0.39, 0, 0.82), (0.46, 0, 0.82), "forearm.L"),
    ("thigh.L", (0.06, 0, 0.53), (0.06, -0.005, 0.29), "hips"),
    ("shin.L", (0.06, -0.005, 0.29), (0.06, 0.01, 0.05), "thigh.L"),
    ("foot.L", (0.06, 0.01, 0.05), (0.06, -0.08, 0.01), "shin.L"),
    ("toe.L", (0.06, -0.08, 0.01), (0.06, -0.12, 0.01), "foot.L"),
]
CONNECTED = {"forearm", "hand", "shin", "foot", "toe", "head", "upper_arm", "chest", "spine"}


def _mirror(name):
    return name[:-2] + ".R" if name.endswith(".L") else name


def _template_bones():
    out = []
    for name, head, tail, parent in HUMANOID:
        out.append((name, head, tail, parent))
        if name.endswith(".L"):
            mh = (-head[0], head[1], head[2])
            mt = (-tail[0], tail[1], tail[2])
            out.append((_mirror(name), mh, mt, _mirror(parent) if parent else None))
    return out


def _fit(mesh_name):
    """Height and center of a mesh standing on the ground, for sizing a template."""
    obj = get_mesh(mesh_name)
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    hi = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    return hi.z - lo.z, Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z)), (hi.x - lo.x)


@command("rig_template", "rig")
def rig_template(name="Armature", kind="humanoid", height=1.8, fit_to=None, arm_span=None, arm_angle=0.0):
    """Create a simple game-ready skeleton (22 deform bones, facing -Y).
    arm_angle: degrees the arms point down from horizontal, to match the mesh's rest pose
    (0 = T-pose, about 45 = A-pose). fit_to: a mesh to size and place it on (height from its
    bounds, arm reach from its width). Adjust individual joints afterwards with edit_bones."""
    if kind != "humanoid":
        raise CommandError("Only kind='humanoid' is built in; use edit_bones for other skeletons")
    origin = Vector((0, 0, 0))
    width = None
    if fit_to:
        height, origin, width = _fit(fit_to)
    angle = math.radians(float(arm_angle))
    shoulder_x = 0.09 * height  # where the arm starts, from the template
    arm_len = (0.46 - 0.09) * height
    span = arm_span or width
    if span:
        # Scale the arm (not the shoulder) so the fingertips reach half the span sideways.
        arm_scale = max(float(span) / 2 - shoulder_x, 0.05 * height) / (arm_len * max(math.cos(angle), 0.2))
    else:
        arm_scale = 1.0
    arm_data = bpy.data.armatures.new(name)
    obj = link_to_scene(bpy.data.objects.new(name, arm_data))
    obj.location = origin
    obj.show_in_front = True

    def place(bname, p):
        p = Vector(p) * height
        if not any(k in bname for k in ("arm", "hand")):
            return p
        side = 1 if p.x > 0 else -1
        pivot = Vector((side * shoulder_x, 0, 0.82 * height))
        d = (p - pivot) * arm_scale
        # Swing the arm down by `angle` around the shoulder, in the body's side plane.
        return pivot + Vector((d.x * math.cos(angle), d.y, d.z - abs(d.x) * math.sin(angle)))

    with ops_context(obj, mode="EDIT"):
        ebs = arm_data.edit_bones
        for bname, head, tail, parent in _template_bones():
            eb = ebs.new(bname)
            eb.head = place(bname, head)
            eb.tail = place(bname, tail)
            if any(k in bname for k in ("arm", "hand")):
                # Keep the T-pose roll convention (local X horizontal, +X raises the arm) when the
                # arm points down, so poses and cycles mean the same thing on an A-pose rig.
                side = 1 if eb.head.x > 0 else -1
                eb.align_roll((eb.tail - eb.head).normalized().cross(Vector((0, 1, 0))) * side)
            eb.use_deform = bname != "root"
            if parent:
                eb.parent = ebs[parent]
                eb.use_connect = bname.split(".")[0] in CONNECTED and (eb.parent.tail - eb.head).length < 1e-4
    obj["claude_arm_angle"] = float(arm_angle)  # pose_test and procedural_cycle offset arm poses by it
    return {"name": obj.name, "bones": len(arm_data.bones), "height": round(height, 4), "arm_angle": float(arm_angle)}


@command("edit_bones", "rig")
def edit_bones(armature, bones=None, remove=None):
    """Add or change bones. bones: [{name, head, tail (world space), parent, connect, deform, roll (deg)}];
    existing names are updated, only given fields change. remove: bone names to delete."""
    obj = get_object(armature, "ARMATURE")
    inv = obj.matrix_world.inverted()
    changed = []
    with ops_context(obj, mode="EDIT"):
        ebs = obj.data.edit_bones
        for name in remove or []:
            eb = ebs.get(name)
            if eb is None:
                raise CommandError(f"No bone {name!r}")
            ebs.remove(eb)
        for spec in bones or []:
            name = spec.get("name")
            if not name:
                raise CommandError("Each bone needs a name")
            eb = ebs.get(name)
            if eb is None:
                if "head" not in spec or "tail" not in spec:
                    raise CommandError(f"New bone {name!r} needs head and tail")
                eb = ebs.new(name)
            if "head" in spec:
                eb.head = inv @ vec(spec["head"])
            if "tail" in spec:
                eb.tail = inv @ vec(spec["tail"])
            if "roll" in spec:
                eb.roll = math.radians(float(spec["roll"]))
            if "parent" in spec:
                p = spec["parent"]
                if p and ebs.get(p) is None:
                    raise CommandError(f"Parent bone {p!r} not found")
                eb.parent = ebs[p] if p else None
            if "connect" in spec:
                eb.use_connect = bool(spec["connect"])
            if "deform" in spec:
                eb.use_deform = bool(spec["deform"])
            if (eb.tail - eb.head).length < 1e-6:
                raise CommandError(f"Bone {name!r} has zero length")
            changed.append(name)
    return {"armature": obj.name, "changed": changed, "bones": len(obj.data.bones)}


def _enable_rigify():
    import addon_utils

    mod = addon_utils.enable("rigify", default_set=True)
    if not mod:
        raise CommandError("The Rigify add-on could not be enabled")


@command("rigify_metarig", "rig")
def rigify_metarig(name="metarig", fit_to=None, height=None):
    """Add Rigify's human metarig, scaled to fit_to's height (or `height`). Adjust joints with
    edit_bones, then rigify_generate. Use this when the user wants animator controls (IK/FK);
    for a plain game skeleton rig_template is lighter."""
    _enable_rigify()
    before = set(bpy.data.objects)
    bpy.ops.object.armature_human_metarig_add()
    meta = next(o for o in bpy.data.objects if o not in before and o.type == "ARMATURE")
    meta.name = name
    meta_height = 1.9  # Rigify's default human is about 1.9 m tall
    target = None
    if fit_to:
        target, origin, _ = _fit(fit_to)
        meta.location = origin
    elif height:
        target = float(height)
    if target:
        s = target / meta_height
        meta.scale = (s, s, s)
        with ops_context(meta, mode="OBJECT"):
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    return {"name": meta.name, "bones": len(meta.data.bones)}


@command("rigify_generate", "rig")
def rigify_generate(metarig="metarig"):
    """Generate the Rigify control rig from a metarig. The result has DEF- deform bones (bind
    the mesh to it and export with deform bones only) and IK/FK controls for animation."""
    _enable_rigify()
    meta = get_object(metarig, "ARMATURE")
    before = {o.name for o in bpy.data.objects}
    from rigify import generate  # noqa: PLC0415

    # Rigify switches active objects as it works, so it can't run inside a pinned
    # active_object override: select the metarig for real and give it only a window.
    vl = bpy.context.view_layer
    for o in vl.objects:
        o.select_set(False)
    meta.select_set(True)
    vl.objects.active = meta
    wm = bpy.context.window_manager
    ctx = bpy.context.temp_override(window=wm.windows[0]) if wm and wm.windows else contextlib.nullcontext()
    with ctx:
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        generate.generate_rig(bpy.context, meta)
    new = [o for o in bpy.data.objects if o.name not in before and o.type == "ARMATURE"]
    rig = new[0] if new else meta.data.rigify_target_rig
    if rig is None:
        raise CommandError("Rigify did not produce a rig")
    return {"rig": rig.name, "bones": len(rig.data.bones), "deform_bones": sum(b.use_deform for b in rig.data.bones)}


@command("bind_mesh", "rig")
def bind_mesh(mesh, armature, method="AUTO"):
    """Parent a mesh to an armature with weights. AUTO = bone heat (best default; needs a clean,
    manifold mesh), ENVELOPE = by bone distance, NAME = empty groups to paint later."""
    obj = get_mesh(mesh)
    arm = get_object(armature, "ARMATURE")
    kinds = {"AUTO": "ARMATURE_AUTO", "ENVELOPE": "ARMATURE_ENVELOPE", "NAME": "ARMATURE_NAME"}
    if method not in kinds:
        raise CommandError(f"method must be one of {sorted(kinds)}")
    for vg in list(obj.vertex_groups):
        if arm.data.bones.get(vg.name):
            obj.vertex_groups.remove(vg)
    for mod in [m for m in obj.modifiers if m.type == "ARMATURE"]:
        obj.modifiers.remove(mod)
    with ops_context(arm, selected=[obj], mode="OBJECT"):
        bpy.ops.object.parent_set(type=kinds[method])
    report = rig_report(mesh, armature)
    if method == "AUTO" and report["unweighted_verts"] == report["verts"]:
        raise CommandError("Bone heat weighting failed (no vertex got weights). Run cleanup on the mesh, "
                           "make sure bones sit inside it, or use method=ENVELOPE")
    return report


@command("rig_report", "rig")
def rig_report(mesh, armature, max_influences=4):
    """Skinning health: unweighted verts, verts over the influence limit game engines use (4),
    deform bones with no weights, and whether weights sum to 1."""
    obj = get_mesh(mesh)
    arm = get_object(armature, "ARMATURE")
    deform = {b.name for b in arm.data.bones if b.use_deform}
    group_names = {g.index: g.name for g in obj.vertex_groups}
    used = set()
    unweighted = over = bad_sum = 0
    for v in obj.data.vertices:
        ws = [(group_names[g.group], g.weight) for g in v.groups if group_names.get(g.group) in deform and g.weight > 1e-5]
        if not ws:
            unweighted += 1
            continue
        used.update(n for n, _ in ws)
        if len(ws) > max_influences:
            over += 1
        if abs(sum(w for _, w in ws) - 1.0) > 0.01:
            bad_sum += 1
    mod = next((m for m in obj.modifiers if m.type == "ARMATURE"), None)
    return {
        "mesh": obj.name,
        "armature": arm.name,
        "bound": bool(mod and mod.object == arm),
        "parent": obj.parent.name if obj.parent else None,
        "verts": len(obj.data.vertices),
        "unweighted_verts": unweighted,
        "verts_over_influence_limit": over,
        "verts_not_normalized": bad_sum,
        "deform_bones": len(deform),
        "deform_bones_without_weights": sorted(deform - used),
    }


@command("limit_weights", "rig")
def limit_weights(mesh, limit=4, normalize=True):
    """Keep at most `limit` bone influences per vertex (game engines need 4 or fewer) and make them sum to 1."""
    obj = get_mesh(mesh)
    changed = 0
    groups = obj.vertex_groups
    for v in obj.data.vertices:
        ws = sorted(((g.group, g.weight) for g in v.groups), key=lambda x: -x[1])
        drop = ws[int(limit):]
        keep = ws[: int(limit)]
        for gi, _ in drop:
            groups[gi].remove([v.index])
        if normalize and keep:
            total = sum(w for _, w in keep)
            if total > 0 and abs(total - 1) > 1e-6:
                for gi, w in keep:
                    groups[gi].add([v.index], w / total, "REPLACE")
        if drop or (normalize and keep):
            changed += 1
    return {"mesh": obj.name, "verts_touched": changed}


def _apply_pose(arm, poses):
    missing = [b for b in poses if b not in arm.pose.bones]
    if missing:
        raise CommandError(f"No bones {missing} in {arm.name!r}")
    for bname, spec in poses.items():
        pb = arm.pose.bones[bname]
        if "rotation" in spec:
            rot = Euler([math.radians(a) for a in spec["rotation"]], "XYZ")
            if pb.rotation_mode == "QUATERNION":
                pb.rotation_quaternion = rot.to_quaternion()
            elif pb.rotation_mode == "AXIS_ANGLE":
                q = rot.to_quaternion()
                axis, angle = q.to_axis_angle()
                pb.rotation_axis_angle = (angle, *axis)
            else:
                pb.rotation_euler = rot
        if "location" in spec:
            pb.location = vec(spec["location"])
        if "scale" in spec:
            pb.scale = vec(spec["scale"], (1, 1, 1))


@command("pose_bones", "rig")
def pose_bones(armature, poses, reset_others=False):
    """Pose bones in local bone space: {bone: {rotation: [x,y,z] degrees, location, scale}}."""
    arm = get_object(armature, "ARMATURE")
    if reset_others:
        reset_pose(armature)
    _apply_pose(arm, poses)
    return {"armature": arm.name, "posed": sorted(poses)}


@command("reset_pose", "rig")
def reset_pose(armature):
    """Back to the rest pose."""
    arm = get_object(armature, "ARMATURE")
    for pb in arm.pose.bones:
        pb.location = (0, 0, 0)
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.rotation_euler = (0, 0, 0)
        pb.rotation_axis_angle = (0, 0, 1, 0)
        pb.scale = (1, 1, 1)
    return {"armature": arm.name}


# Stress poses for rig_template bones (local rotations in degrees). In that rig, +X on a limb
# raises an arm / swings a leg back, +X on the shin bends the knee, and +X on spine bones leans
# forward; Z swings arms forward (R) or back (L).
TEST_POSES = {
    "arms_down_knee_up": {
        "upper_arm.L": {"rotation": [-70, 0, 0]}, "upper_arm.R": {"rotation": [-70, 0, 0]},
        "forearm.L": {"rotation": [0, 0, -60]}, "forearm.R": {"rotation": [0, 0, 60]},
        "thigh.L": {"rotation": [-80, 0, 0]}, "shin.L": {"rotation": [100, 0, 0]},
    },
    "twist_and_reach": {
        "spine": {"rotation": [0, 30, 0]}, "chest": {"rotation": [20, 0, 0]},
        "upper_arm.R": {"rotation": [40, 0, 50]}, "head": {"rotation": [0, 0, 30]},
        "thigh.R": {"rotation": [40, 0, 0]}, "shin.R": {"rotation": [60, 0, 0]},
    },
}


@command("pose_test", "rig")
def pose_test(mesh, armature, poses=None, views=("front", "iso"), resolution=384):
    """Render the bound mesh in stress poses to spot bad weights (collapsing joints, stretching,
    parts left behind). poses: {pose_name: {bone: {rotation}}}; defaults suit rig_template names.
    The armature returns to rest afterwards."""
    from .look import capture_views  # noqa: PLC0415

    arm = get_object(armature, "ARMATURE")
    get_mesh(mesh)
    if not poses:
        # The defaults lower the arms from a T-pose; an A-pose rest is already part way down.
        lower = arm.get("claude_arm_angle", 0.0)
        poses = {}
        for k, v in TEST_POSES.items():
            poses[k] = {}
            for b, spec in v.items():
                if b not in arm.pose.bones:
                    continue
                rot = list(spec["rotation"])
                if b.startswith("upper_arm") and rot[0] < 0:
                    rot[0] = min(0.0, rot[0] + lower)
                poses[k][b] = {**spec, "rotation": rot}
    results = []
    try:
        for pose_name, spec in poses.items():
            reset_pose(armature)
            _apply_pose(arm, spec)
            bpy.context.view_layer.update()
            shots = capture_views(views=views, objects=[mesh], resolution=resolution)
            results.append({"pose": pose_name, "images": shots["images"]})
    finally:
        reset_pose(armature)
    return {"mesh": mesh, "poses": results}


# --- UniRig bridge -------------------------------------------------------------

_jobs = {}


def _bash():
    for cand in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _run_unirig(job, unirig_dir, python_dir, src, workdir, seed):
    env = dict(os.environ)
    if python_dir:
        env["PATH"] = python_dir + os.pathsep + env.get("PATH", "")
    bash = _bash()
    skel = os.path.join(workdir, "skeleton.fbx")
    skin = os.path.join(workdir, "skin.fbx")
    out = os.path.join(workdir, "rigged.glb")
    steps = [
        ("skeleton", [bash, "launch/inference/generate_skeleton.sh", "--input", src, "--output", skel, "--seed", str(seed)]),
        ("skin", [bash, "launch/inference/generate_skin.sh", "--input", skel, "--output", skin]),
        ("merge", [bash, "launch/inference/merge.sh", "--source", skin, "--target", src, "--output", out]),
    ]
    log = []
    try:
        for step, cmd in steps:
            job["step"] = step
            proc = subprocess.run(cmd, cwd=unirig_dir, env=env, capture_output=True, text=True)
            log.append(f"$ {' '.join(cmd[1:])}\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
            if proc.returncode != 0 or (step == "merge" and not os.path.exists(out)):
                job.update(state="failed", error=f"{step} step exited with {proc.returncode}")
                return
        job.update(state="done", output=out)
    except Exception as exc:  # noqa: BLE001
        job.update(state="failed", error=str(exc))
    finally:
        job["log"] = "\n".join(log)[-8000:]
        job["finished"] = time.time()


@command("auto_rig_unirig", "rig")
def auto_rig_unirig(mesh, unirig_dir, python_dir=None, seed=12345):
    """EXPERIMENTAL. Start UniRig (ML auto-rigging: skeleton + skin weights) on a mesh in the
    background. Needs a local UniRig install with its Python environment, an NVIDIA GPU with 8 GB+,
    and bash (Git Bash on Windows). python_dir: folder of the UniRig environment's python.
    Returns a job id; poll auto_rig_status, which imports the rigged result when it finishes."""
    obj = get_mesh(mesh)
    unirig_dir = os.path.expanduser(unirig_dir)
    if not os.path.isfile(os.path.join(unirig_dir, "launch", "inference", "generate_skeleton.sh")):
        raise CommandError(f"{unirig_dir} does not look like a UniRig checkout (launch/inference/ missing)")
    if _bash() is None:
        raise CommandError("bash not found. On Windows install Git for Windows (Git Bash)")
    workdir = tempfile.mkdtemp(prefix="claude_unirig_")
    src = os.path.join(workdir, "input.glb")
    with ops_context(obj, mode="OBJECT"):
        bpy.ops.export_scene.gltf(filepath=src, use_selection=True, export_animations=False, export_skins=False)
    job_id = uuid.uuid4().hex[:8]
    job = {"id": job_id, "state": "running", "step": "export", "mesh": obj.name, "started": time.time(), "workdir": workdir}
    _jobs[job_id] = job
    threading.Thread(target=_run_unirig, args=(job, unirig_dir, python_dir, src, workdir, int(seed)), daemon=True).start()
    return {"job_id": job_id, "workdir": workdir}


@command("auto_rig_status", "rig")
def auto_rig_status(job_id):
    """Check a UniRig job. When done, imports the rigged model (armature + skinned mesh) once."""
    job = _jobs.get(job_id)
    if job is None:
        raise CommandError(f"No job {job_id!r}")
    out = {k: job.get(k) for k in ("id", "state", "step", "error")}
    out["elapsed_seconds"] = round((job.get("finished") or time.time()) - job["started"])
    if job["state"] == "failed":
        out["log_tail"] = job.get("log", "")[-3000:]
    if job["state"] == "done" and not job.get("imported"):
        before = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=job["output"])
        job["imported"] = [o.name for o in bpy.data.objects if o not in before]
    if job.get("imported"):
        out["imported"] = job["imported"]
    return out
