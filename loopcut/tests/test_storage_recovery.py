"""Storage pressure, untitled history and visible output paths, using synthetic files."""
import errno
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/addons_core'))
sys.modules.setdefault('bpy', types.ModuleType('bpy'))
from loopcut import checkpoints as cp, conversations as cv, files, state, workspace
from loopcut.ui import layout

MB = 1024 * 1024


class StorageFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {'LOOPCUT_DATA_DIR': str(self.root)}))


class StorageRecoveryTest(StorageFixture):
    def test_low_space_refuses_before_writing_and_clears_pending_once(self):
        session = state.new_session()
        item = state.item_user('Add a sphere')
        session['items'].append(item)
        cp.begin_turn(session, item)
        save = Mock()
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath=''),
                                     ops=types.SimpleNamespace(wm=types.SimpleNamespace(save_as_mainfile=save)))
        with patch.dict(sys.modules, {'bpy': fake}), patch('shutil.disk_usage', return_value=(100*MB, 50*MB, 50*MB)):
            with self.assertRaises(cp.CheckpointError):
                cp.ensure_for_turn(session)
            self.assertIsNone(session['pending_checkpoint'])
            cp.ensure_for_turn(session)
            cp.begin_turn(session, state.item_user('Add a cone'))
            cp.ensure_for_turn(session)
        save.assert_not_called()
        cards = [i for i in session['items'] if i.get('storage')]
        self.assertEqual(len(cards), 1)
        display = layout.build(session, 480, 800, 1.0, lambda f, s, t: len(t)*s*.5)
        actions = {h['action'][0] for h in display['hits']}
        self.assertTrue({'storage_cleanup', 'storage_preferences', 'storage_continue'} <= actions)

    def test_enospc_from_writer_is_also_recoverable_and_removes_partial_snapshot(self):
        session = state.new_session()
        cp.begin_turn(session, state.item_user('Add a sphere'))
        def write(filepath, **kwargs):
            Path(filepath).write_bytes(b'partial')
            raise OSError(errno.ENOSPC, 'No space left on device')
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath=''),
            ops=types.SimpleNamespace(wm=types.SimpleNamespace(save_as_mainfile=write)))
        with patch.dict(sys.modules, {'bpy': fake}), patch('shutil.disk_usage', return_value=(10**12, 0, 10**12)):
            with self.assertRaises(cp.CheckpointError):
                cp.ensure_for_turn(session)
        self.assertIsNone(session['pending_checkpoint'])
        self.assertEqual(list(self.root.rglob('*.blend')), [])

    def test_startup_prunes_across_stores_keeps_restore_tail_and_active_job(self):
        store = cp.Store(self.root, 'a'*32)
        store.folder.mkdir(parents=True)
        for n, kind in enumerate(('turn', 'pre_restore')):
            row = {'id': f'{n:032x}', 'size': 1024, 'kind': kind, 'expired': False, 'created': n}
            store.entries.append(row)
            store.snapshot_path(row['id']).write_bytes(b'x'*1024)
        store.tail_path(f'{1:032x}').write_text('{}')
        store.save_index()
        ws = self.root / 'workspaces' / ('b'*32)
        ws.mkdir(parents=True)
        (ws / workspace.SOURCE).write_bytes(b'x'*4096)
        for ident, status in (('c'*32, 'complete'), ('d'*32, 'running')):
            job = self.root / 'jobs' / ident
            job.mkdir(parents=True)
            (job / 'scene.blend').write_bytes(b'x'*1024)
            (job / 'status.json').write_text(json.dumps({'state': status}))
        result = cp.cleanup_storage(budget_bytes=4096)
        self.assertLessEqual(result['bytes'], 4096)
        self.assertFalse(ws.exists())
        self.assertTrue(store.snapshot_path(f'{1:032x}').exists())
        self.assertTrue(store.tail_path(f'{1:032x}').exists())
        self.assertTrue((self.root / 'jobs' / ('d'*32) / 'scene.blend').exists())

    def test_atomic_conversation_failure_keeps_original_and_removes_tmp(self):
        path = self.root / 'conversation.json'
        path.write_text('original')
        with patch.object(cv.os, 'replace', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError):
                cv._write_atomic(path, 'replacement')
        self.assertEqual(path.read_text(), 'original')
        self.assertFalse(path.with_name(path.name+'.tmp').exists())

    def test_midturn_save_failure_is_visible_and_retryable_without_ending_turn(self):
        session = state.new_session()
        session['items'].append(state.item_user('Add a sphere'))
        session['busy'] = True
        with patch.object(cv, '_write_atomic', side_effect=OSError(errno.ENOSPC, 'disk full')):
            cv.save(session)
            cv.save(session)
        self.assertEqual(len([i for i in session['items'] if i.get('save_failed')]), 1)
        self.assertTrue(session['busy'])
        cv.save(session)
        self.assertTrue((cv._folder(session['id']) / 'conversation.json').is_file())


class HistoryRecoveryTest(StorageFixture):
    def test_all_history_includes_untitled_and_recovers_missing_meta(self):
        session = state.new_session()
        session['items'].append(state.item_user('Make a cube'))
        session['messages'].append({'role': 'user', 'content': 'Make a cube'})
        cv.save(session)
        (cv._folder(session['id']) / 'meta.json').unlink()
        rows = cv.list_for(None)
        self.assertEqual([r['id'] for r in rows], [session['id']])
        self.assertEqual(cv.load(rows[0]['id'])['messages'], session['messages'])

    def test_first_save_attaches_all_untitled_chats_from_this_scene(self):
        sessions = [state.new_session(), state.new_session()]
        for session in sessions:
            session['items'].append(state.item_user('Make a cube'))
            cv.save(session)
        cv.attach_untitled(sessions[-1], str(self.root / 'scene.blend'))
        rows = cv.list_for(cv.project_key(str(self.root / 'scene.blend')))
        self.assertEqual({r['id'] for r in rows}, {s['id'] for s in sessions})

    def test_recovery_offer_finds_previous_launch_without_loading_it(self):
        from loopcut import jobs
        session = state.new_session()
        session['items'].append(state.item_user('Make a cube'))
        with patch.object(cv, '_LAUNCH', 'previous-launch'):
            cv.save(session, running=True)
        with patch.object(jobs, '_alive', return_value=False):
            self.assertEqual(cv.recovery_candidate()['id'], session['id'])
        self.assertEqual(cv.load(session['id'])['items'][-1]['text'], cv.CLOSED_MESSAGE)

    def test_all_history_has_a_scope_switch(self):
        display = layout.build(state.new_session(), 480, 800, 1.0, lambda f, s, t: len(t)*s*.5,
                               ui={'view': 'history', 'history_all': True})
        self.assertIn(('history_scope', False), [h['action'] for h in display['hits']])


    def test_recovery_callback_selects_the_original_projects_chat(self):
        import ast
        source = Path(__file__).resolve().parents[2]/'scripts/addons_core/loopcut/lifecycle.py'
        node = next(n for n in ast.parse(source.read_text()).body if getattr(n, 'name', '') == '_on_load_post')
        session = state.new_session()
        original = str(self.root/'project.blend')
        candidate = {'id': session['id']}
        conversations = types.SimpleNamespace(project_key=cv.project_key, list_for=Mock(return_value=[candidate]),
            recovery_candidate=Mock(), load=Mock(return_value=session), attach_untitled=Mock(),
            add_project=Mock(), save=Mock(), new_scene=Mock())
        switch = Mock()
        namespace = {'__package__': 'loopcut', 'persistent': lambda f: f, 'state': types.SimpleNamespace(restoring=False, session=lambda: session),
            'bpy': types.SimpleNamespace(data=types.SimpleNamespace(filepath=original),
                app=types.SimpleNamespace(timers=types.SimpleNamespace(is_registered=lambda f: True))),
            'conversations': conversations, 'switch_to': switch, 'resume_for_open_file': Mock(),
            '_dock_after_load': lambda: None}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
        namespace['_on_load_post'](str(self.root/'autosave.blend'))
        conversations.recovery_candidate.assert_not_called()
        conversations.attach_untitled.assert_called_once_with(session, original)
        switch.assert_called_once_with(session)


class VisibleOutputTest(StorageFixture):
    def test_unsaved_output_ignores_temp_render_path_and_workspace(self):
        session = state.new_session()
        session['mode'] = 'workspace'
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath=''),
              context=types.SimpleNamespace(scene=types.SimpleNamespace(render=types.SimpleNamespace(filepath='/tmp/'))))
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(state, 'session', return_value=session), \
                patch.object(Path, 'home', return_value=self.root / 'home'):
            for filename in ('scene.blend', 'model.fbx', 'render.png'):
                target = files.deliverable_path(filename)
                self.assertTrue(target.is_relative_to(self.root / 'home' / 'Documents' / 'Loopcut'))
                self.assertFalse(target.is_relative_to(self.root / 'workspaces'))
            self.assertIn('full paths', files.relative_note('model.fbx'))

    def test_saved_output_is_beside_blend_even_with_workspace(self):
        session = state.new_session()
        session['mode'] = 'workspace'
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath=str(self.root/'project'/'scene.blend')))
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(state, 'session', return_value=session), \
                patch.dict(os.environ, {'LOOPCUT_DATA_DIR': str(self.root/'data')}):
            self.assertEqual(files.deliverable_path('model.fbx'), self.root/'project'/'model.fbx')
            self.assertEqual(files.resolve('build.py'), self.root/'data'/'workspaces'/session['id']/'build.py')


class DeliveryOperatorsTest(StorageFixture):
    def test_exports_report_the_full_visible_path_only_after_success(self):
        session = state.new_session()
        session['mode'] = 'workspace'
        module = types.ModuleType('bpy.ops')
        written = []
        def create(submodule, name):
            def write(**kwargs):
                written.append(kwargs['filepath'])
                if name == 'cancel_export':
                    return {'CANCELLED'}
                Path(kwargs['filepath']).parent.mkdir(parents=True, exist_ok=True)
                Path(kwargs['filepath']).write_bytes(b'synthetic export')
                return {'FINISHED'}
            return write
        module._op_create_function = create
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath=''))
        with patch.dict(sys.modules, {'bpy': fake, 'bpy.ops': module}), \
                patch.object(state, 'session', return_value=session), \
                patch.object(Path, 'home', return_value=self.root/'home'), \
                patch.dict(os.environ, {'LOOPCUT_DATA_DIR': str(self.root/'data')}):
            with files.watching_deliverables():
                raw = self.root/'data'/'workspaces'/session['id']/'model.fbx'
                module._op_create_function('export_scene', 'fbx')(filepath=str(raw))
                module._op_create_function('export_scene', 'cancel_export')(filepath='cancel.fbx')
        self.assertIs(module._op_create_function, create)
        self.assertEqual(written[0], str(raw))
        delivered = str(self.root/'home'/'Documents'/'Loopcut'/('Untitled-'+session['id'][:8])/'model.fbx')
        cards = [i for i in session['items'] if i.get('saved_path')]
        self.assertEqual([i['saved_path'] for i in cards], [delivered])
        display = layout.build(session, 480, 900, 1.0, lambda f, s, t: len(t)*s*.5)
        actions = [h['action'] for h in display['hits']]
        self.assertIn(('reference_open', delivered), actions)
        self.assertIn(('reference_folder', delivered), actions)

    def test_background_delivery_preserves_sidecars_and_does_not_replace_existing_files(self):
        from loopcut import workspace
        session = state.new_session()
        ws, destination = self.root/'workspace', self.root/'project'
        ws.mkdir()
        destination.mkdir()
        (destination/'model.gltf').write_text('existing')
        (ws/'exports').mkdir()
        (ws/'exports'/'model.gltf').write_text('new')
        (ws/'exports'/'model.bin').write_bytes(b'buffer')
        (ws/workspace.SOURCE).write_bytes(b'baseline')
        rows = workspace._publish_outputs(session, ws, {}, workspace.files_state(ws), destination, lambda f: f())
        self.assertEqual((destination/'model.gltf').read_text(), 'existing')
        self.assertEqual((destination/'Export-2'/'model.bin').read_bytes(), b'buffer')
        self.assertEqual((destination/'Export-2'/'model.gltf').read_text(), 'new')
        self.assertFalse((destination/'Export-2'/workspace.SOURCE).exists())
        self.assertEqual(len(rows), 2)

    def test_cleanup_keeps_a_workspace_owned_by_a_live_blender(self):
        ws = self.root/'workspaces'/('f'*32)
        (ws/'.loopcut').mkdir(parents=True)
        (ws/'.loopcut'/'owner.json').write_text(json.dumps({'pid': os.getpid()}))
        (ws/workspace.SOURCE).write_bytes(b'baseline')
        cp.cleanup_storage(budget_bytes=0)
        self.assertTrue((ws/workspace.SOURCE).exists())


    def test_a_restore_target_is_protected_from_per_conversation_pruning(self):
        store = cp.Store(self.root, 'a'*32)
        store.folder.mkdir(parents=True)
        for n in range(8):
            row = {'id': f'{n:032x}', 'size': 1, 'kind': 'turn', 'expired': False, 'created': n}
            store.entries.append(row)
            store.snapshot_path(row['id']).write_bytes(b'x')
        target = store.snapshot_path(f'{1:032x}')
        store.add({'id': f'{8:032x}', 'size': 1, 'kind': 'pre_restore', 'expired': False, 'created': 8},
                  budget_bytes=0, protect=(target,))
        self.assertTrue(target.exists())
        self.assertFalse(store.get(f'{1:032x}')['expired'])


if __name__ == '__main__':
    unittest.main()
