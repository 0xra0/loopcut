# Model from a reference picture

> When: the user gives a picture (a turnaround sheet, concept art, a photo of an object or character) and wants a 3D model that matches it, usually rigged and exported. Ends with a review of what's missing.

## 0. Read the picture first

Look at it before calling any tool, and write down:

- which views it shows (front, side, back) and roughly where each sits, for `crop`;
- the subject's real height (default 1.8 m for an adult human figure);
- the rest pose (T-pose or A-pose) and the arm angle;
- which parts are simple shapes (a sphere head, cylinder limbs, a box) that are better rebuilt as primitives;
- the materials and colours.

If the picture is a real, identifiable person, check that the user is that person or has their consent before making a likeness. Otherwise offer a character inspired by it instead.

## 1. Set up the references

- `set_reference(image_path, view="front", height=..., crop=[...])` for every view in the picture. In a side view where the character faces picture-left, use `view="right"`.
- Check what comes back:
  - `cut_off_at_edge` should be empty for full-body work.
  - `subject_size_m` should be plausible.
  - In the profile, the head row should come out at about head width.
- If the background stays attached, raise tolerance to 0.1-0.15. If thin parts go missing, lower it.

## Quality bar

The user judges likeness by eye, from every angle, not by numbers. Work to these rules:

- **Model it like a 3D artist.** Build by hand: blockout, then forms, then detail. Every distinct piece in the picture gets its own part: plates, joint discs, collars, boots, fingers. Don't use `image_to_3d` unless the user asks for AI generation.
- **Detail all the time.** If you can see it in the picture, model it: panel seams, bevels and edge highlights, gaps between plates, fasteners, finger joints, and the sole and tread of boots. When in doubt, add the detail.
- **Real textures.** Use PBR materials per region: base colour, roughness and normal, with panel lines, subtle colour variation and edge wear baked in. Never ship flat placeholder colours or colour projected from the picture.
- **Outline match is not likeness.** `silhouette_iou` only checks the outline. A blob can score 0.96 and look nothing like the picture. Always also compare `capture_views` at `iso` (3/4) and side angles with the picture, and fix what the eye notices.

## 2. Blockout

Build the proportions from primitives: `create_primitive`, `transform`, `mesh_edit`, `add_modifier`.

- Take sizes from the profile `set_reference` returns, and from `reference_to_world(view, points=[...])` on landmarks: shoulder, elbow, wrist, hip, knee, ankle, chin.
- Start with one part per body segment. Mirror with a Mirror modifier and model only one side.
- Check: `compare_to_reference` in every view, outline match ≥ 0.90. Then `capture_views(views=["front", "right", "iso"])` next to the picture. Fix proportions before adding any detail.

(`silhouette_mesh` gives a quick outline hull to model over or measure against. It is a guide, not the model.)

## 3. Forms and detail

Turn each blockout part into the real piece, part by part, comparing each with the picture as you go.

- **Hard surface (armor, robots, props):**
  - Shape the plates with `mesh_edit` (extrude, inset, bevel), boolean cuts, and Bevel and Solidify modifiers. Follow `hard_surface_prop`.
  - Keep plates as separate pieces with real gaps between them, and leave the dark under-suit visible where the picture shows it.
  - Model panel lines as insets or cuts, not paint.
- **Organic forms (muscles, cloth, faces):** sculpt on a dense copy, following `detailed_sculpt`.
- **Hands:** model fingers with knuckle segments, matching the picture's pose.
- Check at every part:
  - `capture_views` close up, plus `iso`, next to the picture.
  - Name every feature visible in the picture that the part still lacks, and keep going until there are none worth adding.
  - The outline match should stay ≥ 0.95.

## 4. Materials and textures

- Make one PBR material per surface type the picture shows. For example: orange painted armor (roughness about 0.45), dark rubber or under-suit (about 0.7), glossy head (about 0.25), and metal fasteners.
  - Use `create_material`, then `assign_material(..., select=...)` per part or face set.
- Add texture detail:
  - panel lines and plate gaps in the normal map, by baking from the modeled detail (`bake_maps`);
  - subtle colour variation;
  - edge wear on plate edges (lighter, rougher);
  - grime in the creases (an AO bake multiplied in).
- Check: render `capture_views(shading="material")` in `iso` and compare its colours with the picture. Hue and value should read the same at a glance.

## 5. Game mesh, UVs, bake

Follow `retopo_and_bake`. Use `bake_maps(low, high, maps=["DIFFUSE", "NORMAL"])` when the dense mesh is painted.

## 6. Rig and animate

Follow `rig_and_animate`.

- Measure the arm angle from the picture: `reference_to_world(view="front", points=[shoulder, wrist])`, then angle = atan(drop / reach).
- `rig_template(fit_to=..., arm_angle=...)`.
- Check the joints with `capture_views(objects=[mesh, rig], shading="xray")` and move legs and arms to the limbs' centres with `edit_bones(...)`.

## 7. Preview export

Follow `export_to_engine`. Export a preview as soon as the rig works, so the user can drop it into the engine early. Use `export_model(path="<Name>_preview.fbx", preset="unreal")` (or unity), then run `reimport_check(...)`.

## 8. Review: what's missing and what to improve

End with a short review the user can act on. Write it from the evidence, not from memory:

1. **Side by side from every angle.** Front, side, back and 3/4 renders next to the picture. Say honestly whether it reads as the same character at a glance. Outline numbers come second and never stand in for this.
   Also report triangles, bones, clips and texture size.
2. **Largest differences**, with heights and sizes (for example "upper arms 2.5 cm too wide at z = 1.1 m").
3. **What's missing.** Name each feature you can see in the picture but not in the model: panel lines, fingers, separate pieces, accessories, true materials. Look at the side-by-side images for this.
4. **What would improve it most, in order.** Concrete next steps with the tools that would do them, and what each would fix.

Save the review next to the exported files when there's a place for it, and keep the comparison images so the user can see the evidence.
