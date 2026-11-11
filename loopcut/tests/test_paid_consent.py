"""Consent concurrency, balance freshness and unprompted model checks."""

import ast
import json
import re
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_cloud import CreditToolTest, Reply, error, manifest, tool
from test_spending import FirstUseTest
from loopcut import cloud, cloud_account, cloud_jobs, spending, tools


class ConsentConcurrencyTest(FirstUseTest):
    def test_waiting_card_does_not_block_a_previously_approved_tool(self):
        quote = patch.object(cloud_account, '_quote', side_effect=lambda module, descriptor, *a, **k: {
            'quote_id': 'qt_' + descriptor['name'], 'credits': descriptor['cost']['credits_estimate'],
            'expires_at': '2099-01-01T00:00:00Z'})
        quote.start()
        self.addCleanup(quote.stop)
        for first_use in (False, True):
            with self.subTest(first_use=first_use):
                spending.note_use(self.runtime, 'image_generate')
                if not first_use:
                    spending.note_use(self.runtime, 'image_to_3d')
                else:
                    (self.runtime.cache_dir / 'paid-tools.json').unlink()
                    spending.note_use(self.runtime, 'image_generate')
                entered, release, completed = threading.Event(), threading.Event(), threading.Event()
                errors, decisions = [], []

                def authorize(name, credits, waiting=False):
                    descriptor = {'name': name, 'version': 1, 'backend': 'managed', 'file_inputs': [],
                                  'cost': {'credits_estimate': credits, 'quote_required': False},
                                  'approval': {'confirm_over_credits': 100}}

                    def consent(details):
                        if waiting:
                            entered.set()
                            if not release.wait(3):
                                raise AssertionError('card was not released')
                        else:
                            decisions.append(spending.decide(details, spending.new_ledger(), spending.Settings()))
                        return True

                    try:
                        with cloud_account.approval_context(consent):
                            cloud_account.authorize(cloud_jobs, descriptor, {}, {}, project={'id': 'local'},
                                                    cancel=threading.Event(), runtime=self.runtime, client=self.client)
                        if not waiting:
                            completed.set()
                    except Exception as ex:
                        errors.append(ex)

                a = threading.Thread(target=authorize, args=('image_to_3d', 80 if first_use else 120, True))
                b = threading.Thread(target=authorize, args=('image_generate', 6))
                try:
                    a.start()
                    self.assertTrue(entered.wait(2))
                    b.start()
                    independent = completed.wait(1)
                finally:
                    release.set()
                    a.join(2)
                    if b.ident is not None:
                        b.join(2)
                self.assertFalse(a.is_alive() or b.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(independent, 'an unrelated paid call waited for the open card')
                self.assertEqual(decisions, [spending.RUN])


class BalanceFreshnessTest(CreditToolTest):
    def charge(self, runtime):
        module = types.SimpleNamespace(apply_result=lambda *a: 'Applied')
        with patch('loopcut.scene_diff.snapshot', return_value={}), patch.object(runtime, 'refresh_async'):
            return cloud._apply(module, {'backend': 'managed', 'apply': 'none'},
                                {'id': 'job_test', 'credits': {'charged': 80}})

    def test_offline_refresh_overlapping_a_panel_tick_is_not_fresh(self):
        runtime = self.runtime()
        runtime.manifest = manifest(tool())

        def offline(*args, **kwargs):
            with patch.object(cloud, '_snapshot', return_value=runtime), patch.object(runtime, 'refresh_async'):
                cloud.panel_seen()
            raise OSError('offline')

        runtime.client.transport = offline
        self.assertFalse(json.loads(cloud._credits(runtime).text)['fresh'])

    def test_missing_manifest_is_not_a_successful_refresh(self):
        runtime = self.runtime(error('not_found', 404))
        runtime.manifest = manifest(tool())
        self.assertFalse(json.loads(cloud._credits(runtime).text)['fresh'])

    def test_a_refresh_within_one_clock_tick_of_the_last_is_still_fresh(self):
        """Windows' monotonic clock ticks every ~15 ms: two refreshes inside one tick read the same time, and a
        successful second one must not be reported as cached data."""
        runtime = self.runtime()
        runtime.client.client_headers = {}
        runtime.manifest = manifest(tool())
        runtime.client.transport = lambda *a, **k: Reply(manifest(tool()))
        with patch.object(cloud.time, 'monotonic', return_value=1000.0):
            self.assertTrue(json.loads(cloud._credits(runtime).text)['fresh'])
            self.assertTrue(json.loads(cloud._credits(runtime).text)['fresh'])

    def test_refresh_started_before_a_charge_cannot_make_old_balance_fresh(self):
        runtime = self.runtime()
        runtime.client.client_headers = {}
        runtime.manifest = manifest(tool())
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        values, errors = [], []

        def transport(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('refresh was not released')
            return Reply(manifest(tool()))

        runtime.client.transport = transport
        worker = threading.Thread(target=runtime.refresh)

        def read():
            try:
                values.append(json.loads(cloud._credits(runtime).text))
            except Exception as ex:
                errors.append(ex)
            finally:
                completed.set()

        reader = threading.Thread(target=read)
        try:
            worker.start()
            self.assertTrue(entered.wait(2))
            self.charge(runtime)
            reader.start()
            self.assertFalse(completed.wait(.05))
        finally:
            release.set()
            worker.join(2)
            if reader.ident is not None:
                reader.join(2)
        self.assertFalse(worker.is_alive() or reader.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(values[0]['balance'], 100)
        self.assertFalse(values[0]['fresh'], 'pre-charge response was labelled fresh')
        updated = manifest(tool())
        updated['account']['credits']['balance'] = 20
        runtime.client.transport = lambda *a, **k: Reply(updated)
        value = json.loads(cloud._credits(runtime).text)
        self.assertEqual(value['balance'], 20)
        self.assertTrue(value['fresh'])

    def test_charge_between_refresh_and_balance_read_invalidates_freshness(self):
        runtime = self.runtime(Reply(manifest(tool())))
        runtime.manifest = manifest(tool())
        refresh = cloud._refresh_now

        def refresh_then_charge(runtime):
            fresh = refresh(runtime)
            self.charge(runtime)
            return fresh

        with patch.object(cloud, '_refresh_now', side_effect=refresh_then_charge):
            self.assertFalse(json.loads(cloud._credits(runtime).text)['fresh'])

    def test_charge_settlement_invalidates_freshness_before_output_application(self):
        runtime = self.runtime(Reply(manifest(tool())))
        runtime.manifest = manifest(tool())
        refresh = cloud._refresh_now

        def refresh_then_settle(runtime):
            fresh = refresh(runtime)
            cloud._settle_final(lambda *a: None, {'status': 'succeeded', 'credits': {'charged': 80}}, None)
            return fresh

        with patch.object(cloud, '_refresh_now', side_effect=refresh_then_settle):
            self.assertFalse(json.loads(cloud._credits(runtime).text)['fresh'])

    def test_detached_charge_settlement_invalidates_freshness(self):
        runtime = self.runtime(Reply(manifest(tool())))
        runtime.manifest = manifest(tool())
        refresh = cloud._refresh_now

        def refresh_then_settle(runtime):
            fresh = refresh(runtime)
            with patch('loopcut.cloud_restore.settle'):
                cloud._settle_saved(lambda *a: None, {}, {'status': 'succeeded', 'credits': {'charged': 80}})
            return fresh

        with patch.object(cloud, '_refresh_now', side_effect=refresh_then_settle):
            self.assertFalse(json.loads(cloud._credits(runtime).text)['fresh'])


class GuidanceTest(unittest.TestCase):
    def test_paid_test_modules_use_topic_names(self):
        for source in Path(__file__).resolve().parent.glob('test_paid*.py'):
            with self.subTest(module=source.name):
                self.assertNotRegex(source.stem, r'_round\d+$')

    def test_model_harness_requires_standalone_balance_and_price(self):
        source = Path(__file__).resolve().parents[1] / 'harness' / 'fq_paid_model_check.py'
        assertion = next(node for node in ast.walk(ast.parse(source.read_text()))
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id == 'check' and len(node.args) == 2
                         and isinstance(node.args[1], ast.Constant)
                         and node.args[1].value == 'unprompted model stated the observed balance and price before conversion')
        condition = compile(ast.Expression(body=assertion.args[0]), str(source), 'eval')
        for text, expected in (
                ('Balance: 517 credits. Conversion: 80 credits.', True),
                ('Balance (517).\nConversion costs $80.', True),
                ('Balance: 517. Render: 1080 pixels.', False),
                ('Balance: 517. Conversion: 800 credits.', False),
                ('Balance: 517. Render: 1280 pixels.', False),
                ('Balance: 1517. Conversion: 80 credits.', False),
                ('Balance: 5170. Conversion: 80 credits.', False),
                ('Balance: A517. Conversion: 80 credits.', False),
                ('Balance: 517A. Conversion: 80 credits.', False),
                ('Balance: 517. Code: A80.', False),
                ('Balance: 517. Code: 80A.', False),
                ('Balance: 517.', False),
                ('Conversion: 80 credits.', False)):
            with self.subTest(text=text):
                self.assertEqual(bool(eval(condition, {'re': re, 'before': text.splitlines()})), expected)

    def test_run_python_says_the_namespace_names_are_preloaded(self):
        description = next(row['function']['description'] for row in tools.SCHEMAS
                           if row['function']['name'] == 'run_python')
        self.assertIn('preloaded', description)

    def test_model_harness_offers_a_job_only_prompt(self):
        source = Path(__file__).resolve().parents[1] / 'harness' / 'fq_paid_model_check.py'
        function = next((node for node in ast.parse(source.read_text()).body
                         if isinstance(node, ast.FunctionDef) and node.name == 'model_prompt'), None)
        self.assertIsNotNone(function, 'the live harness needs separately selectable prompts')
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        prompted, unprompted = (namespace['model_prompt'](variant) for variant in ('prompted', 'unprompted'))
        self.assertIn('Check the credit balance', prompted)
        self.assertIn('3D', unprompted)
        self.assertIn('camera render', unprompted)
        for hint in ('balance', 'credit', 'price', 'inspect', 'check', 'suitable', 'front view', 'JSON', 'input_check'):
            self.assertNotIn(hint, unprompted)


def load_tests(loader, suite, pattern):
    result = unittest.TestSuite()
    for cls in (ConsentConcurrencyTest, BalanceFreshnessTest, GuidanceTest):
        result.addTests(cls(name) for name in cls.__dict__ if name.startswith('test_'))
    return result
