"""shading.py's pure half on synthetic meshes: a UV sphere with smooth or flat normals, split the way a
glTF import splits it, a cube flat or smoothed across its creases, a sphere with stray hard lines.
numpy ships with Blender, not with every Python: without it these skip."""

import importlib.util
import math
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import shading  # noqa: E402

HAS_NUMPY = importlib.util.find_spec("numpy") is not None


def sphere(segments=32, rings=16):
    """(positions, faces): a UV sphere of radius 1, pole triangles and quads, outward winding."""
    points = [(0.0, 0.0, 1.0)]
    for r in range(1, rings):
        theta = math.pi * r / rings
        for s in range(segments):
            phi = 2 * math.pi * s / segments
            points.append((math.sin(theta) * math.cos(phi), math.sin(theta) * math.sin(phi), math.cos(theta)))
    points.append((0.0, 0.0, -1.0))
    bottom = len(points) - 1

    def ring(r, s):
        return 1 + (r - 1) * segments + s % segments
    faces = [[0, ring(1, s), ring(1, s + 1)] for s in range(segments)]
    for r in range(1, rings - 1):
        faces += [[ring(r, s), ring(r + 1, s), ring(r + 1, s + 1), ring(r, s + 1)] for s in range(segments)]
    faces += [[ring(rings - 1, s), bottom, ring(rings - 1, s + 1)] for s in range(segments)]
    return points, faces


def cube():
    points = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    faces = [[0, 1, 3, 2], [4, 6, 7, 5], [0, 4, 5, 1], [2, 3, 7, 6], [0, 2, 6, 4], [1, 5, 7, 3]]
    return points, faces


def mesh(points, faces, split=False):
    """(positions, loop_start, loop_total, corner_verts); split: every face its own vertices, as glTF
    writes a flat-shaded mesh."""
    import numpy as np
    if split:
        points = [points[v] for face in faces for v in face]
        sizes, faces, n = [len(f) for f in faces], [], 0
        for size in sizes:
            faces.append(list(range(n, n + size)))
            n += size
    totals = np.array([len(f) for f in faces])
    starts = np.concatenate([[0], np.cumsum(totals)[:-1]])
    corners = np.array([v for f in faces for v in f])
    return np.array(points, dtype=float), starts, totals, corners


def flat_normals(positions, starts, totals, corners):
    corner_face, nxt = shading.topology(starts, totals, len(corners))
    normals, _ = shading.face_normals(positions, starts, corners, nxt)
    return normals[corner_face]


def smooth_normals(positions, corners):
    import numpy as np
    n = positions[corners]
    return n / np.linalg.norm(n, axis=1)[:, None]


@unittest.skipUnless(HAS_NUMPY, "numpy ships with Blender, not with every Python")
class AnalyseTest(unittest.TestCase):
    def test_a_smooth_sphere_is_organic_and_soft(self):
        p, s, t, c = mesh(*sphere())
        fact = shading.analyse(p, s, t, c, smooth_normals(p, c))
        self.assertEqual(fact["shape"], "organic", fact)
        self.assertLess(fact["flat"], 0.05)
        self.assertEqual(fact["hard_edges"], 0)
        self.assertEqual(fact["open"], 0)
        said = shading.verdict("Ball", fact, custom=True)
        self.assertEqual(said, {"fix": False, "problems": [], "notes": []})

    def test_flat_custom_normals_on_an_organic_mesh_are_the_safe_fix(self):
        p, s, t, c = mesh(*sphere())
        fact = shading.analyse(p, s, t, c, flat_normals(p, s, t, c))
        self.assertEqual(fact["shape"], "organic")
        self.assertGreater(fact["flat"], 0.99)
        self.assertGreater(fact["hard"], 0.9, "near the poles neighbours differ by under 2°")
        said = shading.verdict("Ball", fact, custom=True)
        self.assertTrue(said["fix"])
        self.assertIn("every face shows", said["problems"][0])
        self.assertIn("'custom_normal', 'sharp_edge', 'sharp_face'", said["problems"][0])
        # Flat-shaded the ordinary way (no custom normals): a look, not the fix.
        plain = shading.verdict("Ball", fact, custom=False)
        self.assertFalse(plain["fix"])
        self.assertEqual(plain["problems"], [])
        self.assertIn("flat shaded on a curved surface", plain["notes"][0])

    def test_a_baked_pair_is_reported_never_fixed(self):
        p, s, t, c = mesh(*sphere())
        fact = shading.analyse(p, s, t, c, flat_normals(p, s, t, c))
        said = shading.verdict("Head", fact, custom=True, maps=["retopo_ab12_normal.png"], record={"job": "job_1"},
                               subdivision=["Subdivision"])
        self.assertFalse(said["fix"])
        text = said["problems"][0]
        for words in ("an earlier retopology result", "baked against flat normals", "prints a grid of quads",
                      "left as it is", "source job job_1", "Its Subdivision modifier already shows that grid"):
            self.assertIn(words, text)
        self.assertNotRegex(text, r"(?i)tripo|rodin|meshy|hunyuan|trellis|fal\b")

    def test_a_split_mesh_is_welded_before_its_shape_is_read(self):
        p, s, t, c = mesh(*sphere(), split=True)
        fact = shading.analyse(p, s, t, c, flat_normals(p, s, t, c))
        self.assertEqual(fact["shape"], "organic", fact)
        self.assertGreater(fact["split"], 0)
        self.assertEqual(fact["open"], 0, "welded: no open edges")
        self.assertIn("Merge by Distance", shading.verdict("Ball", fact, custom=True)["problems"][0])

    def test_a_cube_is_hard_surface_and_flat_is_right_for_it(self):
        p, s, t, c = mesh(*cube())
        fact = shading.analyse(p, s, t, c, flat_normals(p, s, t, c))
        self.assertEqual(fact["edges"], 12)
        self.assertEqual(fact["shape"], "unknown", "12 edges: too few to call")
        p, s, t, c = mesh(*sphere(8, 4))
        self.assertEqual(shading.classify(__import__("numpy").full(40, 90.0))["shape"], "hard_surface")
        said = shading.verdict("Crate", {**fact, "shape": "hard_surface"}, custom=True)
        self.assertEqual(said, {"fix": False, "problems": [], "notes": []})

    def test_a_smoothed_box_smears_across_its_creases(self):
        import numpy as np
        p, s, t, c = mesh(*cube())
        fact = shading.analyse(p, s, t, c, smooth_normals(p, c))
        self.assertEqual((fact["smeared"], fact["creases"]), (12, 12))
        said = shading.verdict("Crate", {**fact, "shape": "hard_surface"}, custom=False)
        self.assertIn("shading smears across 12 of its 12 edges", said["problems"][0])
        self.assertIn("set_sharp_from_angle", said["problems"][0])
        self.assertEqual(shading.classify(np.array([90.0] * 12 + [0.0] * 20))["shape"], "hard_surface")

    def test_a_low_poly_ball_reads_faceted_and_is_not_smoothed(self):
        p, s, t, c = mesh(*sphere(8, 4))
        fact = shading.analyse(p, s, t, c, flat_normals(p, s, t, c))
        self.assertEqual(fact["shape"], "faceted", fact)
        self.assertFalse(shading.verdict("Rock", fact, custom=True)["fix"])

    def test_stray_hard_lines_across_a_smooth_surface(self):
        import numpy as np
        p, s, t, c = mesh(*sphere(64, 32))
        normals = smooth_normals(p, c)
        flat = flat_normals(p, s, t, c)
        corner_face, _ = shading.topology(s, t, len(c))
        band = (corner_face % 64) < 8  # Pole fans and quads in eight columns: flat.
        normals[band] = flat[band]
        fact = shading.analyse(p, s, t, c, normals)
        self.assertLess(fact["flat"], shading.MOSTLY_SMOOTH)
        self.assertGreater(fact["stray"], 0.1 * fact["edges"], fact)
        said = shading.verdict("Ball", fact, custom=False)
        self.assertIn("hard edges cross nearly flat surface", said["problems"][0])
        # The same lines on UV seams are deliberate.
        uvs = np.zeros((len(c), 2))
        uvs[band] = 0.5
        seamed = shading.analyse(p, s, t, c, normals, uvs)
        self.assertLess(seamed["stray"], fact["stray"])

    def test_fingerprints_say_what_changed_after_the_bake(self):
        import numpy as np
        p, s, t, c = mesh(*sphere())
        n = smooth_normals(p, c)
        uvs = np.zeros((len(c), 2))
        before = shading.digests(p, t, c, uvs, n)
        self.assertEqual(shading.changes(before, shading.digests(p.copy(), t, c, uvs, n)), [])
        moved = p.copy()
        moved[3] += 0.01
        self.assertEqual(shading.changes(before, shading.digests(moved, t, c, uvs, n)), ["shape"])
        self.assertEqual(shading.changes(before, shading.digests(p, t, c, uvs + 0.1, n)), ["UVs"])
        self.assertEqual(shading.changes(before, shading.digests(p, t, c, uvs, flat_normals(p, s, t, c))), ["normals"])
        self.assertEqual(shading.changes(None, before), [])
        said = shading.verdict("Head", shading.analyse(p, s, t, c, n), custom=True, maps=["n.png"],
                               record={"job": "job_2"}, changed=["shape"])
        self.assertIn("changed after its bake (shape)", said["problems"][0])
        self.assertIn("source job job_2", said["problems"][0])


class PureTest(unittest.TestCase):
    def test_the_tool_is_read_only_and_short(self):
        from loopcut import tools
        self.assertIn("check_shading", tools.SAME_WHILE_UNCHANGED)
        self.assertNotIn("check_shading", tools.CHANGES_SCENE)
        self.assertLess(len(shading.SCHEMAS[0]["function"]["description"]), 260)
        self.assertIs(shading.DISPATCH["check_shading"], shading.check_shading)


if __name__ == "__main__":
    unittest.main()
