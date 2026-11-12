"""Wire protocol shared by the addon and the MCP server.

One JSON object per line, UTF-8.

Request:  {"id": 1, "cmd": "scene_state", "params": {...}, "timeout": 120, "token": "..."}
Response: {"id": 1, "ok": true, "result": {...}}
          {"id": 1, "ok": false, "error": {"type": "...", "message": "...", "traceback": "..."}}

The addon runs any Python it is sent, so it answers only a request carrying the token it wrote,
on starting, to a file only this user can read.

This module must not import bpy so the MCP server can reuse it.
"""

import json
import os
import secrets
from pathlib import Path

PROTOCOL_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
DEFAULT_TIMEOUT = 120.0
TOKEN_DIR = Path.home() / ".claude_blender"


def token_path(port):
    return TOKEN_DIR / f"token-{port}"


def new_token(port):
    """Write a fresh token for port, readable by this user only, and return it."""
    TOKEN_DIR.mkdir(mode=0o700, exist_ok=True)
    path = token_path(port)
    path.unlink(missing_ok=True)
    token = secrets.token_hex(32)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write(token)
    return token


def read_token(port):
    """The token Blender wrote for port, or "" when the bridge has not started."""
    try:
        return token_path(port).read_text().strip()
    except FileNotFoundError:
        return ""


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
