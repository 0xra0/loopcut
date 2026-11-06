"""Images the user attaches to a message: dropped on the panel or picked with the file browser.

Every file is decoded by Blender and re-saved as a PNG no larger than MAX_SIDE, so what reaches
the conversation folder (and the model) is a real image of bounded size whatever was dropped; it is
stored compact (image_budget.compact: JPEG unless it has transparency or PNG is smaller).
Main thread only: it goes through bpy.data.

Video clips (VIDEO_EXTENSIONS) are attached too, but not here: the panel opens the clip dialog
(ui/host.py), video_attach.py prepares a trimmed clip into timed contact sheets in a background
Blender, and those sheets come back as ordinary attached images. check_clip is the file check both
use. The clip itself never leaves this computer.
"""

import os
import tempfile
from pathlib import Path

import bpy

from . import conversations
from .videoref import MAX_SECONDS as MAX_CLIP_SECONDS, VIDEO_EXTENSIONS, ClipError, is_video  # noqa: F401

EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".tga")
MODEL_EXTENSIONS = (".glb", ".gltf", ".fbx", ".obj")
ACCEPTED = EXTENSIONS + MODEL_EXTENSIONS + VIDEO_EXTENSIONS
MAX_PER_MESSAGE = 6
MAX_SIDE = 2048            # The stored copy; workspace requests and look_at_reference read details from it.
SEND_SIDE = 768            # The cheap loop's copy; workspace requests use MAX_SIDE instead.
MAX_FILE_BYTES = 100_000_000
MAX_CLIP_BYTES = 2_000_000_000  # A phone's 4K minute is about 400 MB; only the trimmed part is read.


class AttachmentError(Exception):
    pass


def paste_image(session: dict, context) -> list[str]:
    """Read Blender's native clipboard image in a temporary Image Editor context."""
    area = context.area
    if area is None:
        return ["Paste an image in the Loopcut panel, or attach an image file."]
    old_type, old_ui_type = area.type, area.ui_type
    space = previous = image = None
    temporary = None
    try:
        area.type = "IMAGE_EDITOR"
        space = area.spaces.active
        previous = space.image
        region = next(r for r in area.regions if r.type == "WINDOW")
        with context.temp_override(area=area, region=region):
            if not bpy.ops.image.clipboard_paste.poll():
                return ["No image is available on the clipboard. Copy an image, or attach an image file."]
            if "FINISHED" not in bpy.ops.image.clipboard_paste():
                return ["Could not paste the clipboard image. Try attaching an image file."]
        image = space.image
        if image is None or image is previous or not all(image.size):
            return ["Could not read the clipboard image. Try attaching an image file."]
        handle, temporary = tempfile.mkstemp(suffix=".png", prefix="loopcut-clipboard-")
        os.close(handle)
        image.filepath_raw, image.file_format = temporary, "PNG"
        image.save()
        count = len(session["attachments"])
        problems = add(session, [temporary])
        for attached in session["attachments"][count:]:
            attached["name"] = "Clipboard image.png"
            attached.pop("source", None)  # The temporary source is deleted; use the stored full image.
        return problems
    except (OSError, RuntimeError, AttributeError, StopIteration) as ex:
        return [f"Could not paste the clipboard image: {ex}. Try attaching an image file."]
    finally:
        if space is not None:
            space.image = previous
        if image is not None and image is not previous:
            bpy.data.images.remove(image)
        area.type = old_type
        area.ui_type = old_ui_type
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def import_models(session: dict, paths: list[str]) -> list[str]:
    """A model dropped on the panel goes into the scene; it is never encoded as an image."""
    from . import state
    problems = []
    attempted = False
    for raw in paths:
        source = Path(raw)
        try:
            if not source.is_file():
                raise AttachmentError("the file is missing")
            if source.stat().st_size > MAX_FILE_BYTES:
                raise AttachmentError(f"the model exceeds {MAX_FILE_BYTES // 1_000_000} MB; import it with File > Import")
            suffix = source.suffix.lower()
            if suffix in (".glb", ".gltf"):
                importer = bpy.ops.import_scene.gltf
            elif suffix == ".fbx":
                importer = bpy.ops.import_scene.fbx
            elif suffix == ".obj":
                importer = bpy.ops.wm.obj_import
            else:
                raise AttachmentError("use a GLB, glTF, FBX or OBJ file")
            attempted = True  # Even a failed importer can leave geometry behind.
            result = importer(filepath=str(source))
            if "FINISHED" not in result:
                raise AttachmentError("the importer cancelled")
        except AttributeError:
            problems.append(f"Could not import {source.name}: its Blender importer is unavailable. Enable it, or use File > Import.")
            continue
        except (AttachmentError, OSError, RuntimeError) as ex:
            problems.append(f"Could not import {source.name}: {ex}.")
            continue
        note = f"Imported {source.name} into the scene."
        session["items"].append(state.item_notice(f"{note} Tell Loopcut what to do with it."))
        session.setdefault("between_turns", []).append(note)
    if attempted:
        try:
            if "FINISHED" not in bpy.ops.ed.undo_push(message="Import model"):
                raise RuntimeError("undo was cancelled")
        except (AttributeError, RuntimeError) as ex:
            problems.append(f"Could not record import undo: {ex}. Inspect the scene for imported or partial geometry.")
    return problems


def _to_png(source: Path, target: Path, max_side: int = MAX_SIDE) -> tuple[int, int]:
    """Returns the source image's size."""
    try:
        image = bpy.data.images.load(str(source), check_existing=False)
    except RuntimeError as ex:
        raise AttachmentError(f"{source.name} is not an image Blender can read") from ex
    try:
        width, height = image.size
        if width == 0 or height == 0:
            raise AttachmentError(f"{source.name} is not an image Blender can read")
        longest = max(width, height)
        if longest > max_side:
            image.scale(max(1, round(width * max_side / longest)), max(1, round(height * max_side / longest)))
        image.filepath_raw = str(target)
        image.file_format = "PNG"
        image.save()
        return width, height
    except RuntimeError as ex:
        raise AttachmentError(f"Could not convert {source.name}: {ex}") from ex
    finally:
        bpy.data.images.remove(image)  # The user's file must not gain an image datablock.


def check_clip(source: Path) -> None:
    """A video file the clip dialog may open: the right kind, there, and not absurdly large. Length is
    checked once Blender has read it (video_attach.probe, videoref.trim)."""
    if not is_video(source.name):
        raise ClipError(f"{source.name} is not a clip Loopcut takes (mp4, mov).")
    try:
        size = source.stat().st_size if source.is_file() else -1
    except OSError:
        size = -1
    if size <= 0 or size > MAX_CLIP_BYTES:
        raise ClipError(f"{source.name} is missing, empty or larger than {MAX_CLIP_BYTES // 1_000_000_000} GB.")


def room(session: dict) -> int:
    """Images that still fit in the next message: sheets of a clip being prepared are counted already."""
    return MAX_PER_MESSAGE - len(session["attachments"]) - session.get("video_reserved", 0)


def add(session: dict, paths: list[str]) -> list[str]:
    """Attach what can be attached; returns one line per file that could not be."""
    problems = []
    for raw in paths:
        source = Path(raw)
        if is_video(source.name):
            problems.append(f"{source.name} is a clip: attach it from the panel, which asks for the part to use.")
            continue
        if room(session) <= 0:
            problems.append(f"Only {MAX_PER_MESSAGE} images fit in one message; the rest were left out.")
            break
        if source.suffix.lower() not in EXTENSIONS:
            problems.append(f"{source.name} is not an image or a clip (png, jpg, webp, bmp, tif, tga, mp4, mov).")
            continue
        try:
            if not source.is_file() or source.stat().st_size > MAX_FILE_BYTES:
                problems.append(f"{source.name} is missing or larger than {MAX_FILE_BYTES // 1_000_000} MB.")
                continue
            handle, temporary = tempfile.mkstemp(suffix=".png", prefix="loopcut-attach-")
            os.close(handle)
            try:
                stored = []
                for side in (MAX_SIDE, SEND_SIDE):
                    _to_png(source, Path(temporary), side)
                    stored.append(conversations.store_image(session, Path(temporary), side=side))
            finally:
                os.unlink(temporary)
        except (AttachmentError, OSError) as ex:
            problems.append(str(ex))
            continue
        full, small = stored
        if all(a["ref"] != small for a in session["attachments"]):
            # source: the original file, which the workspace copies (workspace.py) while it is still there.
            session["attachments"].append({"ref": small, "full": full, "name": source.name, "source": str(source)})
    return problems
