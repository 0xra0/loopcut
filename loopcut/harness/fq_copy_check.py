"""Check transcript selection and clipboard actions in a real GUI, using local synthetic text.

    blender --factory-startup --enable-event-simulate --python loopcut/harness/fq_copy_check.py
"""

import os
import sys
import tempfile
import threading
import traceback
from pathlib import Path

import bpy

os.environ["LOOPCUT_DATA_DIR"] = tempfile.mkdtemp(prefix="loopcut-copy-check-")
os.environ["LOOPCUT_TELEMETRY"] = "off"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()

import loopcut  # noqa: E402
from loopcut import state  # noqa: E402
from loopcut.ui import draw, host  # noqa: E402

USER = "English words keep their spaces when wrapped.\n" + "\u3042\u3044\u3046\u3048\u304a" * 14
ASSISTANT = "**A result.**\n\n```py\nx = 1\nprint(x)\n```"
SHOWN_ASSISTANT = "A result.\n\nx = 1\nprint(x)"
STATE = {}
STEPS = []


def finish(code, message):
    print(message, file=sys.stderr if code else sys.stdout, flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


watchdog = threading.Timer(90, lambda: finish(1, "COPY CHECK FAILED: timeout"))
watchdog.daemon = True
watchdog.start()


def step(fn):
    STEPS.append(fn)
    return fn


def display():
    return host._displays[STATE["panel"].as_pointer()]


def coordinates(x, y):
    region = next(r for r in STATE["panel"].regions if r.type == "WINDOW")
    return {"x": region.x + round(x), "y": region.y + round(display()["height"] - y)}


def source_point(item, offset):
    p = next(p for p in display()["prims"] if "src" in p and p["src"][0] == item
             and p["src"][1] <= offset <= p["src"][2])
    text = display()["transcript"][item]["text"]
    x = p["x"] + draw.measure(p["font"], p["size"], text[p["src"][1]:offset])
    return coordinates(x, p["y"] + p["h"] / 2)


def event(kind, value="PRESS", **values):
    if "x" in values and "y" in values:
        STATE["mouse"] = {"x": values["x"], "y": values["y"]}
    STATE["window"].event_simulate(type=kind, value=value, **{**STATE.get("mouse", {}), **values})


def key(kind, **modifiers):
    event(kind, **modifiers)
    event(kind, "RELEASE", **modifiers)


def click(id):
    hit = next(h for h in display()["hits"] if h["id"] == id)
    point = coordinates(hit["x"] + hit["w"] / 2, hit["y"] + hit["h"] / 2)
    event("MOUSEMOVE", "NOTHING", **point)
    event("LEFTMOUSE", **point)
    event("LEFTMOUSE", "RELEASE", **point)


def assert_clipboard(expected):
    actual = bpy.context.window_manager.clipboard
    assert actual == expected, (actual, expected)
    print(f"ASSERT clipboard equals {len(expected)} source characters", flush=True)


@step
def split():
    window = bpy.context.window_manager.windows[0]
    view = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in view.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=view, region=region):
        bpy.ops.screen.area_split(direction="VERTICAL", factor=0.65)
    STATE["window"] = window


@step
def open_panel():
    panel = max((a for a in STATE["window"].screen.areas if a.type == "VIEW_3D"), key=lambda a: a.x)
    with bpy.context.temp_override(window=STATE["window"], area=panel):
        bpy.ops.loopcut.open()
    STATE["panel"] = panel
    state.reset()
    tool = state.item_tool("run_python", "Make a cube", "print('synthetic payload')")
    tool.update(status="done", output="HIDDEN_PAYLOAD", guidance="HIDDEN_GUIDANCE")
    state.session().update(items=[{"kind": "user", "text": USER}, tool,
                                  {"kind": "assistant", "text": ASSISTANT}], scroll=1e9)
    panel.tag_redraw()


@step
def press_text():
    lines = [p for p in display()["prims"] if p.get("src", (-1,))[0] == 0]
    assert len(lines) > 2, "synthetic text did not wrap"
    point = source_point(0, 0)
    event("MOUSEMOVE", "NOTHING", **point)
    event("LEFTMOUSE", **point)


@step
def drag_text():
    event("MOUSEMOVE", "NOTHING", **source_point(0, len(USER)))


@step
def copy_selection():
    event("LEFTMOUSE", "RELEASE", **source_point(0, len(USER)))
    key("C", ctrl=True)


@step
def copy_user_message():
    assert_clipboard(USER)
    click("item0.copy")


@step
def show_assistant():
    assert_clipboard(USER)
    state.session()["scroll"] = 0
    STATE["panel"].tag_redraw()


@step
def copy_assistant_message():
    click("item2.copy")


@step
def open_conversation_menu():
    assert_clipboard(SHOWN_ASSISTANT)
    click(next(h["id"] for h in display()["hits"] if h["action"][0] == "copy_menu"))


@step
def copy_conversation():
    click("copy_menu.conversation")


@step
def verify_conversation():
    assert_clipboard(f"You: {USER}\n\nMake a cube\n\nLoopcut: {SHOWN_ASSISTANT}")
    print("ASSERT drag, user Copy, assistant Copy, conversation Copy, and hidden-payload exclusion", flush=True)


def run_next():
    try:
        action = STEPS.pop(0)
        action()
        print(f"COPY CHECK STEP: {action.__name__}", flush=True)
    except Exception:
        traceback.print_exc()
        finish(1, "COPY CHECK FAILED")
    if not STEPS:
        finish(0, "COPY CHECK OK")
    return 0.4


try:
    assert not bpy.app.background, "run with a visible GUI and --enable-event-simulate"
    bpy.context.preferences.view.show_splash = False
    loopcut.register()
    bpy.app.timers.register(run_next, first_interval=0.6)
except Exception:
    traceback.print_exc()
    finish(1, "COPY CHECK FAILED: setup")
