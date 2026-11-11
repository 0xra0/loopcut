"""view_follow: what Loopcut adds comes into the user's 3D view (rel020-axe: "show the axe framed clearly" but the view
never moved; rel020-pro: the typewriter landed tiny in the forest), never by moving a camera and never while the user
moves the view. A cloud model import's own framing cases are in test_cloud_jobs.ModelImportTest."""

import contextlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))  # The libraries import it; each test has its own below.
from loopcut import assets, tools, view_follow  # noqa: E402

AWAY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, -40], [0, 0, 0, 1]]


class Thing:
    def __init__(self, name, type="MESH", selected=False):
        self.name, self.type, self.selected = name, type, selected
        self.matrix_world = [[1, 0, 0, 5], [0, 1, 0, -8], [0, 0, 1, 2], [0, 0, 0, 1]]

    def select_get(self):
        return self.selected

    def select_set(self, value):
        self.selected = bool(value)

    def visible_get(self, view_layer=None, viewport=None):
        return True

    def as_pointer(self):
        return id(self)


class ViewFollowTest(unittest.TestCase):
    def setUp(self):
        self.camera = Thing("Camera", "CAMERA")
        self.cube = Thing("Cube", selected=True)  # What the user had selected.
        self.layer = types.SimpleNamespace(objects=[self.camera, self.cube])
        scene = types.SimpleNamespace(camera=self.camera)
        self.view = types.SimpleNamespace(view_matrix=[list(row) for row in AWAY], view_perspective="PERSP")
        self.space = types.SimpleNamespace(lock_camera=True, region_3d=self.view)
        area = types.SimpleNamespace(type="VIEW_3D", regions=[types.SimpleNamespace(type="WINDOW")],
                                     spaces=types.SimpleNamespace(active=self.space), as_pointer=lambda: 7)
        window = types.SimpleNamespace(scene=scene, view_layer=self.layer, screen=types.SimpleNamespace(areas=[area]))
        self.framed = []

        def view_selected(*args, **kwargs):
            self.framed.append(([ob.name for ob in self.layer.objects if ob.selected], self.space.lock_camera))
            self.view.view_matrix = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, -3], [0, 0, 0, 1]]  # It moved.
            return {"FINISHED"}
        operator = mock.Mock(side_effect=view_selected)
        operator.poll = mock.Mock(return_value=True)
        self.bpy = types.SimpleNamespace(
            app=types.SimpleNamespace(background=False), data=types.SimpleNamespace(objects=list(self.layer.objects)),
            context=types.SimpleNamespace(window_manager=types.SimpleNamespace(windows=[window]), scene=scene,
                                          view_layer=self.layer, temp_override=lambda **_: contextlib.nullcontext()),
            ops=types.SimpleNamespace(view3d=types.SimpleNamespace(view_selected=operator)))
        for patcher in (mock.patch.dict(sys.modules, {"bpy": self.bpy}),
                        mock.patch.multiple(view_follow, _views={}, _moved_at=float("-inf"),
                                            _own_until=float("-inf"), _sampled_at=float("-inf"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def add(self, name):
        """An import's new mesh, in the file and the view layer, as a library import leaves it (not selected)."""
        thing = Thing(name)
        self.layer.objects.append(thing)
        self.bpy.data.objects.append(thing)
        return thing

    def test_frames_the_new_meshes_alone_and_leaves_the_selection_and_camera_as_they_were(self):
        typewriter, root = self.add("Typewriter"), Thing("Typewriter root", "EMPTY")
        self.layer.objects.append(root)
        self.assertTrue(view_follow.frame([root, typewriter]))
        self.assertEqual(self.framed, [(["Typewriter"], False)], "only its mesh, the camera lock off for the move")
        self.assertTrue(self.space.lock_camera, "and on again")
        self.assertEqual([ob.name for ob in self.layer.objects if ob.selected], ["Cube"], "the user's selection")
        self.assertEqual(self.camera.matrix_world[0][3], 5, "the scene camera never moves")

    def test_a_mesh_that_refuses_selection_gives_up_that_view_and_the_selection_comes_back(self):
        typewriter = self.add("Typewriter")
        def refuse(value):
            raise RuntimeError("Object 'Typewriter' can't be selected because it is not in View Layer")
        typewriter.select_set = refuse
        self.assertFalse(view_follow.frame([typewriter]))
        self.assertEqual(self.framed, [])
        self.assertEqual([ob.name for ob in self.layer.objects if ob.selected], ["Cube"])
        self.assertTrue(self.space.lock_camera)

    def test_not_while_the_user_moves_the_view_and_its_own_move_is_not_the_users(self):
        typewriter = self.add("Typewriter")
        start = view_follow.time.monotonic()
        view_follow.sample(start)
        self.view.view_matrix = [[0, -1, 0, 0], [1, 0, 0, 0], [0, 0, 1, -40], [0, 0, 0, 1]]  # The user orbits.
        view_follow.sample(start + .3)
        self.assertTrue(view_follow.navigating(start + 1))
        self.assertFalse(view_follow.frame([typewriter]), "a view moved a moment ago: leave it")
        self.assertEqual(self.framed, [])
        later = start + .3 + view_follow.NAVIGATION_SECONDS + 1
        with mock.patch.object(view_follow.time, "monotonic", return_value=later):
            self.assertTrue(view_follow.frame([typewriter]), "once they have stopped")
            view_follow.sample(later + .5)  # Its own smooth move is under way.
        self.assertFalse(view_follow.navigating(later + 1), "Loopcut's move is not the user's")
        self.assertEqual(len(self.framed), 1)

    def test_not_in_a_view_through_the_camera_nor_in_background_blender(self):
        typewriter = self.add("Typewriter")
        self.view.view_perspective = "CAMERA"
        self.assertFalse(view_follow.frame([typewriter]), "the user's shot stays their shot")
        self.view.view_perspective = "PERSP"
        self.bpy.app.background = True
        self.assertFalse(view_follow.frame([typewriter]))
        self.assertEqual(self.framed, [])

    def test_a_library_import_frames_what_it_added(self):
        """assets.import_asset, every library's import (rel020-axe's BlenderKit axe, rel020-pro's typewriter)."""
        def imported(source, asset_id, pick, call):
            self.add("Old Fasioned Type Writer")
            return tools.ToolResult("Imported 'Old Fasioned Type Writer' as 1 object(s) at the origin."), ""
        with mock.patch.object(assets, "_meant", imported):
            result = assets.import_asset(source="blenderkit", id="typewriter")
        self.assertTrue(result.ok)
        self.assertEqual(self.framed, [(["Old Fasioned Type Writer"], False)])
        self.assertEqual([ob.name for ob in self.layer.objects if ob.selected], ["Cube"])

        def refused(source, asset_id, pick, call):
            return tools.ToolResult("Could not download it.", ok=False), ""
        with mock.patch.object(assets, "_meant", refused):
            self.assertFalse(assets.import_asset(source="blenderkit", id="typewriter").ok)
        self.assertEqual(len(self.framed), 1, "nothing added, nothing framed")


if __name__ == "__main__":
    unittest.main()
