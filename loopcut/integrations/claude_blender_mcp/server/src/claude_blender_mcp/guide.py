"""Playbooks: Markdown guides for multi-stage builds, shipped with the server.

Each file in playbooks/ starts with a "# Title" line and a "> When: ..." line that says when
to use it. Tools are referenced as `tool_name(...)`, and a test checks that every name exists.
"""

import pathlib

DIR = pathlib.Path(__file__).resolve().parent / "playbooks"


def names():
    return sorted(p.stem for p in DIR.glob("*.md"))


def read(name):
    path = DIR / f"{name}.md"
    if name not in names():
        raise ValueError(f"No playbook {name!r}. Available: {', '.join(names())}")
    return path.read_text(encoding="utf-8")


def index():
    out = []
    for name in names():
        lines = read(name).splitlines()
        title = lines[0].lstrip("# ").strip()
        when = next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("> When:")), "")
        out.append({"name": name, "title": title, "when": when})
    return out
