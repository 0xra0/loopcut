"""Wire protocol shared by the addon and the MCP server.

One JSON object per line, UTF-8.

Request:  {"id": 1, "cmd": "scene_state", "params": {...}, "timeout": 120}
Response: {"id": 1, "ok": true, "result": {...}}
          {"id": 1, "ok": false, "error": {"type": "...", "message": "...", "traceback": "..."}}

This module must not import bpy so the MCP server can reuse it.
"""

import json

PROTOCOL_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
DEFAULT_TIMEOUT = 120.0


def to_jsonable(value):
    """Best-effort conversion of Blender/mathutils values to JSON types."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    # mathutils.Vector / Color / Euler / Quaternion and bpy_prop_array are iterable
    try:
        return [to_jsonable(v) for v in value]
    except TypeError:
        pass
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return repr(value)


def encode(message):
    return (json.dumps(to_jsonable(message), separators=(",", ":")) + "\n").encode("utf-8")


def decode(line):
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line)


def ok(request_id, result):
    return {"id": request_id, "ok": True, "result": result}


def error(request_id, err_type, message, tb=None):
    err = {"type": err_type, "message": message}
    if tb:
        err["traceback"] = tb
    return {"id": request_id, "ok": False, "error": err}
