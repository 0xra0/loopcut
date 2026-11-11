import bpy, bmesh
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath="/mnt/ssd/ComfyUI/output/3D/lantern_lp.glb")
o=[o for o in bpy.context.scene.objects if o.type=="MESH"][0]; bpy.context.view_layer.objects.active=o; o.select_set(True)
def cov():
    bm=bmesh.new(); bm.from_mesh(o.data); uv=bm.loops.layers.uv.active; s=0
    for f in bm.faces:
        p=[l[uv].uv for l in f.loops]
        for i in range(1,len(p)-1): s+=abs((p[i]-p[0]).cross(p[i+1]-p[0]))/2
    bm.free(); return round(s,3)
def islands():
    bpy.ops.object.mode_set(mode="EDIT"); bpy.ops.mesh.select_all(action="SELECT")
    bm=bmesh.from_edit_mesh(o.data); 
    from bpy_extras import bmesh_utils
    n=len(bmesh_utils.bmesh_linked_uv_islands(bm, bm.loops.layers.uv.active)); bpy.ops.object.mode_set(mode="OBJECT"); return n
print("TEST islands",islands(),"cov now",cov())
for kw in [dict(shape_method="CONCAVE"),dict(shape_method="CONCAVE",margin_method="FRACTION",margin=0.001),dict(shape_method="AABB")]:
    bpy.ops.object.mode_set(mode="EDIT"); bpy.ops.mesh.select_all(action="SELECT"); bpy.ops.uv.select_all(action="SELECT")
    bpy.ops.uv.pack_islands(rotate=True, **kw); bpy.ops.object.mode_set(mode="OBJECT")
    print("TEST",kw,cov())
