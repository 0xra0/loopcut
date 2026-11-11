"""Production config, routing and failure-boundary checks without Blender."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asset_config import ENGINES, allocate_budget, load_regions, parse_asset
from engine_check import check_engine
import pa


class AssetTests(unittest.TestCase):
    def test_red_presets_keep_other_engines_and_source_pivot(self):
        self.assertTrue({"generic", "godot", "unity", "unreal", "cyberpunk", "witcher3"} <= ENGINES.keys())
        for engine, version in (("cyberpunk", "2.5"), ("witcher3", "5.01")):
            args = parse_asset(["in.glb", "out", "--engine", engine, "--formats", "glb"])
            self.assertEqual((args.game_version, args.pivot), (version, "source"))
            if engine == "witcher3":
                self.assertIn("fbx", args.formats)
            args = parse_asset(["in.glb", "out", "--engine", engine, "--pivot", "base", "--game-version", "test.1"])
            self.assertEqual((args.game_version, args.pivot), ("test.1", "base"))
        self.assertEqual(parse_asset(["in.glb", "out"]).pivot, "base")

    def test_red_depot_paths_and_native_check_boundary(self):
        args = parse_asset(["in.glb", "out", "--engine", "cyberpunk", "--red-template", "base\\props\\item.mesh"])
        self.assertEqual(args.red_template, "base/props/item.mesh")
        for flags in (("--engine", "cyberpunk", "--engine-check", "required"),
                      ("--engine", "witcher3", "--red-template", "../item.w2mesh"),
                      ("--engine", "cyberpunk", "--red-template", "/absolute.mesh"),
                      ("--engine", "cyberpunk", "--red-template", "item.w2mesh"),
                      ("--game-version", "2.5")):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_asset(["in.glb", "out", *flags])
        for engine in ("cyberpunk", "witcher3"):
            result = check_engine("unused.json", engine, executable="must-not-run")
            self.assertEqual(result["status"], "not_run")

    def test_process_timeout_and_log(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            log = str(Path(tmp) / "process.log")
            with self.assertRaises(subprocess.TimeoutExpired):
                pa.run_asset_process([sys.executable, "-c", "import time; print('ASSET started',flush=True); time.sleep(5)"], log, .3)
            self.assertIn("ASSET started", Path(log).read_text())

    def test_negative_flag_serialization_and_fresh_failed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fake_run(args, log, timeout):
                self.assertIn("--no-uv-relax", args)
                self.assertNotIn("--uv-relax", args)
                root = Path(tmp) / "output/GameAssets/prop"
                root.mkdir(parents=True)
                (root / "mesh.glb").write_bytes(b"glTF")
                (root / "asset_report.json").write_text(json.dumps({"status": "failed", "validation_passed": True,
                    "lods": [{"files": {"glb": "mesh.glb"}}]}))
                Path(log).write_text("ASSET failed")
                return 0
            with patch.object(pa, "COMFY", tmp), patch.object(pa, "run_asset_process", side_effect=fake_run), \
                    self.assertRaises(SystemExit):
                pa.asset("prop", "source.glb", uv_relax=False)

    def test_preset_and_explicit_overrides(self):
        self.assertEqual(parse_asset(["in.glb", "out", "--preset", "background"]).faces, 5000)
        args = parse_asset(["in.glb", "out", "--preset", "hero", "--faces", "1234", "--texture-size", "512"])
        self.assertEqual((args.faces, args.texture_size), (1234, 512))

    def test_rejects_invalid_production_options(self):
        for flags in (("--lod-ratios", "1,.5,.75"), ("--surface-factor", "nan"), ("--pack-timeout", "0"),
                      ("--engine-check", "required"), ("--size-meters", "-1"), ("--unwrap", "keep")):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_asset(["in.glb", "out", *flags])

    def test_component_budget_is_bounded_and_preserves_small_parts(self):
        result = allocate_budget([1000, 10, 80], [10, .1, 1], 300)
        self.assertEqual(sum(result), 300)
        self.assertTrue(all(0 < x <= y for x, y in zip(result, [1000, 10, 80])))
        self.assertEqual(result[1], 10)
        with self.assertRaises(ValueError):
            allocate_budget([100, 100], [1, 1], 10)
        self.assertEqual(allocate_budget([100, 10], [1, 1], 0), [100, 10])

    def test_region_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "regions.json"
            path.write_text(json.dumps([{"name": "body", "bounds": [[0, 0, 0], [1, .5, 1]], "smooth_iterations": 2}]))
            self.assertEqual(load_regions(path)[0]["name"], "body")
            path.write_text(json.dumps([{"bounds": [[0, 0, 0], [2, 1, 1]]}]))
            with self.assertRaises(ValueError):
                load_regions(path)

    def test_missing_editor_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp, patch("engine_check.shutil.which", return_value=None), \
                patch.dict("os.environ", {}, clear=True):
            path = Path(tmp) / "asset_report.json"
            path.write_text("{}")
            self.assertEqual(check_engine(path, "godot")["status"], "not_run")

    def test_editor_failure_rejects_stale_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engine_check").mkdir()
            (root / "engine_check/result.json").write_text('{"status":"passed","engine":"godot"}')
            path = root / "asset_report.json"
            path.write_text('{"lods":[],"collision":{}}')
            result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch("engine_check.subprocess.run", return_value=result):
                self.assertEqual(check_engine(path, "godot", "fake-godot")["status"], "failed")

    def test_production_cli_preserves_flags(self):
        source, options = pa.production_arguments(["123", "--engine", "unity", "--lod-ratios", "1,.5"], "mesh")
        self.assertEqual(source, "123")
        self.assertEqual(options["engine"], "unity")
        self.assertEqual(options["lod_ratios"], (1, .5))

    def test_asset_runner_requires_fresh_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def fake_run(args, log, timeout):
                Path(log).write_text("ASSET OK")
                return 0
            with patch.object(pa, "COMFY", str(root)), patch.object(pa, "run_asset_process", side_effect=fake_run), \
                    self.assertRaises(SystemExit):
                pa.asset("prop", "source.glb")

    def test_asset_runner_flags_review_required(self):
        for status, expected in (("review_required", True), ("exported", False)):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                def fake_run(args, log, timeout):
                    out = Path(args[args.index("--name") - 1])
                    (out / "meshes").mkdir(parents=True)
                    (out / "meshes/prop_LOD0.glb").write_bytes(b"glTF")
                    (out / "asset_report.json").write_text(json.dumps({
                        "status": status, "validation_passed": True,
                        "lods": [{"files": {"glb": "meshes/prop_LOD0.glb"}}],
                        "quality": {"issues": [{"code": "open_topology", "severity": "review", "message": "holes"}]}}))
                    Path(log).write_text("")
                    return 0
                with patch.object(pa, "COMFY", str(root)), patch.object(pa, "run_asset_process", side_effect=fake_run), \
                        patch("builtins.print") as printed:
                    pa.asset("prop", "source.glb")
                text = " ".join(" ".join(map(str, c.args)) for c in printed.call_args_list)
                self.assertEqual("NOT production-ready" in text, expected)
                self.assertEqual("open_topology" in text, expected)

    def test_portable_provenance_keeps_uvs_after_moving_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "meshes/prop_LOD0.glb"
            path.parent.mkdir()
            path.write_bytes(b"glTF")
            path.with_suffix(".report.json").write_text(json.dumps({"status": "exported", "validation_passed": True,
                "options": {"unwrap": "smart"}, "outputs": ["meshes/prop_LOD0.glb"], "portable_root": ".."}))
            with patch.object(pa.os.path, "exists", return_value=True), patch.object(pa, "prep") as prep, \
                    patch.object(pa, "run", return_value=[]):
                pa.texture("prop", str(path))
                prep.assert_not_called()
