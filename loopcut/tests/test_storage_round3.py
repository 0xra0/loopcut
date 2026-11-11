"""Storage refusal, workspace copies and native untitled recovery with synthetic files."""
import ast
import json
import sys
import threading
import types
from pathlib import Path
from unittest.mock import Mock, patch

from test_storage_recovery import StorageFixture
from loopcut import checkpoints as cp, conversations as cv, files, state, workspace
from loopcut.ui import layout
from loopcut.tools import ToolError

PACKAGE = Path(__file__).resolve().parents[2] / 'scripts/addons_core/loopcut'


def function(module, name, namespace):
    source = PACKAGE / module
    node = next(n for n in ast.parse(source.read_text()).body if getattr(n, 'name', '') == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace[name]


class StorageTest(StorageFixture):
    def copies(self):
        store = cp.Store(self.root, 'a' * 32)
        store.folder.mkdir(parents=True)
        for n in range(8):
            row = dict(id=f'{n:032x}', size=128, kind='turn', expired=False, created=n, message_index=0, item_index=0)
            store.entries.append(row)
            store.snapshot_path(row['id']).write_bytes(b'x' * 128)
        store.save_index()
        ws = self.root / 'workspaces' / ('b' * 32)
        ws.mkdir(parents=True)
        (ws / 'source.blend').write_bytes(b'x' * 1024)
        orphan = store.snapshot_path('f' * 32)
        orphan.write_bytes(b'orphan')
        import os
        os.utime(orphan, (1, 1))
        return store, ws

    def test_unreclaimable_shortfall_leaves_all_files_and_index_unchanged(self):
        self.copies()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(cp, 'storage_budget', return_value=0), \
                patch('shutil.disk_usage', return_value=(10**9, 10**9, 10)):
            with self.assertRaises(cp.StorageFullError):
                cp.require_space(self.root / 'copy.blend', 500 * 1024**2)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(after, before)

    def test_restore_render_and_wave_refuse_without_deleting_other_copies(self):
        from loopcut import job_tools, jobs
        store, ws = self.copies()
        session = state.new_session()
        session['id'] = store.folder.name
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath=''),
            context=types.SimpleNamespace(objects_in_mode=[], view_layer=types.SimpleNamespace(update=Mock())),
            ops=types.SimpleNamespace(wm=types.SimpleNamespace(save_as_mainfile=Mock())))
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(state, 'session', return_value=session), \
                patch.object(cp, '_blocking_job', return_value=None), \
                patch.object(cp, 'storage_budget', return_value=0), \
                patch.object(job_tools, 'external_assets', return_value=([], [])), \
                patch('shutil.disk_usage', return_value=(10**9, 10**9, 10)):
            calls = (
                (cp.CheckpointError, lambda: cp.restore(session, store.entries[0]['id'])),
                (jobs.JobError, lambda: job_tools.save_snapshot({'output': str(self.root / 'output')}, self.root / 'new-job')),
                (cp.CheckpointError, lambda: cp.ensure_for_wave(session, 'Wave', 0, 0)),
            )
            for error, call in calls:
                with self.subTest(call=call), self.assertRaises(error):
                    call()
                after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                self.assertEqual(after, before)

    def test_unavailable_volume_from_disk_query_pauses_checkpoints(self):
        session = state.new_session()
        cp.begin_turn(session, state.item_user('Add a cube'))
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath=''),
            ops=types.SimpleNamespace(wm=types.SimpleNamespace(save_as_mainfile=Mock())))
        with patch.dict(sys.modules, {'bpy': fake}), \
                patch('shutil.disk_usage', side_effect=OSError('Volume disconnected')):
            with self.assertRaises(cp.CheckpointError):
                cp.ensure_for_turn(session)
        self.assertTrue(session.get('checkpoints_disabled'))
        self.assertIn('unavailable', session['items'][-1]['text'].lower())

    def test_sufficient_disposable_space_is_reclaimed_before_copy(self):
        store, ws = self.copies()
        initial = cp.storage_size(self.root)
        def disk(path):
            return (10**9, 0, cp.FREE_RESERVE + initial - cp.storage_size(self.root))
        with patch.object(cp, 'storage_budget', return_value=10**9), patch('shutil.disk_usage', side_effect=disk):
            cp.require_space(self.root / 'copy.blend', 1024)
        self.assertGreaterEqual(initial - cp.storage_size(self.root), 1024)

    def test_discard_serializes_index_update_and_uses_unique_temporary_name(self):
        store, ws = self.copies()
        session = state.new_session()
        session['id'] = store.folder.name
        original = cp.os.replace
        temporaries = []
        def replace(source, target):
            self.assertTrue(cp._STORAGE_LOCK._is_owned(), 'Index update escaped the storage lock')
            temporaries.append(Path(source).name)
            return original(source, target)
        with patch.object(cp.os, 'replace', side_effect=replace):
            cp.discard(session, store.entries[0]['id'])
            cp.discard(session, store.entries[1]['id'])
        self.assertEqual(len(set(temporaries)), 2)
        self.assertNotIn('index.json.tmp', temporaries)
        self.assertEqual(len(cp.Store(self.root, session['id']).entries), 6)

    def test_background_scan_does_not_hold_lock_during_recursive_walk(self):
        self.copies()
        started, release = threading.Event(), threading.Event()
        original = cp.storage_size
        def scan(path=None):
            if threading.current_thread() is not threading.main_thread():
                started.set()
                self.assertTrue(release.wait(2))
            return original(path)
        with patch.object(cp, 'storage_size', side_effect=scan), \
                patch.object(cp, 'storage_budget', return_value=10**9):
            cp.schedule_cleanup()
            try:
                self.assertTrue(started.wait(1))
                acquired = cp._STORAGE_LOCK.acquire(timeout=.1)
                if acquired:
                    cp._STORAGE_LOCK.release()
                self.assertTrue(acquired, 'Background filesystem walk blocked the main thread')
            finally:
                release.set()
                cp._CLEANUP_THREAD.join(2)

    def test_cleanup_reclaims_a_retired_folder_left_by_a_stopped_process(self):
        retired = self.root / 'workspaces' / ('.cleanup-' + 'c' * 32)
        retired.mkdir(parents=True)
        (retired / 'source.blend').write_bytes(b'x' * 1024)
        cp.cleanup_storage(budget_bytes=0)
        self.assertFalse(retired.exists())

    def test_background_deletion_does_not_hold_lock_during_rmtree(self):
        self.copies()
        original = cp.shutil.rmtree
        def remove(path, *args, **kwargs):
            self.assertFalse(cp._STORAGE_LOCK._is_owned(), 'Recursive deletion held the storage lock')
            return original(path, *args, **kwargs)
        with patch.object(cp.shutil, 'rmtree', side_effect=remove):
            cp.cleanup_storage(budget_bytes=0)

    def test_failed_scan_reports_unavailable_size(self):
        with patch.object(cp, 'remove_stale_sessions', side_effect=RuntimeError('Synthetic scan failure')):
            cp.schedule_cleanup()
            cp._CLEANUP_THREAD.join(2)
        with self.assertRaises(OSError):
            cp.cached_storage_size()

    def test_cleanup_scheduling_failure_still_docks(self):
        dock = Mock()
        from loopcut import bundled_addons
        with patch.object(bundled_addons, 'startup_cleanup'), \
                patch.object(cp, 'schedule_cleanup', side_effect=RuntimeError('Cannot start thread')):
            function('lifecycle.py', '_initial_resume', dict(__package__='loopcut',
                resume_for_open_file=Mock(), _dock_if_missing=dock))()
        dock.assert_called_once()

    def test_unavailable_volume_card_does_not_claim_disk_full(self):
        session = state.new_session()
        cp.begin_turn(session, state.item_user('Add a cube'))
        with patch.object(cp, '_write_snapshot', side_effect=cp.StorageFullError('The storage volume is unavailable.')):
            with self.assertRaises(cp.StorageFullError):
                cp.ensure_for_turn(session)
        text = next(i['text'] for i in session['items'] if i.get('storage'))
        self.assertIn('unavailable', text.lower())
        self.assertNotIn('disk is full', text.lower())


class WorkspaceWriteTest(StorageFixture):
    def test_relative_and_absolute_workspace_writes_publish_without_overwriting_project(self):
        ws, visible = self.root / 'workspaces' / ('a' * 32), self.root / 'project'
        ws.mkdir(parents=True)
        visible.mkdir()
        session = state.new_session()
        session['mode'] = 'workspace'
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath=str(visible / 'scene.blend')))
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(state, 'session', return_value=session), \
                patch.object(workspace, '_ROOTS', {ws}), patch.object(workspace, 'current', return_value=ws), \
                patch.object(files, 'blend_base', return_value=(visible, '')), \
                patch.object(files, '_forbidden_roots', return_value=()):
            for n, suffix in enumerate(('.obj', '.gltf', '.mtl', '.usda', '.dae', '.ply', '.stl')):
                name = f'model{suffix}'
                (visible / name).write_text('original project file')
                raw = (str(visible / name) if n == 5 else '//'+name if n == 6 else
                       str(ws / name) if n % 2 else name)
                result = files.write_file(raw, 'working export', overwrite=True)
                self.assertEqual((visible / name).read_text(), 'original project file')
                self.assertEqual((ws / name).read_text(), 'working export')
                delivered = Path(session['items'][-1]['saved_path'])
                self.assertEqual(delivered.read_text(), 'working export')
                self.assertNotEqual(delivered, visible / name)
                self.assertIn(str(delivered), result.text)


    def test_temp_redirect_in_workspace_never_replaces_project_file(self):
        ws, visible, temporary = self.root / 'working', self.root / 'project', self.root / 'temp'
        for folder in (ws, visible, temporary):
            folder.mkdir()
        existing = visible / 'model.obj'
        existing.write_text('original project file')
        session = state.new_session()
        with patch.object(workspace, 'current', return_value=ws), patch.object(workspace, '_ROOTS', {ws}), \
                patch.object(files, 'blend_base', return_value=(visible, '')), \
                patch.object(files, '_temp_roots', return_value=(temporary,)), \
                patch.object(files, '_forbidden_roots', return_value=()), \
                patch.object(state, 'session', return_value=session):
            files.write_file(str(temporary / existing.name), 'working export', overwrite=True)
        self.assertEqual(existing.read_text(), 'original project file')
        self.assertEqual((ws / existing.name).read_text(), 'working export')

    def test_project_redirect_refuses_a_workspace_symlink_back_to_project(self):
        ws, visible = self.root / 'working', self.root / 'project'
        ws.mkdir()
        visible.mkdir()
        existing = visible / 'model.obj'
        existing.write_text('original project file')
        (ws / existing.name).symlink_to(existing)
        with patch.object(workspace, 'current', return_value=ws), patch.object(workspace, '_ROOTS', {ws}), \
                patch.object(files, 'blend_base', return_value=(visible, '')), \
                patch.object(files, '_forbidden_roots', return_value=()):
            with self.assertRaises(ToolError):
                files.write_file(str(existing), 'replacement', overwrite=True)
        self.assertEqual(existing.read_text(), 'original project file')


class PublicationTest(StorageFixture):
    def test_operator_publishes_only_changed_outputs(self):
        self.export(visible=False)

    def test_project_folder_notices_name_only_changed_output(self):
        self.export(visible=True)

    def test_operator_matches_case_insensitively_on_desktop_platforms(self):
        for platform in ('win32', 'darwin'):
            with self.subTest(platform=platform), patch.object(sys, 'platform', platform):
                self.export(visible=False, mixed_case=True)

    def export(self, *, visible, mixed_case=False):
        session = state.new_session()
        ws, project = self.root / 'working', self.root / 'project'
        ws.mkdir(exist_ok=True)
        project.mkdir(exist_ok=True)
        folder = project if visible else ws
        output = folder / 'robot.glb'
        output.write_bytes(b'old')
        for suffix in ('.blend', '.png'):
            (folder / ('robot' + suffix)).write_bytes(b'unrelated sibling')
        module = types.ModuleType('bpy.ops')
        def create(*args):
            def write(**kwargs):
                output.write_bytes(b'changed export')
                return {'FINISHED'}
            return write
        module._op_create_function = create
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath=''))
        requested = output.with_name('Robot.glb') if mixed_case else output
        with patch.dict(sys.modules, {'bpy': fake, 'bpy.ops': module}), \
                patch.object(state, 'session', return_value=session), \
                patch.object(files, 'blend_base', return_value=(project, '')):
            with files.watching_deliverables():
                module._op_create_function('export_scene', 'gltf')(filepath=str(requested))
        self.assertEqual([Path(i['saved_path']).name for i in session['items'] if i.get('saved_path')], ['robot.glb'])
        self.assertFalse((project / 'robot.png').exists() and not visible)
        self.assertFalse((project / 'robot.blend').exists() and not visible)

    def test_background_publishes_only_exports_changed_in_this_call(self):
        ws, visible = self.root / 'working', self.root / 'project'
        exports = ws / 'exports'
        exports.mkdir(parents=True)
        (exports / 'old.glb').write_bytes(b'old export')
        before = workspace.files_state(ws)
        (exports / 'new.glb').write_bytes(b'new export')
        rows = workspace._publish_outputs(state.new_session(), ws, before, workspace.files_state(ws), visible, lambda f: f())
        self.assertEqual(len(rows), 1)
        self.assertFalse((visible / 'old.glb').exists())
        self.assertTrue((visible / 'new.glb').exists())


class RecoveryTest(StorageFixture):
    def setUp(self):
        super().setUp()
        from loopcut import jobs
        self.enterContext(patch.object(jobs, '_alive', return_value=False))
        self.enterContext(patch.object(cv, '_SCENE', 'a' * 32))

    def saved(self, running=False):
        session = state.new_session()
        session['items'].append(state.item_user('Add a cube'))
        with patch.object(cv, '_LAUNCH', 'older-launch'):
            cv.save(session, running=running)
        return session

    def test_explicit_recovery_reattaches_clean_previous_launch(self):
        session = self.saved()
        self.assertIsNone(cv.recovery_candidate())
        candidate = cv.recovery_candidate(interrupted_only=False, untitled_scene=session['untitled_scene'])
        self.assertEqual(candidate['id'], session['id'])
        self.assertIsNone(cv.list_for(None)[0].get('turn_running'))

    def test_explicit_recovery_matches_scene_and_skips_saved_projects(self):
        old = self.saved()
        current = state.new_session()
        current['untitled_scene'] = 'b' * 32
        current['items'].append(state.item_user('Add a sphere'))
        cv.save(current)
        saved = state.new_session()
        saved['items'].append(state.item_user('Add a cone'))
        cv.add_project(saved, str(self.root / 'project.blend'))
        with patch.object(cv, '_LAUNCH', 'older-launch'):
            cv.save(saved)
        self.assertEqual(cv.recovery_candidate(interrupted_only=False, untitled_scene=old['untitled_scene'])['id'], old['id'])

    def test_native_untitled_temp_path_reattaches_chat_after_interrupted_or_clean_quit(self):
        for running in (False, True):
            for name in ('quit.blend', '123_autosave.blend'):
                with self.subTest(running=running, name=name):
                    session = self.saved(running)
                    self.callback(session, str(self.root / name), str(self.root / name))

    def callback(self, expected, current_path, loaded):
        switch, resume = Mock(), Mock()
        empty = state.new_session()
        namespace = dict(__package__='loopcut', persistent=lambda f: f, conversations=cv, switch_to=switch,
            resume_for_open_file=resume, _dock_after_load=lambda: None,
            state=types.SimpleNamespace(restoring=False, session=lambda: empty),
            bpy=types.SimpleNamespace(data=types.SimpleNamespace(filepath=current_path),
                context=types.SimpleNamespace(scene={'loopcut_untitled_scene': expected['untitled_scene']}),
                app=types.SimpleNamespace(timers=types.SimpleNamespace(is_registered=lambda f: True))))
        with patch.object(files, '_temp_roots', return_value=(self.root,)), \
                patch.dict(sys.modules, {'bpy': namespace['bpy']}):
            function('lifecycle.py', '_on_load_post', namespace)(loaded)
        switch.assert_called_once()
        self.assertEqual(switch.call_args.args[0]['id'], expected['id'])
        resume.assert_not_called()

    def test_recovery_banner_can_be_dismissed_without_opening_chat(self):
        candidate = {'id': 'a' * 32}
        session = state.new_session()
        ui = {'recovery_conversation': candidate}
        display = layout.build(session, 480, 800, 1.0, lambda f, s, t: len(t)*s*.5, ui=ui)
        self.assertIn(('recovery_dismiss', None), [h['action'] for h in display['hits']])
        with patch.object(state, 'ui', ui):
            function('ui/host.py', '_do_action', dict(state=state))(session, ('recovery_dismiss', None))
        self.assertNotIn('recovery_conversation', ui)
        self.assertEqual(session['items'], [])

    def test_workspace_source_uses_plain_public_literal(self):
        source = (PACKAGE / 'workspace.py').read_text()
        assignment = next(n for n in ast.parse(source).body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'SOURCE' for t in n.targets))
        self.assertIsInstance(assignment.value, ast.Constant)
        self.assertEqual(assignment.value.value, 'source.blend')
