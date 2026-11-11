import io
import os
import subprocess
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from uv_quality import area, intersection_area, overlaps


class UVTests(unittest.TestCase):
    def test_small_translated_uv_triangle_is_not_collapsed(self):
        self.assertGreater(area(((.5, .5), (.500001, .5), (.5, .500001))), 4.9e-13)

    def test_shared_edge_is_not_overlap(self):
        self.assertEqual(overlaps([((0, 0), (1, 0), (1, 1)), ((0, 0), (1, 1), (0, 1))])["uv_overlap_pairs"], 0)

    def test_identical_islands_overlap(self):
        tri = ((.1, .1), (.5, .1), (.1, .5))
        self.assertEqual(overlaps([tri, tri])["uv_overlap_pairs"], 1)
        self.assertAlmostEqual(intersection_area(tri, tuple(reversed(tri))), .08)

    def test_contained_triangle(self):
        big, small = ((0, 0), (1, 0), (0, 1)), ((.1, .1), (.2, .1), (.1, .2))
        self.assertAlmostEqual(intersection_area(big, small), .005)

    def test_budget_is_explicit(self):
        tri = ((0, 0), (1, 0), (0, 1))
        self.assertEqual(overlaps([tri] * 5, max_tests=1)["uv_overlap_check"], "budget_exceeded")


XATLAS_PYTHON = Path(os.environ.get("PA_XATLAS_PYTHON", "/mnt/ssd/conda-envs/ComfyUI/bin/python3.12"))


@unittest.skipUnless(XATLAS_PYTHON.is_file(), "no Python with xatlas")
class XatlasWorkerTests(unittest.TestCase):
    def test_spiky_grid_comes_back_fold_free_in_input_order(self):
        import numpy as np
        rng = np.random.default_rng(3)
        n = 24
        x, y = np.meshgrid(np.linspace(0, 1, n), np.linspace(0, 1, n))
        # 2 mm across: xatlas's absolute epsilons collapse such triangles unless the worker rescales.
        positions = (np.stack([x.ravel(), y.ravel(), rng.normal(0, .08, n * n)], 1) * .002).astype(np.float32)
        quads = [(r * n + c, r * n + c + 1, (r + 1) * n + c + 1, (r + 1) * n + c) for r in range(n - 1) for c in range(n - 1)]
        triangles = np.array([t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))], dtype=np.uint32)
        payload = io.BytesIO()
        np.savez(payload, positions=positions, triangles=triangles)
        process = subprocess.run([str(XATLAS_PYTHON), "-I", str(Path(__file__).resolve().parents[1] / "xatlas_worker.py"),
                                  "512", "2"], input=payload.getvalue(), capture_output=True, timeout=300)
        self.assertEqual(process.returncode, 0, process.stderr.decode()[-500:])
        uvs = np.load(io.BytesIO(process.stdout), allow_pickle=False)
        self.assertEqual(uvs.shape, (len(triangles), 3, 2))
        tris = [tuple(map(tuple, tri.astype(float))) for tri in uvs]
        self.assertEqual(overlaps(tris)["uv_overlap_pairs"], 0)
        self.assertTrue(all(area(tri) > 1e-12 for tri in tris))
        self.assertTrue(((uvs >= 0) & (uvs <= 1)).all())
