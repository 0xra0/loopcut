"""Command registry. Handlers run on Blender's main thread."""

import traceback

from .. import protocol

COMMANDS = {}


def command(name, toolset):
    def wrap(fn):
        COMMANDS[name] = {"fn": fn, "toolset": toolset, "doc": (fn.__doc__ or "").strip()}
        return fn

    return wrap


class CommandError(Exception):
    """Expected failure with a message meant for Claude (bad name, wrong mode...)."""


def dispatch(request):
    request_id = request.get("id")
    name = request.get("cmd")
    entry = COMMANDS.get(name)
    if entry is None:
        return protocol.error(request_id, "UnknownCommand", f"Unknown command: {name!r}")
    params = request.get("params") or {}
    try:
        # Bring matrices and bounds up to date with any edits made since the last command.
        import bpy

        bpy.context.view_layer.update()
    except Exception:  # noqa: BLE001 - e.g. no view layer during file load
        pass
    try:
        result = entry["fn"](**params)
    except CommandError as exc:
        return protocol.error(request_id, "CommandError", str(exc))
    except TypeError as exc:
        # Usually wrong parameter names from the client.
        return protocol.error(request_id, "TypeError", str(exc), traceback.format_exc())
    except Exception as exc:  # noqa: BLE001 - every failure must reach the client
        return protocol.error(request_id, type(exc).__name__, str(exc), traceback.format_exc())
    return protocol.ok(request_id, protocol.to_jsonable(result))


# Import handler modules so they register themselves.
from . import animate, core, export, image_to_3d, look, materials, modeling, reference, rig, sculpt, topology, uv_bake  # noqa: E402,F401
