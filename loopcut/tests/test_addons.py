"""The pure half of addons: the one line that names installed add-ons to the model."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import addons  # noqa: E402


def entry(name, module, ops=(), description=""):
    return {"name": name, "module": module, "operators": list(ops), "description": description}


class LineTest(unittest.TestCase):
    def test_nothing_installed_is_no_line(self):
        self.assertEqual(addons.line([]), "")

    def test_operators_are_grouped_by_module_with_a_shared_prefix(self):
        line = addons.line([entry("Node Wrangler", "bl_ext.blender_org.node_wrangler",
                                  ["node.nw_add_textures", "node.nw_merge", "node.nw_swap", "wm.nw_import"],
                                  "Various tools to enhance and speed up node-based workflow")])
        self.assertEqual(line, "Installed add-ons: Node Wrangler [bl_ext.blender_org.node_wrangler] "
                               "bpy.ops.node.nw_* (3), bpy.ops.wm.nw_import: "
                               "Various tools to enhance and speed up node-based workflow")

    def test_a_prefix_is_cut_at_an_underscore_or_dropped(self):
        self.assertIn("bpy.ops.probe.say_* (2)", addons.line([entry("P", "p", ["probe.say_hi", "probe.say_bye"])]))
        self.assertIn("bpy.ops.probe.* (2)", addons.line([entry("P", "p", ["probe.alpha", "probe.beta"])]))

    def test_long_descriptions_and_long_lists_are_cut(self):
        long = "x" * 200
        self.assertIn("…", addons.line([entry("A", "a", description=long)]))
        many = [entry(f"Add-on {i:02d}", f"a{i}") for i in range(addons.MAX_LISTED + 4)]
        line = addons.line(many)
        self.assertIn("+4 more", line)
        self.assertNotIn("Add-on 13", line)

    def test_only_the_largest_operator_groups_are_shown(self):
        ops = [f"{m}.op_{i}" for m in ("a", "b", "c", "d", "e") for i in range(2)] + ["a.op_2"]
        line = addons.line([entry("X", "x", ops)])
        self.assertIn("bpy.ops.a.op_* (3)", line)
        self.assertIn("+2 more modules", line)
        self.assertNotIn("bpy.ops.e.", line)

    def test_installed_but_off_add_ons_are_a_count_with_how_to_find_them(self):
        line = addons.line([entry("A", "a")], off_count=95)
        self.assertIn("95 more installed add-ons are off: inspect_api search", line)
        self.assertNotIn("are off", addons.line([entry("A", "a")]))


OFF = [{"module": "bl_ext.user_default.zenuv", "name": "Zen UV", "category": "UV", "description": "UV toolkit"},
       {"module": "auto_unwrap", "name": "Auto Unwrap 2", "category": "UV", "description": "Automatic UV unwrapping"},
       {"module": "BlenRig", "name": "BlenRig 6", "category": "Rigging", "description": "BlenRig 6 rigging system"}]


class OffTest(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        patch = mock.patch.object(addons, "installed_off", return_value=OFF)
        patch.start()
        self.addCleanup(patch.stop)

    def test_search_matches_every_word_in_name_category_or_description(self):
        hits = addons.search_off("uv")
        self.assertIn("addon_utils.enable('<module>', default_set=True)", hits[0])
        self.assertEqual(hits[1:], ["Zen UV [bl_ext.user_default.zenuv] (UV): UV toolkit",
                                    "Auto Unwrap 2 [auto_unwrap] (UV): Automatic UV unwrapping"])
        self.assertEqual(addons.search_off("automatic uv")[1:], ["Auto Unwrap 2 [auto_unwrap] (UV): Automatic UV unwrapping"])
        self.assertEqual(addons.search_off("cloth"), [])

    def test_naming_an_off_add_on_says_how_to_turn_it_on_without_running_it(self):
        # Turning one on runs its register(): that goes through run_python, which the user approves, and
        # inspect_api, which runs unasked, only reads. A test addon_utils records any enable.
        import types
        from unittest import mock
        enabled = []
        addon_utils = types.SimpleNamespace(enable=lambda *a, **k: enabled.append(a))
        with mock.patch.dict(sys.modules, {"addon_utils": addon_utils}):
            self.assertIsNone(addons.off_notice("bpy.ops.mesh.primitive_cube_add"))
            text = addons.off_notice("auto_unwrap")
        self.assertEqual(enabled, [])
        self.assertIn("Auto Unwrap 2 [auto_unwrap]", text)
        self.assertIn("run_python", text)
        self.assertIn("addon_utils.enable('auto_unwrap', default_set=True)", text)


if __name__ == "__main__":
    unittest.main()
