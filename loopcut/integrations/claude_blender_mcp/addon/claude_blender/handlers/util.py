"""Helpers shared by the toolsets."""

import contextlib
import math

import bmesh
import bpy
from mathutils import Vector

from . import CommandError

MESH_COMPONENTS = ("verts", "edges", "faces")


def get_object(name, obj_type=None):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise CommandError(f"No object named {name!r}")
    if obj_type and obj.type != obj_type:
        raise CommandError(f"{name!r} is a {obj.type}, expected {obj_type}")
    return obj


def get_mesh(name):
    return get_object(name, "MESH")


def vec(value, default=(0.0, 0.0, 0.0), size=3):
    if value is None:
        return Vector(default)
    if isinstance(value, (int, float)):
        return Vector((float(value),) * size)
    if len(value) != size:
        raise CommandError(f"Expected {size} numbers, got {value!r}")
    return Vector([float(v) for v in value])


def radians(values):
    """Rotations in the API are degrees; Blender stores radians."""
    return [math.radians(v) for v in values]


def link_to_scene(obj, collection=None):
    coll = bpy.data.collections.get(collection) if collection else bpy.context.scene.collection
    if coll is None:
        coll = bpy.data.collections.new(collection)
        bpy.context.scene.collection.children.link(coll)
    coll.objects.link(obj)
    return obj


def ensure_object_mode():
    if bpy.context.mode != "OBJECT" and bpy.context.view_layer.objects.active:
        with ops_context(bpy.context.view_layer.objects.active):
            bpy.ops.object.mode_set(mode="OBJECT")


@contextlib.contextmanager
def ops_context(active, selected=None, mode=None):
    """Make `active` the active, selected object (plus `selected`) for bpy.ops calls.

    Works from timers and headless. Restores selection, active object and mode afterwards.
    """
    view_layer = bpy.context.view_layer
    selected = list(selected or []) + [active]
    prev_active = view_layer.objects.active
    prev_selected = [o for o in view_layer.objects if o.select_get()]
    prev_mode = active.mode
    for o in prev_selected:
        o.select_set(False)
    for o in selected:
        o.select_set(True)
    view_layer.objects.active = active
    override = {
        "active_object": active,
        "object": active,
        "selected_objects": selected,
        "selected_editable_objects": selected,
    }
    wm = bpy.context.window_manager
    if wm and wm.windows:
        override["window"] = wm.windows[0]
    try:
        with bpy.context.temp_override(**override):
            if mode and active.mode != mode:
                bpy.ops.object.mode_set(mode=mode)
            yield
            if active.mode != prev_mode:
                bpy.ops.object.mode_set(mode=prev_mode if prev_mode != "EDIT" else "OBJECT")
    finally:
        for o in view_layer.objects:
            o.select_set(False)
        # Objects may have been removed by the operator (e.g. join), so check before touching them.
        for o in prev_selected:
            with contextlib.suppress(ReferenceError):
                if o.name in view_layer.objects:
                    o.select_set(True)
        with contextlib.suppress(ReferenceError):
            if prev_active and prev_active.name in view_layer.objects:
                view_layer.objects.active = prev_active


def apply_modifiers(obj, names=None):
    """Bake modifiers into the mesh through the data API (no operator context needed).

    names=None applies every modifier enabled in the viewport; otherwise only those named,
    in stack order. The remaining modifiers stay on the object.
    """
    if obj.type != "MESH":
        raise CommandError(f"Can only apply modifiers on meshes, {obj.name!r} is {obj.type}")
    mods = list(obj.modifiers)
    if names is not None:
        missing = set(names) - {m.name for m in mods}
        if missing:
            raise CommandError(f"No modifiers {sorted(missing)} on {obj.name!r}")
        # Modifiers below the last requested one can't be "applied first" without changing
        # the result, so only a prefix of the stack is allowed.
        last = max(i for i, m in enumerate(mods) if m.name in names)
        prefix = {m.name for m in mods[: last + 1]}
        if prefix != set(names):
            raise CommandError("Apply modifiers in stack order: include every modifier above the last one you name")
    to_apply = [m for m in mods if (names is None and m.show_viewport) or (names is not None and m.name in names)]
    keep = [m for m in mods if m not in to_apply]
    saved = {m.name: m.show_viewport for m in keep}
    for m in keep:
        m.show_viewport = False
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        new_mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph), preserve_all_data_layers=True, depsgraph=depsgraph)
    finally:
        for m in keep:
            m.show_viewport = saved[m.name]
    applied = [m.name for m in to_apply]
    old = obj.data
    obj.data = new_mesh
    for m in to_apply:
        obj.modifiers.remove(m)
    if old.users == 0:
        old_name = old.name
        bpy.data.meshes.remove(old)
        new_mesh.name = old_name
    return applied


def select_elements(bm, obj, select):
    """Resolve a selector to bmesh faces.

    select: None / "all" | {"indices": [...]} | {"normal": [x,y,z], "angle": deg}
            | {"bounds": {"min": [...], "max": [...]}} (face centers, world space)
            | {"material": "Name"}. Keys combine with AND.
    """
    bm.faces.ensure_lookup_table()
    faces = list(bm.faces)
    if select in (None, "all"):
        return faces
    if not isinstance(select, dict):
        raise CommandError("select must be 'all' or an object with indices/normal/bounds/material")
    mw = obj.matrix_world
    nmat = mw.to_3x3().inverted_safe().transposed()
    if "indices" in select:
        wanted = set(select["indices"])
        faces = [f for f in faces if f.index in wanted]
    if "normal" in select:
        target = vec(select["normal"]).normalized()
        limit = math.cos(math.radians(select.get("angle", 30)))
        faces = [f for f in faces if (nmat @ f.normal).normalized().dot(target) >= limit]
    if "bounds" in select:
        lo, hi = vec(select["bounds"]["min"]), vec(select["bounds"]["max"])
        def inside(p):
            return all(lo[i] <= p[i] <= hi[i] for i in range(3))
        faces = [f for f in faces if inside(mw @ f.calc_center_median())]
    if "material" in select:
        names = [s.material.name if s.material else None for s in obj.material_slots]
        if select["material"] not in names:
            raise CommandError(f"{obj.name!r} has no material {select['material']!r}")
        idx = names.index(select["material"])
        faces = [f for f in faces if f.material_index == idx]
    return faces


@contextlib.contextmanager
def edit_bmesh(obj):
    """bmesh for an object-mode mesh, written back on success."""
    if obj.mode != "OBJECT":
        raise CommandError(f"{obj.name!r} is in {obj.mode} mode; switch to object mode first")
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    try:
        yield bm
        bm.normal_update()
        bm.to_mesh(obj.data)
        obj.data.update()
        bpy.context.view_layer.update()
    finally:
        bm.free()


def mesh_counts(obj):
    me = obj.data
    return {"verts": len(me.vertices), "faces": len(me.polygons), "tris": sum(len(p.vertices) - 2 for p in me.polygons)}


def use_nodes(mat):
    """Node materials are the only kind from Blender 5.0, where the use_nodes flag is deprecated."""
    if bpy.app.version < (5, 0, 0) or mat.node_tree is None:
        mat.use_nodes = True
