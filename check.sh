#!/usr/bin/env bash
# The gate a change passes before it is committed:  ./check.sh [BASE]
#
#   1. Rules on the lines added since BASE (default HEAD: uncommitted work, new files included).
#      For a branch, pass where it started:  ./check.sh main
#      Also names used but never defined in the changed files (ruff F821), compared with BASE.
#   2. Headless smoke: the add-on registers in real Blender and runs real tools, studio commands
#      included, through tools.execute (loopcut/harness/smoke_check.py).
#   3. The full unit suite (about 4 minutes). Two failures are known and allowed.
#
# Exit 0 and "CHECK GREEN" only when all three pass. A flagged line that is right as written
# carries "# check: ok <reason>" on that line; the reviewer reads every one of those.
# BLENDER=path picks the Blender build (default: build-full-glogfix/bin/blender, then blender).
set -uo pipefail
cd "$(dirname "$0")"

BASE="${1:-HEAD}"
LOGS="$(mktemp -d "${TMPDIR:-/tmp}/loopcut-check.XXXXXX")"
failed=()

echo "== 1/3 rules on lines added since $BASE"
if ! python3 - "$BASE" <<'PY'
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

base = sys.argv[1]
PKG, TESTS, HARNESS = "scripts/addons_core/loopcut/", "loopcut/tests/", "loopcut/harness/"


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


added, new_modules, path, n = [], set(), None, 0
for line in git("diff", "-U0", "--no-color", "--no-ext-diff", base, "--", PKG, TESTS, HARNESS).splitlines():
    if line.startswith("+++ "):
        path = line[6:] if line.startswith("+++ b/") and line.endswith(".py") else None
    elif line.startswith("@@"):
        n = int(re.search(r"\+(\d+)", line).group(1))
    elif line.startswith("+") and path:
        added.append((path, n, line[1:]))
        n += 1
new_modules |= {p for p in git("diff", "--name-only", "--diff-filter=A", base, "--", PKG).split() if p.endswith(".py")}
for p in git("ls-files", "--others", "--exclude-standard", "--", PKG, TESTS, HARNESS).split():
    if p.endswith(".py"):
        added += [(p, i, t) for i, t in enumerate(Path(p).read_text(errors="replace").splitlines(), 1)]
        if p.startswith(PKG):
            new_modules.add(p)


def in_pkg(p):
    return p.startswith(PKG) and "/tests/" not in p


# Built for every message the user sends; tools that run once on request are not in it.
PER_MESSAGE = {PKG + f for f in ("scene_context.py", "context_manager.py", "context.py", "project_memory.py")}


RULES = [
    (re.compile(r"^\s*except\s*(:|\(?\s*(Base)?Exception\b)"), in_pkg,
     "catch-all except: catch the errors you expect, so TimeoutError and llm.Cancelled pass through"),
    (re.compile(r"^sys\.modules(\.setdefault\(|\[)\s*['\"]bpy"), lambda p: p.startswith(TESTS),
     "module-level fake bpy leaks into every later test: patch sys.modules['bpy'] in setUp"),
    (re.compile(r"--dangerously"), lambda p: True,
     "a permission bypass: the user approves what runs"),
    (re.compile(r"\bfor\b.+\bin\b.+\.(polygons|vertices|edges|loops|faces|verts)\b"),
     lambda p: p in PER_MESSAGE or p.startswith(PKG + "ui/"),
     "a Python loop over mesh elements on the per-message path: read in bulk with foreach_get and numpy"),
    (re.compile(r"urlopen|urllib\.request|requests\.(get|post)|http\.client|socket\.|"
                r"strata\.(test_strata_connection|is_port_open|discover_localhost)"),
     lambda p: p == PKG + "settings.py" or p.startswith(PKG + "ui/"),
     "network call where draw() can reach it: draw shows a stored result, an operator fetches it"),
    (re.compile(r"```"), in_pkg,
     "code fences parsed out of model text: a reply is shown, the model runs code through tool calls"),
]

problems = []
for p, line_no, text in added:
    if re.search(r"#\s*check:\s*ok\s+\S", text):
        continue
    for pattern, applies, why in RULES:
        if applies(p) and pattern.search(text):
            problems.append(f"{p}:{line_no}: {why}\n    {text.strip()}")

for p in sorted({p for p, _, _ in added}):
    if Path(p).exists():
        try:
            compile(Path(p).read_text(), p, "exec")
        except SyntaxError as e:
            problems.append(f"{p}:{e.lineno}: syntax error: {e.msg}")

# A name used and never defined (a missing import) fails only when that line runs, and the fake bpy in
# the unit tests keeps most lines from running. Compared with BASE, so names undefined before don't count.
def undefined(path, source):
    out = subprocess.run(["ruff", "check", "--select", "F821", "--output-format", "json", "--no-cache",
                          "--exit-zero", "--stdin-filename", path, "-"],
                         input=source, capture_output=True, text=True, check=True).stdout
    return [(d["location"]["row"], d["message"]) for d in json.loads(out)]


try:
    for p in sorted({p for p, _, _ in added if Path(p).exists()}):
        old = subprocess.run(["git", "show", f"{base}:{p}"], capture_output=True, text=True)
        known = Counter(m for _, m in undefined(p, old.stdout)) if old.returncode == 0 else Counter()
        for row, message in undefined(p, Path(p).read_text()):
            if known[message]:
                known[message] -= 1
            else:
                problems.append(f"{p}:{row}: {message}: import it or define it")
except FileNotFoundError:
    problems.append("ruff is not installed, so the undefined-name check could not run: pip install ruff")

for p in sorted(new_modules):
    stem = Path(p).stem
    if stem == "__init__" or stem.endswith("_worker") or "/tests/" in p:
        continue
    word = re.compile(rf"\b{re.escape(stem)}\b")

    def mentions(files):
        return any(word.search(line) and not line.lstrip().startswith("#")
                   for f in files if f != Path(p) for line in f.read_text(errors="replace").splitlines())

    if not mentions(Path(PKG).rglob("*.py")):
        problems.append(f"{p}: new module nothing in the add-on uses: wire it to its caller")
    if not mentions(Path(TESTS).glob("*.py")):
        problems.append(f"{p}: new module no test reaches: add the test that goes through its caller")

print("\n".join(problems) if problems else "rules: clean")
sys.exit(1 if problems else 0)
PY
then failed+=("rules")
fi

echo "== 2/3 headless smoke in real Blender"
BLENDER="${BLENDER:-}"
[ -z "$BLENDER" ] && [ -x build-full-glogfix/bin/blender ] && BLENDER=build-full-glogfix/bin/blender
[ -z "$BLENDER" ] && BLENDER="$(command -v blender || true)"
if [ -z "$BLENDER" ]; then
    echo "no Blender found: set BLENDER=/path/to/blender"
    failed+=("smoke")
elif timeout 180 "$BLENDER" -b --factory-startup --python loopcut/harness/smoke_check.py \
        > "$LOGS/smoke.log" 2>&1 && grep -q "^SMOKE OK" "$LOGS/smoke.log"; then
    echo "smoke: ok ($BLENDER)"
else
    grep -A40 "smoke_check.py\", line" "$LOGS/smoke.log" | tail -25
    echo "smoke failed: full log $LOGS/smoke.log"
    failed+=("smoke")
fi
if pgrep -f "loopcut/harness/smoke_check.py" > /dev/null; then
    echo "a smoke Blender is still running:"; pgrep -af "loopcut/harness/smoke_check.py"
    failed+=("leftover-process")
fi

echo "== 3/3 unit suite"
python3 -m unittest discover -s loopcut/tests > "$LOGS/unit.log" 2>&1
KNOWN='^(test_beta_servers_check\.BetaServersCheckTest\.test_production_html_404_lifecycle_panel_and_turns|test_hosted_prompts\.IndexTest\.test_configure_uses_cached_index_without_refreshing)$'
new_failures="$(sed -nE 's/^(FAIL|ERROR): [^ ]+ \(([^)]+)\).*/\2/p' "$LOGS/unit.log" | sort -u | grep -Ev "$KNOWN")"
# Some tests give a localhost connection 50 ms; on a loaded machine (a firewall checking each
# connection) one can miss it. A new failure is rerun once on its own: a real one fails again.
if [ -n "$new_failures" ] && (cd loopcut/tests && python3 -m unittest $new_failures) > "$LOGS/rerun.log" 2>&1; then
    echo "flaky, passed when rerun alone: $(echo $new_failures)"
    new_failures=""
fi
if ! grep -q "^Ran [0-9]* tests" "$LOGS/unit.log"; then
    tail -20 "$LOGS/unit.log"; echo "the suite did not finish: full log $LOGS/unit.log"
    failed+=("unit")
elif [ -n "$new_failures" ]; then
    echo "$new_failures"; echo "new failures: full log $LOGS/unit.log"
    failed+=("unit")
else
    grep -E "^Ran |^(OK|FAILED)" "$LOGS/unit.log" | tr '\n' ' '; echo "(known failures only)"
fi

if [ ${#failed[@]} -eq 0 ]; then
    echo "CHECK GREEN"
else
    echo "CHECK RED: ${failed[*]}"
    exit 1
fi
