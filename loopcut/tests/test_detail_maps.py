"""detail_maps.py's pure half: reoriented normal mapping, tiling and the default scale. numpy ships with Blender,
not with every Python: these skip without it."""

import importlib.util
import math
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))
from loopcut import detail_maps  # noqa: E402

HAS_NUMPY = importlib.util.find_spec("numpy") is not None


def encode(x, y, z):
    length = math.sqrt(x * x + y * y + z * z)
    return [x / length * 0.5 + 0.5, y / length * 0.5 + 0.5, z / length * 0.5 + 0.5]


@unittest.skipUnless(HAS_NUMPY, "numpy")
class RnmTest(unittest.TestCase):
    def test_a_flat_detail_keeps_the_base_and_a_flat_base_takes_the_detail(self):
        import numpy as np
        base, detail, flat = np.array([encode(0.3, -0.2, 1)]), np.array([encode(-0.4, 0.1, 1)]), np.array([encode(0, 0, 1)])
        self.assertTrue(np.allclose(detail_maps.rnm(base, flat, 1.0), base, atol=1e-6))
        self.assertTrue(np.allclose(detail_maps.rnm(flat, detail, 1.0), detail, atol=1e-6))

    def test_strength_zero_is_the_base_and_detail_tilts_it_the_detail_way(self):
        import numpy as np
        base, detail = np.array([encode(0.3, 0.0, 1)]), np.array([encode(0.0, 0.5, 1)])
        self.assertTrue(np.allclose(detail_maps.rnm(base, detail, 0.0), base, atol=1e-6))
        out = detail_maps.rnm(base, detail, 1.0)[0] * 2 - 1
        self.assertGreater(out[1], 0.1)  # Tilted toward the detail's +y ...
        self.assertGreater(out[0], 0.1)  # ... keeping the base's +x.
        self.assertAlmostEqual(float(np.linalg.norm(out)), 1.0, places=6)

    def test_tiling_repeats_at_texel_centres(self):
        import numpy as np
        detail = np.arange(4 * 4).reshape(4, 4, 1).astype(float)
        out = detail_maps.tiled(detail, 8, 8, 2.0)
        self.assertEqual(out.shape, (8, 8, 1))
        self.assertTrue(np.array_equal(out[:4, :4], detail) and np.array_equal(out[4:, 4:], detail))


class ScaleTest(unittest.TestCase):
    def test_one_repeat_covers_the_textures_real_size_clamped(self):
        self.assertEqual(detail_maps.default_scale(2.64, 0.264), 10.0)
        self.assertEqual(detail_maps.default_scale(0.1, 2.0), detail_maps.MIN_SCALE)
        self.assertEqual(detail_maps.default_scale(1e6, 0.1), detail_maps.MAX_SCALE)
        self.assertEqual(detail_maps.default_scale(0.0, 0.3), 10.0)

    def test_every_kind_has_a_texture_with_its_size(self):
        for kind, (texture, strength) in detail_maps.KINDS.items():
            self.assertIn(texture, detail_maps.TILE_METRES, kind)
            self.assertTrue(0 < strength <= 0.5, kind)


if __name__ == "__main__":
    unittest.main()
