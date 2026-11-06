"""Visible Windows storage recovery with real low disk, agent-loop edits and Documents delivery.

Prepare a disposable volume with 1-64 MiB free. This script does not fill or format disks.
python fq_storage_check.py --blender blender.exe --low-space-dir R:\\checks --out <evidence>
Requires a Loopcut GUI build and an unlocked desktop; writes synthetic files to real Documents.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def child(data, out, seconds):
    from fq_gui import setup, start_agent
    gui = setup(data, out, seconds)
    import bpy
    from loopcut import checkpoints as cp, files, state, tools, workspace
    assert not bpy.app.background, 'A visible Blender window is required'
    free = shutil.disk_usage(data)[2]
    assert 1024**2 < free < cp.FREE_RESERVE, f'Test volume must have 1-64 MiB free; got {free}'
    def stages():
        session = state.reset()
        gui.open_panel()
        yield 0.8
        turn = start_agent([
            "import bpy\nbpy.ops.mesh.primitive_cube_add()\nbpy.context.object.name = 'RefusedCube'",
            "import bpy\nbpy.ops.mesh.primitive_cube_add()\nbpy.context.object.name = 'LaterCube'",
            "import bpy\nbpy.ops.mesh.primitive_uv_sphere_add(segments=8, ring_count=4)\nbpy.context.object.name = 'LaterSphere'"])
        while session['busy']:
            yield 0.1
        assert turn.outcome != 'error', session['items']
        assert bpy.data.objects.get('RefusedCube') is None
        assert bpy.data.objects.get('LaterCube') and bpy.data.objects.get('LaterSphere')
        assert sum(bool(i.get('storage')) for i in session['items']) == 1
        yield 0.8
        yield from gui.reveal(('storage_continue', None))
        _, display = gui.display()
        actions = {h['action'][0] for h in display['hits']}
        assert {'storage_cleanup', 'storage_preferences', 'storage_continue'} <= actions
        gui.screenshot('low-space-card.png')
        gui.click(('storage_continue', None))
        yield 0.5
        assert session['checkpoints_disabled']
        assert not any(i.get('storage') for i in session['items'])
        old = data/'workspaces'/('e'*32)
        old.mkdir(parents=True)
        (old/workspace.SOURCE).write_bytes(b'x'*8192)
        result = cp.cleanup_storage(budget_bytes=cp.storage_size()-4096)
        assert not old.exists() and result['bytes'] <= result['budget'], result
        session['mode'] = 'workspace'
        # Keep workspace intermediates off the small test volume for export operations.
        working = out/'working'
        working.mkdir()
        visible = files.blend_base()[0]
        before = bpy.data.filepath
        import addon_utils
        addon_utils.enable('io_scene_fbx')
        code = (f"import bpy\nbpy.ops.wm.save_as_mainfile(filepath={str(working/'delivery.blend')!r})\n"
                f"bpy.ops.export_scene.fbx(filepath={str(working/'delivery.fbx')!r})")
        result = tools.run_python(code)
        assert result.ok, result.text
        assert bpy.data.filepath == before
        for suffix in ('.blend', '.fbx'):
            path = visible/('delivery'+suffix)
            assert path.is_file() and (working/path.name).is_file()
            assert str(path) in result.text
        gui.open_panel()
        session['scroll'] = 0.0
        yield 0.8
        gui.screenshot('documents-delivery.png')
        delivered = visible/'delivery.fbx'
        yield from gui.reveal(('reference_folder', str(delivered)))
        gui.click(('reference_folder', str(delivered)))
        yield 0.8
        assert state.ui.get('action_notice', {}).get('text', '').startswith('Opened'), state.ui
        gui.screenshot('show-in-folder.png')
        yield from gui.reveal(('reference_open', str(delivered)))
        app = association(delivered.suffix)
        errors_before = len([i for i in session['items'] if i.get('kind') == 'error'])
        guard = OpenGuard(out, delivered, app)
        gui.click(('reference_open', str(delivered)))
        yield 0.8
        guard.returned()
        notice = state.ui.get('action_notice', {}).get('text', '')
        errors = [i.get('text', '') for i in session['items'] if i.get('kind') == 'error'][errors_before:]
        observed = guard.report(notice, errors)
        if app:
            assert notice.startswith('Opened'), state.ui
            assert not errors, errors
            opened = 'Open/Show clicks'
        else:
            # No app is registered for this file type here, so nothing can open it. The panel must stay
            # responsive and show the user what happened, either way.
            assert observed['ui_blocked_seconds'] < OpenGuard.BLOCKED, observed
            assert notice.startswith(f'No app is set to open {delivered.suffix.lower()} files, so Loopcut opened the '
                                     'folder'), f'Open on {delivered.name} with no associated app must open its folder: {observed}'
            assert not any('pick an app' in str(w.get('title', '')).lower() for w in observed.get('windows_opened', [])), (
                f'Open with no associated app must not start the Windows app picker: {observed}')
            opened = f'Show click; Open without a {delivered.suffix} app gave notice {notice!r}, errors {errors!r}'
        gui.screenshot('open-click.png')
        (out/'result.txt').write_text(f'PASS: real low disk, agent refusal then two edits, card click, budget prune, '
                                     f'working copies and visible delivery with {opened}.\nSaved to {visible}\n')
        print(f'STORAGE PASS: real Documents folder {files.documents_folder()}; deliverables {visible}', flush=True)
    gui.run(stages())


def association(suffix):
    """The program Windows opens this file type with, or '' when no app is associated (Windows only)."""
    import ctypes
    from ctypes import wintypes
    shlwapi = ctypes.WinDLL('shlwapi')
    shlwapi.AssocQueryStringW.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                          wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    shlwapi.AssocQueryStringW.restype = ctypes.c_long
    size = wintypes.DWORD(1024)
    found = ctypes.create_unicode_buffer(size.value)
    # ASSOCF_INIT_IGNOREUNKNOWN: an unregistered type reports no app instead of the "Open with" chooser.
    status = shlwapi.AssocQueryStringW(0x400, 2, suffix, 'open', found, ctypes.byref(size))  # ASSOCSTR_EXECUTABLE
    app = found.value if status == 0 else ''
    print(f'STORAGE OBSERVED: {suffix} opens with {app or "no associated app"} (AssocQueryString 0x{status & 0xffffffff:08x})',
          flush=True)
    return app


def top_windows():
    """Visible top-level windows: handle, title, class and owning process."""
    import ctypes
    from ctypes import wintypes
    user, kernel = ctypes.WinDLL('user32'), ctypes.WinDLL('kernel32')
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                  ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    found = []

    def visit(hwnd, _):
        if user.IsWindowVisible(hwnd):
            title, kind = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
            user.GetWindowTextW(hwnd, title, 256)
            user.GetClassNameW(hwnd, kind, 256)
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            image = ''
            process = kernel.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            if process:
                name, size = ctypes.create_unicode_buffer(512), wintypes.DWORD(512)
                if kernel.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size)):
                    image = Path(name.value).name
                kernel.CloseHandle(process)
            found.append({'hwnd': int(hwnd or 0), 'title': title.value, 'class': kind.value,
                          'pid': pid.value, 'process': image})
        return True
    user.EnumWindows(callback_type(visit), 0)
    return found


class OpenGuard:
    """Watches one Open click from a side thread. ShellExecute runs on Blender's main thread, so a system
    dialog it raises can block the UI. Past BLOCKED seconds the guard records the new windows and a desktop
    capture, closes what the click opened, and ends the run with a clear message if Blender stays blocked."""
    BLOCKED = 10.0

    def __init__(self, out, target, app):
        import threading
        import time
        self.out, self.target, self.app = out, target, app
        self.before = {w['hwnd'] for w in top_windows()}
        self.clicked, self.back = time.monotonic(), threading.Event()
        self.record = {'file': target.name, 'associated_app': app, 'ui_blocked_seconds': 0.0,
                       'windows_opened': [], 'closed_by_check': []}
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _new_windows(self):
        return [w for w in top_windows() if w['hwnd'] not in self.before]

    def _capture(self, name):
        import subprocess
        script = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
                  "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
                  "$i=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
                  "$g=[System.Drawing.Graphics]::FromImage($i); $g.CopyFromScreen($b.Left,$b.Top,0,0,$i.Size); "
                  f"$i.Save('{str(self.out/name)}')")
        try:
            subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], timeout=30, check=False)
        except Exception as ex:
            self.record['capture_error'] = repr(ex)

    def _close_new(self):
        import ctypes
        user = ctypes.WinDLL('user32')
        user.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        for window in self._new_windows():
            # Only the "Open with" chooser or a dialog inside Blender; never an app the file opened in.
            chooser = window['process'].lower() == 'openwith.exe'
            dialog = window['pid'] == os.getpid() and window['class'] == '#32770'
            if self.app or not (chooser or dialog):
                continue
            user.PostMessageW(window['hwnd'], 0x0010, None, None)  # WM_CLOSE
            self.record['closed_by_check'].append(window)

    def _watch(self):
        import json
        import os
        import time
        if self.back.wait(self.BLOCKED):
            return
        self.record['ui_blocked_seconds'] = round(time.monotonic() - self.clicked, 1)
        self.record['windows_opened'] = self._new_windows()
        print('STORAGE OBSERVED: Blender\'s main thread has not returned from the Open click after '
              f'{self.BLOCKED:g} s; new windows {self.record["windows_opened"]}', flush=True)
        self._capture('open-blocked-desktop.png')
        self._close_new()
        if self.back.wait(20):
            return
        self.record['ui_blocked_seconds'] = round(time.monotonic() - self.clicked, 1)
        (self.out/'open-click.json').write_text(json.dumps(self.record, indent=2))
        print(f'STORAGE FAIL: Open on {self.target.name} (associated app: {self.app or "none"}) blocked '
              f'Blender\'s UI for {self.record["ui_blocked_seconds"]} s, still blocked after closing the windows it '
              f'opened; see {self.out/"open-click.json"} and open-blocked-desktop.png', flush=True)
        os._exit(1)

    def returned(self):
        import time
        if not self.back.is_set():
            elapsed = round(time.monotonic() - self.clicked, 1)
            self.back.set()
            self.record['returned_after_seconds'] = elapsed  # Includes the check's own 0.8 s wait.
            if elapsed >= self.BLOCKED:
                self.record['ui_blocked_seconds'] = elapsed

    def report(self, notice, errors):
        import json
        self.thread.join(timeout=60)
        late = self._new_windows()
        self.record['windows_opened'] = self.record['windows_opened'] or late
        if late:
            self._close_new()  # Leave no chooser open on the desktop.
        self.record.update(notice=notice, errors=errors)
        (self.out/'open-click.json').write_text(json.dumps(self.record, indent=2))
        print(f'STORAGE OBSERVED: Open click result {json.dumps(self.record)}', flush=True)
        return self.record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blender', default=os.environ.get('LOOPCUT_BLENDER'), required=not os.environ.get('LOOPCUT_BLENDER'))
    parser.add_argument('--low-space-dir', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=180)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    args.low_space_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='loopcut-storage-', dir=args.low_space_dir) as data:
        from fq_gui import environment
        command = [args.blender, '--factory-startup', '--enable-event-simulate', '--python-exit-code', '1',
                   '--python', str(Path(__file__).resolve()), '--', '--child', data, str(args.out.resolve()), str(args.seconds)]
        try:
            return subprocess.run(command, env=environment(Path(data)), timeout=args.seconds+15).returncode
        except subprocess.TimeoutExpired:
            print('STORAGE FAIL: process watchdog timeout', file=sys.stderr)
            return 1


if __name__ == '__main__':
    if '--child' in sys.argv:
        n = sys.argv.index('--child')
        child(Path(sys.argv[n+1]), Path(sys.argv[n+2]), float(sys.argv[n+3]))
    else:
        sys.exit(main())
