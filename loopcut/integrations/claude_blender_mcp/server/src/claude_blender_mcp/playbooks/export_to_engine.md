# Export to a game engine

> When: getting a mesh, with or without a rig and animation, into Unity, Unreal or Godot, and proving the file is right.

## Formats and presets

| Engine | Call | Notes |
| --- | --- | --- |
| Unity | `export_model(path="<name>.fbx", preset="unity")` | Y-up with the axis conversion baked in, so objects import with rotation 0 and scale 1 |
| Unreal | `export_model(path="<name>.fbx", preset="unreal")` | Import with "Convert Scene" on. Skeletal meshes need the armature, which comes along automatically |
| Godot | `export_model(path="<name>.glb")` | glTF is Godot's native format |
| Anything else / web | `.glb` | Embedded textures and animations in one file |
| Static mesh only | `.obj` | No rig or animation |

## Preview first

As soon as the mesh is rigged, export a preview (`<Name>_preview.fbx`) and hand it over. Engine problems like scale, axes, materials and missing clips show up early, while they're cheap to fix. Re-export when the model is final.

## Before export

- `apply_transforms(...)` on every mesh and the armature.
- Animated: each clip is in its own NLA track (`push_to_nla(...)`), and `list_actions(...)` shows them all.
- Rigged: `limit_weights(mesh, limit=4)`.
- Materials use image textures. Procedural shader nodes don't survive export. Bake them first (`bake_maps(..., maps=["DIFFUSE"])`).

## Export and verify

1. `export_model(path, objects=[mesh names], preset=...)`. The armatures deforming those meshes are included automatically.
2. `reimport_check(path)`. Compare with the source:
   - triangle count is the same (± the triangulation of n-gons);
   - every object has scale [1,1,1] and the rotation you expect;
   - bones match the deform bone count, and skinned_meshes lists the mesh;
   - one action per NLA clip, with the right names and frame ranges;
   - materials and images are present.
3. Report the path and the numbers to the user.

## Common problems

- Character imports lying down or 100× too big or small: wrong preset for the engine, or transforms weren't applied.
- No animation in the engine: the clip was still the active action instead of in the NLA, or animation=False.
- Extra end bones in Unity: shouldn't happen, because add_leaf_bones is off. If an older file has them, re-export.
