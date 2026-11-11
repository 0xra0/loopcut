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
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, json.JSONDecodeError):
        return {}


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


def set_target_export_format(fmt: str) -> dict:
    """Set target 3D export format (e.g. 'glb', 'fbx', 'obj')."""
    return set_constraint("target_export_format", str(fmt).strip().lower())


def get_target_export_format() -> str | None:
    """Get target 3D export format."""
    return get_constraint("target_export_format")


def record_completed_stage(pipeline: str, stage: str, details: str = "") -> dict:
    """Record a completed stage of a long multi-step pipeline."""
    mem = load_memory()
    stages = mem.setdefault("completed_stages", {})
    pipe_stages = stages.setdefault(pipeline, [])
    if not any(isinstance(s, dict) and s.get("stage") == stage for s in pipe_stages):
        pipe_stages.append({"stage": stage, "details": details})
    save_memory(mem)
    return mem


def get_completed_stages(pipeline: str) -> list[dict]:
    """Get completed stages for a given pipeline name."""
    mem = load_memory()
    return mem.get("completed_stages", {}).get(pipeline, [])


def is_stage_completed(pipeline: str, stage: str) -> bool:
    """Check if a specific pipeline stage has already been completed."""
    completed = get_completed_stages(pipeline)
    return any(isinstance(s, dict) and s.get("stage") == stage for s in completed)


def resume_pipeline(pipeline: str, all_stages: list[str]) -> list[str]:
    """Return remaining uncompleted stages for a pipeline after restart/crash."""
    completed_names = {s.get("stage") for s in get_completed_stages(pipeline) if isinstance(s, dict)}
    return [stage for stage in all_stages if stage not in completed_names]


def record_failed_approach(approach_key: str, reason: str = "") -> dict:
    """Record an unsuccessful approach to prevent cycling through identical failed attempts."""
    mem = load_memory()
    failed = mem.setdefault("failed_approaches", [])
    if not any(isinstance(f, dict) and f.get("approach") == approach_key for f in failed):
        failed.append({"approach": approach_key, "reason": reason})
    save_memory(mem)
    return mem


def get_failed_approaches() -> list[dict]:
    """Get recorded failed approaches."""
    return load_memory().get("failed_approaches", [])


def should_avoid_approach(approach_key: str) -> bool:
    """Check if an approach has already been tried and failed."""
    failed = get_failed_approaches()
    return any(isinstance(f, dict) and f.get("approach") == approach_key for f in failed)


def summary_for_context() -> str | None:
    """One line for the scene context, or None when nothing was set."""
    mem = load_memory()
    parts = []
    if mem.get("target_engine"):
        parts.append(f"engine: {mem['target_engine']}")
    if mem.get("target_export_format"):
        parts.append(f"export: {mem['target_export_format']}")
    if mem.get("poly_budget"):
        parts.append(f"poly budget: {mem['poly_budget']} tris")
    if mem.get("protected_objects"):
        parts.append(f"protected: {', '.join(mem['protected_objects'])}")
    if mem.get("preserved_geometry"):
        parts.append(f"preserve: {', '.join(mem['preserved_geometry'])}")

    completed_stages = mem.get("completed_stages") or {}
    if completed_stages:
        stage_counts = [f"{k}: {len(v)}" for k, v in completed_stages.items() if isinstance(v, list) and v]
        if stage_counts:
            parts.append(f"stages completed ({', '.join(stage_counts)})")

    failed = mem.get("failed_approaches") or []
    if failed:
        avoid_keys = [f["approach"] for f in failed[:3] if isinstance(f, dict) and f.get("approach")]
        if avoid_keys:
            parts.append(f"avoid: {', '.join(avoid_keys)}")

    others = {k: v for k, v in mem.items()
              if k not in ("target_engine", "target_export_format", "poly_budget", "protected_objects",
                           "preserved_geometry", "completed_stages", "failed_approaches") and v}
    if others:
        parts.append(", ".join(f"{k}: {v}" for k, v in others.items()))
    return "project constraints: " + "; ".join(parts) if parts else None
