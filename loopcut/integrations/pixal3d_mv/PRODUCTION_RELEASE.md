# Seven-stage production update — 2026-10-27

## REDengine target update

Cyberpunk 2077 (`--engine cyberpunk`) and The Witcher 3 Remastered (`--engine witcher3`) are now explicit primary targets. Generic, Godot, Unity and Unreal support is retained. See REDENGINE.md for commands and native-tool integration limits.

The update adds a checked WolvenKit GLB handoff, conservative per-submesh vertex limits, REDkit FBX preparation, separate render/collision exports, source-pivot defaults and explicit per-LOD material handoffs. Native resources and cooked mods are not generated; native import status remains not_run.

Current regression validation: **44 checks passed** (24 Python, nine production Blender, 11 legacy Blender), plus lint. Both RED target builds were exercised. The medallion's material-free Cyberpunk GLB preserves all 39,985 triangles in two chunks with 36,617 and 32,782 exported vertices. Its separate Witcher FBX includes tangents and excludes collision meshes. Native WolvenKit/REDkit/game validation remains outstanding. Supplemental reference exports are included under examples/production_medallion/redengine_reference; the original portable sample and Godot evidence remain intact.

## Earlier production validation

The pipeline now adds component-aware cleanup, matched high-to-low PBR/normal/AO baking, independent validated UV atlases per LOD, asset budgets, LOD generation, collision and engine presets, and visual/import quality gates.

`pa.py asset` builds existing models. `mesh` and `all` now run production automatically. See GAME_ASSET_PIPELINE.md for configuration and limitations. The legacy `prep` and `fix` commands remain available.

Validation: **40 regression checks passed** (22 Python, 7 production Blender, 11 legacy Blender), plus lint checks. The medallion's GLBs passed an actual Godot 4.7.2 import/re-import. FBX tangent layers were checked. Blender version: 5.2.3 LTS.

| Mesh | Triangles | Exported vertices |
|---|---:|---:|
| LOD0 | 39,985 | 69,333 |
| LOD1 | 19,699 | 36,948 |
| LOD2 | 9,457 | 19,047 |
| LOD3 | 5,060 | 11,414 |

The sample has zero detected degenerate faces, invalid/collapsed UV faces or UV overlap pairs in all four LODs. It includes 16 convex collision hulls, per-LOD PBR maps, editable Blender data, turntable frames, a portable report and an offline review page.

The sample remains **review required**: its source has no final materials; UV occupancy and subpixel islands need attention; some open edges remain; generated surface folds need artistic review. This update does not certify shipping assets. Unreal/Unity adapters are included; those editors were not available for real import validation here.

Regression logs include deliberately rejected inputs. Their final suite status is the test result.
