import bpy, bmesh, math
from bpy_extras import bmesh_utils
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath="/mnt/ssd/ComfyUI/output/3D/lantern_lp.glb")
o=[o for o in bpy.context.scene.objects if o.type=="MESH"][0]; bpy.context.view_layer.objects.active=o; o.select_set(True)
for ang in (66, 80, 89):
    bpy.ops.object.mode_set(mode="EDIT"); bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(ang), island_margin=0.0, area_weight=0.0, scale_to_bounds=False)
    bpy.ops.uv.average_islands_scale()
    bpy.ops.uv.pack_islands(rotate=True, shape_method="CONCAVE", margin_method="FRACTION", margin=0.001)
    bm=bmesh.from_edit_mesh(o.data); uv=bm.loops.layers.uv.active
    n=len(bmesh_utils.bmesh_linked_uv_islands(bm, uv)); s=0
    for f in bm.faces:
        p=[l[uv].uv for l in f.loops]
        for i in range(1,len(p)-1): s+=abs((p[i]-p[0]).cross(p[i+1]-p[0]))/2
    bpy.ops.object.mode_set(mode="OBJECT"); print("TEST angle",ang,"islands",n,"cov",round(s,3))
