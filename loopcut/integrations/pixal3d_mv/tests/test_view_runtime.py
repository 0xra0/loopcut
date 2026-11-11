"""View QA, run manifest, NAF preflight and reconstruction retries (no ComfyUI, no Blender).
Run with: python -m unittest discover -s tests -v"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pa
import pa_runtime as rt
import view_qa as q


def flag_shape(height=200, width=400, mast_x=60):
    """An asymmetric silhouette: a mast near the left edge with a flag to its right."""
    m = np.zeros((height, width), bool)
    m[20:180, mast_x:mast_x + 30] = True
    m[20:100, mast_x + 30:mast_x + 150] = True
    return m


class ViewChecks(unittest.TestCase):
    def test_true_turnaround_passes_and_unmirrored_back_fails(self):
        front, side = flag_shape(), np.zeros((200, 400), bool)
        side[20:180, 190:210] = True
        side[20:80, 170:230] = True
        good = q.consistency({"front": front, "left": side, "back": np.fliplr(front), "right": np.fliplr(side)})
        self.assertTrue(good["passed"], good["issues"])
        self.assertGreater(good["mirror_iou"]["front_back"], 0.99)
        bad = q.consistency({"front": front, "left": side, "back": front, "right": np.fliplr(side)})
        self.assertFalse(bad["passed"])
        self.assertIn("front/back", bad["issues"][0])

    def test_only_a_transparent_background_counts_as_cut_out(self):
        # Qwen's views come out RGBA with alpha 247-255 everywhere: that is no cut-out (the 2026-11-08 chestplate
        # regen meshed four uncut views because "min alpha < 255" called them cut out).
        rgba = np.full((64, 64, 4), 250, np.uint8)
        self.assertFalse(q.is_cut_out(Image.fromarray(rgba, "RGBA")))
        self.assertFalse(q.is_cut_out(Image.new("RGB", (64, 64), "white")))
        rgba[..., 3] = 0
        rgba[16:48, 16:48, 3] = 255
        self.assertTrue(q.is_cut_out(Image.fromarray(rgba, "RGBA")))

    def test_height_drift_and_empty_view_are_reported(self):
        front = flag_shape()
        short = np.zeros_like(front)
        short[40:180] = front[20:160]
        report = q.consistency({"front": front, "left": front, "back": np.fliplr(short), "right": front})
        self.assertTrue(any("height differs" in i for i in report["issues"]))
        self.assertFalse(q.consistency({"front": front, "left": np.zeros_like(front), "back": front, "right": front})["passed"])

    def test_defringe_unmixes_white_edges(self):
        rgba = np.zeros((9, 9, 4), np.uint8)
        rgba[2:7, 2:7] = (40, 40, 40, 255)
        rgba[1, 2:7] = (148, 148, 148, 128)        # 50% dark object over white background
        out = np.asarray(q.defringe(Image.fromarray(rgba, "RGBA"), erode=0))
        self.assertLessEqual(abs(int(out[1, 4, 0]) - 40), 3)
        trimmed = np.asarray(q.defringe(Image.fromarray(rgba, "RGBA"), erode=1))[..., 3]
        self.assertEqual(trimmed[1, 4], 0)          # the rim pixel is gone
        self.assertEqual(trimmed[4, 4], 255)        # the interior is kept

    def test_normalize_matches_front_height_and_baseline(self):
        def view(h, w):
            image = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
            image.paste(Image.new("RGBA", (w, h), (50, 50, 50, 255)), (10, 30))
            return image
        out = q.normalize({"front": view(200, 80), "left": view(150, 40), "back": view(220, 80), "right": view(200, 40)}, 300)
        boxes = {v: q.bbox(q.mask(i)) for v, i in out.items()}
        self.assertEqual({b[1] - b[0] for b in boxes.values()}, {200})
        self.assertEqual({b[1] for b in boxes.values()}, {boxes["front"][1]})
        self.assertLessEqual(abs((boxes["left"][2] + boxes["left"][3]) / 2 - 150), 1)

    def test_projection_uses_the_pixal3d_file_axes(self):
        # Tall thin plate in file coordinates: wide along X, thin along Y, tall along -Z (up).
        v = np.array([[x, y, z] for x in (-.2, .2) for y in (-.02, .02) for z in (-.5, .5)], float)
        faces = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
                          [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]])
        front = q.bbox(q.project(v, faces, *q.CAMERAS["front"], 200))
        left = q.bbox(q.project(v, faces, *q.CAMERAS["left"], 200))
        self.assertGreater(front[3] - front[2], 5 * (left[3] - left[2]))   # front sees the width, side the thickness
        self.assertGreater(front[1] - front[0], 150)                        # height is vertical in both


class RuntimeChecks(unittest.TestCase):
    def test_naf_problems(self):
        self.assertIsNone(rt.naf_problems(None))
        self.assertEqual(rt.naf_problems(dict(rt.NAF_ENV)), [])
        problems = rt.naf_problems({"TRELLIS2_REAL_NAF": "1", "TRELLIS2_NATTEN_IMPL": "flex"})
        self.assertEqual(problems, ["TRELLIS2_NAF_STRICT=<unset> (needs 1)"])

    def test_preflight_refuses_a_non_strict_server(self):
        with patch.object(rt, "server_env", return_value={"TRELLIS2_REAL_NAF": "0"}), \
                patch.dict(os.environ, {"PA_ALLOW_BILINEAR_NAF": "0"}), self.assertRaises(SystemExit) as stop:
            pa.naf_preflight()
        self.assertIn("TRELLIS2_REAL_NAF=0", str(stop.exception))

    def test_memory_floor(self):
        with patch.object(rt, "ram_available_gib", return_value=4.0), patch.object(rt, "vram_free_gib", return_value=14.0), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIn("RAM", rt.memory_problem("http://x", "mesh"))
        with patch.object(rt, "ram_available_gib", return_value=20.0), patch.object(rt, "vram_free_gib", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(rt.memory_problem("http://x", "mesh"))

    def test_manifest_reuse_needs_same_inputs_and_existing_outputs(self):
        with tempfile.TemporaryDirectory() as comfy:
            out = Path(comfy) / "a.png"
            out.write_bytes(b"x")
            rt.record(comfy, "prop", "front", desc="a sword", seed=None, outputs=[str(out)])
            self.assertEqual(rt.reusable(comfy, "prop", "front", desc="a sword")["outputs"], [str(out)])
            self.assertIsNone(rt.reusable(comfy, "prop", "front", desc="an axe"))
            self.assertIsNone(rt.reusable(comfy, "prop", "sheet"))
            out.unlink()
            self.assertIsNone(rt.reusable(comfy, "prop", "front", desc="a sword"))

    def test_pa_flags(self):
        self.assertEqual(pa.pa_flags(["7", "--tries", "3", "--resume", "--preset", "prop"]),
                         (["7", "--preset", "prop"], 3, True, False))
        self.assertEqual(pa.pa_flags(["--tries=2", "--separate-views"]), ([], 2, False, True))
        with self.assertRaises(SystemExit):
            pa.pa_flags(["--tries", "0"])


class Retries(unittest.TestCase):
    def run_mesh(self, scores, tries):
        """Fake reconstructions whose QA scores are `scores`; return (chosen glb, number generated, manifest)."""
        made = []
        def generate(name, seed):
            made.append(seed)
            return f"/out/prop_{len(made)}.glb", [f"/out/prop_{len(made)}.glb"]
        def qa(views_dir, glb):
            score = scores[int(glb.split("_")[-1].split(".")[0]) - 1]
            return {"score": score, "passed": score >= 1.0}
        with tempfile.TemporaryDirectory() as comfy, patch.object(pa, "COMFY", comfy), \
                patch.object(pa.os.path, "exists", side_effect=lambda p: True if p.endswith(".png") else os.path.isdir(p)), \
                patch.object(pa, "naf_preflight"), \
                patch.object(pa, "check_views"), patch.object(pa, "generate", side_effect=generate), \
                patch.object(pa, "mesh_qa", side_effect=qa), patch.dict(os.environ, {"PA_NO_FIX": "1"}), \
                contextlib.redirect_stdout(io.StringIO()):
            chosen = pa.mesh("prop", 5, {}, tries)[0]
            manifest = json.load(open(rt.manifest_path(comfy, "prop")))
        return chosen, len(made), manifest

    def test_keeps_best_not_latest(self):
        chosen, count, manifest = self.run_mesh([0.6, 0.9, 0.7], 3)
        self.assertEqual((chosen, count), ("/out/prop_2.glb", 3))
        self.assertEqual(manifest["stages"]["mesh"]["outputs"], ["/out/prop_2.glb"])
        self.assertEqual(len(manifest["stages"]["mesh"]["candidates"]), 3)

    def test_stops_at_first_passing_reconstruction(self):
        chosen, count, _ = self.run_mesh([0.6, 1.2, 1.5], 3)
        self.assertEqual((chosen, count), ("/out/prop_2.glb", 2))

    def test_first_try_uses_the_given_seed(self):
        with patch.object(pa, "generate", return_value=(None, [])) as gen, patch.object(pa, "naf_preflight"), \
                patch.object(pa, "check_views"), patch.object(pa.os.path, "exists", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            pa.mesh("prop", 77, {}, 1)
        self.assertEqual(gen.call_args.args[1], 77)


class Resume(unittest.TestCase):
    def test_resume_skips_recorded_stages(self):
        with tempfile.TemporaryDirectory() as comfy, patch.object(pa, "COMFY", comfy), \
                contextlib.redirect_stdout(io.StringIO()):
            img, raw, lod0 = (Path(comfy) / n for n in ("front.png", "raw.glb", "lod0.glb"))
            views = Path(comfy) / "input" / "prop_views"
            views.mkdir(parents=True)
            for f in [img, raw, lod0] + [views / f"{v}.png" for v in q.VIEWS]:
                f.write_bytes(b"x")
            rt.record(comfy, "prop", "front", desc="a sword", seed=None, outputs=[str(img)])
            rt.record(comfy, "prop", "views", image=str(img), desc="a sword", separate=False, seed=None,
                      outputs=[str(views / f"{v}.png") for v in q.VIEWS])
            rt.record(comfy, "prop", "mesh", seed=3, outputs=[str(raw)])
            rt.record(comfy, "prop", "asset", source=str(raw), outputs=[str(lod0)])
            with patch.object(pa, "front") as front, patch.object(pa, "sheet") as sheet, patch.object(pa, "mesh") as mesh, \
                    patch.object(pa, "asset") as asset:
                self.assertEqual(pa.all_stages("prop", "text:a sword", "a sword", ["--resume"]), [str(lod0)])
            for stage in (front, sheet, mesh, asset):
                stage.assert_not_called()
            # a changed description regenerates the views and everything after them
            with patch.object(pa, "sheet", return_value=["s.png"]) as sheet, patch.object(pa, "split"), \
                    patch.object(pa, "mesh", return_value=["new"]) as mesh:
                pa.all_stages("prop", "text:a sword", "a long sword", ["--resume"])
            sheet.assert_called_once()
            mesh.assert_called_once()

    def test_hand_pick_becomes_the_recorded_reconstruction(self):
        with tempfile.TemporaryDirectory() as comfy:
            best, mine = str(Path(comfy) / "best.glb"), str(Path(comfy) / "mine.glb")
            for f in (best, mine):
                Path(f).write_bytes(b"x")
            rt.record(comfy, "prop", "mesh", seed=2, outputs=[best],
                      candidates=[{"seed": 1, "glb": mine, "score": .7}, {"seed": 2, "glb": best, "score": .8}])
            self.assertTrue(rt.pick(comfy, "prop", mine))
            done = rt.reusable(comfy, "prop", "mesh")
            self.assertEqual((done["outputs"], done["seed"], len(done["candidates"])), ([mine], 1, 2))
            self.assertFalse(rt.pick(comfy, "prop", mine))                          # already the pick
            self.assertFalse(rt.pick(comfy, "prop", str(Path(comfy) / "other.glb")))  # not a candidate
            self.assertEqual(rt.reusable(comfy, "prop", "mesh")["outputs"], [mine])


if __name__ == "__main__":
    unittest.main()
