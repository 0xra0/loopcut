# Detailed sculpt

> When: organic high-poly work such as a bust, creature, character body, statue or rock. Also stage 2 of `game_character`.

## How the brushes work

`sculpt_stroke(...)` moves vertices along a path of world-space points. It doesn't use Blender's interactive sculpt mode, so it behaves the same with or without a viewport. Points must lie on or just above the surface. Get them with `surface_points(...)`:

- `surface_points(name, view="front", coords=[[0.4, 0.8], [0.6, 0.8]])` returns where those spots of the front view land on the surface. u runs left to right and v bottom to top across the object's bounding box. Use it to lay a stroke across a forehead, a brow or a spine.
- `surface_points(name, points=[[x, y, z], ...])` snaps rough positions onto the closest surface.

It also returns the normal at each point. For a stroke that should stand off the surface, add a little of the normal to each point.

| Brush | Use for |
| --- | --- |
| clay | Building up muscle masses and planes in flat layers |
| draw / blob | Raising ridges and bumps. Use invert to carve |
| inflate | Swelling a volume (cheeks, bellies, fingers) |
| grab | Moving a whole region by `delta` (proportion fixes) |
| crease | Sharp grooves: lips, eyelids, wrinkles, panel lines on armor |
| pinch | Tightening a crease or edge |
| flatten | Planes on rocks, forehead and cheekbone planes |
| smooth | Cleaning bumps after other brushes |

`symmetry="X"` mirrors every stroke across the object's local X. Keep it on for anything symmetric, and set it to `""` for asymmetric detail at the end.

## Stages

### 1. Prepare the mesh

- `checkpoint(...)`, then `sculpt_prepare(name, target_faces=...)`. Use 50k for big forms. Raise it to 200k-500k with a second `sculpt_prepare` or `voxel_remesh` once the forms are right. Detail needs density: a crease narrower than about 3 faces won't show.
- Check: `mesh_report(...)` shows the expected faces, and the mesh is closed (0 boundary edges).

### 2. Primary forms (large radius, low strength)

- radius about 15-30% of the object size, strength 0.2-0.4, mostly clay, grab and inflate.
- Look after every 3-5 strokes: `capture_views(views=["front","right","iso"])`.
- Check: the silhouette reads correctly from front and side, and so would a black shadow of it.

### 3. Secondary forms (medium radius)

- radius 5-10%, strength 0.3-0.5: muscle groups, eye sockets, nose, brow, knuckles, armor plates.
- `smooth_mesh(name, iterations=2)` or a smooth stroke to clean between passes.
- Check: closer views (`capture_views(objects=[name], resolution=768)`) show each form separately, without lumps.

### 4. Tertiary detail (small radius)

- Raise the density first if needed (stage 1). radius 1-3%, strength 0.5-0.8: crease for wrinkles, draw with invert for pores and scratches.
- Use `surface_noise(...)` for even texture over large areas (skin pores: VORONOI, scale 0.01-0.03, strength 0.001-0.003; rock: CLOUDS or MUSGRAVE).
- Check: detail is visible in a 768 px render and doesn't overpower the forms.

### 5. Finish

- `symmetrize(...)` if one side drifted. Don't do this after asymmetric detail.
- `mesh_report(...)`. Keep this mesh as `<Name>_high`. If it will go into a game, continue with `retopo_and_bake`.

## Tips

- Strength above 0.8 with a large radius blows up the mesh. Use several gentle strokes instead.
- For a face, place points from known landmarks, picked with `surface_points(name, view="front", ...)`: eyes at about half the head height, nose base at about 2/3 of the way down from the eyes to the chin, mouth a third of the way from nose to chin.
- If a stroke spikes or tears the mesh, `rollback(...)` rather than smoothing over it.
