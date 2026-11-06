"""engine_export.py's pure half: each engine's preset and exporter options, the texture cap, the FBX read
back from a parsed element tree, the budget lines, and how the tool is classified and asked for.
No Blender: harness/engine_export_check.py exports real scenes."""

import sys
import types
import unittest
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import agent, engine_export as E, files, tools  # noqa: E402

Elem = namedtuple("FBXElem", ("id", "props", "props_type", "elems"))  # io_scene_fbx.parse_fbx's.


def elem(name: bytes, props=(), elems=()):
    return Elem(name, list(props), b"", list(elems))


class PresetTest(unittest.TestCase):
    def test_each_engine_has_its_format_and_limits(self):
        self.assertEqual({e: E.PRESETS[e]["formats"][0] for e in E.ENGINES},
                         {"godot": "glb", "unity": "fbx", "unreal": "fbx", "roblox": "fbx"})
        self.assertIn("gltf", E.PRESETS["roblox"]["formats"], "Roblox's importer takes .gltf, not .glb")
        self.assertEqual(E.PRESETS["roblox"]["mesh_triangles"], 20_000)
        self.assertEqual(E.PRESETS["roblox"]["texture_cap"], 4096)
        self.assertEqual(E.PRESETS["unreal"]["texture_cap"], 8192)
        self.assertTrue(E.PRESETS["unreal"]["flip_green"])
        self.assertFalse(any(E.PRESETS[e]["flip_green"] for e in ("godot", "unity", "roblox")), "OpenGL-style")
        self.assertEqual([e for e in E.ENGINES if E.PRESETS[e]["lods"]], ["unity"])
        self.assertEqual(E.preset(" Godot ")["engine"], "godot")
        with self.assertRaises(tools.InputMistake):
            E.preset("cryengine")

    def test_fbx_options_triangulate_with_tangents_and_named_clips(self):
        unity = E.fbx_options("unity", armature=False, animated=False)
        for key, value in {"use_tspace": True, "use_triangles": True, "mesh_smooth_type": "FACE", "embed_textures": True,
                           "path_mode": "COPY", "add_leaf_bones": False, "bake_anim": False, "axis_up": "Y",
                           "axis_forward": "-Z", "apply_scale_options": "FBX_SCALE_ALL", "bake_space_transform": True,
                           "bake_anim_use_all_actions": True, "use_selection": True}.items():
            self.assertEqual(unity[key], value, key)
        self.assertFalse(E.fbx_options("unity", armature=True, animated=True)["bake_space_transform"],
                         "a rig keeps Blender's axis bake off")
        self.assertTrue(E.fbx_options("unity", armature=True, animated=True)["bake_anim"])
        self.assertEqual(E.fbx_options("roblox", armature=False, animated=False)["apply_scale_options"], "FBX_SCALE_UNITS")
        self.assertNotIn("CAMERA", E.fbx_options("unreal", armature=False, animated=False)["object_types"])

    def test_gltf_options(self):
        one = E.gltf_options(animated_objects=1)
        self.assertEqual((one["export_format"], one["export_animation_mode"], one["export_tangents"], one["export_apply"],
                          one["export_yup"]), ("GLB", "BROADCAST", True, True, True))
        self.assertEqual(E.gltf_options(animated_objects=2)["export_animation_mode"], "ACTIONS")
        self.assertEqual(E.gltf_options(fmt="gltf")["export_format"], "GLTF_EMBEDDED")
        self.assertTrue(E.gltf_options(shape_keys=True)["export_def_bones"])
        self.assertNotIn("export_def_bones", E.gltf_options())

    def test_textures_are_scaled_to_the_cap_on_their_long_side(self):
        self.assertIsNone(E.scaled((2048, 2048), 4096))
        self.assertIsNone(E.scaled((8192, 8192), None))
        self.assertEqual(E.scaled((8192, 4096), 4096), (4096, 2048))
        self.assertEqual(E.scaled((3000, 9000), 4096), (1365, 4096))
        self.assertIsNone(E.scaled((0, 0), 4096))


class ReadBackTest(unittest.TestCase):
    def fbx(self):
        quads = elem(b"PolygonVertexIndex", [[0, 1, -3, 0, 2, -4]])  # Two triangles.
        tri_body = elem(b"Geometry", [11, b"Crate\x00\x01Geometry", b"Mesh"],
                        [quads, elem(b"LayerElementNormal"), elem(b"LayerElementTangent")])
        ngon = elem(b"Geometry", [12, b"Lid\x00\x01Geometry", b"Mesh"],
                    [elem(b"PolygonVertexIndex", [[0, 1, 2, 3, -5]])])  # One pentagon: 3 triangles.
        shape = elem(b"Geometry", [13, b"Key\x00\x01Geometry", b"Shape"])
        objects = elem(b"Objects", (), [
            tri_body, ngon, shape,
            elem(b"Model", [21, b"Crate\x00\x01Model", b"Mesh"]), elem(b"Model", [22, b"Lid\x00\x01Model", b"Mesh"]),
            elem(b"Model", [23, b"Armature\x00\x01Model", b"Null"]),
            elem(b"AnimationStack", [31, b"Armature|Walk\x00\x01AnimStack", b""]),
            elem(b"AnimationStack", [32, b"Armature|Run\x00\x01AnimStack", b""]),
            elem(b"Material", [41, b"Wood\x00\x01Material", b""]),
            elem(b"Video", [51, b"wood.png\x00\x01Video", b"Clip"], [elem(b"Content", [b"\x89PNG..."])]),
            elem(b"Video", [52, b"lost.png\x00\x01Video", b"Clip"], [elem(b"Content", [b""])]),
        ])
        links = elem(b"Connections", (), [elem(b"C", [b"OO", 11, 21]), elem(b"C", [b"OO", 12, 22]),
                                          elem(b"C", [b"OO", 21, 0])])
        return elem(b"", (), [elem(b"FBXHeaderExtension"), objects, links])

    def test_an_fbx_is_summed_from_its_elements(self):
        found = E.fbx_summary(self.fbx())
        self.assertEqual(found["nodes"], {"Crate": {"tris": 2, "size": None, "tangents": True},
                                          "Lid": {"tris": 3, "size": None, "tangents": False}})
        self.assertEqual((found["meshes"], found["tris"], found["materials"], found["images"]), (2, 5, 1, 1))
        self.assertEqual(found["clips"], ["Armature|Walk", "Armature|Run"])

    def test_gltf_clips_and_tangents(self):
        gltf = {"animations": [{"name": "Walk"}, {}], "meshes": [
            {"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1, "TANGENT": 2}},
                            {"attributes": {"POSITION": 0, "TEXCOORD_0": 1}}, {"attributes": {"POSITION": 0}}]}]}
        self.assertEqual(E.gltf_extra(gltf), {"clips": ["Walk", "animation 1"], "mapped": 2, "tangents": 1})

    def test_budget_and_the_engines_cap_are_plain_words(self):
        lines = E.budget_lines("roblox", {"Body": 26_000, "Hat": 900}, 20_000, 20_000)
        self.assertEqual(lines[0], "Over the triangle budget: 26,900 triangles, 6,900 over 20,000 (34%). "
                                   "Decimate or re-topologise the densest: Body 26,000, Hat 900.")
        self.assertEqual(lines[1], "Roblox takes at most 20,000 triangles per mesh: Body has 26,000. Split or "
                                   "decimate those.")
        self.assertEqual(E.budget_lines("godot", {"Crate": 48}, 500, None), ["Within the triangle budget: 48 of 500."])
        self.assertEqual(E.budget_lines("godot", {"Crate": 48}, None, None), [])


class SurfaceTest(unittest.TestCase):
    def test_it_writes_a_file_so_it_asks_and_takes_a_checkpoint(self):
        self.assertIn("export_for_engine", files.WRITE_TOOLS)
        self.assertTrue(files.needs_approval("export_for_engine", {"engine": "godot"}, ()))
        self.assertIn("export_for_engine", tools.CHANGES_SCENE)
        self.assertEqual(agent._approval("export_for_engine", '{"engine": "unity"}', ()), "gate")
        names = [s["function"]["name"] for s in agent._tool_schemas()]
        self.assertIn("export_for_engine", names)
        self.assertIn("export_for_engine", [s["function"]["name"] for s in agent._orchestrator_schemas()])
        from loopcut import workers
        self.assertNotIn("export_for_engine", workers.tool_names(), "workers build; the orchestrator exports")

    def test_its_card_says_what_and_where(self):
        self.assertEqual(E.describe("export_for_engine", {"engine": "unity", "objects": ["Muse"], "path": "//m.fbx"}),
                         ("Export Muse for Unity to //m.fbx", ""))
        self.assertEqual(E.describe("export_for_engine", {"engine": "godot", "overwrite": True}),
                         ("Export (replacing) every deliverable for Godot", ""))
        self.assertIsNone(E.describe("export_glb", {}))

    def test_limits_cite_their_sources(self):
        source = Path(E.__file__).read_text()
        for url in ("https://create.roblox.com/docs/art/modeling/specifications",
                    "https://create.roblox.com/docs/art/modeling/texture-specifications",
                    "https://dev.epicgames.com/documentation/en-us/unreal-engine/textures-in-unreal-engine",
                    "https://docs.godotengine.org/en/stable/tutorials/assets_pipeline/importing_images.html"):
            self.assertIn(url, source)


if __name__ == "__main__":
    unittest.main()
