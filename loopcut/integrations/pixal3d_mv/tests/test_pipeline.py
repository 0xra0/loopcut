"""Run with: python -m unittest discover -s tests -v (no Blender needed)."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pa
from mesh_config import parse, resolve_source_up


class PipelineTests(unittest.TestCase):
    def test_source_up_auto_stands_only_trimesh_glbs(self):
        def glb(path, generator):
            doc = json.dumps({"asset": {"version": "2.0", "generator": generator}}).encode()
            doc += b" " * (-len(doc) % 4)
            path.write_bytes(b"glTF" + (2).to_bytes(4, "little") + (20 + len(doc)).to_bytes(4, "little") +
                             len(doc).to_bytes(4, "little") + b"JSON" + doc)
            return path
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            raw = glb(root / "raw.glb", "https://github.com/mikedh/trimesh")
            blender = glb(root / "lp.glb", "Khronos glTF Blender I/O v5.2.40")
            self.assertEqual(resolve_source_up(raw, "auto"), "z")
            self.assertEqual(resolve_source_up(blender, "auto"), "y")
            self.assertEqual(resolve_source_up(root / "mesh.obj", "auto"), "y")
            self.assertEqual(resolve_source_up(raw, "y"), "y")
        self.assertEqual(parse(["in.glb", "out.glb"]).source_up, "auto")

    def test_workflow_drift_flags_renamed_input_but_not_autogrow(self):
        info = {"LoadBackgroundRemovalModel": {"input": {"required": {"bg_removal_name": ["COMBO", {}]}}},
                "TextEncodeQwenImage21": {"input": {"required": {"prompt": ["STRING", {}],
                                                                 "images": ["COMFY_AUTOGROW_V3", {}]}}}}
        old = {"3": {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_model": "birefnet.safetensors"}},
               "5": {"class_type": "TextEncodeQwenImage21", "inputs": {"prompt": "x", "images.image_1": ["1", 0]}},
               "6": {"class_type": "GoneNode", "inputs": {}}}
        self.assertEqual(pa.workflow_drift(old, info), [
            "node 3 LoadBackgroundRemovalModel: missing input bg_removal_name",
            "node 3 LoadBackgroundRemovalModel: unknown input bg_removal_model",
            "node 6: GoneNode not installed"])
        for name in ("01_front.json", "02_sheet.json", "03_mesh.json", "04_texture.json"):
            self.assertIsInstance(pa.wf(name), dict)

    def test_zero_faces_is_explicitly_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            out = str(Path(directory) / "prop.glb")
            def run(args, **kwargs):
                self.assertEqual(args[args.index("--faces") + 1], "0")
                self.assertEqual(args[args.index("--python-exit-code") + 1], "1")
                Path(out).write_bytes(b"glTF")
                Path(out).with_suffix(".report.json").write_text(json.dumps({
                    "status": "exported", "validation_passed": True}))
                return type("Result", (), {"returncode": 0, "stdout": "PREP OK done", "stderr": ""})()
            with patch.object(pa.subprocess, "run", side_effect=run):
                self.assertEqual(pa.blender_pass("prop", "source.blend", out, 0, "full", 35, "smart", ("glb",)), out)

    def test_success_text_without_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            result = type("Result", (), {"returncode": 0, "stdout": "PREP OK done", "stderr": ""})()
            with patch.object(pa.subprocess, "run", return_value=result), self.assertRaises(SystemExit):
                pa.blender_pass("prop", "source.glb", str(Path(directory) / "out.glb"), 0, "full", 35, "smart", ("glb",))

    def test_invalid_options_fail_before_blender(self):
        for flags in (("--faces", "-1"), ("--repair", "typo"), ("--angle", "nan"),
                      ("--formats", "glb,bogus"), ("--unwrap", "typo"), ("--voxel-size", "0.01")):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse(["in.glb", "out.glb", *flags])

    def test_legacy_triangle_count_and_flag_precedence(self):
        self.assertEqual(parse(["in.glb", "out.glb", "0"]).faces, 0)
        self.assertEqual(parse(["in.glb", "out.glb", "0", "--faces=123"]).faces, 123)
        source, options = pa.mesh_arguments(["model.blend", "0", "--strict"], "fix")
        self.assertEqual(source, "model.blend")
        self.assertEqual(options["faces"], 0)
        self.assertTrue(options["strict"])

    def test_mesh_processes_the_actual_generation(self):
        with patch.object(pa, "COMFY", "/unused"), patch.object(pa.os.path, "exists", return_value=True), \
                patch.object(pa, "run", return_value=["/new/prop_00001.glb"]), \
                patch.object(pa, "naf_preflight"), patch.object(pa, "check_views"), \
                patch.object(pa, "mesh_qa", return_value=None), patch.object(pa.rt, "record"), \
                patch.object(pa, "mesh_judge", return_value=None), \
                patch.object(pa, "asset", return_value="/new/prop_lp.glb") as prep, \
                patch.dict(pa.os.environ, {"PA_NO_FIX": "0", "PA_LEGACY_PREP": "0"}):
            self.assertEqual(pa.mesh("prop", 123)[0], "/new/prop_lp.glb")
            prep.assert_called_once_with("prop", "/new/prop_00001.glb")

    def _tries(self, qas, verdicts, tries):
        """Run pa.mesh with one GLB per try; return (GLBs generated, GLB kept)."""
        glbs = [f"/new/prop_{i:05d}_.glb" for i in range(1, len(qas) + 1)]
        judge = lambda score, ok: {"verdict": "pass" if ok else "fail", "score": score, "backend": "agy",
                                   "problems": [], "sheet": "x"}
        with patch.object(pa, "COMFY", "/unused"), patch.object(pa.os.path, "exists", return_value=True), \
                patch.object(pa, "naf_preflight"), patch.object(pa, "check_views"), patch.object(pa.rt, "record"), \
                patch.object(pa, "generate", side_effect=[(g, []) for g in glbs]) as gen, \
                patch.object(pa, "mesh_qa", side_effect=[{"passed": p, "score": s} for p, s in qas]), \
                patch.object(pa, "mesh_judge", side_effect=[v and judge(*v) for v in verdicts]), \
                patch.object(pa, "finish", side_effect=lambda name, glb, files, opts: glb), \
                contextlib.redirect_stdout(io.StringIO()):
            kept = pa.mesh("prop", 1, None, tries)
        return gen.call_count, kept

    def test_judge_fail_rerolls_even_when_silhouettes_pass_and_its_rating_picks_the_mesh(self):
        made, kept = self._tries([(True, 1.3), (True, 1.2), (True, 1.1)], [(2, False), (6, False), (3, False)], 3)
        self.assertEqual((made, kept), (3, "/new/prop_00002_.glb"))   # best judge score, not best silhouette

    def test_judge_pass_stops_rerolling_even_when_silhouettes_fail(self):
        made, kept = self._tries([(False, 0.5), (False, 0.6), (True, 1.3)], [(3, False), (8, True), None], 3)
        self.assertEqual((made, kept), (2, "/new/prop_00002_.glb"))

    def test_without_a_judge_the_silhouette_check_decides_as_before(self):
        made, kept = self._tries([(False, 0.5), (False, 0.9), (True, 0.7)], [None, None, None], 3)
        self.assertEqual((made, kept), (3, "/new/prop_00002_.glb"))   # a pass stops it; the best score is kept
        made, kept = self._tries([(False, 0.5), (False, 0.9)], [None, None], 2)
        self.assertEqual((made, kept), (2, "/new/prop_00002_.glb"))

    def test_judge_cameras_see_the_views_with_view_qa_image_axes(self):
        import numpy as np, mesh_judge, view_qa
        for view in view_qa.VIEWS:
            (axis, sign), _ = view_qa.CAMERAS[view]
            expected = np.zeros(3)
            expected[axis] = sign
            right = np.cross(-np.array(mesh_judge.EYE[view], float), mesh_judge.UP)
            self.assertTrue(np.allclose(right, expected), view)

    def test_side_views_go_to_the_cameras_that_see_those_sides(self):
        # Pixal3D's left_image camera (az270) sees the object's RIGHT side: the object faces -Y and az90
        # (right_image) sits at +X, its left. Unswapped, cuirass v3 put its front medallion on the back 3/3.
        with patch.object(pa, "run", return_value=["/o/prop_00001_.glb"]) as submit:
            pa.generate("prop", 7)
        graph = submit.call_args.args[0]
        self.assertEqual([graph[n]["inputs"]["image"] for n in "1234"],
                         ["prop_views/front.png", "prop_views/right.png", "prop_views/back.png", "prop_views/left.png"])
        self.assertEqual(graph["6"]["inputs"]["left_image"][0], "2")

    def test_judge_reply_parsing(self):
        import mesh_judge
        reply = 'Looking at it.\n```json\n{"verdict": "fail", "score": "4", "problems": ["back: relief"]}\n```'
        self.assertEqual(mesh_judge.parse(reply), {"verdict": "fail", "score": 4.0, "problems": ["back: relief"]})
        self.assertIsNone(mesh_judge.parse('{"score": 9}'))
        self.assertIsNone(mesh_judge.parse("no json at all"))

    def test_texture_prepares_external_mesh_and_keeps_smart_uv(self):
        with patch.object(pa.os.path, "exists", return_value=True), \
                patch.object(pa, "prep", return_value="/prepared/prop_lp.glb") as prep, \
                patch.object(pa, "run", return_value=[]) as submit:
            pa.texture("prop", "/no-report/model.obj", 2048)
            prep.assert_called_once_with("prop", "/no-report/model.obj", materials="discard")
            graph = submit.call_args.args[0]
            self.assertEqual(graph["14"]["inputs"]["uv_mode"], "keep_existing")

    def test_texture_memory_factor_comes_from_the_environment_only_when_set(self):
        def graph_for(env):
            with patch.dict(pa.os.environ, env, clear=False), \
                    patch.object(pa.os.path, "exists", return_value=True), \
                    patch.object(pa, "prep", return_value="/prepared/prop_lp.glb"), \
                    patch.object(pa, "run", return_value=[]) as submit:
                pa.os.environ.pop("PA_TEXTURE_MEMORY_FACTOR", None) if not env else None
                pa.texture("prop", "/no-report/model.obj", 2048)
                return submit.call_args.args[0]
        default = graph_for({})
        self.assertEqual([default[n]["inputs"]["memory_factor"] for n in ("18", "20")], [0.0, 0.0])
        raised = graph_for({"PA_TEXTURE_MEMORY_FACTOR": "16"})
        self.assertEqual([raised[n]["inputs"]["memory_factor"] for n in ("18", "20")], [16.0, 16.0])


if __name__ == "__main__":
    unittest.main()
