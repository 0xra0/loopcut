"""MCP tools for the modeling, sculpt, topology and uv_bake toolsets."""

from typing import Any, Literal

Vec3 = list[float]
Selector = Any  # "all" or {"indices"|"normal"+"angle"|"bounds"|"material"}


def register(tool, call, dump):
    # --- modeling --------------------------------------------------------------

    @tool("modeling")
    def create_primitive(
        kind: Literal["cube", "uv_sphere", "ico_sphere", "cylinder", "cone", "plane", "grid", "circle", "torus"],
        name: str | None = None,
        location: Vec3 | None = None,
        rotation: Vec3 | None = None,
        scale: Vec3 | None = None,
        size: float = 2.0,
        segments: int = 32,
        rings: int = 16,
        subdivisions: int = 3,
        depth: float | None = None,
        radius2: float = 0.0,
        collection: str | None = None,
        smooth: bool = False,
    ) -> str:
        """Add a mesh object. rotation in degrees. size is the overall diameter/edge length.
        cone: radius2 is the tip radius. torus: radius2 is the tube radius. Returns name and polycount."""
        return dump(call("create_primitive", kind=kind, name=name, location=location, rotation=rotation, scale=scale,
                         size=size, segments=segments, rings=rings, subdivisions=subdivisions, depth=depth,
                         radius2=radius2, collection=collection, smooth=smooth))

    @tool("modeling")
    def transform(name: str, location: Vec3 | None = None, rotation: Vec3 | None = None, scale: Vec3 | None = None,
                  relative: bool = False) -> str:
        """Set location, rotation (degrees) and scale; relative=True adds/multiplies instead."""
        return dump(call("transform", name=name, location=location, rotation=rotation, scale=scale, relative=relative))

    @tool("modeling")
    def apply_transforms(name: str, location: bool = False, rotation: bool = True, scale: bool = True) -> str:
        """Bake rotation and scale into the mesh (Ctrl+A). Do this before rigging, modifiers that
        depend on scale (bevel, solidify) and export."""
        return dump(call("apply_transforms", name=name, location=location, rotation=rotation, scale=scale))

    @tool("modeling")
    def add_modifier(name: str, type: str, settings: dict | None = None, modifier_name: str | None = None) -> str:
        """Add a modifier by Blender type: SUBSURF, MIRROR, SOLIDIFY, BEVEL, ARRAY, BOOLEAN, REMESH,
        DISPLACE, SHRINKWRAP, WEIGHTED_NORMAL, SKIN, SCREW, SIMPLE_DEFORM, LATTICE, CURVE...
        settings uses Blender property names, e.g. {"levels": 2} or {"use_axis": [true, false, false]};
        object/texture properties take a name. Use get_object_details to read current settings."""
        return dump(call("add_modifier", name=name, type=type, settings=settings, modifier_name=modifier_name))

    @tool("modeling")
    def set_modifier(name: str, modifier: str, settings: dict) -> str:
        """Change settings of an existing modifier."""
        return dump(call("set_modifier", name=name, modifier=modifier, settings=settings))

    @tool("modeling")
    def apply_modifiers(name: str, modifiers: list[str] | None = None) -> str:
        """Bake modifiers into the mesh. None = all enabled; a list must cover the top of the stack."""
        return dump(call("apply_modifiers", name=name, modifiers=modifiers))

    @tool("modeling")
    def boolean(target: str, cutter: str, operation: Literal["DIFFERENCE", "UNION", "INTERSECT"] = "DIFFERENCE",
                apply: bool = True, hide_cutter: bool = True) -> str:
        """Cut, merge or intersect two meshes (exact solver). Checkpoint first; check the result with mesh_report."""
        return dump(call("boolean", target=target, cutter=cutter, operation=operation, apply=apply,
                         hide_cutter=hide_cutter))

    @tool("modeling")
    def duplicate(name: str, new_name: str | None = None, linked: bool = False, location: Vec3 | None = None) -> str:
        """Copy an object. linked=True shares mesh data (instances)."""
        return dump(call("duplicate", name=name, new_name=new_name, linked=linked, location=location))

    @tool("modeling")
    def delete(names: list[str]) -> str:
        """Delete objects."""
        return dump(call("delete", names=names))

    @tool("modeling")
    def join(names: list[str], into: str | None = None) -> str:
        """Merge meshes into one object (the first name, or `into`)."""
        return dump(call("join", names=names, into=into))

    @tool("modeling")
    def set_parent(child: str, parent: str | None, keep_transform: bool = True) -> str:
        """Parent an object (parent=None clears it)."""
        return dump(call("set_parent", child=child, parent=parent, keep_transform=keep_transform))

    @tool("modeling")
    def mesh_edit(name: str,
                  op: Literal["extrude", "inset", "bevel", "subdivide", "delete", "scale", "translate",
                              "triangulate", "flip_normals"],
                  select: Selector = "all", params: dict | None = None) -> str:
        """Edit part of a mesh without edit mode.
        select: "all" | {"normal": [0,0,1], "angle": 30} (faces facing a direction, world space)
          | {"bounds": {"min": [x,y,z], "max": [x,y,z]}} (face centers, world space)
          | {"indices": [...]} | {"material": "Name"}; keys combine with AND.
        params: extrude {distance} | inset {thickness, depth, individual} | bevel {width, segments}
          | subdivide {cuts, smooth} | scale {factor: [x,y,z]} | translate {offset: [x,y,z]}."""
        return dump(call("mesh_edit", name=name, op=op, select=select, params=params))

    # --- topology --------------------------------------------------------------

    @tool("topology")
    def voxel_remesh(name: str, voxel_size: float | None = None, target_faces: int | None = None,
                     adaptivity: float = 0.0) -> str:
        """Rebuild as an even, watertight surface (fuses overlapping parts). Destroys UVs."""
        return dump(call("voxel_remesh", timeout=600, name=name, voxel_size=voxel_size, target_faces=target_faces,
                         adaptivity=adaptivity))

    @tool("topology")
    def quad_remesh(name: str, target_faces: int = 5000, use_symmetry: bool = False, preserve_sharp: bool = False) -> str:
        """QuadriFlow retopology to about target_faces clean quads. For a game mesh from a sculpt:
        duplicate the sculpt, quad_remesh the copy, then bake from the sculpt. Destroys UVs."""
        return dump(call("quad_remesh", timeout=900, name=name, target_faces=target_faces, use_symmetry=use_symmetry,
                         preserve_sharp=preserve_sharp))

    @tool("topology")
    def decimate(name: str, target_tris: int | None = None, ratio: float | None = None,
                 symmetry_axis: Literal["X", "Y", "Z"] | None = None,
                 method: Literal["COLLAPSE", "PLANAR", "UNSUBDIV"] = "COLLAPSE", planar_angle: float = 5.0) -> str:
        """Cut polycount to a triangle budget while keeping the silhouette. Produces triangles;
        prefer quad_remesh when the mesh will deform (characters)."""
        return dump(call("decimate", timeout=600, name=name, target_tris=target_tris, ratio=ratio,
                         symmetry_axis=symmetry_axis, method=method, planar_angle=planar_angle))

    @tool("topology")
    def cleanup(name: str, merge_distance: float = 0.0001, delete_loose: bool = True,
                dissolve_degenerate: bool = True, recalc_normals: bool = True, fill_holes: bool = False) -> str:
        """Merge duplicate verts, delete loose geometry and zero-area faces, fix normals, optionally fill holes."""
        return dump(call("cleanup", name=name, merge_distance=merge_distance, delete_loose=delete_loose,
                         dissolve_degenerate=dissolve_degenerate, recalc_normals=recalc_normals, fill_holes=fill_holes))

    @tool("topology")
    def shade(name: str, smooth: bool = True, sharp_angle: float | None = None) -> str:
        """Flat or smooth shading; with sharp_angle (degrees) hard edges stay crisp."""
        return dump(call("shade", name=name, smooth=smooth, sharp_angle=sharp_angle))

    # --- sculpt ----------------------------------------------------------------

    @tool("sculpt")
    def sculpt_prepare(name: str, target_faces: int = 200000, voxel_size: float | None = None) -> str:
        """Turn a blockout into a dense, even mesh ready for brushes (applies modifiers, voxel remesh,
        smooth shading). 50k faces for big forms, 200k-1M for fine detail. Checkpoint first."""
        return dump(call("sculpt_prepare", timeout=600, name=name, target_faces=target_faces, voxel_size=voxel_size))

    @tool("sculpt")
    def surface_points(name: str, view: Literal["front", "back", "left", "right", "top", "bottom"] | None = None,
                       coords: list[list[float]] | None = None, points: list[Vec3] | None = None) -> str:
        """Find surface positions to aim sculpt_stroke at. view + coords: [[u, v], ...] in 0-1 across
        the object's bounding box as seen in that capture_views view (u left to right, v bottom to
        top); each returns the first surface hit from that side, so [[0.5, 0.8]] on "front" is the
        front surface near the top center. points: world positions snapped to the closest surface.
        Returns world-space co and normal per point; hit is false where a ray missed."""
        return dump(call("surface_points", name=name, view=view, coords=coords, points=points))

    @tool("sculpt")
    def sculpt_stroke(name: str,
                      brush: Literal["draw", "clay", "blob", "inflate", "smooth", "flatten", "pinch", "crease", "grab"],
                      points: list[Vec3], radius: float, strength: float = 0.5, symmetry: str = "X",
                      falloff: Literal["smooth", "sphere", "sharp", "linear", "constant"] = "smooth",
                      invert: bool = False, spacing: float = 0.25, delta: Vec3 | None = None) -> str:
        """Brush along a path of world-space points ON or near the surface (get them from
        surface_points). Dabs are placed every radius*spacing.
        draw/blob raise (invert carves), clay builds flat layers, inflate swells, smooth relaxes,
        flatten levels, pinch tightens toward the path, crease cuts a sharp groove,
        grab moves the area at points[0] by delta. symmetry mirrors in local X/Y/Z ("" for none).
        Work big to small: large radius and low strength for forms, then smaller radius for detail.
        Look with capture_views after a few strokes."""
        return dump(call("sculpt_stroke", timeout=600, name=name, brush=brush, points=points, radius=radius,
                         strength=strength, symmetry=symmetry, falloff=falloff, invert=invert, spacing=spacing,
                         delta=delta))

    @tool("sculpt")
    def symmetrize(name: str, direction: Literal["+X", "-X", "+Y", "-Y", "+Z", "-Z"] = "+X") -> str:
        """Copy one half of the mesh onto the other (local axes). +X copies the +X side onto -X."""
        return dump(call("symmetrize", name=name, direction=direction))

    @tool("sculpt")
    def smooth_mesh(name: str, iterations: int = 5, factor: float = 0.5, preserve_volume: bool = True) -> str:
        """Relax the whole surface."""
        return dump(call("smooth_mesh", timeout=600, name=name, iterations=iterations, factor=factor,
                         preserve_volume=preserve_volume))

    @tool("sculpt")
    def surface_noise(name: str,
                      texture: Literal["CLOUDS", "VORONOI", "MARBLE", "STUCCI", "DISTORTED_NOISE", "MUSGRAVE", "WOOD"] = "CLOUDS",
                      scale: float = 0.25, strength: float = 0.02, detail: int = 2) -> str:
        """Add procedural surface detail (pores, scales, rock, bark) along normals. Needs a dense mesh."""
        return dump(call("surface_noise", timeout=600, name=name, texture=texture, scale=scale, strength=strength,
                         detail=detail))

    # --- uv_bake ---------------------------------------------------------------

    @tool("uv_bake")
    def uv_unwrap(name: str, method: Literal["smart", "angle_based", "conformal", "cube"] = "smart",
                  angle_limit: float = 66.0, island_margin: float = 0.02, seams_from_angle: float | None = None) -> str:
        """Unwrap UVs and return a uv_report. smart for props and hard-surface; angle_based with
        seams_from_angle for organic meshes; cube for quick blockouts."""
        return dump(call("uv_unwrap", timeout=600, name=name, method=method, angle_limit=angle_limit,
                         island_margin=island_margin, seams_from_angle=seams_from_angle))

    @tool("uv_bake")
    def uv_pack(names: list[str], margin: float = 0.01, rotate: bool = True) -> str:
        """Pack UV islands of one or more meshes into one 0-1 atlas."""
        return dump(call("uv_pack", timeout=600, names=names, margin=margin, rotate=rotate))

    @tool("uv_bake")
    def uv_report(name: str) -> str:
        """UV quality numbers: islands, faces outside 0-1, coverage, overlap, texel density spread.
        Before baking you want overlap ~0 and faces_out_of_bounds 0."""
        return dump(call("uv_report", name=name))

    @tool("uv_bake")
    def bake_maps(low: str, high: str | list[str] | None = None,
                  maps: list[Literal["NORMAL", "AO", "DIFFUSE", "ROUGHNESS", "EMIT", "COMBINED"]] | None = None,
                  size: int = 1024, cage_extrusion: float = 0.05, max_ray_distance: float = 0.0, samples: int = 16,
                  output_dir: str | None = None, connect: bool = True) -> str:
        """Bake maps for the low-poly mesh from the high-poly one(s) with Cycles and save PNGs
        (next to the .blend in textures/, or a temp folder). connect wires them into the material.
        Can take minutes for big sizes. Check the result with capture_views."""
        return dump(call("bake_maps", timeout=1800, low=low, high=high, maps=maps or ["NORMAL"], size=size,
                         cage_extrusion=cage_extrusion, max_ray_distance=max_ray_distance, samples=samples,
                         output_dir=output_dir, connect=connect))
