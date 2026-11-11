#!/usr/bin/env python3
"""Install a conservative GLB export-path recovery in Pixal3D pa.py.

Only accepts a .glb created during the specific mesh() request, with a valid
GLB v2 header, to avoid accidentally processing a stale/older mesh.
Does not alter ComfyUI nodes, render settings, prompt patches or Blender stages.
"""
from __future__ import annotations
import ast
import pathlib
import shutil
import sys

MARKER = '_pa_recover_fresh_glb'
HELPER = '''\n\ndef _pa_recover_fresh_glb(name, started_ns):\n    """Recover an exporter-written GLB not listed in ComfyUI history.\n\n    Never return an older run, a malformed GLB, or a processed game asset.\n    """\n    import struct\n    root = os.path.join(COMFY, "output", "3D")\n    pattern = os.path.join(root, f"{name}_*.glb")\n    matches = []\n    for path in glob.glob(pattern):\n        try:\n            stat = os.stat(path)\n            if stat.st_mtime_ns < started_ns - 2_000_000_000 or stat.st_size < 24:\n                continue\n            if path.endswith(("_lp.glb", "_game.glb", "_repaired.glb")):\n                continue\n            with open(path, "rb") as stream:\n                header = stream.read(20)\n            if len(header) != 20 or header[:4] != b"glTF":\n                continue\n            version, size = struct.unpack("<II", header[4:12])\n            if version != 2 or size != stat.st_size:\n                continue\n            matches.append((stat.st_mtime_ns, path))\n        except (OSError, ValueError):\n            continue\n    if len(matches) == 1:\n        return matches[0][1]\n    if len(matches) > 1:\n        print("  GLB recovery ambiguous: multiple fresh exports:",\n              *[p for _, p in sorted(matches)], sep="\\n    ")\n    return None\n'''


def patch(path):
    target = pathlib.Path(path)
    code = target.read_text(encoding='utf-8')
    if 'def '+MARKER+'(' in code:
        print('Already patched; no changes made.')
        return 0
    tree = ast.parse(code)
    mesh = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name=='mesh'), None)
    if mesh is None:
        raise RuntimeError('No top-level mesh() found; file left unchanged.')
    src = code.splitlines(keepends=True)
    match_idx = None
    for i in range(mesh.lineno-1, mesh.end_lineno):
        if src[i].strip() == 'files = run(p)':
            if match_idx is not None:
                raise RuntimeError('Multiple run() calls in mesh(); file left unchanged.')
            match_idx=i
    if match_idx is None:
        raise RuntimeError('Expected files = run(p) in mesh(); file left unchanged.')
    if not any('import ' in x and 'glob' in x for x in src[:mesh.lineno]):
        raise RuntimeError('Expected glob import missing; file left unchanged.')
    # Add helper just before mesh() and change only mesh()'s run(p) call.
    src.insert(match_idx, '    _pa_mesh_started_ns = time.time_ns()\n')
    # account for new line added immediately above current location
    src[match_idx + 1] = '''    files = run(p)\n    if not any(isinstance(f, str) and f.lower().endswith(".glb") for f in files):\n        _pa_fresh = _pa_recover_fresh_glb(name, _pa_mesh_started_ns)\n        if _pa_fresh:\n            print("  GLB export recovered from this run ->", _pa_fresh)\n            files.append(_pa_fresh)\n'''
    # insert helper at function start AFTER run replacement to avoid line shift
    src.insert(mesh.lineno-1, HELPER)
    patched=''.join(src)
    compile(patched, str(target), 'exec')
    backup=target.with_name(target.name+'.bak-export-recovery')
    if backup.exists():
        raise RuntimeError(f'Backup exists at {backup}; refusing to overwrite.')
    shutil.copy2(target,backup)
    target.write_text(patched,encoding='utf-8')
    print(f'Patched: {target}\nBackup: {backup}\nRun python -m py_compile pa.py')
    return 0


if __name__=='__main__':
    try:
        sys.exit(patch(sys.argv[1] if len(sys.argv)>1 else 'pa.py'))
    except (RuntimeError, SyntaxError, OSError) as exc:
        print(f'NOT PATCHED: {exc}',file=sys.stderr)
        sys.exit(1)
