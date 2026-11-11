"""Portable production asset presets, CLI validation and budget allocation."""
import argparse
import json
import math
from pathlib import Path

from mesh_config import add_options, validate

PRESETS = {
    "background": {"faces": 5000, "texture_size": 1024, "max_vertices": 15000, "min_uv_coverage": .35},
    "prop": {"faces": 20000, "texture_size": 2048, "max_vertices": 60000, "min_uv_coverage": .40},
    "hero": {"faces": 40000, "texture_size": 2048, "max_vertices": 120000, "min_uv_coverage": .45},
}
ENGINES = {
    "generic": {"normal": "OpenGL", "packed": "ORM", "preferred_format": "glb", "units": "meters"},
    "godot": {"normal": "OpenGL", "packed": "ORM", "preferred_format": "glb", "units": "meters"},
    "unreal": {"normal": "DirectX", "packed": "ORM", "preferred_format": "fbx", "units": "centimeters"},
    "unity": {"normal": "OpenGL", "packed": "MetallicSmoothness", "preferred_format": "fbx", "units": "meters",
              "render_pipeline": "URP"},
    "cyberpunk": {"normal": "template-dependent", "packed": "separate source maps", "preferred_format": "glb",
                  "units": "meters", "game": "Cyberpunk 2077", "redengine": 4, "tool": "WolvenKit"},
    "witcher3": {"normal": "template-dependent", "packed": "separate source maps", "preferred_format": "fbx",
                 "units": "meters", "game": "The Witcher 3 Remastered", "redengine": 3, "tool": "REDkit"},
}
RED_ENGINES = ("cyberpunk", "witcher3")


def ratios(value):
    try:
        values = tuple(float(x) for x in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated ratios") from exc
    if not values or len(values) > 6 or values[0] != 1 or any(not math.isfinite(x) or x <= 0 or x > 1 for x in values):
        raise argparse.ArgumentTypeError("LOD ratios must start with 1 and be in (0,1]")
    if any(a <= b for a, b in zip(values, values[1:])):
        raise argparse.ArgumentTypeError("LOD ratios must strictly decrease")
    return values


def parse_asset(argv):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--preset", choices=PRESETS, default="prop")
    pre.add_argument("--engine", choices=ENGINES, default="generic")
    known, _ = pre.parse_known_args(argv)
    preset = PRESETS[known.preset]
    parser = argparse.ArgumentParser(description="Build a static game asset, high-to-low bakes, LODs and QA.")
    parser.add_argument("src")
    parser.add_argument("dst", help="output directory (one asset per directory)")
    add_options(parser, faces=preset["faces"], formats="glb,fbx,blend")
    parser.set_defaults(texture_size=preset["texture_size"])
    parser.add_argument("--preset", choices=PRESETS, default=known.preset)
    parser.add_argument("--engine", choices=ENGINES, default="generic")
    parser.add_argument("--game-version", help="REDengine target version label; records intent, not certified compatibility")
    parser.add_argument("--red-template", help="optional game depot path to a .mesh/.w2mesh template; records the handoff target")
    parser.add_argument("--name", default=None)
    parser.add_argument("--source-object", help="select one named .blend mesh; otherwise use render-visible meshes")
    parser.add_argument("--alpha-mode", choices=("auto", "opaque", "mask", "blend"), default="auto")
    parser.add_argument("--two-sided", action="store_true")
    parser.add_argument("--regions", help="JSON file of normalized AABB surface-cleanup regions")
    parser.add_argument("--surface-smooth", type=int, default=2)
    parser.add_argument("--surface-factor", type=float, default=.15)
    parser.add_argument("--feature-angle", type=float, default=55)
    parser.add_argument("--protect-component-size", type=float, default=.12)
    parser.add_argument("--retopology", choices=("decimate", "quadriflow"), default="decimate")
    parser.add_argument("--lod-ratios", type=ratios, default=ratios("1,.5,.25,.125"))
    parser.add_argument("--collision", choices=("none", "box", "hull", "compound"), default="box")
    parser.add_argument("--collision-max-hulls", type=int, default=16)
    parser.add_argument("--size-meters", type=float, default=0, help="longest dimension in meters; 0 preserves scale")
    parser.add_argument("--pivot", choices=("source", "center", "base"),
                        default="source" if known.engine in RED_ENGINES else "base")
    parser.add_argument("--pack-method", choices=("AABB", "CONVEX", "CONCAVE"), default="CONVEX")
    parser.add_argument("--no-uv-relax", dest="uv_relax", action="store_false", default=True)
    parser.add_argument("--chart-merge-angle", type=float, default=20)
    parser.add_argument("--max-chart-faces", type=int, default=512)
    parser.add_argument("--pack-search", action="store_true", help="try additional convex and concave pack candidates")
    parser.add_argument("--pack-timeout", type=int, default=20, help="maximum seconds per isolated UV pack candidate")
    parser.add_argument("--bake-samples", type=int, default=16)
    parser.add_argument("--cage-extrusion", type=float, default=.01, help="fraction of each component diagonal")
    parser.add_argument("--ray-distance", type=float, default=.05, help="fraction of each component diagonal")
    parser.add_argument("--preview-size", type=int, default=256)
    parser.add_argument("--turntable-views", type=int, default=6)
    parser.add_argument("--skip-previews", action="store_true")
    parser.add_argument("--quality", choices=("review", "strict"), default="review")
    parser.add_argument("--min-uv-coverage", type=float, default=preset["min_uv_coverage"])
    parser.add_argument("--max-vertices", type=int, default=preset["max_vertices"])
    parser.add_argument("--max-materials", type=int, default=1)
    parser.add_argument("--max-texture-mib", type=float, default=128)
    parser.add_argument("--max-bake-miss", type=float, default=.02)
    parser.add_argument("--max-shape-error", type=float, default=.02)
    parser.add_argument("--engine-bin", help="Godot, Unity Editor or UnrealEditor executable")
    parser.add_argument("--engine-project", help="Unity project directory or Unreal .uproject file")
    parser.add_argument("--engine-check", choices=("auto", "required", "off"), default="auto")
    parser.add_argument("--texture-from", help="textured GLB of this build's LOD0 (pa.py texture output); its material "
                        "replaces the source colours for every LOD bake")
    args = validate(parser, parser.parse_args(argv))
    if args.texture_from:
        args.texture_from = str(Path(args.texture_from).expanduser().resolve())
        if not Path(args.texture_from).is_file() or args.materials == "discard":
            parser.error("--texture-from needs an existing GLB and --materials bake")
    if args.unwrap != "smart":
        parser.error("production builds generate Smart UVs; use prep/fix for --unwrap keep")
    if not 0 <= args.surface_smooth <= 100 or not 0 <= args.surface_factor <= 1:
        parser.error("surface smoothing must be 0..100 iterations and factor 0..1")
    if not 0 <= args.protect_component_size <= 1 or not 0 < args.feature_angle <= 180:
        parser.error("invalid component protection or feature angle")
    if not 1 <= args.collision_max_hulls <= 128 or not math.isfinite(args.size_meters) or args.size_meters < 0:
        parser.error("invalid collision hull count or size")
    if not 1 <= args.bake_samples <= 4096 or not 0 <= args.cage_extrusion <= .5 or not 0 < args.ray_distance <= 1:
        parser.error("invalid bake samples, cage extrusion or ray distance")
    if not 64 <= args.preview_size <= 2048 or not 2 <= args.turntable_views <= 36:
        parser.error("preview size must be 64..2048; turntable views 2..36")
    if not 1 <= args.pack_timeout <= 600:
        parser.error("pack timeout must be 1..600 seconds")
    if not math.isfinite(args.chart_merge_angle) or not 0 <= args.chart_merge_angle <= 90 or args.max_chart_faces < 1:
        parser.error("invalid UV chart angle or face limit")
    for field in ("surface_factor", "feature_angle", "protect_component_size", "cage_extrusion", "ray_distance",
                  "min_uv_coverage", "max_texture_mib", "max_bake_miss", "max_shape_error"):
        if not math.isfinite(getattr(args, field)):
            parser.error(field + " must be finite")
    if not 0 <= args.min_uv_coverage <= 1 or not 0 <= args.max_bake_miss <= 1 or not 0 <= args.max_shape_error <= 1:
        parser.error("quality fractions must be 0..1")
    if args.max_vertices < 1 or args.max_materials < 1 or args.max_texture_mib <= 0:
        parser.error("budgets must be positive")
    args.name = args.name or Path(args.src).stem
    if not args.name or not all(c.isalnum() or c == "_" for c in args.name):
        parser.error("--name must contain letters, digits or underscores")
    if args.engine_check == "required" and args.engine == "generic":
        parser.error("required engine check needs --engine godot|unity|unreal")
    if args.engine in RED_ENGINES:
        if args.engine_check == "required":
            parser.error("REDengine native import checks are not implemented; use auto/off and validate in WolvenKit/REDkit")
        args.game_version = args.game_version or ("2.5" if args.engine == "cyberpunk" else "5.01")
        if not all(c.isalnum() or c in "._-" for c in args.game_version):
            parser.error("--game-version must be a simple version label")
        if args.red_template:
            args.red_template = args.red_template.replace("\\", "/")
            suffix = ".mesh" if args.engine == "cyberpunk" else ".w2mesh"
            if (not args.red_template.endswith(suffix) or args.red_template.startswith("/") or
                    ":" in args.red_template or any(x in ("", ".", "..") for x in args.red_template.split("/"))):
                parser.error("--red-template must be a relative game depot path ending in " + suffix)
        if args.engine == "witcher3" and "fbx" not in args.formats:
            args.formats = (*args.formats, "fbx")
    elif args.game_version or args.red_template:
        parser.error("--game-version/--red-template require --engine cyberpunk|witcher3")
    return args


def allocate_budget(counts, weights, target, minimum=12):
    """Bounded, deterministic weighted allocation that preserves every component."""
    if len(counts) != len(weights) or any(x <= 0 for x in counts):
        raise ValueError("invalid component counts")
    if not target or target >= sum(counts):
        return list(counts)
    allocation = [min(minimum, n) for n in counts]
    if sum(allocation) > target:
        raise ValueError("Triangle budget cannot preserve all components; increase --faces or explicitly remove shards")
    remaining = target - sum(allocation)
    while remaining:
        eligible = [i for i, n in enumerate(counts) if allocation[i] < n]
        share_weights = [max(weights[i], 1e-30) for i in eligible]
        total = sum(share_weights)
        shares = [remaining * w / total for w in share_weights]
        used = 0
        for i, share in zip(eligible, shares):
            n = min(counts[i] - allocation[i], int(share))
            allocation[i] += n
            used += n
        remaining -= used
        if not used:
            for i in sorted(eligible, key=lambda i: (-weights[i], i))[:remaining]:
                allocation[i] += 1
                remaining -= 1
    return allocation


def load_regions(path):
    if not path:
        return []
    data = json.loads(Path(path).read_text())
    regions = data.get("regions") if isinstance(data, dict) else data
    if not isinstance(regions, list):
        raise ValueError("regions must be an array")
    for region in regions:
        if set(region) - {"name", "bounds", "smooth_iterations", "smooth_factor", "voxel_size", "force"}:
            raise ValueError("unknown region property")
        bounds = region.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 2 or any(len(x) != 3 for x in bounds):
            raise ValueError("region bounds must be [[minX,minY,minZ],[maxX,maxY,maxZ]]")
        if any(not isinstance(x, (int, float)) or not math.isfinite(x) or not 0 <= x <= 1 for row in bounds for x in row):
            raise ValueError("region bounds must be normalized 0..1")
        if any(a > b for a, b in zip(*bounds)):
            raise ValueError("region minimum exceeds maximum")
        if not 0 <= region.get("smooth_iterations", 0) <= 100 or not 0 <= region.get("smooth_factor", .15) <= 1:
            raise ValueError("invalid region smoothing")
        if not 0 <= region.get("voxel_size", 0) <= .1:
            raise ValueError("invalid region voxel size")
    return regions
