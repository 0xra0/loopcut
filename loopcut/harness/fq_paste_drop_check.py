"""Repeated native clipboard paste and an OS model drop, using generated fixtures.

    Blender --factory-startup --enable-event-simulate --python loopcut/harness/fq_paste_drop_check.py

Run on an unlocked Windows desktop. A separate STA process performs a native OLE file drag into
Blender, so the operating system, Blender's drop routing and Loopcut's file handler all run.
"""

import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
os.environ["LOOPCUT_DATA_DIR"] = tempfile.mkdtemp(prefix="loopcut-input-check-")

import loopcut  # noqa: E402
from loopcut import conversations, state, tools, videoref  # noqa: E402
from loopcut.ui import host  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="loopcut-drop-check-"))
SHOTS = Path(os.environ.get("LOOPCUT_SHOTS") or WORK).resolve()
SHOTS.mkdir(parents=True, exist_ok=True)
SEEN = {}
STEPS = []


def finish(code, message):
    print(f"{message}; artifacts: {WORK}", flush=True)
    sys.stderr.flush()
    process = SEEN.get("drop_process")
    if process is not None and process.poll() is None:
        process.kill()
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.mouse_event(4, 0, 0, 0, 0)
    os._exit(code)


watchdog = threading.Timer(90, lambda: finish(1, "PASTE DROP CHECK TIMEOUT"))
watchdog.daemon = True
watchdog.start()


def step(fn):
    STEPS.append(fn)
    return fn


def click(hit_id):
    window, area = SEEN["window"], SEEN["panel"]
    display = host._displays[area.as_pointer()]
    hit = next(h for h in display["hits"] if h["id"] == hit_id)
    region = next(r for r in area.regions if r.type == "WINDOW")
    x = region.x + int(hit["x"] + hit["w"] / 2)
    y = region.y + int(display["height"] - hit["y"] - hit["h"] / 2)
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x, y=y)


def screenshot(name):
    with bpy.context.temp_override(window=SEEN["window"], area=SEEN["panel"]):
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=2)
        bpy.ops.screen.screenshot(filepath=str(SHOTS / f"{name}.png"))


def type_text(text):
    for ch in text:
        kind = "SPACE" if ch == " " else ch.upper()
        SEEN["window"].event_simulate(type=kind, value="PRESS", unicode=ch)
        SEEN["window"].event_simulate(type=kind, value="RELEASE")


def _blender_hwnd(user):
    """This process's Blender window. GetActiveWindow is NULL when the window never became the foreground
    one (a session started by a scheduler may be refused focus), so fall back to Blender's GHOST window
    class, limited to windows owned by this process."""
    import ctypes
    from ctypes import wintypes
    active = user.GetActiveWindow()
    if active:
        return active
    found = []
    pid = os.getpid()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    def visit(hwnd, _):
        owner_pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        name = ctypes.create_unicode_buffer(64)
        user.GetClassNameW(hwnd, name, 64)
        if owner_pid.value == pid and name.value == "GHOST_WindowClass":
            found.append(hwnd)
        return True
    user.EnumWindows(callback_type(visit), 0)
    return found[0] if found else None


def _probe_clipboard():
    """Open and close the native clipboard once, as this script starts and before it registers the add-on."""
    if sys.platform != "win32":
        return "not Windows"
    import ctypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    if user.OpenClipboard(None):
        user.CloseClipboard()
        return "opened"
    return f"refused (error {ctypes.get_last_error()})"


CLIPBOARD_AT_IMPORT = _probe_clipboard()
print(f"PROBE clipboard at script start: {CLIPBOARD_AT_IMPORT}", flush=True)


def _clipboard_diagnosis():
    """Why OpenClipboard is refused: the window holding the clipboard open, and whether this thread's
    desktop is the one receiving input (it is not while the workstation is locked)."""
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.GetOpenClipboardWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user.GetThreadDesktop.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                  ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def desktop_name(handle):
        name, needed = ctypes.create_unicode_buffer(128), wintypes.DWORD()
        user.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed))  # UOI_NAME
        return name.value

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    advapi.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    user.OpenWindowStationW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
    user.OpenWindowStationW.restype = wintypes.HANDLE
    user.CloseWindowStation.argtypes = [wintypes.HANDLE]
    user.GetClipboardOwner.restype = wintypes.HWND

    def integrity(process):
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return f"unreadable (error {ctypes.get_last_error()})"
        try:
            buffer, needed = ctypes.create_string_buffer(256), wintypes.DWORD()
            if not advapi.GetTokenInformation(token, 25, buffer, 256, ctypes.byref(needed)):  # TokenIntegrityLevel
                return f"unreadable (error {ctypes.get_last_error()})"
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            count = advapi.GetSidSubAuthorityCount(sid)[0]
            return hex(advapi.GetSidSubAuthority(sid, count - 1)[0])
        finally:
            kernel.CloseHandle(token)

    def window(hwnd):
        pid, title, kind = wintypes.DWORD(), ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        user.GetWindowTextW(hwnd, title, 256)
        user.GetClassNameW(hwnd, kind, 256)
        found = {"title": title.value, "class": kind.value, "pid": pid.value, "is_this_process": pid.value == os.getpid()}
        process = kernel.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if process:
            name, size = ctypes.create_unicode_buffer(512), wintypes.DWORD(512)
            if kernel.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size)):
                found["process"] = Path(name.value).name
            found["integrity"] = integrity(process)
            kernel.CloseHandle(process)
        return found

    info = {"probe_at_import": CLIPBOARD_AT_IMPORT}
    holder = user.GetOpenClipboardWindow()
    info["holder"] = window(holder) if holder else None
    owner = user.GetClipboardOwner()
    info["last_owner"] = window(owner) if owner else None
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    info["this_integrity"] = integrity(kernel.GetCurrentProcess())
    station = user.OpenWindowStationW("WinSta0", False, 0x0004)  # WINSTA_ACCESSCLIPBOARD
    info["winsta0_clipboard_right"] = "granted" if station else f"denied (error {ctypes.get_last_error()})"
    if station:
        user.CloseWindowStation(station)
    desktop = user.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    info["input_desktop"] = desktop_name(desktop) if desktop else f"not readable (error {ctypes.get_last_error()})"
    if desktop:
        user.CloseDesktop(desktop)
    info["thread_desktop"] = desktop_name(user.GetThreadDesktop(kernel.GetCurrentThreadId()))
    session = wintypes.DWORD()
    kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    info["session"], info["console_session"] = session.value, kernel.WTSGetActiveConsoleSessionId()
    info["suspects"] = _clipboard_suspects(session.value)
    return info


def _clipboard_suspects(session):
    """This session's hung processes and script hosts: a thread that opened the clipboard without a window
    and never closed it locks it for every other program, and Windows does not say which thread that is."""
    import csv
    import io
    hosts = {"python.exe", "pythonw.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "wscript.exe", "cscript.exe",
             "autohotkey.exe", "autohotkey64.exe", "node.exe", "blender.exe", "rdpclip.exe"}
    try:
        listing = subprocess.run(["tasklist", "/v", "/fo", "csv", "/nh", "/fi", f"SESSION eq {session}"],
                                 capture_output=True, text=True, timeout=30).stdout
    except Exception as ex:
        return repr(ex)
    found = []
    for row in csv.reader(io.StringIO(listing)):
        if len(row) < 9:
            continue
        image, pid, status, cpu = row[0], row[1], row[5], row[7]
        if status.lower() == "not responding" or image.lower() in hosts:
            found.append(f"{image} pid {pid} {status} cpu {cpu}" + (" (this process)" if pid == str(os.getpid()) else ""))
    return found


def copy_image(color):
    # CF_DIBV5 is read directly by Blender's Windows clipboard image implementation.
    import ctypes
    from ctypes import wintypes
    assert sys.platform == "win32"
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.OpenClipboard.argtypes, user.OpenClipboard.restype = [wintypes.HWND], wintypes.BOOL
    user.GetActiveWindow.restype = wintypes.HWND
    user.EmptyClipboard.restype = wintypes.BOOL
    user.CloseClipboard.restype = wintypes.BOOL
    user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user.SetClipboardData.restype = wintypes.HANDLE
    user.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user.IsClipboardFormatAvailable.restype = wintypes.BOOL
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = wintypes.HANDLE
    kernel.GlobalLock.argtypes, kernel.GlobalLock.restype = [wintypes.HANDLE], ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel.GlobalFree.argtypes, kernel.GlobalFree.restype = [wintypes.HANDLE], wintypes.HANDLE
    header = bytearray(124)
    pixels = bytes([*(round(c * 255) for c in color), 255]) * (32 * 16)
    struct.pack_into("<IiiHHII", header, 0, 124, 32, 16, 1, 32, 3, len(pixels))
    struct.pack_into("<IIIII", header, 40, 0xff, 0xff00, 0xff0000, 0xff000000, 0x73524742)
    struct.pack_into("<I", header, 108, 4)
    payload = bytes(header) + pixels
    handle = kernel.GlobalAlloc(0x42, len(payload))  # GMEM_MOVEABLE | GMEM_ZEROINIT
    assert handle, "clipboard allocation failed"
    transferred = False
    try:
        address = kernel.GlobalLock(handle)
        assert address, "clipboard allocation could not be locked"
        ctypes.memmove(address, payload, len(payload))
        kernel.GlobalUnlock(handle)
        owner = _blender_hwnd(user)
        assert owner, "Blender's native window was not found"
        opened, until = False, time.monotonic() + 10
        while not opened and time.monotonic() < until:  # Another clipboard client can hold it briefly.
            opened = bool(user.OpenClipboard(owner))
            error = ctypes.get_last_error()
            if not opened:
                time.sleep(0.05)
        assert opened, f"native clipboard could not be opened for 10 s (error {error}): {_clipboard_diagnosis()}"
        try:
            assert user.EmptyClipboard(), "could not empty native clipboard"
            assert user.SetClipboardData(17, handle), "could not set native clipboard image"
            transferred = True  # The OS now owns the allocation.
        finally:
            user.CloseClipboard()
    finally:
        if not transferred:
            kernel.GlobalFree(handle)
    assert user.IsClipboardFormatAvailable(17), "native image format was not published"
    assert not bpy.context.window_manager.clipboard, "fixture contains clipboard text"
    print("ASSERT fixture: native CF_DIBV5 image clipboard, no text", flush=True)


def scene_counts():
    keys = sum(len(fc.keyframe_points) for action in bpy.data.actions for layer in action.layers
               for strip in layer.strips for bag in strip.channelbags for fc in bag.fcurves)
    trees = list(bpy.data.node_groups)
    for collection in (bpy.data.materials, bpy.data.worlds, bpy.data.scenes):
        trees.extend(owner.node_tree for owner in collection if getattr(owner, "node_tree", None))
    return (set(bpy.data.objects.keys()), keys, sum(len(tree.nodes) for tree in trees))


def native_drop(path):
    import ctypes
    from ctypes import wintypes
    assert sys.platform == "win32", "the native OLE drag check requires Windows"
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid() and user32.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            windows.append((rect.right * rect.bottom, hwnd, rect.bottom))
        return True
    callback = callback_type(visit)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows(callback, 0)
    _, hwnd, height = max(windows)
    area = SEEN["panel"]
    point = wintypes.POINT(area.x + area.width // 2, height - area.y - area.height // 2)
    assert user32.ClientToScreen(hwnd, ctypes.byref(point))
    # WinForms implements IDataObject and IDropSource; DoDragDrop uses the native OLE route.
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;
public class FileDrag : Form {
  [DllImport("user32.dll")] static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] static extern void mouse_event(uint flags, uint x, uint y, uint data, UIntPtr extra);
  public string Source;
  public int TargetX, TargetY;
  protected override void OnShown(EventArgs e) {
    base.OnShown(e);
    var timer = new System.Windows.Forms.Timer { Interval = 300 };
    timer.Tick += (s, args) => {
      timer.Stop();
      int sx = Left + Width / 2, sy = Top + Height / 2;
      SetCursorPos(sx, sy);
      mouse_event(2, 0, 0, 0, UIntPtr.Zero);
      var mover = new Thread(() => {
        Thread.Sleep(400);
        for (int n = 1; n <= 12; n++) {
          SetCursorPos(sx + (TargetX - sx) * n / 12, sy + (TargetY - sy) * n / 12);
          Thread.Sleep(60);
        }
        Thread.Sleep(400);
        mouse_event(4, 0, 0, 0, UIntPtr.Zero);
      });
      mover.IsBackground = true;
      mover.Start();
      var data = new DataObject(DataFormats.FileDrop, new string[] { Source });
      var effect = DoDragDrop(data, DragDropEffects.Copy);
      Console.WriteLine("OLE drop effect: " + effect);
      Close();
    };
    timer.Start();
  }
}
'@
$form = New-Object FileDrag
$form.Source = '__SOURCE__'
$form.TargetX = __X__
$form.TargetY = __Y__
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point (__SX__, __SY__)
$form.Size = New-Object System.Drawing.Size (160, 100)
$form.Text = 'Generated file drag'
$form.TopMost = $true
[System.Windows.Forms.Application]::Run($form)
'''
    script = script.replace("__SOURCE__", str(path).replace("'", "''"))
    for marker, value in (("__X__", point.x), ("__Y__", point.y),
                          ("__SX__", point.x - 260), ("__SY__", point.y - 160)):
        script = script.replace(marker, str(value))
    command = WORK / "native-drop.ps1"
    command.write_text(script)
    SEEN["drop_log"] = (WORK / "native-drop.log").open("w")
    SEEN["drop_process"] = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(command)],
        stdout=SEEN["drop_log"], stderr=subprocess.STDOUT)


@step
def prepare():
    assert sys.platform == "win32", "run this check on Windows"
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    with bpy.context.temp_override(window=window, area=area):
        bpy.ops.loopcut.open()
    view = max((a for a in window.screen.areas if a != area), key=lambda a: a.width * a.height)
    view.type = "VIEW_3D"
    state.reset()
    SEEN.update(window=window, panel=area, images=set(bpy.data.images.keys()))
    cube = bpy.data.objects.get("Cube")
    assert cube is not None
    cube.keyframe_insert(data_path="location", frame=1)
    with bpy.context.temp_override(window=window, area=view, region=host._window_region(view)):
        assert "FINISHED" in bpy.ops.view3d.copybuffer(), "could not seed the Blender object copy buffer"
    copy_image((0.2, 0.6, 0.9))


@step
def focus():
    click("input.card")


def paste():
    assert state.session()["focused"], "composer did not take focus"
    assert not bpy.context.window_manager.clipboard, "fixture has clipboard text as well as an image"
    modifiers = {"oskey": True} if sys.platform == "darwin" else {"ctrl": True}
    region = host._window_region(SEEN["panel"])
    x, y = SEEN.get("paste_xy", (region.x + region.width // 2, region.y + region.height // 2))
    SEEN["window"].event_simulate(type="V", value="PRESS", x=x, y=y, **modifiers)
    SEEN["window"].event_simulate(type="V", value="RELEASE", x=x, y=y, **modifiers)


@step
def paste_first():
    paste()


@step
def assert_first_paste():
    session = state.session()
    assert len(session["attachments"]) == 1, session["items"]
    assert set(bpy.data.images.keys()) == SEEN["images"], "clipboard paste left an image datablock"
    assert host.is_host(SEEN["panel"]), "paste changed the panel editor"
    content = videoref.user_content("Inspect this image.", session["attachments"])
    wired = conversations.wire_messages(session["id"], [{"role": "user", "content": content}])
    assert any(p.get("image_url", {}).get("url", "").startswith("data:image/") for p in wired[0]["content"])
    stored = conversations.image_path(session["id"], session["attachments"][0]["full"])
    image = bpy.data.images.load(str(stored), check_existing=False)
    try:
        assert tuple(image.size) == (32, 16), tuple(image.size)
    finally:
        bpy.data.images.remove(image)
    print("ASSERT native paste: composer attachment, model image bytes, clean image datablocks", flush=True)
    type_text("first")


@step
def paste_second():
    assert state.session()["input"] == "first", "typing after first paste failed"
    copy_image((0.9, 0.3, 0.1))
    view = next(a for a in SEEN["window"].screen.areas if a.type == "VIEW_3D")
    region = host._window_region(view)
    SEEN["before_second_paste"] = scene_counts()
    SEEN["paste_xy"] = (region.x + region.width // 2, region.y + region.height // 2)
    SEEN["window"].event_simulate(type="MOUSEMOVE", value="NOTHING",
                                 x=region.x + region.width // 2, y=region.y + region.height // 2)
    paste()


@step
def type_after_second_paste():
    assert scene_counts() == SEEN["before_second_paste"], "paste over the viewport added objects, keyframes or nodes"
    print("ASSERT paste consumed over viewport: no object, keyframe or node added", flush=True)
    assert len(state.session()["attachments"]) == 2, state.session()["items"]
    assert host.is_host(SEEN["panel"])
    assert set(bpy.data.images.keys()) == SEEN["images"]
    type_text(" second")


@step
def assert_second_and_drop():
    session = state.session()
    assert session["input"] == "first second", "typing after second paste failed"
    assert all(a["name"] == "Clipboard image.png" for a in session["attachments"])
    content = videoref.user_content(session["input"], session["attachments"])
    wired = conversations.wire_messages(session["id"], [{"role": "user", "content": content}])
    assert sum(p.get("type") == "image_url" for p in wired[0]["content"]) == 2
    screenshot("second-paste-and-typing")
    print("ASSERT repeated paste: two wired images and typed input after each paste without refocusing", flush=True)
    window = SEEN["window"]
    view = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    with bpy.context.temp_override(window=window, area=view, region=host._window_region(view)):
        bpy.ops.mesh.primitive_cube_add(size=0.25, location=(3, 0, 0))
        bpy.context.object.name = "PriorEdit"
        bpy.ops.ed.undo_push(message="Prior edit")

    # A complete GLB containing one triangle, with no external resources.
    binary = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    document = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
                "nodes": [{"name": "FQDropTriangle", "mesh": 0}],
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
                "buffers": [{"byteLength": len(binary)}],
                "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(binary), "target": 34962}],
                "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                               "min": [0, 0, 0], "max": [1, 1, 0]}]}
    encoded = json.dumps(document).encode()
    encoded += b" " * (-len(encoded) % 4)
    payload = struct.pack("<II", len(encoded), 0x4E4F534A) + encoded
    payload += struct.pack("<II", len(binary), 0x004E4942) + binary
    model = WORK / "generated-triangle.glb"
    model.write_bytes(struct.pack("<4sII", b"glTF", 2, 12 + len(payload)) + payload)
    SEEN["before_drop"] = set(bpy.data.objects.keys())
    native_drop(model)


@step
def assert_model():
    process = SEEN["drop_process"]
    if process.poll() is None:
        return False
    assert process.returncode == 0, (WORK / "native-drop.log").read_text()
    assert "OLE drop effect: Copy" in (WORK / "native-drop.log").read_text()
    obj = bpy.data.objects.get("FQDropTriangle")
    if obj is None:
        return False
    assert obj is not None and obj.type == "MESH" and len(obj.data.vertices) == 3
    assert len(state.session()["attachments"]) == 2, "model import changed the image attachments"
    notices = [i["text"] for i in state.session()["items"] if i["kind"] == "notice"]
    assert any("Imported generated-triangle.glb into the scene" in text for text in notices), notices
    assert "FQDropTriangle" in tools.get_scene_info(name_contains="FQDropTriangle").text
    assert any("Imported generated-triangle.glb" in line for line in state.session()["between_turns"])
    screenshot("native-model-drop")
    area = next(a for a in SEEN["window"].screen.areas if a.type == "VIEW_3D")
    with bpy.context.temp_override(window=SEEN["window"], area=area):
        assert "FINISHED" in bpy.ops.ed.undo()
    print("ASSERT native OLE drop: handler imported triangle, user and model notices, one undo requested", flush=True)


@step
def assert_undo():
    assert set(bpy.data.objects.keys()) == SEEN["before_drop"], "undo removed a previous edit or kept the imported model"
    print("ASSERT import undo: exactly the imported model was removed", flush=True)


def tick():
    try:
        if STEPS[0]() is False:
            return 0.25
        STEPS.pop(0)
        if not STEPS:
            finish(0, "PASTE DROP CHECK OK")
    except Exception:
        traceback.print_exc()
        try:
            if "window" in SEEN:
                screenshot("failure")
        except Exception:
            traceback.print_exc()
        finish(1, "PASTE DROP CHECK FAILED")
    return 0.5


# The startup splash is a popup that takes the first click and every key; scripts run before it opens.
bpy.context.preferences.view.show_splash = False
loopcut.register()
bpy.app.timers.register(tick, first_interval=1.0)
