"""Headless smoke check for check.sh:  Blender -b --factory-startup --python loopcut/harness/smoke_check.py

Registers this checkout's add-on inside real Blender (the unit tests use a fake bpy, so a wrong bpy
name only shows up here), then runs real tools on the default scene through tools.execute, the way
the model calls them: run_python, pixal3d_refine on a named object that is not the active one (twice),
bake_maps, export_model, and every task menu checked against the registered studio commands. Then unregisters it.
Prints SMOKE OK and exits 0, or the traceback and exits 1.
"""
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
os.environ.setdefault("LOOPCUT_DATA_DIR", tempfile.mkdtemp(prefix="loopcut-smoke-"))

code = 1
try:
    import bmesh
    import bpy
    import loopcut
    loopcut.register()

    from loopcut import tools  # type: ignore[attr-defined]  # loaded by path in checkout.use()
    scene = json.loads(tools.execute("get_scene_info", "").text)
    assert "Cube" in {o["name"] for o in scene["objects"]}, scene

    ran = tools.execute("run_python", json.dumps({
        "summary": "Subdivide the cube",
        "code": "import bpy\n"
                "cube = bpy.data.objects['Cube']\n"
                "cube.modifiers.new('Sub', 'SUBSURF').levels = 3\n"
                "mesh = cube.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()\n"
                "print(len(mesh.polygons))"}))
    assert ran.ok and ran.text.startswith("384\n"), ran.text

    def studio(command, **params):
        result = tools.execute("studio_action", json.dumps({"command": command, "params": params, "summary": command}))
        assert result.ok, f"{command}: {result.text}"
        return result.text

    def add_loose_vertex(obj):
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.verts.new((9.0, 9.0, 9.0))
        bm.to_mesh(obj.data)
        bm.free()

    # A named object is the one that changes, whichever object is active, and a second run runs again.
    cube = bpy.data.objects["Cube"]
    bpy.ops.mesh.primitive_uv_sphere_add(location=(4, 0, 0))
    sphere = bpy.context.active_object
    add_loose_vertex(sphere)
    for _ in range(2):
        add_loose_vertex(cube)
        before = len(cube.data.vertices), len(sphere.data.vertices)
        refined = studio("pixal3d_refine", object_name="Cube", min_thickness=0)
        after = len(cube.data.vertices), len(sphere.data.vertices)
        assert after == (before[0] - 1, before[1]), (before, after, refined)

    bpy.ops.mesh.primitive_uv_sphere_add(radius=1.2)
    studio("bake_maps", low="Cube", high=bpy.context.active_object.name, maps=["NORMAL"], size=32)

    # An export to an ordinary folder still writes; the hidden-path refusal is the unit tests'.
    exported = Path(tempfile.mkdtemp(prefix="loopcut-smoke-export-")) / "cube.glb"
    studio("export_model", path=str(exported), objects=["Cube"])
    assert exported.stat().st_size > 0, exported

    from loopcut import studio as studio_module  # type: ignore[attr-defined]
    from loopcut.studio_vendor.handlers import COMMANDS  # type: ignore[import-not-found]
    for task in studio_module.TASK_TOOLSETS:
        names = studio_module.task_tool_menu(task)
        assert names and set(names) <= set(COMMANDS), (task, names)
    loopcut.unregister()
    print("SMOKE OK")
    code = 0
except Exception:
    traceback.print_exc()
    print("SMOKE FAILED", file=sys.stderr)
sys.stdout.flush()
os._exit(code)
