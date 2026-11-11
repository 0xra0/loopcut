"""One local eval task as a real agent turn, the way live_check.py runs one (a GUI Blender, the panel open):
    blender --factory-startup --python run_task.py -- TASK RUN_DIR
The model is whatever LOOPCUT_BASE_URL/LOOPCUT_MODEL name (run.py sets Strata). Writes, in RUN_DIR:
TASK.result.json, TASK.md (transcript) and TASK.conversation/conversation.json (the items distill.py reads)."""
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

import bpy

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import checkout  # noqa: E402

checkout.use()
os.environ.setdefault("LOOPCUT_DATA_DIR", tempfile.mkdtemp(prefix="loopcut-local-eval-"))
import loopcut  # noqa: E402
from loopcut import agent, state  # noqa: E402

TASK, RUN = sys.argv[sys.argv.index("--") + 1], Path(sys.argv[sys.argv.index("--") + 2])
os.environ["LOCAL_EVAL_OUT"] = str(RUN / f"{TASK}.out")
import tasks  # noqa: E402  (after LOCAL_EVAL_OUT: tasks reads it at import)

TIMEOUT = float(os.environ.get("LOCAL_EVAL_TIMEOUT", "1200"))
STATE: dict = {}


def tool_calls(session):
    """(name, arguments) of every tool call the model made, in order."""
    calls = []
    for message in session["messages"]:
        for call in message.get("tool_calls") or []:
            try:
                arguments = json.loads(call["function"]["arguments"] or "{}")
            except ValueError:
                arguments = {"raw": call["function"]["arguments"]}
            calls.append((call["function"]["name"], arguments))
    return calls


def write_outputs(session, passed, detail, elapsed, error=""):
    RUN.mkdir(parents=True, exist_ok=True)
    calls = tool_calls(session)
    items, pending = [], list(calls)
    for item in session["items"]:
        row = {k: item.get(k) for k in ("kind", "text", "name", "status", "summary", "output") if item.get(k) is not None}
        if item["kind"] == "tool" and pending:
            _, arguments = pending.pop(0)
            if isinstance(arguments, dict) and "code" in arguments:
                row["code"] = arguments["code"]
        items.append(row)
    folder = RUN / f"{TASK}.conversation"
    folder.mkdir(exist_ok=True)
    (folder / "conversation.json").write_text(json.dumps({"items": items}, indent=1), encoding="utf-8")
    lines = []
    for row in items:
        if row["kind"] == "tool":
            lines.append(f"## tool {row.get('name')} [{row.get('status')}]\n{row.get('summary', '')}\n")
            if row.get("code"):
                lines.append(f"```python\n{row['code']}\n```\n")
            if row.get("status") == "failed":
                lines.append(f"```\n{str(row.get('output', ''))[-1500:]}\n```\n")
        else:
            lines.append(f"## {row['kind']}\n{row.get('text', '')}\n")
    (RUN / f"{TASK}.md").write_text("\n".join(lines), encoding="utf-8")
    tools = [i for i in items if i["kind"] == "tool"]
    result = {"id": TASK, "prompt": tasks.prompt(TASK), "passed": bool(passed), "detail": detail, "error": error,
              "seconds": round(elapsed), "steps": len(tools),
              "failed_calls": sum(1 for t in tools if t.get("status") == "failed"),
              "tools": [t.get("name") for t in tools],
              "final": next((i.get("text", "") for i in reversed(items) if i["kind"] == "assistant"), "")}
    (RUN / f"{TASK}.result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"LOCAL_EVAL {TASK} {'PASS' if passed else 'FAIL'} {detail}")


def finish(code):
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def split():
    window = bpy.context.window_manager.windows[0]
    view = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in view.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=view, region=region):
        bpy.ops.screen.area_split(direction="VERTICAL", factor=0.7)
    STATE["window"] = window
    bpy.app.timers.register(start, first_interval=0.3)


def start():
    try:
        tasks.setup(TASK)
        window = STATE["window"]
        panel = max((a for a in window.screen.areas if a.type == "VIEW_3D"), key=lambda a: a.x)
        with bpy.context.temp_override(window=window, area=panel):
            bpy.ops.loopcut.open()
        state.reset()
        STATE["t0"] = time.monotonic()
        assert agent.send(tasks.prompt(TASK)), "agent.send refused the prompt"
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        write_outputs(state.session(), False, "could not start the turn", 0, traceback.format_exc())
        finish(2)
    bpy.app.timers.register(poll, first_interval=1.0)


def poll():
    session = state.session()
    elapsed = time.monotonic() - STATE["t0"]
    if session["busy"] and elapsed < TIMEOUT:
        return 1.0
    try:
        if session["busy"]:
            agent.stop()
            passed, detail = False, f"timed out after {TIMEOUT:.0f} s"
        else:
            passed, detail = tasks.check(TASK)
        errors = [i.get("text", "") for i in session["items"] if i["kind"] == "error"]
        write_outputs(session, passed, detail, elapsed, "\n".join(errors))
        finish(0)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        finish(1)


bpy.context.preferences.view.show_splash = False
loopcut.register()
bpy.app.timers.register(split, first_interval=0.5)
