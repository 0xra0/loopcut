# Claude Blender Integration inside Loopcut

This fork embeds the MIT-licensed [Claude Blender Integration](https://github.com/rotembab/claude-blender-integration)
command handlers directly into Loopcut's agent; no extra socket, MCP server, Python installation,
or separate N-panel is needed for **Loopcut chat**.

## Use in Loopcut

Ask: **"Read the retopo_and_bake playbook, clean up the selected character into a game-ready mesh,
inspect the UV islands, and bake a normal map. Check before each destructive step."**

The agent has two additional tools:

* `studio_guide()` lists structured command groups. `studio_guide(toolset="sculpt")` narrows them.
  `studio_guide(command="sculpt_stroke")` returns exact parameters and docstrings.
  `studio_guide(playbook="list")` lists staged workflows; for example
  `studio_guide(playbook="retopo_and_bake")` loads acceptance criteria.
* `studio_action(command="sculpt_stroke", params={...}, summary="...")` executes the original
  handler on Blender's main thread; its result includes scene diff and a normal Loopcut undo step.

Available groups: look, modeling, topology, sculpt, uv_bake, materials, reference, rig, animate, export,
plus the core read-only scene summary. The upstream `run_python`, `checkpoint`, `rollback`,
`list_checkpoints` and MCP-only commands are intentionally unavailable through `studio_action`:
Loopcut already supplies its own approved Python executor, checkpoints and one-click Undo all.
The upstream `capture_views` and `compare_to_reference` are disabled in the Loopcut toolset: use
Loopcut's viewport capture and reference comparison tools, which avoid a blocking render in the UI.

Scene-changing commands use Loopcut's existing approval + checkpoint system. Heavy geometry, renders
and bake commands request the extra heavy-work approval. `auto_rig_unirig` always requires explicit
consent because it launches an external local process; it also needs UniRig and compatible hardware.
`image_to_3d` requires a running local Hunyuan3D service, always asks for consent before
sending the reference image, and is still experimental. Blender 5.2 API
compatibility of individual upstream handlers must be confirmed on your actual meshes; no full Blender
GUI or long-running render was available when this source patch was assembled.

## Claude Code / Desktop (optional, separate)

Loopcut's built-in chat does **not** require MCP. For direct Claude Code/Desktop access to the *same*
Blender session, the upstream MCP server and its agent skills are preserved at
`loopcut/integrations/claude_blender_mcp/`. These operate through the **separate** upstream addon
and its localhost socket 9877, which must be installed and started independently if needed.
Do not run the MCP server and assume it is the same approval-gated Loopcut chat surface:
requests sent to the external MCP addon bypass Loopcut's approval layer.

## Source and licensing

`studio_vendor/handlers/` and `studio_vendor/protocol.py` are taken from upstream's
`addon/claude_blender/`, version `(0, 1, 0)` in the user-provided archive.
`studio_playbooks/` contains the upstream MCP playbooks. License is MIT; read
`studio_vendor/LICENSE`. Loopcut's own source remains GPL-3.0-or-later.

## Quick checks

```bash
python3 -m unittest loopcut.tests.test_studio_integration
python3 -m unittest discover -s loopcut/tests
```

For an actual Blender check, use Loopcut chat with `studio_guide(command="mesh_report")`, then
`studio_action(command="mesh_report", params={"name":"Cube"}, summary="Inspect Cube")`.
