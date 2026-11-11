"""Copy safety, bounded pruning and interrupted-session recovery with synthetic files."""
import ast
import json
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_storage_recovery import StorageFixture, MB
from loopcut import checkpoints as cp, conversations as cv, files, state, workspace
from loopcut.tools import ToolError
from loopcut.ui import layout


class PruningTest(StorageFixture):
    def store(self, session_id='a'*32, count=8):
        store = cp.Store(self.root, session_id)
        store.folder.mkdir(parents=True)
        for n in range(count):
            row = dict(id=f'{n:032x}', size=128, kind='turn', expired=False, created=n)
            store.entries.append(row)
            store.snapshot_path(row['id']).write_bytes(b'x'*128)
        store.save_index()
        return store

    def test_scene_at_or_above_budget_does_not_erase_restore_points_or_refuse(self):
        for size in (4096, 8192):
            with self.subTest(size=size):
                store = self.store(session_id=('a' if size == 4096 else 'b')*32, count=2)
                with patch.object(cp, 'storage_budget', return_value=4096), \
                        patch('shutil.disk_usage', return_value=(10**12, 0, 10**12)):
                    cp.require_space(self.root/'copy.blend', size)
                self.assertTrue(all(store.snapshot_path(e['id']).exists() for e in store.entries))
                import shutil
                shutil.rmtree(store.folder)

    def test_active_newest_checkpoints_survive_other_copy_preflight(self):
        store = self.store()
        session = state.new_session()
        session['id'] = store.folder.name
        with patch.object(state, 'session', return_value=session), \
                patch.object(cp, 'storage_budget', return_value=0), \
                patch('shutil.disk_usage', return_value=(10**12, 0, 10**12)):
            cp.require_space(self.root/'jobs'/'scene.blend', 8192)
        self.assertTrue(all(store.snapshot_path(e['id']).exists() for e in store.entries[-cp.KEEP_NEWEST:]))

    def test_legacy_workspace_is_not_age_pruned_under_budget(self):
        ws = self.root/'workspaces'/('b'*32)
        ws.mkdir(parents=True)
        (ws/'asset.fbx').write_bytes(b'legacy export')
        os.utime(ws, (1, 1))
        cp.cleanup_storage(budget_bytes=10**6)
        self.assertTrue((ws/'asset.fbx').exists())

    def test_clean_up_reclaims_disposable_files_when_disk_full_under_budget(self):
        ws = self.root/'workspaces'/('b'*32)
        ws.mkdir(parents=True)
        (ws/workspace.SOURCE).write_bytes(b'x'*4096)
        with patch('shutil.disk_usage', return_value=(100*MB, 100*MB, 0)):
            result = cp.cleanup_storage(budget_bytes=10**9, reclaim_bytes=4096)
        self.assertFalse(ws.exists())
        self.assertGreaterEqual(result['removed'], 4096)

    def test_cleanup_card_reclaims_files_even_when_under_budget(self):
        ws = self.root/'workspaces'/('b'*32)
        ws.mkdir(parents=True)
        (ws/workspace.SOURCE).write_bytes(b'x'*4096)
        source = Path(__file__).resolve().parents[2]/'scripts/addons_core/loopcut/ui/host.py'
        node = next(n for n in ast.parse(source.read_text()).body if getattr(n, 'name', '') == '_do_action')
        namespace = dict(state=state, checkpoints=cp)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
        session = state.new_session()
        with patch.object(cp, 'storage_budget', return_value=10**9):
            namespace['_do_action'](session, ('storage_cleanup', None))
        self.assertFalse(ws.exists())

    def test_missing_volume_has_bounded_parent_search(self):
        calls = []
        def missing(path):
            calls.append(path)
            if len(calls) > 30:
                raise AssertionError('parent search did not terminate at root')
            return False
        with patch.object(cp, 'cleanup_storage', return_value={'bytes': 0}), \
                patch.object(Path, 'exists', missing):
            with self.assertRaises(cp.CheckpointError):
                cp.require_space(Path('/offline/copy.blend'), 1)

    def test_postwrite_budget_retains_new_snapshot_and_valid_index(self):
        session = state.new_session()
        def write(filepath, **kwargs):
            Path(filepath).write_bytes(b'x'*8192)
            return {'FINISHED'}
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath=''),
            ops=types.SimpleNamespace(wm=types.SimpleNamespace(save_as_mainfile=write)))
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(cp, 'storage_budget', return_value=4096), \
                patch.object(state, 'session', return_value=session), \
                patch.object(cp, 'require_space'), \
                patch('shutil.disk_usage', return_value=(10**12, 0, 10**12)):
            entry = cp._write_snapshot(cp._store(session), 'turn', 'Synthetic checkpoint')
        self.assertEqual(cp.status(session, entry['id']), 'ok')

    def test_cleanup_removes_stale_orphan_snapshots_and_tails(self):
        store = self.store(count=1)
        orphan = store.snapshot_path('f'*32)
        tail = store.tail_path('f'*32)
        for path in (orphan, tail):
            path.write_bytes(b'orphan')
            os.utime(path, (1, 1))
        cp.cleanup_storage(budget_bytes=10**9)
        self.assertFalse(orphan.exists())
        self.assertFalse(tail.exists())

    def test_wave_full_disk_pauses_once_and_later_wave_can_run(self):
        session = state.new_session()
        with patch.object(cp, '_write_snapshot', side_effect=cp.StorageFullError('Disk full')) as write:
            with self.assertRaises(cp.StorageFullError):
                cp.ensure_for_wave(session, 'Wave', 0, 0)
            self.assertEqual(cp.ensure_for_wave(session, 'Later wave', 0, 0), '')
        write.assert_called_once()
        self.assertTrue(session['checkpoints_disabled'])
        self.assertEqual(sum(bool(i.get('storage')) for i in session['items']), 1)

    def test_workspace_snapshot_reports_storage_failure_as_tool_error(self):
        with patch.object(workspace, 'ensure', return_value=self.root), \
                patch.object(workspace, '_save_snapshot', side_effect=cp.StorageFullError('Disk full')):
            with self.assertRaises(ToolError):
                workspace.snapshot_scene()

    def test_storage_display_reads_cached_size(self):
        with patch.object(cp, 'storage_size', side_effect=AssertionError('UI walked storage')):
            self.assertIsInstance(cp.cached_storage_size(), (int, type(None)))

    def test_startup_returns_while_storage_scan_is_still_running(self):
        package = Path(__file__).resolve().parents[2]/'scripts/addons_core/loopcut'
        source = package/'lifecycle.py'
        function = '_initial_resume'
        if 'checkpoints.schedule_cleanup()' not in source.read_text():
            source, function = package/'__init__.py', 'register'
        register = next(n for n in ast.parse(source.read_text()).body if getattr(n, 'name', '') == function)
        node = next(n for n in register.body if isinstance(n, ast.Try)
                    and any(isinstance(i, ast.ImportFrom) and any(a.name == 'checkpoints' for a in i.names)
                            for i in n.body))
        started, release = threading.Event(), threading.Event()
        def scan(**kwargs):
            self.assertIsNot(threading.current_thread(), threading.main_thread(), 'Startup scanned storage inline')
            started.set()
            self.assertTrue(release.wait(2), 'The main thread never regained control')
        with patch.object(cp, 'remove_stale_sessions', side_effect=scan):
            try:
                exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), {'__package__': 'loopcut'})
                self.assertTrue(started.wait(1))
            finally:
                release.set()
                worker = getattr(cp, '_CLEANUP_THREAD', None)
                if worker is not None:
                    worker.join(2)


class RecoveryTest(StorageFixture):
    def test_cleanly_closed_untitled_chat_is_not_offered(self):
        session = state.new_session()
        session['items'].append(state.item_user('Add a cube'))
        with patch.object(cv, '_LAUNCH', 'older-launch'):
            cv.save(session)
        self.assertIsNone(cv.recovery_candidate())

    def test_metadata_recovery_search_does_not_read_clean_conversation_bodies(self):
        session = state.new_session()
        session['items'].append(state.item_user('Add a cube'))
        cv.save(session)
        original = Path.read_text
        def read(path, *args, **kwargs):
            self.assertNotEqual(path.name, 'conversation.json')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            self.assertIsNone(cv.recovery_candidate())

    def callback(self, current_path='', load_error=False, matches=True):
        source = Path(__file__).resolve().parents[2]/'scripts/addons_core/loopcut/lifecycle.py'
        node = next(n for n in ast.parse(source.read_text()).body if getattr(n, 'name', '') == '_on_load_post')
        session = state.new_session()
        candidate = {'id': session['id']}
        conversations = types.SimpleNamespace(project_key=cv.project_key,
            list_for=Mock(return_value=[candidate] if matches else []),
            recovery_candidate=Mock(return_value=candidate), load=Mock(return_value=session),
            attach_untitled=Mock(), add_project=Mock(), save=Mock(), new_scene=Mock())
        if load_error:
            conversations.load.side_effect = cv.ConversationError('Unreadable synthetic file')
        resume, register, switch = Mock(), Mock(), Mock()
        namespace = dict(__package__='loopcut', persistent=lambda f: f, state=types.SimpleNamespace(restoring=False, session=lambda: session),
            bpy=types.SimpleNamespace(data=types.SimpleNamespace(filepath=current_path),
                app=types.SimpleNamespace(timers=types.SimpleNamespace(is_registered=lambda f: False, register=register))),
            conversations=conversations, switch_to=switch, resume_for_open_file=resume, _dock_after_load=lambda: None)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
        namespace['_on_load_post'](str(self.root/'autosave.blend'))
        return conversations, resume, register, switch, session

    def test_saved_project_without_chat_does_not_adopt_unrelated_chat(self):
        cv_mock, resume, register, switch, session = self.callback(str(self.root/'project.blend'), matches=False)
        cv_mock.recovery_candidate.assert_not_called()
        cv_mock.load.assert_not_called()
        resume.assert_called_once()
        register.assert_called_once()

    def test_untitled_native_recovery_selects_interrupted_chat(self):
        cv_mock, resume, register, switch, session = self.callback()
        cv_mock.recovery_candidate.assert_called_once()
        switch.assert_called_once_with(session)
        register.assert_called_once()

    def test_unreadable_recovery_chat_falls_back_and_still_registers_dock(self):
        cv_mock, resume, register, switch, session = self.callback(load_error=True)
        resume.assert_called_once()
        register.assert_called_once()

    def test_recovery_banner_has_action_without_inserting_transcript_item(self):
        session = state.new_session()
        candidate = {'id': 'a'*32}
        before = list(session['items'])
        display = layout.build(session, 480, 800, 1.0, lambda f, s, t: len(t)*s*.5,
                               ui={'recovery_conversation': candidate})
        self.assertIn(('open_conversation', candidate['id']), [h['action'] for h in display['hits']])
        self.assertEqual(session['items'], before)


class PublicationTest(StorageFixture):
    def export(self, *, extension='.fbx', emitted=None, existing=False):
        session = state.new_session()
        ws, visible = self.root/'workspace', self.root/'project'
        ws.mkdir(exist_ok=True)
        visible.mkdir(exist_ok=True)
        raw = ws/(('project' if extension == '.blend' else 'asset')+extension)
        output = raw.with_suffix(emitted) if emitted else raw
        if existing:
            (visible/output.name).write_bytes(b'original')
        module = types.ModuleType('bpy.ops')
        received = []
        def create(submodule, name):
            def write(**kwargs):
                received.append(kwargs['filepath'])
                Path(kwargs['filepath']).with_suffix(output.suffix).write_bytes(b'new output')
                return {'FINISHED'}
            return write
        module._op_create_function = create
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath=str(visible/'project.blend')))
        with patch.dict(sys.modules, {'bpy': fake, 'bpy.ops': module}), \
                patch.object(files, 'blend_base', return_value=(visible, '')), \
                patch.object(files, '_temp_roots', return_value=(self.root/'workspace',)), \
                patch.object(state, 'session', return_value=session):
            with files.watching_deliverables():
                module._op_create_function('wm' if extension == '.blend' else 'export_scene',
                    'save_as_mainfile' if extension == '.blend' else 'gltf' if extension == '.gltf' else 'fbx')(filepath=str(raw))
        return session, raw, output, visible, received

    def test_export_keeps_requested_working_copy(self):
        session, raw, output, visible, received = self.export()
        self.assertEqual(received, [str(raw)])
        self.assertTrue(raw.exists())
        self.assertEqual((visible/raw.name).read_bytes(), b'new output')

    def test_export_never_overwrites_visible_existing_file(self):
        session, raw, output, visible, received = self.export(existing=True)
        self.assertEqual((visible/output.name).read_bytes(), b'original')
        self.assertEqual((visible/'Export-2'/output.name).read_bytes(), b'new output')

    def test_gltf_operator_extension_change_is_successfully_published(self):
        session, raw, output, visible, received = self.export(extension='.gltf', emitted='.glb')
        self.assertTrue(output.exists())
        self.assertTrue((visible/'asset.glb').exists())
        self.assertEqual(session['items'][-1]['saved_path'], str(visible/'asset.glb'))

    def test_save_copy_named_like_user_project_does_not_replace_original(self):
        session, raw, output, visible, received = self.export(extension='.blend', existing=True)
        self.assertEqual(received, [str(raw)])
        self.assertEqual((visible/output.name).read_bytes(), b'original')

    def test_background_intermediates_are_not_published(self):
        ws, visible = self.root/'workspace', self.root/'project'
        ws.mkdir()
        (ws/'preview.png').write_bytes(b'preview')
        (ws/'draft.blend').write_bytes(b'draft')
        rows = workspace._publish_outputs(state.new_session(), ws, {}, workspace.files_state(ws), visible, lambda f: f())
        self.assertEqual(rows, [])
        self.assertFalse(visible.exists())

    def test_explicit_export_folder_publishes_sidecars_together(self):
        ws, visible = self.root/'workspace', self.root/'project'
        exports = ws/'exports'
        (exports/'textures').mkdir(parents=True)
        (exports/'asset.gltf').write_text('synthetic')
        (exports/'asset.bin').write_bytes(b'buffer')
        (exports/'textures'/'map.png').write_bytes(b'texture')
        rows = workspace._publish_outputs(state.new_session(), ws, {}, workspace.files_state(ws), visible, lambda f: f())
        self.assertEqual(len(rows), 3)
        self.assertTrue((visible/'textures'/'map.png').exists())

    def test_path_resolution_alone_does_not_create_visible_folders(self):
        visible = self.root/'project'
        with patch.object(files, 'blend_base', return_value=(visible, '')):
            files.deliverable_path(str(self.root/'temporary'/'inspection'))
        self.assertFalse(visible.exists())

    def test_windows_documents_uses_known_folder_api(self):
        import ctypes
        expected = str(self.root/'redirected-documents')
        def locate(folder, flags, token, result):
            result._obj.value = expected
            return 0
        api = types.SimpleNamespace(shell32=types.SimpleNamespace(SHGetKnownFolderPath=locate),
                                   ole32=types.SimpleNamespace(CoTaskMemFree=Mock()))
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath=''))
        session = state.new_session()
        with patch.object(sys, 'platform', 'win32'), patch.object(ctypes, 'windll', api, create=True), \
                patch.dict(sys.modules, {'bpy': fake}), patch.object(state, 'session', return_value=session), \
                patch.object(Path, 'home', return_value=self.root/'home'):
            folder = files.blend_base()[0]
            self.assertTrue(folder.is_relative_to(Path(expected)))

    def test_inspection_render_stays_temporary_without_selecting_documents(self):
        from loopcut import job_tools, jobs
        render = types.SimpleNamespace(engine='BLENDER_EEVEE', fps=24, fps_base=1)
        scene = types.SimpleNamespace(render=render, name='Scene', camera=None)
        fake = types.SimpleNamespace(context=types.SimpleNamespace(scene=scene),
            data=types.SimpleNamespace(filepath=''), app=types.SimpleNamespace(binary_path='/blender',
                build_hash=b'synthetic', version_string='1.0'))
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(jobs, 'validate_spec'), \
                patch.dict(os.environ, {'LOOPCUT_DATA_DIR': str(self.root/'data')}), \
                patch.object(files, 'deliverable_path', side_effect=AssertionError('Inspection selected Documents')):
            spec, path = job_tools.create_spec(str(self.root/'inspection'), [1], 5,
                width=8, height=8, samples=1, _allow_no_camera=True)
        self.assertTrue(Path(spec['output']).is_relative_to(self.root/'inspection'))


if __name__ == '__main__':
    unittest.main()
