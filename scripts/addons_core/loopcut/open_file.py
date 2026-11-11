"""Switch the live document after explicit consent, saving existing work first."""

from pathlib import Path

import bpy

SCHEMA = {"type": "function", "function": {
    "name": "open_file",
    "title": "Opening a file",
    "description": "Save scene changes and open a .blend with consent. Ends this turn.",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string"},
    }, "required": ["path"]},
}}


def describe(arguments: dict) -> tuple[str, str]:
    from . import files
    from .tools import ToolError
    try:
        raw = arguments.get("path")
        target = str(Path(raw) if raw and not str(raw).startswith("//") and Path(raw).is_absolute() else files.resolve(raw))
    except (ToolError, OSError, ValueError):
        target = "(invalid path)"
    return (f"Save scene changes and open {Path(target).name}",
            f"Open: {target}\n"
            "Scene changes are saved first. Image edits need saving or packing first. "
            "The conversation switches to the opened file.")


def _check(target: Path) -> Path:
    from . import files, ownership, workspace
    from .tools import ToolError
    if (workspace.inside(target) is not None
            or files.inside(target, (*files._forbidden_roots(), *files._temp_roots()))):
        raise ToolError("Open a .blend saved in a permanent project folder, outside Loopcut's data, workspaces and temporary folders.")
    target = files.check(target)
    if target.suffix.lower() != ".blend" or not target.is_file():
        raise ToolError("Choose an existing .blend file to open.")
    dirty = [image.name for image in bpy.data.images if image.is_dirty and not image.get(ownership.TEMP)]
    if dirty:
        raise ToolError(f"Images need saving or packing before opening another file: {', '.join(dirty)}.")
    current = bpy.data.filepath
    if not current and bpy.data.is_dirty:
        raise ToolError("Use File > Save As to save the untitled scene first, then ask to open the other file again.")
    if current and target == Path(current).resolve():
        raise ToolError("That file is already open.")
    return target


def prepare(path: str) -> Path:
    """Resolve and check before consent; the card and dispatch use this absolute path."""
    from . import files
    return _check(files.resolve(path))


def open_file(path: str):
    from .tools import ToolError, ToolResult
    target = Path(path)
    if not str(path).startswith("//") and target.is_absolute():
        # Recheck safety without redirecting an approved path if a symlink changed while waiting.
        checked = _check(target.resolve())
        if target != checked:
            raise ToolError("The file path changed. Ask to open it again so the new path can be approved.")
    else:
        target = prepare(path)
    if bpy.data.is_dirty:
        try:
            saved = bpy.ops.wm.save_mainfile()
        except RuntimeError as ex:
            raise ToolError(f"Could not save the current file; the other file was not opened: {ex}") from ex
        if "FINISHED" not in saved:
            raise ToolError("Could not save the current file; the other file was not opened.")
    try:
        opened = bpy.ops.wm.open_mainfile(filepath=str(target), load_ui=False)
    except RuntimeError as ex:
        raise ToolError(f"Could not open {target.name}: {ex}") from ex
    if "FINISHED" not in opened:
        raise ToolError(f"Could not open {target.name}; the file operation was cancelled.")
    return ToolResult(f"Opened {target.name}. The conversation now follows that file.")
