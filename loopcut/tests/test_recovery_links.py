"""Native recovery follows persisted scene identity and excludes live foreign owners."""
import json
import os
import sys
import types
from unittest.mock import Mock, patch

from test_storage_recovery import StorageFixture
from test_storage_round3 import function
from loopcut import checkpoints as cp, conversations as cv, files, jobs, state


class RecoveryLinksTest(StorageFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(cv, '_SCENE', 'a' * 32))
        self.scene = {}
        self.current = state.new_session()
        self.bpy = types.SimpleNamespace(context=types.SimpleNamespace(scene=self.scene),
            data=types.SimpleNamespace(filepath=''), app=types.SimpleNamespace(tempdir=str(self.root / 'temp' / 'session'), driver_namespace={},
                timers=types.SimpleNamespace(is_registered=lambda f: True)))
        self.enterContext(patch.dict(sys.modules, {'bpy': self.bpy}))
        self.enterContext(patch.object(state, 'session', side_effect=lambda: self.current))
        self.enterContext(patch.object(jobs, '_alive', return_value=False))

    def test_startup_and_new_files_get_a_fresh_link_even_if_startup_blend_carries_one(self):
        self.scene['loopcut_untitled_scene'] = 'b' * 32
        self.bpy.data.filepath = ''
        first = cv.new_scene()
        self.bpy.app.driver_namespace.clear()  # File > New: Blender clears it on every load.
        second = cv.new_scene()
        self.assertNotEqual(first, 'b' * 32)
        self.assertNotEqual(first, second)

    def test_reloading_the_addon_keeps_the_untitled_scenes_link(self):
        self.bpy.data.filepath = ''
        first = cv.new_scene()
        with patch.object(cv, '_SCENE', 'd' * 32):  # A purged and re-imported module starts with a new link.
            self.assertEqual(cv.new_scene(), first)
        self.assertEqual(self.scene['loopcut_untitled_scene'], first)

    def test_a_file_with_a_path_keeps_its_stored_link(self):
        self.scene['loopcut_untitled_scene'] = 'c' * 32
        self.bpy.data.filepath = str(self.root / 'quit.blend')
        self.assertEqual(cv.new_scene(), 'c' * 32)

    def test_a_reused_process_id_from_an_ended_launch_counts_as_not_running(self):
        with patch.object(jobs, '_alive', return_value=True):
            self.assertFalse(cv._foreign_owner({'launch': 'previous-launch', 'pid': os.getpid()}))
            self.assertTrue(cv._foreign_owner({'launch': 'previous-launch', 'pid': os.getpid() + 1}))

    def saved(self, scene='a' * 32, foreign=True):
        chat = state.new_session()
        chat['untitled_scene'] = scene
        chat['items'].append(state.item_user('Add a cube'))
        # A foreign launch is another process: give it a different id from this one.
        with patch.object(cv, '_LAUNCH', 'previous-launch' if foreign else cv._LAUNCH), \
                patch.object(cv.os, 'getpid', return_value=os.getpid() + (1 if foreign else 0)):
            cv.save(chat)
        return chat

    def recover(self, scene, name='quit.blend', current=None, folder=None):
        self.scene.clear()
        if scene:
            self.scene['loopcut_untitled_scene'] = scene
        self.current = current or state.new_session()
        path = (folder or self.root) / name
        self.bpy.data.filepath = str(path)
        switch, resume = Mock(), Mock()
        namespace = dict(__package__='loopcut', persistent=lambda f: f, conversations=cv,
            switch_to=switch, resume_for_open_file=resume, _dock_after_load=lambda: None,
            state=types.SimpleNamespace(restoring=False, session=lambda: self.current), bpy=self.bpy)
        with patch.object(files, '_temp_roots', return_value=(self.root,)):
            function('lifecycle.py', '_on_load_post', namespace)(str(path))
        return switch, resume

    def test_scene_link_is_written_before_native_snapshot_and_matches_saved_chat(self):
        function('lifecycle.py', '_on_save_pre', dict(__package__='loopcut', bpy=self.bpy, persistent=lambda f: f,
            conversations=cv))()
        chat = state.new_session()
        chat['items'].append(state.item_user('Add a cube'))
        cv.save(chat)
        body = json.loads((cv._folder(chat['id']) / 'conversation.json').read_text())
        meta = cv.list_for(None)[0]
        self.assertEqual(self.scene.get('loopcut_untitled_scene'), body['untitled_scene'])
        self.assertEqual(meta['untitled_scene'], body['untitled_scene'])
        self.assertEqual(meta.get('pid'), os.getpid())

    def test_startup_links_untitled_scene_before_first_autosave(self):
        from loopcut import bundled_addons
        with patch.object(bundled_addons, 'startup_cleanup'), patch.object(cp, 'schedule_cleanup'):
            function('lifecycle.py', '_initial_resume', dict(__package__='loopcut',
                resume_for_open_file=Mock(), _dock_if_missing=Mock()))()
        self.assertEqual(self.scene.get('loopcut_untitled_scene'), cv._SCENE)

    def test_first_save_after_native_recovery_attaches_linked_chats(self):
        from loopcut import cloud_restore
        sibling = self.saved()
        self.current = self.saved()
        self.bpy.data.filepath = str(self.root / 'project.blend')
        with patch.object(state, 'restoring', False), patch.object(cloud_restore, 'file_saved'), \
                patch.object(files, '_temp_roots', return_value=(self.root,)):
            function('lifecycle.py', '_on_save_post', dict(__package__='loopcut', persistent=lambda f: f,
                _saving_from=str(self.root / 'quit.blend'), bpy=self.bpy, state=state, conversations=cv))()
        self.assertIn(cv.project_key(self.bpy.data.filepath), cv.load(sibling['id'])['projects'])

    def test_revert_and_second_recovery_keep_the_matching_chat_on_screen(self):
        for name in ('quit.blend', '123_autosave.blend'):
            with self.subTest(name=name):
                current = self.saved(foreign=False)
                self.saved('b' * 32)
                switch, resume = self.recover('a' * 32, name, current)
                self.assertIs(switch.call_args.args[0], current)
                resume.assert_not_called()
                cv.save(current)
                switch, _ = self.recover('a' * 32, name, current)
                self.assertIs(switch.call_args.args[0], current)

    def test_own_autosave_keeps_current_chat_instead_of_an_older_one(self):
        self.saved('b' * 32)
        current = self.saved(foreign=False)
        switch, _ = self.recover('a' * 32, '123_autosave.blend', current)
        self.assertIs(switch.call_args.args[0], current)

    def test_recovery_selects_file_link_instead_of_newer_unrelated_chat(self):
        expected = self.saved()
        self.saved('b' * 32)
        switch, _ = self.recover('a' * 32)
        self.assertEqual(switch.call_args.args[0]['id'], expected['id'])

    def test_recovery_with_no_matching_link_does_not_guess(self):
        self.saved('b' * 32)
        for scene in ('a' * 32, ''):
            with self.subTest(scene=scene):
                switch, resume = self.recover(scene)
                switch.assert_not_called()
                resume.assert_called_once()

    def test_other_running_blender_is_excluded_even_with_matching_scene_link(self):
        self.saved()
        with patch.object(jobs, '_alive', return_value=True):
            switch, resume = self.recover('a' * 32)
        switch.assert_not_called()
        resume.assert_called_once()

    def test_live_launch_is_not_offered_or_reattached_on_first_save(self):
        foreign = self.saved()
        with patch.object(cv, '_LAUNCH', 'previous-launch'), patch.object(cv.os, 'getpid', return_value=os.getpid() + 1):
            cv.save(foreign, running=True)
        current = self.saved(foreign=False)
        with patch.object(jobs, '_alive', return_value=True):
            self.assertIsNone(cv.recovery_candidate())
            cv.attach_untitled(current, str(self.root / 'project.blend'))
        self.assertEqual(cv.load(foreign['id'])['projects'], [])

    def test_custom_temp_parent_quit_file_uses_its_link(self):
        expected = self.saved()
        custom = self.root / 'custom'
        self.bpy.app.tempdir = str(custom / 'session')
        # The custom base is neither the OS temp root nor the per-process temp folder.
        with patch.object(files, '_temp_roots', side_effect=lambda: (self.root / 'system', custom / 'session')):
            self.scene['loopcut_untitled_scene'] = 'a' * 32
            self.current = state.new_session()
            self.bpy.data.filepath = str(custom / 'quit.blend')
            switch = Mock()
            function('lifecycle.py', '_on_load_post', dict(__package__='loopcut', persistent=lambda f: f,
                conversations=cv, switch_to=switch, resume_for_open_file=Mock(), _dock_after_load=lambda: None,
                state=types.SimpleNamespace(restoring=False, session=lambda: self.current), bpy=self.bpy))(
                    self.bpy.data.filepath)
        switch.assert_called_once()
        self.assertEqual(switch.call_args.args[0]['id'], expected['id'])

    def test_recovered_temp_filepath_uses_normal_untitled_deliverable_folder(self):
        for name in ('quit.blend', '123_autosave.blend'):
            with self.subTest(name=name):
                chat = self.saved()
                switch, _ = self.recover('a' * 32, name)
                self.current = switch.call_args.args[0]
                with patch.object(files, '_temp_roots', return_value=(self.root,)), \
                        patch.object(cp, 'data_root', return_value=self.root / 'storage'), \
                        patch.object(files, 'documents_folder', return_value=self.root / 'Documents'):
                    folder, note = files.blend_base()
                self.assertEqual(folder, self.root / 'Documents' / 'Loopcut' / ('Untitled-' + chat['id'][:8]))
                self.assertIn('not saved yet', note)
