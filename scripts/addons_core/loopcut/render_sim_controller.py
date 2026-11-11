"""Rendering and simulation controller for Loopcut.

Manages:
- Cameras (focal length, sensor size, depth of field, framing)
- Lighting setups (three-point studio lighting, HDRI backgrounds, area lights)
- Render engine configuration (Cycles vs Eevee, GPU compute, samples, denoiser)
- Physics simulations (cloth, collision, rigid body) and cache baking
- Output rendering and image validation
"""


def setup_camera(camera_name: str = "Camera", focal_length: float = 50.0,
                 dof_target: str = "", frame_object: str = "") -> dict:
    """Configure or create camera with optics and depth-of-field."""
    import bpy
    cam_obj = bpy.data.objects.get(camera_name)
    if not cam_obj or cam_obj.type != "CAMERA":
        cam_data = bpy.data.cameras.new(camera_name)
        cam_obj = bpy.data.objects.new(camera_name, cam_data)
        bpy.context.collection.objects.link(cam_obj)

    cam = cam_obj.data
    cam.lens = focal_length

    if dof_target and dof_target in bpy.data.objects:
        cam.dof.use_dof = True
        cam.dof.focus_object = bpy.data.objects[dof_target]

    bpy.context.scene.camera = cam_obj
    return {"status": "ok", "camera": camera_name, "focal_length": focal_length}


def setup_studio_lighting(energy_scale: float = 1.0) -> dict:
    """Create a standard three-point studio lighting rig (Key, Fill, Rim)."""
    import bpy
    col = bpy.context.collection

    lights = [
        ("Key_Light", "AREA", (3.0, -3.0, 3.5), 300.0 * energy_scale, 1.5),
        ("Fill_Light", "AREA", (-3.5, -2.5, 2.0), 100.0 * energy_scale, 2.0),
        ("Rim_Light", "SPOT", (0.0, 4.0, 3.0), 250.0 * energy_scale, 0.5),
    ]

    created = []
    for name, l_type, loc, power, size in lights:
        obj = bpy.data.objects.get(name)
        if not obj:
            l_data = bpy.data.lights.new(name=name, type=l_type)
            obj = bpy.data.objects.new(name=name, object_data=l_data)
            col.objects.link(obj)
        obj.location = loc
        obj.data.energy = power
        if hasattr(obj.data, "size"):
            obj.data.size = size
        created.append(name)

    return {"status": "ok", "action": "setup_studio_lighting", "lights": created}


def configure_render_engine(engine: str = "CYCLES", device: str = "GPU",
                            samples: int = 128, use_denoiser: bool = True) -> dict:
    """Configure render settings for Cycles or Eevee."""
    import bpy
    scene = bpy.context.scene
    scene.render.engine = "CYCLES" if engine.upper() == "CYCLES" else "BLENDER_EEVEE_NEXT"

    if scene.render.engine == "CYCLES":
        scene.cycles.device = device.upper()
        scene.cycles.samples = samples
        scene.cycles.use_denoising = use_denoiser

    return {
        "status": "ok",
        "engine": scene.render.engine,
        "device": getattr(scene.cycles, "device", "CPU"),
        "samples": samples,
    }


def add_physics_cloth(object_name: str, preset: str = "SILK") -> dict:
    """Add a cloth simulation modifier to object_name."""
    import bpy
    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != "MESH":
        raise ValueError(f"Mesh '{object_name}' not found.")

    mod = obj.modifiers.new(name="Cloth", type="CLOTH")
    settings = mod.settings
    if preset.upper() == "SILK":
        settings.mass = 0.1
        settings.tension_stiffness = 5.0
        settings.compression_stiffness = 5.0
        settings.shear_stiffness = 5.0
        settings.bending_stiffness = 0.5
    elif preset.upper() == "LEATHER":
        settings.mass = 0.4
        settings.tension_stiffness = 40.0
        settings.compression_stiffness = 40.0

    return {"status": "ok", "action": "add_cloth", "object": object_name, "preset": preset}
