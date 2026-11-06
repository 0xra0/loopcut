"""Shared visible-panel checks: real UI events, local scripted replies and watchdogs."""
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def environment(data: Path):
    return dict(os.environ, LOOPCUT_DATA_DIR=str(data), LOOPCUT_TELEMETRY='off', LOOPCUT_UPDATE_URL='off',
                LOOPCUT_API_KEY='test-key', LOOPCUT_BASE_URL='http://127.0.0.1:1',
                LOOPCUT_MODEL='scripted-model', LOOPCUT_AUTO_RUN='true', LOOPCUT_AUTO_LOOK='false',
                LOOPCUT_ROUTER='off', LOOPCUT_ORCHESTRATE='false')


def setup(data: Path, out: Path, seconds: float):
    os.environ.update(environment(data))
    import bpy
    import checkout
    checkout.use()
    import loopcut
    loopcut.register()
    onboarding = sys.modules.get('loopcut.onboarding')
    if onboarding is not None and bpy.app.timers.is_registered(onboarding.start):
        bpy.app.timers.unregister(onboarding.start)
    bpy.context.preferences.view.show_splash = False
    return Gui(out, seconds)


class Gui:
    def __init__(self, out, seconds):
        self.out, self.deadline = out, time.monotonic() + seconds
        out.mkdir(parents=True, exist_ok=True)

    def window(self):
        import bpy
        return bpy.context.window_manager.windows[0]

    def open_panel(self):
        import bpy
        from loopcut.ui import host
        window = self.window()
        if not any(host.is_host(a) for a in window.screen.areas):
            area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
            with bpy.context.temp_override(window=window, area=area):
                bpy.ops.loopcut.open()

    def display(self):
        from loopcut.ui import host
        area = next(a for a in self.window().screen.areas if host.is_host(a))
        return area, host._displays[area.as_pointer()]

    def click(self, action):
        from loopcut.ui import layout
        area, display = self.display()
        hit = next((h for h in display['hits'] if h['action'] == action), None)
        assert hit, f'Action is not visible: {action}'
        center_x, center_y = hit['x'] + hit['w']/2, hit['y'] + hit['h']/2
        assert 0 <= center_y < display['height'] and layout.hit_test(display, center_x, center_y) == action
        region = next(r for r in area.regions if r.type == 'WINDOW')
        x = region.x + int(hit['x'] + hit['w']/2)
        y = region.y + int(display['height'] - hit['y'] - hit['h']/2)
        window = self.window()
        window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x, y=y)
        window.event_simulate(type='LEFTMOUSE', value='PRESS', x=x, y=y)
        window.event_simulate(type='LEFTMOUSE', value='RELEASE', x=x, y=y)

    def reveal(self, action):
        from loopcut import state
        from loopcut.ui import host, layout
        _, display = self.display()
        limit = int(display['max_scroll'])
        for position in range(0, limit+151, 150):
            state.session()['scroll'] = min(position, limit)
            host.tag_redraw_all()
            yield 0.2
            _, display = self.display()
            if any(h['action'] == action and 0 <= h['y'] + h['h']/2 < display['height']
                   and layout.hit_test(display, h['x'] + h['w']/2, h['y'] + h['h']/2) == action
                   for h in display['hits']):
                return
        raise AssertionError(f'Action could not be scrolled into view: {action}')

    def screenshot(self, name):
        import bpy
        with bpy.context.temp_override(window=self.window()):
            assert bpy.ops.screen.screenshot(filepath=str(self.out/name)) == {'FINISHED'}

    def run(self, stages):
        import bpy
        def tick():
            try:
                assert time.monotonic() < self.deadline, 'GUI watchdog timeout'
                return next(stages)
            except StopIteration:
                finish(0, 'GUI CHECK PASS')
            except Exception:
                traceback.print_exc()
                finish(1, 'GUI CHECK FAIL')
        bpy.app.timers.register(tick, first_interval=1.0, persistent=True)


def finish(code, message):
    print(message, flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def start_agent(codes, text='Edit the synthetic scene', *, hold_final=False):
    """Run the production agent loop and real main-thread tools; replace only the model transport."""
    from loopcut import agent, checkpoints, config, conversations, llm, state, tools
    session = state.session()
    item = state.item_user(text)
    session['items'].append(item)
    session['messages'].append({'role': 'user', 'content': text})
    checkpoints.begin_turn(session, item)
    replies = [llm.Completion(tool_calls=[llm.ToolCall(id=f'call_{n}', name='run_python',
               arguments=json.dumps({'code': code, 'summary': f'Synthetic edit {n}'}))]) for n, code in enumerate(codes)]
    replies.append(llm.Completion(text='The scene check finished.', finish_reason='stop'))
    def stream(**kwargs):
        assert replies, 'Unexpected extra model request'
        result = replies.pop(0)
        if result.text and hold_final:
            while not kwargs['is_cancelled']():
                time.sleep(0.1)
            raise llm.Cancelled()
        if result.text:
            kwargs['on_text'](result.text)
        return result
    llm.stream_chat = stream
    spec = agent.LoopSpec(role='single', system=lambda s: 'Execute the supplied scene edits.',
                          schemas=lambda: tools.SCHEMAS, save=conversations.save, asks=False,
                          changes_card=False, reference_card=False)
    turn = agent.Turn()
    turn.config, turn.spec = config.load(), spec
    turn.roots = (Path.cwd(),)
    session['busy'], session['turn'] = True, turn
    conversations.save(session, running=True)
    turn.thread = threading.Thread(target=agent.run_loop, args=(session, turn, spec), daemon=True)
    turn.thread.start()
    return turn
