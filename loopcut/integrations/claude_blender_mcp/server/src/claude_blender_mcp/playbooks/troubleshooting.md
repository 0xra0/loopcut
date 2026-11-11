# Troubleshooting

> When: a tool returns an error, or the result looks wrong in capture_views.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "Blender is not reachable" | Bridge not started | Ask the user to open Blender, press N, open the Claude tab and click Start Claude Bridge |
| Timeout on a long step | Heavy remesh or bake | Check `get_scene_state()` before retrying. The step may have finished. Retry with lower settings |
| Boolean leaves holes or junk | Cutter not closed, or coplanar faces | `rollback(...)`, `cleanup(...)` on both, offset the cutter slightly, try again |
| Remesh fused parts that should be separate | Parts closer than the voxel size | Separate them, remesh each, `join(...)` afterwards, or use a smaller voxel_size |
| Sculpt stroke did nothing | Points not near the surface, or radius too small | Check positions against the object's bounds and use a larger radius |
| Sculpt stroke refused | Mesh has modifiers or is in edit mode | `apply_modifiers(...)` first |
| quad_remesh fails or loses parts | Non-manifold input or thin parts | `cleanup(...)` and a `voxel_remesh(...)` of the input, then try again with higher target_faces |
| Bake is black or blotchy | No UVs, overlapping UVs, or cage too small | `uv_report(...)`, then raise cage_extrusion |
| bind_mesh AUTO fails ("Bone heat weighting failed") | Non-manifold mesh or bones outside it | `cleanup(...)`, check joints in xray, or bind with method="ENVELOPE" and refine |
| Arms deform with the torso | Arms touching the body in the rest pose | Re-pose to a T-pose, separate the geometry, bind again |
| Animation looks wrong in the engine | Wrong preset or clip not in NLA | See `export_to_engine` |
| Images show the wrong thing | Camera framed hidden or helper objects | `capture_views(objects=[...], isolate=True)` |
| Something broke and you don't know why | | `rollback(...)` to the last good checkpoint and redo the stage in smaller steps |

## When a tool can't do it

Use `run_python(...)`. Prefer bpy.data and bmesh over bpy.ops, and return a `result` you can check. Make a checkpoint first if the script changes geometry.
