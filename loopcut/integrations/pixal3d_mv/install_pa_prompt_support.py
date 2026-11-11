"""Safely add optional prompt controls to an existing pa.py without replacing it.

Run: python install_pa_prompt_support.py /path/to/pa.py
Writes a .bak-prompts backup; aborts without editing on unexpected layout.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python install_pa_prompt_support.py /path/to/pa.py")
    target = Path(sys.argv[1]).expanduser().resolve()
    if not target.is_file():
        raise SystemExit(f"Cannot find {target}")
    source = target.read_text(encoding="utf-8")
    marker = "from pa_prompt_support import configure_from_argv, apply_prompt_settings"
    if marker in source:
        raise SystemExit("Already installed (import marker found). No changes made.")

    # Replace only the Qwen assignment inside the named stage function.
    modified = source
    for stage, template in (("front", "FRONT_PROMPT"), ("sheet", "SHEET_PROMPT")):
        function_match = re.search(rf"(?m)^def {stage}\s*\([^\n]*\)[ \t]*:[ \t]*\n", modified)
        if not function_match:
            raise SystemExit(f"Cannot find def {stage}(...):; no changes made")
        start = function_match.end()
        next_block = re.search(r"(?m)^(?:def |class |if __name__\s*==)", modified[start:])
        end = start + next_block.start() if next_block else len(modified)
        body = modified[start:end]
        pattern = rf'(?m)^([ \t]*)p\["5"\]\["inputs"\]\["prompt"\]\s*=\s*{template}\.format\(desc=desc\)[ \t]*$'
        replacement = rf'\1apply_prompt_settings(p, "{stage}", {template}.format(desc=desc))'
        new_body, count = re.subn(pattern, replacement, body)
        if count != 1:
            raise SystemExit(f"Expected one node-5 assignment in {stage}(); found {count}. No changes made.")
        modified = modified[:start] + new_body + modified[end:]

    main_guard = re.search(r'(?m)^if __name__\s*==\s*[\'\"]__main__[\'\"][ \t]*:[ \t]*$', modified)
    if not main_guard:
        raise SystemExit("Cannot find __main__ guard; no changes made.")
    line_end = modified.find("\n", main_guard.end())
    if line_end == -1:
        raise SystemExit("Empty __main__ block; no changes made.")
    modified = modified[:line_end + 1] + "    configure_from_argv(sys.argv)\n" + modified[line_end + 1:]

    # Place the import after all existing imports but before the template constants.
    anchor = re.search(r"(?m)^FRONT_PROMPT\s*=", modified)
    if not anchor:
        raise SystemExit("Cannot find FRONT_PROMPT; no changes made.")
    modified = modified[:anchor.start()] + marker + "\n\n" + modified[anchor.start():]
    compile(modified, str(target), "exec")

    backup = Path(str(target) + ".bak-prompts")
    if backup.exists():
        raise SystemExit(f"Backup path exists: {backup}. No changes made.")
    backup.write_text(source, encoding="utf-8")
    target.write_text(modified, encoding="utf-8")
    print(f"Patched {target}")
    print(f"Backup {backup}")
    print("Keep pa_prompt_support.py in the same folder as pa.py.")


if __name__ == "__main__":
    main()
