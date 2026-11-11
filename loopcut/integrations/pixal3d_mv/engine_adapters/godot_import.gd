extends SceneTree

var errors: Array[String] = []
var metrics: Array[Dictionary] = []

func mesh_nodes(node: Node) -> Array[MeshInstance3D]:
	var result: Array[MeshInstance3D] = []
	if node is MeshInstance3D:
		result.append(node)
	for child in node.get_children():
		result.append_array(mesh_nodes(child))
	return result

func own_recursive(node: Node, owner_node: Node) -> void:
	for child in node.get_children():
		child.owner = owner_node
		own_recursive(child, owner_node)

func _initialize() -> void:
	var manifest = JSON.parse_string(FileAccess.get_file_as_string("res://manifest.json"))
	if not manifest is Dictionary:
		push_error("Invalid asset manifest")
		quit(1)
		return
	var asset = Node3D.new()
	asset.name = manifest.name
	root.add_child(asset)
	var longest: float = 0.0
	for lod in manifest.lods:
		var path = "res://assets/" + String(lod.files.glb).get_file()
		var packed = load(path) as PackedScene
		if packed == null:
			errors.append("Cannot load " + path)
			continue
		var instance = packed.instantiate()
		asset.add_child(instance)
		var triangles: int = 0
		var vertices: int = 0
		for mesh_node in mesh_nodes(instance):
			var box = mesh_node.mesh.get_aabb()
			longest = max(longest, box.size.length())
			mesh_node.visibility_range_begin = 0 if int(lod.index) == 0 else longest * 4 * pow(2, int(lod.index) - 1)
			mesh_node.visibility_range_end = 0 if int(lod.index) == manifest.lods.size() - 1 else longest * 4 * pow(2, int(lod.index))
			for surface in mesh_node.mesh.get_surface_count():
				var arrays = mesh_node.mesh.surface_get_arrays(surface)
				var positions = arrays[Mesh.ARRAY_VERTEX]
				vertices += positions.size()
				var indices = arrays[Mesh.ARRAY_INDEX]
				triangles += indices.size() / 3 if indices != null and indices.size() > 0 else positions.size() / 3
				if arrays[Mesh.ARRAY_NORMAL] == null or arrays[Mesh.ARRAY_NORMAL].size() != positions.size():
					errors.append("Missing normals on LOD" + str(lod.index))
				if arrays[Mesh.ARRAY_TEX_UV] == null or arrays[Mesh.ARRAY_TEX_UV].size() != positions.size():
					errors.append("Missing UV0 on LOD" + str(lod.index))
				if arrays[Mesh.ARRAY_TANGENT] == null or arrays[Mesh.ARRAY_TANGENT].size() != positions.size() * 4:
					errors.append("Missing tangents on LOD" + str(lod.index))
				var material = mesh_node.get_active_material(surface)
				if material == null or not material is BaseMaterial3D or material.albedo_texture == null:
					errors.append("Missing material/base texture on LOD" + str(lod.index))
		if triangles != int(lod.export_metrics.triangles):
			errors.append("Triangle count mismatch on LOD" + str(lod.index))
		metrics.append({"lod": lod.index, "triangles": triangles, "vertices": vertices})
	var collision_count: int = 0
	if manifest.collision.has("glb"):
		var collision = load("res://assets/" + String(manifest.collision.glb).get_file()) as PackedScene
		if collision == null:
			errors.append("Collision scene did not import")
		else:
			var instance = collision.instantiate()
			asset.add_child(instance)
			var body = StaticBody3D.new()
			body.name = "Collision"
			asset.add_child(body)
			for node in mesh_nodes(instance):
				var points = PackedVector3Array()
				for surface in node.mesh.get_surface_count():
					points.append_array(node.mesh.surface_get_arrays(surface)[Mesh.ARRAY_VERTEX])
				var shape = ConvexPolygonShape3D.new()
				shape.points = points
				var collider = CollisionShape3D.new()
				collider.shape = shape
				body.add_child(collider)
				collider.global_transform = node.global_transform
				collision_count += 1
			asset.remove_child(instance)
			instance.free()
	if collision_count != int(manifest.collision.hulls):
		errors.append("Collision hull count mismatch")
	own_recursive(asset, asset)
	var output_scene = PackedScene.new()
	if output_scene.pack(asset) != OK or ResourceSaver.save(output_scene, "res://asset.tscn") != OK:
		errors.append("Failed to save import-tested asset scene")
	var result = {"status": "passed" if errors.is_empty() else "failed", "engine": "godot",
		"version": Engine.get_version_info().string, "lods": metrics, "collision_hulls": collision_count,
		"errors": errors, "scene": "engine_check/asset.tscn"}
	var file = FileAccess.open("res://result.json", FileAccess.WRITE)
	file.store_string(JSON.stringify(result, "  "))
	quit(0 if errors.is_empty() else 1)
