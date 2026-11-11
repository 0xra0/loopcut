"""Native Claude Blender Integration adapter tests, no Blender binary needed."""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
bpy = types.ModuleType('bpy')
bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault('bpy', bpy)
from loopcut import studio, tools, agent


class StudioTest(unittest.TestCase):
    def test_schemas_present_in_main_agent(self):
        names = {s['function']['name'] for s in agent._tool_schemas()}
        self.assertTrue({'studio_guide', 'studio_action'} <= names)
        self.assertTrue({'studio_guide', 'studio_action'} <= set(tools._DISPATCH))

    def test_safety_and_approval(self):
        self.assertFalse(agent._changes_scene('studio_action', '{"command":"mesh_report"}'))
        self.assertTrue(agent._changes_scene('studio_action', '{"command":"sculpt_stroke"}'))
        self.assertTrue(tools.is_heavy('studio_action', {'command':'bake_maps'}))
        self.assertFalse(tools.is_heavy('studio_action', {'command':'uv_report'}))
        self.assertTrue(tools.installs('studio_action', {'command':'auto_rig_unirig'}))
        self.assertFalse(tools.installs('studio_action', {'command':'quad_remesh'}))
        self.assertTrue(tools.installs('studio_action', {'command':'image_to_3d'}))
        self.assertEqual(agent._approval('studio_action','{"command":"sculpt_stroke"}',None),'heavy')
        self.assertEqual(agent._approval('studio_action','{"command":"mesh_report"}',None),'')

    def test_catalog_and_playbooks(self):
        def fake(x): return x
        entries = {'sculpt_stroke': {'fn': fake, 'toolset': 'sculpt', 'doc': 'Sculpt.'}}
        with mock.patch.object(studio, '_registry', return_value=entries):
            self.assertIn('sculpt_stroke', studio.guide(toolset='sculpt').text)
            self.assertIn('sculpt_stroke(x)', studio.guide(command='sculpt_stroke').text)
        self.assertIn('retopo_and_bake', studio.guide(playbook='list').text)
        self.assertIn('uv_report', studio.guide(playbook='retopo_and_bake').text.lower())

    def test_native_python_bypass_not_available(self):
        with self.assertRaises(tools.InputMistake):
            studio.action('run_python', {'code': 'print(1)'}, 'bypass')
        self.assertNotIn('run_python', studio.READ_ONLY)

    def test_result_png_payload_is_not_passed_to_the_model(self):
        import base64
        png = base64.b64encode(b'\x89PNG\r\n\x1a\n0000').decode()
        value, path = studio._images({'images':[{'view':'front','png_base64':png}]})
        self.assertTrue(path.is_file())
        self.assertNotIn('png_base64', str(value))
        path.unlink()


def _load_pixal3d():
    """studio_vendor/handlers/pixal3d.py alone, under a stub package: the real one imports every handler (mathutils)."""
    import importlib.util
    package = types.ModuleType('p3d_stub')
    package.__path__ = []
    package.CommandError = type('CommandError', (Exception,), {})
    package.command = lambda name, toolset: (lambda fn: fn)
    image_to_3d = types.ModuleType('p3d_stub.image_to_3d')
    image_to_3d._import = lambda path, name, height, base_z: {'name': name, 'path': path}
    sys.modules.update({'p3d_stub': package, 'p3d_stub.image_to_3d': image_to_3d})
    source = Path(studio.__file__).with_name('studio_vendor') / 'handlers' / 'pixal3d.py'
    spec = importlib.util.spec_from_file_location('p3d_stub.pixal3d', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Pixal3DTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.p3d = _load_pixal3d()
        self.tmp = Path(tempfile.mkdtemp(prefix='p3d_test_'))
        self.addCleanup(__import__('shutil').rmtree, self.tmp, True)
        # Its own bpy: other test modules put different stubs in sys.modules first.
        self.p3d.bpy = types.SimpleNamespace(app=types.SimpleNamespace(binary_path='/usr/bin/blender', tempdir=str(self.tmp)),
                                             path=types.SimpleNamespace(abspath=lambda p: p))
        self.env = mock.patch.dict('os.environ', {'PIXAL3D_DIR': str(self.tmp / 'kit'), 'COMFY_DIR': str(self.tmp / 'comfy'),
                                                  'COMFY_PYTHON': sys.executable})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.up = mock.patch.object(self.p3d.urllib.request, 'urlopen')
        self.up.start()
        self.addCleanup(self.up.stop)

    def _kit(self, body):
        (self.tmp / 'kit').mkdir()
        (self.tmp / 'kit' / 'pa.py').write_text(body)

    def _wait(self, job_id):
        import time
        for _ in range(200):
            if self.p3d._jobs[job_id]['state'] != 'running':
                return
            time.sleep(0.05)
        self.fail('job never finished')

    def test_runs_pa_all_and_imports_the_recorded_lod0(self):
        self._kit("import json, os, sys\n"
                  "print('args', sys.argv[1:]); print('blender', os.environ['BLENDER_BIN'])\n"
                  "out = os.path.join(os.environ['COMFY_DIR'], 'output')\n"
                  "os.makedirs(os.path.join(out, 'PixelArtistry'))\n"
                  "glb = os.path.join(out, 'lod0.glb'); open(glb, 'w').close()\n"
                  "json.dump({'stages': {'mesh': {'outputs': ['/missing.glb']}, 'asset': {'outputs': [glb]}}},\n"
                  "          open(os.path.join(out, 'PixelArtistry', sys.argv[2] + '.run.json'), 'w'))\n"
                  "print('LOD0 done')\n")
        job = self.p3d.pixal3d_generate('sword', 'a steel longsword', seed=7, tries=2)['job_id']
        self._wait(job)
        status = self.p3d.pixal3d_status(job, height=1.2)
        self.assertEqual(status['state'], 'done', status)
        self.assertEqual(status['imported']['path'], str(self.tmp / 'comfy' / 'output' / 'lod0.glb'))
        self.assertEqual(status['imported']['name'], 'sword')
        self.assertEqual(status['step'], 'LOD0 done')
        log = Path(status['log']).read_text()
        self.assertIn("['all', 'sword', 'text:a steel longsword', 'a steel longsword', '7', '--tries', '2']", log)
        self.assertIn('blender /usr/bin/blender', log)

    def test_failed_run_reports_the_last_line(self):
        self._kit("import sys\nprint('stage mesh'); print('only 3.1 GiB VRAM free'); sys.exit(2)\n")
        job = self.p3d.pixal3d_generate('sword', 'a steel longsword')['job_id']
        self._wait(job)
        status = self.p3d.pixal3d_status(job)
        self.assertEqual(status['state'], 'failed')
        self.assertIn('code 2: only 3.1 GiB VRAM free', status['error'])
        self.assertNotIn('imported', status)

    def test_refuses_bad_input_and_a_second_gpu_job(self):
        self._kit("import time\ntime.sleep(30)\n")
        with self.assertRaises(self.p3d.CommandError):
            self.p3d.pixal3d_generate('two words', 'a sword')
        with self.assertRaises(self.p3d.CommandError):
            self.p3d.pixal3d_generate('sword', 'a sword', image_path=str(self.tmp / 'none.png'))
        job = self.p3d.pixal3d_generate('sword', 'a sword', max_minutes=0.01)['job_id']
        with self.assertRaises(self.p3d.CommandError):
            self.p3d.pixal3d_generate('shield', 'a shield')
        self._wait(job)
        self.assertIn('stopped pa.py', self.p3d.pixal3d_status(job)['error'])

    def test_needs_approval_every_call(self):
        self.assertTrue(tools.installs('studio_action', {'command': 'pixal3d_generate'}))
        self.assertFalse(tools.installs('studio_action', {'command': 'pixal3d_status'}))


if __name__ == '__main__':
    unittest.main()
