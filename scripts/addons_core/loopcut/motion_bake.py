"""Secondary motion on export (game-asset-quality §9): bone motion an action's keys don't hold, baked into keys.

Engines play keys. Bones a constraint (a spring's Damped Track, IK, Copy Rotation), a driver or a frame handler (a
jiggle or spring add-on) moves play nothing there. Blender's exporters sample the scene each frame, so they do catch
constraints, drivers and handlers (measured on SPLAP 2026-10-31), but they step every clip straight on from the last
one's state, so a simulated spring starts each clip mid-swing, and an FBX also takes every action whose curves fit
the rig. So before export_for_engine writes the file, for each exported armature with such a source:
- every action the exporter would export for it (the one it plays, and every action whose curves all fit it, as the
  FBX exporter picks them) is copied, and the copy baked by Blender's own visual keying (anim_utils.bake_action),
  one frame at a time from the clip's first frame, with the scene's frame range set to the clip so a simulation
  restarts there;
- the copies are exported under the actions' own names, through temporary single-strip NLA tracks (the FBX exporter's
  NLA strips, named "Rig|Action" as its all-actions takes are, and the glTF exporter's ACTIONS mode), with the
  sources switched off meanwhile (constraints disabled, drivers muted, handlers set aside) so nothing is applied twice;
- everything is put back after, whatever happens: the user's actions are never edited (renamed for the export only),
  their assignment, NLA mutes, constraints, drivers, handlers and frame range are as they were.
Handlers are Python functions in bpy.app.handlers.frame_change_pre/post that are not Loopcut's; which bones they move
is found by stepping a clip and comparing each bone with its keys. A mesh's motion (shape keys) is left to the
exporters. Muse's belly spring (out/demos-work/muse-toolbox/tpose/secondary.py) was simulated offline and keyed as the
Belly bone's location in every M_* action, so it is keys already and nothing is baked for it.

harness/secondary_motion_check.py checks it on SPLAP.
"""

import re

_TRANSFORM = re.compile(r'^pose\.bones\["((?:[^"\\]|\\.)*)"\]\.'
                        r'(location|rotation_quaternion|rotation_euler|rotation_axis_angle|scale)$')
_OURS = ("loopcut", "bpy", "bpy_extras", "bl_pkg", "addon_utils", "io_scene_gltf2", "io_scene_fbx", "cycles")
HANDLER_SAMPLES = 24     # Frames stepped through a clip to see which bones a frame handler moves.
MOVED = 1e-4             # A basis element differing from the keys by more than this: moved by something else.
MAX_NAMED = 8            # Bones named per rig in the report.


def _unescape(name: str) -> str:
    return re.sub(r"\\(.)", r"\1", name)


def foreign_handlers() -> list:
    """(handler list name, function) of frame handlers that are not Loopcut's or Blender's own."""
    import bpy
    found = []
    for kind in ("frame_change_pre", "frame_change_post"):
        for fn in getattr(bpy.app.handlers, kind):
            module = (getattr(fn, "__module__", "") or "").split(".")[0]
            if module not in _OURS:
                found.append((kind, fn))
    return found


def sources(rig) -> dict:
    """{bone: {what moves it}} from its enabled constraints and unmuted transform drivers."""
    found = {}
    for pb in rig.pose.bones:
        for constraint in pb.constraints:
            if constraint.enabled and constraint.influence > 0:
                found.setdefault(pb.name, set()).add(constraint.type.replace("_", " ").lower() + " constraint")
    data = rig.animation_data
    for fc in (data.drivers if data is not None else ()):
        match = _TRANSFORM.match(fc.data_path)
        if match and not fc.mute and _unescape(match.group(1)) in rig.pose.bones:
            found.setdefault(_unescape(match.group(1)), set()).add("driver")
    return found


def _fitting_slot(action, resolve):
    """The slot of a channelbag whose curves all resolve on the object (the FBX exporter's rule), or None."""
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                if not bag.fcurves:
                    continue
                try:
                    for fc in bag.fcurves:
                        resolve(fc.data_path + (f"[{fc.array_index}]" if fc.array_index else ""))
                except ValueError:
                    continue
                return bag.slot
    return None


def actions_for(ob) -> list:
    """[(action, slot)] the exporters export for it: the one it plays, then every action whose curves fit it."""
    import bpy
    data = ob.animation_data
    if data is None:
        return []
    found = [(data.action, data.action_slot)] if data.action is not None and data.action_slot is not None else []
    for action in bpy.data.actions:
        if data.action is not None and action == data.action:
            continue
        slot = _fitting_slot(action, ob.path_resolve)
        if slot is not None:
            found.append((action, slot))
    return found


def _keyed_basis(bag, pb, frame):
    """The bone's basis from the action's keys alone at frame (its rest values where it has none)."""
    import bpy
    from mathutils import Euler, Matrix, Quaternion, Vector
    base = f'pose.bones["{bpy.utils.escape_identifier(pb.name)}"]'

    def value(prop, index, default):
        fc = bag.fcurves.find(f"{base}.{prop}", index=index) if bag is not None else None
        return fc.evaluate(frame) if fc is not None else default
    location = Vector([value("location", i, 0.0) for i in range(3)])
    scale = Vector([value("scale", i, 1.0) for i in range(3)])
    mode = pb.rotation_mode
    if mode == "QUATERNION":
        rotation = Quaternion([value("rotation_quaternion", i, (1.0, 0.0, 0.0, 0.0)[i]) for i in range(4)])
        rotation = rotation.normalized().to_matrix() if rotation.magnitude > 1e-9 else Matrix.Identity(3)
    elif mode == "AXIS_ANGLE":
        angle, *axis = [value("rotation_axis_angle", i, (0.0, 0.0, 1.0, 0.0)[i]) for i in range(4)]
        rotation = Matrix.Rotation(angle, 3, Vector(axis)) if Vector(axis).length > 1e-9 else Matrix.Identity(3)
    else:
        rotation = Euler([value("rotation_euler", i, 0.0) for i in range(3)], mode).to_matrix()
    return Matrix.LocRotScale(location, rotation, scale)


def channelbag(action, slot):
    """The slot's F-curves on a layered action: the first strip holding a channelbag for it, else None (the lookup
    bpy_extras.anim_utils does)."""
    if not action or not slot:
        return None
    return next((bag for layer in action.layers for strip in layer.strips
                 if (bag := strip.channelbag(slot)) is not None), None)


def handler_moved(rig, action, slot, scene) -> set:
    """Bones whose basis, stepping through the clip in order, differs from its keys: a handler (or a driver) set it."""
    bag = channelbag(action, slot)
    first, last = (int(round(v)) for v in action.frame_range)
    step = max(1, (last - first) // HANDLER_SAMPLES)
    moved = set()
    for frame in range(first, last + 1, step):
        scene.frame_set(frame)
        for pb in rig.pose.bones:
            if pb.name in moved:
                continue
            keyed = _keyed_basis(bag, pb, frame)
            if any(abs(pb.matrix_basis[i][j] - keyed[i][j]) > MOVED for i in range(4) for j in range(4)):
                moved.add(pb.name)
    return moved


class Baked:
    """What prepare() did for one export: the report lines, whether to export through NLA strips, and undo()."""

    def __init__(self):
        self.lines, self.nla, self._undo = [], False, []

    def later(self, fn):
        self._undo.append(fn)

    def undo(self):
        while self._undo:
            fn = self._undo.pop()
            try:
                fn()
            except (ReferenceError, RuntimeError, ValueError, AttributeError):
                pass


def _bake_options():
    from bpy_extras import anim_utils
    return anim_utils.BakeOptions(only_selected=False, do_pose=True, do_object=False, do_visual_keying=True,
                                  do_constraint_clear=False, do_parents_clear=False, do_clean=False, do_location=True,
                                  do_rotation=True, do_scale=True, do_bbone=False, do_custom_props=False)


def _keep_animation_state(baked, ob):
    """Undo entries for an object's animation data: what it plays, how, its NLA mutes, and tracks added later."""
    data = ob.animation_data
    state = (data.action, data.action_slot, data.action_blend_type, data.action_extrapolation,
             data.action_influence, data.use_tweak_mode)
    mutes = [(track, track.mute, [(strip, strip.mute) for strip in track.strips]) for track in data.nla_tracks]
    existing = {track.name for track in data.nla_tracks}

    def put_back():
        for track in [t for t in data.nla_tracks if t.name not in existing or t.name.startswith("Loopcut bake")]:
            data.nla_tracks.remove(track)
        for track, mute, strips in mutes:
            track.mute = mute
            for strip, strip_mute in strips:
                strip.mute = strip_mute
        if data.use_tweak_mode != state[5]:
            data.use_tweak_mode = state[5]
        data.action = state[0]
        if state[0] is not None and state[1] is not None:
            data.action_slot = state[1]
        data.action_blend_type, data.action_extrapolation, data.action_influence = state[2], state[3], state[4]
    baked.later(put_back)


def prepare(objects, scene) -> Baked:
    """Bake the secondary motion of the exported armatures into temporary copies of their actions and set them up
    for export (see the module docstring). Main thread; Baked.undo() puts everything back, also after a failure here."""
    baked = Baked()
    try:
        return _prepare(baked, objects, scene)
    except BaseException:
        baked.undo()
        raise


def _prepare(baked, objects, scene) -> Baked:
    import bpy
    from bpy_extras import anim_utils
    rigs = [ob for ob in objects if ob.type == "ARMATURE" and ob.animation_data is not None]
    handlers = foreign_handlers()
    found = {rig: sources(rig) for rig in rigs}
    if not rigs or (not handlers and not any(found.values())):
        return baked
    frame = (scene.frame_current, scene.frame_subframe)
    span = (scene.frame_start, scene.frame_end)
    baked.later(lambda: (setattr(scene, "frame_start", span[0]), setattr(scene, "frame_end", span[1]),
                         scene.frame_set(frame[0], subframe=frame[1])))
    copies = []  # (action, its name, its baked copy); removed once nothing plays them (undone second to last).
    baked.later(lambda: [bpy.data.actions.remove(copy) for _, _, copy in copies if copy.users == 0])
    for rig in rigs:
        _keep_animation_state(baked, rig)
    if handlers:  # Which bones they move: a clip stepped in order, each bone against its keys.
        for rig in rigs:
            clips = actions_for(rig)
            if clips:
                rig.animation_data.action = clips[0][0]
                rig.animation_data.action_slot = clips[0][1]
                # The scene's range on the clip, as when baking: a simulation restarts at its first frame.
                scene.frame_start, scene.frame_end = (int(round(v)) for v in clips[0][0].frame_range)
                for name in handler_moved(rig, *clips[0], scene) - set(found[rig]):
                    found[rig].setdefault(name, set()).add("frame handler")
    plans = {rig: actions_for(rig) for rig in rigs if found[rig]}
    plans = {rig: clips for rig, clips in plans.items() if clips}
    if not plans:
        return baked
    # Bake: a copy per action, stepped from its first frame with the scene's range on the clip.
    for rig, clips in plans.items():
        data = rig.animation_data
        if data.use_tweak_mode:
            data.use_tweak_mode = False
        for action, slot in clips:
            copy = action.copy()
            copy.use_fake_user = False
            copies.append((action, action.name, copy))
            first, last = (int(round(v)) for v in action.frame_range)
            scene.frame_start, scene.frame_end = first, last
            data.action = copy
            data.action_slot = next((s for s in copy.slots if s.identifier == slot.identifier), copy.slots[0])
            anim_utils.bake_action(rig, action=copy, frames=range(first, last + 1), bake_options=_bake_options())
    # Names: each copy takes its action's name while the file is written.
    for action, name, copy in copies:
        action.name = name + ".loopcut-original"
        copy.name = name
    baked.later(lambda: [setattr(action, "name", name) for action, name, _ in copies])
    baked.later(lambda: [setattr(copy, "name", copy.name + ".loopcut-baked") for _, _, copy in copies])
    # Export through single-strip NLA tracks: the copies for baked rigs, the actions as they are for the rest.
    by_original = {action: copy for action, _, copy in copies}
    animated = [ob for ob in objects if ob.animation_data is not None and actions_for(ob)]
    for ob in animated:
        if ob not in plans:
            _keep_animation_state(baked, ob)
        data = ob.animation_data
        if data.use_tweak_mode:
            data.use_tweak_mode = False
        for track in data.nla_tracks:
            track.mute = True
            for strip in track.strips:
                strip.mute = True
        for action, slot in actions_for(ob) if ob not in plans else plans[ob]:
            played = by_original.get(action, action)
            track = data.nla_tracks.new()
            track.name = f"Loopcut bake {played.name}"[:63]
            first = int(round(played.frame_range[0]))
            strip = track.strips.new(f"{ob.name}|{played.name}"[:63], first, played)
            match = next((s for s in played.slots if s.identifier == slot.identifier), None)
            if match is not None and strip.action_slot != match:
                strip.action_slot = match
        data.action = None
    # Switch off what was baked, so the exporters don't apply it again.
    for rig in plans:
        for pb in rig.pose.bones:
            for constraint in pb.constraints:
                if constraint.enabled and pb.name in found[rig]:
                    constraint.enabled = False
                    baked.later(lambda c=constraint: setattr(c, "enabled", True))
        for fc in rig.animation_data.drivers:
            match = _TRANSFORM.match(fc.data_path)
            if match and not fc.mute and _unescape(match.group(1)) in found[rig]:
                fc.mute = True
                baked.later(lambda f=fc: setattr(f, "mute", False))
    order = {kind: list(getattr(bpy.app.handlers, kind)) for kind in ("frame_change_pre", "frame_change_post")}
    for kind, fn in handlers:
        getattr(bpy.app.handlers, kind).remove(fn)

    def handlers_back():  # In their places among the others.
        for kind, functions in order.items():
            current = getattr(bpy.app.handlers, kind)
            current.clear()
            current.extend(functions)
    baked.later(handlers_back)
    baked.nla = True
    for rig, clips in plans.items():
        named = sorted(found[rig])
        words = ", ".join(f"{name} ({', '.join(sorted(found[rig][name]))})" for name in named[:MAX_NAMED])
        more = f" and {len(named) - MAX_NAMED} more" if len(named) > MAX_NAMED else ""
        baked.lines.append(f"Secondary motion baked into keys in its {len(clips)} clip{'s' * (len(clips) != 1)}, so "
                           f"engines play it without Blender's constraints, drivers or handlers: {rig.name}: "
                           f"{words}{more}.")
    return baked
