"""merge._held on a Geometry Nodes modifier in real Blender (0.2.22: keys() on the modifier failed every merge of a
worker build that had one, because Blender 5.2 keeps the inputs as modifier.properties.inputs).

    python3 loopcut/windows/laptop.py check --script loopcut/harness/gn_merge_check.py --background --wait
    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/gn_merge_check.py

A cube carries a Geometry Nodes modifier whose group takes two Object inputs: one set to a quarantined object and one
to an ordinary object. _held(cube) must not raise and must name the quarantined object as the modifier's target, and
only that one; _node_inputs must return both objects. Synthetic scene, no network. Exits 0 when every check passes.
"""

import os
import sys
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
from loopcut import merge  # noqa: E402
from loopcut.ownership import QUARANTINE  # noqa: E402

RESULTS = []


def check(name: str, ok, detail="") -> None:
    RESULTS.append(bool(ok))
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  [{str(detail)[:600]}]" if detail else ""), flush=True)


def object_input(group, name: str) -> str:
    socket = group.interface.new_socket(name, in_out="INPUT", socket_type="NodeSocketObject")
    return socket.identifier


def set_input(modifier, identifier: str, value) -> str:
    """The 5.2 way when the modifier has it, else the ID property; says which one took."""
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    if inputs is not None:
        getattr(inputs, identifier).value = value
        return "properties.inputs"
    modifier[identifier] = value
    return "id property"


def main() -> int:
    print(f"Blender {bpy.app.version_string}", flush=True)
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    mesh = bpy.data.meshes.new("GNCheck_Mesh")
    cube = bpy.data.objects.new("GNCheck_Cube", mesh)
    held_target = bpy.data.objects.new("GNCheck_Held", None)
    held_target[QUARANTINE] = True
    free_target = bpy.data.objects.new("GNCheck_Free", None)
    for ob in (cube, held_target, free_target):
        bpy.context.scene.collection.objects.link(ob)

    group = bpy.data.node_groups.new("GNCheck_Group", "GeometryNodeTree")
    group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    held_id, free_id = object_input(group, "Held"), object_input(group, "Free")
    modifier = cube.modifiers.new("GNCheck", "NODES")
    modifier.node_group = group
    how = set_input(modifier, held_id, held_target)
    set_input(modifier, free_id, free_target)
    print(f"inputs set through {how}", flush=True)

    try:
        modifier.keys()
        print("modifier.keys() works on this Blender (the old path)", flush=True)
    except TypeError as error:
        print(f"modifier.keys() raises here, as in 0.2.21's failure: {error}", flush=True)

    try:
        values = merge._node_inputs(modifier)
        check("_node_inputs returns both object inputs",
              held_target in values and free_target in values, [getattr(v, "name", v) for v in values])
    except Exception as error:
        check("_node_inputs runs", False, repr(error))
    try:
        held = merge._held(cube)
        check("_held runs on a cube with a Geometry Nodes modifier", True, held)
        check("_held names the quarantined input as the modifier's target",
              held == [f"{modifier.name} target {held_target.name}"], held)
    except Exception as error:
        check("_held runs on a cube with a Geometry Nodes modifier", False, repr(error))
    passed = all(RESULTS) and len(RESULTS) == 3
    print(f"GN MERGE {'PASS' if passed else 'FAIL'}: {sum(RESULTS)}/{len(RESULTS)}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except BaseException:
        traceback.print_exc()
    finally:
        sys.stdout.flush()
        os._exit(code)
