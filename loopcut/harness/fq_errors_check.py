"""Check error recovery in an isolated Blender desktop with synthetic transports.

    python loopcut/harness/fq_errors_check.py --blender /path/to/blender --seconds 180

A desktop is required. The parent enforces a timeout and requires the child's pass marker.
Screenshots and layout records remain in --output (a printed temporary directory by default).
The worker, main-thread pump, reset timer, plan watcher and tool dispatch are real. Only
configuration, authentication and remote replies are fixtures; no model service is contacted.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_checkout():
    import checkout
    checkout.use()
    # Disabling the installed add-on can lazily import children after checkout.use's
    # initial inventory. Remove those too before importing this checkout's modules.
    for name in list(sys.modules):
        if name.startswith("loopcut."):
            sys.modules.pop(name, None)
    import loopcut
    from loopcut import version_guard
    version_guard.register("loopcut", Path(loopcut.__file__).parent, loopcut.bl_info["version"])


def child():
    import contextlib
    import io
    import json
    import threading
    import time
    import traceback
    import types
    import urllib.error
    import urllib.request
    from datetime import datetime, timezone
    from unittest import mock

    import bpy
    load_checkout()
    from loopcut import (account, agent, chatgpt_auth, chatgpt_responses, cloud, config,
                         context, conversations, credentials, llm, mainthread, settings, state)
    from loopcut.ui import host

    assert not bpy.app.background, "a Blender desktop is required for timers and screenshots"
    output = Path(os.environ["LOOPCUT_FQ_OUTPUT"])
    output.mkdir(parents=True, exist_ok=True)
    stack = contextlib.ExitStack()
    # Stand-in for the add-on preferences: every declared property at its install default (settings.values()
    # reads most of them on each tool call), then the provider and model this check drives.
    defaults = {name: prop.keywords["default"]
                for name, prop in settings.LoopcutPreferences.__annotations__.items()
                if isinstance(getattr(prop, "keywords", None), dict) and "default" in prop.keywords}
    assert "chatgpt_effort" in defaults and "run_timeout" in defaults, "preference defaults were not readable"
    prefs = types.SimpleNamespace(**{**defaults, "provider": "CHATGPT", "model": "synthetic", "tier": "fast"})
    replies, hosted, posts, stray_network = [], [], [], []
    upgraded = threading.Event()
    balance = [20]  # The synthetic service's one credit balance: /v1/me and refusals report the same number.
    spec = agent.LoopSpec(role="single", system=lambda s: "", schemas=lambda: [
        {"type": "function", "function": {"name": "run_python", "parameters": {"type": "object"}}}],
        save=conversations.save, asks=False, checkpoints=False, changes_card=False)

    def cfg():
        base = "chatgpt-plan://" if prefs.provider == "CHATGPT" else config.SERVICE_BASE_URL
        return config.Config("synthetic", base, "synthetic", "low", False, False, 0, 24000)

    def configure(turn, session):
        turn.config, turn.spec = cfg(), spec
        turn.stall_seconds = 0

    def post(*args):
        posts.append(time.monotonic())
        assert threading.current_thread() is not threading.main_thread(), "HTTP ran on Blender's UI thread"
        assert replies, "unexpected ChatGPT request"
        status, payload = replies.pop(0)
        if status != 200:
            raise urllib.error.HTTPError("https://example.invalid", status, "fixture refusal", {},
                                         io.BytesIO(json.dumps({"error": payload}).encode()))
        response = io.BytesIO(payload)
        response.headers = {}
        return response

    real_stream = llm.stream_chat

    def stream(**kwargs):
        if kwargs["base_url"] == "chatgpt-plan://":
            return real_stream(**kwargs)
        assert kwargs["base_url"] == config.SERVICE_BASE_URL and hosted, "unexpected hosted request"
        assert threading.current_thread() is not threading.main_thread(), "hosted call ran on the UI thread"
        value = hosted.pop(0)
        if isinstance(value, Exception):
            if "credits_balance" in (getattr(value, "account", None) or {}):
                balance[0] = value.account["credits_balance"]
            raise value
        kwargs["on_text"](value.text)
        return value

    def get(path, key):
        if path == "/v1/me":
            if upgraded.is_set():
                balance[0] = 20  # The upgrade grants the plan's credits.
            return {"plan": "pro" if upgraded.is_set() else "free", "billing": "credits", "credits_balance": balance[0]}
        assert path == "/v1/models", path
        return {"data": [{"id": "fast", "default": True, "available": True}], "default": "fast"}

    def no_network(*args, **kwargs):
        stray_network.append(True)
        raise AssertionError("unexpected network access")

    for obj, name, value in (
        (settings, "preferences", lambda: prefs), (settings, "set_tier", lambda tier: setattr(prefs, "tier", tier)),
        (config, "load", cfg), (config, "billing_open", lambda: True),
        (agent, "_configure", configure), (credentials, "api_key", lambda *a: "synthetic"),
        (chatgpt_auth, "access_token", lambda: "synthetic"), (chatgpt_auth, "plan_signed_in", lambda: True),
        (chatgpt_responses, "_post", post), (llm, "stream_chat", stream),
        (account, "_get", get), (account, "WATCH_EVERY", .1), (account, "open_browser", lambda url: None),
        (cloud, "_runtime", None), (cloud, "panel_seen", lambda: None),
        (urllib.request, "urlopen", no_network)):
        stack.enter_context(mock.patch.object(obj, name, value))

    mainthread.register()
    host.register()
    window = bpy.context.window_manager.windows[0]
    host.dock(window)
    deadline = time.monotonic() + float(os.environ.get("LOOPCUT_FQ_SECONDS", "180"))
    heartbeat = [0]
    tick_times = []

    def check(ok, message):
        assert ok, message
        print("FQ ASSERT " + message, flush=True)

    def wait_for(predicate, seconds=10):
        until = time.monotonic() + seconds
        while not predicate():
            assert time.monotonic() < until, "condition timed out"
            yield .05

    def pause(seconds):
        until = time.monotonic() + seconds
        yield from wait_for(lambda: time.monotonic() >= until, seconds + 2)

    def frame():
        area = host.find_panel(window)
        assert area is not None
        display = host._displays.get(area.as_pointer())
        assert display, "the real panel did not draw"
        return display

    def screenshot(name):
        with bpy.context.temp_override(window=window):
            bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=2)
            bpy.ops.screen.screenshot(filepath=str(output / (name + ".png")))
        (output / (name + ".json")).write_text(json.dumps(frame(), default=str), encoding="utf-8")

    def click(kind, match=None):
        hits = [hit for hit in frame()["hits"] if hit["action"][0] == kind
                and (match is None or match(hit["action"][1]))]
        assert hits, f"visible {kind} action missing"
        previous = state.session().get("turn")
        host._do_action(state.session(), hits[-1]["action"])
        if kind == "resume":
            chat = state.session()
            assert chat.get("turn") is not previous or chat["turn_outcome"] == "completed", (
                "Try again did not launch a new turn: " + repr(chat["items"][-3:]))

    def start():
        assert not replies and not hosted
        chat = state.reset()
        chat["items"].append(state.item_user("Add a cube"))
        chat["messages"].append({"role": "user", "content": "Add a cube"})
        return chat

    def launch(chat):
        turn = agent.Turn()
        configure(turn, chat)
        agent._launch(chat, turn)
        check(turn.thread is not threading.current_thread(), "agent launches a real worker thread")
        return turn

    def completion(name=None):
        if name:
            code = f"import bpy\nbpy.ops.mesh.primitive_cube_add()\nbpy.context.object.name = {name!r}"
            return llm.Completion(tool_calls=[llm.ToolCall("cube_call", "run_python",
                json.dumps({"code": code, "summary": "Add a cube"}))])
        return llm.Completion(text="Done.")

    def responses(name=None):
        value = completion(name)
        items = [{"type": "function_call", "call_id": call.id, "name": call.name,
                  "arguments": call.arguments, "status": "completed"} for call in value.tool_calls]
        if value.text:
            items.append({"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": value.text}]})
        event = {"type": "response.completed", "response": {"status": "completed", "output": items}}
        return 200, ("data: " + json.dumps(event) + "\n\n").encode()

    def assert_mesh(chat, name):
        check(chat["turn_outcome"] == "completed", "recovery completed: " + repr(chat["items"][-3:]))
        check(bpy.data.objects.get(name) is not None, name + " created by real tool dispatch")
        check(chat["turn_outcome"] == "completed" and not replies and not hosted, "recovery completes once")
        check(sum(context.is_turn_start(m) for m in chat["messages"]) == 1, "original request stays one turn")
        check(len([i for i in chat["items"] if i.get("kind") == "tool" and i.get("name") == "run_python"]) == 1,
              "exactly one scene tool ran")

    def steps():
        yield from wait_for(lambda: host.find_panel(window) is not None)
        yield .3
        chat = start()
        prefs.provider = "CHATGPT"
        replies[:] = [(503, {"code": "server_busy", "message": "Temporarily busy"}) for _ in range(6)]
        before, beats = len(posts), heartbeat[0]
        turn = launch(chat)
        yield from wait_for(lambda: not chat["busy"], 100)
        check(turn.outcome == "error" and state.can_resume(chat), "six HTTP 503s produce a retryable stopped turn")
        check(len(posts) - before == 6, "all six HTTP failures traversed the transport")
        intervals = [b - a for a, b in zip(posts[before:], posts[before + 1:])]
        check(all(gap >= delay - .2 for gap, delay in zip(intervals, llm.CHATGPT_RETRY_DELAYS)),
              "real backoff waits follow the ChatGPT schedule")
        check(heartbeat[0] - beats > 50, "Blender timers continue throughout backoff")
        print(f"FQ TIMING backoff: max harness tick {max(tick_times, default=0):.4f}s; "
              f"late pumps {sum('pump ran late' in what for _, what, _ in mainthread.slow)}", flush=True)
        yield .2
        screenshot("503-card")
        replies[:] = [responses("RecoveryCube_retry"), responses()]
        click("resume")
        yield from wait_for(lambda: not chat["busy"])
        assert_mesh(chat, "RecoveryCube_retry")
        check(any("continue from there" in str(m.get("content", "")) for m in chat["messages"]),
              "Try again carries the failed-step instruction")

        chat = start()
        before = len(posts)
        replies[:] = [(503, {"code": "server_busy"})]
        turn = launch(chat)
        yield from wait_for(lambda: len(posts) > before)
        yield from pause(.2)
        # The composer's Stop is the ("stop_turn", None) hit drawn while a turn is busy.
        yield from wait_for(lambda: any(h["action"][0] == "stop_turn" for h in frame()["hits"]), 2)
        stopped_at = time.monotonic()
        click("stop_turn")
        yield from wait_for(lambda: not chat["busy"], 2)
        check(turn.outcome == "cancelled" and len(posts) == before + 1 and time.monotonic() - stopped_at < 1,
              "Stop interrupts a real backoff without another request")

        chat = start()
        replies[:] = [(403, {"code": "unsupported_country_region_territory"})]
        launch(chat)
        yield from wait_for(lambda: not chat["busy"])
        yield .2
        screenshot("region-card")
        hosted[:] = [completion("RecoveryCube_region"), completion()]
        click("offer", lambda pair: chat["items"][pair[0]]["actions"][pair[1]]["do"] == "continue_loopcut")
        yield from wait_for(lambda: not chat["busy"])
        assert_mesh(chat, "RecoveryCube_region")
        check(prefs.provider == "LOOPCUT", "region fallback changes provider only after the visible action")

        for recovery in ("upgrade", "reset"):
            chat = start()
            prefs.provider = "CHATGPT"
            upgraded.clear()
            reset = datetime.fromtimestamp(time.time() + 30, timezone.utc).isoformat()
            replies[:] = [(429, {"code": "subscription_sharing_usage_limit_exceeded", "resets_at": reset})]
            launch(chat)
            yield from wait_for(lambda: not chat["busy"])
            yield .2
            hosted[:] = [llm.LLMError("Credits exhausted", 402, "insufficient_credits", {
                "plan": "free", "upgrade": {"url": "https://example.invalid/plans", "plan": "pro", "label": "Get Pro"}},
                account={"plan": "free", "billing": "credits", "credits_balance": 0})]
            click("offer", lambda pair: chat["items"][pair[0]]["actions"][pair[1]]["do"] == "continue_loopcut")
            yield from wait_for(lambda: not chat["busy"])
            yield from wait_for(lambda: (state.ui.get("account") or {}).get("credits_balance") == 0)
            cards = [i for i in chat["items"] if i["kind"] in ("offer", "limit")]
            check(len(cards) == 1 and bool(cards[0].get("chatgpt_limit")), "real main-thread refusal leaves one combined card")
            card = cards[0]
            original = chat["messages"][0]
            yield .2
            screenshot("combined-" + recovery)
            drawn = " ".join(p.get("text", "") for p in frame()["prims"])
            check(state.reset_label(reset) in drawn and "saved" in drawn, "panel draws reset time and saved request")
            check({"limit_upgrade", "limit_chatgpt"} <= {h["action"][0] for h in frame()["hits"]},
                  "both recovery actions are visible")
            restored = conversations.load(chat["id"])
            check(any(i.get("chatgpt_limit") for i in restored["items"]), "saved conversation retains limit context")
            before = len(posts)
            yield from pause(.4)
            check(len(posts) == before and prefs.provider == "LOOPCUT", "no reset retry or provider change without a click")
            name = "RecoveryCube_" + recovery
            if recovery == "upgrade":
                hosted[:] = [completion(name), completion()]
                click("limit_upgrade")
                check(card["status"] == "waiting", "upgrade starts the real plan watcher")
                upgraded.set()
            else:
                replies[:] = [responses(name), responses()]
                click("limit_chatgpt")
                check(not chat["busy"] and prefs.provider == "LOOPCUT", "early ChatGPT click arms the real reset timer")
                yield .2
                screenshot("reset-armed")
                check(any("Cancel ChatGPT retry" in p.get("text", "") for p in frame()["prims"]),
                      "scheduled retry has a visible cancel action")
            yield from wait_for(lambda: card.get("status") == "done" and not chat["busy"], 40)
            assert_mesh(chat, name)
            check(chat["messages"][0] is original, "recovery retains the original request object")
            yield from pause(.3)
            assert_mesh(chat, name)
            yield .2
            screenshot("completed-" + recovery)
        check(not stray_network, "all remote boundaries stayed inside synthetic fixtures")

    sequence = steps()

    def finish(code):
        account.stop_watching()
        mainthread.unregister()
        host.unregister()
        stack.close()
        if code:
            os._exit(code)
        bpy.ops.wm.quit_blender()

    def tick():
        heartbeat[0] += 1
        started = time.monotonic()
        try:
            assert time.monotonic() < deadline, "child deadline exceeded"
            return next(sequence)
        except StopIteration:
            print("FQ PASS: real workers, transport backoff, timers, UI recovery and scene mutations", flush=True)
            finish(0)
        except BaseException:
            traceback.print_exc()
            print("FQ FAIL: recovery check failed", flush=True)
            sys.stdout.flush()
            sys.stderr.flush()
            finish(1)
        finally:
            elapsed = time.monotonic() - started
            tick_times.append(elapsed)
            mainthread.note_slow("the recovery harness tick", elapsed)
        return None

    bpy.app.timers.register(tick, first_interval=.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", default=os.environ.get("LOOPCUT_BLENDER"))
    parser.add_argument("--seconds", type=float, default=180)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.blender:
        parser.error("set LOOPCUT_BLENDER or pass --blender")
    if not 0 < args.seconds < float("inf"):
        parser.error("--seconds must be finite and positive")
    output = args.output or Path(tempfile.mkdtemp(prefix="loopcut-recovery-check-"))
    output.mkdir(parents=True, exist_ok=True)
    print("Recovery artifacts: " + str(output), flush=True)
    with tempfile.TemporaryDirectory(prefix="loopcut-recovery-profile-") as folder:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BLENDER_USER_", "LOOPCUT_"))}
        env.update(BLENDER_USER_RESOURCES=folder, LOOPCUT_DATA_DIR=str(Path(folder) / "data"),
                   LOOPCUT_CONFIG_DIR=str(Path(folder) / "config"), LOOPCUT_TELEMETRY="off", LOOPCUT_DEV="0",
                   LOOPCUT_FQ_SECONDS=str(args.seconds - 2), LOOPCUT_FQ_OUTPUT=str(output.resolve()), PYTHONUNBUFFERED="1")
        command = [args.blender, "--factory-startup", "--python-exit-code", "1", "--python", str(Path(__file__).resolve()),
                   "--", "--child"]
        log = output / "blender.log"
        try:
            with log.open("w", encoding="utf-8") as handle:
                result = subprocess.run(command, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                        timeout=args.seconds, check=False)
        except subprocess.TimeoutExpired:
            print(f"FQ FAIL: Blender exceeded {args.seconds:g} seconds; see {log}", flush=True)
            return 1
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        if line.startswith("FQ "):
            print(line)
    if result.returncode or not any(line.startswith("FQ PASS:") for line in lines):
        print("FQ FAIL: missing pass marker or Blender failed; see " + str(log))
        return 1
    return 0


if __name__ == "__main__":
    if "--child" in sys.argv:
        child()
    else:
        raise SystemExit(main())
