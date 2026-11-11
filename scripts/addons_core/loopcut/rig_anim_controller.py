"""Rigging and animation controller for Loopcut.

Manages:
- Armature creation, bone hierarchies, bone roll
- Automatic skinning weights and weight normalization
- Bone constraints (IK, Damped Track, Child Of)
- Shape keys creation and keyframing
- Keyframe insertion and interpolation modes
- Animation baking and NLA strips
"""


def create_armature(armature_name: str = "Armature", bones: list[dict] | None = None) -> dict:
    """Create a new armature with defined bones.
    bones format: [{"name": "Root", "head": (0,0,0), "tail": (0,0,1), "parent": None}, ...]
    """
    import bpy
    arm_data = bpy.data.armatures.new(armature_name)
    arm_obj = bpy.data.objects.new(armature_name, arm_data)
    bpy.context.collection.objects.link(arm_obj)
    bpy.context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)

    bpy.ops.object.mode_set(mode="EDIT")
    created = []
    if bones:
        edit_bones = arm_data.edit_bones
        for b_def in bones:
            b = edit_bones.new(b_def["name"])
            b.head = b_def.get("head", (0, 0, 0))
            b.tail = b_def.get("tail", (0, 0, 1))
            if b_def.get("parent") and b_def["parent"] in edit_bones:
                b.parent = edit_bones[b_def["parent"]]
            created.append(b.name)
    bpy.ops.object.mode_set(mode="OBJECT")

    return {"status": "ok", "armature": armature_name, "bones": created}


def bind_mesh_to_armature(mesh_name: str, armature_name: str, mode: str = "AUTOMATIC") -> dict:
    """Parent mesh to armature with vertex weights (AUTOMATIC, ENVELOPE, EMPTY)."""
    import bpy
    mesh = bpy.data.objects.get(mesh_name)
    arm = bpy.data.objects.get(armature_name)
    if not mesh or not arm:
        raise ValueError("Both mesh and armature objects must exist.")

    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm

    if mode.upper() == "AUTOMATIC":
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    elif mode.upper() == "ENVELOPE":
        bpy.ops.object.parent_set(type="ARMATURE_ENVELOPE")
    else:
        bpy.ops.object.parent_set(type="ARMATURE_NAME")

    return {"status": "ok", "action": "bind_mesh", "mesh": mesh_name, "armature": armature_name, "mode": mode}


def add_ik_constraint(armature_name: str, bone_name: str, target_obj: str,
                      target_bone: str = "", chain_count: int = 2) -> dict:
    """Add an Inverse Kinematics (IK) constraint to a pose bone."""
    import bpy
    arm = bpy.data.objects.get(armature_name)
    tgt = bpy.data.objects.get(target_obj)
    if not arm or not tgt:
        raise ValueError("Armature or target object not found.")

    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="POSE")

    p_bone = arm.pose.bones.get(bone_name)
    if not p_bone:
        bpy.ops.object.mode_set(mode="OBJECT")
        raise ValueError(f"Bone '{bone_name}' not found in armature '{armature_name}'.")

    c = p_bone.constraints.new("IK")
    c.target = tgt
    if target_bone and tgt.type == "ARMATURE":
        c.subtarget = target_bone
    c.chain_count = max(0, chain_count)

    bpy.ops.object.mode_set(mode="OBJECT")
    return {"status": "ok", "bone": bone_name, "constraint": "IK", "chain_count": chain_count}


def set_keyframe(object_name: str, frame: int, data_path: str = "location") -> dict:
    """Insert a keyframe on object_name at specified frame."""
    import bpy
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found.")
    bpy.context.scene.frame_set(frame)
    obj.keyframe_insert(data_path=data_path, frame=frame)
    return {"status": "ok", "object": object_name, "frame": frame, "data_path": data_path}
