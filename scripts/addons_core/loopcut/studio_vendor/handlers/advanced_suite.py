"""Advanced suite handlers for Loopcut (Items 9 - 35).

Exposes task-oriented commands for:
- Operator registry lookup and poll testing
- Structured safe modeling (extrude, inset, bevel, boolean, mirror, bridge, separate, merge, solidify, shrinkwrap, subdivide)
- Full sculpt controller (fast regional smooth/flatten, dyntopo, masks, face sets)
- Thin-shell sculpt protection and hard-surface plate reconstruction
- Quad remeshing, reprojection, and boundary cleanup
- UV seams, unwrapping, packing, texel density, and PBR material setup
- Geometry Nodes inspection, creation, and evaluated geometry testing
- Rigging (armature, skinning, IK constraints, keyframes)
- Rendering and studio lighting, camera optics, cloth physics
- Add-on integrations with native fallbacks (Zen UV, UVPackmaster, Hard Ops)
- Semantic mesh decomposition and geometry diagnostics
- Persistent project memory constraints
- GPU/VRAM monitoring and knowledge base retrieval
"""

from typing import Any
import numpy as np

from . import command, CommandError


# -------------------------------------------------------------------------
# Operator Registry (Item 9)
# -------------------------------------------------------------------------

@command("operator_inspect", "registry")
def operator_inspect(operator_id: str) -> dict:
    """Inspect an operator in bpy.ops: poll status in current context, RNA parameters, docstring."""
    from ... import operator_registry
    return operator_registry.describe_operator(operator_id)


@command("operator_search", "registry")
def operator_search(query: str, category: str = "", limit: int = 20) -> list[dict]:
    """Search registered bpy.ops operators matching query terms."""
    from ... import operator_registry
    return operator_registry.search_operators(query, category=category, limit=limit)


# -------------------------------------------------------------------------
# Structured Modeling Tools (Item 10)
# -------------------------------------------------------------------------

@command("mesh_extrude", "modeling")
def mesh_extrude(name: str, distance: float = 0.1) -> dict:
    """Extrude selected faces or mesh along normal."""
    from ... import modeling_ops
    return modeling_ops.extrude(name, distance=distance)


@command("mesh_inset", "modeling")
def mesh_inset(name: str, thickness: float = 0.05, depth: float = 0.0) -> dict:
    """Inset faces with given thickness and depth."""
    from ... import modeling_ops
    return modeling_ops.inset(name, thickness=thickness, depth=depth)


@command("mesh_bevel", "modeling")
def mesh_bevel(name: str, width: float = 0.02, segments: int = 2, profile: float = 0.5) -> dict:
    """Apply bevel with width and segment count."""
    from ... import modeling_ops
    return modeling_ops.bevel(name, width=width, segments=segments, profile=profile)


@command("mesh_boolean", "modeling")
def mesh_boolean(name: str, target: str, operation: str = "DIFFERENCE", solver: str = "EXACT") -> dict:
    """Apply boolean operation (DIFFERENCE, UNION, INTERSECT)."""
    from ... import modeling_ops
    return modeling_ops.boolean_op(name, target, operation=operation, solver=solver)


@command("mesh_mirror", "modeling")
def mesh_mirror(name: str, axis: str = "X", use_clip: bool = True) -> dict:
    """Add a mirror modifier along given axis."""
    from ... import modeling_ops
    return modeling_ops.mirror(name, axis=axis, use_clip=use_clip)


@command("mesh_bridge", "modeling")
def mesh_bridge(name: str) -> dict:
    """Bridge selected edge loops."""
    from ... import modeling_ops
    return modeling_ops.bridge(name)


@command("mesh_separate", "modeling")
def mesh_separate(name: str, mode: str = "LOOSE") -> dict:
    """Separate mesh into distinct objects (LOOSE, SELECTED, MATERIAL)."""
    from ... import modeling_ops
    return modeling_ops.separate(name, mode=mode)


@command("mesh_merge", "modeling")
def mesh_merge(name: str, mode: str = "DISTANCE", distance: float = 0.0001) -> dict:
    """Merge vertices (DISTANCE, CENTER, COLLAPSE)."""
    from ... import modeling_ops
    return modeling_ops.merge_verts(name, mode=mode, distance=distance)


@command("mesh_solidify", "modeling")
def mesh_solidify(name: str, thickness: float = 0.02, offset: float = -1.0) -> dict:
    """Add a solidify modifier with given thickness."""
    from ... import modeling_ops
    return modeling_ops.solidify(name, thickness=thickness, offset=offset)


@command("mesh_shrinkwrap", "modeling")
def mesh_shrinkwrap(name: str, target: str, offset: float = 0.0) -> dict:
    """Add shrinkwrap projection modifier towards target."""
    from ... import modeling_ops
    return modeling_ops.shrinkwrap(name, target, offset=offset)


@command("mesh_subdivide", "modeling")
def mesh_subdivide(name: str, cuts: int = 1) -> dict:
    """Subdivide mesh edges and faces."""
    from ... import modeling_ops
    return modeling_ops.subdivide(name, cuts=cuts)


# -------------------------------------------------------------------------
# Sculpt Controller & Fast Regional Sculpting (Items 11, 12, 13)
# -------------------------------------------------------------------------

@command("sculpt_fast_smooth", "sculpt")
def sculpt_fast_smooth(name: str, center: list[float], radius: float, strength: float = 0.5,
                       protect_thin_shells: bool = True) -> dict:
    """Perform fast local regional smoothing on the vertices within radius (NumPy, no sculpt mode)."""
    import bpy
    from ... import fast_sculpt, shell_guard
    obj = bpy.data.objects.get(name)
    if not obj or obj.type != "MESH":
        raise CommandError(f"Mesh '{name}' not found.")

    me = obj.data
    n_v = len(me.vertices)
    co = np.empty(n_v * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)

    edges = np.empty(len(me.edges) * 2, dtype=np.int32)
    me.edges.foreach_get("vertices", edges)
    edges = edges.reshape(-1, 2)

    adj = fast_sculpt.build_adjacency(edges, n_v)
    c_arr = np.array(center, dtype=np.float32)

    new_co = fast_sculpt.smooth_region(co, adj, c_arr, radius=radius, strength=strength)

    if protect_thin_shells:
        no = np.empty(n_v * 3, dtype=np.float32)
        me.vertices.foreach_get("normal", no)
        no = no.reshape(-1, 3)
        disp = new_co - co
        active_idx = np.nonzero(np.any(disp != 0, axis=1))[0]
        if len(active_idx) > 0:
            clamped = shell_guard.guard_shell_displacement(co, no, active_idx, disp[active_idx])
            new_co[active_idx] = co[active_idx] + clamped

    me.vertices.foreach_set("co", new_co.ravel())
    me.update()
    return {"status": "ok", "action": "fast_smooth", "object": name, "radius": radius}


@command("sculpt_fast_flatten", "sculpt")
def sculpt_fast_flatten(name: str, center: list[float], radius: float, strength: float = 0.5) -> dict:
    """Perform fast regional planar flattening."""
    import bpy
    from ... import fast_sculpt
    obj = bpy.data.objects.get(name)
    if not obj or obj.type != "MESH":
        raise CommandError(f"Mesh '{name}' not found.")

    me = obj.data
    n_v = len(me.vertices)
    co = np.empty(n_v * 3, dtype=np.float32)
    no = np.empty(n_v * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    me.vertices.foreach_get("normal", no)
    co, no = co.reshape(-1, 3), no.reshape(-1, 3)

    c_arr = np.array(center, dtype=np.float32)
    new_co = fast_sculpt.flatten_region(co, no, c_arr, radius=radius, strength=strength)
    me.vertices.foreach_set("co", new_co.ravel())
    me.update()
    return {"status": "ok", "action": "fast_flatten", "object": name, "radius": radius}


@command("sculpt_dyntopo", "sculpt")
def sculpt_dyntopo(name: str, enable: bool = True, detail_size: float = 12.0) -> dict:
    """Toggle and configure dynamic topology in sculpt mode."""
    from ... import sculpt_controller
    return sculpt_controller.dyntopo_toggle(name, enable=enable, detail_size=detail_size)


@command("sculpt_mask_op", "sculpt")
def sculpt_mask_op(name: str, action: str = "CLEAR") -> dict:
    """Mask operations (CLEAR, INVERT) in sculpt mode."""
    from ... import sculpt_controller
    return sculpt_controller.sculpt_mask(name, action=action)


@command("sculpt_face_sets_op", "sculpt")
def sculpt_face_sets_op(name: str, action: str = "LOOSE_PARTS") -> dict:
    """Face sets operations (LOOSE_PARTS, CLEAR)."""
    from ... import sculpt_controller
    return sculpt_controller.sculpt_face_sets(name, action=action)


# -------------------------------------------------------------------------
# Hard-Surface Reconstruction (Item 14)
# -------------------------------------------------------------------------

@command("hard_surface_rebuild_plate", "modeling")
def hard_surface_rebuild_plate(name: str, face_indices: list[int] | None = None) -> dict:
    """Reconstruct a planar armor plate via SVD plane fitting and vertex projection."""
    from ... import hard_surface
    return hard_surface.rebuild_armor_plate(name, face_indices=face_indices)


# -------------------------------------------------------------------------
# Topology and Retopology (Item 15)
# -------------------------------------------------------------------------

@command("quad_remesh_op", "topology")
def quad_remesh_op(name: str, target_faces: int = 5000, use_symmetry: bool = True) -> dict:
    """Execute feature-preserving quad remeshing."""
    from ... import retopo_tools
    return retopo_tools.quad_remesh(name, target_faces=target_faces, use_symmetry=use_symmetry)


@command("reproject_shrinkwrap_op", "topology")
def reproject_shrinkwrap_op(target: str, source: str, offset: float = 0.0) -> dict:
    """Reproject low-poly target onto high-poly source with RMS error report."""
    from ... import retopo_tools
    return retopo_tools.reproject_and_shrinkwrap(target, source, offset=offset)


@command("cleanup_boundary_op", "topology")
def cleanup_boundary_op(name: str) -> dict:
    """Clean up open boundary loops and degenerate faces."""
    from ... import retopo_tools
    return retopo_tools.cleanup_boundary_edges(name)


# -------------------------------------------------------------------------
# Full UV & Texturing (Item 16)
# -------------------------------------------------------------------------

@command("uv_mark_seams_auto", "uv_bake")
def uv_mark_seams_auto(name: str, angle_threshold_deg: float = 60.0) -> dict:
    """Automatically mark seams along sharp edges above angle threshold."""
    from ... import uv_controller
    return uv_controller.mark_seams(name, angle_threshold_deg=angle_threshold_deg)


@command("uv_unwrap_pack", "uv_bake")
def uv_unwrap_pack(name: str, margin: float = 0.01) -> dict:
    """Unwrap mesh UVs and pack islands."""
    from ... import uv_controller
    return uv_controller.unwrap_and_pack(name, margin=margin)


@command("uv_texel_density", "uv_bake")
def uv_texel_density(name: str, texture_size: int = 2048) -> dict:
    """Measure average texel density (pixels per meter)."""
    from ... import uv_controller
    density = uv_controller.calculate_texel_density(name, texture_size=texture_size)
    return {"status": "ok", "object": name, "texel_density_px_per_m": round(density, 2)}


@command("material_create_pbr", "materials")
def material_create_pbr(material_name: str, base_color: list[float] = [0.8, 0.8, 0.8, 1.0],
                        metallic: float = 0.0, roughness: float = 0.5) -> dict:
    """Create a Principled BSDF PBR material."""
    from ... import uv_controller
    return uv_controller.create_pbr_material(material_name, base_color=tuple(base_color),
                                            metallic=metallic, roughness=roughness)


# -------------------------------------------------------------------------
# Geometry Nodes Controller (Item 17)
# -------------------------------------------------------------------------

@command("geonodes_inspect", "nodes")
def geonodes_inspect(object_name: str, modifier_name: str = "") -> dict:
    """Inspect Geometry Nodes modifier sockets, inputs, and nodes."""
    from ... import geonodes_controller
    return geonodes_controller.inspect_geometry_nodes(object_name, modifier_name=modifier_name)


@command("geonodes_create", "nodes")
def geonodes_create(object_name: str, modifier_name: str = "GeometryNodes") -> dict:
    """Create a new Geometry Nodes modifier and tree."""
    from ... import geonodes_controller
    return geonodes_controller.add_geometry_nodes_modifier(object_name, modifier_name=modifier_name)


@command("geonodes_evaluated_metrics", "nodes")
def geonodes_evaluated_metrics(object_name: str) -> dict:
    """Inspect evaluated geometry output (vertices, polygons, bounds) after nodes execute."""
    from ... import geonodes_controller
    return geonodes_controller.get_evaluated_geometry_metrics(object_name)


# -------------------------------------------------------------------------
# Rigging & Animation (Item 18)
# -------------------------------------------------------------------------

@command("rig_create_armature", "rig")
def rig_create_armature(name: str = "Armature", bones: list[dict] | None = None) -> dict:
    """Create an armature with defined bones."""
    from ... import rig_anim_controller
    return rig_anim_controller.create_armature(armature_name=name, bones=bones)


@command("rig_bind_mesh", "rig")
def rig_bind_mesh(mesh: str, armature: str, mode: str = "AUTOMATIC") -> dict:
    """Parent mesh to armature with skinning weights (AUTOMATIC, ENVELOPE, EMPTY)."""
    from ... import rig_anim_controller
    return rig_anim_controller.bind_mesh_to_armature(mesh, armature, mode=mode)


@command("rig_add_ik", "rig")
def rig_add_ik(armature: str, bone: str, target_obj: str, target_bone: str = "", chain_count: int = 2) -> dict:
    """Add an IK constraint to a pose bone."""
    from ... import rig_anim_controller
    return rig_anim_controller.add_ik_constraint(armature, bone, target_obj, target_bone=target_bone, chain_count=chain_count)


@command("anim_set_keyframe", "animate")
def anim_set_keyframe(object_name: str, frame: int, data_path: str = "location") -> dict:
    """Set keyframe at specified frame for object."""
    from ... import rig_anim_controller
    return rig_anim_controller.set_keyframe(object_name, frame=frame, data_path=data_path)


# -------------------------------------------------------------------------
# Rendering & Simulation (Item 19)
# -------------------------------------------------------------------------

@command("render_setup_camera", "look")
def render_setup_camera(name: str = "Camera", focal_length: float = 50.0, dof_target: str = "") -> dict:
    """Configure camera optics and depth-of-field."""
    from ... import render_sim_controller
    return render_sim_controller.setup_camera(name, focal_length=focal_length, dof_target=dof_target)


@command("render_studio_lighting", "look")
def render_studio_lighting(energy_scale: float = 1.0) -> dict:
    """Setup standard three-point studio lighting rig."""
    from ... import render_sim_controller
    return render_sim_controller.setup_studio_lighting(energy_scale=energy_scale)


@command("render_configure_engine", "look")
def render_configure_engine(engine: str = "CYCLES", device: str = "GPU", samples: int = 128) -> dict:
    """Configure render engine, GPU acceleration, and sample count."""
    from ... import render_sim_controller
    return render_sim_controller.configure_render_engine(engine=engine, device=device, samples=samples)


@command("sim_add_cloth", "look")
def sim_add_cloth(object_name: str, preset: str = "SILK") -> dict:
    """Add a cloth simulation modifier."""
    from ... import render_sim_controller
    return render_sim_controller.add_physics_cloth(object_name, preset=preset)


# -------------------------------------------------------------------------
# Add-on Capability Integrations (Item 20)
# -------------------------------------------------------------------------

@command("addon_unwrap", "uv_bake")
def addon_unwrap(name: str) -> dict:
    """Execute Zen UV unwrap if installed, otherwise native unwrap."""
    from ... import addon_integrations
    return addon_integrations.run_zen_uv_unwrap(name)


@command("addon_pack", "uv_bake")
def addon_pack(name: str, margin: float = 0.01) -> dict:
    """Execute UVPackmaster if installed, otherwise native pack."""
    from ... import addon_integrations
    return addon_integrations.run_uvpackmaster_pack(name, margin=margin)


@command("addon_sharpen", "modeling")
def addon_sharpen(name: str) -> dict:
    """Execute Hard Ops sharpen (C-Sharpen) if installed, otherwise native sharp seams."""
    from ... import addon_integrations
    return addon_integrations.run_hardops_sharpen(name)


# -------------------------------------------------------------------------
# Semantic regions and geometry diagnostics (items 23 - 24)
# -------------------------------------------------------------------------

@command("mesh_segment_regions", "topology")
def mesh_segment_regions(name: str) -> dict:
    """Segment single merged mesh into semantic vertex groups (body, plates, trim, ornaments)."""
    from ... import semantic_mesh
    return semantic_mesh.tag_semantic_regions(name)


@command("diagnose_geometry", "topology")
def diagnose_geometry(name: str) -> dict:
    """Calculate boundary edges, non-manifold edges, self-intersections, thickness, roughness."""
    from ... import geometry_diagnostics
    return geometry_diagnostics.diagnose_object(name)


# -------------------------------------------------------------------------
# Project Memory & Performance (Items 7, 31, 33)
# -------------------------------------------------------------------------

@command("project_get_memory", "core")
def project_get_memory() -> dict:
    """Retrieve persistent project constraints (target engine, budgets, protected objects)."""
    from ... import project_memory
    return project_memory.load_memory()


@command("project_set_constraint", "core")
def project_set_constraint(key: str, value: Any) -> dict:
    """Persist a project constraint alongside .blend file."""
    from ... import project_memory
    return project_memory.set_constraint(key, value)


@command("gpu_get_vram_status", "core")
def gpu_get_vram_status() -> dict:
    """Query current ROCm / system VRAM and memory usage."""
    from ... import gpu_coordinator
    return gpu_coordinator.get_rocm_vram_info()


@command("knowledge_search", "core")
def knowledge_search(query: str) -> list[dict]:
    """Search local Blender 5.2 API and Loopcut playbook documentation."""
    from ... import knowledge_base
    return knowledge_base.search_knowledge_base(query)
