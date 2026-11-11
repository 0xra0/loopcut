"""MCP tools for the reference toolset: build to match a picture."""

import base64
from typing import Literal

View = Literal["front", "back", "left", "right", "side"]


def register(tool, call, dump, image):
    @tool("reference")
    def set_reference(image_path: str, view: View = "front", height: float = 1.8, base_z: float = 0.0,
                      crop: list[float] | None = None, tolerance: float = 0.06) -> str:
        """Tie a reference picture to world space for one view. The subject is cut out of the
        background, stood on base_z and scaled to `height` meters. For a turnaround sheet,
        crop = [left, top, right, bottom] in 0-1 picks this view. Side views: "right" when the
        character faces picture-left. Returns the subject size and a width profile per height.
        Raise tolerance (0.1-0.15) if parts of the background stay attached."""
        return dump(call("set_reference", image_path=image_path, view=view, height=height, base_z=base_z,
                         crop=crop, tolerance=tolerance))

    @tool("reference")
    def list_references() -> str:
        """Reference pictures set so far and their scale."""
        return dump(call("list_references"))

    @tool("reference")
    def reference_to_world(view: View, points: list[list[float]]) -> str:
        """Turn spots you see in a reference picture ([u, v], 0-1 from its top-left) into world
        positions: for measuring joints, head size or arm angle from the picture."""
        return dump(call("reference_to_world", view=view, points=points))

    @tool("reference")
    def compare_to_reference(view: View = "front", objects: list[str] | None = None, resolution: int = 512) -> list:
        """Render the model over exactly the reference picture's area and compare: picture,
        model, and overlay (red = model outside the outline, blue = outline not covered), plus
        silhouette IoU and the heights where widths differ most. Use after every shape change."""
        result = call("compare_to_reference", timeout=300, view=view, objects=objects, resolution=resolution)
        png = result.pop("png_base64")
        return [dump(result), image(base64.b64decode(png))]

    @tool("reference")
    def silhouette_mesh(name: str = "Silhouette", resolution: int = 160, depth_ratio: float = 0.75,
                        max_depth_ratio: float = 2.0) -> str:
        """Base mesh whose outline matches the front (and side) references, with rounded
        cross-sections. The no-GPU starting point: refine it, rebuild simple parts (spheres,
        cylinders) as primitives, then retopologize. Use image_to_3d instead when available."""
        return dump(call("silhouette_mesh", timeout=600, name=name, resolution=resolution, depth_ratio=depth_ratio,
                         max_depth_ratio=max_depth_ratio))

    @tool("reference")
    def paint_from_reference(name: str, views: list[View] | None = None, occlusion: bool = True) -> str:
        """Colour a dense mesh from the reference pictures (each vertex from the view it faces),
        as a colour attribute and material. Then bake_maps(maps=["DIFFUSE"]) onto the low-poly mesh.
        The picture's lighting comes along; flat materials look cleaner for stylised work."""
        return dump(call("paint_from_reference", timeout=600, name=name, views=views, occlusion=occlusion))

    @tool("reference")
    def image_to_3d(image_path: str, server: str = "http://127.0.0.1:8081", crop: list[float] | None = None,
                    texture: bool = False, seed: int = 1234, octree_resolution: int = 256,
                    face_count: int = 40000) -> str:
        """Generate a detailed mesh from one picture with a local Hunyuan3D-2 / 2.1 API server
        (NVIDIA GPU). Use the front view (crop it from a sheet). Removes the background first.
        Returns a job id; poll image_to_3d_status every 20-30 s."""
        return dump(call("image_to_3d", image_path=image_path, server=server, crop=crop, texture=texture, seed=seed,
                         octree_resolution=octree_resolution, face_count=face_count))

    @tool("reference")
    def image_to_3d_status(job_id: str, name: str = "Generated", height: float | None = None,
                           base_z: float = 0.0) -> str:
        """Progress of an image_to_3d job; when done, imports the mesh once as `name`, scaled to
        height and stood on base_z. Then check it with compare_to_reference."""
        return dump(call("image_to_3d_status", timeout=300, job_id=job_id, name=name, height=height, base_z=base_z))
