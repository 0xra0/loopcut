"""Exercise historical re-enable and file replacement in a running GUI Blender.

    python3 loopcut/harness/fq_update_mixed_check.py --blender <executable> --out <new folder>

The normal runner modifies isolated source copies and profiles. Its MSI witness mode observes
an installed instance for msi_upgrade_test.ps1. Neither mode sends model or paid requests.
The runner has a process timeout; each GUI phase also has its own deadline.
"""

import argparse
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def prepare_cases(repo, out):
    source = repo / "scripts/addons_core/loopcut"
    cases = [
        {"name": "legacy-reenable", "ref": "5cc0a53a36a", "reenable": True},
        {"name": "guarded-reenable", "ref": "abba2f12d76", "reenable": True},
        {"name": "running-no-reenable", "ref": None, "reenable": False},
    ]
    for case in cases:
        folder = out / case["name"]
        live = folder / "addons/loopcut"
        if case["ref"]:
            archive = subprocess.run(["git", "archive", case["ref"], "scripts/addons_core/loopcut"],
                                     cwd=repo, capture_output=True, check=True, timeout=30).stdout
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                for member in tar.getmembers():
                    if member.isfile():
                        relative = Path(member.name).relative_to("scripts/addons_core/loopcut")
                        if ".." in relative.parts:
                            raise ValueError("archive path escapes the add-on")
                        target = live / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(tar.extractfile(member).read())
        else:
            shutil.copytree(source, live, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        replacement = folder / "replacement"
        shutil.copytree(source, replacement, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if not case["reenable"]:
            init = replacement / "__init__.py"
            init.write_text(stamped(init.read_text(encoding="utf-8"), (8, 0, 1)), encoding="utf-8")
        (folder / "case.json").write_text(json.dumps(case), encoding="utf-8")
    return cases


def isolated_environment(out):
    (out / "empty.env").touch()
    profile = out / "profile"
    for name in ("blender", "config", "data", "tmp"):
        (profile / name).mkdir(parents=True, exist_ok=True)
    return {**os.environ, "BLENDER_USER_RESOURCES": str(profile / "blender"),
            "LOOPCUT_CONFIG_DIR": str(profile / "config"), "LOOPCUT_DATA_DIR": str(profile / "data"),
            "LOOPCUT_ENV_FILE": str(out / "empty.env"), "LOOPCUT_UPDATE_URL": "off",
            "LOOPCUT_TELEMETRY": "off", "LOOPCUT_DEV": "0",
            "LOOPCUT_SERVICE_URL": "http://127.0.0.1:1", "TMPDIR": str(profile / "tmp"),
            "TEMP": str(profile / "tmp"), "TMP": str(profile / "tmp")}


def stamped(source, version):
    result, count = re.subn(r'("version":\s*)\(\d+,\s*\d+,\s*\d+\)',
                            lambda m: m[1] + repr(version), source, count=1)
    if count != 1:
        raise AssertionError("initializer version was not found")
    return result


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", type=Path, default=os.environ.get("LOOPCUT_BLENDER"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if args.blender is None:
        parser.error("--blender or LOOPCUT_BLENDER is required")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[2]
    cases = prepare_cases(repo, out)
    for case in cases:
        folder = out / case["name"]
        command = [str(args.blender.resolve()), "--factory-startup", "--python-exit-code", "1",
                   "--python", str(Path(__file__).resolve()), "--", "--inside", str(folder)]
        try:
            with (folder / "blender.log").open("w", encoding="utf-8") as log:
                result = subprocess.run(command, env=isolated_environment(folder), stdout=log,
                                        stderr=subprocess.STDOUT, timeout=args.timeout)
            summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
            if result.returncode or not summary.get("ok"):
                raise SystemExit(f"FAILED: see {folder / 'blender.log'} and summary.json")
        except subprocess.TimeoutExpired:
            raise SystemExit(f"FAILED: Blender exceeded {args.timeout}s; see {folder / 'blender.log'}")
        print(f"PASSED {case['name']}: restart card rendered, draft retained, no turn started")
    (out / "summary.json").write_text(json.dumps({"ok": True, "cases": cases}, indent=2), encoding="utf-8")


def inside(out):
    import addon_utils
    import bpy

    live = out / "addons/loopcut"
    case = json.loads((out / "case.json").read_text(encoding="utf-8"))
    summary = {"ok": False, "assertions": []}
    deadline = time.monotonic() + 90
    steps = []

    def asserted(condition, text):
        if not condition:
            raise AssertionError(text)
        summary["assertions"].append(text)
        print("ASSERT:", text, flush=True)

    def finish(code):
        (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)

    def raise_error(ex):
        raise ex

    def stop_setup_timer():
        from loopcut import onboarding
        if bpy.app.timers.is_registered(onboarding.start):
            bpy.app.timers.unregister(onboarding.start)

    def open_panel():
        from loopcut.ui import host
        window = bpy.context.window_manager.windows[0]
        area = max(window.screen.areas, key=lambda a: a.width * a.height)
        with bpy.context.temp_override(window=window, area=area):
            bpy.ops.loopcut.open()
        host.tag_redraw_all()

    def prepare():
        old_guard = sys.modules.get("loopcut.version_guard")
        if "loopcut" in sys.modules:
            addon_utils.disable("loopcut", handle_error=raise_error)
        if old_guard:
            old_guard.unregister("loopcut")
        for name in list(sys.modules):
            if name == "loopcut" or name.startswith("loopcut."):
                del sys.modules[name]
        sys.path.insert(0, str(live.parent))
        addon = addon_utils.enable("loopcut", handle_error=raise_error)
        asserted(addon is not None and Path(addon.__file__).parent == live, "isolated add-on enabled")
        stop_setup_timer()
        from loopcut import agent, llm
        summary["original_send"] = agent.send.__name__
        if case["name"] == "legacy-reenable":
            asserted(not hasattr(llm, "opened"), "historical llm lacks the cross-module function")
            asserted("loopcut.version_guard" not in sys.modules, "historical session has no import guard")
        elif case["name"] == "guarded-reenable":
            from loopcut import version_guard
            asserted(not hasattr(version_guard, "allow_turn"), "historical guard predates module stamps")
            summary["started"] = version_guard._started
        else:
            from loopcut import version_guard
            version_guard.ensure_consistent()
        open_panel()

    def swap_and_submit():
        from loopcut import agent, llm
        old_llm, old_send = llm, agent.send
        from loopcut.ui import host
        if not host._displays:
            return False
        asserted(bool(host._displays), "the initial panel rendered before file replacement")
        if case["reenable"]:
            addon_utils.disable("loopcut", handle_error=raise_error)
        mtime = (live / "__init__.py").stat().st_mtime
        shutil.copytree(out / "replacement", live, dirs_exist_ok=True)
        os.utime(live / "__init__.py", (mtime + 5, mtime + 5))
        for cache in live.rglob("__pycache__"):
            shutil.rmtree(cache)
        importlib.invalidate_caches()
        if case["reenable"]:
            addon = addon_utils.enable("loopcut", handle_error=raise_error)
            asserted(addon is not None and hasattr(addon, "__loopcut_version__"), "Blender reloaded the replaced initializer")
            stop_setup_timer()
        from loopcut import state, version_guard
        asserted(sys.modules["loopcut.llm"] is old_llm, "file replacement retained the older cached llm")
        finder = next(f for f in sys.meta_path if getattr(f, "_loopcut_version_guard", False)
                      and f.package == "loopcut")
        if case["reenable"]:
            asserted(version_guard.version_on_disk(live) == finder.version,
                     "disk and guard versions match so only cached module checks can refuse")
            asserted(agent.send is not old_send and agent.send.__name__ == "refuse",
                     "legacy send entry point replaced with refusal")
            if "started" in summary:
                asserted(tuple(summary["started"]) == version_guard._started,
                         "refreshing the historical guard preserved the starting version")
        else:
            asserted(agent.send is old_send, "submission uses the original entry point without re-enable")
        try:
            version_guard.ensure_consistent()
        except version_guard.RestartNeeded as ex:
            asserted(not case["reenable"] or ex.name != "loopcut", "cached module scan detects the mixed generation")
        else:
            raise AssertionError("mixed modules bypassed the turn check")
        from loopcut.ui import host
        state.reset()
        session = state.session()
        session["input"] = "Create a cube"
        host._submit(session)
        asserted(session["items"][-1] == state.item_error(version_guard.MESSAGE), "send shows the restart card")
        asserted(session["input"] == "Create a cube" and not session["busy"], "send retains the draft without starting a turn")
        session["messages"] = [{"role": "user", "content": "Create a cube"}]
        asserted(agent.resume() is False and not session["busy"], "resume also refuses the mixed modules")
        if hasattr(agent, "turn_into_3d"):
            asserted(agent.turn_into_3d(0) is False and not session["busy"], "conversion click also refuses mixed modules")
        open_panel()

    def rendered_card():
        from loopcut import version_guard
        from loopcut.ui import host
        displays = list(host._displays.values())
        visible = any(version_guard.MESSAGE in " ".join(p.get("text", "") for p in d.get("prims", []))
                      for d in displays)
        if not visible:
            return False
        asserted(visible, "restart card appears in the real panel display list")
        (out / "panel.json").write_text(json.dumps(displays, default=str, indent=2), encoding="utf-8")
        window = bpy.context.window_manager.windows[0]
        with bpy.context.temp_override(window=window):
            result = bpy.ops.screen.screenshot(filepath=str(out / "restart.png"))
        asserted(result == {"FINISHED"} and (out / "restart.png").is_file(), "restart screenshot saved")
        summary["ok"] = True
        finish(0)

    steps.extend([prepare, swap_and_submit, rendered_card])

    def tick():
        try:
            if time.monotonic() > deadline:
                raise TimeoutError("GUI check exceeded 90 seconds")
            if steps[0]() is not False:
                steps.pop(0)
        except BaseException:
            summary["error"] = traceback.format_exc()
            traceback.print_exc()
            finish(1)
        return 1.0

    bpy.app.timers.register(tick, first_interval=1.0)


def msi_witness(out):
    """Keep an installed instance alive across an MSI run controlled by the PowerShell check."""
    import addon_utils
    import bpy
    import loopcut
    from loopcut import agent, llm, state
    from loopcut.ui import host

    summary = {"ok": False, "assertions": [], "pid": os.getpid()}
    live = Path(loopcut.__file__).parent
    original_llm = llm
    original_version = tuple(loopcut.bl_info["version"])
    deadline = time.monotonic() + 900
    phase = "waiting"

    def asserted(condition, message):
        if not condition:
            raise AssertionError(message)
        summary["assertions"].append(message)
        print("ASSERT:", message, flush=True)

    def finish(code):
        (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)

    def raise_error(ex):
        raise ex

    # Cache unregister's imports before replacement, just as a previously toggled add-on does.
    addon_utils.disable("loopcut", handle_error=raise_error)
    addon_utils.enable("loopcut", handle_error=raise_error)
    from loopcut import onboarding
    if bpy.app.timers.is_registered(onboarding.start):
        bpy.app.timers.unregister(onboarding.start)
    asserted("loopcut.chatgpt_responses" not in sys.modules, "response module is still lazy before the upgrade")
    (out / "ready.json").write_text(json.dumps({"pid": os.getpid(), "version": original_version,
                                               "root": str(live)}), encoding="utf-8")

    def tick():
        nonlocal phase
        try:
            if time.monotonic() > deadline:
                raise TimeoutError("open MSI instance exceeded 900 seconds")
            if phase == "waiting":
                marker = out / "install-complete.json"
                if not marker.is_file():
                    return 0.5
                request = json.loads(marker.read_text(encoding="utf-8-sig"))
                expected = tuple(int(p) for p in request["version"].lstrip("v").split("."))
                asserted((live / "__init__.py").is_file(), "MSI replaced the same shared add-on location")
                # Inspect disk without refreshing the cached guard.
                init = (live / "__init__.py").read_text(encoding="utf-8")
                found = re.search(r'"version":\s*\((\d+),\s*(\d+),\s*(\d+)\)', init)
                asserted(found is not None and tuple(map(int, found.groups())) == expected != original_version,
                         "second instance survived while MSI installed a different add-on version")
                if request["reenable"]:
                    addon_utils.disable("loopcut", handle_error=raise_error)
                    addon = addon_utils.enable("loopcut", handle_error=raise_error)
                    asserted(addon is not None and tuple(addon.bl_info["version"]) == expected,
                             "re-enable loaded the newly installed root")
                from loopcut import version_guard
                asserted(sys.modules["loopcut.llm"] is original_llm, "second instance retained its cached llm")
                if not request["reenable"]:
                    asserted(tuple(loopcut.bl_info["version"]) == original_version, "root was not reloaded")
                    asserted("loopcut.version_guard" in sys.modules and version_guard._started == original_version,
                             "the original installed guard remains active")
                    turn_guard = getattr(version_guard, "allow_turn", None)
                    summary["expected_refusal_path"] = "turn-start" if callable(turn_guard) else "configure-time"
                    summary["refusal_path"] = None

                    if callable(turn_guard):
                        def allow_turn(*args, **kwargs):
                            allowed = turn_guard(*args, **kwargs)
                            if not allowed:
                                summary["refusal_path"] = "turn-start"
                            return allowed
                        version_guard.allow_turn = allow_turn

                    def configure(*args):
                        # Stop at the first response import, before any model or paid request.
                        summary["refusal_path"] = "configure-time"
                        importlib.import_module("loopcut.chatgpt_responses")
                        raise AssertionError("response import bypassed the installed guard")
                    agent._configure = configure
                state.reset()
                session = state.session()
                session["input"] = "Create a cube"
                host._submit(session)
                if not request["reenable"]:
                    expected_path = summary["expected_refusal_path"]
                    asserted(summary["refusal_path"] == expected_path,
                             f"expected {expected_path} refusal, observed {summary['refusal_path']}")
                asserted(session["items"][-1] == state.item_error(version_guard.MESSAGE),
                         "submission after MSI replacement shows the restart card")
                asserted(session["input"] == "Create a cube" and not session["busy"],
                         "second instance preserves the draft and starts no request")
                window = bpy.context.window_manager.windows[0]
                area = max(window.screen.areas, key=lambda a: a.width * a.height)
                with bpy.context.temp_override(window=window, area=area):
                    bpy.ops.loopcut.open()
                host.tag_redraw_all()
                phase = "render"
            else:
                from loopcut import version_guard
                displays = list(host._displays.values())
                visible = any(version_guard.MESSAGE in " ".join(p.get("text", "") for p in d.get("prims", []))
                              for d in displays)
                if not visible:
                    return 0.5
                asserted(visible, "MSI survivor renders the restart card")
                (out / "panel.json").write_text(json.dumps(displays, default=str, indent=2), encoding="utf-8")
                window = bpy.context.window_manager.windows[0]
                with bpy.context.temp_override(window=window):
                    result = bpy.ops.screen.screenshot(filepath=str(out / "restart.png"))
                asserted(result == {"FINISHED"} and (out / "restart.png").is_file(), "MSI survivor screenshot saved")
                summary["ok"] = True
                finish(0)
        except BaseException:
            summary["error"] = traceback.format_exc()
            traceback.print_exc()
            finish(1)
        return 0.5

    bpy.app.timers.register(tick, first_interval=0.5)


if __name__ == "__main__":
    if "--msi-witness" in sys.argv:
        msi_witness(Path(sys.argv[sys.argv.index("--msi-witness") + 1]))
    elif "--inside" in sys.argv:
        inside(Path(sys.argv[sys.argv.index("--inside") + 1]))
    else:
        run()
