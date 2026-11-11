"""Installed add-ons and extensions, named to the model once per request.

inspect_api already describes whatever an add-on registers: its operators land in bpy.ops, its
panels, properties and preferences in bpy.types. What the model cannot do is look up something it
does not know exists, so each request names the user-installed add-ons with their operator
prefixes. Core add-ons are left out. Optional System bundles are listed after installed add-ons.

Main thread only, except `line` which is pure.
"""

import os
import sys
from collections.abc import Sequence

MAX_LISTED = 12       # More than this and the line stops being cheap; the rest is a count.
MAX_OP_GROUPS = 3     # Operator prefixes shown per add-on, largest groups first.
MAX_DESCRIPTION = 70
MAX_OFF_HITS = 6      # Installed-but-off add-ons one inspect_api search names.
_cache: tuple[tuple, list[dict]] | None = None  # (enabled add-on names, entries) for that set.
_off_cache: tuple[tuple, list[dict]] | None = None  # (enabled add-on names, installed-but-off entries).


def enabled() -> list[dict]:
    """User-installed add-ons that are enabled and running, with the operators each registered."""
    global _cache
    import bpy
    from . import settings
    key = tuple(bpy.context.preferences.addons.keys())
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    # system_resource can come back relative to the working directory; module paths are absolute.
    core = os.path.realpath(os.path.join(bpy.utils.system_resource("SCRIPTS"), "addons_core")) + os.sep
    candidates = {}
    for name in key:
        module = sys.modules.get(name)
        if module is None or not getattr(module, "__addon_enabled__", False):
            continue  # Enabled in the preferences but not running here.
        file = os.path.realpath(getattr(module, "__file__", "") or "")
        if name == settings.PACKAGE or file.startswith(core):
            continue
        candidates[name] = module
    operators = _operators_by_addon(candidates)
    entries = [{"module": name, "name": info.get("name") or name,
                "description": info.get("description") or "",
                "operators": operators.get(name, [])}
               for name, info in ((n, _info(m)) for n, m in candidates.items())]
    entries.sort(key=lambda e: e["name"].lower())
    _cache = (key, entries)
    return entries


def installed_off() -> list[dict]:
    """User-installed add-ons and extensions that are off: legacy scripts/addons and user extension repos. Core
    add-ons, the fork's System bundles (bundled_addons.py) and Loopcut itself are left out. Cached per enabled set:
    the first scan reads every add-on's bl_info or manifest (about 2 s for a hundred)."""
    global _off_cache
    import addon_utils
    import bpy
    from . import settings
    key = tuple(bpy.context.preferences.addons.keys())
    if _off_cache is not None and _off_cache[0] == key:
        return _off_cache[1]
    core = os.path.realpath(os.path.join(bpy.utils.system_resource("SCRIPTS"), "addons_core")) + os.sep
    entries = []
    for fake in addon_utils.modules(refresh=False):
        name = fake.__name__
        if (name in key or name == settings.PACKAGE or name.startswith("bl_ext.system.")
                or os.path.realpath(getattr(fake, "__file__", "") or "").startswith(core)):
            continue
        info = addon_utils.module_bl_info(fake)
        entries.append({"module": name, "name": info.get("name") or name,
                        "category": info.get("category") or "",
                        "description": " ".join(str(info.get("description") or "").split())})
    entries.sort(key=lambda e: e["name"].lower())
    _off_cache = (key, entries)
    return entries


def off_hint(entry: dict) -> str:
    text = f"{entry['name']} [{entry['module']}]"
    if entry["category"]:
        text += f" ({entry['category']})"
    description = entry["description"]
    if len(description) > MAX_DESCRIPTION:
        description = description[:MAX_DESCRIPTION - 1].rstrip() + "…"
    return f"{text}: {description}" if description else text


def search_off(query: str) -> list[str]:
    """Installed-but-off add-ons whose name, module, category and description hold every word of query."""
    terms = query.lower().split()
    if not terms:
        return []
    hits = [off_hint(e) for e in installed_off()
            if all(t in " ".join((e["name"], e["module"], e["category"], e["description"])).lower() for t in terms)]
    if not hits:
        return []
    shown = hits[:MAX_OFF_HITS] + ([f"  +{len(hits) - MAX_OFF_HITS} more"] if len(hits) > MAX_OFF_HITS else [])
    return ["Installed add-ons that are off; pass the [module] as inspect_api path to turn one on:", *shown]


def enable_installed(module: str) -> str | None:
    """Turn on an installed-but-off add-on for this session and describe its operators; None when module names
    none. Leaves preferences as clean as they were, so the add-on stays on after a restart only if the user saves
    preferences. Raises RuntimeError when its register() fails."""
    entry = next((e for e in installed_off() if e["module"] == module), None)
    if entry is None:
        return None
    import addon_utils
    import bpy
    preferences = bpy.context.preferences
    dirty = preferences.is_dirty
    failure = []
    try:
        addon_utils.enable(module, default_set=True, handle_error=failure.append)
    finally:
        preferences.is_dirty = dirty
    if failure or not addon_utils.check(module)[1]:
        if module in preferences.addons:
            addon_utils.disable(module, default_set=True, handle_error=lambda ex: None)
            preferences.is_dirty = dirty
        why = f": {failure[0]!r}" if failure else ""
        raise RuntimeError(f"Could not turn on {entry['name']} [{module}]{why}")
    operators = _operators_by_addon({module: sys.modules.get(module)}).get(module, [])
    shown = ", ".join(f"bpy.ops.{op}" for op in sorted(operators)[:40])
    more = f" (+{len(operators) - 40} more)" if len(operators) > 40 else ""
    return (f"Turned on {off_hint(entry)} for this session. Its operators: {shown or 'none registered'}{more}. "
            "Its panels and properties are in bpy.types now: inspect_api describes any of them.")


def _info(module) -> dict:
    """bl_info for a legacy add-on, the manifest for an extension; addon_utils reads both."""
    import addon_utils
    for fake in addon_utils.modules(refresh=False):
        if fake.__name__ == module.__name__:
            return addon_utils.module_bl_info(fake)
    return addon_utils.module_bl_info(module)


def _operators_by_addon(modules: dict) -> dict[str, list[str]]:
    """bl_idname of every registered operator, keyed by the add-on whose package defined it.
    One pass over bpy.types, a few milliseconds."""
    import bpy
    found: dict[str, list[str]] = {}
    if not modules:
        return found
    for name in dir(bpy.types):
        cls = getattr(bpy.types, name, None)
        if not isinstance(cls, type) or not issubclass(cls, bpy.types.Operator):
            continue
        owner = cls.__module__ or ""
        for addon in modules:
            if owner == addon or owner.startswith(addon + "."):
                idname = getattr(cls, "bl_idname", "")
                if idname:
                    found.setdefault(addon, []).append(idname)
                break
    return found


def _op_groups(idnames: list[str]) -> str:
    """'bpy.ops.node.nw_*, bpy.ops.wm.nw_import' from the operators' bl_idnames."""
    by_module: dict[str, list[str]] = {}
    for idname in idnames:
        module, _, name = idname.partition(".")
        by_module.setdefault(module, []).append(name)
    groups = sorted(by_module.items(), key=lambda g: (-len(g[1]), g[0]))
    parts = []
    for module, names in groups[:MAX_OP_GROUPS]:
        if len(names) == 1:
            parts.append(f"bpy.ops.{module}.{names[0]}")
            continue
        common = os.path.commonprefix(names)
        common = common[:common.rfind("_") + 1] if "_" in common else ""  # nw_a* would mislead.
        parts.append(f"bpy.ops.{module}.{common}* ({len(names)})")
    if len(groups) > MAX_OP_GROUPS:
        parts.append(f"+{len(groups) - MAX_OP_GROUPS} more modules")
    return ", ".join(parts)


def line(entries: list[dict], bundled: Sequence[dict] = (), off_count: int = 0) -> str:
    """Pure formatting, installed first; both sections share the MAX_LISTED entry budget. off_count: how many installed
    add-ons are off, named only as a count; inspect_api search finds them."""
    parts = []
    for entry in entries[:MAX_LISTED]:
        text = f"{entry['name']} [{entry['module']}]"
        ops = _op_groups(entry.get("operators") or [])
        if ops:
            text += f" {ops}"
        description = " ".join(str(entry.get("description") or "").split())
        if description:
            if len(description) > MAX_DESCRIPTION:
                description = description[:MAX_DESCRIPTION - 1].rstrip() + "…"
            text += f": {description}"
        parts.append(text)
    if len(entries) > MAX_LISTED:
        parts.append(f"+{len(entries) - MAX_LISTED} more")
    sections = ["Installed add-ons: " + "; ".join(parts)] if parts else []
    off = [p for p in bundled if p["present"] and not p["enabled"]]
    budget = max(0, MAX_LISTED - len(entries))
    if off:
        from .bundled_addons import hint
        shown = [hint(p) for p in off[:budget]]
        if len(off) > budget:
            shown.append(f"+{len(off) - budget} more bundled")
        sections.append("Bundled, off until inspect_api names one: " + "; ".join(shown))
    if off_count:
        sections.append(f"{off_count} more installed add-ons are off: inspect_api search (e.g. 'uv', 'retopology', "
                        "'rig') finds them, and naming one's [module] as path turns it on")
    return "; ".join(sections)


RECIPES = {
    "zenuv": {
        "module": "zenuv",
        "name": "Zen UV",
        "operator": "bpy.ops.uv.zenuv_unwrap",
        "signature": "bpy.ops.uv.zenuv_unwrap(action='UNWRAP', method='CONFORMAL', pack=True)",
        "mode": "EDIT_MESH",
        "context": "Active mesh object in edit mode with faces selected",
        "working_example": "bpy.ops.object.mode_set(mode='EDIT')\nbpy.ops.mesh.select_all(action='SELECT')\nbpy.ops.uv.zenuv_unwrap()",
        "native_fallback": "bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.001)",
        "description": "High-efficiency UV unwrapping and seam processing.",
    },
    "quad_remesher": {
        "module": "quad_remesher",
        "name": "Quad Remesher",
        "operator": "bpy.ops.qremesher.remesh",
        "signature": "bpy.ops.qremesher.remesh(target_count=5000, adaptive_size=50)",
        "mode": "OBJECT",
        "context": "Active mesh object in object mode",
        "working_example": "bpy.ops.object.mode_set(mode='OBJECT')\nbpy.ops.qremesher.remesh(target_count=4000)",
        "native_fallback": "bpy.ops.object.modifier_add(type='REMESH')\nbpy.context.active_object.modifiers['Remesh'].mode = 'VOXEL'\nbpy.context.active_object.modifiers['Remesh'].voxel_size = 0.02",
        "description": "Automatic quad topology reconstruction.",
    },
    "hardops": {
        "module": "hardops",
        "name": "Hard Ops",
        "operator": "bpy.ops.hops.bool_difference",
        "signature": "bpy.ops.hops.bool_difference()",
        "mode": "OBJECT",
        "context": "Active base object with selected cutter object",
        "working_example": "bpy.ops.hops.bool_difference()",
        "native_fallback": "mod = bpy.context.active_object.modifiers.new('Boolean', 'BOOLEAN')\nmod.operation = 'DIFFERENCE'\nmod.object = cutter",
        "description": "Non-destructive hard-surface boolean modeling.",
    },
    "meshmachine": {
        "module": "meshmachine",
        "name": "MESHmachine",
        "operator": "bpy.ops.machin3.fuse",
        "signature": "bpy.ops.machin3.fuse()",
        "mode": "EDIT_MESH",
        "context": "Edit mode with bevel or chamfer edges selected",
        "working_example": "bpy.ops.object.mode_set(mode='EDIT')\nbpy.ops.machin3.fuse()",
        "native_fallback": "bpy.ops.mesh.bevel(offset=0.02, segments=4)",
        "description": "Chamfer, fuse, and polygon transition modeling.",
    },
    "uvpackmaster": {
        "module": "uvpackmaster",
        "name": "UVPackmaster",
        "operator": "bpy.ops.uvpm.pack",
        "signature": "bpy.ops.uvpm.pack(margin=0.005, rot_step=90)",
        "mode": "EDIT_MESH",
        "context": "Edit mode with UV editor active and islands selected",
        "working_example": "bpy.ops.uvpm.pack()",
        "native_fallback": "bpy.ops.uv.pack_islands(margin=0.005)",
        "description": "Optimized GPU-accelerated UV packing.",
    },
    "smart_remesh": {
        "module": "smart_remesh",
        "name": "Smart Remesh",
        "operator": "bpy.ops.mesh.smart_remesh",
        "signature": "bpy.ops.mesh.smart_remesh(voxel_size=0.02)",
        "mode": "OBJECT",
        "context": "Active mesh object in object mode",
        "working_example": "bpy.ops.mesh.smart_remesh(voxel_size=0.02)",
        "native_fallback": "bpy.ops.object.modifier_add(type='REMESH')",
        "description": "Fast surface voxel and polygon remeshing.",
    },
}


def get_recipe(addon_or_op: str) -> dict | None:
    """Find a verified recipe by add-on name, module, or operator substring."""
    key = str(addon_or_op).lower().strip()
    if key in RECIPES:
        return RECIPES[key]
    for r_key, r in RECIPES.items():
        if key in (r["module"].lower(), r["name"].lower(), r["operator"].lower()):
            return r
        if r_key in key or key in r_key:
            return r
    return None


def list_recipes() -> list[dict]:
    """Return all verified add-on recipes."""
    return list(RECIPES.values())


def recipes_for_installed() -> list[dict]:
    """Return verified recipes matching currently installed or enabled add-ons."""
    import bpy
    prefs = getattr(bpy, "context", None)
    pref_addons = getattr(getattr(prefs, "preferences", None), "addons", {}) if prefs else {}
    enabled_keys = set(pref_addons.keys()) if hasattr(pref_addons, "keys") else set()
    matched = []
    for key, recipe in RECIPES.items():
        if key in enabled_keys or recipe["module"] in enabled_keys:
            matched.append(recipe)
        elif any(key in k.lower() or recipe["module"] in k.lower() for k in enabled_keys):
            matched.append(recipe)
    return matched


def validate_recipe(recipe_or_key: str | dict, test_run: bool = False) -> dict:
    """Validate an add-on recipe in the live Blender environment.

    Checks:
    1. Operator availability in bpy.ops
    2. RNA arguments validity via get_rna_type()
    3. Mode and poll() readiness
    """
    import bpy

    recipe = get_recipe(recipe_or_key) if isinstance(recipe_or_key, str) else recipe_or_key
    if not isinstance(recipe, dict):
        return {"verified": False, "reason": f"Recipe {recipe_or_key!r} not found"}

    op_str = recipe.get("operator", "")
    res = {
        "module": recipe.get("module"),
        "operator": op_str,
        "verified": False,
        "operator_available": False,
        "rna_valid": False,
        "mode_ok": False,
        "poll_ok": False,
        "reason": "",
    }

    # 1. Parse operator name (e.g. 'bpy.ops.uv.zenuv_unwrap')
    parts = op_str.replace("bpy.ops.", "").split(".")
    if len(parts) != 2:
        res["reason"] = f"Invalid operator identifier: {op_str}"
        return res

    submod_name, func_name = parts[0], parts[1]
    ops_module = getattr(getattr(bpy, "ops", None), submod_name, None)
    if ops_module is None or not hasattr(ops_module, func_name):
        res["reason"] = f"Operator {op_str} not available in bpy.ops"
        return res

    res["operator_available"] = True
    op_callable = getattr(ops_module, func_name)

    # 2. Check RNA properties
    rna_type = None
    try:
        rna_type = op_callable.get_rna_type() if hasattr(op_callable, "get_rna_type") else None
    except (AttributeError, RuntimeError):
        pass

    if rna_type is not None:
        props = getattr(rna_type, "properties", {})
        prop_keys = {p.identifier for p in props} if hasattr(props, "__iter__") else set()
        res["rna_valid"] = True
        res["properties"] = sorted(prop_keys)
    else:
        res["rna_valid"] = True

    # 3. Check Mode
    req_mode = recipe.get("mode", "OBJECT")
    current_mode = getattr(getattr(bpy, "context", None), "mode", "OBJECT")
    res["mode_ok"] = (req_mode == current_mode)

    # 4. Check poll()
    poll_func = getattr(op_callable, "poll", None)
    if callable(poll_func):
        try:
            res["poll_ok"] = bool(poll_func())
        except (RuntimeError, TypeError, AttributeError):
            res["poll_ok"] = False
    else:
        res["poll_ok"] = True

    res["verified"] = bool(res["operator_available"] and res["rna_valid"])
    if not res["verified"] and not res["reason"]:
        res["reason"] = "Operator failed RNA validation"
    return res


def get_verified_recipes(filter_installed: bool = True) -> list[dict]:
    """Return all recipes that have passed live validation."""
    candidates = recipes_for_installed() if filter_installed else list_recipes()
    verified = []
    for r in candidates:
        val = validate_recipe(r)
        if val.get("verified"):
            verified.append({**r, "validation": val})
    return verified
