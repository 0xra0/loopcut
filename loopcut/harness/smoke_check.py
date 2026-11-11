"""Headless smoke check for check.sh:  Blender -b --factory-startup --python loopcut/harness/smoke_check.py

Registers this checkout's add-on inside real Blender (the unit tests use a fake bpy, so a wrong bpy
name only shows up here), then runs two real tools on the default scene and unregisters it.
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
    loopcut.unregister()
    print("SMOKE OK")
    code = 0
except Exception:
    traceback.print_exc()
    print("SMOKE FAILED", file=sys.stderr)
sys.stdout.flush()
os._exit(code)
