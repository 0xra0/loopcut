"""Keeps the conversation on screen in step with the file that is open."""

import bpy
from bpy.app.handlers import persistent

from . import agent, conversations, state


def switch_to(session: dict) -> None:
    """Make `session` the one on screen. The outgoing one is stopped and saved first."""
    agent.stop()  # A turn must not carry on into a different scene.
    agent.end_click_asks(state.session()["id"])  # Nor wait off screen on a Turn it into 3D click's ask.
    conversations.save(state.session())
    state.replace(session)
    state.ui["view"] = "chat"
    from . import cloud_restore
    try:
        cloud_restore.pickup(session)  # Its cloud jobs, back behind job_status; a finished one gets its card.
    except Exception:  # Never a reason the conversation does not open.
        import traceback
        traceback.print_exc()
    from .ui import host
    host.tag_redraw_all()


def resume_for_open_file() -> None:
    from . import files
    current = state.session()
    filepath = "" if files.is_untitled_recovery(bpy.data.filepath) else bpy.data.filepath
    project = conversations.project_key(filepath)
    if current["items"] and project in current["projects"]:
        # Already showing a conversation for this file, e.g. after a hot reload, or opened from History while
        # another file was open: its cloud jobs for this file are followed now (cloud_restore.pickup, once each).
        from . import cloud_restore
        try:
            cloud_restore.pickup(current)
        except Exception:  # Never a reason the file does not open.
            import traceback
            traceback.print_exc()
        return
    switch_to(conversations.resume_or_new(filepath))


@persistent
def _on_load_post(*args):
    if state.restoring:  # checkpoints.restore loads a file too; that is not the user switching files.
        _on_undo_redo()  # But what it loaded may lack a cloud job's outputs, or have them back.
        return
    from pathlib import Path
    from . import files
    loaded = next((a for a in args if isinstance(a, str) and a), "")
    current_path = getattr(getattr(bpy, "data", None), "filepath", "")
    # Blender retains the temp filename when an untitled recovery has no original path.
    untitled_temp = bool(loaded and Path(loaded).resolve() == Path(current_path).resolve()
                         and files.is_untitled_recovery(loaded))
    recovered = untitled_temp or bool(loaded and Path(loaded).suffix.lower() == ".blend" and Path(loaded).name != "startup.blend"
                     and conversations.project_key(loaded) != conversations.project_key(current_path))
    try:
        scene = conversations.new_scene()
        if recovered:
            # A saved project can only recover its own chat. Untitled recovery has no project key.
            project_path = "" if untitled_temp else current_path
            matches = conversations.list_for(conversations.project_key(project_path)) if project_path else []
            previous = state.session()
            if (not project_path and previous["items"] and not previous["projects"]
                    and previous.get("untitled_scene") == scene):
                recovered_chat = previous
            else:
                candidate = ((matches[0] if matches else None) if project_path else
                             conversations.recovery_candidate(interrupted_only=False, untitled_scene=scene))
                recovered_chat = conversations.load(candidate["id"]) if candidate else None
            if recovered_chat is not None:
                conversations.attach_untitled(recovered_chat, project_path)
                conversations.add_project(recovered_chat, project_path)
                conversations.save(recovered_chat)
                switch_to(recovered_chat)
            else:
                resume_for_open_file()
        else:
            resume_for_open_file()
    except Exception:
        import traceback
        traceback.print_exc()
        resume_for_open_file()
    # File > Open, Open Recent, File > New: the file's own layout replaced the screen, usually without the panel.
    if not bpy.app.timers.is_registered(_dock_after_load):
        bpy.app.timers.register(_dock_after_load, first_interval=0.0)


@persistent
def _on_undo_redo(*_):
    """Undo after a cloud job's Load takes its outputs out of the file: its card offers Load again (Redo: loaded)."""
    from . import cloud_restore
    try:
        cloud_restore.recheck(state.session())
    except Exception:  # Never a reason an undo step fails.
        import traceback
        traceback.print_exc()


_saving_from = ""  # The file's path as a save starts (_on_save_pre), for Save As.


@persistent
def _on_save_pre(*_):
    global _saving_from
    _saving_from = bpy.data.filepath
    if not _saving_from:
        conversations.bind_scene()


@persistent
def _on_save_post(*_):
    if state.restoring:
        return
    # Save As, or the first save of an untitled scene: the conversation follows the file, and so do its cloud jobs
    # whose outputs are not in it yet (cloud_restore.file_saved).
    session = state.session()
    from . import files
    if not _saving_from or files.is_untitled_recovery(_saving_from):
        conversations.attach_untitled(session, bpy.data.filepath)
    conversations.add_project(session, bpy.data.filepath)
    from . import cloud_restore
    try:
        cloud_restore.file_saved(session, _saving_from, bpy.data.filepath)
    except Exception:  # Never a reason a save is not recorded.
        import traceback
        traceback.print_exc()
    conversations.save(session)


def _dock_if_missing() -> None:
    """The Loopcut build shows the panel at startup and with every file opened, docked only if the layout
    that loaded has none: a file saved with the panel keeps its own. Stock Blender is left as the user arranged it."""
    from . import settings
    from .ui import host
    prefs = settings.preferences()
    if not host.NATIVE or bpy.app.background or prefs is None or not prefs.dock_on_startup:
        return
    windows = bpy.context.window_manager.windows
    if windows and not any(host.has_panel(w) for w in windows):
        try:
            host.dock(windows[0])
        except RuntimeError as ex:
            print(f"Loopcut: {ex}")


def _dock_after_load():
    # From a timer, as at startup: outside the file read, with the loaded screen up.
    _dock_if_missing()
    return None


def _initial_resume():
    # From a timer: bpy.data is not readable while an add-on is still registering.
    # Saved System add-ons must have finished loading before repairing incidental prefs saves.
    from . import bundled_addons, conversations
    try:
        bundled_addons.startup_cleanup()
    except (bundled_addons.BundledAddonError, OSError, ValueError):
        import traceback
        traceback.print_exc()  # Keep the journal, and leave the panel usable for recovery.
    conversations.new_scene()
    resume_for_open_file()
    try:
        from . import checkpoints
        checkpoints.schedule_cleanup()
    except Exception as ex:
        print(f"Loopcut: could not start storage cleanup: {ex}")
    _dock_if_missing()
    return None


def _poll_jobs():
    """Each step on its own: one that raises (an OSError listing jobs) neither skips the others nor ends the timer,
    which only its return keeps registered."""
    import traceback
    from . import job_tools, workspace
    from .ui import host
    # poll_left: what a shell command left running, ended or due a Stop's SIGKILL. sync_topbars: "Open Loopcut"
    # follows a panel closed through Blender's UI.
    for step in (job_tools.refresh_ui, workspace.poll_left, host.tag_redraw_all, host.sync_topbars):
        try:
            step()
        except Exception:
            traceback.print_exc()
    return 1.0


@persistent
def _on_exit(*_):
    from . import workspace
    workspace.stop_all_left()  # Blender quits: what a shell command left running goes with it.


def _handlers() -> tuple:
    from . import files
    return ((bpy.app.handlers.load_post, _on_load_post), (bpy.app.handlers.save_pre, _on_save_pre),
            (bpy.app.handlers.save_post, _on_save_post), (bpy.app.handlers.render_complete, files.on_render_complete),
            (bpy.app.handlers.undo_post, _on_undo_redo), (bpy.app.handlers.redo_post, _on_undo_redo),
            (bpy.app.handlers.exit_pre, _on_exit))


def register() -> None:
    from . import inspection
    inspection.register()
    for handlers, fn in _handlers():
        handlers.append(fn)
    bpy.app.timers.register(_initial_resume, first_interval=0.0, persistent=True)
    bpy.app.timers.register(_poll_jobs, first_interval=1.0, persistent=True)


def unregister() -> None:
    from . import inspection
    inspection.unregister()
    if bpy.app.timers.is_registered(_poll_jobs):
        bpy.app.timers.unregister(_poll_jobs)
    for timer in (_initial_resume, _dock_after_load):
        if bpy.app.timers.is_registered(timer):  # Disabled before its first tick.
            bpy.app.timers.unregister(timer)
    conversations.save(state.session())
    for handlers, fn in _handlers():
        if fn in handlers:
            handlers.remove(fn)
