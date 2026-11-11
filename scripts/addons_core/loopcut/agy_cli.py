"""Antigravity CLI (agy) model provider for Loopcut.

Streams responses from the local Antigravity CLI ('agy') session using its NDJSON stream-json protocol.
The user selects agy in Blender preferences; default model is Claude Sonnet 4.6 (Thinking).
Tools are exposed to the model via JSON instructions and converted into Loopcut ToolCalls.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from . import config, llm

# Models supported by agy; default is claude-sonnet-4-6.
MODELS = (
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gemini-3.8-flash",
    "gpt-oss-120b-medium",
)

MODEL_LABELS = {
    "claude-sonnet-4-6": "Claude Sonnet 4.6 (Thinking)",
    "claude-opus-4-6-thinking": "Claude Opus 4.6 (Thinking)",
    "claude-opus-4-6": "Claude Opus 4.6 (Thinking)",
    "gemini-3.8-flash": "Gemini 3.8 Flash",
    "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
    "gpt-oss-120b-medium": "GPT-OSS 120B (Medium)",
    "gpt-oss-120b": "GPT-OSS 120B",
    "gpt-120b": "GPT-OSS 120B (Medium)",
}

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def model_label(model_id: str) -> str:
    """Friendly display name for an agy model."""
    if not model_id:
        return ""
    if model_id in MODEL_LABELS:
        return MODEL_LABELS[model_id]
    resolved = resolve_model(model_id)
    if resolved in MODEL_LABELS:
        return MODEL_LABELS[resolved]
    return model_id.replace("-", " ").title()


def resolve_model(model: str, effort: str = "") -> str:
    """Normalize user-friendly or alias model names to the exact agy CLI model identifier.

    - gemini 3.8 flash -> gemini-3.8-flash-<effort> (high, medium, low)
    - claude opus 4.6 -> claude-opus-4-6-thinking
    - gpt 120 b -> gpt-oss-120b-medium
    - claude sonnet 4.6 -> claude-sonnet-4-6
    """
    raw = (model or "").strip()
    low = raw.lower().replace(" ", "-").replace("_", "-")

    # Gemini 3.8 Flash
    if low in ("gemini-3.8-flash", "gemini-flash", "gemini-3-8-flash") or low.startswith("gemini-3.8-flash-"):
        eff = (effort or "high").lower()
        tier = "low" if eff == "low" else ("medium" if eff == "medium" else "high")
        if low in ("gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high") and not effort:
            return low
        return f"gemini-3.8-flash-{tier}"

    # Claude Opus 4.6
    if "opus" in low and ("4-6" in low or "4.6" in low or low == "claude-opus"):
        return "claude-opus-4-6-thinking"

    # Claude Sonnet 4.6
    if "sonnet" in low and ("4-6" in low or "4.6" in low or low == "claude-sonnet"):
        return "claude-sonnet-4-6"

    # GPT 120B / GPT-OSS 120B
    if "120b" in low or "120-b" in low or "gpt-120" in low:
        return "gpt-oss-120b-medium"

    return raw or config.AGY_MODEL


def find_agy_binary() -> str:
    """Locate the agy executable on the local system."""
    override = os.environ.get("LOOPCUT_AGY_PATH", "").strip()
    if override and os.path.exists(override) and os.access(override, os.X_OK):
        return override
    which = shutil.which("agy")
    if which:
        return which
    candidates = [
        Path.home() / ".local" / "bin" / "agy",
        Path("/usr/local/bin/agy"),
        Path("/usr/bin/agy"),
    ]
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise llm.LLMError(
        "Antigravity CLI ('agy') was not found on this computer. "
        "Ensure agy is installed in ~/.local/bin or available on your PATH.",
        code="not_running",
    )


def list_models() -> list[str]:
    """Fetch available models from `agy models`."""
    try:
        binary = find_agy_binary()
        proc = subprocess.run(
            [binary, "models"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            found = []
            for line in proc.stdout.splitlines():
                line = line.strip()
                # Lines are formatted like: "model-id   Model Label"
                parts = line.split()
                if parts and not parts[0].startswith("⠋") and not parts[0].startswith("Fetching"):
                    model_id = parts[0]
                    # Filter out spinners or progress text
                    if re.match(r"^[a-zA-Z0-9_\-\.]+$", model_id):
                        found.append(model_id)
            if found:
                return found
    except Exception:
        pass
    return list(MODELS)


def _render_messages(messages: list[dict]) -> list[str]:
    """Format conversation messages for agy prompt."""
    out = []
    # Build a lookup of call_id -> tool_name across the conversation
    tool_map = {}
    for m in messages:
        for c in m.get("tool_calls") or []:
            fn = c.get("function") or c
            cid = c.get("id") or fn.get("id") or ""
            tname = fn.get("name") or c.get("name") or ""
            if cid and tname:
                tool_map[cid] = tname

    for i, message in enumerate(messages):
        role = message.get("role", "user")
        if role == "system":
            continue

        if role == "tool":
            cid = message.get("tool_call_id") or ""
            tname = message.get("name") or tool_map.get(cid, "")
            title = f"Tool Result: `{tname}`" if tname else "Tool Result"
            if cid:
                title += f" (call_id: `{cid}`)"
            head = f"### [{i}] {title}"
        elif role == "assistant":
            head = f"### [{i}] assistant"
        else:
            head = f"### [{i}] {role}"

        out.append(head)
        out.append("")

        content = message.get("content")
        if isinstance(content, str):
            # If tool result content is massive (e.g. huge vertex dumps), truncate the middle
            # so the model does not lose context or exceed token attention limits
            if role == "tool" and len(content) > 12000:
                truncated_note = f"\n\n... [truncated {len(content) - 10000} characters of output] ...\n\n"
                content = content[:6000] + truncated_note + content[-4000:]
            out.append(content)
            out.append("")
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            # Save data URI to image file in temp directory so agy receives a clean path
                            # instead of hundreds of kilobytes of base64 text.
                            try:
                                img_dir = Path("/tmp/loopcut_agy_images")
                                img_dir.mkdir(parents=True, exist_ok=True)
                                img_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
                                ext = "png"
                                if "image/jpeg" in url or "image/jpg" in url:
                                    ext = "jpg"
                                elif "image/webp" in url:
                                    ext = "webp"
                                img_path = img_dir / f"image_{img_hash}.{ext}"
                                if not img_path.exists():
                                    _, b64_data = url.split(",", 1)
                                    img_path.write_bytes(base64.b64decode(b64_data))
                                out.append(f"[image: {img_path}]")
                            except Exception:
                                out.append("[image: (attached image)]")
                        elif url:
                            out.append(f"[image: {url}]")
                    else:
                        out.append(part.get("text", ""))
                elif isinstance(part, str):
                    out.append(part)
            out.append("")

        for call in message.get("tool_calls") or []:
            fn = call.get("function") or call
            name = fn.get("name", "")
            args = fn.get("arguments", "")
            cid = call.get("id") or fn.get("id") or ""
            id_str = f" (call_id: `{cid}`)" if cid else ""
            out.append(f"Called tool `{name}`{id_str} with arguments:")
            out.append(f"```json\n{args}\n```")
            out.append("")
    return out


def _render_tools(tools: list[dict]) -> str:
    """Format tool definitions into schema guidance for the model."""
    if not tools:
        return ""
    lines = [
        "# Available Tools & Autonomous Execution Guidelines",
        "",
        "You are Loopcut, the autonomous AI assistant operating inside the user's running Blender session.",
        "IMPORTANT: You are a model backend inside Blender. Do NOT execute local shell commands or host terminal tools.",
        "When you want to inspect, query, or modify the Blender scene, you MUST call one of the Loopcut tools listed below.",
        "",
        "## Autonomous Execution Mandate",
        "- When the user requests an action (such as fixing a model, cleaning geometry, unwrapping UVs, setting up shaders, framing a camera, or using installed addons):",
        "  * Carry out ALL necessary steps autonomously from start to finish.",
        "  * Do NOT stop after a single step or pause to ask conversational confirmation (e.g. 'Would you like me to do X next?' or 'Should I separate the materials?'). Doing so prematurely terminates your turn and leaves the task incomplete.",
        "  * Every assistant response MUST include a tool call until the entire task is finished, verified, and visually inspected.",
        "  * Only provide a final plain text summary when ALL requested work and verifications are 100% complete.",
        "",
        "## Multi-Stage Action Workflow (NO Stopping After Analysis)",
        "- When the user gives a multi-step prompt (e.g. 1. Inspect/analyze, 2. Clean geometry, 3. Sculpt, 4. Materials, 5. LOD export):",
        "  * You must NOT stop after step 1 or after taking an inspection render/backup.",
        "  * Proceed immediately to step 2, step 3, etc. in successive tool calls.",
        "  * Never output plain text without a tool call while any part of the requested modeling/refinement workflow remains undone.",
        "",
        "## Utilizing Installed Blender Add-ons",
        "- The scene context lists enabled Blender add-ons and extensions (e.g. Auto Unwrap 2, Smart Remesh, SpeedSculpt, AutoCam, etc.).",
        "- When relevant to the user's request, actively invoke the operators of these add-ons (`bpy.ops.<prefix>.*`) inside `run_python`.",
        "- If you need to check an add-on's exact operator names, arguments, or preferences, call `inspect_api` first, then run the operator.",
        "",
        "## Blender Performance & 30-Second Deadline",
        "- Code executed in `run_python` runs on Blender's main thread and has a strict 30-second timeout.",
        "- Never loop over tens of thousands of vertices or faces in pure Python. Use native C-level operations (`bpy.ops`, `bmesh.ops`) or numpy.",
        "- Never run deep recursive file searches or directory scans on the main thread.",
        "- Break heavy operations into focused, modular `run_python` steps instead of a single giant script.",
        "",
        "## Blender API Guidelines & Common Pitfalls",
        "- NEVER call `bpy.ops.render.render()` or `bpy.ops.render.play_rendered_anim()` inside `run_python`. In-process renders block Blender's main thread and are strictly refused by the cost guard. To render images or inspect scene appearance, use the `see_render` tool with `render=True`, use `capture_viewport`, or use `render_job` to write renders to disk.",
        "- `bpy.types.MeshEdge` does NOT have an `.is_manifold` attribute. To inspect boundary or non-manifold edges, use `bmesh` (`bm.edges[...].is_manifold`), or evaluate polygon edge use counts.",
        "- Color attributes: inspect `domain` ('CORNER' or 'POINT') and `data_type` ('BYTE_COLOR' or 'FLOAT_COLOR'). Access data via `obj.data.color_attributes[name].data[i].color`.",
        "",
        "## Tool Invocation Syntax",
        "CRITICAL: To execute Python code in Blender, invoke `run_python` via a JSON tool call OR output an executable ```python ... ``` code block. Do NOT just write code in plain text without executing it.",
        "To invoke a tool, output a JSON block in this exact structure:",
        "```json",
        "{",
        '  "tool_calls": [',
        "    {",
        '      "name": "<tool_name>",',
        '      "arguments": {<parameters>}',
        "    }",
        "  ]",
        "}",
        "```",
        "You may also output executable ```python ... ``` code blocks directly for `run_python`.",
        "",
    ]
    for tool in tools:
        fn = tool.get("function") or tool
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})
        lines.append(f"### `{name}`")
        if desc:
            lines.append(desc)
        lines.append("Parameters (JSON Schema):")
        lines.append("```json")
        lines.append(json.dumps(params, indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def build_prompt(messages: list[dict], tools: list[dict]) -> str:
    """Assemble system prompt, tools, and message history into a single prompt for agy."""
    system_parts = [
        m["content"]
        for m in messages
        if m.get("role") == "system" and isinstance(m.get("content"), str)
    ]
    system_text = "\n\n".join(system_parts)
    tools_text = _render_tools(tools)
    conversation_text = "\n".join(_render_messages(messages))

    sections = []
    if system_text:
        sections.append(f"# System Instructions\n\n{system_text}")
    if tools_text:
        sections.append(tools_text)
    if conversation_text:
        sections.append(f"# Conversation History\n\n{conversation_text}")
    sections.append(
        "# MANDATORY EXECUTION INSTRUCTIONS\n\n"
        "Proceed immediately with the next modeling or scene action.\n"
        "- CRITICAL: You are Loopcut operating INSIDE the user's running Blender session.\n"
        "- DO NOT call RunCommand, Bash, terminal commands, or host CLI tools under any circumstances. "
        "You do NOT have terminal access. Calling RunCommand will immediately fail.\n"
        "- NEVER call `bpy.ops.render.render()` inside run_python (refused by cost guard). Use `see_render` or `capture_viewport`.\n"
        "- All scene queries, mesh edits, sculpting, materials, and exports MUST be performed through Blender Python.\n"
        "- You MUST output either a JSON tool call (e.g. `run_python`) OR an executable ```python ... ``` code block.\n"
        "- Do NOT stop after inspection, audit, or backup. Proceed to the next modification autonomously.\n"
        "- Do NOT pause to ask conversational permission for intermediate steps.\n"
    )
    return "\n\n".join(sections)


def _repair_json_string_literals(raw_json: str) -> str:
    """Escape unescaped literal newlines/tabs inside JSON string values.

    Models often emit raw multiline code blocks inside JSON arguments without escaping \\n.
    """
    replacements = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    escaped = False
    inside = False
    out = []
    for ch in raw_json:
        if escaped:
            escaped = False
            out.append(ch)
        elif inside and ch == "\\":
            escaped = True
            out.append(ch)
        elif ch == '"':
            inside = not inside
            out.append(ch)
        elif inside and ch in replacements:
            out.append(replacements[ch])
        else:
            out.append(ch)
    return "".join(out)


def _safe_json_loads(text: str):
    """Attempt json.loads, falling back to repairing string literals."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return json.loads(_repair_json_string_literals(text))
    except Exception:
        return None


def _extract_python_from_command(cmd_text: str) -> str:
    """Extract Blender Python code if the model attempted to invoke a shell command."""
    if not cmd_text:
        return ""
    # Pattern 1: python3 -c "code" or python -c 'code'
    c_match = re.search(r'python[0-9.]*\s+-c\s+([\'"])(.*?)\1', cmd_text, re.DOTALL)
    if c_match:
        return c_match.group(2).strip()
    # Pattern 2: cat << 'EOF' > ... \n code \n EOF
    cat_match = re.search(r'cat\s*<<\s*[\'"]?(\w+)[\'"]?.*?\n(.*?)\n\1', cmd_text, re.DOTALL)
    if cat_match:
        return cat_match.group(2).strip()
    # Pattern 3: direct python script embedded in command
    if "bpy" in cmd_text or "bmesh" in cmd_text:
        lines = []
        for line in cmd_text.splitlines():
            s = line.strip()
            if s.startswith(("python", "cat ", "<<", "EOF")):
                continue
            lines.append(line)
        cleaned = "\n".join(lines).strip()
        if "bpy" in cleaned or "bmesh" in cleaned:
            return cleaned
        return cmd_text.strip()
    return ""


def _extract_calls_from_obj(data, allowed_names: set[str] | None = None) -> list[llm.ToolCall]:
    """Extract llm.ToolCall objects from parsed JSON data structures."""
    calls = []
    if isinstance(data, dict):
        if "tool_calls" in data and isinstance(data["tool_calls"], list):
            for item in data["tool_calls"]:
                calls.extend(_extract_calls_from_obj(item, allowed_names))
        else:
            has_explicit_tool_field = "tool" in data or "function" in data or "tool_name" in data
            has_args_field = "arguments" in data or "parameters" in data or "input" in data

            fn = data.get("function") if isinstance(data.get("function"), dict) else data
            name = fn.get("name") or fn.get("tool") or fn.get("tool_name") or data.get("name") or data.get("tool") or ""

            # Only accept if it has an explicit tool indicator, has arguments/parameters, or is a recognized tool name
            is_valid_tool = (
                has_explicit_tool_field
                or has_args_field
                or (allowed_names and name in allowed_names)
            )

            if isinstance(name, str) and name and is_valid_tool:
                args = (
                    fn.get("arguments")
                    or fn.get("parameters")
                    or fn.get("input")
                    or data.get("arguments")
                    or data.get("parameters")
                    or data.get("input")
                )
                if args is None:
                    args = {
                        k: v
                        for k, v in data.items()
                        if k not in ("name", "tool", "tool_name", "function", "id", "type", "tool_calls")
                    }

                # Intercept RunCommand / terminal shell calls containing Blender python
                if name.lower() in ("runcommand", "run_command", "bash", "execute_command", "terminal"):
                    cmd_text = ""
                    if isinstance(args, dict):
                        cmd_text = str(args.get("command") or args.get("CommandLine") or args.get("cmd") or "")
                    elif isinstance(args, str):
                        try:
                            parsed_a = json.loads(args)
                            if isinstance(parsed_a, dict):
                                cmd_text = str(parsed_a.get("command") or parsed_a.get("CommandLine") or parsed_a.get("cmd") or "")
                        except Exception:
                            cmd_text = args
                    code = _extract_python_from_command(cmd_text)
                    if code:
                        name = "run_python"
                        args = {"code": code, "summary": "Run Python script in Blender"}

                if allowed_names and name not in allowed_names and name != "run_python":
                    return calls

                if isinstance(args, dict):
                    arg_str = json.dumps(args)
                elif isinstance(args, str):
                    arg_str = args
                else:
                    arg_str = json.dumps(args) if args is not None else "{}"

                call_id = str(data.get("id") or fn.get("id") or f"call_{uuid.uuid4().hex[:9]}")
                arg_str = llm.normalize_tool_arguments(name, arg_str)
                calls.append(llm.ToolCall(id=call_id, name=name, arguments=arg_str))

    elif isinstance(data, list):
        for item in data:
            calls.extend(_extract_calls_from_obj(item, allowed_names))
    return calls


def _extract_python_calls(text: str) -> tuple[str, list[llm.ToolCall]]:
    """Detect markdown python code blocks and convert them to run_python tool calls.

    When models write Blender python scripts directly in markdown blocks (e.g. ```python ... ```),
    convert them to Loopcut run_python tool calls so the code actually executes in Blender.
    """
    tool_calls = []
    clean = text

    # Matches ```python ... ``` or ```py ... ``` or ```bpy ... ``` or untagged ``` containing bpy
    pattern = re.compile(r"```(?:python|py|bpy)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
    matches = list(pattern.finditer(text))

    for m in reversed(matches):
        code = m.group(1).strip()
        if not code:
            continue
        # Check if the code looks like executable python code (bpy, imports, functions, statements)
        is_blender_or_python = (
            "bpy" in code
            or "bmesh" in code
            or "mathutils" in code
            or ("import " in code and ("os" in code or "sys" in code or "math" in code or "numpy" in code or "np" in code))
            or ("def " in code and ("bpy" in code or "mesh" in code or "context" in code))
        )
        if is_blender_or_python:
            preceding = text[: m.start()].strip().splitlines()
            summary = "Run Python script in Blender"
            if preceding:
                last_line = preceding[-1].strip().lstrip("#-* >").strip()
                if last_line and len(last_line) < 120 and not last_line.endswith("```"):
                    summary = last_line

            call_id = f"call_{uuid.uuid4().hex[:9]}"
            args = json.dumps({"code": code, "summary": summary})
            tool_calls.insert(0, llm.ToolCall(id=call_id, name="run_python", arguments=args))
            clean = clean[:m.start()] + clean[m.end():]

    return clean.strip(), tool_calls


def _find_balanced_json_blocks(text: str):
    """Scan text for balanced {...} and [...] blocks and return list of (start_idx, end_idx, parsed_obj)."""
    results = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] in "{[":
            start = i
            open_ch = text[i]
            close_ch = "}" if open_ch == "{" else "]"
            depth = 0
            in_str = False
            escape = False
            for j in range(start, n):
                c = text[j]
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = not in_str
                elif not in_str:
                    if c == open_ch:
                        depth += 1
                    elif c == close_ch:
                        depth -= 1
                        if depth == 0:
                            candidate = text[start : j + 1]
                            parsed = _safe_json_loads(candidate)
                            if parsed is not None and isinstance(parsed, (dict, list)):
                                results.append((start, j + 1, parsed))
                                i = j
                            break
        i += 1
    return results


def parse_tool_calls(text: str, allowed_names: set[str] | None = None) -> tuple[str, list[llm.ToolCall]]:
    """Parse tool calls from the model response text.

    Supports:
    - Multiple ```json { "tool_calls": [...] } ``` blocks
    - Single tool call objects in JSON blocks: ```json { "name": "...", "arguments": {...} } ```
    - List of tool calls in JSON blocks: ```json [ { "name": ... }, ... ] ```
    - Raw JSON in text without markdown fence (via balanced brace scanner)
    - XML <tool_call>...</tool_call>, <tool_use>...</tool_use>, <invoke>...</invoke> tags
    - Markdown Python code blocks (```python ... ```) converted to run_python calls

    Returns (clean_text, tool_calls).
    """
    tool_calls = []
    clean_text = text.strip()

    # 1. Match all ```(?:json)? ... ``` code blocks
    json_block_pattern = re.compile(r"```(?:json)?\s*(\[\s*\{.*?\}\s*\]|\{.*?\})\s*```", re.DOTALL)
    for m in json_block_pattern.finditer(text):
        data = _safe_json_loads(m.group(1))
        if data is not None:
            calls = _extract_calls_from_obj(data, allowed_names)
            if calls:
                tool_calls.extend(calls)
                clean_text = clean_text.replace(m.group(0), "").strip()

    # 2. Match XML <tool_call>...</tool_call>, <tool_use>...</tool_use>, <invoke>...</invoke>
    xml_matches = list(re.finditer(r"<(?:tool_call|tool_use|invoke)>\s*(.*?)\s*</(?:tool_call|tool_use|invoke)>", text, re.DOTALL))
    for m in xml_matches:
        data = _safe_json_loads(m.group(1))
        if data is not None:
            calls = _extract_calls_from_obj(data, allowed_names)
            if calls:
                tool_calls.extend(calls)
                clean_text = clean_text.replace(m.group(0), "").strip()

    # 3. If no calls found yet, match raw balanced JSON objects/arrays in text
    if not tool_calls:
        balanced_blocks = _find_balanced_json_blocks(clean_text)
        for start_idx, end_idx, data in balanced_blocks:
            calls = _extract_calls_from_obj(data, allowed_names)
            if calls:
                tool_calls.extend(calls)
                matched_str = clean_text[start_idx:end_idx]
                clean_text = clean_text.replace(matched_str, "").strip()

    # 4. Check for markdown Python code blocks (run_python), extracting all executable blocks
    clean_after_python, python_calls = _extract_python_calls(clean_text)
    if python_calls:
        tool_calls.extend(python_calls)
        clean_text = clean_after_python

    # Ensure all tool calls have valid IDs and normalized arguments
    for call in tool_calls:
        if not call.id:
            call.id = f"call_{uuid.uuid4().hex[:9]}"
        call.arguments = llm.normalize_tool_arguments(call.name, call.arguments)

    return clean_text, tool_calls


class _Interrupted(Exception):
    def __init__(self, streamed: bool, message: str = ""):
        super().__init__(message)
        self.streamed = streamed
        self.message = message


def _is_transient_error(err_msg: str) -> bool:
    if not err_msg:
        return False
    msg = str(err_msg).lower()
    return any(
        term in msg
        for term in (
            "stream was interrupted",
            "stream interrupted",
            "interrupted",
            "connection reset",
            "connection closed",
            "broken pipe",
            "econnreset",
            "etimedout",
            "context canceled",
            "context deadline exceeded",
            "deadline exceeded",
            "bad gateway",
            "service unavailable",
            "502",
            "503",
            "504",
            "handshake failed",
            "tls connection",
            "socket hang up",
            "network error",
            "fetch failed",
        )
    )


def _stream_once(
    *,
    binary: str,
    target_model: str,
    effort: str,
    prompt: str,
    tools: list[dict],
    deadline: float | None,
    on_text: Callable[[str], None],
    is_cancelled: Callable[[], bool],
    on_progress: Callable[[], None] | None = None,
    on_retry: Callable[[], None] | None = None,
) -> llm.Completion:
    """Execute a single attempt of agy CLI subprocess streaming."""
    import queue
    import signal

    cmd = [
        binary,
        "--model",
        target_model,
        "--output-format",
        "stream-json",
        "--input-format",
        "text",
        "--disable-slash-commands",
        "--dangerously-skip-permissions",
        "--log-file",
        "/tmp/agy_loopcut.log",
    ]
    if effort and effort in EFFORTS:
        cmd.extend(["--effort", effort])

    pgid = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None
    except OSError as ex:
        raise llm.LLMError(f"Failed to start Antigravity CLI ('agy'): {ex}", code="not_running") from ex

    # Write prompt to stdin in background thread to prevent deadlocks
    def write_stdin():
        try:
            if proc.stdin:
                proc.stdin.write(prompt)
                proc.stdin.close()
        except Exception:
            pass

    stdin_thread = threading.Thread(target=write_stdin, daemon=True)
    stdin_thread.start()

    # Async readers for stdout and stderr to prevent deadlocks and blocking on inherited pipe FDs
    out_queue = queue.Queue()
    err_lines = []

    def stdout_reader():
        try:
            if proc.stdout and hasattr(proc.stdout, "readline"):
                for line in iter(proc.stdout.readline, ""):
                    if not isinstance(line, str):
                        break
                    out_queue.put(line)
        except Exception:
            pass
        finally:
            out_queue.put(None)  # Sentinel for EOF

    def stderr_reader():
        try:
            if proc.stderr and hasattr(proc.stderr, "readline"):
                for line in iter(proc.stderr.readline, ""):
                    if not isinstance(line, str):
                        break
                    err_lines.append(line)
                    if len(err_lines) > 200:
                        err_lines.pop(0)
        except Exception:
            pass

    stdout_thread = threading.Thread(target=stdout_reader, daemon=True)
    stdout_thread.start()

    stderr_thread = threading.Thread(target=stderr_reader, daemon=True)
    stderr_thread.start()

    completion = llm.Completion()
    full_text_chunks = []
    streamed_to_ui = []
    in_tool_block = False

    def kill_proc():
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=0.3)
        except Exception:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    pass
            try:
                proc.kill()
            except Exception:
                pass

    try:
        while True:
            if is_cancelled():
                kill_proc()
                raise llm.Cancelled()

            if deadline and time.monotonic() > deadline:
                kill_proc()
                completion.finish_reason = "length"
                completion.timed_out = True
                break

            try:
                line = out_queue.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None and out_queue.empty():
                    time.sleep(0.05)
                    if out_queue.empty():
                        break
                continue

            if line is None:
                # EOF reached on stdout
                break

            line = line.strip()
            if not line:
                continue

            # Signal progress on data arrival to prevent false stall notifications
            if on_progress:
                on_progress()

            try:
                event_data = json.loads(line)
            except Exception:
                continue

            event_type = event_data.get("event")

            if event_type == "step_update":
                su = event_data.get("step_update", {})
                delta = su.get("text_delta", "")
                if delta:
                    full_text_chunks.append(delta)

                    # Check if entering a tool block to avoid showing raw JSON or code to user
                    accumulated = "".join(full_text_chunks)
                    if (
                        "```json" in accumulated
                        or '{"tool_calls"' in accumulated
                        or "<tool_call>" in accumulated
                        or "<tool_use>" in accumulated
                        or "<invoke>" in accumulated
                        or "```python" in accumulated
                        or "```bpy" in accumulated
                    ):
                        in_tool_block = True

                    if not in_tool_block:
                        streamed_to_ui.append(delta)
                        on_text(delta)

            elif event_type == "result":
                res = event_data.get("result", {})
                status = res.get("status")
                err_msg = res.get("error", "")
                if status == "ERROR":
                    kill_proc()
                    if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
                        raise llm.LLMError(
                            f"Antigravity CLI rate limit or quota reached: {err_msg}",
                            status=429,
                            code="rate_limited",
                        )
                    if _is_transient_error(err_msg):
                        raise _Interrupted(bool(streamed_to_ui), err_msg)
                    raise llm.LLMError(f"Antigravity CLI error: {err_msg}", code="error")

                usage = res.get("usage", {})
                if usage:
                    completion.usage = {
                        "input": usage.get("input_tokens", 0),
                        "output": usage.get("output_tokens", 0),
                        "cached": usage.get("cache_read_tokens", 0),
                    }
                    if "thinking_tokens" in usage:
                        completion.usage["reasoning"] = usage["thinking_tokens"]

    except Exception:
        kill_proc()
        raise

    try:
        proc.wait(timeout=1.0)
    finally:
        kill_proc()

    return_code = proc.returncode
    stderr_output = "".join(str(l) for l in err_lines if isinstance(l, str)).strip()

    if return_code != 0 and not full_text_chunks:
        if "RESOURCE_EXHAUSTED" in stderr_output or "429" in stderr_output or "quota" in stderr_output.lower():
            raise llm.LLMError(
                f"Antigravity CLI quota reached: {stderr_output}",
                status=429,
                code="rate_limited",
            )
        if _is_transient_error(stderr_output):
            raise _Interrupted(bool(streamed_to_ui), stderr_output)
        raise llm.LLMError(
            f"Antigravity CLI exited with code {return_code}: {stderr_output}",
            code="error",
        )

    if not full_text_chunks and _is_transient_error(stderr_output):
        raise _Interrupted(bool(streamed_to_ui), stderr_output)

    raw_text = "".join(full_text_chunks)
    allowed_names = {(t.get("function") or t).get("name", "") for t in tools} if tools else set()
    clean_text, tool_calls = parse_tool_calls(raw_text, allowed_names if tools else set())

    if tool_calls:
        # If tool calls were extracted and we already streamed something to UI,
        # reset the UI if on_retry is available, then display only clean text.
        if on_retry and (in_tool_block or streamed_to_ui):
            on_retry()
            if clean_text:
                on_text(clean_text)
        completion.tool_calls = tool_calls
        completion.finish_reason = "tool_calls"
        completion.text = clean_text
    else:
        completion.text = raw_text
        if not completion.finish_reason:
            completion.finish_reason = "stop"

    return completion


def stream(
    *,
    model: str,
    messages: list[dict],
    tools: list[dict],
    reasoning_effort: str,
    on_text: Callable[[str], None],
    is_cancelled: Callable[[], bool],
    call_max_seconds: float | None = None,
    headers: dict | None = None,
    on_retry: Callable[[], None] | None = None,
    on_progress: Callable[[], None] | None = None,
) -> llm.Completion:
    """Stream model response using agy CLI with NDJSON stream-json protocol and automatic retries."""
    binary = find_agy_binary()
    if not binary:
        raise llm.LLMError(
            "Antigravity CLI binary 'agy' not found. Ensure it is installed and in PATH, "
            "or set the LOOPCUT_AGY_PATH environment variable.",
            code="not_running",
        )

    effort = (reasoning_effort or config.AGY_EFFORT).strip().lower()
    target_model = resolve_model(model or config.AGY_MODEL, effort)
    prompt = build_prompt(messages, tools)

    deadline = time.monotonic() + call_max_seconds if call_max_seconds else None
    delays = llm.RETRY_DELAYS
    attempt = 0
    retried_partial = False

    while True:
        try:
            return _stream_once(
                binary=binary,
                target_model=target_model,
                effort=effort,
                prompt=prompt,
                tools=tools,
                deadline=deadline,
                on_text=on_text,
                is_cancelled=is_cancelled,
                on_progress=on_progress,
                on_retry=on_retry,
            )
        except _Interrupted as ex:
            shown = ex.message or "the stream was interrupted"
            if ex.streamed:
                if retried_partial or on_retry is None:
                    raise llm.LLMError(
                        f"Antigravity CLI connection lost while streaming the reply: {shown}",
                        code=llm.CONNECTION_LOST,
                    ) from ex
                retried_partial = True
                print(f"Loopcut: retrying Antigravity CLI request once from scratch after stream interrupted ({shown})")
                on_retry()
                continue
            if attempt >= len(delays):
                raise llm.LLMError(
                    f"Antigravity CLI connection lost before reply: {shown}",
                    code=llm.CONNECTION_LOST,
                ) from ex
            delay = delays[attempt]
            if deadline:
                delay = min(delay, max(0.0, deadline - time.monotonic()))
            attempt += 1
            print(f"Loopcut: retrying Antigravity CLI request ({attempt}/{len(delays)}, waiting {delay:g}s) after: {shown}")
            llm._wait(delay, is_cancelled)
