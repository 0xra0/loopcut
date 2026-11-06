"""Synthetic consent, preview and refresh regressions."""

import json
import threading
import types
import unittest
from unittest.mock import patch

from test_cloud import CreditToolTest, Reply, manifest, tool
from test_paid_inputs import CHECK, ImageInputTest
from test_spending import FirstUseTest
from loopcut import cloud, cloud_account, cloud_http, cloud_jobs, spending, state, tools
from loopcut import repeats
from test_repeats import failed_run, run_key


class RenderGuidanceTest(unittest.TestCase):
    def test_refusals_keep_the_option_to_explain_the_blocker(self):
        ledger = repeats.Ledger()
        error = failed_run('RuntimeError: Render refused by the cost guard: render.render in run_python holds Blender until it ends.')
        ledger.record(1, run_key('first'), error, ok=False)
        self.assertIn('tell the user what is blocking you', ledger.record(2, run_key('second'), error, ok=False))

    def test_run_python_is_identified_as_the_live_session(self):
        description = next(s['function']['description'] for s in tools.SCHEMAS if s['function']['name'] == 'run_python')
        self.assertIn('live session', description)


class ReferenceFailureTest(ImageInputTest):
    def test_broken_datablock_falls_back_to_attachment(self):
        state.session()['references'].append({'name': 'Synthetic attachment', 'ref': 'stored',
                                               'source': str(self.image)})
        image = types.SimpleNamespace(name='Synthetic attachment')
        fake = types.SimpleNamespace(data=types.SimpleNamespace(images={'Synthetic attachment': image}))
        for error in (cloud_http.CloudError('No pixels', 'invalid_input'), RuntimeError('No pixels'), OSError('Disk')):
            with self.subTest(error=type(error).__name__), patch.object(tools, 'bpy', fake), \
                    patch.object(cloud_jobs, 'resolve_files', side_effect=error):
                self.assertEqual(tools._reference(image.name)[1], self.image)

    def test_broken_unattached_datablock_is_a_tool_error(self):
        fake = types.SimpleNamespace(data=types.SimpleNamespace(images={'Synthetic': types.SimpleNamespace(name='Synthetic')}))
        with patch.object(tools, 'bpy', fake), \
                patch.object(cloud_jobs, 'resolve_files', side_effect=RuntimeError('No pixels')):
            with self.assertRaises(tools.ToolError):
                tools._reference('Synthetic')

    def test_compare_exports_its_datablock_only_once(self):
        fake = types.SimpleNamespace(data=types.SimpleNamespace(images={'Synthetic': types.SimpleNamespace(name='Synthetic')}))
        with patch.object(tools, 'bpy', fake), patch.object(cloud_jobs, 'resolve_files', return_value={'image': self.image}) as export, \
                patch('loopcut.shaders.before_look', side_effect=lambda callback, *a, **k: callback()), \
                patch.object(tools, 'compare_with_reference', side_effect=lambda *a: tools._reference('Synthetic')):
            tools.compare_with_reference_tool('Synthetic')
        self.assertEqual(export.call_count, 1)

    def test_failed_checks_reach_priced_consent_and_never_reach_service_inputs(self):
        cloud_jobs.note_picture_seen(self.image)
        for field in CHECK:
            for approved in (False, True):
                with self.subTest(field=field, approved=approved):
                    pending = self.call({**CHECK, field: False})
                    self.assertTrue(pending.ok, pending.text)
                    self.assertIsNotNone(pending.then)
                    seen = []
                    def consent(details):
                        seen.append(details)
                        self.assertEqual(spending.decide(details, spending.new_ledger(), spending.Settings()), spending.COST)
                        self.assertIn(cloud.IMAGE_CHECK[field], '\n'.join(spending.ask_lines(
                            spending.COST, details, spending.new_ledger(), spending.Settings())))
                        return approved
                    quote = {'quote_id': 'qt_test', 'credits': 80, 'expires_at': '2099-01-01T00:00:00Z'}
                    with cloud_account.approval_context(consent), patch.object(cloud_account, '_quote', return_value=quote), \
                            patch.object(cloud_account, 'recheck', side_effect=cloud_http.CloudError('Stopped', 'canceled')) as recheck:
                        pending.then(lambda: False, lambda fn: fn())
                    self.assertEqual(len(seen), 1)
                    self.assertEqual(seen[0]['credits'], 80)
                    self.assertEqual(recheck.called, approved)
        self.assertEqual(self.transport.calls, [])


class RefreshAfterChargeTest(CreditToolTest):
    def test_credits_waits_for_inflight_balance_refresh(self):
        runtime = self.runtime(Reply(manifest(tool())))
        runtime.client.client_headers = {}
        runtime.manifest = manifest(tool())
        entered, release, complete = threading.Event(), threading.Event(), threading.Event()
        def transport(request, **options):
            entered.set()
            if not release.wait(2):
                raise AssertionError('refresh was not released')
            updated = manifest(tool())
            updated['account']['credits']['balance'] = 20
            return Reply(updated)
        runtime.client.transport = transport
        worker = threading.Thread(target=runtime.refresh)
        worker.start()
        self.assertTrue(entered.wait(2))
        result = []
        reader = threading.Thread(target=lambda: (result.append(json.loads(cloud._credits(runtime).text)), complete.set()))
        try:
            reader.start()
            self.assertFalse(complete.wait(.05), 'credits must wait for the running refresh')
        finally:
            release.set()
            worker.join(2)
            reader.join(2)
        self.assertFalse(worker.is_alive() or reader.is_alive())
        self.assertEqual(result[0]['balance'], 20)
        self.assertTrue(result[0]['fresh'])


class BackendConsentTest(FirstUseTest):
    def test_input_concerns_ask_again_even_for_an_approved_vendor(self):
        details = {'vendor': 'Synthetic vendor', 'credits': None, 'input_warning': 'A single view is required.'}
        self.assertEqual(spending.decide(details, spending.new_ledger(), spending.Settings(),
                                        vendors=['Synthetic vendor']), spending.COST)
        self.assertIn(details['input_warning'], spending.ask_lines(spending.COST, details,
                      spending.new_ledger(), spending.Settings()))

    def test_own_key_consent_does_not_hide_first_credit_card(self):
        details = []
        descriptor = {'name': 'image_to_3d', 'version': 1, 'backend': 'byok', 'byok': {'vendor': 'Synthetic vendor'}}
        with patch.object(cloud_account, 'approval_details', return_value={'vendor': 'Synthetic vendor', 'required': True}), \
                cloud_account.approval_context(lambda value: details.append(value) or True):
            cloud_account.authorize(cloud_jobs, descriptor, {}, {}, project={'id': 'local'}, cancel=threading.Event(),
                                    runtime=self.runtime, client=self.client)
        self.assertTrue(details[0]['first_use'])
        self.assertEqual(len(self.call()), 1)

    def test_parallel_authorizations_only_mark_the_first_one_as_first_use(self):
        entered, release = threading.Event(), threading.Event()
        first_flags, errors = [], []
        descriptor = {'name': 'image_to_3d', 'version': 1, 'backend': 'managed', 'file_inputs': [],
                      'cost': {'credits_estimate': 80}, 'approval': {'confirm_over_credits': 100}}
        def consent(details):
            first_flags.append(bool(details.get('first_use')))
            if len(first_flags) == 1:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('consent was not released')
            return True
        def authorize():
            try:
                with cloud_account.approval_context(consent):
                    cloud_account.authorize(cloud_jobs, descriptor, {}, {}, project={'id': 'local'},
                                            cancel=threading.Event(), runtime=self.runtime, client=self.client)
            except Exception as ex:
                errors.append(ex)
        quote = {'quote_id': 'qt_test', 'credits': 80, 'expires_at': '2099-01-01T00:00:00Z'}
        with patch.object(cloud_account, '_quote', return_value=quote):
            a, b = threading.Thread(target=authorize), threading.Thread(target=authorize)
            try:
                a.start()
                self.assertTrue(entered.wait(2))
                b.start()
                b.join(.05)
            finally:
                release.set()
                a.join(2)
                b.join(2)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(first_flags, [True, False])


def load_tests(loader, suite, pattern):
    result = unittest.TestSuite()
    for cls in (ReferenceFailureTest, RefreshAfterChargeTest, BackendConsentTest, RenderGuidanceTest):
        result.addTests(cls(name) for name in cls.__dict__ if name.startswith('test_'))
    return result
