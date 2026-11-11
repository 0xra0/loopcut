import asyncio
import importlib


def _tool_names(monkeypatch, toolsets):
    monkeypatch.setenv("CLAUDE_BLENDER_TOOLSETS", toolsets)
    import claude_blender_mcp.app as app

    app = importlib.reload(app)
    return {t.name for t in asyncio.run(app.mcp.list_tools())}


def test_all_tools_registered(monkeypatch):
    names = _tool_names(monkeypatch, "all")
    assert {"get_scene_state", "run_python", "checkpoint", "rollback", "list_checkpoints",
            "capture_views", "get_object_details", "mesh_report",
            "create_primitive", "mesh_edit", "boolean", "sculpt_prepare", "sculpt_stroke",
            "quad_remesh", "decimate", "uv_unwrap", "bake_maps",
            "create_material", "rig_template", "bind_mesh", "pose_test", "procedural_cycle",
            "export_model", "reimport_check", "auto_rig_unirig"} <= names


def test_toolset_filter(monkeypatch):
    names = _tool_names(monkeypatch, "core")
    assert "run_python" in names
    assert "capture_views" not in names


def test_every_tool_maps_to_an_addon_command(monkeypatch):
    """Each MCP tool must call a command the addon registers, with parameters it accepts."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    addon_cmds = {}
    for path in (root / "addon/claude_blender/handlers").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and getattr(dec.func, "id", "") == "command":
                        args = {a.arg for a in node.args.args + node.args.kwonlyargs}
                        addon_cmds[dec.args[0].value] = args
    server_dir = root / "server/src/claude_blender_mcp"
    for path in server_dir.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("call", "_call") and node.args:
                cmd = node.args[0].value
                assert cmd in addon_cmds, f"{path.name}: unknown addon command {cmd!r}"
                kw = {k.arg for k in node.keywords} - {"timeout"}
                assert kw <= addon_cmds[cmd], f"{cmd}: addon does not accept {kw - addon_cmds[cmd]}"


def test_playbooks_name_only_real_tools(monkeypatch):
    """Every `tool_name(` in a playbook must be a registered tool, so playbooks can't drift."""
    import re

    from claude_blender_mcp import guide

    tools = _tool_names(monkeypatch, "all")
    for name in guide.names():
        text = guide.read(name)
        assert text.startswith("# ") and "> When:" in text, name
        used = set(re.findall(r"`([a-z_]+)\(", text))
        assert used <= tools, f"{name}.md names unknown tools {sorted(used - tools)}"


def test_guide_tools_and_prompt(monkeypatch):
    import json

    import pytest

    monkeypatch.setenv("CLAUDE_BLENDER_TOOLSETS", "core")
    import claude_blender_mcp.app as app

    app = importlib.reload(app)
    names = {t.name for t in asyncio.run(app.mcp.list_tools())}
    assert {"list_playbooks", "get_playbook"} <= names  # on even with a narrow toolset filter
    index = {p["name"]: p for p in json.loads(app.list_playbooks())}
    assert {"planning", "game_character", "detailed_sculpt", "rig_and_animate", "export_to_engine"} <= set(index)
    assert all(p["when"] for p in index.values())
    assert "acceptance check" in app.get_playbook("planning")
    with pytest.raises(ValueError, match="Available"):
        app.get_playbook("nope")
    assert [p.name for p in asyncio.run(app.mcp.list_prompts())] == ["plan_build"]
    assert "a wooden chair" in app.plan_build("a wooden chair")


def test_agent_plugin_matches_server(monkeypatch):
    """The Claude Code plugin's manifests parse, and its skill and subagent name real tools and playbooks."""
    import json
    import pathlib
    import re

    from claude_blender_mcp import guide

    root = pathlib.Path(__file__).resolve().parents[2]
    agent = root / "agent"
    plugin = json.loads((agent / ".claude-plugin/plugin.json").read_text())
    market = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    assert market["plugins"][0]["name"] == plugin["name"]
    assert "blender" in json.loads((agent / ".mcp.json").read_text())["mcpServers"]

    known = _tool_names(monkeypatch, "all") | set(guide.names()) | {"blender", "wireframe", "xray"}
    for md in [*agent.glob("skills/*/SKILL.md"), *agent.glob("agents/*.md")]:
        text = md.read_text(encoding="utf-8")
        front = dict(re.findall(r"^(\w+): (.+)$", text.split("---")[1], re.M))
        assert front.get("name") and front.get("description"), md
        if md.name == "SKILL.md":
            assert front["name"] == md.parent.name
        names = set(re.findall(r"`([a-z_]+)(?:`|\()", text))
        assert names <= known, f"{md.relative_to(root)} names unknown tools {sorted(names - known)}"
