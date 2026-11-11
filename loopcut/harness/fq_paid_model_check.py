"""Live model acceptance in a disposable Windows Blender scene; paid jobs are blocked.

    blender --factory-startup --python loopcut/harness/fq_paid_model_check.py -- --out OUTPUT_DIRECTORY --variant prompted

Uses the existing ChatGPT-plan connection and its normal allowance. Keep the desktop unlocked.
Synthetic images and prices are local. Saves assertions, transcript and a desktop screenshot.
Run again with --variant unprompted for a job-only request. Each variant has its own artifact folder.
The companion fq_paid_check.py exercises deterministic gates without a model connection.
"""

import contextlib
import argparse
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import traceback

import bpy
import numpy as np
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout
checkout.use()

arguments = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--out', required=True, type=Path)
parser.add_argument('--variant', choices=('prompted', 'unprompted'), default='prompted')
options = parser.parse_args(arguments)
VARIANT = options.variant
OUT = options.out.resolve() / VARIANT
OUT.mkdir(parents=True, exist_ok=True)
os.environ['LOOPCUT_DATA_DIR'] = tempfile.mkdtemp(prefix='loopcut-paid-model-')
os.environ['LOOPCUT_TELEMETRY'] = 'off'

import loopcut
from loopcut import agent, attachments, cloud, cloud_http, config, spending, state, tools
from loopcut.byok import catalog

TIMEOUT = 240
STACK = contextlib.ExitStack()
REQUESTS, CARDS, ASSERTIONS = [], [], []
STARTED = None


def check(condition, message):
    ASSERTIONS.append({'ok': bool(condition), 'assertion': message})
    if not condition:
        raise AssertionError(message)
    print('PASS ' + message, flush=True)


def finish(code):
    session = state.session()
    (OUT / 'transcript.json').write_text(json.dumps(session.get('messages', []), indent=2, default=str), encoding='utf-8')
    (OUT / 'assertions.json').write_text(json.dumps({'variant': VARIANT, 'exit_code': code,
                                                   'checks': ASSERTIONS, 'requests': REQUESTS, 'cards': CARDS},
                                                   indent=2, default=str), encoding='utf-8')
    try:
        window = bpy.context.window_manager.windows[0]
        with bpy.context.temp_override(window=window):
            bpy.ops.screen.screenshot(filepath=str(OUT / 'desktop.png'))
    except Exception:
        traceback.print_exc()
        code = 1
    STACK.close()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def watchdog():
    print('FAIL live paid-tool check timed out', flush=True)
    # A blocked UI cannot service screenshot operators. Preserve the worker-visible transcript.
    try:
        (OUT / 'timeout-transcript.json').write_text(json.dumps(state.session().get('messages', []), default=str), encoding='utf-8')
    finally:
        os._exit(2)


def pictures():
    """Front figure, cropped head, rear figure and three views, drawn from synthetic pixels."""
    def figure(rear=False, portrait=False):
        pixels = np.ones((256, 256, 4), dtype=np.float32)
        y, x = np.mgrid[:256, :256]
        def ellipse(cx, cy, rx, ry, color):
            pixels[((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1, :3] = color
        skin, suit, hair = (.8, .58, .38), (.18, .42, .75), (.18, .12, .08)
        if portrait:
            ellipse(128, 8, 120, 85, suit)
            ellipse(128, 137, 70, 95, skin)
            ellipse(102, 157, 7, 9, hair)
            ellipse(154, 157, 7, 9, hair)
            ellipse(128, 101, 22, 4, hair)
        else:
            for cx in (105, 151):
                ellipse(cx, 52, 16, 45, suit)
            ellipse(128, 133, 38, 50, suit)
            ellipse(76, 126, 13, 44, suit)
            ellipse(180, 126, 13, 44, suit)
            ellipse(128, 212, 28, 32, hair if rear else skin)
            if rear:
                pixels[100:170, 125:131, :3] = (.08, .2, .4)
            else:
                ellipse(117, 218, 3, 4, hair)
                ellipse(139, 218, 3, 4, hair)
                ellipse(128, 200, 9, 2, hair)
        return pixels
    front, portrait, rear = figure(), figure(portrait=True), figure(rear=True)
    sheet = np.concatenate((front, rear, front[:, ::-1]), axis=1)
    for name, pixels in zip(('sample-a.png', 'sample-b.png', 'sample-c.png', 'sample-d.png'),
                            (front, portrait, rear, sheet)):
        path = OUT / name
        tools._save_pixels(pixels, path)
        yield str(path)


def transport(request, *, timeout):
    payload = json.loads(request.data) if request.data else None
    REQUESTS.append({'method': request.method, 'path': request.full_url, 'body': payload})
    if request.method == 'GET' and request.full_url.endswith('/v1/manifest'):
        value = MANIFEST
    elif request.method == 'POST' and request.full_url.endswith('/v1/quotes'):
        value = {'quote_id': 'qt_synthetic', 'credits': 80, 'expires_at': '2099-01-01T00:00:00Z'}
    elif request.method == 'POST' and request.full_url.endswith('/v1/blobs/check'):
        value = {'missing': []}
    else:
        raise AssertionError('Unexpected service request; paid jobs and uploads are disabled')
    response = io.BytesIO(json.dumps(value).encode())
    response.status, response.headers = 200, {}
    return response


def decline(turn):
    # Record the actual card that the model reached, including any failed input requirements.
    for card in state.session()['items']:
        if card.get('status') == 'awaiting' and card.get('ask') in (spending.COST, spending.VENDOR):
            CARDS.append({key: card.get(key) for key in ('name', 'ask', 'cost', 'ask_lines')})
            return False
    return True


def model_prompt(variant):
    if variant == 'unprompted':
        return ('Turn the attached pictures into full-body 3D character models. '
                'Also add a cube and show me a quick camera render.')
    return (
        'Inspect each attached reference for making a full-body character model. Check the credit balance and '
        'conversion price before proposing paid work. Make a model only from a suitable single view. For '
        'unsuitable images, name the concern and suggest a clean front view. If a paid call is declined, '
        'continue the inspection. Also add a cube and inspect it with a quick camera render. Finish with a '
        'JSON object: balance, conversion_price, and images mapping each filename to input_check booleans '
        '(single_subject, whole_subject, front_or_three_quarter, single_view).')


def unprompted_assessments(calls):
    """Record proposals without requiring a response format in the job-only prompt."""
    rows = {name: [] for name in ('sample-a.png', 'sample-b.png', 'sample-c.png', 'sample-d.png')}
    for call in calls:
        if cloud.tool_name(call['name']) != 'image_to_3d':
            continue
        arguments = json.loads(call['arguments'])
        name = Path(arguments.get('image', '')).name
        check(name in rows, 'conversion names an attached reference')
        rows[name].append(arguments.get('input_check', {}))
    return rows


def start():
    global STARTED
    try:
        check(not bpy.app.background, 'GUI session is available')
        check(config.chatgpt_plan(config.load().base_url), 'existing model connection uses the ChatGPT plan')
        window = bpy.context.window_manager.windows[0]
        view = next(area for area in window.screen.areas if area.type == 'VIEW_3D')
        region = next(region for region in view.regions if region.type == 'WINDOW')
        with bpy.context.temp_override(window=window, area=view, region=region):
            bpy.ops.screen.area_split(direction='VERTICAL', factor=.7)
        panel = max((area for area in window.screen.areas if area.type == 'VIEW_3D'), key=lambda area: area.x)
        with bpy.context.temp_override(window=window, area=panel):
            bpy.ops.loopcut.open()
        session = state.session()
        session.update(auto_run=True, auto_heavy=True)
        check(not attachments.add(session, list(pictures())), 'four synthetic references are attached')
        STARTED = time.monotonic()
        check(agent.send(model_prompt(VARIANT)), VARIANT + ' real model turn started')
        bpy.app.timers.register(poll, first_interval=.25)
    except Exception:
        traceback.print_exc()
        finish(1)


def poll():
    session = state.session()
    if session['busy'] and time.monotonic() - STARTED < TIMEOUT:
        return .25
    try:
        check(not session['busy'], 'model turn finished within its deadline')
        check(not any(item.get('kind') == 'error' for item in session['items']), 'model turn completed without an error card')
        calls = [call['function'] for message in session['messages'] for call in message.get('tool_calls', [])]
        credit_reads = [i for i, call in enumerate(calls) if cloud.tool_name(call['name']) == 'credits']
        conversions = [i for i, call in enumerate(calls) if cloud.tool_name(call['name']) == 'image_to_3d']
        check(credit_reads and conversions and credit_reads[0] < conversions[0], 'model checked balance before its paid proposal')
        answers = [message.get('content') for message in session['messages']
                   if message['role'] == 'assistant' and isinstance(message.get('content'), str)]
        if VARIANT == 'prompted':
            text = answers[-1]
            result = json.loads(text[text.index('{'):text.rindex('}') + 1])
            check(result['balance'] == 517 and result['conversion_price'] == 80,
                  'model stated the observed balance and conversion price')
            rows = result['images']
            check(all(rows['sample-a.png'][field] is True for field in cloud.IMAGE_CHECK),
                  'model accepted the complete front reference')
            check(rows['sample-b.png']['whole_subject'] is False, 'model caught the cropped portrait')
            check(rows['sample-c.png']['front_or_three_quarter'] is False, 'model caught the rear view')
            check(rows['sample-d.png']['single_view'] is False, 'model caught the multi-view sheet')
        else:
            # Only assistant text preceding its first conversion counts as stating the price before paid use.
            before, count = [], 0
            for message in session['messages']:
                if message['role'] == 'assistant' and isinstance(message.get('content'), str):
                    before.append(message['content'])
                count += len(message.get('tool_calls', []))
                if count > conversions[0]:
                    break
            check(re.search(r'\b517\b', '\n'.join(before)) and re.search(r'\b80\b', '\n'.join(before)),
                  'unprompted model stated the observed balance and price before conversion')
            rows = unprompted_assessments(calls)
            (OUT / 'image-proposals.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
            check(any(all(row.get(field) is True for field in cloud.IMAGE_CHECK) for row in rows['sample-a.png']),
                  'unprompted model proposed the complete front reference with all checks')
            for name, field in (('sample-b.png', 'whole_subject'), ('sample-c.png', 'front_or_three_quarter'),
                                ('sample-d.png', 'single_view')):
                check(all(row.get(field) is False for row in rows[name]),
                      'unprompted model withheld or flagged unsuitable reference ' + name)
            print('REVIEW unprompted: inspect the transcript for every withheld image and its stated concern', flush=True)
        check(any(call['name'] == 'see_render' and json.loads(call['arguments']).get('render') is True for call in calls),
              'model used see_render for its camera check')
        check(not any('Render refused by the cost guard' in str(message.get('content', ''))
                      for message in session['messages'] if message['role'] == 'tool'), 'no inline-render refusals occurred')
        check(CARDS and any('80' in str(card['cost']) for card in CARDS), 'actual paid proposal reached a priced approval card')
        check(not any(request['path'].endswith('/v1/jobs') for request in REQUESTS), 'no paid job was submitted')
        finish(0)
    except Exception:
        traceback.print_exc()
        finish(1)


descriptor = catalog.descriptors(no_generation=False, private_mode=False, keys_set={'OPENAI_API_KEY': True})[0]
descriptor.update(name='image_to_3d', title='Image to 3D', description='Convert an image into a model.', backend='managed',
                  apply='none', latency='seconds', cost={'credits_estimate': 80, 'quote_required': True},
                  approval={'confirm_over_credits': 100}, input_schema={'type': 'object', 'properties': {}},
                  file_inputs=[{'field': 'image', 'required': True, 'max_mb': 8, 'accept': ['image/png']}])
MANIFEST = {'flags': {'services': True, 'premium_skills': False}, 'tools': [descriptor], 'skills': [],
            'account': {'credits': {'balance': 517}, 'settings': {'private_mode': False}}}
client = cloud_http.Client(base_url='https://service.test', api_key='lc_synthetic', transport=transport,
                           client_headers=cloud_http.contract_headers())
runtime = cloud.Runtime(client, Path(os.environ['LOOPCUT_DATA_DIR']) / 'cloud', signed_in=True)
runtime.manifest = MANIFEST
STACK.enter_context(patch.object(cloud, '_runtime', runtime))
STACK.enter_context(patch.object(cloud, '_snapshot', return_value=runtime))
STACK.enter_context(patch.object(cloud, 'sync_settings'))
STACK.enter_context(patch.object(agent, '_wait_for_approval', side_effect=decline))
bpy.context.preferences.view.show_splash = False
loopcut.register()
timer = threading.Timer(TIMEOUT + 15, watchdog)
timer.daemon = True
timer.start()
bpy.app.timers.register(start, first_interval=.5)
