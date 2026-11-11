"""Comprehensive unit tests for Loopcut's 35-item Local AI Integration and Advanced Studio Suite."""

import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock
import numpy as np

# Ensure scripts/addons_core is in path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "addons_core"))

from loopcut import (
    strata, thinking, project_memory, privacy,
    operator_registry, fast_sculpt, shell_guard, hard_surface,
    retopo_tools, uv_controller, geonodes_controller, rig_anim_controller,
    render_sim_controller, addon_integrations,
    semantic_mesh, geometry_diagnostics, acceptance_criteria,
    transaction, resumable_task,
    gpu_coordinator, context_compression, knowledge_base, headless_suite,
    benchmark_dashboard, studio
)


def fake_bpy(**attrs):
    """A bpy stub for one test, installed with mock.patch.dict(sys.modules, {"bpy": ...})."""
    module = types.ModuleType("bpy")
    module.__dict__.update(context=types.SimpleNamespace(mode="OBJECT", scene={}, preferences=types.SimpleNamespace(addons={})),
                           ops=types.SimpleNamespace(ed=types.SimpleNamespace(undo=lambda: None, undo_push=lambda **_: None)))
    module.__dict__.update(attrs)
    return module


class FakeBpyTest(unittest.TestCase):
    """Every test sees a bpy stub, never whatever bpy module this Python can import."""

    def setUp(self):
        patch = mock.patch.dict(sys.modules, {"bpy": fake_bpy()})
        patch.start()
        self.addCleanup(patch.stop)


class TestLocalAIIntegration(FakeBpyTest):
    """Items 1 - 8: Local AI Integration."""

    def test_item1_strata_provider_discovery_and_status(self):
        # Discover localhost
        with mock.patch("socket.create_connection", side_effect=ConnectionRefusedError):
            self.assertIsNone(strata.discover_localhost([8099]))

        # Local runner info
        info = strata.get_local_runner_info()
        self.assertIn("model_name", info)
        self.assertIn("backend", info)


    def test_item4_thinking_mode_is_the_reasoning_effort_strata_reads(self):
        self.assertEqual([thinking.reasoning_effort(p) for p in ("fast", "balanced", "deep")], ["low", "medium", "high"])
        self.assertEqual(thinking.reasoning_effort("unknown"), "medium")

    def test_item6_and_item7_project_memory_and_context(self):
        scene = {}
        stub = fake_bpy(context=types.SimpleNamespace(mode="OBJECT", scene=scene))
        with mock.patch.dict(sys.modules, {"bpy": stub}):
            self.assertIsNone(project_memory.summary_for_context())  # Nothing assumed until it is set.
            self._set_and_read()
        self.assertIn("project_memory", next(iter(scene)))  # Kept in the scene, so saved with the .blend.

    def _set_and_read(self):
        project_memory.set_constraint("target_engine", "Unreal Engine 5")
        project_memory.set_constraint("poly_budget", 40000)
        project_memory.add_protected_object("Chestplate_Base")

        self.assertTrue(project_memory.is_object_protected("Chestplate_Base"))
        self.assertFalse(project_memory.is_object_protected("Temporary_Proxy"))

        summary = project_memory.summary_for_context()
        self.assertIn("Unreal Engine 5", summary)
        self.assertIn("40000 tris", summary)
        self.assertIn("Chestplate_Base", summary)

    def test_item8_local_privacy(self):
        with mock.patch.dict("os.environ", {"LOOPCUT_LOCAL_ONLY": "1"}):
            self.assertTrue(privacy.is_privacy_mode_enabled())
        settings = types.SimpleNamespace(preferences=lambda: types.SimpleNamespace(local_privacy_mode=False))
        with mock.patch.dict("os.environ", {"LOOPCUT_LOCAL_ONLY": ""}), \
                mock.patch.dict(sys.modules, {"loopcut.settings": settings}), mock.patch("loopcut.settings", settings, create=True):
            self.assertFalse(privacy.is_privacy_mode_enabled())


class TestBlenderWorkController(FakeBpyTest):
    """Items 9 - 20: Controlling all Blender work."""

    def test_item9_operator_registry(self):
        stub = fake_bpy(ops=types.SimpleNamespace(
            ed=types.SimpleNamespace(), mesh=types.SimpleNamespace(primitive_cube_add=types.SimpleNamespace(poll=lambda: True, get_rna_type=lambda: types.SimpleNamespace(description="Add a cube", properties=[])))
        ))
        with mock.patch.dict(sys.modules, {"bpy": stub}):
            self._describe_and_search()

    def _describe_and_search(self):
        desc = operator_registry.describe_operator("mesh.primitive_cube_add")
        self.assertEqual(desc["id"], "mesh.primitive_cube_add")
        self.assertTrue(desc["poll_ok"])

        # Search operators
        hits = operator_registry.search_operators("cube")
        self.assertTrue(isinstance(hits, list))
        self.assertGreater(len(hits), 0)

    def test_item12_fast_regional_sculpting(self):
        # Synthetic 50-vertex grid
        co = np.linspace(-1, 1, 150).reshape(50, 3)
        edges = np.array([[i, i+1] for i in range(49)])
        adj = fast_sculpt.build_adjacency(edges, 50)
        self.assertEqual(len(adj[0]), 51)

        center = np.array([0.0, 0.0, 0.0])
        smoothed = fast_sculpt.smooth_region(co, adj, center, radius=0.5, strength=0.5)
        self.assertEqual(smoothed.shape, co.shape)

    def test_item13_thin_shell_sculpt_protection(self):
        # 4 vertices: 2 facing +Z (front wall), 2 facing -Z (back wall)
        co = np.array([
            [0.0, 0.0, 0.01],
            [0.1, 0.0, 0.01],
            [0.0, 0.0, 0.00],
            [0.1, 0.0, 0.00],
        ])
        no = np.array([
            [0.0, 0.0, 1.0],
            [0.0, 0.0, 1.0],
            [0.0, 0.0, -1.0],
            [0.0, 0.0, -1.0],
        ])
        ref_n = np.array([0.0, 0.0, 1.0])
        front = shell_guard.filter_front_facing_vertices(co, no, np.array([0, 1, 2, 3]), ref_n)
        np.testing.assert_array_equal(front, [0, 1])

        # Clamping displacement towards back wall
        excessive_disp = np.array([[0.0, 0.0, -0.05]])
        clamped = shell_guard.guard_shell_displacement(co, no, np.array([0]), excessive_disp, min_thickness=0.01)
        self.assertGreater(clamped[0, 2], -0.05)

    def test_item14_hard_surface_reconstruction(self):
        # Slightly noisy flat plane vertices spanning 2D in X and Y
        np.random.seed(42)
        grid_pts = []
        for x in (-1.0, 0.0, 1.0):
            for y in (-1.0, 0.0, 1.0):
                grid_pts.append([x, y, float(np.random.normal(0, 0.02))])
        co = np.array(grid_pts, dtype=np.float64)

        planar = hard_surface.planarize_vertices(co, list(range(len(grid_pts))))
        # Residual distance to the fitted plane should be nearly 0
        center = np.mean(planar, axis=0)
        _, _, vh = np.linalg.svd(planar - center)
        normal = vh[2]
        residuals = np.abs((planar - center) @ normal)
        self.assertLess(np.max(residuals), 1e-6)

        # Straighten edge path
        path = list(range(5))
        straight = hard_surface.straighten_edge_path(co, path)
        self.assertEqual(straight.shape, co.shape)

    def test_item20_addon_capability_integration(self):
        addons = addon_integrations.list_available_integrations()
        self.assertIn("Hard Ops", addons)
        self.assertIn("Zen UV", addons)
        self.assertIn("UVPackmaster", addons)
        self.assertTrue(addons["Zen UV"]["fallback_available"])


class TestVisualIntelligenceAndDebugging(FakeBpyTest):
    """Items 25 - 30: acceptance checks, transactions, resumable tasks."""

    def test_item25_visual_success_criteria(self):
        before = {"non_manifold_edges": 0, "boundary_edges": 0, "zero_area_faces": 0}
        after_ok = {"non_manifold_edges": 0, "boundary_edges": 0, "zero_area_faces": 0}
        ok, reason = acceptance_criteria.verify_operation_acceptance("Plate", before, after_ok)
        self.assertTrue(ok)

        # Corruption introduced
        after_bad = {"non_manifold_edges": 2, "boundary_edges": 0, "zero_area_faces": 0}
        bad, bad_reason = acceptance_criteria.verify_operation_acceptance("Plate", before, after_bad)
        self.assertFalse(bad)
        self.assertIn("non-manifold edge", bad_reason)

    def test_item27_transactional_blender_operations(self):
        tx = transaction.BlenderTransaction("BevelPass")
        self.assertFalse(tx.committed)
        self.assertFalse(tx.rolled_back)
        tx.stage_step("mesh_bevel", {"width": 0.05})
        self.assertEqual(len(tx.steps), 1)
        tx.rollback()
        self.assertTrue(tx.rolled_back)
        with self.assertRaises(RuntimeError):
            tx.commit()

    def test_item30_resumable_tasks(self):
        task_id = "test_retopo_character"
        stages = ["segmentation", "retopology", "uv_unwrap", "baking"]
        state = resumable_task.init_task(task_id, stages)
        self.assertEqual(state["current_stage_idx"], 0)

        resumable_task.complete_stage(task_id, "segmentation", {"components": 4})
        resumed = resumable_task.resume_task(task_id)
        self.assertIn("segmentation", resumed["completed_stages"])
        self.assertEqual(resumed["current_stage_idx"], 1)


class TestPerformanceAndBenchmarks(FakeBpyTest):
    """Items 31 - 35: Performance, stability, and testing."""

    def test_item31_gpu_vram_coordination(self):
        vram = gpu_coordinator.get_rocm_vram_info()
        self.assertIn("total_mib", vram)
        self.assertIn("free_mib", vram)

        ok, note = gpu_coordinator.check_vram_headroom_for_heavy_op(required_mib=100)
        self.assertTrue(isinstance(ok, bool))

    def test_item32_adaptive_context_compression(self):
        text = "Line 1\n" * 100
        compacted = context_compression.compact_tool_output(text, max_lines=10, max_chars=100)
        self.assertIn("omitted", compacted)
        self.assertLess(len(compacted), len(text))

        h1 = context_compression.hash_context_payload("Context A")
        h2 = context_compression.hash_context_payload("Context A")
        self.assertEqual(h1, h2)

    def test_item33_knowledge_base_retrieval(self):
        hits = knowledge_base.search_knowledge_base("bmesh edit mode vertex")
        self.assertGreater(len(hits), 0)
        self.assertIn("bmesh", hits[0]["snippet"])

    def test_item34_headless_scenarios(self):
        res = headless_suite.run_sculpt_scenario()
        self.assertTrue(res["passed"])

    def test_item35_benchmark_dashboard(self):
        benchmark_dashboard.record_tool_call_metric(valid=True, was_repaired=False, latency_ms=45.0)
        benchmark_dashboard.record_tool_call_metric(valid=True, was_repaired=True, latency_ms=60.0)
        benchmark_dashboard.record_recovery_metric(succeeded=True)

        summary = benchmark_dashboard.get_dashboard_summary()
        self.assertGreater(summary["total_calls"], 0)
        self.assertEqual(summary["accuracy_pct"], 100.0)
        self.assertGreater(summary["average_latency_ms"], 0)


if __name__ == "__main__":
    unittest.main()
