"""Reject, refuse scratch targets, save before opening, and type after a live file switch.

    Blender --factory-startup --enable-event-simulate --python loopcut/harness/fq_open_file_check.py

Run on an unlocked Windows desktop. Scenes and conversations are generated and isolated.
The scripted model uses the real agent loop, consent buttons, file operators and load handlers.
"""

import atexit
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
WORK = Path(tempfile.mkdtemp(prefix="loopcut-open-check-")).resolve()
atexit.register(shutil.rmtree, WORK, ignore_errors=True)
PROJECT, SCRATCH = WORK / "project", WORK / "scratch"
PROJECT.mkdir()
SCRATCH.mkdir()
os.environ["LOOPCUT_DATA_DIR"] = str(WORK / "data")
os.environ["LOOPCUT_ROUTER"] = "off"

import loopcut  # noqa: E402
from loopcut import agent, config, conversations, files, llm, open_file, state, workspace  # noqa: E402
from loopcut.ui import host, textedit  # noqa: E402

CURRENT, TARGET = PROJECT / "generated-current.blend", PROJECT / "generated-target.blend"
SHOTS = Path(os.environ.get("LOOPCUT_SHOTS") or Path.cwd() / "fq-open-evidence").resolve()
SHOTS.mkdir(parents=True, exist_ok=True)
# Only these generated project fixtures bypass the temp-root rule; every refusal target uses it.
_check_target = open_file._check


def check_fixture(target):
    if target in (CURRENT, TARGET):
        with patch.object(files, "_temp_roots", return_value=(SCRATCH,)):
            return _check_target(target)
    return _check_target(target)


open_file._check = check_fixture
SEEN = {"phase": 0, "requests": 0, "case": "untitled"}


def finish(code, message):
    watchdog.cancel()
    try:
        shutil.rmtree(WORK)
        assert not WORK.exists()
        print("ASSERT cleanup: generated scene and data folders removed from system temp", flush=True)
    except Exception:
        traceback.print_exc()
        code = 1
    print(f"{message}; screenshots: {SHOTS}; exit: {code}", flush=True)
    sys.stderr.flush()
    os._exit(code)


watchdog = threading.Timer(90, lambda: finish(1, "OPEN FILE CHECK TIMEOUT"))
watchdog.daemon = True
watchdog.start()


def panel():
    window = bpy.context.window_manager.windows[0]
    return window, next(a for a in window.screen.areas if host.is_host(a))


def screenshot(name):
    window, area = panel()
    with bpy.context.temp_override(window=window, area=area):
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=2)
        bpy.ops.screen.screenshot(filepath=str(SHOTS / f"{name}.png"))


def click(hit_id):
    window, area = panel()
    display = host._displays.get(area.as_pointer()) or {}
    hit = next((h for h in display.get("hits", []) if h["id"] == hit_id), None)
    if hit is None:
        return False
    region = next(r for r in area.regions if r.type == "WINDOW")
    x = region.x + int(hit["x"] + hit["w"] / 2)
    y = region.y + int(display["height"] - hit["y"] - hit["h"] / 2)
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x, y=y)
    return True


def type_text(text):
    window, _ = panel()
    for ch in text:
        kind = "SPACE" if ch == " " else ch.upper()
        window.event_simulate(type=kind, value="PRESS", unicode=ch)
        window.event_simulate(type=kind, value="RELEASE")


def scripted_request(session, turn, cfg, spec):
    SEEN["requests"] += 1
    if SEEN["requests"] == 1:
        calls = [llm.ToolCall("fixture-open", "open_file", json.dumps({"path": str(SEEN["path"])}))]
        if SEEN["case"] == "approve":
            calls.append(llm.ToolCall("fixture-queued", "run_python", json.dumps({"code": "raise AssertionError('queued call ran')"})))
        return llm.Completion(tool_calls=calls)
    assert SEEN["case"] != "approve", "the old turn continued after switching files"
    assert SEEN["requests"] == 2
    return llm.Completion(text="Stopped.")


def start_case():
    session = state.session()
    assert session["input"] == "open generated file", "composer did not accept typed request"
    text = session["input"]
    textedit.set_text(session, "")
    session["items"].append(state.item_user(text))
    session["messages"].append({"role": "user", "content": text})
    session["auto_run"] = True
    turn = agent.Turn()
    turn.config = config.Config("", "", "fixture", "", True, False, 3, 40000)
    turn.spec = dataclasses.replace(agent.SINGLE_SPEC, checkpoints=False, changes_card=False,
                                    reference_card=False, save=conversations.save)
    session.update(busy=True, turn=turn)
    SEEN.update(session=session, turn=turn, requests=0)
    turn.thread = threading.Thread(target=agent.run_loop, args=(session, turn, turn.spec), daemon=True)
    turn.thread.start()


def add_cube(name):
    window, _ = panel()
    view = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    with bpy.context.temp_override(window=window, area=view, region=host._window_region(view)):
        assert "FINISHED" in bpy.ops.mesh.primitive_cube_add(size=0.25, location=(3, 0, 0))
        bpy.context.object.name = name
        assert "FINISHED" in bpy.ops.ed.undo_push(message="Generated scene edit")
    assert bpy.data.is_dirty, "explicit undo push did not mark the fixture dirty"


def setup():
    assert not bpy.data.filepath, "use --factory-startup for the untitled scenario"
    assert files.inside(WORK, files._temp_roots()), "fixtures must live in system temp"
    cube = bpy.data.objects.get("Cube")
    assert cube is not None
    cube.name = "TargetObject"
    assert "FINISHED" in bpy.ops.wm.save_as_mainfile(filepath=str(TARGET), copy=True)
    assert not bpy.data.filepath
    cube.name = "CurrentObject"
    target_chat = state.new_session()
    conversations.add_project(target_chat, str(TARGET))
    target_chat["items"].append(state.item_user("Inspect the target scene."))
    target_chat["messages"].append({"role": "user", "content": "Inspect the target scene."})
    conversations.save(target_chat)
    SEEN["target_chat"] = target_chat["id"]
    window = bpy.context.window_manager.windows[0]
    area = max((a for a in window.screen.areas if a.type != "VIEW_3D"), key=lambda a: a.width * a.height)
    with bpy.context.temp_override(window=window, area=area):
        bpy.ops.loopcut.open()
    add_cube("UnsavedChange")
    state.reset()
    SEEN["path"] = TARGET
    agent._request = scripted_request


def saved_objects(path):
    """Object names in the file on disk. Blender will not library-load the open file, so read a copy."""
    probe = WORK / f"probe-{SEEN.setdefault('probes', 0)}.blend"
    SEEN["probes"] += 1
    shutil.copyfile(path, probe)
    with bpy.data.libraries.load(str(probe)) as (source, _):
        return list(source.objects)


def answer_when_drawn():
    if SEEN["case"] in {"untitled", "workspace", "temp", "images"}:
        assert not any(i.get("status") == "awaiting" for i in SEEN["session"]["items"]), "invalid target asked for consent"
        if SEEN["turn"].thread.is_alive():
            return False
        print(f"ASSERT {SEEN['case']}: refused before consent", flush=True)
        return True
    waiting = next(((n, item) for n, item in enumerate(SEEN["session"]["items"])
                    if item.get("status") == "awaiting"), None)
    if waiting is None:
        return False
    n, card = waiting
    assert card["name"] == "open_file" and card["always_ask"]
    assert str(files.resolve(str(SEEN["path"]))) in card["code"], card
    assert bpy.data.is_dirty, "saved before consent"
    expected = "" if SEEN["case"] == "untitled" else str(CURRENT)
    assert bpy.data.filepath == expected, "opened before consent"
    if SEEN["case"] == "approve":
        assert "UnsavedAfterSave" not in saved_objects(CURRENT), "fixture was already saved"
    _, area = panel()
    display = host._displays.get(area.as_pointer()) or {}
    assert not any(h["action"][0] == "approve_always" for h in display.get("hits", []))
    button = "reject" if SEEN["case"] == "reject" else "run"
    if not click(f"item{n}.tool.{button}"):
        return False
    print(f"ASSERT {SEEN['case']}: explicit consent, resolved path, no save/open before click", flush=True)
    return True


def assert_case_finished():
    turn, session = SEEN["turn"], SEEN["session"]
    if turn.thread.is_alive():
        return False
    case = SEEN["case"]
    if case == "approve":
        assert Path(bpy.data.filepath).resolve() == TARGET
        assert "TargetObject" in bpy.data.objects and "UnsavedChange" not in bpy.data.objects
        assert "UnsavedAfterSave" in saved_objects(CURRENT), "current changes were not saved"
        assert state.session()["id"] == SEEN["target_chat"], "conversation did not switch"
        assert turn.cancel.is_set() and SEEN["requests"] == 1, "old turn or queued calls continued"
        assert not any(i.get("name") == "run_python" and i.get("status") == "done" for i in session["items"])
        screenshot("target-loaded")
        print("ASSERT save and switch: changes saved, target chat resumed, queued calls stopped", flush=True)
        return True
    assert bpy.data.is_dirty, "a rejected or invalid target saved current changes"
    expected = "" if case == "untitled" else str(CURRENT)
    assert bpy.data.filepath == expected, "a rejected or invalid target changed the live path"
    card = next(i for i in reversed(session["items"]) if i.get("name") == "open_file")
    if case == "reject":
        assert card["status"] == "rejected"
    else:
        assert card["status"] == "failed", card
        expected = "Save As" if case == "untitled" else "saving or packing" if case == "images" else "project folder"
        assert expected in card["output"], card
        if case == "images":
            assert "GeneratedPaint" in card["output"], card
    screenshot(f"{case}-preserved")
    print(f"ASSERT {case}: live path and unsaved changes preserved", flush=True)
    return True


def edit_after_save():
    if bpy.data.is_dirty:
        return False  # Blender has not handled the save's notifier yet.
    assert bpy.data.filepath and Path(bpy.data.filepath).resolve() == CURRENT
    add_cube("UnsavedAfterSave")
    ws = workspace.folder(state.session()["id"]).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    workspace._ROOTS.add(ws)
    assert "FINISHED" in bpy.ops.wm.save_as_mainfile(filepath=str(ws / "generated-workspace.blend"), copy=True)
    # A copy must preserve the live file's unsaved changes.
    assert bpy.data.is_dirty
    state.session()["mode"] = "workspace"
    return True


def next_case():
    case = SEEN["case"]
    if case == "untitled":
        assert "FINISHED" in bpy.ops.wm.save_as_mainfile(filepath=str(CURRENT))
        # Blender marks the file saved from an ND_FILESAVE notifier it handles after this tick, so an edit
        # made in this same tick would count as saved. The edit and the workspace copy wait for a later tick.
        SEEN.update(case="workspace", path="generated-workspace.blend", pending=edit_after_save)
    elif case == "workspace":
        state.session().pop("mode", None)
        temporary = SCRATCH / "generated-temp.blend"
        assert "FINISHED" in bpy.ops.wm.save_as_mainfile(filepath=str(temporary), copy=True)
        assert bpy.data.is_dirty
        SEEN.update(case="temp", path=temporary)
    elif case == "temp":
        image = bpy.data.images.new("GeneratedPaint", width=4, height=4)
        image.pixels[:] = [0.2, 0.3, 0.4, 1.0] * 16
        assert image.is_dirty
        SEEN.update(case="images", path=TARGET, dirty_image=image)
    elif case == "images":
        bpy.data.images.remove(SEEN.pop("dirty_image"))
        SEEN.update(case="reject", path=TARGET)
    elif case == "reject":
        SEEN.update(case="approve", path=TARGET)
    else:
        return False
    return True


def tick():
    try:
        phase = SEEN["phase"]
        if SEEN.get("pending"):
            if SEEN["pending"]():
                SEEN.pop("pending")
        elif phase == 0:
            setup()
            SEEN["phase"] = 1
        elif phase == 1 and click("input.card"):
            SEEN["phase"] = 2
        elif phase == 2:
            type_text("open generated file")
            SEEN["phase"] = 3
        elif phase == 3:
            start_case()
            SEEN["phase"] = 4
        elif phase == 4 and answer_when_drawn():
            SEEN["phase"] = 5
        elif phase == 5 and assert_case_finished():
            SEEN["phase"] = 1 if next_case() else 6
        elif phase == 6 and click("input.card"):
            SEEN["phase"] = 7
        elif phase == 7:
            type_text("continue on this file")
            SEEN["phase"] = 8
        elif phase == 8:
            assert state.session()["input"] == "continue on this file", "panel rejected typed input after switching"
            conversations.save(state.session())
            assert conversations.load(state.session()["id"])["input"] == "continue on this file"
            screenshot("typed-after-switch")
            print("ASSERT keyboard: target chat accepts and persists text typed through UI events", flush=True)
            finish(0, "OPEN FILE CHECK OK")
    except Exception:
        traceback.print_exc()
        try:
            screenshot("failure")
        except Exception:
            traceback.print_exc()
        finish(1, "OPEN FILE CHECK FAILED")
    return 0.25


# The startup splash is a popup that takes the first click and every key; scripts run before it opens.
bpy.context.preferences.view.show_splash = False
loopcut.register()
bpy.app.timers.register(tick, first_interval=1.0, persistent=True)
