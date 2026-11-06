"""Scene checkpoints: restore the scene and the conversation to before any message.

The rule everything here serves: a restore never destroys work. Restoring first snapshots the
current state (including manual edits made since), so a restore is itself a checkpoint and can
be undone. The user's own .blend on disk is never written to.

A snapshot is a compressed copy-save of the whole .blend. One is taken per user message, lazily,
right before the first scene-changing step of that turn, so chat-only turns cost nothing, and one
before each merge of a wave of workers (kind "wave"), which restores to before that wave was asked for.
A cloud render's bundle is collected from its own snapshot of the live scene (kind "bundle"), which is
dropped once collected and never offered as a restore point.

In the Loopcut build of Blender a snapshot is a recovery file (wm.loopcut_snapshot_write): it
records the path of the file it was taken from, so restoring it (wm.loopcut_snapshot_restore)
puts the scene back in place. The open file keeps its path and is marked unsaved.

Stock Blender cannot do that from Python: opening a snapshot leaves Blender's file path pointing
at it, so the user's next Ctrl+S would land in our store. There restore() re-saves straight away
as `<name>.restored-<time>.blend` beside the original. Index entries say which kind they are
(`in_place`), so a store written by one Blender is still restored correctly by the other.
"""

import json
import errno
import contextlib
import os
import re
import shutil
import time
import threading
import functools
import uuid
from pathlib import Path

INDEX_NAME = "index.json"
DEFAULT_BUDGET_MB = 2048
DEFAULT_STORAGE_MB = 10240
FREE_RESERVE = 64 * 1024 * 1024
STALE_SESSION_DAYS = 30
KEEP_NEWEST = 5
_STORAGE_LOCK = threading.RLock()
_STORAGE_SIZES = {}
_STORAGE_ERRORS = {}
_CLEANUP_THREAD = None


def _storage_locked(function):
    @functools.wraps(function)
    def locked(*args, **kwargs):
        with _STORAGE_LOCK:
            return function(*args, **kwargs)
    return locked


def cached_storage_size() -> int | None:
    key = str(data_root())
    if key in _STORAGE_ERRORS:
        raise OSError(_STORAGE_ERRORS[key])
    return _STORAGE_SIZES.get(key)


def storage_scan_running() -> bool:
    return _CLEANUP_THREAD is not None and _CLEANUP_THREAD.is_alive()


_ID = re.compile(r"^[0-9a-f]{32}$")
_RESTORED_SUFFIX = re.compile(r"\.restored-\d{8}-\d{6}(-\d+)?$")


class CheckpointError(Exception):
    pass


class StorageFullError(CheckpointError):
    pass


def storage_budget() -> int:
    try:
        from . import settings
        return int(getattr(settings.preferences(), "storage_budget_mb", DEFAULT_STORAGE_MB)) * 1024 * 1024
    except (AttributeError, ImportError):
        return DEFAULT_STORAGE_MB * 1024 * 1024


def storage_size(path: Path | None = None) -> int:
    """Count owned regular files without following symlinks."""
    path = data_root() if path is None else path
    total = 0
    for folder, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(folder) / d).is_symlink()]
        for name in names:
            child = Path(folder) / name
            if not child.is_symlink():
                with contextlib.suppress(OSError):
                    total += child.stat().st_size
    return total


def _disposable_folder(folder: Path, name: str, protected: set[Path]) -> bool:
    if folder.is_symlink() or not folder.is_dir():
        return False
    # A stopped cleanup can leave a folder already retired from its working path.
    if re.fullmatch(r"\.cleanup-[0-9a-f]{32}", folder.name):
        return True
    if not _ID.fullmatch(folder.name):
        return False
    resolved = folder.resolve()
    if any(p == resolved or resolved in p.parents for p in protected):
        return False
    try:
        if name == "workspaces":
            from . import jobs
            owner = json.loads((folder / ".loopcut" / "owner.json").read_text(encoding="utf-8"))
            return not (type(owner.get("pid")) is int and jobs._alive(owner["pid"]))
        status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
        return (status.get("state") in ("complete", "cancelled", "failed", "interrupted")
                and not (folder / ".lease").exists() and not (folder / "lease").exists())
    except (OSError, ValueError, AttributeError):
        return name == "workspaces"


def _kept_checkpoints(store: "Store", active_id: str) -> set[str]:
    active = [e for e in store.entries if not e["expired"]]
    keep = ({e["id"] for e in active[:1] + active[-KEEP_NEWEST:]}
            if store.folder.name == active_id else set())
    undo = next((e["id"] for e in reversed(active) if e["kind"] == "pre_restore"), None)
    return keep | {undo} | {e["id"] for e in active if e["kind"] == "bundle"}


def cleanup_storage(*, budget_bytes: int | None = None, now: float | None = None,
                    protect: tuple[Path, ...] = (), reclaim_bytes: int = 0,
                    root: Path | None = None, active_session: dict | None = None,
                    require_reclaim: bool = False) -> dict:
    """Scan without holding the storage lock; serialize each index update or folder retirement.

    A copy preflight must be able to reclaim its full shortfall before deleting anything.
    Conversation text, live work and restore undo remain protected even above the budget.
    """
    root = data_root() if root is None else root
    budget = storage_budget() if budget_bytes is None else max(0, budget_bytes)
    cutoff = (time.time() if now is None else now) - STALE_SESSION_DAYS * 86400
    from . import state
    session = state.session() if active_session is None else active_session
    protected = {p.resolve() for p in protect}
    if session.get("mode") == "workspace":
        protected.add((root / "workspaces" / session["id"]).resolve())
    candidates = []
    for name in ("workspaces", "jobs"):
        parent = root / name
        if not parent.is_dir() or parent.is_symlink():
            continue
        for folder in parent.iterdir():
            if _disposable_folder(folder, name, protected):
                candidates.append((folder.stat().st_mtime, folder, None, None))
    parent = root / "checkpoints"
    if parent.is_dir() and not parent.is_symlink():
        for folder in parent.iterdir():
            if folder.is_symlink() or not folder.is_dir() or not _ID.fullmatch(folder.name):
                continue
            try:
                with _STORAGE_LOCK:
                    store = Store(root, folder.name)
                    keep = _kept_checkpoints(store, session["id"])
                for entry in store.entries:
                    path = store.snapshot_path(entry["id"])
                    if not entry["expired"] and entry["id"] not in keep and path.resolve() not in protected:
                        candidates.append((entry.get("created", folder.stat().st_mtime), path, folder.name, entry["id"]))
                indexed = {p.name for e in store.entries if not e["expired"]
                           for p in (store.snapshot_path(e["id"]), store.tail_path(e["id"]))}
                for path in folder.iterdir():
                    if (path.is_symlink() or not path.is_file() or path.name in indexed
                            or path.resolve() in protected):
                        continue
                    if re.fullmatch(r"[0-9a-f]{32}\.(blend|tail\.json)", path.name) and path.stat().st_mtime < cutoff:
                        candidates.append((path.stat().st_mtime, path, folder.name, None))
            except (OSError, ValueError, KeyError, TypeError):
                continue
    total = storage_size(root)
    sized = []
    for stamp, path, conversation, checkpoint in candidates:
        size = storage_size(path) if conversation is None else 0
        if conversation is not None:
            for file in (path, path.with_suffix(".tail.json")) if checkpoint else (path,):
                if not file.is_symlink() and file.is_file():
                    size += file.stat().st_size
        sized.append((stamp, path, conversation, checkpoint, size))
    if require_reclaim and sum(row[4] for row in sized) < reclaim_bytes:
        raise StorageFullError("Disk is full: disposable copies cannot free enough space for this copy.")
    budget = min(budget, max(0, total - reclaim_bytes)) if reclaim_bytes else budget
    removed = 0
    for stamp, path, conversation, checkpoint, size in sorted(sized, key=lambda c: c[0]):
        # Stale unindexed files can always be cleared after the all-or-nothing preflight.
        if total <= budget and not (conversation is not None and checkpoint is None):
            continue
        retired = None
        try:
            with _STORAGE_LOCK:
                current = state.session()
                if current.get("mode") == "workspace":
                    protected.add((root / "workspaces" / current["id"]).resolve())
                if conversation is None:
                    if not _disposable_folder(path, path.parent.name, protected):
                        continue
                    retired = path.with_name(".cleanup-" + uuid.uuid4().hex)
                    path.rename(retired)
                else:
                    store = Store(root, conversation)
                    if checkpoint:
                        entry = store.get(checkpoint)
                        if (entry["expired"] or checkpoint in _kept_checkpoints(store, current["id"])
                                or path.resolve() in protected):
                            continue
                        entry["expired"] = True
                        store.save_index()
                        path.unlink(missing_ok=True)
                        store.tail_path(checkpoint).unlink(missing_ok=True)
                    else:
                        live = {p for e in store.entries if not e["expired"]
                                for p in (store.snapshot_path(e["id"]), store.tail_path(e["id"]))}
                        if path in live or path.resolve() in protected:
                            continue
                        path.unlink(missing_ok=True)
            if retired is not None:
                shutil.rmtree(retired)  # Recursive filesystem work never holds the storage lock.
            total -= size
            removed += size
        except (OSError, CheckpointError):
            if retired is not None and retired.exists():
                with _STORAGE_LOCK:
                    if not path.exists():
                        retired.rename(path)
            continue
    remaining = storage_size(root)
    _STORAGE_SIZES[str(root)] = remaining
    _STORAGE_ERRORS.pop(str(root), None)
    return {"bytes": remaining, "removed": removed, "budget": budget}


@_storage_locked
def require_space(destination: Path, size: int | None = None, *, protect: tuple[Path, ...] = ()) -> None:
    """Prune disposable storage to its budget; refuse a copy only for insufficient disk space."""
    if size is None:
        import bpy
        source = Path(bpy.data.filepath) if bpy.data.filepath else None
        size = source.stat().st_size if source is not None and source.is_file() else FREE_RESERVE
    parent = destination.parent
    while not parent.exists():
        if parent == parent.parent:
            raise StorageFullError(f"The storage volume is unavailable: {destination.anchor}")
        parent = parent.parent
    needed = size + FREE_RESERVE
    try:
        free = shutil.disk_usage(parent)[2]
    except OSError as ex:
        raise StorageFullError(f"The storage volume is unavailable: {parent}: {ex}") from ex
    shortfall = max(0, needed - free)
    root = data_root()
    if shortfall and root.exists() and root.stat().st_dev != parent.stat().st_dev:
        raise StorageFullError("Disk is full: Loopcut copies are on a different volume and cannot free space here.")
    cleanup_storage(protect=protect, reclaim_bytes=shortfall, require_reclaim=bool(shortfall))
    try:
        free = shutil.disk_usage(parent)[2]
    except OSError as ex:
        raise StorageFullError(f"The storage volume is unavailable: {parent}: {ex}") from ex
    if free < needed:
        raise StorageFullError("Disk is full: there is not enough free space for a scene copy and conversation saves.")


def storage_notice(session: dict, cause: Exception | None = None) -> None:
    from . import state
    if not any(i.get("storage") for i in session["items"]):
        unavailable = cause is not None and "unavailable" in str(cause).lower()
        reason = ("Storage volume is unavailable. Reconnect it or choose an available folder. "
                  if unavailable else "Disk is full. Free space, clean up copies, or lower the storage budget in Preferences. ")
        session["items"].append({**state.item_notice(
            reason + "Checkpoints are paused. You can continue without new checkpoints; "
            "those edits cannot be restored from chat."), "storage": True})


# ------------------------------------------------------------------ pure logic (no bpy)

def restored_path(original: str, fallback_dir: Path, now: float, exists=os.path.exists) -> Path:
    """Where a restored scene gets saved. Never an existing file, never inside the store."""
    if original:
        source = Path(original)
        folder, stem = source.parent, _RESTORED_SUFFIX.sub("", source.stem)
    else:
        folder, stem = fallback_dir, "untitled"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    candidate = folder / f"{stem}.restored-{stamp}.blend"
    counter = 1
    while exists(candidate):
        counter += 1
        candidate = folder / f"{stem}.restored-{stamp}-{counter}.blend"
    return candidate


def plan_pruning(entries: list[dict], budget_bytes: int) -> list[str]:
    """Ids to expire, oldest first, until the live snapshots fit the budget.

    Protected: the first checkpoint (the state before Loopcut touched anything), the newest
    KEEP_NEWEST, and the latest pre-restore snapshot (the only way back from the last restore).
    """
    live = [e for e in entries if not e["expired"]]
    total = sum(e["size"] for e in live)
    if total <= budget_bytes:
        return []
    protected = {e["id"] for e in live[:1]} | {e["id"] for e in live[-KEEP_NEWEST:]}
    pre_restores = [e for e in live if e["kind"] == "pre_restore"]
    if pre_restores:
        protected.add(pre_restores[-1]["id"])
    expire = []
    for entry in live:
        if total <= budget_bytes:
            break
        if entry["id"] not in protected:
            expire.append(entry["id"])
            total -= entry["size"]
    return expire


class Store:
    """The checkpoint folder of one conversation: snapshots, conversation tails, and an index."""

    def __init__(self, root: Path, session_id: str):
        if not _ID.match(session_id):
            raise CheckpointError(f"Bad session id {session_id!r}")
        self.folder = root / "checkpoints" / session_id
        self.entries: list[dict] = []
        index = self.folder / INDEX_NAME
        if index.is_file():
            loaded = json.loads(index.read_text(encoding="utf-8"))
            # The index is a file on disk; ids become file names, so they are checked.
            self.entries = [e for e in loaded if _ID.match(str(e.get("id", "")))]

    def snapshot_path(self, checkpoint_id: str) -> Path:
        return self.folder / f"{checkpoint_id}.blend"

    def tail_path(self, checkpoint_id: str) -> Path:
        return self.folder / f"{checkpoint_id}.tail.json"

    def get(self, checkpoint_id: str) -> dict:
        for entry in self.entries:
            if entry["id"] == checkpoint_id:
                return entry
        raise CheckpointError("That checkpoint no longer exists.")

    def save_index(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self.folder / (INDEX_NAME + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(self.entries, indent=1), encoding="utf-8")
            os.replace(temporary, self.folder / INDEX_NAME)
        finally:
            temporary.unlink(missing_ok=True)

    def add(self, entry: dict, budget_bytes: int, *, protect: tuple[Path, ...] = ()) -> None:
        self.entries.append(entry)
        for checkpoint_id in plan_pruning(self.entries, budget_bytes):
            if self.snapshot_path(checkpoint_id) in protect:
                continue
            self.get(checkpoint_id)["expired"] = True
            self.snapshot_path(checkpoint_id).unlink(missing_ok=True)
            self.tail_path(checkpoint_id).unlink(missing_ok=True)
        self.save_index()


# ------------------------------------------------------------------ Blender side

def data_root() -> Path:
    override = os.environ.get("LOOPCUT_DATA_DIR")
    if override:
        return Path(override)
    import bpy
    return Path(bpy.utils.user_resource("DATAFILES", path="loopcut", create=True))


def _budget_bytes() -> int:
    from . import config
    return config.checkpoint_budget_mb() * 1024 * 1024


def _store(session: dict) -> Store:
    return Store(data_root(), session["id"])


def in_place_supported() -> bool:
    """True in the Loopcut build of Blender. (getattr on bpy.ops never fails, so ask dir().)"""
    import bpy
    return "loopcut_snapshot_restore" in dir(bpy.ops.wm)


@_storage_locked
def _write_snapshot(store: Store, kind: str, label: str, *, protect: tuple[Path, ...] = (), **extra) -> dict:
    import bpy
    checkpoint_id = uuid.uuid4().hex
    path = store.snapshot_path(checkpoint_id)
    in_place = in_place_supported()
    try:
        require_space(path, protect=protect)
        store.entries = Store(data_root(), store.folder.name).entries
        store.folder.mkdir(parents=True, exist_ok=True)
        # Either way the open file, its path and its unsaved-changes state are left alone.
        if in_place:
            if bpy.ops.wm.loopcut_snapshot_write(filepath=str(path)) != {"FINISHED"}:
                raise RuntimeError("Blender could not write the snapshot file")
        else:
            bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True, compress=True)
        size = path.stat().st_size
        entry = {"id": checkpoint_id, "kind": kind, "label": label[:80], "created": time.time(),
                 "size": size, "expired": False, "source": bpy.data.filepath, "in_place": in_place, **extra}
        store.add(entry, _budget_bytes(), protect=protect)
        cleanup_storage(protect=(*protect, path))
    except StorageFullError:
        path.unlink(missing_ok=True)
        raise
    except (OSError, RuntimeError) as ex:
        path.unlink(missing_ok=True)
        if getattr(ex, "errno", None) == errno.ENOSPC or "No space left" in str(ex):
            raise StorageFullError(f"Disk is full: could not save a checkpoint to {store.folder}.") from ex
        raise CheckpointError(f"Could not save a checkpoint to {store.folder}: {ex}") from ex
    return entry


def begin_turn(session: dict, user_item: dict, *, text: str | None = None) -> None:
    """Record the request before sending; reused notes may already be in history. Nothing is written yet."""
    if session.get("checkpoints_disabled"):
        session["pending_checkpoint"] = None
        return
    session["pending_checkpoint"] = {
        "item": user_item,
        "input": user_item["text"] if text is None else text,
        "message_index": len(session["messages"]),
        "item_index": next((i for i, item in enumerate(session["items"]) if item is user_item), len(session["items"])),
    }


def ensure_for_turn(session: dict) -> None:
    """Main thread. Snapshot the scene if this turn has not yet. Raises CheckpointError, in
    which case the caller must not go on to change the scene."""
    pending = session.get("pending_checkpoint")
    if not pending:
        return
    try:
        entry = _write_snapshot(_store(session), "turn", pending["item"]["text"],
                                message_index=pending["message_index"], item_index=pending["item_index"],
                                input=pending["input"])
    except StorageFullError as ex:
        session["pending_checkpoint"] = None
        session["checkpoints_disabled"] = True
        storage_notice(session, ex)
        raise
    pending["item"]["checkpoint"] = entry["id"]
    session["pending_checkpoint"] = None


def ensure_for_wave(session: dict, label: str, message_index: int, item_index: int) -> str:
    """Main thread, as a wave's workers start (waves.prepare): snapshot the scene and return the
    checkpoint's id, which each of the wave's merges names. message_index and item_index point at the
    orchestrator's delegate message and tool card (or the wave card when started directly), so restoring
    puts the scene back to before the delegate call, every merge of the wave undone, and cuts the
    conversation to just before the wave was asked for. Raises CheckpointError; the wave must not
    start without one."""
    if session.get("checkpoints_disabled"):
        return ""
    try:
        entry = _write_snapshot(_store(session), "wave", label, message_index=message_index, item_index=item_index)
    except StorageFullError as ex:
        session["pending_checkpoint"] = None
        session["checkpoints_disabled"] = True
        storage_notice(session, ex)
        raise
    return entry["id"]


def for_bundle(session: dict) -> str:
    """Main thread, as a cloud tool with a bundle input is called (cloud.execute): snapshot the scene as it
    is now, this turn's edits included, and return the id bundle_from_checkpoint collects. Not a restore
    point and not the turn's checkpoint; discard() drops it once collected. Raises CheckpointError."""
    return _write_snapshot(_store(session), "bundle", "Cloud bundle")["id"]


@_storage_locked
def discard(session: dict, checkpoint_id: str) -> None:
    """Main thread: remove a snapshot nothing restores (a bundle's, once collected), so it never takes
    the storage budget from the conversation's checkpoints. A missing one is already gone."""
    store = _store(session)
    try:
        entry = store.get(checkpoint_id)
    except CheckpointError:
        return
    store.entries.remove(entry)
    store.save_index()
    store.snapshot_path(checkpoint_id).unlink(missing_ok=True)


def status(session: dict, checkpoint_id: str) -> str:
    """'ok' | 'expired' | 'missing', for the UI."""
    try:
        store = _store(session)
        entry = store.get(checkpoint_id)
    except CheckpointError:
        return "missing"
    return "expired" if entry["expired"] or not store.snapshot_path(checkpoint_id).is_file() else "ok"


def bundle_from_checkpoint(session: dict, checkpoint_id: str, staging_dir) -> dict:
    """Collect one indexed checkpoint without opening it in the live Blender. No upload.

    Return {manifest, missing, unsupported, system}; callers can review dependencies.
    Recovery files preserve source-relative paths, while stock copy-saves rebase them.
    """
    from . import bundles
    import threading
    if threading.current_thread() is threading.main_thread():
        store = _store(session)
    else:
        from . import mainthread
        store = mainthread.run_on_main(lambda: _store(session)).result(timeout=30)
    entry = store.get(checkpoint_id)
    snapshot = store.snapshot_path(checkpoint_id)
    if entry["expired"] or not snapshot.is_file():
        raise CheckpointError("That checkpoint has expired to stay within the storage budget.")
    original = entry.get("source", "") if entry.get("in_place", False) else None
    info = bundles.collect(snapshot, staging_dir, original_path=original)
    info["created_at"] = bundles._timestamp(entry["created"])
    source = {"kind": "checkpoint", "session": session["id"], "checkpoint": checkpoint_id}
    return {"manifest": bundles.manifest(staging_dir, info, source),
            "missing": info["missing"], "unsupported": info["unsupported"], "system": info["system"]}


def _blocking_job() -> str | None:
    import bpy
    for job in ("RENDER", "RENDER_PREVIEW", "OBJECT_BAKE", "COMPOSITE", "SHADER_COMPILATION"):
        if bpy.app.is_job_running(job):
            return job
    return None


@_storage_locked
def restore(session: dict, checkpoint_id: str) -> Path | None:
    """Main thread, outside any operator. Returns the new file the restored scene was saved to,
    or None when it was restored in place."""
    import bpy
    from . import state

    if session["busy"]:
        raise CheckpointError("Stop the running request before restoring.")
    job = _blocking_job()
    if job:
        raise CheckpointError(f"Blender is busy ({job.lower().replace('_', ' ')}). Try again when it finishes.")
    store = _store(session)
    target = store.get(checkpoint_id)
    snapshot = store.snapshot_path(checkpoint_id)
    if target["expired"] or not snapshot.is_file():
        raise CheckpointError("That checkpoint has expired to stay within the storage budget.")

    # 1. Save where we are now, with the part of the conversation about to be cut, so this
    #    restore can be undone. If this fails, nothing has been touched yet.
    #    Both kinds of checkpoint record where the conversation was cut, and everything before
    #    that point is common to both branches, so the same cut applies either way.
    cut_messages, cut_items = target["message_index"], target["item_index"]
    undo = _write_snapshot(store, "pre_restore", "Before restore",
                           protect=(snapshot,), message_index=cut_messages, item_index=cut_items)
    tail = {"messages": session["messages"][cut_messages:], "items": session["items"][cut_items:],
            "input": session["input"], "references": session.get("references") or []}
    store.tail_path(undo["id"]).write_text(json.dumps(tail), encoding="utf-8")

    # 2. Load the snapshot. Whichever way it goes, Blender must not end up thinking the snapshot
    #    is the open file: a Ctrl+S must never write into the store.
    in_place = in_place_supported() and target.get("in_place", False)
    open_path = bpy.data.filepath
    destination = None
    state.restoring = True  # Tells lifecycle this file load is ours, not the user opening a file.
    try:
        if in_place:
            if bpy.ops.wm.loopcut_snapshot_restore(filepath=str(snapshot)) != {"FINISHED"}:
                raise CheckpointError("Blender could not read that checkpoint; nothing was changed.")
        else:
            from . import files
            destination = restored_path(target["source"] or undo["source"], files.blend_base()[0], time.time())
            destination.parent.mkdir(parents=True, exist_ok=True)
            bpy.ops.wm.open_mainfile(filepath=str(snapshot), load_ui=False)
            bpy.ops.wm.save_as_mainfile(filepath=str(destination), compress=False)
    finally:
        state.restoring = False
    # In place, the file that was open stays the open file; an unsaved scene takes the path the
    # snapshot recorded, which for a scene that was never saved is none.
    expected = str(destination) if destination else (open_path or target["source"])
    if Path(bpy.data.filepath) != Path(expected):
        raise CheckpointError(f"Restored, but Blender is pointing at {bpy.data.filepath or 'no file'}; use Save As.")

    # 3. The conversation.
    cut = session["messages"][cut_messages:]
    if target["kind"] == "turn":
        removed = session["items"][cut_items:]
        text = target.get("input", next((i["text"] for i in removed if i["kind"] == "user"), ""))
        del session["messages"][cut_messages:]
        del session["items"][cut_items:]
        session["input"], session["cursor"], session["anchor"] = text, len(text), None
    elif target["kind"] == "wave":  # Mid-turn: back to before the delegate call, the input left alone.
        del session["messages"][cut_messages:]
        del session["items"][cut_items:]
    else:
        tail_file = store.tail_path(checkpoint_id)
        saved = json.loads(tail_file.read_text(encoding="utf-8")) if tail_file.is_file() else None
        del session["messages"][cut_messages:]
        del session["items"][cut_items:]
        if saved:
            session["messages"].extend(saved["messages"])
            session["items"].extend(saved["items"])
            session["input"], session["cursor"], session["anchor"] = saved["input"], len(saved["input"]), None
            known = {r["ref"] for r in session.get("references") or []}
            session["references"] = [*(session.get("references") or []),
                                     *(r for r in saved.get("references") or [] if r["ref"] not in known)]
    _drop_cut_references(session, cut)
    if destination:
        message = f"Restored. You are now working in {destination.name}; your original file was not changed."
    elif open_path:
        message = (f"Restored. {Path(open_path).name} on disk was not changed; save when you want to "
                   f"keep this state.")
    else:
        message = "Restored."
    session["items"].append(state.item_notice(message, "Undo restore", undo["id"]))
    session["scroll"], session["confirm_restore"], session["pending_checkpoint"] = 0.0, None, None
    from . import conversations
    if destination:
        conversations.add_project(session, str(destination))  # The chat belongs to the restored file too.
    conversations.save(session)
    return destination


def _drop_cut_references(session: dict, cut: list) -> None:
    """An image attached in a message the restore cut, and in none left, is no longer attached: a restored-away
    Try it photo stayed in the list, and the model gave its name for the user's own picture (2026-10-25). An
    undo puts it back with its messages (the tail keeps the list). Images attached outside any message (a
    cloud job's outputs) stay."""
    from . import context
    gone = ({ref for m in cut for ref in context._image_refs(m)}
            - {ref for m in session["messages"] for ref in context._image_refs(m)})
    if gone:
        session["references"] = [r for r in session.get("references") or [] if r["ref"] not in gone]


def schedule_cleanup() -> None:
    """Capture Blender settings on the main thread, then scan storage off the startup path."""
    global _CLEANUP_THREAD
    if _CLEANUP_THREAD is not None and _CLEANUP_THREAD.is_alive():
        return
    from . import state
    root, budget, session = data_root(), storage_budget(), state.session()
    def clean():
        try:
            remove_stale_sessions(root=root, budget_bytes=budget, active_session=session)
        except Exception as ex:
            _STORAGE_SIZES.pop(str(root), None)
            _STORAGE_ERRORS[str(root)] = str(ex)
            print(f"Loopcut: could not clean up old copies: {ex}")
    _CLEANUP_THREAD = threading.Thread(target=clean, name="Loopcut storage cleanup", daemon=True)
    try:
        _CLEANUP_THREAD.start()
    except Exception as ex:
        _STORAGE_SIZES.pop(str(root), None)
        _STORAGE_ERRORS[str(root)] = str(ex)
        raise


def remove_stale_sessions(now: float | None = None, *, root: Path | None = None,
                          budget_bytes: int | None = None, active_session: dict | None = None) -> None:
    # Atomic saves remove their own temporary files; also clear leftovers from a stopped process.
    root = data_root() if root is None else root
    parent = root / "conversations"
    if parent.is_dir():
        for folder in parent.iterdir():
            if folder.is_dir() and not folder.is_symlink() and _ID.fullmatch(folder.name):
                for name in ("conversation.json.tmp", "meta.json.tmp"):
                    temporary = folder / name
                    if temporary.is_file() and temporary.stat().st_mtime < (now or time.time()) - 86400:
                        temporary.unlink(missing_ok=True)
    cleanup_storage(now=now, root=root, budget_bytes=budget_bytes, active_session=active_session)
