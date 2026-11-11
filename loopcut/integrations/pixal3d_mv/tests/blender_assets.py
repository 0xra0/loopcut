"""Blender production regressions: run as --python tests/blender_assets.py."""
from pathlib import Path
import json
import struct
import sys
import tempfile
import unittest

import bmesh
import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prep_mesh as prep
from asset_config import parse_asset
from asset_geometry import component_pass, bounds, normalize, collision_meshes, atlas, duplicate, build_low_parts, reduce_part
from asset_bake import pixels
from build_asset import main, glb_metrics
from redengine_handoff import cyberpunk_mesh, _glb


class ProductionTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=1)
        obj = bpy.context.object
        for vertex in obj.data.vertices:
            vertex.co *= 1 + .06 * np.sin(vertex.co.z * 10) * np.sin(vertex.co.x * 8)
        material = bpy.data.materials.new("Source")
        material.use_nodes = True
        shader = material.node_tree.nodes.get("Principled BSDF")
        shader.inputs["Base Color"].default_value = (.7, .05, .01, 1)
        shader.inputs["Metallic"].default_value = .6
        shader.inputs["Roughness"].default_value = .4
        obj.data.materials.append(material)
        source = self.root / "source.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(source))
        return source

    def test_cyberpunk_chunks_preserve_attributes_and_reserved_names(self):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8)
        obj = bpy.context.object
        obj.name = obj.data.name = "submesh_00_LOD_1"
        original_uvs = [x.uv[:] for x in obj.data.uv_layers.active.data]
        original_vertices = len(obj.data.vertices)
        path = self.root / "cyberpunk.glb"
        chunks = cyberpunk_mesh(obj, path, triangle_limit=20)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(x["triangles"] <= 20 and x["vertices"] <= 60 for x in chunks))
        self.assertEqual(sum(x["triangles"] for x in chunks), 224)
        self.assertEqual((obj.name, obj.data.name), ("submesh_00_LOD_1", "submesh_00_LOD_1"))
        self.assertEqual(len(obj.data.vertices), original_vertices)
        self.assertEqual([x.uv[:] for x in obj.data.uv_layers.active.data], original_uvs)
        self.assertEqual(len(bpy.context.scene.objects), 1)
        self.assertNotIn("materials", _glb(path))

    def test_redengine_full_builds_are_manual_handoffs(self):
        source = self.fixture()
        for engine in ("cyberpunk", "witcher3"):
            out = self.root / engine
            report = main([str(source), str(out), "--engine", engine, "--name", "red_test", "--faces", "120",
                           "--texture-size", "256", "--lod-ratios", "1,.5", "--surface-smooth", "0",
                           "--pack-method", "AABB", "--skip-previews", "--formats", "glb"])
            self.assertTrue(report["validation_passed"])
            self.assertEqual(report["engine_check"]["status"], "not_run")
            self.assertEqual(report["status"], "review_required")
            handoff = json.loads((out / "redengine/handoff.json").read_text())
            self.assertFalse(handoff["native_resources_generated"])
            self.assertEqual(len(handoff["lods"]), 2)
            self.assertTrue((out / handoff["collision_reference"]).is_file())
            for lod in handoff["lods"]:
                self.assertTrue((out / lod["mesh"]).is_file())
                self.assertTrue((out / lod["textures"]["normal_dx_lod0"]).is_file())
                if engine == "witcher3":
                    self.assertNotIn(b"UCX_", (out / lod["mesh"]).read_bytes())
            self.assertNotEqual(handoff["lods"][0]["textures"]["basecolor"], handoff["lods"][1]["textures"]["basecolor"])

    def test_component_protection_and_masked_cleanup(self):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8)
        large = bpy.context.object
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=.03, location=(2, 0, 0))
        small = bpy.context.object
        original = [v.co.copy() for v in small.data.vertices]
        prep.activate(large)
        small.select_set(True)
        bpy.ops.object.join()
        args = parse_asset(["source", "out", "--surface-smooth", "3"])
        highs, sources, records, _ = component_pass(large, args, [])
        self.assertEqual(len(highs), 2)
        protected = [p for p, record in zip(highs, records) if record["protected"]]
        self.assertEqual(len(protected), 1)
        self.assertEqual(len(protected[0].data.vertices), len(original))
        self.assertEqual(len(sources), 2)

    def test_normalization_and_convex_collision(self):
        bpy.ops.mesh.primitive_cube_add(size=2, location=(3, 1, 4))
        obj = bpy.context.object
        args = parse_asset(["source", "out", "--size-meters", ".5", "--collision", "hull"])
        normalize(obj, args)
        lo, hi = bounds(obj)
        self.assertAlmostEqual(max(hi - lo), .5)
        self.assertAlmostEqual(lo.z, 0)
        colliders = collision_meshes(obj, args)
        self.assertEqual(len(colliders), 1)
        self.assertEqual(prep.inspect_mesh(colliders[0])["boundary_edges"], 0)

    def test_complete_bake_export_and_lod_bundle(self):
        source = self.fixture()
        out = self.root / "asset"
        report = main([str(source), str(out), "--name", "test_asset", "--faces", "120", "--texture-size", "256",
                       "--lod-ratios", "1,.5", "--surface-smooth", "0", "--pack-method", "AABB",
                       "--size-meters", "1", "--skip-previews", "--engine", "unity", "--engine-check", "off",
                       "--formats", "glb,fbx,blend,obj"])
        self.assertTrue(report["validation_passed"])
        self.assertEqual(len(report["lods"]), 2)
        self.assertEqual(len(report["stages"]), 7)
        self.assertLessEqual(report["lods"][0]["export_metrics"]["triangles"], 120)
        self.assertLess(report["lods"][1]["export_metrics"]["triangles"], report["lods"][0]["export_metrics"]["triangles"])
        for lod in report["lods"]:
            self.assertEqual(glb_metrics(out / lod["files"]["glb"])["triangles"], lod["export_metrics"]["triangles"])
            self.assertTrue((out / lod["files"]["obj"]).is_file())
            fbx = (out / lod["files"]["fbx"]).read_bytes()
            self.assertIn(b"LayerElementTangent", fbx)
        self.assertTrue((out / "review.html").exists())
        self.assertTrue((out / "test_asset.blend").exists())
        self.assertLess(report["bake"]["projection_miss"][0]["fraction"], .02)
        tex = bpy.data.images.load(str(out / report["bake"]["paths"]["metallic_smoothness"]))
        data = pixels(tex)
        mask = data[:, 0] > .1
        self.assertTrue(mask.any())
        self.assertAlmostEqual(float(np.median(data[mask, 0])), .6, delta=.02)
        self.assertAlmostEqual(float(np.median(data[mask, 3])), .6, delta=.02)
        # Normal maps contain projected high-to-low detail, rather than just the neutral normal.
        normal = bpy.data.images.load(str(out / report["bake"]["paths"]["normal_lod0"]))
        normal_data = pixels(normal)
        covered = normal_data[:, 2] > .5
        self.assertGreater(float(np.std(normal_data[covered, 0])), .01)
        glb = (out / report["lods"][0]["files"]["glb"]).read_bytes()
        length = struct.unpack_from("<I", glb, 12)[0]
        material = json.loads(glb[20:20 + length])["materials"][0]
        self.assertEqual(material.get("alphaMode", "OPAQUE"), "OPAQUE")
        self.assertFalse(material.get("doubleSided", False))
        self.assertTrue(report["bake"]["lods_use_independent_validated_atlases"])
        self.assertNotEqual(report["lods"][0]["textures"]["basecolor"], report["lods"][1]["textures"]["basecolor"])

    def test_texture_from_bakes_painted_lod0_onto_every_lod(self):
        source = self.fixture()
        common = ["--name", "t", "--faces", "120", "--texture-size", "256", "--lod-ratios", "1,.5",
                  "--surface-smooth", "0", "--pack-method", "AABB", "--size-meters", "1", "--skip-previews",
                  "--engine-check", "off", "--formats", "glb"]
        first = main([str(source), str(self.root / "a"), *common])
        # Paint LOD0 the way UltraTex does: same triangles and UVs, a new base colour texture.
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath=str(self.root / "a" / first["lods"][0]["files"]["glb"]))
        obj = next(o for o in bpy.context.scene.objects if o.type == "MESH")
        image = bpy.data.images.new("paint", 64, 64)
        image.pixels.foreach_set(np.tile(np.array([.05, .2, .9, 1], np.float32), 64 * 64))
        tree = obj.data.materials[0].node_tree
        node = tree.nodes.new("ShaderNodeTexImage")
        node.image = image
        tree.links.new(node.outputs[0], next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED").inputs["Base Color"])
        painted = self.root / "painted.glb"
        bpy.ops.export_scene.gltf(filepath=str(painted), export_format="GLB")
        report = main([str(source), str(self.root / "b"), *common, "--texture-from", str(painted)])
        for lod in report["lods"]:
            data = pixels(bpy.data.images.load(str(self.root / "b" / lod["textures"]["basecolor"])))
            covered = data[np.max(data[:, :3], axis=1) > .02, :3]
            self.assertGreater(float(np.median(covered[:, 2])), .7)  # the fixture's own colour is red
            self.assertLess(float(np.median(covered[:, 0])), .2)
        with self.assertRaisesRegex(ValueError, "atlas"):
            main([str(source), str(self.root / "c"), *common[:3], "60", *common[4:], "--texture-from", str(painted)])

    def test_lod_reduces_every_component_by_the_lod_ratio(self):
        # Chestplate shape: one large shell plus many tiny protected parts.
        bpy.ops.mesh.primitive_uv_sphere_add(segments=128, ring_count=64, radius=1)
        highs = [bpy.context.object]
        for i in range(200):  # chestplate: 205 studs of ~15 triangles at 2.5% of the shell's size
            bpy.ops.mesh.primitive_cone_add(vertices=8, radius1=.025, depth=.02, location=(3 + i * .06, 0, 0))
            highs.append(bpy.context.object)
        for obj in highs:
            prep.edit_mesh(obj, lambda bm: bmesh.ops.triangulate(bm, faces=list(bm.faces)))
        args = parse_asset(["source", "out", "--lod-ratios", "1,.5,.25"])
        _, targets = build_low_parts(highs, args, lod=2)
        big = len(highs[0].data.polygons)
        self.assertGreater(targets[0], .22 * big)  # sqrt(area) weighting left the shell at 12%
        self.assertLessEqual(sum(targets), int(sum(len(x.data.polygons) for x in highs) * .25))

    def test_closed_part_reduces_to_its_minimum_instead_of_being_restored(self):
        # A chestplate stud at its 4-triangle LOD minimum. Decimating toward 3 triangles collapsed the closed
        # cone into two coincident faces, sanitize dropped them, and the stud came back at full size.
        bpy.ops.mesh.primitive_cone_add(vertices=8, radius1=.025, depth=.02)
        stud = bpy.context.object
        prep.edit_mesh(stud, lambda bm: bmesh.ops.triangulate(bm, faces=list(bm.faces)))
        result = reduce_part(stud, 4, "stud_low", parse_asset(["source", "out"]))
        self.assertNotIn("pa_reduction_fallback", result)
        self.assertLessEqual(len(result.data.polygons), 4)

    def test_region_mask_changes_only_selected_surface(self):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=12)
        obj = bpy.context.object
        args = parse_asset(["source", "out", "--surface-smooth", "0"])
        region = {"name": "upper", "bounds": [[0, 0, .5], [1, 1, 1]], "smooth_iterations": 3}
        highs, sources, records, _ = component_pass(obj, args, [region])
        before = np.array([v.co[:] for v in sources[0].data.vertices])
        after = np.array([v.co[:] for v in highs[0].data.vertices])
        self.assertTrue(np.allclose(before[before[:, 2] < -.2], after[before[:, 2] < -.2]))
        self.assertGreater(float(np.max(np.linalg.norm(before - after, axis=1))), .001)
        self.assertGreater(records[0]["region_actions"][0]["smoothed_vertices"], 0)

    def test_uv_atlas_preserves_component_identity(self):
        bpy.ops.mesh.primitive_cube_add(location=(-2, 0, 0))
        a = bpy.context.object
        b = duplicate(a, "Second")
        b.location.x = 2
        args = parse_asset(["source", "out", "--pack-method", "AABB"])
        parts, report = atlas([a, b], args)
        self.assertEqual(len(parts), 2)
        self.assertGreater(report["metrics"]["coverage"], .3)
        self.assertTrue(all(len(x.data.polygons) == 12 for x in parts))

    def test_failed_build_keeps_previous_output(self):
        source = self.fixture()
        out = self.root / "existing"
        out.mkdir()
        (out / "sentinel.txt").write_text("previous valid build")
        with self.assertRaises(ValueError):
            main([str(source), str(out), "--name", "test", "--faces", "1", "--texture-size", "256",
                  "--skip-previews", "--pack-method", "AABB"])
        self.assertEqual((out / "sentinel.txt").read_text(), "previous valid build")

    def test_named_source_and_visible_mesh_selection(self):
        bpy.ops.mesh.primitive_cube_add()
        visible = bpy.context.object
        visible.name = "VisibleLow"
        bpy.ops.mesh.primitive_cube_add(location=(5, 0, 0))
        hidden = bpy.context.object
        hidden.name = "HiddenHigh"
        hidden.hide_render = True
        path = self.root / "mixed.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(path))
        obj = prep.import_mesh(str(path), visible_only=True)
        self.assertEqual(len(obj.data.vertices), 8)
        obj = prep.import_mesh(str(path), object_name="HiddenHigh", visible_only=True)
        self.assertEqual(len(obj.data.vertices), 8)
        self.assertGreater(obj.location.x, 4)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProductionTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise RuntimeError("Production Blender tests failed")
