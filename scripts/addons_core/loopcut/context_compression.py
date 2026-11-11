"""Adaptive context and tool-schema compression for Loopcut.

Reduces unnecessary context token usage for local models:
- Deduplicates repeated scene descriptions using checksum hashing
- Truncates verbose tabular outputs with concise summaries
- Compresses tool argument schemas
"""

import hashlib
import json

_LAST_CONTEXT_HASH = ""


def hash_context_payload(payload: str) -> str:
    """Generate SHA256 hash of a context string."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def compress_scene_context_if_unchanged(new_context: str) -> str:
    """If the scene context has not changed since the last turn, replace with a 1-line reference."""
    global _LAST_CONTEXT_HASH
    curr_hash = hash_context_payload(new_context)
    if curr_hash == _LAST_CONTEXT_HASH:
        return "[Scene context unchanged since previous step]"
    _LAST_CONTEXT_HASH = curr_hash
    return new_context


def compact_tool_output(output_text: str, max_lines: int = 25, max_chars: int = 1500) -> str:
    """Truncate excessively long tool execution output while preserving essential head and tail lines."""
    if len(output_text) <= max_chars:
        return output_text

    lines = output_text.splitlines()
    if len(lines) <= max_lines:
        return output_text[:max_chars] + "\n... [truncated]"

    half = max_lines // 2
    head = lines[:half]
    tail = lines[-half:]
    omitted = len(lines) - len(head) - len(tail)

    return "\n".join(head) + f"\n... [{omitted} lines omitted] ...\n" + "\n".join(tail)
