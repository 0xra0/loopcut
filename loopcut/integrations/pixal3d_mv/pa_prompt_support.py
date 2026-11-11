"""Optional Qwen-Image 2.1 prompt/CFG controls for Pixal3D pa.py.

Keeps pa.py's existing front/sheet CLI and default model sampling unchanged
unless optional prompt settings are supplied. Requires the node graph exported
in API format (TextEncodeQwenImage21 node 5, KSampler node 7).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

_CONFIG = {}
_ALLOWED = {
    "negative_prompt", "front_negative", "sheet_negative", "sheet_consistency",
    "front_cfg", "sheet_cfg",
}
DEFAULT_NEGATIVE_CFG = 1.5  # Conservative starting point; tune per model/seed.

# --detail-spec: details sorted by scale and by the view they show in (see examples/detail_spec_example.json).
_DETAIL = {}
DETAIL_TIERS = ("large", "medium", "small", "materials")
VIEWS = ("front", "left", "back", "right")
TIER_LABELS = {
    "large": "Large-scale forms (silhouette, main masses, proportions)",
    "medium": "Secondary forms (raised parts, bevels, ornaments)",
    "small": "Fine surface detail (engravings, rivets, scratches)",
    "materials": "Materials and colours",
}


def _lines(value, where):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"detail-spec '{where}' must be a string or a list of nonempty strings")
    return [v.strip().rstrip(".") for v in value]


def load_detail_spec(path):
    """Read and validate a detail spec once, where it enters; the rest of the module trusts it."""
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Detail spec must be a JSON object: {path}")
    unknown = set(data) - set(DETAIL_TIERS) - {"views", "avoid"}
    if unknown:
        raise ValueError(f"Unknown detail-spec fields: {', '.join(sorted(unknown))}")
    spec: dict = {key: _lines(data[key], key) for key in (*DETAIL_TIERS, "avoid") if key in data}
    views = data.get("views", {})
    if not isinstance(views, dict) or set(views) - set(VIEWS):
        raise ValueError(f"detail-spec 'views' must be an object with keys from {', '.join(VIEWS)}")
    spec["views"] = {view: _lines(text, f"views.{view}") for view, text in views.items()}
    return spec


def route_details(stage, view):
    """Which scale tiers and which per-view notes one generated image is told about.

    front: the source image, so every tier plus the front notes.
    sheet (view None): one image holding all four panels, so every tier plus every panel's notes.
    one separate side/back view: only the silhouette and materials, plus that view's own notes, so
    front-only detail (engravings, the central symbol) is not copied onto the sides or the back.
    """
    if stage == "front":
        return DETAIL_TIERS, ("front",)
    if view is None:
        return DETAIL_TIERS, VIEWS
    return ("large", "materials"), (view,)


def detail_block(stage, view=None):
    if not _DETAIL:
        return ""
    tiers, views = route_details(stage, view)
    parts = [f"{TIER_LABELS[t]}: {'; '.join(_DETAIL[t])}." for t in tiers if t in _DETAIL]
    for v in views:
        if v in _DETAIL["views"]:
            where = f"In the {v.upper()} panel only" if len(views) > 1 else f"Seen from the {v.upper()}"
            parts.append(f"{where}: {'; '.join(_DETAIL['views'][v])}.")
    return "DETAIL SPECIFICATION, follow exactly:\n" + "\n".join(parts) if parts else ""


def prompt_fingerprint():
    """Short hash of every optional prompt setting, recorded with a stage so --resume never reuses images
    made from different prompts. None when nothing optional is set (old run records stay reusable)."""
    if not (_CONFIG or _DETAIL):
        return None
    return hashlib.sha256(json.dumps([_CONFIG, _DETAIL], sort_keys=True).encode()).hexdigest()[:12]


def configure_from_argv(argv):
    """Remove only our flags from sys.argv, leaving pa.py commands untouched."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--prompt-config")
    parser.add_argument("--negative-prompt")
    parser.add_argument("--front-negative")
    parser.add_argument("--sheet-negative")
    parser.add_argument("--sheet-consistency")
    parser.add_argument("--front-cfg", type=float)
    parser.add_argument("--sheet-cfg", type=float)
    parser.add_argument("--detail-spec")
    parsed, rest = parser.parse_known_args(argv[1:])
    detail = load_detail_spec(parsed.detail_spec) if parsed.detail_spec else {}

    config = {}
    if parsed.prompt_config:
        path = Path(parsed.prompt_config).expanduser()
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"Prompt config must be a JSON object: {path}")
        unknown = set(data) - _ALLOWED
        if unknown:
            raise ValueError(f"Unknown prompt-config fields: {', '.join(sorted(unknown))}")
        config.update(data)

    # CLI wins over JSON; validate before mutating argv and global settings.
    for key in _ALLOWED:
        value = getattr(parsed, key, None)
        if value is not None:
            config[key] = value
    for key, value in config.items():
        if key.endswith("_cfg"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Prompt config '{key}' must be a number")
            if not math.isfinite(float(value)) or not 1.0 <= float(value) <= 10.0:
                raise ValueError(f"'{key}' must be between 1 and 10")
        elif not isinstance(value, str):
            raise ValueError(f"Prompt config '{key}' must be a string")

    argv[1:] = rest
    _CONFIG.clear()
    _CONFIG.update(config)
    if config:
        print("  optional Qwen prompt settings:", ", ".join(sorted(config)))
    _DETAIL.clear()
    _DETAIL.update(detail)
    if detail:
        print("  detail spec:", ", ".join(k for k in (*DETAIL_TIERS, "avoid") if k in detail),
              "| views:", ", ".join(detail["views"]) or "none")
    return config


def apply_prompt_settings(workflow, stage, original_positive, view=None):
    """Patch prompt inputs and KSampler CFG for a specific Qwen stage.

    `TextEncodeQwenImage21`'s negative conditioning is ignored by ComfyUI
    at CFG=1. Change CFG only when a nonempty negative prompt is requested.
    A supplied --front-cfg/--sheet-cfg always wins over automatic guidance.
    """
    if stage not in ("front", "sheet"):
        raise ValueError(f"Unrecognized Qwen generation stage: {stage!r}")
    try:
        encoder = workflow["5"]
        sampler = workflow["7"]
        inputs = encoder["inputs"]
        sinputs = sampler["inputs"]
    except KeyError as exc:
        raise ValueError("Expected node 5 (encoder) and node 7 (KSampler)") from exc
    if encoder.get("class_type") != "TextEncodeQwenImage21" or sampler.get("class_type") != "KSampler":
        raise ValueError("Unexpected workflow node types at 5/7; review ComfyUI wiring before patching")
    if "prompt" not in inputs or "negative_prompt" not in inputs:
        raise ValueError("Qwen node 5 does not expose prompt and negative_prompt")
    if sinputs.get("positive") != ["5", 0] or sinputs.get("negative") != ["5", 1]:
        raise ValueError("KSampler node 7 not wired to Qwen node 5 positive/negative outputs")
    if "cfg" not in sinputs:
        raise ValueError("KSampler node 7 has no cfg input")

    positive = original_positive
    if stage == "sheet" and _CONFIG.get("sheet_consistency"):
        positive = (f"{original_positive.rstrip()}\n\n"
                    f"ADDITIONAL VIEW CONSISTENCY RULES:\n{_CONFIG['sheet_consistency']}")
    block = detail_block(stage, view)
    if block:
        positive = f"{positive.rstrip()}\n\n{block}"
    inputs["prompt"] = positive

    neg_key = f"{stage}_negative"
    negative = _CONFIG.get(neg_key, _CONFIG.get("negative_prompt"))
    if _DETAIL.get("avoid"):
        avoid = ", ".join(_DETAIL["avoid"])
        base = inputs["negative_prompt"] if negative is None else negative
        negative = f"{base.rstrip().rstrip('.,')}, {avoid}" if base.strip() else avoid
    if negative is not None:
        inputs["negative_prompt"] = negative

    cfg_key = f"{stage}_cfg"
    if cfg_key in _CONFIG:
        sinputs["cfg"] = float(_CONFIG[cfg_key])
    elif negative and negative.strip() and float(sinputs["cfg"]) <= 1:
        sinputs["cfg"] = DEFAULT_NEGATIVE_CFG
        print(f"  {stage}: negative prompt supplied -> enabling CFG={DEFAULT_NEGATIVE_CFG:g}")

    if inputs["negative_prompt"].strip() and float(sinputs["cfg"]) <= 1:
        print(f"  WARNING {stage}: CFG <= 1 means negative_prompt will have no effect")
    elif float(sinputs["cfg"]) > 1 and not inputs["negative_prompt"].strip():
        print(f"  NOTE {stage}: CFG > 1 but negative_prompt is empty")
    return workflow
