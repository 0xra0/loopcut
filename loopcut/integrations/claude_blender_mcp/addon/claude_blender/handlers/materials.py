"""materials toolset: PBR materials, assignment per object or per face, image textures."""

import os

import bpy

from . import CommandError, command
from .util import edit_bmesh, get_mesh, select_elements, use_nodes

PBR_INPUTS = {
    "base_color": "Base Color",
    "metallic": "Metallic",
    "roughness": "Roughness",
    "alpha": "Alpha",
    "emission_color": "Emission Color",
    "emission_strength": "Emission Strength",
    "subsurface_weight": "Subsurface Weight",
    "specular": "Specular IOR Level",
    "normal": "Normal",
}


def _principled(mat):
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            return n
    raise CommandError(f"Material {mat.name!r} has no Principled BSDF node")


def _color(value):
    if len(value) == 3:
        return [*value, 1.0]
    if len(value) == 4:
        return list(value)
    raise CommandError(f"Colors are [r, g, b] or [r, g, b, a] in 0-1, got {value!r}")


def _get_material(name):
    mat = bpy.data.materials.get(name)
    if mat is None:
        raise CommandError(f"No material named {name!r}")
    return mat


@command("create_material", "materials")
def create_material(name, base_color=(0.8, 0.8, 0.8), metallic=0.0, roughness=0.5, emission_color=None,
                    emission_strength=0.0, alpha=1.0, subsurface_weight=0.0, assign_to=None):
    """New Principled BSDF material (or update the one with this name). Colors are linear 0-1."""
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    use_nodes(mat)
    bsdf = _principled(mat)
    bsdf.inputs["Base Color"].default_value = _color(base_color)
    bsdf.inputs["Metallic"].default_value = float(metallic)
    bsdf.inputs["Roughness"].default_value = float(roughness)
    bsdf.inputs["Alpha"].default_value = float(alpha)
    bsdf.inputs["Subsurface Weight"].default_value = float(subsurface_weight)
    if emission_color is not None:
        bsdf.inputs["Emission Color"].default_value = _color(emission_color)
    bsdf.inputs["Emission Strength"].default_value = float(emission_strength)
    if float(alpha) < 1.0 and hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "BLENDED"
    mat.diffuse_color = _color(base_color)  # viewport/Workbench color
    for obj_name in assign_to or []:
        assign_material(obj_name, name)
    return {"material": mat.name}


@command("assign_material", "materials")
def assign_material(name, material, select=None):
    """Give an object a material. With select (same selector as mesh_edit) only those faces get it;
    otherwise it replaces the first slot."""
    obj = get_mesh(name)
    mat = _get_material(material)
    slots = [s.material for s in obj.material_slots]
    if select is None:
        if obj.material_slots:
            obj.material_slots[0].material = mat
        else:
            obj.data.materials.append(mat)
        return {"object": obj.name, "material": mat.name, "faces": len(obj.data.polygons)}
    if mat not in slots:
        if not obj.material_slots:
            # Keep the rest of the mesh on a neutral slot 0 so only the selection changes.
            obj.data.materials.append(None)
        obj.data.materials.append(mat)
    index = [s.material for s in obj.material_slots].index(mat)
    with edit_bmesh(obj) as bm:
        faces = select_elements(bm, obj, select)
        if not faces:
            raise CommandError("Selection matched no faces")
        for f in faces:
            f.material_index = index
        count = len(faces)
    return {"object": obj.name, "material": mat.name, "faces": count}


@command("set_material_texture", "materials")
def set_material_texture(material, input, image_path, uv_map=None):
    """Plug an image file into a Principled input: base_color, roughness, metallic, normal,
    emission_color, alpha. Normal maps get a Normal Map node; data maps are loaded as Non-Color."""
    mat = _get_material(material)
    if input not in PBR_INPUTS:
        raise CommandError(f"input must be one of {sorted(PBR_INPUTS)}")
    path = bpy.path.abspath(image_path)
    if not os.path.isfile(path):
        raise CommandError(f"No image at {path}")
    img = bpy.data.images.load(path, check_existing=True)
    if input not in ("base_color", "emission_color"):
        img.colorspace_settings.name = "Non-Color"
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = _principled(mat)
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.label = input
    tex.location = (bsdf.location.x - 600, bsdf.location.y - 280 * len([n for n in nodes if n.type == "TEX_IMAGE"]))
    if uv_map:
        uv = nodes.new("ShaderNodeUVMap")
        uv.uv_map = uv_map
        uv.location = (tex.location.x - 250, tex.location.y)
        links.new(uv.outputs["UV"], tex.inputs["Vector"])
    target = bsdf.inputs[PBR_INPUTS[input]]
    if input == "normal":
        nm = nodes.new("ShaderNodeNormalMap")
        nm.location = (tex.location.x + 300, tex.location.y)
        links.new(tex.outputs["Color"], nm.inputs["Color"])
        links.new(nm.outputs["Normal"], target)
    else:
        links.new(tex.outputs["Color"], target)
    return {"material": mat.name, "input": input, "image": img.name}
