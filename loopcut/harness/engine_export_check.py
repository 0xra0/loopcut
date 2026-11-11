"""export_for_engine (engine_export.py) against Blender's real exporters, and the Godot file in real Godot.

    Blender --factory-startup --python-exit-code 1 --python loopcut/harness/engine_export_check.py [-- --keep]
    python3 loopcut/windows/laptop.py check --script loopcut/harness/engine_export_check.py --wait   (SPLAP)

The scene: a crate (subdivided cube, UVs) whose material reads a 4608 x 64 colour image and a 64 x 64
normal map (green 0.25), both generated (never saved: an FBX could not embed them as they are); a rig
(two bones) skinning a body, with two actions, Walk (active) and Run (kept by a fake user only); a ball
with flat custom normals and no map (the safe shading fix); a dense grid (over Roblox's 20,000 triangles
per mesh). Checked per engine, reading the written file back:
- Godot (.glb): nodes, both clips by name, tangents on every UV-mapped primitive, the budget line, the
  ball fixed and said so; the user's images, names and selection as they were, no copies left.
  With Godot 4.7.2 at C:\\Loopcut\\godot, the file is imported headless into a fresh project
  (--import, no ERROR lines) and a script lists its clips and tangent surfaces.
- Unity (.fbx, lods=2): Crate_LOD0..2 with falling triangle counts, tangents, embedded textures; the
  crate's name back after; the rig's clips Rig|Walk and Rig|Run.
- Unreal (.fbx): the embedded normal map's green flipped (0.75), the colour image left at full size.
- Roblox (.fbx and .gltf): the colour image capped at 4096 x 57, the grid named over the per-mesh cap.
- An existing file is refused without overwrite.
Writes engine-export-check.json to the working folder (a SPLAP job's folder). Exits 0 when all pass.
"""

import json
import os
import shutil
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
from loopcut.tools import ToolError  # noqa: E402

TIMEOUT = 540
GODOT = Path(r"C:\Loopcut\godot\Godot_v4.7.2-stable_win64_console.exe")
RESULTS = []
GD_SCRIPT = """extends SceneTree

func _init():
\tvar scene = load("res://pack.glb")
\tvar out = {"loaded": scene != null, "animations": [], "surfaces": 0, "tangents": 0, "meshes": []}
\tif scene:
\t\tvar root = scene.instantiate()
\t\tvar stack = [root]
\t\twhile stack.size() > 0:
\t\t\tvar node = stack.pop_back()
\t\t\tfor child in node.get_children():
\t\t\t\tstack.append(child)
\t\t\tif node is AnimationPlayer:
\t\t\t\tfor clip in node.get_animation_list():
\t\t\t\t\tout["animations"].append(String(clip))
\t\t\tif node is MeshInstance3D and node.mesh is ArrayMesh:
\t\t\t\tout["meshes"].append(String(node.name))
\t\t\t\tfor i in node.mesh.get_surface_count():
\t\t\t\t\tout["surfaces"] += 1
\t\t\t\t\tif node.mesh.surface_get_format(i) & Mesh.ARRAY_FORMAT_TANGENT:
\t\t\t\t\t\tout["tangents"] += 1
\t\troot.free()
\tprint("LOOPCUT " + JSON.stringify(out))
\tquit()
"""


def check(name: str, ok, detail="") -> None:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:800]})
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  [{str(detail)[:800]}]" if detail and not ok else ""), flush=True)


def new_object(name: str, build, collection=None):
    import bmesh
    bm = bmesh.new()
    bm.loops.layers.uv.new("UVMap")
    build(bm)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    ob = bpy.data.objects.new(name, mesh)
    (collection or bpy.context.scene.collection).objects.link(ob)
    return ob


def image(name: str, width: int, height: int, rgba, non_color=False):
    made = bpy.data.images.new(name, width, height, alpha=False)
    if non_color:
        made.colorspace_settings.name = "Non-Color"
    made.pixels.foreach_set(list(rgba) * width * height)
    return made


def build() -> dict:
    import bmesh
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    scene = bpy.context.scene

    def crate_mesh(bm):
        bmesh.ops.create_cube(bm, size=1.0, calc_uvs=True)
        bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1, use_grid_fill=True)
    crate = new_object("Crate", crate_mesh)
    albedo = image("crate_albedo", 4608, 64, (0.6, 0.4, 0.2, 1.0))
    normal_image = image("crate_normal", 64, 64, (0.5, 0.25, 1.0, 1.0), non_color=True)
    wood = bpy.data.materials.new("Wood")
    wood.use_nodes = True
    nodes, links = wood.node_tree.nodes, wood.node_tree.links
    color = nodes.new("ShaderNodeTexImage")
    color.image = albedo
    links.new(color.outputs["Color"], nodes["Principled BSDF"].inputs["Base Color"])
    bump = nodes.new("ShaderNodeTexImage")
    bump.image = normal_image
    normal = nodes.new("ShaderNodeNormalMap")
    links.new(bump.outputs["Color"], normal.inputs["Color"])
    links.new(normal.outputs["Normal"], nodes["Principled BSDF"].inputs["Normal"])
    crate.data.materials.append(wood)

    # A rig skinning a body, with two actions: Walk active, Run kept by its fake user only.
    armature = bpy.data.armatures.new("Rig")
    rig = bpy.data.objects.new("Rig", armature)
    scene.collection.objects.link(rig)
    rig.location = (3, 0, 0)
    bpy.context.view_layer.update()
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    root = armature.edit_bones.new("Root")
    root.head, root.tail = (0, 0, 0), (0, 0, 1)
    tip = armature.edit_bones.new("Tip")
    tip.head, tip.tail, tip.parent = (0, 0, 1), (0, 0, 2), root
    bpy.ops.object.mode_set(mode="OBJECT")

    def body_mesh(bm):
        bmesh.ops.create_cone(bm, segments=16, depth=2.0, radius1=0.3, radius2=0.3, cap_ends=True, calc_uvs=True)
        bmesh.ops.translate(bm, verts=bm.verts[:], vec=(0, 0, 1))
    body = new_object("Body", body_mesh)
    body.parent = rig
    low = [v.index for v in body.data.vertices if v.co.z < 1]
    body.vertex_groups.new(name="Root").add(low, 1.0, "REPLACE")
    body.vertex_groups.new(name="Tip").add([v.index for v in body.data.vertices if v.index not in low], 1.0, "REPLACE")
    body.modifiers.new("Armature", "ARMATURE").object = rig
    rig.animation_data_create()
    bone = rig.pose.bones["Tip"]
    bone.rotation_mode = "XYZ"

    def action(name, angle):
        rig.animation_data.action = None
        bone.rotation_euler = (0, 0, 0)
        bone.keyframe_insert("rotation_euler", frame=1)
        bone.rotation_euler = (angle, 0, 0)
        bone.keyframe_insert("rotation_euler", frame=20)
        made = rig.animation_data.action
        made.name = name
        made.use_fake_user = True
        return made
    walk = action("Walk", 0.5)
    action("Run", 1.0)
    rig.animation_data.action = walk
    bone.rotation_euler = (0, 0, 0)

    ball = new_object("Ball", lambda bm: bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=24, radius=0.5,
                                                                   calc_uvs=True))
    ball.location = (-2, 0, 0)
    flat = []
    for polygon in ball.data.polygons:
        flat += [tuple(polygon.normal)] * polygon.loop_total
    ball.data.normals_split_custom_set(flat)
    dense = new_object("Dense", lambda bm: bmesh.ops.create_grid(bm, x_segments=150, y_segments=150, size=2.0,
                                                                 calc_uvs=True))
    dense.location = (0, 4, 0)
    bpy.context.view_layer.update()
    for ob in scene.objects:
        ob.select_set(ob.name == "Dense")
    bpy.context.view_layer.objects.active = dense
    return {"albedo": albedo, "normal": normal_image, "color_node": color, "bump_node": bump}


def fbx_tree(path: Path):
    from io_scene_fbx import parse_fbx
    root, _ = parse_fbx.parse(str(path))
    return root


def embedded(root, name_part: str) -> bytes:
    objects = next(e for e in root.elems if e.id == b"Objects")
    for elem in objects.elems:
        if elem.id == b"Video" and name_part.encode() in bytes(elem.props[1]):
            content = next((c for c in elem.elems if c.id == b"Content"), None)
            if content is not None and content.props and len(content.props[0]):
                return bytes(content.props[0])
    return b""


def loaded(data: bytes, folder: Path, name: str):
    """(size, first pixel) of embedded image bytes, through Blender's own loader."""
    if not data:
        return (0, 0), (0, 0, 0, 0)
    path = folder / f"{name}.png"
    path.write_bytes(data)
    made = bpy.data.images.load(str(path))
    try:
        size = tuple(made.size)
        pixel = tuple(round(v, 2) for v in made.pixels[:4])
    finally:
        bpy.data.images.remove(made)
    return size, pixel


def as_before(fixture: dict) -> list[str]:
    """What is not as the user had it: images, nodes, names, leftover copies and LOD objects."""
    problems = []
    if tuple(fixture["albedo"].size) != (4608, 64):
        problems.append(f"crate_albedo is {tuple(fixture['albedo'].size)}")
    if fixture["color_node"].image != fixture["albedo"] or fixture["bump_node"].image != fixture["normal"]:
        problems.append("a texture node still points at a copy")
    if round(fixture["normal"].pixels[1], 2) != 0.25:
        problems.append(f"crate_normal's green is {fixture['normal'].pixels[1]:.2f}")
    copies = [i.name for i in bpy.data.images if any(i.name.endswith(f".{e}") for e in engine_export.ENGINES)]
    lods = [o.name for o in bpy.data.objects if "_LOD" in o.name]
    if copies or lods:
        problems.append(f"left behind: {copies + lods}")
    if "Crate" not in bpy.data.objects:
        problems.append("Crate is not called Crate any more")
    selected = sorted(o.name for o in bpy.context.scene.objects if o.select_get())
    if selected != ["Dense"]:
        problems.append(f"selection is {selected}")
    return problems


def godot_import(glb_path: Path, folder: Path) -> None:
    if not GODOT.is_file():
        print(f"skip Godot import ({GODOT} not on this machine)", flush=True)
        return
    project = folder / "godot"
    project.mkdir()
    (project / "project.godot").write_text('config_version=5\n\n[application]\n\nconfig/name="loopcut_check"\n')
    shutil.copy(glb_path, project / "pack.glb")
    (project / "check.gd").write_text(GD_SCRIPT)
    first = subprocess.run([str(GODOT), "--headless", "--path", str(project), "--import"], capture_output=True,
                           text=True, timeout=180)
    log = (first.stdout or "") + (first.stderr or "")
    errors = [line for line in log.splitlines() if "ERROR" in line]
    print("GODOT IMPORT", first.returncode, log[-1500:], flush=True)
    check("Godot imports the .glb headless without errors",
          first.returncode == 0 and not errors and (project / ".godot" / "imported").is_dir(), errors[:5] or log[-600:])
    second = subprocess.run([str(GODOT), "--headless", "--path", str(project), "--script", "res://check.gd"],
                            capture_output=True, text=True, timeout=120)
    line = next((l for l in (second.stdout or "").splitlines() if l.startswith("LOOPCUT ")), "")
    found = json.loads(line[len("LOOPCUT "):]) if line else {}
    print("GODOT SCENE", found, flush=True)
    check("in Godot: both clips by name, every surface with tangents",
          found.get("loaded") and {"Walk", "Run"} <= set(found.get("animations") or [])
          and found.get("surfaces") and found.get("tangents") == found.get("surfaces"), f"{found} {second.stderr[-400:]}")


def main() -> int:
    keep = "--keep" in sys.argv
    folder = Path(tempfile.mkdtemp(prefix="lc_engine_check_")).resolve()
    t0 = time.time()
    try:
        fixture = build()

        # Godot: GLB, both clips, tangents, budget, the ball's shading fixed.
        target = folder / "pack.glb"
        result = engine_export.export_for_engine("godot", objects=["Crate", "Rig", "Ball"], path=str(target),
                                                 triangle_budget=100)
        print(result.text, flush=True)
        gltf, _ = glb.parse(target.read_bytes()) if target.exists() else ({}, b"")
        nodes = sorted(glb.summary(gltf)["nodes"]) if gltf else []
        extra = engine_export.gltf_extra(gltf) if gltf else {}
        check("Godot: a checked .glb with every object", result.ok and {"Crate", "Body", "Ball", "Rig"} <= set(nodes),
              f"{nodes} {result.text}")
        check("Godot: both actions as named clips", {"Walk", "Run"} <= set(extra.get("clips") or []), extra)
        check("Godot: tangents on every UV-mapped primitive", extra.get("mapped") and extra["tangents"] == extra["mapped"],
              extra)
        check("Godot: the budget is reported in plain words", "Over the triangle budget:" in result.text, result.text)
        check("Godot: the ball's flat custom normals were fixed before export, and said",
              not bpy.data.objects["Ball"].data.has_custom_normals and "Shading: Ball" in result.text, result.text)
        check("Godot: the normal map is not flipped (Godot reads OpenGL style)", "green flipped" not in result.text,
              result.text)
        check("after Godot: images, nodes, names and selection as they were", not as_before(fixture), as_before(fixture))
        try:
            engine_export.export_for_engine("godot", objects=["Crate"], path=str(target))
            refused = ""
        except ToolError as ex:
            refused = str(ex)
        check("an existing file is refused without overwrite", "already exists; pass overwrite=true" in refused, refused)
        godot_import(target, folder)

        # Unity: FBX with LOD copies; the rig's clips.
        target = folder / "crate_unity.fbx"
        result = engine_export.export_for_engine("unity", objects=["Crate"], path=str(target), lods=2)
        print(result.text, flush=True)
        found = engine_export.fbx_summary(fbx_tree(target)) if target.exists() else {"nodes": {}}
        lods = [found["nodes"].get(f"Crate_LOD{i}", {}).get("tris", 0) for i in range(3)]
        check("Unity: Crate_LOD0..2, each about half the last", result.ok and lods[0] > lods[1] > lods[2] > 0,
              f"{found['nodes']} {result.text}")
        check("Unity: tangents and both textures embedded",
              all(n.get("tangents") for n in found["nodes"].values()) and found.get("images", 0) >= 2, found)
        check("after Unity: Crate is Crate again, no LOD objects or copies left", not as_before(fixture), as_before(fixture))
        target = folder / "rig_unity.fbx"
        result = engine_export.export_for_engine("unity", objects=["Rig"], path=str(target))
        print(result.text, flush=True)
        found = engine_export.fbx_summary(fbx_tree(target)) if target.exists() else {"clips": [], "nodes": {}}
        clips = found.get("clips") or []
        check("Unity: the rig's actions as named clips",
              result.ok and any(c.endswith("Walk") for c in clips) and any(c.endswith("Run") for c in clips)
              and "Body" in found["nodes"], f"{clips} {result.text}")

        # Unreal: normal map flipped to DirectX style on the copy, the colour image at full size.
        target = folder / "crate_unreal.fbx"
        result = engine_export.export_for_engine("unreal", objects=["Crate"], path=str(target))
        print(result.text, flush=True)
        tree = fbx_tree(target)
        normal_size, normal_pixel = loaded(embedded(tree, "crate_normal"), folder, "unreal_normal")
        albedo_size, _ = loaded(embedded(tree, "crate_albedo"), folder, "unreal_albedo")
        check("Unreal: the embedded normal map's green is flipped (0.25 to 0.75)",
              result.ok and abs(normal_pixel[1] - 0.75) < 0.02 and normal_size == (64, 64), f"{normal_pixel} {result.text}")
        check("Unreal: the colour image stays at full size (under its 8192 cap)", albedo_size == (4608, 64), albedo_size)
        check("after Unreal: the user's normal map is unchanged", not as_before(fixture), as_before(fixture))

        # Roblox: colour image capped at 4096, the dense grid over the per-mesh cap; .gltf too.
        target = folder / "roblox.fbx"
        result = engine_export.export_for_engine("roblox", objects=["Crate", "Dense"], path=str(target))
        print(result.text, flush=True)
        albedo_size, albedo_pixel = loaded(embedded(fbx_tree(target), "crate_albedo"), folder, "roblox_albedo")
        check("Roblox: the colour image is capped at 4096 on a copy, its colour kept", result.ok
              and albedo_size == (4096, 57) and albedo_pixel[:3] == (0.6, 0.4, 0.2)
              and "4608x64 to 4096x57 on a copy; yours is unchanged" in result.text,
              f"{albedo_size} {albedo_pixel} {result.text}")
        check("Roblox: the grid is named over the 20,000-triangle per-mesh cap",
              "Roblox takes at most 20,000 triangles per mesh: Dense has" in result.text, result.text)
        target = folder / "roblox.gltf"
        result = engine_export.export_for_engine("roblox", objects=["Crate"], path=str(target))
        print(result.text, flush=True)
        document = json.loads(target.read_text()) if target.exists() else {}
        check("Roblox: a one-file .gltf, checked",
              result.ok and all(str(b.get("uri", "")).startswith("data:") for b in document.get("buffers", [])),
              result.text)
        check("after Roblox: everything as it was", not as_before(fixture), as_before(fixture))
    except Exception:
        traceback.print_exc()
        check("ran to the end", False, "exception above")
    finally:
        if not keep:
            shutil.rmtree(folder, ignore_errors=True)
    passed = sum(r["ok"] for r in RESULTS)
    summary = {"passed": passed, "total": len(RESULTS), "seconds": round(time.time() - t0, 1), "results": RESULTS}
    try:
        (Path.cwd() / "engine-export-check.json").write_text(json.dumps(summary, indent=1))
    except OSError:
        pass
    print(f"ENGINE {passed}/{len(RESULTS)} in {summary['seconds']}s")
    print("ENGINE OK" if passed == len(RESULTS) and RESULTS else "ENGINE FAIL", flush=True)
    return 0 if passed == len(RESULTS) and RESULTS else 1


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
    watchdog = threading.Timer(TIMEOUT, lambda: (print(f"ENGINE FAIL: timed out after {TIMEOUT}s", flush=True),
                                                  os._exit(3)))
    watchdog.daemon = True
    watchdog.start()
    if bpy.app.background:
        run_and_exit()
    else:
        bpy.app.timers.register(run_and_exit, first_interval=0.5)
