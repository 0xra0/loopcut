"""Unit tests for Batch 3: Items 8, 9, 10.

Item 8: Strengthened project memory and crash-resilient pipeline resumption.
Item 9: GPU/VRAM coordination, headroom checks, and competing workload queueing.
Item 10: Local evals extension with Pixal3D repair cases and comprehensive quality tracking.
"""

import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "addons_core"))

from loopcut import project_memory


def fake_bpy(**attrs):
    """Blender stub created inside setUp for isolation."""
    module = types.ModuleType("bpy")
    scene = {}
    context = types.SimpleNamespace(
        mode="OBJECT",
        scene=scene,
        preferences=types.SimpleNamespace(addons={}),
    )
    module.__dict__.update(context=context)
    module.__dict__.update(attrs)
    return module


class Batch3MemoryGpuEvalsTest(unittest.TestCase):
    """Batch 3: Project memory recovery, GPU stage coordination, and quality tracking."""

    def setUp(self):
        self.patch = mock.patch.dict(sys.modules, {"bpy": fake_bpy()})
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
    # Item 8: Strengthen Project Memory and Recovery
    # -------------------------------------------------------------------------

    def test_strengthened_project_memory_and_resumption(self):
        """Project memory persists export format, completed stages, and resumes after crash."""
        # Target export format
        project_memory.set_target_export_format("glb")
        self.assertEqual(project_memory.get_target_export_format(), "glb")

        # Completed pipeline stages
        project_memory.record_completed_stage("pixal3d_refine", "clean_loose", "12 loose bits deleted")
        project_memory.record_completed_stage("pixal3d_refine", "fix_non_manifold", "boundary edges closed")
        self.assertTrue(project_memory.is_stage_completed("pixal3d_refine", "clean_loose"))
        self.assertFalse(project_memory.is_stage_completed("pixal3d_refine", "quad_remesh"))

        # Resume pipeline: only uncompleted stages remain
        all_stages = ["clean_loose", "fix_non_manifold", "solidify_thin_shells", "symmetrize", "quad_remesh"]
        remaining = project_memory.resume_pipeline("pixal3d_refine", all_stages)
        self.assertEqual(remaining, ["solidify_thin_shells", "symmetrize", "quad_remesh"])

        # Failed approaches to avoid
        project_memory.record_failed_approach("sculpt_smooth_all", "collapses thin armor shell")
        self.assertTrue(project_memory.should_avoid_approach("sculpt_smooth_all"))
        self.assertFalse(project_memory.should_avoid_approach("quad_remesh"))

        # Context summary incorporates all memory facets
        summary = project_memory.summary_for_context()
        self.assertIn("export: glb", summary)
        self.assertIn("stages completed (pixal3d_refine: 2)", summary)
        self.assertIn("avoid: sculpt_smooth_all", summary)

    def test_crash_durable_recovery_survives_without_save(self):
        """Memory survives simulated crash when scene property is wiped before save."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            sidecar = Path(tmpdir) / ".test_scene_recovery.json"
            with mock.patch("loopcut.project_memory._recovery_path", return_value=sidecar):
                # User/agent records completed stages
                project_memory.record_completed_stage("pixal3d_refine", "clean_loose", "deleted 10 vertices")
                project_memory.record_completed_stage("pixal3d_refine", "fix_non_manifold", "filled 4 holes")

                # Check that sidecar was written to disk
                self.assertTrue(sidecar.is_file())

                # Simulate Blender crash: scene custom property wiped, scene re-initialized from disk without save
                import bpy
                bpy.context.scene.clear()

                # load_memory should recover the completed stages from the durable sidecar file
                loaded = project_memory.load_memory()
                self.assertTrue(project_memory.is_stage_completed("pixal3d_refine", "clean_loose"))
                self.assertTrue(project_memory.is_stage_completed("pixal3d_refine", "fix_non_manifold"))
                self.assertFalse(project_memory.is_stage_completed("pixal3d_refine", "quad_remesh"))

    # -------------------------------------------------------------------------
    # Item 10: Local Evals Pixal3D Cases and Quality Report
    # -------------------------------------------------------------------------

    def test_local_evals_pixal3d_cases_present(self):
        """Local eval suite registers Pixal3D repair tasks."""
        evals_tasks_path = ROOT / "loopcut" / "harness" / "local_evals" / "tasks.py"
        content = evals_tasks_path.read_text(encoding="utf-8")
        self.assertIn("pixal3d_repair", content)
        self.assertIn("pixal3d_islands", content)

    def test_quality_metrics_reporter(self):
        """Quality report calculates completion rate, tool call errors, geometry damage, and VRAM use."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("eval_run", str(ROOT / "loopcut" / "harness" / "local_evals" / "run.py"))
        eval_run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(eval_run)

        results = [
            {"id": "task_1", "passed": True, "seconds": 15, "steps": 3, "failed_calls": 0, "geometry_damage": 0.0},
            {"id": "task_2", "passed": False, "seconds": 30, "steps": 5, "failed_calls": 2, "geometry_damage": 0.12},
        ]
        vram_info = {"free_mib": 12000, "used_mib": 4384}
        report = eval_run.generate_quality_report(results, vram_info=vram_info)

        self.assertEqual(report["total_tasks"], 2)
        self.assertEqual(report["passed_tasks"], 1)
        self.assertEqual(report["completion_rate_pct"], 50.0)
        self.assertEqual(report["tool_call_errors"], 2)
        self.assertAlmostEqual(report["total_geometry_damage"], 0.12)
        self.assertEqual(report["vram_used_mib"], 4384)


if __name__ == "__main__":
    unittest.main()
