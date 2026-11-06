"""What Loopcut just added comes into the user's view (0.2.22; rel020-axe: "show the axe framed clearly" but the view
never moved; rel020-pro: the typewriter landed tiny in the middle of the forest, out of sight).

frame() is Frame Selected on the new objects' meshes in each 3D viewport that shows them, from a cloud model's import
(cloud_jobs._model_import) and a library asset's (assets.import_asset). It never moves a camera: a viewport locked to
its camera is unlocked for the move and locked again, and one looking through the camera is left as it is, so the
scene camera and the render are unchanged. Nor does it fight the user: while they move a view (a view of any 3D
viewport changed in the last NAVIGATION_SECONDS, which sample() watches from mainthread's pump) nothing moves.

No bpy at import.
"""

import time

NAVIGATION_SECONDS = 4.0  # A view the user moved this recently means they are looking around: leave it.
SAMPLE_SECONDS = 0.2      # How often the pump's sample() reads the views.
OWN_SECONDS = 1.0         # frame()'s own smooth move, which is not the user's.

_views: dict = {}  # A 3D viewport (its area's pointer) -> its view as last read.
_moved_at = float("-inf")  # time.monotonic() when the user last moved a view.
_own_until = float("-inf")
_sampled_at = float("-inf")


def _view(space) -> tuple | None:
    region = getattr(space, "region_3d", None)
    if region is None:
        return None
    return (tuple(round(value, 4) for row in region.view_matrix for value in row), region.view_perspective)


def sample(now: float | None = None) -> None:
    """Main thread, from mainthread's pump: notes when a 3D view moved that frame() did not move. Never raises."""
    global _moved_at, _sampled_at
    now = time.monotonic() if now is None else now
    if now - _sampled_at < SAMPLE_SECONDS:
        return
    _sampled_at = now
    try:
        import bpy
        views = {}
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    view = _view(area.spaces.active)
                    if view is not None:
                        views[area.as_pointer()] = view
    except Exception:  # No window manager (background, a test), or a screen changing under it.
        return
    if now >= _own_until and any(_views.get(key, view) != view for key, view in views.items()):
        _moved_at = now
    _views.clear()
    _views.update(views)


def navigating(now: float | None = None) -> bool:
    """The user moved a 3D view in the last NAVIGATION_SECONDS."""
    return (time.monotonic() if now is None else now) - _moved_at < NAVIGATION_SECONDS


def frame(objects) -> bool:
    """Best effort: Frame Selected on these objects' meshes in a 3D viewport of this scene that shows them, without
    moving a camera or the user's selection. Never while the user moves a view, in background Blender, or in a
    viewport looking through its camera. True when a view moved."""
    global _own_until
    import bpy
    try:
        meshes = [ob for ob in objects if ob.type == "MESH"]
        if bpy.app.background or not meshes or navigating():
            return False
        for window in bpy.context.window_manager.windows:
            if window.scene != bpy.context.scene or window.view_layer != bpy.context.view_layer:
                continue
            for area in window.screen.areas:
                try:
                    if area.type != "VIEW_3D":
                        continue
                    region = next((r for r in area.regions if r.type == "WINDOW"), None)
                    if region is None:
                        continue
                    space = area.spaces.active
                    if getattr(getattr(space, "region_3d", None), "view_perspective", "") == "CAMERA":
                        continue  # The user looks through the camera: leaving that view would change their shot.
                    with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
                        # Hidden geometry can make view_selected return FINISHED without moving the view.
                        if not any(ob.visible_get(view_layer=window.view_layer, viewport=space) for ob in meshes):
                            continue
                        if not bpy.ops.view3d.view_selected.poll():
                            continue
                        result = _frame_selected(space, window.view_layer, meshes)
                    if "FINISHED" in result:
                        _own_until = time.monotonic() + OWN_SECONDS
                        return True
                except Exception as ex:  # A failed candidate must not prevent trying the next view.
                    print(f"Loopcut: could not frame what was added in this viewport: {ex!r}")
    except Exception as ex:  # UI context can disappear while a cloud job is finishing.
        print(f"Loopcut: could not frame what was added: {ex!r}")
    return False


def _frame_selected(space, view_layer, meshes):
    """view_selected on the meshes alone, the selection as it was afterwards, a locked camera never moved. An offset
    import can leave its root or armature origin far from the geometry, so only meshes are framed."""
    import bpy
    locked = space.lock_camera
    selected = [ob for ob in view_layer.objects if ob.select_get()]
    try:
        space.lock_camera = False
        for ob in selected:
            ob.select_set(False)
        for ob in meshes:
            ob.select_set(True)  # One outside the view layer refuses: this view is given up, the selection put back.
        # EXEC_DEFAULT frames immediately, before camera lock is restored.
        return bpy.ops.view3d.view_selected("EXEC_DEFAULT", use_all_regions=False)
    finally:
        for ob, value in [*((ob, False) for ob in meshes), *((ob, True) for ob in selected)]:
            try:
                ob.select_set(value)
            except (RuntimeError, ReferenceError):
                pass
        space.lock_camera = locked
