"""core toolset: scene state, Python fallback, checkpoints."""

import contextlib
import io
import os
import re
import tempfile
import time
import traceback

import bpy

from . import COMMANDS, CommandError, command
from .. import protocol

MAX_CHECKPOINTS = 20
_checkpoints = []  # [{"id", "label", "path", "time"}], oldest first
_next_checkpoint = 1


def _window_override():
    """Context override with a window, needed by some wm ops when run from a timer."""
    wm = bpy.context.window_manager
    if wm and wm.windows:
        return bpy.context.temp_override(window=wm.windows[0])
    return contextlib.nullcontext()


def push_undo(message):
    """Best effort: lets the user Ctrl+Z what Claude did. No-op in background mode."""
    if bpy.app.background:
        return
    try:
        with _window_override():
            bpy.ops.ed.undo_push(message=message)
    except Exception:  # noqa: BLE001 - undo is a convenience, never a failure
        pass


@command("ping", "core")
def ping():
    """Health check. Returns Blender and protocol versions."""
    return {
        "pong": True,
        "protocol": protocol.PROTOCOL_VERSION,
        "blender": bpy.app.version_string,
        "background": bpy.app.background,
    }


@command("list_commands", "core")
def list_commands():
    """All commands the addon understands, grouped by toolset."""
    out = {}
    for name, entry in sorted(COMMANDS.items()):
        out.setdefault(entry["toolset"], []).append(name)
    return out


def _object_summary(obj):
    info = {
        "name": obj.name,
        "type": obj.type,
        "parent": obj.parent.name if obj.parent else None,
        "collections": [c.name for c in obj.users_collection],
        "location": [round(v, 4) for v in obj.location],
        "rotation_euler": [round(v, 4) for v in obj.rotation_euler],
        "scale": [round(v, 4) for v in obj.scale],
        "dimensions": [round(v, 4) for v in obj.dimensions],
        "visible": obj.visible_get() if bpy.context.view_layer.objects.get(obj.name) else False,
        "selected": obj.select_get() if bpy.context.view_layer.objects.get(obj.name) else False,
        "modifiers": [f"{m.name} ({m.type})" for m in obj.modifiers],
        "materials": [s.material.name for s in obj.material_slots if s.material],
    }
    if obj.type == "MESH":
        me = obj.data
        info["mesh"] = {
            "verts": len(me.vertices),
            "faces": len(me.polygons),
            "tris": sum(len(p.vertices) - 2 for p in me.polygons),
        }
    elif obj.type == "ARMATURE":
        info["armature"] = {"bones": len(obj.data.bones)}
    if obj.animation_data and obj.animation_data.action:
        info["action"] = obj.animation_data.action.name
    return info


@command("scene_state", "core")
def scene_state(include_objects=True, limit=200):
    """Summary of the open file, scene settings and objects."""
    scene = bpy.context.scene
    active = bpy.context.view_layer.objects.active
    state = {
        "blender": bpy.app.version_string,
        "file": bpy.data.filepath or None,
        "unsaved_changes": bpy.data.is_dirty,
        "scene": scene.name,
        "mode": bpy.context.mode,
        "active_object": active.name if active else None,
        "frame": {"current": scene.frame_current, "start": scene.frame_start, "end": scene.frame_end, "fps": scene.render.fps},
        "render_engine": scene.render.engine,
        "unit_system": scene.unit_settings.system,
        "unit_scale": scene.unit_settings.scale_length,
        "counts": {
            "objects": len(scene.objects),
            "meshes": len(bpy.data.meshes),
            "materials": len(bpy.data.materials),
            "armatures": len(bpy.data.armatures),
            "actions": len(bpy.data.actions),
            "images": len(bpy.data.images),
        },
        "checkpoints": len(_checkpoints),
    }
    if include_objects:
        objs = list(scene.objects)
        state["objects"] = [_object_summary(o) for o in objs[:limit]]
        if len(objs) > limit:
            state["objects_truncated"] = len(objs) - limit
    return state


@command("run_python", "core")
def run_python(code):
    """Run Python with bpy, bmesh and mathutils in scope. Assign to `result` to return data."""
    import bmesh
    import mathutils

    namespace = {"bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "result": None}
    stdout = io.StringIO()
    started = time.perf_counter()
    try:
        with contextlib.redirect_stdout(stdout):
            exec(compile(code, "<claude>", "exec"), namespace)  # noqa: S102 - this is the point
    except Exception as exc:  # noqa: BLE001
        # Return the output so far alongside the error; the traceback points into <claude>.
        raise CommandError(
            f"{type(exc).__name__}: {exc}\n\nstdout:\n{stdout.getvalue()[-4000:]}\n\n{traceback.format_exc(limit=-3)}"
        ) from exc
    push_undo("Claude: run_python")
    return {
        "result": protocol.to_jsonable(namespace.get("result")),
        "stdout": stdout.getvalue()[-8000:],
        "seconds": round(time.perf_counter() - started, 3),
    }


def _checkpoint_dir():
    path = os.path.join(tempfile.gettempdir(), "claude_blender_checkpoints", str(os.getpid()))
    os.makedirs(path, exist_ok=True)
    return path


@command("checkpoint", "core")
def checkpoint(label=""):
    """Save a copy of the current file state that rollback can restore. The open file is untouched."""
    global _next_checkpoint
    cp_id = _next_checkpoint
    _next_checkpoint += 1
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", label)[:40].strip("_")
    path = os.path.join(_checkpoint_dir(), f"{cp_id:03d}_{slug or 'checkpoint'}.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path, copy=True, check_existing=False, compress=False)
    source = bpy.data.filepath or None
    if source and os.path.dirname(source) == _checkpoint_dir():
        # After a rollback the open file is a checkpoint copy; remember the user's real file.
        source = _checkpoints[-1]["source_file"] if _checkpoints else None
    entry = {"id": cp_id, "label": label, "path": path, "time": time.time(), "source_file": source}
    _checkpoints.append(entry)
    while len(_checkpoints) > MAX_CHECKPOINTS:
        old = _checkpoints.pop(0)
        with contextlib.suppress(OSError):
            os.remove(old["path"])
    return {"id": cp_id, "label": label, "kept": len(_checkpoints)}


@command("list_checkpoints", "core")
def list_checkpoints():
    """Checkpoints saved in this Blender session, oldest first."""
    return [{"id": c["id"], "label": c["label"], "age_seconds": round(time.time() - c["time"])} for c in _checkpoints]


@command("rollback", "core")
def rollback(checkpoint_id=None):
    """Restore a checkpoint (default: the latest). Later checkpoints are discarded."""
    if not _checkpoints:
        raise CommandError("No checkpoints yet. Call checkpoint before risky steps.")
    if checkpoint_id is None:
        target = _checkpoints[-1]
    else:
        matches = [c for c in _checkpoints if c["id"] == checkpoint_id]
        if not matches:
            raise CommandError(f"No checkpoint {checkpoint_id}. Have: {[c['id'] for c in _checkpoints]}")
        target = matches[0]
    with _window_override():
        bpy.ops.wm.open_mainfile(filepath=target["path"], load_ui=False)
    discarded = [c for c in _checkpoints if c["id"] > target["id"]]
    for c in discarded:
        _checkpoints.remove(c)
        with contextlib.suppress(OSError):
            os.remove(c["path"])
    note = None
    if target["source_file"]:
        note = (
            "Blender now has the checkpoint copy open. Save with File > Save As over "
            f"{target['source_file']} to keep this state in your original file."
        )
    return {"restored": target["id"], "label": target["label"], "discarded": [c["id"] for c in discarded], "note": note}
