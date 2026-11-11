"""Targeted tool-argument validation, repair advice, and scene context formatting.

Helps the model correct invalid tool calls directly against tool schemas and scene
state without resetting conversations or falling back to running unstructured chat code.
"""

import json
from typing import Any

from . import llm


def validate_tool_arguments(name: str, arguments_str: str, schemas: list[dict] | None = None) -> tuple[bool, str, dict]:
    """Validate raw tool arguments against JSON syntax and tool schema definitions.

    Returns (is_valid, error_reason, parsed_dict).
    """
    if not isinstance(arguments_str, str):
        return False, f"Expected arguments string, got {type(arguments_str).__name__}", {}

    normalized = llm.normalize_tool_arguments(name, arguments_str)
    try:
        parsed = json.loads(normalized) if normalized.strip() else {}
    except (ValueError, json.JSONDecodeError) as ex:
        return False, f"Malformed JSON arguments: {ex}. Ensure strings are closed and control characters escaped.", {}

    if not isinstance(parsed, dict):
        return False, f"Tool arguments must be a JSON dictionary of key-value pairs, got {type(parsed).__name__}", {}

    if not schemas:
        return True, "", parsed

    schema_fn = None
    for s in schemas:
        fn = s.get("function") if isinstance(s, dict) and "function" in s else s
        if isinstance(fn, dict) and fn.get("name") == name:
            schema_fn = fn
            break

    if not schema_fn:
        return True, "", parsed

    params = schema_fn.get("parameters") or {}
    properties = params.get("properties") or {}
    required = params.get("required") or []

    for req_field in required:
        if req_field not in parsed:
            prop_names = list(properties.keys())
            return False, f"Missing required parameter {req_field!r} for tool {name!r}. Expected parameters: {prop_names}", parsed

    type_checks = {
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, (list, tuple)),
        "object": lambda v: isinstance(v, dict),
    }

    for key, val in parsed.items():
        if key in properties and "type" in properties[key]:
            expected_type = properties[key]["type"]
            check_fn = type_checks.get(expected_type)
            if check_fn and not check_fn(val):
                return False, f"Invalid type for parameter {key!r}: expected {expected_type}, got {type(val).__name__}", parsed

    return True, "", parsed


def get_scene_state_summary() -> str:
    """Summarize active Blender scene state for tool correction context."""
    try:
        import bpy
        context = getattr(bpy, "context", None)
        if context is None:
            return "Blender context unavailable."
        mode = getattr(context, "mode", "UNKNOWN")
        active = getattr(context, "active_object", None)
        selected = getattr(context, "selected_objects", []) or []

        parts = [f"Mode: {mode}"]
        if active is not None:
            name = getattr(active, "name", "Unnamed")
            obj_type = getattr(active, "type", "UNKNOWN")
            data = getattr(active, "data", None)
            verts = len(getattr(data, "vertices", [])) if hasattr(data, "vertices") else None
            active_desc = f"Active: '{name}' ({obj_type}" + (f", {verts} verts)" if verts is not None else ")")
            parts.append(active_desc)
        else:
            parts.append("Active: None")

        sel_names = [getattr(o, "name", "Unnamed") for o in selected[:5]]
        if len(selected) > 5:
            sel_names.append(f"... (+{len(selected) - 5} more)")
        parts.append(f"Selected: [{', '.join(sel_names)}]")
        return "; ".join(parts)
    except (AttributeError, RuntimeError, KeyError):
        return "Blender scene context unavailable."


def build_argument_repair_prompt(name: str, error_message: str, schema_dict: dict | None = None, scene_summary: str = "") -> str:
    """Format targeted schema validation repair advice for Swift/assistant."""
    lines = [
        f"Schema validation failed for tool '{name}': {error_message}",
    ]
    if schema_dict:
        params = schema_dict.get("parameters") or {}
        props = list((params.get("properties") or {}).keys())
        req = params.get("required") or []
        lines.append(f"Expected parameters: {props} (required: {req})")

    summary = scene_summary or get_scene_state_summary()
    lines.append(f"Current scene state: {summary}")
    lines.append(f"Please correct the arguments for '{name}' and call it again with valid parameters.")
    lines.append("Important: Emit your action as a structured tool call. Do not write raw Python code in plain chat text.")
    return "\n".join(lines)


def format_tool_error_with_scene(name: str, error_message: str, schema_dict: dict | None = None) -> str:
    """Enrich tool error output with scene context for targeted correction."""
    return build_argument_repair_prompt(name, error_message, schema_dict)
