"""animate toolset: keyframes, actions, NLA and procedural cycles."""

import math

import bpy
from mathutils import Euler

from . import CommandError, command
from .util import get_object, vec

INTERPOLATIONS = ("BEZIER", "LINEAR", "CONSTANT")


def fcurves(action):
    """F-curves of an action on Blender 4.2 (flat) and 4.4+ (layered/slotted) alike."""
    layers = getattr(action, "layers", None)
    if layers:
        out = []
        for layer in layers:
            for strip in layer.strips:
                for bag in getattr(strip, "channelbags", []):
                    out.extend(bag.fcurves)
        return out
    return list(getattr(action, "fcurves", []))


def _ensure_action(obj, action_name):
    ad = obj.animation_data or obj.animation_data_create()
    if action_name:
        act = bpy.data.actions.get(action_name) or bpy.data.actions.new(action_name)
        if ad.action is not act:
            ad.action = act
    return ad


def _key_transform(target, spec, frame, group=None):
    """Set and key location/rotation/scale on an object or pose bone."""
    kw = {"frame": frame}
    if group:
        kw["group"] = group
    if "location" in spec:
        target.location = vec(spec["location"])
        target.keyframe_insert("location", **kw)
    if "rotation" in spec:
        rot = Euler([math.radians(a) for a in spec["rotation"]], "XYZ")
        if target.rotation_mode == "QUATERNION":
            target.rotation_quaternion = rot.to_quaternion()
            target.keyframe_insert("rotation_quaternion", **kw)
        else:
            if target.rotation_mode == "AXIS_ANGLE":
                target.rotation_mode = "XYZ"
            target.rotation_euler = rot
            target.keyframe_insert("rotation_euler", **kw)
    if "scale" in spec:
        target.scale = vec(spec["scale"], (1, 1, 1))
        target.keyframe_insert("scale", **kw)


@command("set_frame_range", "animate")
def set_frame_range(start=1, end=250, fps=None, current=None):
    """Scene frame range and fps."""
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = int(start), int(end)
    if fps:
        scene.render.fps = int(fps)
    if current is not None:
        scene.frame_set(int(current))
    return {"start": scene.frame_start, "end": scene.frame_end, "fps": scene.render.fps}


@command("keyframe", "animate")
def keyframe(name, keys, action=None, interpolation="BEZIER"):
    """Insert keyframes on an object and/or its pose bones.
    keys: [{frame, location?, rotation? (degrees), scale?, bones?: {bone: {location?, rotation?, scale?}}}]
    action: name of the action to key into (created if missing). Bone values are in local bone space."""
    if interpolation not in INTERPOLATIONS:
        raise CommandError(f"interpolation must be one of {INTERPOLATIONS}")
    obj = get_object(name)
    ad = _ensure_action(obj, action)
    for spec in keys:
        if "frame" not in spec:
            raise CommandError("Every key needs a frame")
        frame = float(spec["frame"])
        _key_transform(obj, spec, frame)
        for bname, bspec in (spec.get("bones") or {}).items():
            if obj.type != "ARMATURE" or bname not in obj.pose.bones:
                raise CommandError(f"No pose bone {bname!r} on {obj.name!r}")
            _key_transform(obj.pose.bones[bname], bspec, frame, group=bname)
    act = ad.action
    for fc in fcurves(act):
        for kp in fc.keyframe_points:
            kp.interpolation = interpolation
    return {"object": obj.name, "action": act.name, "frame_range": list(act.frame_range), "fcurves": len(fcurves(act))}


@command("list_actions", "animate")
def list_actions(name=None):
    """All actions (or those on one object, including NLA strips) with frame ranges."""
    if name:
        obj = get_object(name)
        ad = obj.animation_data
        acts = []
        if ad:
            if ad.action:
                acts.append(ad.action)
            acts += [s.action for t in ad.nla_tracks for s in t.strips if s.action]
    else:
        acts = list(bpy.data.actions)
    return [{"name": a.name, "frame_range": list(a.frame_range), "fcurves": len(fcurves(a)), "users": a.users,
             "fake_user": a.use_fake_user} for a in acts]


@command("set_action", "animate")
def set_action(name, action):
    """Make an existing action the active one on an object (action=None to clear)."""
    obj = get_object(name)
    ad = obj.animation_data or obj.animation_data_create()
    if action is None:
        ad.action = None
        return {"object": obj.name, "action": None}
    act = bpy.data.actions.get(action)
    if act is None:
        raise CommandError(f"No action {action!r}")
    ad.action = act
    return {"object": obj.name, "action": act.name}


@command("push_to_nla", "animate")
def push_to_nla(name, track_name=None, start_frame=None):
    """Move the active action into an NLA track (keeps clips separate for export as named animations)."""
    obj = get_object(name)
    ad = obj.animation_data
    if not ad or not ad.action:
        raise CommandError(f"{obj.name!r} has no active action")
    act = ad.action
    act.use_fake_user = True  # keep it even if unassigned later
    track = ad.nla_tracks.new()
    track.name = track_name or act.name
    start = int(start_frame if start_frame is not None else act.frame_range[0])
    track.strips.new(act.name, start, act)
    ad.action = None
    return {"object": obj.name, "track": track.name, "action": act.name}


# Roles used by procedural_cycle and their defaults on rig_template.
ROLES = ("hips", "spine", "chest", "neck", "head", "upper_arm.L", "upper_arm.R", "forearm.L", "forearm.R",
         "thigh.L", "thigh.R", "shin.L", "shin.R", "foot.L", "foot.R")


def _cycle_pose(kind, p, amp):
    """Rotations (deg, local XYZ) per role and hips bob at phase p in [0, 2pi)."""
    s, c = math.sin(p), math.cos(p)
    if kind == "idle":
        return {
            "chest": [2.0 * s * amp, 0, 0],
            "neck": [-1.0 * s * amp, 0, 0],
            "head": [0, 3.0 * math.sin(p * 0.5) * amp, 1.5 * s * amp],
            "upper_arm.L": [-68 + 2 * s * amp, 0, 0],
            "upper_arm.R": [-68 + 2 * s * amp, 0, 0],
            "forearm.L": [0, 0, -10], "forearm.R": [0, 0, 10],
        }, 0.004 * amp * s
    run = kind == "run"
    leg = (40 if run else 25) * amp
    knee = (95 if run else 45) * amp
    arm = (35 if run else 18) * amp
    lean = 12 if run else 3
    swing_l = (0.5 + 0.5 * c) ** 2  # left knee bends while the left leg swings forward
    swing_r = (0.5 - 0.5 * c) ** 2
    pose = {
        "hips": [0, 6 * s * amp, 0],
        "spine": [lean, -4 * s * amp, 0],
        "chest": [0, -5 * s * amp, 0],
        "head": [-lean, 4 * s * amp, 0],
        "thigh.L": [-leg * s, 0, 0], "thigh.R": [leg * s, 0, 0],
        "shin.L": [5 + knee * swing_l, 0, 0], "shin.R": [5 + knee * swing_r, 0, 0],
        "foot.L": [-10 * s * amp, 0, 0], "foot.R": [10 * s * amp, 0, 0],
        "upper_arm.L": [-70, 0, arm * s], "upper_arm.R": [-70, 0, arm * s],
        "forearm.L": [0, 0, -(30 if run else 15)], "forearm.R": [0, 0, 30 if run else 15],
    }
    bob = (0.025 if run else 0.012) * amp * math.cos(2 * p)
    return pose, bob


@command("procedural_cycle", "animate")
def procedural_cycle(armature, kind="walk", frames=None, action=None, bone_map=None, amplitude=1.0,
                     keys_per_cycle=8, start_frame=1):
    """Generate a looping walk, run or idle on a humanoid rig. Tuned for rig_template bones and
    axes; for other rigs pass bone_map {role: bone} (roles: hips, spine, chest, neck, head,
    upper_arm.L/R, forearm.L/R, thigh.L/R, shin.L/R, foot.L/R) and check the motion with
    capture_views at a few frames. Bones with no mapping are skipped."""
    if kind not in ("walk", "run", "idle"):
        raise CommandError("kind must be walk, run or idle")
    arm = get_object(armature, "ARMATURE")
    frames = int(frames or {"walk": 32, "run": 20, "idle": 60}[kind])
    bone_map = {**{r: r for r in ROLES}, **(bone_map or {})}
    mapped = {role: bone for role, bone in bone_map.items() if bone in arm.pose.bones}
    if not mapped:
        raise CommandError("None of the cycle roles match bones on this armature; pass bone_map")
    action = action or f"{arm.name}_{kind}"
    ad = _ensure_action(arm, action)
    for bone in mapped.values():
        pb = arm.pose.bones[bone]
        if pb.rotation_mode != "XYZ":
            pb.rotation_mode = "XYZ"
    rest_arm_angle = float(arm.get("claude_arm_angle", 0.0))
    hips_bone = mapped.get("hips")
    steps = max(4, int(keys_per_cycle))
    for i in range(steps + 1):
        p = 2 * math.pi * i / steps
        frame = start_frame + frames * i / steps
        pose, bob = _cycle_pose(kind, p, float(amplitude))
        for role, rot in pose.items():
            bone = mapped.get(role)
            if not bone:
                continue
            if role.startswith("upper_arm"):
                # Poses assume a T-pose rest; rig_template's arm_angle already lowers the arms.
                rot = [rot[0] + rest_arm_angle, rot[1], rot[2]]
            pb = arm.pose.bones[bone]
            pb.rotation_euler = [math.radians(a) for a in rot]
            pb.keyframe_insert("rotation_euler", frame=frame, group=bone)
        if hips_bone:
            pb = arm.pose.bones[hips_bone]
            pb.location = (0, bob, 0)  # hips bone points up, so local Y is world up
            pb.keyframe_insert("location", frame=frame, group=hips_bone)
    act = ad.action
    for fc in fcurves(act):
        if not any(m.type == "CYCLES" for m in fc.modifiers):
            fc.modifiers.new("CYCLES")
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = int(start_frame), int(start_frame + frames - 1)
    return {"armature": arm.name, "action": act.name, "frames": frames, "bones": sorted(mapped.values())}


@command("set_frame", "animate")
def set_frame(frame):
    """Jump to a frame (pose updates), e.g. before capture_views to check an animation."""
    bpy.context.scene.frame_set(int(frame))
    return {"frame": bpy.context.scene.frame_current}
