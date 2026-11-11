"""Real Blender regression tests; creates fixtures in a temporary directory.

blender -b --factory-startup --python-exit-code 1 --python tests/blender_integration.py
"""
from pathlib import Path
import sys
import tempfile
import unittest

import bpy
import bmesh

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prep_mesh as prep
from mesh_config import parse


class MeshTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cube(self):
        bpy.ops.mesh.primitive_cube_add()
        return bpy.context.object

    def save(self, obj, name="input.blend"):
        path = self.root / name
        bpy.ops.wm.save_as_mainfile(filepath=str(path))
        return path

    def test_repair_fills_closed_boundary_removes_loose_and_keeps_small_parts(self):
        obj = self.cube()
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bmesh.ops.delete(bm, geom=[next(iter(bm.faces))], context="FACES_ONLY")
        bm.verts.new((5, 5, 5))
        # A separate four-face tetrahedron must survive the default cleanup.
        a, b, c, d = [bm.verts.new(p) for p in ((3, 0, 0), (3.1, 0, 0), (3, .1, 0), (3, 0, .1))]
        for face in ((a, b, c), (a, d, b), (a, c, d), (b, d, c)):
            bm.faces.new(face)
        args = parse(["unused", "unused2", "--hole-max-diameter", "0"])
        stats = prep.repair(bm, args)
        self.assertEqual(stats["holes_filled"], 1)
        self.assertGreater(stats["loose_elements"], 0)
        self.assertEqual(len(bm.faces), 10)
        self.assertTrue(all(e.is_manifold for e in bm.edges))
        bm.free()

    def test_smart_uv_exports_without_topology_tearing(self):
        src = self.save(self.cube())
        report = prep.main([str(src), str(self.root / "out.glb"), "--faces", "0", "--materials", "discard",
                            "--strict", "--formats", "glb,obj,fbx,blend"])
        self.assertTrue(report["validation_passed"])
        self.assertTrue(report["closed_manifold"])
        self.assertEqual(report["after"]["triangles"], 12)
        self.assertEqual(report["after"]["vertices"], 8)
        self.assertEqual(report["after"]["invalid_uv_corners"], 0)
        self.assertEqual(len(report["outputs"]), 4)

    def test_custom_normals_clear_without_splitting_geometry(self):
        obj = self.cube()
        obj.data.normals_split_custom_set([(1, 0, 0)] * len(obj.data.loops))
        self.assertTrue(obj.data.has_custom_normals)
        prep.shade(obj, 35)
        self.assertFalse(obj.data.has_custom_normals)
        self.assertEqual(len(obj.data.vertices), 8)
        self.assertEqual(prep.inspect_mesh(obj)["boundary_edges"], 0)

    def test_opt_in_voxel_remesh_and_smoothing(self):
        src = self.save(self.cube())
        report = prep.main([str(src), str(self.root / "voxel.glb"), "--faces", "0", "--materials", "discard",
                            "--voxel-size", "0.04", "--smooth-iterations", "1", "--strict"])
        self.assertTrue(report["closed_manifold"])
        self.assertTrue(report["validation_passed"])
        self.assertGreater(report["after"]["vertices"], 8)

    def test_pbr_transfer_bakes_color_onto_smart_uv(self):
        obj = self.cube()
        mat = bpy.data.materials.new("SourceRed")
        mat.use_nodes = True
        shader = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
        shader.inputs["Base Color"].default_value = (.8, .05, .02, 1)
        shader.inputs["Roughness"].default_value = .6
        shader.inputs["Metallic"].default_value = .4
        obj.data.materials.append(mat)
        src = self.save(obj)
        report = prep.main([str(src), str(self.root / "baked.glb"), "--faces", "0", "--texture-size", "256"])
        self.assertTrue(report["has_materials"])
        self.assertEqual(len(report["textures"]), 4)
        image = bpy.data.images.load(report["textures"][0])
        pixels = list(image.pixels)
        red = [pixels[i] for i in range(0, len(pixels), 4) if pixels[i + 3] > .95]
        self.assertTrue(red)
        self.assertGreater(sum(red) / len(red), .5)

    def test_keep_without_uv_rejects_instead_of_silently_unwrapping(self):
        obj = self.cube()
        while obj.data.uv_layers:
            obj.data.uv_layers.remove(obj.data.uv_layers[0])
        src = self.save(obj)
        with self.assertRaisesRegex(ValueError, "requires existing UVs"):
            prep.main([str(src), str(self.root / "bad.glb"), "--unwrap", "keep"])

    def test_bowtie_vertex_splits_into_two_closed_shells(self):
        bm = bmesh.new()
        center = bm.verts.new((0, 0, 0))
        verts = [bm.verts.new(p) for p in ((1, 0, 0), (0, 1, 0), (0, 0, 1),
                                          (-1, 0, 0), (0, -1, 0), (0, 0, -1))]
        for a, b, c in (verts[:3], verts[3:]):
            for face in ((center, a, b), (center, c, a), (center, b, c), (a, c, b)):
                bm.faces.new(face)
        stats = prep.repair(bm, parse(["unused", "unused2"]))
        self.assertEqual(stats["split_vertices"], 1)
        self.assertEqual(len(bm.verts), 8)
        self.assertTrue(all(v.is_manifold for v in bm.verts))
        self.assertTrue(all(e.is_manifold for e in bm.edges))
        bm.free()

    def test_uv_fallback_separates_overlapping_triangles(self):
        obj = self.cube()
        prep.edit_mesh(obj, lambda bm: bmesh.ops.triangulate(bm, faces=list(bm.faces)))
        def overlap(bm):
            uv = bm.loops.layers.uv.active
            for face in bm.faces:
                for loop, coord in zip(face.loops, ((0, 0), (1, 0), (0, 1))):
                    loop[uv].uv = coord
        prep.edit_mesh(obj, overlap)
        args = parse(["unused", "unused2"])
        self.assertGreater(prep.inspect_mesh(obj, True)["uv_overlap_pairs"], 0)
        # Folded clusters are re-unwrapped first, so a fully folded cube may need no isolated faces.
        prep.fix_uv_overlaps(obj, args)
        quality = prep.inspect_mesh(obj, True)
        self.assertEqual(quality["uv_overlap_pairs"], 0)
        self.assertEqual(quality["zero_area_uv_faces"], 0)

    def test_isolated_surface_is_not_capped_onto_itself(self):
        bm = bmesh.new()
        verts = [bm.verts.new(p) for p in ((0, 0, 0), (1, 0, 0), (0, 1, 0))]
        bm.faces.new(verts)
        stats = prep.repair(bm, parse(["unused", "unused2", "--hole-max-diameter", "0"]))
        self.assertEqual(stats["holes_filled"], 0)
        self.assertEqual(len(bm.faces), 1)
        bm.free()

    def test_open_dust_is_removed_but_tiny_closed_part_stays(self):
        obj = self.cube()
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.faces.new([bm.verts.new(p) for p in ((3, 0, 0), (3.01, 0, 0), (3, .01, 0))])
        a, b, c, d = [bm.verts.new(p) for p in ((-3, 0, 0), (-2.99, 0, 0), (-3, .01, 0), (-3, 0, .01))]
        for face in ((a, b, c), (a, d, b), (a, c, d), (b, d, c)):
            bm.faces.new(face)
        stats = prep.repair(bm, parse(["unused", "unused2"]))
        self.assertEqual(stats["dust_faces_removed"], 1)
        self.assertEqual(len(bm.faces), 10)
        self.assertTrue(all(e.is_manifold for e in bm.edges))
        bm.free()

    def test_default_dust_removes_sliver_just_over_one_percent(self):
        # vampire_chestplate_ab: a loose flat two-triangle sliver at 1.15% of the diagonal
        # survived the old 0.01 default and flagged open_topology on every LOD.
        obj = self.cube()
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        length = 0.0115 * prep.diagonal(bm)
        a, b, c, d = [bm.verts.new(p) for p in ((0.2, 0.2, 0.2), (0.2 + length, 0.2, 0.2),
                                                (0.2 + length, 0.2, 0.203), (0.2, 0.2, 0.203))]
        bm.faces.new((a, b, c))
        bm.faces.new((a, c, d))
        stats = prep.repair(bm, parse(["unused", "unused2"]))
        self.assertEqual(stats["dust_faces_removed"], 2)
        self.assertEqual(len(bm.faces), 6)
        self.assertFalse(any(e.is_boundary for e in bm.edges))
        bm.free()

    def test_source_up_z_stands_a_lying_model_upright(self):
        obj = self.cube()
        obj.scale = (1, 3, 1)  # lying: long along Blender Y, as Pixal3D multiview GLBs import
        src = self.save(obj)
        upright = prep.import_mesh(str(src), source_up="z")
        self.assertAlmostEqual(upright.dimensions.z, 6, places=4)
        self.assertAlmostEqual(upright.dimensions.y, 2, places=4)
        lying = prep.import_mesh(str(src))
        self.assertAlmostEqual(lying.dimensions.y, 6, places=4)

    def test_yaw_turns_about_up_after_standing_upright(self):
        obj = self.cube()
        obj.scale = (1, 3, 1)       # lying, long along Blender Y
        obj.location = (2, 0, 0)    # off-centre, so the turn direction shows in the location
        src = self.save(obj)
        turned = prep.import_mesh(str(src), source_up="z", yaw=90)
        # Yaw after the stand-up keeps the model tall; before it, the long axis would end up along X.
        self.assertAlmostEqual(turned.dimensions.z, 6, places=4)
        # +90 is counter-clockwise seen from above: +X goes to +Y.
        self.assertAlmostEqual(turned.matrix_world.translation.x, 0, places=4)
        self.assertAlmostEqual(turned.matrix_world.translation.y, 2, places=4)
        back = prep.import_mesh(str(src), source_up="z", yaw=180)
        self.assertAlmostEqual(back.matrix_world.translation.x, -2, places=4)

    def test_first_preview_is_the_gltf_front(self):
        import types
        import numpy as np
        import asset_quality
        plate = self.cube()
        plate.scale = (.5, .05, 1)
        bpy.ops.mesh.primitive_cube_add(location=(.8, 0, .6))
        arm = bpy.context.object
        arm.scale = (.3, .05, .1)
        bpy.ops.object.select_all(action="SELECT")
        bpy.context.view_layer.objects.active = plate
        bpy.ops.object.join()
        args = types.SimpleNamespace(preview_size=64, turntable_views=2)
        asset_quality.previews([plate], [plate], args, self.root / "previews")
        image = bpy.data.images.load(str(self.root / "previews" / "high_00.png"))
        alpha = np.empty(len(image.pixels), dtype=np.float32)
        image.pixels.foreach_get(alpha)
        mask = alpha.reshape(64, 64, 4)[..., 3] > .5            # rows bottom-up
        rows = np.nonzero(mask.any(1))[0]
        arm_row = rows[np.argmax(mask[rows].sum(1))]          # the widest row runs through the arm
        plate_row = rows[0] + 3                               # near the bottom: plate only
        arm_cols, plate_cols = np.nonzero(mask[arm_row])[0], np.nonzero(mask[plate_row])[0]
        # glTF front (+Z) is Blender -Y; seen from there, +X (the arm) sticks out on the right.
        self.assertGreater(arm_cols.max(), plate_cols.max() + 3)
        self.assertLessEqual(abs(int(arm_cols.min()) - int(plate_cols.min())), 1)

    def test_strict_rejects_open_surface_and_does_not_export(self):
        bpy.ops.mesh.primitive_plane_add()
        src = self.save(bpy.context.object)
        output = self.root / "reject.glb"
        with self.assertRaisesRegex(ValueError, "validation failed"):
            prep.main([str(src), str(output), "--materials", "discard", "--strict"])
        self.assertFalse(output.exists())
        self.assertTrue(output.with_suffix(".report.json").exists())

    def test_image_texture_survives_smart_uv_transfer(self):
        obj = self.cube()
        mat = bpy.data.materials.new("Checker")
        mat.use_nodes = True
        shader = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
        image = bpy.data.images.new("SourceChecker", width=16, height=16)
        image.pixels[:] = [v for y in range(16) for x in range(16)
                           for v in ((1, 0, 0, 1) if (x // 4 + y // 4) % 2 else (0, 1, 0, 1))]
        image.pack()
        tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
        tex.image = image
        tex.interpolation = "Closest"
        mat.node_tree.links.new(tex.outputs["Color"], shader.inputs["Base Color"])
        obj.data.materials.append(mat)
        src = self.save(obj)
        report = prep.main([str(src), str(self.root / "checker.glb"), "--faces", "0", "--texture-size", "256"])
        result = bpy.data.images.load(report["textures"][0])
        pixels = list(result.pixels)
        red = sum(pixels[i] > .9 and pixels[i + 1] < .1 for i in range(0, len(pixels), 4))
        green = sum(pixels[i + 1] > .9 and pixels[i] < .1 for i in range(0, len(pixels), 4))
        self.assertGreater(red, 1000)
        self.assertGreater(green, 1000)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(MeshTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise RuntimeError("Blender regression tests failed")
