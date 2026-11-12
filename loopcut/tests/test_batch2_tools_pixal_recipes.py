"""Unit tests for Batch 2: Items 5, 6, 7.

Item 5: Task-sized tool menus with guaranteed discovery path (inspect_api + run_python).
Item 6: Pixal3D refinement is checked in real Blender by loopcut/harness/smoke_check.py.
Item 7: Verified recipes for installed add-ons with signatures, mode, and working examples.
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "addons_core"))

from loopcut import addons, studio


def fake_bpy(**attrs):
    """Blender stub created inside setUp for isolation."""
    module = types.ModuleType("bpy")
    scene_obj = types.SimpleNamespace(
        name="Pixal3D_Imported_Mesh",
        type="MESH",
        data=types.SimpleNamespace(
            vertices=[1, 2, 3, 4],
            edges=[1, 2, 3],
            polygons=[1, 2],
        ),
        matrix_world=types.SimpleNamespace(to_translation=lambda: (0, 0, 0)),
        dimensions=(1.0, 1.0, 1.0),
    )
    context = types.SimpleNamespace(
        mode="OBJECT",
        active_object=scene_obj,
        selected_objects=[scene_obj],
        scene={"objects": [scene_obj]},
        view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=scene_obj), update=lambda: None),
        preferences=types.SimpleNamespace(addons={"zenuv": types.SimpleNamespace()}),
    )
    ops = types.SimpleNamespace(
        object=types.SimpleNamespace(mode_set=lambda **_: None, modifier_add=lambda **_: None),
        mesh=types.SimpleNamespace(select_all=lambda **_: None, delete=lambda **_: None, fill_holes=lambda **_: None),
        uv=types.SimpleNamespace(unwrap=lambda **_: None, pack_islands=lambda **_: None),
        ed=types.SimpleNamespace(undo=lambda: None, undo_push=lambda **_: None),
    )
    module.__dict__.update(
        context=context,
        ops=ops,
        data=types.SimpleNamespace(objects={"Pixal3D_Imported_Mesh": scene_obj}),
    )
    module.__dict__.update(attrs)
    return module


class FakeVector(list):
    def normalized(self):
        return self
    def cross(self, other):
        return FakeVector([0, 0, 1])
    def dot(self, other):
        return 0.0
    def __sub__(self, other):
        return FakeVector([a - b for a, b in zip(self, other)])
    def __add__(self, other):
        return FakeVector([a + b for a, b in zip(self, other)])


def fake_mathutils():
    module = types.ModuleType("mathutils")
    module.Vector = lambda *args: FakeVector(args[0]) if args and isinstance(args[0], (list, tuple)) else FakeVector(args)
    module.Euler = lambda *args: list(args)
    module.Matrix = types.SimpleNamespace(Identity=lambda n: [[1 if i == j else 0 for j in range(n)] for i in range(n)])
    return module


def fake_bmesh():
    module = types.ModuleType("bmesh")
    module.new = lambda: types.SimpleNamespace(from_mesh=lambda *_: None, to_mesh=lambda *_: None, free=lambda: None)
    return module


class Batch2ToolsPixalRecipesTest(unittest.TestCase):
    """Batch 2: Task-sized tool menus, Pixal3D refinement, and verified add-on recipes."""

    def setUp(self):
        self.patch = mock.patch.dict(sys.modules, {
            "bpy": fake_bpy(),
            "mathutils": fake_mathutils(),
            "bmesh": fake_bmesh(),
        })
        self.patch.start()
        self.addCleanup(self.patch.stop)
        import tempfile
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.sidecar = Path(self.tmp_dir.name) / ".test_recovery.json"
        self.sidecar_patch = mock.patch("loopcut.project_memory._recovery_path", return_value=self.sidecar)
        self.sidecar_patch.start()
        self.addCleanup(self.sidecar_patch.stop)

    # -------------------------------------------------------------------------
    # Item 5: Task-Sized Tool Menus with Guaranteed Discovery Path
    # -------------------------------------------------------------------------

    def test_task_menus_list_only_registered_commands(self):
        """A task menu is built from the registry, so every name it offers can be called."""
        registry = {
            "mesh_extrude": {"toolset": "modeling"},
            "quad_remesh": {"toolset": "topology"},
            "sculpt_stroke": {"toolset": "sculpt"},
            "uv_pack": {"toolset": "uv_bake"},
        }
        with mock.patch.object(studio, "_registry", return_value=registry):
            self.assertEqual(studio.task_tool_menu("modeling"), ["mesh_extrude", "quad_remesh"])
            self.assertEqual(studio.task_tool_menu("Sculpting"), ["sculpt_stroke"])
            self.assertIsNone(studio.task_tool_menu("cooking"))
            text = studio.guide(task="uv").text
            self.assertIn("uv_pack", text)
            self.assertNotIn("mesh_extrude", text)
            from loopcut.tools import InputMistake
            with self.assertRaises(InputMistake):
                studio.guide(task="cooking")

    # -------------------------------------------------------------------------
    # Item 7: Verified Recipes for Installed Add-ons
    # -------------------------------------------------------------------------

    def test_verified_addon_recipes(self):
        """Recipes contain signatures, context/mode requirements, and working Python examples."""
        zen_recipe = addons.get_recipe("zenuv")
        self.assertIsNotNone(zen_recipe)
        self.assertEqual(zen_recipe["operator"], "bpy.ops.uv.zenuv_unwrap")
        self.assertEqual(zen_recipe["mode"], "EDIT_MESH")
        self.assertIn("bpy.ops.uv.zenuv_unwrap", zen_recipe["working_example"])
        self.assertIn("bpy.ops.uv.unwrap", zen_recipe["native_fallback"])

        quad_recipe = addons.get_recipe("quad_remesher")
        self.assertIsNotNone(quad_recipe)
        self.assertEqual(quad_recipe["operator"], "bpy.ops.qremesher.remesh")
        self.assertEqual(quad_recipe["mode"], "OBJECT")

        # Test recipes matching installed add-ons
        installed = addons.recipes_for_installed()
        self.assertTrue(any(r["module"] == "zenuv" for r in installed))

    def test_runtime_recipe_validation_in_blender(self):
        """Recipe validation checks real operator existence, RNA types, and mode requirements."""
        # Unregistered operator fails validation
        val_zen = addons.validate_recipe("zenuv")
        self.assertFalse(val_zen["verified"])
        self.assertFalse(val_zen["operator_available"])
        self.assertIn("not available in bpy.ops", val_zen["reason"])

        # Registered operator passes validation
        import bpy
        fake_op = mock.MagicMock()
        fake_op.get_rna_type.return_value = types.SimpleNamespace(properties=[types.SimpleNamespace(identifier="action")])
        fake_op.poll.return_value = True
        bpy.ops.uv.zenuv_unwrap = fake_op

        val_zen_ok = addons.validate_recipe("zenuv")
        self.assertTrue(val_zen_ok["verified"])
        self.assertTrue(val_zen_ok["operator_available"])
        self.assertTrue(val_zen_ok["rna_valid"])
        self.assertIn("action", val_zen_ok["properties"])


if __name__ == "__main__":
    unittest.main()
