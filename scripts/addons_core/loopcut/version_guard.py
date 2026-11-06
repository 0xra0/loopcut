"""Stamp imported modules and refuse mixed versions until Blender restarts.

The import hook checks first imports and explicit reloads. The turn boundary also checks cached
modules and the on-disk version, including when no new import would otherwise reach the hook.
"""

import importlib.abc
import importlib.machinery
import re
import sys
from pathlib import Path

MESSAGE = "Loopcut was updated while Blender was open. Restart Blender to finish the update."
_VERSION = re.compile(r'"version":\s*\((\d+),\s*(\d+),\s*(\d+)\)')
# The version the first register() saw. Kept through unregister and register: Blender reloads __init__.py when
# its file changed (addon_utils.enable) but not the modules it imported, so a later bl_info can be newer than them.
_started: tuple | None = globals().get("_started")


class RestartNeeded(ImportError):
    """A module's file on disk is from another Loopcut version than the one running. Not a
    ModuleNotFoundError, which `from . import name` would turn into a vaguer error."""


def version_on_disk(root: Path) -> tuple | None:
    """bl_info's version as root/__init__.py says it now, or None when it cannot be read."""
    try:
        found = _VERSION.search((root / "__init__.py").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    return tuple(int(part) for part in found.groups()) if found else None


class _Finder(importlib.abc.MetaPathFinder):
    _loopcut_version_guard = True

    def __init__(self, package: str, root: Path, version: tuple):
        self.package, self.root, self.version = package, root, version
        self.package_module = sys.modules.get(package)

    def find_spec(self, fullname, path=None, target=None):
        # A reload keeps the root object; an extension purge ends this finder's lifetime.
        if sys.modules.get(self.package) is not self.package_module:
            # A new list, not an in-place removal: the import system may be iterating the old one right now.
            sys.meta_path = [finder for finder in sys.meta_path if finder is not self]
            return None
        if fullname.startswith(self.package + "."):
            found = version_on_disk(self.root)
            if found is not None and found != self.version:
                print(f"Loopcut: {fullname} on disk is {'.'.join(map(str, found))}, this Blender runs "
                      f"{'.'.join(map(str, self.version))}; not loading it")
                raise RestartNeeded(MESSAGE, name=fullname)
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _Loader(spec.loader, self.version)
                return spec
        return None  # The usual finders load it.


class _Loader:
    def __init__(self, loader, version):
        self.loader, self.version = loader, version

    def __getattr__(self, name):
        return getattr(self.loader, name)

    def create_module(self, spec):
        return self.loader.create_module(spec)

    def exec_module(self, module):
        # Set before execution as well: another thread may inspect an import in progress.
        module.__loopcut_version__ = self.version
        self.loader.exec_module(module)


def register(package: str, root: Path, version) -> None:
    global _started
    _started = _started or tuple(version)
    unregister(package)
    # Only these two modules bootstrap the hook. Unstamped cached children are untrusted,
    # rather than being relabelled with the new initializer's version on re-enable.
    for name in (package, package + ".version_guard"):
        module = sys.modules.get(name)
        if module is not None:
            module.__dict__.setdefault("__loopcut_version__", tuple(version))
    sys.meta_path.insert(0, _Finder(package, Path(root), _started))
    agent = sys.modules.get(package + ".agent")
    if agent is not None and getattr(agent, "__loopcut_version__", None) != _started:
        # Legacy cached entry points predate the turn guard. They must not run a new turn
        # while the newly registered package contains unstamped or mismatched modules.
        def refuse(*args, **kwargs):
            state = importlib.import_module(package + ".state")
            allow_turn(state.session(), package)
            return False
        for name in ("send", "resume", "turn_into_3d"):
            if callable(getattr(agent, name, None)):
                setattr(agent, name, refuse)


def unregister(package: str) -> None:
    sys.meta_path[:] = [f for f in sys.meta_path if not (
        (getattr(f, "_loopcut_version_guard", False) or
         (type(f).__module__ == __name__ and type(f).__name__ == "_Finder")) and f.package == package)]


def ensure_consistent(package: str = __package__) -> None:
    finder = next((f for f in sys.meta_path if getattr(f, "_loopcut_version_guard", False)
                   and f.package == package), None)
    if finder is None:  # Pure helpers can also be used without enabling the add-on.
        return
    found = version_on_disk(finder.root)
    if found is not None and found != finder.version:
        raise RestartNeeded(MESSAGE, name=package)
    for name, module in sys.modules.copy().items():
        if (name == package or name.startswith(package + ".")) and module is not None:
            if getattr(module, "__loopcut_version__", None) != finder.version:
                raise RestartNeeded(MESSAGE, name=name)


def allow_turn(session: dict, package: str = __package__) -> bool:
    try:
        ensure_consistent(package)
    except RestartNeeded:
        state = importlib.import_module(package + ".state")
        if not session["items"] or session["items"][-1].get("text") != MESSAGE:
            session["items"].append(state.item_error(MESSAGE))
        return False
    return True
