---
id: local/geometry-nodes
version: 1
title: Geometry nodes in Blender 5.2
when: geometry nodes, scattering or instancing objects over a surface
roles: [single, workspace]
requires_tools: []
tier: free
kind: assist
part: single
words: geometry.?node|scatter|instanc|distribute
---
Checked in Blender 5.2.2 (verify_facts5.py).
- Tree: `tree = bpy.data.node_groups.new("Scatter", "GeometryNodeTree")`; `tree.nodes` is read-only, add with `tree.nodes.new(...)`. Sockets: `tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")` and the same with "OUTPUT"; then add `NodeGroupInput` and `NodeGroupOutput` nodes. Attach: `obj.modifiers.new("Scatter", "NODES").node_group = tree`.
- Geometry sockets have no `default_value`; they are only linked.
- Scatter: `GeometryNodeDistributePointsOnFaces` (inputs "Mesh", "Selection", "Distance Min", "Density Max", "Density", "Density Factor", "Seed" (an int); `distribute_method` "RANDOM" or "POISSON") -> `GeometryNodeInstanceOnPoints` (inputs "Points", "Selection", "Instance", "Pick Instance", "Instance Index", "Rotation", "Scale"). The thing to instance comes from `GeometryNodeObjectInfo`: `info.inputs["Object"].default_value = obj`, output "Geometry" into "Instance".
- Count: RANDOM gives about Density x surface area points (on a flat plane exactly: Density 2 on a 10 x 10 m plane = 200, for any seed). For N instances set Density = N / area.
- Keep the surface visible: `GeometryNodeJoinGeometry` with both the group input geometry and the instances, into the group output.
- Check the result without realizing it: `g = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).evaluated_geometry()`; `len(g.instances_pointcloud().points)` is the instance count, `g.mesh` the surface that is not instanced. `depsgraph.object_instances` items are only valid inside the loop that yields them.
- `GeometryNodeRotateRotation` is undefined here; rotate instances through Instance on Points' "Rotation" input.
