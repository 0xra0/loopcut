# Rig, skin and animate

> When: a mesh needs a skeleton, skin weights, test poses or animation clips.

## Pick a rig

| Situation | Rig |
| --- | --- |
| Biped going to a game engine | `rig_template(...)`: 21 deform bones with Unity/Unreal-friendly names. The default choice. |
| Animator wants IK/FK controls in Blender | `rigify_metarig(...)` then `rigify_generate(...)`. The DEF- bones deform. |
| Odd anatomy (tail, wings, extra limbs) | `rig_template(...)` plus `edit_bones(...)` to add bones |
| Windows PC with an NVIDIA GPU and UniRig installed, any shape | `auto_rig_unirig(...)`, then poll `auto_rig_status(...)`. Experimental: check the result carefully |

## Stages

### 1. Prepare the mesh

- `apply_transforms(mesh)`: scale 1, rotation 0. The character faces -Y in a T-pose with feet on Z = 0.
- `mesh_report(...)`: AUTO weights need a closed mesh with 0 non-manifold edges. Run `cleanup(...)` if not.

### 2. Build and fit the skeleton

- `rig_template(name="Rig", fit_to=mesh, arm_angle=...)`. arm_angle is 0 for a T-pose and 40-60 for an A-pose. Poses and cycles adjust to it.
- Check with `capture_views(objects=[mesh, "Rig"], views=["front","right"], shading="xray")`. Bones show as green shapes. Joints should sit inside the mesh: shoulder at the armpit's top, elbow and knee at the bend, wrist at the hand's base, hips at the crotch height.
- Move joints with `edit_bones(armature, bones=[{name, head, tail}])`. Bones keep their parents. Add extra bones the same way.

### 3. Bind

- `checkpoint(...)`, then `bind_mesh(mesh, armature, method="AUTO")`. It returns a rig_report.
- Check: unweighted_verts is 0 (or a handful on interior faces).

### 4. Test poses

- `pose_test(mesh, armature)` renders two stress poses on rig_template rigs. For other rigs pass poses.
- Look for:
  - elbows or knees that collapse into a thin pinch;
  - parts that stay behind (a hand that doesn't follow the arm);
  - parts that move with the wrong bone (thigh verts following the other leg, or the torso following the arm).
- Fixes, in order:
  1. Move the joint with `edit_bones(...)` and bind again.
  2. Add edge loops at the joint (more faces in retopology).
  3. Separate parts that touch in the rest pose and bind again.
  4. For precise fixes, `run_python(...)` with vertex group edits.

### 5. Limit and normalize

- `limit_weights(mesh, limit=4)`, then `rig_report(mesh, armature, max_influences=4)`: verts_over_influence_limit 0 and verts_not_normalized 0.

### 6. Animate

- Cycles: `procedural_cycle(armature, kind="walk")`, and likewise "run" and "idle". They're tuned for rig_template. For other rigs pass a bone_map.
- Check: `set_frame(...)` at the start, quarter, half and three-quarters of the cycle, with `capture_views(views=["right"])` each time. Feet should reach forward and back in turn, without sliding through the floor.
- Custom moves: `keyframe(name, keys=[...], action="Wave")`. Rotations are in degrees in local bone space. On rig_template, +X raises an arm and +X on a shin bends the knee.
- After each clip: `push_to_nla(armature)`, so every clip exports as its own named animation. `list_actions(...)` confirms them.
