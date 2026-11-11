"""Shared, Blender-independent command-line validation for the mesh pass."""
import argparse
import json
import math
from pathlib import Path


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be finite")
    return number


def resolve_source_up(src, source_up):
    """auto -> z for raw Pixal3D GLBs (written by trimesh), y for Blender-exported or non-glTF files."""
    if source_up != "auto":
        return source_up
    ext = Path(src).suffix.lower()
    if ext not in (".glb", ".gltf"):
        return "y"
    with open(src, "rb") as stream:
        if ext == ".glb":
            header = stream.read(20)
            doc = json.loads(stream.read(int.from_bytes(header[12:16], "little")))
        else:
            doc = json.load(stream)
    return "z" if "trimesh" in doc.get("asset", {}).get("generator", "") else "y"


def add_options(parser, faces=40000, formats="glb"):
    parser.add_argument("--faces", type=nonnegative, default=faces, help="triangle budget; 0 keeps all triangles")
    parser.add_argument("--source-up", choices=("auto", "y", "z"), default="auto",
                        help="z: source stores Z-up geometry in a Y-up GLB (Pixal3D multiview), stand it upright; "
                             "auto: z for trimesh-written glTF (raw Pixal3D output), y for everything else")
    parser.add_argument("--yaw", type=finite_float, default=0,
                        help="degrees to turn the model about its up axis once upright; positive is counterclockwise "
                             "seen from above (180 turns the back to the front)")
    parser.add_argument("--repair", choices=("off", "light", "full"), default="full")
    parser.add_argument("--angle", type=finite_float, default=35, help="shading crease angle; 0 smooths all")
    parser.add_argument("--unwrap", choices=("smart", "keep", "none"), default="smart",
                        help="none: sculpt base, no UVs or materials, voxel quads kept")
    parser.add_argument("--uv-method", choices=("xatlas", "smart"), default="xatlas",
                        help="xatlas charts triangle meshes (needs PA_XATLAS_PYTHON); quad cages always use Smart UV")
    parser.add_argument("--uv-angle", type=finite_float, default=66)
    parser.add_argument("--uv-merge-faces", type=nonnegative, default=20,
                        help="merge Smart UV islands smaller than this into a neighbour; 0 disables")
    parser.add_argument("--texture-size", type=nonnegative, default=2048)
    parser.add_argument("--uv-margin-px", type=finite_float, default=4)
    parser.add_argument("--weld-distance", type=finite_float, default=0.000001,
                        help="merge distance as fraction of bounding-box diagonal")
    parser.add_argument("--hole-max-edges", type=nonnegative, default=None,
                        help="12 for light, 64 for full; 0 disables filling")
    parser.add_argument("--hole-max-diameter", type=finite_float, default=0.03,
                        help="maximum hole diameter as fraction of mesh diagonal; 0 removes size limit")
    parser.add_argument("--min-component-faces", type=nonnegative, default=0,
                        help="opt-in removal of tiny disconnected parts; default preserves chain links")
    parser.add_argument("--dust-size", type=finite_float, default=0.015,
                        help="remove open shells under this fraction of the diagonal; closed parts stay; 0 disables")
    parser.add_argument("--smooth-iterations", type=nonnegative, default=0)
    parser.add_argument("--smooth-factor", type=finite_float, default=0.2)
    parser.add_argument("--voxel-size", type=finite_float, default=0,
                        help="opt-in voxel remesh as fraction of diagonal; destroys source UVs")
    parser.add_argument("--quad-faces", type=nonnegative, default=0,
                        help="--unwrap none only: QuadriFlow quad cage of about this many faces")
    parser.add_argument("--multires", type=nonnegative, default=0,
                        help="multires levels over the quad cage, detail projected from the remesh (.blend keeps them)")
    parser.add_argument("--materials", choices=("bake", "discard"), default="bake",
                        help="Smart UV: transfer existing PBR textures, or discard before retexturing")
    parser.add_argument("--formats", default=formats, help="comma-separated glb,obj,fbx,blend")
    parser.add_argument("--strict", action="store_true", help="reject remaining open/non-manifold geometry")
    return parser


def validate(parser, args):
    if not 0 <= args.angle <= 180 or not 1 <= args.uv_angle <= 89:
        parser.error("--angle must be 0..180; --uv-angle must be 1..89")
    if not 256 <= args.texture_size <= 8192:
        parser.error("--texture-size must be 256..8192")
    if not 0 <= args.uv_margin_px < args.texture_size / 4:
        parser.error("--uv-margin-px must be nonnegative and less than a quarter of texture size")
    if not 0 <= args.weld_distance <= 0.01 or not 0 <= args.hole_max_diameter <= 1:
        parser.error("--weld-distance must be 0..0.01; --hole-max-diameter must be 0..1")
    if not 0 <= args.dust_size <= 0.1:
        parser.error("--dust-size must be 0..0.1")
    if not 0 <= args.smooth_factor <= 1 or not 0 <= args.voxel_size <= 0.1:
        parser.error("--smooth-factor must be 0..1; --voxel-size must be 0..0.1")
    args.formats = tuple(dict.fromkeys(x.strip().lower() for x in args.formats.split(",")))
    if not args.formats or any(x not in ("glb", "obj", "fbx", "blend") for x in args.formats):
        parser.error("--formats accepts glb,obj,fbx,blend")
    if args.voxel_size and (args.unwrap == "keep" or args.materials != "discard"):
        parser.error("voxel remesh requires --unwrap smart|none --materials discard; retexture afterwards")
    if args.unwrap == "none" and (args.materials != "discard" or args.faces):
        parser.error("--unwrap none requires --materials discard --faces 0")
    if (args.quad_faces or args.multires) and not args.voxel_size:
        parser.error("--quad-faces/--multires require --voxel-size (QuadriFlow needs a manifold)")
    if args.multires and not args.quad_faces or args.multires > 5:
        parser.error("--multires needs --quad-faces and at most 5 levels")
    if args.repair == "off" and args.voxel_size:
        parser.error("--repair off cannot be combined with --voxel-size")
    return args


def parse(argv):
    parser = argparse.ArgumentParser(description="Repair static meshes, Smart UV unwrap, transfer PBR and export.")
    parser.add_argument("src")
    parser.add_argument("dst", help="output basename or .glb path")
    parser.add_argument("legacy_faces", type=nonnegative, nargs="?", default=None)
    add_options(parser)
    args = parser.parse_args(argv)
    if args.legacy_faces is not None and not any(x == "--faces" or x.startswith("--faces=") for x in argv):
        args.faces = args.legacy_faces
    return validate(parser, args)
