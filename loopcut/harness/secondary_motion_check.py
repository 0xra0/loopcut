"""Secondary motion on export (motion_bake.py through export_for_engine), on SPLAP.

    python3 loopcut/windows/laptop.py check --script loopcut/harness/secondary_motion_check.py --wait

- A rig whose motion is partly not keys: Root keyed per action (Walk 30°, Run 60°); Follow turned by a Copy Rotation
  constraint; Driven moved by a driver; Spring moved by a frame handler that simulates a spring and restarts at the
  scene's first frame (as spring add-ons do), with the scene's range far from the clips. Exported for Godot (GLB) and
  Unity (FBX): the clips keep their names and number; in each, Follow, Driven and Spring move, Spring starts each clip
  at rest; the report names the baked bones; afterwards the user's actions, constraint, driver, handler, NLA, frame
  range and frame are as they were and no action is left over.
- Muse (C:\\Loopcut\\godot\\muse_tpose_final.blend, appended): its belly spring is keys already (Belly location per M_*
  action), so nothing is baked; exported for Godot, every M_* clip moves the Belly bone in the GLB and, imported
  headless in Godot, in its AnimationPlayer.
Writes secondary-motion-check.json to the working folder. Exits 0 when every check passes.
"""

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
from loopcut import engine_export, glb  # noqa: E402

TIMEOUT = 900
RESULTS = []
MUSE = Path(r"C:\Loopcut\godot\muse_tpose_final.blend")
GODOT = Path(r"C:\Loopcut\godot\Godot_v4.7.2-stable_win64_console.exe")
SPRING = {"x": 0.0, "v": 0.0}
GD_SCRIPT = """extends SceneTree

func _init():
\tvar scene = load("res://muse.glb")
\tvar out = {"loaded": scene != null, "clips": {}}
\tif scene:
\t\tvar root = scene.instantiate()
\t\tvar stack = [root]
\t\twhile stack.size() > 0:
\t\t\tvar node = stack.pop_back()
\t\t\tfor child in node.get_children():
\t\t\t\tstack.append(child)
\t\t\tif node is AnimationPlayer:
\t\t\t\tfor clip in node.get_animation_list():
\t\t\t\t\tvar anim = node.get_animation(clip)
\t\t\t\t\tvar spread = -1.0
\t\t\t\t\tfor t in anim.get_track_count():
\t\t\t\t\t\tif String(anim.track_get_path(t)).ends_with(":Belly") and anim.track_get_type(t) == Animation.TYPE_POSITION_3D:
\t\t\t\t\t\t\tvar lo = Vector3(INF, INF, INF)
\t\t\t\t\t\t\tvar hi = Vector3(-INF, -INF, -INF)
\t\t\t\t\t\t\tfor k in anim.track_get_key_count(t):
\t\t\t\t\t\t\t\tvar v = anim.track_get_key_value(t, k)
\t\t\t\t\t\t\t\tlo = Vector3(min(lo.x, v.x), min(lo.y, v.y), min(lo.z, v.z))
\t\t\t\t\t\t\t\thi = Vector3(max(hi.x, v.x), max(hi.y, v.y), max(hi.z, v.z))
\t\t\t\t\t\t\tspread = max(hi.x - lo.x, max(hi.y - lo.y, hi.z - lo.z))
\t\t\t\t\tout["clips"][String(clip)] = spread
\t\troot.free()
\tprint("LOOPCUT " + JSON.stringify(out))
\tquit()
"""


def check(name: str, ok, detail="") -> None:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:1200]})
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  [{str(detail)[:1200]}]" if detail else ""), flush=True)


def spring(scene, *_):
    """A frame handler simulating a spring on Spring's location, restarting at the scene's first frame."""
    rig = scene.objects.get("Rig")
    if rig is None or "Spring" not in rig.pose.bones:
        return
    if scene.frame_current <= scene.frame_start:
        SPRING["x"] = SPRING["v"] = 0.0
    else:
        target = 0.4 * math.sin(rig.pose.bones["Root"].rotation_euler.z)
        SPRING["v"] += (target - SPRING["x"]) * 0.3 - 0.05 * SPRING["v"]
        SPRING["x"] += SPRING["v"]
    rig.pose.bones["Spring"].location.x = SPRING["x"]


def build():
    import bmesh
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    for action in list(bpy.data.actions):
        bpy.data.actions.remove(action)
    arm = bpy.data.armatures.new("Rig")
    rig = bpy.data.objects.new("Rig", arm)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.update()
    bpy.context.view_layer.objects.active = rig
    with bpy.context.temp_override(active_object=rig, object=rig):
        bpy.ops.object.mode_set(mode="EDIT")
        for i, name in enumerate(("Root", "Follow", "Driven", "Spring")):
            bone = arm.edit_bones.new(name)
            bone.head, bone.tail = (i * 0.5, 0, 0), (i * 0.5, 0, 0.5)
            if name in ("Driven", "Spring"):
                bone.parent = arm.edit_bones["Root"]
        bpy.ops.object.mode_set(mode="OBJECT")
    mesh = bpy.data.meshes.new("Body")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(mesh)
    bm.free()
    mesh.uv_layers.new()
    body = bpy.data.objects.new("Body", mesh)
    bpy.context.scene.collection.objects.link(body)
    body.parent = rig
    body.modifiers.new("Armature", "ARMATURE").object = rig
    for bone in arm.bones:
        body.vertex_groups.new(name=bone.name).add(list(range(len(mesh.vertices))), 0.25, "REPLACE")
    bones = rig.pose.bones
    constraint = bones["Follow"].constraints.new("COPY_ROTATION")
    constraint.target, constraint.subtarget = rig, "Root"
    driver = bones["Driven"].driver_add("location", 2).driver
    driver.type, driver.expression = "SCRIPTED", "sin(frame/4)*0.3"
    rig.animation_data_create()
    bones["Root"].rotation_mode = "XYZ"
    for name, amplitude in (("Walk", 30), ("Run", 60)):
        action = bpy.data.actions.new(name)
        rig.animation_data.action = action
        for frame in (1, 8, 16, 24):
            bones["Root"].rotation_euler = (0, 0, math.radians(amplitude * math.sin(frame / 24 * math.pi)))
            bones["Root"].keyframe_insert("rotation_euler", frame=frame)
        action.use_fake_user = True
    rig.animation_data.action = bpy.data.actions["Walk"]
    bpy.app.handlers.frame_change_post.append(spring)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 200, 300  # Far from the clips: only a bake that restarts each clip resets it.
    scene.frame_set(250)
    return rig


def channels(bag):
    return sorted((fc.data_path, fc.array_index, len(fc.keyframe_points)) for fc in bag.fcurves)


def state(rig):
    """What the export must leave as it was."""
    from loopcut.motion_bake import channelbag
    scene = bpy.context.scene
    return {"actions": sorted((a.name, a.users, tuple(channels(channelbag(a, a.slots[0]))))
                              for a in bpy.data.actions),
            "playing": rig.animation_data.action.name if rig.animation_data.action else None,
            "tracks": len(rig.animation_data.nla_tracks), "blend": rig.animation_data.action_blend_type,
            "constraint": rig.pose.bones["Follow"].constraints[0].enabled,
            "driver_muted": [fc.mute for fc in rig.animation_data.drivers],
            "handlers": [getattr(fn, "__name__", "") for fn in bpy.app.handlers.frame_change_post],
            "range": (scene.frame_start, scene.frame_end, scene.frame_current)}


def glb_clips(path: Path) -> tuple[dict, dict]:
    """{clip: {node.path: (spread, first value)}} and {node: rest translation}."""
    document, binary = glb.parse(path.read_bytes())
    nodes = [n.get("name") for n in document["nodes"]]
    rest = {n.get("name"): n.get("translation", [0, 0, 0]) for n in document["nodes"]}
    clips = {}
    for animation in document.get("animations", []):
        moving = {}
        for channel in animation["channels"]:
            sampler = animation["samplers"][channel["sampler"]]
            accessor = document["accessors"][sampler["output"]]
            view = document["bufferViews"][accessor["bufferView"]]
            width = {"VEC3": 3, "VEC4": 4, "SCALAR": 1}[accessor["type"]]
            offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
            values = struct.unpack_from("<%df" % (accessor["count"] * width), binary, offset)
            rows = [values[i:i + width] for i in range(0, len(values), width)]
            spread = max(max(r[k] for r in rows) - min(r[k] for r in rows) for k in range(width))
            moving[f"{nodes[channel['target']['node']]}.{channel['target']['path']}"] = (round(spread, 4), rows[0])
        clips[animation["name"]] = moving
    return clips, rest


def fbx_takes(path: Path) -> dict:
    """{take: {bone: basis spread}} of the FBX imported back."""
    before, actions = set(bpy.data.objects), set(bpy.data.actions)
    bpy.ops.import_scene.fbx(filepath=str(path))
    made = [ob for ob in bpy.data.objects if ob not in before]
    rig = next(ob for ob in made if ob.type == "ARMATURE")
    found = {}
    try:
        for action in [a for a in bpy.data.actions if a not in actions]:
            rig.animation_data.action = action
            spread = {}
            for name in ("Root", "Follow", "Driven", "Spring"):
                points = []
                for frame in range(1, 25):
                    bpy.context.scene.frame_set(frame)
                    m = rig.pose.bones[name].matrix_basis
                    points.append(tuple(m.translation) + tuple(m.to_quaternion()))
                spread[name] = round(max(max(p[k] for p in points) - min(p[k] for p in points) for k in range(7)), 4)
            found[action.name] = spread
    finally:
        for ob in made:
            bpy.data.objects.remove(ob)
        for action in [a for a in bpy.data.actions if a not in actions]:
            bpy.data.actions.remove(action)
    return found


def godot_belly(glb_path: Path, folder: Path) -> dict:
    project = folder / "godot"
    project.mkdir()
    (project / "project.godot").write_text('config_version=5\n\n[application]\n\nconfig/name="loopcut_motion"\n')
    shutil.copy(glb_path, project / "muse.glb")
    (project / "check.gd").write_text(GD_SCRIPT)
    first = subprocess.run([str(GODOT), "--headless", "--path", str(project), "--import"], capture_output=True,
                           text=True, timeout=300)
    errors = [line for line in ((first.stdout or "") + (first.stderr or "")).splitlines() if "ERROR" in line]
    second = subprocess.run([str(GODOT), "--headless", "--path", str(project), "--script", "res://check.gd"],
                            capture_output=True, text=True, timeout=180)
    line = next((l for l in (second.stdout or "").splitlines() if l.startswith("LOOPCUT ")), "")
    return {"errors": errors[:5], **(json.loads(line[len("LOOPCUT "):]) if line else {})}


def main() -> int:
    folder = Path(tempfile.mkdtemp(prefix="lc_motion_check_")).resolve()
    t0 = time.time()
    try:
        rig = build()
        before = state(rig)
        print("BEFORE", before, flush=True)

        # Godot: GLB.
        result = engine_export.export_for_engine("godot", ["Body", "Rig"], str(folder / "rig.glb"))
        print("GODOT RESULT", result.text, flush=True)
        check("the rig exports for Godot", result.ok, result.text)
        check("the report names the bones it baked and what moved them",
              "Secondary motion baked into keys" in result.text and all(
                  words in result.text for words in ("Follow (copy rotation constraint)", "Driven (driver)",
                                                     "Spring (frame handler)")), result.text)
        check("after the Godot export, the user's actions, constraint, driver, handler, NLA and frames are as they were",
              state(rig) == before, {"before": before, "after": state(rig)})
        clips, rest = glb_clips(folder / "rig.glb")
        summary = {clip: {k: v[0] for k, v in moving.items() if not k.endswith("scale")} for clip, moving in clips.items()}
        print("GLB CLIPS", summary, flush=True)
        check("GLB: the two clips, by their names, nothing else", sorted(clips) == ["Run", "Walk"], sorted(clips))
        moves = all(clips.get(c, {}).get("Follow.rotation", (0,))[0] > 0.1 and clips[c].get("Driven.translation", (0,))[0] > 0.1
                    and clips[c].get("Spring.translation", (0,))[0] > 0.01 for c in ("Walk", "Run"))
        check("GLB: constraint, driver and handler motion in every clip", moves, summary)
        starts = {c: clips.get(c, {}).get("Spring.translation", (0, [9, 9, 9]))[1] for c in ("Walk", "Run")}
        check("GLB: the simulated spring starts each clip at rest",
              all(max(abs(a - b) for a, b in zip(starts[c], rest["Spring"])) < 1e-3 for c in starts),
              {"first": starts, "rest": rest.get("Spring")})
        check("GLB: per-clip motion kept apart (Run turns Root further than Walk)",
              clips["Run"]["Root.rotation"][0] > clips["Walk"]["Root.rotation"][0] * 1.4,
              (clips["Walk"].get("Root.rotation"), clips["Run"].get("Root.rotation")))

        # Unity: FBX.
        result = engine_export.export_for_engine("unity", ["Body", "Rig"], str(folder / "rig.fbx"))
        print("UNITY RESULT", result.text, flush=True)
        check("the rig exports for Unity, user state as it was", result.ok and state(rig) == before,
              {"text": result.text, "after": state(rig)})
        bpy.app.handlers.frame_change_post.remove(spring)
        try:
            takes = fbx_takes(folder / "rig.fbx")
        finally:
            bpy.app.handlers.frame_change_post.append(spring)
        print("FBX TAKES", takes, flush=True)
        names = sorted(name.split("|", 1)[1] if "|" in name else name for name in takes)
        check("FBX: takes named Rig|Walk and Rig|Run as before, nothing else", names == ["Rig|Run", "Rig|Walk"], takes)
        check("FBX: constraint, driver and handler motion in every take",
              takes and all(v["Follow"] > 0.1 and v["Driven"] > 0.1 and v["Spring"] > 0.01 for v in takes.values()),
              takes)
        bpy.app.handlers.frame_change_post.remove(spring)

        # Muse: the belly spring is keys already.
        if not MUSE.is_file():
            check("Muse is on this machine", False, str(MUSE))
            return 1
        for ob in list(bpy.data.objects):
            bpy.data.objects.remove(ob)
        for action in list(bpy.data.actions):
            bpy.data.actions.remove(action)
        with bpy.data.libraries.load(str(MUSE), link=False) as (source, target):
            target.objects = [n for n in source.objects if n in ("Muse", "Muse_Rig")]
            target.actions = [n for n in source.actions if n.startswith("M_")]
        for ob in target.objects:
            bpy.context.scene.collection.objects.link(ob)
        bpy.context.scene.frame_start, bpy.context.scene.frame_end = 1, 150
        t1 = time.time()
        result = engine_export.export_for_engine("godot", ["Muse", "Muse_Rig"], str(folder / "muse.glb"))
        print("MUSE RESULT", round(time.time() - t1, 1), "s", result.text[:3000], flush=True)
        check("Muse exports for Godot; nothing to bake (its belly is keyed)",
              result.ok and "Secondary motion baked" not in result.text, result.text[:1500])
        clips, _ = glb_clips(folder / "muse.glb")
        belly = {clip: moving.get("Belly.translation", (0,))[0] for clip, moving in clips.items()}
        print("MUSE BELLY GLB", belly, flush=True)
        wanted = sorted(a.name for a in bpy.data.actions if a.name.startswith("M_"))
        check("Muse GLB: every M_ clip, and the belly moves in each (over 1 mm)",
              sorted(belly) == wanted and all(v > 0.001 for v in belly.values()), {"clips": belly, "actions": wanted})
        if GODOT.is_file():
            found = godot_belly(folder / "muse.glb", folder)
            print("GODOT MUSE", found, flush=True)
            spreads = found.get("clips") or {}
            check("Muse in Godot (headless import): every clip's Belly position track moves",
                  found.get("loaded") and not found.get("errors") and len(spreads) == len(wanted)
                  and all(v > 0.001 for v in spreads.values()), found)
        else:
            print(f"skip Godot ({GODOT} not on this machine)", flush=True)
    except Exception:
        traceback.print_exc()
        check("ran to the end", False, "exception above")
    finally:
        if spring in bpy.app.handlers.frame_change_post:
            bpy.app.handlers.frame_change_post.remove(spring)
        shutil.rmtree(folder, ignore_errors=True)
    passed = sum(r["ok"] for r in RESULTS)
    summary = {"passed": passed, "total": len(RESULTS), "seconds": round(time.time() - t0, 1), "results": RESULTS}
    try:
        (Path.cwd() / "secondary-motion-check.json").write_text(json.dumps(summary, indent=1))
    except OSError:
        pass
    print(f"MOTION {passed}/{len(RESULTS)} in {summary['seconds']}s")
    ok = passed == len(RESULTS) and RESULTS
    print("MOTION OK" if ok else "MOTION FAIL", flush=True)
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
    watchdog = threading.Timer(TIMEOUT, lambda: (print(f"MOTION FAIL: timed out after {TIMEOUT}s", flush=True),
                                                  os._exit(3)))
    watchdog.daemon = True
    watchdog.start()
    if bpy.app.background:
        run_and_exit()
    else:
        bpy.app.timers.register(run_and_exit, first_interval=0.5)
