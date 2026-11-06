"""The add-on unit suite on SPLAP's Windows Python, in place of the GitHub Windows CI (windows-rc.yml).

    python3 loopcut/windows/laptop.py check --script loopcut/harness/windows_unit_check.py --driver --timeout 3000 \
        --arg={job} --wait

Runs `python -m unittest discover -s loopcut/tests -v` from the checkout root, with the CI's own Windows sets first
(the cmd.exe workspace shell set, the sign-in/telemetry files, the updater tests), and writes
windows-unit.json to the job folder: each run's exit code, totals and the ids of failing and erroring tests.
Several tests are POSIX-only, so a pass here is "no failure the base revision does not also have": run this same
script on the base and compare the two id lists. Exits 0 when every run finished and the JSON was written; 1 when a
run could not start or timed out.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
RUN_TIMEOUT = 1500
CI_FILES = ("test_device_credentials", "test_onboarding", "test_chatgpt", "test_telemetry", "test_repeats",
            "test_settings")
_BAD = re.compile(r"^(FAIL|ERROR): (\S+) \((\S+)\)", re.M)
_RAN = re.compile(r"^Ran (\d+) tests? in", re.M)


def run(label: str, args: list) -> dict:
    started = time.time()
    log = OUT / f"unit-{label}.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "LOOPCUT_TELEMETRY": "off"}
    try:
        done = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT)
        text, code = done.stdout + done.stderr, done.returncode
    except subprocess.TimeoutExpired as error:
        text = (error.stdout or "") + (error.stderr or "") if isinstance(error.stdout, str) else ""
        code = "timeout"
    log.write_text(text, encoding="utf-8")
    ran = _RAN.findall(text)
    bad = sorted({f"{kind} {cls}.{name}" if not cls.endswith(name) else f"{kind} {cls}"
                  for kind, name, cls in _BAD.findall(text)})
    result = {"label": label, "args": args, "exit": code, "ran": int(ran[-1]) if ran else None, "bad": bad,
              "seconds": round(time.time() - started, 1)}
    print(f"{label}: exit {code}, ran {result['ran']}, {len(bad)} failing", flush=True)
    return result


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"Python {sys.version.split()[0]} at {sys.executable}; checkout {ROOT}", flush=True)
    results = [run("workspace-shell", ["loopcut/tests/test_workspace.py", "WindowsShellTest", "ShellTest",
                                       "ShellSummaryTest", "ProcessTest", "PureTest", "-v"])]
    results += [run(name, [f"loopcut/tests/{name}.py", "-v"]) for name in CI_FILES
                if (ROOT / "loopcut" / "tests" / f"{name}.py").is_file()]
    results.append(run("update", ["-m", "unittest", "discover", "-s", "loopcut/tests", "-p", "test_update.py", "-v"]))
    results.append(run("full", ["-m", "unittest", "discover", "-s", "loopcut/tests", "-v"]))
    (OUT / "windows-unit.json").write_text(json.dumps({"python": sys.version, "results": results}, indent=1),
                                           encoding="utf-8")
    finished = all(r["exit"] != "timeout" and r["ran"] is not None for r in results)
    print(f"WINDOWS UNIT {'FINISHED' if finished else 'INCOMPLETE'}", flush=True)
    return 0 if finished else 1


if __name__ == "__main__":
    sys.exit(main())
