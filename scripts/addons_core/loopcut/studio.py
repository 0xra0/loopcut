"""Native Loopcut integration for rotembab/claude-blender-integration (MIT).

The structured Blender handlers execute IN PROCESS on Loopcut's existing main-thread
agent dispatch.  No extra socket is opened, and Claude's MCP Python executor and
independent file checkpoints are deliberately not exposed (Loopcut already has these,
with approval, timeout, undo and checkpoint safeguards).
"""

import base64
import contextlib
import inspect
import json
import tempfile
from pathlib import Path

# These tools add a discoverable structured API without paying for 70 schemas on every turn.
SCHEMAS = [
    {"type": "function", "function": {
        "name": "studio_guide", "title": "Exploring 3D studio tools",
        "description": ("Discover advanced Blender operations for sculpting, retopology, UVs, baking, "
                        "rigging, character animation, references and game exports, and read staged build "
                        "playbooks with acceptance checks. Call with task for concise task-sized menus "
                        "('modeling', 'sculpting', 'uv', 'export'), toolset for commands in one group, "
                        "command for its exact parameters, or playbook for a workflow. Then call studio_action."),
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "Concise task-sized menu: 'modeling', 'sculpting', 'uv', or 'export'"},
            "toolset": {"type": "string", "description": "One toolset from the overview, e.g. sculpt, topology, uv_bake"},
            "command": {"type": "string", "description": "Specific command's Python signature and usage"},
            "playbook": {"type": "string", "description": "Playbook name, or 'list' for available playbooks"},
        }},
    }},
    {"type": "function", "function": {
        "name": "studio_action", "title": "Editing with 3D studio tools",
        "description": ("Execute one structured command from studio_guide in the live Blender scene. "
                        "Use studio_guide(command=...) to discover exact argument names. "
                        "Mutations require Loopcut approval, get a Loopcut checkpoint and an undo step; "
                        "the result includes the measured scene changes. For custom Python use Loopcut's "
                        "run_python, NOT this tool. For viewing use capture_viewport. Some complex "
                        "commands require additional local dependencies."),
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "Command name, e.g. sculpt_stroke, mesh_report, "
                        "quad_remesh, uv_report, rig_template, export_model"},
            "params": {"type": "object", "description": "Keyword arguments from studio_guide(command=...)"},
            "summary": {"type": "string", "description": "Brief description of the action for the user"},
        }, "required": ["command", "params", "summary"]},
    }},
]

# Loopcut's run_python, checkpoints and previews replace the duplicate upstream APIs.
BLOCKED = {"run_python", "checkpoint", "list_checkpoints", "rollback", "ping", "list_commands",
           "capture_views", "compare_to_reference"}
READ_ONLY = {"scene_state", "object_details", "mesh_report", "uv_report", "rig_report",
             "list_actions", "list_references", "reference_to_world", "surface_points",
             "operator_inspect", "operator_search", "geonodes_inspect", "geonodes_evaluated_metrics",
             "uv_texel_density",
             "diagnose_geometry", "project_get_memory", "gpu_get_vram_status", "knowledge_search"}
# These may take Blender out of the UI for a significant time, or bake/render data.
HEAVY = {"voxel_remesh", "quad_remesh", "decimate", "sculpt_prepare", "sculpt_stroke",
         "bake_maps", "pose_test", "bind_mesh",
         "rigify_generate", "silhouette_mesh", "reimport_check", "export_model",
         "quad_remesh_op", "reproject_shrinkwrap_op", "render_configure_engine", "sim_add_cloth"}
# UniRig and Pixal3D launch scripts in a subprocess: never waive their per-call approval.
EXTERNAL_EXECUTION = {"auto_rig_unirig", "image_to_3d", "pixal3d_generate"}
PLAYBOOKS = Path(__file__).with_name("studio_playbooks")


def _registry():
    # Lazy: importing this registry pulls in bpy, numpy, mathutils and bmesh.
    from .studio_vendor.handlers import COMMANDS
    return {name: entry for name, entry in COMMANDS.items() if name not in BLOCKED}


def only_reads(command: str) -> bool:
    return isinstance(command, str) and command in READ_ONLY


TASK_MENUS = {
    "modeling": {
        "task": "modeling",
        "description": "Safe, structured mesh modeling and topology operations",
        "tools": [
            "extrude", "inset", "bevel", "boolean", "mirror", "solidify",
            "merge", "separate", "knife", "bridge", "mesh_report"
        ],
        "always_available": {
            "inspect_api": "Inspect any bpy.ops / bpy.types operator or property signature",
            "run_python": "Execute direct Python script in Blender's main thread",
            "capture_viewport": "Rendered or solid viewport visual capture",
        },
    },
    "sculpting": {
        "task": "sculpting",
        "description": "Fast regional sculpting and thin-shell surface protection",
        "tools": [
            "sculpt_prepare", "sculpt_stroke", "sculpt_smooth", "sculpt_mask", "thin_shell_guard"
        ],
        "always_available": {
            "inspect_api": "Inspect any bpy.ops / bpy.types operator or property signature",
            "run_python": "Execute direct Python script in Blender's main thread",
            "capture_viewport": "Rendered or solid viewport visual capture",
        },
    },
    "uv": {
        "task": "uv",
        "description": "Seams, unwrapping, packing, texel density, and PBR baking",
        "tools": [
            "uv_smart_project", "uv_unwrap", "uv_pack", "uv_seams", "uv_texel_density", "bake_maps", "uv_report"
        ],
        "always_available": {
            "inspect_api": "Inspect any bpy.ops / bpy.types operator or property signature",
            "run_python": "Execute direct Python script in Blender's main thread",
            "capture_viewport": "Rendered or solid viewport visual capture",
        },
    },
    "export": {
        "task": "export",
        "description": "Game engine export, LOD validation, and asset round-trip checks",
        "tools": [
            "export_model", "validate_export", "reimport_check", "texture_pack"
        ],
        "always_available": {
            "inspect_api": "Inspect any bpy.ops / bpy.types operator or property signature",
            "run_python": "Execute direct Python script in Blender's main thread",
            "capture_viewport": "Rendered or solid viewport visual capture",
        },
    },
}


def task_tool_menu(task: str = "") -> dict:
    """Return concise task-sized tool menu with guaranteed inspect_api and raw bpy access."""
    norm = task.lower().strip()
    if norm in ("sculpt", "sculpting"):
        return TASK_MENUS["sculpting"]
    if norm in ("uv", "uvs", "uv_bake", "texturing"):
        return TASK_MENUS["uv"]
    if norm in ("model", "modeling", "mesh"):
        return TASK_MENUS["modeling"]
    if norm in ("export", "engine_export"):
        return TASK_MENUS["export"]
    if norm and norm in TASK_MENUS:
        return TASK_MENUS[norm]
    return {
        "task": "overview",
        "description": "Selectable task-sized tool menus",
        "available_tasks": list(TASK_MENUS.keys()),
        "always_available": {
            "inspect_api": "Inspect any bpy.ops / bpy.types operator or property signature",
            "run_python": "Execute direct Python script in Blender's main thread",
            "capture_viewport": "Rendered or solid viewport visual capture",
        },
    }


def guide(toolset: str = "", command: str = "", playbook: str = "", task: str = ""):
    from .tools import InputMistake, ToolResult
    if task:
        menu = task_tool_menu(task)
        if menu.get("task") == "overview":
            return ToolResult(f"Available task menus: {', '.join(menu['available_tasks'])}\n"
                              "Guaranteed core tools: inspect_api, run_python, capture_viewport.\n"
                              "Call studio_guide(task='modeling'|'sculpting'|'uv'|'export').")
        tools_str = ", ".join(menu.get("tools", []))
        anchors_str = "\n".join(f"- {k}: {v}" for k, v in menu.get("always_available", {}).items())
        return ToolResult(f"Task Menu [{menu['task'].upper()}]: {menu['description']}\n\n"
                          f"Concise task commands:\n{tools_str}\n\n"
                          f"Guaranteed core tools (always available):\n{anchors_str}\n\n"
                          "Call studio_guide(command=...) for parameter details, then studio_action.")
    if playbook:
        names = sorted(p.stem for p in PLAYBOOKS.glob("*.md"))
        if playbook == "list":
            return ToolResult("Available build playbooks: " + ", ".join(names))
        if playbook not in names:
            raise InputMistake(f"Unknown playbook {playbook!r}. Available: {', '.join(names)}")
        return ToolResult(f"Studio playbook: {playbook}\n"
                          "Loopcut adaptation: checkpoints are automatic before edits; use capture_viewport "
                          "(focus=... instead of objects=...) instead of upstream capture_views; "
                          "use compare_with_reference instead of compare_to_reference. "
                          "Use studio_guide(command=...) for exact structured handler inputs.\n\n"
                          + (PLAYBOOKS / f"{playbook}.md").read_text())
    registry = _registry()
    if command:
        if not isinstance(command, str):
            raise InputMistake("command must be a name from studio_guide")
        entry = registry.get(command)
        if entry is None:
            raise InputMistake(f"Unknown/disabled studio command {command!r}. Try studio_guide(toolset=...).")
        signature = inspect.signature(entry["fn"])
        return ToolResult(f"{command}{signature} [{entry['toolset']}]\n\n{entry['doc']}\n\n"
                          f"Scene changes: {'no' if only_reads(command) else 'possible'}; "
                          f"extra approval: {'yes' if command in HEAVY | EXTERNAL_EXECUTION else 'no'}.\n"
                          "Call studio_action(command=..., params={...}, summary=...).")
    groups = {}
    for name, entry in sorted(registry.items()):
        if toolset and entry["toolset"] != toolset:
            continue
        groups.setdefault(entry["toolset"], []).append(name)
    if toolset and not groups:
        raise InputMistake(f"Unknown studio toolset {toolset!r}. Available: "
                           + ", ".join(sorted({v['toolset'] for v in registry.values()})))
    return ToolResult("Studio commands (read studio_guide(command=...) before calling one):\n"
                      + "\n".join(f"{k}: {', '.join(v)}" for k, v in sorted(groups.items()))
                      + "\nBuild workflows: studio_guide(playbook='list').")


def _images(value):
    """Convert the upstream MCP image payload to Loopcut's own ToolResult image format.

    Never put megabytes of base64 in a model reply or activity card.
    """
    image = None
    if isinstance(value, dict):
        value = dict(value)
        if isinstance(value.get('png_base64'), str):
            image = value.pop('png_base64')
        pictures = value.get('images')
        if isinstance(pictures, list):
            compact = []
            for entry in pictures:
                if not isinstance(entry, dict):
                    compact.append(entry)
                    continue
                item = dict(entry)
                encoded = item.pop('png_base64', None)
                if image is None and isinstance(encoded, str):
                    image = encoded
                if encoded is not None:
                    item['image_attached'] = encoded == image
                compact.append(item)
            value['images'] = compact
    path = None
    if image is not None:
        decoded = base64.b64decode(image, validate=True)
        if len(decoded) > 20 * 1024 * 1024 or not decoded.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('Invalid or oversized studio preview PNG')
        with tempfile.NamedTemporaryFile(prefix='loopcut_studio_', suffix='.png', delete=False) as f:
            f.write(decoded)
            path = Path(f.name)
    return value, path


def action(command: str, params: dict, summary: str):
    """Dispatch a native handler with Loopcut's existing guardrails and audit trail."""
    from . import cost_guard, live_file, op_notes, scene_diff, shape_key_guard, tools
    if not isinstance(command, str) or command in BLOCKED:
        raise tools.InputMistake("This command is not exposed by studio_action; use Loopcut's native tools.")
    registry = _registry()
    entry = registry.get(command)
    if entry is None:
        raise tools.InputMistake(f"Unknown studio command {command!r}. Use studio_guide to find valid commands.")
    if not isinstance(params, dict):
        raise tools.InputMistake("params must be a JSON object of keyword arguments")
    if not isinstance(summary, str) or not summary.strip():
        raise tools.InputMistake("summary is required")
    try:
        inspect.signature(entry['fn']).bind(**params)
    except TypeError as ex:
        raise tools.InputMistake(f"Bad {command} parameters: {ex}. Read studio_guide(command='{command}').") from ex

    # Upstream dispatch updates transforms before each command, do the same here.
    try:
        import bpy
        bpy.context.view_layer.update()
    except (AttributeError, RuntimeError):
        pass

    # Read-only calls intentionally do not push undo or checkpoint. Every other command
    # is a mutation, even when a scene snapshot misses UV, keyframe or file changes.
    if only_reads(command):
        try:
            value, picture = _images(entry['fn'](**params))
            return tools.ToolResult(json.dumps(value, ensure_ascii=False, default=str)[:12000], image_path=picture)
        except Exception as ex:
            return tools.ToolResult(f"Studio {command} failed: {type(ex).__name__}: {ex}", ok=False)

    before = scene_diff.snapshot()
    tools.undo_before(f"Studio: {summary[:45]}")
    cost_guard.begin()
    shape_key_guard.install()
    failed = None
    value = None
    notices = []
    try:
        with (tools._preserve_host_areas() as host_notices, tools._editor_context(""),
              cost_guard.watching_ops(), op_notes.watching_ops(), live_file.watching_ops()):
            value = entry['fn'](**params)
        notices.extend(host_notices)
    except Exception as ex:
        failed = ex
    finally:
        shape_key_guard.check()
        notices.extend(shape_key_guard.take() + op_notes.take() + live_file.take())
    after = scene_diff.snapshot(guard=cost_guard.check)
    guarded, guard_notes = cost_guard.take()
    diff = scene_diff.diff(before, after)
    changes = scene_diff.for_model(diff)
    try:
        value, picture = _images(value)
    except Exception as ex:
        failed = failed or ex
        picture = None
    description = (json.dumps(value, ensure_ascii=False, default=str) if value is not None else '')
    if failed:
        description = f"Studio {command} failed after execution: {type(failed).__name__}: {failed}\n" + description
    if len(description) > 11000:
        description = description[:10980] + '…'
    extra = '\n'.join(str(n) for n in notices)
    output = '\n\n'.join(s for s in (description or 'OK', extra, guarded + changes) if s)
    return tools.ToolResult(output, image_path=picture, ok=failed is None,
                            scene_before=before, scene_after=after, guard_notes=guard_notes)


DISPATCH = {"studio_guide": guide, "studio_action": action}
