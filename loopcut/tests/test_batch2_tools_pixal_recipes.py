"""Unit tests for Batch 2: Items 5, 6, 7.

Item 5: Task-sized tool menus with guaranteed discovery path (inspect_api + run_python).
Item 6: Pixal3D refinement workflow with staged measurement, checkpoints, and acceptance checks.
Item 7: Verified recipes for installed add-ons with signatures, mode, and working examples.
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock
import numpy as np

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

    # -------------------------------------------------------------------------
    # Item 5: Task-Sized Tool Menus with Guaranteed Discovery Path
    # -------------------------------------------------------------------------

    def test_task_tool_menus_guaranteed_discovery_path(self):
        """Task menus return domain tools while guaranteeing inspect_api and raw bpy access."""
        for task in ("modeling", "sculpting", "uv", "export"):
            menu = studio.task_tool_menu(task)
            self.assertEqual(menu["task"], task)
            self.assertGreater(len(menu["tools"]), 0)

            # Guaranteed discovery anchors must always be present
            anchors = menu.get("always_available", {})
            self.assertIn("inspect_api", anchors)
            self.assertIn("run_python", anchors)
            self.assertIn("capture_viewport", anchors)

        # Calling studio_guide with task returns formatted menu text
        res = studio.guide(task="modeling")
        self.assertIn("inspect_api", res.text)
        self.assertIn("run_python", res.text)
        self.assertIn("extrude", res.text)

    # -------------------------------------------------------------------------
    # Item 6: Pixal3D Refinement Workflow
    # -------------------------------------------------------------------------

    def test_pixal3d_refine_staged_repair_and_acceptance(self):
        """Pixal3D refinement measures mesh flaws and executes staged repair with acceptance check."""
        from loopcut.studio_vendor.handlers import pixal3d

        # Mock geometry measurements for staged repair
        meas_before = {
            "islands": 5,
            "non_manifold_edges": 12,
            "min_thickness": 0.001,
            "symmetry_residual": 0.045,
            "bbox_dimensions": [0.8, 0.4, 1.2],
        }
        meas_after = {
            "islands": 1,
            "non_manifold_edges": 0,
            "min_thickness": 0.008,
            "symmetry_residual": 0.002,
            "bbox_dimensions": [0.8, 0.4, 1.2],
        }

        with mock.patch("loopcut.studio_vendor.handlers.pixal3d._measure_pixal3d_flaws", side_effect=[meas_before, meas_after]):
            with mock.patch("loopcut.studio_vendor.handlers.pixal3d._apply_refinement_stages", return_value=["clean_loose", "fix_non_manifold", "solidify_thin_shells", "symmetrize"]):
                report = pixal3d.pixal3d_refine("Pixal3D_Imported_Mesh", target_symmetry=True, min_thickness=0.005)
                self.assertIn("initial_diagnostics", report)
                self.assertIn("final_diagnostics", report)
                self.assertIn("stages_completed", report)
                self.assertTrue(report["acceptance_passed"])
                self.assertEqual(report["final_diagnostics"]["non_manifold_edges"], 0)
                self.assertEqual(report["final_diagnostics"]["islands"], 1)
                self.assertGreaterEqual(report["final_diagnostics"]["min_thickness"], 0.005)

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


if __name__ == "__main__":
    unittest.main()
