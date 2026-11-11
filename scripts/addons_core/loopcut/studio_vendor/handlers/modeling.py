"""modeling toolset: primitives, transforms, modifiers, booleans and bmesh edits.

Everything goes through the data API and bmesh, so it behaves the same headless, from a
timer, and whatever the user's selection or mode is.
"""

import math

import bmesh
import bpy
from mathutils import Matrix

from . import CommandError, command
from .util import (apply_modifiers, edit_bmesh, get_mesh, get_object, link_to_scene, mesh_counts,
                   radians, select_elements, vec)

PRIMITIVES = ("cube", "uv_sphere", "ico_sphere", "cylinder", "cone", "plane", "grid", "circle", "torus")


def _build_primitive(bm, kind, size, segments, rings, subdivisions, depth, radius2):
    r = size / 2
    if kind == "cube":
        bmesh.ops.create_cube(bm, size=size)
    elif kind == "uv_sphere":
        bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=rings, radius=r)
    elif kind == "ico_sphere":
        bmesh.ops.create_icosphere(bm, subdivisions=subdivisions, radius=r)
    elif kind in ("cylinder", "cone"):
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=segments,
                              radius1=r, radius2=r if kind == "cylinder" else radius2, depth=depth or size)
    elif kind == "plane":
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=r)
    elif kind == "grid":
        bmesh.ops.create_grid(bm, x_segments=segments, y_segments=segments, size=r)
    elif kind == "circle":
        bmesh.ops.create_circle(bm, cap_ends=True, segments=segments, radius=r)
    elif kind == "torus":
        minor = radius2 or r * 0.25
        ring = bmesh.ops.create_circle(bm, segments=rings, radius=minor)["verts"]
        bmesh.ops.translate(bm, verts=ring, vec=(r, 0, 0))
        bmesh.ops.rotate(bm, verts=ring, cent=(0, 0, 0), matrix=Matrix.Rotation(math.pi / 2, 3, "X"))
        bmesh.ops.spin(bm, geom=bm.verts[:] + bm.edges[:], cent=(0, 0, 0), axis=(0, 0, 1),
                       angle=math.tau, steps=segments, use_duplicate=False)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)


@command("create_primitive", "modeling")
def create_primitive(kind, name=None, location=None, rotation=None, scale=None, size=2.0, segments=32,
                     rings=16, subdivisions=3, depth=None, radius2=0.0, collection=None, smooth=False):
    """New mesh object. rotation is in degrees. For torus, size is the major diameter and radius2 the tube radius."""
    if kind not in PRIMITIVES:
        raise CommandError(f"Unknown primitive {kind!r}. Use one of {PRIMITIVES}")
    me = bpy.data.meshes.new(name or kind)
    bm = bmesh.new()
    _build_primitive(bm, kind, float(size), int(segments), int(rings), int(subdivisions), depth, float(radius2))
    bm.to_mesh(me)
    bm.free()
    if smooth:
        me.shade_smooth()
    obj = link_to_scene(bpy.data.objects.new(name or kind, me), collection)
    obj.location = vec(location)
    obj.rotation_euler = radians(vec(rotation))
    obj.scale = vec(scale, (1, 1, 1))
    return {"name": obj.name, **mesh_counts(obj)}


@command("transform", "modeling")
def transform(name, location=None, rotation=None, scale=None, relative=False):
    """Set (or with relative=True, add to) location, rotation in degrees and scale."""
    obj = get_object(name)
    if relative:
        if location is not None:
            obj.location += vec(location)
        if rotation is not None:
            r = radians(vec(rotation))
            obj.rotation_euler = [a + b for a, b in zip(obj.rotation_euler, r)]
        if scale is not None:
            obj.scale = [a * b for a, b in zip(obj.scale, vec(scale, (1, 1, 1)))]
    else:
        if location is not None:
            obj.location = vec(location)
        if rotation is not None:
            obj.rotation_euler = radians(vec(rotation))
        if scale is not None:
            obj.scale = vec(scale, (1, 1, 1))
    return {"name": obj.name, "location": list(obj.location),
            "rotation_deg": [round(math.degrees(a), 4) for a in obj.rotation_euler], "scale": list(obj.scale)}


@command("apply_transforms", "modeling")
def apply_transforms(name, location=False, rotation=True, scale=True):
    """Bake rotation/scale (and optionally location) into the mesh, like Ctrl+A. Needed before rigging and export."""
    obj = get_mesh(name)
    if obj.data.users > 1:
        raise CommandError(f"{name!r} shares its mesh with other objects; make it single-user first")
    loc, rot, scl = obj.matrix_basis.decompose()
    m = Matrix.Identity(4)
    if location:
        m = Matrix.Translation(loc) @ m
    if rotation:
        m = m @ rot.to_matrix().to_4x4()
    if scale:
        m = m @ Matrix.Diagonal(scl.to_4d())
    obj.data.transform(m)
    for child in obj.children:
        child.matrix_parent_inverse = m @ child.matrix_parent_inverse
    if location:
        obj.location = (0, 0, 0)
    if rotation:
        obj.rotation_euler = (0, 0, 0)
        obj.rotation_quaternion = (1, 0, 0, 0)
        obj.rotation_axis_angle = (0, 0, 1, 0)
    if scale:
        obj.scale = (1, 1, 1)
    obj.data.update()
    bpy.context.view_layer.update()  # refresh bounds so dimensions are current
    return {"name": obj.name, "dimensions": list(obj.dimensions)}


def _set_props(target, settings, label):
    for key, value in (settings or {}).items():
        if not hasattr(target, key):
            raise CommandError(f"{label} has no setting {key!r}")
        current = getattr(target, key)
        if isinstance(current, bpy.types.ID) or current is None and isinstance(value, str):
            value = bpy.data.objects.get(value) or bpy.data.textures.get(value) or bpy.data.images.get(value) or value
        try:
            setattr(target, key, value)
        except (TypeError, ValueError, AttributeError) as exc:
            raise CommandError(f"{label}.{key} = {value!r}: {exc}") from exc


@command("add_modifier", "modeling")
def add_modifier(name, type, settings=None, modifier_name=None):
    """Add any modifier by type (SUBSURF, MIRROR, SOLIDIFY, BEVEL, ARRAY, BOOLEAN, REMESH, DISPLACE...).
    settings maps property names to values; object/texture/image properties take a name."""
    obj = get_object(name)
    try:
        mod = obj.modifiers.new(modifier_name or type.title(), type.upper())
    except (TypeError, RuntimeError) as exc:
        raise CommandError(f"Can't add modifier {type!r}: {exc}") from exc
    try:
        _set_props(mod, settings, f"{mod.type} modifier")
    except CommandError:
        obj.modifiers.remove(mod)
        raise
    return {"object": obj.name, "modifier": mod.name, "stack": [m.name for m in obj.modifiers]}


@command("set_modifier", "modeling")
def set_modifier(name, modifier, settings):
    """Change settings on an existing modifier."""
    obj = get_object(name)
    mod = obj.modifiers.get(modifier)
    if mod is None:
        raise CommandError(f"No modifier {modifier!r} on {name!r}. Have: {[m.name for m in obj.modifiers]}")
    _set_props(mod, settings, f"{mod.type} modifier")
    return {"object": obj.name, "modifier": mod.name}


@command("apply_modifiers", "modeling")
def apply_modifiers_cmd(name, modifiers=None):
    """Bake modifiers into the mesh. modifiers=None applies all enabled ones; a list must be the top of the stack."""
    obj = get_mesh(name)
    applied = apply_modifiers(obj, modifiers)
    return {"object": obj.name, "applied": applied, **mesh_counts(obj)}


@command("boolean", "modeling")
def boolean(target, cutter, operation="DIFFERENCE", apply=True, hide_cutter=True, solver="EXACT"):
    """Boolean the cutter into the target (DIFFERENCE, UNION or INTERSECT)."""
    obj = get_mesh(target)
    cut = get_mesh(cutter)
    mod = obj.modifiers.new(f"Bool_{cut.name}", "BOOLEAN")
    mod.operation = operation.upper()
    mod.object = cut
    mod.solver = solver.upper()
    result = {"target": obj.name}
    if apply:
        # Apply only this modifier: move it to the top so the stack-order rule holds.
        while obj.modifiers.find(mod.name) > 0:
            obj.modifiers.move(obj.modifiers.find(mod.name), obj.modifiers.find(mod.name) - 1)
        apply_modifiers(obj, [mod.name])
        result.update(mesh_counts(obj))
    if hide_cutter:
        cut.hide_set(True)
        cut.hide_render = True
    return result


@command("duplicate", "modeling")
def duplicate(name, new_name=None, linked=False, location=None):
    """Copy an object (linked=True shares the mesh data)."""
    obj = get_object(name)
    new = obj.copy()
    if obj.data is not None and not linked:
        new.data = obj.data.copy()
    new.name = new_name or f"{obj.name}.copy"
    for coll in obj.users_collection:
        coll.objects.link(new)
    if location is not None:
        new.location = vec(location)
    return {"name": new.name}


@command("delete", "modeling")
def delete(names):
    """Delete objects by name."""
    deleted = []
    for n in names:
        obj = get_object(n)
        bpy.data.objects.remove(obj, do_unlink=True)
        deleted.append(n)
    return {"deleted": deleted}


@command("join", "modeling")
def join(names, into=None):
    """Merge several meshes into one object (the first, or `into`)."""
    objs = [get_mesh(n) for n in names]
    target = get_mesh(into) if into else objs[0]
    others = [o for o in objs if o is not target]
    if not others:
        raise CommandError("Need at least two meshes to join")
    from .util import ops_context

    with ops_context(target, selected=others, mode="OBJECT"):
        bpy.ops.object.join()
    return {"name": target.name, **mesh_counts(target)}


@command("set_parent", "modeling")
def set_parent(child, parent, keep_transform=True):
    """Parent one object to another (parent=None clears it)."""
    obj = get_object(child)
    world = obj.matrix_world.copy()
    obj.parent = get_object(parent) if parent else None
    if keep_transform:
        obj.matrix_world = world
    return {"child": obj.name, "parent": parent}


MESH_OPS = ("extrude", "inset", "bevel", "subdivide", "delete", "solidify_faces", "scale", "translate", "triangulate", "flip_normals")


@command("mesh_edit", "modeling")
def mesh_edit(name, op, select="all", params=None):
    """Edit part of a mesh. select picks faces: "all", {"indices"}, {"normal": [x,y,z], "angle": deg},
    {"bounds": {"min", "max"}} in world space, {"material"}; keys combine.

    op / params:
      extrude    distance
      inset      thickness, depth, individual (bool)
      bevel      width, segments (bevels the edges of the selected faces' boundary)
      subdivide  cuts, smooth
      delete     (removes selected faces)
      scale      factor [x,y,z] around the selection center
      translate  offset [x,y,z] in world space
      triangulate, flip_normals
    """
    if op not in MESH_OPS:
        raise CommandError(f"Unknown op {op!r}. Use one of {MESH_OPS}")
    params = params or {}
    obj = get_mesh(name)
    with edit_bmesh(obj) as bm:
        faces = select_elements(bm, obj, select)
        if not faces:
            raise CommandError("Selection matched no faces")
        touched = len(faces)
        inv = obj.matrix_world.inverted()
        if op == "extrude":
            res = bmesh.ops.extrude_face_region(bm, geom=faces)
            verts = [g for g in res["geom"] if isinstance(g, bmesh.types.BMVert)]
            new_faces = [g for g in res["geom"] if isinstance(g, bmesh.types.BMFace)]
            normal = sum((f.normal for f in new_faces), faces[0].normal * 0).normalized()
            bmesh.ops.translate(bm, verts=verts, vec=normal * float(params.get("distance", 0.2)))
            # extrude_face_region leaves the original faces inside the new shell.
            bmesh.ops.delete(bm, geom=faces, context="FACES")
        elif op == "inset":
            if params.get("individual"):
                bmesh.ops.inset_individual(bm, faces=faces, thickness=float(params.get("thickness", 0.05)),
                                           depth=float(params.get("depth", 0.0)))
            else:
                bmesh.ops.inset_region(bm, faces=faces, thickness=float(params.get("thickness", 0.05)),
                                       depth=float(params.get("depth", 0.0)))
        elif op == "bevel":
            region = set(faces)
            edges = [e for f in faces for e in f.edges
                     if len(region) == len(bm.faces) or any(lf not in region for lf in e.link_faces)]
            bmesh.ops.bevel(bm, geom=list(set(edges)), offset=float(params.get("width", 0.05)),
                            segments=int(params.get("segments", 2)), affect="EDGES", profile=0.5)
        elif op == "subdivide":
            edges = list({e for f in faces for e in f.edges})
            bmesh.ops.subdivide_edges(bm, edges=edges, cuts=int(params.get("cuts", 1)),
                                      smooth=float(params.get("smooth", 0.0)), use_grid_fill=True)
        elif op == "delete":
            bmesh.ops.delete(bm, geom=faces, context="FACES")
        elif op in ("scale", "translate"):
            verts = list({v for f in faces for v in f.verts})
            if op == "scale":
                center = sum((v.co for v in verts), verts[0].co * 0) / len(verts)
                bmesh.ops.scale(bm, verts=verts, vec=vec(params.get("factor", 1.0)),
                                space=Matrix.Translation(-center))
            else:
                offset = inv.to_3x3() @ vec(params.get("offset"))
                bmesh.ops.translate(bm, verts=verts, vec=offset)
        elif op == "triangulate":
            bmesh.ops.triangulate(bm, faces=faces)
        elif op == "flip_normals":
            bmesh.ops.reverse_faces(bm, faces=faces)
    return {"name": obj.name, "op": op, "faces_selected": touched, **mesh_counts(obj)}
