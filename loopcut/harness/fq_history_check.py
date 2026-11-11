"""GUI recovery after interrupted turns and normal quits, with history and a continued turn.

python fq_history_check.py --blender blender.exe --out <evidence> --seconds 120
Uses isolated Blender temp directories. Writers either quit normally or are killed mid-turn.
Requires a Loopcut GUI build and an unlocked desktop. No live model service is used.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def child(phase, recovery, ending, root, out, seconds):
    from fq_gui import setup, start_agent
    gui = setup(root/'data', out, seconds)
    import bpy
    from loopcut import conversations as cv, state
    assert not bpy.app.background, 'A visible Blender window is required'
    def stages():
        if phase == 'write':
            session = state.reset()
            gui.open_panel()
            yield 0.8
            assert not bpy.data.filepath
            start_agent(["import bpy\nbpy.ops.mesh.primitive_cube_add()\nbpy.context.object.name = 'BeforeCrashCube'"],
                        text='Add a cube', hold_final=ending == 'crash')
            while not bpy.data.objects.get('BeforeCrashCube'):
                yield 0.1
            yield 0.5
            if ending == 'quit':
                while session['busy']:
                    yield 0.1
                cv.save(session)
            else:
                assert session['busy']
            saved = json.loads((cv._folder(session['id'])/'conversation.json').read_text())
            assert bool(saved.get('turn_running')) == (ending == 'crash')
            assert 'loopcut_snapshot_write' in dir(bpy.ops.wm), 'Use the Loopcut Blender build'
            # A clean quit deletes this process's own <pid>_autosave.blend, so after a normal quit the auto-save
            # that Recover Auto Save finds is another process's: name it after the driver's pid instead.
            owner = os.getpid() if ending == 'crash' else os.getppid()
            target = root/'native-temp'/('quit.blend' if recovery == 'last' else f'{owner}_autosave.blend')
            assert bpy.ops.wm.loopcut_snapshot_write(filepath=str(target)) == {'FINISHED'}
            gui.screenshot('before-exit.png')
            (root/'ready.json').write_text(json.dumps({'id': session['id'], 'snapshot': str(target)}))
            if ending == 'quit':
                bpy.ops.wm.quit_blender()
            # Interrupted writers stay alive until the parent kills them.
            while True:
                yield 0.2
        notes = json.loads((root/'ready.json').read_text())
        conversation = notes['id']
        gui.open_panel()
        yield 1.0
        _, display = gui.display()
        offered = ('open_conversation', conversation) in [h['action'] for h in display['hits']]
        assert offered == (ending == 'crash'), 'First-draw offer must follow the interrupted-turn marker'
        gui.screenshot('first-draw.png')
        # Let Blender dispatch its native load_post handler; never call lifecycle handlers manually.
        if recovery == 'last':
            result = bpy.ops.wm.recover_last_session(use_scripts=False)
        else:
            result = bpy.ops.wm.recover_auto_save(filepath=notes['snapshot'], use_scripts=False)
        assert result == {'FINISHED'}, f'Native {recovery} recovery failed'
        yield 1.0
        gui.open_panel()
        yield 0.8
        session = state.session()
        assert session['id'] == conversation, 'Native recovery did not resume the untitled chat'
        assert bpy.data.objects.get('BeforeCrashCube') is not None
        assert any(i.get('text') == cv.CLOSED_MESSAGE for i in session['items']) == (ending == 'crash')
        gui.screenshot('native-recovered.png')
        gui.click(('history_open', None))
        yield 0.5
        _, display = gui.display()
        assert ('open_conversation', conversation) in [h['action'] for h in display['hits']]
        gui.screenshot('all-conversations.png')
        gui.click(('open_conversation', conversation))
        yield 0.5
        assert state.session()['id'] == conversation
        project = out/'recovered-project.blend'
        assert bpy.ops.wm.save_as_mainfile(filepath=str(project)) == {'FINISHED'}
        assert conversation in [r['id'] for r in cv.list_for(cv.project_key(str(project)))]
        session = state.session()
        turn = start_agent(["import bpy\nbpy.ops.mesh.primitive_cube_add(size=0.5)\nbpy.context.object.name = 'ContinuedCube'"],
                           text='Add a smaller cube')
        while session['busy']:
            yield 0.1
        assert turn.outcome != 'error', session['items']
        assert bpy.data.objects.get('ContinuedCube') is not None
        assert cv.load(conversation)['messages'] == session['messages']
        yield 0.8
        gui.screenshot('continued-turn.png')
        (out/'result.txt').write_text(f'PASS: {ending}, first draw, native {recovery} recovery, '
                                     'All conversations click, first-save attachment and continued agent turn.\n')
        print(f'HISTORY PASS: {ending}, native {recovery} recovery, GUI and continued agent loop', flush=True)
    gui.run(stages())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blender', default=os.environ.get('LOOPCUT_BLENDER'), required=not os.environ.get('LOOPCUT_BLENDER'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=120)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for recovery, ending in ((r, e) for r in ('last', 'auto') for e in ('crash', 'quit')):
        with tempfile.TemporaryDirectory(prefix='loopcut-history-') as temporary:
            root = Path(temporary)
            native_temp = root/'native-temp'
            native_temp.mkdir()
            from fq_gui import environment
            env = dict(environment(root/'data'), TEMP=str(native_temp), TMP=str(native_temp), TMPDIR=str(native_temp))
            out = args.out.resolve()/f'{recovery}-{ending}'
            out.mkdir(parents=True, exist_ok=True)
            def command(phase):
                return [args.blender, '--factory-startup', '--enable-event-simulate', '--python-exit-code', '1',
                        '--python', str(Path(__file__).resolve()), '--', '--child', phase, recovery, ending,
                        str(root), str(out), str(args.seconds)]
            process = subprocess.Popen(command('write'), env=env)
            try:
                deadline = time.monotonic()+args.seconds
                while not (root/'ready.json').exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('Writer did not reach the crash-ready marker')
                    time.sleep(0.2)
                if ending == 'quit':
                    assert process.wait(timeout=args.seconds) == 0, 'Normal quit failed'
            except Exception as ex:
                print(f'HISTORY FAIL: {ex}', file=sys.stderr)
                return 1
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            try:
                result = subprocess.run(command('read'), env=env, timeout=args.seconds+15)
            except subprocess.TimeoutExpired:
                print(f'HISTORY FAIL: {recovery} watchdog timeout', file=sys.stderr)
                return 1
            if result.returncode:
                return result.returncode
    return 0


if __name__ == '__main__':
    if '--child' in sys.argv:
        n = sys.argv.index('--child')
        child(sys.argv[n+1], sys.argv[n+2], sys.argv[n+3], Path(sys.argv[n+4]), Path(sys.argv[n+5]), float(sys.argv[n+6]))
    else:
        sys.exit(main())
