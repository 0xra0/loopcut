"""Every local eval check fails on the untouched scene and passes on its reference solution. No model.

    blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/local_evals/selfcheck.py [-- task ...]
"""
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("LOCAL_EVAL_OUT", tempfile.mkdtemp(prefix="local-evals-selfcheck-"))
import tasks  # noqa: E402

wanted = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else list(tasks.TASKS)
bad = []
for task_id in wanted:
    try:
        tasks.setup(task_id)
        before, why_before = tasks.check(task_id)
        tasks.reference(task_id)
        after, why_after = tasks.check(task_id)
    except Exception:  # noqa: BLE001
        bad.append(task_id)
        print(f"ERROR {task_id}\n{traceback.format_exc()}")
        continue
    good = not before and after
    bad += [] if good else [task_id]
    print(f"{'OK  ' if good else 'BAD '} {task_id}\n     untouched: {before} {why_before}\n     reference: {after} {why_after}")
print(f"SELFCHECK {len(wanted) - len(bad)}/{len(wanted)} ok" + (f"; bad: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
