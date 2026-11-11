"""Project constraints (target engine, polygon budget, protected objects...) kept in the .blend.

Stored as JSON in a scene custom property, so they are saved with the file, come back with it after
a restart, and never leak into another file. Only what was set is kept: nothing is assumed.
Main thread only.
"""

import json
from typing import Any

MEMORY_PROP_KEY = "loopcut_project_memory"


def load_memory() -> dict:
    """The constraints set for the open file; {} when none were."""
    import bpy
    raw = bpy.context.scene.get(MEMORY_PROP_KEY)
    if not raw:
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def save_memory(mem: dict) -> None:
    import bpy
    bpy.context.scene[MEMORY_PROP_KEY] = json.dumps(mem, ensure_ascii=False)


def get_constraint(key: str, default: Any = None) -> Any:
    return load_memory().get(key, default)


def set_constraint(key: str, value: Any) -> dict:
    """Set one constraint and save it with the scene. Saving the .blend keeps it."""
    mem = load_memory()
    mem[key] = value
    save_memory(mem)
    return mem


def add_protected_object(name: str) -> dict:
    """Mark an object as protected from deletion or destructive edits."""
    mem = load_memory()
    mem["protected_objects"] = sorted(set(mem.get("protected_objects", [])) | {name})
    save_memory(mem)
    return mem


def is_object_protected(name: str) -> bool:
    return name in load_memory().get("protected_objects", [])


def summary_for_context() -> str | None:
    """One line for the scene context, or None when nothing was set."""
    mem = load_memory()
    parts = []
    if mem.get("target_engine"):
        parts.append(f"engine: {mem['target_engine']}")
    if mem.get("poly_budget"):
        parts.append(f"poly budget: {mem['poly_budget']} tris")
    if mem.get("protected_objects"):
        parts.append(f"protected: {', '.join(mem['protected_objects'])}")
    if mem.get("preserved_geometry"):
        parts.append(f"preserve: {', '.join(mem['preserved_geometry'])}")
    others = {k: v for k, v in mem.items()
              if k not in ("target_engine", "poly_budget", "protected_objects", "preserved_geometry") and v}
    if others:
        parts.append(", ".join(f"{k}: {v}" for k, v in others.items()))
    return "project constraints: " + "; ".join(parts) if parts else None
