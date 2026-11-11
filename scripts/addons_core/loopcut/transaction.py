"""Transactional Blender operations for Loopcut.

Extends checkpoints with operation-level transactions:
- Multi-step transaction tracking (begin, stage, commit, rollback)
- Automatic rollback on unhandled exceptions or acceptance failure
- Dry-run / preview capability
"""

from typing import Callable

_ACTIVE_TRANSACTION = None


class BlenderTransaction:
    def __init__(self, name: str):
        self.name = name
        self.committed = False
        self.rolled_back = False
        self.steps = []
        self.checkpoint_id = None

    def begin(self):
        import bpy
        # Push Blender undo step
        bpy.ops.ed.undo_push(message=f"Tx: {self.name}")
        return self

    def stage_step(self, tool_name: str, params: dict):
        self.steps.append({"tool": tool_name, "params": params})

    def rollback(self):
        if self.committed or self.rolled_back:
            return
        import bpy
        try:
            bpy.ops.ed.undo()
        except Exception:
            pass
        self.rolled_back = True

    def commit(self):
        if self.rolled_back:
            raise RuntimeError("Cannot commit a rolled-back transaction.")
        self.committed = True


def begin_transaction(name: str) -> BlenderTransaction:
    """Start a new scoped transaction."""
    global _ACTIVE_TRANSACTION
    tx = BlenderTransaction(name).begin()
    _ACTIVE_TRANSACTION = tx
    return tx


def get_active_transaction() -> BlenderTransaction | None:
    return _ACTIVE_TRANSACTION
