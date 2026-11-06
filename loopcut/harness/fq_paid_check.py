"""Deterministic paid-tool gates, real image datablocks and approval-card layout, without service traffic.

    blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/fq_paid_check.py

Uses temporary synthetic images and an in-memory manifest and quote. Exits after 90 seconds on a stall.
Tests inspection/consent wiring, not a model's visual judgment or tool choices.
"""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout
checkout.use()


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print('PASS ' + message, flush=True)


def timeout():
    print('FAIL paid check timed out', flush=True)
    os._exit(2)


def main():
    import bpy
    from loopcut import (agent, blobs, cloud, cloud_account, cloud_http, cloud_jobs, conversations,
                         spending, state, tools)
    from loopcut.ui import layout
    from loopcut.byok import catalog

    with tempfile.TemporaryDirectory(prefix='loopcut-paid-check-') as directory:
        root = Path(directory)
        os.environ['LOOPCUT_DATA_DIR'] = str(root / 'data')
        os.environ['LOOPCUT_CONFIG_DIR'] = str(root / 'config')
        descriptor = catalog.descriptors(no_generation=False, private_mode=False,
                                         keys_set={'OPENAI_API_KEY': True})[0]
        descriptor.update(name='image_to_3d', title='Image to 3D', description='Convert a reference image.',
            backend='managed', apply='none', latency='seconds',
            input_schema={'type': 'object', 'properties': {}},
            cost={'credits_estimate': 80, 'quote_required': True}, approval={'confirm_over_credits': 100},
            file_inputs=[{'field': 'image', 'required': True, 'max_mb': 8, 'accept': ['image/png', 'image/jpeg']}])
        manifest = {'flags': {'services': True, 'premium_skills': False}, 'tools': [descriptor], 'skills': [],
                    'account': {'credits': {'balance': 500}, 'settings': {'private_mode': False}}}
        requests = []

        def transport(request, *, timeout):
            requests.append((request.method, request.full_url))
            check(request.method == 'GET' and request.full_url.endswith('/v1/manifest'), 'only the free manifest is requested')
            reply = io.BytesIO(json.dumps(manifest).encode())
            reply.status, reply.headers = 200, {}
            return reply

        client = cloud_http.Client(base_url='https://service.test', api_key='lc_test', transport=transport)
        runtime = cloud.Runtime(client, root / 'cache', signed_in=True)
        runtime.manifest = manifest
        session = state.new_session()
        image = bpy.data.images.new('Synthetic subject', width=64, height=64, alpha=True)
        image.pixels.foreach_set([0.25, 0.5, 0.75, 1.0] * (64 * 64))
        source = root / 'synthetic-input.png'
        image.filepath_raw, image.file_format = str(source), 'PNG'
        image.save()
        try:
            with patch.object(state, '_session', session), patch.object(cloud, '_runtime', runtime), \
                    patch.object(cloud, 'sync_settings'), patch.object(agent, '_redraw'), \
                    patch.object(conversations, 'save'):
                arguments = {'image': image.name, 'input_check': {key: True for key in cloud.IMAGE_CHECK}}
                refused = cloud.execute(cloud.PREFIX + 'image_to_3d', arguments)
                check(not refused.ok and refused.then is None and not requests, 'unviewed own datablock is refused before quoting')
                check(tools.look_at_reference(image.name).image_path is not None, 'whole datablock preview returns real pixels')
                for field in cloud.IMAGE_CHECK:
                    bad = {**arguments, 'input_check': {**arguments['input_check'], field: False}}
                    pending = cloud.execute(cloud.PREFIX + 'image_to_3d', bad)
                    check(pending.ok and pending.then is not None and not requests,
                          'failed ' + field + ' prepares for priced consent without submitting')
                    asked = []
                    def decline(details):
                        asked.append(details)
                        lines = spending.ask_lines(spending.COST, details, spending.new_ledger(), spending.Settings())
                        check(cloud.IMAGE_CHECK[field] in ' '.join(lines) and details['credits'] == 80,
                              'approval names the failed check and quoted price')
                        return False
                    quote = {'quote_id': 'qt_test', 'credits': 80, 'expires_at': '2099-01-01T00:00:00Z'}
                    with patch.object(cloud_account, '_quote', return_value=quote), cloud_account.approval_context(decline):
                        pending.then(lambda: False, lambda fn: fn())
                    check(len(asked) == 1 and not requests, 'decline prevents job submission')
                check(cloud.execute(cloud.PREFIX + 'image_to_3d', arguments).then is not None,
                      'a viewed suitable image can reach the quote stage')
                def inspect_input(name):
                    session['seen_pictures'] = []
                    args = {**arguments, 'image': name}
                    check(not cloud.execute(cloud.PREFIX + 'image_to_3d', args).ok, name + ': unseen input stops')
                    check(tools.look_at_reference(name).image_path is not None, name + ': whole preview returns pixels')
                    _, viewed = tools._reference(name)
                    sent = cloud_jobs.resolve_files(descriptor, {'image': name}, root / 'resolved')
                    check(blobs.sha256(viewed) == blobs.sha256(sent['image']), name + ': preview and conversion read identical bytes')
                    check(cloud.execute(cloud.PREFIX + 'image_to_3d', args).then is not None, name + ': viewed input prepares')

                inspect_input(image.name)
                image.pack()
                source.unlink()
                inspect_input(image.name)
                unsaved = bpy.data.images.new('Synthetic unsaved', width=32, height=32)
                unsaved.pixels.foreach_set([0.9, 0.1, 0.2, 1.0] * (32 * 32))
                inspect_input(unsaved.name)

                jpeg = root / 'synthetic-attachment.jpg'
                unsaved.filepath_raw, unsaved.file_format = str(jpeg), 'JPEG'
                unsaved.save()
                reference = conversations.store_image(session, jpeg)
                session['references'].append({'name': 'Synthetic attachment', 'ref': reference, 'source': str(jpeg)})
                inspect_input('Synthetic attachment')
                shadow = bpy.data.images.new('Synthetic attachment', width=1, height=1)
                shadow.source, shadow.filepath_raw = 'FILE', str(root / 'absent.png')
                try:
                    shadow.reload()
                except RuntimeError:
                    pass
                check(tools.look_at_reference('Synthetic attachment').image_path is not None,
                      'broken-path texture cannot crash an attachment look')
                check(tools._reference('Synthetic attachment')[1] == jpeg, 'failed texture export falls back to the attachment')
                bpy.data.images.remove(shadow)

                made = {'tool': 'image_generate', 'job': 'job_synthetic'}
                unsaved['loopcut_provenance'] = json.dumps({'tool': made['tool'], 'job_id': made['job']})
                session['references'].append({'name': unsaved.name, 'ref': reference, 'source': str(jpeg), 'made': made})
                session['seen_pictures'] = []
                made_args = {**arguments, 'image': unsaved.name}
                check(not cloud.execute(cloud.PREFIX + 'image_to_3d', made_args).ok, 'made picture requires its own look')
                clicked = agent._Consent(session, agent.Turn(), {}, clicked=True, shown=80)
                with cloud_account.approval_context(clicked):
                    check(cloud.execute(cloud.PREFIX + 'image_to_3d', {'image': unsaved.name}).then is not None,
                          'priced user click bypasses the model inspection gate')
                check(tools.look_at_reference(unsaved.name).image_path is not None and made.get('seen'),
                      'whole made-picture look records inspection')
                check(cloud.execute(cloud.PREFIX + 'image_to_3d', made_args).then is not None,
                      'inspected made picture prepares without a click')
                bpy.data.images.remove(unsaved)

                pending = cloud.execute(cloud.PREFIX + 'credits', {})
                info = json.loads(pending.then(lambda: False, lambda fn: fn()).text)
                check(info['fresh'] and info['balance'] == 500 and info['prices'][0]['credits_estimate'] == 80,
                      'free credits result exposes balance and price on every provider path')

                def authorize(price):
                    turn = agent.Turn()
                    card = state.item_tool(cloud.PREFIX + 'image_to_3d', 'Image to 3D', '')
                    chat = state.new_session()
                    chat['items'].append(card)
                    consent = agent._Consent(chat, turn, card)
                    asked = []

                    def answer(_turn):
                        asked.append(card['ask'])
                        drawn = layout.build(chat, 420, 900, 1, lambda font, size, text: len(text) * size * .5,
                                             'fast', None, {})
                        words = ' '.join(p.get('text', '') for p in drawn['prims'])
                        check(str(price) in words and 'Approve' in words, 'awaiting card layout contains price and approval')
                        return True

                    quote = {'quote_id': 'qt_test', 'credits': price, 'expires_at': '2099-01-01T00:00:00Z'}
                    with patch.object(agent, '_wait_for_approval', side_effect=answer), \
                            patch.object(cloud_account, '_quote', return_value=quote), cloud_account.approval_context(consent):
                        cloud_account.authorize(cloud_jobs, descriptor, {}, {}, project={'id': 'local'},
                                                cancel=threading.Event(), runtime=runtime, client=client)
                    return asked

                check(authorize(80) == [spending.COST], 'first paid use asks below the threshold')
                runtime = cloud.Runtime(client, root / 'cache', signed_in=True)
                check(authorize(80) == [], 'second use after runtime restart follows the threshold')
                check(authorize(101) == [spending.COST], 'over-threshold use still asks')
        finally:
            bpy.data.images.remove(image)
    print('PASS paid-tool check', flush=True)


watchdog = threading.Timer(90, timeout)
watchdog.daemon = True
watchdog.start()
try:
    with patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected network request')):
        main()
except Exception:
    traceback.print_exc()
    print('FAIL paid-tool check', flush=True)
    sys.stdout.flush()
    os._exit(1)
finally:
    watchdog.cancel()
