"""MCP tools for the materials, rig, animate and export toolsets."""

import base64
from typing import Any, Literal

Vec3 = list[float]
Selector = Any


def register(tool, call, dump, image):
    # --- materials -------------------------------------------------------------

    @tool("materials")
    def create_material(name: str, base_color: list[float] | None = None, metallic: float = 0.0,
                        roughness: float = 0.5, emission_color: list[float] | None = None,
                        emission_strength: float = 0.0, alpha: float = 1.0, subsurface_weight: float = 0.0,
                        assign_to: list[str] | None = None) -> str:
        """Create or update a PBR (Principled BSDF) material. Colors are linear RGB 0-1.
        Skin: subsurface_weight 0.1-0.3. Metal: metallic 1, roughness 0.2-0.5."""
        return dump(call("create_material", name=name, base_color=base_color or [0.8, 0.8, 0.8], metallic=metallic,
                         roughness=roughness, emission_color=emission_color, emission_strength=emission_strength,
                         alpha=alpha, subsurface_weight=subsurface_weight, assign_to=assign_to))

    @tool("materials")
    def assign_material(name: str, material: str, select: Selector = None) -> str:
        """Assign a material to a whole mesh, or with select (mesh_edit selector) to some faces."""
        return dump(call("assign_material", name=name, material=material, select=select))

    @tool("materials")
    def set_material_texture(material: str,
                             input: Literal["base_color", "metallic", "roughness", "alpha", "emission_color",
                                            "emission_strength", "subsurface_weight", "specular", "normal"],
                             image_path: str, uv_map: str | None = None) -> str:
        """Plug an image file into a material input (normal maps get a Normal Map node)."""
        return dump(call("set_material_texture", material=material, input=input, image_path=image_path, uv_map=uv_map))

    # --- rig ---------------------------------------------------------------------

    @tool("rig")
    def rig_template(name: str = "Armature", height: float = 1.8, fit_to: str | None = None,
                     arm_span: float | None = None, arm_angle: float = 0.0) -> str:
        """Game-ready humanoid skeleton: 21 deform bones plus root, facing -Y, Unity/Unreal
        friendly names (hips, spine, chest, neck, head, shoulder/upper_arm/forearm/hand.L/R,
        thigh/shin/foot/toe.L/R). arm_angle matches the mesh's rest pose: 0 = T-pose, 40-60 =
        A-pose (measure it with reference_to_world). fit_to sizes it to a mesh. Then check joints
        with capture_views(objects=[mesh, rig], shading="xray") and move them with edit_bones."""
        return dump(call("rig_template", name=name, height=height, fit_to=fit_to, arm_span=arm_span,
                         arm_angle=arm_angle))

    @tool("rig")
    def edit_bones(armature: str, bones: list[dict] | None = None, remove: list[str] | None = None) -> str:
        """Add or adjust bones: [{name, head: [x,y,z], tail: [x,y,z] (world), parent, connect, deform,
        roll (degrees)}]. Existing names are updated with only the given fields. Use it for tails,
        wings, ears, props, or to line joints up with elbows and knees."""
        return dump(call("edit_bones", armature=armature, bones=bones, remove=remove))

    @tool("rig")
    def rigify_metarig(name: str = "metarig", fit_to: str | None = None, height: float | None = None) -> str:
        """Rigify human metarig sized to a mesh. For animator-friendly IK/FK controls."""
        return dump(call("rigify_metarig", name=name, fit_to=fit_to, height=height))

    @tool("rig")
    def rigify_generate(metarig: str = "metarig") -> str:
        """Generate the Rigify control rig. Bind meshes to the generated rig (its DEF- bones deform)."""
        return dump(call("rigify_generate", timeout=600, metarig=metarig))

    @tool("rig")
    def bind_mesh(mesh: str, armature: str, method: Literal["AUTO", "ENVELOPE", "NAME"] = "AUTO") -> str:
        """Skin a mesh to an armature. AUTO (bone heat) gives the best automatic weights; it needs a
        clean, closed mesh with bones inside it. Returns a rig_report."""
        return dump(call("bind_mesh", timeout=900, mesh=mesh, armature=armature, method=method))

    @tool("rig")
    def rig_report(mesh: str, armature: str, max_influences: int = 4) -> str:
        """Skinning health: unweighted verts, verts over the bone-influence limit, unnormalized
        weights, deform bones that move nothing."""
        return dump(call("rig_report", mesh=mesh, armature=armature, max_influences=max_influences))

    @tool("rig")
    def limit_weights(mesh: str, limit: int = 4, normalize: bool = True) -> str:
        """Cap bone influences per vertex (4 for most engines) and normalize. Run before export."""
        return dump(call("limit_weights", timeout=600, mesh=mesh, limit=limit, normalize=normalize))

    @tool("rig")
    def pose_bones(armature: str, poses: dict, reset_others: bool = False) -> str:
        """Pose bones: {bone: {rotation: [x,y,z] degrees, location, scale}} in local bone space.
        On rig_template: +X raises an arm or swings a leg back, +X on a shin bends the knee,
        +X on spine bones leans forward."""
        return dump(call("pose_bones", armature=armature, poses=poses, reset_others=reset_others))

    @tool("rig")
    def reset_pose(armature: str) -> str:
        """Return all bones to the rest pose."""
        return dump(call("reset_pose", armature=armature))

    @tool("rig")
    def pose_test(mesh: str, armature: str, poses: dict | None = None, views: list[str] | None = None,
                  resolution: int = 384) -> list:
        """Render the skinned mesh in stress poses to find bad weights (collapsing elbows/knees,
        stretched or left-behind parts). Defaults suit rig_template; pass poses for other rigs."""
        result = call("pose_test", timeout=600, mesh=mesh, armature=armature, poses=poses,
                      views=views or ["front", "iso"], resolution=resolution)
        out = []
        for pose in result["poses"]:
            for img in pose["images"]:
                out.append(f"pose: {pose['pose']} / view: {img['view']}")
                out.append(image(base64.b64decode(img["png_base64"])))
        return out

    @tool("rig")
    def auto_rig_unirig(mesh: str, unirig_dir: str, python_dir: str | None = None, seed: int = 12345) -> str:
        """EXPERIMENTAL: run a local UniRig install (ML auto-rig: skeleton + skin) on a mesh in the
        background. Needs an NVIDIA GPU (8 GB+), UniRig's Python environment (python_dir = its folder)
        and bash (Git Bash on Windows). Returns a job id for auto_rig_status."""
        return dump(call("auto_rig_unirig", mesh=mesh, unirig_dir=unirig_dir, python_dir=python_dir, seed=seed))

    @tool("rig")
    def auto_rig_status(job_id: str) -> str:
        """Progress of a UniRig job; imports the rigged result once it finishes."""
        return dump(call("auto_rig_status", timeout=300, job_id=job_id))

    # --- animate -------------------------------------------------------------------

    @tool("animate")
    def keyframe(name: str, keys: list[dict], action: str | None = None,
                 interpolation: Literal["BEZIER", "LINEAR", "CONSTANT"] = "BEZIER") -> str:
        """Key an object and/or pose bones: keys = [{frame, location?, rotation? (deg), scale?,
        bones?: {bone: {rotation?, location?, scale?}}}]. action names the clip (created if missing)."""
        return dump(call("keyframe", name=name, keys=keys, action=action, interpolation=interpolation))

    @tool("animate")
    def procedural_cycle(armature: str, kind: Literal["walk", "run", "idle"] = "walk", frames: int | None = None,
                         action: str | None = None, bone_map: dict | None = None, amplitude: float = 1.0) -> str:
        """Generate a looping walk/run/idle clip. Works out of the box on rig_template; for other
        rigs map roles to bones with bone_map. Check with set_frame + capture_views, then refine
        with keyframe."""
        return dump(call("procedural_cycle", armature=armature, kind=kind, frames=frames, action=action,
                         bone_map=bone_map, amplitude=amplitude))

    @tool("animate")
    def list_actions(name: str | None = None) -> str:
        """Animation clips (all, or on one object incl. NLA) with frame ranges."""
        return dump(call("list_actions", name=name))

    @tool("animate")
    def set_action(name: str, action: str | None) -> str:
        """Make a clip the active action on an object (None clears it)."""
        return dump(call("set_action", name=name, action=action))

    @tool("animate")
    def push_to_nla(name: str, track_name: str | None = None, start_frame: int | None = None) -> str:
        """Stash the active clip in an NLA track. Each track exports as its own named animation."""
        return dump(call("push_to_nla", name=name, track_name=track_name, start_frame=start_frame))

    @tool("animate")
    def set_frame_range(start: int = 1, end: int = 250, fps: int | None = None, current: int | None = None) -> str:
        """Scene frame range and fps."""
        return dump(call("set_frame_range", start=start, end=end, fps=fps, current=current))

    @tool("animate")
    def set_frame(frame: int) -> str:
        """Go to a frame (to look at a pose with capture_views)."""
        return dump(call("set_frame", frame=frame))

    # --- export ----------------------------------------------------------------------

    @tool("export")
    def export_model(path: str, format: Literal["fbx", "glb", "gltf", "obj"] | None = None,
                     objects: list[str] | None = None,
                     preset: Literal["unity", "unreal", "godot", "generic"] = "generic",
                     animation: bool = True, apply_modifiers: bool = True) -> str:
        """Export to a file (format from the extension by default). Armatures deforming the chosen
        meshes come along automatically; NLA tracks become named clips; only deform bones export.
        Always follow with reimport_check."""
        return dump(call("export_model", timeout=900, path=path, format=format, objects=objects, preset=preset,
                         animation=animation, apply_modifiers=apply_modifiers))

    @tool("export")
    def reimport_check(path: str) -> str:
        """Re-import an exported file into a scratch scene and report objects, transforms, tris,
        bones, skinned meshes, clips and materials, then clean up. Compare against the source."""
        return dump(call("reimport_check", timeout=900, path=path))
