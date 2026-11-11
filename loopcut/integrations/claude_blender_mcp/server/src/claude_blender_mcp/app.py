"""MCP tool definitions. Each tool is a thin, documented wrapper over one addon command."""

import base64
import json
import os

try:  # mcp 2.x
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp import Image

from . import guide, protocol
from . import tools_build, tools_character, tools_reference
from .connection import BlenderConnection, BlenderError, BlenderUnavailable

INSTRUCTIONS = """\
Tools for building 3D content in a live Blender session.

Before any build that takes more than a few steps, call get_playbook("planning"). It explains
how to plan in stages with measurable checks, and which playbook covers the request
(from_reference, game_character, detailed_sculpt, hard_surface_prop, retopo_and_bake,
rig_and_animate, export_to_engine, troubleshooting). When the user gives a picture to match,
use from_reference.

Working rules:
- Start with get_scene_state to see what exists.
- Call checkpoint before any step that could wreck the model (booleans, remesh, applying
  modifiers, big run_python scripts). If a step goes wrong, rollback instead of patching.
- After each meaningful change, look at it: capture_views for shape, mesh_report for numbers.
  Do not assume a script did what you intended.
- Prefer dedicated tools when they exist; use run_python for everything else.
- Units are meters; rotations in tool arguments are degrees. Characters face -Y.
"""

ALL_TOOLSETS = ("core", "look", "modeling", "topology", "sculpt", "uv_bake", "materials", "rig", "animate", "export",
                "reference")

mcp = _Server("claude-blender", instructions=INSTRUCTIONS)
_conn = BlenderConnection(
    host=os.environ.get("CLAUDE_BLENDER_HOST", protocol.DEFAULT_HOST),
    port=int(os.environ.get("CLAUDE_BLENDER_PORT", protocol.DEFAULT_PORT)),
)


def enabled_toolsets():
    raw = os.environ.get("CLAUDE_BLENDER_TOOLSETS", "all")
    if raw.strip().lower() == "all":
        return set(ALL_TOOLSETS)
    return {t.strip() for t in raw.split(",") if t.strip()}


def _call(cmd, timeout=protocol.DEFAULT_TIMEOUT, **params):
    try:
        return _conn.call(cmd, params, timeout=timeout)
    except BlenderError as exc:
        # Surface Blender's message (and a short traceback) so Claude can fix its call.
        detail = str(exc)
        if exc.traceback:
            detail += "\n" + "\n".join(exc.traceback.strip().splitlines()[-6:])
        raise RuntimeError(detail) from exc
    except BlenderUnavailable as exc:
        raise RuntimeError(str(exc)) from exc


def _dump(data):
    return json.dumps(data, indent=1, default=str)


def tool(toolset):
    """Register an MCP tool only when its toolset is enabled."""

    def wrap(fn):
        if toolset in enabled_toolsets():
            return mcp.tool()(fn)
        return fn

    return wrap


# --- core --------------------------------------------------------------------


@tool("core")
def get_scene_state(include_objects: bool = True, limit: int = 200) -> str:
    """Summary of the open Blender file: mode, active object, frame range, render engine,
    counts, and per-object type, transforms, dimensions, modifiers, materials and polycount."""
    return _dump(_call("scene_state", include_objects=include_objects, limit=limit))


@tool("core")
def run_python(code: str, timeout_seconds: float = 120) -> str:
    """Run Python inside Blender with `bpy`, `bmesh` and `mathutils` already imported.
    Assign a JSON-serialisable value to `result` to return it; print() output is returned too.
    Runs on Blender's main thread. Raise timeout_seconds for long bakes or remeshes.
    Prefer context-free data API calls (bpy.data, bmesh) over bpy.ops where you can,
    since operators depend on mode, selection and the active object."""
    return _dump(_call("run_python", timeout=timeout_seconds, code=code))


@tool("core")
def checkpoint(label: str = "") -> str:
    """Save a restorable snapshot of the whole file before a risky step. Returns its id.
    The user's open file on disk is not modified. Keeps the 20 most recent."""
    return _dump(_call("checkpoint", label=label))


@tool("core")
def list_checkpoints() -> str:
    """Checkpoints saved in this Blender session, oldest first."""
    return _dump(_call("list_checkpoints"))


@tool("core")
def rollback(checkpoint_id: int | None = None) -> str:
    """Restore the file to a checkpoint (default: the latest). Checkpoints newer than it are dropped.
    Use this instead of trying to undo a broken result by hand."""
    return _dump(_call("rollback", checkpoint_id=checkpoint_id))


# --- guide (always on: no Blender call, nothing to filter) ---------------------


@mcp.tool()
def list_playbooks() -> str:
    """Playbooks for multi-stage builds, with when to use each. Start with "planning"."""
    return _dump(guide.index())


@mcp.tool()
def get_playbook(name: str) -> str:
    """Read a playbook: the stages, tools and acceptance checks for one kind of build."""
    return guide.read(name)


@mcp.prompt()
def plan_build(request: str) -> str:
    """Plan and build something in Blender, stage by stage, checking each stage."""
    return (f"Build this in Blender: {request}\n\n" + guide.read("planning") +
            "\n\nNow call get_scene_state, load the playbooks this request needs with get_playbook, "
            "write the stage plan with acceptance checks, and work through it.")


# --- look ----------------------------------------------------------------------


@tool("look")
def capture_views(
    views: list[str] | None = None,
    objects: list[str] | None = None,
    resolution: int = 512,
    shading: str = "solid",
    isolate: bool = False,
) -> list:
    """Render images of the model from fixed angles so you can check proportions and shape.
    views: any of front, back, left, right, top, bottom, iso (default front, right, iso).
    objects: frame only these (default: all visible). isolate hides everything else.
    shading: solid, wireframe (shows topology) or xray. Armatures among the objects are drawn as
    green bone shapes, so objects=[mesh, rig] with xray shows the joints inside the mesh.
    Uses Workbench, so it is fast and independent of the scene's render settings."""
    result = _call(
        "capture_views",
        timeout=300,
        views=views or ["front", "right", "iso"],
        objects=objects,
        resolution=resolution,
        shading=shading,
        isolate=isolate,
    )
    images = result.pop("images")
    out = [_dump({**result, "views": [i["view"] for i in images]})]
    for img in images:
        out.append(f"view: {img['view']}")
        out.append(Image(data=base64.b64decode(img["png_base64"]), format="png"))
    return out


@tool("look")
def get_object_details(name: str) -> str:
    """Everything about one object: transforms, modifier settings, materials, vertex groups,
    shape keys, UV layers, constraints, bones (for armatures) and animation."""
    return _dump(_call("object_details", name=name))


@tool("look")
def mesh_report(name: str, evaluated: bool = True) -> str:
    """Topology numbers for one mesh: verts, faces, tris, quads, n-gons, non-manifold and
    boundary edges, loose geometry, zero-area faces, UV layers, whether scale is applied.
    evaluated=True measures the mesh with modifiers applied (what would be exported)."""
    return _dump(_call("mesh_report", name=name, evaluated=evaluated))


tools_build.register(tool, _call, _dump)
tools_character.register(tool, _call, _dump, lambda data: Image(data=data, format="png"))
tools_reference.register(tool, _call, _dump, lambda data: Image(data=data, format="png"))
