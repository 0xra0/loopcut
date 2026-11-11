"""Run the local eval tasks against a model on this computer (Strata by default), each in its own GUI Blender.

    python3 loopcut/harness/local_evals/run.py [task ...] [--label "what changed"] [--no-skills]
                                               [--base-url http://127.0.0.1:8099/v1] [--model strata]

Writes RUNS/<time>/: results.json (the format distill.py reads), summary.md, and per task its transcript,
conversation, Blender log and output folder. summary.md compares the pass rate with the previous run.
Blender runs with ~/.local/bin stripped from PATH (its Python must not come from there).
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = Path(os.environ.get("LOCAL_EVAL_RUNS", "/mnt/ssd/work/loopcut-local/runs"))
SKILLS = Path(os.environ.get("LOCAL_EVAL_SKILLS", "/mnt/sata/loopcut/local-skills"))
sys.path.insert(0, str(HERE))


def task_ids():
    """The task ids without importing bpy: tasks.py's TASKS keys, read from its source."""
    import ast
    tree = ast.parse((HERE / "tasks.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "TASKS":
            return [k.value for k in node.value.keys]
    raise SystemExit("tasks.py has no TASKS")


def server_up(base_url):
    """Something answers HTTP there (an error status counts: harness/fake_model.py serves only POST)."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=10):
            return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def main():
    ids = task_ids()
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks", nargs="*", help=f"default: all of {', '.join(ids)}")
    ap.add_argument("--label", default="")
    ap.add_argument("--no-skills", action="store_true")
    ap.add_argument("--base-url", default="http://127.0.0.1:8099/v1")
    ap.add_argument("--model", default="strata")
    ap.add_argument("--timeout", type=float, default=1200, help="seconds per task turn")
    ap.add_argument("--max-steps", type=int, default=40)
    args = ap.parse_args()
    wanted = args.tasks or ids
    unknown = sorted(set(wanted) - set(ids))
    if unknown:
        raise SystemExit(f"unknown tasks {unknown}; known: {ids}")
    if not server_up(args.base_url):
        raise SystemExit(f"no model server at {args.base_url} (start Strata first)")

    run = RUNS / time.strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True)
    clean_path = ":".join(p for p in os.environ.get("PATH", "").split(":") if ".local/bin" not in p)
    env = dict(os.environ, PATH=clean_path, LOOPCUT_BASE_URL=args.base_url, LOOPCUT_MODEL=args.model,
               LOOPCUT_API_KEY="local", LOOPCUT_AUTO_RUN="true", LOOPCUT_STALL_SECONDS="0",
               LOOPCUT_AGENT_TERMS="off", LOOPCUT_ORCHESTRATE="false", LOOPCUT_ROUTER="off",
               LOOPCUT_MAX_STEPS=str(args.max_steps), LOOPCUT_TELEMETRY="off",
               LOCAL_EVAL_TIMEOUT=str(args.timeout), LOOPCUT_SKILLS_DIR="" if args.no_skills else str(SKILLS))
    meta = {"label": args.label, "model": args.model, "base_url": args.base_url,
            "skills": None if args.no_skills else str(SKILLS), "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    results = []
    for task in wanted:
        print(f"[{time.strftime('%H:%M:%S')}] {task} ...", flush=True)
        log = run / f"{task}.log"
        with log.open("w") as handle:
            try:
                subprocess.run(["blender", "--factory-startup", "--python", str(HERE / "run_task.py"), "--", task,
                                str(run)], env=env, stdout=handle, stderr=subprocess.STDOUT,
                               timeout=args.timeout + 300)
            except subprocess.TimeoutExpired:
                handle.write("\nrun.py: Blender did not exit in time and was killed\n")
        result_file = run / f"{task}.result.json"
        if result_file.is_file():
            result = json.loads(result_file.read_text(encoding="utf-8"))
        else:
            result = {"id": task, "passed": False, "detail": f"no result (see {log.name})", "steps": 0,
                      "seconds": 0, "failed_calls": 0}
        results.append(result)
        print(f"    {'PASS' if result['passed'] else 'FAIL'} {result['seconds']}s {result['steps']} steps: "
              f"{result['detail']}", flush=True)
        (run / "results.json").write_text(json.dumps({**meta, "results": results}, indent=1), encoding="utf-8")

    passed = sum(1 for r in results if r["passed"])
    previous = sorted(p for p in RUNS.iterdir() if p.is_dir() and p != run and (p / "results.json").is_file())
    lines = [f"# Local evals {run.name}", "", f"{meta}", "",
             f"**{passed}/{len(results)} passed**, {sum(r['seconds'] for r in results)} s, "
             f"{sum(r['steps'] for r in results)} steps, {sum(r['failed_calls'] for r in results)} failed calls", ""]
    if previous:
        before = {r["id"]: r for r in json.loads((previous[-1] / "results.json").read_text())["results"]}
        won = [r["id"] for r in results if r["passed"] and not before.get(r["id"], {}).get("passed", True)]
        lost = [r["id"] for r in results if not r["passed"] and before.get(r["id"], {}).get("passed")]
        lines += [f"Against {previous[-1].name}: won {won or 'none'}, lost {lost or 'none'}", ""]
    lines += ["| task | result | s | steps | failed calls | detail |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {'PASS' if r['passed'] else 'FAIL'} | {r['seconds']} | {r['steps']} | "
              f"{r['failed_calls']} | {str(r['detail']).replace('|', '/')} |" for r in results]
    (run / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{passed}/{len(results)} passed -> {run / 'summary.md'}")


if __name__ == "__main__":
    main()
