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
