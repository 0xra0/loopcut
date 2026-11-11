---
id: local/blender-52-api
version: 1
title: Blender 5.2 API facts
when: any request; this Blender is 5.2.2 LTS and older bpy habits fail here
roles: [single, workspace]
requires_tools: []
tier: free
kind: assist
part: single
words: .
---
This Blender is 5.2.2 LTS. Each fact below was checked in it (verify_facts*.py); older habits that fail are marked GONE.
- Shader nodes: find by type, never by name (names are localized): `next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")`; output is type `OUTPUT_MATERIAL`, image texture `TEX_IMAGE`.
- `mat.use_nodes = True` already makes the Principled BSDF and the Material Output; do not `node_tree.nodes.clear()` (that deletes the output too). New shader nodes take class names: `nodes.new("ShaderNodeBsdfPrincipled")`, `"ShaderNodeOutputMaterial"` (`ShaderNodeBSDF_PRINCIPLED` is undefined).
- A number the request gives (roughness 0.35) goes on the unlinked input's `default_value`; a texture linked into that input replaces the number, so add variation only when asked.
- Principled BSDF inputs: "Base Color", "Metallic", "Roughness", "IOR", "Alpha", "Normal", "Specular IOR Level", "Emission Color", "Emission Strength", "Transmission Weight", "Subsurface Weight", "Coat Weight". GONE: "Specular", "Emission", "Transmission", "Subsurface", "Clearcoat".
- Material colour goes on the node input; `material.diffuse_color` is the viewport colour only. Transparency mode: `mat.surface_render_method` ("DITHERED"/"BLENDED").
- Smooth shading: GONE `mesh.use_auto_smooth`. `bpy.ops.object.shade_smooth_by_angle()` marks sharp edges in the mesh itself (use this for game exports); `bpy.ops.object.shade_auto_smooth()` adds a "Smooth by Angle" NODES modifier instead. GONE `mesh.calc_normals_split()`; read `mesh.corner_normals`.
- Vertex colours: use `mesh.color_attributes` (`vertex_colors` is a legacy alias).
- Render engines: 'BLENDER_EEVEE', 'BLENDER_WORKBENCH', 'CYCLES' (EEVEE Next is just BLENDER_EEVEE). GONE `scene.eevee.use_ssr`. Baking (`bpy.ops.object.bake`) needs CYCLES.
- Never hardcode an enum you are unsure of; read it: `[i.identifier for i in bpy.types.X.bl_rna.properties["prop"].enum_items]`. Some enums are filled at runtime and read empty; then try the value inside try/except TypeError, whose message lists the valid values.
- Animation uses slotted actions. GONE `action.fcurves`. Use `from bpy_extras import anim_utils` and `anim_utils.action_ensure_channelbag_for_slot(action, slot).fcurves`; `keyframe_insert()` on the datablock still works and creates the slot itself.
- Node group sockets: GONE `tree.inputs.new`/`tree.outputs.new`. Use `tree.interface.new_socket(name, in_out="INPUT", socket_type="NodeSocketFloat")`.
- Export: `bpy.ops.export_scene.gltf(filepath=..., export_format="GLB", use_selection=True, export_apply=True)`; `bpy.ops.export_scene.fbx(filepath=..., use_selection=True, axis_forward=..., axis_up=..., apply_scale_options=..., mesh_smooth_type="FACE", use_tspace=True, add_leaf_bones=False)`.
- After an AttributeError, TypeError or "enum not found": call inspect_api for that type before retrying, and do not guess a second name.
