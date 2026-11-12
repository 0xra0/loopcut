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
WRITES_FILE = {"export_model"}  # Its `path` is where it writes.
PLAYBOOKS = Path(__file__).with_name("studio_playbooks")


def _registry():
    # Lazy: importing this registry pulls in bpy, numpy, mathutils and bmesh.
    from .studio_vendor.handlers import COMMANDS
    return {name: entry for name, entry in COMMANDS.items() if name not in BLOCKED}


def only_reads(command: str) -> bool:
    return isinstance(command, str) and command in READ_ONLY


# A task names the toolsets it needs; its menu lists the commands registered in them.
TASK_TOOLSETS = {
    "modeling": ("modeling", "topology"),
    "sculpting": ("sculpt",),
    "uv": ("uv_bake",),
    "export": ("export",),
}


def task_tool_menu(task: str) -> list[str] | None:
    """The registered commands for a task, or None for a task not in TASK_TOOLSETS."""
    toolsets = TASK_TOOLSETS.get(task.lower().strip())
    if toolsets is None:
        return None
    return sorted(name for name, entry in _registry().items() if entry["toolset"] in toolsets)


def guide(toolset: str = "", command: str = "", playbook: str = "", task: str = ""):
    from .tools import InputMistake, ToolResult
    if task:
        names = task_tool_menu(task)
        if names is None:
            raise InputMistake(f"Unknown task {task!r}. Tasks: {', '.join(TASK_TOOLSETS)}")
        return ToolResult(f"Studio commands for {task}: {', '.join(names)}\n"
                          "Read studio_guide(command=...) before calling one. For an operator outside "
                          "these, inspect_api finds it; run_python does anything else.")
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
    from . import cost_guard, files, live_file, op_notes, scene_diff, shape_key_guard, tools
    if not isinstance(command, str) or command in BLOCKED:
        raise tools.InputMistake("This command is not exposed by studio_action; use Loopcut's native tools.")
    # A file it writes is held to what write_file may write: nothing hidden, nothing of Loopcut's own.
    if command in WRITES_FILE and isinstance(params, dict) and "path" in params:
        params = {**params, "path": str(files.check(files.resolve(str(params["path"]))))}
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
