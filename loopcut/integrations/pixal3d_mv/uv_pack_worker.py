"""Isolated Blender UV pack worker; parent enforces a wall-clock timeout."""
from pathlib import Path
import sys

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prep_mesh import activate

src, dst, method, margin = sys.argv[sys.argv.index("--") + 1:]
bpy.ops.wm.read_factory_settings(use_empty=True)
with bpy.data.libraries.load(src, link=False) as (source, target):
    target.objects = source.objects
obj = next(x for x in target.objects if x and x.type == "MESH")
bpy.context.collection.objects.link(obj)
activate(obj)
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.uv.select_all(action="SELECT")
bpy.ops.uv.pack_islands(rotate=True, shape_method=method, margin_method="FRACTION", margin=float(margin))
bpy.ops.object.mode_set(mode="OBJECT")
result = np.empty(len(obj.data.uv_layers.active.data) * 2, dtype=np.float32)
obj.data.uv_layers.active.data.foreach_get("uv", result)
np.save(dst, result, allow_pickle=False)
