"""Shading health: whether a mesh renders the way its shape asks, measured from its normals rather than
judged from a picture (game-asset-quality §3).

Cloud retopology results shipped with flat per-face custom normals and a normal map baked against them:
96.6% of corner normals equal their face normal and 94% of edges import hard. Left alone they show
faceted highlights in engines; smoothed or subdivided they print a grid of quads, because the map
still bends light for the flat normals it was baked against. Models skip advice found in tool
results, so Loopcut runs this itself: after a cloud model import (cloud_jobs._model_import), before
every engine export (engine_export.py), in the advisory "shading" kind (acceptance.py) and as the
read-only check_shading tool.

What it measures, on the mesh's own data (before modifiers, which it lists instead):
- flat: the share of corners on a curved part of the surface (welded smooth normal more than CURVED_DEG
  from the face's) whose final normal is within FLAT_DEG of their face normal; with custom normals
  present, "flat custom normals";
- hard: the share of edges whose two faces' corner normals differ (what renders as a hard line: sharp
  edges, split vertices, custom normals alike); stray: hard edges across nearly flat surface that are
  not UV seams; smeared: edges sharper than SMEAR_DEG that render smooth;
- shape: organic, hard_surface, faceted (low-poly, every angle wide) or mixed, from the dihedral
  angles of edges between faces, positions welded first (a glTF import splits vertices at every
  hard edge and UV seam);
- maps: tangent-space normal map images wired into its materials;
- its bake record: what an import stamped (job, source high-poly, a fingerprint of shape, UVs and
  normals), so a mesh changed after its bake is told to re-bake from the source.

What it does: only the safe fix (organic, flat custom normals, no normal map baked against them):
clear the custom normals, shade smooth without keeping sharp edges, welding split vertices first.
A baked pair is never smoothed. Hard-surface meshes keep their hard edges where faces meet at an
angle or at UV seams; only clear problems are reported. Everything else is words, never a refusal.

The top half is pure numpy over plain arrays (tests/test_shading.py); harness/shading_check.py runs
the rest in Blender.
"""

import hashlib
import json
import math
import re

FLAT_DEG = 1.0        # A corner normal this close to its face normal is flat.
CURVED_DEG = 3.0      # A corner whose welded smooth normal is this far from its face normal sits on a curve.
HARD_DEG = 2.0        # Corner normals across an edge further apart than this render a hard line.
COPLANAR_DEG = 0.1    # Dihedral angles below this are flat panels (or triangulated quads).
CREASE_DEG = 30.0     # And from this up creases, as Blender's smooth-by-angle default.
SMEAR_DEG = 60.0      # An edge this sharp rendering smooth smears the shading across it.
STRAY_DEG = 10.0      # A hard edge flatter than this, on no UV seam, is a stray line.
FLAT_CUSTOM = 0.9     # Curved corners flat from this share up: flat custom normals.
MOSTLY_SMOOTH = 0.5   # Stray and smeared edges are only counted on a mesh flat below this share.
ORGANIC_CURVED, ORGANIC_CREASE, ORGANIC_MEDIAN = 0.6, 0.08, 20.0
FACETED_COPLANAR, FACETED_STEEP, STEEP_DEG = 0.2, 0.1, 75.0
HARD_CREASE, HARD_COPLANAR = 0.1, 0.5
MIN_EDGES = 24        # Fewer edges between two faces: too small to say what shape it is.
UV_TOL = 1e-5
MAX_CORNERS = 3_000_000   # Bigger meshes are named, not measured (arrays of a few hundred MB).
MAX_OBJECTS = 12          # Meshes one check_shading call measures; the rest are counted.
MAX_LINES = 8             # Problem lines one import or export reports.
RECORD = "loopcut_bake"               # JSON: the bake an import recorded (stamp).
SOURCE_JOB = "loopcut_source_job"     # The cloud job a mesh came from.
HIGH_SHA = "loopcut_high_sha256"      # Its source high-poly blob (a retopology's files.model), when known.
FIXED = "loopcut_shading_fix"         # What the safe fix did, when it ran.
# Images an earlier retopology result carried (its normal map was baked against flat normals).
_LEGACY_MAP = re.compile(r"(?i)^retopo_.*_normal")
_SUBDIVIDES = {"SUBSURF", "MULTIRES"}
_RESHADES = {"WEIGHTED_NORMAL", "NORMAL_EDIT", "NODES", "SMOOTH", "CORRECTIVE_SMOOTH", "LAPLACIANSMOOTH"}

SCHEMAS = [{"type": "function", "function": {  # Sent with every request: every word here is paid for on every step.
    "name": "check_shading",
    "title": "Checking shading",
    "description": ("Read-only: whether meshes shade as their shape asks (flat custom normals, hard edges, normal "
                    "maps, changed since its bake), each problem with its fix."),
    "parameters": {"type": "object", "properties": {
        "object": {"type": "string", "description": "With children; default selection, else visible meshes"},
    }},
}}]


# ------------------------------------------------------------------ pure

def _unit(vectors):
    import numpy as np
    length = np.linalg.norm(vectors, axis=1)
    out = np.zeros_like(vectors)
    good = length > 1e-12
    out[good] = vectors[good] / length[good, None]
    return out, length


def topology(loop_start, loop_total, n_corners: int):
    """(corner_face, next_corner) of a mesh whose corners run face by face (Blender's face offsets)."""
    import numpy as np
    loop_start = np.asarray(loop_start, dtype=np.int64)
    loop_total = np.asarray(loop_total, dtype=np.int64)
    corner_face = np.repeat(np.arange(len(loop_total)), loop_total)
    starts = np.repeat(loop_start, loop_total)
    offset = np.arange(n_corners) - starts
    return corner_face, starts + (offset + 1) % np.repeat(loop_total, loop_total)


def face_normals(positions, loop_start, corner_verts, nxt):
    """(unit normal, area) per face by Newell's method: right for any polygon, flat or not."""
    import numpy as np
    cross = np.cross(positions[corner_verts], positions[corner_verts[nxt]])
    newell = np.add.reduceat(cross, np.asarray(loop_start, dtype=np.int64), axis=0)
    normals, length = _unit(newell)
    return normals, length / 2


def weld(positions):
    """Each vertex's welded id: vertices at one position (to a millionth of the mesh's size) are one."""
    import numpy as np
    if not len(positions):
        return np.zeros(0, dtype=np.int64)
    size = float(np.linalg.norm(positions.max(axis=0) - positions.min(axis=0)))
    tol = max(size, 1e-3) * 1e-6
    _, inverse = np.unique(np.round(positions / tol).astype(np.int64), axis=0, return_inverse=True)
    return inverse.reshape(-1)


def classify(dihedral) -> dict:
    """The shape the angles between faces say: organic (curved nearly everywhere, few creases, small
    angles); faceted (bent everywhere by wide angles but seldom square: a low-poly ball or rock);
    hard_surface (creases, or flat panels); else mixed; "unknown" below MIN_EDGES edges."""
    import numpy as np
    n = len(dihedral)
    if n < MIN_EDGES:
        return {"shape": "unknown", "coplanar": 0.0, "curved": 0.0, "crease": 0.0, "median": 0.0}
    coplanar = float((dihedral < COPLANAR_DEG).mean())
    crease = float((dihedral >= CREASE_DEG).mean())
    bent = dihedral[dihedral >= COPLANAR_DEG]
    median = float(np.median(bent)) if len(bent) else 0.0
    curved = 1.0 - coplanar - crease
    steep = float((dihedral >= STEEP_DEG).mean())
    if curved >= ORGANIC_CURVED and crease <= ORGANIC_CREASE and median <= ORGANIC_MEDIAN:
        shape = "organic"
    elif coplanar < FACETED_COPLANAR and steep < FACETED_STEEP and median > ORGANIC_MEDIAN * 0.75:
        shape = "faceted"
    elif crease >= HARD_CREASE or coplanar >= HARD_COPLANAR:
        shape = "hard_surface"
    else:
        shape = "mixed"
    return {"shape": shape, "coplanar": round(coplanar, 3), "curved": round(curved, 3), "crease": round(crease, 3),
            "median": round(median, 2)}


def analyse(positions, loop_start, loop_total, corner_verts, corner_normals, uvs=None) -> dict:
    """The facts of one mesh from plain arrays: positions (V, 3), loop_start and loop_total per face,
    corner_verts (C), corner_normals (C, 3) as it renders, uvs (C, 2) of its render UV map or None."""
    import numpy as np
    positions = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    corner_verts = np.asarray(corner_verts, dtype=np.int64)
    normals, _ = _unit(np.asarray(corner_normals, dtype=np.float64).reshape(-1, 3))
    n_corners = len(corner_verts)
    fact = {"faces": int(len(loop_total)), "corners": n_corners, "edges": 0, "open": 0, "flat": None,
            "flat_all": 0.0, "curved_corners": 0, "hard": 0.0, "hard_edges": 0, "stray": 0, "smeared": 0,
            "creases": 0, "seams": 0, "split": 0}
    if not n_corners:
        fact.update(classify(np.zeros(0)))
        return fact
    corner_face, nxt = topology(loop_start, loop_total, n_corners)
    fn, area = face_normals(positions, loop_start, corner_verts, nxt)
    welded = weld(positions)
    wc = welded[corner_verts]
    # Smooth normals of the welded surface, area-weighted, to tell curved corners from flat panels.
    weighted = fn * area[:, None]
    count = int(welded.max()) + 1
    smooth = np.stack([np.bincount(wc, weights=weighted[corner_face, k], minlength=count) for k in range(3)], axis=1)
    smooth, _ = _unit(smooth)
    face_at = fn[corner_face]
    flat = (normals * face_at).sum(axis=1) >= math.cos(math.radians(FLAT_DEG))
    curved = (smooth[wc] * face_at).sum(axis=1) < math.cos(math.radians(CURVED_DEG))
    fact["flat_all"] = round(float(flat.mean()), 4)
    fact["curved_corners"] = int(curved.sum())
    if curved.sum() >= 16:
        fact["flat"] = round(float(flat[curved].mean()), 4)
    # Edges between two faces of the welded surface: each face's side of it is a half-edge (a corner
    # and the next), with the corners at its lower and higher welded vertex.
    a, b = wc, wc[nxt]
    half = np.nonzero(a != b)[0]
    low, high = np.minimum(a[half], b[half]), np.maximum(a[half], b[half])
    at_low = np.where(a[half] <= b[half], half, nxt[half])
    at_high = np.where(a[half] <= b[half], nxt[half], half)
    key = low * count + high
    order = np.argsort(key, kind="stable")
    _, first, counts = np.unique(key[order], return_index=True, return_counts=True)
    fact["open"] = int((counts == 1).sum())
    one, two = order[first[counts == 2]], order[first[counts == 2] + 1]
    f1, f2 = corner_face[half[one]], corner_face[half[two]]
    keep = f1 != f2
    one, two, f1, f2 = one[keep], two[keep], f1[keep], f2[keep]
    fact["edges"] = int(len(one))
    if not len(one):
        fact.update(classify(np.zeros(0)))
        return fact
    dihedral = np.degrees(np.arccos(np.clip((fn[f1] * fn[f2]).sum(axis=1), -1.0, 1.0)))
    cos_hard = math.cos(math.radians(HARD_DEG))
    hard = (((normals[at_low[one]] * normals[at_low[two]]).sum(axis=1) < cos_hard)
            | ((normals[at_high[one]] * normals[at_high[two]]).sum(axis=1) < cos_hard))
    if uvs is not None and len(uvs) == n_corners:
        uvs = np.asarray(uvs, dtype=np.float64).reshape(-1, 2)
        seam = ((np.abs(uvs[at_low[one]] - uvs[at_low[two]]).max(axis=1) > UV_TOL)
                | (np.abs(uvs[at_high[one]] - uvs[at_high[two]]).max(axis=1) > UV_TOL))
    else:
        seam = np.zeros(len(one), dtype=bool)
    split = ((corner_verts[at_low[one]] != corner_verts[at_low[two]])
             | (corner_verts[at_high[one]] != corner_verts[at_high[two]]))
    fact.update(classify(dihedral))
    fact["hard"] = round(float(hard.mean()), 4)
    fact["hard_edges"] = int(hard.sum())
    fact["stray"] = int((hard & (dihedral < STRAY_DEG) & ~seam).sum())
    fact["smeared"] = int((~hard & (dihedral >= SMEAR_DEG)).sum())
    fact["creases"] = int((dihedral >= SMEAR_DEG).sum())
    fact["seams"] = int(seam.sum())
    fact["split"] = int(split.sum())
    return fact


def digests(positions, loop_total, corner_verts, uvs=None, normals=None) -> dict:
    """A cheap fingerprint of what a bake depends on: shape (positions to 0.1 mm, faces and their
    corners), UVs (to 1e-5) and normals (to 1e-3), 16 hex digits each."""
    import numpy as np

    def digest(*arrays) -> str:
        h = hashlib.blake2b(digest_size=8)
        for array in arrays:
            h.update(np.ascontiguousarray(array).tobytes())
        return h.hexdigest()
    positions = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    out = {"verts": int(len(positions)), "faces": int(len(loop_total)),
           "shape": digest(np.round(positions * 1e4).astype("<i8"), np.asarray(loop_total, dtype="<i4"),
                           np.asarray(corner_verts, dtype="<i4"))}
    out["uv"] = digest(np.round(np.asarray(uvs, dtype=np.float64) * 1e5).astype("<i8")) if uvs is not None else ""
    out["normals"] = digest(np.round(np.asarray(normals, dtype=np.float64) * 1e3).astype("<i4")) if normals is not None else ""
    return out


def changes(recorded: dict | None, now: dict) -> list[str]:
    """What differs from the fingerprint an import recorded: "shape", "UVs", "normals"; [] without a record."""
    if not isinstance(recorded, dict) or not recorded.get("shape"):
        return []
    found = []
    if recorded.get("shape") != now.get("shape") or recorded.get("verts") != now.get("verts"):
        found.append("shape")
    if recorded.get("uv") and recorded.get("uv") != now.get("uv"):
        found.append("UVs")
    if recorded.get("normals") and recorded.get("normals") != now.get("normals"):
        found.append("normals")
    return found


def _pct(value) -> str:
    return f"{value:.0%}" if value is not None else "n/a"


def _source(record: dict | None, name: str = "", rebake: str = "") -> str:
    """The re-bake advice; rebake: the cloud tool's name when the catalog offers it and this mesh has a source it
    can bake from (rebaked_from), so the fix names it."""
    job = (record or {}).get("job")
    if rebake and name:
        return (f"Fix: {rebake} with model={name!r} bakes fresh maps from its source"
                + (f" (source job {job})." if job else "."))
    return f"Re-bake its maps from the source model (source job {job})." if job else \
        "Re-bake its maps from the source high-poly model."


# The cloud tools whose result was baked from a high-poly the service keeps, and that file's field in the job.
BAKED_FROM = {"retopology": "model", "rebake": "high"}


def rebaked_from(record: dict | None) -> bool:
    """Whether the re-bake tool can work on a mesh with this record: it names its source high-poly, or came
    from a job that kept one."""
    record = record or {}
    return bool(record.get("high_sha256")) or (record.get("tool") in BAKED_FROM and bool(record.get("job")))


def verdict(name: str, fact: dict, *, custom: bool, maps=(), record=None, changed=(), subdivision=(),
            reshading=(), rebake: str = "") -> dict:
    """What to say about one mesh: {"fix": the safe fix applies, "problems": [...], "notes": [...]},
    plain words, each problem with its fix. rebake: the re-bake tool's name when the catalog offers it."""
    problems, notes = [], []
    rebake = rebake if rebaked_from(record) else ""
    flat = fact.get("flat")
    flat_custom = custom and flat is not None and flat >= FLAT_CUSTOM
    legacy = any(_LEGACY_MAP.match(m) for m in maps)
    fix = flat_custom and not maps and fact.get("shape") == "organic"
    shown = ", ".join(list(maps)[:2])
    if flat_custom and maps:
        what = "an earlier retopology result: " if legacy else ""
        lead = "Smoothing" if fact.get("shape") == "hard_surface" else \
            "Highlights look faceted in engines, and smoothing"
        line = (f"{name}: {what}its normal map ({shown}) was baked against flat normals ({flat:.0%} of its curved "
                f"corners). {lead} or subdividing it prints a grid of quads, so it was left as it is. "
                f"{_source(record, name, rebake)}")
        if subdivision:
            line += f" Its {', '.join(subdivision)} modifier already shows that grid."
        problems.append(line)
    elif fix:
        problems.append(
            f"{name}: its custom normals are flat on a curved surface ({flat:.0%} of its curved corners), so every "
            f"face shows. Fix (run_python): m = bpy.data.objects[{name!r}].data; "
            f"[m.attributes.remove(m.attributes[n]) for n in ('custom_normal', 'sharp_edge', 'sharp_face') "
            f"if n in m.attributes]" + ("; merge its split vertices first (Merge by Distance)" if fact.get("split") else ""))
    if changed and (maps or (record or {}).get("maps")):
        problems.append(f"{name}: changed after its bake ({', '.join(changed)}), so its normal map no longer "
                        f"matches. {_source(record, name, rebake)}")
    if not reshading and not custom and flat is not None and flat < MOSTLY_SMOOTH:
        edges = fact.get("edges", 0)
        if fact.get("stray", 0) >= max(8, 0.1 * edges):
            problems.append(f"{name}: {fact['stray']} hard edges cross nearly flat surface (under {STRAY_DEG:g}°, "
                            f"on no UV seam), so lines show where it should look smooth. Fix (run_python): "
                            f"bpy.data.objects[{name!r}].data.set_sharp_from_angle(angle=0.5236)")
        if fact.get("shape") == "hard_surface" and fact.get("smeared", 0) >= max(4, 0.5 * fact.get("creases", 0)):
            problems.append(f"{name}: shading smears across {fact['smeared']} of its {fact['creases']} edges sharper "
                            f"than {SMEAR_DEG:g}°, so its flat sides look dented. Fix (run_python): "
                            f"bpy.data.objects[{name!r}].data.set_sharp_from_angle(angle=0.5236), its faces smooth")
    if (not custom and flat is not None and flat >= 0.95 and fact.get("shape") == "organic"
            and not problems):
        notes.append(f"{name}: flat shaded on a curved surface (a faceted look). If it should look smooth: shade "
                     f"it smooth.")
    return {"fix": bool(fix), "problems": problems, "notes": notes}


def summary(name: str, fact: dict, *, custom: bool, maps=(), record=None, changed=(), modifiers=()) -> str:
    """One mesh's facts on one line, for check_shading."""
    flat = fact.get("flat")
    if flat is None:
        normals = "custom normals, no curved surface" if custom else "no curved surface"
    else:
        normals = f"{'custom normals' if custom else 'normals'} flat on {_pct(flat)} of curved corners"
    parts = [fact.get("shape", "unknown").replace("_", "-"), f"{fact['faces']:,} faces", normals,
             f"{fact.get('hard', 0):.0%} of edges hard"]
    parts.append(f"normal map {', '.join(list(maps)[:2])}" if maps else "no normal map")
    if record:
        parts.append(f"bake: changed ({', '.join(changed)})" if changed else "bake: matches its record")
    if modifiers:
        parts.append(f"measured before its modifiers {', '.join(modifiers[:3])}")
    return f"{name}: " + "; ".join(parts)


# ------------------------------------------------------------------ Blender (main thread)

def arrays(mesh) -> dict:
    """The arrays analyse() and digests() read, from a mesh in Object Mode."""
    import numpy as np
    n_v, n_f, n_c = len(mesh.vertices), len(mesh.polygons), len(mesh.loops)
    positions = np.empty(n_v * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", positions)
    loop_start = np.empty(n_f, dtype=np.int32)
    loop_total = np.empty(n_f, dtype=np.int32)
    mesh.polygons.foreach_get("loop_start", loop_start)
    mesh.polygons.foreach_get("loop_total", loop_total)
    corner_verts = np.empty(n_c, dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", corner_verts)
    normals = np.empty(n_c * 3, dtype=np.float32)
    mesh.corner_normals.foreach_get("vector", normals)
    uvs = None
    layer = next((layer for layer in mesh.uv_layers if layer.active_render), None) or mesh.uv_layers.active
    if layer is not None and n_c:
        uvs = np.empty(n_c * 2, dtype=np.float32)
        layer.uv.foreach_get("vector", uvs)
        uvs = uvs.reshape(-1, 2)
    if n_f and np.any(np.diff(loop_start) < 0):  # Faces whose corners are not in face order: put them in order.
        order = np.argsort(loop_start, kind="stable")
        index = np.concatenate([np.arange(s, s + t) for s, t in zip(loop_start[order], loop_total[order])])
        corner_verts, normals = corner_verts[index], normals.reshape(-1, 3)[index].reshape(-1)
        uvs = uvs[index] if uvs is not None else None
        loop_total = loop_total[order]
        loop_start = np.concatenate([[0], np.cumsum(loop_total)[:-1]]).astype(np.int32)
    return {"positions": positions.reshape(-1, 3), "loop_start": loop_start, "loop_total": loop_total,
            "corner_verts": corner_verts, "normals": normals.reshape(-1, 3), "uvs": uvs}


def normal_maps(ob) -> list[str]:
    """Images wired into its materials as tangent-space normal maps: an Image Texture into a Normal Map
    node's Color, that node's Normal output used."""
    names = []
    for slot in getattr(ob, "material_slots", ()):
        material = slot.material
        if material is None or material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            if node.bl_idname != "ShaderNodeNormalMap" or node.mute or getattr(node, "space", "TANGENT") != "TANGENT":
                continue
            if not any(link.to_node is not None and not link.is_muted for link in node.outputs["Normal"].links):
                continue
            for link in node.inputs["Color"].links:
                source = link.from_node
                if source.bl_idname == "ShaderNodeGroup" and source.get("loopcut_detail") and source.inputs["Base"].links:
                    source = source.inputs["Base"].links[0].from_node  # A detail normal over it (detail_maps.py).
                if source.bl_idname == "ShaderNodeTexImage" and source.image is not None and not source.mute:
                    names.append(source.image.name)
    return list(dict.fromkeys(names))


def record(ob) -> dict | None:
    """The bake an import recorded on it (stamp), or None."""
    raw = ob.get(RECORD)
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _custom(mesh) -> bool:
    return bool(getattr(mesh, "has_custom_normals", False)) or "custom_normal" in mesh.attributes


def _rebake_tool() -> str:
    """The re-bake cloud tool's name when the account's catalog offers it, else ""."""
    try:
        from . import rebake
        return rebake.tool_name() if rebake.offered() else ""
    except Exception:  # noqa: BLE001 - advice without the tool's name, never a failed check.
        return ""


def measure(ob) -> dict:
    """One mesh object: {"name", "fact", "custom", "maps", "record", "changed", "modifiers", "verdict"},
    or {"name", "skipped": why}."""
    mesh = ob.data
    if ob.mode == "EDIT":
        return {"name": ob.name, "skipped": "in Edit Mode; leave it to measure"}
    if not len(mesh.polygons):
        return {"name": ob.name, "skipped": "no faces"}
    if len(mesh.loops) > MAX_CORNERS:
        return {"name": ob.name, "skipped": f"{len(mesh.polygons):,} faces, too many to measure here"}
    data = arrays(mesh)
    fact = analyse(data["positions"], data["loop_start"], data["loop_total"], data["corner_verts"], data["normals"],
                   data["uvs"])
    custom = _custom(mesh)
    maps = normal_maps(ob)
    bake = record(ob)
    changed = changes(bake, digests(data["positions"], data["loop_total"], data["corner_verts"], data["uvs"],
                                    data["normals"])) if bake else []
    live = [m for m in ob.modifiers if m.show_viewport or m.show_render]
    subdivision = [m.name for m in live if m.type in _SUBDIVIDES]
    reshading = [m.name for m in live if m.type in _RESHADES or m.type in _SUBDIVIDES]
    said = verdict(ob.name, fact, custom=custom, maps=maps, record=bake, changed=changed, subdivision=subdivision,
                   reshading=reshading, rebake=_rebake_tool())
    return {"name": ob.name, "fact": fact, "custom": custom, "maps": maps, "record": bake, "changed": changed,
            "modifiers": [m.name for m in live], "verdict": said}


def smooth(ob) -> str:
    """The safe fix: weld split vertices (a glTF import splits them at every hard edge), clear custom
    normals, shade smooth without sharp edges. Says what it did; recorded on the object (FIXED)."""
    text = ", ".join(smooth_mesh(ob.data))
    ob[FIXED] = text
    return text


def smooth_mesh(mesh) -> list[str]:
    """One smooth surface, as the cloud bakes shade their target (bake_common.smooth_shading): exact duplicate
    vertices welded, custom normals, sharp edges and flat faces cleared. What it did, in words."""
    import bmesh
    done = []
    data = arrays(mesh)
    fact = analyse(data["positions"], data["loop_start"], data["loop_total"], data["corner_verts"], data["normals"])
    if fact["split"]:
        points = data["positions"]
        size = float(((points.max(axis=0) - points.min(axis=0)) ** 2).sum() ** 0.5)
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            before = len(bm.verts)
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=max(size, 1e-3) * 1e-6)
            merged = before - len(bm.verts)
            bm.to_mesh(mesh)
        finally:
            bm.free()
        if merged:
            done.append(f"welded {merged:,} split vertices")
    removed = [n for n in ("custom_normal", "sharp_edge", "sharp_face") if n in mesh.attributes]
    for name in removed:
        mesh.attributes.remove(mesh.attributes[name])
    if "custom_normal" in removed:
        done.append("cleared its custom normals")
    if "sharp_edge" in removed:
        done.append("cleared its sharp edges")
    done.append("shaded it smooth")
    mesh.update()
    return done


def check(objects, fix: bool = False) -> list[dict]:
    """measure() each mesh among objects (each once); fix: run the safe fix where it applies (an
    import or an export), and say so in its verdict."""
    results, seen = [], set()
    for ob in objects:
        if ob is None or ob.type != "MESH" or ob.data is None or ob.session_uid in seen:
            continue
        seen.add(ob.session_uid)
        try:
            result = measure(ob)
            if fix and result.get("verdict", {}).get("fix"):
                did = smooth(ob)
                result = {**measure(ob), "did": did}
                result["verdict"]["notes"].insert(0, f"{ob.name}: its custom normals were flat on a curved, organic "
                                                     f"surface, so every face showed: {did}.")
        except Exception as ex:  # noqa: BLE001 - a measuring bug costs its line, never the import or the export.
            result = {"name": ob.name, "skipped": f"not measured ({type(ex).__name__}: {ex})"[:160]}
        results.append(result)
    return results


def lines(results, with_notes: bool = True, limit: int = MAX_LINES) -> list[str]:
    """The problems (and notes) of check() results, at most limit lines, the rest counted."""
    said = []
    for result in results:
        if "verdict" not in result:
            continue
        said += result["verdict"]["problems"]
        if with_notes:
            said += result["verdict"]["notes"]
    said = list(dict.fromkeys(said))
    if len(said) > limit:
        said = said[:limit] + [f"and {len(said) - limit} more like these"]
    return said


def stamp(ob, job_id: str = "", tool: str = "", high_sha256: str = "", space=None) -> None:
    """Record its bake on an imported mesh object: source job, source high-poly, fingerprint, maps, and the space
    it was baked in (space: 16 numbers, row by row; default its world matrix, as it is when an import records it,
    its import's root still at the origin). A re-bake sends the mesh in that space (rebake.py)."""
    data = arrays(ob.data)
    if space is None:
        space = [round(float(v), 7) for row in ob.matrix_world for v in row]
    value = {"job": job_id, "tool": tool, **digests(data["positions"], data["loop_total"], data["corner_verts"],
                                                    data["uvs"], data["normals"]),
             "maps": normal_maps(ob), "space": list(space)}
    fact = analyse(data["positions"], data["loop_start"], data["loop_total"], data["corner_verts"], data["normals"])
    value["flat"] = fact["flat"] if _custom(ob.data) else None
    if high_sha256:
        value["high_sha256"] = high_sha256
        ob[HIGH_SHA] = high_sha256
    if job_id:
        ob[SOURCE_JOB] = job_id
    ob[RECORD] = json.dumps(value, sort_keys=True)


def after_import(objects, job_id: str = "", tool: str = "", high_sha256: str = "") -> list[str]:
    """After a cloud model import: each mesh's bake recorded (its job named in what is said, a later
    change seen), the safe fix where it applies (and the record taken again after it), then what to
    say: problems and fixes, plain words."""
    meshes = [ob for ob in objects if ob.type == "MESH" and ob.data is not None and len(ob.data.polygons)]

    def record_all(which):
        for ob in which:
            try:
                if len(ob.data.loops) <= MAX_CORNERS:
                    stamp(ob, job_id, tool, high_sha256)
            except Exception:  # noqa: BLE001 - no record is a missing hint later, not a failed import.
                pass
    record_all(meshes)
    results = check(meshes, fix=True)
    fixed = {result["name"] for result in results if result.get("did")}
    record_all([ob for ob in meshes if ob.name in fixed])
    return [f"Shading: {line}" for line in lines(results)]


def _targets(name):
    import bpy
    from .tools import ToolError
    if name:
        ob = bpy.data.objects.get(str(name))
        if ob is None:
            raise ToolError(f"No object named {name!r}.")
        return [ob, *ob.children_recursive]
    layer = bpy.context.view_layer
    present = [ob for ob in layer.objects if ob is not None and ob.type == "MESH"]
    return [ob for ob in present if ob.select_get()] or [ob for ob in present if ob.visible_get()]


def check_shading(object: str | None = None):  # noqa: A002 - the schema's name for it.
    """The tool (see SCHEMAS and the module docstring): read-only."""
    from .tools import ToolResult
    targets = [ob for ob in _targets(object) if ob.type == "MESH"]
    if not targets:
        return ToolResult("No mesh to check" + (f" under {object}." if object else " is selected or visible."))
    results = check(targets[:MAX_OBJECTS])
    out = []
    for result in results:
        if "skipped" in result:
            out.append(f"{result['name']}: {result['skipped']}")
            continue
        out.append(summary(result["name"], result["fact"], custom=result["custom"], maps=result["maps"],
                           record=result["record"], changed=result["changed"], modifiers=result["modifiers"]))
        out += [f"  Problem: {p}" for p in result["verdict"]["problems"]]
        out += [f"  Note: {n}" for n in result["verdict"]["notes"]]
    if len(targets) > MAX_OBJECTS:
        out.append(f"and {len(targets) - MAX_OBJECTS} more meshes not checked; name one to check it")
    clean = sum(1 for r in results if "verdict" in r and not r["verdict"]["problems"])
    head = f"Checked {len(results)} mesh{'es' * (len(results) != 1)}: {clean} without a problem."
    return ToolResult("\n".join([head, *out]))


DISPATCH = {"check_shading": check_shading}
