"""Detail normals (detail_maps.py) in Blender and through export_for_engine, on SPLAP.

    python3 loopcut/windows/laptop.py check --script loopcut/harness/detail_maps_check.py --wait

Needs Poly Haven reachable (cotton_jersey's 1k nor_gl, cached after the first run).
- A 2 m plane with UVs over 0-1 and a 1024 px normal map of soft bumps: add_detail_normal(fabric, scale 1) wires
  base map -> "Loopcut Detail Normal" (RNM) -> its Normal Map node, the detail through a UV Map and Mapping node,
  Non-Color and packed; check_shading still finds the base map; a second call updates it in place.
- What Blender renders equals what the export bakes: a Cycles tangent-space normal bake of the plane against
  detail_maps.combined(), texel by texel (within 8-bit rounding).
- Godot GLB: the material's one normal texture is the combined map (not the base map); the material is as it was
  after. Unreal FBX: the embedded normal map is the combined map with green flipped.
- Without a normal map: a flat base and a Normal Map node are added. A 1.8 m sphere gets a default scale from the
  texture's real size.
Writes detail-maps-check.json to the working folder. Exits 0 when every check passes.
"""

import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
from loopcut import detail_maps, engine_export, glb, shading  # noqa: E402

TIMEOUT = 600
RESULTS = []
SIZE = 1024


def check(name: str, ok, detail="") -> None:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:1200]})
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  [{str(detail)[:1200]}]" if detail else ""), flush=True)


def plane():
    import bmesh
    mesh = bpy.data.meshes.new("Plane")
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=1.0, calc_uvs=True)
    bm.to_mesh(mesh)
    bm.free()
    ob = bpy.data.objects.new("Plane", mesh)
    bpy.context.scene.collection.objects.link(ob)
    layer = mesh.uv_layers.active or mesh.uv_layers.new(name="UVMap")
    xs, ys = [v.co.x for v in mesh.vertices], [v.co.y for v in mesh.vertices]
    for loop in mesh.loops:  # UVs exactly the plane's x, y over 0-1.
        co = mesh.vertices[loop.vertex_index].co
        layer.uv[loop.index].vector = ((co.x - min(xs)) / (max(xs) - min(xs)), (co.y - min(ys)) / (max(ys) - min(ys)))
    return ob


def base_image():
    """Soft bumps, tangent-space, OpenGL-style, Non-Color."""
    import numpy as np
    image = bpy.data.images.new("bumps_normal", SIZE, SIZE, alpha=False)
    image.colorspace_settings.name = "Non-Color"
    u, v = np.meshgrid((np.arange(SIZE) + 0.5) / SIZE, (np.arange(SIZE) + 0.5) / SIZE)
    x, y = 0.25 * np.cos(u * math.pi * 6), 0.25 * np.sin(v * math.pi * 4)
    n = np.stack([x, y, np.ones_like(x)], axis=-1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    rgba = np.concatenate([n * 0.5 + 0.5, np.ones((SIZE, SIZE, 1))], axis=-1).astype(np.float32)
    image.pixels.foreach_set(rgba.ravel())
    image.pack()
    return image


def material_with_map(name: str, image=None):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    bsdf = tree.nodes["Principled BSDF"]
    if image is not None:
        texture = tree.nodes.new("ShaderNodeTexImage")
        texture.image = image
        normal = tree.nodes.new("ShaderNodeNormalMap")
        tree.links.new(texture.outputs["Color"], normal.inputs["Color"])
        tree.links.new(normal.outputs["Normal"], bsdf.inputs["Normal"])
    return material


def pixels(image):
    import numpy as np
    data = np.empty(image.size[0] * image.size[1] * image.channels, dtype=np.float32)
    image.pixels.foreach_get(data)
    return data.reshape(image.size[1], image.size[0], image.channels)[..., :3]


def angles(a, b):
    import numpy as np
    x, y = a * 2 - 1, b * 2 - 1
    x /= np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-9)
    y /= np.maximum(np.linalg.norm(y, axis=-1, keepdims=True), 1e-9)
    d = np.degrees(np.arccos(np.clip((x * y).sum(axis=-1), -1, 1)))
    return {"mean": round(float(d.mean()), 3), "p99": round(float(np.percentile(d, 99)), 3)}


def loaded(data: bytes, folder: Path, name: str):
    path = folder / f"{name}.png"
    path.write_bytes(data)
    image = bpy.data.images.load(str(path), check_existing=False)
    image.colorspace_settings.name = "Non-Color"
    try:
        return pixels(image).copy()
    finally:
        bpy.data.images.remove(image)


def cycles_bake(ob, material):
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    target = bpy.data.images.new("baked_normal", SIZE, SIZE, alpha=False)
    target.colorspace_settings.name = "Non-Color"
    node = material.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = target
    material.node_tree.nodes.active = node
    bpy.context.view_layer.update()  # Objects removed through bpy.data: sync the view layer first.
    for other in bpy.context.view_layer.objects:
        if other is not None:
            other.select_set(False)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    with bpy.context.temp_override(active_object=ob, object=ob, selected_objects=[ob], selected_editable_objects=[ob]):
        bpy.ops.object.bake(type="NORMAL", normal_space="TANGENT", margin=0, use_clear=True)
    result = pixels(target).copy()
    material.node_tree.nodes.remove(node)
    bpy.data.images.remove(target)
    return result


def gltf_normal(path: Path, folder: Path, material: str):
    document, binary = glb.parse(path.read_bytes())
    found = [m for m in document.get("materials", []) if m.get("name") == material]
    index = (found[0].get("normalTexture") or {}).get("index") if found else None
    if index is None:
        return None, document.get("materials")
    image = document["images"][document["textures"][index]["source"]]
    view = document["bufferViews"][image["bufferView"]]
    start = view.get("byteOffset", 0)
    return loaded(bytes(binary[start:start + view["byteLength"]]), folder, "gltf_normal"), image.get("name")


def fbx_normal(path: Path, folder: Path, name_part: str):
    from io_scene_fbx import parse_fbx
    root, _ = parse_fbx.parse(str(path))
    objects = next(e for e in root.elems if e.id == b"Objects")
    for elem in objects.elems:
        if elem.id == b"Video" and name_part.encode() in bytes(elem.props[1]):
            content = next((c for c in elem.elems if c.id == b"Content"), None)
            if content is not None and content.props and len(content.props[0]):
                return loaded(bytes(content.props[0]), folder, "fbx_normal")
    return None


def tree_state(material):
    tree = material.node_tree
    return sorted((n.bl_idname, n.name) for n in tree.nodes), sorted(
        (l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier) for l in tree.links)


def main() -> int:
    folder = Path(tempfile.mkdtemp(prefix="lc_detail_check_")).resolve()
    t0 = time.time()
    try:
        for ob in list(bpy.data.objects):
            bpy.data.objects.remove(ob)
        bpy.context.view_layer.update()
        ob = plane()
        base = base_image()
        material = material_with_map("Cloth", base)
        ob.data.materials.append(material)
        t1 = time.time()
        result = detail_maps.add_detail_normal("Plane", "fabric", scale=1.0)
        print("ADD", round(time.time() - t1, 1), "s", result.text, flush=True)
        group, normal_map = detail_maps._ours(material)
        texture = next((n for n in material.node_tree.nodes if n.get(detail_maps.TAG) and n.type == "TEX_IMAGE"), None)
        wired = (group is not None and normal_map is not None and texture is not None
                 and group.inputs["Base"].links[0].from_node.image == base
                 and group.inputs["Detail"].links[0].from_node == texture
                 and normal_map.outputs["Normal"].links[0].to_node.type == "BSDF_PRINCIPLED"
                 and texture.inputs["Vector"].links[0].from_node.type == "MAPPING")
        check("fabric detail wired: base map -> RNM group -> its Normal Map -> BSDF, the detail through UV and Mapping",
              wired and result.ok, result.text)
        image = texture.image if texture else None
        check("the detail is Poly Haven's cotton_jersey nor_gl, Non-Color, packed",
              image is not None and image.get("polyhaven") == "cotton_jersey"
              and image.colorspace_settings.name == "Non-Color" and image.packed_file is not None,
              getattr(image, "name", None))
        check("check_shading still finds the base normal map through the detail",
              shading.normal_maps(ob) == [base.name], shading.normal_maps(ob))
        count = len(material.node_tree.nodes)
        again = detail_maps.add_detail_normal("Plane", "fabric", scale=1.0, strength=0.5)
        check("a second call updates it in place", len(material.node_tree.nodes) == count
              and abs(group.inputs["Strength"].default_value - 0.5) < 1e-6 and "updated" in again.text, again.text)

        # What Blender shows = what the export bakes.
        ours = detail_maps.combined(group)
        baked = cycles_bake(ob, material)
        gap = angles(ours, baked)
        print("CYCLES vs EXPORT BAKE", gap, flush=True)
        check("Cycles' tangent-space bake of the node group equals the export's combined map (8-bit rounding)",
              gap["mean"] < 0.6 and gap["p99"] < 2.0, gap)
        detail_effect = angles(ours, pixels(base))
        check("the detail changes the normal (it is not the base map alone)", detail_effect["mean"] > 1.0,
              detail_effect)

        # Godot: GLB.
        before = tree_state(material)
        exported = engine_export.export_for_engine("godot", ["Plane"], str(folder / "plane.glb"))
        print("GODOT", exported.text, flush=True)
        normal, name = gltf_normal(folder / "plane.glb", folder, "Cloth")
        check("Godot GLB: its normal texture is the combined map",
              exported.ok and normal is not None and angles(normal, ours)["mean"] < 0.6
              and "detail baked into the exported normal map" in exported.text,
              {"name": name, "vs_combined": angles(normal, ours) if normal is not None else None, "text": exported.text})
        check("after the export the material is as it was", tree_state(material) == before
              and not [i for i in bpy.data.images if i.name.endswith("_normal_detail")], tree_state(material))
        # Unreal: FBX, green flipped.
        exported = engine_export.export_for_engine("unreal", ["Plane"], str(folder / "plane.fbx"))
        print("UNREAL", exported.text, flush=True)
        embedded = fbx_normal(folder / "plane.fbx", folder, "normal_detail")
        flipped = ours.copy()
        flipped[..., 1] = 1 - flipped[..., 1]
        check("Unreal FBX: the embedded normal map is the combined map, green flipped",
              exported.ok and embedded is not None and angles(embedded, flipped)["mean"] < 0.6,
              angles(embedded, flipped) if embedded is not None else exported.text)
        check("after the FBX export the material is as it was", tree_state(material) == before, tree_state(material))

        # No normal map of its own; a default scale from the texture's size.
        import bmesh
        mesh = bpy.data.meshes.new("Ball")
        bm = bmesh.new()
        bm.loops.layers.uv.new("UVMap")
        bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=0.9, calc_uvs=True)
        bm.to_mesh(mesh)
        bm.free()
        ball = bpy.data.objects.new("Ball", mesh)
        bpy.context.scene.collection.objects.link(ball)
        plain = material_with_map("Plain")
        mesh.materials.append(plain)
        said = detail_maps.add_detail_normal("Ball", "fabric").text
        group, normal_map = detail_maps._ours(plain)
        scale = json.loads(group[detail_maps.TAG])["scale"] if group else None
        print("BALL", said, flush=True)
        check("without a normal map: a flat base under a new Normal Map node; a default scale from its real size",
              group is not None and normal_map is not None and not group.inputs["Base"].is_linked
              and scale is not None and 3 <= scale <= 40 and "no normal map" in said, {"scale": scale, "said": said})
    except Exception:
        traceback.print_exc()
        check("ran to the end", False, "exception above")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    passed = sum(r["ok"] for r in RESULTS)
    summary = {"passed": passed, "total": len(RESULTS), "seconds": round(time.time() - t0, 1), "results": RESULTS}
    try:
        (Path.cwd() / "detail-maps-check.json").write_text(json.dumps(summary, indent=1))
    except OSError:
        pass
    print(f"DETAIL {passed}/{len(RESULTS)} in {summary['seconds']}s")
    ok = passed == len(RESULTS) and RESULTS
    print("DETAIL OK" if ok else "DETAIL FAIL", flush=True)
    return 0 if ok else 1


def run_and_exit() -> None:
    code = 1
    try:
        windows = bpy.context.window_manager.windows
        if windows:
            with bpy.context.temp_override(window=windows[0], screen=windows[0].screen):
                code = main()
        else:
            code = main()
    except BaseException:
        traceback.print_exc()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)


if __name__ == "__main__":
    watchdog = threading.Timer(TIMEOUT, lambda: (print(f"DETAIL FAIL: timed out after {TIMEOUT}s", flush=True),
                                                  os._exit(3)))
    watchdog.daemon = True
    watchdog.start()
    if bpy.app.background:
        run_and_exit()
    else:
        bpy.app.timers.register(run_and_exit, first_interval=0.5)
