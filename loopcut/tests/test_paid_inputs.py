"""Synthetic image suitability checks before quotes, uploads or paid jobs."""

import tempfile
import sys
import types
from pathlib import Path
from unittest.mock import patch

from test_cloud import RuntimeFixture, manifest, tool
sys.modules.setdefault('bpy', types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={})))
from loopcut import cloud, cloud_account, cloud_http, cloud_jobs, files, state, tools


CHECK = {'single_subject': True, 'whole_subject': True, 'front_or_three_quarter': True, 'single_view': True}


class ImageInputTest(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.runtime()
        self.descriptor = tool('image_to_3d', input_schema={'type': 'object', 'properties': {}},
                               file_inputs=[{'field': 'image', 'required': True, 'accept': ['image/png'], 'max_mb': 8}],
                               apply='none', latency='seconds', backend='managed')
        cloud._runtime.manifest = manifest(self.descriptor)
        self.image = Path(self.directory.name) / 'synthetic-input.png'
        self.image.write_bytes(b'synthetic image bytes')
        for context in (patch.object(state, '_session', state.new_session()), patch.object(cloud, 'sync_settings'),
                        patch.object(cloud_jobs, 'resolve_files', return_value={'image': self.image}),
                        patch.object(cloud_jobs, 'made_picture', return_value=None),
                        patch.object(files, 'project_roots', return_value=(Path(tempfile.gettempdir()),))):
            context.start()
            self.addCleanup(context.stop)

    def call(self, check=CHECK):
        return cloud.execute('loopcut_cloud__image_to_3d', {'image': str(self.image), 'input_check': check})

    def test_own_image_cannot_submit_before_the_model_views_it(self):
        result = self.call()
        self.assertFalse(result.ok)
        self.assertTrue(result.input_mistake)
        self.assertIsNone(result.then)
        self.assertIn('read_file', result.text)
        self.assertEqual(self.transport.calls, [])

    def test_failed_checklists_prepare_for_explicit_consent_without_submitting(self):
        cloud_jobs.note_picture_seen(self.image)
        for failed in ('whole_subject', 'front_or_three_quarter', 'single_view', 'single_subject'):
            with self.subTest(failed=failed):
                result = self.call({**CHECK, failed: False})
                self.assertTrue(result.ok, result.text)
                self.assertIsNotNone(result.then)
        self.assertEqual(self.transport.calls, [])

    def test_good_viewed_input_prepares_and_checklist_is_not_sent(self):
        cloud_jobs.note_picture_seen(self.image)
        result = self.call()
        self.assertTrue(result.ok, result.text)
        self.assertIsNotNone(result.then)
        with patch.object(cloud_account, 'authorize', side_effect=cloud_http.CloudError('stopped', 'canceled')) as authorize:
            result.then(lambda: False, lambda fn: fn())
        self.assertNotIn('input_check', authorize.call_args.args[2])

    def test_changed_picture_needs_another_look_and_missing_checklist_is_refused(self):
        cloud_jobs.note_picture_seen(self.image)
        self.assertFalse(self.call({}).ok)
        self.image.write_bytes(b'changed synthetic bytes')
        self.assertFalse(self.call().ok)

    def test_successful_file_preview_records_the_original_for_the_gate(self):
        self.assertFalse(self.call().ok)
        with patch('loopcut.attachments._to_png', return_value=(16, 16)):
            self.assertTrue(files._read_image(self.image).ok)
        self.assertTrue(self.call().ok)

    def test_attached_image_needs_a_whole_view_rather_than_a_detail_crop(self):
        state.session()['references'].append({'name': 'Synthetic attachment', 'ref': 'stored'})
        pixels = types.SimpleNamespace(shape=(16, 16, 4))
        with patch('loopcut.conversations.image_path', return_value=self.image), \
                patch.object(tools, '_pixels', return_value=pixels), \
                patch.object(tools, 'crop', return_value=(pixels, 'image')), \
                patch.object(tools, '_save_pixels', return_value=(16, 16)):
            tools.look_at_reference('Synthetic attachment', region=[0, 0, .5, .5])
            self.assertFalse(self.call().ok)
            tools.look_at_reference('Synthetic attachment')
            self.assertTrue(self.call().ok)

    def test_reference_look_uses_the_actual_input_when_its_source_changed(self):
        stored = Path(self.directory.name) / 'stored-preview.png'
        stored.write_bytes(b'older synthetic image bytes')
        state.session()['references'].append({'name': 'Synthetic attachment', 'ref': 'stored',
                                               'source': str(self.image)})
        with patch('loopcut.conversations.image_path', return_value=stored):
            _, viewed = tools._reference('Synthetic attachment')
        self.assertEqual(viewed, self.image)
