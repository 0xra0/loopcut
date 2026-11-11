"""Resumable long-running task manager for Loopcut.

Persists pipeline stage progression, intermediate geometry checkpoints,
and diagnostic metrics to disk so crashes or disconnections do not require
restarting multi-stage workflows from scratch.
"""

import json
from pathlib import Path
from typing import Any

TASK_STATE_DIR = Path("/tmp/loopcut_tasks")


def _get_task_file(task_id: str) -> Path:
    TASK_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return TASK_STATE_DIR / f"{task_id}.json"


def init_task(task_id: str, stages: list[str]) -> dict:
    """Initialize a multi-stage task workflow."""
    state = {
        "task_id": task_id,
        "stages": stages,
        "current_stage_idx": 0,
        "completed_stages": [],
        "artifacts": {},
        "status": "in_progress",
    }
    _get_task_file(task_id).write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def complete_stage(task_id: str, stage_name: str, stage_data: dict | None = None) -> dict:
    """Mark a stage as completed and persist intermediate results."""
    tf = _get_task_file(task_id)
    if not tf.is_file():
        state = init_task(task_id, [stage_name])
    else:
        state = json.loads(tf.read_text(encoding="utf-8"))

    if stage_name not in state["completed_stages"]:
        state["completed_stages"].append(stage_name)
    if stage_data:
        state["artifacts"][stage_name] = stage_data

    # Advance stage index
    if state["stages"] and stage_name in state["stages"]:
        idx = state["stages"].index(stage_name)
        state["current_stage_idx"] = min(len(state["stages"]) - 1, idx + 1)

    if len(state["completed_stages"]) == len(state["stages"]):
        state["status"] = "completed"

    tf.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def resume_task(task_id: str) -> dict:
    """Retrieve the last saved stage state to resume execution."""
    tf = _get_task_file(task_id)
    if not tf.is_file():
        return {"status": "not_found", "task_id": task_id}
    return json.loads(tf.read_text(encoding="utf-8"))
